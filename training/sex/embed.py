#!/usr/bin/env python
"""Build the labeled deer-crop set and extract SpeciesNet features.

Run with .venv-classifier. One row per deer crop: the capture's label
(M = at least one buck present, F = none), its visit, its split, and an IR
(night) flag. With --speciesnet it also saves SpeciesNet embeddings
(features.npz); the model now uses DINOv2 (embed_dino.py), which beat them,
so the monthly retrain skips that slow pass.

Visits: captures at one station within VISIT_GAP_MIN of each other are one
visit. The train/eval split (split.py) is drawn by whole visit, stratified
by station x label x day/night — frames of one visit are near-duplicates, so
a random crop split would score the model on frames it trained on.
"""
from __future__ import annotations

import hashlib
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import CROPS, DEER_TAXA, DETECTIONS_DB, OUT, deer_labels, ro  # noqa: E402
from training.sex.split import assign_split, coverage  # noqa: E402

VISIT_GAP_MIN = 30
# The last 15x15x1280 map, before SpeciesNet's own global average pool. Pooled
# three ways: avg (what SpeciesNet uses), max and GeM (p=3) keep local detail
# — antlers are a small region that a plain average dilutes.
FEATURE_NODE = "SpeciesNet/efficientnetv2-m/top_activation/mul_1"
POOLS = ("avg", "max", "gem")


def _when(lab: dict) -> datetime:
    t = (lab["time"] or "12:00")[:8]
    fmt = "%Y-%m-%d %H:%M:%S" if t.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(f"{lab['date']} {t}", fmt)


def collect() -> list[dict]:
    labels = deer_labels()
    legacy = {m["asset_id"]: m for m in json.loads((OUT / "legacy_detections.json").read_text())}
    det = ro(DETECTIONS_DB)
    rows, skipped = [], {"no_capture": 0, "no_deer_crop": 0}
    for aid, lab in labels.items():
        if aid.startswith("legacy:"):
            crops = [(c["path"], c["conf"]) for c in legacy.get(aid, {}).get("crops", [])]
            media = "image"
        else:
            cap = det.execute("SELECT * FROM captures WHERE asset_id=? ORDER BY rowid DESC LIMIT 1",
                              (aid,)).fetchone()
            if not cap:
                skipped["no_capture"] += 1
                continue
            media = cap["media_type"]
            crops = [(str(CROPS / d["crop_path"]), d["detector_confidence"]) for d in det.execute(
                """SELECT crop_path, detector_confidence, taxon FROM detections
                   WHERE asset_id=? AND run_id=? AND detector_class='animal' AND crop_path IS NOT NULL""",
                (aid, cap["run_id"])) if (d["taxon"] or "") in DEER_TAXA]
        crops = [c for c in crops if Path(c[0]).exists()]
        if not crops:
            skipped["no_deer_crop"] += 1
            continue
        for path, conf in crops:
            rows.append({"crop": path, "asset_id": aid, "label": 1 if lab["sex"] == "M" else 0,
                         "n_deer": len(crops), "station": lab["station"], "media": media,
                         "date": lab["date"], "when": _when(lab).isoformat(), "det_conf": conf,
                         "legacy": aid.startswith("legacy:")})
    print("skipped captures:", skipped, file=sys.stderr)
    return rows


def assign_visits(rows: list[dict]) -> None:
    caps = {}
    for r in rows:
        caps.setdefault(r["asset_id"], r)
    by_station: dict[str, list] = {}
    for c in caps.values():
        by_station.setdefault(c["station"], []).append(c)
    visit_of = {}
    for station, cs in by_station.items():
        cs.sort(key=lambda c: c["when"])
        vid, last = None, None
        for c in cs:
            t = datetime.fromisoformat(c["when"])
            if last is None or (t - last).total_seconds() > VISIT_GAP_MIN * 60:
                vid = f"{station}|{c['when']}"
            last = t
            visit_of[c["asset_id"]] = vid
    for r in rows:
        r["visit"] = visit_of[r["asset_id"]]


def ir_flags(rows: list[dict]) -> None:
    """IR night frames are (near) grayscale."""
    import numpy as np
    from PIL import Image
    for r in rows:
        a = np.asarray(Image.open(r["crop"]).convert("RGB").resize((64, 64)), dtype=np.float32)
        r["ir"] = float(np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 1] - a[..., 2]).mean()) < 6.0


def embed(rows: list[dict]):
    import numpy as np
    import torch
    from PIL import Image, ImageOps
    from speciesnet import SpeciesNet

    net = SpeciesNet("kaggle:google/speciesnet/pyTorch/v4.0.1a", components="classifier",
                     multiprocessing=False)
    clf = net.classifier
    grab = {}
    dict(clf.model.named_modules())[FEATURE_NODE].register_forward_hook(
        lambda m, i, o: grab.__setitem__("f", o.detach().cpu()))

    def feats(imgs):
        arr = np.stack([clf.preprocess(im).arr / 255 for im in imgs]).astype(np.float32)
        with torch.no_grad():
            clf.model(torch.from_numpy(arr).to(clf.device))
        fmap = grab["f"]  # (B, 1280, 15, 15)
        return {"avg": fmap.mean((2, 3)).numpy(), "max": fmap.amax((2, 3)).numpy(),
                "gem": fmap.clamp(min=1e-6).pow(3).mean((2, 3)).pow(1 / 3).numpy()}

    F = {k: [] for k in POOLS}
    Ff = {k: [] for k in POOLS}
    ir = []
    B = 16
    for i in range(0, len(rows), B):
        batch = [Image.open(r["crop"]).convert("RGB") for r in rows[i:i + B]]
        for k, v in feats(batch).items():
            F[k].append(v)
        for k, v in feats([ImageOps.mirror(im) for im in batch]).items():
            Ff[k].append(v)
        for im in batch:  # IR night frames are (near) grayscale
            a = np.asarray(im.resize((64, 64)), dtype=np.float32)
            ir.append(float(np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 1] - a[..., 2]).mean()) < 6.0)
        if (i // B) % 10 == 0:
            print(f"  {i + len(batch)}/{len(rows)}", file=sys.stderr, flush=True)
    for r, flag in zip(rows, ir):
        r["ir"] = flag
    return ({k: np.concatenate(v) for k, v in F.items()},
            {k: np.concatenate(v) for k, v in Ff.items()})


def main() -> int:
    import numpy as np
    rows = collect()
    assign_visits(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    if "--speciesnet" in sys.argv:
        X, Xf = embed(rows)  # also sets r["ir"]
        np.savez_compressed(OUT / "features.npz", **{f"X_{k}": X[k] for k in POOLS},
                            **{f"Xf_{k}": Xf[k] for k in POOLS})
    else:
        ir_flags(rows)
    assign_split(rows)  # stratifies on station x label x day/night
    (OUT / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(coverage(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
