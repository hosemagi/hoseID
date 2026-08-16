#!/usr/bin/env python
"""Bridge: run stage-2 SpeciesNet over an external MegaDetector results file.

The 2026-08-16 measurement run produced md_results.json outside the package's
detections.db (MPS run at 0.1 floor, archive in batch dirs). This script reuses
the package's classify machinery — crop convention (detect._write_crop padding),
Geofencer (classifier-only mode ignores the geofence), taxonomy roll-up — without
touching detections.db, and writes predictions as a sidecar JSON next to the MD
results. Runs in .venv-classifier.

Usage:
  .venv-classifier/bin/python scripts/classify_md_results.py \
      --md-results ~/trailcam/derived/runs/2026-08-16-md-combined/md_results.json
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from PIL import Image  # noqa: E402

from hoseid import paths, taxonomy  # noqa: E402
from stages.classify import CLASSIFIER_KAGGLE, CLASSIFIER_MODEL, Geofencer  # noqa: E402

ASSETS = Path("/Users/hosebot/trailcam/landing/assets")
PAD_FRAC = 0.15   # matches stages/detect.py --crop-padding default


def write_crop(pil, bbox, det_id) -> Path:
    # Same geometry as stages.detect._write_crop; bridge crops live under
    # crops_dir()/bridge/ so they satisfy the stage-2 crop-tree requirement.
    W, H = pil.size
    x, y, w, h = bbox
    x0, y0, x1, y1 = x * W, y * H, (x + w) * W, (y + h) * H
    pw, ph = max(16.0, (x1 - x0) * PAD_FRAC), max(16.0, (y1 - y0) * PAD_FRAC)
    box = (max(0, int(x0 - pw)), max(0, int(y0 - ph)),
           min(W, int(x1 + pw)), min(H, int(y1 + ph)))
    dest = paths.crops_dir() / "bridge" / f"{det_id}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pil.crop(box).save(dest, "JPEG", quality=92)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md-results", required=True)
    ap.add_argument("--min-conf", type=float, default=0.1)
    ap.add_argument("--country", default="USA")
    ap.add_argument("--admin1", default="CA")
    args = ap.parse_args()

    md_path = Path(args.md_results).expanduser()
    images = json.loads(md_path.read_text())["images"]
    work = []
    for im in images:
        dets = [d for d in im.get("detections") or []
                if d["category"] == "1" and d["conf"] >= args.min_conf]
        if dets:
            work.append((im["file"], dets))
    n_dets = sum(len(d) for _, d in work)
    print(f"{len(work)} images with animal detections, {n_dets} crops to classify")

    from speciesnet import SpeciesNet
    net = SpeciesNet(CLASSIFIER_KAGGLE, components="classifier", multiprocessing=False)
    geofencer = Geofencer(net, args.country, args.admin1)

    out, done, t0 = {}, 0, time.time()
    for rel, dets in work:
        pil = Image.open(ASSETS / rel).convert("RGB")
        name = Path(rel).name
        preds = []
        for i, d in enumerate(dets):
            det_id = f"{Path(name).stem}_d{i}"
            crop = write_crop(pil, d["bbox"], det_id)
            inst = {"filepath": str(crop), "country": args.country,
                    "admin1_region": args.admin1}
            try:
                res = net.classify(instances_dict={"instances": [inst]},
                                   progress_bars=False)
                pred = res["predictions"][0]
            except Exception as e:  # keep going; a failed crop is visible, not fatal
                print(f"  CLASSIFY FAILED {det_id}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                continue
            cls = pred.get("classifications", {}) or {}
            labels = list(cls.get("classes") or [])
            scores = [float(s) for s in (cls.get("scores") or [])]
            if not labels:
                continue
            taxon_raw, score, source = geofencer.apply(labels, scores)
            mapped = taxonomy.map_taxon(taxon_raw, float(score))
            preds.append({
                "det_index": i, "bbox": d["bbox"], "md_conf": d["conf"],
                "taxon_raw": taxon_raw, "taxon": mapped.taxon,
                "score": round(float(score), 4),
                "review_priority": mapped.review_priority,
                "geofence_source": source,
            })
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{n_dets} ({done / (time.time() - t0):.1f}/s)",
                      flush=True)
        if preds:
            out[name] = preds

    sidecar = md_path.parent / "speciesnet_predictions.json"
    sidecar.write_text(json.dumps({
        "classifier": CLASSIFIER_MODEL,
        "geofence": {"country": args.country, "admin1": args.admin1},
        "taxon_map_version": taxonomy.map_version(),
        "md_results": str(md_path),
        "min_conf": args.min_conf,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "predictions": out,
    }, indent=1))
    print(f"\n{done}/{n_dets} classified in {time.time() - t0:.0f}s -> {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
