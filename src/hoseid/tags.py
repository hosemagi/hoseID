"""The tag store.

Written only by the review path. The pipeline never writes here (invariant 3).

Two properties that are easy to lose later and expensive to recover:

* Tags are SPARSE POSITIVE LABELS (invariant 5). The absence of a tag means "not tagged", never
  "not present". `score_against_pipeline` therefore only ever scores detections that carry at
  least one species-like tag, and never counts an untagged detection as a pipeline error.
* Tags anchor to detections, never to encounters. Encounter grouping is derived downstream from
  a gap threshold that will be retuned; encounter-anchored tags would orphan on every retune.
  Bulk-apply fans out across member detections instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from . import db

_WS = re.compile(r"\s+")


def normalise(tag: str) -> str:
    """Lowercase and collapse whitespace.

    A fixed vocabulary would be wrong by November, but `Ben` / `ben` / `Big Ben` recorded as
    three different animals is worse. Normalise the trivially-equivalent, allow the rest, and
    let autocomplete do the nudging.
    """
    return _WS.sub(" ", tag.strip().lower())


@dataclass(frozen=True)
class Tag:
    detection_id: str
    tag: str
    added_at: str
    added_by: str
    note: str | None


def add_tags(detection_id: str, tags: Iterable[str], *, added_by: str = "p",
             note: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [(detection_id, normalise(t), now, added_by, note) for t in tags if normalise(t)]
    if not rows:
        return 0
    with db.tags() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO tags (detection_id, tag, added_at, added_by, note)
               VALUES (?,?,?,?,?)""", rows)
        conn.commit()
        return conn.total_changes


def bulk_apply(detection_ids: Iterable[str], tags: Iterable[str], *, added_by: str = "p",
               note: str | None = None) -> int:
    """Apply a tag set across many detections -- 'all of these are Ben'.

    Fans out to individual detection rows rather than creating a group object, so a later change
    to encounter grouping cannot orphan the labels.
    """
    tags = list(tags)
    total = 0
    for did in detection_ids:
        total += add_tags(did, tags, added_by=added_by, note=note)
    return total


def remove_tag(detection_id: str, tag: str) -> int:
    with db.tags() as conn:
        cur = conn.execute("DELETE FROM tags WHERE detection_id=? AND tag=?",
                           (detection_id, normalise(tag)))
        conn.commit()
        return cur.rowcount


def tags_for(detection_id: str) -> list[Tag]:
    with db.tags() as conn:
        rows = conn.execute(
            "SELECT * FROM tags WHERE detection_id=? ORDER BY added_at", (detection_id,)).fetchall()
    return [Tag(r["detection_id"], r["tag"], r["added_at"], r["added_by"], r["note"]) for r in rows]


def vocabulary(limit: int = 200) -> list[tuple[str, int, str]]:
    """Backs autocomplete: existing tags, most-used first."""
    with db.tags() as conn:
        rows = conn.execute("SELECT tag, n, last_used FROM tag_vocabulary LIMIT ?",
                            (limit,)).fetchall()
    return [(r["tag"], r["n"], r["last_used"]) for r in rows]


# --- the accumulating eval set ------------------------------------------------
# Tags double as corrections: a `coyote` tag on a detection the pipeline called `mule deer` is
# the same mechanism as a `ben` tag. That makes the tag store a growing labelled set, and this
# join is the regression suite for any future model change. It costs nothing to build now and
# is impossible to reconstruct retroactively.

def score_against_pipeline(run_id: str, species_tags: set[str] | None = None) -> dict:
    """Compare pipeline taxa against human tags for detections that carry a species-like tag.

    species_tags restricts scoring to tags that name a species. Without it, individual-animal
    tags ("ben") and attribute tags ("antlered") would be scored as if they were species
    predictions. Defaults to the taxa the pipeline itself can emit.
    """
    from .taxonomy import _load, DEFAULT_CONFIG
    if species_tags is None:
        cfg = _load(str(DEFAULT_CONFIG))
        # Skip `_`-prefixed documentation keys; only real mapping entries carry a taxon.
        species_tags = {v["taxon"] for k, v in cfg["map"].items()
                        if not k.startswith("_") and isinstance(v, dict) and "taxon" in v}
        species_tags |= {r["taxon"] for r in cfg["fallbacks"]["rules"]}
        species_tags.add(cfg["fallbacks"]["default"])

    with db.detections() as dconn:
        preds = {r["detection_id"]: (r["taxon"], r["taxon_confidence"])
                 for r in dconn.execute(
                     "SELECT detection_id, taxon, taxon_confidence FROM detections WHERE run_id=?",
                     (run_id,)).fetchall()}
    with db.tags() as tconn:
        rows = tconn.execute("SELECT detection_id, tag FROM tags").fetchall()

    by_det: dict[str, set[str]] = {}
    for r in rows:
        by_det.setdefault(r["detection_id"], set()).add(r["tag"])

    agree = disagree = 0
    unscored_no_species_tag = 0
    confusion: dict[tuple[str, str], int] = {}
    for did, tagset in by_det.items():
        if did not in preds:
            continue
        truth = {t for t in tagset if t in species_tags}
        if not truth:
            # Tagged, but not with a species -- invariant 5: absence of a species tag is not
            # evidence the pipeline was wrong.
            unscored_no_species_tag += 1
            continue
        pred = preds[did][0]
        if pred in truth:
            agree += 1
        else:
            disagree += 1
            confusion[(pred or "none", sorted(truth)[0])] = \
                confusion.get((pred or "none", sorted(truth)[0]), 0) + 1

    scored = agree + disagree
    return {
        "run_id": run_id,
        "scored_detections": scored,
        "agree": agree,
        "disagree": disagree,
        "accuracy": round(agree / scored, 4) if scored else None,
        "tagged_but_no_species_tag": unscored_no_species_tag,
        "confusion": sorted(({"pipeline": k[0], "human": k[1], "n": v}
                             for k, v in confusion.items()), key=lambda d: -d["n"]),
        "_note": "Scored only over detections carrying a species tag. Untagged detections are "
                 "not counted as errors -- tags are sparse positive labels (invariant 5).",
    }
