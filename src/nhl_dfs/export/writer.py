"""Splice roster cells into DKEntries.csv bytes. Rows are never re-serialized:
every byte outside the roster cells of entry lines is copied unchanged,
including the BOM, line endings, quoting, instructions, and player list.
"""

from __future__ import annotations

from pathlib import Path

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, PoolRow
from nhl_dfs.intake.entries import BOM, EntriesFile, canonical_to_template, physical_lines
from nhl_dfs.intake.salary import SalaryPool

_NEEDS_QUOTES = (",", '"', "\r", "\n")


def format_cell(row: PoolRow) -> str:
    text = f"{row.name} ({row.role_id})"
    if any(ch in text for ch in _NEEDS_QUOTES):
        return '"' + text.replace('"', '""') + '"'
    return text


def field_spans(line: bytes) -> list[tuple[int, int]]:
    """Byte spans (start, end) of each comma-separated field, quotes included.
    `line` excludes its line ending."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(line)
    while True:
        start = i
        if i < n and line[i : i + 1] == b'"':
            i += 1
            while i < n:
                if line[i : i + 1] == b'"':
                    if line[i + 1 : i + 2] == b'"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        while i < n and line[i : i + 1] != b",":
            i += 1
        spans.append((start, i))
        if i >= n:
            return spans
        i += 1  # skip the comma; a trailing comma yields a final empty field


def _split_ending(line: bytes) -> tuple[bytes, bytes]:
    for ending in (b"\r\n", b"\n", b"\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, b""


def write_entries(
    entries: EntriesFile,
    assignment: dict[str, tuple[str, ...]],
    pool: SalaryPool,
    out_path,
) -> bytes:
    """assignment maps Entry ID -> role IDs in canonical slot order
    (CLASSIC_SLOTS or SHOWDOWN_SLOTS); the template's column order is applied here."""
    if entries.mode is not pool.mode:
        raise ValueError(f"entries file is {entries.mode.value}, salary file is {pool.mode.value}")
    file_ids = [e.entry_id for e in entries.entries]
    missing = [eid for eid in file_ids if eid not in assignment]
    extra = sorted(set(assignment) - set(file_ids))
    if missing or extra:
        raise ValueError(f"assignment mismatch: missing={missing} unknown={extra}")

    n_slots = len(CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS)
    bom, lines = physical_lines(entries.raw)
    labels = entries.roster_labels
    for entry in entries.entries:
        role_ids = assignment[entry.entry_id]
        if len(role_ids) != n_slots:
            raise ValueError(f"entry {entry.entry_id}: {len(role_ids)} role IDs, expected {n_slots}")
        rows = []
        for rid in role_ids:
            if rid not in pool.by_role_id:
                raise ValueError(f"entry {entry.entry_id}: role ID {rid} is not selectable in the pool")
            rows.append(pool.by_role_id[rid])
        cells = canonical_to_template([format_cell(r) for r in rows], labels, pool.mode)

        body, ending = _split_ending(lines[entry.line_no])
        spans = field_spans(body)
        if len(spans) <= entries.roster_cols[-1]:
            raise ValueError(f"entry {entry.entry_id}: line has {len(spans)} fields")
        pieces: list[bytes] = []
        cursor = 0
        for col, cell in zip(entries.roster_cols, cells):
            start, end = spans[col]
            pieces.append(body[cursor:start])
            pieces.append(cell.encode("utf-8"))
            cursor = end
        pieces.append(body[cursor:])
        lines[entry.line_no] = b"".join(pieces) + ending

    out = (BOM if bom else b"") + b"".join(lines)
    Path(out_path).write_bytes(out)
    return out
