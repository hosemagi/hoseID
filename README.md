# hoseID

Trail-camera capture identification for the cabin property (Nevada County, CA — Zone D-3).

Answers "what is in this file" per capture. Encounter grouping, corridor analysis, alerting and
Sage integration are all downstream of this layer.

## Pipeline

```
SD card / Reveal export / fetch daemon      stills AND video clips
(Reveal + Arlo, `fetchers/`)
        │
        ▼
  landing zone            append-only, immutable, content-addressed
        │                 a clip is ONE capture, not N frames
        │
        ├─ stage 1: MegaDetector v1000 redwood, threshold 0.1 as run
        │            (`hoseid run` and nightly: CPU; real-time pass: MPS;
        │            `stages/detect.py` alone defaults to 0.2)
        │            images: detect directly
        │            video:  sample 2 fps (≤40 frames), detect across all,
        │                    keep the single best frame by conf × bbox_area
        │            → boxes + crops + capture rollup (including empty captures)
        │
        └─ stage 2: SpeciesNet 5.0.5, MPS, USA/CA geofence, ON CROPS
                     → taxon + confidence + review priority
        │
        ├─ stage 3 (real-time pass only, best-effort): `stages/sex.py`
        │            buck/doe score for new blacktail crops → `detections.buck_prob`
        ▼
  derived/                regenerable; delete and rebuild at will
  tags/                   human labels; the only irreplaceable data here
```

There is no VLM. It was measured out of the pipeline: count comes free from the detector, species
from SpeciesNet at 92.7%, direction is derivable from bbox movement across a burst, and the
remaining attributes were never measured and are better covered by P's own tagging. See
`~/.claude/scratch/spike/vision-model-findings.md` for the measurement record.

Camera renamed or moved? Follow `docs/CAMERA_RENAMES.md` — station names live in several
layers. Arlo renames are followed automatically by device id; Reveal renames are by hand.

## Services on nvlm

All five are LaunchDaemons running as `hosebot`; the plists in `config/` are the templates.

| label | runs | schedule / port | log |
|---|---|---|---|
| `com.hoseid.fetch` | `.venv-fetch/bin/python -m fetchers.daemon` | KeepAlive; Reveal poll every 300 s, Arlo sweep on media events and every 900 s | `~/logs/hoseid-fetch.log` |
| `com.hoseid.review` | `~/venvs/megadetector/bin/uvicorn app:app` | KeepAlive; port 8870 | `~/logs/hoseid-review.log` |
| `com.hoseid.nightly` | `scripts/nightly.sh` | daily 02:30 | `~/logs/hoseid-nightly.log` |
| `com.hoseid.wildlife-mcp` | `.venv/bin/python -m hoseid.mcp_wildlife.app` | KeepAlive; port 8853 | `~/logs/hoseid-wildlife-mcp.log` |
| `com.hoseid.sex-retrain` | `training/sex/retrain.sh` | 1st of the month 03:40 | `~/logs/hoseid-sex-retrain.log` |

**Fetch daemon** (`fetchers/`). Polls the Reveal cloud and sweeps the Arlo library, lands new
captures, then runs the same detect + classify pass as the nightly (run id `nightly`, serialized
by `derived/.pipeline.lock`) on the fresh assets. Credentials and intervals come from
`~/.config/hoseid-fetch/credentials.toml`; cursors and health verdicts live in
`derived/fetch-state.json` (losing it only costs a re-scan). Arlo cameras whose name starts with
`MH -` (another property) are skipped at fetch time. Reveal polls also record each camera's
battery and signal in `landing/station_health.json` (plus a daily
`station_health_history.jsonl`, 180 days), named by station override first, then the rename
rules; hoseserv reads those files (hosemap trail-cam popup, battery days-left estimate).

**Alerts** (`fetchers/alerts.py`). hoseID does not decide who gets paged. Every detection outside
a review exclusion zone is POSTed, unfiltered by species (invariant 4), to hoseserv's hoseflow
webhook `wildlife-webhook`; the hoseflow `wildlife-*` rules decide taxa, thresholds, night hours,
priority and cooldown and deliver through hosenotify. Fetch-source outages and recoveries go to
the `hoseid-fetch-health` webhook once per transition. Direct ntfy (`common.notify()`) carries
no wildlife alerts; it is the operator channel that still works when hoseserv is down: three
failed webhook batches in a row ("alerts DOWN"), a health transition hoseflow did not deliver,
and fetch-daemon notices (an Arlo camera rename applied or refused, an empty Arlo download).

