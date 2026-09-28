"""Test-only helpers. Not a solver: one deterministic, cheap, legal lineup so
round-trip tests can fill blank templates."""

from __future__ import annotations

import re

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, check_lineup, slot_accepts
from nhl_dfs.intake.entries import existing_lineup

_SPAN_RE = re.compile(rb'"(?:[^"]|"")*"|[^,]*')


def pick_legal(pool) -> tuple[str, ...]:
    """Cheapest-first per slot, preferring an unrepresented team until the
    mode's team minimum is met. Canonical slot order."""
    slots = CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    need = 3 if pool.mode is Mode.CLASSIC else 2
    chosen, people, teams = [], set(), set()
    for slot in slots:
        counts_toward_teams = pool.mode is Mode.SHOWDOWN or slot != "G"
        candidates = [
            r for r in pool.rows if slot_accepts(slot, r, pool.mode) and r.person_key not in people
        ]

        def key(r):
            new_team = counts_toward_teams and len(teams) < need and r.team not in teams
            return (0 if new_team else 1, r.salary, r.role_id)

        best = min(candidates, key=key)
        chosen.append(best)
        people.add(best.person_key)
        if counts_toward_teams:
            teams.add(best.team)
    result = check_lineup(chosen, pool.mode)
    assert result.ok, result.reasons
    return tuple(r.role_id for r in chosen)


def fill_assignment(entries, pool) -> dict[str, tuple[str, ...]]:
    """Each entry keeps its existing lineup; blank entries get pick_legal(pool)."""
    fallback = pick_legal(pool)
    out = {}
    for e in entries.entries:
        current = existing_lineup(e, entries)
        out[e.entry_id] = tuple(current) if all(current) else fallback
    return out


def spans(line: bytes) -> list[tuple[int, int]]:
    """Field spans via a regex tokenizer, independent of export.writer.field_spans."""
    out, pos = [], 0
    while True:
        m = _SPAN_RE.match(line, pos)
        out.append((m.start(), m.end()))
        pos = m.end()
        if pos >= len(line) or line[pos : pos + 1] != b",":
            return out
        pos += 1


def strip_ending(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def first_diff(a: bytes, b: bytes) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"first difference at byte {i}"
    return f"lengths {len(a)} vs {len(b)}"
