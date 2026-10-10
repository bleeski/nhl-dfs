"""C21 stack size study (flags 56 to 63, docs/experiments/stack_size_2026-10-10.md): for each stack shape, the modeled probability that a
lineup of that shape finishes in the top 1 percent, and whether 5-2-1 or 3-3-2 beats the 4-3-1 core on both real slates. The rule, the arms,
the streams, the error parts and the verdict mapping are preregistered; this script only runs them.

Two stages, so the study itself is a pure function of frozen caches:
  1. replay   the slate's saved inputs run offline through today's tree in scratch roots (`run_slate(scenario=True, offline=True)`); only its
              sampled large_gpp field LINEUPS are kept (an offline replay has no odds and no contest details, so its draws are not used).
  2. study    saved scenario cache S (draws, play mask, contests) scored against the replay field (decides) and against S's own saved field
              (reported, decides nothing); shapes forced with solver group rows and counted from role IDs.

    python scripts/c21_stack_study.py --out <scratch dir> --bed                       # the synthetic 6-team bed (cannot promote anything)
    python scripts/c21_stack_study.py --out <scratch dir> --saved-run <copy of runs\\<id>> --saved-run <copy of runs\\<id2>>   # B107, real caches

Set PYTHONHASHSEED=0 for the runs (PowerShell: $env:PYTHONHASHSEED="0"). Exit code 1 means a write other than the known observation-log append
was refused.

Data safety: the four NHL_DFS_* roots point into --out, the network is blocked and every write under the repo's data/, runs/, outputs/ or
BACKLOG.md is refused and counted (the guard of scripts/c17_replay.py). A saved run folder is COPIED into --out (inputs, versions, scenario,
manifest, current) before anything reads it. Nothing in src/ or config/ is changed by this chunk.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import shutil
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))

BED = REPO / "tests" / "fixtures" / "late_swap" / "classic"
BED_CLOCK = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SHAPES = {"4-3-1": (4, 3, 1), "5-2-1": (5, 2, 1), "6-1-1": (6, 1, 1), "3-3-2": (3, 3, 2)}  # preregistered
FREE = "free"
ARMS = (*SHAPES, FREE)
BASELINE = "4-3-1"
CHALLENGERS = ("5-2-1", "3-3-2")  # 6-1-1 is reported and never a challenger (B101)
LABEL_6_1_1 = "against a field that has none (B101)"
SEEDS = [20261010 + i for i in range(5)]  # preregistered
REPLAY_SEED = 20261010
T_CUT = 2.78  # 95% two-sided t cutoff, 4 degrees of freedom (preregistered, judgment)
PASS_REL, FAIL_REL, MDE_REL = 0.03, 0.01, 0.10
FIDELITY_TOL = 10.0  # points, the C46 gate's tolerance
SCENARIO_N = {
    "bed": {"design": 1000, "selection": 3000, "referee": 3000, "field_target": 1000},
    "bed_small": {"design": 300, "selection": 800, "referee": 800, "field_target": 400},
    "replay": {"design": 300, "selection": 800, "referee": 800, "field_target": 5000},
}


@dataclass
class Config:
    seeds: tuple[int, ...] = tuple(SEEDS)
    n_cand: int = 60
    n_primary: int = 6
    boot_scen: int = 200
    boot_field: int = 30
    best_k: int = 10
    gen_time_s: float = 600.0
    tuple_time_s: float = 30.0
    rng_seed: int = 20261010
    block: int = 100  # scenario rows per block when the field is re-weighted

    @classmethod
    def small(cls) -> "Config":
        return cls(seeds=tuple(SEEDS[:2]), n_cand=12, n_primary=3, boot_scen=20, boot_field=4, best_k=3, gen_time_s=60.0, tuple_time_s=10.0)


# -- shapes, counted from role IDs --------------------------------------------------------------------------------------

def shape_of(role_ids, pool) -> tuple[int, ...]:
    """Skaters per team of one Classic lineup, largest first, goalie excluded, counted from the role IDs themselves."""
    c: Counter = Counter()
    for rid in role_ids:
        row = pool.by_role_id[rid]
        if not row.is_goalie:
            c[row.team] += 1
    return tuple(sorted(c.values(), reverse=True))


def shape_name(shape: tuple[int, ...]) -> str:
    return "-".join(str(x) for x in shape)


def skater_roles(pool) -> dict[str, frozenset]:
    by: dict[str, set] = {}
    for r in pool.rows:
        if not r.is_goalie:
            by.setdefault(r.team, set()).add(r.role_id)
    return {t: frozenset(v) for t, v in by.items()}


def shape_groups(sk: dict[str, frozenset], need: dict[str, int]):
    """Group rows forcing exactly `need[team]` skaters of each named team and at most 1 of every other team. With the solver's rule of at
    least 3 skater teams this yields exactly the shape asked and no other (8 skaters: 6+1+1, 5+2+1, 4+3+1, 3+3+2)."""
    from nhl_dfs.build.milp import GroupConstraint

    return tuple(GroupConstraint(ids, need[t], need[t]) if t in need else GroupConstraint(ids, 0, 1) for t, ids in sorted(sk.items()))


def companion_tuples(shape: str, primary: str, teams: list[str]):
    """The team tuples that complete `shape` around its primary team (the largest stack; for 3-3-2 one of the two 3-stacks)."""
    others = [t for t in teams if t != primary]
    if shape == "6-1-1":
        return [(primary,)]
    if shape in ("5-2-1", "4-3-1"):
        return [(primary, u) for u in others]
    if shape == "3-3-2":
        return [(primary, u, w) for u in others for w in others if w != u]
    raise ValueError(shape)


def need_of(shape: str, tup: tuple[str, ...]) -> dict[str, int]:
    return dict(zip(tup, SHAPES[shape]))


# -- tuples: one per eligible primary team, the companions chosen by the best unperturbed objective ----------------------

def eligible_primaries(design, pool, n: int) -> list[str]:
    from nhl_dfs.build import portfolio as pf

    tp = pf.team_points(design, pool)
    return sorted(tp, key=lambda t: (-float(tp[t].mean()), t))[:n]


def rank_tuples(pool, objective: dict, shape: str, primaries: list[str], teams: list[str], rule: bool, time_limit_s: float) -> dict:
    """For each eligible primary team the best companion tuple for `shape`, ranked by `LineupModel.solve` called directly (not by
    candidates.generate: its overlap rows and budget would bend the ranking). Returns {"menu": [(name, groups)], "tuples": [...], "status": {...}}."""
    from nhl_dfs.build.milp import InfeasibleInput, LineupModel
    from nhl_dfs.contracts.geometry import Mode

    sk = skater_roles(pool)
    menu, rows, status = [], [], Counter()
    for primary in primaries:
        best = None
        for tup in companion_tuples(shape, primary, teams):
            groups = shape_groups(sk, need_of(shape, tup))
            try:
                model = LineupModel(pool, Mode.CLASSIC, groups=groups, avoid_own_goalie=rule)
            except InfeasibleInput:
                status["INFEASIBLE_MODEL"] += 1
                continue
            res = model.solve(objective, time_limit_s=time_limit_s)
            status[res.status.value] += 1
            if res.lineup is None:
                continue
            if shape_name(shape_of(res.lineup, pool)) != shape:  # the rows must give exactly the shape asked
                raise RuntimeError(f"{shape} {tup}: the solver returned shape {shape_name(shape_of(res.lineup, pool))}")
            key = (-float(res.objective_value), tup)
            if best is None or key < best[0]:
                best = (key, tup, groups, float(res.objective_value))
        if best is None:
            continue  # no feasible tuple around this primary: dropped for this shape, reported
        _, tup, groups, val = best
        menu.append((f"{shape}:{'/'.join(tup)}", groups))
        rows.append({"primary": primary, "tuple": list(tup), "objective": round(val, 3)})
    return {"menu": menu, "tuples": rows, "status": dict(status)}


# -- the scored world ----------------------------------------------------------------------------------------------------

def build_sets(cache, pool):
    """Selection and referee ScenarioSets with the play mask applied once over the FULL cached stream (the mask draws depend on the stream
    length), then the selection stream split in two: design (first half) and choosing (second half)."""
    from nhl_dfs.build import objectives as ob

    out = {}
    for purpose in ("selection", "referee"):
        out[purpose] = ob.scenario_set(cache.base(purpose, cache.n(purpose)), cache.person_keys, pool, purpose=purpose, seed=cache.seed,
                                       purpose_code=int(cache.meta["purposes"][purpose]["code"]), play_prob=cache.meta.get("play_prob"))
    sel = out["selection"]
    h = sel.n // 2
    return {"design": ob.ScenarioSet(sel.role_ids, sel.base[:h], "selection-design", sel.seed),
            "choose": ob.ScenarioSet(sel.role_ids, sel.base[h:], "selection-choose", sel.seed), "referee": out["referee"]}


def pick_contest(cache, risk_cfg) -> tuple[str | None, str]:
    """The large_gpp contest of the cache with the most opponents, in weighted field mode; (None, reason) when there is none."""
    cap = int(risk_cfg["objectives"]["sampled_max_opponents"])
    cands = [(cid, int(cache.n_opponents[cid])) for cid, c in sorted(cache.contests.items()) if c.family == "large_gpp" and cid in cache.n_opponents]
    if not cands:
        return None, "the cache holds no large_gpp contest"
    big = [x for x in cands if x[1] > cap]
    if not big:
        return None, f"no large_gpp contest has more than {cap} opponents (the field would be drawn per scenario, not weighted)"
    return sorted(big, key=lambda x: (-x[1], x[0]))[0][0], ""


class Scorer:
    """P(top 1%) per candidate per scenario against one weighted field: the engine's own rule (objectives._evaluate:
    top = clip(top_k - G, 0, T) / T with T = ties + 1), ranks from swap_objective.SortedField as late swap computes them."""

    def __init__(self, scen, spec, contest, risk_cfg, mode):
        from nhl_dfs.build.swap_objective import SortedField

        self.scen, self.spec, self.contest, self.cfg, self.mode = scen, spec, contest, risk_cfg, mode
        self.top_k = max(1, int(math.floor(float(risk_cfg["objectives"]["top_pct"]) * contest.field_size)))
        scores, weights = spec.scores(scen, mode)
        self.field_scores = scores
        self.sf = SortedField(scores, weights)

    def ranks(self, lineups):
        cand = self.scen.scores(lineups, self.mode).full()
        G, E = self.sf.ranks(cand)
        return cand, G, E

    def top(self, lineups) -> "np.ndarray":
        import numpy as np

        _, G, E = self.ranks(lineups)
        T = E + 1
        return (np.clip(self.top_k - G, 0, T) / T).astype(np.float32)

    def engine_p(self, lineups):
        """objectives.metrics_from_ranks(...).p_top1pct for the same candidates: the figure the engine itself reports."""
        from nhl_dfs.build import objectives as ob

        _, G, E = self.ranks(lineups)
        return ob.metrics_from_ranks(G, E, self.contest, self.cfg).p_top1pct

    def reps(self, cand, weights_list) -> "np.ndarray":
        """(R, K) mean P(top 1%) of each candidate under R field weight vectors over the same lineups, scenario rows in blocks, the field
        sorted once per block (objectives.ranks_multi)."""
        import numpy as np

        from nhl_dfs.build import objectives as ob

        S, K = cand.shape
        acc = np.zeros((len(weights_list), K))
        step = 100
        for a in range(0, S, step):
            b = min(S, a + step)
            res = ob.ranks_multi(cand[a:b], np.asarray(self.field_scores[a:b]), list(weights_list))
            for r, (G, E) in enumerate(res):
                T = E + 1
                acc[r] += (np.clip(self.top_k - G, 0, T) / T).astype(np.float32).sum(axis=0)
        return acc / S


# -- the study ----------------------------------------------------------------------------------------------------------

def sha(lineups) -> str:
    return hashlib.sha256(json.dumps([list(x) for x in lineups]).encode()).hexdigest()[:16]


def field_shares(lineups, pool) -> dict:
    """Percent of lineups with a team of at least 3, 4, 5 skaters and the share of each exact shape, counted from role IDs."""
    shapes = Counter(shape_of(lu, pool) for lu in lineups)
    n = max(1, len(lineups))
    pct = lambda k: round(100.0 * k / n, 2)  # noqa: E731
    return {"n": len(lineups), "stack3": pct(sum(c for s, c in shapes.items() if s and s[0] >= 3)),
            "stack4": pct(sum(c for s, c in shapes.items() if s and s[0] >= 4)),
            "stack5": pct(sum(c for s, c in shapes.items() if s and s[0] >= 5)),
            "six_one_one": pct(shapes.get((6, 1, 1), 0)),
            "top_shapes": {shape_name(s): pct(c) for s, c in shapes.most_common(7)}}


def fidelity(shares: dict, table: dict) -> tuple[bool, dict]:
    gaps = {k: round(shares[k] - float(table[k]), 2) for k in ("stack3", "stack4", "stack5")}
    return all(abs(v) <= FIDELITY_TOL for v in gaps.values()), gaps


def study(*, name: str, synthetic: bool, saved, saved_view, replay, cfg: Config, risk_cfg: dict, runtime: dict, log=print) -> dict:
    """Stage 2: a pure function of the frozen saved cache, its pool and the replay's cache. Returns the slate result."""
    import numpy as np

    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import own_goalie as og
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.build.run import pool_without
    from nhl_dfs.contracts.geometry import Mode

    t0 = time.perf_counter()
    res: dict = {"name": name, "synthetic": synthetic, "failed": None, "fidelity_ok": False, "challengers": {}, "label_6_1_1": LABEL_6_1_1}
    cid, why = pick_contest(saved, risk_cfg)
    if cid is None:
        res["failed"] = why
        return res
    contest = saved.contests[cid]
    n_opp = int(saved.n_opponents[cid])
    full_pool = saved_view.pool
    sets = build_sets(saved, full_pool)
    keep = set(sets["referee"].role_ids) & set(sets["design"].role_ids)
    pool = pool_without(full_pool, [r for r in full_pool.by_role_id if r not in keep])
    rule = bool(og.rule_families(risk_cfg, Mode.CLASSIC) & {"large_gpp"})
    res.update({"contest": {"id": cid, "family": contest.family, "field_size": contest.field_size, "n_opponents": n_opp,
                            "payout_source": contest.payout_source.value}, "own_goalie_rule": rule,
                "streams": {k: sets[k].n for k in ("design", "choose", "referee")}, "seed": saved.seed})

    # the replay field, its fidelity, and the saved field beside it
    fam_r = replay.families.get("large_gpp")
    if not fam_r or not fam_r[0]:
        res["failed"] = "the replay holds no large_gpp field"
        return res
    r_lus, r_ks = fam_r
    unknown = {rid for lu in r_lus for rid in lu if rid not in sets["referee"].col}
    if unknown:
        res["failed"] = f"{len(unknown)} replay field role IDs are not on the saved cache's person axis (the replay is not the same slate)"
        return res
    table = risk_cfg_table()
    res["replay_field"] = field_shares(r_lus, full_pool)
    res["fidelity_ok"], res["fidelity_gaps"] = fidelity(res["replay_field"], table)
    s_lus, s_ks = saved.families[saved.contest_family[cid]]
    res["saved_field"] = field_shares([lu for lu in s_lus if all(r in full_pool.by_role_id for r in lu)], full_pool)
    res["pooled_table"] = {k: table[k] for k in ("stack3", "stack4", "stack5")}
    spec_r = ob.field_spec(r_lus, r_ks, n_opp, risk_cfg, n_scenarios=sets["referee"].n, seed=saved.seed, salt=f"{cid}|referee")
    spec_s = ob.field_spec(s_lus, s_ks, n_opp, risk_cfg, n_scenarios=sets["referee"].n, seed=saved.seed, salt=f"{cid}|referee")
    if spec_r.mode != "weighted" or spec_s.mode != "weighted":
        res["failed"] = f"field mode {spec_r.mode}/{spec_s.mode}: the study scores weighted fields only"
        return res

    # tuples and candidates
    objective = pf.role_objective(sets["design"], pool)
    primaries = eligible_primaries(sets["design"], pool, cfg.n_primary)
    teams = sorted(skater_roles(pool))
    menus, tuple_report = {}, {}
    for shape in SHAPES:
        got = rank_tuples(pool, objective, shape, primaries, teams, rule, cfg.tuple_time_s)
        menus[shape], tuple_report[shape] = got["menu"], {"tuples": got["tuples"], "solve_status": got["status"]}
    res["primaries"] = primaries
    res["tuples"] = tuple_report
    res["tuple_counts"] = {s: len(m) for s, m in menus.items()}
    res["equal_depth"] = len(set(res["tuple_counts"].values())) == 1
    sd, mpd = float(runtime["candidates"]["perturb_sd_points"]), int(runtime["candidates"]["min_pairwise_diff"])
    lineups: dict[tuple, int] = {}
    arms: dict[str, dict[int, list[int]]] = {a: {} for a in ARMS}
    gen: dict = {}

    def add(lus):
        out = []
        for lu in lus:
            out.append(lineups.setdefault(tuple(lu), len(lineups)))
        return out

    def run_arm(arm, seed):
        menu = menus.get(arm, [])
        if arm != FREE and not menu:
            return []
        t = time.perf_counter()
        got = cand_mod.generate(pool, Mode.CLASSIC, objective, cfg.n_cand, seed=seed, perturb_sd=sd, groups_menu=menu,
                                time_limit_total_s=cfg.gen_time_s, min_pairwise_diff=mpd, avoid_own_goalie=rule)
        gen.setdefault(arm, {})[seed] = {"returned": len(got), "seconds": round(time.perf_counter() - t, 2)}
        lus = [g.role_ids for g in got]
        if arm in SHAPES:
            bad = [lu for lu in lus if shape_name(shape_of(lu, pool)) != arm]
            if bad:
                raise RuntimeError(f"{arm}: {len(bad)} of {len(lus)} candidates are not that shape (counted from role IDs)")
        return lus

    noise = {}
    for arm in (FREE, "5-2-1"):  # noise baseline first (B98): the same arm twice on identical inputs
        a, b = run_arm(arm, cfg.seeds[0]), run_arm(arm, cfg.seeds[0])
        noise[arm] = {"identical": a == b, "sha_first": sha(a), "sha_second": sha(b), "n": [len(a), len(b)], "_cols": (add(a), add(b))}
    for seed in cfg.seeds:
        for arm in ARMS:
            arms[arm][seed] = add(run_arm(arm, seed))
    res["generation"] = gen
    res["own_goalie_conflicts"] = sum(1 for lu in lineups if og.faces_own_goalie(lu, pool)) if rule else None
    order = sorted(lineups, key=lineups.get)
    K = len(order)
    log(f"{name}: {K} distinct candidates generated in {time.perf_counter() - t0:.1f}s; tuples per shape {res['tuple_counts']}", )

    # scoring: referee stream, replay field (decides)
    ref = Scorer(sets["referee"], spec_r, contest, risk_cfg, Mode.CLASSIC)
    top = ref.top(order)  # (S, K) float32
    p_ref = top.mean(axis=0, dtype=np.float64)
    res["plumbing"] = {"indicator_mean_vs_engine_max_abs_diff": float(np.abs(p_ref - ref.engine_p(order)).max())}
    for arm, v in noise.items():
        c1, c2 = v.pop("_cols")
        v["metric_first"], v["metric_second"] = (float(p_ref[c1].mean()) if c1 else None), (float(p_ref[c2].mean()) if c2 else None)
        v["metric_difference"] = None if not (c1 and c2) else abs(v["metric_first"] - v["metric_second"])
    res["noise_baseline"] = noise
    cho = Scorer(sets["choose"], spec_r, contest, risk_cfg, Mode.CLASSIC)
    p_cho = cho.top(order).mean(axis=0, dtype=np.float64)
    del cho
    rng = np.random.default_rng(cfg.rng_seed)
    cand_scores = sets["referee"].scores(order, Mode.CLASSIC).full()
    w_reps = [np.asarray(spec_r.weights, np.int64)] + [rng.multinomial(n_opp, spec_r.counts / spec_r.counts.sum()) for _ in range(cfg.boot_field)]
    reps = ref.reps(cand_scores, w_reps)  # (1 + R, K); row 0 is the field as saved
    res["plumbing"]["reps_base_vs_sorted_field_max_abs_diff"] = float(np.abs(reps[0] - p_ref).max())
    del cand_scores

    def arm_p(arm):
        return [float(np.mean(p_ref[arms[arm][s]])) for s in cfg.seeds if arms[arm][s]]

    table_arms = {}
    for arm in ARMS:
        ps = arm_p(arm)
        shapes = Counter(shape_name(shape_of(order[c], pool)) for s in cfg.seeds for c in arms[arm][s])
        n_all = max(1, sum(shapes.values()))
        table_arms[arm] = {"candidates_per_seed": [len(arms[arm][s]) for s in cfg.seeds], "mean_p_top1pct": (statistics.fmean(ps) if ps else None),
                           "seed_means": ps, "shape_mix_pct": {k: round(100.0 * v / n_all, 1) for k, v in shapes.most_common(6)},
                           "label": LABEL_6_1_1 if arm == "6-1-1" else ""}
    res["arms"] = table_arms
    base = arm_p(BASELINE)
    if len(base) != len(cfg.seeds):
        res["failed"] = f"the {BASELINE} baseline arm returned no candidates for some seed"
        return res
    res["mean_base"] = statistics.fmean(base)

    def compare(c: str) -> dict | None:
        """Gain of arm c over the baseline: seed-pooled difference with the three error parts, the best-k condition, per-seed signs."""
        d_seed, vec, rep_d, b10 = [], [], [], []
        for s in cfg.seeds:
            a, b = arms[c][s], arms[BASELINE][s]
            m = min(len(a), len(b))
            if m == 0:
                return None
            a, b = a[:m], b[:m]
            d_seed.append(float(p_ref[a].mean() - p_ref[b].mean()))
            vec.append(top[:, a].mean(axis=1, dtype=np.float64) - top[:, b].mean(axis=1, dtype=np.float64))
            rep_d.append(reps[1:, a].mean(axis=1) - reps[1:, b].mean(axis=1))
            k = min(cfg.best_k, m)
            ba = sorted(a, key=lambda i: (-p_cho[i], i))[:k]
            bb = sorted(b, key=lambda i: (-p_cho[i], i))[:k]
            b10.append(float(p_ref[ba].mean() - p_ref[bb].mean()))
        n = len(cfg.seeds)
        d = statistics.fmean(d_seed)
        se_seed = statistics.stdev(d_seed) / math.sqrt(n) if n > 1 else float("nan")
        v = np.mean(vec, axis=0)  # the pooled difference is linear in the scenario rows
        brng = np.random.default_rng(cfg.rng_seed)
        boot = [float(v[brng.integers(0, len(v), len(v))].mean()) for _ in range(cfg.boot_scen)]
        se_scen = statistics.stdev(boot) if len(boot) > 1 else float("nan")
        rd = np.mean(rep_d, axis=0)
        se_field = statistics.stdev(map(float, rd)) if len(rd) > 1 else float("nan")
        se = math.sqrt(sum(x * x for x in (se_seed, se_scen, se_field)))
        return {"d": d, "se": se, "se_seed": se_seed, "se_scen": se_scen, "se_field": se_field, "d_over_base": d / res["mean_base"],
                "best10_diff": statistics.fmean(b10), "seed_diffs": d_seed, "positive_seeds": sum(1 for x in d_seed if x > 0), "n_seeds": n}

    for c in CHALLENGERS:
        got = compare(c)
        if got is not None:
            res["challengers"][c] = got
    lab = compare("6-1-1")
    res["label_only_6_1_1"] = lab
    if res["challengers"]:
        res["mde"] = T_CUT * max(x["se"] for x in res["challengers"].values())

    # the saved field beside it (information only): the same candidates, a point estimate, no error parts
    sav = Scorer(sets["referee"], spec_s, contest, risk_cfg, Mode.CLASSIC)
    p_sav = sav.top(order).mean(axis=0, dtype=np.float64)
    del sav
    res["saved_field_condition"] = {
        "label": "built before C19 and C46; decides nothing",
        "arm_means": {a: (statistics.fmean(float(np.mean(p_sav[arms[a][s]])) for s in cfg.seeds if arms[a][s]) if any(arms[a][s] for s in cfg.seeds) else None) for a in ARMS}}
    # the published portfolio's shape mix (information only)
    pub = Counter(shape_name(shape_of(lu, full_pool)) for eid, lu in saved_view.lineups.items()
                  if saved_view.contest_of.get(eid) == cid and all(lu))
    n_pub = max(1, sum(pub.values()))
    res["published_shape_mix_pct"] = {k: round(100.0 * v / n_pub, 1) for k, v in pub.most_common(6)}
    res["ordering"] = sorted((a for a in ARMS if table_arms[a]["mean_p_top1pct"] is not None), key=lambda a: -table_arms[a]["mean_p_top1pct"])
    res["seconds"] = round(time.perf_counter() - t0, 1)
    return res


