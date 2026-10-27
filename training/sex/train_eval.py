#!/usr/bin/env python
"""Train the buck/doe head on SpeciesNet features; evaluate once on held-out visits.

Run with .venv-classifier (torch only; no sklearn there by design).
- Train crops: every crop of an F capture (all does/fawns) + crops of
  single-deer M captures (that crop is the buck). Multi-deer M captures are
  ambiguous per crop and are used only for capture-level evaluation.
- Model selection (features, head, weight decay) and the threshold come
  from 5-fold cross-validation grouped by visit on the TRAIN split. The
  selection criterion is precision at TARGET_RECALL (recall-first, the
  operator's choice), not AP: AP picked a model that ranked well overall
  but was worse exactly at the operating point. The eval
  split is scored once, at the end.
- Capture score = max over its deer crops of the mean(orig, mirror) prob —
  "at least one buck present", matching how M is tagged.
- Threshold: recall-first, the highest one reaching TARGET_RECALL of buck
  captures in cross-validation.
"""
from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import OUT  # noqa: E402

TARGET_RECALL = 0.90
FOLDS = 5
SEED = 7
HEADS = [("logistic", 1e-2), ("mlp", 1e-2), ("mlp", 1e-1)]


# (features file, keys). SpeciesNet avg is the baseline; max/GeM pooling of
# its map tested worse (its features are trained to merge bucks and does).
# SpeciesNet ("features.npz", ("avg",)) is no longer extracted by default:
# DINOv2 beat it on every head (embed.py --speciesnet to compare again).
FEATURE_SETS = [("features_dino.npz", ("cls",)),
                ("features_dino.npz", ("mean",)),
                ("features_dino.npz", ("cls", "mean"))]


def load(fset: tuple):
    fname, keys = fset
    rows = [json.loads(line) for line in (OUT / "rows.jsonl").read_text().splitlines() if line]
    z = np.load(OUT / fname)
    X = np.concatenate([z[f"X_{k}"] for k in keys], 1).astype(np.float32)
    Xf = np.concatenate([z[f"Xf_{k}"] for k in keys], 1).astype(np.float32)
    return rows, X, Xf


def fset_name(fset: tuple) -> str:
    return ("dino:" if "dino" in fset[0] else "speciesnet:") + "+".join(fset[1])


def make_head(kind: str, d: int) -> torch.nn.Module:
    if kind == "logistic":
        return torch.nn.Linear(d, 1)
    return torch.nn.Sequential(torch.nn.Linear(d, 256), torch.nn.ReLU(),
                               torch.nn.Dropout(0.3), torch.nn.Linear(256, 1))


class Model:
    """Standardize -> head. Trained on originals + mirrors, class-weighted."""

    def __init__(self, kind: str, wd: float):
        self.kind, self.wd = kind, wd

    def fit(self, X, Xf, y, epochs=300):
        torch.manual_seed(SEED)
        A = np.concatenate([X, Xf]); t = np.concatenate([y, y]).astype(np.float32)
        self.mu, self.sd = A.mean(0), A.std(0) + 1e-6
        A = torch.from_numpy((A - self.mu) / self.sd); t = torch.from_numpy(t)
        self.head = make_head(self.kind, A.shape[1])
        pos = max(1.0, float((t == 0).sum() / max(1, (t == 1).sum())))
        loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos))
        opt = torch.optim.AdamW(self.head.parameters(), lr=1e-3, weight_decay=self.wd)
        self.head.train()
        for _ in range(epochs):
            opt.zero_grad()
            loss = loss_fn(self.head(A).squeeze(1), t)
            loss.backward()
            opt.step()
        self.head.eval()
        return self

    def prob(self, X, Xf):
        with torch.no_grad():
            p = [torch.sigmoid(self.head(torch.from_numpy((Z - self.mu) / self.sd))).squeeze(1).numpy()
                 for Z in (X, Xf)]
        return (p[0] + p[1]) / 2


def trainable(r) -> bool:
    return r["label"] == 0 or r["n_deer"] == 1


def capture_scores(rows, idx, probs):
    """asset_id -> (max crop prob, label, a representative row)."""
    out = {}
    for i, p in zip(idx, probs):
        r = rows[i]
        cur = out.get(r["asset_id"])
        out[r["asset_id"]] = (max(p, cur[0]) if cur else p, r["label"], r)
    return out


def pr_at(caps, thr):
    tp = sum(1 for s, y, _ in caps.values() if y and s >= thr)
    fp = sum(1 for s, y, _ in caps.values() if not y and s >= thr)
    fn = sum(1 for s, y, _ in caps.values() if y and s < thr)
    return (tp / (tp + fp) if tp + fp else float("nan"), tp / (tp + fn) if tp + fn else float("nan"),
            tp, fp, fn)


def ap(caps):
    """Average precision (area under PR) for buck captures."""
    ranked = sorted(caps.values(), key=lambda c: -c[0])
    npos = sum(y for _, y, _ in ranked)
    tp, total = 0, 0.0
    for k, (_, y, _) in enumerate(ranked, 1):
        if y:
            tp += 1
            total += tp / k
    return total / npos if npos else float("nan")


def threshold_for_recall(caps, target):
    pos = sorted((s for s, y, _ in caps.values() if y), reverse=True)
    if not pos:
        return 0.5
    k = int(np.ceil(target * len(pos))) - 1
    return float(pos[min(k, len(pos) - 1)])


_CV_CACHE: dict = {}


def cv_caps_for(rows, X, Xf, kind, wd):
    key = (X.shape, kind, wd)
    if key not in _CV_CACHE:
        _CV_CACHE[key] = cross_val(rows, X, Xf, kind, wd)
    return _CV_CACHE[key]


