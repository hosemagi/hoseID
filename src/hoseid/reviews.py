"""The review store: complete human verdicts, and scoring the pipeline against them.

This is the second human-label store, and it is NOT the tag store. The distinction is the whole
reason this module exists separately from `tags.py`, and getting it wrong in either direction
produces confidently wrong accuracy numbers:

    tags     (tags.py)     SPARSE POSITIVE labels, keyed by detection_id.
                           Absence means "not tagged" (invariant 5).

    reviews  (this module) COMPLETE VERDICTS, keyed by capture.
                           The reviewer looked at the whole frame and recorded everything in it.
                           Absence of a species DOES mean "not present", and an explicit
                           ``empty`` verdict is a real negative.

That difference is what makes this store able to answer questions the tag store cannot: how often
the detector fires on nothing, and how often it misses something real. Both require true
negatives, which sparse positive labels do not have.

Written by the review app (`app.py`), never by the pipeline. This module only reads it -- with
the single exception of `backfill_asset_ids`, which fills in a join key the review app never
recorded and which is derived, not judged.

WHY THE BACKFILL EXISTS. The review app addresses images by filename, because it reviews files on
disk. The pipeline addresses captures by content hash. There was no column joining the two, so
2,334 human verdicts and the pipeline output they describe could not be compared at all -- and
`hoseid score` read the empty `tags` table instead and reported an accuracy of zero findings
rather than an error. `asset_id` is that missing join.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from . import db, paths
from .sidecar import compute_asset_id

VOCAB_CONFIG = Path(__file__).resolve().parents[2] / "config" / "review_vocab.json"


# --- vocabulary ---------------------------------------------------------------

@lru_cache(maxsize=4)
def _vocab(path_str: str) -> dict:
    return json.loads(Path(path_str).read_text())


def vocab_version(config_path: Path | None = None) -> str:
    return str(_vocab(str(config_path or VOCAB_CONFIG)).get("version", "0"))


def _taxon_to_group(cfg: dict) -> dict[str, str]:
    """Reverse index: pipeline taxon -> human tag whose group contains it."""
    out: dict[str, str] = {}
    for tag, spec in cfg["groups"].items():
        for taxon in spec["taxa"]:
            out[taxon] = tag
    return out


# --- reading the store --------------------------------------------------------

@dataclass(frozen=True)
class Review:
    """One capture's verdict, as the reviewer recorded it."""
    basename: str
    image: str
    asset_id: str | None
    device_id: str | None
    captured_at: str | None
    tags: frozenset[str]
    md_max_conf: float | None
    reviewed_at: str
    reviewer: str

    @property
    def is_empty(self) -> bool:
        return "empty" in self.tags

    @property
    def is_unsure(self) -> bool:
        return "unsure" in self.tags


