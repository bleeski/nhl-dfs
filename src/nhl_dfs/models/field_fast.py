"""Vectorized Classic field sampler (C8): the same field model as models.field.sample, solved faster.

Every field draw is argmax over legal Classic lineups of (behavior objective + Gumbel person noise),
exactly the draw models.field.sample hands to the MILP. Here a batch of draws is solved at once:

1. Salary-priced greedy relaxation: for a price lam per dollar, take the goalie, the two best C,
   three best W, two best D and the best remaining skater (UTIL) by value - lam x salary, after
   forcing the stack team's best three skaters in when the behavior stacks (any three skaters fit
   C, C, UTIL / W, W, W / D, D, UTIL). A per-draw binary search finds the smallest price whose
   lineup fits the $50,000 cap.
2. Best-improvement single swaps on the true value, keeping the position minimums, the cap, three
   skater teams and the stack.
3. Every lineup is checked with contracts.geometry.check_lineup and the team and stack rules; a
   draw that fails any of them is solved by the MILP instead (counted in the detail).

It is exact for the relaxation and near-exact for the MILP: tests/test_field_fast.py and
tests/bench_field.py record how often it returns the MILP's own lineup on identical objectives and
the value gap. It needs the compact Classic form (every row exactly one of C / W / D / G, every
skater UTIL-eligible, one row per person), as the C2a compact MILP does; otherwise it returns None
and the caller uses the MILP sampler.
"""

from __future__ import annotations

import math
import time
import zlib
from collections import defaultdict
from typing import Mapping, Sequence

import numpy as np

from nhl_dfs.contracts.geometry import Mode, check_lineup, lineup_key
from nhl_dfs.intake.salary import SalaryPool

CAP = 50_000
NEG = -1e18
C, W, D, G = 0, 1, 2, 3
MIN = np.array([2, 3, 2])  # C, W, D minimums; 8 skaters; the surplus skater is UTIL


def _group(row) -> int | None:
    slots = set(row.roster_positions) - {"UTIL"}
    if row.is_goalie:
        return G if slots == {"G"} else None
    if "UTIL" not in row.roster_positions or len(slots) != 1:
        return None
    s = next(iter(slots))
    return {"C": C, "W": W, "LW": W, "RW": W, "D": D}.get(s)


class ClassicArrays:
    def __init__(self, pool: SalaryPool):
        self.pool = pool
        self.rows = list(pool.rows)
        self.ids = [r.role_id for r in self.rows]
        g = [_group(r) for r in self.rows]
        persons = [r.person_key for r in self.rows]
        self.ok = pool.mode is Mode.CLASSIC and None not in g and len(set(persons)) == len(persons)
        self.grp = np.asarray([x if x is not None else -1 for x in g])
        self.sal = np.asarray([r.salary for r in self.rows], float)
        teams = sorted({r.team for r in self.rows})
        self.team_names = teams
        self.team = np.asarray([teams.index(r.team) for r in self.rows])
        self.idx = {k: np.nonzero(self.grp == k)[0] for k in (C, W, D, G)}
        self.skaters = np.nonzero(self.grp != G)[0]
        self.T = len(teams)


def _greedy(A: ClassicArrays, U: np.ndarray, forced: np.ndarray) -> np.ndarray:
    """(B, R) bool: the best position-legal lineup by U given forced skaters (relaxation: no cap)."""
    B = U.shape[0]
    sel = forced.copy()
    for k in (C, W, D):
        cols = A.idx[k]
        fk = forced[:, cols].sum(axis=1)
        need = np.maximum(0, MIN[k] - fk)
        Uk = np.where(forced[:, cols], NEG, U[:, cols])
        order = np.argsort(-Uk, axis=1, kind="stable")
        ranks = np.empty_like(order)
        np.put_along_axis(ranks, order, np.arange(len(cols))[None, :].repeat(B, axis=0), axis=1)
        sel[:, cols] |= ranks < need[:, None]
    sk = A.skaters
    extra = 8 - sel[:, sk].sum(axis=1)
    Ur = np.where(sel[:, sk], NEG, U[:, sk])
    order = np.argsort(-Ur, axis=1, kind="stable")
    ranks = np.empty_like(order)
    np.put_along_axis(ranks, order, np.arange(len(sk))[None, :].repeat(B, axis=0), axis=1)
    sel[:, sk] |= ranks < extra[:, None]
    gcols = A.idx[G]
    gi = gcols[np.argmax(U[:, gcols], axis=1)]
    sel[np.arange(B), gi] = True
    return sel


def _forced(A: ClassicArrays, U: np.ndarray, stack_team: np.ndarray, k: int = 3) -> np.ndarray:
    """The stack team's best k skaters by U (stack_team -1: none)."""
    B = U.shape[0]
    forced = np.zeros(U.shape, bool)
    for t in np.unique(stack_team[stack_team >= 0]):
        rows = np.nonzero(stack_team == t)[0]
        cols = A.skaters[A.team[A.skaters] == t]
        order = np.argsort(-U[np.ix_(rows, cols)], axis=1, kind="stable")[:, :k]
        forced[rows[:, None], cols[order]] = True
    return forced


