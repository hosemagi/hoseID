"""The review app's GET /health liveness probe.

hoseserv's stack panel probes a service's /health every few seconds. For
hoseID review that probe must prove the review DB is readable, not just that
the static UI is served. /health opens the tags DB and the detections DB
read-only and reports the unreviewed capture count; a 503 means a DB could not
be read. _QuietHealth stops uvicorn logging every successful poll.

Per AGENTS.md: import app and monkeypatch the hardcoded module constants
directly (no paths.py refactor); init_db() at import is idempotent and safe.
"""
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py lives at the repo root
pytest.importorskip("fastapi")  # app.py is a FastAPI app; skip where it isn't installed
import app


def _decode(resp):
    """A JSONResponse: status + parsed body."""
    return resp.status_code, json.loads(resp.body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    det_db, tags_db = tmp_path / "detections.db", tmp_path / "tags.db"

    conn = sqlite3.connect(det_db)
    mig = Path(__file__).parent.parent / "migrations" / "detections"
    for sql in sorted(mig.glob("*.sql")):
        conn.executescript(sql.read_text())
    conn.execute(
        "INSERT INTO runs (run_id, started_at, detector_model, detector_version,"
        " detector_threshold) VALUES (?,?,?,?,?)",
        ("run-1", "2026-08-30T02:00:00", "md_v5", "v5", 0.45))
    conn.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, n_detections,"
        " has_animal, has_human, has_vehicle, is_empty) VALUES "
        "(?,?,?,?,?,?,?,?,?)",
        ("capture:" + "a" * 64, "run-1", "Sage Creek", "2026-08-28T04:12:07", 0, 0, 0,
         0, 1))
    conn.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, n_detections,"
        " has_animal, has_human, has_vehicle, is_empty) VALUES "
        "(?,?,?,?,?,?,?,?,?)",
        ("capture:" + "b" * 64, "run-1", "Sage Creek", "2026-08-29T05:00:00", 0, 0, 0,
         0, 1))
    conn.commit()
    conn.close()

    monkeypatch.setattr(app, "DB_PATH", tags_db)
    monkeypatch.setattr(app, "DETECTIONS_DB", det_db)
    app.init_db()
    return {"det_db": det_db, "tags_db": tags_db}


def _review(tags_db, asset_id):
    conn = sqlite3.connect(tags_db)
    conn.execute(
        "INSERT INTO reviews (asset_id, basename, image, tags, counts, reviewed_at)"
        " VALUES (?,?,?,?,?,?)",
        (asset_id, f"{asset_id}.jpg", f"{asset_id}.jpg",
         json.dumps(["deer"]), json.dumps({"deer": 1}), "2026-08-30T03:00:00"))
    conn.commit()
    conn.close()


def test_health_ok_reports_queue_depth(env):
    """Two captures, one reviewed: the probe is up and queue_depth is the
    unreviewed count (captures minus reviewed)."""
    _review(env["tags_db"], "capture:" + "a" * 64)
    status, body = _decode(app.health())
    assert status == 200
    assert body["ok"] is True
    assert body["queue_depth"] == 1   # 2 captures - 1 reviewed


def test_health_queue_depth_counts_distinct_unreviewed_captures(env):
    """captures holds one row per capture per run: a re-run must not count a
    capture twice. A review whose asset_id is not a capture (legacy, or since
    pruned) must not hide a real unreviewed capture (next test). queue_depth
    is the set difference, not a row-count subtraction."""
    conn = sqlite3.connect(env["det_db"])
    conn.execute(
        "INSERT INTO runs (run_id, started_at, detector_model, detector_version,"
        " detector_threshold) VALUES (?,?,?,?,?)",
        ("run-2", "2026-08-31T02:00:00", "md_v5", "v5", 0.45))
    conn.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, n_detections,"
        " has_animal, has_human, has_vehicle, is_empty) VALUES "
        "(?,?,?,?,?,?,?,?,?)",
        ("capture:" + "b" * 64, "run-2", "Sage Creek", "2026-08-29T05:00:00", 0, 0, 0,
         0, 1))
    conn.commit()
    conn.close()
    _review(env["tags_db"], "capture:" + "a" * 64)
    status, body = _decode(app.health())
    assert status == 200
    assert body["queue_depth"] == 1   # only b, once (3 rows - 1 review would be 2)


