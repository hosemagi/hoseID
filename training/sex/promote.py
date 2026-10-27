#!/usr/bin/env python
"""Archive this retrain and decide whether it becomes the current model.

Run with .venv-classifier (fetchers.common for the notify needs requests;
the report email's example crops need PIL — both are there).
Copies the working outputs to runs/<date>[-N]/ and points `current` at it,
unless its CV precision at the target recall dropped by more than
MAX_DROP versus the current model — then it is kept but held back, and the
operator is told. The held-out eval changes every month (new visits join
the split), so CV precision is the like-for-like number; eval is reported.

The KEEP newest runs are kept (~2 MB each), plus whichever run `current`
points at if it is older — a rewound model is never pruned out from under
the pipeline. History is the runs/ directory; `current` is a pointer. Rewind:
    promote.py --list            runs with their numbers, current marked
    promote.py --use 2026-10-26  point current at that run (logged)

Either way Sage emails the operator the monthly report (mail_report.py).
If the email can't go out (Mail Bridge only runs in a login session, and
nvlm has no auto-login), the operator gets an ntfy ping with the run path.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import OUT  # noqa: E402

MAX_DROP = 0.05
KEEP = 3
FILES = ["sex_head.pt", "metrics.json", "report.txt", "eval_errors.json", "rows.jsonl", "backbone.json"]


HISTORY = OUT / "promotions.jsonl"


def _point_current(run: Path, reason: str) -> None:
    current = OUT / "current"
    before = current.resolve().name if current.exists() else None
    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(run.relative_to(OUT))
    with HISTORY.open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "from": before, "to": run.name, "reason": reason}) + "\n")


def list_runs() -> str:
    current = (OUT / "current").resolve().name if (OUT / "current").exists() else None
    lines = []
    for run in sorted((OUT / "runs").iterdir()) if (OUT / "runs").exists() else []:
        try:
            m = json.loads((run / "metrics.json").read_text())
        except (OSError, ValueError):
            continue
        lines.append(f"{'*' if run.name == current else ' '} {run.name:14s} "
                     f"CV prec@{m['target_recall']:.0%} {m['cv_precision_at_target']:.2f}  "
                     f"eval AP {m['eval_ap']:.2f} recall {m['eval_recall']:.2f} "
                     f"precision {m['eval_precision']:.2f}  train crops {m['train_crops']}")
    return "\n".join(lines) or "(no runs)"


def use(name: str) -> str:
    run = OUT / "runs" / name
    if not (run / "sex_head.pt").exists():
        raise SystemExit(f"no run {name!r} (see --list)")
    _point_current(run, "manual --use")
    return f"current -> {name}"


def prune() -> list[str]:
    """Delete runs beyond the KEEP newest, never the current one."""
    runs = sorted((OUT / "runs").iterdir(), key=lambda p: p.name) if (OUT / "runs").exists() else []
    current = (OUT / "current").resolve() if (OUT / "current").exists() else None
    removed = []
    for run in runs[:-KEEP]:
        if current is not None and run.resolve() == current:
            continue
        shutil.rmtree(run)
        removed.append(run.name)
    if removed:
        with HISTORY.open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "pruned": removed, "keep": KEEP}) + "\n")
    return removed


def _new_run_dir() -> Path:
    base = date.today().isoformat()
    run, n = OUT / "runs" / base, 2
    while run.exists():  # a second run today must not overwrite the first
        run, n = OUT / "runs" / f"{base}-{n}", n + 1
    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--use", metavar="RUN")
    args = ap.parse_args()
    if args.list:
        print(list_runs())
        return 0
    if args.use:
        print(use(args.use))
        return 0

    from fetchers.common import load_config, notify

    new = json.loads((OUT / "metrics.json").read_text())
    run = _new_run_dir()
    run.mkdir(parents=True)
    for f in FILES:
        if (OUT / f).exists():
            shutil.copy2(OUT / f, run / f)

    current = OUT / "current"
    old = json.loads((current / "metrics.json").read_text()) if (current / "metrics.json").exists() else None
    prev_run = current.resolve() if old else None
    cfg = load_config()

    def mail(status: str) -> None:
        from training.sex.mail_report import report
        try:
            print(report(run, status, old, prev_run))
        except Exception as e:  # noqa: BLE001 — never lose the report silently
            print(f"report email failed: {type(e).__name__}: {e}")
            notify(cfg, f"buck/doe monthly report could not be emailed ({type(e).__name__}: {e}). "
                        f"Run {status}: {summary}. Report: {run / 'report.txt'}",
                   title="hoseid buck/doe report not emailed")
    summary = (f"CV precision@{new['target_recall']:.0%} recall {new['cv_precision_at_target']:.2f}; "
               f"held-out AP {new['eval_ap']:.2f}, recall {new['eval_recall']:.2f}, "
               f"precision {new['eval_precision']:.2f} ({new['eval_bucks']} bucks / "
               f"{new['eval_does']} does); {new['train_crops']} training crops")
    if old and new["cv_precision_at_target"] < old["cv_precision_at_target"] - MAX_DROP:
        msg = (f"buck/doe retrain held back: {summary}. Current model "
               f"({current.resolve().name}) had CV precision {old['cv_precision_at_target']:.2f}. "
               f"Run kept in {run}.")
        print(msg)
        notify(cfg, msg, title="hoseid buck/doe retrain held back")
        mail("held_back")
        print(f"pruned: {prune() or 'nothing'}")
        return 0
    _point_current(run, "monthly retrain")
    print(f"promoted {run.name}: {summary}")
    mail("promoted")
    print(f"pruned: {prune() or 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
