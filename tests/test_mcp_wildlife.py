"""Wildlife MCP server: aggregate-first queries, computed light, units and
truncation flags, batch images with reference frames, registries, schema.

Runs against a synthetic wildlife.db under a tmp HOSEID_ROOT (same DDL as the
live log) with real JPEGs and, when ffmpeg is present, a real mp4 in the
assets dir. The server is exercised in-process via FastMCP.call_tool /
read_resource, so no port is opened.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import sqlite3
import subprocess
from datetime import date

import pytest

from hoseid import paths
from hoseid.mcp_wildlife import media, queries, solar

pytest.importorskip("mcp.server.fastmcp")
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from hoseid.mcp_wildlife.server import create_server  # noqa: E402

DDL = """
CREATE TABLE stations (name TEXT PRIMARY KEY, aliases TEXT NOT NULL DEFAULT '[]',
    camera TEXT, description TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
CREATE TABLE individuals (name TEXT PRIMARY KEY, species TEXT NOT NULL, first_seen TEXT,
    description TEXT NOT NULL DEFAULT '', id_basis TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '');
CREATE TABLE sightings (sighting_id INTEGER PRIMARY KEY, date TEXT NOT NULL, time TEXT,
    station TEXT NOT NULL, species TEXT NOT NULL, individual TEXT, individual_confidence TEXT,
    count INTEGER NOT NULL DEFAULT 1, count_is_visits INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'camera', category TEXT NOT NULL DEFAULT 'other',
    capture_asset_id TEXT, auto INTEGER NOT NULL DEFAULT 0, harvest INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '', doc_ref TEXT, media_refs TEXT NOT NULL DEFAULT '[]',
    weather TEXT);
CREATE TABLE claims (claim_id INTEGER PRIMARY KEY, claim TEXT NOT NULL, status TEXT NOT NULL,
    registered_at TEXT, resolved_at TEXT, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE open_items (item_id INTEGER PRIMARY KEY, item TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', added TEXT, resolved TEXT, notes TEXT NOT NULL DEFAULT '');
CREATE VIEW individual_tally AS
    SELECT individual, COUNT(*) AS capture_events, SUM(source != 'camera') AS direct_visuals,
           MIN(date) AS first_logged, MAX(date) AS last_logged,
           COUNT(DISTINCT station) AS stations_hit
    FROM sightings WHERE individual IS NOT NULL GROUP BY individual;
"""
WX = json.dumps({"temp_f": 64.7, "pressure_inhg": 29.81, "wind_mph": 7.2, "wind_dir": "E",
                 "pressure_change_3h_inhg": -0.05, "precip_in": 0.0})
LAT, LON = 39.32097, -120.97152


def _jpeg(path, size=(1600, 1200)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (90, 120, 60)).save(path, "JPEG")


def _mp4(path) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=2",
                        "-pix_fmt", "yuv420p", str(path)], capture_output=True)
    return r.returncode == 0 and path.is_file()


def _media(*paths_kinds):
    return json.dumps([{"ref": f"sha256:{p.stem}", "path": str(p), "kind": k} for p, k in paths_kinds])


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    monkeypatch.setenv("WILDLIFE_LAT", str(LAT))
    monkeypatch.setenv("WILDLIFE_LON", str(LON))
    paths.ensure_layout()
    img1 = paths.assets_dir() / "ab" / "cd" / "abcd1.jpg"; _jpeg(img1)
    img2 = paths.assets_dir() / "ab" / "ce" / "abce2.jpg"; _jpeg(img2, (800, 1600))
    img3 = paths.assets_dir() / "ab" / "cf" / "abcf3.jpg"; _jpeg(img3)
    vid = paths.assets_dir() / "ef" / "01" / "ef01.mp4"
    have_video = _mp4(vid)
    outside = tmp_path / "outside.jpg"; _jpeg(outside, (40, 40))

    conn = sqlite3.connect(queries.wildlife_db())
    conn.executescript(DDL)
    conn.executemany("INSERT INTO stations VALUES (?,?,?,?,?)", [
        ("Storm Oak", '["CAM1", "CB1"]', "tactacam", "clearing", ""),
        ("Bench", "[]", "tactacam", "", ""),
    ])
    conn.executemany("INSERT INTO individuals (name, species, first_seen, id_basis) VALUES (?,?,?,?)", [
        ("Al", "deer", "2026-07-18", "rack mass vs Ben"), ("Boar", "bear", "2026-06-25", "color + build"),
    ])
    # 2026-09-01 at the property: civil dawn ~05:59, sunrise ~06:29, sunset ~19:35, dusk ~20:04
    rows = [
        # id, date, time, station, species, individual, conf, count, source, category, asset, auto, media, notes, weather
        (1, "2026-09-01", "03:10", "Storm Oak", "bear", None, None, 1, "camera", "bear", "sha256:a1", 1,
         _media((img1, "image")), "", WX),
        (2, "2026-09-01", "06:10", "Bench", "deer", "Al", "confirmed", 2, "camera", "deer", "sha256:a2", 0,
         _media((img2, "image")), "dawn-edge; reference frame for the fork check", None),
        (3, "2026-09-02", "23:40", "Storm Oak", "deer", None, None, 1, "camera", "deer", "sha256:a3", 1,
         _media((vid, "video")) if have_video else "[]", "", None),
        (4, "2026-09-03", None, "deck", "deer", "Al", "assumed", 1, "direct_visual", "deer", None, 0,
         "[]", "deck watch", None),
        (5, "2026-09-03", "13:00", "Bench", "jackrabbit", None, None, 1, "camera", "small_game", "sha256:a5", 1,
         json.dumps([{"ref": "x", "path": str(outside), "kind": "image"},
                     {"ref": "y", "path": str(paths.assets_dir() / "zz" / "nope.jpg"), "kind": "image"}]),
         "", None),
        (6, "2026-08-30", "19:05", "CB1", "bear", "Boar", "by_elimination", 1, "camera", "bear", "sha256:a6", 0,
         _media((img3, "image")), "", None),
        # same capture as row 1, split by species (two rows, one capture)
        (7, "2026-09-01", "03:10", "Storm Oak", "other-animal", None, None, 1, "camera", "other", "sha256:a1", 1,
         _media((img1, "image")), "", WX),
        (8, "2026-09-04", "12:00", "CB7", "gray_fox", None, None, 1, "camera", "predator", "sha256:a8", 1, "[]", "", None),
        (9, "2026-09-04", "12:30", "CB7", "fox", None, None, 1, "camera", "predator", "sha256:a9", 1, "[]", "", None),
    ]
    conn.executemany(
        "INSERT INTO sightings (sighting_id, date, time, station, species, individual, "
        "individual_confidence, count, source, category, capture_asset_id, auto, media_refs, notes, "
        "weather) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?)", [
        (1, "jackrabbit not brush rabbit", "resolved", None, "2026-08-05", "Owner visual. Do not re-raise."),
        (2, "pressure predicts movement", "open", None, None, "untracked"),
    ])
    conn.executemany("INSERT INTO open_items VALUES (?,?,?,?,?,?)", [
        (1, "map CB stations", "open", "2026-08-16", None, ""),
        (2, "fix CB1 tag", "done", "2026-08-16", "2026-08-16", ""),
    ])
    conn.commit()
    conn.close()
    return {"root": tmp_path, "have_video": have_video, "img1": img1, "img2": img2}


def _run(coro):
    return asyncio.run(coro)


def _tool(name, **args):
    m = create_server()
    return _run(m.call_tool(name, args))


def _json(name, **args):
    return json.loads(_tool(name, **args)[0].text)


# ── solar ─────────────────────────────────────────────────────────────────

def test_sun_times_move_through_the_season():
    jul = solar.sun_times(date(2026, 7, 1), LAT, LON)
    sep = solar.sun_times(date(2026, 9, 12), LAT, LON)
    assert jul.sunrise.strftime("%H:%M") == "05:40" and sep.sunrise.strftime("%H:%M") == "06:42"
    assert (sep.sunrise - jul.sunrise.replace(month=9, day=12)).total_seconds() / 60 > 50
    assert solar.classify("2026-09-12", "06:20", LAT, LON) == ("civil_twilight", True)
    assert solar.classify("2026-09-12", "06:00", LAT, LON) == ("dark", False)
    assert solar.classify("2026-09-12", "19:45", LAT, LON) == ("dark", True)   # inside +30 min
    assert solar.classify("2026-09-12", None, LAT, LON) == (None, None)


# ── query layer ───────────────────────────────────────────────────────────

def test_summary_is_default_and_names_units(root):
    d = queries.query_sightings({})
    assert d["response_mode"] == "summary" and d["unit"] == "sightings"
    assert d["sighting_count"] == 9 and d["capture_count"] == 8   # rows 1+7 share a capture
    assert "rows" not in d
    assert {i["species"] for i in d["by_species"]["items"]} >= {"deer", "bear"}
    al = next(i for i in d["by_individual"]["items"] if i["individual"] == "Al")
    assert al["by_confidence"] == {"confirmed": 1, "assumed": 1}
    assert al["by_source"] == {"camera": 1, "direct_visual": 1}
    assert d["by_light"]["items"] and all("legal_light_sightings" in i for i in d["by_light"]["items"])
    assert d["light_model"]["lat"] == LAT


def test_light_filter_is_computed_per_date(root):
    tw = queries.query_sightings({"light": "civil_twilight"}, "rows", limit=10)
    assert [r["sighting_id"] for r in tw["rows"]] == [2]
    assert tw["rows"][0]["legal_light"] is True
    dark = queries.query_sightings({"light": ["dark"]}, "rows", limit=10)
    assert {r["sighting_id"] for r in dark["rows"]} == {1, 3, 7} if root["have_video"] or True else None
    legal = queries.query_sightings({"legal_light": True}, "rows", limit=10)
    assert {r["sighting_id"] for r in legal["rows"]} == {2, 5, 6, 8, 9}
    assert queries.query_sightings({"light": "daylight", "species": "deer"})["sighting_count"] == 0


def test_filters_species_confidence_source_station_alias_auto(root):
    assert queries.query_sightings({"species": "bear, deer"})["sighting_count"] == 5
    assert queries.query_sightings({"confidence": ["confirmed", "by_elimination"]})["sighting_count"] == 2
    assert queries.query_sightings({"confidence": "none"})["sighting_count"] == 6
    assert queries.query_sightings({"source": "direct_visual"})["sighting_count"] == 1
    q = queries.query_sightings({"station": "cam1"})
    assert q["filters_applied"]["station"] == ["Storm Oak"] and q["sighting_count"] == 3
    assert queries.query_sightings({"auto": False})["sighting_count"] == 3
    assert queries.query_sightings({"time_of_day": "night"})["sighting_count"] == 3
    assert queries.query_sightings({"hour_from": 22, "hour_to": 4})["sighting_count"] == 3
    assert queries.query_sightings({"start_date": "2026-09-03", "end_date": "2026-09-04"})["sighting_count"] == 4


def test_rows_mode_requires_limit_and_reports_truncation(root):
    with pytest.raises(queries.QueryError, match="requires an explicit limit"):
        queries.query_sightings({}, "rows")
    with pytest.raises(queries.QueryError, match="1..200"):
        queries.query_sightings({}, "rows", limit=500)
    r = queries.query_sightings({}, "rows", limit=4)
    assert r["returned"] == 4 and r["total_matching"] == 9 and r["truncated"] is True
    assert r["rows"][0]["sighting_id"] == 9   # newest first by default
    assert {"individual_confidence", "source", "light", "legal_light"} <= set(r["rows"][0])
    r2 = queries.query_sightings({}, "rows", limit=4, offset=8)
    assert r2["returned"] == 1 and r2["truncated"] is False
    asc = queries.query_sightings({}, "rows", limit=2, order="asc")
    assert asc["rows"][0]["sighting_id"] == 6


def test_by_day_and_histograms(root):
    d = queries.query_sightings({}, "by_day", order="asc")
    assert [x["date"] for x in d["days"]][:2] == ["2026-08-30", "2026-09-01"]
    sep1 = next(x for x in d["days"] if x["date"] == "2026-09-01")
    assert sep1["sighting_count"] == 3 and sep1["capture_count"] == 2 and sep1["legal_light_sightings"] == 1
    h = queries.query_sightings({}, "histogram", "hour")
    assert len(h["bins"]) == 24 and h["bins"][3]["sighting_count"] == 2 and h["untimed_sightings"] == 1
    t = queries.query_sightings({}, "histogram", "time_of_day")
    assert {b["time_of_day"]: b["sighting_count"] for b in t["bins"]} == \
        {"night": 3, "morning": 1, "midday": 3, "evening": 1}
    l = queries.query_sightings({}, "histogram", "light")
    assert {b["light"]: b["sighting_count"] for b in l["bins"]} == {"daylight": 4, "civil_twilight": 1, "dark": 3}
    assert l["sun_times_first_day"]["date"] == "2026-08-30"


def test_bad_inputs_raise_query_error(root):
    for f, kw in (({"start_date": "9/1"}, {}), ({"light": "dawn"}, {}), ({"confidence": "sure"}, {}),
                  ({"hour_from": 3}, {}), ({"auto": "maybe"}, {}), ({}, {"response_mode": "csv"}),
                  ({}, {"response_mode": "histogram", "histogram_bin": "week"})):
        with pytest.raises(queries.QueryError):
            queries.query_sightings(f, **kw)


def test_payload_cap_truncates_visibly(root, monkeypatch):
    monkeypatch.setattr(queries, "PAYLOAD_CAP_BYTES", 1500)
    r = queries.query_sightings({}, "rows", limit=9)
    assert r["truncated"] is True and r["returned"] < 9 and "payload cap" in r["truncated_reason"]


# ── registries ────────────────────────────────────────────────────────────

def test_individuals_reference_frames_and_staleness(root):
    (paths.tags_dir() / "reference_frames.json").write_text(json.dumps({"individuals": {
        "Boar": {"reference_frames": [{"sighting_id": 6, "media_index": 0, "note": "curated"}]},
        "Al": {"appearance_changes": [{"date": "2026-09-05", "note": "velvet shed"}]},
    }}))
    d = queries.get_individuals()
    by = {i["name"]: i for i in d["individuals"]}
    al = by["Al"]
    assert al["reference_frames"][0]["sighting_id"] == 2 and al["reference_frames"][0]["source"] == "notes"
    assert al["reference_frames_stale"] is True and "velvet" in al["reference_frames_stale_reason"]
    assert al["id_basis"] == "rack mass vs Ben" and al["sightings_by_confidence"] == {"confirmed": 1, "assumed": 1}
    assert al["direct_visuals"] == 1 and al["station_count"] == 2
    boar = by["Boar"]
    assert boar["reference_frames"][0]["source"] == "curated" and boar["reference_frames"][0]["date"] == "2026-08-30"
    assert boar["reference_frames_stale"] is False


def test_claims_open_items_stations_schema(root):
    c = queries.get_claims()
    assert c["claims"][0]["do_not_reraise"] is True and c["claims"][1]["do_not_reraise"] is False
    assert [x["claim_id"] for x in queries.get_claims("open")["claims"]] == [2]
    assert queries.get_open_items(["done"])["returned"] == 1
    s = queries.get_stations()
    by = {x["name"]: x for x in s["stations"]}
    assert by["Storm Oak"]["unmapped"] is False and by["Storm Oak"]["capture_count"] == 2
    assert by["CB7"]["unmapped"] is True and s["unmapped_stations"] == ["CB7", "CB1", "deck"] or \
        set(s["unmapped_stations"]) == {"CB7", "CB1", "deck"}
    sc = queries.get_schema()
    kinds = {i["kind"]: i for i in sc["vocabulary_issues"]}
    assert kinds["split_species"]["values"] == ["fox", "gray_fox"]
    assert kinds["other_animal_duplicates"]["count"] == 1
    assert "category_as_species" not in kinds
    assert {x["station"] for x in sc["stations"] if not x["registered"]} == {"deck", "CB1", "CB7"}
    assert sc["rows_max_limit"] == 200 and "sightings" in sc["units"]


# ── media ─────────────────────────────────────────────────────────────────

def test_media_refuses_paths_outside_assets_and_missing_files(root):
    s = queries.get_sightings_by_id([5])[5]
    with pytest.raises(media.MediaError, match="outside"):
        media.safe_path(s["media"][0])
    with pytest.raises(media.MediaError, match="missing"):
        media.safe_path(s["media"][1])


def test_preview_downscales_keeps_aspect_and_caches(root):
    from PIL import Image
    entry = queries.get_sightings_by_id([2])[2]["media"][0]   # 800x1600 portrait
    im = Image.open(io.BytesIO(media.preview_jpeg(entry, 400)))
    assert im.size == (200, 400)
    im = Image.open(io.BytesIO(media.preview_jpeg(entry, 5000)))   # clamped to MAX_MAX_PX
    assert max(im.size) == media.MAX_MAX_PX
    assert len(list(media.preview_dir().glob("*.jpg"))) == 2


# ── MCP surface ───────────────────────────────────────────────────────────

def test_tool_surface_is_read_only(root):
    m = create_server()
    tools = _run(m.list_tools())
    assert {t.name for t in tools} == {"query_sightings", "get_images", "get_image_urls", "get_individuals", "get_claims",
                                       "get_open_items", "get_stations", "get_schema"}
    assert all(t.annotations and t.annotations.readOnlyHint and not t.annotations.destructiveHint
               for t in tools)
    for t in tools:
        assert "Example" in t.description
    assert "summary" in next(t for t in tools if t.name == "query_sightings").description


def test_query_tool_accepts_lists_and_strings(root):
    a = _json("query_sightings", species=["bear", "deer"], response_mode="by_day")
    b = _json("query_sightings", species="bear,deer", response_mode="by_day")
    assert a["sighting_count"] == b["sighting_count"] == 5
    with pytest.raises(ToolError, match="requires an explicit limit"):
        _tool("query_sightings", response_mode="rows")


def test_get_images_batch_with_references(root):
    out = _tool("get_images", sighting_id=4, include_references_for_individual=True)
    head = json.loads(out[0].text)
    # row 4 has no media -> error entry; Al's reference (row 2, from notes) still arrives
    assert head["individual"] == "Al" and head["returned"] == 1 and head["truncated"] is False
    assert head["images"][0]["error"] == "sighting has no media"
    ref = head["images"][1]
    assert ref["is_reference_frame"] and ref["sighting_id"] == 2 and ref["confidence"] == "confirmed"
    assert ref["light"] == "civil_twilight" and ref["content_position"] == 1
    assert out[1].type == "image" and out[1].mimeType == "image/jpeg"

    out = _tool("get_images", individual="al", reference_frames_only=True, max_px=300)
    head = json.loads(out[0].text)
    assert head["returned"] == 1 and head["max_px"] == 300 and head["images"][0]["is_reference_frame"]

    out = _tool("get_images", sighting_ids=[1, 5, 999])
    head = json.loads(out[0].text)
    assert head["returned"] == 1
    assert [i.get("error") for i in head["images"]] == [None, "media[0] path is outside the assets dir", "no such sighting"]
    assert len(out) == 2

    with pytest.raises(ToolError, match="at most 6"):
        _tool("get_images", sighting_ids=[1, 2, 3, 4, 5, 6, 7])
    with pytest.raises(ToolError, match="no individual is attributed"):
        _tool("get_images", sighting_id=1, include_references_for_individual=True)
    with pytest.raises(ToolError, match="give sighting_ids"):
        _tool("get_images")


def test_get_images_video_poster(root):
    if not root["have_video"]:
        pytest.skip("ffmpeg not available")
    out = _tool("get_images", sighting_id=3)
    head = json.loads(out[0].text)
    assert head["images"][0]["kind"] == "video_poster" and out[1].type == "image"


def test_resources_serve_bytes(root):
    m = create_server()
    r = list(_run(m.read_resource("wildlife://sightings/1/media/0/preview.jpg")))
    assert r[0].mime_type == "image/jpeg" and r[0].content[:2] == b"\xff\xd8"
    r = list(_run(m.read_resource("wildlife://sightings/1/media/0/original.jpg")))
    assert r[0].content == root["img1"].read_bytes()
    with pytest.raises(Exception, match="not mp4"):
        _run(m.read_resource("wildlife://sightings/1/media/0/original.mp4"))


# ── tls / entrypoint ──────────────────────────────────────────────────────

def test_selfsigned_cert_covers_host_and_ips(tmp_path, monkeypatch):
    from hoseid.mcp_wildlife import tls
    monkeypatch.setenv("WILDLIFE_MCP_CERTFILE", str(tmp_path / "c.crt"))
    monkeypatch.setenv("WILDLIFE_MCP_KEYFILE", str(tmp_path / "c.key"))
    monkeypatch.setenv("WILDLIFE_MCP_SAN_EXTRA", "10.0.0.9, mcp.example")
    san = tls.san_entries()
    assert "DNS:localhost" in san and "IP:127.0.0.1" in san
    assert "IP:10.0.0.9" in san and "DNS:mcp.example" in san
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    cert, key = tls.ensure_selfsigned_cert()
    assert cert.exists() and key.exists() and (key.stat().st_mode & 0o077) == 0
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                         capture_output=True, text=True).stdout
    assert "10.0.0.9" in out and "mcp.example" in out
    eku = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "extendedKeyUsage"],
                         capture_output=True, text=True).stdout
    assert "TLS Web Server Authentication" in eku   # Apple rejects TLS certs without serverAuth
    assert tls.ensure_selfsigned_cert() == (cert, key)   # idempotent


def test_build_servers_tls_and_loopback(tmp_path, monkeypatch, root):
    from hoseid.mcp_wildlife import app as entry
    monkeypatch.setenv("WILDLIFE_MCP_CERTFILE", str(tmp_path / "c.crt"))
    monkeypatch.setenv("WILDLIFE_MCP_KEYFILE", str(tmp_path / "c.key"))
    monkeypatch.setenv("WILDLIFE_MCP_PORT", "18853")
    monkeypatch.setenv("WILDLIFE_MCP_HTTP_PORT", "18854")
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    servers = entry.build_servers()   # default: plain http, one listener
    assert [(s.config.host, s.config.port, bool(s.config.ssl_certfile)) for s in servers] == \
        [("0.0.0.0", 18853, False)]
    monkeypatch.setenv("WILDLIFE_MCP_TLS", "1")
    servers = entry.build_servers()
    assert [(s.config.host, s.config.port, bool(s.config.ssl_certfile)) for s in servers] == \
        [("0.0.0.0", 18853, True), ("127.0.0.1", 18854, False)]


# ── get_image_urls ────────────────────────────────────────────────────────

def test_get_image_urls_by_filters_reports_kind_url_and_paging(root):
    d = _json("get_image_urls", species=["bear", "deer", "jackrabbit"])
    assert d["unit"].startswith("media files") and d["selection"] == "filters"
    assert d["filters_applied"]["species"] == ["bear", "deer", "jackrabbit"] and "light_model" in d
    # rows 1,2,(3 if video),5(2 files, both broken),6 have media; row 4 has none
    ids = [(e["sighting_id"], e["media_index"]) for e in d["entries"]]
    assert ids[0] == (5, 0) and ids[-1] == (6, 0)          # newest first
    assert d["sightings_without_media"] == 1
    by = {(e["sighting_id"], e["media_index"]): e for e in d["entries"]}
    assert by[(1, 0)]["url"].endswith("/images/ab/cd/abcd1.jpg") and by[(1, 0)]["kind"] == "image"
    assert by[(1, 0)]["bytes"] == root["img1"].stat().st_size and "path" not in by[(1, 0)]
    assert by[(2, 0)]["individual_confidence"] == "confirmed" and by[(2, 0)]["light"] == "civil_twilight"
    assert by[(5, 0)]["url"] is None and "outside" in by[(5, 0)]["error"]
    assert by[(5, 1)]["url"] is None and "missing" in by[(5, 1)]["error"]
    assert by[(5, 0)]["media_total"] == 2
    if root["have_video"]:
        assert by[(3, 0)]["kind"] == "video" and by[(3, 0)]["url"].endswith(".mp4")
    total = d["total_matching"]
    page = _json("get_image_urls", species=["bear", "deer", "jackrabbit"], limit=2, offset=1)
    assert page["returned"] == 2 and page["truncated"] is True and page["total_matching"] == total
    assert [e["sighting_id"] for e in page["entries"]] == [5, 3 if root["have_video"] else 2][:2] or page["entries"][0]["sighting_id"] == 5


def test_get_image_urls_by_ids_and_reference_frames(root):
    d = _json("get_image_urls", sighting_ids=[6, 999, 1])
    assert [e["sighting_id"] for e in d["entries"]] == [6, 1]
    assert d["filters_applied"] == {"sighting_ids": [6, 999, 1], "sighting_ids_not_found": [999]}
    r = _json("get_image_urls", individual="al", reference_frames_only=True)
    assert r["selection"] == "reference_frames" and r["returned"] == 1
    assert r["entries"][0]["is_reference_frame"] and r["entries"][0]["sighting_id"] == 2
    with pytest.raises(ToolError, match="requires individual"):
        _tool("get_image_urls", reference_frames_only=True)
    with pytest.raises(ToolError, match="1..500"):
        _tool("get_image_urls", limit=1000)
    assert _json("get_image_urls", individual="Al")["returned"] == 1   # plain individual filter


def test_get_image_urls_payload_cap(root, monkeypatch):
    monkeypatch.setattr(queries, "URLS_PAYLOAD_CAP_BYTES", 2500)
    d = _json("get_image_urls")
    assert d["truncated"] is True and d["returned"] < d["total_matching"] and "payload cap" in d["truncated_reason"]
