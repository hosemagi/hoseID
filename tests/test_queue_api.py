"""The review queue's JSON contract, tested through app.queue() as a plain function.

The property under test: video detections must carry the sampled-frame offset
(frame_offset_s) so the front end can anchor the box overlay to the one frame
the boxes describe — a box without its frame is a label pointing at whatever
the clip happens to be showing. Stills must carry the key as None so one
consumer handles both media types.

Per AGENTS.md: import app and monkeypatch the hardcoded module constants
directly (no paths.py refactor); init_db() at import is idempotent and safe.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py lives at the repo root
pytest.importorskip("fastapi")  # app.py is a FastAPI app; skip where it isn't installed
import app

RUN = "r-video"
VID = "a" * 64          # content hash of a clip
IMG = "b" * 64          # content hash of a still


@pytest.fixture
def env(tmp_path, monkeypatch):
    assets, sidecars = tmp_path / "assets", tmp_path / "sidecars"
    det_db, tags_db = tmp_path / "detections.db", tmp_path / "tags.db"
    assets.mkdir(); sidecars.mkdir()

    conn = sqlite3.connect(det_db)
    mig = Path(__file__).parent.parent / "migrations" / "detections"
    for sql in sorted(mig.glob("*.sql")):
        conn.executescript(sql.read_text())
    conn.execute(
        "INSERT INTO runs (run_id, started_at, detector_model, detector_version,"
        " detector_threshold) VALUES (?,?,?,?,?)",
        (RUN, "2026-08-30T02:00:00", "md_v5", "v5", 0.45))
    conn.commit()
    conn.close()

    monkeypatch.setattr(app, "ASSETS", assets)
    monkeypatch.setattr(app, "SIDECARS", sidecars)
    monkeypatch.setattr(app, "DETECTIONS_DB", det_db)
    monkeypatch.setattr(app, "DB_PATH", tags_db)
    app.init_db()                        # reviews/zone tables in the tmp tags DB
    app._pipeline_cache.update(mtime=None, items={})   # mtime-keyed cache, per AGENTS gotcha
    return {"assets": assets, "sidecars": sidecars, "det_db": det_db}


def _seed(env, digest, ext, *, media_type, offset, taxon, conf, bbox, cap_time):
    a, b = digest[:2], digest[2:4]
    (env["assets"] / a / b).mkdir(parents=True)
    (env["assets"] / a / b / f"{digest}{ext}").write_bytes(b"\x00")
    (env["sidecars"] / a / b).mkdir(parents=True)
    (env["sidecars"] / a / b / f"{digest}.json").write_text(
        json.dumps({"device_id": "dev123"}))
    conn = sqlite3.connect(env["det_db"])
    conn.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, n_detections,"
        " has_animal, has_human, has_vehicle, is_empty, media_type,"
        " count_is_lower_bound, sampled_frames) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"capture:{digest}", RUN, "Sage Creek", cap_time, 1, 1, 0, 0, 0,
         media_type, 1 if media_type == "video" else 0, 20))
    conn.execute(
        "INSERT INTO detections (detection_id, asset_id, run_id, bbox_x, bbox_y,"
        " bbox_w, bbox_h, detector_class, detector_confidence, taxon_raw, taxon,"
        " taxon_confidence, frame_offset_s, frame_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"det-{digest}", f"capture:{digest}", RUN, *bbox, "animal", conf,
         f"Ursus americanus;{taxon}", taxon, conf, offset, 0))
    conn.commit()
    conn.close()


def _items(status="all"):
    return {i["asset_id"]: i for i in app.queue(status=status)["items"]}


def test_video_detection_carries_frame_offset_in_queue(env):
    _seed(env, VID, ".mp4", media_type="video", offset=12.3, taxon="black bear",
          conf=0.94, bbox=[0.1, 0.2, 0.4, 0.5], cap_time="2026-08-28T04:12:07")
    item = _items()["capture:" + VID]
    assert item["media_type"] == "video"
    d = item["detections"][0]
    assert d["frame_offset_s"] == 12.3        # the anchor for the box overlay
    assert d["bbox"] == [0.1, 0.2, 0.4, 0.5]
    assert d["excluded"] is False
    sp = item["species"][0]
    assert sp["det_index"] == 0 and sp["taxon"] == "black bear" and sp["score"] == 0.94


def test_still_detection_frame_offset_is_none(env):
    _seed(env, IMG, ".jpg", media_type="image", offset=None, taxon="white-tailed deer",
          conf=0.88, bbox=[0.3, 0.1, 0.2, 0.3], cap_time="2026-08-29T05:00:00")
    item = _items()["capture:" + IMG]
    assert item["detections"][0]["frame_offset_s"] is None


def test_unreviewed_status_returns_video_item(env):
    _seed(env, VID, ".mp4", media_type="video", offset=3.5, taxon="black bear",
          conf=0.94, bbox=[0.1, 0.2, 0.4, 0.5], cap_time="2026-08-28T04:12:07")
    assert "capture:" + VID in _items(status="unreviewed")


def test_review_round_trips_sex(env):
    """Sex set at review time is stored and comes back on the queue item;
    leaving it out means not set."""
    _seed(env, IMG, ".jpg", media_type="image", offset=None, taxon="white-tailed deer",
          conf=0.88, bbox=[0.3, 0.1, 0.2, 0.3], cap_time="2026-08-29T05:00:00")
    _seed(env, VID, ".mp4", media_type="video", offset=3.5, taxon="black bear",
          conf=0.94, bbox=[0.1, 0.2, 0.4, 0.5], cap_time="2026-08-28T04:12:07")
    app.review(app.ReviewIn(asset_id="capture:" + IMG, tags=["deer"], sex="F"))
    app.review(app.ReviewIn(asset_id="capture:" + VID, tags=["bear"]))
    items = _items(status="reviewed")
    assert items["capture:" + IMG]["review"]["sex"] == "F"
    assert items["capture:" + VID]["review"]["sex"] is None


def test_review_rejects_unknown_sex():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        app.ReviewIn(asset_id="x", tags=["deer"], sex="male")
