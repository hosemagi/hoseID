"""The shared schema-evolution helper: ensure_columns, and that the review
app actually imports it (that was the mistake that sank the first build).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py lives at the repo root
pytest.importorskip("fastapi")  # app.py is a FastAPI app; skip where it isn't installed
import app
from hoseid.schema import SIGHTINGS_COLUMNS, ensure_columns

# --- ensure_columns -----------------------------------------------------------

def test_missing_column_is_added(tmp_path):
    """A column not present is added, committed, and returned; a second call
    adds nothing (idempotent)."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    conn.commit()

    added = ensure_columns(conn, "t", {"b": "TEXT", "c": "TEXT NOT NULL DEFAULT 'x'"})
    assert added == ["b", "c"]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
    assert existing == {"id", "a", "b", "c"}

    added_again = ensure_columns(conn, "t", {"b": "TEXT", "c": "TEXT NOT NULL DEFAULT 'x'"})
    assert added_again == []


def test_commit_only_when_something_changed(tmp_path):
    """A call that adds no column must not commit: the caller's open
    transaction is left intact (and can still be rolled back)."""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, b TEXT)")
    conn.commit()
    conn.execute("INSERT INTO t (id) VALUES (1)")  # caller's open transaction
    assert conn.in_transaction
    assert ensure_columns(conn, "t", {"b": "TEXT"}) == []
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_works_with_a_row_factory_and_positional_access(tmp_path):
    """ensure_columns reads PRAGMA positionally (row[1]), so it works whether
    the caller set a Row factory or not."""
    db_path = tmp_path / "t.db"

    row_conn = sqlite3.connect(db_path)
    row_conn.row_factory = sqlite3.Row
    row_conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    row_conn.commit()
    assert ensure_columns(row_conn, "t", {"b": "TEXT"}) == ["b"]
    row_conn.close()

    plain_conn = sqlite3.connect(db_path)
    assert ensure_columns(plain_conn, "t", {"b": "TEXT"}) == []
    plain_conn.close()


# --- the sex CHECK constraint -------------------------------------------------

def test_sex_check_constraint_rejects_other_values(tmp_path):
    """SIGHTINGS_COLUMNS['sex'] carries CHECK (sex IN ('M', 'F')); a value
    outside it is rejected by the column added through ALTER, and M/F/NULL
    are accepted."""
    db_path = tmp_path / "wl.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sightings (sighting_id INTEGER PRIMARY KEY, date TEXT)")
    conn.commit()
    ensure_columns(conn, "sightings", {"sex": SIGHTINGS_COLUMNS["sex"]})

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sightings (date, sex)"
                     " VALUES ('2026-01-01', 'X')")
    for ok in ("M", "F", None):
        conn.execute("INSERT INTO sightings (date, sex) VALUES ('2026-01-01', ?)", (ok,))
    conn.close()


# --- legacy reviews table + init_db ------------------------------------------

def _legacy_reviews(conn: sqlite3.Connection) -> None:
    """The review app's tables as they stood before the newer columns:
    reviews without asset_id/counts/individual/individual_confidence/sex,
    exclusion_zones without epoch."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY,
            basename TEXT NOT NULL,
            image TEXT NOT NULL,
            device_id TEXT,
            captured_at TEXT,
            tags TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            md_max_conf REAL,
            reviewed_at TEXT NOT NULL,
            reviewer TEXT NOT NULL DEFAULT 'p'
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exclusion_zones (
            id INTEGER PRIMARY KEY,
            device_id TEXT NOT NULL,
            category TEXT NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL,
            w REAL NOT NULL, h REAL NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.commit()


def test_init_db_adds_the_new_columns_to_a_legacy_reviews_table(
        tmp_path, monkeypatch):
    """A database with the old reviews schema gets asset_id, counts,
    individual, individual_confidence and sex when app.init_db() runs.
    app.init_db opens its connection with row_factory = sqlite3.Row, so this
    also covers a Row-factory connection going through ensure_columns."""
    db_path = tmp_path / "tags.db"
    conn = sqlite3.connect(db_path)
    _legacy_reviews(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr(app, "DETECTIONS_DB", db_path)
    monkeypatch.setattr(app, "DB_PATH", db_path)
    app.init_db()

    cols = {r[1] for r in sqlite3.connect(db_path)
            .execute("PRAGMA table_info(reviews)")}
    assert cols >= {"asset_id", "counts", "individual",
                    "individual_confidence", "sex"}
    check = sqlite3.connect(db_path)
    zcols = {r[1] for r in check.execute("PRAGMA table_info(exclusion_zones)")}
    assert "epoch" in zcols
    indexes = {r[0] for r in check.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_reviews_asset" in indexes


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """A second init_db() on an up-to-date database adds nothing and fails
    on nothing (ALTER of an existing column would raise)."""
    db_path = tmp_path / "tags.db"
    monkeypatch.setattr(app, "DETECTIONS_DB", db_path)
    monkeypatch.setattr(app, "DB_PATH", db_path)
    app.init_db()
    app.init_db()
    conn = sqlite3.connect(db_path)
    assert ensure_columns(conn, "reviews", app.REVIEWS_COLUMNS) == []
    assert ensure_columns(conn, "exclusion_zones", app.EXCLUSION_ZONES_COLUMNS) == []
