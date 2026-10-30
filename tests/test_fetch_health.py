"""fetchers.common.report_health: a fetch source going BROKEN or coming back
HEALTHY is POSTed once per transition to hoseflow's hoseid-fetch-health
webhook, and falls back to ntfy (notify()) whenever hoseflow didn't deliver."""
import importlib.util
import sys
import time
from unittest import mock

import pytest

# Same stubbing as test_daemon.py: the main test venv has neither requests nor
# pyaarlo, and every request here goes through a monkeypatched post().
_STUBS = ("requests", "urllib3", "pyaarlo", "pyaarlo.constant", "pyaarlo.util")
_missing = [m for m in _STUBS if importlib.util.find_spec(m.split(".")[0]) is None]
sys.modules.update({m: mock.MagicMock() for m in _missing})
if "requests" in _missing:
    # daemon.is_transient_reveal isinstance()-checks these on a failed run.
    _req = sys.modules["requests"]
    _req.RequestException = type("RequestException", (IOError,), {})
    for _cls in ("ConnectionError", "Timeout", "HTTPError"):
        setattr(_req, _cls, type(_cls, (_req.RequestException,), {}))
try:
    from fetchers import common, daemon
    from fetchers.common import State
finally:
    for m in _missing:
        sys.modules.pop(m, None)


class Webhook:
    """Stands in for hoseserv: records posts; by default a rule sends."""

    def __init__(self):
        self.calls = []
        self.fail = None          # None | "raise" | "500" | "unfired"

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers, **kw})
        if self.fail == "raise":
            raise ConnectionError("hoseserv down")
        fail = self.fail

        class Resp:
            def raise_for_status(self):
                if fail == "500":
                    raise RuntimeError("500 Server Error")

            def json(self):
                if fail == "unfired":
                    return {"fired": False, "results": {}}
                return {"fired": True, "results": {"fetch-broken": {
                    "fetch-notify.send": "notification:sent:fetch:p4"}}}
        return Resp()


@pytest.fixture
def webhook(monkeypatch):
    wh = Webhook()
    monkeypatch.setattr(common.requests, "post", wh.post)
    return wh


@pytest.fixture
def ntfy(monkeypatch):
    sent = []
    monkeypatch.setattr(common, "notify", lambda cfg, msg, title="": sent.append(msg))
    monkeypatch.setattr(daemon, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(common, "log", lambda msg: None)
    monkeypatch.setattr(daemon, "log", lambda msg: None)
    return sent


CFG = {"alerts": {"hoseflow_api_key": "k"}}


def _pass(state, failures, *, fail, cfg=CFG):
    def fn():
        if fail:
            raise RuntimeError("Reveal 504")
        return []
    daemon.run_source("reveal", fn, cfg=cfg, state=state, failures=failures,
                      login_at=time.time(), once=False, stop=lambda: None)


def _verdicts(webhook):
    return [c["json"]["verdict"] for c in webhook.calls]


def test_broken_sent_once_across_repeated_failures(webhook, ntfy, tmp_path):
    state, failures = State(tmp_path / "s.json"), {"reveal": 0}
    for _ in range(daemon.FAILURE_RESTART_THRESHOLD - 1):
        _pass(state, failures, fail=True)
    assert _verdicts(webhook) == ["BROKEN"]
    assert webhook.calls[0]["json"]["failures"] == daemon.FAILURE_ALERT_THRESHOLD
    assert state.get("fetch_health_reveal") == "BROKEN"
    assert ntfy == []


def test_healthy_sent_once_on_recovery_and_quiet_while_healthy(webhook, ntfy, tmp_path):
    state, failures = State(tmp_path / "s.json"), {"reveal": 0}
    for _ in range(3):
        _pass(state, failures, fail=False)
    assert webhook.calls == []
    for _ in range(daemon.FAILURE_ALERT_THRESHOLD):
        _pass(state, failures, fail=True)
    for _ in range(3):
        _pass(state, failures, fail=False)
    assert _verdicts(webhook) == ["BROKEN", "HEALTHY"]
    assert webhook.calls[1]["json"]["error"] is None
    assert state.get("fetch_health_reveal") == "HEALTHY"
    assert ntfy == []


def test_recovery_announced_after_process_restart(webhook, ntfy, tmp_path):
    path = tmp_path / "s.json"
    state, failures = State(path), {"reveal": 0}
    for _ in range(daemon.FAILURE_ALERT_THRESHOLD):
        _pass(state, failures, fail=True)
    # launchd restart: fresh process, failure counts gone, State reloaded.
    state, failures = State(path), {"reveal": 0}
    _pass(state, failures, fail=True)
    _pass(state, failures, fail=False)
    assert _verdicts(webhook) == ["BROKEN", "HEALTHY"]


@pytest.mark.parametrize("mode", ["raise", "500", "unfired"])
def test_dead_hoseflow_falls_back_to_ntfy(webhook, ntfy, tmp_path, mode):
    webhook.fail = mode
    state, failures = State(tmp_path / "s.json"), {"reveal": 0}
    for _ in range(daemon.FAILURE_ALERT_THRESHOLD + 1):
        _pass(state, failures, fail=True)
    _pass(state, failures, fail=False)
    _pass(state, failures, fail=False)
    assert ntfy == ["hoseid-fetch: reveal failing repeatedly: Reveal 504",
                    "hoseid-fetch: reveal recovered"]
    assert _verdicts(webhook) == ["BROKEN", "HEALTHY"]
    assert state.get("fetch_health_reveal") == "HEALTHY"


def test_missing_config_goes_straight_to_ntfy(webhook, ntfy, tmp_path):
    state = State(tmp_path / "s.json")
    common.report_health({}, state, "arlo", False, 5, "boom", "arlo down")
    common.report_health({}, state, "arlo", True, 0, None, "")
    assert webhook.calls == []
    assert ntfy == ["arlo down", "hoseid-fetch: arlo recovered"]


def test_payload_contract_and_alerts_creds(webhook, ntfy):
    cfg = {"alerts": {"hoseflow_api_key": "secret",
                      "hoseflow_url": "https://nvlm:8000/api/v1/hoseflow/"
                                      "webhooks/wildlife-webhook"}}
    assert common.push_health(cfg, "arlo", "BROKEN", 5, "x" * 500) is True
    call = webhook.calls[0]
    assert set(call["json"]) == {"source", "verdict", "failures", "error"}
    assert call["json"] == {"source": "arlo", "verdict": "BROKEN",
                            "failures": 5, "error": "x" * 200}
    assert call["headers"] == {"X-Api-Key": "secret"}
    assert call["verify"] is False
    assert call["url"] == ("https://nvlm:8000/api/v1/hoseflow/webhooks/"
                           "hoseid-fetch-health")


def test_health_url_override_and_default():
    assert common.health_url({}) == common.DEFAULT_HEALTH_WEBHOOK
    assert common.health_url({"notify": {"hoseflow_health_url": "https://h/x"}}) \
        == "https://h/x"
