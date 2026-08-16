#!/usr/bin/env python
"""Reconcile imported wildlife-log sightings with their media.

Matches each camera-sourced sighting that lacks media against both stores:
  - pipeline captures (detections.db -> content-addressed landing assets)
  - the legacy Reveal batch archive (tags.db reviews, filename timestamps)

Match rule: capture within +/-TOLERANCE_MIN of the logged local time AND
species-compatible (human tag when the capture was reviewed, else pipeline
taxon). Matches land in sightings.media_refs as a JSON list of
{ref, path, kind}; ambiguous or unmatched rows are reported, never guessed.

Direct-visual sightings (deck watches etc.) are skipped by design — there is
no media to link. Re-runnable: only fills rows whose media_refs is empty.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"
TAGS_DB = Path.home() / "trailcam/tags/tags.db"
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"
ASSETS = Path.home() / "trailcam/landing/assets"
LOCAL_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
TOLERANCE_MIN = 3
MAX_REFS = 8   # a long stay matches many frames; keep the arrival cluster

# log species -> acceptable human tags / pipeline taxa
COMPAT = {
    "deer": {"deer", "blacktail", "deer_unspecified"},
    "bear": {"bear", "black_bear"},
    "jackrabbit": {"jackrabbit"},
    "turkey": {"turkey", "wild_turkey"},
    "coyote": {"coyote"},
    "gray_fox": {"fox", "gray_fox"},
    "skunk": {"skunk", "striped_skunk"},
    "squirrel": {"squirrel", "western_gray_squirrel", "ground_squirrel"},
    "domestic-dog": {"domestic-dog", "domestic_dog"},
}
FNAME_RE = re.compile(r"^\d{15}-\d+-\d+-(\d{14})-[A-Z]+\d+\.jpg$")


def load_candidates():
    """All media events: (local_dt, species_set, ref, path, kind)."""
    out = []
    tags = sqlite3.connect(f"file:{TAGS_DB}?mode=ro", uri=True)
    tags.row_factory = sqlite3.Row
    latest = {r["basename"]: r for r in tags.execute(
        "SELECT r.* FROM reviews r JOIN (SELECT basename, MAX(id) id FROM"
        " reviews GROUP BY basename) m ON r.id=m.id")}

    # Legacy Reveal archive: filename timestamps are local wall-clock.
    for base, r in latest.items():
        m = FNAME_RE.match(base)
        if not m:
            continue
        ts = m.group(1)
        dt = datetime(int(ts[4:8]), int(ts[0:2]), int(ts[2:4]),
                      int(ts[8:10]), int(ts[10:12]), int(ts[12:14]))
        species = set(json.loads(r["tags"]))
        hits = list(ASSETS.glob(f"*/**/{base}"))
        out.append((dt, species, f"legacy:{base}",
                    str(hits[0]) if hits else "", "image"))

    # Pipeline captures: species from human review when present, else taxon.
    if DETECTIONS_DB.exists():
        det = sqlite3.connect(f"file:{DETECTIONS_DB}?mode=ro", uri=True)
        det.row_factory = sqlite3.Row
        taxa = {}
        for d in det.execute(
                "SELECT asset_id, run_id, taxon FROM detections WHERE"
                " detector_class='animal' AND taxon IS NOT NULL"
                " ORDER BY detector_confidence"):
            taxa[(d["asset_id"], d["run_id"])] = d["taxon"]
        for cap in det.execute("SELECT * FROM captures"):
            digest = cap["asset_id"].split(":", 1)[1]
            review = next((latest[b] for b in
                           (digest + ".jpg", digest + ".mp4") if b in latest), None)
            if review:
                species = set(json.loads(review["tags"]))
            else:
                t = taxa.get((cap["asset_id"], cap["run_id"]))
                species = {t} if t else set()
            if not species:
                continue
            utc = datetime.fromisoformat(cap["capture_time"])
            if utc.tzinfo is None:
                utc = utc.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
            local = utc.astimezone(LOCAL_TZ).replace(tzinfo=None)
            hits = list((ASSETS / digest[:2] / digest[2:4]).glob(f"{digest}.*"))
            kind = "video" if (cap["media_type"] if "media_type" in cap.keys()
                               else "image") == "video" else "image"
            out.append((local, species, cap["asset_id"],
                        str(hits[0]) if hits else "", kind))
    return out


def main() -> int:
    wl = sqlite3.connect(WILDLIFE_DB)
    wl.row_factory = sqlite3.Row
    cols = {r[1] for r in wl.execute("PRAGMA table_info(sightings)")}
    if "media_refs" not in cols:
        wl.execute("ALTER TABLE sightings ADD COLUMN media_refs TEXT"
                   " NOT NULL DEFAULT '[]'")

    candidates = load_candidates()
    tol = timedelta(minutes=TOLERANCE_MIN)
    matched = skipped = unmatched = 0
    unmatched_rows = []
    for s in wl.execute(
            "SELECT * FROM sightings WHERE media_refs='[]' AND"
            " capture_asset_id IS NULL AND source LIKE '%camera%'").fetchall():
        if not s["time"]:
            skipped += 1
            continue
        want = COMPAT.get(s["species"], {s["species"]})
        base_dt = datetime.fromisoformat(f"{s['date']}T{s['time']}")
        refs = []
        for dt, species, ref, path, kind in candidates:
            if abs(dt - base_dt) <= tol and species & want:
                refs.append({"ref": ref, "path": path, "kind": kind,
                             "at": dt.strftime("%H:%M:%S")})
        if refs:
            refs.sort(key=lambda x: x["at"])
            wl.execute("UPDATE sightings SET media_refs=? WHERE sighting_id=?",
                       (json.dumps(refs[:MAX_REFS]), s["sighting_id"]))
            matched += 1
        else:
            unmatched += 1
            unmatched_rows.append(
                f"  {s['date']} {s['time']} {s['station']} {s['species']}")
    wl.commit()
    print(f"reconciled: {matched} sightings linked, {unmatched} unmatched, "
          f"{skipped} skipped (no time), {len(candidates)} media candidates")
    if unmatched_rows:
        print("unmatched (no media within tolerance — likely pre-archive or"
              " Arlo person/untyped filter):")
        print("\n".join(unmatched_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
