#!/usr/bin/env python
"""Group wildlife-log sightings into encounters (derived, regenerable).

An encounter is a chain of same-species sightings where consecutive events are
within --gap-min of each other, regardless of station — so a multi-camera
track (Storm Oak 06:14 -> Bench 06:29 -> Crossroads 06:43) is ONE encounter
with a station sequence, and a night-long stay of discrete captures is one
encounter with a span.

Honesty rules carried from the season log:
  - span is capture-bracketed, not proof of continuous presence
    (first..last capture; the gaps are inference)
  - count is the MAX simultaneous count seen in any member sighting, not a sum
  - individual is carried only from member sightings that name one; an
    encounter mixing named and unnamed members keeps the name with the
    weakest member confidence

Output: ~/trailcam/derived/encounters.db, table `encounters`, rebuilt
WHOLESALE every run (gap threshold will be retuned; derived state must never
accumulate). Sighting ids of members are stored as JSON for drill-down.

Run: .venv/bin/python scripts/build_encounters.py [--gap-min 30]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"
OUT_DB = Path.home() / "trailcam/derived/encounters.db"

CONF_RANK = {"confirmed": 3, "assumed": 2, "by_elimination": 1, "unconfirmed": 0}

# Encounters are reserved for the bigger, wide-ranging animals whose
# cross-station movement is individually meaningful (P, 2026-08-16). Dense
# multi-resident small game (squirrels, jackrabbits...) made chains that
# merged different animals — e.g. a "squirrel encounter" hopping South
# Clearing -> Storm Oak in 11 minutes. Small game stays sighting-level.
ENCOUNTER_SPECIES = {
    "deer", "bear", "mountain-lion", "coyote", "bobcat", "fox",
    "turkey", "domestic-dog",
}
# NOTE: an impossible-travel floor (different stations too close in time)
# was tried and removed: adjacent stations are a 1-2 minute walk apart
# (boar: Cabin Yard 12:55 -> Trailer 12:56), so without the station-distance
# map (open item) travel-based inference over-counts. min_individuals rests
# on simultaneous counts only.

SCHEMA = """
DROP TABLE IF EXISTS encounters;
CREATE TABLE encounters (
    encounter_id  INTEGER PRIMARY KEY,
    species       TEXT NOT NULL,
    individual    TEXT,
    individual_confidence TEXT,
    start_local   TEXT NOT NULL,     -- YYYY-MM-DD HH:MM
    end_local     TEXT NOT NULL,
    duration_min  INTEGER NOT NULL,  -- capture-bracketed, not continuous presence
    n_sightings   INTEGER NOT NULL,
    max_count     INTEGER NOT NULL,  -- max simultaneous, never a sum
    min_individuals INTEGER NOT NULL DEFAULT 1,  -- structural floor: simultaneous
                                     -- counts / impossible cross-station travel
    stations      TEXT NOT NULL,     -- JSON ordered [station, ...] as visited
    n_stations    INTEGER NOT NULL,
    multi_cam     INTEGER NOT NULL,
    sighting_ids  TEXT NOT NULL,     -- JSON [id, ...]
    gap_min_used  INTEGER NOT NULL
);
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build(gap_min: int) -> dict:
    wl = sqlite3.connect(f"file:{WILDLIFE_DB}?mode=ro", uri=True)
    wl.row_factory = sqlite3.Row
    rows = [dict(r) for r in wl.execute(
        "SELECT sighting_id, date, time, station, species, individual,"
        " individual_confidence, count FROM sightings"
        " WHERE time IS NOT NULL ORDER BY species, date, time")]
    wl.close()

    out = sqlite3.connect(OUT_DB)
    out.executescript(SCHEMA)
    gap = timedelta(minutes=gap_min)
    n_enc = 0
    by_group: dict[tuple, list[dict]] = {}
    skipped_small = 0
    for r in rows:
        if r["species"] not in ENCOUNTER_SPECIES:
            skipped_small += 1
            continue
        r["dt"] = datetime.fromisoformat(f"{r['date']}T{r['time']}")
        by_group.setdefault((r["species"],), []).append(r)

    for key, srows in by_group.items():
        species = key[0]
        srows.sort(key=lambda r: r["dt"])
        chain: list[dict] = []

        def flush():
            nonlocal n_enc
            if not chain:
                return
            stations = []
            for m in chain:
                if not stations or stations[-1] != m["station"]:
                    stations.append(m["station"])
            named = [m for m in chain if m["individual"]]
            individual = conf = None
            if named:
                names = {m["individual"] for m in named}
                if len(names) == 1:
                    individual = named[0]["individual"]
                    conf = min((m["individual_confidence"] or "unconfirmed"
                                for m in named), key=lambda c: CONF_RANK.get(c, 0))
                else:
                    individual = "+".join(sorted(names))   # e.g. Al+Ben together
                    conf = min((m["individual_confidence"] or "unconfirmed"
                                for m in named), key=lambda c: CONF_RANK.get(c, 0))
            dur = int((chain[-1]["dt"] - chain[0]["dt"]).total_seconds() // 60)
            # structural minimum individuals: max simultaneous count seen
            min_ind = max(m["count"] for m in chain)
            out.execute(
                "INSERT INTO encounters (species, individual,"
                " individual_confidence, start_local, end_local, duration_min,"
                " n_sightings, max_count, min_individuals, stations,"
                " n_stations, multi_cam, sighting_ids, gap_min_used)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (species, individual, conf,
                 chain[0]["dt"].strftime("%Y-%m-%d %H:%M"),
                 chain[-1]["dt"].strftime("%Y-%m-%d %H:%M"),
                 dur, len(chain), max(m["count"] for m in chain), min_ind,
                 json.dumps(stations), len(set(stations)),
                 int(len(set(stations)) > 1),
                 json.dumps([m["sighting_id"] for m in chain]), gap_min))
            n_enc += 1
            chain.clear()

        for r in srows:
            if chain and (r["dt"] - chain[-1]["dt"]) > gap:
                flush()
            chain.append(r)
        flush()

    out.execute("INSERT INTO meta VALUES ('built_at', datetime('now'))")
    out.execute("INSERT INTO meta VALUES ('gap_min', ?)", (str(gap_min),))
    out.commit()
    return {"sightings": len(rows), "encounters": n_enc,
            "skipped_small_game": skipped_small}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap-min", type=int, default=30)
    args = ap.parse_args()
    stats = build(args.gap_min)
    print(f"encounters built: {stats['encounters']} from {stats['sightings']} "
          f"timed sightings ({stats['skipped_small_game']} small-game rows "
          f"excluded; gap {args.gap_min} min) -> {OUT_DB}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
