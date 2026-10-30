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

The camera still named 'Madrone' in Arlo IS the log's Trailer station
(P, 2026-08-16: rename never happened after the 07/25 relocation).
Pre-07/25 captures from it are the prior spot — different ground — per
the log's own caveat. The camera named 'Trailer' in Arlo also watches
the trailer (two cameras, one station).
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
