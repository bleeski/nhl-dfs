"""Tracker and dependency-graph tooling for the NHL DFS build chunks.

Parses BUILD_STATUS.md by literal column order and chunks.yaml as the source
of truth for dependencies, requires_files globs, and gated_on text. Never
invents a rule chunks.yaml or BUILD_STATUS.md does not state.

    (no args)               eligible chunk(s): deps DONE, whatever their own
                            status; runs DONE predecessors' checks first;
                            honors requires_files and gated_on
    --check <id>            run that chunk's checks; exit 1 on failure
    --start <id>            set IN_PROGRESS with today's date
    --done <id> --commit H  set DONE, finish date, commit; refuses unless
                            --check passes and H resolves to a commit
    --block <id> --reason   set BLOCKED with reason
    --status                print the table and open [BEN] flags
    --root <dir>            repo root to operate on (default: repo containing
                            this script); tests point this at a fixture root
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    _here = Path(__file__).resolve()
    _repo_root_guess = _here.parent.parent
    for _candidate in (
        _repo_root_guess / ".venv" / "Scripts" / "python.exe",
        _repo_root_guess / ".venv" / "bin" / "python",
    ):
        if _candidate.exists():
            _result = subprocess.run([str(_candidate), *sys.argv])
            sys.exit(_result.returncode)
    sys.stderr.write(
        "pyyaml is not importable and no .venv was found next to tools/. "
        "Run `uv sync` at the repo root first.\n"
    )
    sys.exit(1)

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CHUNKS_HEADING = "## Chunks"
FLAGS_HEADING = "## Open [BEN] flags"
NEXT_HEADING_RE = re.compile(r"^## ")

# BUILD_STATUS.md columns, in literal order.
COLUMNS = ("Chunk", "Status", "Depends on", "Started", "Finished", "Commit", "Exit checks", "Notes")
NUM_LEAD_COLUMNS = len(COLUMNS) - 1  # every column except Notes; Notes absorbs the remainder


def default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_python_for(root: Path) -> str:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def today() -> str:
    return _dt.date.today().isoformat()


# --- BUILD_STATUS.md parsing -------------------------------------------------


class Row:
    def __init__(self, line_index: int, cells: list[str], line_ending: str):
        self.line_index = line_index
        self.cells = cells  # matches COLUMNS order, length 8
        self.line_ending = line_ending

    @property
    def chunk(self) -> str:
        return self.cells[0].strip()

    @property
    def status(self) -> str:
        return self.cells[1].strip()

    def render(self) -> str:
        return "| " + " | ".join(c.strip() for c in self.cells) + " |" + self.line_ending


def _split_row(raw_line: str) -> tuple[list[str], str]:
    ending = ""
    body = raw_line
    for e in ("\r\n", "\n", "\r"):
        if body.endswith(e):
            ending = e
            body = body[: -len(e)]
            break
    # Drop the leading/trailing empty strings produced by the outer pipes.
    parts = body.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    lead = parts[:NUM_LEAD_COLUMNS]
    rest = parts[NUM_LEAD_COLUMNS:]
    while len(lead) < NUM_LEAD_COLUMNS:
        lead.append("")
    notes = "|".join(rest).strip()
    cells = [c.strip() for c in lead] + [notes]
    return cells, ending


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    if set(stripped.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
        return False  # header separator row, e.g. |---|---|
    return True


def load_tracker_lines(status_path: Path) -> list[str]:
    with open(status_path, "r", encoding="utf-8", newline="") as f:
        return f.readlines()


def find_section(lines: list[str], heading: str) -> tuple[int, int]:
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i
            break
    if start is None:
        raise ValueError(f"heading {heading!r} not found in BUILD_STATUS.md")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if NEXT_HEADING_RE.match(lines[i]):
            end = i
            break
    return start, end


def parse_rows(lines: list[str]) -> dict[str, Row]:
    start, end = find_section(lines, CHUNKS_HEADING)
    rows: dict[str, Row] = {}
    header_seen = False
    for i in range(start, end):
        line = lines[i]
        if not _is_table_row(line):
            continue
        if not header_seen:
            header_seen = True  # first table row is the header labels
            continue
        cells, ending = _split_row(line)
        row = Row(i, cells, ending)
        if row.chunk:
            rows[row.chunk] = row
    return rows


def write_row(status_path: Path, lines: list[str], row: Row) -> None:
    lines[row.line_index] = row.render()
    with open(status_path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


# --- chunks.yaml --------------------------------------------------------------


def load_graph(chunks_yaml_path: Path) -> dict[str, dict]:
    with open(chunks_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {c["id"]: c for c in data["chunks"]}


# --- check execution -----------------------------------------------------------


def _map_check_command(check: str, root: Path) -> list[str]:
    tokens = shlex.split(check)
    venv_py = venv_python_for(root)
    if not tokens:
        return tokens
    if tokens[0] == "pytest":
        return [venv_py, "-m", "pytest", *tokens[1:]]
    if tokens[0] == "python":
        return [venv_py, *tokens[1:]]
    return tokens


def run_checks(chunk: dict, root: Path) -> tuple[bool, list[str]]:
    """Run every check for a chunk. Returns (all_passed, per-check report lines)."""
    reports = []
    all_ok = True
    for check in chunk.get("checks", []):
        cmd = _map_check_command(check, root)
        full_env = {**os.environ, "PYTHONUTF8": "1"}
        result = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True, encoding="utf-8", env=full_env
        )
        ok = result.returncode == 0
        all_ok = all_ok and ok
        status = "PASS" if ok else f"FAIL (exit {result.returncode})"
        reports.append(f"  [{status}] {check}")
        if not ok:
            tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
            reports.append(f"    tail:\n{_indent(tail, 6)}")
    return all_ok, reports


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


# --- requires_files / gated_on --------------------------------------------------


def requires_files_status(chunk: dict, root: Path) -> tuple[bool, list[str]]:
    patterns = chunk.get("requires_files", [])
    if not patterns:
        return True, []
    missing = []
    for pattern in patterns:
        if not list(root.glob(pattern)):
            missing.append(pattern)
    return (not missing), missing


# --- eligibility ----------------------------------------------------------------


def deps_done(chunk_id: str, graph: dict[str, dict], rows: dict[str, Row]) -> bool:
    chunk = graph[chunk_id]
    for dep in chunk.get("depends", []):
        row = rows.get(dep)
        if row is None or row.status != "DONE":
            return False
    return True


def find_next(graph: dict[str, dict], rows: dict[str, Row], root: Path) -> str:
    """Verify DONE predecessors still pass, then report the next chunk."""
    lines_out: list[str] = []

    for chunk_id, chunk in graph.items():
        row = rows.get(chunk_id)
        if row is None or row.status != "DONE":
            continue
        ok, reports = run_checks(chunk, root)
        if not ok:
            lines_out.append(f"REFUSED: predecessor {chunk_id} no longer passes its checks:")
            lines_out.extend(reports)
            return "\n".join(lines_out)

    candidates = [
        cid for cid in graph if deps_done(cid, graph, rows) and rows.get(cid) and rows[cid].status != "DONE"
    ]
    if not candidates:
        return "No eligible chunk: every chunk with satisfied dependencies is already DONE."

    in_progress = [cid for cid in candidates if rows[cid].status == "IN_PROGRESS"]
    chosen = in_progress[0] if in_progress else next(cid for cid in graph if cid in candidates)

    row = rows[chosen]
    lines_out.append(chosen)
    if row.status == "IN_PROGRESS":
        lines_out.append(f"{chosen}: IN_PROGRESS, resume. Notes: {row.cells[7]}")
    elif row.status == "BLOCKED":
        ok, missing = requires_files_status(graph[chosen], root)
        if ok:
            lines_out.append(f"{chosen}: BLOCKED, but requires_files now present; flip to TODO.")
        else:
            lines_out.append(f"{chosen}: BLOCKED. Missing files: {missing}. Notes: {row.cells[7]}")
    elif row.status == "GATED":
        gated_on = graph[chosen].get("gated_on", "")
        lines_out.append(f"{chosen}: GATED. Condition: {gated_on}")
    else:
        lines_out.append(f"{chosen}: TODO, eligible to start.")
    return "\n".join(lines_out)


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--root", type=str, default=None)
    parser.add_argument("--check", type=str, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--done", type=str, default=None)
    parser.add_argument("--commit", type=str, default=None)
    parser.add_argument("--block", type=str, default=None)
    parser.add_argument("--reason", type=str, default=None)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else default_root()
    status_path = root / "BUILD_STATUS.md"
    chunks_yaml_path = root / "chunks.yaml"
    graph = load_graph(chunks_yaml_path)

    if args.status:
        lines = load_tracker_lines(status_path)
        c_start, c_end = find_section(lines, CHUNKS_HEADING)
        f_start, f_end = find_section(lines, FLAGS_HEADING)
        sys.stdout.write("".join(lines[c_start:c_end]))
        sys.stdout.write("\n")
        sys.stdout.write("".join(lines[f_start:f_end]))
        return 0

    if args.check:
        chunk_id = args.check
        if chunk_id not in graph:
            sys.stderr.write(f"unknown chunk: {chunk_id}\n")
            return 1
        ok, reports = run_checks(graph[chunk_id], root)
        print(f"{chunk_id}: {'PASS' if ok else 'FAIL'}")
        for line in reports:
            print(line)
        return 0 if ok else 1

    if args.start:
        chunk_id = args.start
        lines = load_tracker_lines(status_path)
        rows = parse_rows(lines)
        if chunk_id not in rows:
            sys.stderr.write(f"unknown chunk: {chunk_id}\n")
            return 1
        row = rows[chunk_id]
        row.cells[1] = "IN_PROGRESS"
        row.cells[3] = today()
        write_row(status_path, lines, row)
        print(f"{chunk_id}: IN_PROGRESS, started {row.cells[3]}")
        return 0

    if args.done:
        chunk_id = args.done
        if not args.commit:
            sys.stderr.write("--done requires --commit <hash>\n")
            return 1
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", f"{args.commit}^{{commit}}"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            sys.stderr.write(f"--commit {args.commit!r} does not resolve to a commit in {root}\n")
            return 1
        if chunk_id not in graph:
            sys.stderr.write(f"unknown chunk: {chunk_id}\n")
            return 1
        ok, reports = run_checks(graph[chunk_id], root)
        if not ok:
            sys.stderr.write(f"{chunk_id}: checks failed, refusing to mark DONE\n")
            for line in reports:
                sys.stderr.write(line + "\n")
            return 1
        lines = load_tracker_lines(status_path)
        rows = parse_rows(lines)
        if chunk_id not in rows:
            sys.stderr.write(f"unknown chunk: {chunk_id}\n")
            return 1
        row = rows[chunk_id]
        row.cells[1] = "DONE"
        row.cells[4] = today()
        row.cells[5] = args.commit
        row.cells[6] = "PASS"
        write_row(status_path, lines, row)
        print(f"{chunk_id}: DONE, commit {args.commit}")
        return 0

    if args.block:
        chunk_id = args.block
        if not args.reason:
            sys.stderr.write("--block requires --reason <text>\n")
            return 1
        lines = load_tracker_lines(status_path)
        rows = parse_rows(lines)
        if chunk_id not in rows:
            sys.stderr.write(f"unknown chunk: {chunk_id}\n")
            return 1
        row = rows[chunk_id]
        row.cells[1] = "BLOCKED"
        existing = row.cells[7].strip()
        row.cells[7] = f"{args.reason}. {existing}" if existing else args.reason
        write_row(status_path, lines, row)
        print(f"{chunk_id}: BLOCKED. {args.reason}")
        return 0

    # No args: report eligibility.
    lines = load_tracker_lines(status_path)
    rows = parse_rows(lines)
    print(find_next(graph, rows, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
