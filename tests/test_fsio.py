"""hoseid.fsio: atomic writes land the content, and a failed write re-raises,
leaves no temp file and does not touch the existing destination; connect_ro is
a read-only opener that survives a path with a space or '#' in it (the old
f"file:{path}?mode=ro" broke on that), refuses to write, and waits a moment for
a concurrent writer instead of failing with 'database is locked'."""
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from hoseid import fsio


def test_bytes_text_json_written(tmp_path):
    fsio.atomic_write_bytes(tmp_path / "a.bin", b"\x00\x01")
    fsio.atomic_write_text(tmp_path / "b.txt", "café\n")
    fsio.atomic_write_json(tmp_path / "c.json", {"name": "Bärenhöhle", "n": [1]})
    assert (tmp_path / "a.bin").read_bytes() == b"\x00\x01"
    assert (tmp_path / "b.txt").read_bytes() == "café\n".encode()
    # camera_names' old _write_json output: indent 2, raw UTF-8, trailing newline
    assert (tmp_path / "c.json").read_text() == (
        json.dumps({"name": "Bärenhöhle", "n": [1]}, indent=2, ensure_ascii=False) + "\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.bin", "b.txt", "c.json"]


def test_makes_parent_and_replaces(tmp_path):
    dest = tmp_path / "x" / "y" / "f.txt"
    fsio.atomic_write_text(dest, "one")
    fsio.atomic_write_text(dest, "two")
    assert dest.read_text() == "two"


@pytest.mark.parametrize("exc", [OSError("disk full"), KeyboardInterrupt()])
def test_failure_reraises_cleans_up_and_keeps_old(tmp_path, monkeypatch, exc):
    dest = tmp_path / "state.json"
    dest.write_text("old")

    def boom(src, dst):
        raise exc
    monkeypatch.setattr(fsio.os, "replace", boom)

    with pytest.raises(type(exc)):
        fsio.atomic_write_json(dest, {"new": True})
    assert dest.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_mode_applied(tmp_path):
    fsio.atomic_write_text(tmp_path / "shared", "x", mode=0o644)
    fsio.atomic_write_text(tmp_path / "private", "x")
    assert stat.S_IMODE(os.stat(tmp_path / "shared").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(tmp_path / "private").st_mode) == 0o600


def test_state_set_round_trips(tmp_path):
    from fetchers.common import State
    path = tmp_path / "derived" / "fetch-state.json"
    State(path).set("health", {"reveal": "BROKEN"})
    assert json.loads(path.read_text()) == {"health": {"reveal": "BROKEN"}}
    assert State(path).get("health") == {"reveal": "BROKEN"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert [p.name for p in path.parent.iterdir()] == ["fetch-state.json"]


def _make_db(path, journal=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    if not journal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (k, v)")
    conn.execute("INSERT INTO t VALUES ('a', 1)")
    conn.commit()
    conn.close()
    return path


def test_connect_ro_reads(tmp_path):
    p = _make_db(tmp_path / "hoseint.db")
    conn = fsio.connect_ro(p)
    assert conn.execute("SELECT v FROM t WHERE k='a'").fetchone()[0] == 1
    conn.close()


def test_connect_ro_path_with_space_and_hash(tmp_path):
    """The regression marker: a '#' in the path is read by sqlite3's URI
    parser as the start of a fragment, so the old f"file:{path}?mode=ro" points
    at a truncated, empty db. as_uri() percent-encodes it and the helper reads
    the real table."""
    p = _make_db(tmp_path / "dir with space#and?/hoseint.db")
    conn = fsio.connect_ro(p)
    assert conn.execute("SELECT v FROM t WHERE k='a'").fetchone()[0] == 1
    conn.close()


def test_hand_built_uri_breaks_on_hash(tmp_path):
    p = _make_db(tmp_path / "dir with space#and?/hoseint.db")
    # The '#' truncates the path as a URI fragment, so the hand-built URI opens
    # an empty db: reading the real table fails instead of being read.
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT v FROM t WHERE k='a'")


def test_connect_ro_refuses_write(tmp_path):
    p = _make_db(tmp_path / "hoseint.db")
    conn = fsio.connect_ro(p)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES ('b', 2)")
    conn.close()


def test_connect_ro_row_factory(tmp_path):
    p = _make_db(tmp_path / "hoseint.db")
    conn = fsio.connect_ro(p, row_factory=True)
    row = conn.execute("SELECT k, v FROM t").fetchone()
    assert row["k"] == "a" and row["v"] == 1
    conn.close()


def _hold_exclusive(path, held, release):
    """Hold BEGIN EXCLUSIVE until `release` is set. RESERVED (BEGIN
    IMMEDIATE) does not block readers in either journal mode; EXCLUSIVE in
    rollback-journal mode does, which is the window a nightly writer's
    commit opens."""
    w = sqlite3.connect(str(path))
    w.execute("BEGIN EXCLUSIVE")
    held.set()
    release.wait(5)
    w.rollback()
    w.close()


def test_read_waits_for_writer(tmp_path):
    """A rollback-journal db: while another connection holds an exclusive
    lock, a no-timeout reader fails with 'database is locked' (the bug), and
    connect_ro waits for the lock to clear and then reads."""
    p = _make_db(tmp_path / "hoseint.db")
    held, release = threading.Event(), threading.Event()
    blocker = threading.Thread(target=_hold_exclusive, args=(p, held, release))
    blocker.start()
    try:
        assert held.wait(5)
        bare = sqlite3.connect(p.absolute().as_uri() + "?mode=ro", uri=True,
                               timeout=0)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            bare.execute("SELECT v FROM t").fetchone()
        bare.close()

        threading.Timer(0.3, release.set).start()
        start = time.monotonic()
        conn = fsio.connect_ro(p)
        value = conn.execute("SELECT v FROM t WHERE k='a'").fetchone()[0]
        waited = time.monotonic() - start
        conn.close()
    finally:
        release.set()
        blocker.join()
    assert value == 1
    assert waited >= 0.2  # it waited for the lock rather than slipping past


def test_read_during_writer_wal(tmp_path):
    """A WAL db: a reader is never blocked by a writer, even one holding an
    exclusive transaction; connect_ro reads the last committed value."""
    p = _make_db(tmp_path / "wal.db", journal=False)
    held, release = threading.Event(), threading.Event()
    blocker = threading.Thread(target=_hold_exclusive, args=(p, held, release))
    blocker.start()
    try:
        assert held.wait(5)
        conn = fsio.connect_ro(p)
        value = conn.execute("SELECT v FROM t WHERE k='a'").fetchone()[0]
        conn.close()
    finally:
        release.set()
        blocker.join()
    assert value == 1
