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
        *([f"- History store: {_history_line(m)}"] if m.get("history") else []),
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
    if m.get("objective"):
        lines += _objective_lines(m)
    if m.get("started_slate"):
        lines += _started_lines(m["started_slate"])
    if m.get("goalies"):
        from nhl_dfs.build.goalies import notes_lines

        lines += notes_lines(m["goalies"])
    if m.get("messages"):
        lines += ["", "## Messages", ""] + [f"- {x}" for x in m["messages"]]
    return "\n".join(lines) + "\n"


def _history_line(m: dict[str, Any]) -> str:
    """C39: the history store's state at the as-of date, from the manifest block (data/history/status.render_line)."""
    from nhl_dfs.data.history.status import render_line

    try:
        return render_line(m["history"], m.get("model"))
    except Exception as exc:  # a notes line must never stop the notes (or the run) from being written
        return f"unavailable ({type(exc).__name__}: {str(exc)[:80]})"


def _started_lines(s: dict[str, Any]) -> list[str]:
    """C15 section: the games in progress, the players left out, the open games built, the cells that stayed."""
    out = [
        "",
        "## Started slate (flag 15)",
        "",
        f"- Games in progress (teams): {', '.join(s['started_teams'])}; {s['excluded_rows']} player row(s) excluded and "
        f"never added ({s['how']}).",
        f"- Open games built: {', '.join(s['open_games']) or 'none'}.",
        f"- Pinned cells (a started-game player the entries file already held, left as it is): {s['pinned_cells']} in "
        f"{len(s['pinned_entries'])} entr{'y' if len(s['pinned_entries']) == 1 else 'ies'}.",
    ]
    if s.get("unrepaired_entries"):
        out.append("- Entries kept as they were (no legal rebuild around the pinned cells): " + ", ".join(s["unrepaired_entries"]))
    out.append("- Excluded players:")
    out += [f"  - {team}: {', '.join(names)}" for team, names in s["excluded_players"].items()]
    return out


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
        # C17: what the ownership prior was told; a manifest written before C17 has none and says so
        "- " + (p["field_inputs"]["line"] if p.get("field_inputs") else "FIELD_INPUTS not recorded (written before C17)"),
        "- Fields: " + "; ".join(
            f"{fam} {f['n_draws']}/{f['n_requested']} draws, {f['repeats']} repeats"
            + (" DEGRADED" if f["degraded"] else "") for fam, f in p["fields"].items()),
    ]
    if p.get("network"):
        out.append("- Contest detail problems: " + "; ".join(p["network"]))
    out += ["", "| Contest | Family (source) | PAYOUT_SOURCE | Field size (source) |", "|---|---|---|---|"]
    for c in p["contests"]:
        out.append(f"| {c['contest_id']} {c['name']} | {c['family']} ({c['family_source']}) | {_payout_cell(c)} "
                   f"| {_size_cell(c)} |")
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


def _payout_cell(c: dict[str, Any]) -> str:
    """PAYOUT_SOURCE cell: a TEMPLATE contest names the table it was priced on (C16), a PRIOR one the reason it has none."""
    t = c.get("payout_template")
    if t:
        return f"TEMPLATE (table of contest {t['template_contest_id']}, {t['paid_places']:,} paid)"
    if c["PAYOUT_SOURCE"] == "PRIOR" and c.get("payout_note"):
        return f"PRIOR ({c['payout_note']})"
    return c["PAYOUT_SOURCE"]


def _size_cell(c: dict[str, Any]) -> str:
    """Field size with its FIELD_SIZE_SOURCE (C38: EXACT, LOBBY or PRIOR). A manifest written before C38 has only the
    older field_size_source value, which is shown as it was; the scenario table of such a manifest shows the size alone."""
    label = c.get("FIELD_SIZE_SOURCE") or c.get("field_size_source")
    return f"{c['field_size']} ({label})" if label else f"{c['field_size']}"