def risk_cfg_table() -> dict:
    from nhl_dfs.models import ownership

    return ownership.load_ownership_config()["field"]["classic_stack_table"]


# -- the preregistered verdict -------------------------------------------------------------------------------------------

def slate_status(slate: dict, challenger: str) -> str:
    """PASS / FAIL / INCONCLUSIVE / NO VERDICT for one challenger on one slate, the rule in its preregistered order."""
    ch = (slate.get("challengers") or {}).get(challenger)
    if slate.get("failed") or not slate.get("fidelity_ok") or ch is None or not slate.get("challengers"):
        return "NO VERDICT"
    base = float(slate["mean_base"])
    mde = T_CUT * max(float(x["se"]) for x in slate["challengers"].values())
    if mde > MDE_REL * base:
        return "INCONCLUSIVE"
    d, se = float(ch["d"]), float(ch["se"])
    if d >= T_CUT * se and d >= PASS_REL * base and float(ch["best10_diff"]) > 0:
        return "PASS"
    if d <= 0 or d < FAIL_REL * base:
        return "FAIL"
    return "INCONCLUSIVE"


def verdict(slates: list[dict]) -> dict:
    """The study verdict from per-slate results (see the preregistration). Pure: tests pin every branch."""
    status = {s["name"]: {c: slate_status(s, c) for c in CHALLENGERS} for s in slates}
    out = {"per_slate": status, "label_6_1_1": LABEL_6_1_1}
    real = [s for s in slates if not s["synthetic"]]
    if any(s["synthetic"] for s in slates):
        out["study"] = "NOT MEASURED on real caches (a synthetic slate is in the run; its statuses are shown and decide nothing)"
    elif len(real) < 2:
        out["study"] = "NOT MEASURED (fewer than two real slates)"
    elif any(all(status[s["name"]][c] == "NO VERDICT" for c in CHALLENGERS) for s in real):
        out["study"] = "NO VERDICT"
    else:
        passing = [c for c in CHALLENGERS if all(status[s["name"]][c] == "PASS" for s in real)]
        if passing:
            share = {c: min(float(s["challengers"][c]["d"]) / float(s["mean_base"]) for s in real) for c in passing}
            best = max(passing, key=lambda c: (round(share[c], 12), -CHALLENGERS.index(c)))
            out["study"], out["challenger"] = f"ACCEPT-TO-SHADOW({best})", best
        elif all(any(status[s["name"]][c] == "FAIL" for s in real) for c in CHALLENGERS):
            out["study"] = "REJECT"
        else:
            out["study"] = "INCONCLUSIVE"
    out["ships"] = "nothing: no sleeve is built in C21; only ACCEPT-TO-SHADOW on both real slates leads to a follow-up Ben opens"
    return out


