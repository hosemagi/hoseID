# Design invariants

These are locked. Violating one is a design regression, not an implementation detail. If one
seems wrong while building, raise it rather than working around it.

Each is listed with where it is enforced in code and which test pins it, so a future change that
breaks one fails loudly rather than silently.

---

### 1. The landing zone is append-only and immutable

Assets and their sidecars are never modified after write. Everything else is derived from them.

*Enforced:* `landing.store_asset` writes via atomic temp+rename and refuses to overwrite a
sidecar whose capture-describing fields differ. `paths.asset_path` is content-addressed, so
identical bytes cannot produce a conflicting write.

*Nuance:* re-ingesting the same card is a **no-op, not a conflict**. Two ingests of identical
bytes legitimately differ in `ingested_at` and in the mount path recorded in
`raw_vendor_payload` — that is provenance of the ingest run, not a property of the capture.
`landing._PROVENANCE_FIELDS` lists what is exempt from the conflict check. A difference in
anything that describes the capture (station, capture_time, resolution_class, device) is still
refused.

*Tests:* `test_reingesting_identical_bytes_is_idempotent`,
`test_reingest_with_different_ingest_time_is_a_noop`, `test_conflicting_station_is_still_refused`,
`test_no_partial_files_left_visible`.

### 2. The derived layer is fully regenerable

Any analysis output can be deleted and rebuilt from the landing zone. Every derived record
carries `run_id`, the model identities, and a version for whatever produced it.

*Enforced:* `migrations/001_detections.sql` — the `runs` table records detector model+version,
classifier model+version, geofence, threshold and taxon-map version; `captures` and `detections`
are keyed by `run_id` and cascade on delete.

### 3. The tag store is separate, and the pipeline never writes to it

Human tags are the only irreplaceable data in the system and the only source of attributes.
Own file, own backup, keyed by `detection_id`.

*Enforced:* physically separate SQLite database (`tags/tags.db` vs `derived/detections.db`),
separate migration, and no write path to it outside `tags.py`.

*Test:* `test_tag_and_detection_databases_are_separate_files`.

*Amended 2026-08-20 — there are now TWO human-label stores, and the second one arrived without
this invariant being updated.* The review app (`app.py`) created a `reviews` table inside
`tags/tags.db` with its own DDL. It is written from outside `tags.py`, keyed by image basename
rather than `detection_id`, and it never touches `PRAGMA user_version`, so hoseid's migration
runner still reads that database as version 0 while the table has existed for weeks. The
separate-file half of this invariant holds; the single-writer and `detection_id`-keyed halves do
not. The consequences were not theoretical: `hoseid score` went on querying the empty `tags`
table and reported `accuracy: null` over zero rows rather than failing, for the entire period in
which 2,334 verdicts accumulated. Raised here rather than quietly worked around, per the note at
the top of this file. See invariant 9 for the semantics the second store actually has.

### 4. Alerts are never filtered by species

Any capture with an animal detection is loggable and reachable in review. Species affects how an
alert is phrased, never whether it fires. This is what makes a misclassified mountain lion still
reach P.

*Enforced:* `review.review_queue` / `review.alertable` order by priority and contain no species
predicate. There is deliberately no `species=` parameter on the alert path. Geofence roll-ups
that could conceal a lion (`unknown_felid`, `unknown_carnivore`) are pinned to `always_high` in
`config/taxon_map.json`.

*Tests:* `test_every_animal_detection_is_alertable_regardless_of_taxon`,
`test_a_misclassified_lion_still_reaches_review`, `test_family_rollup_of_cat_is_high_priority`.

*Measured:* `docs/EVAL_MOUNTAIN_LION.md` — all 151 lion detections reachable, 96% routed high.

*Known limit:* this invariant protects against **classifier** error. It does not protect against
a detector that never fires. Measured detector miss rate on lions is 1 sequence in 46.

### 5. Tags are sparse positive labels

Absence of a tag means "not tagged", never "not present". Anything downstream that treats tag
absence as negative evidence is a bug.

*Enforced:* `tags.score_against_pipeline` scores only detections carrying a species-like tag, and
reports `tagged_but_no_species_tag` separately rather than counting those as errors.