def _row_to_review(r: sqlite3.Row) -> Review:
    keys = r.keys()
    try:
        tags = frozenset(json.loads(r["tags"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        tags = frozenset()
    return Review(
        basename=r["basename"],
        image=r["image"],
        asset_id=r["asset_id"] if "asset_id" in keys else None,
        device_id=r["device_id"],
        captured_at=r["captured_at"],
        tags=tags,
        md_max_conf=r["md_max_conf"],
        reviewed_at=r["reviewed_at"],
        reviewer=r["reviewer"],
    )


# The review app is insert-only and takes the newest row per basename as current. Any read that
# does not do the same double-counts every re-reviewed capture.
_LATEST = """
    SELECT r.* FROM reviews r
    JOIN (SELECT basename, MAX(id) AS id FROM reviews GROUP BY basename) m
      ON m.id = r.id
"""


def latest_reviews() -> list[Review]:
    with db.tags(create=False) as conn:
        _ensure_asset_id_column(conn)
        return [_row_to_review(r) for r in conn.execute(_LATEST).fetchall()]


# --- the missing join key -----------------------------------------------------

def _ensure_asset_id_column(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER, deliberately not a numbered migration.

    `reviews` is created and owned by the review app's own DDL, which runs `CREATE TABLE IF NOT
    EXISTS` and never touches `PRAGMA user_version` -- the counter that hoseid's migration runner
    keys on is still 0 on the live tag database even though the table has existed for weeks.
    Adding this column through the numbered chain would therefore replay 001 as well, creating a
    vestigial `tags` table as a side effect of asking a question. Matching the review app's own
    idempotent-DDL style keeps the two writers from fighting over one schema counter.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()}
    if "asset_id" not in cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN asset_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_asset ON reviews(asset_id)")
        conn.commit()


def _asset_id_for(image_rel: str) -> tuple[str | None, str]:
    """Resolve one review's image path to an asset_id. Returns (asset_id, how).

    Two populations live side by side under landing/assets, and they resolve differently:

    * Content-addressed captures (`aa/bb/<digest>.jpg`) carry their own identity in the filename.
      Reading it off the path is exact and free -- no hashing.
    * Bulk vendor-export files sitting in a named directory have no identity in their path, so
      the bytes have to be hashed. That is the slow path and the reason this is a batch command
      rather than something done at query time.
    """
    p = paths.assets_dir() / image_rel
    parts = Path(image_rel).parts
    stem = Path(image_rel).stem
    # aa/bb/<64-hex>.ext -- the content-addressed layout, shard dirs and all.
    if (len(parts) == 3 and len(parts[0]) == 2 and len(parts[1]) == 2
            and len(stem) == 64 and all(c in "0123456789abcdef" for c in stem)
            and parts[0] == stem[:2] and parts[1] == stem[2:4]):
        return f"sha256:{stem}", "path"
    if not p.exists():
        return None, "missing"
    return compute_asset_id(p), "hashed"


@dataclass
class BackfillReport:
    total: int = 0
    already: int = 0
    from_path: int = 0
    hashed: int = 0
    missing: int = 0
    missing_examples: list[str] = None

    def __post_init__(self):
        if self.missing_examples is None:
            self.missing_examples = []


def backfill_asset_ids(*, dry_run: bool = False, rehash: bool = False) -> BackfillReport:
    """Fill `reviews.asset_id` so human verdicts can join the pipeline's captures.

    Derived, not judged: an asset_id is a fact about the bytes, so writing it into the review
    store adds no human judgement and cannot corrupt a label. Existing values are left alone
    unless `rehash` is set.
    """
    rep = BackfillReport()
    with db.tags(create=False) as conn:
        _ensure_asset_id_column(conn)
        rows = conn.execute(f"SELECT id, image, asset_id FROM ({_LATEST})").fetchall()
        updates: list[tuple[str, int]] = []
        for r in rows:
            rep.total += 1
            if r["asset_id"] and not rehash:
                rep.already += 1
                continue
            aid, how = _asset_id_for(r["image"])
            if aid is None:
                rep.missing += 1
                if len(rep.missing_examples) < 10:
                    rep.missing_examples.append(r["image"])
                continue
            setattr(rep, "from_path" if how == "path" else "hashed",
                    getattr(rep, "from_path" if how == "path" else "hashed") + 1)
            updates.append((aid, r["id"]))
        if updates and not dry_run:
            conn.executemany("UPDATE reviews SET asset_id=? WHERE id=?", updates)
            conn.commit()
    return rep


# --- scoring ------------------------------------------------------------------

def _capture_rows(run_id: str) -> tuple[dict[str, sqlite3.Row], dict[str, list[sqlite3.Row]]]:
    with db.detections() as conn:
        caps = {r["asset_id"]: r for r in conn.execute(
            "SELECT * FROM captures WHERE run_id=?", (run_id,)).fetchall()}
        dets: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for d in conn.execute(
                "SELECT * FROM detections WHERE run_id=?", (run_id,)).fetchall():
            dets[d["asset_id"]].append(d)
    return caps, dets


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else None)
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
    }


def score_against_pipeline(run_id: str, config_path: Path | None = None) -> dict:
    """Score a pipeline run against P's review verdicts.

    Reported in two layers, because they fail independently and have different fixes:

    * **detector** -- did anything get found? Scored over every reviewed capture, using the
      ``empty`` verdict as a true negative. This is the layer that answers "how much of the
      review queue is the detector firing at nothing" and "what would a confidence floor cost".
    * **classifier** -- was it named correctly? Scored only over captures where both sides name
      a species, so a detector miss is not also counted as a classification error.

    Captures the reviewer marked ``unsure`` are excluded from both and counted separately: an
    unsure verdict is an absence of ground truth, not evidence against the pipeline.
    """
    cfg = _vocab(str(config_path or VOCAB_CONFIG))
    roles = {k: v for k, v in cfg["roles"].items() if not k.startswith("_")}
    groups = cfg["groups"]
    taxon_group = _taxon_to_group(cfg)
    det_class_group = {k: v for k, v in cfg["detector_class_groups"].items()
                       if not k.startswith("_")}
    declined = set(cfg["declined_to_name"]["taxa"])

    caps, dets = _capture_rows(run_id)
    reviews = latest_reviews()

    unjoined = 0            # reviewed, but no asset_id -> cannot be matched at all
    not_in_run = 0          # joined, but this run never processed that capture
    excluded_unsure = 0

    det_tp = det_fp = det_fn = det_tn = 0
    cls_agree = cls_disagree = 0
    cls_unscorable = 0
    confusion: dict[tuple[str, str], int] = defaultdict(int)
    per_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    fn_examples: list[dict[str, Any]] = []
    fp_examples: list[dict[str, Any]] = []

    for rv in reviews:
        if rv.is_unsure:
            excluded_unsure += 1
            continue
        if not rv.asset_id:
            unjoined += 1
            continue
        cap = caps.get(rv.asset_id)
        if cap is None:
            not_in_run += 1
            continue

        human_tags = {t for t in rv.tags if t in groups}
        # "other-animal" asserts presence without naming it: it counts for the detector layer
        # (something was there) but cannot be scored by the classifier, which is why it is a role
        # rather than a group.
        unspecified = {t for t, role in roles.items() if role == "animal_unspecified"}
        human_says_something = bool(human_tags or (rv.tags & unspecified))
        pipeline_found = bool(dets.get(rv.asset_id))

        # --- detector layer: presence, not identity ---------------------------
        if human_says_something and pipeline_found:
            det_tp += 1
        elif human_says_something and not pipeline_found:
            det_fn += 1
            if len(fn_examples) < 20:
                fn_examples.append({"asset_id": rv.asset_id, "station": cap["station"],
                                    "captured_at": rv.captured_at,
                                    "human": sorted(rv.tags)})
        elif not human_says_something and pipeline_found:
            det_fp += 1
            if len(fp_examples) < 20:
                fp_examples.append({
                    "asset_id": rv.asset_id, "station": cap["station"],
                    "captured_at": rv.captured_at,
                    "pipeline": sorted({d["taxon"] for d in dets[rv.asset_id] if d["taxon"]}),
                    "max_detector_conf": round(
                        max(d["detector_confidence"] for d in dets[rv.asset_id]), 4)})
        else:
            det_tn += 1

        # --- classifier layer: identity, only where both sides name one -------
        if not human_tags:
            continue
        rows = dets.get(rv.asset_id, [])
        if not rows:
            # Nothing was detected, so there was nothing to name. Already counted as a detector
            # false negative above; charging it to the classifier as well would double-count one
            # failure and make the classifier look worse than it is.
            continue
        # What the pipeline actually NAMED, from both places a name can come from: the classifier
        # for animal crops, and the detector itself for people and vehicles (stage 2 never runs
        # on those, so their taxon is NULL by design, not by failure).
        predicted_groups = {taxon_group.get(d["taxon"], d["taxon"])
                            for d in rows if d["taxon"] and d["taxon"] not in declined}
        predicted_groups |= {det_class_group[d["detector_class"]] for d in rows
                             if d["detector_class"] in det_class_group}
        if not predicted_groups:
            # Something was detected but nothing was named -- the classifier declined. That is a
            # recall failure against whatever the reviewer saw, so it is charged to each human
            # tag below rather than quietly dropped, but it is not a confusion: there is no
            # competing name to confuse it with.
            cls_unscorable += 1
            for tag in human_tags:
                per_tag[tag]["fn"] += 1
            continue

        # Agreement is set overlap, not equality: a capture can legitimately hold a deer and a
        # squirrel, and the reviewer tags both.
        hit = human_tags & predicted_groups
        if hit:
            cls_agree += 1
        else:
            cls_disagree += 1
            for p in sorted(predicted_groups):
                confusion[(p, sorted(human_tags)[0])] += 1

        for tag in groups:
            in_human, in_pred = tag in human_tags, tag in predicted_groups
            if in_human and in_pred:
                per_tag[tag]["tp"] += 1
            elif in_pred and not in_human:
                per_tag[tag]["fp"] += 1
            elif in_human and not in_pred:
                per_tag[tag]["fn"] += 1

    det_scored = det_tp + det_fp + det_fn + det_tn
    cls_scored = cls_agree + cls_disagree

    return {
        "run_id": run_id,
        "taxon_map_version": None,
        "review_vocab_version": vocab_version(config_path),
        "reviews_total": len(reviews),
        "coverage": {
            "scored": det_scored,
            "excluded_unsure": excluded_unsure,
            "unjoined_no_asset_id": unjoined,
            "joined_but_not_in_run": not_in_run,
            "_note": "unjoined_no_asset_id > 0 means `hoseid backfill-reviews` has not been run "
                     "or the file is gone. joined_but_not_in_run means the capture exists in the "
                     "review store but this run never processed it -- usually un-ingested files.",
        },
        "detector": {
            **_prf(det_tp, det_fp, det_fn),
            "tn": det_tn,
            "false_positive_rate": round(det_fp / (det_fp + det_tn), 4) if (det_fp + det_tn) else None,
            "_note": "Presence only. tp = reviewer saw something and the detector fired. "
                     "fp = reviewer marked the capture empty and the detector fired anyway. "
                     "fn = reviewer saw something the detector missed entirely.",
        },
        "classifier": {
            "scored_captures": cls_scored,
            "agree": cls_agree,
            "disagree": cls_disagree,
            "accuracy": round(cls_agree / cls_scored, 4) if cls_scored else None,
            "declined_to_name": cls_unscorable,
            "per_taxon": {t: _prf(v["tp"], v["fp"], v["fn"])
                          for t, v in sorted(per_tag.items())
                          if v["tp"] or v["fp"] or v["fn"]},
            "confusion": sorted(({"pipeline": k[0], "human": k[1], "n": v}
                                 for k, v in confusion.items()), key=lambda d: -d["n"]),
            "_note": "accuracy is over captures where the pipeline named something. "
                     "declined_to_name counts captures where it detected something, the reviewer "
                     "named it, and the classifier returned only 'blank' -- excluded from "
                     "accuracy (there is no competing name to be wrong about) but charged "
                     "against that tag's recall in per_taxon, so it cannot hide there either.",
        },
        "detector_misses": fn_examples,
        "false_triggers": fp_examples,
    }


def confidence_sweep(run_id: str, config_path: Path | None = None) -> dict:
    """What a detector-confidence floor would cost and save, measured on real verdicts.

    The review queue is dominated by captures the reviewer marked empty. The obvious lever is to
    stop showing the ones the detector was least sure about -- but "least sure" has to be priced
    against what it discards, and the only honest price is measured on labelled captures rather
    than assumed.

    Reports, for each candidate floor: how many reviewed captures drop out, how many of those
    were genuinely empty (the saving), and how many held something real (the cost), split out by
    what the human said it was -- because losing a squirrel is not the same as losing a bear.
    """
    caps, dets = _capture_rows(run_id)
    cfg = _vocab(str(config_path or VOCAB_CONFIG))
    groups = cfg["groups"]

    rows = []
    for rv in latest_reviews():
        if rv.is_unsure or not rv.asset_id or rv.asset_id not in caps:
            continue
        d = dets.get(rv.asset_id, [])
        if not d:
            continue                       # nothing to threshold; never in the queue anyway
        rows.append((max(x["detector_confidence"] for x in d),
                     rv.is_empty, sorted({t for t in rv.tags if t in groups})))

    out = []
    for floor in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        dropped = [r for r in rows if r[0] < floor]
        lost = [r for r in dropped if not r[1]]
        by_tag: dict[str, int] = defaultdict(int)
        for _, _, tags in lost:
            for t in (tags or ["(untagged non-empty)"]):
                by_tag[t] += 1
        out.append({
            "floor": floor,
            "captures_dropped": len(dropped),
            "empties_removed": len(dropped) - len(lost),
            "real_captures_lost": len(lost),
            "lost_by_tag": dict(sorted(by_tag.items(), key=lambda kv: -kv[1])),
        })
    return {
        "run_id": run_id,
        "queue_size": len(rows),
        "empties_in_queue": sum(1 for r in rows if r[1]),
        "sweep": out,
        "_note": "Measured over reviewed captures that carry at least one detection in this run. "
                 "'real_captures_lost' is the cost: a capture the reviewer said held something, "
                 "which this floor would have hidden.",
    }