def _price_search(A: ClassicArrays, V: np.ndarray, stack_team: np.ndarray, iters: int = 26) -> np.ndarray:
    B = V.shape[0]

    def lineup(lam):
        U = V - lam[:, None] * A.sal[None, :]
        return _greedy(A, U, _forced(A, U, stack_team))

    lo = np.zeros(B)
    sel0 = lineup(lo)
    ok0 = (sel0 * A.sal).sum(axis=1) <= CAP
    hi = np.full(B, 1e-3)
    for _ in range(12):  # grow the upper price until every draw fits
        sal = (lineup(hi) * A.sal).sum(axis=1)
        if (sal <= CAP).all():
            break
        hi = np.where(sal > CAP, hi * 4, hi)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fits = (lineup(mid) * A.sal).sum(axis=1) <= CAP
        hi = np.where(fits, mid, hi)
        lo = np.where(fits, lo, mid)
    sel = lineup(hi)
    sel[ok0] = sel0[ok0]
    return sel


def _improve(A: ClassicArrays, V: np.ndarray, sel: np.ndarray, stack_team: np.ndarray, rounds: int = 6) -> np.ndarray:
    """Best-improvement single swaps on V keeping minimums, cap, >= 3 skater teams and the stack."""
    B, R = V.shape
    sel = sel.copy()
    ar = np.arange(B)
    for _ in range(rounds):
        idx = np.argsort(~sel, axis=1, kind="stable")[:, :9]  # the 9 selected columns
        vi = np.take_along_axis(V, idx, axis=1)  # (B, 9)
        gi = A.grp[idx]
        ti = A.team[idx]
        left = CAP - (sel * A.sal).sum(axis=1)
        cnt = np.stack([(sel[:, A.idx[k]]).sum(axis=1) for k in (C, W, D)], axis=1)  # (B, 3)
        tc = np.zeros((B, A.T), int)
        sk = sel.copy()
        sk[:, A.idx[G]] = False
        np.add.at(tc, (np.nonzero(sk)[0], A.team[np.nonzero(sk)[1]]), 1)
        distinct = (tc > 0).sum(axis=1)
        gain = V[:, None, :] - vi[:, :, None]  # (B, 9, R)
        ok = np.repeat(~sel[:, None, :], 9, axis=1)
        ok &= (A.sal[None, None, :] - A.sal[idx][:, :, None]) <= left[:, None, None]
        gj = A.grp[None, None, :]
        gii = gi[:, :, None]
        ok &= (gii == G) == (gj == G)
        for k in (C, W, D):  # minimum count of group k after the swap
            after = cnt[:, k][:, None, None] - (gii == k) + (gj == k)
            ok &= after >= MIN[k]
        tj = A.team[None, None, :]
        tii = ti[:, :, None]
        tci = np.take_along_axis(tc, ti, axis=1)[:, :, None]  # count of i's team
        tcj = tc[:, A.team][:, None, :]  # count of j's team
        same = tii == tj
        dist_after = distinct[:, None, None] - ((tci == 1) & ~same) + ((tcj == 0) & ~same)
        ok &= (gii == G) | (dist_after >= 3)
        has = stack_team >= 0
        if has.any():
            st = np.where(has, stack_team, 0)
            cst = tc[ar, st][:, None, None]
            after_s = cst - ((tii == st[:, None, None]) & (gii != G)) + ((tj == st[:, None, None]) & (gj != G))
            ok &= ~has[:, None, None] | (after_s >= 3)
        gain = np.where(ok, gain, 0.0)
        flat = gain.reshape(B, -1)
        best = flat.argmax(axis=1)
        bg = flat[ar, best]
        move = bg > 1e-9
        if not move.any():
            break
        i_pos, j = np.divmod(best, R)
        out_col = idx[ar, i_pos]
        rows = ar[move]
        sel[rows, out_col[move]] = False
        sel[rows, j[move]] = True
    return sel


def _canonical(A: ClassicArrays, cols: Sequence[int]) -> tuple[str, ...] | None:
    """Canonical slot order C, C, W, W, W, D, D, UTIL, G for one selected set."""
    rows = [A.rows[c] for c in cols]
    by = defaultdict(list)
    for r in sorted(rows, key=lambda r: r.role_id):
        by[_group(r)].append(r)
    try:
        cs, ws, ds, gs = by[C], by[W], by[D], by[G]
        util = (cs[2:] + ws[3:] + ds[2:])
        if len(util) != 1 or len(gs) != 1:
            return None
        lu = cs[:2] + ws[:3] + ds[:2] + util + gs
    except (IndexError, KeyError):
        return None
    return tuple(r.role_id for r in lu)


