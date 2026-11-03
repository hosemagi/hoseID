"""The site's wall-clock zone, the one local time every hoseid module uses.

SITE_TZ (an IANA name) is the same variable hoseserv's app.utils.site_tz
reads. Unset, empty or an unknown name falls back to America/Los_Angeles (an
unknown name is warned about once, on stderr). It decides which local day a
capture, sighting or alert lands on, so define the zone here, not file by file.

TZ is read once at import: changing SITE_TZ needs a restart of the service
(review app, fetch daemon, wildlife MCP). The launchd plists don't set it
today; add it to their EnvironmentVariables if the zone ever changes.

No third-party imports: hoseid core runs in several venvs.
"""
from __future__ import annotations

import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Los_Angeles"
_TZ_WARNED: set[str] = set()


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


TZ = site_tz()
