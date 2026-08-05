"""SD-card ingest.

The card is the highest-quality source: originals rather than cellular downsizes, and EXIF
timestamps rather than vendor API metadata. It is also the path a human drives by hand, so it
must be hard to get wrong.

Ingest is deliberately cheap and must not fail (invariant 6). It does no analysis, reads no
station registry, and writes nothing derived.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import landing, paths, video
from .sidecar import (
    CaptureTimeSource, Conditions, MediaType, ResolutionClass, Sidecar, StationSource,
    TriggerType, compute_asset_id,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = video.VIDEO_SUFFIXES


@dataclass
class IngestReport:
    scanned: int = 0
    ingested: int = 0
    duplicates: int = 0
    # Clips ingested but unreadable by ffprobe. Counted separately because they are neither a
    # success nor an ingest error: the asset landed, but analysis cannot look at it.
    probe_failed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _exif_capture_time(p: Path) -> tuple[datetime | None, CaptureTimeSource]:
    """Read EXIF DateTimeOriginal without a hard Pillow dependency.

    Pillow is not in this package's dependency set (it must stay light enough to install into
    both ML venvs), so this degrades to file mtime when Pillow is unavailable -- recorded
    honestly via capture_time_source rather than silently.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None, CaptureTimeSource.file_mtime
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            for tag in (36867, 306):          # DateTimeOriginal, DateTime
                raw = exif.get(tag)
                if raw:
                    dt = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    # Camera clocks are local-time and usually unlabelled. We store UTC, so an
                    # unlabelled local timestamp is recorded as-is and flagged medium confidence.
                    return dt.replace(tzinfo=timezone.utc), CaptureTimeSource.exif
    except Exception:
        pass
    return None, CaptureTimeSource.file_mtime


def _dimensions(p: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None, None


_BURST_RE = re.compile(r"[_-](\d{1,2})of(\d{1,2})", re.I)


def _burst(p: Path) -> tuple[int | None, int | None]:
    m = _BURST_RE.search(p.stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def ingest_directory(src_dir: Path, station: str, *, vendor: str = "unknown",
                     device_id: str | None = None, dry_run: bool = False,
                     recursive: bool = True) -> IngestReport:
    """Ingest a directory of card files into the landing zone under one station name."""
    rep = IngestReport()
    paths.ensure_layout()
    it = src_dir.rglob("*") if recursive else src_dir.glob("*")

    for p in sorted(it):
        if not p.is_file() or p.name.startswith("."):
            continue
        suffix = p.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            media = MediaType.image
        elif suffix in VIDEO_SUFFIXES:
            media = MediaType.video
        else:
            continue
        rep.scanned += 1

        try:
            asset_id = compute_asset_id(p)
            ct, ct_src = _exif_capture_time(p) if media is MediaType.image else (None, CaptureTimeSource.file_mtime)
            if ct is None:
                ct = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                ct_src = CaptureTimeSource.file_mtime
            # file_mtime is never trustworthy for a trail cam: copying a card can rewrite it.
            confidence = "high" if ct_src is CaptureTimeSource.exif else "low"
            w, h = _dimensions(p) if media is MediaType.image else (None, None)
            bi, bt = _burst(p)

            vmeta = None
            probe_status, probe_error = "ok", None
            if media is MediaType.video:
                # Probe rather than guess: stage 1 needs duration to compute sampling cadence.
                # A probe failure does NOT discard the asset -- ingest must not fail (invariant 6)
                # and the bytes are the irreplaceable part. It is recorded on the sidecar so
                # analysis routes the clip to decode_failed instead of silently sampling nothing.
                vmeta, probe_error = video.probe_safe(p)
                if vmeta is None:
                    probe_status = "failed"
                    rep.probe_failed += 1
                else:
                    w, h = vmeta.width, vmeta.height

            sc = Sidecar(
                asset_id=asset_id,
                media_type=media,
                source="sd_card",
                # The card holds what the camera actually wrote -- by definition the original.
                resolution_class=ResolutionClass.original,
                station=station,
                station_source=StationSource.manual,
                device_id=device_id,
                vendor=vendor,
                vendor_asset_id=None,
                capture_time=ct,
                capture_time_source=ct_src,
                capture_time_confidence=confidence,
                ingested_at=landing.utcnow(),
                width=w, height=h,
                bytes=p.stat().st_size,
                conditions=Conditions(),
                trigger_type=TriggerType.unknown,
                burst_index=bi, burst_total=bt,
                duration_s=vmeta.duration_s if vmeta else None,
                fps=vmeta.fps if vmeta else None,
                frame_count=vmeta.frame_count if vmeta else None,
                probe_status=probe_status,
                probe_error=probe_error,
                raw_vendor_payload={"source_filename": p.name,
                                    "source_relpath": str(p.relative_to(src_dir))},
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
