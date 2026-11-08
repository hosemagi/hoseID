"""Idempotent schema evolution for hoseID's tables.

Six places used to add a column to a pre-existing table by hand: the review
app's ``init_db`` (``app.py``), the review store's ``asset_id`` backfill
(``reviews.py``), three wildlife-log writer scripts, and hoseserv's wildlife
endpoint. Each read ``PRAGMA table_info`` and, when the column was missing,
ran its own ``ALTER TABLE ADD COLUMN`` and committed. Copying that dance means
the next column like ``sex`` gets added in four places and missed in the
fifth.

This is that dance, once. Callers pass the columns they want; the ones already
present are skipped and the ones missing are added.
"""
from __future__ import annotations

import sqlite3


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    cols: dict[str, str],
) -> list[str]:
    """Ensure ``table`` has every column in ``cols`` (name -> DDL).

    Reads ``PRAGMA table_info`` positionally (row index ``[1]`` is the name) so
    it works whether or not the caller has set a ``row_factory``; runs
    ``ALTER TABLE ADD COLUMN`` for each missing one; commits once, only if it
    added something; and returns the added names. Idempotent: a second call
    with the same (or a superset) of columns returns ``[]``.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = [name for name, ddl in cols.items() if name not in existing]
    if not missing:
        return []
    for name in missing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {cols[name]}")
    conn.commit()
    return missing


# tags.db `reviews`: columns newer than the review app's original CREATE
REVIEWS_COLUMNS: dict[str, str] = {
    # content hash; also added (and filled for old rows) by `hoseid backfill-reviews`
    "asset_id": "TEXT",
    # JSON object tag -> count; a tag absent here counts as 1
    "counts": "TEXT NOT NULL DEFAULT '{}'",
    # named-animal attribution at review time (Al/Ben/Boar...); confidence is
    # mandatory whenever individual is set
    "individual": "TEXT",
    "individual_confidence": "TEXT",
    # M / F; NULL = not set. Carried into the wildlife log by sync_wildlife_log.py
    "sex": "TEXT",
}

# tags.db `exclusion_zones`
EXCLUSION_ZONES_COLUMNS: dict[str, str] = {
    "epoch": "INTEGER NOT NULL DEFAULT 1",
}

# wildlife.db `sightings`: columns newer than migrations/wildlife/001_wildlife.sql.
# hoseserv's wildlife API adds `sex` itself (no import across repos); its
# tests/test_wildlife_schema_drift.py compares that DDL against this dict.
SIGHTINGS_COLUMNS: dict[str, str] = {
    "media_refs": "TEXT NOT NULL DEFAULT '[]'",
    # M / F; NULL = not set
    "sex": "TEXT CHECK (sex IN ('M', 'F'))",
    "weather": "TEXT",
}
