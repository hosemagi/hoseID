"""hoseid.site.TZ: one site zone from SITE_TZ (America/Los_Angeles when unset,
empty or unknown, the same rule as hoseserv's app.utils.site_tz), and the
modules that used to hard-code LA follow it.

TZ is read at import, so overrides run in a fresh interpreter."""
import json
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hoseid import site

REPO = Path(__file__).resolve().parents[1]

# 2026-10-26T12:50Z: 05:50 PDT on the 26th, 01:50 NZDT on the 27th
_PROBE = """
import json
from fetchers import alerts, reveal
from hoseid import ingest_reveal_export, site
from hoseid.mcp_wildlife import solar
det = {"taxon": "blacktail", "detector_confidence": .9, "detector_class": "animal",
       "taxon_confidence": .8, "taxon_raw": "", "bbox_w": .1, "bbox_h": .1}
cap = {"asset_id": "sha256:aa11", "station": "Bench",
       "capture_time": "2026-10-26T12:50:00+00:00", "media_type": "image"}
ev = alerts.build_event(det, cap, None, None)
print(json.dumps({
    "tz": site.TZ.key,
    "zones": sorted({z.key for z in (alerts.LOCAL_TZ, reveal.PROPERTY_TZ,
                                     ingest_reveal_export.PROPERTY_TZ, solar.LOCAL_TZ)}),
    "local_day": alerts._local_capture_time(cap["capture_time"]).date().isoformat(),
    "capture_hhmm": ev["capture_hhmm"],
    "capture_hour": ev["capture_hour"],
}))
"""


def _probe(site_tz: str | None) -> tuple[dict, str]:
    env = {k: v for k, v in os.environ.items() if k != "SITE_TZ"}
    if site_tz is not None:
        env["SITE_TZ"] = site_tz
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO)])
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout), out.stderr


def test_default_is_los_angeles(monkeypatch):
    monkeypatch.delenv("SITE_TZ", raising=False)
    assert site.site_tz() == ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("value", ["", "   ", "Not/AZone", "../etc/passwd"])
def test_empty_or_unknown_falls_back(monkeypatch, value):
    monkeypatch.setenv("SITE_TZ", value)
    assert site.site_tz() == ZoneInfo("America/Los_Angeles")


def test_unknown_name_warns_once(monkeypatch, capsys):
    monkeypatch.setattr(site, "_TZ_WARNED", set())
    monkeypatch.setenv("SITE_TZ", "Mars/Olympus_Mons")
    site.site_tz()
    site.site_tz()
    assert capsys.readouterr().err.count("Mars/Olympus_Mons") == 1


def test_override(monkeypatch):
    monkeypatch.setenv("SITE_TZ", "Pacific/Auckland")
    assert site.site_tz() == ZoneInfo("Pacific/Auckland")


def test_modules_and_alert_time_default_to_los_angeles():
    got, _ = _probe(None)
    assert got["tz"] == "America/Los_Angeles"
    assert got["zones"] == ["America/Los_Angeles"]
    assert got["local_day"] == "2026-10-26"
    assert (got["capture_hhmm"], got["capture_hour"]) == ("05:50", 5)


def test_site_tz_moves_alert_time_and_local_day():
    got, _ = _probe("Pacific/Auckland")
    assert got["tz"] == "Pacific/Auckland"
    assert got["zones"] == ["Pacific/Auckland"]
    assert got["local_day"] == "2026-10-27"
    assert (got["capture_hhmm"], got["capture_hour"]) == ("01:50", 1)


def test_bad_site_tz_keeps_los_angeles_and_says_so():
    got, err = _probe("Atlantis/Lost_City")
    assert got["zones"] == ["America/Los_Angeles"]
    assert got["capture_hhmm"] == "05:50"
    assert "Atlantis/Lost_City" in err
