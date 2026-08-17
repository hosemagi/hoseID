#!/bin/bash
# Nightly pipeline: detect+classify new landing-zone assets, then sync
# reviewed animal captures into the wildlife sighting log, then attach
# weather to any new sighting rows.
set -uo pipefail
cd "$(dirname "$0")/.."

.venv/bin/hoseid run --run-id nightly --threshold 0.1
status=$?

# Log sync runs even if the pipeline pass failed — reviews may still be new.
.venv/bin/python scripts/sync_wildlife_log.py

# Weather for any new sighting rows (fills NULLs only; two API calls)
.venv/bin/python scripts/weather_backfill.py || true

exit $status
