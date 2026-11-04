"""config/station_renames.json: one set of camera rename rules for the log
sync and hoseserv's security view. hoseserv's tests/test_security.py runs
the same table against this file."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
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
    ("Cabin - Madrone", "2026-07-25", "Madrone"),
    ("Cabin - Madrone", None, "Madrone"),
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


# --- serials resolve from the registry instead of a hand-copied dict ---------
def _write_registry(tmp_path, entries):
    """A stations.json fixture: list of (station_name, device_id) pairs."""
    landing = tmp_path / "landing"
    landing.mkdir(parents=True, exist_ok=True)
    (landing / "stations.json").write_text(json.dumps(
        {"stations": [{"name": name, "device_id": device_id,
                       "vendor": "reveal"}
                      for name, device_id in entries]}))
    return tmp_path


def test_registry_serial_resolves(tmp_path, monkeypatch):
    """A camera serial in the registry maps to its station, incl. the newer
    Enchanted Forest cam that the old hand-copied dict never reached."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Lost Woods", "016579006023894"),
                               ("Enchanted Forest", "016578007553206")])
    assert sync.normalize_station("016578007553206") == "Enchanted Forest"
    assert sync.device_station("016578007553206") == "Enchanted Forest"


def test_registry_serial_passes_through_unknown_serial(tmp_path, monkeypatch):
    """A serial the registry does not know is returned unchanged, so the sync
    still records something instead of swallowing the id."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Enchanted Forest", "016578007553206")])
    assert sync.normalize_station("016579006999999") == "016579006999999"
    assert sync.device_station("016579006999999") is None


def test_legacy_device_ids_resolve_from_the_registry(tmp_path, monkeypatch):
    """Serials map through the registry now, not a hand-copied dict; the
    old Lost Woods cam and Storm Oak both resolve."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [
        ("Lost Woods", "016579006023894"),   # was South Clearing
        ("Storm Oak", "016579006078088"),
    ])
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


# --- end-to-end sync: a blank station never writes "" -------------------------
def _init_db(db_path, migrations):
    conn = sqlite3.connect(db_path)
    conn.executescript(migrations)
    conn.commit()
    return conn


def _run_sync(monkeypatch, tmp_path, *, det_station=None, det_device_id=None,
              legacy_device_id=None, legacy_serial=None):
    """Point the sync at fresh tmp DBs and run main(); return the sightings.

    det_station seeds a pipeline capture (detections.db) whose captures.station
    column holds this value — "" reproduces sighting-479's blank.
    legacy_device_id seeds a legacy Reveal review (tags.db) carrying only a
    device id, so basename = f"legacy-<id>.jpg".
    """
    wl = tmp_path / "wildlife.db"
    tags = tmp_path / "tags.db"
    det = tmp_path / "detections.db"
    for p in (wl, tags, det):
        p.write_text("")
    monkeypatch.setattr(sync, "WILDLIFE_DB", wl)
    monkeypatch.setattr(sync, "TAGS_DB", tags)
    monkeypatch.setattr(sync, "DETECTIONS_DB", det)
    # main() runs argparse; pytest's own args would look like an unknown flag
    monkeypatch.setattr("sys.argv", ["sync_wildlife_log"])
    _init_db(tags, TAGS_MIGRATIONS)
    _init_db(wl, WILDLIFE_MIGRATIONS)

    if det_station is not None:
        _init_db(det, DETMIGRATIONS)
        det_conn = sqlite3.connect(det)
        det_conn.execute("INSERT INTO runs (run_id) VALUES ('r1')")
        digest = "a" * 64
        det_conn.execute(
            "INSERT INTO captures (asset_id, run_id, station, capture_time, "
            "has_animal, n_detections) VALUES (?, 'r1', ?, ?, 1, 1)",
            (f"sha256:{digest}", det_station, "2026-08-22T10:00:00Z"))
        det_conn.commit(); det_conn.close()
        # a review whose basename stem is the digest drives the pipeline branch
        tags_conn = sqlite3.connect(tags)
        tags_conn.execute(
            "INSERT INTO reviews (id, basename, device_id, captured_at, tags, "
            "counts, notes) VALUES (2, ?, ?, '2026-08-22', "
            "'[\"jackrabbit\"]', '{}', '')",
            (f"{digest}.jpg", det_device_id))
        tags_conn.commit(); tags_conn.close()

    if legacy_device_id is not None:
        basename = f"legacy-{legacy_device_id}.jpg"
        tags_conn = sqlite3.connect(tags)
        tags_conn.execute(
            "INSERT INTO reviews (id, basename, device_id, captured_at, tags, "
            "counts, notes) VALUES (1, ?, ?, ?, '[\"jackrabbit\"]', '{}', '')",
            (basename, legacy_device_id, "2026-08-22"))
        tags_conn.commit(); tags_conn.close()

    if legacy_serial is not None:
        # a legacy review carrying only a serial the registry maps to a station
        tags_conn = sqlite3.connect(tags)
        tags_conn.execute(
            "INSERT INTO reviews (id, basename, device_id, captured_at, tags, "
            "counts, notes) VALUES (3, ?, ?, ?, '[\"jackrabbit\"]', '{}', '')",
            (f"legacy-{legacy_serial}.jpg", legacy_serial, "2026-08-22"))
        tags_conn.commit(); tags_conn.close()

    assert sync.main() == 0
    wl_conn = sqlite3.connect(wl)
    wl_conn.row_factory = sqlite3.Row
    return [dict(r) for r in wl_conn.execute("SELECT * FROM sightings")]


