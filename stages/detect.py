#!/usr/bin/env python
"""Stage 1 -- MegaDetector v1000 `redwood`, CPU.

Runs in .venv-detector (protobuf 3.20.1, pinned by ultralytics-yolov5). Must not import anything
from the classifier environment.

CPU rather than MPS deliberately: findings §7 measured only 1.22x from MPS (4.16 vs 3.40 img/s),
and running on CPU leaves the GPU entirely free -- both for the classifier stage and for Sage,
which §8 showed degrades 1.8x under GPU contention.

Emits one detection per box plus a capture-level rollup, INCLUDING for empty captures: the
false-trigger rate per station is real signal about wind, vegetation and camera health.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoseid import db, landing, paths, stations, video  # noqa: E402
from hoseid.sidecar import validate_sidecar  # noqa: E402

DETECTOR_MODEL = "megadetector-v1000-redwood"
DETECTOR_VERSION = "10.0.24"
CATEGORY = {"1": "animal", "2": "person", "3": "vehicle"}


def _staged_weights() -> str | None:
    """Prefer weights under the project models dir over MegaDetector's temp download.

    MegaDetector hardcodes `tempfile.gettempdir()` with no override; on macOS that is a
    periodically-purged /var/folders path, so a temp purge would silently trigger a 281 MB
    re-download in the middle of a batch.
    """
    p = paths.models_dir() / "md_v1000.0.0-redwood.pt"
    return str(p) if p.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 1: detection over the landing zone")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--threshold", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--device", default="cpu", choices=["cpu", "mps"])
    ap.add_argument("--crop-padding", type=float, default=0.15)
    ap.add_argument("--reprocess", action="store_true",
                    help="redo assets already recorded for this run_id")
    ap.add_argument("--sample-fps", type=float, default=video.DEFAULT_POLICY.nominal_fps)
    ap.add_argument("--max-sampled-frames", type=int, default=video.DEFAULT_POLICY.max_frames)
    args = ap.parse_args()

    policy = video.SamplingPolicy(nominal_fps=args.sample_fps, max_frames=args.max_sampled_frames)

    from megadetector.detection.run_detector import load_detector
    from megadetector.visualization.visualization_utils import load_image
    from PIL import Image

    paths.ensure_layout()
    model_ref = _staged_weights() or "redwood"
    det = load_detector(model_ref, detector_options={"device": args.device})

    overrides = stations.load_overrides()
    started = datetime.now(timezone.utc).isoformat()

    with db.detections() as conn:
        db.start_run(conn, run_id=args.run_id, started_at=started,
                     detector_model=DETECTOR_MODEL, detector_version=DETECTOR_VERSION,
                     detector_threshold=args.threshold,
                     sampling_policy=json.dumps(policy.as_provenance()),
                     notes=f"device={args.device}")

        sidecar_files = landing.iter_sidecars()
        if args.limit:
            sidecar_files = sidecar_files[: args.limit]

        n_cap = n_det = n_empty = n_failed = 0
        for sp in sidecar_files:
            sc = validate_sidecar(sp)
            if not args.reprocess and db.already_processed(conn, sc.asset_id, args.run_id):
                continue
            asset = landing.find_asset(sc.asset_id)
            if asset is None:
                print(f"  MISSING ASSET {sc.asset_id}", file=sys.stderr)
                continue

            station, corrected = stations.resolve_station(
                sc.station, sc.device_id, sc.capture_time, overrides)

            t0 = time.time()
            frame_offset_s = frame_index = None
            n_sampled = None
            decode_error = None
            try:
                if sc.is_video and not sc.probe_ok:
                    # ffprobe could not read this clip at ingest. Do not attempt to sample it --
                    # record the decode failure directly.
                    boxes, pil, n_sampled = [], Image.new("RGB", (1, 1)), 0
                    decode_error = sc.probe_error or "probe failed at ingest"
                elif sc.is_video:
                    boxes, pil, frame_offset_s, frame_index, n_sampled = _detect_video(
                        det, load_image, Image, asset, sc, policy, args.threshold)
                    if n_sampled == 0:
                        decode_error = "no frames decoded"
                else:
                    img = load_image(str(asset))
                    res = det.generate_detections_one_image(img, image_id=sc.asset_id)
                    boxes = [d for d in (res.get("detections") or [])
                             if d.get("conf", 0) >= args.threshold]
                    pil = Image.open(asset).convert("RGB")
            except Exception as e:
                print(f"  DETECT FAILED {sc.asset_id}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            elapsed_ms = (time.time() - t0) * 1000.0

            W, H = pil.size
            rows = []
            for d in boxes:
                cls = CATEGORY.get(str(d["category"]), "unknown")
                x, y, w, h = d["bbox"]                      # normalised xywh
                detection_id = uuid.uuid4().hex
                crop_rel = None
                if cls == "animal":
                    crop_rel = _write_crop(pil, (x, y, w, h), W, H, detection_id,
                                           sc.capture_time, args.crop_padding)
                rows.append((detection_id, sc.asset_id, args.run_id, x, y, w, h,
                             cls, float(d["conf"]), crop_rel, frame_offset_s, frame_index))

            conn.executemany(
                """INSERT OR REPLACE INTO detections
                   (detection_id, asset_id, run_id, bbox_x, bbox_y, bbox_w, bbox_h,
                    detector_class, detector_confidence, crop_path, frame_offset_s, frame_index)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

            classes = {r[7] for r in rows}
            state = video.decode_state(sc.is_video, sc.duration_s, sc.fps, n_sampled,
                                       probe_ok=sc.probe_ok)
            failed = state == video.STATE_DECODE_FAILED
            # A decode failure is NOT an empty capture. "We could not look" must never be
            # counted as "we looked and saw nothing", or an unreadable codec presents as a
            # quiet station and nobody investigates.
            is_empty = 0 if failed else (1 if not rows else 0)
            n_empty += is_empty
            n_failed += int(failed)
            conn.execute(
                """INSERT OR REPLACE INTO captures
                   (asset_id, run_id, station, station_corrected, capture_time, time_trusted,
                    n_detections, has_animal, has_human, has_vehicle, is_empty, detector_ms,
                    media_type, count_is_lower_bound, sampled_frames, decode_status, decode_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sc.asset_id, args.run_id, station, int(corrected),
                 sc.capture_time.isoformat(), int(sc.time_is_trustworthy),
                 len(rows), int("animal" in classes), int("person" in classes),
                 int("vehicle" in classes), is_empty, elapsed_ms,
                 sc.media_type,
                 # Invariant 7: only one frame per clip is kept, so a video count can only ever
                 # be a lower bound on the animals actually present.
                 int(sc.is_video), n_sampled, state, decode_error))
            conn.commit()
            if failed:
                print(f"  DECODE FAILED {sc.asset_id[:20]} ({decode_error})", file=sys.stderr)

            n_cap += 1
            n_det += len(rows)
            if n_cap % 50 == 0:
                print(f"  {n_cap} captures, {n_det} detections", flush=True)

        db.finish_run(conn, args.run_id, datetime.now(timezone.utc).isoformat())

    print(json.dumps({"stage": "detect", "run_id": args.run_id, "captures": n_cap,
                      "detections": n_det, "empty_captures": n_empty,
                      # Surfaced separately and always, so an unreadable codec is countable
                      # rather than hiding inside the empty count.
                      "decode_failures": n_failed,
                      "device": args.device, "threshold": args.threshold}, indent=1))
    return 0


def _detect_video(det, load_image, Image, asset, sc, policy, threshold):
    """Detect across sampled frames, keep the single best frame, emit detections from it only.

    Deliberately no tracking. An earlier design associated boxes into tracks across frames to get
    one detection per animal per clip plus heading from centroid drift; it was rejected as
    overengineered. P reviews clips by hand and the clip itself is in the landing zone, so the
    selected frame is not a summary anyone is stuck with -- it is a thumbnail with a timestamp
    pointing into the real artifact. Do not reintroduce tracking without a new decision.

    Returns (boxes, pil_of_selected_frame, offset_s, frame_index, n_sampled).
    """
    meta = video.VideoMeta(duration_s=sc.duration_s, fps=sc.fps,
                           frame_count=sc.frame_count, width=sc.width, height=sc.height)
    best = None          # (score, boxes, frame)
    n_sampled = 0
    with video.FrameSampler(asset, policy, meta=meta) as frames:
        n_sampled = len(frames)
        for f in frames:
            res = det.generate_detections_one_image(
                load_image(str(f.path)), image_id=f"{sc.asset_id}#{f.frame_index}")
            dets = [d for d in (res.get("detections") or []) if d.get("conf", 0) >= threshold]
            score = video.score_frame(dets)
            if best is None or score > best[0]:
                # Hold the decoded pixels now: the temp dir is cleaned up on context exit.
                best = (score, dets, Image.open(f.path).convert("RGB"), f)
        if best is None:
            # No frames decoded at all -- treat as an empty capture rather than an error, so a
            # zero-length or unreadable clip is still recorded and visible in station stats.
            return [], Image.new("RGB", (1, 1)), None, None, n_sampled
        _, boxes, pil, frame = best
        return boxes, pil, frame.offset_s, frame.frame_index, n_sampled


def _write_crop(pil, bbox, W, H, detection_id, capture_time, pad_frac) -> str:
    """Crops are a deliverable, not an intermediate.

    They are both the review UX (a 300px animal inside a 4000px frame is useless on a phone) and
    a hard requirement of stage 2: findings §A2 measured SpeciesNet at 92.7% on crops versus
    29.2% on full frames.
    """
    x, y, w, h = bbox
    x0, y0, x1, y1 = x * W, y * H, (x + w) * W, (y + h) * H
    pw, ph = max(16.0, (x1 - x0) * pad_frac), max(16.0, (y1 - y0) * pad_frac)
    box = (max(0, int(x0 - pw)), max(0, int(y0 - ph)),
           min(W, int(x1 + pw)), min(H, int(y1 + ph)))
    crop = pil.crop(box)
    rel = Path(f"{capture_time:%Y/%m}") / f"{detection_id}.jpg"
    dest = paths.crops_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest, "JPEG", quality=92)
    return str(rel)


if __name__ == "__main__":
    raise SystemExit(main())
