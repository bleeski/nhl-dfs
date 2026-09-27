"""Command-line entry point: `python -m nhl_dfs.cli <command>`.

Only `status` is real in C0a. `verify` and `run` are stubs until C0b and C2b.
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


def cmd_verify(_args: argparse.Namespace) -> int:
    print("verify: available after C0b")
    return 2


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
