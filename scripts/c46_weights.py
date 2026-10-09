"""C46 (flags 47 and 49): the one-pass weights of the Classic stack mix with the team5 behavior, and the real-pool gate.

    python scripts/c46_weights.py                                   # measure each behavior alone on the stand-in pool and solve once
    python scripts/c46_weights.py --check --pool <DKSalaries.csv>   # the flag 49 gate on a real slate's salary file

Solve (flag 49, flag 40's method with one more behavior). Each of the eight behaviors is sampled alone (3,000 draws, seed
20261008, the vectorized sampler, the large_gpp objective) on the synthetic 6-team stand-in pool and its five shares are
recorded: 3+ stack, 4+, 5+, two 3+ stacks, 4-3-1. The field's shares are linear in the mixture, so the script searches the
weights on a 0.01 grid, the four non-stack behaviors held in their ratio (optimizer 30, stars_value 15, casual 20,
contrarian 10), for the least weighted sum of squared errors against the pooled Field row (93.5, 65.7, 21.3, 44.8, 31.4)
with the 3+, 4+ and 5+ errors counting 4 times, the other two once, and all three gated errors within 3 points. Ties go to
the first grid point in the walk order (stacker, stacker4, double_stack, stacker5 ascending). One pass: the printed YAML is
what goes into config/ownership.yaml `field.classic_mixtures_team5`; nothing here is iterated against the gate.

Check (flag 49). `behaviors_for_pool` with both switches forced on in a copy of the config, `sample_fast`, the salary-prior
projection, 5,000 draws, seed 20261008: exactly 5,000 draws, all legal with skaters from 3 teams, the 3+ share within 10
points of 93.5, the 4+ share within 10 of 65.7, the 5+ share within 10 of 21.3, at least one team3 and one team5 draw, at most
100 draws solved by the MILP after failing a vectorized check. Read-only: it reads the file and the config and prints.
Exit 0 on pass, 1 on a miss, 2 when no file was given.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c19_weights as c19  # noqa: E402  (the C19 script: shares, the real-pool reader and the targets)

SEED = c19.SEED
TARGETS = c19.TARGETS
KEYS = c19.KEYS
WEIGHT = {"stack3": 4.0, "stack4": 4.0, "stack5": 4.0, "two3": 1.0, "s431": 1.0}
GATED = ("stack3", "stack4", "stack5")
GATED_BOUND = 3.0  # points, on the stand-in
STACKERS = ("stacker", "stacker4", "double_stack", "stacker5")
NON_STACK = c19.NON_STACK
GATE_BOUND = 10.0  # points, on the real pool
MAX_FALLBACKS = 100  # draws of 5,000 solved by the MILP after a failed vectorized check (2 percent)


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
        out[name] = c19.shares(fld, pool)
        out[name]["_n"] = fld.n
        out[name]["_fallback"] = _fallbacks(fld)
    return out


def _fallbacks(fld) -> int:
    m = re.search(r"(\d+) draw\(s\) failed a check", "; ".join(fld.detail))
    return int(m.group(1)) if m else 0


def solve(vec: dict[str, dict[str, float]]):
    """(best, rows) for the grid: best = (sse, w3, w4, wd, w5, m, predicted, error) or None, rows = the number of grid
    points inside the bound. The grid is walked in a fixed order (w3, w4, wd, w5 ascending); a later point replaces the
    best only with a strictly smaller error."""
    import numpy as np

    ns_total = sum(NON_STACK.values())
    ns_vec = {k: sum(NON_STACK[b] / ns_total * vec[b][k] for b in NON_STACK) for k in KEYS}
    v = {k: [vec[b][k] for b in STACKERS] for k in KEYS}
    best = None
    inside = 0
    c, d = np.meshgrid(np.arange(101), np.arange(101), indexing="ij")  # (wd, w5), row-major = walk order
    for w3 in range(0, 101):
        for w4 in range(0, 101 - w3):
            room = 100 - w3 - w4
            ok_cd = (c + d) <= room
            m = room - c - d
            pred = {k: (w3 * v[k][0] + w4 * v[k][1] + c * v[k][2] + d * v[k][3] + m * ns_vec[k]) / 100 for k in KEYS}
            err = {k: pred[k] - TARGETS[k] for k in KEYS}
            ok = ok_cd.copy()
            for k in GATED:
                ok &= np.abs(err[k]) <= GATED_BOUND
            if not ok.any():
                continue
            inside += int(ok.sum())
            sse = sum(WEIGHT[k] * err[k] ** 2 for k in KEYS)
            sse = np.where(ok, sse, np.inf)
            idx = int(np.argmin(sse.reshape(-1)))  # the first minimum in row-major order
            i, j = divmod(idx, 101)
            if best is None or sse[i, j] < best[0]:
                best = (float(sse[i, j]), w3, w4, int(i), int(j), int(m[i, j]),
                        {k: float(pred[k][i, j]) for k in KEYS}, {k: float(err[k][i, j]) for k in KEYS})
    return best, inside


def yaml_block(w3, w4, wd, w5, m) -> str:
    """Whole-percent weights summing to exactly 1.00: the non-stack mass split in its ratio, remainder to the optimizer."""
    ns_total = sum(NON_STACK.values())
    ns = {b: round(m * NON_STACK[b] / ns_total) for b in NON_STACK}
    ns["optimizer"] += m - sum(ns.values())
    allw = {"optimizer": ns["optimizer"], "stacker": w3, "stacker4": w4, "double_stack": wd, "stacker5": w5,
            "stars_value": ns["stars_value"], "casual": ns["casual"], "contrarian": ns["contrarian"]}
    assert sum(allw.values()) == 100
    return "{" + ", ".join(f"{k}: {v / 100:.2f}" for k, v in allw.items()) + "}"


def gate_config(cfg: dict) -> dict:
    """A copy of `cfg` with both Classic mixture switches on (the flag 49 gate never reads the shipped switches)."""
    cfg = copy.deepcopy(cfg)
    cfg["field"]["classic_mixtures"]["enabled"] = True
    cfg["field"]["classic_mixtures_team5"]["enabled"] = True
    return cfg


def check(pool, cfg) -> bool:
    """The flag 49 gate on `pool`."""
    from nhl_dfs.contracts.geometry import Mode, check_lineup
    from nhl_dfs.models import field as fm
    from nhl_dfs.models import field_fast as ff
    from nhl_dfs.models import ownership
    from nhl_dfs.models.projection import PriorProjection

    cfg = gate_config(cfg)
    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, "large_gpp"))
    beh = fm.behaviors_for_pool("large_gpp", pool, cfg)
    fld = ff.sample_fast(pool, util, beh, 5000, SEED, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    if fld is None:
        print("GATE FAIL: the pool is not in the compact Classic form the vectorized sampler needs")
        return False
    legal = sum(check_lineup([pool.by_role_id[r] for r in lu], Mode.CLASSIC).ok for lu in fld.lineups)
    three_teams = sum(len(fm.skater_shape(pool, lu)) >= 3 for lu in fld.lineups)
    sh = c19.shares(fld, pool)
    mix = fm.stack_shape_mix(fld.lineups, pool)
    rule = {b.name: b.stack_rule for b in beh}
    t3 = sum(1 for b in fld.behavior_id if rule[b] == "team3")
    t5 = sum(1 for b in fld.behavior_id if rule[b] == "team5")
    fb = _fallbacks(fld)
    print(f"pool: {len(pool.rows)} rows, {len(pool.teams)} teams, {len(pool.games)} games, sha256 {pool.sha256[:12]}")
    print(f"mixture in force ({fm.classic_mixture_name('large_gpp', cfg)}): " + ", ".join(f"{b.name} {b.weight:.2f}" for b in beh))
    print(f"draws {fld.n} legal {legal} with 3 skater teams {three_teams}; detail: {'; '.join(fld.detail) or 'none'}; "
          f"MILP fallbacks {fb}; team3 draws {t3}; team5 draws {t5}")
    for k, label in (("stack3", "3+ stack"), ("stack4", "4+ stack"), ("stack5", "5+ stack"),
                     ("two3", "two 3+ stacks (reported)"), ("s431", "4-3-1 (reported)")):
        print(f"  {label}: {sh[k]:.1f}% (table {TARGETS[k]})")
    print(f"  6-1-1 (reported): {mix['shapes'].get('6-1-1', 0.0):.1f}% (table 6.3)")
    print("  top shapes: " + ", ".join(f"{s} {v:.1f}%" for s, v in list(mix["shapes"].items())[:8]))
    ok = (fld.n == 5000 and legal == fld.n and three_teams == fld.n and t3 >= 1 and t5 >= 1 and fb <= MAX_FALLBACKS
          and all(abs(sh[k] - TARGETS[k]) <= GATE_BOUND for k in GATED))
    print("GATE " + ("PASS" if ok else "FAIL") + " (flag 49: 5,000 legal draws on 3 skater teams, 3+, 4+ and 5+ within 10 points, "
          f"a team3 and a team5 draw present, at most {MAX_FALLBACKS} MILP fallbacks)")
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
        return 0 if check(c19.real_pool(a.pool), cfg) else 1

    from pool_builder import stand_in_pool

    pool = stand_in_pool(a.teams)
    vec = measure(pool, cfg, a.n)
    print(f"each behavior alone on the stand-in pool ({a.teams} teams, {a.n} draws, seed {SEED}); shares in percent")
    print(f"{'behavior':14s} " + " ".join(f"{k:>7s}" for k in KEYS) + "   draws  MILP")
    for b, v in vec.items():
        print(f"{b:14s} " + " ".join(f"{v[k]:7.1f}" for k in KEYS) + f"   {v['_n']}  {v['_fallback']}")
    print(f"{'target':14s} " + " ".join(f"{TARGETS[k]:7.1f}" for k in KEYS))
    best, inside = solve(vec)
    if best is None:
        print(f"NO GRID POINT keeps the three gated errors within {GATED_BOUND} points: the behaviors cannot reach the targets")
        return 1
    sse, w3, w4, wd, w5, m, pred, err = best
    print(f"grid points inside the bound: {inside}; best weighted SSE {sse:.2f}")
    print("predicted:     " + " ".join(f"{pred[k]:7.1f}" for k in KEYS))
    print("error:         " + " ".join(f"{err[k]:+7.1f}" for k in KEYS))
    print(f"weights: stacker (team3) {w3}%, stacker4 {w4}%, double_stack {wd}%, stacker5 {w5}%, non-stack {m}%")
    print("YAML: " + yaml_block(w3, w4, wd, w5, m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
