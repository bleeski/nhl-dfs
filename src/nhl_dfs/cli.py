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
    m = read_manifest(run)
    report = check_file(out, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv",
                        added_ids=frozenset(m.get("salary_added_ids") or ()))
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


REPORTED_EXTRA = ("MARKET_COVERAGE", "RISK_BUDGET")  # B40: printed on every run, late swap and refresh


def _print_result(result) -> int:
    for k, v in result.statuses.items():
        print(f"{k}={v}")
    for k in REPORTED_EXTRA:  # the scenario pass sets them; a baseline-only run or a late swap does not evaluate them
        if k not in result.statuses:
            print(f"{k}=NOT_EVALUATED")
    print(f"run={result.run.run_id} slate={result.slate_id}")
    if result.public_path:
        print(f"published: {result.public_path}")
    else:
        print("published: nothing (see RUN_NOTES.md)")
    for msg in result.messages:
        print(f"note: {msg}")
    print(f"notes: {result.notes_path}")
    _print_goalies((result.manifest or {}).get("goalies"))
    return 0 if result.ok else 1


def _print_risk_budget(run) -> None:
    """B40: after qa-apply or overrides-apply, the budget line as it stands (NOT_EVALUATED once a version changed lineups)."""
    from nhl_dfs.build.manifest import read_manifest

    try:
        st = read_manifest(run).get("statuses", {})
    except (OSError, ValueError):
        return
    print(f"RISK_BUDGET={st.get('RISK_BUDGET', 'NOT_EVALUATED')}")


def _print_goalies(rec) -> None:
    """B17: the goalie table ends every run, late swap and refresh output (and qa-apply, overrides-apply)."""
    from nhl_dfs.build.goalies import cli_lines

    for line in cli_lines(rec):
        print(line)


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


def _latest_for_entries(runs_root: Path, entries_path) -> str | None:
    """The newest run (by manifest creation time) of the export's mode whose input entries include every entry id in
    the export: a Classic export never resolves to tonight's Showdown run."""
    import json

    from nhl_dfs.intake.entries import read_entries

    cur = read_entries(entries_path)
    want = {e.entry_id for e in cur.entries}
    best = None
    for d in runs_root.iterdir() if runs_root.is_dir() else []:
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            if m.get("mode") != cur.mode.value:
                continue
            have = {e.entry_id for e in read_entries(d / "inputs" / "DKEntries.csv").entries}
        except (OSError, ValueError):
            continue
        if want <= have and (best is None or (m.get("created_utc") or "") > best[0]):
            best = (m.get("created_utc") or "", d.name)
    return best[1] if best else None


def cmd_late_swap(args: argparse.Namespace) -> int:
    from nhl_dfs.build import late_swap

    pos = list(getattr(args, "positional", None) or [])
    if not args.run and pos:
        args.run = pos.pop(0)
    if not args.entries and pos:
        args.entries = pos.pop(0)
    if not args.salary and pos:  # B1: an optional re-downloaded DKSalaries.csv of the same slate
        args.salary = pos.pop(0)
    if pos:
        print(f"late-swap: unexpected extra argument(s): {' '.join(pos)}")
        return 2
    if not (args.run and args.entries):
        print("late-swap needs --run <id> and --entries <current DKEntries.csv downloaded from DK>")
        return 2
    if args.run == "latest":
        found = _latest_for_entries(_roots(args)[0], args.entries)
        if found is None:
            print("FILE_VALID=FALSE")
            print(f"reason: no run under {_roots(args)[0]} matches this export's mode and entry ids; pass --run <id>")
            return 1
        args.run = found
        print(f"latest matching run: {found}")
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


def _qa_run(args):
    """--run <id>, or --run latest (the newest run with a manifest: the skills run the engine first and do not
    know the id in advance)."""
    import json

    from nhl_dfs.build.state import open_run

    runs_root, outputs_root = _roots(args)
    if args.run == "latest":  # by the manifest's creation time, not the folder name (c0b-demo sorts after dates)
        best = None
        for d in runs_root.iterdir() if runs_root.is_dir() else []:
            try:
                created = json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("created_utc") or ""
            except (OSError, ValueError):
                continue
            if best is None or created > best[0]:
                best = (created, d.name)
        if best is None:
            raise SystemExit(f"no run under {runs_root}")
        return open_run(runs_root, best[1]), runs_root, outputs_root
    return open_run(runs_root, args.run), runs_root, outputs_root


