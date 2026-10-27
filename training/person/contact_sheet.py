#!/usr/bin/env python
"""Local HTML contact sheet: one row of thumbnails per cluster, for naming.

Run after embed_cluster.py. Writes derived/training/person/contact_sheet.html
next to crops/ (relative links, open it straight from disk). Clusters are
sorted by size; each header lists stations, days and visits so a cluster
that is "one person across days" stands apart from "one camera's
afternoon". Never published: these are images of real people.
"""
from __future__ import annotations

import collections
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.person.common import OUT, load_rows  # noqa: E402

MAX_THUMBS = 40
MIN_SIZE = 2  # singletons go in one trailing block


def main() -> int:
    rows = load_rows()
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["cluster"]].append(r)
    big = sorted((g for g in groups.values() if len(g) >= MIN_SIZE), key=len, reverse=True)
    singles = [r for g in groups.values() if len(g) < MIN_SIZE for r in g]

    def thumbs(rs):
        rs = sorted(rs, key=lambda r: r["when"])
        step = max(1, len(rs) // MAX_THUMBS)
        return "".join(
            f'<figure><img loading="lazy" src="crops/{Path(r["crop"]).name}">'
            f'<figcaption>{html.escape(r["station"])}<br>{r["when"][:16].replace("T", " ")}'
            f'</figcaption></figure>' for r in rs[::step][:MAX_THUMBS])

    parts = []
    for g in big:
        stations = collections.Counter(r["station"] for r in g).most_common()
        days = sorted({r["date"] for r in g})
        parts.append(
            f'<section><h2>cluster {g[0]["cluster"]} · {len(g)} crops · {len(days)} day(s) · '
            f'{len({r["visit"] for r in g})} visit(s)</h2><p>'
            + html.escape(", ".join(f"{s} {n}" for s, n in stations))
            + f' · {days[0]} → {days[-1]}</p><div class="row">{thumbs(g)}</div></section>')
    parts.append(f'<section><h2>singletons · {len(singles)}</h2><div class="row">'
                 f'{thumbs(singles)}</div></section>')
    page = f"""<!doctype html><meta charset="utf-8"><title>Person clusters</title>
<style>
body{{font:13px system-ui;margin:16px;background:#fafafa;color:#222}}
h2{{font-size:14px;margin:18px 0 2px}} p{{margin:0 0 6px;color:#666}}
.row{{display:flex;flex-wrap:wrap;gap:6px}}
figure{{margin:0;width:120px}} img{{width:120px;height:160px;object-fit:contain;background:#ddd}}
figcaption{{font-size:10px;color:#555;line-height:1.2}}
</style>
<h1>Person clusters ({len(rows)} crops, {len(big)} clusters + {len(singles)} singletons)</h1>
<p>Rough DINOv2 grouping — expect clusters to follow clothing and camera, not identity.</p>
{''.join(parts)}"""
    dest = OUT / "contact_sheet.html"
    dest.write_text(page)
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
