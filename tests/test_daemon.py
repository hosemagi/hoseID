"""fetchers.daemon: a routine Arlo session expiry restarts quietly (no ERROR
line, no page), and so does the first Reveal hiccup in a row (logs a plain
line); early session death, a frozen library, repeated failures and threshold
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
if "requests" in _missing:
    # isinstance() needs real classes, shaped like requests' own hierarchy.
    _req = sys.modules["requests"]
    _req.RequestException = type("RequestException", (IOError,), {})
    for _cls in ("ConnectionError", "Timeout", "HTTPError"):
        setattr(_req, _cls, type(_cls, (_req.RequestException,), {}))
try:
    from fetchers import daemon
    from fetchers.arlo import LibraryFrozenError, SessionDeadError
    requests = daemon.requests
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


def _http_error(status):
    exc = requests.HTTPError(f"{status} Server Error")
    exc.response = mock.Mock(status_code=status)
    return exc


@pytest.mark.parametrize("exc, name, failures, expected", [
    (_http_error(504), "reveal", 1, True),
    (_http_error(502), "reveal", 1, True),
    (_http_error(503), "reveal", 1, True),
    (_http_error(504), "reveal", 2, False),
    (_http_error(401), "reveal", 1, False),
    (_http_error(500), "reveal", 1, False),
    (requests.HTTPError("no response"), "reveal", 1, False),
    (requests.ConnectionError("reset by peer"), "reveal", 1, True),
    (requests.Timeout("read timed out"), "reveal", 1, True),
    (_http_error(504), "arlo", 1, False),
    (requests.ConnectionError("reset by peer"), "arlo", 1, False),
    (RuntimeError("504"), "reveal", 1, False),
], ids=["504", "502", "503", "504-second", "401", "500", "no-response",
        "connection", "timeout", "arlo-504", "arlo-connection", "not-requests"])
def test_is_transient_reveal(exc, name, failures, expected):
    assert daemon.is_transient_reveal(exc, name, failures) is expected


def _reveal(exc, failures, *, once=False):
    def poll():
        if exc is None:
            return ["sha256:bb"]
        raise exc
    return daemon.run_source("reveal", poll, cfg={}, failures=failures,
                             login_at=time.time(), once=once,
                             stop=lambda: None)


def test_first_reveal_504_logs_plain_line(captured):
    lines, notes = captured
    failures = {"reveal": 0}
    assert _reveal(_http_error(504), failures) == []
    assert failures["reveal"] == 1
    assert lines == ["reveal: transient HTTPError, retrying next sweep"]
    assert notes == []


def test_second_reveal_504_in_a_row_logs_error(captured):
    lines, notes = captured
    failures = {"reveal": 0}
    _reveal(_http_error(504), failures)
    _reveal(_http_error(504), failures)
    assert failures["reveal"] == 2
    assert not any("ERROR" in line for line in lines[:1])
    assert lines[1].startswith("reveal: ERROR (2 consecutive): HTTPError: 504")


@pytest.mark.parametrize("exc", [requests.ConnectionError("reset"),
                                 requests.Timeout("timed out")],
                         ids=["connection", "timeout"])
def test_first_reveal_connection_failure_is_transient(captured, exc):
    lines, _ = captured
    _reveal(exc, {"reveal": 0})
    assert lines == [f"reveal: transient {type(exc).__name__}, "
                     "retrying next sweep"]


def test_reveal_401_still_logs_error(captured):
    lines, _ = captured
    _reveal(_http_error(401), {"reveal": 0})
    assert lines == ["reveal: ERROR (1 consecutive): HTTPError: 401 Server Error"]


def test_reveal_504_during_once_logs_error(captured):
    lines, _ = captured
    _reveal(_http_error(504), {"reveal": 0}, once=True)
    assert len(lines) == 1 and "ERROR (1 consecutive)" in lines[0]


def test_success_makes_next_reveal_504_transient_again(captured):
    lines, _ = captured
    failures = {"reveal": 0}
    _reveal(_http_error(504), failures)
    _reveal(_http_error(504), failures)
    assert _reveal(None, failures) == ["sha256:bb"]
    assert failures["reveal"] == 0
    _reveal(_http_error(504), failures)
    assert lines[-1] == "reveal: transient HTTPError, retrying next sweep"


def test_reveal_504_streak_still_notifies_at_5_and_restarts_at_8(captured):
    lines, notes = captured
    failures = {"reveal": 0}
    for _ in range(daemon.FAILURE_RESTART_THRESHOLD - 1):
        _reveal(_http_error(504), failures)
    assert len(notes) == 1 and "failing repeatedly" in notes[0]
    assert failures["reveal"] == daemon.FAILURE_ALERT_THRESHOLD + 2
    with pytest.raises(SystemExit) as exit_:
        _reveal(_http_error(504), failures)
    assert exit_.value.code == daemon.EXIT_RESTART
    assert failures["reveal"] == daemon.FAILURE_RESTART_THRESHOLD
    assert len(notes) == 2 and "exiting for a launchd restart" in notes[1]
    assert sum("transient" in line for line in lines) == 1
