"""Re-check written DKEntries bytes against the input salary and entries files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from nhl_dfs.referee import rules
from nhl_dfs.referee.reader import RefEntries, read_entries_min, read_salary_min

_NAME_ID = re.compile(r"^(.*)\((\d+)\)\s*$")


@dataclass
class RefereeReport:
    ok: bool
    reasons: list[str]
    mode: str
    entries_checked: int
    out_sha256: str
    salary_sha256: str
    entries_sha256: str
    parent_sha256: str | None
    notes: list[str] = field(default_factory=list)


def _cell_id(cell: str) -> tuple[str | None, str | None]:
    """(role_id, name_or_None). Blank -> (None, None). Raises on unreadable text."""
    text = cell.strip()
    if not text:
        return None, None
    if text.isdigit():
        return text, None
    m = _NAME_ID.match(text)
    if not m:
        raise ValueError(text)
    return m.group(2), m.group(1).strip()


def _ending(line: bytes) -> bytes:
    for e in (b"\r\n", b"\n", b"\r"):
        if line.endswith(e):
            return e
    return b""


def _binding_reasons(out: RefEntries, ref: RefEntries, ref_label: str) -> list[str]:
    why: list[str] = []
    if out.bom != ref.bom:
        why.append(f"BOM differs from the {ref_label} file")
    if out.entry_ids != ref.entry_ids:
        missing = [e for e in ref.entry_ids if e not in out.entry_ids]
        added = [e for e in out.entry_ids if e not in ref.entry_ids]
        why.append(
            f"entry set or order differs from the {ref_label} file "
            f"(missing={missing[:5]}, added={added[:5]})"
        )
        return why
    if len(out.lines) != len(ref.lines):
        why.append(f"{len(out.lines)} lines, {ref_label} file has {len(ref.lines)}")
        return why
    entry_lines = set(ref.line_of.values())
    roster = set(ref.roster_cols)
    for n, (a, b) in enumerate(zip(out.lines, ref.lines)):
        if n not in entry_lines:
            if a != b:
                why.append(f"non-entry line {n} changed")
            continue
        if _ending(a) != _ending(b):
            why.append(f"line {n}: line ending changed")
        fa, fb = out.fields[n], ref.fields[n]
        if len(fa) != len(fb):
            why.append(f"line {n}: {len(fa)} fields, expected {len(fb)}")
            continue
        changed = [i for i in range(len(fb)) if i not in roster and fa[i] != fb[i]]
        if changed:
            why.append(f"line {n}: non-roster fields changed at columns {changed}")
    return why


def check_file(
    out_path,
    salary_path,
    entries_path,
    *,
    parent_path=None,
    locked: dict[tuple[str, int], str] | None = None,
    added_ids=frozenset(),
) -> RefereeReport:
    """locked maps (Entry ID, roster column index in template order, 0-based)
    to the exact cell text that must still be in the output. added_ids: role IDs a re-downloaded salary file
    ADDED to the draft group after the entries export (backlog B1, declared in the run's manifest); an embedded
    player list missing exactly some of those still binds to this salary file."""
    salary = read_salary_min(salary_path)
    entries = read_entries_min(entries_path)
    out = read_entries_min(out_path)
    parent = read_entries_min(parent_path) if parent_path is not None else None
    reasons: list[str] = []
    notes = list(entries.notes)

    out_mode = "showdown" if "CPT" in out.labels else "classic"
    if out.labels != entries.labels:
        reasons.append(f"roster header {out.labels} differs from the entries file {entries.labels}")
    if out_mode != salary.mode:
        reasons.append(f"entries file is {out_mode}, salary file is {salary.mode}")

    reasons.extend(_binding_reasons(out, entries, "entries"))
    if parent is not None:
        reasons.extend(_binding_reasons(out, parent, "parent"))

    if entries.embedded_ids is not None and entries.embedded_ids != set(salary.rows):
        only_e = len(entries.embedded_ids - set(salary.rows))
        extra = set(salary.rows) - entries.embedded_ids
        if only_e == 0 and extra <= set(added_ids):
            notes.append(f"{len(extra)} ID(s) in the salary file were added by DraftKings after the entries export "
                         "(declared by the run)")
        else:
            reasons.append(
                f"draft group mismatch: {only_e} IDs only in the entries player list, {len(extra)} only in the salary file"
            )

    if salary.mode == out_mode and out.labels == entries.labels:
        for eid in out.entry_ids:
            reasons.extend(_entry_reasons(eid, out.cells[eid], out.labels, salary))

    for (eid, k), expected in (locked or {}).items():
        actual = out.cells.get(eid, [None] * (k + 1))[k] if k < len(out.labels) else None
        if actual != expected:
            reasons.append(f"entry {eid}: locked cell {k} changed ({expected!r} -> {actual!r})")

    return RefereeReport(
        ok=not reasons,
        reasons=reasons,
        mode=out_mode,
        entries_checked=len(out.entry_ids),
        out_sha256=hashlib.sha256(Path(out_path).read_bytes()).hexdigest(),
        salary_sha256=salary.sha256,
        entries_sha256=entries.sha256,
        parent_sha256=parent.sha256 if parent is not None else None,
        notes=notes,
    )


def _entry_reasons(eid: str, cells: list[str], labels: list[str], salary) -> list[str]:
    why: list[str] = []
    rows = []
    for k, cell in enumerate(cells):
        try:
            rid, name = _cell_id(cell)
        except ValueError:
            why.append(f"entry {eid}: cell {k} ({labels[k]}) unreadable: {cell!r}")
            continue
        if rid is None:
            why.append(f"entry {eid}: empty roster cell {k} ({labels[k]})")
            continue
        row = salary.rows.get(rid)
        if row is None:
            why.append(f"entry {eid}: cell {k} ID {rid} is not in the salary file")
            continue
        if rid in salary.ambiguous:
            why.append(f"entry {eid}: cell {k} ID {rid} has an ambiguous identity in the salary file")
        if name is not None and name != row.name:
            why.append(f"entry {eid}: cell {k} name {name!r} does not match ID {rid} ({row.name!r})")
        rows.append(row)
    if why:
        return why
    ok, rule_reasons = rules.legal(rows, salary.mode, slots=labels)
    return [] if ok else [f"entry {eid}: {r}" for r in rule_reasons]
