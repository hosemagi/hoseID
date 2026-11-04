"""Wildlife MCP log noise: successful /health probes are quieted.

The stack panel probes GET /health every few seconds, and uvicorn logs every one
at INFO, so the log was mostly probe lines. _QuietHealth drops the successful
polls but keeps the time of the newest one, and logs it when a probe first
fails. Failures and every other path are kept.
"""
import logging

from hoseid.mcp_wildlife import app


def _access(path, status, created=None):
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5555", "GET", path, "1.1", status), None,
    )
    if created is not None:
        rec.created = created
    return rec


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_health_filter_drops_only_successful_health():
    f = app._QuietHealth()
    # The first success after start is kept: the log shows when health came up.
    assert f.filter(_access("/health", 200)) is True
    assert f.filter(_access("/health", 200)) is False
    assert f.filter(_access("/health?x=1", 200)) is False
    assert f.filter(_access("/health", 204)) is False
    assert f.filter(_access("/health", 503)) is True
    assert f.filter(_access("/mcp", 200)) is True
    assert f.filter(_access("/healthz", 200)) is True
    # Records not shaped like uvicorn access lines pass through untouched.
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain", None, None)
    assert f.filter(plain) is True


def test_health_filter_remembers_last_success():
    f = app._QuietHealth()
    assert f.last_ok is None
    f.filter(_access("/health", 200, created=1000.0))
    f.filter(_access("/health", 200, created=2000.0))
    f.filter(_access("/mcp", 200, created=2500.0))  # other paths don't count
    assert f.last_ok == 2000.0

    cap = _Capture()
    err = logging.getLogger("uvicorn.error")
    err.addHandler(cap)
    try:
        assert f.filter(_access("/health", 503, created=3000.0)) is True
        assert f.filter(_access("/health", 503, created=3005.0)) is True
    finally:
        err.removeHandler(cap)
    # One line per outage, naming the last success; a failure keeps last_ok.
    assert len(cap.messages) == 1
    assert "status 503" in cap.messages[0]
    assert "last successful probe at" in cap.messages[0]
    assert f.last_ok == 2000.0

    # Recovery: the first success after a failure is kept, the next dropped.
    assert f.filter(_access("/health", 200, created=4000.0)) is True
    assert f.filter(_access("/health", 200, created=4005.0)) is False
    assert f.last_ok == 4005.0


def test_health_filter_installed_once():
    app._install_access_filter()
    app._install_access_filter()
    access = logging.getLogger("uvicorn.access")
    assert sum(type(f).__name__ == "_QuietHealth" for f in access.filters) == 1
    access.filter(_access("/health", 200))  # first success (kept or already seen)
    assert not access.filter(_access("/health", 200))
    assert access.filter(_access("/health", 503))