def _legal(A: ClassicArrays, lu: tuple[str, ...], stack: int) -> bool:
    rows = [A.pool.by_role_id[r] for r in lu]
    if not check_lineup(rows, Mode.CLASSIC).ok:
        return False
    if stack >= 0:
        return sum(1 for r in rows if not r.is_goalie and r.team == A.team_names[stack]) >= 3
    return True


def solve_batch(A: ClassicArrays, V: np.ndarray, stack_team: np.ndarray) -> tuple[list, int]:
    """Lineups (canonical role ids, or None where the relaxation failed a check) for (B, R) values."""
    sel = _price_search(A, V, stack_team)
    sel = _improve(A, V, sel, stack_team)
    out, bad = [], 0
    for b in range(V.shape[0]):
        cols = np.nonzero(sel[b])[0]
        lu = _canonical(A, cols) if len(cols) == 9 else None
        if lu is None or not _legal(A, lu, int(stack_team[b])):
            out.append(None)
            bad += 1
        else:
            out.append(lu)
    return out, bad


def _seed(seed: int, *parts: str) -> int:
    return zlib.crc32("|".join((str(seed),) + parts).encode("utf-8"))


def perturbed(A: ClassicArrays, obj: Mapping[str, float], k: int, noise_sd: float, seed: int) -> np.ndarray:
    """(k, R) values: objective plus one Gumbel draw per person per lineup (models.field / C2a noise)."""
    rng = np.random.default_rng(seed)
    beta = noise_sd * math.sqrt(6.0) / math.pi
    base = np.asarray([float(obj.get(r, 0.0)) for r in A.ids])
    noise = rng.gumbel(0.0, beta, size=(k, len(A.ids))) if beta > 0 else np.zeros((k, len(A.ids)))
    return base[None, :] + noise


def sample_fast(pool: SalaryPool, util: Mapping[str, float], behaviors, n: int, seed: int, contest_family: str, *,
                proj, feats=None, cfg=None, batch: int = 1000, milp_fallback: bool = True, time_limit_s: float = 60.0):
    """models.field.Field with the behaviors' draw counts of models.field.sample, or None when the pool is
    not in the compact Classic form. Draws that fail a check are re-solved by the MILP when allowed."""
    from nhl_dfs.build.milp import GroupConstraint, LineupModel
    from nhl_dfs.models import field as fm
    from nhl_dfs.models.ownership import feature_table, load_ownership_config

    A = ClassicArrays(pool)
    if not A.ok:
        return None
    cfg = cfg if cfg is not None else load_ownership_config()
    feats = feats if feats is not None else feature_table(pool, proj, cfg=cfg)
    t0 = time.perf_counter()
    counts = fm.split_counts([b.weight for b in behaviors], n)
    skaters_by_team = defaultdict(list)
    for r in pool.rows:
        if not r.is_goalie:
            skaters_by_team[r.team].append(r.role_id)
    stack_teams = sorted(t for t, rids in skaters_by_team.items() if len(rids) >= 3)
    team_total = {t: next((feats[rid]["implied_total"] for rid in skaters_by_team[t]), 0.0) for t in stack_teams}
    lineups, beh, detail = [], [], []
    fallback = 0
    for b, n_b in zip(behaviors, counts):
        if n_b == 0:
            continue
        obj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, b, cfg["field"]["captain_rules"])
        if b.stack_rule == "team3" and stack_teams:
            per = [(t, k) for t, k in zip(stack_teams, fm.split_counts([math.exp(team_total[t]) for t in stack_teams], n_b)) if k]
        else:
            per = [(None, n_b)]
        for team, k in per:
            for j, a in enumerate(range(0, k, batch)):
                m = min(batch, k - a)
                V = perturbed(A, obj, m, b.noise_sd, _seed(seed, b.name, team or "", f"fast{j}"))
                st = np.full(m, A.team_names.index(team) if team else -1)
                got, _ = solve_batch(A, V, st)
                for i, lu in enumerate(got):
                    if lu is None and milp_fallback:
                        groups = ()
                        if team:
                            groups = (GroupConstraint(role_ids=frozenset(skaters_by_team[team]), min_count=3),)
                        res = LineupModel(pool, Mode.CLASSIC, groups=groups).solve(
                            {r: float(V[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
                        lu = tuple(res.lineup) if res.lineup is not None else None
                        fallback += 1
                    if lu is not None:
                        lineups.append(lu)
                        beh.append(b.name)
    if fallback:
        detail.append(f"{fallback} draw(s) failed a check in the vectorized solve and were solved by the MILP")
    keys = [lineup_key([pool.by_role_id[r] for r in lu], Mode.CLASSIC) for lu in lineups]
    fld = fm.Field(contest_family, lineups, keys, beh, n, detail)
    fld.elapsed_s = time.perf_counter() - t0
    return fld
