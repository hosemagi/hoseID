-- Wildlife sighting log. Human-curated interpretation layer over captures:
-- stations, named individuals, sightings, claims, open items. Lives in
-- tags/wildlife.db — irreplaceable-human-data class, same as tags.db.
--
-- Epistemic rules carried over from the source markdown log:
--   * individual_confidence is mandatory when an individual is named;
--     "a wrong name is worse than no name" — unidentified is a valid value.
--   * counts are CAPTURE counts unless count_is_visits=1 (a multi-cam track
--     is many captures, one movement).
--   * claims carry status incl. 'withdrawn' (never established) as distinct
--     from 'falsified' (tested and failed) — the 08/07 temperature-model
--     lesson, encoded.
--   * documents holds source markdown verbatim; structured rows cite it.
--     Fidelity lives in the document; queryability lives in the rows.

CREATE TABLE IF NOT EXISTS documents (
    doc_id      INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    added_at    TEXT NOT NULL,
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    name        TEXT PRIMARY KEY,
    aliases     TEXT NOT NULL DEFAULT '[]',   -- JSON array
    camera      TEXT,                          -- 'tactacam' | 'arlo' | null (no camera)
    description TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS individuals (
    name        TEXT PRIMARY KEY,
    species     TEXT NOT NULL,
    first_seen  TEXT,
    description TEXT NOT NULL DEFAULT '',
    id_basis    TEXT NOT NULL DEFAULT '',     -- what actually discriminates
    status      TEXT NOT NULL DEFAULT 'active',
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sightings (
    sighting_id INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,                -- YYYY-MM-DD
    time        TEXT,                         -- HH:MM[:SS] local, null if unknown
    station     TEXT NOT NULL,                -- stations.name, or free text (deck etc.)
    species     TEXT NOT NULL,
    individual  TEXT,                          -- individuals.name when attributed
    individual_confidence TEXT CHECK (individual_confidence IN
        ('confirmed','assumed','by_elimination','unconfirmed', NULL)),
    count       INTEGER NOT NULL DEFAULT 1,
    count_is_visits INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'camera',  -- camera | direct_visual | camera+visual
    category    TEXT NOT NULL DEFAULT 'other',   -- deer|bear|small_game|predator|bird|disturbance|other
    capture_asset_id TEXT,                    -- landing-zone link when camera-sourced
    auto        INTEGER NOT NULL DEFAULT 0,   -- 1 = created by log-sync from a review
    harvest     INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    doc_ref     TEXT                          -- documents.name backing this row
);
CREATE INDEX IF NOT EXISTS idx_sightings_date ON sightings(date);
CREATE INDEX IF NOT EXISTS idx_sightings_individual ON sightings(individual);
CREATE INDEX IF NOT EXISTS idx_sightings_asset ON sightings(capture_asset_id);

CREATE TABLE IF NOT EXISTS claims (
    claim_id    INTEGER PRIMARY KEY,
    claim       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
        ('open','supported','withdrawn','falsified','resolved')),
    registered_at TEXT,                       -- pre-registration date when applicable
    resolved_at TEXT,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS open_items (
    item_id     INTEGER PRIMARY KEY,
    item        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
    added       TEXT,
    resolved    TEXT,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Views: per-individual and per-station tallies. Deliberately count captures
-- and label them as such.
CREATE VIEW IF NOT EXISTS individual_tally AS
    SELECT individual,
           COUNT(*)                          AS capture_events,
           SUM(source != 'camera')           AS direct_visuals,
           MIN(date)                         AS first_logged,
           MAX(date)                         AS last_logged,
           COUNT(DISTINCT station)           AS stations_hit
    FROM sightings WHERE individual IS NOT NULL
    GROUP BY individual;

CREATE VIEW IF NOT EXISTS station_activity AS
    SELECT station, species, COUNT(*) AS capture_events,
           MIN(date) AS first, MAX(date) AS last
    FROM sightings GROUP BY station, species;
