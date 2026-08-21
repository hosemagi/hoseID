#!/usr/bin/env python
"""hoseID review — tag trail-cam images that haven't been human-reviewed.

Run:  ~/venvs/megadetector/bin/uvicorn app:app --host 0.0.0.0 --port 8870
from this directory.

Reads pipeline output from detections.db, serves originals read-only from the
immutable archive, and stores human verdicts in ~/trailcam/tags/tags.db.
The reviews table is insert-only; the latest row per ASSET wins.

Identity is `asset_id`, the content hash — not the filename (2026-08-20). It used
to be the basename, and there were two populations with two naming schemes: a bulk
Reveal export reviewed in place under vendor filenames, and pipeline captures named
by content hash. When the export was finally ingested, every one of those 1,726
captures existed under both names, so the queue offered each of them a second time
as though it had never been reviewed. Filenames were never identity; the same bytes
had two of them. The hash is the same for both, so a verdict recorded under either
name now attaches to the capture itself, and grouping by asset_id means the newest
verdict wins regardless of which name it happened to be recorded under.

That is also why the MegaDetector-results sidecar is gone as an image source: the
export is in the landing zone now, so the pipeline is the single source of captures
and its per-detection taxa replace the separate speciesnet predictions file.

Exclusion zones are the ONE lossy layer in the pipeline, so they carry guards:
  - zones are versioned by camera deployment epoch; "camera moved" bumps the
    epoch, deactivating the old set instead of letting it apply to new framing
  - unions that would grow a zone past MAX_ZONE_AREA_FRAC of the frame are
    refused unless explicitly confirmed
  - every zone mutation lands in the append-only zone_log
  - suppressed detections stay countable via /api/suppressed
"""

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ASSETS = Path("/Users/hosebot/trailcam/landing/assets")
SIDECARS = Path("/Users/hosebot/trailcam/landing/sidecars")
DETECTIONS_DB = Path("/Users/hosebot/trailcam/derived/detections.db")
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
    ("domestic-dog", "g"), ("other-animal", "o"), ("person", "p"),
    ("vehicle", "v"), ("unsure", "u"),
]


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
                asset_id TEXT,                -- content hash; THE identity (see module docstring)
                basename TEXT NOT NULL,       -- filename at review time; provenance, not identity
                image TEXT NOT NULL,          -- path relative to landing/assets
                device_id TEXT,
                captured_at TEXT,             -- ISO local
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
        if "asset_id" not in cols:
            # Also added by `hoseid backfill-reviews`, which fills it for existing
            # rows. Created here too so a fresh database has it from the start.
            conn.execute("ALTER TABLE reviews ADD COLUMN asset_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_asset ON reviews(asset_id)"
        )
        if "counts" not in cols:
            # JSON object tag -> count; a tag absent here counts as 1
            conn.execute(
                "ALTER TABLE reviews ADD COLUMN counts TEXT NOT NULL DEFAULT '{}'"
            )
        if "individual" not in cols:
            # named-animal attribution at review time (Al/Ben/Boar...);
            # confidence is mandatory whenever individual is set
            conn.execute("ALTER TABLE reviews ADD COLUMN individual TEXT")
            conn.execute("ALTER TABLE reviews ADD COLUMN"
                         " individual_confidence TEXT")
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


# --- pipeline captures (fetch daemon -> landing zone -> nightly hoseid run) ---
# Read live from detections.db, cached on its mtime, so new nightly output
# appears in the review queue without an app restart.
_CAT_CODE = {"animal": "1", "person": "2", "vehicle": "3"}
_LOCAL_TZ = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
_pipeline_cache = {"mtime": None, "items": {}}


