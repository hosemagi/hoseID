-- Video support.
--
-- A clip is ONE capture: one asset, one sidecar, one captures row. Sampled frames are internal
-- to stage 1 and never become captures. Stage 1 detects across every sampled frame, keeps the
-- single best-scoring frame, and emits detections from that frame only -- so a video capture
-- still produces ordinary detection rows and everything downstream is unchanged.

-- Where in the clip the selected frame came from. NULL for image captures.
ALTER TABLE detections ADD COLUMN frame_offset_s REAL;
ALTER TABLE detections ADD COLUMN frame_index INTEGER;

-- INVARIANT 7. Counts from video captures are LOWER BOUNDS, not censuses.
--
-- Only one frame per clip is kept, so an animal that is only visible at some other moment in the
-- clip produces no detection at all. Nothing downstream may treat n_detections on a video capture
-- as a complete count of animals present. Same rule as tags being sparse positive labels:
-- absence is not evidence of absence.
--
-- Stored as a column rather than derived from media_type at query time so the constraint travels
-- with the row: anything reading captures sees it without having to know the rule.
ALTER TABLE captures ADD COLUMN count_is_lower_bound INTEGER NOT NULL DEFAULT 0;
ALTER TABLE captures ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image';
ALTER TABLE captures ADD COLUMN sampled_frames INTEGER;   -- how many frames stage 1 examined

-- Sampling policy, recorded per run so a later re-run at different density is comparable rather
-- than silently different.
ALTER TABLE runs ADD COLUMN sampling_policy TEXT;

CREATE INDEX IF NOT EXISTS idx_cap_media ON captures(media_type);

-- Census-safe view. Anything computing group-size statistics should read this, not `captures`.
-- Excluding lower-bound rows is the default because the specific failure this prevents is an
-- aggregate like "average group size" silently mixing complete image counts with truncated
-- video counts and producing a meaningless number.
CREATE VIEW IF NOT EXISTS captures_census AS
    SELECT * FROM captures WHERE count_is_lower_bound = 0;
