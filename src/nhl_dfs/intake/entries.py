"""Read a DKEntries.csv template, keeping every byte.

An entry row is a physical line whose first field is a numeric Entry ID.
Everything else (instructions to the right, the embedded player list below)
is kept verbatim and never parsed as an entry. Repeated roster labels
(C, C, W, W, W, ... or FLEX x5) are handled positionally, never by name.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode

BOM = b"\xef\xbb\xbf"
LEAD_COLUMNS = ("Entry ID", "Contest Name", "Contest ID", "Entry Fee")
_ROSTER_LABELS = set(CLASSIC_SLOTS) | set(SHOWDOWN_SLOTS)
_NAME_ID_CELL = re.compile(r"^.*\((\d+)\)\s*$")


@dataclass(frozen=True)
class EntryRow:
    entry_id: str
    contest_name: str
    contest_id: str
    fee: str
    cells: tuple[str, ...]  # roster cells, template column order
    line_no: int  # 0-based index into physical_lines(raw); the header is line 0
    line_bytes: bytes  # including its line ending


@dataclass
class EntriesFile:
    raw: bytes
    newline: str
    bom: bool
    header: list[str]
    roster_cols: list[int]
    entries: list[EntryRow]
    tail_lines: list[bytes]  # every non-entry line after the header, verbatim
    sha256: str

    @property
    def roster_labels(self) -> tuple[str, ...]:
        return tuple(self.header[c] for c in self.roster_cols)

    @property
    def mode(self) -> Mode:
        return Mode.SHOWDOWN if "CPT" in self.roster_labels else Mode.CLASSIC


def physical_lines(raw: bytes) -> tuple[bool, list[bytes]]:
    """Split raw bytes into (bom, lines-with-endings). Joining returns the input."""
    bom = raw.startswith(BOM)
    body = raw[len(BOM):] if bom else raw
    return bom, body.splitlines(keepends=True)


def _parse_line(line: bytes) -> list[str]:
    text = line.decode("utf-8").rstrip("\r\n")
    return next(csv.reader([text]), [])


def template_permutation(labels: Sequence[str], mode: Mode) -> tuple[int, ...]:
    """perm[k] = canonical slot index that template roster column k holds."""
    canonical = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    if Counter(labels) != Counter(canonical):
        raise ValueError(f"template roster labels {list(labels)} do not match {mode.value} slots")
    free: dict[str, list[int]] = {}
    for j, slot in enumerate(canonical):
        free.setdefault(slot, []).append(j)
    return tuple(free[label].pop(0) for label in labels)


def canonical_to_template(values: Sequence, labels: Sequence[str], mode: Mode) -> tuple:
    perm = template_permutation(labels, mode)
    return tuple(values[perm[k]] for k in range(len(perm)))


def template_to_canonical(values: Sequence, labels: Sequence[str], mode: Mode) -> tuple:
    perm = template_permutation(labels, mode)
    out: list = [None] * len(perm)
    for k, j in enumerate(perm):
        out[j] = values[k]
    return tuple(out)


def cell_role_id(cell: str) -> str | None:
    """Role ID from a roster cell: "Name (ID)" or a bare ID. Blank -> None."""
    text = cell.strip()
    if text == "":
        return None
    if text.isdigit():
        return text
    m = _NAME_ID_CELL.match(text)
    if not m:
        raise ValueError(f"unrecognized roster cell: {cell!r}")
    return m.group(1)


def existing_lineup(entry: EntryRow, entries: EntriesFile) -> tuple[str | None, ...]:
    """The entry's current role IDs in canonical slot order (None for blanks)."""
    ids = tuple(cell_role_id(c) for c in entry.cells)
    return template_to_canonical(ids, entries.roster_labels, entries.mode)


def read_entries(path) -> EntriesFile:
    raw = Path(path).read_bytes()
    bom, lines = physical_lines(raw)
    if not lines:
        raise ValueError(f"{path}: empty entries file")
    first = lines[0]
    newline = "\r\n" if first.endswith(b"\r\n") else "\n" if first.endswith(b"\n") else ""
    header = _parse_line(first)
    if tuple(header[: len(LEAD_COLUMNS)]) != LEAD_COLUMNS:
        raise ValueError(f"{path}: header does not start with {LEAD_COLUMNS}")

    roster_cols: list[int] = []
    for i in range(len(LEAD_COLUMNS), len(header)):
        if header[i] == "":
            break
        if header[i] not in _ROSTER_LABELS:
            raise ValueError(f"{path}: unexpected roster header {header[i]!r} at column {i}")
        roster_cols.append(i)
    labels = [header[c] for c in roster_cols]
    template_permutation(labels, Mode.SHOWDOWN if "CPT" in labels else Mode.CLASSIC)

    entries: list[EntryRow] = []
    tail: list[bytes] = []
    seen: set[str] = set()
    for idx in range(1, len(lines)):
        fields = _parse_line(lines[idx])
        if fields and fields[0].strip().isdigit():
            eid = fields[0].strip()
            if eid in seen:
                raise ValueError(f"{path}: duplicate Entry ID {eid}")
            seen.add(eid)
            if len(fields) <= roster_cols[-1]:
                raise ValueError(f"{path}: entry {eid} has {len(fields)} fields")
            entries.append(
                EntryRow(
                    entry_id=eid,
                    contest_name=fields[1],
                    contest_id=fields[2],
                    fee=fields[3],
                    cells=tuple(fields[c] for c in roster_cols),
                    line_no=idx,
                    line_bytes=lines[idx],
                )
            )
        else:
            tail.append(lines[idx])

    return EntriesFile(
        raw=raw,
        newline=newline,
        bom=bom,
        header=header,
        roster_cols=roster_cols,
        entries=entries,
        tail_lines=tail,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
