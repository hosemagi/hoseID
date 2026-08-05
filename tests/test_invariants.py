"""Tests for the locked design invariants.

These are not unit tests of convenience -- each one pins a property the brief calls a design
regression if violated. If one of these fails, the fix is not to change the test.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoseid import landing, paths, stations
from hoseid.sidecar import (
    CaptureTimeSource, MediaType, ResolutionClass, Sidecar, SidecarError, StationSource,
    compute_asset_id, validate_sidecar,
)


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    return tmp_path


def _asset(tmp_path: Path, content: bytes = b"fake-jpeg-bytes", name: str = "a.jpg") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def _sidecar(src: Path, **over) -> Sidecar:
    base = dict(
        asset_id=compute_asset_id(src),
        media_type=MediaType.image,
        source="sd_card",
        resolution_class=ResolutionClass.original,
        station="Crossroads",
        station_source=StationSource.manual,
        device_id="TC-REVEALX3-0419",
        vendor="tactacam",
        capture_time=datetime(2026, 8, 3, 4, 12, 7, tzinfo=timezone.utc),
        capture_time_source=CaptureTimeSource.exif,
        capture_time_confidence="high",
        ingested_at=datetime(2026, 8, 3, 4, 14, 51, tzinfo=timezone.utc),
        bytes=src.stat().st_size,
    )
    base.update(over)
    return Sidecar(**base)


# --- invariant 1: landing zone is append-only and immutable -------------------
def test_reingesting_identical_bytes_is_idempotent(tmp_path):
    src = _asset(tmp_path)
    sc = _sidecar(src)
    r1 = landing.store_asset(src, sc)
    r2 = landing.store_asset(src, sc)
    assert r1.asset_id == r2.asset_id
    assert not r1.already_present and r2.already_present
    assert len(list(paths.assets_dir().rglob("*.jpg"))) == 1


def test_sidecar_overwrite_with_different_content_is_refused(tmp_path):
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    with pytest.raises(landing.ImmutabilityError):
        landing.store_asset(src, _sidecar(src, station="Saddle"))


def test_sidecar_must_match_asset_content(tmp_path):
    src = _asset(tmp_path)
    other = _asset(tmp_path, b"different-bytes", "b.jpg")
    with pytest.raises(SidecarError):
        landing.store_asset(src, _sidecar(other))


def test_no_partial_files_left_visible(tmp_path):
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    assert not [p for p in paths.landing_dir().rglob(".tmp-*")]


# --- content addressing / multi-writer safety --------------------------------
def test_differing_bytes_cannot_collide(tmp_path):
    a, b = _asset(tmp_path, b"aaa", "a.jpg"), _asset(tmp_path, b"bbb", "b.jpg")
    ra = landing.store_asset(a, _sidecar(a))
    rb = landing.store_asset(b, _sidecar(b))
    assert ra.asset_path != rb.asset_path


def test_asset_id_is_content_hash(tmp_path):
    src = _asset(tmp_path, b"known")
    import hashlib
    assert compute_asset_id(src) == "sha256:" + hashlib.sha256(b"known").hexdigest()


# --- sidecar contract ---------------------------------------------------------
def test_naive_timestamps_rejected(tmp_path):
    src = _asset(tmp_path)
    with pytest.raises(Exception):
        _sidecar(src, capture_time=datetime(2026, 8, 3, 4, 12, 7))


def test_untrusted_time_sources_flagged(tmp_path):
    src = _asset(tmp_path)
    trusted = _sidecar(src, capture_time_source=CaptureTimeSource.exif)
    untrusted = _sidecar(src, capture_time_source=CaptureTimeSource.file_mtime)
    assert trusted.time_is_trustworthy
    assert not untrusted.time_is_trustworthy


def test_resolution_class_is_required(tmp_path):
    src = _asset(tmp_path)
    d = json.loads(_sidecar(src).to_json())
    del d["resolution_class"]
    with pytest.raises(SidecarError):
        validate_sidecar(d)


def test_unknown_vendor_fields_are_preserved_not_rejected(tmp_path):
    src = _asset(tmp_path)
    d = json.loads(_sidecar(src).to_json())
    d["some_future_vendor_field"] = "value"
    sc = validate_sidecar(d)
    assert sc.model_dump()["some_future_vendor_field"] == "value"


def test_schema_version_mismatch_rejected(tmp_path):
    src = _asset(tmp_path)
    d = json.loads(_sidecar(src).to_json())
    d["schema_version"] = 99
    with pytest.raises(SidecarError):
        validate_sidecar(d)


# --- station corrections applied at analysis time, never by rewriting ---------
def test_station_override_applies_without_touching_sidecar(tmp_path):
    src = _asset(tmp_path)
    sc = _sidecar(src, capture_time=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc))
    landing.store_asset(src, sc)

    ov = [stations.StationOverride(
        device_id="TC-REVEALX3-0419",
        start=datetime(2026, 8, 15, tzinfo=timezone.utc),
        end=datetime(2026, 8, 19, 23, 59, 59, tzinfo=timezone.utc),
        station="Saddle")]
    effective, corrected = stations.resolve_station(sc.station, sc.device_id, sc.capture_time, ov)
    assert effective == "Saddle" and corrected

    on_disk = validate_sidecar(paths.sidecar_path(sc.asset_id))
    assert on_disk.station == "Crossroads", "sidecar must not be rewritten"


def test_override_outside_window_does_not_apply(tmp_path):
    src = _asset(tmp_path)
    sc = _sidecar(src, capture_time=datetime(2026, 9, 1, tzinfo=timezone.utc))
    ov = [stations.StationOverride(
        device_id="TC-REVEALX3-0419",
        start=datetime(2026, 8, 15, tzinfo=timezone.utc),
        end=datetime(2026, 8, 19, tzinfo=timezone.utc),
        station="Saddle")]
    effective, corrected = stations.resolve_station(sc.station, sc.device_id, sc.capture_time, ov)
    assert effective == "Crossroads" and not corrected


# --- landing zone check -------------------------------------------------------
def test_check_detects_missing_asset(tmp_path):
    src = _asset(tmp_path)
    sc = _sidecar(src)
    landing.store_asset(src, sc)
    landing.find_asset(sc.asset_id).unlink()
    rep = landing.check_landing_zone()
    assert sc.asset_id in rep.missing_asset and not rep.ok


def test_check_detects_orphan_asset(tmp_path):
    src = _asset(tmp_path)
    sc = _sidecar(src)
    landing.store_asset(src, sc)
    paths.sidecar_path(sc.asset_id).unlink()
    rep = landing.check_landing_zone()
    assert rep.orphan_assets and not rep.ok


def test_check_clean_zone_is_ok(tmp_path):
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    rep = landing.check_landing_zone(verify_digests=True)
    assert rep.ok and rep.n_sidecars == 1 and rep.n_assets_present == 1


# --- re-ingest must be a no-op, but real conflicts must still be refused ------
def test_reingest_with_different_ingest_time_is_a_noop(tmp_path):
    """Re-ingesting the same card generates a fresh ingested_at. That is provenance of the
    ingest run, not a property of the capture, and must not be treated as a conflict."""
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    later = _sidecar(src, ingested_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    r = landing.store_asset(src, later)
    assert r.already_present
    on_disk = validate_sidecar(paths.sidecar_path(r.asset_id))
    assert on_disk.ingested_at == datetime(2026, 8, 3, 4, 14, 51, tzinfo=timezone.utc), \
        "original sidecar must be preserved, not overwritten"


def test_reingest_from_a_different_mount_path_is_a_noop(tmp_path):
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src, raw_vendor_payload={"source_relpath": "DCIM/a.jpg"}))
    r = landing.store_asset(src, _sidecar(src, raw_vendor_payload={"source_relpath": "X/a.jpg"}))
    assert r.already_present


def test_conflicting_station_is_still_refused(tmp_path):
    """A genuine disagreement about the capture must still fail loudly."""
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    with pytest.raises(landing.ImmutabilityError) as e:
        landing.store_asset(src, _sidecar(src, station="Saddle"))
    assert "station" in str(e.value)


def test_conflicting_capture_time_is_still_refused(tmp_path):
    src = _asset(tmp_path)
    landing.store_asset(src, _sidecar(src))
    with pytest.raises(landing.ImmutabilityError):
        landing.store_asset(src, _sidecar(
            src, capture_time=datetime(2020, 1, 1, tzinfo=timezone.utc)))
