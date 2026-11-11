"""hoseid.zones is the one exclusion-zone rule: one case table, run against the
helper and against both callers (fetchers/alerts.py and app.py), so an alert
can't fire for a detection the review UI hides, or the reverse.

hoseserv's Security view keeps a copy (app/api/v1/endpoints/security.py);
sagecode's contracts.toml pairs it with zones.py as 'exclusion-zones'.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py lives at the repo root
from fetchers import alerts  # noqa: E402  (also puts src/ on sys.path)
from hoseid import paths, zones  # noqa: E402

# Zone edges are binary-exact (.25/.5) so the inclusive-edge cases test <=, not
# float rounding.
ZONE_ROWS = [
    # id, device, epoch, category, x, y, w, h
    (1, "camA", 1, "2", .25, .25, .25, .25),   # camA has no camera_epochs row: epoch 1
    (2, "camB", 1, "2", .25, .25, .25, .25),   # camB retired this one (now epoch 2)
    (3, "camB", 2, "1", .25, .25, .25, .25),   # camB's live animal zone
]
EPOCH_ROWS = [("camB", 1), ("camB", 2)]
ACTIVE_IDS = [1, 3]

CASES = [
    # bbox [x, y, w, h], detector class, device, excluded?
    pytest.param((.3, .3, .1, .1), "person", "camA", True, id="centre-inside"),
    pytest.param((.125, .125, .25, .25), "person", "camA", True, id="centre-on-low-edge"),
    pytest.param((.375, .375, .25, .25), "person", "camA", True, id="centre-on-high-edge"),
    pytest.param((.5, .5, .125, .125), "person", "camA", False, id="centre-just-outside"),
    pytest.param((0., 0., .5, .45), "person", "camA", False, id="overlaps-but-centre-outside"),
    pytest.param((.3, .3, .1, .1), "animal", "camA", False, id="wrong-category"),
    pytest.param((.3, .3, .1, .1), "vehicle", "camA", False, id="vehicle-no-zone"),
    pytest.param((.3, .3, .1, .1), "person", "camC", False, id="wrong-device"),
    pytest.param((.3, .3, .1, .1), "person", None, False, id="unknown-device"),
    pytest.param((.3, .3, .1, .1), "person", "camB", False, id="retired-epoch"),
    pytest.param((.3, .3, .1, .1), "animal", "camB", True, id="current-epoch"),
    pytest.param((.3, .3, .1, .1), "mystery", "camA", False, id="unknown-class"),
]


@pytest.fixture
def tags_db(tmp_path):
    path = tmp_path / "tags.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE exclusion_zones (id INTEGER PRIMARY KEY, device_id TEXT,
            epoch INTEGER, category TEXT, x REAL, y REAL, w REAL, h REAL);
        CREATE TABLE camera_epochs (device_id TEXT, epoch INTEGER);
    """)
    conn.executemany("INSERT INTO exclusion_zones VALUES (?,?,?,?,?,?,?,?)", ZONE_ROWS)
    conn.executemany("INSERT INTO camera_epochs VALUES (?,?)", EPOCH_ROWS)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def active(tags_db):
    conn = sqlite3.connect(tags_db)
    try:
        return zones.active_zones(conn)
    finally:
        conn.close()


def test_active_zones_current_epoch_only(active):
    assert sorted(z["id"] for z in active) == ACTIVE_IDS


def test_active_zones_one_device(tags_db):
    conn = sqlite3.connect(tags_db)
    assert [z["id"] for z in zones.active_zones(conn, "camB")] == [3]
    assert conn.row_factory is None, "the caller's connection must not be changed"
    conn.close()


@pytest.mark.parametrize("bbox, cls, device, expected", CASES)
def test_helper(active, bbox, cls, device, expected):
    assert zones.in_zone(bbox, cls, device, active) is expected


@pytest.mark.parametrize("bbox, cls, device, expected", CASES)
def test_alerts_wrappers(tags_db, monkeypatch, bbox, cls, device, expected):
    monkeypatch.setattr(alerts, "TAGS_DB", tags_db)
    active = alerts._active_zones()
    assert sorted(z["id"] for z in active) == ACTIVE_IDS
    x, y, w, h = bbox
    det = {"bbox_x": x, "bbox_y": y, "bbox_w": w, "bbox_h": h, "detector_class": cls}
    assert alerts._in_zone(det, device, active) is expected


@pytest.mark.parametrize("bbox, cls, device, expected", CASES)
def test_review_ui(tags_db, bbox, cls, device, expected):
    """app.py the way queue()/suppressed() use it: zones grouped by device,
    detections carrying the category code pipeline_images() gives them."""
    pytest.importorskip("fastapi")
    import app
    conn = sqlite3.connect(tags_db)
    conn.row_factory = sqlite3.Row
    active = app.active_zones(conn)
    conn.close()
    assert sorted(z["id"] for z in active) == ACTIVE_IDS
    by_dev = {}
    for z in active:
        by_dev.setdefault(z["device_id"], []).append(z)
    det = {"bbox": list(bbox), "category": app._CAT_CODE.get(cls, cls)}
    assert app._excluded(det, by_dev.get(device, [])) is expected


def test_category_map_lives_in_one_place():
    pytest.importorskip("fastapi")
    import app
    assert app._CAT_CODE is zones.CAT_CODE
    assert zones.CAT_CODE == {"animal": "1", "person": "2", "vehicle": "3"}


def test_alerts_without_tags_db_has_no_zones(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "TAGS_DB", tmp_path / "missing.db")
    assert alerts._active_zones() == []


# --- device_for -------------------------------------------------------------

DIGEST = "ab12" + "0" * 60


def _sidecar(root: Path, text: str) -> None:
    p = root / DIGEST[:2] / DIGEST[2:4] / f"{DIGEST}.json"
    p.parent.mkdir(parents=True)
    p.write_text(text)


def test_device_for_reads_configured_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    _sidecar(paths.sidecars_dir(), json.dumps({"device_id": "camA"}))
    assert zones.device_for(DIGEST) == "camA"
    assert alerts._device_for(DIGEST) == "camA"


def test_device_for_explicit_root(tmp_path):
    _sidecar(tmp_path, json.dumps({"device_id": "camB"}))
    assert zones.device_for(DIGEST, tmp_path) == "camB"


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "{}"], ids=["corrupt", "not-a-dict", "no-device"])
def test_device_for_bad_sidecar_is_no_device(tmp_path, text):
    _sidecar(tmp_path, text)
    assert zones.device_for(DIGEST, tmp_path) is None


def test_device_for_missing_sidecar(tmp_path):
    assert zones.device_for(DIGEST, tmp_path) is None


def test_production_sidecar_root_unchanged(monkeypatch):
    """The alert fetcher used Path.home()/trailcam/landing/sidecars; with
    HOSEID_ROOT unset, paths.sidecars_dir() must be that same directory."""
    monkeypatch.delenv("HOSEID_ROOT", raising=False)
    assert paths.sidecars_dir() == Path.home() / "trailcam/landing/sidecars"
