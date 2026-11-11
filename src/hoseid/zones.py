"""Exclusion zones: the one rule for whether a detection is inside one.

Zones are rows in tags.db: exclusion_zones(device_id, epoch, category, x, y, w, h)
and camera_epochs(device_id, epoch). Only a device's rows at its current epoch
(default 1) are active; "camera moved" bumps the epoch and retires the old set.
A detection is excluded when the centre of its [x, y, w, h] bbox is inside a
zone box (edges inclusive) of the same device and the same category code.

The alert fetcher (fetchers/alerts.py) and the review UI (app.py) both call
this module, so an alert never fires for a detection the review UI hides.
hoseserv's Security view keeps its own copy (app/api/v1/endpoints/security.py,
it can't import hoseID); sagecode's contracts.toml pairs the two as
'exclusion-zones'. Change both together.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import paths

# MegaDetector class -> exclusion_zones.category code.
CAT_CODE = {"animal": "1", "person": "2", "vehicle": "3"}


class _AnyDevice:
    def __repr__(self) -> str:
        return "ANY_DEVICE"


# Pass as in_zone's device_id when the zones are already one device's set (the
# review UI groups them by device first). None is NOT "any device": it is an
# unknown device (no sidecar), which matches no zone.
ANY_DEVICE = _AnyDevice()


def category(detector_class: str | None) -> str | None:
    """Category code for a detector class; a value that is already a code (or
    unknown) passes through unchanged."""
    return CAT_CODE.get(detector_class, detector_class)


def active_zones(conn: sqlite3.Connection, device_id: str | None = None) -> list[dict]:
    """Zones at each device's current epoch (or only `device_id`'s)."""
    def _q(sql, args=()):
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row   # whatever the caller's conn uses
        return cur.execute(sql, args)

    epochs = {r["device_id"]: r["e"] for r in _q(
        "SELECT device_id, MAX(epoch) e FROM camera_epochs GROUP BY device_id")}
    rows = _q(
        "SELECT * FROM exclusion_zones" + (" WHERE device_id=?" if device_id else ""),
        (device_id,) if device_id else ())
    return [dict(r) for r in rows if r["epoch"] == epochs.get(r["device_id"], 1)]


def in_zone(bbox, detector_class: str | None, device_id, zones: list[dict]) -> bool:
    """True when the centre of `bbox` ([x, y, w, h], normalised) lies inside an
    active zone (inclusive) of the same device and category. `device_id` may be
    ANY_DEVICE to skip the device check."""
    x, y, w, h = bbox
    cx, cy = x + w / 2, y + h / 2
    cat = category(detector_class)
    return any((device_id is ANY_DEVICE or z["device_id"] == device_id)
               and z["category"] == cat
               and z["x"] <= cx <= z["x"] + z["w"]
               and z["y"] <= cy <= z["y"] + z["h"] for z in zones)


def device_for(digest: str, sidecars_dir: Path | None = None) -> str | None:
    """device_id from a capture's sidecar; None when it is missing or corrupt
    (a bad sidecar means "no device", never an exception)."""
    root = Path(sidecars_dir) if sidecars_dir is not None else paths.sidecars_dir()
    p = root / digest[:2] / digest[2:4] / f"{digest}.json"
    try:
        sidecar = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return sidecar.get("device_id") if isinstance(sidecar, dict) else None
