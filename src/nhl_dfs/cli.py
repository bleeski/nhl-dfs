"""Command-line entry point: `python -m nhl_dfs.cli <command>`.

`status` and `verify` are real. `run` is a stub until C2b.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NEXT_CHUNK = REPO_ROOT / "tools" / "next_chunk.py"


def cmd_status(_args: argparse.Namespace) -> int:
    result = subprocess.run([sys.executable, str(NEXT_CHUNK), "--status"], cwd=str(REPO_ROOT))
    return result.returncode


MAX_REASONS_PRINTED = 20


def cmd_verify(args: argparse.Namespace) -> int:
    from nhl_dfs.referee.check_file import check_file

    if not (args.salary and args.entries and args.out):
        print("verify needs --salary, --entries, and --out")
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


def cmd_run(_args: argparse.Namespace) -> int:
    print("run: available after C2b")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nhl_dfs")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--salary", type=str, default=None)
    verify_parser.add_argument("--entries", type=str, default=None)
    verify_parser.add_argument("--out", type=str, default=None)
    verify_parser.add_argument("--parent", type=str, default=None)
    verify_parser.set_defaults(func=cmd_verify)

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--salary", type=str, default=None)
    run_parser.add_argument("--entries", type=str, default=None)
    run_parser.add_argument("--baseline", action="store_true")
    run_parser.add_argument("--offline", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