def _sidecar_meta(digest: str) -> dict:
    p = SIDECARS / digest[:2] / digest[2:4] / f"{digest}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def pipeline_images() -> dict:
    if not DETECTIONS_DB.exists():
        return {}
    mtime = DETECTIONS_DB.stat().st_mtime
    if _pipeline_cache["mtime"] == mtime:
        return _pipeline_cache["items"]
    conn = sqlite3.connect(DETECTIONS_DB)
    conn.row_factory = sqlite3.Row
    # latest capture row per asset (a standing run_id keeps this to one row,
    # but re-runs under new ids must not duplicate queue entries)
    caps = {r["asset_id"]: r for r in conn.execute(
        "SELECT * FROM captures ORDER BY rowid")}
    dets_by_asset = {}
    for d in conn.execute("SELECT * FROM detections ORDER BY detection_id"):
        dets_by_asset.setdefault((d["asset_id"], d["run_id"]), []).append(d)
    conn.close()

    items = {}
    for asset_id, cap in caps.items():
        digest = asset_id.split(":", 1)[1]
        hits = list((ASSETS / digest[:2] / digest[2:4]).glob(f"{digest}.*"))
        if not hits:
            continue
        asset = hits[0]
        meta = _sidecar_meta(digest)
        rows = dets_by_asset.get((asset_id, cap["run_id"]), [])
        dets, species = [], []
        for i, d in enumerate(rows):
            dets.append({
                "category": _CAT_CODE.get(d["detector_class"], d["detector_class"]),
                "conf": d["detector_confidence"],
                "bbox": [d["bbox_x"], d["bbox_y"], d["bbox_w"], d["bbox_h"]],
            })
            if d["taxon"]:
                species.append({"det_index": i, "taxon": d["taxon"],
                                "score": d["taxon_confidence"],
                                "taxon_raw": d["taxon_raw"],
                                "review_priority": d["review_priority"]})
        cap_dt = datetime.fromisoformat(cap["capture_time"])
        captured = cap_dt.astimezone(_LOCAL_TZ).replace(tzinfo=None).isoformat()
        items[asset_id] = {
            "asset_id": asset_id,
            "basename": asset.name,
            "image": str(asset.relative_to(ASSETS)),
            "media_type": cap["media_type"] if "media_type" in cap.keys() else "image",
            "station": cap["station"],
            "device_id": meta.get("device_id"),
            "captured_at": captured,
            "detections": dets,
            "md_max_conf": max((d["conf"] for d in dets), default=0.0),
            "pipeline_species": species or None,
        }
    _pipeline_cache.update(mtime=mtime, items=items)
    return items


