"""The Settlement section of RUN_NOTES.md (card C11; plan section 12 "Short run-note fields").

The section is kept in `runs/<id>/settle/settlement.md` next to `grades.json`, and RUN_NOTES.md shows it at its
end. build.notes.write_run_notes appends it on every later rewrite (a controller round, a goalie-table refresh),
so settlement text is never lost. Settling never writes the manifest, inputs, versions or scenario files.
Money is in dollars; "unknown" is never $0.
"""

from __future__ import annotations

from pathlib import Path

HEADING = "## Settlement"


def _usd(c) -> str:
    return "unknown" if c is None else (f"-${-c / 100:.2f}" if c < 0 else f"${c / 100:.2f}")


def render(rec: dict) -> str:
    led = rec["ledger"]
    t = led["totals"]
    lines = ["", HEADING, "",
             f"- Settled {rec['settled_utc']} from {rec['standings']['files']} standings export(s); forecast "
             f"{rec['forecast']['status']} ({rec['forecast']['detail']})",
             f"- Money: fees {_usd(t['fees_cents'])}; known contests: gross {_usd(t['gross_known_cents'])}, net "
             f"{_usd(t['net_known_cents'])} on {_usd(t['fees_known_cents'])} of fees; fees in unknown contests "
             f"{_usd(t['fees_unknown_cents'])}; slate net {_usd(t['net_cents'])}"]
    for cid, c in led["by_contest"].items():
        lines.append(f"  - {cid} {c['contest_name']}: {c['entries']} entr{'y' if c['entries'] == 1 else 'ies'}, fees "
                     f"{_usd(c['fees_cents'])}, gross {_usd(c['gross_cents'])}, net {_usd(c['net_cents'])} "
                     f"(PAYOUT_SOURCE {'/'.join(c['sources'])})")
    for e in led["entries"]:
        lines.append(f"  - entry {e['entry_id']}: rank {e['rank']}{' (tied ' + str(e['tied']) + ')' if (e['tied'] or 1) > 1 else ''}"
                     f", {e['points']} pts, payout {_usd(e['payout_cents'])} ({e['payout_source']}: {e['source_detail'][:120]})"
                     + ("" if e["entered_matches"] is not False else f"; {e['entered_detail']}"))
    dd = rec.get("drawdown") or {}
    lines.append(f"- Ledger: cumulative net on known payouts {_usd(dd.get('cum_net_known_cents'))}, drawdown "
                 f"{_usd(dd.get('drawdown_cents'))}, max {_usd(dd.get('max_drawdown_cents'))} over {dd.get('runs', 0)} run(s) on "
                 f"{dd.get('slate_dates', 0)} slate date(s)"
                 f"{'' if dd.get('complete', True) else ' (incomplete: some payouts unknown)'}")
    for g in rec.get("ownership", []):
        lines.append(f"- Ownership {g['contest_id']} ({g['family']}): MAE {g['mae_all']:.2f} pts (active {g['mae_active']:.2f}, "
                     f"top-20 actual {g['mae_popular']:.2f}), Pearson {g['pearson']:.2f}, Spearman {g['spearman']:.2f}, "
                     f"top-10 recall {g['top_chalk_recall']:.1f}"
                     + (f", Captain share MAE {g['cpt_share_err']:.2f}" if g.get("cpt_share_err") is not None else "")
                     + (f", zero-observed mass {g['zero_observed_mass']:.1f}" if g.get("zero_observed_mass") is not None else ""))
    fg = rec.get("forecasts")
    if fg:
        o = fg["overall"]
        d = fg["goalie_decisions"]
        lines.append(f"- Forecasts (PARTICIPATION={fg['participation_status']}; {fg.get('skater_conditioning', '')[:48]}): "
                     f"{o.get('n', 0)} players, MAE "
                     f"{o.get('mae', float('nan')):.2f}, bias {o.get('bias', float('nan')):+.2f}, CRPS {o.get('crps', float('nan')):.2f}, "
                     f"p10-p90 coverage {o.get('cover_p10_p90', float('nan')):.2f} (target 0.80); goalie starts "
                     + (f"{d.get('accuracy')} of {d.get('teams')} teams right" if d.get("teams") else d.get("status", ""))
                     + (f" (missed: {'; '.join(d['misses'])})" if d.get("misses") else ""))
        lines.append(f"- Bonus-rate calibration: {fg['bonus_rate_calibration']}")
    else:
        lines.append("- Forecasts: not graded (the run saved no scenario cache)")
    for mode, gt in (rec.get("gates") or {}).items():
        lines.append(f"- Evidence gate ({mode}): tier {gt['tier']}; nothing is tuned (" +
                     "; ".join(f"{k}: {', '.join(v[:2])}" for k, v in list(gt["shortfalls"].items())[:2]) + ")")
    for b in rec.get("backlog", []):
        lines.append(f"- Backlog: {b['id']} {'added' if b['added'] else 'already holds'} {b['key']}")
    lines.append(f"- Statuses: OUTCOME_CALIBRATION={rec['statuses']['OUTCOME_CALIBRATION']} "
                 f"FIELD_CALIBRATION={rec['statuses']['FIELD_CALIBRATION']} PAYOUT_SOURCE={rec['statuses']['PAYOUT_SOURCE']}")
    for n in rec.get("notes", [])[:12]:
        lines.append(f"- Note: {n}")
    lines.append(f"- Keep / change: {rec.get('recommendation', 'keep; no parameter changes (the evidence gate allows none)')}")
    return "\n".join(lines) + "\n"


def write(run, rec: dict, text: str) -> Path:
    """settle/settlement.md, then RUN_NOTES.md with its earlier Settlement section (if any) replaced."""
    d = run.path / "settle"
    d.mkdir(exist_ok=True)
    (d / "settlement.md").write_text(text, encoding="utf-8")
    notes = run.path / "RUN_NOTES.md"
    body = notes.read_text(encoding="utf-8") if notes.exists() else f"# Run notes: {run.run_id}\n"
    body = strip(body)
    notes.write_text(body.rstrip("\n") + "\n" + text, encoding="utf-8")
    return notes


def strip(body: str) -> str:
    """RUN_NOTES text without its Settlement section (the section is always last)."""
    i = body.find("\n" + HEADING + "\n")
    return body if i < 0 else body[:i + 1]
