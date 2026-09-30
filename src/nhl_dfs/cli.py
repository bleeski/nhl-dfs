"""Command-line entry point: `python -m nhl_dfs.cli <command>`.

`status`, `verify`, `probe`, `run --baseline` (C2b), `late-swap`, and `refresh` (C2c) are real.
`run` without --baseline adds the provisional leverage pass, and `field --run` reports the
sampled opponent field (C3); since C8 it then adds the scenario version (objective-aware portfolio). `history --backfill` and `identity --seed | --accept` are C4.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NEXT_CHUNK = REPO_ROOT / "tools" / "next_chunk.py"
# C10: NHL_DFS_RUNS_ROOT / NHL_DFS_OUTPUTS_ROOT redirect the defaults (headless rehearsals and exit checks run
# into scratch folders and can never replace the public file Ben uploads). Explicit --runs-root/--outputs-root win.
RUNS_ROOT = Path(os.environ.get("NHL_DFS_RUNS_ROOT") or REPO_ROOT / "runs")
OUTPUTS_ROOT = REPO_ROOT / "outputs"


def _outputs_default(runs_root: Path) -> Path:
    env = os.environ.get("NHL_DFS_OUTPUTS_ROOT")
    return Path(env) if env else runs_root.parent / "outputs"


def last_run_lines(runs_root: Path) -> list[str]:
    """One short block describing the newest run that has a manifest."""
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.notes import chicago
    from nhl_dfs.build.state import latest_run

    run = latest_run(runs_root)
    if run is None:
        return ["last run: none"]
    m = read_manifest(run)
    final = m["versions"][-1] if m.get("versions") else None
    lines = [
        f"last run: {m['run_id']} ({m['mode']}, {m['entry_count']} entries, created {chicago(m['created_utc'])})",
        "  " + " ".join(f"{k}={v}" for k, v in m["statuses"].items()),
    ]
    if final:
        where = m.get("public_path") if final.get("public_replaced") else "not published (see RUN_NOTES.md)"
        lines.append(f"  v{final['version']} sha256 {final['sha256'][:16]} -> {where}")
    lines.append(f"  notes: {run.path / 'RUN_NOTES.md'}")
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    result = subprocess.run([sys.executable, str(NEXT_CHUNK), "--status"], cwd=str(REPO_ROOT))
    for line in last_run_lines(Path(args.runs_root)):
        print(line)
    return result.returncode


MAX_REASONS_PRINTED = 20


def verify_run(runs_root: Path, run_id: str) -> tuple[bool, list[str]]:
    """Re-check a run's current version against its input copies and its manifest hashes."""
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.state import open_run, sha256
    from nhl_dfs.referee.check_file import check_file

    run = open_run(runs_root, run_id)
    n = run.current_version()
    if n is None:
        return False, [f"run {run_id} has no published version"]
    out = run.version_file(n)
    report = check_file(out, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    m = read_manifest(run)
    why = list(report.reasons)
    rec = next((v for v in m["versions"] if v["version"] == n), None)
    if rec is None:
        why.append(f"manifest has no record of v{n}")
    elif rec["sha256"] != report.out_sha256:
        why.append(f"v{n} bytes do not match the manifest hash")
    if m["salary_sha256"] != report.salary_sha256 or m["entries_sha256"] != report.entries_sha256:
        why.append("input copies do not match the manifest hashes")
    lines = [
        f"FILE_VALID={'TRUE' if not why else 'FALSE'}",
        f"run={run_id} version=v{n} mode={report.mode} entries={report.entries_checked}",
        f"out_sha256={report.out_sha256}",
        f"salary_sha256={report.salary_sha256}",
        f"entries_sha256={report.entries_sha256}",
    ]
    lines += [f"note: {x}" for x in report.notes]
    lines += [f"reason: {x}" for x in why[:MAX_REASONS_PRINTED]]
    return not why, lines


def cmd_verify(args: argparse.Namespace) -> int:
    from nhl_dfs.referee.check_file import check_file

    if args.run:
        try:
            ok, lines = verify_run(Path(args.runs_root), args.run)
        except FileNotFoundError as exc:
            print("FILE_VALID=FALSE")
            print(f"reason: {exc}")
            return 1
        for line in lines:
            print(line)
        return 0 if ok else 1
    if not (args.salary and args.entries and args.out):
        print("verify needs --run <id>, or --salary, --entries, and --out")
        return 2
    report = check_file(args.out, args.salary, args.entries, parent_path=args.parent)
    print(f"FILE_VALID={'TRUE' if report.ok else 'FALSE'}")
    print(f"mode={report.mode} entries={report.entries_checked}")
    print(f"out_sha256={report.out_sha256}")
    print(f"salary_sha256={report.salary_sha256}")
    print(f"entries_sha256={report.entries_sha256}")
    if report.parent_sha256:
        print(f"parent_sha256={report.parent_sha256}")
    for note in report.notes:
        print(f"note: {note}")
    for reason in report.reasons[:MAX_REASONS_PRINTED]:
        print(f"reason: {reason}")
    if len(report.reasons) > MAX_REASONS_PRINTED:
        print(f"reason: ... {len(report.reasons) - MAX_REASONS_PRINTED} more")
    return 0 if report.ok else 1


def probe_sources(cache) -> list[str]:
    """Hit each live source once; one status line per source. Failures are lines, not errors."""
    from datetime import date, timedelta

    from nhl_dfs.data.sources import covers, dk_public, nhl

    lines: list[str] = []

    def attempt(name, fn, describe):
        try:
            result = fn()
        except Exception as exc:  # every failure is reported, none stops the probe
            lines.append(f"{name:<18} FAIL {type(exc).__name__}: {str(exc)[:110]}")
            return None
        lines.append(f"{name:<18} OK   {describe(result)}")
        return result

    today = date.today()
    contests = attempt("dk_lobby", lambda: dk_public.lobby(cache=cache), lambda r: f"{len(r)} contests")
    if contests:
        first = max(contests, key=lambda c: c.field_size)
        attempt("dk_contest", lambda: dk_public.contest_detail(first.id, cache=cache),
                lambda r: f"{r.contest_id} {len(r.payout_table)} payout tiers, {r.entries}/{r.maximum_entries} entries")
        attempt("dk_draftables", lambda: dk_public.draftables(first.draft_group_id, cache=cache),
                lambda r: f"group {r.draft_group_id}: {len(r.rows)} rows")
    else:
        lines.append(f"{'dk_contest':<18} SKIP no lobby contest id")
        lines.append(f"{'dk_draftables':<18} SKIP no lobby draft group id")
    games = attempt("nhl_schedule", lambda: nhl.schedule(today, cache=cache), lambda r: f"{len(r)} games {today}")
    team = games[0].home if games else "TOR"
    players = attempt("nhl_roster", lambda: nhl.roster(team, cache=cache), lambda r: f"{team}: {len(r)} players")
    week_start = today - timedelta(days=7)  # the endpoint returns a 7-day window from the date
    past = attempt("nhl_schedule_prev", lambda: nhl.week_schedule(week_start, cache=cache),
                   lambda r: f"{len(r)} games in the week from {week_start}")
    done = sorted((g for g in (past or []) if g.game_state in ("OFF", "FINAL")), key=lambda g: g.start_utc)
    if done:
        attempt("nhl_boxscore", lambda: nhl.boxscore(done[-1].game_id, cache=cache),
                lambda r: f"{r.game_id}: {len(r.skaters)} skaters, {len(r.goalies)} goalies")
    else:
        lines.append(f"{'nhl_boxscore':<18} SKIP no completed game in the past week")
    skater = next((p for p in (players or []) if p.position != "G"), None)
    if skater:
        season = today.year if today.month >= 9 else today.year - 1
        attempt("nhl_gamelog", lambda: nhl.game_log(skater.nhl_id, (season - 1) * 10000 + season, 2, cache=cache),
                lambda r: f"{skater.nhl_id}: {len(r)} games last season")
    else:
        lines.append(f"{'nhl_gamelog':<18} SKIP no roster skater")
    attempt("nhl_report", lambda: nhl.skater_report("timeonice", today - timedelta(days=1), today - timedelta(days=1), cache=cache),
            lambda r: f"timeonice {len(r)} rows")
    attempt("nhl_partner_odds", lambda: nhl.partner_odds(cache=cache),
            lambda r: f"{r.book} {len(r.games)} games, lastUpdatedUTC {r.as_of_utc.isoformat()}")
    attempt("covers", lambda: covers.odds(cache=cache), lambda r: f"{len(r.games)} games, book {r.book}")
    attempt("dailyfaceoff", lambda: cache.get_text(
        "https://www.dailyfaceoff.com/starting-goalies", source="dailyfaceoff", ttl_s=0,
        schema=lambda p: None).data.count("<script"), lambda n: f"starting-goalies page, {n} script tags")
    return lines


def cmd_probe(_args: argparse.Namespace) -> int:
    from nhl_dfs.data.http import HttpCache

    cache = HttpCache(force_refresh=True)
    for line in probe_sources(cache):
        print(line)
    print(f"calls made: {cache.calls_made}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from nhl_dfs.build.run import run_slate

    if not (args.salary and args.entries):
        print("run needs --salary <DKSalaries.csv> and --entries <DKEntries.csv>")
        return 2
    runs_root = Path(args.runs_root)
    outputs_root = Path(args.outputs_root) if args.outputs_root else _outputs_default(runs_root)
    result = run_slate(args.salary, args.entries, offline=args.offline, baseline_only=args.baseline,
                       out_root=runs_root, outputs_root=outputs_root, scenario=not args.baseline)
    return _print_result(result)


def _print_result(result) -> int:
    for k, v in result.statuses.items():
        print(f"{k}={v}")
    print(f"run={result.run.run_id} slate={result.slate_id}")
    if result.public_path:
        print(f"published: {result.public_path}")
    else:
        print("published: nothing (see RUN_NOTES.md)")
    for msg in result.messages:
        print(f"note: {msg}")
    print(f"notes: {result.notes_path}")
    return 0 if result.ok else 1


def _as_of(text: str | None):
    """--as-of 2026-10-15T23:10:00Z: a labeled rehearsal clock (UTC)."""
    if not text:
        return None
    from datetime import datetime, timezone

    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SystemExit("--as-of needs a UTC offset, for example 2026-10-15T23:10:00Z")
    return t.astimezone(timezone.utc)


def _roots(args) -> tuple[Path, Path]:
    runs_root = Path(args.runs_root)
    return runs_root, Path(args.outputs_root) if args.outputs_root else _outputs_default(runs_root)


def cmd_simulate(args: argparse.Namespace) -> int:
    import json
    import statistics
    import time

    from nhl_dfs.build.run import load_run_pool, slate_as_of
    from nhl_dfs.build.state import latest_run, open_run
    from nhl_dfs.contracts.statuses import Participation
    from nhl_dfs.models import params as params_mod
    from nhl_dfs.sim import cache, game
    from nhl_dfs.sim.slate import build_slate, fetch_odds

    runs_root = Path(args.runs_root)
    try:
        run = open_run(runs_root, args.run) if args.run else latest_run(runs_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"no such run: {exc}")
        return 1
    if run is None:
        print("simulate needs --run <id> (no runs found)")
        return 2
    pool, _entries, work, st = load_run_pool(run)
    as_of = slate_as_of(pool, lambda: run_clock(run.run_id))
    table = params_mod.projection_for(work, as_of)
    snapshot, msgs = (None, ["odds: skipped (--offline); every game takes the model intensities"]) if args.offline else fetch_odds()
    slate, game_lines = build_slate(work, table, snapshot)
    out = run.path / "sim"
    seed = int(args.seed) if args.seed is not None else int(pool.sha256[:8], 16)
    n = int(args.n)
    print(f"simulate: run {run.run_id} ({pool.mode.value}); {len(table.persons)} persons; MODEL_STATUS={table.source().value}; "
          f"params as of {as_of}; {n} scenarios, seed {seed}, purpose {args.purpose}")
    for line in msgs + game_lines:
        print(line)
    if snapshot is not None:
        (out).mkdir(parents=True, exist_ok=True)
        (out / "odds.json").write_text(json.dumps(
            {"source": snapshot.source, "book": snapshot.book, "as_of_utc": snapshot.as_of_utc.isoformat(),
             "as_of_basis": snapshot.as_of_basis, "games": [g.__dict__ for g in snapshot.games]}, default=str), encoding="utf-8")
    t0 = time.perf_counter()
    info = cache.build(out, slate, table, n, seed, args.purpose)
    wall = time.perf_counter() - t0
    sm = info.summary
    keys = info.person_keys
    print(f"wall clock: {wall:.1f} s for {n} scenarios in {info.chunks} chunk(s) of {info.chunk_size} "
          f"({'within' if wall <= 90 else 'OVER'} the 90 s target); wrote {out} ({info.bytes / 1e6:.1f} MB)")
    any_market = any(g.rates.source == "MARKET" for g in slate.games)
    clause = ("market-fit team goals rescale the analytic means; recorded, not tuned" if any_market
              else "every game is MODEL, so the difference is allocation and dressing only; recorded, not tuned")
    for grp in ("F", "D", "G"):
        ratios = [sm["person_mean"]["points"][i] / 10.0 / (table.persons[k].mean_tenths / 10.0)
                  for i, k in enumerate(keys) if table.persons[k].group == grp and table.persons[k].mean_tenths >= 20
                  and table.persons[k].source.value != "PRIOR"]
        if ratios:
            print(f"scale {grp}: simulated mean / ParamTable mean, median {statistics.median(ratios):.2f} "
                  f"over {len(ratios)} persons with history ({clause})")
    for i, t in enumerate(sm["team"]["keys"]):
        print(f"team {t}: goals {sm['team']['goals'][i]:.2f}, SOG {sm['team']['sog'][i]:.1f}, empty-net {sm['team']['en'][i]:.2f}")
    dtd = [r for r, (p, _) in st.items() if p is Participation.QUESTIONABLE]
    print(f"note: {len(dtd)} QUESTIONABLE (DTD) rows keep their normal play probability here; C8 prices the participation risk")
    print(f"sim spec sha256 {info.spec_sha256[:16]}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from nhl_dfs.sim import validate

    return validate.run_cli(args)


def cmd_roles(args: argparse.Namespace) -> int:
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo

    from nhl_dfs.build import news
    from nhl_dfs.build.run import salary_statuses, slate_as_of
    from nhl_dfs.contracts.statuses import Participation
    from nhl_dfs.data.http import HttpCache, SourceSchemaError, SourceUnavailable
    from nhl_dfs.data.sources import dailyfaceoff as df
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.models import params as params_mod
    from nhl_dfs.models import roles as roles_mod

    if not args.salary:
        print("roles needs --salary <DKSalaries.csv> [--offline]")
        return 2
    pool = read_salary(args.salary)
    now = _as_of(args.as_of) or datetime.now(timezone.utc)
    et = ZoneInfo("America/New_York")
    days = sorted({g.start_utc.astimezone(et).date() for g in pool.games.values()})
    cache = HttpCache(offline=bool(args.offline))
    codes = df.team_codes()
    slate = {c["nhl"]: (slug, c) for slug, c in codes.items() if c["dk"] and c["dk"] in pool.teams}
    print(f"roles: {pool.mode.value} slate {', '.join(str(d) for d in days)}, teams {', '.join(sorted(pool.teams))}; now {now:%Y-%m-%d %H:%MZ}; "
          f"{'OFFLINE: the latest stored Daily Faceoff pages' if args.offline else 'online, cache first'}")
    lines, problems = {}, []
    for nhl, (slug, c) in sorted(slate.items()):
        try:
            lines[nhl] = df.team_lines(slug, cache=cache)
        except (SourceSchemaError, SourceUnavailable) as exc:
            problems.append(f"{c['dk']}: team page unavailable ({type(exc).__name__}: {str(exc)[:90]})")
    reports, path = [], "none"
    for d in days:
        got = df.fetch_goalies(d, cache=cache, teams=sorted(slate))
        reports += got.reports
        path = got.path if path == "none" else path
        problems += [f"goalies {d}: {n}" for n in got.notes]
    try:
        table = params_mod.projection_for(pool, slate_as_of(pool, lambda: now))
        rotation = roles_mod.rotation_from(table)
    except Exception as exc:  # no history: rotation unknown, goalies fall to an even split
        rotation = {}
        problems.append(f"rotation unavailable ({type(exc).__name__}); goalie mixtures use an even split")
    rs = roles_mod.merge(None, lines, reports, rotation, now, pool=pool, csv_status=salary_statuses(pool), goalie_path=path)
    print(f"goalie data path in use: {path} (docs/sources.md); team pages read {len(lines)} of {len(slate)}")
    by_dk = {c["dk"]: (slug, nhl) for nhl, (slug, c) in slate.items()}
    for team in sorted(pool.teams):
        g = next((x for x in pool.games.values() if team in (x.home, x.away)), None)
        opp = (g.away if g and g.home == team else g.home) if g else "?"
        page = rs.team_pages.get(team)
        head = f"{team} {'vs' if g and g.home == team else '@'} {opp}"
        if page is None:
            print(f"{head}: no Daily Faceoff page" + ("" if team in by_dk else " (DK team code not verified in config/teams.yaml)"))
        else:
            tl = lines[by_dk[team][1]]
            flag = "USED" if page["usable"] else "NOT USED (older than the age limit)"
            print(f"{head}: lines updated {page['updated_utc']:%m-%d %H:%MZ} by {page['source'] or 'unattributed'}, {page['age_h']:.1f} h old, {flag}"
                  + (", LOW CONFIDENCE" if page["low_confidence"] else ""))
            if page["usable"]:
                print("  F: " + " | ".join(f"L{i}: " + ", ".join(p.name for p in grp) for i, grp in enumerate(tl.f_lines, 1) if grp))
                print("  D: " + " | ".join(f"P{i}: " + ", ".join(p.name for p in grp) for i, grp in enumerate(tl.d_pairs, 1) if grp))
                print("  PP1: " + ", ".join(p.name for p in tl.pp1) + " | PP2: " + ", ".join(p.name for p in tl.pp2))
                if tl.injuries:
                    print("  tags: " + ", ".join(f"{n} ({s})" for n, s in tl.injuries))
        gr = rs.goalies.get(team)
        if gr:
            mix = ", ".join(f"{k.split('|')[0].title()} {v:.2f}" for k, v in sorted(gr.p_start.items(), key=lambda kv: -kv[1]) if v > 0.005)
            extra = ""
            if gr.report is not None and gr.report.source_name:
                extra = f" (source {gr.report.source_name}, {gr.report.news_created_utc:%m-%d %H:%MZ})" if gr.report.news_created_utc else ""
            print(f"  goalie {gr.state.value}{extra}: {mix}")
    people = list(rs.persons.values())
    print(f"participation: OUT {sum(p.participation is Participation.OUT for p in people)}, "
          f"QUESTIONABLE {sum(p.participation is Participation.QUESTIONABLE for p in people)}, "
          f"conflicts {sum(p.conflict for p in people)}, UNKNOWN {sum(p.participation is Participation.UNKNOWN for p in people)}")
    dtd = [p for p in people if p.participation is Participation.QUESTIONABLE]

    def shown(key: str) -> str:  # the DK row's own spelling, not the normalized key
        r = pool.persons[key]
        return (r.classic or r.flex or r.cpt).name

    if dtd:
        print("monitor (QUESTIONABLE until news confirms playing or out): "
              + ", ".join(f"{shown(p.person_key)} ({p.team}, DK {p.dk_status or 'None'}"
                          + (f", DF {p.df_status}" if p.df_status else "") + f", play {p.p_play:.2f})" for p in dtd))
    for w in rs.warnings:
        print(f"warning: {w}")
    for r in rs.reports + problems:
        print(f"note: {r}")
    print(f"NEWS_STATE={news.state(rs, pool).value}")
    return 0


def cmd_late_swap(args: argparse.Namespace) -> int:
    from nhl_dfs.build import late_swap

    if not (args.run and args.entries):
        print("late-swap needs --run <id> and --entries <current DKEntries.csv downloaded from DK>")
        return 2
    runs_root, outputs_root = _roots(args)
    print(f"mode: {'fast repair (only entries that need it)' if args.fast else 'full re-optimize of open cells'}")
    result = late_swap.run(args.run, args.entries, offline=args.offline, fast=args.fast, runs_root=runs_root,
                           outputs_root=outputs_root, salary_path=args.salary, as_of=_as_of(args.as_of),
                           objective=args.objective)
    _print_objective(result)
    return _print_result(result)


def _print_objective(result) -> None:
    """C9: which objective valued the open cells (each step down with its reason) and the live status."""
    o = result.manifest.get("objective") or {}
    print(f"OBJECTIVE={o.get('kind', 'unknown')} (requested {o.get('requested', 'auto')})")
    for f in o.get("fallbacks", []):
        print(f"  {f['from']} not used: {f['reason'][:200]}")
    print(f"LIVE_STATUS={(result.manifest.get('live') or {}).get('LIVE_STATUS', 'NO_SNAPSHOT')}")


def cmd_refresh(args: argparse.Namespace) -> int:
    from nhl_dfs.build import refresh

    if not args.run:
        print("refresh needs --run <id>")
        return 2
    runs_root, outputs_root = _roots(args)
    try:
        result = refresh.run(args.run, offline=args.offline, runs_root=runs_root, outputs_root=outputs_root,
                             salary_path=args.salary, as_of=_as_of(args.as_of), objective=args.objective)
    except refresh.NoDeliveredVersion as exc:
        print("FILE_VALID=FALSE")
        print(f"reason: {exc}")
        return 1
    _print_objective(result)
    return _print_result(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nhl_dfs")
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    status_parser.set_defaults(func=cmd_status)
    sub.add_parser("probe").set_defaults(func=cmd_probe)

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--salary", type=str, default=None)
    verify_parser.add_argument("--entries", type=str, default=None)
    verify_parser.add_argument("--out", type=str, default=None)
    verify_parser.add_argument("--parent", type=str, default=None)
    verify_parser.add_argument("--run", type=str, default=None)
    verify_parser.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    verify_parser.set_defaults(func=cmd_verify)

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--salary", type=str, default=None)
    run_parser.add_argument("--entries", type=str, default=None)
    run_parser.add_argument("--baseline", action="store_true")
    run_parser.add_argument("--offline", action="store_true")
    run_parser.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    run_parser.add_argument("--outputs-root", type=str, default=None)
    run_parser.set_defaults(func=cmd_run)

    hist = sub.add_parser("history")
    hist.add_argument("--backfill", type=int, default=None, help="number of completed seasons")
    hist.add_argument("--since", type=str, default=None, help="incremental: refetch from this date (YYYY-MM-DD)")
    hist.add_argument("--no-moneypuck", action="store_true", help="Tier B only (NHL reports)")
    hist.add_argument("--store-root", type=str, default=None)
    hist.add_argument("--raw-root", type=str, default=None)
    hist.set_defaults(func=cmd_history)

    prm = sub.add_parser("params")
    prm.add_argument("--salary", type=str, default=None)
    prm.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD; default: earlier of today and the first slate game (ET)")
    prm.add_argument("--store-root", type=str, default=None)
    prm.add_argument("--out", type=str, default=None)
    prm.set_defaults(func=cmd_params)

    ident = sub.add_parser("identity")
    ident.add_argument("--seed", action="store_true")
    ident.add_argument("--salary", type=str, default=None)
    ident.add_argument("--accept", type=str, default=None, metavar="PROPOSAL_ID")
    ident.add_argument("--nhl-id", type=int, default=None)
    ident.add_argument("--offline", action="store_true", help="use stored history only (no roster calls)")
    ident.add_argument("--store-root", type=str, default=None)
    ident.add_argument("--accepted-path", type=str, default=None)
    ident.add_argument("--proposals-path", type=str, default=None)
    ident.set_defaults(func=cmd_identity)

    field_parser = sub.add_parser("field")
    field_parser.add_argument("--run", type=str, default=None)
    field_parser.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    field_parser.set_defaults(func=cmd_field)

    sim = sub.add_parser("simulate")
    sim.add_argument("--run", type=str, default=None)
    sim.add_argument("--n", type=int, default=20000)
    sim.add_argument("--seed", type=int, default=None)
    sim.add_argument("--purpose", choices=["design", "selection", "referee"], default="selection")
    sim.add_argument("--offline", action="store_true", help="no odds fetch: every game takes the model intensities")
    sim.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    sim.set_defaults(func=cmd_simulate)

    rl = sub.add_parser("roles")
    rl.add_argument("--salary", type=str, default=None)
    rl.add_argument("--offline", action="store_true", help="read the latest stored Daily Faceoff pages; no network")
    rl.add_argument("--as-of", type=str, default=None, help="UTC time to apply the age policy at, e.g. 2026-09-29T14:30:00Z")
    rl.set_defaults(func=cmd_roles)

    cal = sub.add_parser("calibrate")
    cal.add_argument("--seasons", type=int, default=1)
    cal.add_argument("--dates", type=int, default=None, help="held-out dates to sample per season (default: config)")
    cal.add_argument("--scenarios", type=int, default=None)
    cal.add_argument("--store-root", type=str, default=None)
    cal.add_argument("--out-dir", type=str, default=None)
    cal.set_defaults(func=cmd_calibrate)

    for name, func in (("late-swap", cmd_late_swap), ("refresh", cmd_refresh)):
        sp = sub.add_parser(name)
        sp.add_argument("--run", type=str, default=None)
        if name == "late-swap":
            sp.add_argument("--entries", type=str, default=None)
            sp.add_argument("--fast", action="store_true")
        sp.add_argument("--offline", action="store_true")
        sp.add_argument("--salary", type=str, default=None, help="a fresh DKSalaries.csv of the same slate (status update)")
        sp.add_argument("--as-of", type=str, default=None, help="REHEARSAL clock in UTC, e.g. 2026-10-15T23:10:00Z")
        sp.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
        sp.add_argument("--outputs-root", type=str, default=None)
        sp.add_argument("--objective", choices=("auto", "scenario", "provisional", "baseline"), default="auto",
                        help="auto: scenario, then provisional, then baseline (each fallback reported)")
        sp.set_defaults(func=func)

    return parser


def run_clock(run_id: str):
    """The UTC time a run id encodes (YYYYMMDD-HHMMSS-mode)."""
    from datetime import datetime, timezone

    return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def field_lines(summary: dict) -> list[str]:
    out = [summary.get("label", "PROVISIONAL")]
    for fam, f in summary["families"].items():
        mass = ", ".join(f"{k} {v:.0f}%" for k, v in f["mass"].items())
        out.append(f"[{fam}] contests {', '.join(f['contests'])}: {f['n_draws']} of {f['n_requested']} draws"
                   f"{' (DEGRADED)' if f['degraded'] else ''}, {f['distinct_lineups']} distinct, "
                   f"{f['repeats']} repeats; mass {mass}")
        out.append("  top ownership (provisional): " + "; ".join(
            f"{r['name']} {r['team']} {r['slot']} {r['own_pct']:.1f}%" for r in f["top_ownership"][:10]))
        if f["cpt_share_top"]:
            out.append("  captain share (provisional): " + "; ".join(
                f"{r['name']} {r['cpt_pct']:.1f}%" for r in f["cpt_share_top"][:5]))
        out.append("  stacks: " + (", ".join(f"{t} {v:.0f}%" for t, v in f["stack_freq"].items()) or "none"))
        out.append("  salary left: " + ", ".join(f"{b} {v:.0f}%" for b, v in f["salary_left_hist"].items()))
    return out


def cmd_field(args: argparse.Namespace) -> int:
    import json

    from nhl_dfs.build.state import open_run

    if not args.run:
        print("field needs --run <id>")
        return 2
    try:
        run = open_run(Path(args.runs_root), args.run)
    except (FileNotFoundError, ValueError) as exc:
        print(f"no such run: {exc}")
        return 1
    path = run.path / "field.json"
    if path.exists():
        summary = json.loads(path.read_text(encoding="utf-8"))
        source = "saved by the run's provisional pass"
    else:  # a baseline-only run: sample now from the run's own inputs, offline, family priors
        from nhl_dfs.build import provisional
        from nhl_dfs.build.run import load_run_pool
        from nhl_dfs.contracts.statuses import Participation
        from nhl_dfs.models import contests
        from nhl_dfs.models.projection import PriorProjection

        pool, entries, work, st = load_run_pool(run)
        ctx = contests.resolve(entries)
        statuses = {r: p for r, (p, _) in st.items() if r in work.by_role_id}
        seed = int(pool.sha256[:8], 16)
        from nhl_dfs.build.run import slate_as_of
        from nhl_dfs.models import params as params_mod

        try:
            proj = params_mod.projection_for(work, slate_as_of(pool, lambda: run_clock(run.run_id)))
        except Exception:  # history unavailable: the priors still give a field
            proj = PriorProjection(work)
        fb = provisional.build_fields(work, proj, ctx, seed=seed, statuses=statuses)
        summary = provisional.field_summary(work, fb, ctx, model_status=proj.source().value)
        source = "sampled now from the run's inputs (offline, family priors)"
    print(f"field for run {args.run} ({source})")
    for line in field_lines(summary):
        print(line)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    import time
    from datetime import date, datetime, timezone

    from nhl_dfs.data.history import history_seasons, nhl_reports, regular_season_complete, store
    from nhl_dfs.data.http import load_sources_config

    if not args.backfill:
        print("history needs --backfill <number of completed seasons>")
        return 2
    cfg = load_sources_config()
    today = datetime.now(timezone.utc).date()
    seasons = history_seasons(today, int(args.backfill))
    since = date.fromisoformat(args.since) if args.since else None
    mp_on = cfg["moneypuck"]["enabled"] and not args.no_moneypuck
    print(f"history backfill: seasons {', '.join(map(str, seasons))}" + (f" since {since}" if since else ""))
    print("run this outside any slate clock (it is not part of a slate run)")
    if mp_on:
        print(f"credit: {cfg['moneypuck']['attribution']}")
    stats = nhl_reports.backfill(seasons, since=since, cfg=cfg, today=today, moneypuck=mp_on,
                                 store_root=args.store_root, raw_root=args.raw_root)
    for k, v in sorted(stats.rows.items()):
        print(f"rows {k}: {v}")
    for season, t in sorted(stats.tiers.items()):
        print(f"tiers {season}: A={t.get('A', 0)} B={t.get('B', 0)}")
    for k, v in sorted(stats.moneypuck.items()):
        print(f"moneypuck {k}: {v}")
    checked = sum(c["checked"] for c in stats.crosscheck.values())
    bad = sum(c["mismatch"] for c in stats.crosscheck.values())
    print(f"box score cross-check: {len(stats.crosscheck)} games, {checked} players checked, {bad} mismatched fields")
    print(f"requests {stats.calls}, windows {stats.windows}, splits {stats.splits}, elapsed {stats.elapsed_s:.1f}s")
    for msg in stats.messages:
        print(f"note: {msg}")
    done = [s for s in seasons if regular_season_complete(s, today)][-2:]
    t0 = time.perf_counter()
    n = len(store.read("skater_games", done, root=args.store_root))
    print(f"store read of {len(done)} completed seasons: {n} skater-game rows in {time.perf_counter() - t0:.2f}s")
    return 0 if not bad else 1


def cmd_params(args: argparse.Namespace) -> int:
    import statistics
    from datetime import date, datetime, timezone

    from nhl_dfs.build.run import slate_as_of, slate_id_for
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.models import params
    from nhl_dfs.models.priors import prior_table

    if not args.salary:
        print("params needs --salary <DKSalaries.csv> [--as-of YYYY-MM-DD]")
        return 2
    pool = read_salary(args.salary)
    as_of = date.fromisoformat(args.as_of) if args.as_of else slate_as_of(pool, lambda: datetime.now(timezone.utc))
    table = params.projection_for(pool, as_of, store_root=args.store_root)
    df = table.to_frame()
    out = Path(args.out) if args.out else REPO_ROOT / "data" / "features" / "params" / f"{slate_id_for(pool)}_{as_of}" / "params.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    c = table.counts()
    nulls = int(df.isna().sum().sum())
    print(f"params: as_of {as_of} (games before this date only); persons {len(df)}; "
          f"PRIOR={c['PRIOR']} HISTORY={c['HISTORY']} MIXED={c['MIXED']}; MODEL_STATUS={table.source().value}; nulls {nulls}")
    pri = prior_table(pool)
    first_role = {r.person_key: r.role_id for r in reversed(pool.rows)}
    for grp in ("F", "D", "G"):
        m = [p for p in table.persons.values() if p.group == grp and p.source.value != "PRIOR"]
        if m:
            print(f"scale {grp}: {len(m)} persons with history; median mean {statistics.median(p.mean_tenths for p in m) / 10:.1f} "
                  f"pts vs salary/APPG prior {statistics.median(pri[first_role[p.person_key]].mean_tenths for p in m) / 10:.1f} pts "
                  "(recorded, not tuned)")
    for note in table.notes:
        print(f"note: {note}")
    print(f"wrote {out}")
    return 0 if nulls == 0 else 1


def cmd_identity(args: argparse.Namespace) -> int:
    from nhl_dfs.data.history import store
    from nhl_dfs.data.identity import crosswalk as cw

    paths = {"accepted_path": args.accepted_path, "proposals_path": args.proposals_path}
    if args.accept:
        try:
            prop = cw.accept(args.accept, args.nhl_id, **paths)
        except (KeyError, ValueError) as exc:
            print(f"not accepted: {exc}")
            return 1
        print(f"accepted: {prop.dk_name} ({prop.dk_team}, {prop.position_group}) -> NHL {prop.nhl_id} "
              f"{prop.nhl_name} ({prop.nhl_team})")
        return 0
    if not (args.seed and args.salary):
        print("identity needs --seed --salary <DKSalaries.csv>, or --accept <proposal_id>")
        return 2
    from nhl_dfs.intake.salary import read_salary

    pool = read_salary(args.salary)
    people, problems = [], []
    if not args.offline:
        people, problems = cw.directory_from_rosters(cw.nhl_codes())
    seasons = sorted({s for k in ("skater_games", "goalie_games") for s in store.seasons_present(k, root=args.store_root)})
    people += cw.directory_from_history(seasons, store_root=args.store_root)
    res = cw.seed(pool, directory=people, **paths)
    c = res.counts()
    print(f"identity seed: accepted={c['accepted']} proposals={c['proposals']} unmatched={c['unmatched']} "
          f"(new exact accepts written: {res.new_exact}; directory {len(people)} NHL people)")
    for pr in res.proposals:
        print(f"proposal {pr.proposal_id}: {pr.dk_name} ({pr.dk_team}, {pr.position_group}) -> NHL {pr.nhl_id} "
              f"{pr.nhl_name} ({pr.nhl_team} {pr.nhl_position}): {pr.reason}")
    for u in res.unmatched:
        print(f"unmatched: {u['name']} ({u['team']}, {u['group']}): {u['reason']}")
    for msg in problems:
        print(f"note: {msg}")
    print("review proposals, then: .\\nhl.ps1 identity --accept <proposal_id>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
