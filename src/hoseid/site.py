"""The site's wall-clock zone and its coordinates: the two things 'where is
the site' needs.

SITE_TZ (an IANA name) is the same variable hoseserv's app.utils.site_tz
reads. Unset, empty or an unknown name falls back to America/Los_Angeles (an
unknown name is warned about once, on stderr). It decides which local day a
capture, sighting or alert lands on, so define the zone here, not file by file.

SITE_LAT/SITE_LON (decimal degrees) are the same variables hoseserv's
app.utils.site_coords reads for its sun, sky and map; site_coords() is the one
place hoseid asks, so sunrise-based answers can't drift from hoseserv's.

TZ is read once at import, so changing SITE_TZ needs a restart of the service
(review app, fetch daemon, wildlife MCP). site_coords() reads the env on each
call, but a launchd service only sees what its plist sets: put SITE_LAT/SITE_LON
(and SITE_TZ, if the zone changes) in each plist's EnvironmentVariables. No
plist sets them today.

No third-party imports: hoseid core runs in several venvs.
"""
from __future__ import annotations

import json
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hoseid import paths

DEFAULT_TZ = "America/Los_Angeles"
_TZ_WARNED: set[str] = set()

# Storm Oak, the same default hoseserv's app.utils.site_coords falls back to.
STORM_OAK_LAT, STORM_OAK_LON = 39.32097, -120.97152
_COORDS_DEFAULT_LOGGED = False


def site_tz() -> ZoneInfo:
    """ZoneInfo for SITE_TZ, else America/Los_Angeles."""
    name = os.environ.get("SITE_TZ", "").strip()
    if not name:
        return ZoneInfo(DEFAULT_TZ)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name not in _TZ_WARNED:
            _TZ_WARNED.add(name)
            print(f"hoseid: SITE_TZ {name!r} is not a known zone; using {DEFAULT_TZ}",
                  file=sys.stderr)
        return ZoneInfo(DEFAULT_TZ)


def _valid_point(lat, lon) -> tuple[float, float] | None:
    """(lat, lon) as floats when both parse and are in range, else None."""
    try:
        f_lat, f_lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if -90.0 <= f_lat <= 90.0 and -180.0 <= f_lon <= 180.0:
        return f_lat, f_lon
    return None


def _env_point(lat_key: str, lon_key: str) -> tuple[float, float] | None:
    return _valid_point(os.environ.get(lat_key), os.environ.get(lon_key))


def site_coords() -> tuple[float, float, str]:
    """(lat, lon, source) of the site, first match wins:

    1. SITE_LAT / SITE_LON (hoseserv's variables).
    2. The first station in stations.json with usable coordinates.
    3. Storm Oak (one stderr line per process).

    A pair counts only when both halves parse and are in range (lat -90..90,
    lon -180..180); anything else falls through to the next step. Never raises.
    """
    global _COORDS_DEFAULT_LOGGED
    pt = _env_point("SITE_LAT", "SITE_LON")
    if pt:
        return pt[0], pt[1], "env SITE_LAT/SITE_LON"

    try:
        reg = json.loads(paths.stations_file().read_text())
        for s in reg.get("stations", []):
            pt = _valid_point(s.get("lat"), s.get("lon"))
            if pt:
                return pt[0], pt[1], f"stations.json:{s.get('name')}"
    except (OSError, ValueError, AttributeError):
        pass   # missing, corrupt or wrongly shaped registry

    if not _COORDS_DEFAULT_LOGGED:
        _COORDS_DEFAULT_LOGGED = True
        print(f"hoseid: SITE_LAT/SITE_LON unset and no station coordinates; "
              f"using Storm Oak {STORM_OAK_LAT}, {STORM_OAK_LON}", file=sys.stderr)
    return STORM_OAK_LAT, STORM_OAK_LON, "fallback (Storm Oak)"


TZ = site_tz()
