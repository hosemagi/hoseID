#!/usr/bin/env python
"""hoseid-fetch: auto-fetch daemon for Reveal photos and Arlo recordings.

Run:  .venv-fetch/bin/python -m fetchers.daemon [--once] [--backfill-hours N]

  --once             single pass (poll Reveal, sweep Arlo) then exit
  --backfill-hours N on FIRST run only, set cursors N hours into the past
                     instead of "now" (ingest is idempotent, so overlap with
                     existing archive costs bandwidth, never correctness)

Loop shape: Reveal polls on a fixed interval (no push channel exists; the
cameras batch-transmit anyway). Arlo sweeps when the event stream reports
media activity — debounced — and on a slow fixed interval as a catch-all.
A source failing never stops the other; repeated failures alert via notify().
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetchers import reveal as reveal_mod  # noqa: E402
from fetchers.arlo import ArloFetcher  # noqa: E402
from fetchers.common import State, load_config, log, notify  # noqa: E402

FAILURE_ALERT_THRESHOLD = 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill-hours", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    state = State()
    stopping = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(flag=True))

    reveal = reveal_mod.RevealClient(cfg["reveal"]["email"], cfg["reveal"]["password"])
    arlo = ArloFetcher(cfg, state)

    backfill_h = args.backfill_hours
    if backfill_h is None:
        backfill_h = cfg["fetch"].get("backfill_hours", 0)

    # First-run bootstrap: place cursors so history isn't re-downloaded.
    if state.get("reveal_cursor_ms") is None and backfill_h:
        cutoff = int((time.time() - backfill_h * 3600) * 1000)
        state.set("reveal_cursor_ms", cutoff)
        log(f"reveal: cursor backfilled {backfill_h}h -> {cutoff}")

    arlo.connect()
    if state.get("arlo_cursors") is None:
        if backfill_h:
            cutoff = int((time.time() - backfill_h * 3600) * 1000)
            state.set("arlo_cursors",
                      {c.device_id: cutoff for c in arlo._arlo.cameras})
            log(f"arlo: cursors backfilled {backfill_h}h")
        else:
            arlo.bootstrap_cursors()

    reveal_interval = cfg["fetch"]["reveal_interval_s"]
    arlo_interval = cfg["fetch"]["arlo_sweep_interval_s"]
    failures = {"reveal": 0, "arlo": 0}
    next_run = {"reveal": 0.0, "arlo": 0.0}

    def run_source(name: str, fn) -> None:
        try:
            fn()
            failures[name] = 0
        except Exception as e:
            failures[name] += 1
            log(f"{name}: ERROR ({failures[name]} consecutive): "
                f"{type(e).__name__}: {e}")
            if failures[name] == FAILURE_ALERT_THRESHOLD:
                notify(cfg, f"hoseid-fetch: {name} failing repeatedly: {e}")

    log(f"hoseid-fetch up (reveal every {reveal_interval}s, "
        f"arlo sweep every {arlo_interval}s + event-driven)")

    while True:
        now = time.time()
        if now >= next_run["reveal"]:
            run_source("reveal", lambda: reveal_mod.poll(reveal, state))
            next_run["reveal"] = now + reveal_interval
        if now >= next_run["arlo"] or arlo.sweep_wanted.is_set():
            if arlo.sweep_wanted.is_set():
                arlo.sweep_wanted.clear()
                time.sleep(20)   # debounce: let the clip finish uploading
            run_source("arlo", arlo.sweep)
            next_run["arlo"] = time.time() + arlo_interval
        if args.once:
            break
        # wake early for arlo events, otherwise tick towards the next poll
        arlo.sweep_wanted.wait(timeout=min(30.0, max(1.0,
            min(next_run.values()) - time.time())))
        if stopping["flag"]:
            break

    arlo.stop()
    log("hoseid-fetch stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
