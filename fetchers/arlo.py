"""Arlo fetcher: event-driven download of new cloud recordings.

pyaarlo keeps a persistent event stream; any media/motion event schedules a
library sweep (debounced), and a periodic sweep catches anything the stream
missed. The sweep is the single download path — events only decide *when* it
runs — so falling back to pure polling is a no-op config change.

2FA codes go to Sage's Proton mailbox, read via Mail Bridge on localhost.
Bridge serves STARTTLS with a self-signed cert while pyaarlo hardcodes
IMAP4_SSL, hence the BridgeIMAP4 patch (localhost-only, verification relaxed).
The saved session (storage_dir) makes 2FA rare; when it does trigger, it is
fully automatic.
"""
from __future__ import annotations

import imaplib
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path

from .common import TMP_DIR, State, ingest, log, notify

SESSION_DIR = Path.home() / ".config/hoseid-fetch/arlo-session"


class BridgeIMAP4(imaplib.IMAP4):
    def __init__(self, host, port=1143, ssl_context=None):
        super().__init__(host, port)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.starttls(ctx)


imaplib.IMAP4_SSL = BridgeIMAP4

import pyaarlo  # noqa: E402  (after the IMAP patch)


class ArloFetcher:
    def __init__(self, cfg: dict, state: State):
        self._cfg = cfg
        self._state = state
        self._arlo = None
        self.sweep_wanted = threading.Event()

    def connect(self) -> None:
        arlo_cfg, imap = self._cfg["arlo"], self._cfg["tfa_imap"]
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._arlo = pyaarlo.PyArlo(
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
            library_days=2,
            stream_timeout=180,
            reconnect_every=90,
        )
        if not self._arlo.is_connected:
            raise RuntimeError(f"arlo connect failed: {self._arlo.last_error}")
        # Any media-ish event just schedules a sweep; the sweep does the work.
        for cam in self._arlo.cameras:
            cam.add_attr_callback("*", self._on_event)
        log(f"arlo: connected, {len(self._arlo.cameras)} cameras, "
            "event stream live")

    def _on_event(self, device, attr, value) -> None:
        if attr in ("mediaUploadNotification", "motionDetected", "lastImage",
                    "mediaObjectCount"):
            log(f"arlo: event {attr} from {device.name!r} -> sweep scheduled")
            self.sweep_wanted.set()

    def sweep(self) -> list[str]:
        """Download every cloud recording newer than each camera's cursor."""
        cursors = self._state.get("arlo_cursors", {})
        ingested = 0
        new_assets: list[str] = []
        for cam in self._arlo.cameras:
            cur = cursors.get(cam.device_id, 0)
            for vid in reversed(cam.last_n_videos(50) or []):
                created_ms = int(vid.created_at or 0)
                if created_ms <= cur:
                    continue
                name = f"{cam.device_id}_{created_ms}.mp4"
                tmp = TMP_DIR / name
                tmp.parent.mkdir(parents=True, exist_ok=True)
                vid.download_video(str(tmp))
                if not tmp.exists() or tmp.stat().st_size == 0:
                    log(f"arlo: EMPTY download {name}; will retry next sweep")
                    notify(self._cfg, f"hoseid-fetch: empty Arlo download {name}")
                    continue
                aid = self._ingest_video(cam, vid, tmp, created_ms)
                if aid:
                    ingested += 1
                    new_assets.append(aid)
                cursors[cam.device_id] = created_ms
                self._state.set("arlo_cursors", cursors)
        if ingested:
            log(f"arlo: sweep ingested {ingested} recording(s)")
        return new_assets

    def _ingest_video(self, cam, vid, tmp: Path, created_ms: int) -> str | None:
        from hoseid.video import probe_safe
        meta, err = probe_safe(tmp)
        video_fields = dict(
            duration_s=meta.duration_s, fps=meta.fps, frame_count=meta.frame_count,
        ) if meta else dict(probe_status="failed", probe_error=err)
        result = ingest(tmp, dict(
            media_type="video",
            source="arlo_cloud",
            resolution_class="original",   # the cloud clip IS the recording
            station=cam.name,
            station_source="vendor_label",
            device_id=cam.device_id,
            vendor="arlo",
            vendor_asset_id=getattr(vid, "id", None) or f"{cam.device_id}_{created_ms}",
            capture_time=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc),
            capture_time_source="vendor_api",
            capture_time_confidence="high",
            trigger_type="motion",
            raw_vendor_payload={"camera": cam.name, "model": cam.model_id,
                                "created_ms": created_ms,
                                "content_type": vid.content_type},
            **video_fields,
        ))
        return None if result.already_present else result.asset_id

    def bootstrap_cursors(self) -> None:
        """First run: point cursors at the newest existing recordings so we
        only fetch captures that happen from now on."""
        cursors = {}
        for cam in self._arlo.cameras:
            vids = cam.last_n_videos(1) or []
            cursors[cam.device_id] = int(vids[0].created_at) if vids else 0
        self._state.set("arlo_cursors", cursors)
        log(f"arlo: bootstrapped cursors for {len(cursors)} cameras")

    def stop(self) -> None:
        if self._arlo:
            self._arlo.stop(logout=False)
