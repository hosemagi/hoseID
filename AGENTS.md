# hoseID — notes for coding agents

## Testing app.py (the review API)
- app.py's data paths are HARDCODED module constants (app.py:45-49:
  ASSETS, SIDECARS, DETECTIONS_DB, DB_PATH, MOVE_FLAGS). They do NOT read
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
- Run tests with `.venv/bin/pytest -q` from the repo root.

## Frontend (static/index.html)
- There is NO JS test harness in this repo and none should be built for a
  routine change. Frontend changes are verified by hand on port 8870;
  say so in the final report instead of inventing a node/jsdom harness.
- No query-stringed assets: the ?v= cache-buster convention does NOT
  apply here (that is a hoseserv convention).

## Running / restarting
- Service: launchd (`com.hoseid.review`, plist in config/), review app on port 8870. Templates/static are read
  from disk — a browser reload picks up HTML/JS edits, no restart.
- app.py changes need the 8870 process restarted; it runs under launchd —
  tell the operator rather than restarting it yourself.
- The Reveal archive is immutable; never write under the archive roots.
  Live data: ~/trailcam/landing (assets/sidecars), ~/trailcam/derived
  (detections.db), ~/trailcam/tags (tags.db).

## Real-time alerts (fetchers/alerts.py)
- hoseID decides only what was detected (detect/classify + exclusion zones).
  Who gets paged is hoseflow rules on nvlm (`wildlife-*`, hoseserv
  `data/hoseflow/rules.yaml`): every non-zone detection is POSTed to
  hoseserv's `wildlife-webhook`. Don't add species/threshold/cooldown logic
  back here, and don't filter by species before posting (invariant 4).
- The event fields in `build_event()` are a contract with those rules'
  `match` clauses and `{field}` message templates — renaming one silently
  breaks alerts. Taxon labels are classifier labels (deer = `blacktail`).
- No direct-ntfy fallback. 3 failed batches in a row (webhook down, or a rule
  fired but its notification wasn't delivered) send one "alerts DOWN" via
  `common.notify()`, which goes straight to ntfy.
- After editing it, restart the fetch daemon:
  `sudo launchctl kickstart -k system/com.hoseid.fetch`.

## Gotchas
- pipeline cache in app.py is keyed by file mtime; reset it directly in
  tests rather than touching files.
- detect.py keeps ONE best frame per video clip; all detections of a
  video share frame_offset_s (migrations/detections/002_video.sql).
