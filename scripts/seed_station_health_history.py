#!/usr/bin/env python
"""Seed station_health_history.jsonl from the battery readings already in
Reveal sidecars (conditions.battery_pct), so hoseserv's "days left" estimate
works without waiting for a week of live polls.

Uses the fetcher's own append_health_history, one call per property-local
day, so it is re-runnable: a day already recorded keeps whichever sample has
the later capture time, and days older than the 180-day cap are dropped.
Reads sidecars only; never writes under the landing zone's assets/sidecars.

Run from the repo root with the fetch venv (fetchers import requests):
    .venv-fetch/bin/python scripts/seed_station_health_history.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetchers import reveal  # noqa: E402 (also puts src/ on sys.path)
from hoseid import landing, paths  # noqa: E402
from hoseid.station_renames import normalize_station  # noqa: E402


def readings_by_day(sidecar_paths) -> dict[str, dict[str, dict]]:
    """{local date: {renamed station: {"battery_pct", "when"}}}, newest
    capture per station per day, from Reveal sidecars with a battery reading."""
    days: dict[str, dict[str, dict]] = {}
    for p in sidecar_paths:
        try:
            sc = json.loads(Path(p).read_text())
            battery = (sc.get("conditions") or {}).get("battery_pct")
            if sc.get("vendor") != "tactacam" or not isinstance(battery, int):
                continue
            when = datetime.fromisoformat(sc["capture_time"])
            if when.tzinfo is None:
                continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        day = when.astimezone(reveal.PROPERTY_TZ).date().isoformat()
        station = normalize_station(sc.get("station") or "unknown", day)
        prev = days.setdefault(day, {}).get(station)
        if prev is None or when > prev["when"]:
            days[day][station] = {"battery_pct": battery, "when": when}
    return days


def seed(sidecar_paths, path=None) -> int:
    """Append every day's readings; returns the number of (day, station) samples."""
    days = readings_by_day(sidecar_paths)
    for day in sorted(days):
        reveal.append_health_history(days[day], path=path)
    return sum(len(v) for v in days.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="count samples without writing the history file")
    args = ap.parse_args()
    sidecars = landing.iter_sidecars()
    if args.dry_run:
        days = readings_by_day(sidecars)
        print(f"{len(sidecars)} sidecars -> {sum(len(v) for v in days.values())} "
              f"samples over {len(days)} days (dry run, nothing written)")
        return 0
    n = seed(sidecars)
    print(f"{len(sidecars)} sidecars -> {n} samples; wrote "
          f"{paths.station_health_history_file()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
