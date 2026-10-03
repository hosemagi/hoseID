"""Read-only query layer over tags/wildlife.db, designed for an LLM caller.

Design rules (from the 2026-09-12 brief):
  * aggregate by default; rows only on request and always under a limit
  * every count names its unit: `sightings` (log rows), `captures`
    (distinct camera files; a row without an asset id counts as its own
    capture), `encounters` (derived visit chains from derived/encounters.db)
  * never truncate silently: truncated / returned / total_matching
  * carry individual_confidence and source through, never flattened

Pure sqlite + stdlib; the DB is opened mode=ro so read-only is enforced at
the connection. Rows are pulled once per call and aggregated in Python
because light classification (sunrise/sunset per date) is computed, not
stored; the log is ~1k rows so this is milliseconds.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from hoseid import paths
from hoseid.mcp_wildlife import solar

# local-hour buckets, inclusive; night wraps midnight. Same buckets as Sage's
# wildlife_query (hoselm voice/wildlife_actions.py) so the two answer alike.
TIME_OF_DAY: dict[str, tuple[int, int]] = {
    "night": (22, 4), "morning": (5, 11), "midday": (12, 16), "evening": (17, 21),
}
LIGHT_VALUES = ("daylight", "civil_twilight", "dark")
CONFIDENCE_VALUES = ("confirmed", "assumed", "by_elimination", "unconfirmed")
RESPONSE_MODES = ("summary", "by_day", "histogram", "rows")
HISTOGRAM_BINS = ("hour", "time_of_day", "light")
ROWS_MAX_LIMIT = 200
BREAKDOWN_CAP = 30
BY_DAY_CAP = 200
NOTES_CAP = 240
PAYLOAD_CAP_BYTES = 60_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

UNIT_NOTES = {
    "sightings": "log rows; one row per species per reviewed capture, or one hand-logged event",
    "captures": "distinct camera files behind those rows; a row with no capture link counts as one",
    "encounters": "derived visit chains (same species, consecutive sightings within the gap; "
                  "big-animal species only) from derived/encounters.db",
}


class QueryError(ValueError):
    """Bad filter input. Surfaces to the client as a tool error, not a crash."""


def wildlife_db() -> Path:
    return paths.tags_dir() / "wildlife.db"


def encounters_db() -> Path:
    return paths.derived_dir() / "encounters.db"


def connect(db: Path | None = None) -> sqlite3.Connection:
    p = db or wildlife_db()
    if not p.exists():
        raise FileNotFoundError(f"wildlife log not found: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── filter parsing ────────────────────────────────────────────────────────

def _date(v: Any, what: str) -> str | None:
    if v in (None, ""):
        return None
    v = str(v).strip()
    if not _DATE_RE.match(v):
        raise QueryError(f"{what} must be YYYY-MM-DD, got {v!r}")
    return v


def _hour(v: Any, what: str) -> int | None:
    if v in (None, ""):
        return None
    try:
        h = int(v)
    except (TypeError, ValueError):
        raise QueryError(f"{what} must be an integer hour 0-23, got {v!r}")
    if not 0 <= h <= 23:
        raise QueryError(f"{what} must be 0-23, got {h}")
    return h


def _list(v: Any) -> list[str]:
    """Accept a list, a comma-separated string, or None -> lowercase list."""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [str(s).strip().lower() for s in v if s is not None and str(s).strip()]


def _bool(v: Any, what: str) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise QueryError(f"{what} must be true or false, got {v!r}")


def _hour_clause(lo: int, hi: int) -> str:
    h = "CAST(substr(time, 1, 2) AS INTEGER)"
    return f"({h} BETWEEN {lo} AND {hi})" if lo <= hi else f"({h} >= {lo} OR {h} <= {hi})"


def resolve_station(conn: sqlite3.Connection, name: str) -> str:
    """Station name or alias (case-insensitive) -> canonical stations.name;
    unknown names pass through so free-text stations (deck, CB7) still match."""
    want = name.strip().lower()
    for r in conn.execute("SELECT name, aliases FROM stations"):
        if r["name"].lower() == want:
            return r["name"]
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        if any(a.lower() == want for a in aliases):
            return r["name"]
    return name.strip()


def build_where(conn: sqlite3.Connection, f: dict[str, Any]) -> tuple[str, list, dict]:
    """Public filters -> (WHERE, params, applied). Light filters are applied
    later in Python (see fetch_rows); `applied` echoes what was used."""
    clauses: list[str] = []
    params: list = []
    applied: dict[str, Any] = {}

    lo = _date(f.get("start_date"), "start_date")
    hi = _date(f.get("end_date"), "end_date")
    if lo and hi and lo > hi:
        raise QueryError(f"start_date {lo} is after end_date {hi}")
    if lo:
        clauses.append("date >= ?"); params.append(lo); applied["start_date"] = lo
    if hi:
        clauses.append("date <= ?"); params.append(hi); applied["end_date"] = hi

    for col in ("species", "category", "source"):
        vals = _list(f.get(col))
        if vals:
            clauses.append(f"lower({col}) IN ({','.join('?' * len(vals))})")
            params.extend(vals)
            applied[col] = vals

    conf = _list(f.get("confidence"))
    if conf:
        bad = [c for c in conf if c not in CONFIDENCE_VALUES and c != "none"]
        if bad:
            raise QueryError(f"confidence must be from {list(CONFIDENCE_VALUES)} (or 'none' for "
                             f"unattributed rows), got {bad}")
        parts = []
        real = [c for c in conf if c != "none"]
        if real:
            parts.append(f"individual_confidence IN ({','.join('?' * len(real))})")
            params.extend(real)
        if "none" in conf:
            parts.append("individual_confidence IS NULL")
        clauses.append("(" + " OR ".join(parts) + ")")
        applied["confidence"] = conf

    stations = [resolve_station(conn, s) for s in _list(f.get("station"))]
    if stations:
        clauses.append(f"lower(station) IN ({','.join('?' * len(stations))})")
        params.extend(s.lower() for s in stations)
        applied["station"] = stations

    individuals = _list(f.get("individual"))
    if individuals:
        clauses.append(f"lower(individual) IN ({','.join('?' * len(individuals))})")
        params.extend(individuals)
        applied["individual"] = individuals

    tod = _list(f.get("time_of_day"))
    hf = _hour(f.get("hour_from"), "hour_from")
    ht = _hour(f.get("hour_to"), "hour_to")
    if (hf is None) != (ht is None):
        raise QueryError("hour_from and hour_to must be given together")
    if tod or hf is not None:
        parts = []
        for name in tod:
            if name not in TIME_OF_DAY:
                raise QueryError(f"time_of_day must be from {sorted(TIME_OF_DAY)}, got {name!r}")
            parts.append(_hour_clause(*TIME_OF_DAY[name]))
        if hf is not None:
            parts.append(_hour_clause(hf, ht))
            applied["hour_from"], applied["hour_to"] = hf, ht
        if tod:
            applied["time_of_day"] = tod
        clauses.append("(time IS NOT NULL AND (" + " OR ".join(parts) + "))")

    auto = _bool(f.get("auto"), "auto")
    if auto is not None:
        clauses.append("auto = 1" if auto else "auto = 0"); applied["auto"] = auto
    hm = _bool(f.get("has_media"), "has_media")
    if hm is not None:
        clauses.append("media_refs != '[]'" if hm else "media_refs = '[]'"); applied["has_media"] = hm
    if _bool(f.get("individual_only"), "individual_only"):
        clauses.append("individual IS NOT NULL"); applied["individual_only"] = True

    # light filters: validated here, applied in Python
    light = _list(f.get("light"))
    bad = [v for v in light if v not in LIGHT_VALUES]
    if bad:
        raise QueryError(f"light must be from {list(LIGHT_VALUES)}, got {bad}")
    if light:
        applied["light"] = light
    legal = _bool(f.get("legal_light"), "legal_light")
    if legal is not None:
        applied["legal_light"] = legal

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, applied


# ── row shaping ───────────────────────────────────────────────────────────

def weather_summary(js: str | None) -> str | None:
    if not js:
        return None
    try:
        w = json.loads(js)
    except json.JSONDecodeError:
        return None
    parts = []
    if w.get("temp_f") is not None:
        parts.append(f"{w['temp_f']:.0f}F")
    if w.get("wind_mph") is not None:
        parts.append(f"wind {w.get('wind_dir') or ''} {w['wind_mph']:.0f} mph".replace("  ", " "))
    if w.get("pressure_inhg") is not None:
        d = w.get("pressure_change_3h_inhg")
        trend = "" if d is None else (" rising" if d > 0.02 else " falling" if d < -0.02 else " steady")
        parts.append(f"{w['pressure_inhg']:.2f} inHg{trend}")
    if w.get("precip_in"):
        parts.append(f"{w['precip_in']:.2f} in precip")
    return ", ".join(parts) or None


def media_entries(row: sqlite3.Row | dict) -> list[dict]:
    try:
        refs = json.loads(row["media_refs"] or "[]")
    except (json.JSONDecodeError, KeyError, TypeError):
        refs = []
    return [{"index": i, "kind": m.get("kind") or "image", "ref": m.get("ref"),
             "path": m.get("path"), "at": m.get("at")}
            for i, m in enumerate(refs) if isinstance(m, dict)]


def time_of_day_of(t: str | None) -> str | None:
    try:
        h = int(str(t)[:2])
    except (TypeError, ValueError):
        return None
    if not 0 <= h < 24:
        return None
    for name, (lo, hi) in TIME_OF_DAY.items():
        if (lo <= hi and lo <= h <= hi) or (lo > hi and (h >= lo or h <= hi)):
            return name
    return None


def _capture_key(row: sqlite3.Row) -> str:
    return row["capture_asset_id"] or f"row:{row['sighting_id']}"


def enrich(row: sqlite3.Row, lat: float, lon: float) -> dict[str, Any]:
    light, legal = solar.classify(row["date"], row["time"], lat, lon)
    media = media_entries(row)
    return {
        "sighting_id": row["sighting_id"],
        "date": row["date"],
        "time": row["time"],
        "time_of_day": time_of_day_of(row["time"]),
        "light": light,
        "legal_light": legal,
        "station": row["station"],
        "species": row["species"],
        "category": row["category"],
        "individual": row["individual"],
        "individual_confidence": row["individual_confidence"],
        "count": row["count"],
        "count_is_visits": bool(row["count_is_visits"]),
        "source": row["source"],
        "auto": bool(row["auto"]),
        "harvest": bool(row["harvest"]),
        "notes": row["notes"] or "",
        "weather": weather_summary(row["weather"]),
        "weather_raw": row["weather"],
        "capture_asset_id": row["capture_asset_id"],
        "_capture_key": _capture_key(row),
        "media": media,
    }


def fetch_rows(filters: dict[str, Any], db: Path | None = None) -> tuple[list[dict], dict, dict]:
    """All matching rows, enriched, oldest first. Returns (rows, applied, light_model)."""
    lat, lon, src = solar.property_coords()
    conn = connect(db)
    try:
        where, params, applied = build_where(conn, filters)
        raw = conn.execute(
            f"SELECT * FROM sightings{where} ORDER BY date, time IS NULL, time, sighting_id",
            params).fetchall()
    finally:
        conn.close()
    rows = [enrich(r, lat, lon) for r in raw]
    if applied.get("light"):
        want = set(applied["light"])
        rows = [r for r in rows if r["light"] in want]
    if applied.get("legal_light") is not None:
        rows = [r for r in rows if r["legal_light"] is applied["legal_light"]]
    model = {"lat": lat, "lon": lon, "coords_source": src, "tz": str(solar.LOCAL_TZ),
             "method": "computed per date: sunrise/sunset (zenith 90.833), civil twilight "
                       "(zenith 96); legal_light = sunrise-30min..sunset+30min",
             "time_of_day_buckets": {k: f"{lo:02d}:00-{hi:02d}:59" for k, (lo, hi) in TIME_OF_DAY.items()}}
    return rows, applied, model


# ── aggregation helpers ───────────────────────────────────────────────────

def _counts(rows: list[dict]) -> dict[str, int]:
    return {"sighting_count": len(rows),
            "capture_count": len({r["_capture_key"] for r in rows})}


def _breakdown(rows: list[dict], key, label: str, cap: int = BREAKDOWN_CAP,
               extra=None) -> dict[str, Any]:
    groups: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    items = []
    for k, rs in ordered[:cap]:
        item = {label: k, **_counts(rs)}
        if extra:
            item.update(extra(rs))
        items.append(item)
    return {"items": items, "groups_total": len(ordered), "groups_returned": len(items),
            "truncated": len(ordered) > cap}


def _conf_split(rs: list[dict]) -> dict:
    return {"by_confidence": dict(Counter(r["individual_confidence"] or "none" for r in rs)),
            "by_source": dict(Counter(r["source"] for r in rs))}


def _encounter_counts(rows: list[dict], db: Path | None = None) -> dict[str, Any] | None:
    p = db or encounters_db()
    if not p.exists():
        return None
    ids = {r["sighting_id"] for r in rows}
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        n = 0
        multi = 0
        by_species: Counter = Counter()
        for e in conn.execute("SELECT species, multi_cam, sighting_ids FROM encounters"):
            try:
                members = set(json.loads(e["sighting_ids"]))
            except json.JSONDecodeError:
                continue
            if members & ids:
                n += 1
                multi += int(e["multi_cam"])
                by_species[e["species"]] += 1
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {"encounter_count": n, "multi_camera_encounters": multi,
            "by_species": dict(by_species.most_common()),
            "unit": "encounters", "note": UNIT_NOTES["encounters"] +
            "; an encounter is counted if ANY member sighting matched the filters",
            "meta": meta}


def _envelope(applied: dict, model: dict, mode: str, rows: list[dict]) -> dict[str, Any]:
    return {
        "response_mode": mode,
        "unit": "sightings",
        "units": UNIT_NOTES,
        "total_matching": len(rows),
        **_counts(rows),
        "date_range": {"first": rows[0]["date"], "last": rows[-1]["date"]} if rows else None,
        "filters_applied": applied,
        "light_model": model,
    }


# ── public: query_sightings ───────────────────────────────────────────────

def query_sightings(filters: dict[str, Any], response_mode: str = "summary",
                    histogram_bin: str = "hour", limit: int | None = None, offset: int = 0,
                    order: str = "desc", db: Path | None = None) -> dict[str, Any]:
    mode = (response_mode or "summary").lower()
    if mode not in RESPONSE_MODES:
        raise QueryError(f"response_mode must be one of {list(RESPONSE_MODES)}, got {mode!r}")
    if mode == "rows":
        if limit is None:
            raise QueryError("response_mode='rows' requires an explicit limit (max "
                             f"{ROWS_MAX_LIMIT}); use summary/by_day/histogram for counts")
        try:
            limit = int(limit)
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            raise QueryError("limit and offset must be integers")
        if limit < 1 or limit > ROWS_MAX_LIMIT:
            raise QueryError(f"limit must be 1..{ROWS_MAX_LIMIT}, got {limit}")
    if histogram_bin not in HISTOGRAM_BINS:
        raise QueryError(f"histogram_bin must be one of {list(HISTOGRAM_BINS)}, got {histogram_bin!r}")

    rows, applied, model = fetch_rows(filters, db)
    out = _envelope(applied, model, mode, rows)

    if mode == "summary":
        out["by_species"] = _breakdown(rows, lambda r: r["species"], "species")
        out["by_station"] = _breakdown(rows, lambda r: r["station"], "station")
        out["by_individual"] = _breakdown(
            [r for r in rows if r["individual"]], lambda r: r["individual"], "individual",
            extra=_conf_split)
        out["unattributed_sightings"] = sum(1 for r in rows if not r["individual"])
        out["by_source"] = _breakdown(rows, lambda r: r["source"], "source")
        out["by_confidence"] = _breakdown(
            rows, lambda r: r["individual_confidence"] or "none", "individual_confidence")
        out["by_category"] = _breakdown(rows, lambda r: r["category"], "category")
        out["by_light"] = _breakdown(rows, lambda r: r["light"] or "untimed", "light",
                                     extra=lambda rs: {"legal_light_sightings":
                                                       sum(1 for r in rs if r["legal_light"])})
        out["auto_sightings"] = sum(1 for r in rows if r["auto"])
        enc = _encounter_counts(rows)
        if enc is not None:
            out["encounters"] = enc
        return out

    if mode == "by_day":
        days = _breakdown(rows, lambda r: r["date"], "date", cap=10**9,
                          extra=lambda rs: {"legal_light_sightings": sum(1 for r in rs if r["legal_light"]),
                                            "species": dict(Counter(r["species"] for r in rs))})
        items = sorted(days["items"], key=lambda d: d["date"])
        if order.lower().startswith("d"):
            items.reverse()
        truncated = len(items) > BY_DAY_CAP
        out["days"] = items[:BY_DAY_CAP]
        out["days_total"] = len(items)
        out["days_returned"] = min(len(items), BY_DAY_CAP)
        out["truncated"] = truncated
        return out

    if mode == "histogram":
        out["histogram_bin"] = histogram_bin
        timed = [r for r in rows if r["time"] is not None and r["time_of_day"] is not None]
        out["untimed_sightings"] = len(rows) - len(timed)
        if histogram_bin == "hour":
            bins = []
            groups: dict[int, list[dict]] = defaultdict(list)
            for r in timed:
                groups[int(r["time"][:2])].append(r)
            for h in range(24):
                rs = groups.get(h, [])
                bins.append({"hour": h, **_counts(rs),
                             "legal_light_sightings": sum(1 for r in rs if r["legal_light"]),
                             "light": dict(Counter(r["light"] for r in rs))})
        elif histogram_bin == "time_of_day":
            bins = []
            for name in TIME_OF_DAY:
                rs = [r for r in timed if r["time_of_day"] == name]
                bins.append({"time_of_day": name, "hours": model["time_of_day_buckets"][name],
                             **_counts(rs),
                             "legal_light_sightings": sum(1 for r in rs if r["legal_light"]),
                             "light": dict(Counter(r["light"] for r in rs))})
        else:
            bins = []
            for name in LIGHT_VALUES:
                rs = [r for r in timed if r["light"] == name]
                bins.append({"light": name, **_counts(rs),
                             "legal_light_sightings": sum(1 for r in rs if r["legal_light"]),
                             "species": dict(Counter(r["species"] for r in rs).most_common(10))})
        out["bins"] = bins
        if rows:
            out["sun_times_first_day"] = solar.sun_times(
                solar.date.fromisoformat(rows[0]["date"]), model["lat"], model["lon"]).as_dict()
            out["sun_times_last_day"] = solar.sun_times(
                solar.date.fromisoformat(rows[-1]["date"]), model["lat"], model["lon"]).as_dict()
        return out

    # rows
    ordered = rows if order.lower().startswith("a") else list(reversed(rows))
    page = ordered[offset:offset + limit]
    out["order"] = "asc" if order.lower().startswith("a") else "desc"
    out["offset"] = offset
    out["limit"] = limit
    out["rows"] = [_public_row(r) for r in page]
    out["returned"] = len(page)
    out["truncated"] = offset + len(page) < len(rows)
    return bound_payload(out, "rows")


def _public_row(r: dict) -> dict:
    notes = r["notes"]
    d = {k: v for k, v in r.items() if k not in ("_capture_key", "weather_raw", "media")}
    d["notes"] = notes if len(notes) <= NOTES_CAP else notes[:NOTES_CAP] + "…"
    d["notes_truncated"] = len(notes) > NOTES_CAP
    d["media"] = {"images": sum(m["kind"] == "image" for m in r["media"]),
                  "videos": sum(m["kind"] == "video" for m in r["media"])}
    return d


def bound_payload(out: dict, list_key: str, cap: int | None = None) -> dict:
    """Drop trailing list items until the JSON fits the cap; flag it."""
    cap = PAYLOAD_CAP_BYTES if cap is None else cap
    while len(json.dumps(out)) > cap and out.get(list_key):
        out[list_key].pop()
        out["returned"] = len(out[list_key])
        out["truncated"] = True
        out["truncated_reason"] = f"payload cap {cap} bytes; page with offset"
    return out


# ── public: sighting lookup for images ────────────────────────────────────

def get_sightings_by_id(ids: list[int], db: Path | None = None) -> dict[int, dict]:
    if not ids:
        return {}
    lat, lon, _ = solar.property_coords()
    conn = connect(db)
    try:
        rows = conn.execute(
            f"SELECT * FROM sightings WHERE sighting_id IN ({','.join('?' * len(ids))})",
            [int(i) for i in ids]).fetchall()
    finally:
        conn.close()
    return {r["sighting_id"]: enrich(r, lat, lon) for r in rows}


# ── public: registries ────────────────────────────────────────────────────

def reference_frames_file() -> Path:
    return paths.tags_dir() / "reference_frames.json"


def load_reference_frames(db: Path | None = None) -> dict[str, dict]:
    """Per individual: designated reference frames + appearance changes.

    Two sources, merged: the curated tags/reference_frames.json (owner-edited),
    and sightings whose notes call themselves a reference frame (derived,
    source='notes'). Curated entries win on conflict."""
    out: dict[str, dict] = defaultdict(lambda: {"reference_frames": [], "appearance_changes": []})
    conn = connect(db)
    try:
        for r in conn.execute(
                "SELECT sighting_id, date, time, station, individual, individual_confidence, "
                "media_refs, notes FROM sightings WHERE individual IS NOT NULL "
                "AND lower(notes) LIKE '%reference%' AND media_refs != '[]'"):
            for m in media_entries(r):
                out[r["individual"]]["reference_frames"].append({
                    "sighting_id": r["sighting_id"], "media_index": m["index"], "date": r["date"],
                    "time": r["time"], "station": r["station"],
                    "confidence": r["individual_confidence"], "source": "notes",
                    "note": (r["notes"] or "")[:160]})
    finally:
        conn.close()
    p = reference_frames_file()
    if p.exists():
        try:
            cur = json.loads(p.read_text()).get("individuals", {})
        except (json.JSONDecodeError, AttributeError):
            cur = {}
        for name, spec in cur.items():
            frames = out[name]["reference_frames"]
            for f in spec.get("reference_frames", []):
                key = (f.get("sighting_id"), f.get("media_index", 0))
                frames[:] = [x for x in frames if (x["sighting_id"], x["media_index"]) != key]
                frames.append({"sighting_id": f.get("sighting_id"),
                               "media_index": f.get("media_index", 0),
                               "date": f.get("date"), "time": f.get("time"),
                               "station": f.get("station"), "confidence": f.get("confidence"),
                               "source": "curated", "note": f.get("note", "")})
            out[name]["appearance_changes"].extend(spec.get("appearance_changes", []))
    # fill date/station for curated frames from the DB
    need = [f for v in out.values() for f in v["reference_frames"] if not f.get("date")]
    if need:
        by_id = get_sightings_by_id([f["sighting_id"] for f in need], db)
        for f in need:
            s = by_id.get(f["sighting_id"])
            if s:
                f.update({"date": s["date"], "time": s["time"], "station": s["station"],
                          "confidence": s["individual_confidence"]})
    return dict(out)


def get_individuals(db: Path | None = None) -> dict[str, Any]:
    refs = load_reference_frames(db)
    conn = connect(db)
    try:
        tally = {r["individual"]: dict(r) for r in conn.execute("SELECT * FROM individual_tally")}
        stations = defaultdict(list)
        for r in conn.execute("SELECT individual, station, COUNT(*) n FROM sightings "
                              "WHERE individual IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC"):
            stations[r["individual"]].append({"station": r["station"], "sightings": r["n"]})
        conf = defaultdict(dict)
        for r in conn.execute("SELECT individual, individual_confidence c, COUNT(*) n FROM sightings "
                              "WHERE individual IS NOT NULL GROUP BY 1, 2"):
            conf[r["individual"]][r["c"] or "none"] = r["n"]
        items = []
        for r in conn.execute("SELECT * FROM individuals ORDER BY species, name"):
            t = tally.get(r["name"], {})
            ref = refs.get(r["name"], {"reference_frames": [], "appearance_changes": []})
            frames = sorted(ref["reference_frames"], key=lambda f: (f.get("date") or "", f["sighting_id"]))
            last_ref = max((f.get("date") or "" for f in frames), default=None)
            stale = [c for c in ref["appearance_changes"]
                     if last_ref and c.get("date") and c["date"] > last_ref]
            items.append({
                "name": r["name"], "species": r["species"], "status": r["status"],
                "first_seen": r["first_seen"], "description": r["description"],
                "id_basis": r["id_basis"], "notes": r["notes"],
                "first_logged": t.get("first_logged"), "last_logged": t.get("last_logged"),
                "capture_events": t.get("capture_events", 0),
                "direct_visuals": t.get("direct_visuals", 0),
                "station_count": t.get("stations_hit", 0),
                "stations": stations.get(r["name"], []),
                "sightings_by_confidence": conf.get(r["name"], {}),
                "reference_frames": frames,
                "reference_frame_count": len(frames),
                "appearance_changes": ref["appearance_changes"],
                "reference_frames_stale": bool(stale) or (not frames),
                "reference_frames_stale_reason": (
                    "no designated reference frames" if not frames else
                    "; ".join(f"{c.get('date')}: {c.get('note', '')}" for c in stale) or None),
            })
    finally:
        conn.close()
    return {"unit": "capture_events = sightings naming this individual",
            "reference_frames_source": f"{reference_frames_file()} (curated) + sightings whose "
                                       "notes say 'reference' (derived)",
            "individuals": items}


def get_claims(status: list[str] | str | None = None, db: Path | None = None) -> dict[str, Any]:
    want = _list(status)
    conn = connect(db)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY claim_id")]
    finally:
        conn.close()
    if want:
        rows = [r for r in rows if r["status"] in want]
    for r in rows:
        r["do_not_reraise"] = "do not re-raise" in (r["notes"] or "").lower()
    return {"claims": rows, "returned": len(rows), "filters_applied": {"status": want} if want else {},
            "statuses": ["open", "supported", "withdrawn", "falsified", "resolved"],
            "note": "settled = supported/withdrawn/falsified/resolved; do_not_reraise flags the "
                    "owner's explicit closure markers"}


def get_open_items(status: list[str] | str | None = None, db: Path | None = None) -> dict[str, Any]:
    want = _list(status)
    conn = connect(db)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM open_items ORDER BY item_id")]
    finally:
        conn.close()
    if want:
        rows = [r for r in rows if r["status"] in want]
    return {"open_items": rows, "returned": len(rows),
            "filters_applied": {"status": want} if want else {}, "statuses": ["open", "done", "dropped"]}


def get_stations(db: Path | None = None) -> dict[str, Any]:
    conn = connect(db)
    try:
        activity: dict[str, dict] = {}
        for r in conn.execute("SELECT station, COUNT(*) n, COUNT(DISTINCT COALESCE(capture_asset_id, "
                              "'row:'||sighting_id)) c, MIN(date) f, MAX(date) l FROM sightings GROUP BY 1"):
            activity[r["station"]] = {"sighting_count": r["n"], "capture_count": r["c"],
                                      "first": r["f"], "last": r["l"], "species": {}}
        for r in conn.execute("SELECT station, species, COUNT(*) n FROM sightings GROUP BY 1, 2 "
                              "ORDER BY n DESC"):
            sp = activity[r["station"]]["species"]
            if len(sp) < 12:
                sp[r["species"]] = r["n"]
        items, seen = [], set()
        for r in conn.execute("SELECT * FROM stations ORDER BY name"):
            try:
                aliases = json.loads(r["aliases"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            a = activity.get(r["name"], {"sighting_count": 0, "capture_count": 0, "first": None,
                                         "last": None, "species": {}})
            seen.add(r["name"])
            items.append({"name": r["name"], "camera": r["camera"], "aliases": aliases,
                          "description": r["description"], "notes": r["notes"], "unmapped": False,
                          **a})
        for name, a in sorted(activity.items(), key=lambda kv: -kv[1]["sighting_count"]):
            if name not in seen:
                items.append({"name": name or "(blank)", "camera": None, "aliases": [],
                              "description": "", "unmapped": True,
                              "notes": "empty station string on these rows" if not name else "",
                              **a})
    finally:
        conn.close()
    unmapped = [i["name"] for i in items if i["unmapped"]]
    return {"unit": "sightings (capture_count alongside)", "stations": items,
            "unmapped_stations": unmapped,
            "note": "unmapped = appears in sightings but has no registry entry, description or "
                    "location; treat its placement as unknown, not as absent"}


def get_schema(db: Path | None = None) -> dict[str, Any]:
    conn = connect(db)
    try:
        def vals(col):
            return [{col: r[0], "sightings": r[1]} for r in conn.execute(
                f"SELECT {col}, COUNT(*) FROM sightings WHERE {col} IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC")]
        species = vals("species")
        categories = vals("category")
        confidence = vals("individual_confidence")
        sources = vals("source")
        stations = vals("station")
        individuals = vals("individual")
        registered = {r[0] for r in conn.execute("SELECT name FROM stations")}
        cat_names = {c["category"] for c in categories}
        untimed = conn.execute("SELECT COUNT(*) FROM sightings WHERE time IS NULL").fetchone()[0]
        no_media = conn.execute("SELECT COUNT(*) FROM sightings WHERE media_refs = '[]'").fetchone()[0]
        dupes = [dict(r) for r in conn.execute(
            "SELECT a.sighting_id AS other_animal_id, b.sighting_id AS specific_id, a.date, a.time, "
            "a.station, b.species FROM sightings a JOIN sightings b ON a.date = b.date "
            "AND a.time = b.time AND a.station = b.station AND a.species = 'other-animal' "
            "AND b.species != 'other-animal' ORDER BY a.date LIMIT 20")]
    finally:
        conn.close()

    issues = []
    names = [s["species"] for s in species]
    for a in names:
        for b in names:
            if a != b and (b.endswith("_" + a) or b.endswith("-" + a)):
                issues.append({"kind": "split_species", "values": [a, b],
                               "note": f"'{b}' and '{a}' are separate species values; almost "
                                       "certainly one animal. Query both."})
    for s in names:
        if "disturbance" in s or s in ("other", "predator", "small_game", "bird"):
            issues.append({"kind": "category_as_species", "values": [s],
                           "note": f"'{s}' is a category-style value stored as a species"})
    blank = next((s for s in stations if s["station"] == ""), None)
    if blank:
        issues.append({"kind": "blank_station", "count": blank["sightings"],
                       "note": "sightings with an empty station string; location unknown"})
    if dupes:
        issues.append({"kind": "other_animal_duplicates", "count": len(dupes), "examples": dupes,
                       "note": "an 'other-animal' row shares date/time/station with a "
                               "specific-species row; counting both double-counts the capture"})
    return {
        "species": species, "categories": categories,
        "confidence_levels": list(CONFIDENCE_VALUES) + ["none (unattributed)"],
        "confidence_in_use": confidence, "sources": sources,
        "stations": [{**s, "registered": s["station"] in registered} for s in stations],
        "individuals": individuals,
        "time_of_day": {k: f"{lo:02d}:00-{hi:02d}:59" for k, (lo, hi) in TIME_OF_DAY.items()},
        "light": list(LIGHT_VALUES), "response_modes": list(RESPONSE_MODES),
        "histogram_bins": list(HISTOGRAM_BINS), "rows_max_limit": ROWS_MAX_LIMIT,
        "units": UNIT_NOTES,
        "row_facts": {"untimed_sightings": untimed, "sightings_without_media": no_media},
        "vocabulary_issues": issues,
    }


# ── public: media entry listing (for get_image_urls) ─────────────────────

URLS_DEFAULT_LIMIT = 50
URLS_MAX_LIMIT = 500
URLS_PAYLOAD_CAP_BYTES = 400_000   # ~700 B/entry x 500, under the 1 MB tool-result cap


def _sighting_media_entries(s: dict, is_ref: bool = False, ref_source: str | None = None) -> list[dict]:
    out = []
    for m in s["media"]:
        out.append({
            "sighting_id": s["sighting_id"], "media_index": m["index"], "media_total": len(s["media"]),
            "kind": m["kind"], "ref": m["ref"], "path": m["path"],
            "station": s["station"], "timestamp": f"{s['date']} {s['time'] or '--:--'}",
            "date": s["date"], "time": s["time"], "time_of_day": s["time_of_day"],
            "species": s["species"], "category": s["category"], "individual": s["individual"],
            "individual_confidence": s["individual_confidence"], "source": s["source"],
            "light": s["light"], "legal_light": s["legal_light"], "count": s["count"],
            "auto": s["auto"], "harvest": s["harvest"], "is_reference_frame": is_ref,
            "reference_source": ref_source, "notes": s["notes"][:NOTES_CAP],
        })
    return out


def list_media_entries(filters: dict[str, Any], sighting_ids: list[int] | None = None,
                       individual: str | None = None, reference_frames_only: bool = False,
                       limit: int | None = None, offset: int = 0, order: str = "desc",
                       db: Path | None = None) -> dict[str, Any]:
    """One entry per attached media file, selected the same three ways get_images
    selects: explicit sighting_ids, an individual's designated reference frames,
    or the query_sightings filter set. Rows without media contribute no entries
    and are counted in sightings_without_media. Paged; never silently cut."""
    try:
        limit = URLS_DEFAULT_LIMIT if limit is None else int(limit)
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise QueryError("limit and offset must be integers")
    if limit < 1 or limit > URLS_MAX_LIMIT:
        raise QueryError(f"limit must be 1..{URLS_MAX_LIMIT}, got {limit}")

    lat, lon, src = solar.property_coords()
    model = {"lat": lat, "lon": lon, "coords_source": src, "tz": str(solar.LOCAL_TZ),
             "method": "computed per date: sunrise/sunset (zenith 90.833), civil twilight "
                       "(zenith 96); legal_light = sunrise-30min..sunset+30min"}
    entries: list[dict] = []
    applied: dict[str, Any] = {}
    selection: str
    sightings_without_media = 0

    if reference_frames_only:
        if not individual:
            raise QueryError("reference_frames_only=true requires individual='Name'")
        refs = load_reference_frames(db)
        key = next((k for k in refs if k.lower() == individual.lower()), None)
        frames = refs[key]["reference_frames"] if key else []
        by_id = get_sightings_by_id(sorted({f["sighting_id"] for f in frames}), db)
        for f in sorted(frames, key=lambda f: (f.get("date") or "", f["sighting_id"], f["media_index"])):
            s = by_id.get(f["sighting_id"])
            if s is None:
                continue
            for e in _sighting_media_entries(s, True, f.get("source")):
                if e["media_index"] == f["media_index"]:
                    entries.append(e)
        selection = "reference_frames"
        applied = {"individual": [key or individual], "reference_frames_only": True}
    elif sighting_ids:
        ids = [int(i) for i in sighting_ids]
        by_id = get_sightings_by_id(ids, db)
        missing = [i for i in ids if i not in by_id]
        for i in ids:
            s = by_id.get(i)
            if s is None:
                continue
            if not s["media"]:
                sightings_without_media += 1
            entries.extend(_sighting_media_entries(s))
        selection = "sighting_ids"
        applied = {"sighting_ids": ids}
        if missing:
            applied["sighting_ids_not_found"] = missing
    else:
        rows, applied, model = fetch_rows(filters, db)
        if individual and not applied.get("individual"):
            rows = [r for r in rows if (r["individual"] or "").lower() == individual.lower()]
            applied["individual"] = [individual]
        for r in reversed(rows) if not order.lower().startswith("a") else rows:
            if not r["media"]:
                sightings_without_media += 1
                continue
            entries.extend(_sighting_media_entries(r))
        selection = "filters"

    total = len(entries)
    page = entries[offset:offset + limit]
    return {
        "unit": "media files (one entry per file attached to a sighting)",
        "units": UNIT_NOTES,
        "selection": selection,
        "filters_applied": applied,
        "light_model": model,
        "total_matching": total,
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "order": "asc" if order.lower().startswith("a") else "desc",
        "truncated": offset + len(page) < total,
        "sightings_without_media": sightings_without_media,
        "entries": page,
    }
