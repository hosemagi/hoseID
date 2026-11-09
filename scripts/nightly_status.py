"""Record the last nightly pipeline pass so the System page can say when it ran,
how long it took and which step (if any) failed.

`nightly_status.py` is a tiny CLI driven by nightly.sh:

    nightly_status.py begin
    nightly_status.py step pipeline 0 --started 2026-11-08T10:30:00Z
    nightly_status.py step sync_wildlife_log 2
    nightly_status.py step weather_backfill 0
    nightly_status.py step build_encounters 0
    nightly_status.py end

It keeps the in-progress run in a temp file next to the published status file
(`.tmp-nightly_status.json`) and publishes the final status file
(`~/trailcam/derived/nightly_status.json`) through `fsio.atomic_write_json`
(mode 0644: hoseserv reads it). Each subcommand updates the draft and republishes,
so a reader always sees a whole file. `begin` writes an open run (no `ended`),
`step` sets one step's exit code, start (`--started`, default now) and end
time, and `end` closes the run and deletes the draft. A step that never
reported keeps `exit: null`, and the run then counts as not ok. The draft persists across commands, so a run killed
mid-way leaves an in-progress file on disk rather than being replaced by last
night's finished snapshot.

The exit status is always 0 unless the arguments are wrong: nightly.sh runs
these after every step guarded with `|| true`, so status writing can never
break the run. `--path` lets tests point at a temp file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# this repo's src first: the venv's editable install may point at another checkout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hoseid import fsio
from hoseid.paths import nightly_status_file

SCHEMA_VERSION = 1

# Names nightly.sh knows, in run order. Anything else is a usage error.
STEP_NAMES = ("pipeline", "sync_wildlife_log", "weather_backfill", "build_encounters")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _open_run() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "started": _iso(_now()),
        "ended": None,
        "ok": None,
        "steps": [
            {"name": name, "exit": None, "started": None, "ended": None}
            for name in STEP_NAMES
        ],
    }


def _tmp_path(dest: Path) -> Path:
    return dest.parent / f".tmp-{dest.name}"


def _log_err(dest: Path, msg: str) -> None:
    print(f"nightly_status: could not write {dest}: {msg}", file=sys.stderr)


def _read(src: Path) -> dict:
    """The current draft if it is a well-formed run, else the published file if
    so, else a fresh open run. The draft is the source of truth inside a run;
    the published file is the fallback when the draft is missing."""
    try:
        data = json.loads(Path(src).read_text())
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("steps"):
        return data
    return _open_run()


def _commit(dest: Path, data: dict) -> None:
    """Publish `data`: write the draft, then atomically replace the published
    file with it. Never raises -- a failure here is a status miss, not a run
    failure, so nightly.sh can keep going."""
    try:
        fsio.atomic_write_json(_tmp_path(dest), data)
    except Exception as e:  # noqa: BLE001
        _log_err(_tmp_path(dest), str(e))
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_json(dest, data, mode=0o644)
    except Exception as e:  # noqa: BLE001
        _log_err(dest, str(e))


def _begin(dest: Path) -> None:
    _commit(dest, _open_run())


def _step(dest: Path, name: str, exit_code: int, started: str | None = None) -> None:
    data = _read(_tmp_path(dest))
    step = next((s for s in data["steps"] if s["name"] == name), None)
    if step is None:
        step = {"name": name, "exit": None, "started": None, "ended": None}
        data["steps"].append(step)
    step["started"] = started or _iso(_now())
    step["exit"] = exit_code
    step["ended"] = _iso(_now())
    _commit(dest, data)


def _end(dest: Path) -> None:
    data = _read(_tmp_path(dest))
    data["ended"] = _iso(_now())
    data["ok"] = bool(data["steps"]) and all(s["exit"] == 0 for s in data["steps"])
    _commit(dest, data)
    _tmp_path(dest).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record the nightly pipeline pass.")
    ap.add_argument("--path", type=Path, help="override the status file location")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("begin", help="start a run")

    p_step = sub.add_parser("step", help="record one step's exit code")
    p_step.add_argument("name")
    p_step.add_argument("exit", type=int)
    p_step.add_argument("--started", help="ISO-8601 time the step started (default: now)")

    sub.add_parser("end", help="close the run")

    args = ap.parse_args(argv)
    dest = args.path or nightly_status_file()

    if args.command == "begin":
        _begin(dest)
        return 0
    if args.command == "step":
        if args.name not in STEP_NAMES:
            print(f"nightly_status: unknown step {args.name!r}", file=sys.stderr)
            return 2
        if not 0 <= args.exit <= 255:
            print("nightly_status: exit must be between 0 and 255", file=sys.stderr)
            return 2
        _step(dest, args.name, args.exit, args.started)
        return 0
    if args.command == "end":
        _end(dest)
        return 0
    return 2  # pragma: no cover — argparse guarantees a matched subcommand


if __name__ == "__main__":
    sys.exit(main())
