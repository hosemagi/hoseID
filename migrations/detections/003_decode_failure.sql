-- Decode failure is its own state, not an empty capture.
--
-- Before this, a clip whose frames all failed to decode was recorded with n_detections=0 and
-- is_empty=1 -- indistinguishable from a station where nothing walked past. That is the same
-- silent-failure shape that has already bitten this project twice (the geofence that no-op'd
-- while recording itself as applied; hosenotify sitting dead on every peer while appearing
-- installed). A codec hoseID cannot read would have looked like quiet woods, with no reason
-- to investigate.
--
-- The distinction is "we looked and saw nothing" versus "we could not look". Only the first
-- belongs in the empty bucket.

ALTER TABLE captures ADD COLUMN decode_status TEXT NOT NULL DEFAULT 'ok';   -- ok | decode_failed
ALTER TABLE captures ADD COLUMN decode_error TEXT;

CREATE INDEX IF NOT EXISTS idx_cap_decode ON captures(decode_status);

-- Census must exclude decode failures as well as lower-bound counts: a clip that could not be
-- opened contributes no count at all, not even a lower bound.
DROP VIEW IF EXISTS captures_census;
CREATE VIEW captures_census AS
    SELECT * FROM captures
    WHERE count_is_lower_bound = 0 AND decode_status = 'ok';

-- Countable surface for the failures themselves, so they are visible rather than merely absent.
CREATE VIEW IF NOT EXISTS captures_decode_failed AS
    SELECT * FROM captures WHERE decode_status = 'decode_failed';
