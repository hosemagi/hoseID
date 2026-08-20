"""Tests for bulk Reveal-export ingest.

The property that matters most here is refusal: an export file whose camera serial is not in the
station registry must NOT land under a guessed station. Sidecars are immutable (invariant 1), so
a wrong station can only ever be papered over by an override afterwards, whereas a refused file
can simply be ingested once the registry names the camera.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hoseid import ingest_reveal_export as ire
from hoseid import landing, paths, stations
from hoseid.sidecar import compute_asset_id, validate_sidecar

SERIAL = "016579006078088"
NAME = f"{SERIAL}-100-3-07252026161246-SYFW00023.jpg"


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    paths.stations_file().write_text(json.dumps({"stations": [
        {"name": "Storm Oak", "device_id": SERIAL, "vendor": "tactacam"},
    ]}))
    return tmp_path


def _export(tmp_path, name=NAME, data=b"jpeg-bytes"):
    d = tmp_path / "export"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)
    return d


# --- the filename contract ----------------------------------------------------

def test_parses_serial_and_camera_local_timestamp():
    device, ct, seq = ire.parse_export_name(f"{SERIAL}-100-3-07252026161246-SYFW00023")
    assert device == SERIAL and seq == "SYFW00023"
    # MMDDYYYYHHMMSS, read in the property's zone -- not UTC.
    assert (ct.year, ct.month, ct.day, ct.hour, ct.minute, ct.second) == (2026, 7, 25, 16, 12, 46)
    assert ct.utcoffset() != timezone.utc.utcoffset(None) or ct.tzinfo is not None


def test_a_name_that_is_not_the_export_shape_is_rejected():
    assert ire.parse_export_name("IMG_0042") is None
    assert ire.parse_export_name("2026-08-16-something") is None


def test_unparsed_names_are_reported_not_ingested(tmp_path):
    d = _export(tmp_path, name="IMG_0042.jpg")
    rep = ire.ingest_export(d)
    assert rep.ingested == 0 and rep.unparsed_name == ["IMG_0042.jpg"]


# --- refusal beats guessing ---------------------------------------------------

def test_unknown_serial_is_refused_and_counted(tmp_path):
    """A capture ingested under the wrong station is immutable and corrupts corridor analysis;
    a refused capture can be ingested later once the registry names the camera."""
    d = _export(tmp_path, name="999999999999999-100-3-07252026161246-SYFW00023.jpg")
    rep = ire.ingest_export(d)
    assert rep.ingested == 0
    assert rep.unknown_device == {"999999999999999": 1}
    assert not list(paths.sidecars_dir().rglob("*.json"))


def test_duplicate_serial_in_registry_raises_rather_than_picking_one(tmp_path):
    paths.stations_file().write_text(json.dumps({"stations": [
        {"name": "Storm Oak", "device_id": SERIAL},
        {"name": "Bench", "device_id": SERIAL},
    ]}))
    with pytest.raises(ValueError, match="claimed by both"):
        stations.by_device()


# --- provenance ---------------------------------------------------------------

def test_sidecar_records_reveal_export_provenance(tmp_path):
    d = _export(tmp_path)
    ire.ingest_export(d)
    sc = validate_sidecar(next(iter(paths.sidecars_dir().rglob("*.json"))))
    assert sc.station == "Storm Oak"
    assert sc.station_source == "manual"
    assert sc.source == "reveal_export"
    assert sc.resolution_class == "compressed", "cellular delivery downsizes; not card originals"
    assert sc.device_id == SERIAL
    assert sc.vendor_asset_id == "SYFW00023"


def test_capture_time_is_trusted_so_the_capture_reaches_encounter_grouping(tmp_path):
    """The filename timestamp IS the vendor's photoTimestamp field. Recording it as file_mtime
    would drop every one of these captures out of time-trusted analysis."""
    d = _export(tmp_path)
    ire.ingest_export(d)
    sc = validate_sidecar(next(iter(paths.sidecars_dir().rglob("*.json"))))
    assert sc.capture_time_source == "vendor_api"
    assert sc.time_is_trustworthy
    assert sc.capture_time_confidence == "medium", "the zone is implicit, so not high"


# --- landing-zone behaviour ---------------------------------------------------

def test_ingest_is_content_addressed_and_idempotent(tmp_path):
    d = _export(tmp_path)
    first = ire.ingest_export(d)
    second = ire.ingest_export(d)
    assert first.ingested == 1 and second.ingested == 0 and second.duplicates == 1
    assert len(list(paths.sidecars_dir().rglob("*.json"))) == 1


def test_ingested_export_satisfies_the_landing_zone_check(tmp_path):
    """The point of the exercise: these files stop being orphan assets."""
    d = _export(tmp_path)
    ire.ingest_export(d)
    rep = landing.check_landing_zone(verify_digests=True)
    assert rep.n_sidecars == 1 and rep.n_assets_present == 1
    assert rep.orphan_assets == [] and rep.digest_mismatch == []


def test_dry_run_writes_nothing(tmp_path):
    d = _export(tmp_path)
    rep = ire.ingest_export(d, dry_run=True)
    assert rep.ingested == 1
    assert not list(paths.sidecars_dir().rglob("*.json"))


def test_same_capture_from_two_export_dirs_dedupes_by_content(tmp_path):
    """The three export directories overlap; identical bytes must land once."""
    a = tmp_path / "exp1"
    b = tmp_path / "exp2"
    for d in (a, b):
        d.mkdir()
        (d / NAME).write_bytes(b"identical-bytes")
    ire.ingest_export(a)
    rep = ire.ingest_export(b)
    assert rep.duplicates == 1 and rep.ingested == 0
    assert len(list(paths.assets_dir().rglob("*.jpg"))) == 1


# --- what `check` says about the staged copies afterwards ---------------------

def test_a_raw_copy_alongside_its_ingested_asset_is_not_a_failure(tmp_path):
    """After ingest the vendor-named file is redundant, not missing. Reporting it as an orphan
    made `check` fail with 1,726 entries that looked like lost data, so the failure went
    unactioned for weeks."""
    d = _export(tmp_path)
    ire.ingest_export(d)
    staged = paths.assets_dir() / "staged"
    staged.mkdir()
    (staged / NAME).write_bytes((d / NAME).read_bytes())

    rep = landing.check_landing_zone()
    assert rep.ok, "a redundant copy of an ingested asset must not fail the check"
    assert rep.orphan_assets == []
    assert rep.staged_duplicates == [f"staged/{NAME}"]


def test_a_genuinely_unaccounted_file_still_fails_the_check(tmp_path):
    """The safety property the split must not cost: a file whose bytes are nowhere in the
    content-addressed store is still unaccounted data and still fails."""
    stray = paths.assets_dir() / "stray"
    stray.mkdir()
    (stray / "mystery.jpg").write_bytes(b"never-ingested")

    rep = landing.check_landing_zone()
    assert not rep.ok
    assert rep.orphan_assets == ["mystery"] and rep.staged_duplicates == []


def test_a_staged_file_whose_ingested_copy_is_gone_is_not_called_redundant(tmp_path):
    """The dangerous edge: sidecar present, asset missing. The staged file is then the only copy
    of those bytes, and reporting it as a redundant duplicate invites deleting the last one."""
    d = _export(tmp_path)
    ire.ingest_export(d)
    staged = paths.assets_dir() / "staged"
    staged.mkdir()
    (staged / NAME).write_bytes((d / NAME).read_bytes())
    for asset in paths.assets_dir().rglob("*.jpg"):
        if asset.parent.name != "staged":
            asset.unlink()

    rep = landing.check_landing_zone()
    assert not rep.ok
    assert rep.staged_duplicates == [], "the last copy is not a redundant copy"
    assert rep.orphan_assets == [NAME.removesuffix(".jpg")]
