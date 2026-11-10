"""Arlo fetch daemon's freshness detectors (fetchers/arlo.py).

These decide when the fetch daemon restarts itself: a fleet-wide event burst
must not count as an upload witness, a frozen media library must raise
LibraryFrozenError, and a dead REST session must raise SessionDeadError.

The daemon runs from .venv-fetch (which has pyaarlo); the main test venv does
not, so pytest.importorskip would skip this whole file. Like test_daemon.py,
pyaarlo is stubbed with a MagicMock just long enough to import the module.
No network and no pyaarlo session: _arlo is a stub and the clock is faked.
"""
import importlib.util
import sys
import threading
from unittest import mock

import pytest

# --- conditional pyaarlo stub (same shape as test_daemon.py) ----------------
_MISSING = [m for m in ("pyaarlo", "pyaarlo.constant", "pyaarlo.util")
            if importlib.util.find_spec(m.split(".")[0]) is None]
sys.modules.update({m: mock.MagicMock() for m in _MISSING})
try:
    arlo = __import__("fetchers.arlo", fromlist=["ArloFetcher"])
    from fetchers.arlo import (
        BURST_WINDOW_S,
        LIBRARY_STALE_AFTER_S,
        ArloFetcher,
        LibraryFrozenError,
        SessionDeadError,
    )
finally:
    for m in _MISSING:
        sys.modules.pop(m, None)


class _Cam:
    def __init__(self, device_id, name):
        self.device_id = device_id
        self.name = name


@pytest.fixture
def fetcher(monkeypatch):
    """A bare ArloFetcher with a stubbed _arlo and a fake clock.

    Returns (fetcher, clock) where clock is a dict with a mutable "t" that
    arlo.time.time reads.
    """
    cams = [_Cam("MAD1", "Cabin - Madrone"),
            _Cam("YRD1", "Cabin - Yard"),
            _Cam("BENCH1", "Bench")]
    f = ArloFetcher.__new__(ArloFetcher)
    f._lock = threading.Lock()
    f._recent_media = []
    f._media_events = {}
    f._arlo = type("Arlo", (), {"cameras": cams})()

    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(arlo.time, "time", lambda: clock["t"])
    monkeypatch.setattr(arlo.time, "sleep", lambda *_: None)  # never actually sleep
    return f, clock


def _advance(clock, seconds):
    clock["t"] += seconds


# --- _note_media_event: drop a fleet-wide burst, keep a lone event ----------

def test_three_cameras_in_window_are_kept(fetcher):
    f, clock = fetcher
    for dev in ("MAD1", "YRD1", "BENCH1"):
        _advance(clock, 1)
        f._note_media_event(dev)
    assert set(f._media_events) == {"MAD1", "YRD1", "BENCH1"}


def test_fourth_camera_in_window_drops_the_whole_burst(fetcher):
    f, clock = fetcher
    for dev in ("MAD1", "YRD1", "BENCH1"):
        _advance(clock, 1)
        f._note_media_event(dev)
    assert len(f._media_events) == 3                       # not a burst yet
    _advance(clock, 1)
    f._note_media_event("GRY1")                            # 4th distinct camera
    assert f._media_events == {}                           # burst: witnesses retracted


def test_fourth_camera_outside_window_is_not_a_burst(fetcher):
    f, clock = fetcher
    for dev in ("MAD1", "YRD1", "BENCH1", "GRY1"):
        # just over BURST_WINDOW_S apart -> each event ages out before the next
        _advance(clock, BURST_WINDOW_S + 0.1)
        f._note_media_event(dev)
    assert set(f._media_events) == {"MAD1", "YRD1", "BENCH1", "GRY1"}


def test_lone_event_is_kept(fetcher):
    f, _ = fetcher
    f._note_media_event("MAD1")
    assert f._media_events == {"MAD1": pytest.approx(1_700_000_000.0)}


# --- _assert_library_fresh ---------------------------------------------------
# Times are seconds for events, epoch-ms for clips/cursors, as in sweep().

EVENT_T = 1_700_000_000.0


def _ms(t):
    return int(t * 1000)


def test_no_cursor_camera_is_ignored(fetcher):
    """A camera that never produced a library clip (no cursor) is not a
    witness, even with a clip far older than its event in the window."""
    f, _ = fetcher
    f._media_events = {"MAD1": EVENT_T}
    newest = {"MAD1": _ms(EVENT_T - 10 * LIBRARY_STALE_AFTER_S)}
    f._assert_library_fresh(newest_by_cam=newest, cursors={})        # no raise
    f._assert_library_fresh(newest_by_cam=newest, cursors={"MAD1": 0})  # 0 = none


