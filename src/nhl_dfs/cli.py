"""Command-line entry point: `python -m nhl_dfs.cli <command>`.

`status`, `verify`, `probe`, and `run --baseline` are real (C2b).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NEXT_CHUNK = REPO_ROOT / "tools" / "next_chunk.py"
RUNS_ROOT = REPO_ROOT / "runs"
OUTPUTS_ROOT = REPO_ROOT / "outputs"


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
    if not args.baseline:
        print("run: only the baseline run exists until C8; add --baseline")
        return 2
    runs_root = Path(args.runs_root)
    outputs_root = Path(args.outputs_root) if args.outputs_root else runs_root.parent / "outputs"
    result = run_slate(args.salary, args.entries, offline=args.offline, out_root=runs_root, outputs_root=outputs_root)
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
