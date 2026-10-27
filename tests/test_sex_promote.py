"""Monthly buck/doe retrain: a new run is promoted unless its CV precision
at the target recall drops by more than MAX_DROP; a held-back run is kept
and the operator is told."""
import json
import sys
import types

import pytest

from training.sex import common, mail_report, promote


def _metrics(cv_p):
    return {"target_recall": 0.9, "cv_precision_at_target": cv_p, "eval_ap": 0.9,
            "eval_recall": 0.88, "eval_precision": 0.5, "eval_bucks": 60, "eval_does": 200,
            "train_crops": 1000}


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUT", tmp_path)
    monkeypatch.setattr(promote, "OUT", tmp_path)
    monkeypatch.setattr(promote, "HISTORY", tmp_path / "promotions.jsonl")
    class Sent(list):
        mailed: list
    sent = Sent()
    fake = types.ModuleType("fetchers.common")
    fake.load_config = lambda: {}
    fake.notify = lambda cfg, msg, title="": sent.append((title, msg))
    monkeypatch.setitem(sys.modules, "fetchers.common", fake)
    mailed = []
    monkeypatch.setattr(mail_report, "report",
                        lambda run, status, old, prev: mailed.append((run.name, status)) or "ok")
    sent.mailed = mailed
    return tmp_path, sent


def _run(out, day, cv_p, monkeypatch):
    out = out[0]
    (out / "metrics.json").write_text(json.dumps(_metrics(cv_p)))
    (out / "sex_head.pt").write_bytes(b"model")

    class D:
        @staticmethod
        def today():
            return types.SimpleNamespace(isoformat=lambda: day)
    monkeypatch.setattr(promote, "date", D)
    monkeypatch.setattr(sys, "argv", ["promote.py"])
    assert promote.main() == 0


def test_first_run_becomes_current(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    assert (root / "current").resolve().name == "2026-10-01"
    assert sent == []


def test_small_drop_still_promotes(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    _run(out, "2026-11-01", 0.47, monkeypatch)
    assert (root / "current").resolve().name == "2026-11-01"
    assert sent == []


def test_big_drop_is_held_back_and_reported(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    _run(out, "2026-11-01", 0.40, monkeypatch)
    assert (root / "current").resolve().name == "2026-10-01"
    assert (root / "runs" / "2026-11-01" / "sex_head.pt").exists()  # kept, not promoted
    (title, msg), = sent
    assert "held back" in title and "0.40" in msg and "0.50" in msg


def test_every_run_is_emailed_with_its_status(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    _run(out, "2026-11-01", 0.40, monkeypatch)
    assert sent.mailed == [("2026-10-01", "promoted"), ("2026-11-01", "held_back")]


def test_email_failure_falls_back_to_ntfy(out, monkeypatch):
    root, sent = out

    def down(*a):
        raise ConnectionRefusedError("bridge down")
    monkeypatch.setattr(mail_report, "report", down)
    _run(out, "2026-10-01", 0.50, monkeypatch)
    assert (root / "current").resolve().name == "2026-10-01"  # still promoted
    (title, msg), = sent
    assert "not emailed" in title and "bridge down" in msg and "report.txt" in msg


def test_report_message_renders_from_a_real_run_shape(tmp_path):
    run = tmp_path / "2026-11-01"
    run.mkdir()
    (run / "metrics.json").write_text(json.dumps({
        "features": "dino:cls+mean", "head": "mlp", "wd": 0.1, "threshold": 0.002,
        "target_recall": 0.9, "cv_precision_at_target": 0.51, "cv_recall": 0.9, "eval_ap": 0.89,
        "eval_recall": 0.88, "eval_precision": 0.53, "eval_bucks": 67, "eval_does": 211,
        "train_crops": 1158}))
    (run / "report.txt").write_text("== HELD-OUT EVAL <script>")
    (run / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"asset_id": "a", "split": "train", "label": 1, "visit": "v1", "station": "Bench",
         "ir": True, "date": "2026-10-01", "legacy": False, "crop": "/nope.jpg"},
        {"asset_id": "b", "split": "eval", "label": 0, "visit": "v2", "station": "Path",
         "ir": False, "date": "2026-10-02", "legacy": True, "crop": "/nope.jpg"}]))
    old = {"cv_precision_at_target": 0.45, "eval_ap": 0.85, "eval_recall": 0.9, "eval_precision": 0.5}
    msg = mail_report.build_message(run, "promoted", old, None, "sage@x", ["p@x"])
    html_body = msg.get_body(("html",)).get_content()
    assert "updated" in msg["Subject"] and "AP 0.89" in msg["Subject"]
    assert "(+0.06)" in html_body  # CV precision delta vs the previous model
    assert "&lt;script&gt;" in html_body and "<script>" not in html_body  # report escaped
    assert "1</b> buck" in html_body and "first run" in html_body
    assert msg["To"] == "p@x"


def test_same_day_rerun_gets_its_own_run_dir(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    _run(out, "2026-10-01", 0.52, monkeypatch)
    assert sorted(p.name for p in (root / "runs").iterdir()) == ["2026-10-01", "2026-10-01-2"]
    assert (root / "current").resolve().name == "2026-10-01-2"


def test_rewind_with_use_and_list(out, monkeypatch):
    root, sent = out
    _run(out, "2026-10-01", 0.50, monkeypatch)
    _run(out, "2026-11-01", 0.48, monkeypatch)
    assert promote.use("2026-10-01") == "current -> 2026-10-01"
    assert (root / "current").resolve().name == "2026-10-01"
    listing = promote.list_runs()
    assert listing.splitlines()[0].startswith("* 2026-10-01")
    assert listing.splitlines()[1].startswith("  2026-11-01")
    history = [json.loads(x) for x in (root / "promotions.jsonl").read_text().splitlines()]
    assert history[-1]["from"] == "2026-11-01" and history[-1]["to"] == "2026-10-01"
    assert history[-1]["reason"] == "manual --use"
    with pytest.raises(SystemExit):
        promote.use("1999-01-01")


def test_keeps_three_newest_runs(out, monkeypatch):
    root, sent = out
    for month in ("08", "09", "10", "11", "12"):
        _run(out, f"2026-{month}-01", 0.50, monkeypatch)
    assert sorted(p.name for p in (root / "runs").iterdir()) == \
        ["2026-10-01", "2026-11-01", "2026-12-01"]
    pruned = [json.loads(x) for x in (root / "promotions.jsonl").read_text().splitlines()
              if "pruned" in x]
    assert [p["pruned"] for p in pruned] == [["2026-08-01"], ["2026-09-01"]]


def test_rewound_current_is_never_pruned(out, monkeypatch):
    root, sent = out
    for month in ("08", "09", "10"):
        _run(out, f"2026-{month}-01", 0.50, monkeypatch)
    promote.use("2026-08-01")  # rewind to the oldest
    _run(out, "2026-11-01", 0.30, monkeypatch)  # held back: current stays 08
    names = sorted(p.name for p in (root / "runs").iterdir())
    assert "2026-08-01" in names and (root / "current").resolve().name == "2026-08-01"
    assert names == ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"]
