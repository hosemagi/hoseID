# hoseID — notes for coding agents

## Testing app.py (the review API)
- app.py's data paths are HARDCODED module constants (app.py:57-62:
  ASSETS, SIDECARS, DETECTIONS_DB, DB_PATH, MOVE_FLAGS, WILDLIFE_DB). They do NOT read
  HOSEID_ROOT. Do NOT refactor them through src/hoseid/paths.py to make a
  test possible — that is a production change nobody asked for.
- The right pattern: `import app`, then monkeypatch the constants
  directly (app.DETECTIONS_DB = tmp_db, app.ASSETS = tmp_assets, ...)
  and call the handler functions (e.g. app.queue()) as plain functions.
  init_db() runs at import time and is idempotent (CREATE IF NOT EXISTS
  against the real tags.db), so importing is safe.
- HOSEID_ROOT redirection works only for code that goes through
  src/hoseid/paths.py (the pipeline stages, tests/test_video.py pattern).
- Seed schemas by applying migrations/detections/*.sql to the tmp DB.
- Run tests with `~/venvs/megadetector/bin/python -m pytest -q -rs` from the
  repo root (the review app's venv; `-rs` shows skips: the two wildlife MCP
  test files skip there, 388 passed / 2 skipped on 2026-11-09). The wildlife
  MCP tests need the repo `.venv` (it has `mcp`): `.venv/bin/python -m pytest
  -q tests/test_mcp_wildlife.py tests/test_mcp_wildlife_log_noise.py`. `.venv` alone cannot run the suite (no
  fastapi, numpy or requests; `fetchers` imports fail).
- sqlite: open databases through `hoseid.fsio.connect_ro` / `connect_rw`
  (encoded URI; 5 s busy timeout for readers, 30 s for writers). A drift test
  rejects a bare `sqlite3.connect(` outside fsio.py, db.py and
  camera_names.py. Add-if-missing columns go through
  `hoseid.schema.ensure_columns` (it does not commit; the caller does), and
  `SIGHTINGS_COLUMNS` must stay identical to what hoseserv's `_ensure_schema`
  adds (drift test). File writes that others read: `fsio.atomic_write_*`.

## Frontend (static/index.html)
- There is NO JS test harness in this repo and none should be built for a
  routine change. Frontend changes are verified by hand on port 8870;
  say so in the final report instead of inventing a node/jsdom harness.
- No query-stringed assets: the ?v= cache-buster convention does NOT
  apply here (that is a hoseserv convention).

## Running / restarting
- Service: LaunchDaemon `com.hoseid.review` (the installed plist in
  /Library/LaunchDaemons runs as hosebot from `~/venvs/megadetector`), review
  app on port 8870, log `~/logs/hoseid-review.log`. The config/*.plist files
  are templates; all but `com.hoseid.sex-retrain` lack `UserName`: never
  install one of those verbatim (it would run as root). Templates/static are read from disk — a browser reload picks up
  HTML/JS edits, no restart.
- app.py changes need the 8870 process restarted — tell the operator rather
  than restarting it yourself (they run
  `sudo launchctl kickstart -k system/com.hoseid.review`).
- Wildlife MCP (`src/hoseid/mcp_wildlife/`): LaunchDaemon
  `com.hoseid.wildlife-mcp`, `.venv/bin/python`, port 8853 (plain http,
  `WILDLIFE_MCP_TLS=0`), `GET /health` returns `{"ok": true}` (hoseserv's
  Stack panel probes it). Log `~/logs/hoseid-wildlife-mcp.log`. Restart:
  `sudo launchctl kickstart -k system/com.hoseid.wildlife-mcp`.
- Nightly pipeline: LaunchDaemon `com.hoseid.nightly` (scripts/nightly.sh,
  02:30, log `~/logs/hoseid-nightly.log`): `hoseid run --run-id nightly`,
  then sync_wildlife_log, weather_backfill, build_encounters. Each step's exit
  code goes to `~/trailcam/derived/nightly_status.json` via
  scripts/nightly_status.py (hoseserv's System page reads it); a failed
  pipeline or log sync makes the script exit non-zero.
- Fetch daemon: LaunchDaemon `com.hoseid.fetch` (`.venv-fetch/bin/python -m
  fetchers.daemon`, log `~/logs/hoseid-fetch.log`). Don't run it (or
  `--once`, or scripts/backfill_arlo.py) to test a change: it talks to the
  live Reveal/Arlo accounts and lands captures. Config is
  `~/.config/hoseid-fetch/credentials.toml` (don't read it); state is
  `~/trailcam/derived/fetch-state.json`.
- The Reveal archive is immutable; never write under the archive roots.
  Live data: ~/trailcam/landing (assets/sidecars, stations.json,
  station_overrides.json, station_health.json), ~/trailcam/derived
  (detections.db, encounters.db), ~/trailcam/tags (tags.db, wildlife.db).

## Stations and camera names
- A station name lives in several layers (docs/CAMERA_RENAMES.md). Vendor
  label -> station goes through `hoseid.stations.resolve_station` (overrides
  by device id and date) and `hoseid.station_renames.normalize_station`
  (config/station_renames.json: `strip_prefix`, `renames`, `special`).
  hoseserv reads this repo's copy of that JSON, so its shape is a contract.
- Arlo renames are followed automatically by device id
  (`hoseid.camera_names`, called by the fetch daemon on every connect and
  sweep); it rewrites station_renames.json, the registry, overrides,
  wildlife.db and detections.db. Reveal renames are by hand, per the runbook.
- The Arlo camera `Cabin - Bench` is station `Bench (Arlo)` (a `special`
  entry), not the Reveal station `Bench`. Don't "simplify" that entry away.
- Reveal battery/signal readings (station_health.json) are named like
  captures: station override for the serial first, then the rename rules.
- Sidecars keep the vendor label of capture time and are never rewritten.

## Real-time alerts (fetchers/alerts.py)
- hoseID decides only what was detected (detect/classify + exclusion zones).
  Who gets paged is hoseflow rules on nvlm (`wildlife-*`, hoseserv
  `data/hoseflow/rules.yaml`): every non-zone detection is POSTed to
  hoseserv's `wildlife-webhook`. Don't add species/threshold/cooldown logic
  back here, and don't filter by species before posting (invariant 4).
- The event fields in `build_event()` are a contract with those rules'
  `match` clauses and `{field}` message templates — renaming one silently
  breaks alerts. Taxon labels are classifier labels (deer = `blacktail`).
- `sex_guess` ("buck"/"doe"/"") is part of that contract: the capture's max
  `detections.buck_prob` (stages/sex.py, migration 004) against the current
  buck/doe model's own `metrics.json` threshold. The raw score is deliberately
  NOT in the payload (uncalibrated; the threshold is ~0.001). The sex step in
  `run_pipeline_incremental` is best-effort: its failure logs, never returns
  False (that would count toward "alerts DOWN").
- No direct-ntfy fallback. 3 failed batches in a row (webhook down, or a rule
  fired but its notification wasn't delivered) send one "alerts DOWN" via
  `common.notify()`, which goes straight to ntfy.
- Fetch-source outages (`common.report_health`, from `daemon.run_source`)
  POST `{source, verdict: BROKEN|HEALTHY, failures, error}` to hoseserv's
  `hoseid-fetch-health` webhook once per transition (verdict kept in
  fetch-state.json); those keys are a contract with the `fetch-*` rules in
  hoseserv docs/ALERTS.md. Unless hoseflow reports a notification sent, it
  falls back to `notify()` (ntfy).
- After editing it, restart the fetch daemon (it runs from `.venv-fetch`):
  `sudo launchctl kickstart -k system/com.hoseid.fetch`.

## Buck/doe model (training/sex/, see its README)
- Labels are the hand-reviewed wildlife-log sex tags, all confirmed by the
  operator: M = at least one buck in the capture, F = no buck. So every crop
  in an F capture is a doe/fawn; in a multi-deer M capture you don't know
  which crop is the buck (those are eval-only, scored as max over crops).
- Venvs: run the scripts with `.venv-classifier` (torch + PIL + requests);
  `detect_legacy.py` with `.venv-detector`. There is no sklearn in
  .venv-classifier and none should be added (protobuf conflicts) — heads are
  plain torch. Tests run with `~/venvs/megadetector`.
- Split by whole visit (stratified by station x label x day/night), never by
  crop: frames of one visit are near-duplicates. Pick models on train-split
  CV; the held-out eval is for reporting, not for choosing.
- `retrain.sh` and `promote.py` (no flags) send a real email to the operator
  (or an ntfy ping when Mail Bridge is down) and repoint `current` — don't run
  them just to test. Use `mail_report.py --dry-run <file>` and the unit tests
  (`tests/test_sex_promote.py`, which stub notify/mail).
- Outputs only under `~/trailcam/derived/training/sex/`; the 3 newest runs
  are kept, plus `current`. Monthly LaunchDaemon `com.hoseid.sex-retrain`.

## Gotchas
- Local time is `hoseid.site.TZ` (SITE_TZ, default America/Los_Angeles, the
  same rule as hoseserv). Never define a ZoneInfo per file. It is read at
  import; no plist sets SITE_TZ today, so if the zone ever changes add it to
  each service's EnvironmentVariables and restart them.
- The site's coordinates come from `hoseid.site.site_coords()` -> (lat, lon,
  source): `SITE_LAT`/`SITE_LON` (the same variables as hoseserv's
  `app.utils.site_coords`), else the first stations.json entry with
  coordinates, else Storm Oak. Never hard-code the point elsewhere. It is
  read from the process env, so a service only sees it if its plist's
  EnvironmentVariables set it (none do today).
- pipeline cache in app.py is keyed by file mtime; reset it directly in
  tests rather than touching files.
- detect.py keeps ONE best frame per video clip; all detections of a
  video share frame_offset_s (migrations/detections/002_video.sql).
