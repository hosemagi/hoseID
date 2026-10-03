# hoseid.mcp_wildlife — read-only MCP over the wildlife log

Streamable-HTTP MCP server on **port 8853** (plain http, ZeroTier/LAN perimeter, like the corpus MCP on :8851) exposing `~/trailcam/tags/wildlife.db` to an LLM
caller. **Strictly read-only**: the DB is opened `mode=ro`; the only files written are
downscaled preview JPEGs under `~/trailcam/derived/mcp-previews/` (regenerable, keyed by asset
digest + size). No write / propose tools; those are deferred and must go through the review
queue when they come.

```
.venv/bin/pip install -e '.[mcp]'            # main venv only (mcp<2 pinned)
.venv/bin/python -m hoseid.mcp_wildlife.app  # http://localhost:8853/mcp
cp config/com.hoseid.wildlife-mcp.plist ~/Library/LaunchAgents/ && \
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hoseid.wildlife-mcp.plist
claude mcp add --transport http wildlife http://localhost:8853/mcp
```

## Design rules

- **Aggregate by default, rows on request.** `query_sightings` defaults to `summary`; `rows`
  refuses to run without an explicit `limit` (max 200) and pages with `offset`.
- **Every count names its unit.** `unit: sightings` (log rows: one per species per reviewed
  capture, or one hand-logged event), `capture_count` (distinct camera files) alongside, and
  `encounters` (derived visit chains from `derived/encounters.db`) in summary mode. The
  sightings:captures ratio is reported, never left to be guessed.
- **Never silently truncate.** `truncated`, `returned`, `total_matching` on every list; a 60 KB
  payload cap trims rows and says so.
- **Confidence and provenance survive.** `individual_confidence` (confirmed / assumed /
  by_elimination / unconfirmed / none) and `source` (camera / direct_visual / camera+visual)
  ride on every row and split every individual breakdown.
- **Light is computed, not hardcoded.** `light` (daylight / civil_twilight / dark) and
  `legal_light` (sunrise−30 min … sunset+30 min) come from sunrise/sunset at the property for
  the row's date (`solar.py`, Almanac-for-Computers algorithm, coords from
  `landing/stations.json` or `WILDLIFE_LAT/LON`). Sunrise moves ~1 h across the season.

## Tools (all `readOnlyHint`)

| tool | returns |
|---|---|
| `query_sightings` | `response_mode` = `summary` (default) · `by_day` · `histogram` (`histogram_bin` = hour / time_of_day / light, with legal-light per bin and sun times for first/last day) · `rows` (limit required) |
| `get_image_urls` | the same selection as `get_images` (sighting_ids / individual + reference_frames_only / the query filter set) as **URLs, no bytes** — one entry per attached file with `kind` (image \| video), `url` on the review app, and the full sighting metadata. For the **user** to look: viewers, link lists, browsing. Default 50, max 500, own 400 KB payload cap, never silently cut |
| `get_images` | for the **model** to look: batch ≤ 6 downscaled JPEGs (`max_px` default 768, cap 1024; aspect kept; video → poster frame). `sighting_ids[]` or `sighting_id`+`media_index`; `include_references_for_individual=true` appends the animal's designated reference frames; `reference_frames_only=true` with `individual` returns just the ID set. JSON manifest first (station, timestamp, species, individual, confidence, source, light, is_reference_frame, notes, errors), then images in manifest order |
| `get_individuals` | registry + `id_basis` + stations + `sightings_by_confidence` + `reference_frames` + `reference_frames_stale` with reason |
| `get_claims` | claims with `do_not_reraise` flag; optional `status[]` |
| `get_open_items` | open items; optional `status[]` |
| `get_stations` | registry + activity + top species; `unmapped: true` for stations with no registry entry (CB5–CB8) |
| `get_schema` | vocabularies in use with counts + `vocabulary_issues` (fox/gray_fox split, category-as-species, other-animal duplicates, blank station) |

Filters for `query_sightings`: `start_date`/`end_date` · `species`, `station` (aliases resolve),
`individual`, `category`, `source`, `confidence` (lists or comma strings, case-insensitive) ·
`time_of_day` (night 22–04 / morning 05–11 / midday 12–16 / evening 17–21, same buckets as
Sage's `wildlife_query`) · `hour_from`/`hour_to` (wraps midnight) · `light` · `legal_light` ·
`auto` · `has_media`. Rows with no recorded time never match a time or light filter and are
reported as `untimed`.

## Reference frames

`~/trailcam/tags/reference_frames.json` (owner-edited) designates re-ID frames per individual
and records `appearance_changes`; sightings whose notes say "reference" are picked up
automatically (`source: notes`). An appearance change dated after every reference frame marks
the set stale (e.g. velvet-era buck references after the shed).

## Claude Desktop

Desktop's Settings-page "custom connector" form insists on an https URL **and probes it from
Anthropic's cloud**, which cannot reach a ZeroTier address ("No server responded at this URL" /
"Couldn't reach wildlife"). The corpus MCP hit the same wall on 2026-05-15; the working path is
the **mcp-remote stdio bridge** in `~/Library/Application Support/Claude/claude_desktop_config.json`
on the client Mac:

```json
"wildlife": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "http://10.159.206.106:8853/mcp", "--allow-http"]
}
```

Full Cmd-Q and relaunch Desktop afterwards (it caches the tool list). Optional
`WILDLIFE_MCP_TLS=1` serves self-signed https on 8853 (SAN covers nvlm + LAN + ZeroTier IPs,
serverAuth EKU) plus loopback http on 8854; not needed for the bridge.

## Resources

```
wildlife://sightings/{id}/media/{i}/preview.jpg
wildlife://sightings/{id}/media/{i}/original.jpg
wildlife://sightings/{id}/media/{i}/original.mp4
```

## Safety

A media entry is served only if its stored path resolves inside `landing/assets/` and exists;
anything else is reported in the manifest with the reason, never as bytes.

## Env

`WILDLIFE_MCP_HOST` (0.0.0.0) · `WILDLIFE_MCP_PORT` (8853) · `WILDLIFE_MCP_HTTP_PORT` (8854, loopback) ·
`WILDLIFE_MCP_TLS` (0) · `WILDLIFE_MCP_CERTFILE`/`WILDLIFE_MCP_KEYFILE` · `WILDLIFE_MCP_SAN_EXTRA` · `WILDLIFE_LAT`/`WILDLIFE_LON`
(default: first station with coords in stations.json) · `HOSEID_REVIEW_URL`
(http://localhost:8870) · `HOSEID_ROOT` (~/trailcam)