def _prior_note(ev: dict[str, Any], contests: list[dict[str, Any]]) -> str:
    """Overall PRIOR is the weakest contest: say which contests are PRIOR when others were priced on a table."""
    if ev.get("PAYOUT_SOURCE") != "PRIOR":
        return ""
    prior = [c["contest_id"] for c in contests if c.get("PAYOUT_SOURCE") == "PRIOR"]
    if prior and len(prior) < len(contests):
        return (f" (PRIOR payout curves for contest(s) {', '.join(prior)}: their dollar figures are an uncalibrated "
                "scenario proxy; the other contests were priced on a DraftKings table, see PAYOUT_SOURCE per contest)")
    return " (PRIOR payout curves: every dollar figure is an uncalibrated scenario proxy)"


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
        + _prior_note(ev, s.get("contests", [])),
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
        out.append(f"| {c['contest_id']} {c['name']} | {c['family']} ({c['family_source']}) | {_payout_cell(c)} | {_size_cell(c)} "
                   f"| {c['fee']:.2f} | {c['paid_positions']} / {c['cash_line']} | {c['first_prize']:.2f} | {c['field']} |")
    out += ["", "Frontier (selection scenarios; dominated points removed; knobs that chose the same portfolio share a row):", "",
            "| Knobs (kappa) | Tail utility / fees | P(lose >= 80% of fees) | E[payout] $ | Max goalie fee share | Max game fee share "
            "| Max Captain fee share | Most lineups on one goalie / game | Inside budget |", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for p in s.get("frontier", []):
        ks = ", ".join(f"{k:g}" for k in (p.get("kappas") or [p["kappa"]]))
        g = "n/a" if p.get("game_share_max") is None else f"{p['game_share_max']:.2f}"
        cp = "n/a" if p.get("captain_share_max") is None else f"{p['captain_share_max']:.2f}"
        out.append(f"| {ks} | {_pm(p['tail_utility'], p['tail_utility_se'])} | {_pm(p['p_lose80'], p['p_lose80_se'])} "
                   f"| {p['exp_payout']:.2f} | {p['goalie_share_max']:.2f} | {g} | {cp} | {p.get('goalie_lineups_max', '?')} / "
                   f"{'n/a' if p.get('game_lineups_max') is None else p['game_lineups_max']} | "
                   f"{'yes' if p['feasible'] else 'no: ' + '; '.join(p['reasons'])} |")
    ch = s.get("chosen", {})
    out += ["", f"- Chosen: knob {ch.get('kappa')}: {ch.get('reason')}.",
            f"- Budget in force (config/risk.yaml, [BEN] flag 2 placeholder): " + ", ".join(
                f"{k} {v}" if isinstance(v, int) else f"{k} {v:.2f}" for k, v in s.get("budget", {}).items() if v is not None),
            "- Caps: " + "; ".join(s.get("caps", {}).get("notes", []) or ["defaults"]),
            f"- Candidate families: target {s.get('family_mix', {}).get('target')}, selected {s.get('family_mix', {}).get('selected')}; "
            f"{s.get('discovery', {}).get('candidates')} candidates ({s.get('discovery', {}).get('from_bank_and_provisional')} from the "
            f"Phase A bank and the provisional picks), {s.get('discovery', {}).get('screened', {}).get('kept', '?')} kept by the "
            f"per-contest screen; chalk team {s.get('discovery', {}).get('chalk_team')}.",
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


def _objective_lines(m: dict[str, Any]) -> list[str]:
    """C9 section: which objective valued the open cells, every step down with its reason, the live
    standings status, the role state, and per repaired entry its family figure with the Monte Carlo SE."""
    o = m["objective"]
    live = m.get("live", {})
    news = m.get("news", {})
    out = ["", "## Objective (C9)", "",
           f"- OBJECTIVE={o['kind']} (requested {o.get('requested', 'auto')}; order scenario, provisional, baseline)"]
    for f in o.get("fallbacks", []):
        out.append(f"- {f['from']} not used: {f['reason']}")
    out.append(f"- LIVE_STATUS={live.get('LIVE_STATUS', 'NO_SNAPSHOT')}: {live.get('reason', 'no standings snapshot')}")
    if news.get("roles_news_state"):
        out.append(f"- ROLES_NEWS_STATE={news['roles_news_state']} (Daily Faceoff lines and goalies; NEWS_STATE above is DK status coverage)")
    for w in news.get("roles_warnings", [])[:10]:
        out.append(f"- roles warning: {w}")
    for n in o.get("notes", [])[:12]:
        out.append(f"- {n}")
    sc = o.get("scenario")
    if sc:
        out.append(f"- Scenarios: cache from run {sc['cache_run']}, selection {sc['n']['selection']} / referee {sc['n']['referee']}; "
                   f"re-simulated games: {', '.join(sc['games_resimulated']) or 'none'}"
                   + (f"; changed but started (cached draws kept): {', '.join(sc['games_changed_but_started'])}"
                      if sc.get("games_changed_but_started") else ""))
    if o.get("game_sources"):
        out.append("- Game sources: " + ", ".join(f"{k} {v}" for k, v in sorted(o["game_sources"].items()))
                   + ("; MODEL means team-strength intensities, not a market price (backlog B5)"
                      if any(v.startswith("MODEL") for v in o["game_sources"].values()) else ""))
    ev = o.get("evidence")
    if ev:
        out.append("- Evidence: " + " ".join(f"{k}={v}" for k, v in ev.items())
                   + "; every figure below is an uncalibrated scenario proxy with its Monte Carlo SE")
    for eid, e in sorted((o.get("entries") or {}).items()):
        sel, ref = e.get("selection") or {}, e.get("referee") or {}
        if "value" not in sel:
            out.append(f"- entry {eid}: {sel.get('note', 'surrogate best')}")
            continue
        line = (f"- entry {eid} ({sel['objective']}, policy {sel['policy']}, {sel['candidates']} candidates): "
                f"choosing draws {_pm(sel['value'], sel['se'], 4)} (optimistic: measured where it was chosen)")
        if ref:
            line += f"; referee draws {_pm(ref['value'], ref['se'], 4)}, E[payout] {_pm(ref['exp_payout'], ref['exp_payout_se'], 2)}"
        out.append(line)
    return out


def write_run_notes(run: RunDir, manifest: dict[str, Any]) -> Path:
    """RUN_NOTES.md from the manifest; a settled run's Settlement section (settle/settlement.md, C11) stays last."""
    path = run.path / "RUN_NOTES.md"
    text = render(manifest)
    settled = run.path / "settle" / "settlement.md"
    if settled.exists():
        text = text.rstrip("\n") + "\n" + settled.read_text(encoding="utf-8")
    atomic_write(path, text.encode("utf-8"))
    return path
