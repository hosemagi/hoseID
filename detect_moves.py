#!/usr/bin/env python
"""Flag possible camera repositioning from frame-similarity step changes.

Method: compare median thumbnails of the SAME clock hour across nearby days,
so sun/shadow position is held constant (naive consecutive-frame comparison
false-flags on shadow motion). For each day boundary we take the BEST
similarity over all shared hours — if any matched hour still looks the same,
the camera didn't move. A boundary is flagged only when that best similarity
is low AND the following boundary is also low (a real move persists; a one-day
anomaly is weather).

This script only FLAGS — bumping the zone epoch stays a manual action in the
review UI.

Run:  ~/venvs/megadetector/bin/python detect_moves.py
Writes ~/trailcam/derived/camera-move-flags.json (device_id -> "~date (score)")
The review app surfaces flags from that file at startup.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ASSETS = Path("/Users/hosebot/trailcam/landing/assets")
OUT = Path("/Users/hosebot/trailcam/derived/camera-move-flags.json")
THUMB = (48, 27)
DAY_HOURS = range(6, 20)
MAX_GAP_DAYS = 10       # don't compare across longer gaps (vegetation drift)
FLAG_BELOW = 0.35       # best same-hour similarity that counts as "changed"

FNAME_RE = re.compile(r"(?P<device>\d{15})-\d+-\d+-(?P<ts>\d{14})-[A-Z]+\d+\.jpg$")


def thumb(path):
    im = Image.open(path).convert("L").resize(THUMB)
    a = np.asarray(im, dtype=np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    return a


def sim(a, b):
    return float((a * b).mean())


def main():
    # (device, date) -> hour -> [paths]
    buckets = defaultdict(lambda: defaultdict(list))
    for p in sorted(ASSETS.rglob("*.jpg")):
        m = FNAME_RE.search(p.name)
        if not m:
            continue
        ts = m.group("ts")
        hour = int(ts[8:10])
        if hour not in DAY_HOURS:
            continue
        date = f"{ts[4:8]}-{ts[0:2]}-{ts[2:4]}"
        buckets[(m.group("device"), date)][hour].append(p)

    per_dev_dates = defaultdict(list)
    for (dev, date) in sorted(buckets):
        per_dev_dates[dev].append(date)

    def day_median(dev, date, hour):
        paths = buckets[(dev, date)][hour]
        return np.median(np.stack([thumb(p) for p in paths]), axis=0)

    def boundary_sim(dev, d1, d2):
        """Best same-hour similarity between two days; None if no shared hour."""
        shared = set(buckets[(dev, d1)]) & set(buckets[(dev, d2)])
        if not shared:
            return None
        return max(sim(day_median(dev, d1, h), day_median(dev, d2, h))
                   for h in shared)

    flags = {}
    for dev, dates in per_dev_dates.items():
        sims = []          # (boundary_date, best_sim)
        for d1, d2 in zip(dates, dates[1:]):
            gap = (np.datetime64(d2) - np.datetime64(d1)).astype(int)
            if gap > MAX_GAP_DAYS:
                continue
            s = boundary_sim(dev, d1, d2)
            if s is not None:
                sims.append((d2, s))
        if not sims:
            print(f"{dev}: no comparable day boundaries")
            continue
        worst = min(sims, key=lambda x: x[1])
        print(f"{dev}: {len(sims)} boundaries, worst {worst[1]:.3f} at {worst[0]}"
              f" (median {np.median([s for _, s in sims]):.3f})")
        for i, (date, s) in enumerate(sims):
            nxt = sims[i + 1][1] if i + 1 < len(sims) else None
            if s < FLAG_BELOW and (nxt is None or nxt < FLAG_BELOW):
                flags[dev] = f"~{date} (score {s:.2f})"
                break      # first persistent step per device is enough

    OUT.write_text(json.dumps(flags, indent=2))
    print(f"\n{len(flags)} flag(s) -> {OUT}")


if __name__ == "__main__":
    main()
