"""Tracker and dependency-graph tooling for the NHL DFS build chunks.

Parses BUILD_STATUS.md by literal column order and chunks.yaml as the source
of truth for dependencies, requires_files globs, and gated_on text. Never
invents a rule chunks.yaml or BUILD_STATUS.md does not state.

    (no args)               the next chunk: the first in chunks.yaml order whose
                            deps are DONE and that is not itself DONE; runs DONE
                            predecessors' checks first; honors requires_files;
                            skips GATED chunks and chunks BLOCKED on a [BEN]
                            flag (`needs:`), naming them
    --check <id>            run that chunk's checks; exit 1 on failure
    --start <id>            set IN_PROGRESS with today's date
    --done <id> --commit H  set DONE, finish date, commit; refuses unless
                            --check passes and H resolves to a commit
    --block <id> --reason   set BLOCKED with reason
    --status                print the table and open [BEN] flags
    --lint                  check that chunks.yaml, BUILD_STATUS.md, BACKLOG.md, pytest.ini and
                            the Queue block of BUILD_CHUNKS.md agree; exit 1 on any defect
    --render-queue          rewrite the Queue section of BUILD_CHUNKS.md (between
                            the QUEUE:BEGIN and QUEUE:END markers) from chunks.yaml
                            and BUILD_STATUS.md; --dry-run prints it instead
    --root <dir>            repo root to operate on (default: repo containing
                            this script); tests point this at a fixture root

chunks.yaml file order is the rank (the 2026-10-03 queue). A queued chunk carries
`band` (0 legal file, 1 win, 2 washout, 3 other), `size` (S, M, L; XL must be
split), `effort` (reading load), `breakpoint` (the first committable hand-off
point), `needs` (flag numbers that block it), `backlog` (B-rows), `findings`
(review ids) and `impact` (the rationale, with its figures labeled). A top-level
`deferred:` list holds items whose trigger cannot fire yet; `flag_recommendations:`
maps a flag number to the planner's recommendation. The tool validates these
fields and never invents a rank.
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


BANDS = {0: "0 legal file", 1: "1 win", 2: "2 washout", 3: "3 other"}
SIZES = ("S", "M", "L")
_BACKLOG_ID = re.compile(r"^B\d+$")
_FINDING_ID = re.compile(r"^R\d{2}$")


def load_data(chunks_yaml_path: Path) -> dict:
    """The whole chunks.yaml, validated. Raises ValueError naming the first defect."""
    with open(chunks_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    validate_data(data)
    return data


def load_graph(chunks_yaml_path: Path) -> dict[str, dict]:
    data = load_data(chunks_yaml_path)
    return {c["id"]: c for c in data["chunks"]}


def validate_data(data: dict) -> None:
    chunks = data.get("chunks") or []
    ids = [c.get("id") for c in chunks]
    if len(ids) != len(set(ids)) or any(not i for i in ids):
        raise ValueError("chunks.yaml: chunk ids must be present and unique")
    known = set(ids)
    for c in chunks:
        cid = c["id"]
        for dep in c.get("depends", []) or []:
            if dep not in known:
                raise ValueError(f"chunks.yaml: {cid} depends on unknown chunk {dep}")
        if "band" in c:
            if c["band"] not in BANDS:
                raise ValueError(f"chunks.yaml: {cid} band must be one of {sorted(BANDS)}")
            for field in ("size", "effort", "breakpoint", "impact"):
                if not isinstance(c.get(field), str) or not c[field].strip():
                    raise ValueError(f"chunks.yaml: {cid} needs a non-empty `{field}` (a queued chunk carries band, size, "
                                     "effort, breakpoint, impact)")
            if c["size"] not in SIZES:
                raise ValueError(f"chunks.yaml: {cid} size must be S, M or L (XL must be split into chunks)")
        if any(not isinstance(n, int) for n in c.get("needs", []) or []):
            raise ValueError(f"chunks.yaml: {cid} needs must be flag numbers")
        if any(not _BACKLOG_ID.match(str(b)) for b in c.get("backlog", []) or []):
            raise ValueError(f"chunks.yaml: {cid} backlog must be B-row ids (B43, not B43a)")
        if any(not _FINDING_ID.match(str(r)) for r in c.get("findings", []) or []):
            raise ValueError(f"chunks.yaml: {cid} findings must be review ids like R01")
    for d in data.get("deferred") or []:
        if not (d.get("item") or d.get("chunk")):
            raise ValueError("chunks.yaml: a deferred entry needs `item` or `chunk`")
        if d.get("chunk") and d["chunk"] not in known:
            raise ValueError(f"chunks.yaml: deferred entry names unknown chunk {d['chunk']}")
        if not isinstance(d.get("trigger"), str) or not d["trigger"].strip():
            raise ValueError(f"chunks.yaml: deferred entry {d.get('item') or d.get('chunk')} needs a `trigger`")
    for k in (data.get("flag_recommendations") or {}):
        if not isinstance(k, int):
            raise ValueError("chunks.yaml: flag_recommendations keys must be flag numbers")


# --- [BEN] flags (BUILD_STATUS.md) -----------------------------------------------


def parse_flags(lines: list[str]) -> dict[int, dict]:
    """Open [BEN] flags by number: question, default in force, where it lands (literal column order)."""
    try:
        start, end = find_section(lines, FLAGS_HEADING)
    except ValueError:
        return {}
    out: dict[int, dict] = {}
    header_seen = False
    for i in range(start, end):
        line = lines[i]
        if not _is_table_row(line):
            continue
        if not header_seen:
            header_seen = True
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if not parts or not parts[0].isdigit():
            continue
        out[int(parts[0])] = {"flag": parts[1] if len(parts) > 1 else "", "default": parts[2] if len(parts) > 2 else "",
                              "lands": parts[3] if len(parts) > 3 else ""}
    return out


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

    # GATED chunks and chunks BLOCKED on a [BEN] flag keep their rank but are skipped (the queue rule).
    skipped: list[str] = []
    eligible: list[str] = []
    for cid in candidates:
        st = rows[cid].status
        if st == "GATED":
            skipped.append(f"skipped {cid}: GATED ({graph[cid].get('gated_on', '')[:90]})")
        elif st == "BLOCKED" and graph[cid].get("needs"):
            skipped.append(f"skipped {cid}: BLOCKED, needs flag(s) {graph[cid]['needs']} (Ben's answer)")
        else:
            eligible.append(cid)
    if not eligible:
        return "\n".join(["No eligible chunk: every candidate is GATED or BLOCKED on a [BEN] flag.", *skipped])

    in_progress = [cid for cid in eligible if rows[cid].status == "IN_PROGRESS"]
    chosen = in_progress[0] if in_progress else eligible[0]

    row = rows[chosen]
    lines_out.append(chosen)
    lines_out.extend(s for s in skipped if list(graph).index(s.split()[1].rstrip(":")) < list(graph).index(chosen))
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


# --- queue rendering (BUILD_CHUNKS.md between the markers) -----------------------

QUEUE_BEGIN = "<!-- QUEUE:BEGIN -->"
QUEUE_END = "<!-- QUEUE:END -->"


def _cell(text) -> str:
    s = "" if text is None else str(text)
    return s.replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _ids(seq) -> str:
    return ", ".join(str(x) for x in (seq or [])) or "none"


def render_queue(data: dict, rows: dict[str, Row], flags: dict[int, dict], today_str: str | None = None) -> list[str]:
    """The Queue section as lines (no line endings). Rank = chunks.yaml order among queued chunks: chunks that carry a
    `band` and are not DONE or GATED in the tracker. GATED chunks and the `deferred:` list render under Deferred."""
    chunks = data["chunks"]
    status_of = {cid: (rows[cid].status if cid in rows else "no tracker row") for cid in (c["id"] for c in chunks)}
    queued = [c for c in chunks if "band" in c and status_of[c["id"]] not in ("DONE", "GATED")]
    done = [c["id"] for c in chunks if status_of[c["id"]] == "DONE"]
    unranked = [c["id"] for c in chunks if "band" not in c and status_of[c["id"]] not in ("DONE", "GATED")]
    L = [f"Generated {today_str or today()} by `python tools/next_chunk.py --render-queue` from chunks.yaml and BUILD_STATUS.md. "
         "Do not edit by hand; edit chunks.yaml and rerun.", ""]
    L.append(f"DONE: {', '.join(done) if done else 'none'}.")
    if unranked:
        L.append(f"Not ranked (no band in chunks.yaml): {', '.join(unranked)}.")
    L.append("")
    # Defaults taken: flags that block a queued chunk, in rank order of the first chunk each unblocks.
    first_rank: dict[int, int] = {}
    for rank, c in enumerate(queued, 1):
        for n in c.get("needs", []) or []:
            first_rank.setdefault(int(n), rank)
    L.append("### Defaults taken (overturn any in one line)")
    L.append("")
    recs = data.get("flag_recommendations") or {}
    listed = sorted(set(first_rank) | set(recs), key=lambda n: (first_rank.get(n, 10 ** 6), n))
    if not first_rank:
        L.append("No queued chunk is blocked on a [BEN] flag: every chunk proceeds on the default in force and records it.")
        L.append("")
    if listed:
        L.append("| Flag | Blocks (rank) | Question | Default in force | Where it lands | Recommendation |")
        L.append("|---|---|---|---|---|---|")
        for n in listed:
            f = flags.get(n, {})
            who = ", ".join(c["id"] for c in queued if n in (c.get("needs") or []))
            blocks = f"{who} (#{first_rank[n]})" if n in first_rank else "nothing (default proceeds)"
            L.append(_row_md([n, blocks, f.get("flag", "flag not found in BUILD_STATUS.md"), f.get("default", ""),
                              f.get("lands", ""), recs.get(n, "")]))
    L.append("")
    L.append("### Ranked chunks")
    L.append("")
    L.append("| # | Chunk | Band | Title | Size | Effort | Depends | Needs | Backlog | Findings | Status |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for rank, c in enumerate(queued, 1):
        L.append(_row_md([rank, c["id"], BANDS[c["band"]], c.get("title", ""), c["size"], c["effort"], _ids(c.get("depends")),
                          _ids(c.get("needs")), _ids(c.get("backlog")), _ids(c.get("findings")), status_of[c["id"]]]))
    L.append("")
    L.append("### Why each ranks where it does, and where to stop")
    L.append("")
    for rank, c in enumerate(queued, 1):
        L.append(f"- **#{rank} {c['id']}** ({BANDS[c['band']]}): {_cell(c['impact'])} Breakpoint: {_cell(c['breakpoint'])}")
    L.append("")
    L.append("### Deferred")
    L.append("")
    L.append("| Item | Trigger | Backlog | Findings |")
    L.append("|---|---|---|---|")
    deferred = list(data.get("deferred") or [])
    named = {d.get("chunk") for d in deferred if d.get("chunk")}
    for c in chunks:
        if status_of[c["id"]] == "GATED" and c["id"] not in named:
            deferred.append({"chunk": c["id"], "trigger": c.get("gated_on", ""), "backlog": c.get("backlog"), "findings": c.get("findings")})
    for d in deferred:
        label = d.get("chunk") or d.get("item")
        if d.get("chunk"):
            title = next((c.get("title", "") for c in chunks if c["id"] == d["chunk"]), "")
            label = f"{d['chunk']}: {title}" if title else d["chunk"]
        L.append(_row_md([label, d.get("trigger", ""), _ids(d.get("backlog")), _ids(d.get("findings"))]))
    if not deferred:
        L.append("| none | | | |")
    return L


def _row_md(cells: list) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def write_queue(build_chunks_path: Path, body: list[str]) -> None:
    """Replace the lines between the markers (markers kept), preserving the file's line endings."""
    raw = build_chunks_path.read_text(encoding="utf-8", newline="")
    ending = "\r\n" if raw.count("\r\n") >= raw.count("\n") - raw.count("\r\n") and "\r\n" in raw else "\n"
    lines = raw.split(ending) if ending in raw else raw.splitlines()
    try:
        b = next(i for i, l in enumerate(lines) if l.strip() == QUEUE_BEGIN)
        e = next(i for i, l in enumerate(lines) if l.strip() == QUEUE_END and i > b)
    except StopIteration:
        raise ValueError(f"{build_chunks_path.name}: the markers {QUEUE_BEGIN} and {QUEUE_END} must both be present, in order")
    new_lines = lines[: b + 1] + body + lines[e:]
    build_chunks_path.write_text(ending.join(new_lines), encoding="utf-8", newline="")


