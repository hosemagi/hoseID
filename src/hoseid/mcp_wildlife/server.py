"""FastMCP server: read-only tools over the wildlife log, shaped for an LLM caller.

Tools (all readOnlyHint):
  query_sightings   aggregate-first: summary | by_day | histogram | rows(limit required)
  get_images        batch (<= 6) downscaled captures for the MODEL to look at,
                    optionally with an individual's reference frames for re-ID
  get_image_urls    the same selection as URLs (no bytes) for the USER to view;
                    paged, default 50 / max 500
  get_individuals   registry + id_basis + reference frames + staleness flag
  get_claims        settled/open claims (do_not_reraise markers surfaced)
  get_open_items    owner's open/done/dropped items
  get_stations      registry + activity, unmapped stations flagged
  get_schema        controlled vocabularies in use + known vocabulary issues

Resources (raw bytes by URI):
  wildlife://sightings/{id}/media/{i}/preview.jpg
  wildlife://sightings/{id}/media/{i}/original.jpg | original.mp4

No write tools. The DB is opened mode=ro; see queries.py.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from hoseid.mcp_wildlife import media as _media
from hoseid.mcp_wildlife import queries as _q

DEFAULT_HOST = os.environ.get("WILDLIFE_MCP_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("WILDLIFE_MCP_PORT", "8853"))
REVIEW_URL = os.environ.get("HOSEID_REVIEW_URL", "http://localhost:8870")
MAX_IMAGES = 6

_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                      openWorldHint=False)

INSTRUCTIONS = (
    "Read-only access to the cabin trail-camera wildlife sighting log (Nevada "
    "County CA hilltop, 2026 season). Aggregate first: query_sightings defaults "
    "to response_mode='summary' (counts, no rows); ask for rows only with an "
    "explicit limit. Every count names its unit: 'sightings' are log rows (one "
    "per species per reviewed capture), 'captures' are distinct camera files, "
    "'encounters' are derived visit chains; the ratios are reported, not "
    "inferable. Times are local (America/Los_Angeles). time_of_day buckets: "
    "night 22-04, morning 05-11, midday 12-16, evening 17-21. 'light' "
    "(daylight | civil_twilight | dark) and legal_light (sunrise-30min to "
    "sunset+30min) are computed per date from sunrise/sunset at the property, "
    "so dawn-edge rows classify correctly as the season shifts. Individual "
    "attributions carry individual_confidence (confirmed | assumed | "
    "by_elimination | unconfirmed | none) and source (camera | direct_visual | "
    "camera+visual); weigh them, never flatten them. Call get_schema for valid "
    "filter values, get_claims before re-raising a settled question, and "
    "get_images with include_references_for_individual=true when comparing a "
    "capture against a known animal."
)

_FILTERS = (
    "Filters (all optional, combinable; lists may be JSON arrays or comma strings, "
    "case-insensitive): start_date/end_date (YYYY-MM-DD inclusive); species, "
    "station (aliases resolve: CAM1 -> Storm Oak), individual, category, source, "
    "confidence (confirmed|assumed|by_elimination|unconfirmed|none); time_of_day "
    "(night|morning|midday|evening); hour_from/hour_to (inclusive local hours, wraps "
    "midnight); light (daylight|civil_twilight|dark, computed per date); legal_light "
    "true/false; auto true/false (true = nightly review-sync rows, species only); "
    "has_media true/false."
)


def _plain(v: Any) -> Any:
    """FastMCP passes JSON arrays as lists and scalars as str; both are accepted."""
    return v


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastMCP:
    mcp = FastMCP("wildlife", instructions=INSTRUCTIONS, host=host, port=port,
                  stateless_http=True, json_response=True, log_level="INFO")

    @mcp.tool(annotations=_RO, description=(
        "Query the sighting log. Unit: sightings (capture_count reported alongside). "
        "Default response_mode='summary' returns totals plus breakdowns by species, "
        "station, individual (with confidence/source split), source, confidence, "
        "category and light - no rows. 'by_day' = per-day counts. 'histogram' = "
        "counts per histogram_bin (hour | time_of_day | light) with legal_light per "
        "bin and sunrise/sunset for the first and last day. 'rows' returns "
        "individual sightings and REQUIRES limit (max 200); use offset to page; "
        "truncated/returned/total_matching are always reported. " + _FILTERS +
        " Example (aggregate): query_sightings(species='deer', start_date='2026-09-01', "
        "response_mode='histogram', histogram_bin='light')."))
    def query_sightings(
        response_mode: str = "summary", histogram_bin: str = "hour",
        start_date: str | None = None, end_date: str | None = None,
        species: list[str] | str | None = None, station: list[str] | str | None = None,
        individual: list[str] | str | None = None, category: list[str] | str | None = None,
        source: list[str] | str | None = None, confidence: list[str] | str | None = None,
        time_of_day: list[str] | str | None = None,
        hour_from: int | None = None, hour_to: int | None = None,
        light: list[str] | str | None = None, legal_light: bool | None = None,
        auto: bool | None = None, has_media: bool | None = None,
        limit: int | None = None, offset: int = 0, order: str = "desc",
    ) -> dict:
        try:
            return _q.query_sightings(
                {"start_date": start_date, "end_date": end_date, "species": species,
                 "station": station, "individual": individual, "category": category,
                 "source": source, "confidence": confidence, "time_of_day": time_of_day,
                 "hour_from": hour_from, "hour_to": hour_to, "light": light,
                 "legal_light": legal_light, "auto": auto, "has_media": has_media},
                response_mode=response_mode, histogram_bin=histogram_bin,
                limit=limit, offset=offset, order=order)
        except _q.QueryError as exc:
            raise ValueError(str(exc))

    @mcp.tool(annotations=_RO, description=(
        "Fetch capture images as inline bytes for the MODEL to look at (max 6 "
        "images per call): re-identification, rack/coat comparison, anything "
        "where you must see the frame. The user's chat never shows these "
        "images; when the USER needs to see captures use get_image_urls "
        "instead (cheap, paged, gives links for a viewer). Images are downscaled to "
        "max_px on the long edge (default 768, cap 1024; aspect kept, never "
        "cropped; video clips return a poster frame). Give sighting_ids (up to 6) "
        "OR one sighting_id; media_index picks which file when a sighting has "
        "several (default 0). For individual re-identification set "
        "include_references_for_individual=true: the individual's designated "
        "reference frames are appended so candidate and references arrive "
        "together. reference_frames_only=true with individual='Al' returns just "
        "the ID set. Returns a JSON manifest first (per image: sighting_id, "
        "media_index, station, timestamp, species, individual, confidence, "
        "source, light, is_reference_frame, notes) then the images in that "
        "order; missing files are reported in the manifest, never silently "
        "dropped. Example: get_images(sighting_id=612, "
        "include_references_for_individual=true)."))
    def get_images(
        sighting_ids: list[int] | None = None, sighting_id: int | None = None,
        media_index: int = 0, include_references_for_individual: bool = False,
        individual: str | None = None, reference_frames_only: bool = False,
        max_px: int = _media.DEFAULT_MAX_PX,
    ) -> list:
        max_px = _media.clamp_px(max_px)
        wanted: list[tuple[int, int, bool, str]] = []   # (sighting_id, media_index, is_ref, why)

        ids: list[int] = []
        if sighting_ids:
            ids = [int(i) for i in sighting_ids]
        elif sighting_id is not None:
            ids = [int(sighting_id)]
        if len(ids) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} sighting_ids per call, got {len(ids)}")

        if not reference_frames_only:
            for sid in ids:
                wanted.append((sid, int(media_index) if len(ids) == 1 else 0, False, "requested"))

        ref_individual = individual
        if (include_references_for_individual or reference_frames_only) and not ref_individual:
            found = _q.get_sightings_by_id(ids)
            names = {s["individual"] for s in found.values() if s["individual"]}
            if len(names) == 1:
                ref_individual = names.pop()
            elif not names:
                raise ValueError("no individual is attributed to the requested sighting(s); "
                                 "pass individual='Name' explicitly")
            else:
                raise ValueError(f"requested sightings name several individuals {sorted(names)}; "
                                 "pass individual='Name' explicitly")
        if not wanted and not ref_individual:
            raise ValueError("give sighting_ids, sighting_id, or individual (with "
                             "reference_frames_only=true)")

        refs_meta: list[dict] = []
        if ref_individual:
            all_refs = _q.load_reference_frames()
            key = next((k for k in all_refs if k.lower() == ref_individual.lower()), None)
            refs_meta = all_refs[key]["reference_frames"] if key else []
            have = {(w[0], w[1]) for w in wanted}
            for f in sorted(refs_meta, key=lambda f: (f.get("date") or "", f["sighting_id"])):
                if (f["sighting_id"], f["media_index"]) not in have:
                    wanted.append((f["sighting_id"], f["media_index"], True, f.get("source", "")))

        truncated = wanted[MAX_IMAGES:]
        wanted = wanted[:MAX_IMAGES]
        rows = _q.get_sightings_by_id(sorted({w[0] for w in wanted}))

        manifest: list[dict] = []
        blobs: list[bytes] = []
        for sid, idx, is_ref, why in wanted:
            s = rows.get(sid)
            entry_meta: dict[str, Any] = {"sighting_id": sid, "media_index": idx,
                                          "is_reference_frame": is_ref}
            if s is None:
                entry_meta["error"] = "no such sighting"
                manifest.append(entry_meta)
                continue
            entry_meta.update({
                "station": s["station"], "timestamp": f"{s['date']} {s['time'] or '--:--'}",
                "date": s["date"], "time": s["time"], "light": s["light"],
                "legal_light": s["legal_light"], "species": s["species"],
                "individual": s["individual"], "confidence": s["individual_confidence"],
                "source": s["source"], "count": s["count"], "auto": s["auto"],
                "notes": s["notes"][:_q.NOTES_CAP], "media_total": len(s["media"]),
            })
            if is_ref:
                entry_meta["reference_source"] = why
            if not 0 <= idx < len(s["media"]):
                entry_meta["error"] = (f"sighting has no media" if not s["media"] else
                                       f"media_index out of range 0..{len(s['media']) - 1}")
                manifest.append(entry_meta)
                continue
            m = s["media"][idx]
            entry_meta["kind"] = "video_poster" if m["kind"] == "video" else "image"
            try:
                p = _media.safe_path(m)
                entry_meta["review_url"] = _media.review_url(p, REVIEW_URL)
                data = _media.preview_jpeg(m, max_px)
            except _media.MediaError as exc:
                entry_meta["error"] = str(exc)
                manifest.append(entry_meta)
                continue
            entry_meta["bytes"] = len(data)
            entry_meta["content_position"] = len(blobs) + 1  # 0 = manifest
            manifest.append(entry_meta)
            blobs.append(data)

        head = {
            "unit": "images (one per manifest entry that has no error)",
            "max_px": max_px,
            "individual": ref_individual,
            "reference_frames_designated": len(refs_meta) if ref_individual else None,
            "requested": len(wanted) + len(truncated),
            "returned": len(blobs),
            "truncated": bool(truncated),
            "dropped": [{"sighting_id": t[0], "media_index": t[1], "is_reference_frame": t[2]}
                        for t in truncated],
            "images": manifest,
        }
        out: list[Any] = [TextContent(type="text", text=json.dumps(head))]
        for data in blobs:
            out.append(ImageContent(type="image", mimeType="image/jpeg",
                                    data=base64.b64encode(data).decode("ascii")))
        return out

    @mcp.tool(annotations=_RO, description=(
        "List capture media as URLs for the USER to view - no image bytes. Use "
        "this for anything exploratory: what captures exist, building a viewer "
        "page, handing the user links. Reserve get_images for the few frames the "
        "model itself must analyse. Unit: media files (one entry per file; a "
        "sighting with several files yields several entries). Selection matches "
        "get_images: sighting_ids[] (explicit), or individual + "
        "reference_frames_only=true (the designated ID set), or the "
        "query_sightings filter set. Per entry: sighting_id, media_index, "
        "media_total, kind (image | video - emit <img> or <video> accordingly), "
        "url (review app on nvlm:8870, LAN/ZeroTier only), station, timestamp, "
        "date, time, species, individual, individual_confidence, source, light, "
        "legal_light, count, auto, harvest, is_reference_frame, notes, bytes; "
        "missing files carry an error instead of a url and are never dropped. "
        "Default limit 50, max 500, newest first; returned/total_matching/"
        "truncated are always reported, plus filters_applied and light_model as "
        "in query_sightings. " + _FILTERS +
        " Example: get_image_urls(species='deer', station='Storm Oak', "
        "start_date='2026-09-01', limit=100)."))
    def get_image_urls(
        sighting_ids: list[int] | None = None, individual: str | None = None,
        reference_frames_only: bool = False,
        start_date: str | None = None, end_date: str | None = None,
        species: list[str] | str | None = None, station: list[str] | str | None = None,
        category: list[str] | str | None = None, source: list[str] | str | None = None,
        confidence: list[str] | str | None = None, time_of_day: list[str] | str | None = None,
        hour_from: int | None = None, hour_to: int | None = None,
        light: list[str] | str | None = None, legal_light: bool | None = None,
        auto: bool | None = None, has_media: bool | None = None,
        limit: int | None = None, offset: int = 0, order: str = "desc",
    ) -> dict:
        try:
            out = _q.list_media_entries(
                {"start_date": start_date, "end_date": end_date, "species": species,
                 "station": station, "individual": individual, "category": category,
                 "source": source, "confidence": confidence, "time_of_day": time_of_day,
                 "hour_from": hour_from, "hour_to": hour_to, "light": light,
                 "legal_light": legal_light, "auto": auto, "has_media": has_media},
                sighting_ids=sighting_ids, individual=individual,
                reference_frames_only=reference_frames_only,
                limit=limit, offset=offset, order=order)
        except _q.QueryError as exc:
            raise ValueError(str(exc))
        for e in out["entries"]:
            try:
                p = _media.safe_path(e)
                e["url"] = _media.review_url(p, REVIEW_URL)
                e["bytes"] = p.stat().st_size
                e["mime"] = _media.mime_of(p)
            except _media.MediaError as exc:
                e["url"] = None
                e["error"] = str(exc)
            e.pop("path", None)
        out["url_base"] = REVIEW_URL
        return _q.bound_payload(out, "entries", cap=_q.URLS_PAYLOAD_CAP_BYTES)

    @mcp.tool(annotations=_RO, description=(
        "Named individual animals (bucks Al, Ben; bears Boar, Sow, Small bear): "
        "species, status, first/last seen, description, id_basis (what actually "
        "discriminates - weigh attributions by it), notes, station_count and "
        "stations, capture_events and direct_visuals, sightings_by_confidence, "
        "designated reference_frames (sighting_id + media_index for get_images), "
        "and reference_frames_stale with the reason (e.g. all references are "
        "velvet-era after a shed). Unit: capture_events = sightings naming the "
        "individual. Example: get_individuals()."))
    def get_individuals() -> dict:
        return _q.get_individuals()

    @mcp.tool(annotations=_RO, description=(
        "The claims register: id, claim, status (open | supported | withdrawn | "
        "falsified | resolved), registered/resolved dates, notes, and a "
        "do_not_reraise flag for questions the owner has explicitly closed. Check "
        "this before flagging a species ID or capture validity question. "
        "Optional status list filter. Example: get_claims(status=['open'])."))
    def get_claims(status: list[str] | str | None = None) -> dict:
        return _q.get_claims(status)

    @mcp.tool(annotations=_RO, description=(
        "The owner's open-items list: id, item, status (open | done | dropped), "
        "added/resolved dates, notes. Distinguishes genuinely unresolved questions "
        "from decided ones and shows what is already planned. Optional status "
        "list filter. Example: get_open_items(status=['open'])."))
    def get_open_items(status: list[str] | str | None = None) -> dict:
        return _q.get_open_items(status)

    @mcp.tool(annotations=_RO, description=(
        "Camera stations and property features: name, camera (tactacam | arlo | "
        "null), aliases, description, notes, first/last sighting, sighting_count "
        "and capture_count, top species. Stations that appear in sightings but "
        "have no registry entry (e.g. CB5-CB8) are returned with unmapped=true; "
        "treat their location as unknown. Example: get_stations()."))
    def get_stations() -> dict:
        return _q.get_stations()

    @mcp.tool(annotations=_RO, description=(
        "Controlled vocabularies actually in use, with counts: species, "
        "categories, confidence levels, sources, stations (registered flag), "
        "individuals, time_of_day buckets, light values, response modes, and "
        "units. Also vocabulary_issues known in the data (e.g. fox vs gray_fox "
        "split, category-style species values, other-animal rows duplicating a "
        "specific-species row). Call this first to build valid filters. "
        "Example: get_schema()."))
    def get_schema() -> dict:
        return _q.get_schema()

    # ── resources: raw bytes by URI ───────────────────────────────────────

    def _entry(sighting_id: int, index: int) -> dict:
        s = _q.get_sightings_by_id([int(sighting_id)]).get(int(sighting_id))
        if s is None:
            raise ValueError(f"no sighting with id {sighting_id}")
        if not 0 <= int(index) < len(s["media"]):
            raise ValueError(f"sighting {sighting_id} has media indexes 0..{len(s['media']) - 1}")
        return s["media"][int(index)]

    @mcp.resource("wildlife://sightings/{sighting_id}/media/{index}/preview.jpg",
                  mime_type="image/jpeg", name="sighting_media_preview",
                  description="JPEG preview (downscaled still or video poster frame)")
    def resource_preview(sighting_id: int, index: int) -> bytes:
        return _media.preview_jpeg(_entry(sighting_id, index))

    @mcp.resource("wildlife://sightings/{sighting_id}/media/{index}/original.jpg",
                  mime_type="image/jpeg", name="sighting_media_original_image",
                  description="Original full-resolution still")
    def resource_original_jpg(sighting_id: int, index: int) -> bytes:
        data, mime, _ = _media.original(_entry(sighting_id, index))
        if not mime.startswith("image/"):
            raise ValueError(f"media {index} of sighting {sighting_id} is {mime}, not an image")
        return data

    @mcp.resource("wildlife://sightings/{sighting_id}/media/{index}/original.mp4",
                  mime_type="video/mp4", name="sighting_media_original_video",
                  description="Original video clip")
    def resource_original_mp4(sighting_id: int, index: int) -> bytes:
        data, mime, _ = _media.original(_entry(sighting_id, index))
        if mime != "video/mp4":
            raise ValueError(f"media {index} of sighting {sighting_id} is {mime}, not mp4")
        return data

    return mcp


def build_starlette_app(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """ASGI app for uvicorn. No auth - ZeroTier/LAN is the perimeter, same as sage-mcp."""
    return create_server(host=host, port=port).streamable_http_app()
