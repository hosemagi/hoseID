#!/usr/bin/env python
"""Run MegaDetector on the legacy (pre-pipeline) labeled deer captures.

Run with .venv-detector. The 103 legacy captures (82 of them July bucks)
were tagged in the wildlife log before the pipeline existed, so they have no
detections. Reads the Reveal export images (read-only), writes crops and a
manifest under derived/training/sex/ — not the pipeline's crops/ tree or
detections.db. Same detector, threshold and crop padding as stage 1.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stages"))

from training.sex.common import LEGACY_CROPS, OUT, deer_labels, legacy_file  # noqa: E402

THRESHOLD = 0.2
PAD = 0.15


def main() -> int:
    from megadetector.detection.run_detector import load_detector
    from megadetector.visualization.visualization_utils import load_image
    from PIL import Image
    from detect import _staged_weights

    labels = {a: v for a, v in deer_labels().items() if a.startswith("legacy:")}
    manifest_path = OUT / "legacy_detections.json"
    if "--if-changed" in sys.argv and manifest_path.exists():
        done = {m["asset_id"]: m for m in json.loads(manifest_path.read_text())}
        if set(done) == set(labels) and all(done[a]["sex"] == labels[a]["sex"] for a in labels):
            print(json.dumps({"legacy": "unchanged", "captures": len(done)}))
            return 0
    det = load_detector(_staged_weights() or "redwood", detector_options={"device": "mps"})
    LEGACY_CROPS.mkdir(parents=True, exist_ok=True)
    manifest = []
    for aid, lab in sorted(labels.items()):
        f = legacy_file(aid)
        if f is None:
            print(f"  missing file for {aid}", file=sys.stderr)
            continue
        res = det.generate_detections_one_image(load_image(str(f)), image_id=aid)
        pil = Image.open(f).convert("RGB")
        W, H = pil.size
        crops = []
        for d in res.get("detections") or []:
            if str(d["category"]) != "1" or d["conf"] < THRESHOLD:
                continue
            x, y, w, h = d["bbox"]
            x0, y0, x1, y1 = x * W, y * H, (x + w) * W, (y + h) * H
            pw, ph = max(16.0, (x1 - x0) * PAD), max(16.0, (y1 - y0) * PAD)
            box = (max(0, int(x0 - pw)), max(0, int(y0 - ph)), min(W, int(x1 + pw)), min(H, int(y1 + ph)))
            dest = LEGACY_CROPS / f"{uuid.uuid4().hex}.jpg"
            pil.crop(box).save(dest, "JPEG", quality=92)
            crops.append({"path": str(dest), "conf": float(d["conf"]), "bbox": [x, y, w, h]})
        manifest.append({"asset_id": aid, **lab, "crops": crops})
    (OUT / "legacy_detections.json").write_text(json.dumps(manifest, indent=1))
    n = sum(len(m["crops"]) for m in manifest)
    print(json.dumps({"captures": len(manifest), "crops": n,
                      "no_animal": sum(1 for m in manifest if not m["crops"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
