"""fetchers.alerts: detections are forwarded to the hoseflow webhook
(policy lives there), exclusion zones still apply here, and a failing
webhook raises one operator alert instead of silently dropping pages."""
import json
import sqlite3
from pathlib import Path

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
            detector_confidence REAL, taxon TEXT, crop_path TEXT,
            taxon_confidence REAL, taxon_raw TEXT, buck_prob REAL);
    """)
    # 2026-10-26T12:50Z = 05:50 PDT: a pre-dawn capture
    conn.execute("INSERT INTO captures VALUES ('sha256:aa11', 'nightly', 'Bench', "
                 "'2026-10-26T12:50:00+00:00', 'video')")
    conn.executemany("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("d1", "sha256:aa11", "nightly", .1, .1, .2, .4, "animal", .93, "mountain_lion", None,
         .3024, "u;mammalia;carnivora;felidae;puma;concolor;puma", None),
        ("d2", "sha256:aa11", "nightly", .8, .8, .1, .1, "animal", .40, "blacktail", None, None,
         None, None),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(alerts, "DETECTIONS_DB", path)
    monkeypatch.setattr(alerts, "TAGS_DB", tmp_path / "no-tags.db")
    monkeypatch.setattr(alerts, "_device_for", lambda digest: "cam1")
    monkeypatch.setattr(alerts, "_alert_image", lambda det, digest: None)
    monkeypatch.setattr(alerts, "SEX_CURRENT", tmp_path / "no-sex-model")
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
    assert (lion["taxon_confidence"], lion["taxon_confidence_pct"]) == (0.3024, "30%")
    assert lion["species_guess"] == "puma"
    assert lion["box_area"] == 0.08
    deer = webhook.calls[1]["json"]
    assert (deer["taxon_confidence"], deer["taxon_confidence_pct"], deer["species_guess"]) == (None, "", "")
    assert lion["sex_guess"] == deer["sex_guess"] == ""  # no buck/doe model, no score
    assert "buck_prob" not in deer  # only the guess goes out, never the raw score
    assert operator == []


def _sex_model(tmp_path, monkeypatch, threshold):
    run = tmp_path / "sex/runs/2026-11-01"
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(json.dumps({"threshold": threshold}))
    (tmp_path / "sex/current").symlink_to("runs/2026-11-01")
    monkeypatch.setattr(alerts, "SEX_CURRENT", tmp_path / "sex/current")


def _add_deer(det_db, rows):
    conn = sqlite3.connect(det_db)
    conn.executemany("UPDATE detections SET buck_prob=? WHERE detection_id=?", rows)
    conn.commit()
    conn.close()


@pytest.mark.parametrize("probs, guess", [
    ({"d2": 0.002}, "buck"),       # at/above the model's stored threshold
    ({"d2": 0.0005}, "doe"),
    # a second, fainter deer crop in the same capture: max over crops still says buck
    ({"d2": 0.0005, "d3": 0.003}, "buck"),
])
def test_sex_guess_for_blacktail_only(det_db, webhook, operator, tmp_path, monkeypatch,
                                      probs, guess):
    _sex_model(tmp_path, monkeypatch, 0.001)
    conn = sqlite3.connect(det_db)
    conn.execute("INSERT INTO detections VALUES ('d3', 'sha256:aa11', 'nightly', .5, .5, .1, .1, "
                 "'animal', .5, 'blacktail', NULL, NULL, NULL, NULL)")
    # a lion row with a stray score must not leak into the capture max or its own event
    conn.execute("UPDATE detections SET buck_prob=0.9 WHERE detection_id='d1'")
    conn.commit()
    conn.close()
    _add_deer(det_db, [(p, d) for d, p in probs.items()])
    alerts.check_and_alert(CFG, State(tmp_path / "s.json"), ["sha256:aa11"])
    events = {c["json"]["taxon"]: c["json"] for c in webhook.calls}
    deer = [c["json"] for c in webhook.calls if c["json"]["taxon"] == "blacktail"]
    assert [e["sex_guess"] for e in deer] == [guess, guess]  # capture-level, on every deer crop
    assert events["mountain_lion"]["sex_guess"] == ""


def test_scored_deer_without_model_has_no_guess(det_db, webhook, operator, tmp_path):
    _add_deer(det_db, [(0.9, "d2")])
    alerts.check_and_alert(CFG, State(tmp_path / "s.json"), ["sha256:aa11"])
    assert [c["json"]["sex_guess"] for c in webhook.calls] == ["", ""]


def test_db_without_buck_prob_column_still_alerts(tmp_path, webhook, operator, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE captures (asset_id TEXT, run_id TEXT, station TEXT,
                               capture_time TEXT, media_type TEXT);
        CREATE TABLE detections (detection_id TEXT, asset_id TEXT, run_id TEXT,
            bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL, detector_class TEXT,
            detector_confidence REAL, taxon TEXT, crop_path TEXT,
            taxon_confidence REAL, taxon_raw TEXT);
        INSERT INTO captures VALUES ('sha256:bb', 'nightly', 'Bench', '2026-10-26T12:50:00+00:00',
                                     'image');
        INSERT INTO detections VALUES ('d1', 'sha256:bb', 'nightly', .1, .1, .1, .1, 'animal',
                                       .9, 'blacktail', NULL, NULL, NULL);
    """)
    conn.close()
    monkeypatch.setattr(alerts, "DETECTIONS_DB", path)
    monkeypatch.setattr(alerts, "TAGS_DB", tmp_path / "no-tags.db")
    monkeypatch.setattr(alerts, "_device_for", lambda digest: "cam1")
    monkeypatch.setattr(alerts, "_alert_image", lambda det, digest: None)
    _sex_model(tmp_path, monkeypatch, 0.001)
    alerts.check_and_alert(CFG, State(tmp_path / "s.json"), ["sha256:bb"])
    assert [c["json"]["sex_guess"] for c in webhook.calls] == [""]


