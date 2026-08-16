#!/usr/bin/env python
"""hoseID review — tag trail-cam images that haven't been human-reviewed.

Run:  ~/venvs/megadetector/bin/uvicorn app:app --host 0.0.0.0 --port 8870
from this directory.

Reads MegaDetector results (sidecar JSON), serves originals read-only from the
immutable archive, and stores human tags in ~/trailcam/tags/tags.db.
The reviews table is insert-only; the latest row per image basename wins.

Exclusion zones are the ONE lossy layer in the pipeline, so they carry guards:
  - zones are versioned by camera deployment epoch; "camera moved" bumps the
    epoch, deactivating the old set instead of letting it apply to new framing
  - unions that would grow a zone past MAX_ZONE_AREA_FRAC of the frame are
    refused unless explicitly confirmed
  - every zone mutation lands in the append-only zone_log
  - suppressed detections stay countable via /api/suppressed
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ASSETS = Path("/Users/hosebot/trailcam/landing/assets")
MD_RESULTS = Path(
    "/Users/hosebot/trailcam/derived/runs/2026-08-16-md-combined/md_results.json"
)
DB_PATH = Path("/Users/hosebot/trailcam/tags/tags.db")
MOVE_FLAGS = Path("/Users/hosebot/trailcam/derived/camera-move-flags.json")
STATIC = Path(__file__).parent / "static"

MAX_ZONE_AREA_FRAC = 0.25   # a single zone larger than this needs confirm

# (tag, hotkey); hotkeys must not collide with UI keys a/x/n/z/space/enter/arrows.
# "empty" and "unsure" are exclusive flags; every other tag carries a count.
PRESET_TAGS = [
    ("empty", "e"), ("deer", "d"), ("bear", "b"), ("mountain-lion", "m"),
    ("coyote", "c"), ("bobcat", "w"), ("fox", "f"), ("skunk", "k"),
    ("turkey", "t"), ("jackrabbit", "j"), ("squirrel", "s"), ("bird", "i"),
    ("other-animal", "o"), ("person", "p"), ("vehicle", "v"), ("unsure", "u"),
]

FNAME_RE = re.compile(
    r"(?P<device>\d{15})-\d+-\d+-"
    r"(?P<mm>\d{2})(?P<dd>\d{2})(?P<yyyy>\d{4})(?P<hh>\d{2})(?P<mi>\d{2})(?P<ss>\d{2})-"
    r"[A-Z]+\d+\.jpg$"
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY,
                basename TEXT NOT NULL,
                image TEXT NOT NULL,          -- path relative to landing/assets
                device_id TEXT,
                captured_at TEXT,             -- ISO local, parsed from filename
                tags TEXT NOT NULL,           -- JSON array of strings
                notes TEXT NOT NULL DEFAULT '',
                md_max_conf REAL,
                reviewed_at TEXT NOT NULL,    -- ISO UTC
                reviewer TEXT NOT NULL DEFAULT 'p'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_basename ON reviews(basename)"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "counts" not in cols:
            # JSON object tag -> count; a tag absent here counts as 1
            conn.execute(
                "ALTER TABLE reviews ADD COLUMN counts TEXT NOT NULL DEFAULT '{}'"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exclusion_zones (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                epoch INTEGER NOT NULL DEFAULT 1,
                category TEXT NOT NULL,       -- MD category: '1'|'2'|'3'
                x REAL NOT NULL, y REAL NOT NULL,
                w REAL NOT NULL, h REAL NOT NULL,   -- normalized, like MD bboxes
                source_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        zcols = {r[1] for r in conn.execute("PRAGMA table_info(exclusion_zones)")}
        if "epoch" not in zcols:
            conn.execute(
                "ALTER TABLE exclusion_zones ADD COLUMN epoch INTEGER NOT NULL DEFAULT 1"
            )
        # Deployment epochs: bumping deactivates a camera's zone set wholesale.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS camera_epochs (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        # Append-only audit of every zone mutation.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS zone_log (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,         -- add|merge|delete|epoch_bump
                detail TEXT NOT NULL          -- JSON
            )
        """)