def cmd_slate(args: argparse.Namespace) -> int:
    """C10 /nhl-run preprocessing in ONE command (a skill's permission rule matches a single `.\\nhl.ps1 ...`
    call): run the slate; only if the run published, write and print the research request and the round-1 QA
    packet for THAT run (never "latest")."""
    import json
    from datetime import datetime, timezone

    from nhl_dfs.build import controller, packet
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.build.state import open_run

    if len(args.files) != 2:
        print('slate needs two files: <DKSalaries.csv> <DKEntries.csv> (quote paths with spaces)')
        return 2
    runs_root, outputs_root = _roots(args)
    result = run_slate(args.files[0], args.files[1], offline=args.offline, out_root=runs_root, outputs_root=outputs_root,
                       baseline_only=False, scenario=True)
    code = _print_result(result)
    if code != 0:
        print("RUN FAILED: no research request and no QA packet")
        return code
    run = open_run(runs_root, result.run.run_id)
    now = datetime.now(timezone.utc)
    req = packet.research_request(run, now=now, runs_root=runs_root)
    rpath = run.path / "news" / "research_request.json"
    rpath.parent.mkdir(exist_ok=True)
    rpath.write_text(json.dumps(req, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"RESEARCH_PLAYERS={len(req['players'])}")
    print("----- REQUEST JSON (pass inline, verbatim) -----")
    print(packet.serialize(req))
    cfg = packet.load_qa_config()
    ok, why = controller.permitted(run, 1, cfg, now, runs_root)
    if not ok:
        print(f"QA_PERMITTED=NO: {why}")
        return 0
    try:
        p = packet.build(run, 1, cfg, now=now, runs_root=runs_root)
    except ValueError as exc:  # over the cap even after trimming: QA ends, the published file stands
        print(f"QA_PERMITTED=NO: packet could not be built ({exc}); the checked file stands")
        return 0
    (controller.qa_dir(run) / "packet_1.json").write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"QA_PERMITTED=YES: {why} (packet_id {p['packet_id']}, about {p['size']['est_tokens']} tokens)")
    print("----- PACKET JSON (pass inline, verbatim) -----")
    print(packet.serialize(p))
    return 0


def cmd_qa_rehearse(args: argparse.Namespace) -> int:
    """C10 isolation rehearsal: --prepare builds a throwaway run under <runs-root>/_rehearsal and prints the
    PLANTED token (for this conversation only) and the canary packet; --check --reply <file> verifies the
    adversary's reply and appends the verdict to docs/measured_usage.md."""
    from nhl_dfs.build import packet, rehearse

    runs_root = Path(args.runs_root)
    if args.prepare:
        st = rehearse.prepare(runs_root)
        print(f"REHEARSAL run={st['run_id']} packet_id={st['packet_id']}")
        print(f"PLANTED TOKEN (stays in this conversation; never pass it to the adversary): {st['planted']}")
        print(f"CANARY (inside the packet): {st['canary']}")
        print(f"SAVE THE REPLY TO: {st['reply_path']}")
        print("----- PACKET JSON (pass inline, verbatim) -----")
        print(packet.serialize(st["packet"]))
        return 0
    if args.check and args.reply:
        res = rehearse.check(Path(args.reply).read_text(encoding="utf-8-sig", errors="replace"), runs_root)
        for k, v in res.items():
            print(f"{k}={v}")
        print(f"REHEARSAL={res['verdict']} (recorded in docs/measured_usage.md)")
        return 0 if res["verdict"] == "PASS" else 1
    print("qa-rehearse needs --prepare, or --check --reply <file saved verbatim>")
    return 2


def cmd_qa_packet(args: argparse.Namespace) -> int:
    """C10: write runs/<id>/qa/packet_<k>.json and print it (the skill passes the content inline to the adversary)."""
    import json
    from datetime import datetime, timezone

    from nhl_dfs.build import controller, packet

    if not (args.run and args.round):
        print("qa-packet needs --run <id> --round <k>")
        return 2
    run, runs_root, _ = _qa_run(args)
    cfg = packet.load_qa_config()
    now = _as_of(args.as_of) or datetime.now(timezone.utc)
    ok, why = controller.permitted(run, int(args.round), cfg, now, runs_root)
    if not ok:
        print(f"QA_PERMITTED=NO: {why}")
        return 1
    try:
        p = packet.build(run, int(args.round), cfg, now=now, canary=args.canary, runs_root=runs_root)
    except ValueError as exc:
        print(f"QA_PERMITTED=NO: packet could not be built ({exc}); the checked file stands")
        return 1
    path = controller.qa_dir(run) / f"packet_{int(args.round)}.json"
    path.write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"QA_PERMITTED=YES: {why}")
    print(f"packet: {path} (packet_id {p['packet_id']}, about {p['size']['est_tokens']} tokens)")
    print("----- PACKET JSON (pass inline, verbatim) -----")
    print(packet.serialize(p))
    return 0


