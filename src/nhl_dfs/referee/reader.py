"""Minimal readers for the referee. Standard-library csv only; positional
columns so repeated roster labels stay distinct.

Identity here is the raw (Name, TeamAbbrev, Position) triple from the salary
file: the same evidence DK's CPT/FLEX rows share. Salaries are integer dollars.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

_SALARY_COLUMNS = ("Name", "ID", "Roster Position", "Salary", "TeamAbbrev", "Position")
_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class RefRow:
    role_id: str
    name: str
    person: str  # "Name|TeamAbbrev|Position", raw text
    roster_positions: frozenset[str]
    salary: int
    team: str
    is_goalie: bool


@dataclass
class RefSalary:
    rows: dict[str, RefRow]
    mode: str  # "classic" | "showdown"
    ambiguous: frozenset[str]  # role IDs whose triple has more rows than the mode allows
    sha256: str


@dataclass
class RefEntries:
    entry_ids: list[str]  # file order
    cells: dict[str, list[str]]  # template column order
    labels: list[str]  # roster labels, template order
    roster_cols: list[int]
    line_of: dict[str, int]  # entry id -> physical line index
    lines: list[bytes]  # physical lines with endings, BOM stripped
    fields: list[list[str]]  # csv fields per physical line
    embedded_ids: set[str] | None  # IDs in the embedded player list, if present
    bom: bool
    sha256: str
    notes: list[str] = field(default_factory=list)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_salary_min(path) -> RefSalary:
    raw = Path(path).read_bytes()
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    header = next(reader)
    idx = {}
    for name in _SALARY_COLUMNS:
        if header.count(name) != 1:
            raise ValueError(f"referee: salary column {name!r} appears {header.count(name)} times")
        idx[name] = header.index(name)
    rows: dict[str, RefRow] = {}
    for rec in reader:
        if not any(x.strip() for x in rec):
            continue
        rid = rec[idx["ID"]].strip()
        position = rec[idx["Position"]]
        rows[rid] = RefRow(
            role_id=rid,
            name=rec[idx["Name"]],
            person=f"{rec[idx['Name']]}|{rec[idx['TeamAbbrev']]}|{position}",
            roster_positions=frozenset(x.strip() for x in rec[idx["Roster Position"]].split("/")),
            salary=int(rec[idx["Salary"]]),
            team=rec[idx["TeamAbbrev"]],
            is_goalie=position == "G",
        )
    showdown = any({"CPT", "FLEX"} & r.roster_positions for r in rows.values())
    mode = "showdown" if showdown else "classic"

    counts: dict[tuple[str, bool], list[str]] = {}
    for r in rows.values():
        role_key = "CPT" in r.roster_positions if showdown else False
        counts.setdefault((r.person, role_key), []).append(r.role_id)
    ambiguous = frozenset(rid for ids in counts.values() if len(ids) > 1 for rid in ids)
    return RefSalary(rows=rows, mode=mode, ambiguous=ambiguous, sha256=_sha(raw))


def read_entries_min(path) -> RefEntries:
    raw = Path(path).read_bytes()
    bom = raw.startswith(_UTF8_BOM)
    body = raw[len(_UTF8_BOM):] if bom else raw
    lines = body.splitlines(keepends=True)
    fields = [next(csv.reader([ln.decode("utf-8").rstrip("\r\n")]), []) for ln in lines]
    header = fields[0] if fields else []
    if header[:1] != ["Entry ID"]:
        raise ValueError("referee: entries header does not start with 'Entry ID'")

    roster_cols: list[int] = []
    for i in range(4, len(header)):
        if header[i] == "":
            break
        roster_cols.append(i)
    labels = [header[i] for i in roster_cols]

    entry_ids: list[str] = []
    cells: dict[str, list[str]] = {}
    line_of: dict[str, int] = {}
    for n, rec in enumerate(fields[1:], start=1):
        if rec and rec[0].strip().isdigit():
            eid = rec[0].strip()
            entry_ids.append(eid)
            cells[eid] = [rec[c] if c < len(rec) else "" for c in roster_cols]
            line_of[eid] = n

    notes: list[str] = []
    embedded: set[str] | None = None
    if "Instructions" in header:
        base = header.index("Instructions")
        for n, rec in enumerate(fields):
            if rec[base : base + 4] == ["Position", "Name + ID", "Name", "ID"]:
                embedded = set()
                for later in fields[n + 1 :]:
                    if len(later) > base + 3 and later[base + 3].strip():
                        embedded.add(later[base + 3].strip())
                break
    if embedded is None:
        notes.append("no embedded player list; draft-group binding not checked")

    return RefEntries(
        entry_ids=entry_ids,
        cells=cells,
        labels=labels,
        roster_cols=roster_cols,
        line_of=line_of,
        lines=lines,
        fields=fields,
        embedded_ids=embedded,
        bom=bom,
        sha256=_sha(raw),
        notes=notes,
    )
