"""Ingest a bulk Reveal web export into the landing zone.

The gap this closes. The Reveal portal's bulk export writes flat directories of files whose only
metadata is the filename the vendor generated. Three such directories were copied straight into
`landing/assets/` and reviewed in place -- 1,726 captures with no sidecar, no content address,
and therefore no way to join P's verdicts on them to any pipeline output. `hoseid check` had been
reporting them as orphan assets and failing; nothing consumed the failure.

This is deliberately a separate command from `ingest-sd` rather than a flag on it. `ingest-sd`
takes ONE station for a whole directory, which is correct for a card pulled from one camera; an
export directory interleaves every camera on the property, so station has to be resolved per
file. Folding that into ingest-sd would make the common, hand-driven path more dangerous to get
wrong for the sake of a path run rarely.

Filename contract, as the vendor writes it:

    016579006078088-100-3-07252026161246-SYFW00023.jpg
    └── camera serial   │ └── MMDDYYYYHHMMSS      └── vendor sequence handle
                        └── unmodelled vendor counters

Metadata provenance is chosen to match `fetchers/reveal.py` exactly, so a capture ingested from
the export and the same capture arriving later through the API describe themselves identically:

* `capture_time_source = vendor_api`. The timestamp in the filename IS the vendor's
  `photoTimestamp` field -- the same camera-local MMDDYYYYHHMMSS value the API returns, written
  into the name by the vendor's own exporter. Recording it as `file_mtime` would be a lie about
  where it came from and would drop these captures out of encounter grouping, which checks time
  trust; `exif` would be a different lie. Confidence stays `medium` for the same reason the API
  path uses medium: the zone is implicit and assumed to be the property's.
* `resolution_class = compressed`. Reveal delivers over cellular and downsizes; the export serves
  the same delivered bytes, not the card originals.
* `station_source = manual`. The serial-to-station mapping is P's hand-authored registry entry,
  not a label the vendor attached to the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import landing, paths, stations
from .sidecar import (
    CaptureTimeSource, Conditions, MediaType, ResolutionClass, Sidecar, StationSource,
    TriggerType, compute_asset_id,
)

PROPERTY_TZ = ZoneInfo("America/Los_Angeles")

# Anchored on the serial and the timestamp; the two counters between them are vendor-internal and
# deliberately unmodelled. Suffix is matched case-insensitively but the extension is kept as-is.
EXPORT_RE = re.compile(
    r"^(?P<device>\d{15})-\d+-\d+-"
    r"(?P<mm>\d{2})(?P<dd>\d{2})(?P<yyyy>\d{4})(?P<hh>\d{2})(?P<mi>\d{2})(?P<ss>\d{2})-"
    r"(?P<seq>[A-Za-z]+\d+)$"
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass
class ExportIngestReport:
    scanned: int = 0
    ingested: int = 0
    duplicates: int = 0
    unparsed_name: list[str] = field(default_factory=list)
    unknown_device: dict[str, int] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)


def parse_export_name(stem: str) -> tuple[str, datetime, str] | None:
    """(device_id, capture_time, vendor_seq) or None if the name is not the export shape."""
    m = EXPORT_RE.match(stem)
    if not m:
        return None
    g = m.groupdict()
    ct = datetime(int(g["yyyy"]), int(g["mm"]), int(g["dd"]),
                  int(g["hh"]), int(g["mi"]), int(g["ss"]), tzinfo=PROPERTY_TZ)
    return g["device"], ct, g["seq"]


def _dimensions(p: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None, None


def ingest_export(src_dir: Path, *, dry_run: bool = False,
                  stations_path: Path | None = None) -> ExportIngestReport:
    """Ingest every export-shaped file under src_dir, resolving station per file.

    A file whose serial is not in the station registry is REFUSED, not ingested under a guessed
    or placeholder station. Station is what corridor and encounter analysis rest on, and a wrong
    station is worse than an absent capture: the capture can be ingested later once the registry
    names the camera, but a sidecar written with the wrong station is immutable (invariant 1) and
    can only be repaired through the override file.
    """
    rep = ExportIngestReport()
    paths.ensure_layout()
    by_device = stations.by_device(stations_path)

    for p in sorted(src_dir.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rep.scanned += 1

        parsed = parse_export_name(p.stem)
        if parsed is None:
            rep.unparsed_name.append(p.name)
            continue
        device_id, capture_time, seq = parsed

        st = by_device.get(device_id)
        if st is None:
            rep.unknown_device[device_id] = rep.unknown_device.get(device_id, 0) + 1
            continue

        try:
            w, h = _dimensions(p)
            sc = Sidecar(
                asset_id=compute_asset_id(p),
                media_type=MediaType.image,
                source="reveal_export",
                resolution_class=ResolutionClass.compressed,
                station=st.name,
                station_source=StationSource.manual,
                device_id=device_id,
                vendor=st.vendor or "tactacam",
                vendor_asset_id=seq,
                capture_time=capture_time,
                capture_time_source=CaptureTimeSource.vendor_api,
                capture_time_confidence="medium",
                ingested_at=landing.utcnow(),
                width=w, height=h,
                bytes=p.stat().st_size,
                conditions=Conditions(),
                trigger_type=TriggerType.motion,
                raw_vendor_payload={"source_filename": p.name,
                                    "source_relpath": str(p.relative_to(src_dir)),
                                    "export_dir": src_dir.name},
            )
            if dry_run:
                rep.ingested += 1
                continue
            res = landing.store_asset(p, sc)
            if res.already_present:
                rep.duplicates += 1
            else:
                rep.ingested += 1
        except Exception as e:
            rep.errors.append((str(p), f"{type(e).__name__}: {e}"))
    return rep
