"""fetchers.daemon: a routine Arlo session expiry restarts quietly (no ERROR
line, no page); early session death, a frozen library and threshold
restarts still log ERROR and notify."""
import importlib.util
import sys
import time
from unittest import mock

import pytest

# The daemon runs from .venv-fetch (requests + pyaarlo); the main test venv
# has neither. Stub them just long enough to import the module under test --
# nothing here touches the network or a real Arlo session.
_STUBS = ("requests", "urllib3", "pyaarlo", "pyaarlo.constant", "pyaarlo.util")
_missing = [m for m in _STUBS if importlib.util.find_spec(m.split(".")[0]) is None]
# Only the stubs are removed afterwards: everything else the import pulls in
# (hoseid.sidecar, ...) must stay loaded, or later tests see duplicate classes.
sys.modules.update({m: mock.MagicMock() for m in _missing})
try:
    from fetchers import daemon
    from fetchers.arlo import LibraryFrozenError, SessionDeadError
finally:
    for m in _missing:
        sys.modules.pop(m, None)

MIN = 60


@pytest.mark.parametrize("exc, failures, age, expected", [
    # (restart, alert, routine)
    (Exception("504"), 1, 10 * MIN, (False, False, False)),
    (Exception("504"), 8, 10 * MIN, (True, True, False)),
    (LibraryFrozenError("stale"), 1, 120 * MIN, (True, True, False)),
    (SessionDeadError("401"), 1, 59 * MIN, (True, True, False)),
    (SessionDeadError("401"), 1, 60 * MIN, (True, False, True)),
    (SessionDeadError("401"), 1, 61 * MIN, (True, False, True)),
], ids=["transient", "threshold", "frozen", "session-early", "session-boundary",
        "session-routine"])
def test_classify_failure(exc, failures, age, expected):
    assert daemon.classify_failure(exc, failures, age) == expected


@pytest.fixture
def captured(monkeypatch):
    lines, notes = [], []
    monkeypatch.setattr(daemon, "log", lines.append)
    monkeypatch.setattr(daemon, "notify", lambda cfg, msg: notes.append(msg))
    return lines, notes


def _run(session_age, *, once=False, exc=None, stopped=None):
    stopped = [] if stopped is None else stopped

    def sweep():
        raise exc or SessionDeadError("401 on library refresh")

    failures = {"reveal": 0, "arlo": 0}
    result = daemon.run_source(
        "arlo", sweep, cfg={}, failures=failures,
        login_at=time.time() - session_age, once=once,
        stop=lambda: stopped.append(True))
    return result, failures, stopped


def test_routine_expiry_restarts_without_error_or_notify(captured):
    lines, notes = captured
    stopped = []
    with pytest.raises(SystemExit) as exit_:
        _run(112 * MIN, stopped=stopped)
    assert exit_.value.code == daemon.EXIT_RESTART
    assert stopped == [True]
    assert not any("ERROR" in line for line in lines)
    assert lines == ["arlo: session expired after 112 min (routine); restarting"]
    assert notes == []


def test_early_session_death_still_errors_and_notifies(captured):
    lines, notes = captured
    with pytest.raises(SystemExit) as exit_:
        _run(10 * MIN)
    assert exit_.value.code == daemon.EXIT_RESTART
    assert any("ERROR" in line for line in lines)
    assert len(notes) == 1 and "exiting for a launchd restart" in notes[0]


def test_session_death_during_once_is_an_error_without_restart(captured):
    lines, notes = captured
    result, failures, stopped = _run(112 * MIN, once=True)
    assert result == [] and failures["arlo"] == 1 and stopped == []
    assert any("ERROR" in line for line in lines)
    assert notes == []


def test_transient_failure_logs_error_and_keeps_running(captured):
    lines, notes = captured
    result, failures, stopped = _run(10 * MIN, exc=RuntimeError("Reveal 504"))
    assert result == [] and failures["arlo"] == 1 and stopped == []
    assert lines == ["arlo: ERROR (1 consecutive): RuntimeError: Reveal 504"]
    assert notes == []


def test_success_resets_failures(captured):
    failures = {"arlo": 3}
    new = daemon.run_source("arlo", lambda: ["sha256:aa"], cfg={},
                            failures=failures, login_at=time.time(),
                            once=False, stop=lambda: None)
    assert new == ["sha256:aa"] and failures["arlo"] == 0
