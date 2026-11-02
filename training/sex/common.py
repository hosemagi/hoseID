"""Shared paths and label loading for the buck/doe (deer sex) model.

Labels come from the hand-reviewed wildlife log: a deer sighting tagged
M means "at least one male present", F means "no male present". So every
deer crop in an F capture is a clean negative; in an M capture at least
one crop is a buck. Training artifacts live under derived/ (never the
archive, never the pipeline's crops tree or detections.db).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HOME = Path.home()
WILDLIFE_DB = HOME / "trailcam/tags/wildlife.db"
DETECTIONS_DB = HOME / "trailcam/derived/detections.db"
CROPS = HOME / "trailcam/derived/crops"
ASSETS = HOME / "trailcam/landing/assets"
OUT = HOME / "trailcam/derived/training/sex"
LEGACY_CROPS = OUT / "legacy_crops"

DEER_TAXA = {"blacktail"}

# Backbone the heads are trained on (embed_dino.py) and scored with
# (stages/sex.py): the two must build features identically.
DINO_MODEL = "dinov2_vitb14"
DINO_SIZE = 448  # multiple of the 14 px patch


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def deer_labels() -> dict[str, dict]:
    """asset_id -> {sex, date, time, station} for camera-linked deer sightings.
    A capture with any M sighting is M (at least one male present)."""
    out: dict[str, dict] = {}
    for r in ro(WILDLIFE_DB).execute(
            """SELECT capture_asset_id, sex, date, time, station FROM sightings
               WHERE species='deer' AND capture_asset_id IS NOT NULL AND sex IN ('M','F')"""):
        cur = out.get(r["capture_asset_id"])
        if cur is None or r["sex"] == "M":
            out[r["capture_asset_id"]] = dict(sex=r["sex"], date=r["date"], time=r["time"] or "",
                                              station=r["station"])
    return out


def legacy_file(asset_id: str) -> Path | None:
    """legacy:<original camera filename> -> the file in a Reveal export folder."""
    name = asset_id.split(":", 1)[1]
    hits = sorted(ASSETS.glob(f"*/reveal-images*/{name}"))
    return hits[0] if hits else None


def dino_features(model, imgs, dev: str, size: int = DINO_SIZE):
    """(CLS token, mean patch token) as numpy arrays for a list of RGB PIL
    images: resized to size x size bicubic, ImageNet-normalized. torch, numpy
    and PIL are imported here so label-only callers don't need them."""
    import numpy as np
    import torch
    from PIL import Image

    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
    x = torch.stack([torch.from_numpy(np.asarray(im.resize((size, size), Image.BICUBIC),
                                                 dtype=np.float32) / 255).permute(2, 0, 1)
                     for im in imgs])
    x = ((x - mean) / std).to(dev)
    with torch.no_grad():
        out = model.forward_features(x)
    return (out["x_norm_clstoken"].cpu().numpy(),
            out["x_norm_patchtokens"].mean(1).cpu().numpy())


def make_head(kind: str, d: int):
    """The buck/doe head on standardized features (trained in train_eval.py,
    rebuilt from its state_dict by stages/sex.py)."""
    import torch

    if kind == "logistic":
        return torch.nn.Linear(d, 1)
    return torch.nn.Sequential(torch.nn.Linear(d, 256), torch.nn.ReLU(),
                               torch.nn.Dropout(0.3), torch.nn.Linear(256, 1))
