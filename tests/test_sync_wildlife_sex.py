"""scripts/sync_wildlife_log.py carries a review's sex into the wildlife log.

New sightings take the review's sex. A later re-review of an already-synced
capture fills sex in only where the log has none — a value set in the log
(possibly by hand on the wildlife page) is never overwritten.

The script's DB paths are module constants; monkeypatch them (AGENTS.md).
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
pytest.importorskip("fastapi")
import app  # noqa: E402  (its init_db creates the reviews table)

DIGEST = "c" * 64


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sync_wildlife_log", ROOT / "scripts" / "sync_wildlife_log.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    wl_db, tags_db, det_db = (tmp_path / n for n in
                              ("wildlife.db", "tags.db", "detections.db"))
    con = sqlite3.connect(wl_db)
    con.executescript((ROOT / "migrations/wildlife/001_wildlife.sql").read_text())
    con.close()
    monkeypatch.setattr(app, "DB_PATH", tags_db)
    app.init_db()
    con = sqlite3.connect(det_db)
    for sql in sorted((ROOT / "migrations/detections").glob("*.sql")):
        con.executescript(sql.read_text())
    con.execute(
        "INSERT INTO runs (run_id, started_at, detector_model, detector_version,"
        " detector_threshold) VALUES ('r','2026-09-01T00:00:00','md','v5',0.45)")
    con.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, n_detections,"
        " has_animal, has_human, has_vehicle, is_empty, media_type,"
        " count_is_lower_bound, sampled_frames)"
        " VALUES (?, 'r', 'Bench', '2026-09-01T13:00:00+00:00', 1,1,0,0,0,'image',0,1)",
        (f"sha256:{DIGEST}",))
    con.commit()
    con.close()
    monkeypatch.setattr(sync, "WILDLIFE_DB", wl_db)
    monkeypatch.setattr(sync, "TAGS_DB", tags_db)
    monkeypatch.setattr(sync, "DETECTIONS_DB", det_db)
    monkeypatch.setattr(sys, "argv", ["sync_wildlife_log.py"])
    return sync, wl_db, tags_db


def _review(tags_db, sex):
    con = sqlite3.connect(tags_db)
    con.execute(
        "INSERT INTO reviews (basename, image, tags, reviewed_at, sex)"
        " VALUES (?, ?, '[\"deer\"]', '2026-09-02T00:00:00Z', ?)",
        (f"{DIGEST}.jpg", f"cc/cc/{DIGEST}.jpg", sex))
    con.commit()
    con.close()


def _sex(wl_db):
    con = sqlite3.connect(wl_db)
    rows = con.execute("SELECT sex FROM sightings").fetchall()
    con.close()
    return [r[0] for r in rows]


def test_new_sighting_takes_review_sex(env):
    sync, wl_db, tags_db = env
    _review(tags_db, "M")
    assert sync.main() == 0
    assert _sex(wl_db) == ["M"]


def test_rereview_fills_unset_sex_only(env):
    sync, wl_db, tags_db = env
    _review(tags_db, None)
    sync.main()
    assert _sex(wl_db) == [None]
    _review(tags_db, "F")            # re-review adds sex
    sync.main()
    assert _sex(wl_db) == ["F"]
    _review(tags_db, "M")            # log already has a value: left alone
    sync.main()
    assert _sex(wl_db) == ["F"]
