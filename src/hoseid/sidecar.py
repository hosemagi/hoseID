"""The asset sidecar contract.

This is the interface that lets the Reveal fetcher, the Arlo fetcher, and the SD-card path be
built independently and later. None of them exist yet; this contract is what makes that safe.

Everything here describes the capture as the *source* reported it. Nothing in this file is
derived, inferred, or corrected -- corrections live in station_overrides.json and are applied at
analysis time, because the landing zone is immutable (invariant 1).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 2

# v1 sidecars are image-only and remain valid: the landing zone is immutable, so anything already
# written can never be migrated in place. Readers must keep understanding older versions forever.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})


class MediaType(str, Enum):
    image = "image"
    video = "video"


class ResolutionClass(str, Enum):
    """Whether these are the full-resolution bytes.

    Cellular delivery downsizes; the SD original for the same capture may arrive weeks later.
    Without this the system cannot tell a compressed cellular frame from an original, and would
    silently run detection on the worse copy when the better one exists.
    """
    compressed = "compressed"
    original = "original"


class CaptureTimeSource(str, Enum):
    """Where capture_time came from.

    Camera clocks drift and reset to epoch on battery swaps. Only vendor_api and exif deserve
    trust; ocr_databar and file_mtime are recorded but should not be used for ordering or for
    encounter grouping without a confidence check.
    """
    vendor_api = "vendor_api"
    exif = "exif"
    ocr_databar = "ocr_databar"
    file_mtime = "file_mtime"


TRUSTED_TIME_SOURCES = frozenset({CaptureTimeSource.vendor_api, CaptureTimeSource.exif})


class StationSource(str, Enum):
    vendor_label = "vendor_label"
    manual = "manual"


class TriggerType(str, Enum):
    motion = "motion"
    timelapse = "timelapse"
    manual = "manual"
    unknown = "unknown"


class Conditions(BaseModel):
    """Vendor-reported environmental and camera-health telemetry.

    All optional: vendors differ in what they report and a missing field must never block ingest.
    battery_pct and signal_bars are camera health, not wildlife data, but they explain gaps in
    coverage later and cost nothing to keep.
    """
    model_config = ConfigDict(extra="allow")

    temperature_f: float | None = None
    moon_phase: str | None = None
    battery_pct: int | None = None
    signal_bars: int | None = None
    ir_flash: bool | None = None


class Sidecar(BaseModel):
    """One capture, as its source described it.

    extra="allow" is deliberate: a vendor adding a field should not fail ingest. Anything
    unmodelled is still preserved in raw_vendor_payload regardless.
    """
    model_config = ConfigDict(extra="allow", use_enum_values=True)

    schema_version: int = SCHEMA_VERSION
    asset_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: MediaType
    source: str
    resolution_class: ResolutionClass

    station: str
    station_source: StationSource
    device_id: str | None = None
    vendor: str
    vendor_asset_id: str | None = None

    capture_time: datetime
    capture_time_source: CaptureTimeSource
    capture_time_confidence: Literal["high", "medium", "low"]
    ingested_at: datetime

    width: int | None = None
    height: int | None = None
    bytes: int

    # --- video only (schema_version >= 2) ---
    # Required when media_type == "video", absent for images. A clip is ONE capture: one asset,
    # one sidecar, one landing-zone row. Sampled frames are internal to analysis and never
    # become captures -- that would explode the capture count and make clip identity implicit.
    duration_s: float | None = None
    fps: float | None = None
    frame_count: int | None = None

    conditions: Conditions = Field(default_factory=Conditions)
    trigger_type: TriggerType = TriggerType.unknown
    burst_index: int | None = None
    burst_total: int | None = None

    # Kept verbatim. There will be a field nobody thought to extract, and re-fetching is
    # impossible once the vendor ages the image out of its cloud.
    raw_vendor_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capture_time", "ingested_at")
    @classmethod
    def _tz_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v.astimezone(timezone.utc)

    @field_validator("station")
    @classmethod
    def _station_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("station must not be empty")
        return v

    @model_validator(mode="after")
    def _video_fields_required_for_video(self) -> "Sidecar":
        """Video captures must carry duration/fps/frame_count; images must not.

        Enforced rather than merely documented because the analysis stage needs duration to
        compute its sampling cadence, and a clip that silently reaches stage 1 without it would
        be sampled at the wrong density with no error.
        """
        is_video = MediaType(self.media_type) is MediaType.video
        missing = [f for f in ("duration_s", "fps", "frame_count") if getattr(self, f) is None]
        if is_video and missing:
            raise ValueError(f"media_type=video requires {missing}")
        if not is_video and len(missing) < 3:
            present = [f for f in ("duration_s", "fps", "frame_count") if getattr(self, f) is not None]
            raise ValueError(f"media_type={self.media_type} must not carry video fields {present}")
        if is_video and self.duration_s is not None and self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        return self

    @property
    def is_video(self) -> bool:
        return MediaType(self.media_type) is MediaType.video

    @property
    def digest(self) -> str:
        return self.asset_id.split(":", 1)[1]

    @property
    def time_is_trustworthy(self) -> bool:
        return CaptureTimeSource(self.capture_time_source) in TRUSTED_TIME_SOURCES

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=1, sort_keys=True)


class SidecarError(ValueError):
    """Raised when a sidecar does not conform to the contract."""


def validate_sidecar(data: dict[str, Any] | str | Path) -> Sidecar:
    """Validate a sidecar dict, JSON string, or path. Raises SidecarError on any violation."""
    if isinstance(data, Path):
        try:
            data = json.loads(data.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SidecarError(f"unreadable sidecar: {e}") from e
    elif isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise SidecarError(f"invalid JSON: {e}") from e
    try:
        sc = Sidecar.model_validate(data)
    except Exception as e:
        raise SidecarError(str(e)) from e
    if sc.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SidecarError(
            f"schema_version {sc.schema_version} not in supported "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    if sc.schema_version < 2 and sc.is_video:
        raise SidecarError("schema_version 1 has no video support; video requires >= 2")
    return sc


def compute_asset_id(path: Path, *, chunk: int = 1 << 20) -> str:
    """sha256 of file content, as `sha256:<hex>`.

    Content addressing is the multi-writer safety property: two writers producing the same bytes
    produce the same asset_id and the same destination path, so concurrent ingest is idempotent
    rather than conflicting.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return f"sha256:{h.hexdigest()}"
