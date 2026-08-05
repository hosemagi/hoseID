# hoseID

Trail-camera capture identification for the cabin property (Nevada County, CA — Zone D-3).

Answers "what is in this file" per capture. Encounter grouping, corridor analysis, alerting and
Sage integration are all downstream of this layer.

## Pipeline

```
SD card / (future) Reveal + Arlo fetch      stills AND video clips
        │
        ▼
  landing zone            append-only, immutable, content-addressed
        │                 a clip is ONE capture, not N frames
        │
        ├─ stage 1: MegaDetector v1000 redwood, CPU, threshold 0.2
        │            images: detect directly
        │            video:  sample 2 fps (≤40 frames), detect across all,
        │                    keep the single best frame by conf × bbox_area
        │            → boxes + crops + capture rollup (including empty captures)
        │
        └─ stage 2: SpeciesNet 5.0.5, MPS, USA/CA geofence, ON CROPS
                     → taxon + confidence + review priority
        │
        ▼
  derived/                regenerable; delete and rebuild at will
  tags/                   human labels; the only irreplaceable data here
```

There is no VLM. It was measured out of the pipeline: count comes free from the detector, species
from SpeciesNet at 92.7%, direction is derivable from bbox movement across a burst, and the
remaining attributes were never measured and are better covered by P's own tagging. See
`~/.claude/scratch/spike/vision-model-findings.md` for the measurement record.

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
hoseid score <run-id>                          # pipeline vs P's tags
```

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

## Layout

| Path | Contents | Backup priority |
|---|---|---|
| `~/trailcam/landing/` | assets + sidecars, immutable | **high** — irreplaceable machine data |
| `~/trailcam/derived/` | detections.db, crops, rollups | none — regenerable |
| `~/trailcam/tags/` | tags.db | **highest** — irreplaceable human data |
| `~/trailcam/models/` | detector weights | none — re-downloadable |

Detector weights are staged into `models/` deliberately: MegaDetector otherwise downloads to
`tempfile.gettempdir()`, which on macOS is periodically purged, and a purge mid-batch would
trigger a silent 281 MB re-download.

## Environments

Three venvs, because speciesnet and megadetector cannot coexist:

| venv | Contents | protobuf |
|---|---|---|
| `.venv` | the `hoseid` package, CLI, tests | — |
| `.venv-detector` | megadetector 10.0.24 | 3.20.1 (pinned by ultralytics-yolov5) |
| `.venv-classifier` | speciesnet 5.0.5 | 7.35.1 |

`hoseid run` invokes each stage as a subprocess in its own interpreter. The stages communicate
through the filesystem and the detections database, never by importing each other.

## Documentation

- `docs/INVARIANTS.md` — the locked design invariants, where each is enforced and tested
- `docs/EVAL_MOUNTAIN_LION.md` — end-to-end lion test (97.8% sequence-level detector recall)

## Status

Built against ENA24 and Caltech Camera Traps. **Re-validate when P's real Reveal frames land** —
empty-frame rate, blacktail accuracy, bear colour phase in IR, and this property's actual
illuminators and camera angles are all still unmeasured.

Not built yet, by design: the Reveal and Arlo fetch modules (deferred until the sidecar contract
is settled against a real sample) and the review UI (highest-leverage remaining component; the
tag store and crop addressing exist so it can be dropped in).