def test_lag_over_threshold_raises_and_names_the_camera(fetcher):
    f, _ = fetcher
    f._media_events = {"MAD1": EVENT_T}
    newest = {"MAD1": _ms(EVENT_T - LIBRARY_STALE_AFTER_S - 1)}       # 3601s behind
    cursors = {"MAD1": newest["MAD1"]}
    with pytest.raises(LibraryFrozenError) as exc:
        f._assert_library_fresh(newest_by_cam=newest, cursors=cursors)
    assert "Cabin - Madrone" in str(exc.value)


def test_lag_exactly_threshold_does_not_raise(fetcher):
    f, _ = fetcher
    f._media_events = {"MAD1": EVENT_T}
    newest = {"MAD1": _ms(EVENT_T - LIBRARY_STALE_AFTER_S)}           # exactly 3600s
    cursors = {"MAD1": newest["MAD1"]}
    f._assert_library_fresh(newest_by_cam=newest, cursors=cursors)    # no raise


def test_newer_clip_in_library_does_not_raise(fetcher):
    """The newest clip in the window wins over an old cursor."""
    f, _ = fetcher
    f._media_events = {"MAD1": EVENT_T}
    newest = {"MAD1": _ms(EVENT_T - 100)}
    cursors = {"MAD1": _ms(EVENT_T - 10 * LIBRARY_STALE_AFTER_S)}     # stale on its own
    f._assert_library_fresh(newest_by_cam=newest, cursors=cursors)    # no raise


def test_cursor_but_no_clip_is_the_driveway_freeze(fetcher):
    """The 2026-08-25 Driveway case: a camera with a cursor but no clip in the
    library window falls back to its cursor, so an upload event hours past it
    still counts as frozen -- and names that camera, not another one."""
    f, _ = fetcher
    f._media_events = {"YRD1": EVENT_T}
    cursors = {"MAD1": _ms(EVENT_T - 100),                            # fresh, has a clip
               "YRD1": _ms(EVENT_T - 4 * 3600)}                       # 4h stale, no clip
    newest = {"MAD1": cursors["MAD1"]}
    with pytest.raises(LibraryFrozenError) as exc:
        f._assert_library_fresh(newest_by_cam=newest, cursors=cursors)
    assert "Cabin - Yard" in str(exc.value)


def test_cursor_fallback_within_threshold_does_not_raise(fetcher):
    f, _ = fetcher
    f._media_events = {"YRD1": EVENT_T}
    cursors = {"YRD1": _ms(EVENT_T - LIBRARY_STALE_AFTER_S)}
    f._assert_library_fresh(newest_by_cam={}, cursors=cursors)        # no raise


# --- _assert_session_alive ---------------------------------------------------

def test_session_dead_after_two_failed_probes(fetcher):
    f, _ = fetcher
    calls = {"n": 0}

    def be_get(*_):
        calls["n"] += 1

    f._arlo.be = type("BE", (), {"get": be_get})()
    with pytest.raises(SessionDeadError):
        f._assert_session_alive()
    assert calls["n"] == 2


def test_recovers_after_one_retry(fetcher, monkeypatch):
    f, _ = fetcher
    seq = iter([None, "ok"])

    def be_get(*_):
        return next(seq)

    slept = []
    monkeypatch.setattr(arlo.time, "sleep", lambda s: slept.append(s))
    f._arlo.be = type("BE", (), {"get": be_get})()
    f._assert_session_alive()  # fails once, then succeeds
    assert slept == [10]       # one back-off before the retry


def test_first_probe_success_skips_sleep(fetcher, monkeypatch):
    f, _ = fetcher
    slept = {"n": 0}
    monkeypatch.setattr(arlo.time, "sleep",
                        lambda *_: slept.__setitem__("n", slept["n"] + 1))
    f._arlo.be = type("BE", (), {"get": lambda *_: "ok"})()
    f._assert_session_alive()
    assert slept["n"] == 0


def test_sleep_is_stubbed_even_with_fake_clock(fetcher, monkeypatch):
    """The fake clock only replaces time.time; time.sleep must still be a no-op."""
    f, _ = fetcher
    sleep_calls = {"n": 0}
    monkeypatch.setattr(arlo.time, "sleep",
                        lambda *_: sleep_calls.__setitem__("n",
                                                            sleep_calls["n"] + 1))
    f._arlo.be = type("BE", (), {"get": lambda *_: None})()
    with pytest.raises(SessionDeadError):
        f._assert_session_alive()
    assert sleep_calls["n"] == 1