# --- lint: the queue, the backlog and the tracker agree --------------------------

BACKLOG_STATUSES = ("NEW", "SHADOW", "READY", "DONE", "REJECTED")
OPEN_BACKLOG = ("NEW", "SHADOW", "READY")
# A DONE row may still be named by a live chunk when its Result cell says part of the work remains.
_PARTIAL_WORDS = ("partial", "remainder", "open part", "later", "follow-up", "deferred", "not done")
_BACKLOG_LINE = re.compile(r"^\|\s*(B\d+)\s*\|")
_GENERATED = re.compile(r"^Generated (\d{4}-\d{2}-\d{2}) by ")
_STATUS_CELL = re.compile(r"\|\s*(NEW|SHADOW|READY|DONE|REJECTED)\s*\|")


def read_backlog(path: Path) -> tuple[dict[str, dict], list[str]]:
    """BACKLOG.md rows by id: {status, result}. The second value lists defects (duplicate id, unreadable status)."""
    rows: dict[str, dict] = {}
    problems: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _BACKLOG_LINE.match(raw)
        if not m:
            continue
        bid = m.group(1)
        parts = [p.strip() for p in raw.strip().strip("|").split("|")]
        cell = parts[8] if len(parts) == 10 else ""
        m2 = re.match(r"(NEW|SHADOW|READY|DONE|REJECTED)\b", cell)
        if m2 is None:
            found = _STATUS_CELL.findall(raw)
            m2 = re.match(r"(NEW|SHADOW|READY|DONE|REJECTED)\b", found[-1]) if found else None
        if m2 is None:
            problems.append(f"BACKLOG.md: {bid} has no readable status")
            status, qualified = "?", False
        else:
            status, qualified = m2.group(1), "(" in cell  # "DONE (partial)": a qualified status keeps work open
        if bid in rows:
            problems.append(f"BACKLOG.md: {bid} appears twice")
        rows[bid] = {"status": status, "qualified": qualified, "result": parts[9] if len(parts) == 10 else ""}
    return rows, problems