def test_health_review_of_a_non_capture_does_not_shrink_the_queue(env):
    _review(env["tags_db"], "capture:" + "f" * 64)   # not in captures
    status, body = _decode(app.health())
    assert status == 200
    assert body["queue_depth"] == 2   # a and b are both unreviewed


def test_health_garbage_tags_db_returns_503(env, monkeypatch):
    bad = env["tags_db"].parent / "garbage-tags.db"
    bad.write_bytes(b"this is not a sqlite database" * 200)
    monkeypatch.setattr(app, "DB_PATH", bad)
    status, body = _decode(app.health())
    assert status == 503
    assert body["ok"] is False
    assert "tags.db" in body["error"]


def test_health_queue_depth_is_zero_when_nothing_unreviewed(env):
    _review(env["tags_db"], "capture:" + "a" * 64)
    _review(env["tags_db"], "capture:" + "b" * 64)
    status, body = _decode(app.health())
    assert status == 200
    assert body["ok"] is True
    assert body["queue_depth"] == 0


def test_health_missing_detections_db_reports_empty_queue(env, monkeypatch):
    """A missing detections DB is a queue of 0, matching pipeline_images():
    health stays up but the review DB alone is what 200 proves."""
    monkeypatch.setattr(app, "DB_PATH", env["tags_db"])
    monkeypatch.setattr(app, "DETECTIONS_DB", Path("/no/such/detections.db"))
    status, body = _decode(app.health())
    assert status == 200
    assert body["ok"] is True
    assert body["queue_depth"] == 0


def test_health_tags_db_unreadable_returns_503(env, monkeypatch):
    """A tags DB that cannot be opened (a missing file) is a 503: the probe
    says 'down' rather than pretending the queue is empty."""
    monkeypatch.setattr(app, "DB_PATH", Path("/no/such/tags.db"))
    status, body = _decode(app.health())
    assert status == 503
    assert body["ok"] is False
    assert "tags.db" in body["error"]


def test_health_garbage_detections_db_returns_503(env, monkeypatch):
    """A detections DB that is not a real sqlite file is a 503."""
    bad = env["det_db"].parent / "garbage.db"
    bad.write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr(app, "DETECTIONS_DB", bad)
    status, body = _decode(app.health())
    assert status == 503
    assert body["ok"] is False
    assert "detections.db" in body["error"]


def test_health_503_does_not_create_a_missing_db(env, monkeypatch):
    """connect_ro opens read-only, so a missing tags DB must not be created
    as a side effect (a write path would defeat the probe)."""
    missing = env["tags_db"].parent / "never-created.db"
    monkeypatch.setattr(app, "DB_PATH", missing)
    app.health()
    assert not missing.exists()


def test_health_route_precedes_the_static_mount(env, monkeypatch):
    """/health must be registered before the static mount so the mount does
    not shadow it with its catch-all. Verified against the ASGI route list."""
    from starlette.routing import Mount

    monkeypatch.setattr(app, "DB_PATH", env["tags_db"])
    monkeypatch.setattr(app, "DETECTIONS_DB", env["det_db"])
    app.init_db()
    routes = app.app.routes
    health_i = next(i for i, r in enumerate(routes) if getattr(r, "path", None) == "/health")
    static_i = next(i for i, r in enumerate(routes) if isinstance(r, Mount))
    assert health_i < static_i


# --- _QuietHealth: the successful health poll is quieted --------------------


def _access(path, status, created=None):
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5555", "GET", path, "1.1", status), None,
    )
    if created is not None:
        rec.created = created
    return rec


def test_quiet_health_drops_only_successful_health():
    f = app._QuietHealth()
    assert f.filter(_access("/health", 200)) is True   # first success kept
    assert f.filter(_access("/health", 200)) is False   # next dropped
    assert f.filter(_access("/health?x=1", 200)) is False
    assert f.filter(_access("/health", 204)) is False
    assert f.filter(_access("/health", 503)) is True
    assert f.filter(_access("/api/queue", 200)) is True
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain", None, None)
    assert f.filter(plain) is True


def test_quiet_health_installed_once():
    app._install_access_filter()
    app._install_access_filter()
    access = logging.getLogger("uvicorn.access")
    assert sum(type(f).__name__ == "_QuietHealth" for f in access.filters) == 1
    access.filter(_access("/health", 200))
    assert not access.filter(_access("/health", 200))
    assert access.filter(_access("/health", 503))
