"""Shared plumbing for the fetch daemon: config, cursor state, ingest, notify.

Runs in .venv-fetch (requests + pyaarlo). Imports hoseid core the same way the
stages do (sys.path), never the other way around.
"""
from __future__ import annotations

import json
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hoseid import paths  # noqa: E402
from hoseid.landing import StoreResult, store_asset  # noqa: E402
from hoseid.sidecar import Sidecar, compute_asset_id  # noqa: E402

CREDS_PATH = Path.home() / ".config/hoseid-fetch/credentials.toml"
STATE_PATH = paths.derived_dir() / "fetch-state.json"
TMP_DIR = Path.home() / ".config/hoseid-fetch/tmp"


def load_config() -> dict:
    cfg = tomllib.loads(CREDS_PATH.read_text())
    fetch = cfg.setdefault("fetch", {})
    fetch.setdefault("reveal_interval_s", 300)
    fetch.setdefault("arlo_sweep_interval_s", 900)
    fetch.setdefault("backfill_hours", 0)
    return cfg


class State:
    """Cursor state, persisted after every successful batch. Losing this file
    is safe: ingest is idempotent, so a re-scan only costs bandwidth."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def download(url: str, dest: Path, timeout: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
    return dest


def ingest(tmp_file: Path, sidecar_kwargs: dict) -> StoreResult:
    """Build the sidecar for downloaded bytes and land them. The temp copy is
    removed after a successful store."""
    sc = Sidecar(
        asset_id=compute_asset_id(tmp_file),
        bytes=tmp_file.stat().st_size,
        ingested_at=now_utc(),
        **sidecar_kwargs,
    )
    result = store_asset(tmp_file, sc)
    tmp_file.unlink(missing_ok=True)
    return result


def notify_url(cfg: dict) -> str:
    """Where operator alerts go. ``[notify] url`` if set; otherwise the
    ``[alerts]`` ntfy topic P's phone already subscribes to for wildlife
    pages. The fallback exists because for three days in Aug 2026 the
    daemon raised 393 consecutive "library frozen" errors into a notify()
    that had no URL configured, and nobody heard a thing."""
    url = cfg.get("notify", {}).get("url", "")
    if url:
        return url
    a = cfg.get("alerts", {})
    if a.get("ntfy_server") and a.get("ntfy_topic"):
        return f"{a['ntfy_server'].rstrip('/')}/{a['ntfy_topic']}"
    return ""


def notify(cfg: dict, message: str, title: str = "hoseid-fetch") -> None:
    """Best-effort operator alert (ntfy-style POST). Never raises."""
    url = notify_url(cfg)
    if not url:
        log(f"notify: NO URL CONFIGURED, dropping: {message}")
        return
    try:
        # ntfy headers are latin-1; keep the title ASCII-safe.
        requests.post(url, data=message.encode(), timeout=10,
                      headers={"Title": title.encode("ascii", "replace")
                               .decode(), "Priority": "high",
                               "Tags": "warning"})
    except Exception as e:
        log(f"notify: send failed: {type(e).__name__}: {e}")


DEFAULT_HEALTH_WEBHOOK = ("https://127.0.0.1:8000/api/v1/hoseflow/webhooks/"
                          "hoseid-fetch-health")
HEALTH_DEVICE = "hoseid-fetch-health"


def health_url(cfg: dict) -> str:
    """``[notify] hoseflow_health_url`` if set; else the ``[alerts]``
    hoseflow_url host with device hoseid-fetch-health."""
    url = cfg.get("notify", {}).get("hoseflow_health_url", "")
    if url:
        return url
    base = cfg.get("alerts", {}).get("hoseflow_url", "")
    if base:
        return f"{base.rstrip('/').rsplit('/', 1)[0]}/{HEALTH_DEVICE}"
    return DEFAULT_HEALTH_WEBHOOK


def push_health(cfg: dict, source: str, verdict: str, failures: int,
                error: str | None) -> bool:
    """POST one fetch-health transition to hoseflow. Never raises. True only
    when hoseflow answered 2xx AND a rule actually sent a notification: a
    webhook with no (enabled) rule behind it answers 2xx with nothing fired,
    and counting that as delivered would make the outage page silent."""
    key = cfg.get("alerts", {}).get("hoseflow_api_key", "")
    if not key:
        log("health: no [alerts] hoseflow_api_key; using ntfy")
        return False
    payload = {"source": source, "verdict": verdict, "failures": failures,
               "error": error[:200] if error else None}
    try:
        # Loopback to hoseserv's self-signed cert, as in alerts._post_event.
        resp = requests.post(health_url(cfg), json=payload, timeout=30,
                             verify=False, headers={"X-Api-Key": key})
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        log(f"health: hoseflow webhook failed: {type(e).__name__}: {e}")
        return False
    results = [str(r) for rule in ((body or {}).get("results") or {}).values()
               for r in rule.values()]
    if not any(r.startswith("notification:sent") for r in results):
        log(f"health: hoseflow sent no notification for {source} {verdict}: "
            f"{json.dumps(body)[:300]}")
        return False
    return True


def report_health(cfg: dict, state: State, source: str, ok: bool,
                  failures: int, error: str | None, fallback_msg: str) -> None:
    """Announce a source going BROKEN or coming back HEALTHY, once per
    transition. The verdict lives in State, not memory: the daemon exits for
    a launchd restart while a source is failing, and an in-memory flag would
    forget BROKEN and never announce the recovery. ntfy (notify()) is the
    fallback whenever hoseflow didn't deliver, so an alert can't go silent."""
    key = f"fetch_health_{source}"
    verdict = "HEALTHY" if ok else "BROKEN"
    if state.get(key, "HEALTHY") == verdict:
        return
    if not push_health(cfg, source, verdict, failures, None if ok else error):
        notify(cfg, f"hoseid-fetch: {source} recovered" if ok else fallback_msg)
    state.set(key, verdict)


def log(msg: str) -> None:
    print(f"{now_utc().isoformat(timespec='seconds')} {msg}", flush=True)
