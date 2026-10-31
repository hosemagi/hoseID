"""Arlo camera renames follow through the stack, matched by device id
(hoseid.camera_names)."""
import json
import sqlite3

import pytest

from hoseid import camera_names, station_renames
from hoseid.camera_names import (
    Paths,
    Rename,
    apply_rename,
    reconcile,
    rename_in_map_layers,
)

RULES = {
    "renames": {"Yard": "Cabin Yard"},
    "strip_prefix": "Cabin - ",
    "special": [{"name": "Cabin - Madrone", "before": "Madrone (old spot)",
                 "after": "Madrone", "cutoff": "2026-07-25"}],
}


@pytest.fixture
def p(tmp_path):
    paths = Paths(renames=tmp_path / "station_renames.json",
                  overrides=tmp_path / "station_overrides.json",
                  stations=tmp_path / "stations.json",
                  wildlife_db=tmp_path / "wildlife.db",
                  detections_db=tmp_path / "detections.db")
    paths.renames.write_text(json.dumps(RULES))
    paths.overrides.write_text(json.dumps({"overrides": [
        {"device_id": "MAD1", "station": "Madrone", "start": "2026-07-25T00:00:00Z",
         "end": "2099-01-01T00:00:00Z"},
        {"device_id": "MAD1", "station": "Madrone (old spot)", "start": "2020-01-01T00:00:00Z",
         "end": "2026-07-25T00:00:00Z"}]}))
    paths.stations.write_text(json.dumps({"stations": [
        {"name": "Madrone", "lat": 39.3, "lon": -120.9, "bearing_deg": 277.0},
        {"name": "Cabin Yard", "lat": 39.4, "lon": -120.8}]}))
    w = sqlite3.connect(paths.wildlife_db)
    w.executescript("""
        CREATE TABLE sightings (id INTEGER PRIMARY KEY, station TEXT, date TEXT);
        CREATE TABLE stations (name TEXT PRIMARY KEY, aliases TEXT, camera TEXT,
                               description TEXT NOT NULL, notes TEXT);
        INSERT INTO sightings (station, date) VALUES ('Madrone', '2026-10-01'),
            ('Madrone', '2026-10-02'), ('Madrone (old spot)', '2026-07-01'), ('Cabin Yard', '2026-10-03');
        INSERT INTO stations VALUES ('Madrone', '["Cabin - Madrone (post-07/25)"]', 'arlo', 'at the trailer', NULL),
            ('Madrone (old spot)', '[]', 'arlo', 'old spot', NULL),
            ('Cabin Yard', '[]', 'arlo', 'yard', NULL);
    """)
    w.commit(); w.close()
    d = sqlite3.connect(paths.detections_db)
    d.executescript("""
        CREATE TABLE captures (asset_id TEXT, station TEXT, station_corrected INT);
        INSERT INTO captures VALUES ('a1', 'Madrone', 1), ('a2', 'Cabin - Madrone', 0),
            ('a3', 'Cabin Yard', 0);
    """)
    d.commit(); d.close()
    yield paths
    station_renames._default_rules.cache_clear()


def _rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_rename_moves_the_station_everywhere(p):
    r = apply_rename(Rename("MAD1", "Cabin - Madrone", "Cabin - Gully"), p)
    assert r.applied and (r.old_station, r.new_station) == ("Madrone", "Gully")
    assert (r.sightings, r.captures) == (2, 1)

    rules = json.loads(p.renames.read_text())
    assert rules["special"][0]["after"] == "Gully"                 # old label, new station
    assert rules["special"][0]["before"] == "Madrone (old spot)"   # the old ground stays
    assert station_renames.normalize_station("Cabin - Madrone", "2026-10-01", rules) == "Gully"
    assert station_renames.normalize_station("Cabin - Gully", None, rules) == "Gully"

    ov = json.loads(p.overrides.read_text())["overrides"]
    assert [o["station"] for o in ov] == ["Gully", "Madrone (old spot)"]
    reg = json.loads(p.stations.read_text())["stations"]
    assert reg[0] == {"name": "Gully", "lat": 39.3, "lon": -120.9, "bearing_deg": 277.0}

    assert _rows(p.wildlife_db, "SELECT station, COUNT(*) FROM sightings GROUP BY station ORDER BY station") == [
        ("Cabin Yard", 1), ("Gully", 2), ("Madrone (old spot)", 1)]
    [(aliases,)] = _rows(p.wildlife_db, "SELECT aliases FROM stations WHERE name='Gully'")
    assert json.loads(aliases) == ["Cabin - Madrone (post-07/25)", "Cabin - Madrone"]
    assert _rows(p.detections_db, "SELECT station FROM captures ORDER BY asset_id") == [
        ("Gully",), ("Cabin - Madrone",), ("Cabin Yard",)]


def test_plain_rename_adds_a_label_mapping(p):
    r = apply_rename(Rename("YRD", "Cabin - Yard", "Cabin - Back Yard"), p)
    assert (r.old_station, r.new_station) == ("Cabin Yard", "Back Yard")
    rules = json.loads(p.renames.read_text())
    assert rules["renames"]["Yard"] == "Back Yard"
    assert station_renames.normalize_station("Cabin - Yard", None, rules) == "Back Yard"


