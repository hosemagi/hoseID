#!/usr/bin/env python
"""Spike: authenticate to the Reveal API, list cameras, pull one recent photo.

Proves: Cognito USER_PASSWORD_AUTH, which token the API wants, /cameras and
/photos response shapes, pagination fields, and an S3 download. Read-only
against P's account; downloads land in the scratch dir, not the archive.

Run: .venv-fetch/bin/python scripts/spike_reveal.py
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import requests

CREDS = Path.home() / ".config/hoseid-fetch/credentials.toml"
COGNITO = "https://cognito-idp.us-east-1.amazonaws.com/"
CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
API = "https://api.reveal.ishareit.net/v1"
UA = "RevealWeb/5.4.0"
OUT = Path("/private/tmp/hoseid-spike")


def cognito_login(email: str, password: str) -> dict:
    r = requests.post(
        COGNITO,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["AuthenticationResult"]


def main() -> int:
    cfg = tomllib.loads(CREDS.read_text())["reveal"]
    if cfg["email"] == "FILL_ME":
        print("credentials.toml [reveal] not filled in"); return 1

    print("== Cognito login")
    auth = cognito_login(cfg["email"], cfg["password"])
    print(f"   token_type={auth.get('TokenType')} expires_in={auth.get('ExpiresIn')}s "
          f"has_refresh={'RefreshToken' in auth}")

    s = requests.Session()
    s.headers["User-Agent"] = UA

    # Learn which token the API accepts.
    for token_name in ("IdToken", "AccessToken"):
        s.headers["Authorization"] = f"Bearer {auth[token_name]}"
        r = s.get(f"{API}/cameras", timeout=30)
        print(f"== GET /cameras with {token_name}: HTTP {r.status_code}")
        if r.ok:
            break
    else:
        print(r.text[:500]); return 1

    cams = r.json()
    body = cams if isinstance(cams, list) else cams.get("cameras", cams)
    print(json.dumps(body, indent=1)[:1500])

    print("\n== GET /photos?size=3&page=0")
    r = s.get(f"{API}/photos", params={"size": 3, "page": 0,
                                       "includeWeatherData": "true"}, timeout=30)
    print(f"   HTTP {r.status_code}")
    r.raise_for_status()
    photos = r.json()
    print(json.dumps(photos, indent=1)[:2500])

    items = photos if isinstance(photos, list) else None
    if items is None:
        body = photos.get("response", photos)   # envelope: {message, response:{photos}}
        for key in ("photos", "content", "items", "data"):
            if key in body:
                items = body[key]; break
    if not items:
        print("could not locate photo list in response"); return 1

    first = items[0]
    url = next((first[k] for k in ("url", "photoUrl", "imageUrl", "hdUrl", "s3Url")
                if k in first and first[k]), None)
    if not url:
        print("no download URL field found; keys:", list(first.keys())); return 1

    OUT.mkdir(exist_ok=True)
    dest = OUT / "reveal_latest.jpg"
    img = requests.get(url, timeout=60)
    img.raise_for_status()
    dest.write_bytes(img.content)
    print(f"\n== downloaded {len(img.content)} bytes -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
