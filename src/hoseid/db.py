"""SQLite access for the derived layer.

Two physically separate databases (invariant 3):
    derived/detections.db   regenerable, written by the pipeline
    tags/tags.db            irreplaceable, written only by the review path

Nothing in this module writes to the tag database. See tags.py.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import paths

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent readers during a batch run
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _apply(conn: sqlite3.Connection, sql_file: Path) -> None:
    conn.executescript(sql_file.read_text())
    conn.commit()


@contextmanager
def detections(create: bool = True):
    p = paths.detections_db()
    conn = _connect(p)
    try:
        if create:
            _apply(conn, MIGRATIONS / "001_detections.sql")
        yield conn
    finally:
        conn.close()


@contextmanager
def tags(create: bool = True):
    p = paths.tags_db()
    conn = _connect(p)
    try:
        if create:
            _apply(conn, MIGRATIONS / "tags_001.sql")
        yield conn
    finally:
        conn.close()


def start_run(conn: sqlite3.Connection, *, run_id: str, started_at: str,
              detector_model: str, detector_version: str,
              detector_threshold: float,
              classifier_model: str | None = None, classifier_version: str | None = None,
              geofence_country: str | None = None, geofence_admin1: str | None = None,
              taxon_map_version: str | None = None, notes: str | None = None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, started_at, detector_model, detector_version, classifier_model,
            classifier_version, geofence_country, geofence_admin1, detector_threshold,
            taxon_map_version, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, started_at, detector_model, detector_version, classifier_model,
         classifier_version, geofence_country, geofence_admin1, detector_threshold,
         taxon_map_version, notes))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, finished_at: str) -> None:
    conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (finished_at, run_id))
    conn.commit()


def already_processed(conn: sqlite3.Connection, asset_id: str, run_id: str) -> bool:
    """Idempotency check: a re-run over the landing zone skips work already done for this run_id."""
    cur = conn.execute("SELECT 1 FROM captures WHERE asset_id=? AND run_id=?", (asset_id, run_id))
    return cur.fetchone() is not None