def test_buck_threshold_follows_current(tmp_path, monkeypatch):
    _sex_model(tmp_path, monkeypatch, 0.001)
    assert alerts.buck_threshold() == 0.001
    run2 = tmp_path / "sex/runs/2026-12-01"
    run2.mkdir()
    (run2 / "metrics.json").write_text(json.dumps({"threshold": 0.02}))
    (tmp_path / "sex/current").unlink()
    (tmp_path / "sex/current").symlink_to("runs/2026-12-01")
    assert alerts.buck_threshold() == 0.02  # a promotion or rewind needs no daemon restart


def test_sex_step_failure_does_not_fail_the_pipeline(tmp_path, monkeypatch):
    """detect + classify succeed, the buck/doe step crashes or hangs: still True
    (alerts go out without a sex guess), and it was logged."""
    import subprocess

    monkeypatch.setattr(alerts, "PIPELINE_LOCK", tmp_path / ".lock")
    logged = []
    monkeypatch.setattr(alerts, "log", logged.append)
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode, self.stderr = rc, b"Traceback: boom"

    def run(cmd, **kw):
        calls.append(cmd[1])
        if cmd[1].endswith("sex.py"):
            if len(calls) > 3:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return R(1)
        return R(0)
    monkeypatch.setattr(alerts.subprocess, "run", run)
    assert alerts.run_pipeline_incremental() is True
    assert alerts.run_pipeline_incremental() is True
    assert [Path(c).name for c in calls] == ["detect.py", "classify.py", "sex.py"] * 2
    assert len(logged) == 2 and all("buck/doe step failed" in m for m in logged)


def test_classify_failure_skips_sex_step(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "PIPELINE_LOCK", tmp_path / ".lock")
    monkeypatch.setattr(alerts, "log", lambda m: None)
    calls = []

    class R:
        returncode, stderr = 1, b"x"
    monkeypatch.setattr(alerts.subprocess, "run", lambda cmd, **kw: calls.append(cmd[1]) or R())
    assert alerts.run_pipeline_incremental() is False
    assert len(calls) == 1


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
