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

_WILDLIFE_001 = Path(__file__).resolve().parents[1] / \
    "migrations" / "wildlife" / "001_wildlife.sql"


def _new_wildlife_db(db_path: Path) -> Path:
    """A fresh wildlife.db at the shipped 001 schema, before any writer added
    media_refs/sex/weather."""
    con = sqlite3.connect(db_path)
    con.executescript(_WILDLIFE_001.read_text())
    con.commit()
    con.close()
    return db_path


def test_missing_column_is_added(tmp_path):
    """A column not present is added and returned; a second call adds
    nothing (idempotent)."""
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


def test_ensure_columns_adds_the_real_migration_columns(tmp_path):
    """Against a db built from the shipped migrations/wildlife/001_wildlife.sql
    (which lacks media_refs, sex and weather), ensure_columns adds exactly those
    three from SIGHTINGS_COLUMNS, in order; a second run is a no-op. This is the
    drift the helper exists to close: whatever writer runs first produces the
    same table as whatever runs later."""
    db_path = _new_wildlife_db(tmp_path / "wildlife.db")
    conn = sqlite3.connect(db_path)
    added = ensure_columns(conn, "sightings", SIGHTINGS_COLUMNS)
    assert added == ["media_refs", "sex", "weather"]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sightings)")}
    assert {"media_refs", "sex", "weather"}.issubset(existing)
    second = ensure_columns(conn, "sightings", SIGHTINGS_COLUMNS)
    assert second == []
    conn.close()


def test_noop_call_leaves_the_callers_transaction_open(tmp_path):
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


def test_adding_a_column_leaves_the_callers_transaction_uncommitted(tmp_path):
    """ensure_columns does NOT commit: the caller keeps its own commit
    behaviour. Right after the add, the caller's open transaction is still
    open (the old autocommitting helper left it closed); and the caller's own
    uncommitted INSERT can still be rolled back untouched."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.execute("INSERT INTO t (id) VALUES (1)")  # caller's open transaction
    assert ensure_columns(conn, "t", {"b": "TEXT"}) == ["b"]
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    conn.close()


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
    """SIGHTINGS_COLUMNS['sex'] carries CHECK (sex IN ('M','F')); a value
    outside it is rejected by the column added through ALTER on the real 001
    schema, and M/F/NULL are accepted."""
    db_path = _new_wildlife_db(tmp_path / "wildlife.db")
    conn = sqlite3.connect(db_path)
    ensure_columns(conn, "sightings", {"sex": SIGHTINGS_COLUMNS["sex"]})
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sightings (date, station, species, sex)"
            " VALUES ('2026-01-01', 'deck', 'other', 'X')")
    for ok in ("M", "F", None):
        conn.execute(
            "INSERT INTO sightings (date, station, species, sex)"
            " VALUES ('2026-01-01', 'deck', 'other', ?)",
            (ok,))
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
