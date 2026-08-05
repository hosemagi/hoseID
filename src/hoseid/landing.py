"""Writing to and checking the landing zone.

Invariant 1: append-only and immutable. Assets and sidecars are never modified after write.
Everything else in the system is derived from them.

That invariant is enforced here rather than documented: `store_asset` refuses to overwrite
differing content, and all writes go through an atomic temp+rename so a crashed or concurrent
writer can never leave a torn file visible.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .sidecar import Sidecar, SidecarError, compute_asset_id, validate_sidecar


class ImmutabilityError(RuntimeError):
    """Raised on an attempt to modify something the landing zone considers already written."""


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)          # atomic within a filesystem
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class StoreResult:
    asset_id: str
    asset_path: Path
    sidecar_path: Path
    already_present: bool


def store_asset(src: Path, sidecar: Sidecar, *, allow_existing: bool = True) -> StoreResult:
    """Copy an asset and write its sidecar, atomically and idempotently.

    Re-ingesting the same bytes is a no-op rather than an error -- carrying the same SD card home
    twice, or a cabin-side sync overlapping a manual copy, must not fail. Because the path is
    derived from the content hash, "already present" provably means "identical bytes".
    """
    if sidecar.asset_id != compute_asset_id(src):
        raise SidecarError("sidecar.asset_id does not match the content of src")

    ap = paths.asset_path(sidecar.asset_id, src.suffix.lower())
    sp = paths.sidecar_path(sidecar.asset_id)
    existed = ap.exists()

    if existed and not allow_existing:
        raise ImmutabilityError(f"asset already present: {sidecar.asset_id}")

    if not existed:
        _atomic_copy(src, ap)

    if sp.exists():
        # Sidecars are immutable, but not every difference is a conflict.
        #
        # Re-ingesting the same card -- or a cabin-side sync overlapping a manual copy -- must be
        # a no-op. Those runs legitimately differ in ingest provenance (`ingested_at`, and for SD
        # ingest the mount path recorded in raw_vendor_payload) while describing the identical
        # capture. Treating that as a conflict would make the common case fail.
        #
        # A difference in anything that describes the *capture* -- station, capture_time,
        # resolution_class, device -- is a genuine conflict. Resolving it by overwrite would
        # destroy the earlier record, so refuse and let a human decide.
        prior = _normalise(json.loads(sp.read_text()))
        incoming = _normalise(json.loads(sidecar.to_json()))
        differing = _semantic_diff(prior, incoming)
        if differing:
            raise ImmutabilityError(
                f"sidecar exists with conflicting content for {sidecar.asset_id}; "
                f"differing fields: {sorted(differing)}. "
                "Landing zone is immutable -- resolve manually."
            )
        # Semantically identical: keep the original sidecar untouched.
    else:
        _atomic_write_bytes(sp, sidecar.to_json().encode())

    return StoreResult(sidecar.asset_id, ap, sp, existed)


# Fields that describe *this ingest run* rather than the capture, and so may legitimately differ
# between two ingests of identical bytes.
#
# schema_version is here because the stored sidecar is authoritative and is never rewritten: a
# version difference records *when* it was written, not a disagreement about the capture. Without
# this, re-ingesting a card that landed under an older schema would conflict on every file.
# probe_error is diagnostic text from one ffprobe invocation and is not even deterministic --
# it embeds a memory address. probe_STATUS is deliberately NOT exempt: if a clip that was
# unreadable becomes readable (say, after an ffmpeg upgrade), that is a real change about the
# capture and a human should decide what to do with the earlier record.
_PROVENANCE_FIELDS = frozenset({"ingested_at", "raw_vendor_payload", "schema_version",
                                "probe_error"})


def _normalise(raw: dict) -> dict:
    """Fill defaults so a sidecar written before a field existed compares equal to one written now.

    Adding an optional field with a default is a backward-compatible schema change, but a raw
    dict comparison sees the old sidecar as missing the key and flags a conflict on every file.
    Validating both sides first means the comparison is over semantic content, not JSON shape.
    """
    try:
        return json.loads(validate_sidecar(raw).to_json())
    except SidecarError:
        # Unparseable stored sidecar: compare raw rather than crash, and let the diff report it.
        return raw


def _semantic_diff(prior: dict, incoming: dict) -> set[str]:
    """Fields that differ and actually describe the capture."""
    keys = (set(prior) | set(incoming)) - _PROVENANCE_FIELDS
    return {k for k in keys if prior.get(k) != incoming.get(k)}


def iter_sidecars() -> "list[Path]":
    d = paths.sidecars_dir()
    if not d.exists():
        return []
    return sorted(d.rglob("*.json"))


def find_asset(asset_id: str) -> Path | None:
    digest = asset_id.split(":", 1)[-1]
    a, b = digest[:2], digest[2:4]
    parent = paths.assets_dir() / a / b
    if not parent.exists():
        return None
    for p in parent.glob(f"{digest}.*"):
        return p
    return None


@dataclass
class CheckReport:
    n_sidecars: int = 0
    n_assets_present: int = 0
    invalid: list[tuple[str, str]] = None          # (path, error)
    missing_asset: list[str] = None                # asset_id
    orphan_assets: list[str] = None                # asset files with no sidecar
    digest_mismatch: list[str] = None              # content no longer matches asset_id
    untrusted_time: list[str] = None               # informational, not a failure

    def __post_init__(self):
        for f in ("invalid", "missing_asset", "orphan_assets", "digest_mismatch", "untrusted_time"):
            if getattr(self, f) is None:
                setattr(self, f, [])

    @property
    def ok(self) -> bool:
        return not (self.invalid or self.missing_asset or self.orphan_assets or self.digest_mismatch)


def check_landing_zone(*, verify_digests: bool = False) -> CheckReport:
    """Validate the whole landing zone.

    verify_digests re-hashes every asset. Slow, and only worth it when you suspect bit-rot or a
    bad copy -- content addressing means a mismatch indicates corruption, not a stale record.
    """
    rep = CheckReport()
    seen: set[str] = set()

    for sp in iter_sidecars():
        rep.n_sidecars += 1
        try:
            sc = validate_sidecar(sp)
        except SidecarError as e:
            rep.invalid.append((str(sp), str(e)))
            continue
        seen.add(sc.digest)
        ap = find_asset(sc.asset_id)
        if ap is None:
            rep.missing_asset.append(sc.asset_id)
            continue
        rep.n_assets_present += 1
        if not sc.time_is_trustworthy:
            rep.untrusted_time.append(sc.asset_id)
        if verify_digests and compute_asset_id(ap) != sc.asset_id:
            rep.digest_mismatch.append(sc.asset_id)

    ad = paths.assets_dir()
    if ad.exists():
        for p in ad.rglob("*"):
            if p.is_file() and not p.name.startswith(".tmp-"):
                if p.stem not in seen:
                    rep.orphan_assets.append(p.stem)
    return rep


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
