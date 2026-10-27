#!/usr/bin/env python
"""Train/eval split by visit, stratified by station x label x day/night.

A visit is one session at one station (captures within 30 min chained), and
stays whole: frames of one session are near-duplicates. Within each
(station, visit label, day/night) cell, visits are taken in a fixed hash
order into eval until that cell's eval share reaches EVAL_FRAC of its
captures, so one long session can't make up the eval set. A cell with two
or more visits always has at least one on each side; a single-visit cell
trains (it can't be both). Re-runnable on rows.jsonl without re-embedding.
"""
from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import OUT  # noqa: E402

EVAL_FRAC = 0.2


def assign_split(rows: list[dict]) -> None:
    caps: dict[str, dict] = {}
    for r in rows:
        caps.setdefault(r["asset_id"], r)
    visits: dict[str, list[dict]] = collections.defaultdict(list)
    for c in caps.values():
        visits[c["visit"]].append(c)
    cells: dict[tuple, list[str]] = collections.defaultdict(list)
    for vid, cs in visits.items():
        label = max(c["label"] for c in cs)
        night = sum(c["ir"] for c in cs) * 2 > len(cs)
        cells[(cs[0]["station"], label, night)].append(vid)
    eval_visits: set[str] = set()
    for vids in cells.values():
        if len(vids) < 2:
            continue
        vids.sort(key=lambda v: hashlib.sha1(v.encode()).hexdigest())
        total = sum(len(visits[v]) for v in vids)
        target, got, chosen = EVAL_FRAC * total, 0, []
        for v in vids:
            if got >= target:
                break
            if chosen and got + len(visits[v]) > 1.5 * target:
                continue  # would overshoot badly; try a smaller visit
            chosen.append(v)
            got += len(visits[v])
        if len(chosen) == len(vids):  # keep at least one visit for training
            chosen.pop()
        eval_visits.update(chosen)
    for r in rows:
        r["split"] = "eval" if r["visit"] in eval_visits else "train"


def coverage(rows: list[dict]) -> str:
    caps: dict[str, dict] = {}
    for r in rows:
        caps.setdefault(r["asset_id"], r)
    c = collections.Counter()
    for x in caps.values():
        dn = "night" if x["ir"] else "day"
        for key in (("all",), ("dn", dn), ("st", x["station"])):
            c[(key, x["split"], x["label"])] += 1
    keys = sorted({k for k, _, _ in c}, key=lambda k: (len(k), k))
    lines = [f"{'':24s} train bucks/does   eval bucks/does"]
    for k in keys:
        name = k[-1] if k != ("all",) else "ALL"
        lines.append(f"{name[:24]:24s} {c[(k,'train',1)]:5d}/{c[(k,'train',0)]:<6d}      "
                     f"{c[(k,'eval',1)]:5d}/{c[(k,'eval',0)]:<6d}")
    return "\n".join(lines)


def main() -> int:
    path = OUT / "rows.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assign_split(rows)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(coverage(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