def test_renaming_back_restores_the_arlo_name(p):
    apply_rename(Rename("YRD", "Cabin - Yard", "Cabin - Back Yard"), p)
    r = apply_rename(Rename("YRD", "Cabin - Back Yard", "Cabin - Yard"), p)
    assert r.applied and r.new_station == "Yard"
    rules = json.loads(p.renames.read_text())
    assert "Yard" not in rules["renames"]
    assert rules["renames"]["Back Yard"] == "Yard"
    assert _rows(p.wildlife_db, "SELECT COUNT(*) FROM sightings WHERE station='Yard'") == [(1,)]


def test_rename_onto_another_cameras_station_is_refused(p):
    before = {f: f.read_bytes() for f in (p.renames, p.overrides, p.stations)}
    r = apply_rename(Rename("MAD1", "Cabin - Madrone", "Cabin - Yard"), p)
    assert not r.applied and "already exists" in r.skipped
    assert {f: f.read_bytes() for f in before} == before
    assert _rows(p.wildlife_db, "SELECT COUNT(*) FROM sightings WHERE station='Madrone'") == [(2,)]


def test_reconcile_records_first_sighting_then_renames(p):
    seen = {}
    assert reconcile([("MAD1", "Cabin - Madrone"), ("YRD", "Cabin - Yard")], seen, p) == []
    assert seen == {"MAD1": "Cabin - Madrone", "YRD": "Cabin - Yard"}
    [r] = reconcile([("MAD1", "Cabin - Gully"), ("YRD", "Cabin - Yard"), ("", "x")], seen, p)
    assert r.applied and r.new_station == "Gully" and seen["MAD1"] == "Cabin - Gully"
    assert reconcile([("MAD1", "Cabin - Gully")], seen, p) == []


def test_map_layers_swap_the_station():
    calls = []

    class Resp:
        def __init__(self, body=None):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def request(method, url, **kw):
        calls.append((method, url.split("/api/v1/hosemap")[1]))
        if method == "GET":
            return Resp([{"id": "L1", "trailcam_ids": ["Madrone", "Bench"]},
                         {"id": "L2", "trailcam_ids": ["Bench"]}])
        return Resp()

    assert rename_in_map_layers("https://h:8000", "k", "Madrone", "Gully", request) == 1
    assert calls == [("GET", "/layers"), ("POST", "/layers/L1/trailcams/Gully"),
                     ("DELETE", "/layers/L1/trailcams/Madrone")]


def test_map_layer_failure_is_swallowed():
    def boom(*a, **kw):
        raise ConnectionError("down")
    assert rename_in_map_layers("https://h:8000", "k", "a", "b", boom) == 0


def test_summary_text():
    r = Rename("D", "Cabin - A", "Cabin - B", "A", "B", applied=True, sightings=3, captures=4)
    assert r.summary() == "Camera renamed in Arlo: A → B (3 sightings, 4 captures relabelled)"
    assert "not renamed" in Rename("D", "x", "y", skipped="clash").summary()


def test_default_paths_point_at_the_real_files():
    d = Paths()
    assert d.renames == station_renames.RULES_FILE
    assert d.wildlife_db.name == "wildlife.db" and d.detections_db.name == "detections.db"
    assert camera_names.WILDLIFE_DB.parent.name == "tags"


# --- the fetch daemon's hook (fetchers/arlo.py) ---

def test_fetcher_follows_renames_and_reports(p, monkeypatch, tmp_path):
    arlo = pytest.importorskip("fetchers.arlo")
    from fetchers.common import State

    class Cam:
        def __init__(self, device_id, name):
            self.device_id, self.name = device_id, name

    f = arlo.ArloFetcher.__new__(arlo.ArloFetcher)
    f._cfg = {"alerts": {"hoseflow_url": "https://127.0.0.1:8000/api/v1/hoseflow/webhooks/x",
                         "hoseflow_api_key": "k"}}
    f._state = State(tmp_path / "state.json")
    f._excluded_prefixes = ("MH -",)
    f._arlo = type("A", (), {"cameras": [Cam("MAD1", "Cabin - Madrone"), Cam("MH1", "MH - Gate")]})()
    monkeypatch.setattr(camera_names, "Paths", lambda: p)
    notes, alerts = [], []
    monkeypatch.setattr(arlo, "_logbook_note", lambda base, key, text, detail: notes.append((base, text)))
    monkeypatch.setattr(arlo, "notify", lambda cfg, msg: alerts.append(msg))
    monkeypatch.setattr(camera_names, "rename_in_map_layers", lambda *a: 0)

    f.follow_renames()                                   # first sighting: names recorded
    assert f._state.get("arlo_names") == {"MAD1": "Cabin - Madrone"}
    assert notes == [] and alerts == []

    f._arlo.cameras[0].name = "Cabin - Gully"
    f.follow_renames()
    msg = "Camera renamed in Arlo: Madrone → Gully (2 sightings, 1 captures relabelled)"
    assert notes == [("https://127.0.0.1:8000", msg)]
    f._arlo.cameras[0].name = "Cabin - Yard"            # onto another camera's station
    f.follow_renames()
    assert len(notes) == 1 and "already exists" in alerts[0]
