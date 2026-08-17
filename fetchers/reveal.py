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

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .common import TMP_DIR, State, download, ingest, log

COGNITO = "https://cognito-idp.us-east-1.amazonaws.com/"
CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
API = "https://api.reveal.ishareit.net/v1"
UA = "RevealWeb/5.4.0"
PROPERTY_TZ = ZoneInfo("America/Los_Angeles")
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
    new_assets: list[str] = []
    for item in sorted(new_items, key=lambda p: p["createdTimestamp"]):
        pid = item["photoId"]
        tmp = download(item["photoUrl"], TMP_DIR / pid)
        result = ingest(tmp, dict(
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
            conditions={
                "battery_pct": int(item["metadata"]["batteryLevel"])
                if item.get("metadata", {}).get("batteryLevel") else None,
                "signal_bars": int(item["metadata"]["signal"])
                if item.get("metadata", {}).get("signal") else None,
            },
            trigger_type="motion",
            raw_vendor_payload=item,
        ))
        if not result.already_present:
            ingested += 1
            new_assets.append(result.asset_id)
        state.set("reveal_cursor_ms", item["createdTimestamp"])
    if new_items:
        log(f"reveal: {len(new_items)} new, {ingested} ingested "
            f"({len(new_items) - ingested} already present)")
    return new_assets
