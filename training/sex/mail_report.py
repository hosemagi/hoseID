#!/usr/bin/env python
"""Sage's monthly buck/doe model report, by email.

Called by promote.py after each retrain (or by hand: --run <dir>
--dry-run writes the .eml instead of sending). Sent from Sage's Proton
address through Mail Bridge SMTP, like the briefings (hoseint.mail), to the
operator only: ~/.config/sage-mail.toml [model_reports] to = [...].
Bridge credentials: credentials.toml [tfa_imap].
"""
from __future__ import annotations

import argparse
import collections
import html
import io
import json
import smtplib
import ssl
import sys
import tomllib
from datetime import date
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from training.sex.common import OUT  # noqa: E402

MAIL_CFG = Path.home() / ".config/sage-mail.toml"
CREDS = Path.home() / ".config/hoseid-fetch/credentials.toml"
EXAMPLES = 3


def _rows(run: Path) -> list[dict]:
    p = run / "rows.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x] if p.exists() else []


def volume(rows: list[dict], prev_rows: list[dict]) -> dict:
    caps: dict[str, dict] = {}
    for r in rows:
        caps.setdefault(r["asset_id"], r)
    c = collections.Counter((x["split"], x["label"]) for x in caps.values())
    prev = {r["asset_id"] for r in prev_rows}
    new = [x for a, x in caps.items() if a not in prev] if prev_rows else []
    return {
        "train_bucks": c[("train", 1)], "train_does": c[("train", 0)],
        "eval_bucks": c[("eval", 1)], "eval_does": c[("eval", 0)],
        "crops": len(rows), "visits": len({r["visit"] for r in rows}),
        "stations": len({x["station"] for x in caps.values()}),
        "night_captures": sum(1 for x in caps.values() if x["ir"]),
        "day_captures": sum(1 for x in caps.values() if not x["ir"]),
        "legacy_captures": sum(1 for x in caps.values() if x.get("legacy")),
        "months": sorted({x["date"][:7] for x in caps.values()}),
        "new_bucks": sum(1 for x in new if x["label"]), "new_does": sum(1 for x in new if not x["label"]),
        "first_run": not prev_rows,
    }


