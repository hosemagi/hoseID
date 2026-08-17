#!/usr/bin/env python
"""Attach historical weather to wildlife-log sightings.

Source: Open-Meteo (no key). Weather from the forecast API with past_days
(hourly temp/pressure/precip/wind), UV index + US AQI from the air-quality
API. Property coordinates from the Reveal camera GPS (39.3212, -120.9713),
not the town. Each sighting gets a `weather` JSON blob for its nearest hour,
including the 3-hour pressure trend — the variable the season log explicitly
asked to start recording.

Re-runnable: only fills rows where weather IS NULL (or --refresh for all).
Air-quality history reaches back ~92 days; older rows record what's missing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import urllib.request

DB = Path.home() / "trailcam/tags/wildlife.db"
LAT, LON = 39.3212, -120.9713
TZ = "America/Los_Angeles"
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}&timezone={TZ}&past_days=92&forecast_days=1"
    "&hourly=temperature_2m,pressure_msl,precipitation,"
    "wind_speed_10m,wind_direction_10m"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
)
AIR_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LAT}&longitude={LON}&timezone={TZ}&past_days=92&forecast_days=1"
    "&hourly=us_aqi,uv_index"
)
CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def cardinal(deg: float | None) -> str | None:
    if deg is None:
        return None
    return CARDINALS[int((deg + 11.25) // 22.5) % 16]


def build_series():
    wx = fetch(WEATHER_URL)["hourly"]
    air = fetch(AIR_URL)["hourly"]
    series = {}
    for i, t in enumerate(wx["time"]):
        p_hpa = wx["pressure_msl"][i]
        series[t] = {
            "temp_f": wx["temperature_2m"][i],
            "pressure_inhg": round(p_hpa * 0.02953, 2) if p_hpa is not None else None,
            "precip_in": wx["precipitation"][i],
            "wind_mph": wx["wind_speed_10m"][i],
            "wind_dir_deg": wx["wind_direction_10m"][i],
            "wind_dir": cardinal(wx["wind_direction_10m"][i]),
        }
    for i, t in enumerate(air["time"]):
        if t in series:
            series[t]["us_aqi"] = air["us_aqi"][i]
            series[t]["uv_index"] = air["uv_index"][i]
    # 3h pressure trend, the log's requested variable
    times = sorted(series)
    for i, t in enumerate(times):
        prior = series[times[i - 3]]["pressure_inhg"] if i >= 3 else None
        cur = series[t]["pressure_inhg"]
        series[t]["pressure_change_3h_inhg"] = (
            round(cur - prior, 2) if cur is not None and prior is not None else None)
    return series


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="recompute even where weather already present")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sightings)")}
    if "weather" not in cols:
        conn.execute("ALTER TABLE sightings ADD COLUMN weather TEXT")

    series = build_series()
    where = "" if args.refresh else "WHERE weather IS NULL"
    filled = missing = skipped = 0
    for s in conn.execute(f"SELECT sighting_id, date, time FROM sightings {where}"):
        if not s["time"]:
            skipped += 1
            continue
        hour_key = f"{s['date']}T{s['time'][:2]}:00"
        w = series.get(hour_key)
        if w is None:
            missing += 1
            continue
        w = dict(w, source="open-meteo", hour=hour_key)
        conn.execute("UPDATE sightings SET weather=? WHERE sighting_id=?",
                     (json.dumps(w), s["sighting_id"]))
        filled += 1
    conn.commit()
    print(f"weather: {filled} filled, {missing} outside series range, "
          f"{skipped} skipped (no time); series {min(series)}..{max(series)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
