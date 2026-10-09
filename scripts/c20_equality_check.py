"""C20: what must not change, as sha256 hashes of ordered output on fixed pools and seeds. Run once per tree and compare:

    python scripts/c20_equality_check.py --tree <origin/master export> > master.txt
    python scripts/c20_equality_check.py --tree <this checkout>        > branch.txt

Every key both trees print must be equal: the field sampler (sample_fast with and without the MILP fallback, field.sample, the
stand-in pool), candidates.generate as cash, satellite and Showdown callers call it (no rule asked), solve_lineup, discovery with no
rule asked, and a whole offline scenario run with both C20 keys off (the arm A0 of docs/experiments/goalie_leverage_2026-10-09.md).
Keys only this tree has (discovery with the rule asked) are printed for the next comparison against the previous commit.
--tree puts that tree's src and tests first on sys.path, so an export of origin/master can be run with this script. Writes nothing
outside a temp folder; the whole-run section is skipped with --skip-run.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", required=True, type=Path)
    ap.add_argument("--skip-run", action="store_true")
    args = ap.parse_args()
    tree = args.tree.resolve()
    sys.path.insert(0, str(tree / "tests"))
    sys.path.insert(0, str(tree / "src"))
    import numpy as np

    import nhl_dfs.build.objectives as ob
    from nhl_dfs.build import candidates as cand
    from nhl_dfs.build import milp
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.models import field as fm
    from nhl_dfs.models import field_fast as ff
    from nhl_dfs.models import ownership
    from nhl_dfs.models.projection import PriorProjection
    from pool_builder import stand_in_pool, varied_pool

    out = {"tree_src": ob.__file__}
    H = lambda k, m: int(hashlib.md5(k.encode()).hexdigest(), 16) % m  # noqa: E731
    cfg = ownership.load_ownership_config()
    pool = varied_pool(Mode.CLASSIC, seed=3)
    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, "large_gpp"))
    beh = fm.behaviors_for("large_gpp", cfg)
    out["sample_fast_nofallback"] = sha([list(l) for l in ff.sample_fast(pool, util, beh, 300, 11, "large_gpp", proj=proj, feats=feats, cfg=cfg, milp_fallback=False).lineups])
    out["sample_fast_fallback"] = sha([list(l) for l in ff.sample_fast(pool, util, beh, 300, 11, "large_gpp", proj=proj, feats=feats, cfg=cfg).lineups])
    out["field_sample_milp_path"] = sha([list(l) for l in fm.sample(pool, pool.mode, util, beh, 120, 5, "large_gpp", proj=proj, feats=feats, cfg=cfg).lineups])
    big = stand_in_pool(6)
    pb = PriorProjection(big)
    fb = ownership.feature_table(big, pb, None, cfg=cfg)
    ub = ownership.perceived(big, pb, fb, ownership.family_weights(cfg, "large_gpp"))
    out["sample_fast_standin"] = sha([list(l) for l in ff.sample_fast(big, ub, beh, 400, 13, "large_gpp", proj=pb, feats=fb, cfg=cfg).lineups])

    ls = read_salary(str(tree / "tests" / "fixtures" / "late_swap" / "classic" / "DKSalaries.csv"))
    obj = {r.role_id: H(r.role_id, 97) / 10.0 for r in ls.rows}
    goalies = sorted(r.role_id for r in ls.rows if r.is_goalie)
    menu = [(f"g{x}", (milp.GroupConstraint(frozenset({x}), min_count=1),)) for x in goalies[:5]] + [("base", ())]
    out["generate_classic_menu"] = sha([[c.family, list(c.role_ids)] for c in cand.generate(ls, Mode.CLASSIC, obj, 30, seed=3, perturb_sd=1.5, groups_menu=menu, time_limit_total_s=120.0)])
    out["generate_classic_base"] = sha([list(c.role_ids) for c in cand.generate(ls, Mode.CLASSIC, obj, 30, seed=4, perturb_sd=1.5, time_limit_total_s=120.0)])
    sd = varied_pool(Mode.SHOWDOWN, seed=2)
    objs = {r.role_id: H("s" + r.role_id, 89) / 10.0 for r in sd.rows}
    out["generate_showdown"] = sha([list(c.role_ids) for c in cand.generate(sd, Mode.SHOWDOWN, objs, 25, seed=5, perturb_sd=1.5, time_limit_total_s=120.0)])
    out["solve_lineup_classic"] = sha(milp.solve_lineup(ls, Mode.CLASSIC, obj).lineup)
    out["solve_lineup_showdown"] = sha(milp.solve_lineup(sd, Mode.SHOWDOWN, objs).lineup)

    ids = [r.role_id for r in ls.rows]
    rng = np.random.default_rng(42)
    base = np.zeros((600, len(ids)), np.int32)
    for j, r in enumerate(ls.rows):
        mu = (r.salary / 1000.0) * (1.4 if r.is_goalie else 1.0)
        base[:, j] = np.maximum(0, rng.normal(mu, 3.0 + (3.0 if r.is_goalie else 0.0), 600) * 10).astype(np.int32)
    design = ob.ScenarioSet(ids, base, "design", 1)
    risk = ob.load_risk_config()
    runtime = {"candidates": {"perturb_sd_points": 2.0, "time_limit_total_s": 120.0, "min_pairwise_diff": 2}}
    keys = sorted({r.person_key for r in ls.rows if r.is_goalie})
    has_rule = "avoid_own_goalie" in inspect.signature(pf.discover).parameters
    for rule in ((False, True) if has_rule else (False,)):
        kw = {"avoid_own_goalie": rule} if has_rule else {}
        got, _ = pf.discover(ls, design, 60, runtime, risk, seed=7, chalk_team="AAA", goalies=keys, **kw)
        out["discover_rule_" + ("on" if rule else "off") + ("" if rule else "_or_unasked")] = sha([[c.family, list(c.role_ids)] for c in got])

    if not args.skip_run:
        from nhl_dfs.build import packet
        from nhl_dfs.build.run import run_slate
        from pool_builder import clone_entries

        real = ob.load_risk_config

        def patched(*a, **k):
            c = real(*a, **k)
            if "own_goalie" in c:  # this tree has the rule: switch it off (arm A0); origin/master has no such key
                c["own_goalie"]["enabled"] = False
            return c

        ob.load_risk_config = patched
        bed = tree / "tests" / "fixtures" / "late_swap" / "classic"
        tmp = Path(tempfile.mkdtemp())
        many = tmp / "E.csv"
        clone_entries(bed / "DKEntries.template.csv", many, 40)
        clock = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        r = run_slate(bed / "DKSalaries.csv", many, offline=True, baseline_only=False, scenario=True, seed=20261009,
                      scenario_n={"design": 1000, "selection": 3000, "referee": 3000, "field_target": 1000},
                      out_root=tmp / "runs", outputs_root=tmp / "o", clock=lambda: clock)
        v = packet.RunView(r.run)
        out["whole_run_keys_off_lineups"] = hashlib.sha256(json.dumps([list(lu) for lu in v.lineups.values()]).encode()).hexdigest()[:16]
        out["whole_run_phases"] = [x["phase"] for x in r.manifest["versions"]]
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
