"""Follow Arlo camera renames through the stack, matched by device id.

The fetch daemon calls `reconcile` with Arlo's current (device_id, name) list
on every connect and sweep. A device seen before under a different name is a
rename: "Cabin - Madrone" -> "Cabin - Gully" moves station Madrone to Gully
everywhere a station name is stored, so P renames a camera once, in the Arlo
app, and nothing else needs touching:

  config/station_renames.json   old raw label still maps to the new station
                                (captures already on disk keep the old label);
                                a date split for the old label keeps its
                                `before` and moves its `after`
  station_overrides.json        the device's override windows naming it
  stations.json                 the registry entry (position, view, notes)
  wildlife.db                   sightings.station, the stations row (+ alias)
  detections.db                 captures.station
  hosemap layers                trail-cam membership (hoseserv API, best effort)

A rename onto a station name another camera already uses is refused and
reported, never merged. The first sighting of a device just records its name.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, station_renames

WILDLIFE_DB = Path.home() / "trailcam/tags/wildlife.db"


@dataclass
class Paths:
    renames: Path = station_renames.RULES_FILE
    overrides: Path = field(default_factory=paths.station_overrides_file)
    stations: Path = field(default_factory=paths.stations_file)
    wildlife_db: Path = WILDLIFE_DB
    detections_db: Path = field(default_factory=paths.detections_db)


@dataclass
class Rename:
    device_id: str
    old_label: str
    new_label: str
    old_station: str = ""
    new_station: str = ""
    applied: bool = False
    skipped: str = ""            # why nothing was changed
    sightings: int = 0
    captures: int = 0

    def summary(self) -> str:
        if self.skipped:
            return (f"Arlo camera {self.old_label!r} renamed to {self.new_label!r}; "
                    f"station not renamed: {self.skipped}")
        return (f"Camera renamed in Arlo: {self.old_station} → {self.new_station} "
                f"({self.sightings} sightings, {self.captures} captures relabelled)")


def _write_json(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _strip(label: str, rules: dict) -> str:
    prefix = rules.get("strip_prefix", "")
    return label[len(prefix):] if prefix and label.startswith(prefix) else label


def known_stations(p: Paths) -> set[str]:
    names = {s.get("name") for s in _read_json(p.stations, {}).get("stations", [])}
    if p.wildlife_db.exists():
        con = sqlite3.connect(p.wildlife_db, timeout=30)
        try:
            names |= {r[0] for r in con.execute("SELECT name FROM stations")}
        finally:
            con.close()
    return {n for n in names if n}


def apply_rename(r: Rename, p: Paths | None = None) -> Rename:
    """Move station old -> new everywhere (module docstring). Idempotent."""
    p = p or Paths()
    rules = station_renames.load_rules(p.renames)
    r.old_station = station_renames.normalize_station(r.old_label, None, rules)
    r.new_station = station_renames.normalize_station(r.new_label, None, rules)
    back = _strip(r.new_label, rules)
    if r.new_station == r.old_station and rules.get("renames", {}).get(back) == r.old_station:
        # renamed back to a label an earlier rename pointed at this station:
        # drop that mapping, the camera's Arlo name is the station again
        del rules["renames"][back]
        r.new_station = back
    old, new = r.old_station, r.new_station
    if old == new:
        r.skipped = "the station name is unchanged"
        return r
    if new in known_stations(p) - {old}:
        r.skipped = f"station {new!r} already exists (another camera); rename one of them in Arlo"
        return r

    # 1. rename rules: the old raw label (on captures already stored) -> new
    special = [s for s in rules.get("special", []) if s.get("name") == r.old_label]
    for s in special:
        if s.get("after") == old:
            s["after"] = new
    if not special:
        rules.setdefault("renames", {})[_strip(r.old_label, rules)] = new
    for k, v in list(rules.get("renames", {}).items()):
        if v == old:
            rules["renames"][k] = new
    _write_json(p.renames, rules)
    station_renames._default_rules.cache_clear()

    # 2. overrides for this device that name the old station
    ov = _read_json(p.overrides, {"overrides": []})
    changed = False
    for o in ov.get("overrides", []):
        if o.get("device_id") == r.device_id and o.get("station") == old:
            o["station"], changed = new, True
    if changed:
        _write_json(p.overrides, ov)

    # 3. station registry (keeps position and view)
    reg = _read_json(p.stations, {"stations": []})
    for s in reg.get("stations", []):
        if s.get("name") == old:
            s["name"] = new
            _write_json(p.stations, reg)
            break

    # 4. wildlife log
    if p.wildlife_db.exists():
        con = sqlite3.connect(p.wildlife_db, timeout=30)
        try:
            with con:
                r.sightings = con.execute(
                    "UPDATE sightings SET station=? WHERE station=?", (new, old)).rowcount
                row = con.execute("SELECT aliases FROM stations WHERE name=?", (old,)).fetchone()
                if row is not None:
                    aliases = json.loads(row[0] or "[]")
                    if r.old_label not in aliases:
                        aliases.append(r.old_label)
                    con.execute("UPDATE stations SET name=?, aliases=? WHERE name=?",
                                (new, json.dumps(aliases), old))
        finally:
            con.close()

    # 5. detections (derived, but read by the review UI and hoseserv)
    if p.detections_db.exists():
        con = sqlite3.connect(p.detections_db, timeout=30)
        try:
            with con:
                r.captures = con.execute(
                    "UPDATE captures SET station=? WHERE station=?", (new, old)).rowcount
        finally:
            con.close()
    r.applied = True
    return r


def reconcile(cameras: Iterable[tuple[str, str]], seen: dict[str, str],
              p: Paths | None = None) -> list[Rename]:
    """Compare Arlo's (device_id, name) list with `seen` (device_id -> last
    name, updated in place) and apply each rename. Returns the renames."""
    out = []
    for device_id, name in cameras:
        if not device_id or not name:
            continue
        before = seen.get(device_id)
        seen[device_id] = name
        if before is None or before == name:
            continue
        out.append(apply_rename(Rename(device_id, before, name), p))
    return out


def rename_in_map_layers(base_url: str, api_key: str, old: str, new: str,
                         request: Callable | None = None) -> int:
    """Swap the trail-cam station in every hosemap layer that lists it.
    Best effort: returns layers changed, 0 on any failure."""
    if request is None:
        import requests
        request = requests.request
    headers = {"X-Api-Key": api_key}
    api = f"{base_url.rstrip('/')}/api/v1/hosemap"
    try:
        resp = request("GET", f"{api}/layers", headers=headers, timeout=15, verify=False)
        resp.raise_for_status()
        layers = resp.json()
        layers = layers.get("layers", layers) if isinstance(layers, dict) else layers
        n = 0
        for layer in layers:
            if old in (layer.get("trailcam_ids") or []):
                for method, station in (("POST", new), ("DELETE", old)):
                    request(method, f"{api}/layers/{layer['id']}/trailcams/{station}",
                            headers=headers, timeout=15, verify=False).raise_for_status()
                n += 1
        return n
    except Exception:  # noqa: BLE001 — the map is a nicety; the log says so
        return 0
