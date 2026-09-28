"""Roster legality, written independently of contracts.geometry.

Source: docs/rules/NHL_Classic.txt and docs/rules/NHL_Showdown_Captain_Mode.txt.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

CAP_DOLLARS = 50_000
SLOTS = {
    "classic": ("C", "C", "W", "W", "W", "D", "D", "UTIL", "G"),
    "showdown": ("CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"),
}
MIN_TEAMS = {"classic": 3, "showdown": 2}  # classic counts skaters only


def _mode_name(mode) -> str:
    return str(getattr(mode, "value", mode)).lower()


def _slot_ok(slot: str, row) -> bool:
    if slot == "G":
        return row.is_goalie and "G" in row.roster_positions
    if slot == "UTIL":
        return (not row.is_goalie) and "UTIL" in row.roster_positions
    return slot in row.roster_positions


def legal(rows_in_slot_order: Sequence, mode, slots: Sequence[str] | None = None) -> tuple[bool, list[str]]:
    """rows_in_slot_order: objects with roster_positions, is_goalie, person, team, salary.
    slots defaults to this module's canonical order; check_file passes the
    template's own column labels instead so no reordering happens."""
    m = _mode_name(mode)
    expected = SLOTS[m]
    slot_list = tuple(slots) if slots is not None else expected
    why: list[str] = []
    if Counter(slot_list) != Counter(expected):
        why.append(f"slot labels {list(slot_list)} are not a {m} roster")
    if len(rows_in_slot_order) != len(slot_list):
        why.append(f"{len(rows_in_slot_order)} players for {len(slot_list)} slots")
        return False, why

    for k, (slot, row) in enumerate(zip(slot_list, rows_in_slot_order)):
        if not _slot_ok(slot, row):
            why.append(f"slot {k} {slot}: {row.role_id} not eligible")

    people = Counter(row.person for row in rows_in_slot_order)
    repeated = sorted(p for p, n in people.items() if n > 1)
    if repeated:
        why.append(f"same person more than once: {repeated}")

    total = sum(row.salary for row in rows_in_slot_order)
    if total > CAP_DOLLARS:
        why.append(f"salary {total} over the {CAP_DOLLARS} cap")

    if m == "classic":
        teams = {row.team for row in rows_in_slot_order if not row.is_goalie}
    else:
        teams = {row.team for row in rows_in_slot_order}
    if len(teams) < MIN_TEAMS[m]:
        why.append(f"{len(teams)} team(s) represented, need {MIN_TEAMS[m]}")

    return (not why), why
