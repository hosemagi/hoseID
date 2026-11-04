"""Real-time detection events: fresh captures → immediate pipeline pass →
one hoseflow webhook event per detection, where the alert rules live.

Closes the latency gap between the fetch daemon (minutes after capture) and
the 02:30 nightly analysis (hours). After each ingest batch the daemon runs
the SAME incremental pipeline as the nightly — detect + classify under the
standing run_id, then a best-effort buck/doe score of new deer
(stages/sex.py), serialized by a lock file so concurrent invocations (or the
02:30 run) never fight over detections.db — then forwards just the new
assets' detections.

What stays here: detection, classification (plus the buck/doe guess from the
monthly model's own threshold) and the review-layer exclusion
zones (the trampoline never reaches anyone — the zones need tags.db geometry).
Every other detection is forwarded unfiltered by species (invariant 4).

What moved to hoseflow (rules on nvlm, editable in the hoseflow UI): which
taxa page, thresholds, night hours, priority, per (station, reason) cooldown,
and delivery via hosenotify with the crop attached. hoseflow's alert level
also modulates priority (RED prefixes titles / raises priority).

Config [alerts] in credentials.toml:
  hoseflow_url      webhook URL (default: local hoseserv, device wildlife-webhook)
  hoseflow_api_key  hoseserv AUTH_SECRET (X-Api-Key)
  ntfy_server/ntfy_topic are still read by common.notify() for operator alerts.

There is no direct-ntfy fallback (deliberate hard cutover). If the webhook
fails WEBHOOK_FAIL_ALERT_AFTER times in a row, one operator alert goes out
through common.notify() — which talks to ntfy directly, so it still works when
hoseserv or hosenotify is the thing that's down.
"""
from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests  # noqa: F401  (tests patch alerts.requests.post)
import urllib3

from .common import (DEFAULT_WEBHOOK, State, hoseflow_results, log, notify,
                     notifications_sent, post_to_hoseflow)
from hoseid import fsio  # noqa: E402 (sys.path set by .common)
from hoseid.site import TZ as LOCAL_TZ  # noqa: E402 (sys.path set by .common)

REPO = Path(__file__).resolve().parents[1]
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"
TAGS_DB = Path.home() / "trailcam/tags/tags.db"
CROPS = Path.home() / "trailcam/derived/crops"
ASSETS = Path.home() / "trailcam/landing/assets"
PIPELINE_LOCK = Path.home() / "trailcam/derived/.pipeline.lock"
SEX_CURRENT = Path.home() / "trailcam/derived/training/sex/current"

WEBHOOK_FAIL_ALERT_AFTER = 3
# The buck/doe step is best-effort: capped so a backlog (first run, or after
# a retrain is rewound) can't hold the lock for long, newest crops first.
SEX_STEP_LIMIT = 200
SEX_STEP_TIMEOUT_S = 300