*Tests:* `test_untagged_detections_are_not_counted_as_pipeline_errors`,
`test_non_species_tags_do_not_score_as_predictions`.

### 6. Ingest and analysis are separate stages

Ingest is cheap, continuous, and must not fail. Analysis is batched and re-runnable.

*Enforced:* `hoseid ingest-sd` performs no analysis and reads no station registry; `hoseid run`
reads only the landing zone and is idempotent per `run_id` (`db.already_processed`). The two
pipeline stages are separate processes in separate venvs — required anyway, since speciesnet and
megadetector have an unresolvable protobuf conflict.

### 7. Counts from video captures are lower bounds, not censuses

One frame is selected per clip, so an animal that only becomes visible at another moment in the
clip produces no detection. Nothing downstream may treat a video-derived count as a complete
count of animals present. This is the same rule as invariant 5: **absence is not evidence of
absence.**

This is the one real cost of dropping tracking, and it is accepted deliberately rather than
discovered later. The specific failure to prevent is something like Sage computing average group
size across mixed image and video captures and producing a number that is neither a group size
nor a detection rate.

*Enforced:* `captures.count_is_lower_bound` is set to 1 for every video capture by
`stages/detect.py` — stored as a column rather than derived from `media_type` at query time, so
the constraint travels with the row and any reader sees it without knowing the rule. The
`captures_census` view excludes lower-bound rows, and `review.group_size_stats` reads that view
by default, reporting `excluded_lower_bound_captures` rather than hiding the omission.
`include_lower_bounds=True` remains available for callers that genuinely want a detection rate,
and the returned dict is labelled so the distinction survives into whatever consumes it.

*Tests:* `test_video_captures_are_flagged_as_lower_bounds`, `test_census_view_excludes_video`,
`test_group_size_stats_excludes_video_by_default`,
`test_including_lower_bounds_is_possible_but_labelled`.

### 8. A decode failure is not an empty capture

`sampled_frames == 0` on a video, or metadata too broken to sample from, means **we could not
look** — which is a different fact from **we looked and saw nothing**. Only the second belongs in
the empty-capture bucket.

Without this, a codec hoseID cannot read looks exactly like a station where nothing walked past:
same zero count, same empty flag, no reason to investigate. That is the same silent-failure shape
as the geofence that no-op'd while recording itself as applied, and hosenotify sitting dead on
every peer while appearing installed.

*Enforced:* `video.decode_state()` decides the outcome; `stages/detect.py` writes
`captures.decode_status` and forces `is_empty = 0` for failures, printing to stderr as it goes.
The `captures_census` view excludes them (a clip that could not be opened contributes no count at
all, not even a lower bound), `captures_decode_failed` makes them countable,
`review.station_activity` reports `decode_failures` alongside — never folded into —
`empty_captures`, and `hoseid stats` prints a loud warning listing them.

Ingest is deliberately unaffected: a clip whose probe fails still **lands** in the landing zone
(invariant 6 — ingest must not fail, and the bytes are the irreplaceable part; a future ffmpeg
may well decode them). The failure is recorded on the sidecar via `probe_status` / `probe_error`,
and `IngestReport.probe_failed` counts it separately from ingest errors.

*Tests:* `test_decode_state_video_with_zero_sampled_frames_is_a_failure`,
`test_decode_state_unusable_duration_or_fps_is_a_failure`,
`test_decode_failure_is_excluded_from_empty_capture_stats`,
`test_decode_failures_are_countable_and_inspectable`,
`test_decode_failure_excluded_from_census`,
`test_corrupt_clip_still_lands_in_the_landing_zone`.

### 9. Reviews are complete verdicts; tags are sparse positive labels

The two human-label stores have opposite semantics and must never be scored by the same code.

A **tag** is a positive assertion about one detection. Absence means "not tagged" (invariant 5).
A **review** is a verdict on one whole capture: the reviewer looked at the frame and recorded
everything in it, so absence of a species DOES mean "not present", and an explicit `empty`
verdict is a real negative.

That difference is not cosmetic — it is the only reason the pipeline can be measured at all.
False positives and misses both require true negatives, which sparse positive labels structurally
cannot supply. Scoring reviews as if they were tags throws away every negative and reports only
naming accuracy; scoring tags as if they were reviews invents negatives that were never asserted
and manufactures false positives out of frames nobody looked at.

