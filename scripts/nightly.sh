#!/bin/bash
# Nightly pipeline: detect+classify new landing-zone assets, then sync
# reviewed animal captures into the wildlife sighting log, then attach
# weather to any new sighting rows.
set -uo pipefail
cd "$(dirname "$0")/.."

# Serialize against the daemon's real-time alert pipeline (macOS has no
# flock(1); use a python fcntl wrapper on the shared lock file).
.venv/bin/python - <<'PYEOF'
import fcntl, subprocess, sys
lock = open("/Users/hosebot/trailcam/derived/.pipeline.lock", "w")
fcntl.flock(lock, fcntl.LOCK_EX)
sys.exit(subprocess.call([".venv/bin/hoseid", "run", "--run-id", "nightly",
                          "--threshold", "0.1"]))
PYEOF
status=$?

# Log sync runs even if the pipeline pass failed — reviews may still be new.
.venv/bin/python scripts/sync_wildlife_log.py

# Weather for any new sighting rows (fills NULLs only; two API calls)
.venv/bin/python scripts/weather_backfill.py || true

exit $status

# Rebuild derived encounters (wholesale; gap threshold is retunable)
.venv/bin/python scripts/build_encounters.py --gap-min 90 || true
