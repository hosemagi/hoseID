"""nightly_status.py: the last pass is recorded as one JSON file written
atomically through fsio, so the System page can read when it ran, how long it
took and which step failed. Covers begin/step/end, the atomic write (mode 0644,
no temp file left behind), HOSEID_ROOT redirection, and an interrupted run."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/ lives one level up from tests/, scripts/ imports src/, so add both
# to the path and make sure the local src wins over any editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[0:0] = [str(REPO_ROOT / "scripts"), str(REPO_ROOT / "src")]

import nightly_status

from hoseid.paths import nightly_status_file


@pytest.fixture
def root(tmp_path):
    (tmp_path / "derived").mkdir(parents=True)
    return tmp_path


def _write_file(root):
    return root / "derived" / "nightly_status.json"


def _load(root):
    return json.loads(_write_file(root).read_text())


@pytest.fixture(autouse=True)
def _tmp_root(root, monkeypatch):
    """HOSEID_ROOT redirects paths so the helper writes into the tmp dir."""
    monkeypatch.setenv("HOSEID_ROOT", str(root))
    yield root


# a full, clean run ----------------------------------------------------------

def test_full_run_records_start_end_and_ok(root):
    assert nightly_status.main(["begin"]) == 0
    assert nightly_status.main(["step", "pipeline", "0"]) == 0
    assert nightly_status.main(["step", "sync_wildlife_log", "0"]) == 0
    assert nightly_status.main(["step", "weather_backfill", "0"]) == 0
    assert nightly_status.main(["step", "build_encounters", "0"]) == 0
    assert nightly_status.main(["end"]) == 0

    data = _load(root)
    assert data["schema_version"] == 1
    assert data["ok"] is True
    assert data["started"] and data["ended"]
    exits = {s["name"]: s["exit"] for s in data["steps"]}
    assert exits == {
        "pipeline": 0, "sync_wildlife_log": 0,
        "weather_backfill": 0, "build_encounters": 0,
    }
    assert [s["name"] for s in data["steps"]] == [
        "pipeline", "sync_wildlife_log", "weather_backfill", "build_encounters"]
    assert all(s["started"] and s["ended"] for s in data["steps"])


def test_failed_step_marks_ok_false_and_keeps_the_code(root):
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "0"])
    nightly_status.main(["step", "sync_wildlife_log", "2"])
    nightly_status.main(["step", "weather_backfill", "0"])
    nightly_status.main(["step", "build_encounters", "0"])
    nightly_status.main(["end"])

    data = _load(root)
    assert data["ok"] is False
    assert data["steps"][1]["exit"] == 2
    # every step recorded, none left None
    assert all(s["exit"] is not None for s in data["steps"])


def test_end_with_an_unreported_step_is_not_ok(root):
    """A step whose helper call never landed keeps exit null; the run must not
    claim ok just because nothing reported a failure."""
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "0"])
    nightly_status.main(["end"])
    data = _load(root)
    assert data["ended"]
    assert data["ok"] is False
    exits = {s["name"]: s["exit"] for s in data["steps"]}
    assert exits == {
        "pipeline": 0, "sync_wildlife_log": None,
        "weather_backfill": None, "build_encounters": None,
    }


def test_step_records_the_started_time_it_is_given(root):
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "0", "--started", "2026-11-08T10:30:00Z"])
    step = _load(root)["steps"][0]
    assert step["started"] == "2026-11-08T10:30:00Z"
    assert step["ended"] > step["started"]


# in-progress draft ----------------------------------------------------------

def test_step_before_end_keeps_the_draft(root):
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "0"])
    nightly_status.main(["step", "sync_wildlife_log", "0"])
    nightly_status.main(["step", "weather_backfill", "1"])

    # the published file exists ...
    assert _write_file(root).exists()
    # ... and the in-progress draft is still on disk (run was not ended)
    assert (root / "derived" / ".tmp-nightly_status.json").exists()
    data = _load(root)
    assert data["ended"] is None
    assert data["ok"] is None


def test_end_clears_the_draft(root):
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "0"])
    nightly_status.main(["end"])
    assert not (root / "derived" / ".tmp-nightly_status.json").exists()


def test_crash_leaves_an_in_progress_run(root):
    """The file is published at `begin` and refreshed per step (the risk note:
    a crash leaves no `ended`, and the run must show as interrupted, not as
    last night's success). So an interrupted run overwrites the finished one."""
    nightly_status.main(["begin"])
    for name in nightly_status.STEP_NAMES:
        nightly_status.main(["step", name, "0"])
    nightly_status.main(["end"])
    assert _load(root)["ok"] is True

    # next run begins and takes one step, then is killed (no `end`)
    nightly_status.main(["begin"])
    nightly_status.main(["step", "pipeline", "1"])
    data = _load(root)
    # the published file is now the interrupted run: a step failed, nothing ended
    assert data["ok"] is None
    assert data["steps"][0]["exit"] == 1
    assert data["ended"] is None


# publishing -----------------------------------------------------------------

def test_status_file_is_written_mode_0644(root):
    nightly_status.main(["begin"])
    nightly_status.main(["end"])
    mode = stat.S_IMODE(_write_file(root).stat().st_mode)
    assert mode == 0o644
    # no temp file is left behind on a clean end
    assert not (root / "derived" / ".tmp-nightly_status.json").exists()


def test_hoseid_root_redirect_targets_the_file(root, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(root))
    assert nightly_status_file() == root / "derived" / "nightly_status.json"


# usage ----------------------------------------------------------------------

def test_unknown_step_is_usage_error(root):
    nightly_status.main(["begin"])
    rc = nightly_status.main(["step", "nope", "0"])
    assert rc == 2


def test_out_of_range_exit_is_usage_error(root):
    nightly_status.main(["begin"])
    rc = nightly_status.main(["step", "pipeline", "300"])
    assert rc == 2


def test_begin_then_end_without_steps_is_not_ok(root):
    nightly_status.main(["begin"])
    nightly_status.main(["end"])
    data = _load(root)
    assert data["ok"] is False
    assert len(data["steps"]) == 4


# nightly.sh end to end, with every step stubbed --------------------------------

FAKE_PYTHON = """#!/bin/bash
# stands in for .venv/bin/python: runs the real status helper, fakes the steps
case "$1" in
  scripts/nightly_status.py) exec "$REAL_PYTHON" "$@" ;;
  -) cat >/dev/null; exit "$FAKE_PIPELINE" ;;
  scripts/sync_wildlife_log.py) exit "$FAKE_SYNC" ;;
  scripts/weather_backfill.py) exit "$FAKE_WEATHER" ;;
  scripts/build_encounters.py) exit "$FAKE_ENCOUNTERS" ;;
esac
exit 99
"""


def _run_nightly(tmp_path, root, pipeline=0, sync=0, weather=0, encounters=0):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "nightly.sh", repo / "scripts" / "nightly.sh")
    shutil.copy2(REPO_ROOT / "scripts" / "nightly_status.py", repo / "scripts" / "nightly_status.py")
    shutil.copytree(REPO_ROOT / "src", repo / "src")
    py = repo / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(FAKE_PYTHON)
    py.chmod(0o755)
    env = dict(os.environ, HOSEID_ROOT=str(root), REAL_PYTHON=sys.executable,
               FAKE_PIPELINE=str(pipeline), FAKE_SYNC=str(sync),
               FAKE_WEATHER=str(weather), FAKE_ENCOUNTERS=str(encounters))
    return subprocess.run([str(repo / "scripts" / "nightly.sh")], env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def test_nightly_sh_is_executable():
    # launchd runs it directly (config/com.hoseid.nightly.plist)
    assert os.access(REPO_ROOT / "scripts" / "nightly.sh", os.X_OK)


def test_nightly_sh_clean_run(tmp_path, root):
    proc = _run_nightly(tmp_path, root)
    assert proc.returncode == 0, proc.stderr
    data = _load(root)
    assert data["ok"] is True and data["ended"]
    assert [s["exit"] for s in data["steps"]] == [0, 0, 0, 0]
    for name in nightly_status.STEP_NAMES:
        assert f"=== nightly {name} 20" in proc.stdout


def test_nightly_sh_sync_failure_fails_the_run(tmp_path, root):
    proc = _run_nightly(tmp_path, root, sync=3)
    assert proc.returncode == 3
    data = _load(root)
    assert data["ok"] is False
    assert {s["name"]: s["exit"] for s in data["steps"]}["sync_wildlife_log"] == 3


def test_nightly_sh_pipeline_failure_wins_the_exit_code(tmp_path, root):
    proc = _run_nightly(tmp_path, root, pipeline=2, sync=3)
    assert proc.returncode == 2
    assert _load(root)["steps"][0]["exit"] == 2


def test_nightly_sh_weather_and_encounter_failures_are_recorded_not_fatal(tmp_path, root):
    proc = _run_nightly(tmp_path, root, weather=1, encounters=4)
    assert proc.returncode == 0, proc.stderr
    data = _load(root)
    assert data["ok"] is False
    exits = {s["name"]: s["exit"] for s in data["steps"]}
    assert exits["weather_backfill"] == 1 and exits["build_encounters"] == 4