*Enforced:* `reviews.score_against_pipeline` reads the `reviews` table and reports a detector
layer (presence, using `empty` as a true negative) separately from a classifier layer (identity,
scored only where the pipeline named something). `tags.score_against_pipeline` keeps the sparse
contract for the tag store and carries a docstring pointing at the difference. `hoseid score`
calls the review scorer.

*Corollaries that cost real accuracy when missed, each pinned by a test:*

- People and vehicles are named by the **detector**, not the classifier — stage 2 only runs on
  `detector_class='animal'`, so a correctly-detected vehicle carries a NULL `taxon` forever.
  Scoring those against `taxon` alone charged the classifier for 31 vehicles it was never asked
  about and reported classifier accuracy as 0.57 when it was 0.88.
- A `blank` classification is an **absence of a name**, not a competing name. It counts against
  that tag's recall and is reported as `declined_to_name`; it never enters the confusion matrix.
- A capture with no detections at all is a **detector** miss only. Charging the classifier too
  double-counts one failure.
- Human and pipeline vocabularies sit at different resolutions on purpose ("deer" vs
  "blacktail"). `config/review_vocab.json` maps between them, so a vocabulary gap is not scored
  as a pipeline error.

*Tests:* `tests/test_reviews.py` — `test_empty_verdict_with_a_detection_is_a_false_positive`,
`test_animal_the_detector_never_found_is_a_false_negative`,
`test_vehicle_is_scored_from_the_detector_class_not_the_taxon`,
`test_blank_is_a_declined_naming_not_a_confusion`,
`test_detector_miss_is_not_also_charged_as_a_naming_error`,
`test_vocabulary_gap_is_not_scored_as_a_disagreement`,
`test_unjoined_reviews_are_reported_not_silently_dropped`.

### 10. A scorer that cannot find its labels must say so

Reporting an accuracy computed over zero rows is worse than crashing: it reads as "nothing to
score yet" and survives indefinitely. `hoseid score` therefore reports coverage as a first-class
part of its output — how many reviews were scored, how many carry no `asset_id` to join on, and
how many joined but were never processed by the run being scored.

*Enforced:* `reviews.score_against_pipeline` returns a `coverage` block on every call, and
`accuracy` is `null` rather than `0` when nothing was scorable.

*Test:* `test_unjoined_reviews_are_reported_not_silently_dropped`.

---

## Two non-negotiable operational facts

**SpeciesNet must be fed detector crops, never full frames.** Findings §A2: 92.7% on crops,
29.2% on full frames. It is an `always_crop` model and fails *silently and confidently* on a full
frame, returning `blank` at ~0.99. Guarded by `classify._assert_is_crop`; tested in
`tests/test_crop_guard.py`.

**A clip is one capture.** One asset, one sidecar, one `captures` row. Sampled frames are internal
to stage 1 and never become captures — that would explode the capture count and make clip identity
implicit rather than explicit. Sampled frames are also temporary: only the selected frame's crop
persists, addressed by `detection_id` exactly as image crops are.

**Frame selection is detector-driven, never motion-driven.** Stage 1 runs MegaDetector across
every sampled frame and keeps the best by `confidence × bbox_area`. Motion is a proxy for "an
animal is here"; the detector is the direct measure and is cheap enough to run on everything.
Motion is actively worse here: wind moves branches, so it selects for exactly the false triggers
this pipeline exists to drop, and the highest-motion frame is frequently the most motion-blurred —
the worst frame to identify from. There is deliberately **no tracking**; do not reintroduce it
without a new decision.

**The geofence must be applied explicitly.** SpeciesNet's `components="classifier"` mode ignores
`country`/`admin1_region` entirely — verified: identical output for no-geofence, USA, USA/CA and
USA/PA. Only the full ensemble applies it, and the ensemble insists on running its own bundled
MegaDetector v5a, which would duplicate stage 1. `classify.Geofencer` therefore applies the
geofence and roll-up itself using the same maps the ensemble would use. Without this the geofence
recorded in run provenance would be a lie.