**Review app** (`app.py` + `static/index.html`, port 8870). Per-capture verdicts into the
`reviews` table of `tags/tags.db` (species tags, sex M/F, one or more `+`-joined individual or
person names, exclusion zones, camera epochs). `/api/queue` takes `status`, `device`, `sort`
and `since=YYYY-MM-DD` (local date; the last was added for hoseserv's camreview history).

**Nightly** (`scripts/nightly.sh`). `hoseid run --run-id nightly --threshold 0.1`, then
`sync_wildlife_log.py` (reviewed animal captures → `tags/wildlife.db` sightings),
`weather_backfill.py`, `build_encounters.py --gap-min 90`. Each step's exit code is recorded in
`derived/nightly_status.json` (hoseserv's System page reads it); the script exits non-zero when
the pipeline or the log sync fails.

## Stations

`~/trailcam/landing/stations.json` is the registry (name, vendor, `device_id` for Reveal
serials, position). `station_overrides.json` corrects a device's station for a date window at
analysis time, and `config/station_renames.json` maps vendor camera labels onto log stations
(`strip_prefix` `Cabin - `, `renames`, and `special` date splits). As of 2026-11-09:

- Reveal (tactacam): Storm Oak, Lost Woods, Bench, North Oak, Enchanted Forest, Cliffside,
  Trailer Path, North Entry.
- Arlo: Cabin Yard, Madrone, Crossroads, Path, Garage, Driveway, Entry, Field of Sticks,
  Orchard, Deck, Trailer, Shed, and Bench (Arlo) — the Arlo camera `Cabin - Bench` is its own
  station, not the Reveal station Bench.
- `Madrone (old spot)` is the Madrone camera's ground before its 2026-07-25 move.

## Wildlife MCP (read-only)

`python -m hoseid.mcp_wildlife.app` serves the sighting log over MCP on port 8853: list and
summarise sightings by date / species / station / time of day, and pull the capture image or
capture images behind any sighting (batch, with re-ID reference frames). Plist in `config/`, docs in `src/hoseid/mcp_wildlife/README.md`,
install with `.[mcp]`.

## Quick start

```bash
hoseid init                                    # create ~/trailcam/{landing,derived,tags}
hoseid ingest-sd /Volumes/CARD/DCIM --station Crossroads --vendor tactacam \
       --device-id TC-REVEALX3-0419
hoseid check                                   # validate the landing zone
hoseid run                                     # both stages; prints a taxon summary
hoseid queue <run-id>                          # review queue, priority-ordered
hoseid stats <run-id>                          # per-station activity incl. empty rate
hoseid tag <detection-id> ben antlered         # review path only
hoseid score <run-id>                          # pipeline vs P's review verdicts
```

Bulk vendor exports come in through their own command, because one export directory interleaves
every camera on the property and `ingest-sd` takes a single station for a whole directory:

```bash
hoseid ingest-reveal-export ~/some-reveal-export --dry-run
hoseid ingest-reveal-export ~/some-reveal-export
```

Station is resolved per file from the camera serial in the filename via the `device_id` field in
`stations.json`. A serial the registry does not know is **refused, not guessed** — sidecars are
immutable, so a capture ingested under the wrong station can only ever be papered over by an
override, while a refused one can simply be ingested once the registry names the camera.

### Measuring the pipeline against P's reviews

```bash
hoseid backfill-reviews      # fill reviews.asset_id — run once after a bulk review or ingest
hoseid score nightly         # detector and classifier layers, scored separately
hoseid sweep nightly         # what a detector-confidence floor would cost and save
```

`score` reports two layers because they fail independently and have different fixes: the
**detector** (did it find anything — using `empty` verdicts as true negatives) and the
**classifier** (did it name it right — scored only where the pipeline named something). It also
reports coverage on every call, so a scorer that cannot find its labels says so instead of
returning an accuracy computed over nothing. See invariants 9 and 10.

`backfill-reviews` is the join between the two halves of the system: the review app addresses
images by filename, the pipeline addresses captures by content hash, and nothing connected them.
It is idempotent, and hashes only files that are not already content-addressed.

`HOSEID_ROOT` overrides the data root (used by the eval runs and the tests).

### Video

Video ingests through the same path — `ingest-sd` accepts `.mp4/.avi/.mov/.mkv/.m4v` alongside
stills and probes duration/fps/frame_count via ffprobe. Decoding uses system ffmpeg rather than
PyAV or opencv, so the core package stays dependency-free for both ML venvs.

**Video counts are lower bounds, not censuses** (invariant 7). One frame is kept per clip, so an
animal visible only at another moment produces no detection. `captures.count_is_lower_bound`
carries this on every row; use the `captures_census` view or `review.group_size_stats()` for
anything that assumes complete counts.

Sampling is 2 fps capped at 40 frames per clip. When the cap binds, the 40 frames are spread
evenly across the *whole* clip rather than truncating at 20 s — otherwise an animal appearing
late in a long clip would never be sampled. The policy is recorded per run in `runs.sampling_policy`
so a re-run at different density is comparable rather than silently different.

Frames are selected on elapsed presentation time and their **real PTS** is read back, rather than
deriving `frame_index / rate`. That matters on variable-frame-rate sources: a clip with a gap in
its timestamps makes the `fps` filter duplicate frames across the gap and label them with
fabricated offsets — measured at up to 20.5 s wrong on a 20 s stall, for a field whose entire job
is scrubbing to the right moment.

**A decode failure is not an empty capture** (invariant 8). A clip that could not be read is
recorded with `decode_status = 'decode_failed'`, excluded from empty-capture stats and from the
census view, and surfaced loudly by `hoseid stats`. An unreadable clip still lands in the landing
zone — the bytes are the irreplaceable part — flagged via `probe_status` on its sidecar.

**Known limitation.** Because the cap spreads, effective fps falls as clip duration grows: a
5-minute clip samples at ~0.13 fps, so an animal crossing frame in 3 seconds can be missed
entirely. This is still a valid lower bound under invariant 7, but clip duration silently controls
detection sensitivity.

**Measured 2026-08-20**, now that real clips have landed (415 video captures): median duration
10.8 s, p90 20.2 s, max 108.8 s. The cap binds — i.e. 2 fps would exceed 40 frames — on **43 of
415 clips (10.4%)**. So long clips are real but not common, and the worst case is a 108.8 s clip
sampled at an effective 0.37 fps. The fixed cap stays for now on that evidence; the number to
watch is the 10.4%, not the maximum. Note that changing the sampling policy invalidates
comparison with existing rows — `runs.sampling_policy` records it per run precisely so a re-run
at a different density is comparable rather than silently different, which means a change here
means a re-run, not an edit.

## Layout

| Path | Contents | Backup priority |
|---|---|---|
| `~/trailcam/landing/` | assets + sidecars, immutable; `stations.json`, `station_overrides.json`, `station_health*.json(l)` | **high** — irreplaceable machine data |
| `~/trailcam/derived/` | detections.db, crops, encounters.db, fetch-state.json, mcp-previews, `training/sex/` | none — regenerable |
| `~/trailcam/tags/` | tags.db (tags, reviews, people, exclusion zones), wildlife.db (the sighting log), reference_frames.json | **highest** — irreplaceable human data |
| `~/trailcam/models/` | detector weights | none — re-downloadable |

Detector weights are staged into `models/` deliberately: MegaDetector otherwise downloads to
`tempfile.gettempdir()`, which on macOS is periodically purged, and a purge mid-batch would
trigger a silent 281 MB re-download.

## Environments

Separate venvs, because speciesnet and megadetector cannot coexist (see `docs/ENVIRONMENTS.md`):

| venv | Contents | protobuf |
|---|---|---|
| `.venv` | the `hoseid` package, CLI, nightly scripts, wildlife MCP (`.[mcp]`) | — |
| `.venv-detector` | megadetector 10.0.24 | 3.20.1 (pinned by ultralytics-yolov5) |
| `.venv-classifier` | speciesnet 5.0.5; also `stages/sex.py` and `training/sex` | 7.35.1 |
| `.venv-fetch` | requests + pyaarlo, the fetch daemon | — |
| `~/venvs/megadetector` | outside the repo: the review app, and the full test suite | — |

Tests: `~/venvs/megadetector/bin/python -m pytest -q -rs` from the repo root (the two wildlife
MCP test files skip there; run them with `.venv/bin/python -m pytest -q tests/test_mcp_wildlife*.py`).

`hoseid run` invokes each stage as a subprocess in its own interpreter. The stages communicate
through the filesystem and the detections database, never by importing each other.

## Documentation

- `docs/INVARIANTS.md` — the locked design invariants, where each is enforced and tested
- `docs/EVAL_MOUNTAIN_LION.md` — end-to-end lion test (97.8% sequence-level detector recall)
- `docs/CAMERA_RENAMES.md` — runbook for a camera rename or move
- `docs/ENVIRONMENTS.md` — why the venvs are split and how to recreate them
- `src/hoseid/mcp_wildlife/README.md` — the wildlife MCP server
- `training/sex/README.md` — the buck/doe model
- `AGENTS.md` — notes for coding agents (test pattern, restarts, contracts)

## Status

Built against ENA24 and Caltech Camera Traps. **Re-validate when P's real Reveal frames land** —
empty-frame rate, blacktail accuracy, bear colour phase in IR, and this property's actual
illuminators and camera angles are all still unmeasured.

The fetch modules (`fetchers/`), the review UI (`app.py`), the wildlife log sync and the
wildlife MCP have since been built and run as the services above. Parked: automatic person ID
(`training/person/`, 2026-10-26 — best balanced accuracy 0.44 on held-out days against chance
0.17).
