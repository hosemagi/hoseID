"""Atomic file writes, and the read-only and writable sqlite openers.

Atomic writes: temp file in the destination directory, fsync, os.replace. A
reader sees either the old file or the new one, never half of either; on any
failure the temp file is removed and the error re-raised, and an existing
destination is left untouched. Temp names start with ".tmp-", which landing's
directory scans skip.

mkstemp creates files 0600. Pass `mode` when another user or service reads
the result (hoseserv reads the station-health files, for example).

The read-only opener (connect_ro) is here next to the atomic-write helper
rather than in hoseid.paths: paths lives alongside the pipeline stages, and a
peer importing it from a daemon (the review app, the fetch daemon) would pull
the whole pipeline. connect_ro opens a sqlite file mode=ro so the write is
refused at the connection; as_uri() percent-encodes the path, so a space, '#'
or '?' in it is safe; and a 5 s busy_timeout lets a read land behind a
concurrent writer instead of failing with 'database is locked' (the nightly
pipeline and the fetch daemon both write the databases these readers open).

The writable opener (connect_rw) gives every writer of wildlife.db (the review
app, the nightly log sync, the reconcile, repair and weather backfill scripts)
the same 30 s busy_timeout that db.py and camera_names.py already use. A bare
sqlite3.connect() gets sqlite3's 5 s default, so a long nightly write could fail
a concurrent review save with 'database is locked' depending on which path got
there first. It does not set WAL or foreign_keys; db.py keeps those for the
pipeline."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path


def atomic_write_bytes(dest: Path, data: bytes, mode: int | None = None) -> None:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".tmp-{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dest)          # atomic within a filesystem
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(dest: Path, text: str, mode: int | None = None) -> None:
    atomic_write_bytes(dest, text.encode("utf-8"), mode=mode)


def atomic_write_json(dest: Path, data, indent: int | None = 2,
                      ensure_ascii: bool = False, mode: int | None = None) -> None:
    """JSON plus a trailing newline."""
    atomic_write_text(dest, json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n",
                      mode=mode)


def connect_ro(path: Path | str, row_factory: bool = False) -> sqlite3.Connection:
    """Open a sqlite file read-only (mode=ro: never writes, not even a schema
    check). as_uri() percent-encodes the path, so a space, '#' or '?' in it is
    safe; a hand-built 'file:{path}' URI is not. The 5 s busy_timeout lets a
    read land behind a concurrent writer instead of failing with
    'database is locked'."""
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def connect_rw(path: Path | str, row_factory: bool = False) -> sqlite3.Connection:
    """Open a sqlite file for writing. as_uri() percent-encodes the path, so a
    space, '#' or '?' in it is safe (a hand-built 'file:{path}' URI is not); a
    30 s busy_timeout lets a write land behind a concurrent writer instead of
    failing with 'database is locked' (the review app and the nightly sync both
    write wildlife.db). Matches db.py's 30 s and leaves WAL/foreign_keys to it
    for the pipeline."""
    uri = Path(path).absolute().as_uri()
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn
