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
from datetime import datetime, timezone
from pathlib import Path

# this repo's src first: the venv's editable install may point at another checkout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hoseid import fsio, station_renames, stations
from hoseid.schema import SIGHTINGS_COLUMNS, ensure_columns
from hoseid.site import TZ as LOCAL_TZ

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"
TAGS_DB = Path.home() / "trailcam/tags/tags.db"
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"


# sex for a new sighting whose review set none. Deer default to doe: bucks
# are the minority and get tagged (ctrl+M); everything else stays unset.
DEFAULT_SEX = {"deer": "F"}

def local_date(capture_time_iso: str) -> str:
    t = datetime.fromisoformat(capture_time_iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")

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

# Serial -> station, from the registry (~/trailcam/landing/stations.json),
# for the legacy Reveal corpus whose reviews carry only a device id.
# stations.by_device() raises on a duplicate serial, and so does this one:
# two stations claiming one camera is a registry error the sync aborts on
# rather than silently writing a wrong station (docs/CAMERA_RENAMES.md).
def device_station(serial: str | None, path=None) -> str | None:
    if not serial:
        return None
    reg = stations.by_device(path)
    return reg.get(serial).name if serial in reg else None


def normalize_station(name: str, date: str | None = None) -> str:
    """Camera / legacy device id -> log station name. Serials resolve through
    the registry; a serial it does not know falls through unchanged. The
    camera rename rules (detections.db captures written BEFORE a rename still
    carry the old label) live in config/station_renames.json, shared with
    hoseserv's security view; add a line there per rename
    (docs/CAMERA_RENAMES.md)."""
    return station_renames.normalize_station(device_station(name) or name, date)


def individuals_for(individual: str | None, species: str,
                    species_of: dict[str, str]) -> str | None:
    """The part of a review's individual field that belongs to this species.

    One review covers the whole capture, so its individual field can name
    several individuals ("Al+Ben") and people too (hoseID's people table): a
    capture of a dog and P, named "P", must not log the dog as P. Only names
    that are registered wildlife individuals of this species pass; people
    never appear in wildlife.db, so they never reach the log."""
    names = [n for n in (individual or "").split("+") if species_of.get(n) == species]
    return "+".join(names) or None


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

    wl = fsio.connect_rw(WILDLIFE_DB, row_factory=True)
    # media_refs and sex; weather is added by weather_backfill.py
    ensure_columns(
        wl, "sightings", {
            "media_refs": SIGHTINGS_COLUMNS["media_refs"],
            "sex": SIGHTINGS_COLUMNS["sex"],
        }
    )
    tags = fsio.connect_ro(TAGS_DB, row_factory=True)
    det = fsio.connect_ro(DETECTIONS_DB, row_factory=True) \
        if DETECTIONS_DB.exists() else None

    species_of = dict(wl.execute("SELECT name, species FROM individuals").fetchall())
    row = wl.execute("SELECT value FROM meta WHERE key='review_watermark'").fetchone()
    watermark = int(row["value"]) if row else 0

    since = 0 if args.backfill else watermark
    claimed = claimed_refs(wl) if args.backfill else set()
    reviews = tags.execute(
        "SELECT * FROM reviews WHERE id > ? ORDER BY id", (since,)).fetchall()
    added = skipped_claimed = sex_filled = 0
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
                station = normalize_station(cap["station"],
                                            local_date(cap["capture_time"]))
                t = datetime.fromisoformat(cap["capture_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                local = t.astimezone(LOCAL_TZ)
                date, time = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
                if not station:
                    # a capture with no station label (sighting 479): captures
                    # has no serial column, but the review carries the
                    # sidecar's device_id, so try the registry, else
                    # "unknown" -- "" is invisible to every per-station view
                    station = device_station(r["device_id"]) or "unknown"
        if station is None:
            # legacy-corpus basename, or a pipeline capture with no cap row:
            # the review carries a device id. Resolve the serial through the
            # registry, else fall back to the id, else "unknown" — never "".
            station = device_station(r["device_id"]) or r["device_id"] or "unknown"
            if r["captured_at"]:
                date, time = r["captured_at"][:10], r["captured_at"][11:16]
            hits = list((Path.home() / "trailcam/landing/assets").glob(
                f"*/**/{r['basename']}"))
            if hits:
                media.append({"ref": asset_id, "path": str(hits[0]),
                              "kind": "video" if hits[0].suffix == ".mp4"
                              else "image"})
        if date is None:
            continue
        station = station or "unknown"   # never write a blank station

        counts = json.loads(r["counts"] or "{}")
        cols = r.keys()
        indiv = r["individual"] if "individual" in cols else None
        indiv_conf = r["individual_confidence"] if "individual_confidence" in cols else None
        review_sex = r["sex"] if "sex" in cols else None
        for sp in sorted(species_tags):
            # reviews default to F since 2026-10-26 (no "not set" in the UI),
            # so a review's F is only trusted for deer, the species it is the
            # default for; an M is always a deliberate mark
            sex = review_sex if review_sex == "M" or sp in DEFAULT_SEX else None
            dup = wl.execute(
                "SELECT 1 FROM sightings WHERE capture_asset_id=? AND species=?",
                (asset_id, sp)).fetchone() if asset_id else None
            if dup:
                # a re-review that adds sex fills it in. It never overrides
                # an M, but an explicit M does override an F: deer default
                # to F (DEFAULT_SEX, 2026-10-26), so a deer F is usually
                # "not marked buck" rather than a checked doe
                if sex:
                    sex_filled += wl.execute(
                        "UPDATE sightings SET sex=? WHERE capture_asset_id=?"
                        " AND species=? AND (sex IS NULL OR (sex='F' AND ?='M'))",
                        (sex, asset_id, sp, sex)).rowcount
                continue
            if not media and asset_id.startswith("sha256:"):
                digest = asset_id.split(":", 1)[1]
                hits = list((Path.home() / "trailcam/landing/assets" /
                             digest[:2] / digest[2:4]).glob(f"{digest}.*"))
                if hits:
                    media.append({"ref": asset_id, "path": str(hits[0]),
                                  "kind": "video" if hits[0].suffix == ".mp4"
                                  else "image"})
            sp_indiv = individuals_for(indiv, sp, species_of)
            wl.execute(
                "INSERT INTO sightings (date, time, station, species, count,"
                " source, category, capture_asset_id, auto, individual,"
                " individual_confidence, sex, notes, media_refs)"
                " VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
                (date, time, station, sp, counts.get(sp, 1), "camera",
                 CATEGORY.get(sp, "other"), asset_id, sp_indiv,
                 indiv_conf if sp_indiv else None,
                 sex or DEFAULT_SEX.get(sp),
                 r["notes"] or "", json.dumps(media)))
            added += 1

    wl.execute("INSERT OR REPLACE INTO meta VALUES ('review_watermark', ?)",
               (str(max_id),))
    wl.commit()
    extra = f", {skipped_claimed} skipped (already in log)" if args.backfill else ""
    if sex_filled:
        extra += f", sex filled on {sex_filled} existing"
    print(f"wildlife-log sync: {len(reviews)} reviews scanned, {added} sightings "
          f"added{extra}, watermark {watermark} -> {max_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
