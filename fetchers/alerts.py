"""Real-time detection alerts: fresh captures → immediate pipeline pass →
ntfy push (with image) when something alert-worthy appears.

Closes the latency gap between the fetch daemon (minutes after capture) and
the 02:30 nightly analysis (hours). After each ingest batch the daemon runs
the SAME incremental pipeline as the nightly — detect + classify under the
standing run_id, serialized by a lock file so concurrent invocations (or the
02:30 run) never fight over detections.db — then applies the alert rules to
just the new assets.

Alert policy (config [alerts] in credentials.toml, defaults below):
  - always-alert taxa regardless of confidence: mountain_lion, unknown_felid,
    unknown_carnivore (invariant-4 alignment: a rolled-up lion must reach P)
  - bear at detector conf >= bear_min_conf
  - person detections during night hours (family asleep; daytime people are
    usually family)
  - review-layer exclusion zones apply (the trampoline never pages anyone)
  - per (station, species) cooldown so a feeding bear is one alert, not forty

Notifications go direct to ntfy (P's established pattern) as a PUT with the
detection crop attached, so the phone notification carries the image.
"""
from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import requests

from .common import State, log

REPO = Path(__file__).resolve().parents[1]
DETECTIONS_DB = Path.home() / "trailcam/derived/detections.db"
TAGS_DB = Path.home() / "trailcam/tags/tags.db"
CROPS = Path.home() / "trailcam/derived/crops"
ASSETS = Path.home() / "trailcam/landing/assets"
PIPELINE_LOCK = Path.home() / "trailcam/derived/.pipeline.lock"

DEFAULTS = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "always_taxa": ["mountain_lion", "unknown_felid", "unknown_carnivore"],
    "bear_taxa": ["black_bear"],
    "bear_min_conf": 0.5,
    "night_person": True,
    "night_hours": [22, 6],          # local, start/end
    "cooldown_min": 30,
}


def run_pipeline_incremental(timeout_s: int = 900) -> bool:
    """Detect + classify anything new in the landing zone, serialized against
    the nightly run via flock. Returns False if the pipeline failed."""
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
    return True


def _active_zones() -> list[dict]:
    if not TAGS_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{TAGS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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


def _ascii(s: str) -> str:
    """HTTP headers are latin-1; ntfy titles must survive ASCII-only clients."""
    return s.replace("\u2014", "-").encode("ascii", "replace").decode()


def _notify(cfg: dict, title: str, message: str, priority: str,
            tags: str, image: Path | None) -> bool:
    url = f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}"
    headers = {"Title": _ascii(title), "Message": _ascii(message),
               "Priority": priority, "Tags": tags}
    try:
        if image is not None:
            headers["Filename"] = image.name
            requests.put(url, data=image.read_bytes(), headers=headers,
                         timeout=30)
        else:
            requests.post(url, data=_ascii(message).encode(), headers={
                "Title": _ascii(title), "Priority": priority, "Tags": tags},
                timeout=30)
        return True
    except Exception as e:
        log(f"alerts: ntfy send failed: {type(e).__name__}: {e}")
        return False


def check_and_alert(cfg_all: dict, state: State, asset_ids: list[str]) -> int:
    """Apply alert rules to freshly processed assets; returns alerts sent."""
    cfg = {**DEFAULTS, **cfg_all.get("alerts", {})}
    if not cfg["ntfy_topic"] or not asset_ids:
        return 0
    conn = sqlite3.connect(f"file:{DETECTIONS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    zones = _active_zones()
    cooldowns = state.get("alert_cooldowns", {})
    now = time.time()
    sent = 0

    for asset_id in asset_ids:
        cap = conn.execute(
            "SELECT * FROM captures WHERE asset_id=? ORDER BY rowid DESC LIMIT 1",
            (asset_id,)).fetchone()
        if not cap:
            continue
        digest = asset_id.split(":", 1)[1]
        device = _device_for(digest)
        local_hour = int(time.localtime().tm_hour)
        n0, n1 = cfg["night_hours"]
        is_night = local_hour >= n0 or local_hour < n1

        for det in conn.execute(
                "SELECT * FROM detections WHERE asset_id=? AND run_id=?",
                (asset_id, cap["run_id"])):
            if _in_zone(det, device, zones):
                continue
            taxon = det["taxon"] or ""
            conf = det["detector_confidence"]
            reason = priority = None
            if taxon in cfg["always_taxa"]:
                reason, priority = taxon, "urgent"
            elif taxon in cfg["bear_taxa"] and conf >= cfg["bear_min_conf"]:
                reason, priority = "bear", "high"
            elif (cfg["night_person"] and det["detector_class"] == "person"
                  and conf >= 0.5 and is_night):
                reason, priority = "person (night)", "high"
            if reason is None:
                continue
            key = f"{cap['station']}|{reason}"
            if now - cooldowns.get(key, 0) < cfg["cooldown_min"] * 60:
                continue
            cooldowns[key] = now
            state.set("alert_cooldowns", cooldowns)
            label = taxon.replace("_", " ") if taxon else det["detector_class"]
            title = f"{label} - {cap['station']}"
            msg = (f"{label} at {cap['station']}, conf {conf:.2f}, "
                   f"{cap['capture_time'][11:16]} UTC"
                   + (" [VIDEO]" if cap["media_type"] == "video" else ""))
            emoji = ("lion" if "lion" in reason or "felid" in reason
                     else "bear" if reason == "bear" else "rotating_light")
            if _notify(cfg, title, msg, priority, emoji,
                       _alert_image(det, digest)):
                log(f"alerts: SENT [{priority}] {title}")
                sent += 1
            else:
                log(f"alerts: FAILED to send [{priority}] {title}")
    conn.close()
    return sent
