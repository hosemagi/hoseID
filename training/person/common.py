"""Shared paths for the person-identification survey.

Phase 0 only: are the camera's person detections good enough to tell
people apart? There are no identity labels anywhere yet, so this cuts
crops from the detector's person boxes, groups them, and leaves naming to
P. Everything lives under derived/training/person (regenerable; never the
archive, the pipeline's crops tree or detections.db).

Output stays on this machine: the crops are images of real people,
including visitors, so nothing here is published or sent anywhere.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HOME = Path.home()
DETECTIONS_DB = HOME / "trailcam/derived/detections.db"
ASSETS = HOME / "trailcam/landing/assets"
OUT = HOME / "trailcam/derived/training/person"
CROPS = OUT / "crops"

MIN_CONF = 0.5
PAD_FRAC = 0.15  # same padding as stages/detect.py
VISIT_GAP_MIN = 30


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def asset_file(asset_id: str) -> Path | None:
    """Content-addressed original (any extension) for an asset id."""
    digest = asset_id.split(":", 1)[1] if ":" in asset_id else asset_id
    hits = sorted((ASSETS / digest[:2] / digest[2:4]).glob(f"{digest}.*"))
    return hits[0] if hits else None


def load_rows() -> list[dict]:
    return [json.loads(line) for line in (OUT / "rows.jsonl").read_text().splitlines() if line.strip()]
