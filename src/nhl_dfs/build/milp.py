"""Single-lineup MILP on SciPy/HiGHS (plan section 7 solver paragraph, section 10 ladder step 4).

One binary x[row, slot type] per (row, slot type) the row's roster positions accept, so
arbitrary eligibility works in both modes. Constraints: exact slot counts, one row per
person, salary cap, and team indicators (Classic: skaters span >= 3 teams; Showdown: >= 2
teams) over ALL slots, locked ones included. Locked rows are fixed with lower bound 1.

Status mapping (SciPy milp): 0 -> FEASIBLE; 1 with an incumbent -> TIME_LIMIT_WITH_INCUMBENT;
1 without one -> ERROR (a timeout is never infeasibility); 2 -> INFEASIBLE (of the submitted
problem, i.e. under the given excludes, locks, groups, and overlaps); 3, 4 -> ERROR. Every
returned lineup is re-validated with check_lineup; a lineup that fails is never returned.

scipy is imported lazily so this module imports, and solver_available() answers False,
when scipy is missing. Callers route to build.feasible.find_one in that case.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from nhl_dfs.contracts.geometry import (
    CLASSIC_SLOTS,
    SALARY_CAP,
    SHOWDOWN_SLOTS,
    Mode,
    PoolRow,
    check_lineup,
    slot_accepts,
)
from nhl_dfs.contracts.statuses import SearchStatus
from nhl_dfs.intake.salary import SalaryPool

MIN_TEAMS = {Mode.CLASSIC: 3, Mode.SHOWDOWN: 2}


@dataclass(frozen=True)
class GroupConstraint:
    """min_count <= (rows chosen from role_ids) <= max_count."""

    role_ids: frozenset[str]
    min_count: int = 0
    max_count: int | None = None


@dataclass
class SolveResult:
    status: SearchStatus
    lineup: list[str] | None  # role_ids in canonical slot order (CLASSIC_SLOTS / SHOWDOWN_SLOTS)
    objective_value: float | None  # sum of the supplied objective over the lineup
    elapsed_s: float
    solver_status_code: int | None
    detail: str = ""


def solver_available() -> bool:
    """True when scipy's MILP interface imports. Callers use feasible.find_one otherwise."""
    try:
        from scipy.optimize import milp  # noqa: F401
        from scipy.sparse import csr_array  # noqa: F401
    except ImportError:
        return False
    return True


def slots_for(mode: Mode) -> tuple[str, ...]:
    return CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS


def overlap_units(rows: Iterable[PoolRow], mode: Mode) -> set[str]:
    """What 'shared' means between two lineups: persons in Classic, role IDs in Showdown.

    In Showdown a captain swap (CPT A + FLEX B -> CPT B + FLEX A) changes two role IDs, so it
    counts as a difference of 2 even though the six people are the same.
    """
    if mode is Mode.CLASSIC:
        return {r.person_key for r in rows}
    return {r.role_id for r in rows}


class InfeasibleInput(Exception):
    """Locks that can never be legal (slot rejects the row, one person locked twice)."""


