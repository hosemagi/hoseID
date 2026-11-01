"""hoseid.hoseserv: one place for where hoseserv is, its key, and the TLS rule
(verify off for loopback hosts only), shared by the fetch daemon's webhooks,
the Arlo rename logbook note and the hosemap layer rename."""
import sys
import types

import pytest

from hoseid import camera_names, hoseserv


# ---------- URL and key ----------

@pytest.mark.parametrize("url, base", [
    (None, "https://127.0.0.1:8000"),
    ("", "https://127.0.0.1:8000"),
    ("https://nvlm:8000/api/v1/hoseflow/webhooks/wildlife-webhook",
     "https://nvlm:8000"),
    ("https://nvlm:8000/hooks/wildlife-webhook", "https://nvlm:8000"),
    ("https://nvlm:8000/hooks/wildlife-webhook/", "https://nvlm:8000"),
    ("https://localhost:8000/", "https://localhost:8000"),
    ("https://[::1]:8000/api/v1/hoseflow/webhooks/w", "https://[::1]:8000"),
    ("http://hoseserv.lan/x/api/v1/y", "http://hoseserv.lan"),
])
def test_base_url(url, base):
    cfg = {"alerts": {"hoseflow_url": url}} if url is not None else {}
    assert hoseserv.base_url(cfg) == base


def test_default_is_the_local_wildlife_webhook():
    assert hoseserv.webhook_url({}) == hoseserv.DEFAULT_WEBHOOK
    assert hoseserv.base_url({}) == "https://127.0.0.1:8000"


def test_api_key():
    assert hoseserv.api_key({}) == ""
    assert hoseserv.api_key({"alerts": {}}) == ""
    assert hoseserv.api_key({"alerts": {"hoseflow_api_key": "k"}}) == "k"
    assert hoseserv.headers("k") == {"X-Api-Key": "k"}


# ---------- TLS: verify off for loopback only ----------

@pytest.mark.parametrize("url", ["https://127.0.0.1:8000/x",
                                 "https://localhost:8000",
                                 "https://LOCALHOST:8000/x",
                                 "https://[::1]:8000/x"])
def test_verify_off_on_loopback(url):
    assert hoseserv.verify_for(url) is False


@pytest.mark.parametrize("url", ["https://nvlm:8000/x",
                                 "https://h/w",
                                 "https://192.168.1.10:8000/x",
                                 "https://127.0.0.1.evil.example/x"])
def test_verify_on_elsewhere(url):
    assert hoseserv.verify_for(url) is True


# ---------- request(): header, verify, never raises ----------

class Resp:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Server Error")


@pytest.fixture
def fake_requests(monkeypatch):
    """A stand-in requests module; the test venv has no requests."""
    mod = types.ModuleType("requests")
    mod.calls, mod.result = [], Resp()

    def request(method, url, **kw):
        mod.calls.append((method, url, kw))
        if isinstance(mod.result, Exception):
            raise mod.result
        return mod.result
    mod.request = request
    monkeypatch.setitem(sys.modules, "requests", mod)
    return mod


@pytest.mark.parametrize("url, verify", [
    ("https://127.0.0.1:8000/api/v1/logbook/notes", False),
    ("https://nvlm:8000/api/v1/logbook/notes", True),
])
def test_request_sends_key_and_verify_rule(fake_requests, url, verify):
    logged = []
    resp = hoseserv.request("POST", url, "k", json={"a": 1}, timeout=15,
                            label="x", log=logged.append)
    assert resp is fake_requests.result
    assert fake_requests.calls == [("POST", url, {
        "json": {"a": 1}, "headers": {"X-Api-Key": "k"}, "timeout": 15,
        "verify": verify})]
    assert logged == []


def test_request_never_raises(fake_requests):
    logged = []
    fake_requests.result = ConnectionError("hoseserv down")
    assert hoseserv.request("GET", "https://127.0.0.1:8000/x", "k",
                            label="arlo: logbook note", log=logged.append) is None
    fake_requests.result = Resp(500)
    assert hoseserv.request("GET", "https://127.0.0.1:8000/x", "k",
                            label="arlo: logbook note", log=logged.append) is None
    assert logged == ["arlo: logbook note failed: ConnectionError: hoseserv down",
                      "arlo: logbook note failed: RuntimeError: 500 Server Error"]


# ---------- the callers ----------

@pytest.mark.parametrize("base, verify", [("https://127.0.0.1:8000", False),
                                          ("https://nvlm:8000", True)])
def test_map_layer_rename_uses_verify_rule(base, verify):
    seen = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": "L1", "trailcam_ids": ["a"]}]

    def request(method, url, **kw):
        seen.append((kw["verify"], kw["headers"]))
        return R()
    assert camera_names.rename_in_map_layers(base, "k", "a", "b", request) == 1
    assert seen == [(verify, {"X-Api-Key": "k"})] * 3


def test_logbook_note_goes_through_helper(fake_requests, monkeypatch):
    arlo = pytest.importorskip("fetchers.arlo")
    logged = []
    monkeypatch.setattr(arlo, "log", logged.append)
    arlo._logbook_note("https://127.0.0.1:8000", "k", "renamed", "device D")
    assert fake_requests.calls == [(
        "POST", "https://127.0.0.1:8000/api/v1/logbook/notes",
        {"json": {"text": "renamed", "detail": "device D"},
         "headers": {"X-Api-Key": "k"}, "timeout": 15, "verify": False})]
    fake_requests.result = ConnectionError("down")
    arlo._logbook_note("https://127.0.0.1:8000", "k", "renamed", "device D")
    assert logged == ["arlo: logbook note failed: ConnectionError: down"]
