#!/usr/bin/env python
"""Can DINOv2 features tell P's people apart? Leave-group-out evaluation.

Run with .venv-classifier after extract.py + embed_cluster.py. Labels come
from hoseID review: a crop is labeled when its capture has exactly one
person crop (rows.jsonl) and the latest review names exactly one person
from tags.db `people` -- one name, one box, no ambiguity. Multi-person
captures are left out.

Three ways to hold data out, from leaky to honest:
  crop  random crops: frames of one visit land on both sides
  visit whole visits held out (30-min chains at one station)
  day   whole days held out: the realistic case, since a new day usually
        means different clothes
Two classifiers on the L2-normalised features: k-nearest-neighbour
(cosine, k=5, majority vote) and nearest class centroid. Reported as
balanced accuracy (mean per-person recall) next to a majority-class
baseline, because one person dominates the captures.
"""
from __future__ import annotations

import collections
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.person.common import OUT, load_rows  # noqa: E402

TAGS_DB = Path.home() / "trailcam/tags/tags.db"
FOLDS = 5
K = 5


def labeled() -> tuple[list[dict], list[int]]:
    rows = load_rows()
    conn = sqlite3.connect(f"file:{TAGS_DB}?mode=ro", uri=True)
    people = {r[0] for r in conn.execute("SELECT name FROM people")}
    latest = dict(conn.execute("""
        SELECT r.asset_id, r.individual FROM reviews r
        JOIN (SELECT asset_id, MAX(id) id FROM reviews WHERE asset_id IS NOT NULL
              GROUP BY asset_id) m ON r.id = m.id"""))
    per_asset = collections.Counter(r["asset_id"] for r in rows)
    out, idx = [], []
    for i, r in enumerate(rows):
        names = [n for n in (latest.get(r["asset_id"]) or "").split("+") if n in people]
        if per_asset[r["asset_id"]] == 1 and len(names) == 1:
            out.append(dict(r, person=names[0]))
            idx.append(i)
    return out, idx


def _fold(key: str) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % FOLDS


def evaluate(X, y, groups, classes):
    import numpy as np
    folds = np.array([_fold(g) for g in groups])
    pred_knn = np.empty(len(y), dtype=int)
    pred_cen = np.empty(len(y), dtype=int)
    for f in range(FOLDS):
        te, tr = folds == f, folds != f
        if not te.any() or not tr.any():
            continue
        S = X[te] @ X[tr].T
        nn = np.argsort(-S, axis=1)[:, :K]
        for row, (i, n) in enumerate(zip(np.where(te)[0], nn)):
            votes = collections.Counter(y[tr][n])
            pred_knn[i] = votes.most_common(1)[0][0]
        cents = np.stack([X[tr & (y == c)].mean(0) if (tr & (y == c)).any()
                          else np.zeros(X.shape[1]) for c in range(len(classes))])
        cents /= np.linalg.norm(cents, axis=1, keepdims=True) + 1e-9
        pred_cen[te] = np.argmax(X[te] @ cents.T, axis=1)

    def bal(pred):
        rec = {classes[c]: float((pred[y == c] == c).mean()) for c in range(len(classes))}
        return float(np.mean(list(rec.values()))), rec
    return bal(pred_knn), bal(pred_cen)


def main() -> int:
    import argparse
    import numpy as np
    ap = argparse.ArgumentParser()
    ap.add_argument("features", nargs="?", default="features_dino.npy",
                    help="an .npy under derived/training/person, row-aligned with rows.jsonl")
    args = ap.parse_args()
    rows, idx = labeled()
    X = np.load(OUT / args.features)[idx]
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    print(f"features: {args.features}")
    classes = sorted({r["person"] for r in rows})
    y = np.array([classes.index(r["person"]) for r in rows])
    counts = collections.Counter(r["person"] for r in rows)
    days = {c: len({r["date"] for r in rows if r["person"] == c}) for c in classes}
    print(f"labeled crops: {len(rows)} (single person, single name)")
    for c in classes:
        print(f"  {c:10s} {counts[c]:4d} crops on {days[c]:2d} day(s)")
    # majority-class baseline: balanced accuracy is 1/len(classes) by construction
    print(f"\nbalanced accuracy (mean per-person recall); chance = {1 / len(classes):.2f}")
    print(f"{'held out':8s} {'kNN':>6s} {'centroid':>9s}   per-person recall (kNN)")
    for name, key in (("crop", "detection_id"), ("visit", "visit"), ("day", "date")):
        (bk, rk), (bc, _) = evaluate(X, y, [r[key] for r in rows], classes)
        per = " ".join(f"{c}:{v:.2f}" for c, v in rk.items())
        print(f"{name:8s} {bk:6.2f} {bc:9.2f}   {per}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
