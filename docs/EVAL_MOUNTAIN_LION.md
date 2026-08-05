# Mountain lion end-to-end test

**2026-08-05.** Brief task 4. Mountain lion was absent from ENA24 entirely, so neither stage had
ever been measured on one — and it is the detection where a false negative has real cost.

## Data

**Caltech Camera Traps** (LILA), southwestern US desert. All 145 images labelled `mountain_lion`,
spanning **46 distinct capture sequences**. Ingested through the real SD-card path into an
isolated eval root (`~/trailcam-eval`) so the production landing zone stays clean, then run
through the real pipeline — same detector, same classifier, same geofence, same code.

CCT has **no bounding boxes**, which is fine: the question is whether the detector fires at all
on a frame containing a lion, not whether the box is tight.

## The metric trap, and the correction

The obvious metric — frame-level recall — gives **91.0% (132/145)**, i.e. 13 lion images
apparently missed. Lowering the detector threshold does not recover them: 12 of 13 are
unrecoverable at *any* threshold, 8 with literally 0.0000 confidence.

That looked like a serious gap in the safety-critical path. It is not. Inspecting the frames:

- One is a night IR frame, databar `M 4/5`, containing no visible animal at all.
- Another is a daylight frame, databar `M 3/3`, showing empty brush.

**Caltech labels are assigned per sequence, not per frame.** Every frame in a burst inherits the
sequence label even when the animal only appears in one frame. Most of the "missed" frames
genuinely contain no lion — the animal had already left. The detector was right and the metric
was wrong.

Scored at the level the labels actually describe:

| Metric | Value |
|---|---|
| Frames ingested | 145 |
| Distinct lion sequences | 46 |
| Sequences with ≥1 detection | 45 |
| **Sequence-level detector recall** | **97.8%** |
| Sequences missed entirely | **1** |

Sequence-level is also the operationally correct metric: the pipeline needs to catch the animal
in the encounter, not in every frame of the burst.

## Results

**Stage 1 — MegaDetector v1000 redwood, CPU, threshold 0.2**

- **97.8% sequence-level recall** (45/46). One sequence (`701ed6f8…`, 3 frames) produced no
  detection on any frame.
- 151 detections over 145 frames.

**Stage 2 — SpeciesNet 5.0.5, MPS, USA/CA geofence, on detector crops**

- **88.7% of detections classified `mountain_lion`** (134/151), at 6.9 img/s.
- The CA geofence does not suppress puma, as §A1 predicted from the geofence map — now confirmed
  in a live run rather than inferred from the data file.
- Misclassifications: 9 `deer_unspecified`, 3 `domestic_cat`, 3 `unknown_mammal`, 1 `red_fox`,
  1 `blank`. Several of these are on burst frames where the lion is partly or wholly out of frame.

**Safety behaviour**

- **96.0% of detections routed to `high` review priority** (145/151).
- All 151 detections are returned by `alertable()` — unfiltered by species, per invariant 4. A
  lion classified as a deer still reaches review; that is the property under test and it holds.

## What this does and does not establish

Established: the detector reliably fires on lions (97.8% of sequences), the classifier usually
names them correctly (88.7%), the CA geofence does not suppress them, and invariant 4 keeps even
misclassified lions reachable.

Not established:

1. **One sequence in 46 was missed entirely.** At this sample size that is 2.2% ± a lot — it
   could be 1% or 5%. It is a real non-zero miss rate and it is the number worth re-measuring
   when more lion data exists.
2. **This is desert scrub in the southwest**, not Sierra mixed conifer/oak at 3000 ft. Different
   vegetation, different camera angles, different IR illuminators.
3. **Nothing here validates blacktail**, which still has no test data anywhere.

The 1-in-46 miss is the argument for not treating the detector as infallible in the alerting
design downstream. Invariant 4 protects against classifier error; nothing protects against a
detector that never fires. If lion alerting matters, a second cheap signal (e.g. capture-rate
anomaly per station, or a lower threshold on a lion-plausible subset) is worth considering.

## Reproducing

```bash
export HOSEID_ROOT=~/trailcam-eval
hoseid ingest-sd /tmp/puma --station LILA-CCT-puma --vendor lila-cct
hoseid run --run-id puma-eval
```
