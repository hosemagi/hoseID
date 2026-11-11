"""hoseid.site.site_coords(): one site point from SITE_LAT/SITE_LON (the
variables hoseserv's app.utils.site_coords reads), then the first
stations.json entry with coordinates, then Storm Oak. Bad values fall through;
nothing raises.

Stdlib-only so it runs in every venv. stations.json lives under a tmp
HOSEID_ROOT; the once-per-process stderr flags are reset per test."""
import json

import pytest

from hoseid import site

STORM_OAK = (39.32097, -120.97152, "fallback (Storm Oak)")
SITE = (39.0, -121.0, "env SITE_LAT/SITE_LON")
BENCH = (39.5, -121.2, "stations.json:Bench")


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for k in ("SITE_LAT", "SITE_LON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))   # no stations.json yet
    monkeypatch.setattr(site, "_COORDS_DEFAULT_LOGGED", False)


def _stations(tmp_path, stations=None, raw=None):
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    text = raw if raw is not None else json.dumps({"stations": stations})
    (landing / "stations.json").write_text(text)


def _site(mp, lat="39.0", lon="-121.0"):
    mp.setenv("SITE_LAT", lat)
    mp.setenv("SITE_LON", lon)


def _bench(tmp_path):
    _stations(tmp_path, [{"name": "Bench", "lat": 39.5, "lon": -121.2}])


# ── precedence: SITE_* > stations.json > Storm Oak ────────────────────────

def test_site_wins_over_everything(monkeypatch, tmp_path):
    _site(monkeypatch); _bench(tmp_path)
    assert site.site_coords() == SITE


def test_stations_used_when_env_unset(tmp_path):
    _bench(tmp_path)
    assert site.site_coords() == BENCH


def test_storm_oak_when_nothing_set(capsys):
    assert site.site_coords() == STORM_OAK
    site.site_coords()
    err = capsys.readouterr().err
    assert err.count("Storm Oak") == 1


def test_site_does_not_warn(monkeypatch, tmp_path, capsys):
    _site(monkeypatch); _bench(tmp_path)
    site.site_coords()
    assert capsys.readouterr().err == ""


# ── malformed values fall through ─────────────────────────────────────────

@pytest.mark.parametrize("lat,lon", [
    ("not-a-number", "-121.0"),   # unparseable
    ("", "-121.0"),               # empty
    ("500", "-121.0"),            # lat out of range
    ("39.0", "-200"),             # lon out of range
])
def test_bad_site_falls_to_stations(monkeypatch, tmp_path, lat, lon):
    _site(monkeypatch, lat, lon); _bench(tmp_path)
    assert site.site_coords() == BENCH


def test_half_set_site_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("SITE_LAT", "39.0")
    _bench(tmp_path)
    assert site.site_coords() == BENCH


def test_stations_skips_unusable_entries(tmp_path):
    _stations(tmp_path, [
        {"name": "NoCoords"},
        {"name": "Nulls", "lat": None, "lon": None},
        {"name": "Garbage", "lat": "abc", "lon": -121.2},
        {"name": "Far", "lat": 200.0, "lon": -121.2},
        {"name": "Bench", "lat": 39.5, "lon": -121.2},
    ])
    assert site.site_coords() == BENCH


@pytest.mark.parametrize("raw", ["{not valid json", "[]", '{"stations": [1, 2]}'])
def test_corrupt_stations_falls_to_storm_oak(tmp_path, raw):
    _stations(tmp_path, raw=raw)
    assert site.site_coords() == STORM_OAK


# ── the wildlife MCP's sun follows it ─────────────────────────────────────

def test_property_coords_is_site_coords(monkeypatch, tmp_path):
    solar = pytest.importorskip("hoseid.mcp_wildlife.solar")
    _site(monkeypatch); _bench(tmp_path)
    assert solar.property_coords() == SITE