def cmd_qa_apply(args: argparse.Namespace) -> int:
    from nhl_dfs.build import controller

    if not (args.run and args.round and args.proposals):
        print("qa-apply needs --run <id> --round <k> --proposals <file saved verbatim>")
        return 2
    run, runs_root, outputs_root = _qa_run(args)
    as_of = _as_of(args.as_of)  # B52: only a labeled rehearsal freezes the clock; production reads the wall clock live
    res = controller.apply_round(run, int(args.round), Path(args.proposals), now=as_of,
                                 clock=(lambda: as_of) if as_of is not None else None,
                                 runs_root=runs_root, outputs_root=outputs_root)
    for line in res.lines():
        print(line)
    _print_risk_budget(run)
    _print_goalies(_goalie_refresh(run))
    return 0


def _goalie_refresh(run):
    from datetime import datetime, timezone

    from nhl_dfs.build import goalies

    try:
        return goalies.refresh_run_record(run, now=datetime.now(timezone.utc), offline=True)
    except Exception as exc:  # the table is a report; a failure here never changes the file
        print(f"goalie table unavailable ({type(exc).__name__}: {str(exc)[:100]})")
        return None


def cmd_research_request(args: argparse.Namespace) -> int:
    import json
    from datetime import datetime, timezone

    from nhl_dfs.build import packet

    if not args.run:
        print("research-request needs --run <id>")
        return 2
    run, runs_root, _ = _qa_run(args)
    req = packet.research_request(run, now=_as_of(args.as_of) or datetime.now(timezone.utc), runs_root=runs_root)
    path = run.path / "news" / "research_request.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(req, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"RESEARCH_PLAYERS={len(req['players'])}")
    print(f"request: {path}")
    print("----- REQUEST JSON (pass inline, verbatim) -----")
    print(packet.serialize(req))
    return 0


def cmd_overrides_apply(args: argparse.Namespace) -> int:
    from nhl_dfs.build import controller

    if not (args.run and args.file):
        print("overrides-apply needs --run <id> --file <researcher reply saved verbatim>")
        return 2
    run, runs_root, outputs_root = _qa_run(args)
    news = run.path / "news"
    k = 1 + max([int(p.stem.split("_")[1]) for p in news.glob("overrides_*_result.json")] or [0]) if news.exists() else 1
    as_of = _as_of(args.as_of)
    res = controller.apply_round(run, k, Path(args.file), now=as_of, clock=(lambda: as_of) if as_of is not None else None,
                                 runs_root=runs_root, outputs_root=outputs_root, source="overrides")
    for line in res.lines()[:-1]:
        print(line.replace("QA round", "overrides file"))
    print(f"OVERRIDES_ACCEPTED={res.accepted_correctness}")
    _print_risk_budget(run)
    _print_goalies(_goalie_refresh(run))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    from nhl_dfs.build import refresh

    pos = list(getattr(args, "positional", None) or [])
    if not args.run and pos:
        args.run = pos.pop(0)
    if not args.salary and pos:  # B1: an optional re-downloaded DKSalaries.csv of the same slate
        args.salary = pos.pop(0)
    if pos:
        print(f"refresh: unexpected extra argument(s): {' '.join(pos)}")
        return 2
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


def _usd(c) -> str:
    return "unknown" if c is None else (f"-${-c / 100:.2f}" if c < 0 else f"${c / 100:.2f}")


def cmd_scheduled_refresh(args: argparse.Namespace) -> int:
    """B23: the dispatcher in one `.\nhl.ps1` call (a Claude Code scheduled task runs exactly this)."""
    from nhl_dfs.build.scheduled import main as dispatch_main

    argv = ["--once"] + (["--dry-run"] if args.dry_run else []) + (["--no-toast"] if args.no_toast else [])
    return dispatch_main(argv + (["--as-of", args.as_of] if args.as_of else []))


