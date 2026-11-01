"""Where hoseserv is, its key, and the one TLS rule for talking to it.

The fetch daemon's hoseflow webhooks (fetchers/common.py), the Arlo rename
logbook note (fetchers/arlo.py) and the hosemap layer rename
(camera_names.rename_in_map_layers) all reach the same hoseserv, configured
by ``[alerts] hoseflow_url`` / ``hoseflow_api_key`` in the fetch credentials.

hoseserv serves self-signed TLS on loopback, so certificate verification is
off for localhost / 127.0.0.1 / ::1 and on for any other host (the same
rule as hoseint's hoseserv client).

No third-party imports at module level: hoseid core runs in several venvs.
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

DEFAULT_WEBHOOK = "https://127.0.0.1:8000/api/v1/hoseflow/webhooks/wildlife-webhook"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def verify_for(url: str) -> bool:
    """False (skip TLS verification) only for loopback hosts."""
    return urlsplit(url).hostname not in LOOPBACK_HOSTS


def webhook_url(cfg: dict) -> str:
    """``[alerts] hoseflow_url``, or the local wildlife webhook."""
    return cfg.get("alerts", {}).get("hoseflow_url") or DEFAULT_WEBHOOK


def base_url(cfg: dict) -> str:
    """hoseserv's scheme://host[:port] from the webhook URL, whatever its
    path looks like."""
    parts = urlsplit(webhook_url(cfg))
    return f"{parts.scheme}://{parts.netloc}"


def api_key(cfg: dict) -> str:
    return cfg.get("alerts", {}).get("hoseflow_api_key", "")


def headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key}


def request(method: str, url: str, key: str, *, json=None, timeout: float = 15,
            label: str = "hoseserv", log: Callable[[str], None] = print):
    """One best-effort call to hoseserv. The Response on 2xx; None on any
    failure, logged as ``{label} failed: {Type}: {error}``. Never raises."""
    try:
        import requests
        resp = requests.request(method, url, json=json, headers=headers(key),
                                timeout=timeout, verify=verify_for(url))
        resp.raise_for_status()
        return resp
    except Exception as e:  # noqa: BLE001 — callers are best effort
        log(f"{label} failed: {type(e).__name__}: {e}")
        return None