def load_images():
    """basename -> image record, from the MD results sidecar."""
    results = json.loads(MD_RESULTS.read_text())
    images = {}
    for im in results["images"]:
        rel = im["file"]
        name = Path(rel).name
        m = FNAME_RE.search(name)
        meta = m.groupdict() if m else None
        captured = (
            f"{meta['yyyy']}-{meta['mm']}-{meta['dd']}"
            f"T{meta['hh']}:{meta['mi']}:{meta['ss']}" if meta else None
        )
        dets = im.get("detections") or []
        images[name] = {
            "basename": name,
            "image": rel,
            "device_id": meta["device"] if meta else None,
            "captured_at": captured,
            "detections": dets,
            "md_max_conf": max((d["conf"] for d in dets), default=0.0),
        }
    return images


IMAGES = load_images()
init_db()

app = FastAPI(title="hoseID review")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_zone(conn, device_id, action, detail):
    conn.execute(
        "INSERT INTO zone_log (ts, device_id, action, detail) VALUES (?,?,?,?)",
        (now_iso(), device_id, action, json.dumps(detail)),
    )


def current_epoch(conn, device_id):
    row = conn.execute(
        "SELECT MAX(epoch) FROM camera_epochs WHERE device_id=?", (device_id,)
    ).fetchone()
    return row[0] or 1


def active_zones(conn, device_id=None):
    """Zones belonging to each device's current epoch."""
    zones = [dict(r) for r in conn.execute(
        "SELECT * FROM exclusion_zones" +
        (" WHERE device_id=?" if device_id else ""),
        (device_id,) if device_id else (),
    )]
    epochs = {}
    out = []
    for z in zones:
        d = z["device_id"]
        if d not in epochs:
            epochs[d] = current_epoch(conn, d)
        if z["epoch"] == epochs[d]:
            out.append(z)
    return out


def latest_reviews(conn):
    rows = conn.execute("""
        SELECT r.* FROM reviews r
        JOIN (SELECT basename, MAX(id) AS id FROM reviews GROUP BY basename) m
          ON r.id = m.id
    """).fetchall()
    return {r["basename"]: r for r in rows}


def _intersects(a, b):
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _union(a, b):
    x1, y1 = min(a[0], b[0]), min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    return [x1, y1, x2 - x1, y2 - y1]


def _excluded(det, zones):
    """Center test: a detection is excluded when its center falls inside an
    active zone of the same category."""
    x, y, w, h = det["bbox"]
    cx, cy = x + w / 2, y + h / 2
    return any(
        z["category"] == det["category"]
        and z["x"] <= cx <= z["x"] + z["w"] and z["y"] <= cy <= z["y"] + z["h"]
        for z in zones
    )


class ReviewIn(BaseModel):
    basename: str
    tags: list[str]
    counts: dict[str, int] = {}   # tag -> count, only for counts > 1
    notes: str = ""


class ZoneBox(BaseModel):
    category: str                 # MD category code
    bbox: list[float]             # [x, y, w, h] normalized


class ZonesIn(BaseModel):
    device_id: str
    boxes: list[ZoneBox]
    confirm: bool = False         # required when a union exceeds the area cap


class EpochIn(BaseModel):
    device_id: str
    reason: str = ""


@app.get("/api/config")
def config():
    devices = sorted({im["device_id"] for im in IMAGES.values() if im["device_id"]})
    move_flags = (
        json.loads(MOVE_FLAGS.read_text()) if MOVE_FLAGS.exists() else {}
    )
    with db() as conn:
        epochs = {d: current_epoch(conn, d) for d in devices}
    return {
        "preset_tags": [{"tag": t, "key": k} for t, k in PRESET_TAGS],
        "no_count_tags": ["empty", "unsure"],
        "devices": devices,
        "epochs": epochs,
        "max_zone_area_frac": MAX_ZONE_AREA_FRAC,
        "move_flags": move_flags,
    }


