#!/usr/bin/env python
"""Repair sighting rows whose station is a raw device serial.

These rows were created by the backfill while detections.db was mid-rebuild
(the standing run had been cascade-wiped by the old INSERT OR REPLACE bug),
so the capture lookup came back empty and the sync fell back to the review's
device id. With the rebuild complete, resolve each row's station from its
capture and register any Arlo station names missing from the stations table.
Re-runnable; only touches rows whose station is not a known station name.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_wildlife_log import normalize_station  # noqa: E402

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"


def main() -> int:
    wl = sqlite3.connect(WILDLIFE_DB)
    wl.row_factory = sqlite3.Row
    det = sqlite3.connect(f"file:{DETECTIONS_DB}?mode=ro", uri=True)
    det.row_factory = sqlite3.Row

    known = {r["name"] for r in wl.execute("SELECT name FROM stations")}
    fixed = unresolved = 0
    new_stations = set()
    for s in wl.execute(
            "SELECT sighting_id, station, capture_asset_id FROM sightings "
            "WHERE station NOT IN (SELECT name FROM stations)"):
        station = None
        if s["capture_asset_id"] and s["capture_asset_id"].startswith("sha256:"):
            cap = det.execute(
                "SELECT station FROM captures WHERE asset_id=? "
                "ORDER BY rowid DESC LIMIT 1", (s["capture_asset_id"],)).fetchone()
            if cap:
                station = normalize_station(cap["station"])
        if station is None:
            # Arlo names are already real station names, just unregistered
            if not s["station"] or s["station"].isupper() and len(s["station"]) == 13:
                unresolved += 1
                continue
            station = s["station"]
        if station != s["station"]:
            wl.execute("UPDATE sightings SET station=? WHERE sighting_id=?",
                       (station, s["sighting_id"]))
            fixed += 1
        if station not in known:
            new_stations.add(station)

    for name in sorted(new_stations):
        wl.execute(
            "INSERT OR IGNORE INTO stations (name, aliases, camera, description,"
            " notes) VALUES (?, '[]', 'arlo', ?, 'auto-registered by station repair')",
            (name, f"Arlo camera '{name}' (auto-registered)"))
    wl.commit()
    print(f"stations repaired: {fixed} rows updated, {unresolved} unresolved, "
          f"{len(new_stations)} station(s) registered: {sorted(new_stations)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
