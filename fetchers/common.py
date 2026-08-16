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


def notify(cfg: dict, message: str) -> None:
    """Best-effort operator alert (ntfy-style POST). Never raises."""
    url = cfg.get("notify", {}).get("url", "")
    if not url:
        return
    try:
        requests.post(url, data=message.encode(), timeout=10)
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"{now_utc().isoformat(timespec='seconds')} {msg}", flush=True)