# Loopback to hoseserv's self-signed cert; verify is off for loopback hosts
# only (hoseid.hoseserv.verify_for), in common.post_to_hoseflow.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _local_capture_time(capture_time_iso: str) -> datetime:
    t = datetime.fromisoformat(capture_time_iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(LOCAL_TZ)


def run_pipeline_incremental(timeout_s: int = 900) -> bool:
    """Detect + classify anything new in the landing zone, serialized against
    the nightly run via flock, then score new deer buck/doe. Returns False if
    detect or classify failed; the buck/doe step only logs (an alert without
    a sex guess beats a missing alert or a false "alerts DOWN")."""
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(PIPELINE_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)   # waits if nightly is mid-run
        for cmd in (
            [str(REPO / ".venv-detector/bin/python"),
             str(REPO / "stages/detect.py"),
             "--run-id", "nightly", "--threshold", "0.1", "--device", "mps"],
            [str(REPO / ".venv-classifier/bin/python"),
             str(REPO / "stages/classify.py"), "--run-id", "nightly"],
        ):
            r = subprocess.run(cmd, capture_output=True, timeout=timeout_s,
                               cwd=str(REPO))
            if r.returncode != 0:
                log(f"alerts: pipeline step failed: {r.stderr.decode()[-300:]}")
                return False
        _run_sex_step()
    return True


def _run_sex_step() -> None:
    cmd = [str(REPO / ".venv-classifier/bin/python"), str(REPO / "stages/sex.py"),
           "--run-id", "nightly", "--limit", str(SEX_STEP_LIMIT)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=SEX_STEP_TIMEOUT_S,
                           cwd=str(REPO))
    except (OSError, subprocess.SubprocessError) as e:
        log(f"alerts: buck/doe step failed (alerts go out without it): "
            f"{type(e).__name__}: {e}")
        return
    if r.returncode != 0:
        log(f"alerts: buck/doe step failed (alerts go out without it): "
            f"{r.stderr.decode()[-300:]}")


_threshold_cache: dict = {}


def buck_threshold() -> float | None:
    """The current buck/doe model's threshold (metrics.json, recall-first),
    re-read when `current` is repointed or rewritten; None without a model."""
    try:
        metrics = SEX_CURRENT.resolve() / "metrics.json"
        key = (str(metrics), metrics.stat().st_mtime)
    except OSError:
        return None
    if key not in _threshold_cache:
        try:
            thr = json.loads(metrics.read_text()).get("threshold")
        except (OSError, ValueError):
            thr = None
        _threshold_cache.clear()
        _threshold_cache[key] = float(thr) if thr is not None else None
    return _threshold_cache[key]


def capture_buck_prob(conn, asset_id: str, run_id: str) -> float | None:
    """Capture score = max over its blacktail crops ("at least one buck",
    as train_eval scores captures). None if unscored or the DB predates
    migration 004."""
    try:
        row = conn.execute(
            "SELECT MAX(buck_prob) FROM detections WHERE asset_id=? AND run_id=? "
            "AND taxon='blacktail'", (asset_id, run_id)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _active_zones() -> list[dict]:
    if not TAGS_DB.exists():
        return []
    conn = fsio.connect_ro(TAGS_DB, row_factory=True)
    epochs = {r["device_id"]: r["e"] for r in conn.execute(
        "SELECT device_id, MAX(epoch) e FROM camera_epochs GROUP BY device_id")}
    zones = [dict(r) for r in conn.execute("SELECT * FROM exclusion_zones")
             if r["epoch"] == epochs.get(r["device_id"], 1)]
    conn.close()
    return zones


def _in_zone(det, device_id, zones) -> bool:
    x, y, w, h = det["bbox_x"], det["bbox_y"], det["bbox_w"], det["bbox_h"]
    cx, cy = x + w / 2, y + h / 2
    cat = {"animal": "1", "person": "2", "vehicle": "3"}.get(det["detector_class"])
    return any(z["device_id"] == device_id and z["category"] == cat
               and z["x"] <= cx <= z["x"] + z["w"]
               and z["y"] <= cy <= z["y"] + z["h"] for z in zones)


def _device_for(digest: str) -> str | None:
    p = Path.home() / "trailcam/landing/sidecars" / digest[:2] / digest[2:4] / f"{digest}.json"
    if p.exists():
        return json.loads(p.read_text()).get("device_id")
    return None


def _alert_image(det, digest) -> Path | None:
    if det["crop_path"]:
        p = CROPS / det["crop_path"]
        if p.exists():
            return p
    hits = list((ASSETS / digest[:2] / digest[2:4]).glob(f"{digest}.jpg"))
    return hits[0] if hits else None


def build_event(det, cap, device_id: str | None, image: Path | None,
                buck_prob: float | None = None, buck_threshold: float | None = None) -> dict:
    """The webhook payload hoseflow rules match on and template from.

    buck_prob is the capture's buck/doe score, buck_threshold the model's
    own cut-off; only the resulting guess goes out. The raw score is not a
    calibrated probability (the 2026-11 recall-first threshold is ~0.001),
    so a percentage would mislead."""
    taxon = det["taxon"] or ""
    sex_guess = ""
    if taxon == "blacktail" and buck_prob is not None and buck_threshold is not None:
        sex_guess = "buck" if buck_prob >= buck_threshold else "doe"
    conf = det["detector_confidence"]
    local = _local_capture_time(cap["capture_time"])
    label = taxon.replace("_", " ") if taxon else det["detector_class"]
    # The classifier's own certainty and raw guess, separate from the
    # detector's "some animal is here" confidence above: a 98% detection can
    # carry a 30% species call (a bear rolled up to unknown_mustelid).
    tconf = det["taxon_confidence"]
    raw = [p for p in (det["taxon_raw"] or "").split(";")[1:] if p]
    return {
        "asset_id": cap["asset_id"],
        "station": cap["station"],
        "device_id": device_id or "",
        "taxon": taxon,
        "detector_class": det["detector_class"],
        "confidence": round(conf, 4),
        "confidence_pct": f"{conf:.0%}",
        "label": label,
        "reason": taxon or det["detector_class"],
        "taxon_confidence": round(tconf, 4) if tconf is not None else None,
        "taxon_confidence_pct": f"{tconf:.0%}" if tconf is not None else "",
        "species_guess": raw[-1] if raw else "",
        # "buck" / "doe" for a scored blacktail capture (at least one buck
        # crop), "" for other taxa or with no model
        "sex_guess": sex_guess,
        # share of the frame the box covers, 0-1: big = near or large animal
        "box_area": round(det["bbox_w"] * det["bbox_h"], 4),
        "capture_time": cap["capture_time"],
        "capture_hhmm": local.strftime("%H:%M"),
        # capture hour (not processing hour): a 05:50 capture ingested at
        # 06:10 is still a night capture
        "capture_hour": local.hour,
        "media_type": cap["media_type"],
        "video_suffix": " [VIDEO]" if cap["media_type"] == "video" else "",
        "image_path": str(image) if image else "",
    }


def _post_event(cfg: dict, event: dict) -> dict | None:
    """POST one event; returns hoseflow's {fired, results} or None on failure."""
    url = cfg.get("hoseflow_url") or DEFAULT_WEBHOOK
    return post_to_hoseflow(url, cfg.get("hoseflow_api_key", ""), event, "alerts")


def _undelivered(response: dict) -> bool:
    """A rule fired but none of its notifications got out (hosenotify down,
    ntfy refusing): as bad as the webhook failing, for the DOWN alert."""
    results = [r for r in hoseflow_results(response) if "notification" in r]
    return bool(results) and not any(r.startswith("notification:sent") for r in results)


def _track_failures(cfg_all: dict, state: State, failed: list[dict]) -> None:
    """Count consecutive failed batches; raise one operator alert at the
    threshold and one on recovery."""
    streak = state.get("alert_webhook_failures", 0)
    if not failed:
        if streak >= WEBHOOK_FAIL_ALERT_AFTER:
            notify(cfg_all, "hoseflow webhook recovered; wildlife alerts flowing again",
                   title="hoseid alerts recovered")
        if streak:
            state.set("alert_webhook_failures", 0)
        return
    streak += 1
    state.set("alert_webhook_failures", streak)
    if streak == WEBHOOK_FAIL_ALERT_AFTER:
        seen = sorted({f"{e['label']} @ {e['station']}" for e in failed})
        notify(cfg_all,
               f"hoseflow webhook failing ({streak} batches in a row); wildlife "
               f"alerts are NOT being delivered. Undelivered: {', '.join(seen)[:300]}",
               title="hoseid alerts DOWN")


def check_and_alert(cfg_all: dict, state: State, asset_ids: list[str]) -> int:
    """Forward fresh detections to hoseflow; returns notifications sent."""
    cfg = cfg_all.get("alerts", {})
    if not asset_ids:
        return 0
    conn = fsio.connect_ro(DETECTIONS_DB, row_factory=True)
    zones = _active_zones()
    sent = 0
    failed: list[dict] = []

    for asset_id in asset_ids:
        cap = conn.execute(
            "SELECT * FROM captures WHERE asset_id=? ORDER BY rowid DESC LIMIT 1",
            (asset_id,)).fetchone()
        if not cap:
            continue
        digest = asset_id.split(":", 1)[1]
        device = _device_for(digest)
        buck_prob = capture_buck_prob(conn, asset_id, cap["run_id"])
        thr = buck_threshold() if buck_prob is not None else None

        for det in conn.execute(
                "SELECT * FROM detections WHERE asset_id=? AND run_id=?",
                (asset_id, cap["run_id"])):
            if _in_zone(det, device, zones):
                continue
            event = build_event(det, cap, device, _alert_image(det, digest), buck_prob, thr)
            response = _post_event(cfg, event)
            if response is None:
                failed.append(event)
                continue
            n = notifications_sent(response)
            sent += n
            if _undelivered(response):
                log(f"alerts: ERROR {event['label']} @ {event['station']} matched but "
                    f"was not delivered: {json.dumps(response['results'])}")
                failed.append(event)
            if response.get("results"):
                log(f"alerts: {event['label']} @ {event['station']} -> "
                    f"{json.dumps(response['results'])}")
            if n:
                log(f"alerts: SENT {n} for {event['label']} - {event['station']}")
    conn.close()
    _track_failures(cfg_all, state, failed)
    return sent
