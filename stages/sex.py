#!/usr/bin/env python
"""Stage 3 -- buck/doe score for blacktail crops, from the monthly model.

Runs in .venv-classifier, after classify.py (it needs `taxon`). Loads the run that
derived/training/sex/current points at (training/sex/, see its README): sex_head.pt (head +
standardization) and backbone.json (which DINOv2 weights the head was trained on). Writes
detections.buck_prob per blacktail crop; a capture's score is the max over its crops, which
fetchers/alerts.py computes when it builds the webhook event.

Best-effort by design. No model (`current` missing), or backbone weights that don't match the
ones the head was trained on, is a logged no-op with exit 0: a rewound head on different
weights would score nonsense, and an unscored deer still alerts, just without a buck/doe guess.

Features are built exactly as embed_dino.py builds them (training.sex.common.dino_features):
448 px bicubic, ImageNet-normalized, CLS + mean patch token, original and mirror; the head's
probability is the mean of the two, as in train_eval.Model.prob.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from hoseid import db, paths  # noqa: E402

BATCH = 16


def model_dir() -> Path:
    return paths.derived_dir() / "training" / "sex"


def resolve_current() -> tuple[Path, dict, dict] | str:
    """(run dir, metrics, backbone) for the current model, or why there is none."""
    current = model_dir() / "current"
    if not current.exists():
        return f"no model: {current} missing"
    run = current.resolve()
    if not (run / "sex_head.pt").exists():
        return f"no model: {run / 'sex_head.pt'} missing"
    try:
        metrics = json.loads((run / "metrics.json").read_text())
        backbone = json.loads((run / "backbone.json").read_text())
    except (OSError, ValueError) as e:
        return f"no model: {run.name} metrics/backbone unreadable ({type(e).__name__}: {e})"
    return run, metrics, backbone


def hub_checkpoint(weights: str) -> Path:
    import torch

    return Path(torch.hub.get_dir()) / "checkpoints" / weights


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_backbone(name: str, size: int):
    """embed(list of PIL images) -> (cls, mean) numpy arrays, on MPS when available.

    Loaded from the cached hub checkout when present (source="local"), so a scoring pass
    never reaches for the network; the weights come from hub/checkpoints, already verified."""
    import torch

    from training.sex.common import dino_features

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    local = Path(torch.hub.get_dir()) / "facebookresearch_dinov2_main"
    if local.exists():
        model = torch.hub.load(str(local), name, source="local")
    else:
        model = torch.hub.load("facebookresearch/dinov2", name)
    model = model.eval().to(dev)
    return lambda imgs: dino_features(model, imgs, dev, size)


def load_head(path: Path):
    """prob(X, Xf) -> per-crop P(buck), mean of original and mirror; plus the feature keys."""
    import numpy as np
    import torch

    from training.sex.common import make_head

    ck = torch.load(path, map_location="cpu", weights_only=False)  # our own file: numpy mu/sd
    mu, sd = np.asarray(ck["mu"], dtype=np.float32), np.asarray(ck["sd"], dtype=np.float32)
    head = make_head(ck["kind"], mu.shape[0])
    head.load_state_dict(ck["state_dict"])
    head.eval()

    def prob(X, Xf):
        with torch.no_grad():
            p = [torch.sigmoid(head(torch.from_numpy(((Z - mu) / sd).astype(np.float32))))
                 .squeeze(1).numpy() for Z in (X, Xf)]
        return (p[0] + p[1]) / 2
    return prob, list(ck["features"]["keys"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 3: buck/doe score of blacktail crops")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--limit", type=int, default=0,
                    help="score at most N crops, newest first (the alert path caps a pass)")
    ap.add_argument("--rescore", action="store_true",
                    help="rescore crops that already have a buck_prob")
    args = ap.parse_args(argv)

    cur = resolve_current()
    if isinstance(cur, str):
        print(json.dumps({"stage": "sex", "run_id": args.run_id, "scored": 0, "skipped": cur}))
        return 0
    run, metrics, backbone = cur

    with db.detections() as conn:
        q = """SELECT detection_id, crop_path FROM detections
               WHERE run_id=? AND taxon='blacktail' AND crop_path IS NOT NULL"""
        if not args.rescore:
            q += " AND buck_prob IS NULL"
        q += " ORDER BY rowid DESC"
        rows = conn.execute(q, (args.run_id,)).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        if not rows:
            print(json.dumps({"stage": "sex", "run_id": args.run_id, "model": run.name,
                              "scored": 0}))
            return 0

        # The head is only valid on the weights it was trained on (embed_dino.py records
        # their sha256 per run); a mismatch means scoring nonsense, so don't.
        ckpt = hub_checkpoint(backbone.get("weights") or f"{backbone['model']}_pretrain.pth")
        sha = sha256_of(ckpt) if ckpt.exists() else None
        if not backbone.get("sha256") or sha != backbone["sha256"]:
            msg = (f"backbone mismatch: {ckpt} sha256 {sha} != {backbone.get('sha256')} "
                   f"recorded by {run.name}; not scoring")
            print(f"  {msg}", file=sys.stderr)
            print(json.dumps({"stage": "sex", "run_id": args.run_id, "scored": 0, "skipped": msg}))
            return 0

        import numpy as np
        from PIL import Image, ImageOps

        embed = load_backbone(backbone["model"], int(backbone["size"]))
        prob, keys = load_head(run / "sex_head.pt")

        n = 0
        t_start = time.time()
        for i in range(0, len(rows), BATCH):
            batch, ids = [], []
            for r in rows[i:i + BATCH]:
                crop = paths.crops_dir() / r["crop_path"]
                try:
                    batch.append(Image.open(crop).convert("RGB"))
                    ids.append(r["detection_id"])
                except OSError as e:
                    print(f"  UNREADABLE CROP {crop}: {type(e).__name__}: {e}", file=sys.stderr)
            if not batch:
                continue
            feats = dict(zip(("cls", "mean"), embed(batch)))
            flipped = dict(zip(("cls", "mean"), embed([ImageOps.mirror(im) for im in batch])))
            X = np.concatenate([feats[k] for k in keys], 1).astype(np.float32)
            Xf = np.concatenate([flipped[k] for k in keys], 1).astype(np.float32)
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "UPDATE detections SET buck_prob=?, sex_model=?, sex_scored_at=? "
                "WHERE detection_id=?",
                [(float(p), run.name, now, d) for p, d in zip(prob(X, Xf), ids)])
            conn.commit()
            n += len(ids)

    elapsed = time.time() - t_start
    print(json.dumps({"stage": "sex", "run_id": args.run_id, "model": run.name, "scored": n,
                      "threshold": metrics.get("threshold"),
                      "img_per_sec": round(n / elapsed, 2) if elapsed > 0 and n else None},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
