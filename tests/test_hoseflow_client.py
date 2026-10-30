"""fetchers.common's one hoseflow webhook client, shared by wildlife alerts
and fetch-health: post_to_hoseflow never raises, and notifications_sent only
counts what hoseflow reports as actually sent."""
import importlib.util
import sys
from unittest import mock

import pytest

# Same stubbing as test_fetch_health.py: every request here goes through a
# monkeypatched post().
_STUBS = ("requests", "urllib3", "pyaarlo", "pyaarlo.constant", "pyaarlo.util")
_missing = [m for m in _STUBS if importlib.util.find_spec(m.split(".")[0]) is None]
sys.modules.update({m: mock.MagicMock() for m in _missing})
try:
    from fetchers import alerts, common
finally:
    for m in _missing:
        sys.modules.pop(m, None)

SENT = "notification:sent:ch:p5"


@pytest.mark.parametrize("body, n", [
    (None, 0),
    ({}, 0),
    ({"fired": False, "results": {}}, 0),
    ({"fired": False, "results": None}, 0),
    # a rule fired but its send errored
    ({"fired": True, "results": {"lion": {
        "wildlife-notify.send": "notification:error:hosenotify down"}}}, 0),
    # a rule fired with a non-notification action only
    ({"fired": True, "results": {"lion": {"log.write": "ok"}}}, 0),
    ({"fired": True, "results": {
        "lion": {"wildlife-notify.send": SENT, "log.write": "ok"},
        "bear": {"wildlife-notify.send": "notification:error:timeout"}}}, 1),
])
def test_notifications_sent(body, n):
    assert common.notifications_sent(body) == n


def test_undelivered_uses_shared_results():
    fired = {"lion": {"wildlife-notify.send": "notification:error:x"}}
    assert alerts._undelivered({"results": fired}) is True
    fired["bear"] = {"wildlife-notify.send": SENT}
    assert alerts._undelivered({"results": fired}) is False
    assert alerts._undelivered({"results": {"lion": {"log.write": "ok"}}}) is False
    assert alerts._undelivered({}) is False


class Resp:
    def __init__(self, status=200, body=None):
        self.status, self.body = status, body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Server Error")

    def json(self):
        return self.body


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(common, "log", lines.append)
    return lines


def test_post_returns_body_with_key_and_loopback_tls(monkeypatch, logged):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return Resp(body={"fired": True, "results": {}})
    monkeypatch.setattr(common.requests, "post", post)
    assert common.post_to_hoseflow("https://h/w", "k", {"a": 1}, "alerts") \
        == {"fired": True, "results": {}}
    assert calls == [("https://h/w", {"json": {"a": 1}, "timeout": 30,
                                      "verify": False,
                                      "headers": {"X-Api-Key": "k"}})]
    assert logged == []


@pytest.mark.parametrize("label", ["alerts", "health"])
def test_post_returns_none_on_exception(monkeypatch, logged, label):
    def post(url, **kw):
        raise ConnectionError("hoseserv down")
    monkeypatch.setattr(common.requests, "post", post)
    assert common.post_to_hoseflow("https://h/w", "k", {}, label) is None
    assert logged == [f"{label}: ERROR hoseflow webhook failed: "
                      "ConnectionError: hoseserv down"]


def test_post_returns_none_on_http_error(monkeypatch, logged):
    monkeypatch.setattr(common.requests, "post", lambda url, **kw: Resp(500))
    assert common.post_to_hoseflow("https://h/w", "k", {}, "health") is None
    assert logged == ["health: ERROR hoseflow webhook failed: "
                      "RuntimeError: 500 Server Error"]


def test_health_url_derives_from_alerts_url():
    assert common.DEFAULT_HEALTH_WEBHOOK == (
        "https://127.0.0.1:8000/api/v1/hoseflow/webhooks/hoseid-fetch-health")
    assert alerts.DEFAULT_WEBHOOK == common.DEFAULT_WEBHOOK
    cfg = {"alerts": {"hoseflow_url": "https://nvlm:8000/hooks/wildlife-webhook/"},
           "notify": {"hoseflow_health_url": ""}}
    assert common.health_url(cfg) == "https://nvlm:8000/hooks/hoseid-fetch-health"
