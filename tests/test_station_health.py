"""fetchers.reveal: each poll records the newest battery/signal reading per
(renamed) station in station_health.json, which hoseserv's map and Security
view read. Newest by capture time, not fetch order; a write failure only logs."""
import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
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
    assert sorted(p.name for p in health.path.parent.iterdir()) == [
        "station_health.json", "station_health_history.jsonl"]


# --- station_health_history.jsonl (hoseserv's "days left" estimate) ---------

TODAY = date(2026, 11, 2)


def reading(battery, y, mo, d, h=10):
    return {"battery_pct": battery, "signal_bars": 3,
            "when": datetime(y, mo, d, h, tzinfo=reveal.PROPERTY_TZ)}


def history(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def hist(tmp_path):
    return tmp_path / "landing" / "station_health_history.jsonl"


def test_history_one_line_per_station_per_date(hist):
    reveal.append_health_history({"Ridge": reading(80, 2026, 10, 30),
                                  "Creek": reading(50, 2026, 10, 30)}, hist, TODAY)
    reveal.append_health_history({"Ridge": reading(78, 2026, 10, 31)}, hist, TODAY)
    assert history(hist) == [
        {"date": "2026-10-30", "station": "Creek", "battery_pct": 50,
         "capture_time": "2026-10-30T17:00:00+00:00"},
        {"date": "2026-10-30", "station": "Ridge", "battery_pct": 80,
         "capture_time": "2026-10-30T17:00:00+00:00"},
        {"date": "2026-10-31", "station": "Ridge", "battery_pct": 78,
         "capture_time": "2026-10-31T17:00:00+00:00"},
    ]


def test_history_date_is_property_local(hist):
    # 23:30 Pacific is already the next day in UTC; the line keeps the local date.
    reveal.append_health_history({"Ridge": reading(80, 2026, 10, 30, h=23)}, hist, TODAY)
    assert history(hist)[0]["date"] == "2026-10-30"


def test_history_same_day_keeps_later_capture(hist):
    reveal.append_health_history({"Ridge": reading(70, 2026, 10, 30, h=12)}, hist, TODAY)
    # Reveal re-serves an earlier photo from the same day: ignored.
    reveal.append_health_history({"Ridge": reading(75, 2026, 10, 30, h=8)}, hist, TODAY)
    assert [r["battery_pct"] for r in history(hist)] == [70]
    # A later capture the same day replaces the line instead of adding one.
    reveal.append_health_history({"Ridge": reading(68, 2026, 10, 30, h=18)}, hist, TODAY)
    assert [(r["date"], r["battery_pct"]) for r in history(hist)] == [("2026-10-30", 68)]


def test_history_drops_lines_older_than_cap(hist):
    reveal.append_health_history({"Old": reading(90, 2026, 5, 5),      # 181 days
                                  "Edge": reading(91, 2026, 5, 6),     # 180 days
                                  "Ridge": reading(80, 2026, 11, 1)}, hist, TODAY)
    assert {r["station"] for r in history(hist)} == {"Edge", "Ridge"}
    # Lines already in the file age out on a later write too.
    reveal.append_health_history({"Ridge": reading(79, 2026, 11, 2)}, hist,
                                 date(2026, 11, 3))
    assert {r["station"] for r in history(hist)} == {"Ridge"}


def test_history_skips_readings_without_battery(hist):
    reveal.append_health_history({"Ridge": reading(None, 2026, 10, 30),
                                  "Creek": reading(40, 2026, 10, 30)}, hist, TODAY)
    assert [r["station"] for r in history(hist)] == ["Creek"]


def test_history_malformed_lines_are_dropped(hist):
    hist.parent.mkdir(parents=True)
    good = {"date": "2026-10-29", "station": "Ridge", "battery_pct": 81,
            "capture_time": "2026-10-29T17:00:00+00:00"}
    hist.write_text("{not json\n" + json.dumps(good) + "\n"
                    + json.dumps({"date": "soon", "station": "X", "battery_pct": 1,
                                  "capture_time": "x"}) + "\n[]\n")
    reveal.append_health_history({"Ridge": reading(80, 2026, 10, 30)}, hist, TODAY)
    assert [r["date"] for r in history(hist)] == ["2026-10-29", "2026-10-30"]


def test_history_written_by_poll_atomically(health):
    reveal.poll(Client([item(1, "Ridge", "10302026100000", battery="55"),
                        item(2, "Creek", "10302026100000", battery=None)]), State())
    hist = health.path.parent / "station_health_history.jsonl"
    assert [(r["station"], r["battery_pct"]) for r in history(hist)] == [("Ridge", 55)]
    assert not [p for p in hist.parent.iterdir() if p.name.startswith(".")]


def test_history_write_failure_logs_and_does_not_block(health, monkeypatch):
    def boom(readings, path=None, today=None):
        raise OSError("disk full")
    monkeypatch.setattr(reveal, "append_health_history", boom)
    state = State()
    assert len(reveal.poll(Client([item(1, "Ridge", "10302026100000")]), state)) == 1
    assert state.get("reveal_cursor_ms") == 1001
    assert any("station health history not written" in line for line in health.lines)
    # station_health.json is still written.
    assert health.read()["stations"]["Ridge"]["battery_pct"] == 80


def test_history_failed_replace_leaves_old_file_and_no_temp(hist, monkeypatch):
    reveal.append_health_history({"Ridge": reading(80, 2026, 10, 30)}, hist, TODAY)
    before = hist.read_text()
    from hoseid import fsio
    monkeypatch.setattr(fsio.os, "replace", mock.Mock(side_effect=OSError("ro")))
    with pytest.raises(OSError):
        reveal.append_health_history({"Ridge": reading(70, 2026, 10, 31)}, hist, TODAY)
    assert hist.read_text() == before
    assert [p.name for p in hist.parent.iterdir()] == [hist.name]


def test_seed_script_builds_daily_history(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "seed_history", Path(__file__).resolve().parents[1]
        / "scripts" / "seed_station_health_history.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    monkeypatch.setattr(seed, "normalize_station", partial(normalize_station, rules=RULES))
    monkeypatch.setattr(reveal, "now_utc", lambda: datetime(2026, 11, 2, 12, tzinfo=timezone.utc))

    def sidecar(n, station, when, battery, vendor="tactacam"):
        p = tmp_path / f"{n}.json"
        p.write_text(json.dumps({"vendor": vendor, "station": station,
                                 "capture_time": when, "conditions": {"battery_pct": battery}}))
        return p
    sidecars = [
        sidecar(1, "Cabin - Yard", "2026-10-30T15:00:00+00:00", 82),
        sidecar(2, "Cabin - Yard", "2026-10-31T01:00:00+00:00", 81),  # 10-30 local, later
        sidecar(3, "Cabin - Yard", "2026-10-31T17:00:00+00:00", 80),
        sidecar(4, "Ridge", "2026-10-31T17:00:00+00:00", None),
        sidecar(5, "Front Door", "2026-10-31T17:00:00+00:00", 50, vendor="arlo"),
    ]
    (tmp_path / "bad.json").write_text("{nope")
    sidecars.append(tmp_path / "bad.json")
    hist = tmp_path / "h.jsonl"
    assert seed.seed(sidecars, hist) == 2
    expected = [("2026-10-30", "Cabin Yard", 81), ("2026-10-31", "Cabin Yard", 80)]
    assert [(r["date"], r["station"], r["battery_pct"]) for r in history(hist)] == expected
    seed.seed(sidecars, hist)  # re-runnable
    assert [(r["date"], r["station"], r["battery_pct"]) for r in history(hist)] == expected
