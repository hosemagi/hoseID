"""Review queries.

Invariant 4: alerts are never filtered by species. Any capture with an animal detection is
loggable and reachable in review. Species affects how an alert is phrased, never whether it
fires -- this is the property that makes a misclassified mountain lion still reach P.

Every query in this module is therefore built to *order* by priority, never to *exclude* by
taxon. There is deliberately no `species=` filter parameter on the alert path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import db


@dataclass(frozen=True)
class ReviewItem:
    detection_id: str
    asset_id: str
    station: str
    capture_time: str
    taxon: str | None
    taxon_confidence: float | None
    detector_confidence: float
    crop_path: str | None
    review_priority: str


_PRIORITY_ORDER = "CASE review_priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def review_queue(run_id: str, limit: int = 200, offset: int = 0) -> list[ReviewItem]:
    """Everything with an animal detection, ordered by priority then recency.

    Ordering only. No species predicate anywhere in this query -- see invariant 4.
    """
    sql = f"""
        SELECT d.detection_id, d.asset_id, c.station, c.capture_time, d.taxon,
               d.taxon_confidence, d.detector_confidence, d.crop_path, d.review_priority
        FROM detections d
        JOIN captures c ON c.asset_id = d.asset_id AND c.run_id = d.run_id
        WHERE d.run_id = ? AND d.detector_class = 'animal'
        ORDER BY {_PRIORITY_ORDER}, c.capture_time DESC
        LIMIT ? OFFSET ?"""
    with db.detections() as conn:
        rows = conn.execute(sql, (run_id, limit, offset)).fetchall()
    return [ReviewItem(**dict(r)) for r in rows]


def alertable(run_id: str) -> list[ReviewItem]:
    """Every animal detection in the run. Unfiltered, by design.

    Phrasing an alert is a downstream concern that may consult taxon; whether an alert exists
    is decided here and depends only on there being an animal.
    """
    return review_queue(run_id, limit=10**9)


def station_activity(run_id: str) -> list[dict[str, Any]]:
    """Per-station capture counts including empties.

    The empty/false-trigger rate is why empty captures are recorded at all: it tracks wind,
    vegetation growth into frame, and camera health, and is invisible if empties are dropped.
    """
    sql = """
        SELECT station,
               COUNT(*)                             AS captures,
               SUM(is_empty)                        AS empty_captures,
               SUM(has_animal)                      AS animal_captures,
               SUM(has_human)                       AS human_captures,
               SUM(has_vehicle)                     AS vehicle_captures,
               ROUND(AVG(is_empty) * 100, 1)        AS empty_pct,
               SUM(station_corrected)               AS corrected_rows,
               SUM(CASE WHEN time_trusted = 0 THEN 1 ELSE 0 END) AS untrusted_time_rows
        FROM captures WHERE run_id = ?
        GROUP BY station ORDER BY captures DESC"""
    with db.detections() as conn:
        return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]


def taxon_summary(run_id: str) -> list[dict[str, Any]]:
    sql = """
        SELECT COALESCE(taxon, '(unclassified)') AS taxon,
               COUNT(*) AS n,
               ROUND(AVG(taxon_confidence), 3) AS mean_confidence,
               SUM(CASE WHEN review_priority = 'high' THEN 1 ELSE 0 END) AS high_priority
        FROM detections WHERE run_id = ? AND detector_class = 'animal'
        GROUP BY taxon ORDER BY n DESC"""
    with db.detections() as conn:
        return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]
