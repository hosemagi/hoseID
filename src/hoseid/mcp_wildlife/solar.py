"""Sunrise / sunset / civil twilight for the property, computed per date.

Sunrise at the cabin moves ~50 minutes between early July and mid-September,
and legal shooting hours (30 min before sunrise to 30 min after sunset) move
with it, so light classification is computed from date + lat/lon rather than
a fixed cutoff. Algorithm: Almanac for Computers (1990) sunrise/sunset, the
one behind most "NOAA-style" calculators; accurate to a minute or two here,
which is well inside the camera-clock jitter.

Coordinates come from WILDLIFE_LAT/WILDLIFE_LON, else the first station in
landing/stations.json with coordinates, else a Storm Oak fallback.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from hoseid import paths

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
FALLBACK_LAT, FALLBACK_LON = 39.32097, -120.97152   # Storm Oak, landing/stations.json
ZENITH_OFFICIAL = 90.833   # sunrise/sunset, refraction + solar radius
ZENITH_CIVIL = 96.0        # civil dawn/dusk
LEGAL_MARGIN = timedelta(minutes=30)


def property_coords() -> tuple[float, float, str]:
    """(lat, lon, source)."""
    lat, lon = os.environ.get("WILDLIFE_LAT"), os.environ.get("WILDLIFE_LON")
    if lat and lon:
        return float(lat), float(lon), "env WILDLIFE_LAT/LON"
    try:
        reg = json.loads(paths.stations_file().read_text())
        for s in reg.get("stations", []):
            if s.get("lat") is not None and s.get("lon") is not None:
                return float(s["lat"]), float(s["lon"]), f"stations.json:{s.get('name')}"
    except (OSError, ValueError):
        pass
    return FALLBACK_LAT, FALLBACK_LON, "fallback (Storm Oak)"


def _event_utc_hours(d: date, lat: float, lon: float, zenith: float, rising: bool) -> float | None:
    """Hour-of-day UTC of the event, or None when the sun never crosses that zenith."""
    n = d.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = n + ((6 if rising else 18) - lng_hour) / 24.0
    m = (0.9856 * t) - 3.289
    l = m + (1.916 * math.sin(math.radians(m))) + (0.020 * math.sin(math.radians(2 * m))) + 282.634
    l %= 360.0
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360.0
    lq, raq = (math.floor(l / 90.0)) * 90.0, (math.floor(ra / 90.0)) * 90.0
    ra = (ra + (lq - raq)) / 15.0
    sin_dec = 0.39782 * math.sin(math.radians(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(math.radians(zenith)) - (sin_dec * math.sin(math.radians(lat)))) / \
            (cos_dec * math.cos(math.radians(lat)))
    if cos_h > 1 or cos_h < -1:
        return None
    h = (360.0 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    h /= 15.0
    t_local_mean = h + ra - (0.06571 * t) - 6.622
    return (t_local_mean - lng_hour) % 24.0


@dataclass(frozen=True)
class SunTimes:
    date: date
    civil_dawn: datetime | None
    sunrise: datetime | None
    sunset: datetime | None
    civil_dusk: datetime | None

    @property
    def legal_start(self) -> datetime | None:
        return self.sunrise - LEGAL_MARGIN if self.sunrise else None

    @property
    def legal_end(self) -> datetime | None:
        return self.sunset + LEGAL_MARGIN if self.sunset else None

    def as_dict(self) -> dict:
        f = lambda t: t.strftime("%H:%M") if t else None  # noqa: E731
        return {"date": self.date.isoformat(), "civil_dawn": f(self.civil_dawn),
                "sunrise": f(self.sunrise), "sunset": f(self.sunset), "civil_dusk": f(self.civil_dusk),
                "legal_light": f"{f(self.legal_start)}-{f(self.legal_end)}"}


def _to_local(d: date, hours_utc: float | None) -> datetime | None:
    if hours_utc is None:
        return None
    base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=hours_utc)
    local = base.astimezone(LOCAL_TZ)
    # The UTC day can straddle the local day; pin the event to the requested local date.
    if local.date() != d:
        local = local + timedelta(days=(d - local.date()).days)
    return local


@lru_cache(maxsize=512)
def sun_times(d: date, lat: float, lon: float) -> SunTimes:
    return SunTimes(
        date=d,
        civil_dawn=_to_local(d, _event_utc_hours(d, lat, lon, ZENITH_CIVIL, True)),
        sunrise=_to_local(d, _event_utc_hours(d, lat, lon, ZENITH_OFFICIAL, True)),
        sunset=_to_local(d, _event_utc_hours(d, lat, lon, ZENITH_OFFICIAL, False)),
        civil_dusk=_to_local(d, _event_utc_hours(d, lat, lon, ZENITH_CIVIL, False)),
    )


def parse_local(date_s: str, time_s: str | None) -> datetime | None:
    if not time_s:
        return None
    try:
        hh, mm = int(time_s[0:2]), int(time_s[3:5])
        ss = int(time_s[6:8]) if len(time_s) >= 8 else 0
        d = date.fromisoformat(date_s)
        return datetime.combine(d, time(hh, mm, ss), tzinfo=LOCAL_TZ)
    except (ValueError, TypeError):
        return None


def classify(date_s: str, time_s: str | None, lat: float, lon: float) -> tuple[str | None, bool | None]:
    """(light, legal_light): light in daylight | civil_twilight | dark, or None when untimed."""
    t = parse_local(date_s, time_s)
    if t is None:
        return None, None
    st = sun_times(t.date(), lat, lon)
    if not (st.sunrise and st.sunset and st.civil_dawn and st.civil_dusk):
        return None, None
    if st.sunrise <= t <= st.sunset:
        light = "daylight"
    elif st.civil_dawn <= t < st.sunrise or st.sunset < t <= st.civil_dusk:
        light = "civil_twilight"
    else:
        light = "dark"
    return light, bool(st.legal_start <= t <= st.legal_end)
