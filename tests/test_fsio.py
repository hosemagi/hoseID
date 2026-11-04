"""hoseid.fsio: atomic writes land the content, and a failed write re-raises,
leaves no temp file and does not touch the existing destination."""
import json
import os
import stat

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
