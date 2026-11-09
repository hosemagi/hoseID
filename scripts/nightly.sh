#!/bin/bash
# Nightly pipeline: detect+classify new landing-zone assets, then sync
# reviewed animal captures into the wildlife sighting log, then attach
# weather to any new sighting rows.
#
# Each step gets an ISO-timestamp header line in the log, and its exit code is
# recorded into ~/trailcam/derived/nightly_status.json by
# scripts/nightly_status.py (hoseserv's System page reads it). The helper calls
# are guarded with `|| true` so status writing can never break the run.
# Exit status: the first non-zero of the pipeline and the log sync. A failed
# weather backfill or encounter rebuild is recorded in the status file but
# does not change the exit code.
set -uo pipefail
cd "$(dirname "$0")/.."

STATUS=(.venv/bin/python scripts/nightly_status.py)
step_started=""

# header <step>: remember the step's start and print a log boundary line.
header() {
    step_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "=== nightly $1 $step_started"
}

# record <step> <exit>: write the step's exit code into the status file.
record() {
    "${STATUS[@]}" step "$1" "$2" --started "$step_started" || true
}

"${STATUS[@]}" begin || true

# Serialize against the daemon's real-time alert pipeline (macOS has no
# flock(1); use a python fcntl wrapper on the shared lock file).
header pipeline
.venv/bin/python - <<'PYEOF'
import fcntl, subprocess, sys
lock = open("/Users/hosebot/trailcam/derived/.pipeline.lock", "w")
fcntl.flock(lock, fcntl.LOCK_EX)
sys.exit(subprocess.call([".venv/bin/hoseid", "run", "--run-id", "nightly",
                          "--threshold", "0.1"]))
PYEOF
status=$?
record pipeline "$status"

# Log sync runs even if the pipeline pass failed — reviews may still be new.
header sync_wildlife_log
.venv/bin/python scripts/sync_wildlife_log.py
sync_status=$?
record sync_wildlife_log "$sync_status"

# Weather for any new sighting rows (fills NULLs only; two API calls).
# Non-fatal for the exit code; recorded in the status file.
header weather_backfill
.venv/bin/python scripts/weather_backfill.py
record weather_backfill "$?"

# Rebuild derived encounters (wholesale; gap threshold is retunable).
# Non-fatal for the exit code; recorded in the status file.
header build_encounters
.venv/bin/python scripts/build_encounters.py --gap-min 90
record build_encounters "$?"

"${STATUS[@]}" end || true
echo "=== nightly done $(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$status" -ne 0 ]; then
    exit "$status"
fi
exit "$sync_status"
