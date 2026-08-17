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

# P's device->station mapping (2026-08-16) for the legacy Reveal corpus,
# whose reviews carry only a device id. Mirrors ~/trailcam/landing/stations.json.
DEVICE_STATION = {
    "016579006078088": "Storm Oak",
    "016579006023894": "South Clearing",
    "016579006127489": "Bench",
    "016579006157692": "North Oak",
}


def normalize_station(name: str) -> str:
    """Arlo camera names -> log station names ('Cabin - Yard' -> 'Cabin Yard',
    'Cabin - Crossroads' -> 'Crossroads'); unknown names pass through."""
    if name == "Cabin - Yard":
        return "Cabin Yard"
    if name.startswith("Cabin - "):
        return name[len("Cabin - "):]
    return DEVICE_STATION.get(name, name)


def claimed_refs(wl: sqlite3.Connection) -> set[str]:
    """Every media ref already represented by an existing sighting — either
    as capture_asset_id or inside media_refs (curated entries linked by the
    reconciliation pass). Backfill must not re-enter those captures."""
    out = set()
    for r in wl.execute("SELECT capture_asset_id, media_refs FROM sightings"):
        if r["capture_asset_id"]:
            out.add(r["capture_asset_id"])
        for ref in json.loads(r["media_refs"] or "[]"):
            if ref.get("ref"):
                out.add(ref["ref"])
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="process ALL reviews (ignore watermark), skipping "
                         "captures already claimed by existing sightings")
    args = ap.parse_args()

    wl = sqlite3.connect(WILDLIFE_DB)
    wl.row_factory = sqlite3.Row
    if "media_refs" not in {r[1] for r in wl.execute("PRAGMA table_info(sightings)")}:
        wl.execute("ALTER TABLE sightings ADD COLUMN media_refs TEXT"
                   " NOT NULL DEFAULT '[]'")
    tags = sqlite3.connect(f"file:{TAGS_DB}?mode=ro", uri=True)
    tags.row_factory = sqlite3.Row
    det = sqlite3.connect(f"file:{DETECTIONS_DB}?mode=ro", uri=True) \
        if DETECTIONS_DB.exists() else None
    if det:
        det.row_factory = sqlite3.Row

    row = wl.execute("SELECT value FROM meta WHERE key='review_watermark'").fetchone()
    watermark = int(row["value"]) if row else 0

    since = 0 if args.backfill else watermark
    claimed = claimed_refs(wl) if args.backfill else set()
    reviews = tags.execute(
        "SELECT * FROM reviews WHERE id > ? ORDER BY id", (since,)).fetchall()
    added = skipped_claimed = 0
    max_id = watermark
    for r in reviews:
        max_id = max(max_id, r["id"])
        species_tags = set(json.loads(r["tags"])) & ANIMAL_TAGS
        if not species_tags:
            continue
        digest = Path(r["basename"]).stem
        if len(digest) == 64:
            asset_id = f"sha256:{digest}"
        else:
            asset_id = f"legacy:{r['basename']}"   # Reveal batch archive
        if asset_id in claimed:
            skipped_claimed += 1
            continue

        station, date, time = None, None, None
        media = []
        if asset_id.startswith("sha256:") and det:
            cap = det.execute(
                "SELECT station, capture_time FROM captures WHERE asset_id=? "
                "ORDER BY rowid DESC LIMIT 1", (asset_id,)).fetchone()
            if cap:
                station = normalize_station(cap["station"])
                t = datetime.fromisoformat(cap["capture_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                local = t.astimezone(LOCAL_TZ)
                date, time = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
        if station is None:
            # legacy-corpus basename: station from the device mapping,
            # time from the review's captured_at (filename-derived, local)
            station = DEVICE_STATION.get(r["device_id"], r["device_id"] or "unknown")
            if r["captured_at"]:
                date, time = r["captured_at"][:10], r["captured_at"][11:16]
            hits = list((Path.home() / "trailcam/landing/assets").glob(
                f"*/**/{r['basename']}"))
            if hits:
                media.append({"ref": asset_id, "path": str(hits[0]),
                              "kind": "image"})
        if date is None:
            continue

        counts = json.loads(r["counts"] or "{}")
        for sp in sorted(species_tags):
            dup = wl.execute(
                "SELECT 1 FROM sightings WHERE capture_asset_id=? AND species=?",
                (asset_id, sp)).fetchone() if asset_id else None
            if dup:
                continue
            if not media and asset_id.startswith("sha256:"):
                digest = asset_id.split(":", 1)[1]
                hits = list((Path.home() / "trailcam/landing/assets" /
                             digest[:2] / digest[2:4]).glob(f"{digest}.*"))
                if hits:
                    media.append({"ref": asset_id, "path": str(hits[0]),
                                  "kind": "video" if hits[0].suffix == ".mp4"
                                  else "image"})
            wl.execute(
                "INSERT INTO sightings (date, time, station, species, count,"
                " source, category, capture_asset_id, auto, notes, media_refs)"
                " VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                (date, time, station, sp, counts.get(sp, 1), "camera",
                 CATEGORY.get(sp, "other"), asset_id, r["notes"] or "",
                 json.dumps(media)))
            added += 1

    wl.execute("INSERT OR REPLACE INTO meta VALUES ('review_watermark', ?)",
               (str(max_id),))
    wl.commit()
    extra = f", {skipped_claimed} skipped (already in log)" if args.backfill else ""
    print(f"wildlife-log sync: {len(reviews)} reviews scanned, {added} sightings "
          f"added{extra}, watermark {watermark} -> {max_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
