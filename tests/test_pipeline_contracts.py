"""Tests for taxonomy mapping, review invariants, tag semantics, and the crop guard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from hoseid import db, paths, review, tags as tagstore
from hoseid.taxonomy import map_taxon

SN = "uuid;mammalia;{order};{fam};{genus};{species};{common}"


def _raw(genus, species, common, order="carnivora", fam="felidae"):
    return SN.format(order=order, fam=fam, genus=genus, species=species, common=common)


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    return tmp_path


# --- taxonomy mapping ---------------------------------------------------------
def test_mule_deer_maps_to_blacktail():
    """SpeciesNet emits 'mule deer' for O. hemionus; in D-3 the only subspecies is Columbian
    blacktail, so the mapping is unambiguous on this property."""
    r = map_taxon(_raw("odocoileus", "hemionus", "mule deer", "cetartiodactyla", "cervidae"), 0.9)
    assert r.taxon == "blacktail"
    assert "hemionus" in r.taxon_raw, "taxon_raw must always be retained"


def test_genus_level_deer_takes_the_blacktail_property_prior():
    # taxon_map v3 (P, 2026-08-16): the only deer at 95959 are blacktail, so
    # genus/family/order rollups all map to blacktail rather than
    # deer_unspecified. A species-level whitetail call still surfaces as
    # whitetail (anomaly signal) — pinned separately below.
    r = map_taxon(_raw("odocoileus", "", "odocoileus species", "cetartiodactyla", "cervidae"), 0.5)
    assert r.taxon == "blacktail"


def test_species_level_whitetail_stays_distinct_despite_the_prior():
    r = map_taxon(_raw("odocoileus", "virginianus", "white-tailed deer"), 0.9)
    assert r.taxon == "whitetail", "geofence-anomaly signal must not be folded"


def test_mountain_lion_maps_and_is_always_high_priority():
    r = map_taxon(_raw("puma", "concolor", "puma"), 0.99)
    assert r.taxon == "mountain_lion"
    assert r.review_priority == "high", "high confidence must not demote a lion"


def test_low_confidence_raises_review_priority():
    hi = map_taxon(_raw("lynx", "rufus", "bobcat"), 0.95)
    lo = map_taxon(_raw("lynx", "rufus", "bobcat"), 0.30)
    assert hi.review_priority == "normal" and lo.review_priority == "high"


def test_unmapped_species_in_a_known_family_resolves_via_family():
    """Raccoon dog is a canid: unmapped at species level, but the family is known."""
    r = map_taxon(_raw("nyctereutes", "procyonoides", "raccoon dog", fam="canidae"), 0.8)
    assert r.taxon == "unknown_canid"


def test_wholly_unrecognised_mammal_falls_back_not_discarded():
    """Neither species nor family nor order mapped -- still surfaced, never dropped."""
    r = map_taxon(_raw("suricata", "suricatta", "meerkat", order="carnivora", fam="herpestidae"), 0.8)
    assert r.taxon == "unknown_carnivore", "order-level fallback applies before the generic one"
    r2 = map_taxon("uuid;mammalia;pholidota;manidae;manis;javanica;pangolin", 0.8)
    assert r2.taxon == "unknown_mammal" and r2.review_priority == "high"


def test_bird_fallback():
    r = map_taxon("uuid;aves;passeriformes;corvidae;corvus;corax;common raven", 0.8)
    assert r.taxon == "unknown_bird"


# --- invariant 4: alerts never filtered by species ----------------------------
def _seed(run_id="r1"):
    with db.detections() as conn:
        db.start_run(conn, run_id=run_id, started_at="2026-08-05T00:00:00Z",
                     detector_model="md", detector_version="1", detector_threshold=0.2)
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
                        time_trusted, n_detections, has_animal, is_empty)
                        VALUES ('sha256:a','{r}','Crossroads','2026-08-03T04:12:07Z',1,2,1,0)"""
                     .format(r=run_id))
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
                        time_trusted, n_detections, has_animal, is_empty)
                        VALUES ('sha256:b','{r}','Saddle','2026-08-03T05:00:00Z',1,0,0,1)"""
                     .format(r=run_id))
        rows = [
            ("d_lion", "sha256:a", run_id, .1, .1, .1, .1, "animal", .95,
             "mountain_lion", 0.88, "high"),
            ("d_mis",  "sha256:a", run_id, .5, .5, .1, .1, "animal", .90,
             "blacktail", 0.55, "high"),
        ]
        conn.executemany(
            """INSERT INTO detections (detection_id, asset_id, run_id, bbox_x, bbox_y, bbox_w,
               bbox_h, detector_class, detector_confidence, taxon, taxon_confidence,
               review_priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.commit()
    return run_id


def test_every_animal_detection_is_alertable_regardless_of_taxon():
    run_id = _seed()
    ids = {i.detection_id for i in review.alertable(run_id)}
    assert ids == {"d_lion", "d_mis"}, "no species may be excluded from the alert path"


def test_a_misclassified_lion_still_reaches_review():
    """The safety property: if the classifier calls a lion a deer, it must still surface."""
    run_id = _seed()
    q = review.review_queue(run_id, limit=100)
    assert "d_mis" in {i.detection_id for i in q}


def test_review_queue_orders_by_priority_without_filtering():
    run_id = _seed()
    q = review.review_queue(run_id, limit=100)
    assert len(q) == 2
    assert all(i.review_priority == "high" for i in q[:2])


def test_empty_captures_are_recorded():
    """False-trigger rate per station is real signal about wind, vegetation and camera health."""
    run_id = _seed()
    acts = {a["station"]: a for a in review.station_activity(run_id)}
    assert acts["Saddle"]["empty_captures"] == 1
    assert acts["Saddle"]["empty_pct"] == 100.0


# --- invariant 3 & 5: tag store separate, tags are sparse positive labels ------
def test_tag_and_detection_databases_are_separate_files():
    assert paths.tags_db() != paths.detections_db()
    assert paths.tags_db().parent != paths.detections_db().parent


def test_tags_are_normalised_on_write():
    tagstore.add_tags("d1", ["  Ben ", "BEN", "Big  Ben"])
    got = {t.tag for t in tagstore.tags_for("d1")}
    assert got == {"ben", "big ben"}, "Ben/BEN collapse; Big Ben stays distinct"


def test_bulk_apply_fans_out_to_detections_not_encounters():
    tagstore.bulk_apply(["d1", "d2", "d3"], ["ben"])
    for d in ("d1", "d2", "d3"):
        assert [t.tag for t in tagstore.tags_for(d)] == ["ben"]


def test_untagged_detections_are_not_counted_as_pipeline_errors():
    """Invariant 5: absence of a tag means 'not tagged', never 'not present'."""
    run_id = _seed()
    tagstore.add_tags("d_mis", ["mountain_lion"])       # a correction
    rep = tagstore.score_against_pipeline(run_id)
    assert rep["scored_detections"] == 1, "only the tagged detection is scored"
    assert rep["disagree"] == 1 and rep["accuracy"] == 0.0
    assert rep["confusion"][0] == {"pipeline": "blacktail", "human": "mountain_lion", "n": 1}


def test_non_species_tags_do_not_score_as_predictions():
    run_id = _seed()
    tagstore.add_tags("d_lion", ["ben", "antlered"])    # individual + attribute, not species
    rep = tagstore.score_against_pipeline(run_id)
    assert rep["scored_detections"] == 0
    assert rep["tagged_but_no_species_tag"] == 1


def test_agreement_counted_when_tag_matches_pipeline():
    run_id = _seed()
    tagstore.add_tags("d_lion", ["mountain_lion"])
    rep = tagstore.score_against_pipeline(run_id)
    assert rep["agree"] == 1 and rep["accuracy"] == 1.0


# --- geofence roll-ups (the normal case under an active geofence) -------------
def _rollup(family, common, cls="mammalia", order="carnivora"):
    """Geofenced roll-up label shape: genus and species blank."""
    return f"uuid;{cls};{order};{family};;;{common}"


def test_family_rollup_of_deer_keeps_deer_meaning():
    """A whitetail in CA is geofenced to 'cervidae family'. That must not become unknown_mammal --
    the classifier was confident it was a deer and only declined the species."""
    r = map_taxon(_rollup("cervidae", "cervidae family", order="cetartiodactyla"), 0.95)
    # taxon_map v3: the family rollup takes the blacktail property prior; the
    # invariant this test protects is unchanged — a confident deer must never
    # degrade to unknown_mammal.
    assert r.taxon == "blacktail"


def test_family_rollup_of_cat_is_high_priority():
    """SAFETY: a family-level felid could be a mountain lion."""
    r = map_taxon(_rollup("felidae", "felidae family"), 0.95)
    assert r.taxon == "unknown_felid"
    assert r.review_priority == "high"


def test_order_level_carnivore_rollup_is_high_priority():
    r = map_taxon(_rollup("", "carnivora order"), 0.95)
    assert r.review_priority == "high"


def test_single_species_families_resolve_to_that_species():
    assert map_taxon(_rollup("ursidae", "ursidae family"), 0.9).taxon == "black_bear"
    assert map_taxon(_rollup("procyonidae", "procyonidae family"), 0.9).taxon == "raccoon"


def test_species_level_still_wins_over_family():
    """Genus;species must be tried before family, or every label would collapse to its family."""
    r = map_taxon(_raw("lynx", "rufus", "bobcat"), 0.95)
    assert r.taxon == "bobcat"


def test_blank_and_vehicle_labels_map():
    assert map_taxon("uuid;;;;;;blank", 0.99).taxon == "blank"
    assert map_taxon("uuid;;;;;;vehicle", 0.99).taxon == "vehicle"
