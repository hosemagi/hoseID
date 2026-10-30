"""config/station_renames.json: one set of camera rename rules for the log
sync and hoseserv's security view. hoseserv's tests/test_security.py runs
the same table against this file."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / "config" / "station_renames.json"


def _sync():
    spec = importlib.util.spec_from_file_location(
        "sync_wildlife_log", ROOT / "scripts" / "sync_wildlife_log.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync = _sync()

# (camera name, local day, log station); kept identical in hoseserv
TABLE = [
    ("Cabin - Madrone", "2026-07-24", "Madrone (old spot)"),
    ("Cabin - Madrone", "2026-07-25", "Trailer"),
    ("Cabin - Madrone", None, "Trailer"),
    ("Cabin - Yard", "2026-09-01", "Cabin Yard"),
    ("Cabin - Crossroads", "2026-09-01", "Crossroads"),
    ("CB5", "2026-09-01", "Enchanted Forest"),
    ("South Clearing", "2026-09-01", "Lost Woods"),
    ("Storm Oak", "2026-09-01", "Storm Oak"),
    ("Nowhere Cam", "2026-09-01", "Nowhere Cam"),
]


@pytest.mark.parametrize("name,day,expected", TABLE)
def test_sync_normalizes_camera_names_from_the_json(name, day, expected):
    assert sync.normalize_station(name, day) == expected


def test_legacy_device_ids_still_map_before_the_renames():
    assert sync.normalize_station("016579006023894", "2026-08-01") == "Lost Woods"
    assert sync.normalize_station("016579006078088") == "Storm Oak"


def test_the_sync_reads_this_repos_rules_file():
    assert sync.station_renames.RULES_FILE == RULES_FILE


def test_rules_file_is_well_formed():
    rules = json.loads(RULES_FILE.read_text())
    assert set(rules) == {"renames", "strip_prefix", "special"}
    assert rules["strip_prefix"] == "Cabin - "
    assert all(isinstance(k, str) and isinstance(v, str) and v
               for k, v in rules["renames"].items())
    for s in rules["special"]:
        assert set(s) == {"name", "before", "after", "cutoff"}, s
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", s["cutoff"]), s
        assert all(isinstance(s[k], str) and s[k] for k in ("name", "before", "after"))


def test_explicit_rules_override_the_file():
    rules = {"renames": {"Old": "New"}, "strip_prefix": "Cabin - ",
             "special": [{"name": "Cabin - X", "before": "A", "after": "B",
                          "cutoff": "2026-01-02"}]}
    n = sync.station_renames.normalize_station
    assert n("Cabin - Old", "2026-05-01", rules) == "New"
    assert n("Cabin - X", "2026-01-01", rules) == "A"
    assert n("Cabin - X", "2026-01-02", rules) == "B"
    assert n("CB5", None, rules) == "CB5"
