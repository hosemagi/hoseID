"""Camera name -> wildlife-log station name, from config/station_renames.json.

One data file, two readers: scripts/sync_wildlife_log.py (the log sync) and
hoseserv's security view (app/api/v1/endpoints/security.py), which reads this
repo's copy of the file. Add a line to the JSON per rename; see
docs/CAMERA_RENAMES.md.

    renames       old label -> new station, applied after the prefix strip
    strip_prefix  Arlo's 'Cabin - ' camera-name prefix
    special       date splits for a camera that moved to different ground:
                  {name, before, after, cutoff}; a capture on or after the
                  cutoff (local YYYY-MM-DD) is `after`, earlier is `before`

Stations are named after the Arlo camera (P, 2026-10-30): 'Cabin - Madrone'
is station Madrone (at the trailer since its 07/25 move; earlier captures
are 'Madrone (old spot)', different ground), and 'Cabin - Trailer' is
station Trailer. A camera renamed in the Arlo app is followed automatically
by device id (hoseid.camera_names, run by the fetch daemon), which edits this
file so captures stored under the old label still land on the new station.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "station_renames.json"


def load_rules(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or RULES_FILE).read_text())


@lru_cache(maxsize=1)
def _default_rules() -> dict:
    return load_rules()


def normalize_station(name: str, date: str | None = None,
                      rules: dict | None = None) -> str:
    """'Cabin - Yard' -> 'Cabin Yard', 'Cabin - Crossroads' -> 'Crossroads';
    unknown names pass through. A None date means today's name (`after`)."""
    rules = rules if rules is not None else _default_rules()
    for s in rules.get("special", []):
        if name == s["name"]:
            return s["after"] if (date is None or date >= s["cutoff"]) else s["before"]
    prefix = rules.get("strip_prefix", "")
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    return rules.get("renames", {}).get(name, name)
