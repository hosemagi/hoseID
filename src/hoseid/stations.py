"""Station registry and station corrections.

`station` in a sidecar is whatever the vendor label said at capture time. P renames cameras when
he moves them, so the vendor label *is* the station -- but a rename can lag a physical move by
days, during which frames carry the wrong station.

`device_id` exists purely as the repair handle for that: "every frame from serial 0419 between
Aug 15 and Aug 19" is the query that selects the bad set. Corrections are applied here, at
analysis time. Sidecars are never rewritten (invariant 1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class Station:
    name: str
    lat: float | None = None
    lon: float | None = None
    active_from: str | None = None
    active_to: str | None = None
    notes: str | None = None
    # The camera serial currently at this station. Optional because the registry predates it and
    # the Arlo cameras are keyed through station_overrides instead. It is what lets a bulk vendor
    # export -- whose only station signal is the serial in each filename -- be ingested under the
    # right station without a human labelling every file.
    device_id: str | None = None
    vendor: str | None = None


@dataclass(frozen=True)
class StationOverride:
    """Reassign captures from one device over a date window to the correct station."""
    device_id: str
    start: datetime
    end: datetime
    station: str
    reason: str | None = None

    def matches(self, device_id: str | None, capture_time: datetime) -> bool:
        return (device_id is not None
                and device_id == self.device_id
                and self.start <= capture_time <= self.end)


def load_stations(path: Path | None = None) -> dict[str, Station]:
    p = path or paths.stations_file()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    out: dict[str, Station] = {}
    for entry in raw.get("stations", []):
        out[entry["name"]] = Station(
            name=entry["name"], lat=entry.get("lat"), lon=entry.get("lon"),
            active_from=entry.get("active_from"), active_to=entry.get("active_to"),
            notes=entry.get("notes"), device_id=entry.get("device_id"),
            vendor=entry.get("vendor"),
        )
    return out


def by_device(path: Path | None = None) -> dict[str, Station]:
    """Serial -> station, for sources whose only station signal is the camera serial.

    Raises on a duplicate serial rather than picking one: two stations claiming the same camera
    is a registry error that would otherwise silently mis-attribute every capture from it, and
    station attribution is the thing corridor analysis rests on.
    """
    out: dict[str, Station] = {}
    for st in load_stations(path).values():
        if not st.device_id:
            continue
        if st.device_id in out:
            raise ValueError(
                f"device_id {st.device_id} claimed by both '{out[st.device_id].name}' and "
                f"'{st.name}' in the station registry; resolve before ingesting")
        out[st.device_id] = st
    return out


def load_overrides(path: Path | None = None) -> list[StationOverride]:
    p = path or paths.station_overrides_file()
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    out: list[StationOverride] = []
    for e in raw.get("overrides", []):
        out.append(StationOverride(
            device_id=e["device_id"],
            start=datetime.fromisoformat(e["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(e["end"].replace("Z", "+00:00")),
            station=e["station"],
            reason=e.get("reason"),
        ))
    return out


def resolve_station(sidecar_station: str, device_id: str | None, capture_time: datetime,
                    overrides: list[StationOverride]) -> tuple[str, bool]:
    """Return (effective_station, was_corrected).

    First matching override wins; order in the file is therefore meaningful and the file is
    hand-edited, so keep it small and readable.
    """
    for ov in overrides:
        if ov.matches(device_id, capture_time):
            return ov.station, True
    return sidecar_station, False


STATIONS_TEMPLATE = {
    "_comment": "Station registry. Analysis reads this; ingest does not. "
                "Coordinates are approximate and only used for map/corridor work downstream.",
    "stations": [
        {"name": "Crossroads", "lat": None, "lon": None,
         "active_from": None, "active_to": None, "notes": "example -- replace"},
    ],
}

OVERRIDES_TEMPLATE = {
    "_comment": "Hand-edited station corrections, applied at analysis time. Use when a camera "
                "rename lagged a physical move: pick the device_id and the date window during "
                "which its frames carried the wrong station label. Sidecars are never rewritten.",
    "overrides": [
        {"device_id": "TC-REVEALX3-0419", "start": "2026-08-15T00:00:00Z",
         "end": "2026-08-19T23:59:59Z", "station": "Saddle",
         "reason": "example -- moved 8/15, renamed in app 8/20"},
    ],
}


def write_templates() -> list[Path]:
    written = []
    for path, tmpl in ((paths.stations_file(), STATIONS_TEMPLATE),
                       (paths.station_overrides_file(), OVERRIDES_TEMPLATE)):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(tmpl, indent=2) + "\n")
            written.append(path)
    return written
