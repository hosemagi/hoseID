-- Derived layer. Fully regenerable from the landing zone (invariant 2).
-- Every row carries run_id and the model identities that produced it, so a re-run with a
-- different model is additive rather than destructive and two runs can be compared.

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    detector_model    TEXT NOT NULL,
    detector_version  TEXT NOT NULL,
    classifier_model  TEXT,
    classifier_version TEXT,
    geofence_country  TEXT,
    geofence_admin1   TEXT,
    detector_threshold REAL NOT NULL,
    taxon_map_version TEXT,
    notes             TEXT
);

-- One row per capture per run, INCLUDING captures with no detections.
-- Empty captures are signal: false-trigger rate per station tracks wind, vegetation growth,
-- and camera health, and is invisible if empties are not recorded.
CREATE TABLE IF NOT EXISTS captures (
    asset_id       TEXT NOT NULL,
    run_id         TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    station        TEXT NOT NULL,          -- effective station, after overrides
    station_corrected INTEGER NOT NULL DEFAULT 0,
    capture_time   TEXT NOT NULL,
    time_trusted   INTEGER NOT NULL DEFAULT 0,
    n_detections   INTEGER NOT NULL DEFAULT 0,
    has_animal     INTEGER NOT NULL DEFAULT 0,
    has_human      INTEGER NOT NULL DEFAULT 0,
    has_vehicle    INTEGER NOT NULL DEFAULT 0,
    is_empty       INTEGER NOT NULL DEFAULT 1,
    detector_ms    REAL,
    PRIMARY KEY (asset_id, run_id)
);

CREATE TABLE IF NOT EXISTS detections (
    detection_id        TEXT PRIMARY KEY,
    asset_id            TEXT NOT NULL,
    run_id              TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bbox_x              REAL NOT NULL,      -- normalised xywh, as MegaDetector emits
    bbox_y              REAL NOT NULL,
    bbox_w              REAL NOT NULL,
    bbox_h              REAL NOT NULL,
    detector_class      TEXT NOT NULL,      -- animal | person | vehicle
    detector_confidence REAL NOT NULL,
    crop_path           TEXT,
    -- classifier fields are NULL until stage 2 runs; stage 1 output is independently useful
    taxon_raw           TEXT,               -- full semicolon taxonomy, always retained
    taxon               TEXT,               -- mapped display label
    taxon_confidence    REAL,
    review_priority     TEXT NOT NULL DEFAULT 'normal',
    classified_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_det_asset   ON detections(asset_id, run_id);
CREATE INDEX IF NOT EXISTS idx_det_taxon   ON detections(taxon);
CREATE INDEX IF NOT EXISTS idx_det_review  ON detections(review_priority);
CREATE INDEX IF NOT EXISTS idx_det_conf    ON detections(taxon_confidence);
CREATE INDEX IF NOT EXISTS idx_cap_station ON captures(station, capture_time);
CREATE INDEX IF NOT EXISTS idx_cap_run     ON captures(run_id);
