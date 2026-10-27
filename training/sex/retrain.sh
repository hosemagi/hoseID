#!/bin/zsh
# Monthly buck/doe retrain on the current hand-reviewed wildlife log tags.
# LaunchDaemon com.hoseid.sex-retrain (1st of the month, 03:40, after the
# 02:30 nightly). Read-only on the log, archive and detections.db; writes
# only under ~/trailcam/derived/training/sex/. Quiet on success; one
# operator notify on failure or when the new model is held back.
set -euo pipefail
REPO=${0:A:h:h:h}
cd "$REPO"
DET=.venv-detector/bin/python CLS=.venv-classifier/bin/python FETCH=.venv-fetch/bin/python
STEP=start
fail() {
    $FETCH -c "from fetchers.common import load_config, notify; notify(load_config(), 'buck/doe monthly retrain FAILED at step: $STEP (see ~/logs/hoseid-sex-retrain.log)', title='hoseid buck/doe retrain failed')" || true
}
trap fail ERR
echo "$(date -u +%FT%TZ) sex-retrain: start"
STEP=legacy-detect;  $DET training/sex/detect_legacy.py --if-changed
STEP=dataset;        $CLS training/sex/embed.py
STEP=dino-features;  $CLS training/sex/embed_dino.py
STEP=train-eval;     $CLS training/sex/train_eval.py > ~/trailcam/derived/training/sex/report.txt
STEP=promote;        $CLS training/sex/promote.py
echo "$(date -u +%FT%TZ) sex-retrain: done"
