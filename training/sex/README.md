# Buck/doe model

Predicts "at least one buck present" for a deer capture, trained on the
hand-reviewed sex tags in `tags/wildlife.db` (M = at least one male present,
F = no male). Not wired into the pipeline yet; `current` is what a future
`stages/sex.py` would load.

## How it's built
- `detect_legacy.py` (.venv-detector) — MegaDetector on the pre-pipeline
  `legacy:` captures (Reveal export, read-only); crops to `legacy_crops/`.
- `embed.py` (.venv-classifier) — one row per deer crop with its capture label,
  visit (same station, gaps ≤ 30 min) and IR flag; `--speciesnet` also saves
  SpeciesNet features (tested worse: trained to merge bucks and does).
- `split.py` — whole visits to eval (~20%), stratified by station × label ×
  day/night, filled by capture count.
- `embed_dino.py` — DINOv2 ViT-B/14 @ 448 px, CLS + mean patch token,
  original + mirror; records the backbone weights' sha256.
- `train_eval.py` — heads chosen by 5-fold CV grouped by visit on train only,
  by precision at 90% recall (recall-first); threshold from CV; held-out eval
  at capture level (max over crops).
- `promote.py` (.venv-classifier) — archive to `runs/<date>[-N]/`, point
  `current` at it unless CV precision dropped > 0.05; Sage emails the report
  (`mail_report.py`, to `[model_reports]` in `~/.config/sage-mail.toml`),
  ntfy if the email can't go out.

Outputs live in `~/trailcam/derived/training/sex/` (never the archive,
`derived/crops/` or `detections.db`).

## Monthly job
LaunchDaemon `com.hoseid.sex-retrain` (`config/`), 1st of the month 03:40,
runs `retrain.sh`; log `~/logs/hoseid-sex-retrain.log`. Run by hand:
`./training/sex/retrain.sh` (promotes and emails like the real thing).

## Versions / rewind
Each run is ~2 MB (head 1.6 MB + labels snapshot, metrics, report). The 3
newest are kept (`KEEP` in `promote.py`), plus the current run if you rewound
to an older one — it is never pruned. The 337 MB DINOv2 backbone is shared, not copied — each run's
`backbone.json` records its sha256.

    .venv-classifier/bin/python training/sex/promote.py --list
    .venv-classifier/bin/python training/sex/promote.py --use 2026-10-26

Switches and prunes are logged in `promotions.jsonl`.

## Known limits (2026-10-26 run)
Held-out AP 0.89; at the recall-first threshold 88% of buck captures caught,
53% precision. Weak on video (one sampled frame per clip) and in doe-heavy
Sep–Oct. Bucks shed ~Jan–Mar and will read as does until winter tags exist.
