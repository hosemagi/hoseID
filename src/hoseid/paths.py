"""Filesystem layout.

Three layers, deliberately separate directories so each gets its own backup policy:

    landing/   append-only, immutable. The only irreplaceable *machine* data.
    derived/   fully regenerable. Delete and rebuild from landing at any time.
    tags/      append-only human labels. The only irreplaceable data in the system.

Root is overridable with HOSEID_ROOT, mainly so tests can point at a tmpdir.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / "trailcam"


def root() -> Path:
    return Path(os.environ.get("HOSEID_ROOT", DEFAULT_ROOT)).expanduser()


# --- landing zone (immutable) ------------------------------------------------
def landing_dir() -> Path:
    return root() / "landing"


def assets_dir() -> Path:
    return landing_dir() / "assets"


def sidecars_dir() -> Path:
    return landing_dir() / "sidecars"


def stations_file() -> Path:
    """Station registry: name, coordinates, rough active dates. Analysis reads it; ingest does not."""
    return landing_dir() / "stations.json"


def station_overrides_file() -> Path:
    """Hand-edited corrections (device + date range -> correct station).

    Applied at analysis time. Sidecars are never rewritten -- this file is how a lagged
    camera rename gets repaired without violating landing-zone immutability.
    """
    return landing_dir() / "station_overrides.json"


# --- derived layer (regenerable) ---------------------------------------------
def derived_dir() -> Path:
    return root() / "derived"


def detections_db() -> Path:
    return derived_dir() / "detections.db"


def crops_dir() -> Path:
    """Crops are a deliverable, not an intermediate: they are the review UX."""
    return derived_dir() / "crops"


def runs_dir() -> Path:
    return derived_dir() / "runs"


# --- tag store (separate; the pipeline never writes here) --------------------
def tags_dir() -> Path:
    return root() / "tags"


def tags_db() -> Path:
    return tags_dir() / "tags.db"


# --- models (cache; regenerable by re-download) ------------------------------
def models_dir() -> Path:
    """Stable home for detector weights.

    MegaDetector downloads to `tempfile.gettempdir()` with no override, which on macOS is a
    periodically-purged /var/folders path. We stage weights here and load by explicit path so a
    temp purge cannot silently trigger a 281 MB re-download mid-batch.
    """
    return root() / "models"


def _shard(digest: str) -> tuple[str, str]:
    """Two-level sharding so no directory accumulates hundreds of thousands of entries."""
    return digest[:2], digest[2:4]


def asset_path(asset_id: str, suffix: str) -> Path:
    """Content-addressed location for an asset.

    Content addressing (rather than date-based paths) is what makes the landing zone safe for
    two concurrent writers -- cabin-side SD staging and cards carried home can both write without
    coordination, because identical bytes produce an identical path and differing bytes cannot
    collide. It also dedupes re-ingested cards for free.

    Deliberately NOT date-based: capture_time comes from camera clocks that drift and reset on
    battery swaps, so it is not trustworthy enough to be structural.
    """
    digest = _digest_of(asset_id)
    a, b = _shard(digest)
    return assets_dir() / a / b / f"{digest}{suffix}"


def sidecar_path(asset_id: str) -> Path:
    digest = _digest_of(asset_id)
    a, b = _shard(digest)
    return sidecars_dir() / a / b / f"{digest}.json"


def _digest_of(asset_id: str) -> str:
    return asset_id.split(":", 1)[1] if ":" in asset_id else asset_id


def ensure_layout() -> None:
    for d in (assets_dir(), sidecars_dir(), derived_dir(), crops_dir(), runs_dir(),
              tags_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)
