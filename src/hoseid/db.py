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


def migrate(conn: sqlite3.Connection, which: str) -> int:
    """Apply pending migrations in filename order, tracked by PRAGMA user_version.

    A user_version counter rather than CREATE-IF-NOT-EXISTS, because migrations now include
    ALTER TABLE, which is not idempotent -- re-running it raises "duplicate column name". The
    counter is the number of migration files applied, so each file runs exactly once.
    """
    d = MIGRATIONS / which
    files = sorted(d.glob("*.sql"))
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = 0
    for i, f in enumerate(files, start=1):
        if i <= current:
            continue
        conn.executescript(f.read_text())
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()
        applied += 1
    return applied


@contextmanager
def detections(create: bool = True):
    p = paths.detections_db()
    conn = _connect(p)
    try:
        if create:
            migrate(conn, "detections")
        yield conn
    finally:
        conn.close()


@contextmanager
def tags(create: bool = True):
    p = paths.tags_db()
    conn = _connect(p)
    try:
        if create:
            migrate(conn, "tags")
        yield conn
    finally:
        conn.close()


def start_run(conn: sqlite3.Connection, *, run_id: str, started_at: str,
              detector_model: str, detector_version: str,
              detector_threshold: float,
              classifier_model: str | None = None, classifier_version: str | None = None,
              geofence_country: str | None = None, geofence_admin1: str | None = None,
              taxon_map_version: str | None = None, notes: str | None = None,
              sampling_policy: str | None = None) -> None:
    # UPSERT, never REPLACE: captures/detections cascade on runs deletion, so
    # `INSERT OR REPLACE` silently wiped a run's prior work whenever the same
    # run_id was started again — turning the standing incremental run
    # (run_id='nightly') into a full reprocess of the landing zone each night.
    # Re-starting a run keeps its rows and original started_at; the mutable
    # provenance fields update to the current invocation.
    conn.execute(
        """INSERT INTO runs
           (run_id, started_at, detector_model, detector_version, classifier_model,
            classifier_version, geofence_country, geofence_admin1, detector_threshold,
            taxon_map_version, notes, sampling_policy)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id) DO UPDATE SET
             detector_model=excluded.detector_model,
             detector_version=excluded.detector_version,
             detector_threshold=excluded.detector_threshold,
             notes=excluded.notes,
             sampling_policy=excluded.sampling_policy,
             finished_at=NULL""",
        (run_id, started_at, detector_model, detector_version, classifier_model,
         classifier_version, geofence_country, geofence_admin1, detector_threshold,
         taxon_map_version, notes, sampling_policy))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, finished_at: str) -> None:
    conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (finished_at, run_id))
    conn.commit()


def already_processed(conn: sqlite3.Connection, asset_id: str, run_id: str) -> bool:
    """Idempotency check: a re-run over the landing zone skips work already done for this run_id."""
    cur = conn.execute("SELECT 1 FROM captures WHERE asset_id=? AND run_id=?", (asset_id, run_id))
    return cur.fetchone() is not None
