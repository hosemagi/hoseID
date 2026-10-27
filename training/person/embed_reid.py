#!/usr/bin/env python
"""Person re-identification features for every crop in rows.jsonl.

Run with .venv-person (boxmot; kept out of the pipeline venvs). DINOv2 is a
general image model and mostly encodes scene and pose (eval_id.py). Re-ID
models are trained for exactly "same person, different photo" on large
pedestrian datasets, so they are the next thing to try. Each model's
features go to features_reid_<model>.npy, row-aligned with rows.jsonl, for
eval_id.py. Tight boxes (common.tight_crop), original + mirror averaged.

Weights download once into boxmot's weights dir. They are pickled .pt files
(osnet_* from Google Drive, lmbn_* from boxmot's GitHub releases) and
loading a pickle can run code -- fetch them deliberately, not in a pipeline.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.person.common import OUT, load_rows, tight_crop  # noqa: E402

MODELS = ("osnet_ain_x1_0_msmt17", "osnet_x1_0_msmt17", "lmbn_n_market")


def main() -> int:
    import numpy as np
    import torch
    from boxmot.reid.core.runtime import ReID
    from boxmot.utils import WEIGHTS

    models = sys.argv[1:] or list(MODELS)
    rows = load_rows()
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    crops = [np.ascontiguousarray(np.asarray(tight_crop(r))[:, :, ::-1]) for r in rows]  # RGB -> BGR
    for name in models:
        reid = ReID(weights=WEIGHTS / f"{name}.pt", device=dev)
        acc = []
        for i in range(0, len(crops), 64):
            batch = crops[i:i + 64]
            f = reid(batch) + reid([np.ascontiguousarray(c[:, ::-1]) for c in batch])
            acc.append(f / np.linalg.norm(f, axis=1, keepdims=True))
        X = np.concatenate(acc)
        np.save(OUT / f"features_reid_{name}.npy", X)
        print(f"{name}: {X.shape}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