class LineupModel:
    """The fixed part of the MILP for one (pool, mode, exclude, locked, groups).

    Built once and solved many times with different objectives and overlap rows
    (candidates.generate reuses it). Use solve_lineup for a single solve.
    """

    def __init__(
        self,
        pool: SalaryPool,
        mode: Mode,
        *,
        exclude: frozenset[str] = frozenset(),
        locked: Mapping[int, str] | None = None,
        groups: Sequence[GroupConstraint] = (),
    ) -> None:
        from scipy.sparse import coo_array

        self.pool, self.mode = pool, mode
        self.slots = slots_for(mode)
        self.slot_types = list(dict.fromkeys(self.slots))
        counts = Counter(self.slots)
        self.locked = dict(locked or {})
        self._check_locks()
        locked_rids = {rid: self.slots[i] for i, rid in self.locked.items()}

        # Variables: (row, slot type). A locked row gets only its locked slot type, lb = 1.
        self.var_row: list[PoolRow] = []
        self.var_slot: list[str] = []
        lb: list[float] = []
        for r in pool.rows:
            if r.role_id in locked_rids:
                self.var_row.append(r)
                self.var_slot.append(locked_rids[r.role_id])
                lb.append(1.0)
                continue
            if r.role_id in exclude:
                continue
            for s in self.slot_types:
                if slot_accepts(s, r, mode):
                    self.var_row.append(r)
                    self.var_slot.append(s)
                    lb.append(0.0)
        n_x = len(self.var_row)

        # Team indicators y[t] for teams that can count toward the team rule.
        counts_team = [
            i for i in range(n_x) if mode is Mode.SHOWDOWN or not self.var_row[i].is_goalie
        ]
        self.teams = sorted({self.var_row[i].team for i in counts_team})
        team_ix = {t: n_x + k for k, t in enumerate(self.teams)}
        self.n_vars = n_x + len(self.teams)
        self.n_x = n_x
        self.lb = lb + [0.0] * len(self.teams)

        rows_i: list[int] = []
        cols_i: list[int] = []
        vals: list[float] = []
        cl: list[float] = []
        cu: list[float] = []

        def add(entries: Iterable[tuple[int, float]], lo: float, hi: float) -> None:
            k = len(cl)
            for j, v in entries:
                rows_i.append(k)
                cols_i.append(j)
                vals.append(v)
            cl.append(lo)
            cu.append(hi)

        inf = float("inf")
        for s in self.slot_types:
            add(((i, 1.0) for i in range(n_x) if self.var_slot[i] == s), counts[s], counts[s])
        by_person: dict[str, list[int]] = {}
        for i, r in enumerate(self.var_row):
            by_person.setdefault(r.person_key, []).append(i)
        for ixs in by_person.values():
            if len(ixs) > 1:
                add(((i, 1.0) for i in ixs), 0.0, 1.0)
        add(((i, float(self.var_row[i].salary)) for i in range(n_x)), 0.0, float(SALARY_CAP))
        for t, j in team_ix.items():
            # y[t] <= sum of counting x of team t
            add([(j, 1.0)] + [(i, -1.0) for i in counts_team if self.var_row[i].team == t], -inf, 0.0)
        add(((j, 1.0) for j in team_ix.values()), float(MIN_TEAMS[mode]), inf)
        for g in groups:
            hi = inf if g.max_count is None else float(g.max_count)
            add(((i, 1.0) for i in range(n_x) if self.var_row[i].role_id in g.role_ids), float(g.min_count), hi)

        self.A = coo_array((vals, (rows_i, cols_i)), shape=(len(cl), self.n_vars)).tocsr()
        self.cl, self.cu = cl, cu

    def _check_locks(self) -> None:
        seen: dict[str, int] = {}
        for i, rid in sorted(self.locked.items()):
            if not 0 <= i < len(self.slots):
                raise ValueError(f"locked slot index {i} out of range for {self.mode.value}")
            r = self.pool.by_role_id.get(rid)
            if r is None:
                raise ValueError(f"locked role {rid} is not a selectable row of this pool")
            if not slot_accepts(self.slots[i], r, self.mode):
                raise InfeasibleInput(f"locked slot {i} ({self.slots[i]}) rejects role {rid}")
            if r.person_key in seen:
                raise InfeasibleInput(f"person {r.person_key} locked in slots {seen[r.person_key]} and {i}")
            seen[r.person_key] = i

    def overlap_row(self, lineup: Sequence[str], max_shared: int) -> tuple[dict[int, float], float]:
        """Sparse row: shared units with `lineup` <= max_shared (see overlap_units)."""
        units = overlap_units((self.pool.by_role_id[rid] for rid in lineup), self.mode)
        key = (lambda r: r.person_key) if self.mode is Mode.CLASSIC else (lambda r: r.role_id)
        return {i: 1.0 for i, r in enumerate(self.var_row) if key(r) in units}, float(max_shared)

    def solve(
        self,
        objective: Mapping[str, float],
        *,
        overlaps: Sequence[tuple[dict[int, float], float]] = (),
        time_limit_s: float = 5.0,
        mip_rel_gap: float | None = None,
        score: Mapping[str, float] | None = None,
    ) -> SolveResult:
        """Maximize `objective`; report objective_value from `score` (defaults to objective)."""
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_array, vstack

        t0 = time.perf_counter()
        c = np.zeros(self.n_vars)
        for i, r in enumerate(self.var_row):
            c[i] = -float(objective.get(r.role_id, 0.0))
        A, cl, cu = self.A, list(self.cl), list(self.cu)
        if overlaps:
            ri, ci, vv = [], [], []
            for k, (entries, hi) in enumerate(overlaps):
                for j, v in entries.items():
                    ri.append(k)
                    ci.append(j)
                    vv.append(v)
                cl.append(-np.inf)
                cu.append(hi)
            extra = coo_array((vv, (ri, ci)), shape=(len(overlaps), self.n_vars))
            A = vstack([A, extra]).tocsr()
        options: dict = {"time_limit": max(float(time_limit_s), 1e-3), "disp": False}
        if mip_rel_gap is not None:
            options["mip_rel_gap"] = mip_rel_gap
        try:
            res = milp(
                c,
                constraints=LinearConstraint(A, cl, cu),
                integrality=np.ones(self.n_vars),
                bounds=Bounds(self.lb, np.ones(self.n_vars)),
                options=options,
            )
        except Exception as exc:  # solver crash is ERROR, never infeasibility
            return SolveResult(SearchStatus.ERROR, None, None, time.perf_counter() - t0, None, f"solver raised: {exc!r}")
        return self._result(res, score if score is not None else objective, t0)

    def _result(self, res, score: Mapping[str, float], t0: float) -> SolveResult:
        code = int(res.status)
        elapsed = time.perf_counter() - t0
        if code == 2:
            return SolveResult(SearchStatus.INFEASIBLE, None, None, elapsed, code, "infeasible under the submitted constraints")
        if code not in (0, 1) or res.x is None:
            why = "time limit with no incumbent" if code == 1 else f"solver status {code}: {res.message}"
            return SolveResult(SearchStatus.ERROR, None, None, elapsed, code, why)
        chosen = [i for i in range(self.n_x) if res.x[i] > 0.5]
        lineup = self.place(chosen)
        if lineup is None:
            return SolveResult(SearchStatus.ERROR, None, None, elapsed, code, "solution does not fill the slots")
        rows = [self.pool.by_role_id[rid] for rid in lineup]
        check = check_lineup(rows, self.mode)
        if not check.ok:
            return SolveResult(SearchStatus.ERROR, None, None, elapsed, code, "re-validation failed: " + "; ".join(check.reasons))
        status = SearchStatus.FEASIBLE if code == 0 else SearchStatus.TIME_LIMIT_WITH_INCUMBENT
        value = sum(float(score.get(rid, 0.0)) for rid in lineup)
        return SolveResult(status, lineup, value, elapsed, code)

    def place(self, chosen: Sequence[int]) -> list[str] | None:
        """Chosen variables -> role_ids in canonical slot order, locked rows at their indices."""
        out: list[str | None] = [None] * len(self.slots)
        for i, rid in self.locked.items():
            out[i] = rid
        locked_rids = set(self.locked.values())
        free = {s: [k for k, slot in enumerate(self.slots) if slot == s and out[k] is None] for s in self.slot_types}
        for i in sorted(chosen, key=lambda i: (self.var_slot[i], self.var_row[i].role_id)):
            rid = self.var_row[i].role_id
            if rid in locked_rids:
                continue
            spots = free[self.var_slot[i]]
            if not spots:
                return None
            out[spots.pop(0)] = rid
        if any(x is None for x in out):
            return None
        return out  # type: ignore[return-value]


def solve_lineup(
    pool: SalaryPool,
    mode: Mode,
    objective: Mapping[str, float],
    *,
    exclude: frozenset[str] = frozenset(),
    locked: Mapping[int, str] | None = None,
    groups: Sequence[GroupConstraint] = (),
    max_overlap_with: Sequence[tuple[Sequence[str], int]] = (),
    time_limit_s: float = 5.0,
) -> SolveResult:
    """Best legal lineup for `objective` (role_id -> value; missing ids count 0).

    locked: canonical slot index -> role_id; it wins over exclude (a started game's cell
    cannot change). max_overlap_with: (lineup role_ids, max shared units) pairs, where
    shared units are persons in Classic and role IDs in Showdown (overlap_units).
    """
    t0 = time.perf_counter()
    try:
        model = LineupModel(pool, mode, exclude=exclude, locked=locked, groups=groups)
    except InfeasibleInput as exc:
        return SolveResult(SearchStatus.INFEASIBLE, None, None, time.perf_counter() - t0, None, str(exc))
    overlaps = [model.overlap_row(lu, k) for lu, k in max_overlap_with]
    return model.solve(objective, overlaps=overlaps, time_limit_s=time_limit_s)