def _registered_markers(pytest_ini: Path) -> set[str]:
    out: set[str] = set()
    in_markers = False
    for line in pytest_ini.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("markers"):
            in_markers = True
            continue
        if in_markers:
            if line.startswith((" ", "\t")) and ":" in line:
                out.add(line.strip().split(":", 1)[0])
            elif line.strip():
                in_markers = False
    return out


def lint_queue(root: Path) -> tuple[list[str], list[str]]:
    """(problems, notes). Every check reads tracked files only; nothing is written."""
    problems: list[str] = []
    notes: list[str] = []
    try:
        data = load_data(root / "chunks.yaml")
    except ValueError as exc:
        return [str(exc)], notes
    chunks = data["chunks"]
    ids = [c["id"] for c in chunks]
    lines = load_tracker_lines(root / "BUILD_STATUS.md")
    rows = parse_rows(lines)
    flags = parse_flags(lines)

    for cid in ids:
        if cid not in rows:
            problems.append(f"{cid}: no row in BUILD_STATUS.md")

    backlog, bproblems = read_backlog(root / "BACKLOG.md")
    problems.extend(bproblems)

    # Who carries each B-row: a chunk id, or "deferred: <item>" for a deferred item that is not a chunk.
    owners: dict[str, list[str]] = {}

    def own(bid: str, owner: str) -> None:
        if owner not in owners.setdefault(bid, []):
            owners[bid].append(owner)

    for c in chunks:
        for b in c.get("backlog") or []:
            own(b, c["id"])
    for d in data.get("deferred") or []:
        owner = d.get("chunk") or f"deferred: {d.get('item')}"
        for b in d.get("backlog") or []:
            own(b, owner)

    for bid, row in backlog.items():
        if row["status"] in OPEN_BACKLOG and bid not in owners:
            problems.append(f"{bid} is {row['status']} but no chunk or deferred item carries it")
    for bid, who in owners.items():
        row = backlog.get(bid)
        if row is None:
            problems.append(f"{bid} (carried by {', '.join(who)}) is not a BACKLOG.md row")
            continue
        live = [w for w in who if not (w in rows and rows[w].status == "DONE")]
        if not live:
            continue
        if row["status"] == "REJECTED":
            problems.append(f"{bid} is REJECTED but {', '.join(live)} still carries it")
        elif row["status"] == "DONE" and not row["qualified"] and not any(w in row["result"].lower() for w in _PARTIAL_WORDS):
            problems.append(f"{bid} is DONE (not qualified, and its Result does not say part remains) but {', '.join(live)} still carries it")
        if len(who) > 1:
            notes.append(f"{bid} is split across {', '.join(who)}")

    for c in chunks:
        for n in c.get("needs") or []:
            if n not in flags:
                problems.append(f"{c['id']} needs flag {n}, which is not in the Open [BEN] flags table")
    for n in (data.get("flag_recommendations") or {}):
        if n not in flags:
            problems.append(f"flag_recommendations names flag {n}, which is not in the Open [BEN] flags table")

    pytest_ini = root / "pytest.ini"
    if pytest_ini.exists():
        registered = _registered_markers(pytest_ini)
        for c in chunks:
            if c.get("marker") and c["marker"] not in registered:
                problems.append(f"{c['id']}: marker {c['marker']} is not registered in pytest.ini (--strict-markers)")

    build_chunks = root / "BUILD_CHUNKS.md"
    text = build_chunks.read_text(encoding="utf-8") if build_chunks.exists() else ""
    for c in chunks:
        if "band" in c and not re.search(rf"^### {re.escape(c['id'])} [·-]", text, re.M):
            problems.append(f"{c['id']}: no card heading '### {c['id']} · ...' in BUILD_CHUNKS.md")

    body = text.replace("\r\n", "\n").split("\n")
    try:
        b = next(i for i, l in enumerate(body) if l.strip() == QUEUE_BEGIN)
        e = next(i for i, l in enumerate(body) if l.strip() == QUEUE_END and i > b)
    except StopIteration:
        problems.append("BUILD_CHUNKS.md: the QUEUE:BEGIN and QUEUE:END markers are missing")
    else:
        committed = [l.rstrip() for l in body[b + 1:e]]
        m = _GENERATED.match(committed[0]) if committed else None
        if not m:
            problems.append("BUILD_CHUNKS.md: the Queue block does not start with its 'Generated <date> by' line")
        else:
            fresh = [l.rstrip() for l in render_queue(data, rows, flags, today_str=m.group(1))]
            if fresh != committed:
                first = next((i for i, (x, y) in enumerate(zip(fresh, committed)) if x != y), min(len(fresh), len(committed)))
                problems.append(f"BUILD_CHUNKS.md: the Queue block is stale (first difference at block line {first + 1}); "
                                "rerun --render-queue")
    return problems, notes


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
    parser.add_argument("--render-queue", action="store_true")
    parser.add_argument("--lint", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else default_root()
    status_path = root / "BUILD_STATUS.md"
    chunks_yaml_path = root / "chunks.yaml"
    try:
        data = load_data(chunks_yaml_path)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    graph = {c["id"]: c for c in data["chunks"]}

    if args.lint:
        problems, notes = lint_queue(root)
        for n in notes:
            print(f"note: {n}")
        for p in problems:
            print(f"LINT: {p}")
        print("LINT=OK" if not problems else f"LINT=FAIL ({len(problems)} problem(s))")
        return 0 if not problems else 1

    if args.render_queue:
        lines = load_tracker_lines(status_path)
        body = render_queue(data, parse_rows(lines), parse_flags(lines))
        if args.dry_run:
            sys.stdout.write("\n".join(body) + "\n")
            return 0
        try:
            write_queue(root / "BUILD_CHUNKS.md", body)
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        print(f"BUILD_CHUNKS.md: Queue section rewritten ({sum(1 for c in data['chunks'] if 'band' in c)} ranked chunk(s), "
              f"{len(data.get('deferred') or [])} deferred item(s))")
        return 0

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
