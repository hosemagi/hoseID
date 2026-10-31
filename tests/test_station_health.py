"""fetchers.reveal: each poll records the newest battery/signal reading per
(renamed) station in station_health.json, which hoseserv's map and Security
view read. Newest by capture time, not fetch order; a write failure only logs."""
import importlib.util
import json
import sys
from functools import partial
from types import SimpleNamespace
from unittest import mock

import pytest

# Same stubbing as test_daemon.py: the main test venv has no requests, and
# nothing here touches the network (photos/download/ingest are stubbed).
_STUBS = ("requests", "urllib3", "pyaarlo", "pyaarlo.constant", "pyaarlo.util")
_missing = [m for m in _STUBS if importlib.util.find_spec(m.split(".")[0]) is None]
sys.modules.update({m: mock.MagicMock() for m in _missing})
try:
    from fetchers import reveal
    from hoseid.station_renames import normalize_station
finally:
    for m in _missing:
        sys.modules.pop(m, None)

RULES = {"renames": {"Yard": "Cabin Yard", "CB5": "Enchanted Forest"},
         "strip_prefix": "Cabin - ", "special": []}


def item(n, camera, ts, battery="80", signal="4"):
    meta = {}
    if battery is not None:
        meta["batteryLevel"] = battery
    if signal is not None:
        meta["signal"] = signal
    return {"photoId": f"p{n}", "photoUrl": f"https://x/{n}", "createdTimestamp": 1000 + n,
            "cameraName": camera, "cameraId": f"dev-{camera}", "photoTimestamp": ts,
            "metadata": meta}


class Client:
    def __init__(self, items):
        self.items = items

    def photos(self, page=0, size=reveal.PAGE_SIZE):
        return self.items if page == 0 else []


class State:
    def __init__(self):
        self.data = {"reveal_cursor_ms": 0}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture
def health(tmp_path, monkeypatch):
    path = tmp_path / "landing" / "station_health.json"
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    monkeypatch.setattr(reveal, "download", lambda url, dest: dest)
    monkeypatch.setattr(reveal, "ingest", lambda tmp, sc: SimpleNamespace(
        already_present=False, asset_id=f"sha256:{sc['vendor_asset_id']}"))
    monkeypatch.setattr(reveal, "normalize_station", partial(normalize_station, rules=RULES))
    lines = []
    monkeypatch.setattr(reveal, "log", lines.append)
    return SimpleNamespace(path=path, lines=lines,
                           read=lambda: json.loads(path.read_text()))


def test_writes_newest_reading_per_station(health):
    items = [
        # Fetch order differs from capture order: the 09:00 photo comes last.
        item(1, "Ridge", "10302026100000", battery="55", signal="3"),
        item(2, "Ridge", "10302026090000", battery="60", signal="4"),
        item(3, "Creek", "10292026230000", battery="18", signal="1"),
    ]
    assert len(reveal.poll(Client(items), State())) == 3
    data = health.read()
    assert set(data) == {"updated", "stations"}
    assert data["stations"] == {
        "Ridge": {"battery_pct": 55, "signal_bars": 3,
                  "capture_time": "2026-10-30T17:00:00+00:00"},
        "Creek": {"battery_pct": 18, "signal_bars": 1,
                  "capture_time": "2026-10-30T06:00:00+00:00"},
    }


def test_later_fetch_keeps_newer_entry(health):
    reveal.poll(Client([item(1, "Ridge", "10302026100000", battery="55"),
                        item(2, "Creek", "10292026100000", battery="90")]), State())
    # Reveal re-serves an older Ridge photo; a newer Creek one also arrives.
    reveal.poll(Client([item(3, "Ridge", "10282026100000", battery="99"),
                        item(4, "Creek", "10302026120000", battery="88")]), State())
    st = health.read()["stations"]
    assert st["Ridge"]["battery_pct"] == 55
    assert st["Creek"]["battery_pct"] == 88


def test_missing_readings_are_skipped(health):
    reveal.poll(Client([item(1, "Ridge", "10302026100000", battery=None, signal=None),
                        item(2, "Creek", "10302026100000", battery=None, signal="2")]), State())
    st = health.read()["stations"]
    assert "Ridge" not in st
    assert st["Creek"] == {"battery_pct": None, "signal_bars": 2,
                           "capture_time": "2026-10-30T17:00:00+00:00"}


def test_no_readings_writes_nothing(health):
    reveal.poll(Client([item(1, "Ridge", "10302026100000", battery=None, signal=None)]), State())
    assert not health.path.exists()


def test_renamed_station_uses_new_name(health):
    reveal.poll(Client([item(1, "Cabin - Yard", "10302026100000"),
                        item(2, "CB5", "10302026100000")]), State())
    assert set(health.read()["stations"]) == {"Cabin Yard", "Enchanted Forest"}


def test_conflict_skips_still_record_health(health, monkeypatch):
    def conflict(tmp, sc):
        raise reveal.ImmutabilityError("conflicting sidecar")
    monkeypatch.setattr(reveal, "ingest", conflict)
    state = State()
    assert reveal.poll(Client([item(1, "Ridge", "10302026100000", battery="42")]), state) == []
    assert state.get("reveal_cursor_ms") == 1001
    assert health.read()["stations"]["Ridge"]["battery_pct"] == 42


def test_malformed_existing_file_is_replaced(health):
    health.path.parent.mkdir(parents=True)
    health.path.write_text("{not json")
    reveal.poll(Client([item(1, "Ridge", "10302026100000")]), State())
    assert set(health.read()["stations"]) == {"Ridge"}


def test_write_failure_logs_and_does_not_block(health, monkeypatch):
    def boom(readings, path=None):
        raise OSError("disk full")
    monkeypatch.setattr(reveal, "write_station_health", boom)
    state = State()
    assert len(reveal.poll(Client([item(1, "Ridge", "10302026100000")]), state)) == 1
    assert state.get("reveal_cursor_ms") == 1001
    assert any("station health not written" in line for line in health.lines)
    assert not health.path.exists()


def test_write_leaves_no_temp_files(health):
    reveal.poll(Client([item(1, "Ridge", "10302026100000")]), State())
    assert [p.name for p in health.path.parent.iterdir()] == ["station_health.json"]