# -- stage 1 and the runner ----------------------------------------------------------------------------------------------

def set_roots(out: Path) -> dict[str, str]:
    roots = {"NHL_DFS_RUNS_ROOT": out / "roots" / "runs", "NHL_DFS_OUTPUTS_ROOT": out / "roots" / "outputs",
             "NHL_DFS_LEDGER_ROOT": out / "roots" / "ledger", "NHL_DFS_BACKLOG": out / "roots" / "BACKLOG.md"}
    for k, v in roots.items():
        os.environ[k] = str(v)
        (v if k != "NHL_DFS_BACKLOG" else v.parent).mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in roots.items()}


def replay_run(salary: Path, entries: Path, clock: datetime, scenario_n: dict, out: Path, tag: str, seed: int = REPLAY_SEED):
    """Stage 1: the slate's inputs through today's tree, offline, in scratch roots. Returns the replay's RunDir."""
    from nhl_dfs.build.run import run_slate

    res = run_slate(salary, entries, offline=True, baseline_only=False, scenario=True, scenario_n=scenario_n, seed=seed,
                    out_root=out / "work" / tag / "runs", outputs_root=out / "work" / tag / "outputs", clock=lambda: clock)
    if not res.ok:
        raise RuntimeError(f"{tag}: the replay did not finish ({res.manifest.get('failed')})")
    return res.run


