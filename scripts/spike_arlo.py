#!/usr/bin/env python
"""Spike: authenticate to Arlo (2FA via Sage's Proton mailbox through Bridge),
list cameras, pull the most recent cloud recording.

Proves: pyaarlo auth with IMAP TFA against localhost Bridge, session
persistence (storage_dir -> subsequent runs should skip 2FA), device listing,
library access, video download. Downloads land in the scratch dir.

Run: .venv-fetch/bin/python scripts/spike_arlo.py
"""
from __future__ import annotations

import imaplib
import ssl
import sys
import tomllib
from pathlib import Path

import pyaarlo


class BridgeIMAP4(imaplib.IMAP4):
    """IMAP for Proton Bridge: STARTTLS on localhost with a self-signed cert.

    pyaarlo hardcodes imaplib.IMAP4_SSL, but Bridge serves STARTTLS on 1143
    and its cert is self-signed (so IMAP4_SSL would fail even in Bridge's SSL
    mode). Localhost-only traffic; skipping verification is acceptable here.
    Installed process-wide below so pyaarlo's tfa module picks it up.
    """

    def __init__(self, host, port=1143, ssl_context=None):
        super().__init__(host, port)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.starttls(ctx)


imaplib.IMAP4_SSL = BridgeIMAP4

CREDS = Path.home() / ".config/hoseid-fetch/credentials.toml"
SESSION_DIR = Path.home() / ".config/hoseid-fetch/arlo-session"
OUT = Path("/private/tmp/hoseid-spike")


def main() -> int:
    cfg = tomllib.loads(CREDS.read_text())
    arlo_cfg, imap = cfg["arlo"], cfg["tfa_imap"]
    if arlo_cfg["email"] == "FILL_ME":
        print("credentials.toml [arlo] not filled in"); return 1

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print("== Arlo login (2FA code will be read from Sage's mailbox via Bridge)")
    arlo = pyaarlo.PyArlo(
        username=arlo_cfg["email"],
        password=arlo_cfg["password"],
        tfa_source="imap",
        tfa_type="email",
        tfa_host=f"{imap['host']}:{imap['port']}",
        tfa_username=imap["username"],
        tfa_password=imap["password"],
        storage_dir=str(SESSION_DIR),
        save_session=True,
        synchronous_mode=True,
        library_days=7,
    )
    if not arlo.is_connected:
        print(f"connect failed: {arlo.last_error}"); return 1

    print("== connected; devices:")
    for c in arlo.cameras:
        print(f"   camera: {c.name!r} model={c.model_id} battery={c.battery_level}%")
    for b in arlo.base_stations:
        print(f"   base:   {b.name!r} model={b.model_id}")

    vids = []
    for c in arlo.cameras:
        vids += [(v, c) for v in (c.last_n_videos(3) or [])]
    if not vids:
        print("no cloud recordings in the last 7 days -- is Arlo Secure active?")
        arlo.stop(logout=False)
        return 1

    vids.sort(key=lambda vc: vc[0].created_at or 0, reverse=True)
    v, cam = vids[0]
    print(f"== newest recording: cam={cam.name!r} at={v.created_at_pretty()} "
          f"type={v.content_type}")
    OUT.mkdir(exist_ok=True)
    dest = OUT / "arlo_latest.mp4"
    v.download_video(str(dest))
    size = dest.stat().st_size if dest.exists() else 0
    print(f"== downloaded {size} bytes -> {dest}")

    arlo.stop(logout=False)   # keep the trusted session alive
    return 0 if size else 1


if __name__ == "__main__":
    sys.exit(main())