TAGS_MIGRATIONS = """
CREATE TABLE reviews (id INTEGER PRIMARY KEY, asset_id TEXT, basename TEXT NOT NULL,
    image TEXT NOT NULL DEFAULT '', device_id TEXT, captured_at TEXT,
    tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
    md_max_conf REAL, reviewed_at TEXT NOT NULL DEFAULT '2026-08-22T00:00:00Z',
    reviewer TEXT NOT NULL DEFAULT 'p', counts TEXT NOT NULL DEFAULT '{}');
"""

WILDLIFE_MIGRATIONS = """
CREATE TABLE individuals (name TEXT PRIMARY KEY, species TEXT NOT NULL,
    first_seen TEXT, description TEXT NOT NULL DEFAULT '', id_basis TEXT NOT NULL
    DEFAULT '', status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sightings (sighting_id INTEGER PRIMARY KEY, date TEXT NOT NULL,
    time TEXT, station TEXT NOT NULL, species TEXT NOT NULL, individual TEXT,
    individual_confidence TEXT, count INTEGER NOT NULL DEFAULT 1,
    count_is_visits INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL
    DEFAULT 'camera', category TEXT NOT NULL DEFAULT 'other',
    capture_asset_id TEXT, auto INTEGER NOT NULL DEFAULT 0, harvest INTEGER NOT
    NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', doc_ref TEXT);
"""

DETMIGRATIONS = """
CREATE TABLE runs (run_id TEXT PRIMARY KEY);
CREATE TABLE captures (asset_id TEXT NOT NULL, run_id TEXT NOT NULL,
    station TEXT NOT NULL, station_corrected INTEGER NOT NULL DEFAULT 0,
    capture_time TEXT NOT NULL, time_trusted INTEGER NOT NULL DEFAULT 0,
    n_detections INTEGER NOT NULL DEFAULT 0, has_animal INTEGER NOT NULL
    DEFAULT 0, has_human INTEGER NOT NULL DEFAULT 0, has_vehicle INTEGER NOT
    NULL DEFAULT 0, is_empty INTEGER NOT NULL DEFAULT 1, detector_ms REAL,
    PRIMARY KEY (asset_id, run_id));
"""


def test_blank_pipeline_station_writes_unknown_not_empty(monkeypatch, tmp_path):
    """A pipeline capture whose captures.station is '' must not write a sighting
    with an empty station — it must be visible in per-station views."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Enchanted Forest", "016578007553206")])
    rows = _run_sync(monkeypatch, tmp_path, det_station="",
                     det_device_id="016579006999999")
    assert len(rows) == 1
    assert rows[0]["station"] == "unknown"


def test_blank_pipeline_station_falls_back_to_review_serial(monkeypatch, tmp_path):
    """A blank captures.station resolves the review's device_id (the sidecar
    serial) through the registry before giving up to 'unknown'."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Enchanted Forest", "016578007553206")])
    rows = _run_sync(monkeypatch, tmp_path, det_station="",
                     det_device_id="016578007553206")
    assert [r["station"] for r in rows] == ["Enchanted Forest"]


def test_pipeline_station_serial_resolves_through_registry(monkeypatch, tmp_path):
    """A pipeline capture whose captures.station holds a serial resolves that
    serial through the registry instead of writing the raw id."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Lost Woods", "016579006023894")])
    rows = _run_sync(monkeypatch, tmp_path, det_station="016579006023894")
    assert rows[0]["station"] == "Lost Woods"


def test_legacy_blank_device_id_writes_unknown(monkeypatch, tmp_path):
    """A legacy review with no station and no device id writes 'unknown', never
    ''."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    rows = _run_sync(monkeypatch, tmp_path, legacy_device_id="")
    assert rows[0]["station"] == "unknown"


def test_legacy_device_id_resolves_through_registry(monkeypatch, tmp_path):
    """A legacy review carrying only a serial resolves it through the registry."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _write_registry(tmp_path, [("Lost Woods", "016579006023894")])
    rows = _run_sync(monkeypatch, tmp_path, legacy_serial="016579006023894")
    assert rows[0]["station"] == "Lost Woods"