def copy_saved_run(src: Path, out: Path) -> tuple[Path, str]:
    """A scratch copy of a real run folder (only what the study reads); returns (runs root of the copy, run id)."""
    src = Path(src).resolve()
    dst = out / "saved" / src.name
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("inputs", "versions", "scenario"):
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name, dirs_exist_ok=True)
    for name in ("manifest.json", "current"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    return dst.parent, src.name


def load_view(runs_root: Path, run_id: str):
    from nhl_dfs.build import packet
    from nhl_dfs.build.state import open_run

    return packet.RunView(open_run(runs_root, run_id), runs_root)


def run_slate_study(name: str, synthetic: bool, saved_view, saved_cache, replay_cache, cfg: Config, log=print) -> dict:
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build.run import load_runtime_config

    return study(name=name, synthetic=synthetic, saved=saved_cache, saved_view=saved_view, replay=replay_cache, cfg=cfg,
                 risk_cfg=ob.load_risk_config(), runtime=load_runtime_config(), log=log)


def report_md(slates: list[dict], v: dict, meta: dict) -> str:
    L = [f"# C21 stack study ({meta['source']})", "", f"Study verdict: **{v['study']}**", f"Ships: {v['ships']}", ""]
    for s in slates:
        L += [f"## {s['name']} ({'SYNTHETIC BED, decides nothing' if s['synthetic'] else 'real cache, in-sample'})", ""]
        if s.get("failed"):
            L += [f"NO RESULT: {s['failed']}", ""]
            continue
        L += [f"Contest {s['contest']['id']} ({s['contest']['field_size']} entries, {s['contest']['n_opponents']} opponents, payouts {s['contest']['payout_source']}); "
              f"own-goalie rule {'on' if s['own_goalie_rule'] else 'off'}; streams {s['streams']}; fidelity gate {'passed' if s['fidelity_ok'] else 'FAILED'} "
              f"(gaps {s.get('fidelity_gaps')}); tuples per shape {s['tuple_counts']} (equal depth {s['equal_depth']})", "",
              "| Arm | Mean P(top 1%) | Candidates per seed | Shape mix of its candidates |", "|---|---:|---|---|"]
        for a in ARMS:
            x = s["arms"][a]
            m = "n/a" if x["mean_p_top1pct"] is None else f"{x['mean_p_top1pct']:.5f}"
            L.append(f"| {a} {x['label']} | {m} | {x['candidates_per_seed']} | {x['shape_mix_pct']} |")
        L += ["", "| Challenger | Gain over 4-3-1 | SE (seed, scenario, field) | Best-10 gain | Positive seeds | Status |", "|---|---:|---|---:|---|---|"]
        for c, x in s["challengers"].items():
            L.append(f"| {c} | {x['d']:+.5f} ({100 * x['d_over_base']:+.1f}%) | {x['se']:.5f} ({x['se_seed']:.5f}, {x['se_scen']:.5f}, {x['se_field']:.5f}) | "
                     f"{x['best10_diff']:+.5f} | {x['positive_seeds']} of {x['n_seeds']} | {v['per_slate'][s['name']][c]} |")
        if s.get("mde") is not None:
            L.append(f"\nMinimum detectable effect {s['mde']:.5f} ({100 * s['mde'] / s['mean_base']:.1f}% of the 4-3-1 mean).")
        L += [f"\nReplay field {s['replay_field']['top_shapes']} (3+ {s['replay_field']['stack3']}, 4+ {s['replay_field']['stack4']}, 5+ {s['replay_field']['stack5']}, "
              f"6-1-1 {s['replay_field']['six_one_one']}); saved field {s['saved_field']['top_shapes']} (3+ {s['saved_field']['stack3']}, 4+ {s['saved_field']['stack4']}, "
              f"5+ {s['saved_field']['stack5']}); published portfolio {s['published_shape_mix_pct']}; ordering {s['ordering']}", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="scratch folder (all writes go here)")
    ap.add_argument("--bed", action="store_true", help="the synthetic 6-team bed (cannot promote anything)")
    ap.add_argument("--saved-run", action="append", type=Path, default=[], help="a real run folder (copied into --out first); repeat for each slate")
    ap.add_argument("--small", action="store_true", help="the reduced configuration the tests use (NOT the preregistered sample)")
    args = ap.parse_args(argv)
    if not args.bed and not args.saved_run:
        ap.error("give --bed or at least one --saved-run")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = set_roots(out)
    import c17_replay as guard  # the repo's write guard and network block

    guard.install_guards(out)
    listing = os.popen("tasklist" if os.name == "nt" else "ps -eo pid,cmd").read().splitlines()
    others = [x.strip() for x in listing if ("python" in x.lower() or "pytest" in x.lower()) and "c21_stack_study" not in x]
    print(f"other python processes at start: {len(others)} {others[:3]}; PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}", flush=True)
    cfg = Config.small() if args.small else Config()
    slates, source = [], []
    if args.bed:
        from pool_builder import clone_entries

        entries = out / "bed_DKEntries.csv"
        clone_entries(BED / "DKEntries.template.csv", entries, 40)
        n = SCENARIO_N["bed_small" if args.small else "bed"]
        run = replay_run(BED / "DKSalaries.csv", entries, BED_CLOCK, n, out, "bed")
        view = load_view(run.path.parent, run.run_id)
        cache = view.cache
        if cache is None:
            raise RuntimeError("the bed replay wrote no scenario cache")
        print(f"bed replayed: run {run.run_id}", flush=True)
        slates.append(run_slate_study("synthetic bed", True, view, cache, cache, cfg, log=lambda m: print(m, flush=True)))
        source.append("synthetic bed (late_swap fixture, 40 entries): decides nothing")
    for src in args.saved_run:
        runs_root, rid = copy_saved_run(src, out)
        view = load_view(runs_root, rid)
        saved = view.cache
        if saved is None:
            slates.append({"name": rid, "synthetic": False, "failed": "the run holds no scenario cache", "fidelity_ok": False, "challengers": {}})
            continue
        created = json.loads((runs_root / rid / "manifest.json").read_text(encoding="utf-8"))["created_utc"]
        clock = datetime.fromisoformat(created.replace("Z", "+00:00"))
        run = replay_run(runs_root / rid / "inputs" / "DKSalaries.csv", runs_root / rid / "inputs" / "DKEntries.csv", clock, SCENARIO_N["replay"], out, rid)
        rview = load_view(run.path.parent, run.run_id)
        if rview.cache is None:
            slates.append({"name": rid, "synthetic": False, "failed": "the replay wrote no scenario cache", "fidelity_ok": False, "challengers": {}})
            continue
        print(f"{rid}: replayed as {run.run_id}", flush=True)
        slates.append(run_slate_study(rid, False, view, saved, rview.cache, cfg, log=lambda m: print(m, flush=True)))
        source.append(f"real run {rid} (in-sample if it is a 09-29 or 09-30 slate)")
    v = verdict(slates)
    meta = {"source": "; ".join(source) + (" [SMALL CONFIGURATION: not the preregistered sample]" if args.small else ""),
            "pythonhashseed": os.environ.get("PYTHONHASHSEED"), "other_python_at_start": others, "roots": roots, "seeds": list(cfg.seeds)}
    (out / "results.json").write_text(json.dumps({"meta": meta, "slates": slates, "verdict": v}, indent=1, default=str), encoding="utf-8")
    md = report_md(slates, v, meta)
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; roots: {roots}", flush=True)
    (out / "guard.json").write_text(json.dumps({"writes_refused": guard.WRITES, "network_blocked": guard.NET, "other_python_at_start": others}), encoding="utf-8")
    # the only write the offline run tries outside --out is the observation log (the known gap, refused and counted)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


if __name__ == "__main__":
    raise SystemExit(main())
