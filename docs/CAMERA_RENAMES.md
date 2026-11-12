# Camera renames and moves — runbook

> **Arlo cameras rename themselves (2026-10-30).** Rename the camera in the Arlo app and the
> fetch daemon follows it by device id within ~2 h (every connect and sweep):
> `hoseid.camera_names` updates `station_renames.json` (old label → new station),
> `station_overrides.json`, `stations.json`, `wildlife.db` (sightings + stations row, old label
> kept as an alias), `detections.db`, and hosemap layers, then posts a note to the shared
> Logbook. A rename onto a name another camera already uses is refused and alerted. Still by
> hand: Sage's station note (below) and prose in the log. Reveal cameras still use this runbook.
>
> **Two cameras, one name (2026-11-09).** The Arlo camera `Cabin - Bench` is station
> `Bench (Arlo)`; the Reveal camera labelled `Bench` keeps station `Bench`. The prefix strip
> would have merged them, so `Cabin - Bench` has a `special` entry in
> `config/station_renames.json` with `before` = `after` = `Bench (Arlo)`. Do the same for any
> future Arlo camera whose stripped name collides with a Reveal station.

P renames cameras in the vendor app (Reveal / Arlo) when he moves them. The vendor label *is*
the station name, but it reaches the system through several layers that do not update
themselves. This is the checklist that worked on 2026-09-12 (South Clearing → Lost Woods,
CB5–CB8 → Enchanted Forest / Cliffside / Trailer Path / North Entry).

## Where a station name lives

| layer | file | rewrite? |
|---|---|---|
| sidecars (landing zone) | `~/trailcam/landing/sidecars/**` `station` field = vendor label at capture time | **never** (invariant 1) |
| station registry | `~/trailcam/landing/stations.json` | yes — rename entry / add entry with `device_id` |
| analysis-time corrections | `~/trailcam/landing/station_overrides.json` | yes — add `device_id → new name` for the window the sidecars carry the old label |
| detections (derived) | `~/trailcam/derived/detections.db` `captures.station` | no — `stages/detect.py` applies overrides when it writes new rows; old rows are covered by the sync mapping below |
| log sync + security view mapping | `config/station_renames.json` (read by `scripts/sync_wildlife_log.py` and hoseserv's security view). The sync resolves camera **serials** to stations from `stations.json` itself (`hoseid.stations.by_device`), so there is no hand-copied device map to edit | yes — add a line to the JSON per rename |
| the wildlife log | `~/trailcam/tags/wildlife.db` `sightings.station`, `stations` table | yes — see SQL below; keep the old name as an alias |
| encounters (derived) | `~/trailcam/derived/encounters.db` | rebuild (`scripts/build_encounters.py --gap-min 90`; nightly does it too) |
| Reveal battery / signal | `~/trailcam/landing/station_health.json`, `station_health_history.jsonl` | no — `fetchers/reveal.py` names each reading like a capture: the serial's station override first, then the rename rules (so Reveal label `North Entrance` reports as `North Entry`). Without an override for the serial it falls back to the renamed vendor label |
| Sage's station note | `~/sage/memory/trailcam-stations-and-cameras.md` | yes — Sage answers "which camera is which" from it |
| prose in the log (claims, open items, sighting notes, individuals) | wildlife.db text columns | leave — they are history; the alias makes the old name resolvable in the MCP |

The wildlife MCP (`hoseid.mcp_wildlife`) needs nothing: it reads `stations.aliases`, so filters
on the old name resolve to the new one, and `get_stations` reports any station with no registry
row as `unmapped`. Use that as the check that nothing was missed.

## Steps

1. **Find the device and its label history** (which sidecars carry which name):
   ```
   .venv/bin/python - <<'EOF'
   import json, glob, collections
   c = collections.Counter()
   for f in glob.glob('/Users/hosebot/trailcam/landing/sidecars/*/*/*.json'):
       d = json.load(open(f)); c[(d.get('station'), d.get('device_id'), d.get('vendor'))] += 1
   for k, n in sorted(c.items()): print(n, k)
   EOF
   ```
2. **Back up the log**: `cp ~/trailcam/tags/wildlife.db ~/trailcam/tags/backups/wildlife-pre-rename-$(date +%F).db`
3. **Rename in the log** (repeat per pair; keep the old name as an alias):
   ```sql
   UPDATE sightings SET station='NEW' WHERE station='OLD';
   -- existing registry row:
   UPDATE stations SET name='NEW', aliases=json_insert(aliases,'$[#]','OLD'),
          notes=notes||'; renamed from OLD YYYY-MM-DD' WHERE name='OLD';
   -- or a label that never had a row (e.g. a CBx default name):
   INSERT INTO stations (name, aliases, camera, description, notes)
     VALUES ('NEW', '["OLD"]', 'tactacam', '', 'renamed from OLD YYYY-MM-DD; Reveal device <id>');
   ```
   A physical **move** to different ground is not a rename: give the old spot its own station
   row (see `Madrone (old spot)`) and split by date with an override window instead.
4. **Registry** `stations.json`: rename the entry or add one with `device_id`, `vendor`,
   `active_from`. Two entries must never share a `device_id` (`stations.by_device` raises).
5. **Overrides** `station_overrides.json`: add `{device_id, start, end, station: NEW, reason}`.
   For a pure rename use `2020-01-01 … 2099-01-01`; for a move use the window during which the
   sidecars carried the wrong label. First matching override wins — order matters.
6. **Sync mapping**: add a line `"OLD": "NEW"` to `renames` in `config/station_renames.json`
   (the vendor label without the `Cabin - ` prefix). A move that splits one camera name by date
   goes in `special` as `{name, before, after, cutoff}` (see `Cabin - Madrone`). hoseserv's
   security view reads the same file, so there is no second copy to edit. Camera
   **serials** already resolve from `stations.json` (`hoseid.stations.by_device`), so no separate
   device map to update — but add the `device_id` to the registry if a Reveal export serial is
   missing, otherwise it won't resolve to a station. Check:
   `.venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import sync_wildlife_log as s; print(s.normalize_station('OLD','2026-09-12'))"`
7. **Rebuild encounters**: `.venv/bin/python scripts/build_encounters.py --gap-min 90`
8. **Sage note**: edit `~/sage/memory/trailcam-stations-and-cameras.md`.
9. **Verify** through the MCP (or `hoseid.mcp_wildlife.queries` directly):
   `get_stations()["unmapped_stations"]` should list nothing new, and
   `query_sightings(station='OLD')` should report `filters_applied.station == ['NEW']`.
10. Run `~/venvs/megadetector/bin/python -m pytest -q -rs` (`.venv` alone cannot collect the
    suite; see AGENTS.md). Nothing to restart: the MCP opens the DB per call, and the
    nightly sync picks up the mapping on its next run, and the security view rereads the JSON
    when its mtime changes. The review app (8870) shows whatever
    `captures.station` says for old rows; that is cosmetic and regenerable.

## Gotchas seen

- Reveal API sidecars carry the camera's *current* vendor name at fetch time, so after a rename
  new captures arrive under the new name by themselves; the override + mapping exist for the
  backlog and for stale `captures` rows.
- The sync never rewrites rows it has already written, and it never inserts a blank station
  anymore: a capture with no station label now lands under the registry station for the
  review's `device_id` (the sidecar serial; `captures` has no serial column), or `"unknown"` if
  the serial is missing or not registered. One old sighting still carries an empty station — the 2026-08-22
  jackrabbit (sighting 479), caught before this rule. `get_schema` flags it as `blank_station`;
  fix it by hand if the capture can be attributed, because the sync will not.
