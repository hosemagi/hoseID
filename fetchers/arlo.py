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
import time
from datetime import datetime, timezone
from pathlib import Path

from .common import TMP_DIR, State, ingest, log, notify

SESSION_DIR = Path.home() / ".config/hoseid-fetch/arlo-session"

# Events that mean a clip was actually written to the cloud, as opposed to
# motion that may never have been recorded (disarmed camera, dead battery).
MEDIA_EVENTS = ("mediaUploadNotification", "mediaObjectCount")

# How far the library may lag a media event before we call it frozen. Generous:
# the sweep interval is 900s and uploads are not instant.
LIBRARY_STALE_AFTER_S = 3600

# hoseID covers the cabin property only. The Arlo account also holds cameras at
# another property, labelled with an "MH - " prefix; those are not cabin
# cameras and must never reach the wildlife review queue. Filtering at the
# fetch boundary rather than at analysis time keeps them out of the landing
# zone entirely, which matters because the landing zone is append-only --
# anything ingested here can only be removed by hand.
# Override per-deployment with `exclude_station_prefixes` in [arlo].
DEFAULT_EXCLUDED_PREFIXES = ("MH -",)


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
        self._last_media_event = 0.0
        self._excluded_prefixes = tuple(
            cfg.get("arlo", {}).get("exclude_station_prefixes",
                                    DEFAULT_EXCLUDED_PREFIXES))

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
        # Excluded cameras get no callback, so they never even wake a sweep.
        watched = [c for c in self._arlo.cameras if not self.is_excluded(c.name)]
        for cam in watched:
            cam.add_attr_callback("*", self._on_event)
        skipped = len(self._arlo.cameras) - len(watched)
        log(f"arlo: connected, {len(watched)} cameras, event stream live"
            + (f" ({skipped} excluded by prefix {self._excluded_prefixes})"
               if skipped else ""))

    def is_excluded(self, station: str | None) -> bool:
        return bool(station) and station.startswith(self._excluded_prefixes)

    def _on_event(self, device, attr, value) -> None:
        if attr in ("mediaUploadNotification", "motionDetected", "lastImage",
                    "mediaObjectCount"):
            if attr in MEDIA_EVENTS:
                self._last_media_event = time.time()
            log(f"arlo: event {attr} from {device.name!r} -> sweep scheduled")
            self.sweep_wanted.set()

    def sweep(self) -> list[str]:
        """Download every cloud recording newer than each camera's cursor."""
        cursors = self._state.get("arlo_cursors", {})
        ingested = 0
        newest_ms = 0
        new_assets: list[str] = []
        for cam in self._arlo.cameras:
            if self.is_excluded(cam.name):
                continue
            cur = cursors.get(cam.device_id, 0)
            for vid in reversed(cam.last_n_videos(50) or []):
                created_ms = int(vid.created_at or 0)
                newest_ms = max(newest_ms, created_ms)
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
        else:
            self._assert_library_fresh(newest_ms)
        return new_assets

    def _assert_library_fresh(self, newest_ms: int) -> None:
        """A sweep that ingests nothing is normally just a quiet night -- but it
        is also exactly what a frozen media library looks like, and that
        failure is silent: pyaarlo serves each camera's cached video list, so
        `last_n_videos` keeps returning stale rows and nothing raises.

        The event stream is the independent witness. A media event means a clip
        reached the cloud, so a library whose newest recording predates that
        event by hours is stale, not empty. Raise, so the daemon's consecutive-
        failure counter and notify() path actually fire instead of the fetcher
        reporting health while ingesting nothing.

        Only checked when nothing was ingested, so this can never discard work.
        A wholly empty library (newest_ms == 0) is left alone -- it is the
        legitimate state before the first recording lands.
        """
        if not self._last_media_event or not newest_ms:
            return
        lag_s = self._last_media_event - newest_ms / 1000
        if lag_s <= LIBRARY_STALE_AFTER_S:
            return
        newest = datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc)
        raise RuntimeError(
            f"media library appears frozen: newest recording is "
            f"{newest.isoformat()}, but a media-upload event arrived "
            f"{lag_s / 3600:.1f}h after it. The library cache has stopped "
            f"refreshing -- restart the daemon."
        )

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
            if self.is_excluded(cam.name):
                continue
            vids = cam.last_n_videos(1) or []
            cursors[cam.device_id] = int(vids[0].created_at) if vids else 0
        self._state.set("arlo_cursors", cursors)
        log(f"arlo: bootstrapped cursors for {len(cursors)} cameras")

    def stop(self) -> None:
        if self._arlo:
            self._arlo.stop(logout=False)
