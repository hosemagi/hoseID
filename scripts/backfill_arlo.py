#!/usr/bin/env python
"""One-off: backfill historical Arlo cloud recordings into the landing zone.

Filters by Arlo's own object classification (objCategory): only the requested
types are downloaded (P asked for vehicle + animal). Skips are counted and
reported, never silent. Ingest is content-addressed and idempotent, so overlap
with the daemon's captures is a no-op.

Stop the fetch daemon before running — two concurrent Arlo sessions can evict
each other:  launchctl bootout gui/501/com.hoseid.fetch

Run:  .venv-fetch/bin/python scripts/backfill_arlo.py --days 31 --types animal,vehicle
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetchers.arlo import ArloFetcher  # noqa: E402  (installs the Bridge IMAP patch)
from fetchers.common import State, load_config, log  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--types", default="animal,vehicle",
                    help="comma-separated objCategory values to ingest")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    wanted = {t.strip().lower() for t in args.types.split(",")}

    cfg = load_config()
    state = State()   # cursors untouched: backfill never moves the daemon's cursor
    fetcher = ArloFetcher(cfg, state)
    fetcher._cfg = cfg

    import pyaarlo
    imap = cfg["tfa_imap"]
    arlo = pyaarlo.PyArlo(
        username=cfg["arlo"]["email"], password=cfg["arlo"]["password"],
        tfa_source="imap", tfa_type="email",
        tfa_host=f"{imap['host']}:{imap['port']}",
        tfa_username=imap["username"], tfa_password=imap["password"],
        storage_dir=str(Path.home() / ".config/hoseid-fetch/arlo-session"),
        save_session=True, synchronous_mode=True,
        library_days=args.days,
    )
    if not arlo.is_connected:
        print(f"connect failed: {arlo.last_error}"); return 1
    fetcher._arlo = arlo

    from fetchers.common import TMP_DIR
    seen = Counter()
    ingested = skipped_type = errors = 0
    for cam in arlo.cameras:
        vids = cam.last_n_videos(10000) or []
        log(f"{cam.name!r}: {len(vids)} recordings in the last {args.days}d")
        for vid in reversed(vids):
            otype = (vid.object_type or "untyped").lower()
            seen[otype] += 1
            if otype not in wanted:
                skipped_type += 1
                continue
            created_ms = int(vid.created_at or 0)
            if args.dry_run:
                continue
            name = f"{cam.device_id}_{created_ms}.mp4"
            tmp = TMP_DIR / name
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                vid.download_video(str(tmp))
                if not tmp.exists() or tmp.stat().st_size == 0:
                    raise RuntimeError("empty download")
                ingested += fetcher._ingest_video(cam, vid, tmp, created_ms)
            except Exception as e:
                errors += 1
                log(f"  ERROR {name}: {type(e).__name__}: {e}")
                tmp.unlink(missing_ok=True)

    log(f"backfill done: ingested {ingested} new, skipped {skipped_type} by type, "
        f"{errors} errors")
    log(f"library contents by Arlo objCategory: {dict(seen.most_common())}")
    arlo.stop(logout=False)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
