"""The stage-2 crop guard.

Findings §A2 measured SpeciesNet at 92.7% on detector crops and 29.2% on full frames, and the
full-frame failure is silent: the always-crop classifier returns `blank` at ~0.99 confidence
rather than erroring. That is the worst kind of bug -- confidently wrong, no signal.

These tests import the guard directly rather than the whole stage module, so they run in the
main venv without speciesnet installed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hoseid import paths

STAGE = Path(__file__).resolve().parents[1] / "stages" / "classify.py"


def _load_guard(monkeypatch, tmp_path):
    """Load classify.py's guard without importing speciesnet (absent from this venv)."""
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    sys.modules.pop("stages_classify", None)
    spec = importlib.util.spec_from_file_location("stages_classify", STAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # speciesnet is imported inside main(), not at module load
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    m = _load_guard(monkeypatch, tmp_path)
    paths.ensure_layout()
    return m


def test_full_frame_from_landing_zone_is_rejected(guard, tmp_path):
    full = paths.assets_dir() / "ab" / "cd" / "abcd.jpg"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"x")
    with pytest.raises(guard.FullFrameError) as e:
        guard._assert_is_crop(full, "abcd")
    assert "always-crop" in str(e.value)


def test_arbitrary_path_outside_crops_dir_is_rejected(guard, tmp_path):
    stray = tmp_path / "somewhere" / "img.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    with pytest.raises(guard.FullFrameError):
        guard._assert_is_crop(stray, "img")


def test_genuine_crop_is_accepted(guard):
    did = "deadbeef"
    crop = paths.crops_dir() / "2026" / "08" / f"{did}.jpg"
    crop.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(b"x")
    guard._assert_is_crop(crop, did)      # must not raise


def test_crop_belonging_to_a_different_detection_is_rejected(guard):
    crop = paths.crops_dir() / "2026" / "08" / "aaaa.jpg"
    crop.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(b"x")
    with pytest.raises(guard.FullFrameError):
        guard._assert_is_crop(crop, "bbbb")
