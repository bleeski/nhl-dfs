"""Many legal lineups from one objective (plan section 7 solver paragraph).

Each draw perturbs the objective with Gumbel noise and solves the MILP. Noise is drawn per
person, so a Showdown person's CPT and FLEX rows move together (the CPT row gets 1.5x the
person's draw, matching its 1.5x score). perturb_sd is the noise standard deviation in the
objective's own units (Gumbel scale = perturb_sd * sqrt(6) / pi).

distinct=True (portfolio use): each accepted lineup adds an overlap row, so every pair of
returned candidates differs by at least min_pairwise_diff units (persons in Classic, role IDs
in Showdown; see milp.overlap_units), and no key repeats. distinct=False (field use): draws
are independent and repeats are returned as drawn.

Candidate.objective_value is always the UNPERTURBED objective. The solver must be available;
callers route to feasible.find_one when milp.solver_available() is False.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from nhl_dfs.build.milp import GroupConstraint, InfeasibleInput, LineupModel, overlap_units, solver_available
from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.contracts.statuses import SearchStatus
from nhl_dfs.intake.salary import SalaryPool

BASE_FAMILY = "base"
_PER_SOLVE_CAP_S = 5.0
_MAX_CONSECUTIVE_ERRORS = 3


@dataclass(frozen=True)
class Candidate:
    role_ids: tuple[str, ...]  # canonical slot order
    key: str  # contracts.geometry.lineup_key
    objective_value: float  # unperturbed
    family: str  # groups_menu entry name, or "base"


class SolverUnavailable(RuntimeError):
    pass


def generate(
    pool: SalaryPool,
    mode: Mode,
    objective: Mapping[str, float],
    n: int,
    *,
    seed: int,
    perturb_sd: float,
    groups_menu: Sequence[tuple[str, Sequence[GroupConstraint]]] = (),
    time_limit_total_s: float = 20.0,
    distinct: bool = True,
    min_pairwise_diff: int = 2,
) -> list[Candidate]:
    """Up to n candidates. Fewer come back when the budget ends or every family is exhausted.

    groups_menu: (family name, group constraints) entries; draw i uses entry i mod len.
    An empty menu is one family, "base", with no groups.
    """
    import numpy as np

    if not solver_available():
        raise SolverUnavailable("scipy MILP unavailable; route to build.feasible.find_one")
    if n < 0 or perturb_sd < 0:
        raise ValueError("n and perturb_sd must be non-negative")
    t0 = time.perf_counter()
    deadline = t0 + time_limit_total_s
    size = 9 if mode is Mode.CLASSIC else 6
    if distinct and not 1 <= min_pairwise_diff <= size:
        raise ValueError(f"min_pairwise_diff must be in [1, {size}]")

    menu = list(groups_menu) or [(BASE_FAMILY, ())]
    models: dict[str, LineupModel | None] = {}
    for name, groups in menu:
        try:
            models[name] = LineupModel(pool, mode, groups=tuple(groups))
        except InfeasibleInput:
            models[name] = None
    exhausted = {name for name, m in models.items() if m is None}

    rng = np.random.default_rng(seed)
    persons = sorted({r.person_key for r in pool.rows})
    p_index = {p: i for i, p in enumerate(persons)}
    beta = perturb_sd * math.sqrt(6.0) / math.pi
    base = {r.role_id: float(objective.get(r.role_id, 0.0)) for r in pool.rows}
    captain = {r.role_id: mode is Mode.SHOWDOWN and "CPT" in r.roster_positions for r in pool.rows}

    out: list[Candidate] = []
    keys: set[str] = set()
    max_shared = size - min_pairwise_diff
    accepted: list[tuple[set[str], tuple[str, ...]]] = []  # (overlap units, lineup)
    active: list = []  # overlap rows in the model: only those some draw has violated
    in_model: set[int] = set()  # indices into accepted whose row is active
    errors = 0
    draw = 0
    while len(out) < n and len(exhausted) < len(menu):
        remaining = deadline - time.perf_counter()
        if remaining <= 0 or errors >= _MAX_CONSECUTIVE_ERRORS:
            break
        name, _ = menu[draw % len(menu)]
        draw += 1
        if name in exhausted:
            continue
        model = models[name]
        noise = rng.gumbel(0.0, beta, size=len(persons)) if beta > 0 else np.zeros(len(persons))
        perturbed = {
            rid: v + (1.5 if captain[rid] else 1.0) * float(noise[p_index[pool.by_role_id[rid].person_key]])
            for rid, v in base.items()
        }
        # Lazy pairwise-difference rows: solve, add the rows the answer violates, re-solve.
        # Exact: the accepted answer satisfies every row, and each solve relaxes the full
        # problem, so an optimum or an infeasibility of the relaxation holds for the full one.
        while True:
            remaining = deadline - time.perf_counter()
            res = model.solve(
                perturbed,
                overlaps=active if distinct else (),
                time_limit_s=max(min(_PER_SOLVE_CAP_S, remaining), 1e-3),
                score=base,
            )
            if not distinct or res.lineup is None:
                break
            units = overlap_units((pool.by_role_id[rid] for rid in res.lineup), mode)
            violated = [k for k, (u, _) in enumerate(accepted) if k not in in_model and len(u & units) > max_shared]
            if not violated:
                break
            for k in violated:
                # Group constraints add rows but never change the variables, so one overlap
                # row serves every family.
                active.append(model.overlap_row(accepted[k][1], max_shared))
                in_model.add(k)
        if res.status is SearchStatus.INFEASIBLE:
            exhausted.add(name)  # no further lineup satisfies this family's constraints
            continue
        if res.lineup is None:
            errors += 1
            continue
        errors = 0
        rows = [pool.by_role_id[rid] for rid in res.lineup]
        key = lineup_key(rows, mode)
        if distinct:
            if key in keys:  # the overlap rows forbid this; never return it if it happens
                errors += 1
                continue
            accepted.append((overlap_units(rows, mode), tuple(res.lineup)))
        keys.add(key)
        out.append(Candidate(tuple(res.lineup), key, float(res.objective_value), name))
    return out