def cross_val(rows, X, Xf, kind, wd):
    train_idx = [i for i, r in enumerate(rows) if r["split"] == "train"]
    visits = sorted({rows[i]["visit"] for i in train_idx})
    fold_of = {v: int(hashlib.sha1(v.encode()).hexdigest(), 16) % FOLDS for v in visits}
    oof_idx, oof_p = [], []
    for f in range(FOLDS):
        fit = [i for i in train_idx if fold_of[rows[i]["visit"]] != f and trainable(rows[i])]
        hold = [i for i in train_idx if fold_of[rows[i]["visit"]] == f]
        y = np.array([rows[i]["label"] for i in fit])
        m = Model(kind, wd).fit(X[fit], Xf[fit], y)
        oof_idx += hold
        oof_p += list(m.prob(X[hold], Xf[hold]))
    return capture_scores(rows, oof_idx, oof_p)


def breakdown(caps, thr, key):
    groups = collections.defaultdict(dict)
    for aid, c in caps.items():
        groups[key(c[2])][aid] = c
    lines = []
    for g in sorted(groups, key=str):
        p, r, tp, fp, fn = pr_at(groups[g], thr)
        npos = tp + fn
        nneg = sum(1 for _, y, _ in groups[g].values() if not y)
        lines.append(f"    {str(g)[:22]:22s} bucks {npos:3d}  does {nneg:4d}  "
                     f"recall {r:5.2f}  precision {p:5.2f}  (missed {fn}, false bucks {fp})")
    return "\n".join(lines)


def main() -> int:
    print("== model selection (5-fold CV by visit, train split only)")
    best = None
    for fset in FEATURE_SETS:
        rows, X, Xf = load(fset)
        for kind, wd in HEADS:
            caps = cross_val(rows, X, Xf, kind, wd)
            score = ap(caps)
            thr = threshold_for_recall(caps, TARGET_RECALL)
            p, r, *_ = pr_at(caps, thr)
            print(f"  {fset_name(fset):18s} {kind:8s} wd={wd:<5g} AP {score:.3f}   "
                  f"at {TARGET_RECALL:.0%} recall: precision {p:.2f}")
            crit = (p if p == p else 0.0, score)  # precision at target recall, then AP
            if best is None or crit > best[0]:
                best = (crit, fset, kind, wd, thr)
    _, fset, kind, wd, thr = best
    rows, X, Xf = load(fset)
    print(f"  chosen: {fset_name(fset)} {kind} wd={wd}, threshold {thr:.4g} "
          f"(CV recall target {TARGET_RECALL:.0%})")

    fit = [i for i, r in enumerate(rows) if r["split"] == "train" and trainable(r)]
    model = Model(kind, wd).fit(X[fit], Xf[fit], np.array([rows[i]["label"] for i in fit]))
    ev = [i for i, r in enumerate(rows) if r["split"] == "eval"]
    caps = capture_scores(rows, ev, model.prob(X[ev], Xf[ev]))
    p, r, tp, fp, fn = pr_at(caps, thr)
    base = sum(y for _, y, _ in caps.values()) / len(caps)
    print("\n== HELD-OUT EVAL (scored once; capture level: any buck present)")
    print(f"  captures {len(caps)} (bucks {tp + fn}, does {len(caps) - tp - fn}); base rate {base:.2f}")
    print(f"  AP {ap(caps):.3f}   threshold {thr:.3f}: recall {r:.2f}  precision {p:.2f}  "
          f"(caught {tp}, missed {fn}, false bucks {fp})")
    for target in (0.80, 0.85, 0.95):
        t2 = threshold_for_recall(cv_caps_for(rows, X, Xf, kind, wd), target)
        p2, r2, tp2, fp2, fn2 = pr_at(caps, t2)
        print(f"  (if tuned for {target:.0%} CV recall: eval recall {r2:.2f}, precision {p2:.2f}, "
              f"false bucks {fp2})")
    for name, key in [("day/night", lambda x: "night" if x["ir"] else "day"),
                      ("photo/video", lambda x: x["media"]),
                      ("month", lambda x: x["date"][:7]),
                      ("deer in frame", lambda x: "1" if x["n_deer"] == 1 else "2+"),
                      ("station", lambda x: x["station"])]:
        print(f"  by {name}:\n{breakdown(caps, thr, key)}")

    torch.save({"kind": kind, "wd": wd, "threshold": thr,
                "features": {"file": fset[0], "keys": list(fset[1])}, "mu": model.mu, "sd": model.sd,
                "state_dict": model.head.state_dict(),
                "target_recall": TARGET_RECALL}, OUT / "sex_head.pt")
    cv_p, cv_r, *_ = pr_at(cv_caps_for(rows, X, Xf, kind, wd), thr)
    (OUT / "metrics.json").write_text(json.dumps({
        "features": fset_name(fset), "head": kind, "wd": wd, "threshold": thr,
        "target_recall": TARGET_RECALL, "cv_precision_at_target": cv_p, "cv_recall": cv_r,
        "eval_ap": ap(caps), "eval_recall": r, "eval_precision": p,
        "eval_bucks": tp + fn, "eval_does": len(caps) - tp - fn,
        "train_crops": len(fit),
        "backbone": json.loads((OUT / "backbone.json").read_text())
        if (OUT / "backbone.json").exists() else None}, indent=1))
    errs = {"missed_bucks": [], "false_bucks": []}
    for score, y, r in sorted(caps.values(), key=lambda c: c[0]):
        if y and score < thr:
            errs["missed_bucks"].append([float(score), r["crop"], r["station"]])
        elif not y and score >= thr:
            errs["false_bucks"].append([float(score), r["crop"], r["station"]])
    (OUT / "eval_errors.json").write_text(json.dumps(errs, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
