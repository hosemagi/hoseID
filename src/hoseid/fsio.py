"""Atomic file writes: temp file in the destination directory, fsync,
os.replace. A reader sees either the old file or the new one, never half of
either; on any failure the temp file is removed and the error re-raised, and
an existing destination is left untouched. Temp names start with ".tmp-",
which landing's directory scans skip.

mkstemp creates files 0600. Pass `mode` when another user or service reads
the result (hoseserv reads the station-health files, for example)."""
from __future__ import annotations

import json
import os
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
