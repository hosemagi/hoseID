#!/usr/bin/env python
"""DINOv2 features for the same labeled crops (rows.jsonl from embed.py).

Run with .venv-classifier. SpeciesNet's features are trained to put bucks
and does in one class, so they barely see antlers; DINOv2 is self-supervised
and keeps fine-grained detail. ViT-B/14 at 448 px (antlers are small),
CLS token + mean of patch tokens, original and mirror. Weights come via
torch.hub (facebookresearch/dinov2) into ~/.cache/torch/hub.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import OUT  # noqa: E402

MODEL = "dinov2_vitb14"
SIZE = 448  # multiple of the 14 px patch


def main() -> int:
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    rows = [json.loads(line) for line in (OUT / "rows.jsonl").read_text().splitlines() if line]
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    model = torch.hub.load("facebookresearch/dinov2", MODEL).eval().to(dev)
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)

    def feats(imgs):
        x = torch.stack([torch.from_numpy(np.asarray(im.resize((SIZE, SIZE), Image.BICUBIC),
                                                     dtype=np.float32) / 255).permute(2, 0, 1)
                         for im in imgs])
        x = ((x - mean) / std).to(dev)
        with torch.no_grad():
            out = model.forward_features(x)
        return (out["x_norm_clstoken"].cpu().numpy(),
                out["x_norm_patchtokens"].mean(1).cpu().numpy())

    acc = {k: [] for k in ("X_cls", "X_mean", "Xf_cls", "Xf_mean")}
    B = 16
    for i in range(0, len(rows), B):
        batch = [Image.open(r["crop"]).convert("RGB") for r in rows[i:i + B]]
        c, m = feats(batch)
        acc["X_cls"].append(c); acc["X_mean"].append(m)
        c, m = feats([ImageOps.mirror(im) for im in batch])
        acc["Xf_cls"].append(c); acc["Xf_mean"].append(m)
        if (i // B) % 20 == 0:
            print(f"  {i + len(batch)}/{len(rows)}", file=sys.stderr, flush=True)
    np.savez_compressed(OUT / "features_dino.npz", **{k: np.concatenate(v) for k, v in acc.items()})
    # Pin what the head is trained on: a rewound head is only valid on the
    # same backbone weights, so record their checksum with every run.
    import hashlib
    ckpt = Path(torch.hub.get_dir()) / "checkpoints" / f"{MODEL}_pretrain.pth"
    sha = hashlib.sha256(ckpt.read_bytes()).hexdigest() if ckpt.exists() else None
    (OUT / "backbone.json").write_text(json.dumps({"model": MODEL, "size": SIZE,
                                                  "weights": ckpt.name, "sha256": sha}, indent=1))
    print(json.dumps({"crops": len(rows), "model": MODEL, "size": SIZE}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