@app.get("/api/zones")
def zones(device: str = ""):
    with db() as conn:
        return {"zones": active_zones(conn, device or None)}


@app.post("/api/zones")
def add_zones(z: ZonesIn):
    now = now_iso()
    added, rejected = [], []
    with db() as conn:
        epoch = current_epoch(conn, z.device_id)
        for box in z.boxes:
            merged = list(box.bbox)
            sources = 1
            absorbed = []
            changed = True
            while changed:
                changed = False
                for row in conn.execute(
                    "SELECT * FROM exclusion_zones"
                    " WHERE device_id=? AND category=? AND epoch=?",
                    (z.device_id, box.category, epoch),
                ).fetchall():
                    zr = [row["x"], row["y"], row["w"], row["h"]]
                    if row["id"] not in [a["id"] for a in absorbed] \
                            and _intersects(merged, zr):
                        merged = _union(merged, zr)
                        sources += row["source_count"]
                        absorbed.append(dict(row))
                        changed = True
            area = merged[2] * merged[3]
            if area > MAX_ZONE_AREA_FRAC and not z.confirm:
                # refuse silent growth past the cap; client must confirm
                rejected.append({
                    "category": box.category, "bbox": box.bbox,
                    "merged_bbox": merged, "merged_area": round(area, 3),
                })
                continue
            for a in absorbed:
                conn.execute("DELETE FROM exclusion_zones WHERE id=?", (a["id"],))
            conn.execute(
                "INSERT INTO exclusion_zones"
                " (device_id, epoch, category, x, y, w, h, source_count,"
                "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (z.device_id, epoch, box.category, *merged, sources, now, now),
            )
            log_zone(conn, z.device_id, "merge" if absorbed else "add", {
                "epoch": epoch, "category": box.category, "box": box.bbox,
                "absorbed": [
                    {k: a[k] for k in ("id", "x", "y", "w", "h")} for a in absorbed
                ],
                "result": merged, "area": round(area, 3),
                "confirmed_oversize": area > MAX_ZONE_AREA_FRAC,
            })
            added.append({"category": box.category, "bbox": merged})
        result = active_zones(conn, z.device_id)
    return {"ok": True, "added": added, "rejected": rejected, "zones": result}


