"""C19 (flags 40 and 46): the one-pass weights of the Classic stack mix, and the 2026-09-30 gate check.

    python scripts/c19_weights.py                      # measure each behavior alone on the stand-in pool and solve once
    python scripts/c19_weights.py --check --pool <DKSalaries.csv>   # the flag 46 gate on a real slate's salary file

Solve (flag 40). Each behavior is sampled alone (3,000 draws, seed 20261008, the vectorized sampler, the large_gpp
objective) and its five shares are recorded: 3+ stack, 4+, 5+, two 3+ stacks, 4-3-1. The field's shares are linear in the
mixture, so the script searches the weights on a 0.01 grid, the four non-stack behaviors held in their current ratio
(optimizer 30, stars_value 15, casual 20, contrarian 10), for the least weighted sum of squared errors against the pooled
Field row (93.5, 65.7, 21.3, 44.8, 31.4) with the 3+ and 4+ errors counting 4 times and both within 3 points. One pass:
the printed YAML is what goes into config/ownership.yaml; nothing here is iterated against the gate.

Check (flag 46). `behaviors_for_pool` with the switch on, `sample_fast`, the salary-prior projection, 5,000 draws, seed
20261008: exactly 5,000 draws, all legal, the 3+ share within 10 points of 93.5 and the 4+ share within 10 of 65.7, at
least one team3 draw. Read-only: it reads the file and the config and prints. Exit 0 on pass, 1 on a miss.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

SEED = 20261008
TARGETS = {"stack3": 93.5, "stack4": 65.7, "stack5": 21.3, "two3": 44.8, "s431": 31.4}
KEYS = tuple(TARGETS)
WEIGHT = {"stack3": 4.0, "stack4": 4.0, "stack5": 1.0, "two3": 1.0, "s431": 1.0}
GATED_BOUND = 3.0  # points, on the stand-in
STACKERS = ("stacker", "stacker4", "double_stack")
NON_STACK = {"optimizer": 30, "stars_value": 15, "casual": 20, "contrarian": 10}


def shares(fld, pool) -> dict[str, float]:
    from nhl_dfs.models.field import stack_shape_mix

    m = stack_shape_mix(fld.lineups, pool)
    return {"stack3": m["stack3"], "stack4": m["stack4"], "stack5": m["stack5"], "two3": m["two3"],
            "s431": m["shapes"].get("4-3-1", 0.0)}


def measure(pool, cfg, n: int) -> dict[str, dict[str, float]]:
    from nhl_dfs.models import field as fm
    from nhl_dfs.models import field_fast as ff
    from nhl_dfs.models import ownership
    from nhl_dfs.models.projection import PriorProjection

    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, "large_gpp"))
    out = {}
    for name in (*NON_STACK, *STACKERS):
        spec = cfg["field"]["behaviors"][name]
        b = fm.Behavior(name, 1.0, float(spec["noise_sd"]), spec["stack_rule"], spec["captain_rule"],
                        float(spec["salary_left_pref"]), float(spec.get("popularity_weight", 1.0)))
        fld = ff.sample_fast(pool, util, [b], n, SEED, "large_gpp", proj=proj, feats=feats, cfg=cfg)
        out[name] = shares(fld, pool)
        out[name]["_n"] = fld.n
    return out


def solve(vec: dict[str, dict[str, float]]):
    """(weights, predicted shares, error table) for the best grid point; the grid is walked in a fixed order."""
    ns_total = sum(NON_STACK.values())
    ns_vec = {k: sum(NON_STACK[b] / ns_total * vec[b][k] for b in NON_STACK) for k in KEYS}
    best = None
    rows = []
    for w3 in range(0, 101):
        for w4 in range(0, 101 - w3):
            for wd in range(0, 101 - w3 - w4):
                m = 100 - w3 - w4 - wd
                pred = {k: (w3 * vec["stacker"][k] + w4 * vec["stacker4"][k] + wd * vec["double_stack"][k] + m * ns_vec[k]) / 100
                        for k in KEYS}
                err = {k: pred[k] - TARGETS[k] for k in KEYS}
                if abs(err["stack3"]) > GATED_BOUND or abs(err["stack4"]) > GATED_BOUND:
                    continue
                sse = sum(WEIGHT[k] * err[k] ** 2 for k in KEYS)
                rows.append((sse, w3, w4, wd, m))
                if best is None or sse < best[0]:
                    best = (sse, w3, w4, wd, m, pred, err)
    if best is None:
        return None, rows
    return best, rows


def yaml_block(w3, w4, wd, m) -> str:
    """Whole-percent weights summing to exactly 1.00: the non-stack mass split in its ratio, remainder to the largest."""
    ns_total = sum(NON_STACK.values())
    ns = {b: round(m * NON_STACK[b] / ns_total) for b in NON_STACK}
    ns["optimizer"] += m - sum(ns.values())
    allw = {"optimizer": ns["optimizer"], "stacker": w3, "stacker4": w4, "double_stack": wd, "stars_value": ns["stars_value"],
            "casual": ns["casual"], "contrarian": ns["contrarian"]}
    assert sum(allw.values()) == 100
    return "{" + ", ".join(f"{k}: {v / 100:.2f}" for k, v in allw.items()) + "}"


def real_pool(path: Path):
    from nhl_dfs.build.run import _person_rows, pool_without, salary_statuses
    from nhl_dfs.contracts.statuses import Participation
    from nhl_dfs.intake.salary import read_salary

    p = read_salary(path)
    st = salary_statuses(p)
    out = {p.by_role_id[r].person_key for r, (q, _) in st.items() if q is Participation.OUT}
    return pool_without(p, _person_rows(p, out))


def check(pool, cfg) -> bool:
    """The flag 46 gate on `pool`, with the Classic mixtures switched on in a copy of the config."""
    from nhl_dfs.contracts.geometry import Mode, check_lineup
    from nhl_dfs.models import field as fm
    from nhl_dfs.models import field_fast as ff
    from nhl_dfs.models import ownership
    from nhl_dfs.models.projection import PriorProjection

    cfg = copy.deepcopy(cfg)
    cfg["field"]["classic_mixtures"]["enabled"] = True
    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, "large_gpp"))
    beh = fm.behaviors_for_pool("large_gpp", pool, cfg)
    fld = ff.sample_fast(pool, util, beh, 5000, SEED, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    if fld is None:
        print("GATE FAIL: the pool is not in the compact Classic form the vectorized sampler needs")
        return False
    legal = sum(check_lineup([pool.by_role_id[r] for r in lu], Mode.CLASSIC).ok for lu in fld.lineups)
    sh = shares(fld, pool)
    mix = fm.stack_shape_mix(fld.lineups, pool)
    rule = {b.name: b.stack_rule for b in beh}
    t3 = sum(1 for b in fld.behavior_id if rule[b] == "team3")
    print(f"pool: {len(pool.rows)} rows, {len(pool.teams)} teams, {len(pool.games)} games, sha256 {pool.sha256[:12]}")
    print(f"mixture in force: {', '.join(f'{b.name} {b.weight:.2f}' for b in beh)}")
    print(f"draws {fld.n} legal {legal}; detail: {'; '.join(fld.detail) or 'none'}; team3 draws {t3}")
    for k, label in (("stack3", "3+ stack"), ("stack4", "4+ stack"), ("stack5", "5+ stack (reported)"),
                     ("two3", "two 3+ stacks (reported)"), ("s431", "4-3-1 (reported)")):
        print(f"  {label}: {sh[k]:.1f}% (table {TARGETS[k]})")
    print("  top shapes: " + ", ".join(f"{s} {v:.1f}%" for s, v in list(mix["shapes"].items())[:7]))
    ok = (fld.n == 5000 and legal == fld.n and t3 >= 1 and abs(sh["stack3"] - TARGETS["stack3"]) <= 10
          and abs(sh["stack4"] - TARGETS["stack4"]) <= 10)
    print("GATE " + ("PASS" if ok else "FAIL") + " (flag 46: 5,000 legal draws, 3+ and 4+ within 10 points, a team3 draw present)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--teams", type=int, default=6)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pool", type=Path, default=None)
    a = ap.parse_args()

    from nhl_dfs.models import ownership

    cfg = ownership.load_ownership_config()
    if a.check:
        if a.pool is None or not a.pool.exists():
            print(f"GATE NOT RUN: no salary file at {a.pool}")
            return 2
        return 0 if check(real_pool(a.pool), cfg) else 1

    from pool_builder import stand_in_pool

    pool = stand_in_pool(a.teams)
    vec = measure(pool, cfg, a.n)
    print(f"each behavior alone on the stand-in pool ({a.teams} teams, {a.n} draws, seed {SEED}); shares in percent")
    print(f"{'behavior':14s} " + " ".join(f"{k:>7s}" for k in KEYS) + "   draws")
    for b, v in vec.items():
        print(f"{b:14s} " + " ".join(f"{v[k]:7.1f}" for k in KEYS) + f"   {v['_n']}")
    print(f"{'target':14s} " + " ".join(f"{TARGETS[k]:7.1f}" for k in KEYS))
    best, rows = solve(vec)
    if best is None:
        print(f"NO GRID POINT keeps both gated errors within {GATED_BOUND} points: the behaviors cannot reach the targets")
        return 1
    sse, w3, w4, wd, m, pred, err = best
    print(f"grid points inside the bound: {len(rows)}; best weighted SSE {sse:.2f}")
    print("predicted:     " + " ".join(f"{pred[k]:7.1f}" for k in KEYS))
    print("error:         " + " ".join(f"{err[k]:+7.1f}" for k in KEYS))
    print(f"weights: stacker (team3) {w3}%, stacker4 {w4}%, double_stack {wd}%, non-stack {m}%")
    print("YAML: " + yaml_block(w3, w4, wd, m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
