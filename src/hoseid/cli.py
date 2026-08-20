"""hoseid CLI.

Ingest and analysis are separate commands on purpose (invariant 6): ingest is cheap, continuous
and must not fail; analysis is batched and re-runnable.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import ingest_reveal_export, landing, paths, review, reviews, stations, tags as tagstore
from .ingest_sd import ingest_directory
from .sidecar import SidecarError, validate_sidecar

PROJECT = Path(__file__).resolve().parents[2]


@click.group()
def cli() -> None:
    """Trail-camera capture identification."""


# --- landing zone -------------------------------------------------------------
@cli.command()
def init() -> None:
    """Create the directory layout and config templates."""
    paths.ensure_layout()
    written = stations.write_templates()
    click.echo(f"root: {paths.root()}")
    for d in ("landing", "derived", "tags"):
        click.echo(f"  {d}/")
    for p in written:
        click.echo(f"  wrote template {p.name}")
    if not written:
        click.echo("  templates already present, left alone")


@cli.command("ingest-sd")
@click.argument("src", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--station", required=True, help="Station name for every file in this directory")
@click.option("--vendor", default="unknown")
@click.option("--device-id", default=None, help="Camera serial; the repair handle for renames")
@click.option("--dry-run", is_flag=True)
def ingest_sd_cmd(src: Path, station: str, vendor: str, device_id: str | None,
                  dry_run: bool) -> None:
    """Ingest a directory of SD-card files under one station."""
    rep = ingest_directory(src, station, vendor=vendor, device_id=device_id, dry_run=dry_run)
    click.echo(json.dumps({
        "scanned": rep.scanned, "ingested": rep.ingested, "duplicates": rep.duplicates,
        "errors": rep.errors[:10], "n_errors": len(rep.errors), "dry_run": dry_run}, indent=1))
    if rep.errors:
        sys.exit(1)


@cli.command("ingest-reveal-export")
@click.argument("src", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True)
def ingest_reveal_export_cmd(src: Path, dry_run: bool) -> None:
    """Ingest a bulk Reveal web export, resolving station per file from the camera serial.

    Separate from ingest-sd because an export interleaves every camera on the property, so one
    station for the whole directory would be wrong. Files whose serial is not in stations.json
    are refused rather than guessed at.
    """
    rep = ingest_reveal_export.ingest_export(src, dry_run=dry_run)
    click.echo(json.dumps({
        "scanned": rep.scanned, "ingested": rep.ingested, "duplicates": rep.duplicates,
        "unparsed_name": len(rep.unparsed_name),
        "unparsed_examples": rep.unparsed_name[:5],
        "unknown_device": rep.unknown_device,
        "errors": rep.errors[:10], "n_errors": len(rep.errors), "dry_run": dry_run}, indent=1))
    if rep.errors:
        sys.exit(1)


@cli.command()
@click.option("--verify-digests", is_flag=True, help="Re-hash every asset (slow)")
def check(verify_digests: bool) -> None:
    """Validate the entire landing zone against the sidecar contract."""
    rep = landing.check_landing_zone(verify_digests=verify_digests)
    out = {
        "sidecars": rep.n_sidecars, "assets_present": rep.n_assets_present,
        "invalid": rep.invalid[:20], "missing_asset": rep.missing_asset[:20],
        "orphan_assets": rep.orphan_assets[:20], "digest_mismatch": rep.digest_mismatch[:20],
        "untrusted_capture_time": len(rep.untrusted_time),
        "staged_duplicates": len(rep.staged_duplicates),
        "staged_duplicate_examples": rep.staged_duplicates[:5],
        "ok": rep.ok,
    }
    click.echo(json.dumps(out, indent=1))
    if rep.staged_duplicates:
        click.echo(
            f"\nnote: {len(rep.staged_duplicates)} file(s) sit under a vendor-supplied name but "
            f"are already ingested content-addressed. Nothing is at risk and the check still "
            f"passes; they are redundant copies taking disk. The review app still serves some of "
            f"them by path, so remove them only alongside repointing it.", err=True)
    sys.exit(0 if rep.ok else 1)


@cli.command("validate-sidecar")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate_sidecar_cmd(path: Path) -> None:
    """Validate one sidecar file."""
    try:
        sc = validate_sidecar(path)
    except SidecarError as e:
        click.echo(f"INVALID: {e}", err=True)
        sys.exit(1)
    click.echo(f"OK  {sc.asset_id}  station={sc.station}  "
               f"time={sc.capture_time.isoformat()} ({sc.capture_time_source}, "
               f"trusted={sc.time_is_trustworthy})")


# --- pipeline -----------------------------------------------------------------
def _venv_python(name: str) -> str:
    p = PROJECT / name / "bin" / "python"
    if not p.exists():
        raise click.ClickException(
            f"missing venv {p}. The stages need separate environments: speciesnet and "
            f"megadetector have an unresolvable protobuf conflict (findings §A9).")
    return str(p)


@cli.command()
@click.option("--run-id", default=None, help="Defaults to a UTC timestamp")
@click.option("--threshold", type=float, default=0.2)
@click.option("--limit", type=int, default=0)
@click.option("--detect-only", is_flag=True)
@click.option("--classify-only", is_flag=True)
@click.option("--country", default="USA")
@click.option("--admin1", default="CA")
def run(run_id: str | None, threshold: float, limit: int, detect_only: bool,
        classify_only: bool, country: str, admin1: str) -> None:
    """Run the pipeline over the landing zone.

    Each stage is a subprocess in its own venv, communicating through the filesystem and the
    detections database.
    """
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    click.echo(f"run_id: {run_id}")

    if not classify_only:
        cmd = [_venv_python(".venv-detector"), str(PROJECT / "stages" / "detect.py"),
               "--run-id", run_id, "--threshold", str(threshold), "--device", "cpu"]
        if limit:
            cmd += ["--limit", str(limit)]
        click.echo("== stage 1: detect (CPU) ==")
        if subprocess.call(cmd) != 0:
            raise click.ClickException("stage 1 failed")

    if not detect_only:
        cmd = [_venv_python(".venv-classifier"), str(PROJECT / "stages" / "classify.py"),
               "--run-id", run_id, "--country", country, "--admin1", admin1]
        click.echo("== stage 2: classify (MPS, crops only) ==")
        if subprocess.call(cmd) != 0:
            raise click.ClickException("stage 2 failed")

    click.echo(json.dumps(review.taxon_summary(run_id), indent=1))


# --- review -------------------------------------------------------------------
@cli.command()
@click.argument("run_id")
@click.option("--limit", type=int, default=25)
def queue(run_id: str, limit: int) -> None:
    """Review queue: every animal detection, ordered by priority. Never species-filtered."""
    for it in review.review_queue(run_id, limit=limit):
        click.echo(f"[{it.review_priority:6s}] {it.capture_time}  {it.station:14s} "
                   f"{str(it.taxon):22s} conf={it.taxon_confidence}  {it.detection_id[:12]}")


@cli.command()
@click.argument("run_id")
def stats(run_id: str) -> None:
    """Per-station activity, empty-capture rate, and decode failures."""
    failures = review.decode_failures(run_id)
    click.echo(json.dumps({"stations": review.station_activity(run_id),
                           "taxa": review.taxon_summary(run_id),
                           "decode_failures": len(failures),
                           "group_size": review.group_size_stats(run_id)}, indent=1))
    if failures:
        # Loud on purpose. These are clips the pipeline could not look at, and they are
        # deliberately absent from the empty-capture count.
        click.echo(f"\n!! {len(failures)} capture(s) could not be decoded "
                   f"-- these are NOT empty captures:", err=True)
        for f in failures[:10]:
            click.echo(f"   {f['station']:14s} {f['capture_time']}  "
                       f"{f['asset_id'][:20]}  {f['decode_error']}", err=True)


# --- tags ---------------------------------------------------------------------
@cli.command("tag")
@click.argument("detection_id")
@click.argument("tags_", nargs=-1, required=True, metavar="TAG...")
@click.option("--note", default=None)
def tag_cmd(detection_id: str, tags_: tuple[str, ...], note: str | None) -> None:
    """Add tags to a detection (review path only; the pipeline never writes tags)."""
    n = tagstore.add_tags(detection_id, tags_, note=note)
    click.echo(f"added {n} tag(s) to {detection_id}")


@cli.command("tags")
@click.argument("detection_id")
def tags_show(detection_id: str) -> None:
    for t in tagstore.tags_for(detection_id):
        click.echo(f"{t.tag:20s} {t.added_at}  {t.added_by}  {t.note or ''}")


@cli.command("vocab")
def vocab_cmd() -> None:
    """Existing tag vocabulary, most-used first. Backs autocomplete."""
    for tag, n, last in tagstore.vocabulary():
        click.echo(f"{n:5d}  {tag:24s} {last}")


# --- reviews (complete verdicts; see reviews.py on why this is not the tag store) ------
@cli.command("backfill-reviews")
@click.option("--dry-run", is_flag=True)
@click.option("--rehash", is_flag=True, help="Recompute asset_id even where one is already set")
def backfill_reviews_cmd(dry_run: bool, rehash: bool) -> None:
    """Fill reviews.asset_id so human verdicts can join pipeline captures.

    Hashes any reviewed file that is not already content-addressed, so the first run over a large
    export is slow. Idempotent: re-running only touches rows that still have no asset_id.
    """
    rep = reviews.backfill_asset_ids(dry_run=dry_run, rehash=rehash)
    click.echo(json.dumps({
        "reviews": rep.total, "already_had_asset_id": rep.already,
        "resolved_from_path": rep.from_path, "resolved_by_hashing": rep.hashed,
        "unresolvable_missing_file": rep.missing,
        "missing_examples": rep.missing_examples, "dry_run": dry_run}, indent=1))


@cli.command("score")
@click.argument("run_id")
def score_cmd(run_id: str) -> None:
    """Pipeline output vs P's review verdicts -- the accumulating regression suite.

    Two layers, reported separately: the detector (did it find anything) and the classifier (did
    it name it right). Run `backfill-reviews` first, or coverage will report everything unjoined.
    """
    click.echo(json.dumps(reviews.score_against_pipeline(run_id), indent=1))


@cli.command("sweep")
@click.argument("run_id")
def sweep_cmd(run_id: str) -> None:
    """What a detector-confidence floor would cost and save, measured on real verdicts."""
    click.echo(json.dumps(reviews.confidence_sweep(run_id), indent=1))


if __name__ == "__main__":
    cli()
