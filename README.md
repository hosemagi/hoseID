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
detection sensitivity. Revisit once real Arlo/Reveal clip durations are known — if long clips are
common, the cap should scale with duration rather than stay fixed.

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