@app.delete("/api/zones/{zone_id}")
def delete_zone(zone_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM exclusion_zones WHERE id=?", (zone_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("DELETE FROM exclusion_zones WHERE id=?", (zone_id,))
        log_zone(conn, row["device_id"], "delete",
                 {k: row[k] for k in ("id", "epoch", "category", "x", "y", "w", "h")})
    return {"ok": True}


@app.post("/api/epoch")
def bump_epoch(e: EpochIn):
    """'I moved this camera': deactivate its zone set by starting a new epoch."""
    with db() as conn:
        old = current_epoch(conn, e.device_id)
        n_zones = conn.execute(
            "SELECT COUNT(*) FROM exclusion_zones WHERE device_id=? AND epoch=?",
            (e.device_id, old),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO camera_epochs (device_id, epoch, reason, created_at)"
            " VALUES (?,?,?,?)",
            (e.device_id, old + 1, e.reason, now_iso()),
        )
        log_zone(conn, e.device_id, "epoch_bump",
                 {"from": old, "to": old + 1, "reason": e.reason,
                  "zones_deactivated": n_zones})
    return {"ok": True, "epoch": old + 1, "zones_deactivated": n_zones}


@app.get("/api/suppressed")
def suppressed(device: str = "", since: str = "", until: str = ""):
    """What is exclusion dropping? Counts detections whose center falls in an
    ACTIVE zone, over images captured in [since, until] (ISO date prefixes)."""
    with db() as conn:
        zones_all = active_zones(conn, device or None)
    by_dev = {}
    for z in zones_all:
        by_dev.setdefault(z["device_id"], []).append(z)
    total, by_cat, by_month = 0, {}, {}
    for im in IMAGES.values():
        if device and im["device_id"] != device:
            continue
        cap = im["captured_at"] or ""
        if since and cap < since:
            continue
        if until and cap > until + "￿":
            continue
        dev_zones = by_dev.get(im["device_id"], [])
        if not dev_zones:
            continue
        for d in im["detections"]:
            if _excluded(d, dev_zones):
                total += 1
                by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
                mo = cap[:7]
                by_month[mo] = by_month.get(mo, 0) + 1
    return {"total": total, "by_category": by_cat,
            "by_month": dict(sorted(by_month.items()))}


@app.get("/api/queue")
def queue(status: str = "unreviewed", device: str = "", sort: str = "conf"):
    with db() as conn:
        reviewed = latest_reviews(conn)
        zones_all = active_zones(conn)
    by_dev = {}
    for z in zones_all:
        by_dev.setdefault(z["device_id"], []).append(z)
    items = []
    for im in IMAGES.values():
        if device and im["device_id"] != device:
            continue
        r = reviewed.get(im["basename"])
        if status == "unreviewed" and r:
            continue
        if status == "reviewed" and not r:
            continue
        item = dict(im)
        dev_zones = by_dev.get(im["device_id"], [])
        dets = [dict(d, excluded=_excluded(d, dev_zones)) for d in im["detections"]]
        item["detections"] = dets
        item["md_max_conf_eff"] = max(
            (d["conf"] for d in dets if not d["excluded"]), default=0.0
        )
        item["review"] = (
            {"tags": json.loads(r["tags"]), "counts": json.loads(r["counts"]),
             "notes": r["notes"], "reviewed_at": r["reviewed_at"]} if r else None
        )
        items.append(item)
    if sort == "conf":
        items.sort(key=lambda i: -i["md_max_conf_eff"])
    elif sort == "time_asc":
        items.sort(key=lambda i: i["captured_at"] or "")
    else:
        items.sort(key=lambda i: i["captured_at"] or "", reverse=True)
    return {"count": len(items), "items": items}


@app.get("/api/stats")
def stats():
    with db() as conn:
        reviewed = latest_reviews(conn)
    per_tag, individuals = {}, {}
    for r in reviewed.values():
        counts = json.loads(r["counts"])
        for t in json.loads(r["tags"]):
            per_tag[t] = per_tag.get(t, 0) + 1
            individuals[t] = individuals.get(t, 0) + counts.get(t, 1)
    return {
        "total": len(IMAGES),
        "reviewed": len(reviewed),
        "unreviewed": len(IMAGES) - len(reviewed),
        "per_tag": dict(sorted(per_tag.items(), key=lambda kv: -kv[1])),
        "individuals": dict(sorted(individuals.items(), key=lambda kv: -kv[1])),
    }


@app.post("/api/review")
def review(r: ReviewIn):
    im = IMAGES.get(r.basename)
    if not im:
        raise HTTPException(404, f"unknown image {r.basename}")
    if not r.tags:
        raise HTTPException(400, "at least one tag required")
    counts = {t: c for t, c in r.counts.items() if c > 1}
    if bad := set(counts) - set(r.tags):
        raise HTTPException(400, f"counts for untagged: {sorted(bad)}")
    with db() as conn:
        conn.execute(
            "INSERT INTO reviews (basename, image, device_id, captured_at,"
            " tags, counts, notes, md_max_conf, reviewed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (im["basename"], im["image"], im["device_id"], im["captured_at"],
             json.dumps(sorted(r.tags)), json.dumps(counts), r.notes.strip(),
             im["md_max_conf"], now_iso()),
        )
    return {"ok": True}


@app.get("/images/{rel:path}")
def image(rel: str):
    path = (ASSETS / rel).resolve()
    if not path.is_relative_to(ASSETS) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
