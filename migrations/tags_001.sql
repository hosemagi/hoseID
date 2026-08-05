-- Tag store. Physically separate database from detections (invariant 3).
--
-- This is the only irreplaceable data in the system: detections can be regenerated from the
-- landing zone at any time, human tags cannot. Separate file, separate backup.
--
-- The pipeline NEVER writes here. Only the review path does.
--
-- Tags are SPARSE POSITIVE LABELS (invariant 5): the absence of a tag means "not tagged",
-- never "not present". Anything downstream that treats absence as negative evidence is a bug.

CREATE TABLE IF NOT EXISTS tags (
    tag_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Anchored to a detection, never to an encounter. Encounter grouping is derived downstream
    -- from a gap threshold that will be retuned, and encounter-level tags would orphan on every
    -- retune. Bulk-apply across a set fans out to member detections instead.
    detection_id TEXT NOT NULL,
    tag          TEXT NOT NULL,             -- normalised on write: lowercased, trimmed
    added_at     TEXT NOT NULL,
    added_by     TEXT NOT NULL DEFAULT 'p',
    note         TEXT,
    UNIQUE (detection_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_detection ON tags(detection_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag       ON tags(tag);

-- Vocabulary is free text with normalisation, not a fixed enum: a fixed vocabulary will be wrong
-- by November, but `Ben` / `ben` / `Big Ben` recorded as three animals is worse. This view backs
-- autocomplete so the UI nudges toward reuse without forbidding new terms.
CREATE VIEW IF NOT EXISTS tag_vocabulary AS
    SELECT tag, COUNT(*) AS n, MAX(added_at) AS last_used
    FROM tags GROUP BY tag ORDER BY n DESC, tag;
