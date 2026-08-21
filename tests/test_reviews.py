"""Tests for the review store: the asset_id join, and two-layer scoring.

The property under test throughout is the one that separates this store from the tag store:
reviews are COMPLETE VERDICTS, so an ``empty`` review is a true negative and the pipeline can be
charged with a false positive. The tag store cannot express that and must not be scored this way
(see test_pipeline_contracts.py for its own, sparse-label, contract).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hoseid import db, paths, reviews
from hoseid.sidecar import compute_asset_id

RUN = "r1"


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    return tmp_path


def _reviews_table(conn: sqlite3.Connection) -> None:
    """The review app's own DDL, reproduced: it owns this table, not hoseid's migrations."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY,
            basename TEXT NOT NULL,
            image TEXT NOT NULL,
            device_id TEXT,
            captured_at TEXT,
            tags TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            md_max_conf REAL,
            reviewed_at TEXT NOT NULL,
            reviewer TEXT NOT NULL DEFAULT 'p',
            counts TEXT NOT NULL DEFAULT '{}',
            individual TEXT,
            individual_confidence TEXT
        )""")
    conn.commit()


def _add_review(image: str, tags: list[str], *, md_max_conf: float = 0.5,
                basename: str | None = None, reviewed_at: str = "2026-08-16T00:00:00Z") -> None:
    with db.tags(create=False) as conn:
        _reviews_table(conn)
        conn.execute(
            "INSERT INTO reviews (basename, image, device_id, captured_at, tags, md_max_conf,"
            " reviewed_at, reviewer) VALUES (?,?,?,?,?,?,?,'p')",
            (basename or image.split("/")[-1], image, "dev1", "2026-08-03T04:12:07",
             json.dumps(tags), md_max_conf, reviewed_at))
        conn.commit()


def _capture(conn, asset_id: str, *, station="Crossroads", empty=0, n=0):
    conn.execute(
        "INSERT INTO captures (asset_id, run_id, station, capture_time, time_trusted,"
        " n_detections, has_animal, is_empty) VALUES (?,?,?,?,1,?,?,?)",
        (asset_id, RUN, station, "2026-08-03T04:12:07Z", n, 1 if n else 0, empty))


def _detection(conn, det_id: str, asset_id: str, taxon: str | None, conf=0.9,
               det_conf=0.9, cls="animal"):
    conn.execute(
        "INSERT INTO detections (detection_id, asset_id, run_id, bbox_x, bbox_y, bbox_w, bbox_h,"
        " detector_class, detector_confidence, taxon, taxon_confidence, review_priority)"
        " VALUES (?,?,?,.1,.1,.1,.1,?,?,?,?,'normal')",
        (det_id, asset_id, RUN, cls, det_conf, taxon, conf))


def _seed_run():
    with db.detections() as conn:
        db.start_run(conn, run_id=RUN, started_at="2026-08-05T00:00:00Z",
                     detector_model="md", detector_version="1", detector_threshold=0.2)
        yield_conn = conn
    return yield_conn


# --- the join key -------------------------------------------------------------

def test_content_addressed_review_resolves_without_hashing(tmp_path):
    """A capture already in the landing zone carries its identity in its path. Reading it off the
    path rather than re-hashing is what keeps the backfill affordable on the common case."""
    digest = "a" * 64
    _add_review(f"{digest[:2]}/{digest[2:4]}/{digest}.jpg", ["deer"])
    rep = reviews.backfill_asset_ids()
    assert rep.from_path == 1 and rep.hashed == 0 and rep.missing == 0
    assert reviews.latest_reviews()[0].asset_id == f"sha256:{digest}"


def test_export_file_is_resolved_by_hashing_its_bytes(tmp_path):
    """The 1,700-file case: a vendor export dropped in a named directory has no identity in its
    path, so the bytes are the only handle."""
    src = paths.assets_dir() / "2026-08-16-reveal-export" / "img.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"real-jpeg-bytes")
    _add_review("2026-08-16-reveal-export/img.jpg", ["bear"])
    rep = reviews.backfill_asset_ids()
    assert rep.hashed == 1 and rep.from_path == 0
    assert reviews.latest_reviews()[0].asset_id == compute_asset_id(src)


def test_missing_file_is_reported_not_guessed():
    _add_review("gone/vanished.jpg", ["deer"])
    rep = reviews.backfill_asset_ids()
    assert rep.missing == 1 and rep.missing_examples == ["gone/vanished.jpg"]
    # Unjoinable, so it cannot appear in the by-asset view -- but it must still be countable,
    # or it leaves the store silently. That is what count_unjoinable is for.
    assert reviews.latest_reviews() == []
    assert reviews.count_unjoinable() == 1


def test_backfill_is_idempotent():
    digest = "b" * 64
    _add_review(f"{digest[:2]}/{digest[2:4]}/{digest}.jpg", ["deer"])
    reviews.backfill_asset_ids()
    second = reviews.backfill_asset_ids()
    assert second.already == 1 and second.from_path == 0


def test_dry_run_writes_nothing():
    digest = "c" * 64
    _add_review(f"{digest[:2]}/{digest[2:4]}/{digest}.jpg", ["deer"])
    reviews.backfill_asset_ids(dry_run=True)
    assert reviews.count_unjoinable() == 1, "still unjoined, so nothing was written"


def test_only_the_latest_review_per_capture_counts():
    """The review app is insert-only and the newest row wins. A reader that does not dedupe
    double-counts every capture P looked at twice."""
    digest = "d" * 64
    img = f"{digest[:2]}/{digest[2:4]}/{digest}.jpg"
    _add_review(img, ["deer"], reviewed_at="2026-08-16T00:00:00Z")
    _add_review(img, ["empty"], reviewed_at="2026-08-17T00:00:00Z")
    reviews.backfill_asset_ids()
    rv = reviews.latest_reviews()
    assert len(rv) == 1 and rv[0].tags == frozenset({"empty"})


def test_one_capture_reviewed_under_two_filenames_resolves_to_one_verdict():
    """The case that forced identity onto the hash: a bulk-export capture reviewed under its
    vendor filename, ingested, then reviewed again under its content-addressed name. Grouping by
    basename yields two rows that disagree about whether it was reviewed at all."""
    src = paths.assets_dir() / "2026-08-16-reveal-export" / "vendor-name.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"the-same-bytes")
    digest = compute_asset_id(src).split(":", 1)[1]
    ca = paths.assets_dir() / digest[:2] / digest[2:4] / f"{digest}.jpg"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_bytes(b"the-same-bytes")

    _add_review("2026-08-16-reveal-export/vendor-name.jpg", ["deer"],
                reviewed_at="2026-08-16T00:00:00Z")
    _add_review(f"{digest[:2]}/{digest[2:4]}/{digest}.jpg", ["empty"],
                reviewed_at="2026-08-20T00:00:00Z")
    reviews.backfill_asset_ids()

    rv = reviews.latest_reviews()
    assert len(rv) == 1, "two filenames, one capture, one verdict"
    assert rv[0].tags == frozenset({"empty"}), "the newer verdict wins"


# --- the detector layer: only possible because reviews are complete -----------

def test_empty_verdict_with_a_detection_is_a_false_positive():
    """The number that justifies a confidence floor. Requires a true negative, which the tag
    store structurally cannot provide."""
    _seed_run()
    aid = "sha256:" + "e" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "blacktail")
        conn.commit()
    _add_review(f"ee/ee/{'e' * 64}.jpg", ["empty"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["detector"]["fp"] == 1
    assert rep["detector"]["tp"] == 0 and rep["detector"]["fn"] == 0
    assert rep["false_triggers"][0]["pipeline"] == ["blacktail"]


def test_animal_the_detector_never_found_is_a_false_negative():
    _seed_run()
    aid = "sha256:" + "f" * 64
    with db.detections() as conn:
        _capture(conn, aid, empty=1, n=0)
        conn.commit()
    _add_review(f"ff/ff/{'f' * 64}.jpg", ["bear"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["detector"]["fn"] == 1
    assert rep["detector_misses"][0]["human"] == ["bear"]


def test_empty_verdict_with_no_detection_is_a_true_negative():
    _seed_run()
    aid = "sha256:" + "1" * 64
    with db.detections() as conn:
        _capture(conn, aid, empty=1, n=0)
        conn.commit()
    _add_review(f"11/11/{'1' * 64}.jpg", ["empty"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["detector"]["tn"] == 1
    assert rep["detector"]["false_positive_rate"] == 0.0


# --- the classifier layer -----------------------------------------------------

def test_vocabulary_gap_is_not_scored_as_a_disagreement():
    """P tags 'deer'; the classifier says 'blacktail'. Scoring that as an error would measure the
    vocabulary gap rather than the pipeline."""
    _seed_run()
    aid = "sha256:" + "2" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "blacktail")
        conn.commit()
    _add_review(f"22/22/{'2' * 64}.jpg", ["deer"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["agree"] == 1 and rep["classifier"]["accuracy"] == 1.0


def test_a_real_misidentification_is_scored_and_lands_in_the_confusion_matrix():
    _seed_run()
    aid = "sha256:" + "3" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "blacktail")
        conn.commit()
    _add_review(f"33/33/{'3' * 64}.jpg", ["bear"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["disagree"] == 1
    assert rep["classifier"]["confusion"][0] == {"pipeline": "deer", "human": "bear", "n": 1}


def test_unknown_felid_does_not_score_as_a_correct_lion_call():
    """Deliberately strict. Invariant 4 keeps a rolled-up felid reaching review; counting it as a
    correct lion identification would hide the classifier weakness that matters most."""
    _seed_run()
    aid = "sha256:" + "4" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "unknown_felid")
        conn.commit()
    _add_review(f"44/44/{'4' * 64}.jpg", ["mountain-lion"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["disagree"] == 1


def test_detector_miss_is_not_also_charged_as_a_naming_error():
    """A capture the detector never fired on has nothing to name. Counting it in both layers
    would double-charge one failure and make the classifier look worse than it is."""
    _seed_run()
    aid = "sha256:" + "5" * 64
    with db.detections() as conn:
        _capture(conn, aid, empty=1, n=0)
        conn.commit()
    _add_review(f"55/55/{'5' * 64}.jpg", ["deer"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["detector"]["fn"] == 1
    assert rep["classifier"]["scored_captures"] == 0
    assert rep["classifier"]["declined_to_name"] == 0, \
        "nothing was detected, so the classifier was never asked and must not be charged"
    assert rep["classifier"]["per_taxon"] == {}


def test_unsure_verdicts_are_excluded_from_both_layers():
    """An unsure verdict is an absence of ground truth, not evidence against the pipeline."""
    _seed_run()
    aid = "sha256:" + "6" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "blacktail")
        conn.commit()
    _add_review(f"66/66/{'6' * 64}.jpg", ["unsure"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["coverage"]["excluded_unsure"] == 1
    assert rep["coverage"]["scored"] == 0


def test_multi_species_capture_agrees_on_overlap():
    """A frame can hold a deer and a squirrel; the reviewer tags both. Requiring set equality
    would score a partially-correct call as a total miss."""
    _seed_run()
    aid = "sha256:" + "7" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=2)
        _detection(conn, "d1", aid, "blacktail")
        _detection(conn, "d2", aid, "western_gray_squirrel")
        conn.commit()
    _add_review(f"77/77/{'7' * 64}.jpg", ["deer", "squirrel"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["agree"] == 1
    assert rep["classifier"]["per_taxon"]["deer"]["tp"] == 1
    assert rep["classifier"]["per_taxon"]["squirrel"]["tp"] == 1


def test_vehicle_is_scored_from_the_detector_class_not_the_taxon():
    """Stage 2 only classifies detector_class='animal', so a correctly-detected truck carries a
    NULL taxon forever. Reading `taxon` alone reports the pipeline getting vehicles right as
    getting them wrong -- it was 31 of 35 on the real data before this was fixed."""
    _seed_run()
    aid = "sha256:" + "b" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, None, cls="vehicle")
        conn.commit()
    _add_review(f"bb/bb/{'b' * 64}.jpg", ["vehicle"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["agree"] == 1
    assert rep["classifier"]["per_taxon"]["vehicle"]["tp"] == 1


def test_blank_is_a_declined_naming_not_a_confusion():
    """SpeciesNet returning 'blank' over a real bobcat is a recall failure, not the pipeline
    mistaking a bobcat for a species called 'blank'. It must not enter the confusion matrix, and
    it must still count against bobcat recall."""
    _seed_run()
    aid = "sha256:" + "c" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "blank")
        conn.commit()
    _add_review(f"cc/cc/{'c' * 64}.jpg", ["bobcat"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["declined_to_name"] == 1
    assert rep["classifier"]["disagree"] == 0
    assert rep["classifier"]["confusion"] == []
    assert rep["classifier"]["per_taxon"]["bobcat"]["fn"] == 1
    assert rep["classifier"]["per_taxon"]["bobcat"]["recall"] == 0.0


def test_a_blank_animal_box_does_not_mask_a_correct_vehicle_call():
    """The real shape of a driveway capture: the detector boxes the vehicle AND fires an animal
    box on part of it, which the classifier calls blank. The vehicle call is still correct."""
    _seed_run()
    aid = "sha256:" + "d" * 64
    with db.detections() as conn:
        _capture(conn, aid, n=2)
        _detection(conn, "d1", aid, None, cls="vehicle")
        _detection(conn, "d2", aid, "blank", cls="animal")
        conn.commit()
    _add_review(f"dd/dd/{'d' * 64}.jpg", ["vehicle"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["classifier"]["agree"] == 1 and rep["classifier"]["disagree"] == 0


# --- coverage honesty ---------------------------------------------------------

def test_unjoined_reviews_are_reported_not_silently_dropped():
    """The original bug in one line: a scorer that cannot find its labels must say so, not
    report an accuracy computed over nothing."""
    _seed_run()
    _add_review("gone/vanished.jpg", ["deer"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["coverage"]["unjoined_no_asset_id"] == 1
    assert rep["coverage"]["scored"] == 0
    assert rep["classifier"]["accuracy"] is None


def test_reviews_outside_the_run_are_counted_separately():
    """A capture reviewed but never processed by this run is a coverage gap, not a pipeline
    error -- exactly the un-ingested-export case."""
    _seed_run()
    digest = "8" * 64
    _add_review(f"88/88/{digest}.jpg", ["deer"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["coverage"]["joined_but_not_in_run"] == 1
    assert rep["coverage"]["scored"] == 0


# --- the threshold sweep ------------------------------------------------------

def test_sweep_prices_a_floor_in_both_directions():
    """A floor is only defensible if what it discards is measured, not assumed."""
    _seed_run()
    empty_aid = "sha256:" + "9" * 64
    bear_aid = "sha256:" + "a1" + "0" * 62
    with db.detections() as conn:
        _capture(conn, empty_aid, n=1)
        _detection(conn, "d1", empty_aid, "blank", det_conf=0.08)
        _capture(conn, bear_aid, n=1)
        _detection(conn, "d2", bear_aid, "black_bear", det_conf=0.12)
        conn.commit()
    _add_review(f"99/99/{'9' * 64}.jpg", ["empty"])
    _add_review(f"a1/00/{'a1' + '0' * 62}.jpg", ["bear"])
    reviews.backfill_asset_ids()
    sweep = {s["floor"]: s for s in reviews.confidence_sweep(RUN)["sweep"]}
    # The floor is exclusive: "drop below 0.1" keeps a detection sitting exactly on it.
    assert sweep[0.1]["empties_removed"] == 1 and sweep[0.1]["real_captures_lost"] == 0
    # Raising it past the bear starts costing something, and the cost is named.
    assert sweep[0.15]["real_captures_lost"] == 1
    assert sweep[0.15]["lost_by_tag"] == {"bear": 1}


def test_other_animal_counts_as_presence_but_is_not_a_species_claim():
    """'other-animal' asserts something was there without naming it: it must count for the
    detector layer and be invisible to the classifier layer."""
    _seed_run()
    aid = "sha256:" + "e1" + "0" * 62
    with db.detections() as conn:
        _capture(conn, aid, n=1)
        _detection(conn, "d1", aid, "unknown_mammal")
        conn.commit()
    _add_review(f"e1/00/{'e1' + '0' * 62}.jpg", ["other-animal"])
    reviews.backfill_asset_ids()
    rep = reviews.score_against_pipeline(RUN)
    assert rep["detector"]["tp"] == 1, "presence was asserted and the detector found it"
    assert rep["classifier"]["scored_captures"] == 0
    assert rep["classifier"]["declined_to_name"] == 0


# --- run provenance (invariant 2) ---------------------------------------------

def test_resuming_a_run_on_different_detector_terms_is_refused():
    """The UPSERT rewrites the run row to the new terms, so a silent resume leaves provenance
    describing rows that were never produced that way. Measured stake: every labelled bobcat on
    this property sits at detector confidence 0.114-0.135, so a resume at 0.2 erases the species
    while the run still looks healthy."""
    _seed_run()
    with db.detections() as conn:
        with pytest.raises(db.RunProvenanceConflict, match="detector_threshold"):
            db.start_run(conn, run_id=RUN, started_at="2026-08-06T00:00:00Z",
                         detector_model="md", detector_version="1", detector_threshold=0.9)


def test_resuming_on_identical_terms_still_works():
    """The standing nightly run must keep resuming; only a change is refused."""
    _seed_run()
    with db.detections() as conn:
        db.start_run(conn, run_id=RUN, started_at="2026-08-06T00:00:00Z",
                     detector_model="md", detector_version="1", detector_threshold=0.2)
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id=?", (RUN,)).fetchone()[0] == 1


def test_a_deliberate_provenance_change_is_possible_but_explicit():
    _seed_run()
    with db.detections() as conn:
        db.start_run(conn, run_id=RUN, started_at="2026-08-06T00:00:00Z",
                     detector_model="md", detector_version="2", detector_threshold=0.9,
                     allow_provenance_change=True)
        row = conn.execute("SELECT detector_threshold FROM runs WHERE run_id=?",
                           (RUN,)).fetchone()
        assert row["detector_threshold"] == 0.9
