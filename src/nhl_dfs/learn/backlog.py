"""BACKLOG.md rows from settlement (card C11; plan section 12 "Backlog row").

`add` appends one row to the end of the table and never rewrites an existing line: the file's bytes before the
new line are unchanged. Rows written here carry a stable key in their evidence cell (`[key: ...]`); a row whose
key is already present is not added again (repeated observations are deduplicated). Rows written by hand before
keys existed are mapped in KNOWN, so settlement points at them instead of duplicating them.

Settlement adds rows only for deterministic defects and missing frozen artifacts. One slate's strategy
hypotheses go into the grades and the run notes, pointing at the rows that already hold them (B20 to B22).
NHL_DFS_BACKLOG redirects the file (scratch and test runs).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STATUSES = ("NEW", "SHADOW", "READY", "DONE", "REJECTED")
# Hand-written rows that predate keys: settlement refers to these instead of adding a duplicate.
KNOWN = {
    "ownership_prior_error": "B20",
    "cash_line_field": "B21",
    "stack_size_single_entry": "B22",
    "prize_table_missing": "B24",
    "goalie_not_starting": "B17",
}
_KEY = re.compile(r"\[key: ([a-z0-9_:\-]+)\]")


def default_path() -> Path:
    return Path(os.environ.get("NHL_DFS_BACKLOG") or REPO_ROOT / "BACKLOG.md")


@dataclass
class Row:
    key: str
    evidence: str  # date / run evidence
    problem: str  # "Deterministic defect: ..." or "Gap: ..." or "Hypothesis: ..."
    metric: str
    change: str  # a bounded change
    confidence: str
    acceptance: str
    priority: str  # High | Medium | Low
    status: str = "NEW"
    result: str = ""


def _cell(s: str) -> str:
    return " ".join(str(s).replace("|", "/").split())


def existing(path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(row ids in order, key -> row id)."""
    p = Path(path) if path is not None else default_path()
    ids, keys = [], {}
    if not p.exists():
        return ids, keys
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(B\d+)\s*\|", line)
        if m:
            ids.append(m.group(1))
            for k in _KEY.findall(line):
                keys.setdefault(k, m.group(1))
    return ids, keys


def add(row: Row, path: Path | None = None) -> tuple[str, bool]:
    """(row id, added). Appends only; an existing or known key returns its row id without writing."""
    if row.status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if row.key in KNOWN:
        return KNOWN[row.key], False
    p = Path(path) if path is not None else default_path()
    ids, keys = existing(p)
    if row.key in keys:
        return keys[row.key], False
    if not p.exists():
        raise FileNotFoundError(f"{p}: the backlog file must exist (it is created with its header by hand)")
    new_id = f"B{max((int(i[1:]) for i in ids), default=0) + 1}"
    cells = [new_id, f"{_cell(row.evidence)} [key: {row.key}]", _cell(row.problem), _cell(row.metric), _cell(row.change),
             _cell(row.confidence), _cell(row.acceptance), _cell(row.priority), row.status, _cell(row.result)]
    raw = p.read_bytes()
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    line = ("| " + " | ".join(cells) + " |").encode("utf-8")
    with open(p, "ab") as f:
        if raw and not raw.endswith(b"\n"):
            f.write(nl)
        f.write(line + nl)
    return new_id, True
