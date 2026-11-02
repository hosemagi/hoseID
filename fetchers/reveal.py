"""Reveal fetcher: poll the (unofficial) Reveal API for new photos.

No push channel exists for Reveal; the cameras batch-transmit on their own
schedule, so polling latency is dominated by the camera anyway. Auth is AWS
Cognito USER_PASSWORD_AUTH; the API wants the AccessToken (12 h) and we renew
via the RefreshToken, falling back to a full login.

Capture time: photoTimestamp is camera-local wall-clock (matches the burned-in
banner), MMDDYYYYHHMMSS, with no zone marker — interpreted as the property's
zone (America/Los_Angeles). Trusted (vendor_api) but flagged medium rather
than high confidence purely because of the implicit zone.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .common import TMP_DIR, State, download, ingest, log, now_utc
from hoseid import paths  # noqa: E402 (sys.path set by .common)
from hoseid.landing import ImmutabilityError  # noqa: E402
from hoseid.station_renames import normalize_station  # noqa: E402

COGNITO = "https://cognito-idp.us-east-1.amazonaws.com/"
CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
API = "https://api.reveal.ishareit.net/v1"
UA = "RevealWeb/5.4.0"
PROPERTY_TZ = ZoneInfo("America/Los_Angeles")
HISTORY_DAYS = 180        # station_health_history.jsonl keeps this many days
PAGE_SIZE = 100


class RevealClient:
    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._session = requests.Session()
        self._session.headers["User-Agent"] = UA
        self._refresh_token = None
        self._token_expiry = 0.0

    def _cognito(self, flow: str, params: dict) -> dict:
        r = requests.post(
            COGNITO,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
            json={"AuthFlow": flow, "ClientId": CLIENT_ID, "AuthParameters": params},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["AuthenticationResult"]

    def _login(self) -> None:
        auth = self._cognito("USER_PASSWORD_AUTH",
                            {"USERNAME": self._email, "PASSWORD": self._password})
        self._apply(auth)
        self._refresh_token = auth["RefreshToken"]
        log("reveal: full login ok")

    def _apply(self, auth: dict) -> None:
        self._session.headers["Authorization"] = f"Bearer {auth['AccessToken']}"
        self._token_expiry = time.time() + int(auth.get("ExpiresIn", 43200)) - 600

    def _ensure_auth(self) -> None:
        if time.time() < self._token_expiry:
            return
        if self._refresh_token:
            try:
                self._apply(self._cognito(
                    "REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": self._refresh_token}))
                log("reveal: token refreshed")
                return
            except Exception as e:
                log(f"reveal: refresh failed ({e}); doing full login")
        self._login()

    def _get(self, path: str, **params) -> dict:
        self._ensure_auth()
        r = self._session.get(f"{API}{path}", params=params, timeout=60)
        if r.status_code == 401:
            self._token_expiry = 0.0
            self._ensure_auth()
            r = self._session.get(f"{API}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()["response"]

    def photos(self, page: int = 0, size: int = PAGE_SIZE) -> list[dict]:
        return self._get("/photos", size=size, page=page,
                         includeWeatherData="true")["photos"]


def _capture_time(item: dict) -> datetime:
    ts = item["photoTimestamp"]  # MMDDYYYYHHMMSS, camera-local
    return datetime(int(ts[4:8]), int(ts[0:2]), int(ts[2:4]),
                    int(ts[8:10]), int(ts[10:12]), int(ts[12:14]),
                    tzinfo=PROPERTY_TZ)


def _conditions(item: dict) -> dict:
    meta = item.get("metadata") or {}
    return {
        "battery_pct": int(meta["batteryLevel"]) if meta.get("batteryLevel") else None,
        "signal_bars": int(meta["signal"]) if meta.get("signal") else None,
    }


def _note_health(readings: dict, item: dict, conditions: dict) -> None:
    """Keep the newest battery/signal reading per (renamed) station. Newest
    by capture time, not fetch order: Reveal re-serves old photos with their
    old telemetry."""
    if conditions["battery_pct"] is None and conditions["signal_bars"] is None:
        return
    when = _capture_time(item)
    station = normalize_station(item.get("cameraName") or "unknown",
                                when.date().isoformat())
    prev = readings.get(station)
    if prev is None or when > prev["when"]:
        readings[station] = {**conditions, "when": when}


def write_station_health(readings: dict, path=None) -> None:
    """Merge readings into station_health.json (read by hoseserv's map and
    Security view), keeping an existing entry whose reading is newer.
    Replaced atomically so a reader never sees half a file."""
    path = path or paths.station_health_file()
    try:
        old = json.loads(path.read_text()).get("stations", {})
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError, AttributeError):
        old = {}
    stations = dict(old)
    for name, r in readings.items():
        when = r["when"].astimezone(timezone.utc)
        try:
            prev = datetime.fromisoformat(old[name]["capture_time"])
            if prev.tzinfo is not None and prev >= when:
                continue
        except (KeyError, TypeError, ValueError):
            pass
        stations[name] = {"battery_pct": r["battery_pct"],
                          "signal_bars": r["signal_bars"],
                          "capture_time": when.isoformat()}
    body = json.dumps({"updated": now_utc().isoformat(timespec="seconds"),
                       "stations": stations}, indent=1, sort_keys=True)
    _replace_file(path, body + "\n")


def _history_row(line: str) -> dict | None:
    """One well-formed station_health_history.jsonl line, else None."""
    try:
        row = json.loads(line)
        date.fromisoformat(row["date"])
        if not (isinstance(row["station"], str) and isinstance(row["capture_time"], str)
                and isinstance(row["battery_pct"], int)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return {k: row[k] for k in ("date", "station", "battery_pct", "capture_time")}


def append_health_history(readings: dict, path=None, today: date | None = None) -> None:
    """Record one battery sample per station per property-local day in
    station_health_history.jsonl (hoseserv fits its "days left" estimate to
    it). A same-day sample is replaced only by a later capture; days older
    than HISTORY_DAYS are dropped. The file is tiny, so it is rewritten whole
    and replaced atomically; unreadable lines are discarded."""
    path = path or paths.station_health_history_file()
    try:
        text = path.read_text()
    except (OSError, ValueError):
        text = ""
    rows: dict[tuple[str, str], dict] = {}
    for line in text.splitlines():
        row = _history_row(line)
        if row is not None:
            rows[(row["date"], row["station"])] = row
    for name, r in readings.items():
        if r.get("battery_pct") is None:
            continue
        when = r["when"].astimezone(timezone.utc)
        key = (r["when"].astimezone(PROPERTY_TZ).date().isoformat(), name)
        try:
            prev = datetime.fromisoformat(rows[key]["capture_time"])
            if prev.tzinfo is not None and prev >= when:
                continue
        except (KeyError, ValueError):
            pass
        rows[key] = {"date": key[0], "station": name,
                     "battery_pct": int(r["battery_pct"]),
                     "capture_time": when.isoformat()}
    today = today or now_utc().astimezone(PROPERTY_TZ).date()
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    body = "".join(json.dumps(rows[k], sort_keys=True) + "\n"
                   for k in sorted(rows) if k[0] >= cutoff)
    _replace_file(path, body)


def _replace_file(path, body: str) -> None:
    """Write body to path via a temp file + os.replace, so a reader never
    sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def poll(client: RevealClient, state: State) -> list[str]:
    """One poll pass: fetch photos newer than the cursor, ingest, advance.

    Cursor is the vendor createdTimestamp (ms epoch) of the newest ingested
    photo. First run with no cursor bootstraps to the newest photo visible
    (optionally minus backfill_hours, handled by the daemon) so the manually
    ingested batch archive is never re-downloaded.
    """
    cursor = state.get("reveal_cursor_ms")
    if cursor is None:
        newest = client.photos(size=1)
        cursor = newest[0]["createdTimestamp"] if newest else 0
        state.set("reveal_cursor_ms", cursor)
        log(f"reveal: bootstrapped cursor to {cursor}")
        return []

    new_items: list[dict] = []
    page = 0
    while True:
        batch = client.photos(page=page)
        fresh = [p for p in batch if p["createdTimestamp"] > cursor]
        new_items += fresh
        if not batch or len(fresh) < len(batch):
            break            # crossed the cursor inside this page
        page += 1

    ingested = 0
    conflicts = 0
    new_assets: list[str] = []
    readings: dict[str, dict] = {}
    for item in sorted(new_items, key=lambda p: p["createdTimestamp"]):
        pid = item["photoId"]
        conditions = _conditions(item)
        _note_health(readings, item, conditions)
        tmp = download(item["photoUrl"], TMP_DIR / pid)
        sidecar = dict(
            media_type="image",
            source="reveal_api",
            resolution_class="compressed",      # cellular delivery downsizes
            station=item.get("cameraName") or "unknown",
            station_source="vendor_label",
            device_id=item.get("cameraId"),
            vendor="tactacam",
            vendor_asset_id=pid,
            capture_time=_capture_time(item),
            capture_time_source="vendor_api",
            capture_time_confidence="medium",
            conditions=conditions,
            trigger_type="motion",
            raw_vendor_payload=item,
        )
        try:
            result = ingest(tmp, sidecar)
        except ImmutabilityError as e:
            # A conflicting sidecar is deterministic -- the same bytes will
            # conflict on every future poll -- so raising here stops the cursor
            # advancing and blocks every later photo behind this one, forever.
            # (Observed 2026-08-21: 198 consecutive failures over ~16h on a
            # single asset.) Reveal re-serves an identical photo under a new
            # photoId when a camera re-transmits, and the transmit-time
            # telemetry in the id and in `conditions` differs, which the
            # landing zone reads as a conflicting capture.
            #
            # Skip this one item and keep going. The skip is logged per
            # occurrence and counted in the summary -- it is real, if small,
            # data loss, and must not be silent. Only ImmutabilityError is
            # swallowed: transient failures still raise, so they keep their
            # retry semantics instead of being skipped past.
            conflicts += 1
            log(f"reveal: SKIP {pid} (conflicting sidecar, not retryable): {e}")
            tmp.unlink(missing_ok=True)
        else:
            if not result.already_present:
                ingested += 1
                new_assets.append(result.asset_id)
        state.set("reveal_cursor_ms", item["createdTimestamp"])
    if readings:
        # Best effort: a battery badge is never worth blocking ingest over.
        try:
            write_station_health(readings)
        except Exception as e:
            log(f"reveal: station health not written: {type(e).__name__}: {e}")
        try:
            append_health_history(readings)
        except Exception as e:
            log(f"reveal: station health history not written: {type(e).__name__}: {e}")
    if new_items:
        log(f"reveal: {len(new_items)} new, {ingested} ingested "
            f"({len(new_items) - ingested - conflicts} already present, "
            f"{conflicts} skipped on conflict)")
    return new_assets