def cmd_settle(args: argparse.Namespace) -> int:
    """C11: one call does everything (the /nhl-settle skill preprocesses exactly this)."""
    from nhl_dfs.learn import settle

    pos = list(args.positional or [])
    if not args.run and pos:
        args.run = pos.pop(0)
    if not args.standings and pos:
        args.standings = pos.pop(0)
    if not (args.run and args.standings):
        print('settle needs <run-id> "<standings file, zip or folder>"')
        return 2
    try:
        rec = settle.run(args.run, args.standings, runs_root=Path(args.runs_root), prize_paths=args.prize_table,
                         winnings_path=args.winnings, boxscores=not args.no_boxscores, offline=args.offline,
                         ledger_root=args.ledger_root, backlog_path=args.backlog)
    except (FileNotFoundError, ValueError) as exc:
        print("SETTLE=FAILED")
        print(f"reason: {exc}")
        return 1
    led = rec["ledger"]
    t = led["totals"]
    fz = rec["freeze_check"]
    print(f"SETTLE={'OK' if fz['ok'] else 'FREEZE_CHECK_FAILED'} run={rec['run_id']} mode={rec['mode']} "
          f"slate_date={led['slate_date']}")
    print(f"FORECAST={rec['forecast']['status']} ({rec['forecast']['detail']})")
    print(f"FREEZE_CHECK={'OK' if fz['ok'] else 'CHANGED: ' + ', '.join(fz['changed'][:5])} ({fz['files']} frozen files)")
    for k, v in rec["statuses"].items():
        print(f"{k}={v}")
    for cid, c in led["by_contest"].items():
        print(f"contest {cid} {c['contest_name']}: {c['entries']} entr{'y' if c['entries'] == 1 else 'ies'}, fees "
              f"{_usd(c['fees_cents'])}, gross {_usd(c['gross_cents'])}, net {_usd(c['net_cents'])} "
              f"({'/'.join(c['sources'])})")
    for e in led["entries"]:
        print(f"  entry {e['entry_id']}: rank {e['rank']}{' tied ' + str(e['tied']) if (e['tied'] or 1) > 1 else ''}, "
              f"{e['points']} pts, payout {_usd(e['payout_cents'])} ({e['payout_source']})"
              + ("" if e["entered_matches"] is not False else " ENTERED LINEUP DIFFERS from the run's final version"))
    print(f"NET_KNOWN={_usd(t['net_known_cents'])} on {_usd(t['fees_known_cents'])} of fees; UNKNOWN_FEES="
          f"{_usd(t['fees_unknown_cents'])}; SLATE_NET={_usd(t['net_cents'])}")
    dd = rec["drawdown"]
    print(f"LEDGER: {dd['runs']} run(s) on {dd['slate_dates']} slate date(s), cumulative net known {_usd(dd['cum_net_known_cents'])}, drawdown "
          f"{_usd(dd['drawdown_cents'])} (max {_usd(dd['max_drawdown_cents'])}){'' if dd['complete'] else ', INCOMPLETE'}")
    for g in rec["ownership"]:
        print(f"OWNERSHIP {g['contest_id']} {g['family']}: MAE {g['mae_all']:.2f} (active {g['mae_active']:.2f}), "
              f"Pearson {g['pearson']:.2f}, Spearman {g['spearman']:.2f}, top-10 recall {g['top_chalk_recall']:.1f}"
              + (f", CPT MAE {g['cpt_share_err']:.2f}" if g.get("cpt_share_err") is not None else ""))
    fg = rec["forecasts"]
    if fg:
        o, d = fg["overall"], fg["goalie_decisions"]
        cond = "skaters conditional on dressing" if str(fg.get("skater_conditioning", "")).startswith("CONDITIONAL") \
            else "skaters unconditional on dressing"
        print(f"FORECASTS (PARTICIPATION={fg['participation_status']}; {cond}): n {o.get('n')}, "
              f"MAE {o.get('mae')}, bias "
              f"{o.get('bias')}, CRPS {o.get('crps')}, p10-p90 coverage {o.get('cover_p10_p90')}; goalie starts "
              + (f"{d.get('accuracy')} of {d.get('teams')} teams" if d.get("teams") else d.get("status")))
        if not isinstance(fg["bonus_rate_calibration"], str):  # B25: a cache with per-draw indicators
            from nhl_dfs.learn.grade_forecasts import bonus_summary

            print(f"BONUS_CALIBRATION: {bonus_summary(fg['bonus_rate_calibration'])}")
        sc = fg.get("start_probability_check") or {}
        if sc.get("goalies"):
            print(f"P_START: saved at build; max |z| {sc['max_abs_z']} vs the frozen draws "
                  f"({'within' if sc['within_3_se'] else 'OUTSIDE'} 3 SE); Brier {d.get('brier')}")
    for mode, gt in rec["gates"].items():
        print(f"GATE {mode}: tier {gt['tier']}; allowed: {', '.join(k for k, v in gt['allowed'].items() if v) or 'none'}")
    for b in rec["backlog"]:
        print(f"BACKLOG {b['id']}: {'added' if b['added'] else 'already holds'} {b['key']}")
    if rec["winnings_template"]:
        print(f"WINNINGS_TEMPLATE={rec['winnings_template']}")
    for n in rec["notes"][:10]:
        print(f"note: {n}")
    print(f"grades: {Path(args.runs_root) / rec['run_id'] / 'settle' / 'grades.json'}")
    print(f"ledger: {rec['ledger_path']}")
    return 0 if fz["ok"] else 1


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
    hist.add_argument("--keep-raw", action="store_true", help="B2: skip the raw report cache eviction after the backfill")
    hist.add_argument("--status", action="store_true", help="C39: report how current the store is (local files only)")
    hist.add_argument("--as-of", type=str, default=None, help="with --status or --refresh: YYYY-MM-DD; default today (ET)")
    hist.add_argument("--refresh", action="store_true", help="C39: bring the store up to the last finished day (NHL reports only)")
    hist.add_argument("--budget-s", type=float, default=120.0, help="with --refresh: give up after this many seconds, store unchanged")
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

    sl = sub.add_parser("slate")
    sl.add_argument("files", nargs="*")
    sl.add_argument("--offline", action="store_true")
    sl.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    sl.add_argument("--outputs-root", type=str, default=None)
    sl.set_defaults(func=cmd_slate)

    reh = sub.add_parser("qa-rehearse")
    reh.add_argument("--prepare", action="store_true")
    reh.add_argument("--check", action="store_true")
    reh.add_argument("--reply", type=str, default=None)
    reh.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    reh.set_defaults(func=cmd_qa_rehearse)

    for name, func in (("qa-packet", cmd_qa_packet), ("qa-apply", cmd_qa_apply), ("research-request", cmd_research_request),
                       ("overrides-apply", cmd_overrides_apply)):
        sp = sub.add_parser(name)
        sp.add_argument("--run", type=str, default=None)
        sp.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
        sp.add_argument("--outputs-root", type=str, default=None)
        sp.add_argument("--as-of", type=str, default=None, help="REHEARSAL clock in UTC")
        if name in ("qa-packet", "qa-apply"):
            sp.add_argument("--round", type=int, default=None)
        if name == "qa-packet":
            sp.add_argument("--canary", type=str, default=None, help="rehearsal only: a token the adversary must echo")
        if name == "qa-apply":
            sp.add_argument("--proposals", type=str, default=None)
        if name == "overrides-apply":
            sp.add_argument("--file", type=str, default=None)
        sp.set_defaults(func=func)

    for name, func in (("late-swap", cmd_late_swap), ("refresh", cmd_refresh)):
        sp = sub.add_parser(name)
        sp.add_argument("--run", type=str, default=None)
        if name == "late-swap":
            sp.add_argument("--entries", type=str, default=None)
            sp.add_argument("--fast", action="store_true")
            sp.add_argument("positional", nargs="*",
                            help="skills: <run-id|latest> <current DKEntries.csv> [<fresh DKSalaries.csv>]")
        else:
            sp.add_argument("positional", nargs="*", help="skills: <run-id> [<fresh DKSalaries.csv>]")
        sp.add_argument("--offline", action="store_true")
        sp.add_argument("--salary", type=str, default=None, help="a fresh DKSalaries.csv of the same slate (status update)")
        sp.add_argument("--as-of", type=str, default=None, help="REHEARSAL clock in UTC, e.g. 2026-10-15T23:10:00Z")
        sp.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
        sp.add_argument("--outputs-root", type=str, default=None)
        sp.add_argument("--objective", choices=("auto", "scenario", "provisional", "baseline"), default="auto",
                        help="auto: scenario, then provisional, then baseline (each fallback reported)")
        sp.set_defaults(func=func)

    sp = sub.add_parser("scheduled-refresh", help="B23: refresh the newest delivered run of each slate at T-60 and T-20")
    sp.add_argument("--dry-run", action="store_true", help="list what is due; refresh and notify nothing")
    sp.add_argument("--no-toast", action="store_true", help="no Windows toast (a Claude scheduled task relays NOTIFY)")
    sp.add_argument("--as-of", type=str, default=None, help="REHEARSAL clock in UTC (nothing is refreshed)")
    sp.set_defaults(func=cmd_scheduled_refresh)

    sp = sub.add_parser("settle", help="C11: settle a run from DraftKings standings (money, grades, notes, backlog)")
    sp.add_argument("--run", type=str, default=None)
    sp.add_argument("--standings", type=str, default=None, help="a standings CSV, a .zip, or a folder of them")
    sp.add_argument("positional", nargs="*", help="skills: <run-id> <standings path>")
    sp.add_argument("--prize-table", action="append", default=[], help="a DK contest detail JSON (repeatable)")
    sp.add_argument("--winnings", type=str, default=None, help="winnings.csv from DraftKings My Contests")
    sp.add_argument("--no-boxscores", action="store_true", help="do not fetch NHL box scores (participation UNKNOWN)")
    sp.add_argument("--offline", action="store_true", help="box scores from the local cache only")
    sp.add_argument("--runs-root", type=str, default=str(RUNS_ROOT))
    sp.add_argument("--ledger-root", type=str, default=None, help="default data/ledger (NHL_DFS_LEDGER_ROOT)")
    sp.add_argument("--backlog", type=str, default=None, help="default BACKLOG.md (NHL_DFS_BACKLOG)")
    sp.set_defaults(func=cmd_settle)

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
    from zoneinfo import ZoneInfo

    from nhl_dfs.data.history import history_seasons, nhl_reports, regular_season_complete, store
    from nhl_dfs.data.http import load_sources_config

    if args.status:  # C39: where the store stands, from local files only; nothing is fetched or written
        from nhl_dfs.data.history import status

        as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(ZoneInfo("America/New_York")).date()
        print(f"history store status as of {as_of} (games strictly before that date feed the models)")
        print(status.measure(as_of, store_root=args.store_root).line())
        return 0
    if args.refresh:  # C39: the same bounded refresh a run does after its first publish, by hand (NHL reports only)
        from nhl_dfs.data.history import refresh as refresh_mod
        from nhl_dfs.data.history import status
        from nhl_dfs.data.http import HttpCache

        now = datetime.now(timezone.utc)
        as_of = date.fromisoformat(args.as_of) if args.as_of else now.astimezone(ZoneInfo("America/New_York")).date()
        print(f"history refresh as of {as_of}: NHL reports only; the store changes only if the whole fetch succeeds")
        print("before: " + status.measure(as_of, store_root=args.store_root).line())
        cache = HttpCache(Path(args.raw_root)) if args.raw_root else None
        res = refresh_mod.refresh_incremental(as_of, now=now, store_root=args.store_root, cache=cache, budget_s=float(args.budget_s))
        print("result: " + res.line())
        print("after: " + status.measure(as_of, store_root=args.store_root).line())
        return 0 if res.outcome in ("REFRESHED", "NO_NEW_GAMES", "SKIPPED") else 1
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
    # B2: the raw report cache is cleaned only after a clean backfill (no exception, no cross-check mismatch)
    from pathlib import Path

    from nhl_dfs.data.history import moneypuck as mp_mod

    mp_root = Path(args.raw_root) if args.raw_root else mp_mod.REPO_ROOT / cfg["moneypuck"]["raw_root"]
    evict = not (bad or args.keep_raw or not cfg["history"].get("evict_raw", True))
    if not evict:
        why = "cross-check mismatches" if bad else ("--keep-raw" if args.keep_raw else "history.evict_raw is false")
        print(f"raw cache: not evicted ({why}); sizes and what a clean run would evict:")
    ev = nhl_reports.evict_raw(stats.raw_root, cfg=cfg, store_root=args.store_root, today=today, mp_root=mp_root,
                               dry_run=not evict)
    for line in ev.lines():
        print(line)
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
