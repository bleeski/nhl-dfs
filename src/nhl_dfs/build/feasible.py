"""Dependency-light roster feasibility search (plan section 10, ladder step 4). No scipy.

find_one runs a complete depth-first search, so it can tell a timeout from proof:
  FOUND              a lineup that passes check_lineup
  INFEASIBLE_PROVEN  the search space was exhausted (or a static bound proves it empty)
  TIMEOUT            the budget ran out first; says nothing about feasibility

Search order: Classic fills the goalie first, then C, W, D, UTIL; Showdown enumerates CPT
rows by salary, then completes FLEX. Candidates within each slot type are tried
cheapest-first with backtracking. Positions of the same slot type are filled as a
combination (increasing index), which removes permutations and nothing else.

Pruning uses only valid bounds, never a heuristic: salary so far plus the cheapest possible
fill of the remaining positions must fit the cap, and the counting teams so far plus the
new teams still reachable must reach the team rule (the team-count repair: the search
backtracks out of cheap one- and two-team prefixes as soon as the rule becomes unreachable).
Duplicate persons, arbitrary roster_positions, and locked slots are respected.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from nhl_dfs.contracts.geometry import (
    CLASSIC_SLOTS,
    SALARY_CAP,
    SHOWDOWN_SLOTS,
    Mode,
    PoolRow,
    check_lineup,
    slot_accepts,
)
from nhl_dfs.contracts.statuses import FeasibleStatus
from nhl_dfs.intake.salary import SalaryPool

_MIN_TEAMS = {Mode.CLASSIC: 3, Mode.SHOWDOWN: 2}
_GROUP_ORDER = {Mode.CLASSIC: ("G", "C", "W", "D", "UTIL"), Mode.SHOWDOWN: ("CPT", "FLEX")}
_CHECK_EVERY = 64


@dataclass
class FeasibleResult:
    status: FeasibleStatus
    lineup: list[str] | None  # role_ids in canonical slot order
    nodes: int
    elapsed_s: float
    detail: str = ""


class _Timeout(Exception):
    pass


def _counts(mode: Mode, row: PoolRow) -> bool:
    """Does this row count toward the team rule? Classic counts skaters only."""
    return mode is Mode.SHOWDOWN or not row.is_goalie


def find_one(
    pool: SalaryPool,
    mode: Mode,
    *,
    exclude: frozenset[str] = frozenset(),
    locked: Mapping[int, str] | None = None,
    budget_s: float = 2.0,
) -> FeasibleResult:
    """One legal lineup, or proof that none exists, or TIMEOUT.

    locked: canonical slot index -> role_id (CLASSIC_SLOTS / SHOWDOWN_SLOTS order); a locked
    row wins over exclude. A locked role that is not in the pool is a ValueError (bad input);
    a lock the slot rejects, or one person locked twice, is INFEASIBLE_PROVEN.
    """
    t0 = time.perf_counter()
    deadline = t0 + max(budget_s, 0.0)
    slots = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    locked = dict(locked or {})

    def done(status: FeasibleStatus, lineup=None, nodes=0, detail="") -> FeasibleResult:
        return FeasibleResult(status, lineup, nodes, time.perf_counter() - t0, detail)

    # Locks.
    used: set[str] = set()
    salary0 = 0
    teams0: Counter[str] = Counter()
    for i, rid in sorted(locked.items()):
        if not 0 <= i < len(slots):
            raise ValueError(f"locked slot index {i} out of range for {mode.value}")
        r = pool.by_role_id.get(rid)
        if r is None:
            raise ValueError(f"locked role {rid} is not a selectable row of this pool")
        if not slot_accepts(slots[i], r, mode):
            return done(FeasibleStatus.INFEASIBLE_PROVEN, detail=f"locked slot {i} ({slots[i]}) rejects role {rid}")
        if r.person_key in used:
            return done(FeasibleStatus.INFEASIBLE_PROVEN, detail=f"person {r.person_key} locked twice")
        used.add(r.person_key)
        salary0 += r.salary
        if _counts(mode, r):
            teams0[r.team] += 1

    # Free positions, grouped by slot type in search order.
    free_idx = {s: [k for k, slot in enumerate(slots) if slot == s and k not in locked] for s in _GROUP_ORDER[mode]}
    locked_rids = set(locked.values())
    groups: list[tuple[str, list[PoolRow], int]] = []  # (slot type, sorted candidates, positions)
    for s in _GROUP_ORDER[mode]:
        k = len(free_idx[s])
        if k == 0:
            continue
        cands = sorted(
            (r for r in pool.rows
             if r.role_id not in exclude and r.role_id not in locked_rids
             and r.person_key not in used and slot_accepts(s, r, mode)),
            key=lambda r: (r.salary, r.role_id),
        )
        if len(cands) < k:
            return done(FeasibleStatus.INFEASIBLE_PROVEN, detail=f"slot {s}: {len(cands)} candidate(s) for {k} position(s)")
        groups.append((s, cands, k))

    need = _MIN_TEAMS[mode]
    # Static bounds for groups not yet started: cheapest fill, and reachable new teams.
    prefix = [[0] for _ in groups]
    for g, (_, cands, _) in enumerate(groups):
        for r in cands:
            prefix[g].append(prefix[g][-1] + r.salary)
    fut_cost = [0] * (len(groups) + 1)
    fut_slots = [0] * (len(groups) + 1)
    fut_teams: list[frozenset[str]] = [frozenset()] * (len(groups) + 1)
    for g in range(len(groups) - 1, -1, -1):
        s, cands, k = groups[g]
        fut_cost[g] = fut_cost[g + 1] + prefix[g][k]
        counting = [r for r in cands if _counts(mode, r)]
        fut_slots[g] = fut_slots[g + 1] + (k if counting else 0)
        fut_teams[g] = fut_teams[g + 1] | {r.team for r in counting}

    if salary0 + fut_cost[0] > SALARY_CAP:
        return done(FeasibleStatus.INFEASIBLE_PROVEN, detail="cheapest possible fill exceeds the salary cap")
    reachable = len(teams0) + min(fut_slots[0], len(fut_teams[0] - set(teams0)))
    if reachable < need:
        return done(FeasibleStatus.INFEASIBLE_PROVEN, detail=f"at most {reachable} team(s) reachable, need {need}")

    picks: list[list[PoolRow]] = [[] for _ in groups]
    teams = Counter(teams0)
    nodes = 0

    def team_ok(g: int, left_in_group: int, start: int) -> bool:
        """Can the counting teams still reach `need` after the current pick?"""
        have = len(teams)
        if have >= need:
            return True
        _, cands, _ = groups[g]
        new = {r.team for r in cands[start:] if _counts(mode, r) and r.team not in teams}
        slots_left = left_in_group if any(_counts(mode, r) for r in cands[start:]) else 0
        new |= fut_teams[g + 1] - set(teams)
        return have + min(slots_left + fut_slots[g + 1], len(new)) >= need

    def dfs(g: int, start: int, left: int, salary: int) -> bool:
        nonlocal nodes
        if left == 0:
            g, start = g + 1, 0
            if g == len(groups):
                return len(teams) >= need
            left = groups[g][2]
        _, cands, _ = groups[g]
        pre = prefix[g]
        for j in range(start, len(cands)):
            nodes += 1
            if nodes % _CHECK_EVERY == 0 and time.perf_counter() > deadline:
                raise _Timeout
            m = left - 1  # positions left in this group after picking j
            if j + 1 + m > len(cands):
                break
            rest = (pre[j + 1 + m] - pre[j + 1]) + fut_cost[g + 1]
            r = cands[j]
            if salary + r.salary + rest > SALARY_CAP:
                break  # candidates are salary-sorted and rest only grows with j
            if r.person_key in used:
                continue
            used.add(r.person_key)
            counted = _counts(mode, r)
            if counted:
                teams[r.team] += 1
            picks[g].append(r)
            if team_ok(g, m, j + 1) and dfs(g, j + 1, m, salary + r.salary):
                return True
            picks[g].pop()
            if counted:
                teams[r.team] -= 1
                if teams[r.team] == 0:
                    del teams[r.team]
            used.discard(r.person_key)
        return False

    if time.perf_counter() > deadline:
        return done(FeasibleStatus.TIMEOUT, nodes=nodes, detail="budget spent before search")
    try:
        found = dfs(0, 0, groups[0][2], salary0) if groups else len(teams) >= need
    except _Timeout:
        return done(FeasibleStatus.TIMEOUT, nodes=nodes, detail=f"budget {budget_s}s spent")
    if not found:
        return done(FeasibleStatus.INFEASIBLE_PROVEN, nodes=nodes, detail="search space exhausted")

    out: list[str | None] = [None] * len(slots)
    for i, rid in locked.items():
        out[i] = rid
    for (s, _, _), chosen in zip(groups, picks):
        for k, r in zip(free_idx[s], chosen):
            out[k] = r.role_id
    lineup = [x for x in out if x is not None]
    check = check_lineup([pool.by_role_id[x] for x in lineup], mode)
    if not check.ok:  # a search bug, never shipped as a lineup
        raise AssertionError("feasible search produced an illegal lineup: " + "; ".join(check.reasons))
    return done(FeasibleStatus.FOUND, lineup, nodes)
