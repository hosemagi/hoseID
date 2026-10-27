#!/usr/bin/env python
"""Cut person crops from the originals and record what each one is.

Run with .venv-classifier. stages/detect.py keeps bboxes for person
detections but only writes crops for animals, so the crops are cut here
from the landing-zone originals: images directly, videos at the detector's
selected frame (frame_offset_s). One row per crop in rows.jsonl with
station, time, crop size, IR flag and visit (training/sex visit rule:
captures at one station within 30 min chain into one visit).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.person.common import (CROPS, DETECTIONS_DB, MIN_CONF, OUT, PAD_FRAC,  # noqa: E402
                                     asset_file, ro)
from training.sex.embed import assign_visits  # noqa: E402


def _video_frame(path: Path, offset_s: float, Image):
    with tempfile.TemporaryDirectory(prefix="person-frame-") as tmp:
        out = Path(tmp) / "f.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{offset_s or 0:.3f}",
                        "-i", str(path), "-frames:v", "1", "-q:v", "2", str(out)],
                       check=False, capture_output=True, timeout=120)
        return Image.open(out).convert("RGB") if out.exists() else None


def _is_ir(im) -> bool:
    import numpy as np  # same test as training/sex/embed.py: IR frames are (near) grayscale
    a = np.asarray(im.resize((64, 64)), dtype=np.float32)
    return float(np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 1] - a[..., 2]).mean()) < 6.0


def main() -> int:
    from PIL import Image
    det = ro(DETECTIONS_DB)
    q = det.execute(
        """SELECT d.detection_id, d.asset_id, d.bbox_x, d.bbox_y, d.bbox_w, d.bbox_h,
                  d.detector_confidence, d.frame_offset_s, c.station, c.capture_time, c.media_type
           FROM detections d JOIN captures c USING (asset_id, run_id)
           WHERE d.detector_class='person' AND d.detector_confidence >= ?
           ORDER BY d.asset_id""", (MIN_CONF,)).fetchall()
    CROPS.mkdir(parents=True, exist_ok=True)
    rows, skipped = [], {"no_file": 0, "no_frame": 0}
    frame_cache: dict[str, object] = {}
    for i, d in enumerate(q):
        aid = d["asset_id"]
        if aid not in frame_cache:
            frame_cache.clear()  # rows are ordered by asset; keep only the current frame
            src = asset_file(aid)
            if src is None:
                frame_cache[aid] = None
                skipped["no_file"] += 1
            elif d["media_type"] == "video":
                frame_cache[aid] = _video_frame(src, d["frame_offset_s"], Image)
                if frame_cache[aid] is None:
                    skipped["no_frame"] += 1
            else:
                frame_cache[aid] = Image.open(src).convert("RGB")
        pil = frame_cache[aid]
        if pil is None:
            continue
        W, H = pil.size
        x0, y0 = d["bbox_x"] * W, d["bbox_y"] * H
        x1, y1 = x0 + d["bbox_w"] * W, y0 + d["bbox_h"] * H
        pw, ph = max(16.0, (x1 - x0) * PAD_FRAC), max(16.0, (y1 - y0) * PAD_FRAC)
        crop = pil.crop((max(0, int(x0 - pw)), max(0, int(y0 - ph)),
                         min(W, int(x1 + pw)), min(H, int(y1 + ph))))
        dest = CROPS / f"{d['detection_id']}.jpg"
        crop.save(dest, "JPEG", quality=92)
        rows.append({"crop": str(dest), "detection_id": d["detection_id"], "asset_id": aid,
                     "station": d["station"], "when": d["capture_time"][:19],
                     "date": d["capture_time"][:10], "media": d["media_type"],
                     "det_conf": d["detector_confidence"],
                     "box_w": int(x1 - x0), "box_h": int(y1 - y0), "frame_w": W, "frame_h": H,
                     "ir": _is_ir(crop)})
        if i % 100 == 0:
            print(f"  {i}/{len(q)}", file=sys.stderr, flush=True)
    assign_visits(rows)
    (OUT / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(json.dumps({"detections": len(q), "crops": len(rows), "skipped_detections_by_asset": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
