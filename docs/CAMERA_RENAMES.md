# Camera renames and moves — runbook

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
| log sync mapping | `scripts/sync_wildlife_log.py` `STATION_RENAMES` (+ `DEVICE_STATION` for the legacy export corpus) | yes — one line per rename |
| the wildlife log | `~/trailcam/tags/wildlife.db` `sightings.station`, `stations` table | yes — see SQL below; keep the old name as an alias |
| encounters (derived) | `~/trailcam/derived/encounters.db` | rebuild (`scripts/build_encounters.py --gap-min 90`; nightly does it too) |
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
6. **Sync mapping** `scripts/sync_wildlife_log.py`: add `"OLD": "NEW"` to `STATION_RENAMES`
   (and update `DEVICE_STATION` if the device is one of the four legacy-export serials). Check:
   `.venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import sync_wildlife_log as s; print(s.normalize_station('OLD','2026-09-12'))"`
7. **Rebuild encounters**: `.venv/bin/python scripts/build_encounters.py --gap-min 90`
8. **Sage note**: edit `~/sage/memory/trailcam-stations-and-cameras.md`.
9. **Verify** through the MCP (or `hoseid.mcp_wildlife.queries` directly):
   `get_stations()["unmapped_stations"]` should list nothing new, and
   `query_sightings(station='OLD')` should report `filters_applied.station == ['NEW']`.
10. Run `.venv/bin/pytest -q`. Nothing to restart: the MCP opens the DB per call, and the
    nightly sync picks up the mapping on its next run. The review app (8870) shows whatever
    `captures.station` says for old rows; that is cosmetic and regenerable.

## Gotchas seen

- Reveal API sidecars carry the camera's *current* vendor name at fetch time, so after a rename
  new captures arrive under the new name by themselves; the override + mapping exist for the
  backlog and for stale `captures` rows.
- One sighting (2026-08-22, jackrabbit) has an empty station string; `get_schema` flags it as
  `blank_station`. Fix by hand if the capture can be attributed.
