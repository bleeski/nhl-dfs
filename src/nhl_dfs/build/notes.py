"""RUN_NOTES.md: the short human note for one run (plan section 12, "Short run-note fields").

Fields: run/slate ID; mode; entry count/fees; first-export time; final hash/status; material
news changes; accepted/rejected QA changes; fallback/relaxation; what worked operationally;
what failed; keep/change recommendation. Times display in America/Chicago. "No noteworthy
finding" is a valid line.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nhl_dfs.build.state import RunDir, atomic_write

CHICAGO = ZoneInfo("America/Chicago")


def chicago(iso_utc: str | None) -> str:
    if not iso_utc:
        return "none"
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return t.astimezone(CHICAGO).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _fees(fees: list[str]) -> str:
    counts = Counter(fees)
    return ", ".join(f"{n} x {fee}" for fee, n in sorted(counts.items())) or "none"


def render(m: dict[str, Any]) -> str:
    s = m["statuses"]
    versions = m.get("versions", [])
    first = versions[0] if versions else None
    final = versions[-1] if versions else None
    news = m.get("news", {})
    relax = m.get("relaxations", [])
    kinds = Counter(r["kind"] for r in relax)
    lines = [
        f"# Run notes: {m['run_id']}",
        "",
        f"- Run / slate: `{m['run_id']}` / `{m['slate_id']}`",
        f"- Mode: {m['mode']}",
        f"- Entries / fees: {m['entry_count']} ({_fees(m.get('entry_fees', []))})",
        f"- First export: {chicago(first['created_utc']) if first else 'none (nothing published)'}",
        f"- Final: v{final['version']} sha256 `{final['sha256'][:16]}` "
        f"{'published to ' + m.get('public_path', '') if final and final.get('public_replaced') else '(public file not replaced)'}"
        if final else "- Final: nothing published",
        "- Status: " + " ".join(f"{k}={v}" for k, v in s.items()),
        f"- DK status from the salary file: {news.get('csv_summary', 'not read')}",
        f"- Material news changes: {news.get('phase_b_summary', 'none (offline or no network pass)')}",
        "- QA changes: none (no QA round in the baseline run)",
        f"- Fallback / relaxation: route {m.get('search_route', '?')}; "
        + (", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) if kinds else "no relaxations"),
    ]
    worked = m.get("worked") or ["No noteworthy finding"]
    failed = m.get("failed") or ["No noteworthy finding"]
    lines.append("- What worked: " + "; ".join(worked))
    lines.append("- What failed: " + "; ".join(failed))
    lines.append(f"- Keep / change: {m.get('recommendation', 'keep')}")
    if m.get("provisional"):
        lines += _provisional_lines(m["provisional"])
    if m.get("scenario"):
        lines += _scenario_lines(m["scenario"])
    if m.get("messages"):
        lines += ["", "## Messages", ""] + [f"- {x}" for x in m["messages"]]
    return "\n".join(lines) + "\n"


def _provisional_lines(p: dict[str, Any]) -> list[str]:
    """C3 section. Every figure is labeled provisional; no probability or ceiling is reported."""
    ev = p["evidence"]
    ver = f"v{p['version']}" if p.get("version") else "not published (previous version stays current)"
    out = [
        "",
        "## Provisional leverage pass (PROVISIONAL)",
        "",
        f"- {p['label']}. Version: {ver}.",
        "- Evidence: " + " ".join(f"{k}={v}" for k, v in ev.items()),
        f"- Prefit: {p.get('prefit', 'not run')}",
        "- Fields: " + "; ".join(
            f"{fam} {f['n_draws']}/{f['n_requested']} draws, {f['repeats']} repeats"
            + (" DEGRADED" if f["degraded"] else "") for fam, f in p["fields"].items()),
    ]
    if p.get("network"):
        out.append("- Contest detail problems: " + "; ".join(p["network"]))
    out += ["", "| Contest | Family (source) | PAYOUT_SOURCE | Field size (source) |", "|---|---|---|---|"]
    for c in p["contests"]:
        out.append(f"| {c['contest_id']} {c['name']} | {c['family']} ({c['family_source']}) | {c['PAYOUT_SOURCE']} "
                   f"| {c['field_size']} ({c['field_size_source']}) |")
    out += ["", "| Entry | Contest | Family | Projected mean pts, DTD-adjusted (provisional) | Lineup own % sum "
            "(provisional) | Dup proxy (provisional) | Field dup est. (provisional) | Band pts | DTD |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for e in p["entries"]:
        out.append(f"| {e['entry_id']} | {e['contest_id']} | {e['family']} | {e['mean_pts']:.1f} | {e['own_sum_pct']:.1f} "
                   f"| {e['dup_proxy']:.2f} | {e['field_dup_est']:.1f} | {e['band_pts']:.1f} | {e['dtd_players']} |")
    out += ["", "Dup proxy: sum of log ownership + salary-left term + Captain log ownership; higher means more "
            "likely duplicated. Field dup est.: sampled copies of the lineup scaled to the field size. Both are "
            "uncalibrated scenario proxies, not measured facts."]
    return out


def _pm(x, se, nd=3) -> str:
    return f"{x:.{nd}f} +/- {se:.{nd}f}"


def _scenario_lines(s: dict[str, Any]) -> list[str]:
    """C8 section. Every payout figure is a scenario estimate with its Monte Carlo standard error and
    the three evidence states; a PRIOR payout curve makes it an uncalibrated scenario proxy."""
    out = ["", "## Scenario portfolio (C8)", ""]
    if s.get("skipped") or s.get("error"):
        out.append(f"- Not published: {s.get('skipped') or 'the pass failed (see What failed)'}; the previous version stays current.")
        return out
    ev = s.get("evidence", {})
    ver = f"v{s['version']}" if s.get("version") else "not published (previous version stays current)"
    out += [
        f"- {s['label']}. Version: {ver}.",
        "- Evidence on every figure below: " + " ".join(f"{k}={v}" for k, v in ev.items())
        + (" (PRIOR payout curves: every dollar figure is an uncalibrated scenario proxy)" if ev.get("PAYOUT_SOURCE") == "PRIOR" else ""),
        "- Scenarios: " + "; ".join(f"{p} {v['n']} (seed {v['seed']}, spec {v['spec_sha256']})" for p, v in s["scenarios"].items())
        + ". Discovery used design, the choice used selection, the figures below use referee.",
        "- Games (odds source per game; MODEL is the hockey model, never a market price): "
        + "; ".join(f"{k} {v}" for k, v in s.get("game_sources", {}).items()),
    ]
    out += [f"  - {x}" for x in s.get("odds", []) + s.get("games", [])]
    part = s.get("participation", {})
    out.append(f"- Participation: {len(part.get('persons', []))} QUESTIONABLE (DTD) person(s) play in "
               f"{part.get('questionable_play_prob', 1):.0%} of scenarios, priced once here (no ranking haircut on top); "
               "their minutes are not handed to teammates.")
    fg = s.get("field_growth") or {}
    if fg:
        out.append("- Field: " + "; ".join(f"{fam} {v['draws']} draws" + (f" ({v['distinct']} distinct, grown by {v['grown_by']} in "
                                                                         f"{v['seconds']} s)" if v.get("grown_by") else "")
                                         for fam, v in fg.items())
                   + ". Ownership in this section is read off these fields.")
    out += ["", "| Contest | Family | PAYOUT_SOURCE | Field size | Fee | Paid / cash line | First prize | Field |", "|---|---|---|---:|---:|---|---:|---|"]
    for c in s.get("contests", []):
        out.append(f"| {c['contest_id']} {c['name']} | {c['family']} ({c['family_source']}) | {c['PAYOUT_SOURCE']} | {c['field_size']} "
                   f"| {c['fee']:.2f} | {c['paid_positions']} / {c['cash_line']} | {c['first_prize']:.2f} | {c['field']} |")
    out += ["", "Frontier (selection scenarios; dominated points removed; knobs that chose the same portfolio share a row):", "",
            "| Knobs (kappa) | Tail utility / fees | P(lose >= 80% of fees) | E[payout] $ | Max goalie fee share | Max game fee share "
            "| Max Captain fee share | Inside budget |", "|---|---:|---:|---:|---:|---:|---:|---|"]
    for p in s.get("frontier", []):
        ks = ", ".join(f"{k:g}" for k in (p.get("kappas") or [p["kappa"]]))
        g = "n/a" if p.get("game_share_max") is None else f"{p['game_share_max']:.2f}"
        cp = "n/a" if p.get("captain_share_max") is None else f"{p['captain_share_max']:.2f}"
        out.append(f"| {ks} | {_pm(p['tail_utility'], p['tail_utility_se'])} | {_pm(p['p_lose80'], p['p_lose80_se'])} "
                   f"| {p['exp_payout']:.2f} | {p['goalie_share_max']:.2f} | {g} | {cp} | "
                   f"{'yes' if p['feasible'] else 'no: ' + '; '.join(p['reasons'])} |")
    ch = s.get("chosen", {})
    out += ["", f"- Chosen: knob {ch.get('kappa')}: {ch.get('reason')}.",
            f"- Budget in force (config/risk.yaml, [BEN] flag 2 placeholder): " + ", ".join(
                f"{k} {v:.2f}" for k, v in s.get("budget", {}).items() if v is not None),
            "- Caps: " + "; ".join(s.get("caps", {}).get("notes", []) or ["defaults"]),
            f"- Candidate families: target {s.get('family_mix', {}).get('target')}, selected {s.get('family_mix', {}).get('selected')}; "
            f"{s.get('discovery', {}).get('candidates')} candidates ({s.get('discovery', {}).get('from_bank_and_provisional')} from the "
            f"Phase A bank and the provisional picks); chalk team {s.get('discovery', {}).get('chalk_team')}.",
            f"- PRIOR (no-history) persons selected: v1 {s['prior_persons_selected']['baseline_v1']}, provisional "
            f"{s['prior_persons_selected']['provisional']}, scenario {s['prior_persons_selected']['scenario']}."]
    if s.get("relaxations"):
        out.append("- Relaxations: " + "; ".join(f"{r['entry_id']} {r['kind']}" for r in s["relaxations"]))
    pf = s.get("portfolio", {})
    if pf:
        conc = pf.get("concentration", {})
        sf = conc.get("shared_failure", {})
        out += ["", "Portfolio (referee scenarios; R(s) = sum of payouts - fees):", "",
                f"- Fees ${pf['fees']:.2f}; E[payout] ${_pm(pf['exp_payout'], pf['exp_payout_se'], 2)}; P(zero payout) {pf['p_zero_payout']:.3f}; "
                f"P(net loss) {pf['p_net_loss']:.3f}; P(lose >= 80% of fees) {_pm(pf['p_lose80'], pf['p_lose80_se'])}; "
                f"worst-5% expected shortfall ${pf['es5']:.2f}; tail utility / fees {_pm(pf['tail_utility'], pf['tail_utility_se'])}; "
                f"tickets (not cash) ${pf.get('tickets_value', 0):.2f}.",
                "- Recovery (payout / fees) quantiles: " + ", ".join(f"{k} {v}" for k, v in pf.get("recovery_quantiles", {}).items()),
                "- Fee share by goalie: " + ", ".join(f"{k} {v:.2f}" for k, v in list(conc.get("goalie", {}).items())[:4]),
                "- Fee share by primary game: " + (", ".join(f"{k} {v:.2f}" for k, v in conc.get("game", {}).items())
                                                   if conc.get("n_games", 1) > 1 else "single-game slate (no per-game cap)"),
                ]
        if conc.get("captain"):
            out.append("- Fee share by Captain: " + ", ".join(f"{k} {v:.2f}" for k, v in list(conc["captain"].items())[:4]))
        if sf:
            out.append(f"- Shared failure ({sf.get('definition')}): fees cashing nothing {sf.get('baseline_fee_share_cashing_nothing', 0):.2f} "
                       "in all scenarios; worst teams " + ", ".join(f"{w['team']} {w['fee_share_cashing_nothing']:.2f}" for w in sf.get("worst", [])))
    out += ["", "| Entry | Contest | Family | Objective (value +/- SE) | E[payout] $ | P(cash) | P(top 1%) | First-place equity "
            "| P(clear line) | P(seat) | Own % sum | Dup (measure) | Candidate family | DTD |", "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|"]
    for e in s.get("entries", []):
        out.append(f"| {e['entry_id']} | {e['contest_id']} | {e['family']} | {e['objective']} {_pm(e['value'], e['se'], 4)} "
                   f"| {_pm(e['exp_payout'], e['exp_payout_se'], 2)} | {e['p_cash']:.3f} | {_pm(e['p_top1pct'], e['p_top1pct_se'], 4)} "
                   f"| {_pm(e['first_place_equity'], e['first_place_equity_se'], 5)} | {_pm(e['p_clear_line'], e['p_clear_line_se'])} "
                   f"| {_pm(e['p_seat'], e['p_seat_se'])} | {e['own_sum_pct']:.1f} | {e['dup']:.2f} ({e['dup_measure']}) "
                   f"| {e['candidate_family']} | {e['dtd_players']} |")
    out += ["", "OUTCOME_CALIBRATION=UNVALIDATED: the C6 calibration flags the simulated 3+ point tail as thin (backlog B6), so "
            "top-1% and first-place figures are labeled scenario estimates with their Monte Carlo error; 20,000 scenarios "
            "cannot give precise massive-field win rates. None of these figures is a measured ROI, EV or ruin probability."]
    return out


def write_run_notes(run: RunDir, manifest: dict[str, Any]) -> Path:
    path = run.path / "RUN_NOTES.md"
    atomic_write(path, render(manifest).encode("utf-8"))
    return path