def _thumb(path: str, px: int = 280) -> bytes | None:
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((px, px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a missing crop must not block the report
        return None


def build_message(run: Path, status: str, old: dict | None, prev_run: Path | None,
                  sender: str, to: list[str]) -> EmailMessage:
    m = json.loads((run / "metrics.json").read_text())
    v = volume(_rows(run), _rows(prev_run) if prev_run else [])
    report = (run / "report.txt").read_text() if (run / "report.txt").exists() else ""
    errs = json.loads((run / "eval_errors.json").read_text()) if (run / "eval_errors.json").exists() else {}

    held = status == "held_back"
    msg = EmailMessage()
    msg["Subject"] = (f"Buck/doe model {date.today():%B %Y}: "
                      + ("HELD BACK" if held else "updated")
                      + f" — AP {m['eval_ap']:.2f}, {m['eval_recall']:.0%} of bucks caught")
    msg["From"] = f"Sage <{sender}>"
    msg["To"] = ", ".join(to)

    def row(label, key, fmt="{:.2f}"):
        new = m[key]
        delta = ""
        if old and key in old and isinstance(old[key], (int, float)):
            d = new - old[key]
            delta = f" <span style='color:#888'>({'+' if d >= 0 else ''}{fmt.format(d)})</span>"
        return f"<tr><td>{label}</td><td><b>{fmt.format(new)}</b>{delta}</td></tr>"

    headline = ("<p style='color:#b54'><b>This month's model was held back.</b> Its cross-validated "
                "precision dropped by more than 0.05, so last month's model stays in use. "
                f"The new run is kept in <code>{html.escape(str(run))}</code>.</p>" if held else
                "<p>This month's model is now the current one.</p>")
    new_tags = ("first run — no previous model" if v["first_run"]
                else f"{v['new_bucks']} buck and {v['new_does']} doe captures since last run")
    plain = (f"Buck/doe model report ({status}). CV precision at {m['target_recall']:.0%} recall "
             f"{m['cv_precision_at_target']:.2f}; held-out AP {m['eval_ap']:.2f}, recall "
             f"{m['eval_recall']:.2f}, precision {m['eval_precision']:.2f}. Training: "
             f"{v['train_bucks']} buck / {v['train_does']} doe captures. New tags: {new_tags}.\n\n{report}")
    msg.set_content(plain)

    images, ex_html = [], ""
    for title, key in (("Bucks it missed (lowest scores)", "missed_bucks"),
                       ("Does it called bucks (highest scores)", "false_bucks")):
        items = errs.get(key, [])[:EXAMPLES] if key == "missed_bucks" else \
            sorted(errs.get(key, []), key=lambda e: -e[0])[:EXAMPLES]
        cells = ""
        for score, crop, station in items:
            data = _thumb(crop)
            if data is None:
                continue
            cid = make_msgid(domain="sage.local")
            images.append((cid, data))
            cells += (f"<td style='padding:4px;text-align:center'><img src='cid:{cid[1:-1]}' "
                      f"style='max-width:280px'><br><small>{html.escape(station)} · "
                      f"score {score:.3f}</small></td>")
        if cells:
            ex_html += f"<h3>{title}</h3><table><tr>{cells}</tr></table>"

    body = f"""<html><body style='font-family:-apple-system,sans-serif;max-width:760px'>
<h2>Buck/doe model — {date.today():%B %Y}</h2>
{headline}
<h3>Model</h3>
<table cellpadding='4'>
{row(f"CV precision at {m['target_recall']:.0%} recall (selection criterion)", 'cv_precision_at_target')}
{row('Held-out AP (area under precision-recall)', 'eval_ap')}
{row('Held-out recall (share of bucks caught)', 'eval_recall')}
{row('Held-out precision (share of "buck" calls that are bucks)', 'eval_precision')}
<tr><td>Features / head</td><td>{html.escape(m['features'])} / {m['head']} (threshold {m['threshold']:.4g})</td></tr>
</table>
<h3>Training volume</h3>
<table cellpadding='4'>
<tr><td>Train captures</td><td><b>{v['train_bucks']}</b> buck · <b>{v['train_does']}</b> doe ({m['train_crops']} training crops)</td></tr>
<tr><td>Held-out captures</td><td>{v['eval_bucks']} buck · {v['eval_does']} doe</td></tr>
<tr><td>New tags</td><td>{new_tags}</td></tr>
<tr><td>Coverage</td><td>{v['crops']} crops, {v['visits']} visits, {v['stations']} stations; {v['day_captures']} day / {v['night_captures']} night captures; months {', '.join(v['months'])}; {v['legacy_captures']} legacy captures</td></tr>
</table>
{ex_html}
<h3>Full eval report</h3>
<pre style='font-size:11px;background:#f6f6f6;padding:8px;overflow-x:auto'>{html.escape(report)}</pre>
<p style='color:#888;font-size:12px'>Labels are your reviewed wildlife-log tags (M = at least one buck present).
The held-out set is ~20% of visits, stratified by station, day/night and label; it changes as new visits join,
so month-to-month changes in held-out numbers include that drift. Run: {html.escape(run.name)}.</p>
</body></html>"""
    msg.add_alternative(body, subtype="html")
    html_part = msg.get_payload()[-1]
    for cid, data in images:
        html_part.add_related(data, "image", "jpeg", cid=cid)
    if report:
        msg.add_attachment(report.encode(), maintype="text", subtype="plain",
                           filename=f"buck-doe-report-{run.name}.txt")
    return msg


def send(msg: EmailMessage) -> None:
    creds = tomllib.loads(CREDS.read_text())["tfa_imap"]
    cfg = tomllib.loads(MAIL_CFG.read_text())
    smtp = {"host": "127.0.0.1", "port": 1025, **cfg.get("smtp", {})}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # Bridge: localhost, self-signed cert
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=60) as s:
        s.starttls(context=ctx)
        s.login(creds["username"], creds["password"])
        s.send_message(msg)


def report(run: Path, status: str, old: dict | None, prev_run: Path | None,
           dry_run: Path | None = None) -> str:
    cfg = tomllib.loads(MAIL_CFG.read_text())
    to = cfg.get("model_reports", {}).get("to") or []
    if not to:
        raise RuntimeError("no [model_reports] to = [...] in ~/.config/sage-mail.toml")
    sender = tomllib.loads(CREDS.read_text())["tfa_imap"]["username"]
    msg = build_message(run, status, old, prev_run, sender, to)
    if dry_run:
        dry_run.write_bytes(bytes(msg))
        return f"wrote {dry_run}"
    send(msg)
    return f"emailed report to {', '.join(to)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, default=OUT / "current")
    ap.add_argument("--status", default="promoted", choices=["promoted", "held_back"])
    ap.add_argument("--dry-run", type=Path, help="write the .eml here instead of sending")
    args = ap.parse_args()
    run = args.run.resolve()
    print(report(run, args.status, None, None, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