def all_images() -> dict:
    """asset_id -> capture record. The pipeline is the only source of captures."""
    return pipeline_images()


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
    """asset_id -> newest verdict for that capture.

    Grouped by asset_id rather than basename so a capture reviewed under its vendor
    filename and again under its content-addressed name resolves to ONE verdict —
    the newest — instead of two rows that disagree about whether it was reviewed.
    Rows with no asset_id are skipped: they are superseded older reviews from before
    the backfill, and their current verdict is carried by a row that does have one.
    """
    rows = conn.execute("""
        SELECT r.* FROM reviews r
        JOIN (SELECT asset_id, MAX(id) AS id FROM reviews
              WHERE asset_id IS NOT NULL GROUP BY asset_id) m
          ON r.id = m.id
    """).fetchall()
    return {r["asset_id"]: r for r in rows}


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
    asset_id: str
    tags: list[str]
    counts: dict[str, int] = {}   # tag -> count, only for counts > 1
    individual: str | None = None
    individual_confidence: str | None = None
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
    devices = sorted({im["device_id"] for im in all_images().values() if im["device_id"]})
    move_flags = (
        json.loads(MOVE_FLAGS.read_text()) if MOVE_FLAGS.exists() else {}
    )
    with db() as conn:
        epochs = {d: current_epoch(conn, d) for d in devices}
    individuals = []
    wldb = Path("/Users/hosebot/trailcam/tags/wildlife.db")
    if wldb.exists():
        wconn = sqlite3.connect(f"file:{wldb}?mode=ro", uri=True)
        individuals = [{"name": n, "species": sp} for n, sp in
                       wconn.execute("SELECT name, species FROM individuals"
                                     " ORDER BY name")]
        wconn.close()
    return {
        "preset_tags": [{"tag": t, "key": k} for t, k in PRESET_TAGS],
        "no_count_tags": ["empty", "unsure"],
        "devices": devices,
        "epochs": epochs,
        "max_zone_area_frac": MAX_ZONE_AREA_FRAC,
        "move_flags": move_flags,
        "individuals": individuals,
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
    for im in all_images().values():
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
    for im in all_images().values():
        if device and im["device_id"] != device:
            continue
        r = reviewed.get(im["asset_id"])
        if status == "unreviewed" and r:
            continue
        if status == "reviewed" and not r:
            continue
        item = dict(im)
        dev_zones = by_dev.get(im["device_id"], [])
        dets = [dict(d, excluded=_excluded(d, dev_zones)) for d in im["detections"]]
        item["detections"] = dets
        item["species"] = im.get("pipeline_species")
        item.pop("pipeline_species", None)
        item["md_max_conf_eff"] = max(
            (d["conf"] for d in dets if not d["excluded"]), default=0.0
        )
        item["review"] = (
            {"tags": json.loads(r["tags"]), "counts": json.loads(r["counts"]),
             "individual": r["individual"],
             "individual_confidence": r["individual_confidence"],
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
        "total": len(all_images()),
        "reviewed": len(reviewed),
        "unreviewed": len(all_images()) - len(reviewed),
        "per_tag": dict(sorted(per_tag.items(), key=lambda kv: -kv[1])),
        "individuals": dict(sorted(individuals.items(), key=lambda kv: -kv[1])),
    }


@app.post("/api/review")
def review(r: ReviewIn):
    im = all_images().get(r.asset_id)
    if not im:
        raise HTTPException(404, f"unknown asset {r.asset_id}")
    if not r.tags:
        raise HTTPException(400, "at least one tag required")
    counts = {t: c for t, c in r.counts.items() if c > 1}
    if bad := set(counts) - set(r.tags):
        raise HTTPException(400, f"counts for untagged: {sorted(bad)}")
    if r.individual and not r.individual_confidence:
        raise HTTPException(400, "individual_confidence required when naming "
                                 "an individual (a wrong name is worse than "
                                 "no name)")
    with db() as conn:
        conn.execute(
            "INSERT INTO reviews (asset_id, basename, image, device_id, captured_at,"
            " tags, counts, individual, individual_confidence, notes,"
            " md_max_conf, reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (im["asset_id"], im["basename"], im["image"], im["device_id"],
             im["captured_at"],
             json.dumps(sorted(r.tags)), json.dumps(counts), r.individual,
             r.individual_confidence, r.notes.strip(),
             im["md_max_conf"], now_iso()),
        )
    return {"ok": True}


@app.post("/api/sync-wildlife")
def sync_wildlife():
    """On-demand reviews → wildlife-log sync: the same scripts the 02:30
    nightly runs (sync, weather, encounters), so a fresh review session can
    land in the log without waiting for tonight."""
    repo = Path(__file__).parent
    py = repo / ".venv/bin/python"
    output = []
    for script, fatal in (("sync_wildlife_log.py", True),
                          ("weather_backfill.py", False),
                          ("build_encounters.py", False)):
        args = [str(py), str(repo / "scripts" / script)]
        if script == "build_encounters.py":
            args += ["--gap-min", "90"]
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=300, cwd=str(repo))
        if r.returncode != 0:
            if fatal:
                raise HTTPException(500, f"{script}: {r.stderr.strip()[-300:]}")
            output.append(f"{script}: failed (non-fatal)")
            continue
        output.append(r.stdout.strip())
    return {"ok": True, "output": output}


@app.get("/images/{rel:path}")
def image(rel: str):
    path = (ASSETS / rel).resolve()
    if not path.is_relative_to(ASSETS) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
