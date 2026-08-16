#!/usr/bin/env python
"""Sync reviewed animal captures into the wildlife sighting log.

Runs after the nightly pipeline pass (and on demand). For every human review
newer than the watermark whose tags name an animal, insert one sighting row
per species: date/time/station from the capture, count from the review's
count field, the review note attached, auto=1, linked by capture_asset_id.

What stays human: individual attribution (Al/Ben/Boar...), multi-cam track
grouping, and every interpretive claim. The sync records that an animal was
captured; it never guesses which animal. Idempotent via the reviews.id
watermark plus a capture+species uniqueness check.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"
TAGS_DB = Path.home() / "trailcam/tags/tags.db"
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"
LOCAL_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

ANIMAL_TAGS = {
    "deer", "bear", "mountain-lion", "coyote", "bobcat", "fox", "skunk",
    "turkey", "jackrabbit", "squirrel", "bird", "domestic-dog", "other-animal",
}
CATEGORY = {
    "deer": "deer", "bear": "bear",
    "coyote": "predator", "fox": "predator", "bobcat": "predator",
    "mountain-lion": "predator",
    "turkey": "bird", "bird": "bird",
    "jackrabbit": "small_game", "squirrel": "small_game", "skunk": "small_game",
}


def main() -> int:
    wl = sqlite3.connect(WILDLIFE_DB)
    wl.row_factory = sqlite3.Row
    tags = sqlite3.connect(f"file:{TAGS_DB}?mode=ro", uri=True)
    tags.row_factory = sqlite3.Row
    det = sqlite3.connect(f"file:{DETECTIONS_DB}?mode=ro", uri=True) \
        if DETECTIONS_DB.exists() else None
    if det:
        det.row_factory = sqlite3.Row

    row = wl.execute("SELECT value FROM meta WHERE key='review_watermark'").fetchone()
    watermark = int(row["value"]) if row else 0

    reviews = tags.execute(
        "SELECT * FROM reviews WHERE id > ? ORDER BY id", (watermark,)).fetchall()
    added = 0
    max_id = watermark
    for r in reviews:
        max_id = max(max_id, r["id"])
        species_tags = set(json.loads(r["tags"])) & ANIMAL_TAGS
        if not species_tags:
            continue
        digest = Path(r["basename"]).stem
        asset_id = f"sha256:{digest}" if len(digest) == 64 else None

        station, date, time = None, None, None
        if asset_id and det:
            cap = det.execute(
                "SELECT station, capture_time FROM captures WHERE asset_id=? "
                "ORDER BY rowid DESC LIMIT 1", (asset_id,)).fetchone()
            if cap:
                station = cap["station"]
                t = datetime.fromisoformat(cap["capture_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                local = t.astimezone(LOCAL_TZ)
                date, time = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
        if station is None:
            # legacy-corpus basename: station unknown to this sync; fall back
            # to review metadata (device id) and the review's captured_at
            station = r["device_id"] or "unknown"
            if r["captured_at"]:
                date, time = r["captured_at"][:10], r["captured_at"][11:16]
        if date is None:
            continue

        counts = json.loads(r["counts"] or "{}")
        for sp in sorted(species_tags):
            dup = wl.execute(
                "SELECT 1 FROM sightings WHERE capture_asset_id=? AND species=?",
                (asset_id, sp)).fetchone() if asset_id else None
            if dup:
                continue
            wl.execute(
                "INSERT INTO sightings (date, time, station, species, count,"
                " source, category, capture_asset_id, auto, notes)"
                " VALUES (?,?,?,?,?,?,?,?,1,?)",
                (date, time, station, sp, counts.get(sp, 1), "camera",
                 CATEGORY.get(sp, "other"), asset_id, r["notes"] or ""))
            added += 1

    wl.execute("INSERT OR REPLACE INTO meta VALUES ('review_watermark', ?)",
               (str(max_id),))
    wl.commit()
    print(f"wildlife-log sync: {len(reviews)} new reviews, {added} sightings added, "
          f"watermark {watermark} -> {max_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
