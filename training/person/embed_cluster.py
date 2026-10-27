#!/usr/bin/env python
"""DINOv2 features for the person crops, then a rough grouping into people.

Run with .venv-classifier after extract.py. Same model and preprocessing as
training/sex/embed_dino.py (ViT-B/14, CLS + mean patch token, original +
mirror averaged) but at 224 px: people are large in frame and detail is not
the bottleneck. Persons are taller than wide, so crops are letterboxed to
square rather than stretched.

Grouping is plain agglomerative clustering on cosine distance (average
linkage, cut at CUT). It is a first look, not identities: DINOv2 has no
notion of "same person", so groups are expected to follow clothing, pose
and station. What matters is whether one person's groups span days.
"""
from __future__ import annotations

import collections
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.person.common import OUT, load_rows  # noqa: E402

MODEL = "dinov2_vitb14"
SIZE = 224
CUT = 0.35  # cosine distance


def _square(im, Image):
    s = max(im.size)
    canvas = Image.new("RGB", (s, s), (124, 116, 104))  # ~ImageNet mean
    canvas.paste(im, ((s - im.width) // 2, (s - im.height) // 2))
    return canvas


def embed(rows):
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    model = torch.hub.load("facebookresearch/dinov2", MODEL).eval().to(dev)
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)

    def feats(imgs):
        x = torch.stack([torch.from_numpy(np.asarray(im.resize((SIZE, SIZE), Image.BICUBIC),
                                                     dtype=np.float32) / 255).permute(2, 0, 1)
                         for im in imgs])
        with torch.no_grad():
            out = model.forward_features(((x - mean) / std).to(dev))
        return np.concatenate([out["x_norm_clstoken"].cpu().numpy(),
                               out["x_norm_patchtokens"].mean(1).cpu().numpy()], 1)

    acc, B = [], 32
    for i in range(0, len(rows), B):
        batch = [_square(Image.open(r["crop"]).convert("RGB"), Image) for r in rows[i:i + B]]
        acc.append((feats(batch) + feats([ImageOps.mirror(im) for im in batch])) / 2)
        if (i // B) % 5 == 0:
            print(f"  {i + len(batch)}/{len(rows)}", file=sys.stderr, flush=True)
    X = np.concatenate(acc)
    return X / np.linalg.norm(X, axis=1, keepdims=True)


def cluster(X):
    from scipy.cluster.hierarchy import fcluster, linkage
    return fcluster(linkage(X, method="average", metric="cosine"), t=CUT, criterion="distance")


def main() -> int:
    import numpy as np
    rows = load_rows()
    X = embed(rows)
    np.save(OUT / "features_dino.npy", X)
    labels = cluster(X)
    for r, c in zip(rows, labels):
        r["cluster"] = int(c)
    (OUT / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["cluster"]].append(r)
    sizes = sorted((len(g) for g in groups.values()), reverse=True)
    multi_day = [g for g in groups.values() if len({r["date"] for r in g}) > 1]
    multi_visit = [g for g in groups.values() if len({r["visit"] for r in g}) > 1]
    print(json.dumps({"crops": len(rows), "clusters": len(groups), "singletons": sizes.count(1),
                      "largest": sizes[:10], "clusters_spanning_days": len(multi_day),
                      "clusters_spanning_visits": len(multi_visit), "cut": CUT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
