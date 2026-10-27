"""fetchers.alerts: detections are forwarded to the hoseflow webhook
(policy lives there), exclusion zones still apply here, and a failing
webhook raises one operator alert instead of silently dropping pages."""
import sqlite3

import pytest

from fetchers import alerts
from fetchers.common import State


@pytest.fixture
def det_db(tmp_path, monkeypatch):
    path = tmp_path / "detections.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE captures (asset_id TEXT, run_id TEXT, station TEXT,
                               capture_time TEXT, media_type TEXT);
        CREATE TABLE detections (detection_id TEXT, asset_id TEXT, run_id TEXT,
            bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL, detector_class TEXT,
            detector_confidence REAL, taxon TEXT, crop_path TEXT);
    """)
    # 2026-10-26T12:50Z = 05:50 PDT: a pre-dawn capture
    conn.execute("INSERT INTO captures VALUES ('sha256:aa11', 'nightly', 'Bench', "
                 "'2026-10-26T12:50:00+00:00', 'video')")
    conn.executemany("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("d1", "sha256:aa11", "nightly", .1, .1, .1, .1, "animal", .93, "mountain_lion", None),
        ("d2", "sha256:aa11", "nightly", .8, .8, .1, .1, "animal", .40, "blacktail", None),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(alerts, "DETECTIONS_DB", path)
    monkeypatch.setattr(alerts, "TAGS_DB", tmp_path / "no-tags.db")
    monkeypatch.setattr(alerts, "_device_for", lambda digest: "cam1")
    monkeypatch.setattr(alerts, "_alert_image", lambda det, digest: None)
    return path


class Webhook:
    """Stands in for hoseserv: records posts; lion events report a sent notification."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.fail:
            raise ConnectionError("hoseserv down")
        sent = json["taxon"] == "mountain_lion"
        body = {"fired": sent, "results": {"lion": {
            "wildlife-notify.send": "notification:sent:ch:p5"}} if sent else {}}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return body
        return Resp()


@pytest.fixture
def webhook(monkeypatch):
    wh = Webhook()
    monkeypatch.setattr(alerts.requests, "post", wh.post)
    return wh


@pytest.fixture
def operator(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda cfg, msg, title="": sent.append((title, msg)))
    return sent


CFG = {"alerts": {"hoseflow_api_key": "k"}}


def test_every_detection_forwarded_with_event_fields(det_db, webhook, operator, tmp_path):
    n = alerts.check_and_alert(CFG, State(tmp_path / "s.json"), ["sha256:aa11"])
    assert n == 1  # one notification:sent in the results
    assert [c["json"]["taxon"] for c in webhook.calls] == ["mountain_lion", "blacktail"]
    lion = webhook.calls[0]["json"]
    assert webhook.calls[0]["url"] == alerts.DEFAULT_WEBHOOK
    assert webhook.calls[0]["headers"] == {"X-Api-Key": "k"}
    assert lion["station"] == "Bench"
    assert lion["label"] == "mountain lion"
    assert lion["confidence_pct"] == "93%"
    assert lion["reason"] == "mountain_lion"
    assert (lion["capture_hhmm"], lion["capture_hour"]) == ("05:50", 5)
    assert lion["video_suffix"] == " [VIDEO]"
    assert operator == []


def test_exclusion_zone_detections_not_forwarded(det_db, webhook, operator, tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "_in_zone", lambda det, dev, zones: det["taxon"] == "blacktail")
    alerts.check_and_alert(CFG, State(tmp_path / "s.json"), ["sha256:aa11"])
    assert [c["json"]["taxon"] for c in webhook.calls] == ["mountain_lion"]


def test_webhook_failure_logged_not_raised_then_one_operator_alert(
        det_db, webhook, operator, tmp_path):
    webhook.fail = True
    state = State(tmp_path / "s.json")
    for _ in range(alerts.WEBHOOK_FAIL_ALERT_AFTER + 1):
        assert alerts.check_and_alert(CFG, state, ["sha256:aa11"]) == 0
    assert len(operator) == 1  # once at the threshold, not every batch
    title, msg = operator[0]
    assert "DOWN" in title and "mountain lion @ Bench" in msg

    webhook.fail = False
    alerts.check_and_alert(CFG, state, ["sha256:aa11"])
    assert operator[-1][0] == "hoseid alerts recovered"
    assert state.get("alert_webhook_failures") == 0


def test_matched_but_undelivered_counts_toward_down_alert(det_db, webhook, operator, tmp_path,
                                                          monkeypatch):
    """A rule fired but hosenotify/ntfy didn't deliver: same as the webhook failing."""
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"fired": True, "results": {"lion": {
                "wildlife-notify.send": "error:notification_undelivered:ch:1/1:429"}}}

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: Resp())
    state = State(tmp_path / "s.json")
    for _ in range(alerts.WEBHOOK_FAIL_ALERT_AFTER):
        assert alerts.check_and_alert(CFG, state, ["sha256:aa11"]) == 0
    assert len(operator) == 1 and "DOWN" in operator[0][0]
