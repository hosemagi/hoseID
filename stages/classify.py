#!/usr/bin/env python
"""Stage 2 -- SpeciesNet 5.0.5, MPS, USA/CA geofence, fed detector crops.

Runs in .venv-classifier (protobuf 7.35.1). Must not import anything from the detector
environment; the two have an unresolvable protobuf conflict (findings §A9).

THE CROP REQUIREMENT IS NOT OPTIONAL. Findings §A2: 92.7% accuracy on detector crops versus
29.2% on full frames. The classifier is Google's `always_crop` EfficientNetV2-M -- handed a full
frame it returns `blank` at 0.99 confidence, i.e. it fails silently and confidently. The
assertion below exists so that failure mode can never recur unnoticed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoseid import db, paths, taxonomy  # noqa: E402

CLASSIFIER_MODEL = "speciesnet-5.0.5"
CLASSIFIER_KAGGLE = "kaggle:google/speciesnet/pyTorch/v4.0.1a"


class FullFrameError(RuntimeError):
    """Stage 2 was handed something that is not a detector crop."""


class Geofencer:
    """Applies the geofence and taxonomic roll-up to raw classifier output.

    This must be done explicitly. SpeciesNet's `components="classifier"` mode returns raw
    classification scores and applies NO geofencing -- passing country/admin1_region to
    `classify()` in that mode is silently a no-op (verified: identical output for no-geofence,
    USA, USA/CA and USA/PA). Only the full ensemble applies it, and the ensemble insists on
    running its own bundled MegaDetector v5a, which would duplicate stage 1 and override our
    detector choice.

    So we keep our detector, run the classifier on our crops, and apply the geofence ourselves
    using the same maps the ensemble would have used. This is what makes the geofence recorded in
    the run provenance actually true.

    Effect, per findings §A1: in CA this suppresses white-tailed deer (rolling it up to genus
    `odocoileus species`) while leaving mountain lion allowed.
    """

    def __init__(self, net, country: str | None, admin1: str | None, enabled: bool = True):
        import json as _json

        from speciesnet.ensemble import _load_taxonomy_from_file
        from speciesnet.geofence_utils import geofence_animal_classification

        self._fn = geofence_animal_classification
        self.country = country
        self.admin1 = admin1
        self.enabled = enabled
        info = net.classifier.model_info
        # Same loaders the ensemble uses: taxonomy ships as a text file, geofence as JSON.
        self.taxonomy_map = _load_taxonomy_from_file(info.taxonomy)
        with open(info.geofence, encoding="utf-8") as fp:
            self.geofence_map = _json.load(fp)

    def apply(self, labels: list[str], scores: list[float]) -> tuple[str, float, str]:
        return self._fn(
            labels=labels, scores=scores, country=self.country, admin1_region=self.admin1,
            taxonomy_map=self.taxonomy_map, geofence_map=self.geofence_map,
            enable_geofence=self.enabled,
        )


def _assert_is_crop(path: Path, detection_id: str) -> None:
    """Guard the §A2 failure mode.

    Crops live under derived/crops/ and are written by stage 1 keyed by detection_id. Anything
    outside that tree -- in particular anything under landing/assets/ -- is a full frame and
    would produce confident garbage.
    """
    try:
        path.resolve().relative_to(paths.crops_dir().resolve())
    except ValueError:
        raise FullFrameError(
            f"stage 2 received {path}, which is not under {paths.crops_dir()}. "
            "SpeciesNet is an always-crop model: full frames return 'blank' at ~0.99 "
            "confidence (findings §A2, 92.7% on crops vs 29.2% on full frames)."
        ) from None
    if path.stem != detection_id:
        raise FullFrameError(f"crop {path} does not correspond to detection {detection_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 2: species classification of detector crops")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--country", default="USA")
    ap.add_argument("--admin1", default="CA",
                    help="CA keeps mountain lion allowed and suppresses white-tailed deer "
                         "(findings §A1). Never used to filter what is loggable (invariant 4).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reclassify", action="store_true")
    ap.add_argument("--no-geofence", action="store_true",
                    help="disable the regional prior (for measuring its effect)")
    args = ap.parse_args()

    from speciesnet import SpeciesNet

    net = SpeciesNet(CLASSIFIER_KAGGLE, components="classifier", multiprocessing=False)
    geofencer = Geofencer(net, args.country, args.admin1, enabled=not args.no_geofence)
    tmap_version = taxonomy.map_version()

    with db.detections() as conn:
        conn.execute(
            """UPDATE runs SET classifier_model=?, classifier_version=?,
               geofence_country=?, geofence_admin1=?, taxon_map_version=? WHERE run_id=?""",
            (CLASSIFIER_MODEL, "v4.0.1a", args.country, args.admin1, tmap_version, args.run_id))
        conn.commit()

        q = """SELECT detection_id, crop_path FROM detections
               WHERE run_id=? AND detector_class='animal' AND crop_path IS NOT NULL"""
        if not args.reclassify:
            q += " AND taxon IS NULL"
        q += " ORDER BY detection_id"
        rows = conn.execute(q, (args.run_id,)).fetchall()
        if args.limit:
            rows = rows[: args.limit]

        n = 0
        t_start = time.time()
        for r in rows:
            crop = paths.crops_dir() / r["crop_path"]
            _assert_is_crop(crop, r["detection_id"])
            if not crop.exists():
                print(f"  MISSING CROP {crop}", file=sys.stderr)
                continue

            inst = {"filepath": str(crop), "country": args.country}
            if args.admin1:
                inst["admin1_region"] = args.admin1
            try:
                out = net.classify(instances_dict={"instances": [inst]}, progress_bars=False)
                pred = out["predictions"][0]
            except Exception as e:
                print(f"  CLASSIFY FAILED {r['detection_id']}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                continue

            cls = pred.get("classifications", {}) or {}
            labels = list(cls.get("classes") or [])
            scores = [float(s) for s in (cls.get("scores") or [])]
            if not labels:
                print(f"  NO CLASSIFICATION {r['detection_id']}", file=sys.stderr)
                continue

            # Apply the geofence + roll-up ourselves; classifier-only mode does not.
            taxon_raw, score, source = geofencer.apply(labels, scores)
            score = float(score)
            mapped = taxonomy.map_taxon(taxon_raw, score)

            conn.execute(
                """UPDATE detections SET taxon_raw=?, taxon=?, taxon_confidence=?,
                   review_priority=?, classified_at=? WHERE detection_id=?""",
                (taxon_raw, mapped.taxon, score, mapped.review_priority,
                 datetime.now(timezone.utc).isoformat(), r["detection_id"]))
            n += 1
            if n % 50 == 0:
                conn.commit()
                print(f"  {n}/{len(rows)}", flush=True)
        conn.commit()

    elapsed = time.time() - t_start
    print(json.dumps({"stage": "classify", "run_id": args.run_id, "classified": n,
                      "geofence": f"{args.country}/{args.admin1}",
                      "taxon_map_version": tmap_version,
                      "img_per_sec": round(n / elapsed, 2) if elapsed > 0 and n else None},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
