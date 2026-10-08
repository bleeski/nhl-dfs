"""C8 exit-check benchmark: field construction (5,000 lineups) plus payout evaluation for 150 candidates
x 20,000 scenarios within 60 s, and peak memory under the cap.

    python tests/bench_field.py

Not part of `pytest -m c8` (a wall-clock benchmark must not decide a unit test). Two pools:
  synthetic  a real-size Classic pool (8 teams, 288 rows) built here, so the check never depends on
             gitignored files; its scenarios are synthetic correlated scores (timing only).
  real       tests/fixtures/real/2026-09-29/classic when present (skipped, and said so, when absent),
             with scenarios simulated from the ParamTable (the simulation is timed separately, not
             counted: the check is about the field and the payouts).
The field is the vectorized Classic sampler (models/field_fast.py) the scenario pass uses to grow
GPP fields. Every field lineup is checked with contracts.geometry.check_lineup; the MILP agreement on
identical objectives is reported. Peak memory is the process peak working set (Windows
GetProcessMemoryInfo; ru_maxrss elsewhere) against config/sim.yaml memory_cap_mb. Exit code 0 only
when every pool meets the time, memory and legality checks.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402

BUDGET_S = 60.0
N_FIELD, N_CAND, N_SCEN = 5000, 150, 20000


def peak_mb() -> float:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e6
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3


def synthetic_pool():
    from dataclasses import replace

    from nhl_dfs.contracts.geometry import Mode
    from pool_builder import make_pool, row

    rng = random.Random(20260929)
    teams = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
    games = {t: f"{teams[2 * (i // 2)]}@{teams[2 * (i // 2) + 1]} 10/15/2026 0{7 + i // 2}:00PM ET" for i, t in enumerate(teams)}
    rows, n = [], 0
    for t in teams:
        for pos, k in (("C", 8), ("LW", 7), ("RW", 7), ("D", 11), ("G", 3)):
            for i in range(k):
                n += 1
                sal = rng.randrange(25, 96) * 100 if pos != "G" else rng.randrange(70, 90) * 100
                appg = max(0.5, (sal - 2000) / 700 + rng.gauss(0, 1.5))
                rows.append(replace(row(n, t, pos, sal, appg=round(appg, 1)), game_info=games[t]))
    return make_pool(Mode.CLASSIC, rows)


def real_pool():
    d = ROOT / "tests" / "fixtures" / "real" / "2026-09-29" / "classic"
    if not (d / "DKSalaries.csv").exists():
        return None
    from nhl_dfs.build.run import _person_rows, pool_without, salary_statuses
    from nhl_dfs.contracts.statuses import Participation
    from nhl_dfs.intake.salary import read_salary

    p = read_salary(d / "DKSalaries.csv")
    st = salary_statuses(p)
    out = {p.by_role_id[r].person_key for r, (q, _) in st.items() if q is Participation.OUT}
    return pool_without(p, _person_rows(p, out))


def synthetic_scenarios(pool, n):
    """Correlated synthetic scores (tenths): a team factor plus person noise; timing only."""
    from nhl_dfs.build import objectives as ob

    rng = np.random.default_rng(1)
    teams = sorted({r.team for r in pool.rows})
    tf = rng.gamma(4.0, 0.25, size=(n, len(teams)))
    means = np.asarray([pool_mean(r) for r in pool.rows])
    ti = np.asarray([teams.index(r.team) for r in pool.rows])
    base = rng.poisson(means[None, :] * tf[:, ti]).astype(np.int32)
    return ob.ScenarioSet([r.role_id for r in pool.rows], base, "selection", 1)


def pool_mean(r) -> float:
    return 10.0 * max(1.0, (r.appg_raw or 3.0))


def real_scenarios(pool, n, seed):
    from datetime import date

    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import scenario_pass
    from nhl_dfs.models import params
    from nhl_dfs.sim.slate import build_slate

    table = params.projection_for(pool, date(2026, 9, 29))
    slate, _ = build_slate(pool, table, None)
    t = time.perf_counter()
    base, keys = scenario_pass.simulate_base(slate, table, n, seed, "selection")
    sim_s = time.perf_counter() - t
    return ob.scenario_set(base, keys, pool, purpose="selection", seed=seed, purpose_code=1), table, sim_s


def bench(label: str, pool, proj, scen) -> bool:
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build.milp import GroupConstraint, LineupModel
    from nhl_dfs.contracts.geometry import Mode, check_lineup, lineup_key
    from nhl_dfs.contracts.statuses import PayoutSource
    from nhl_dfs.models import contests as contests_mod
    from nhl_dfs.models import field as fm
    from nhl_dfs.models import field_fast as ff
    from nhl_dfs.models import ownership
    from nhl_dfs.sim.market import load_sim_config

    risk = ob.load_risk_config()
    fam = contests_mod.load_contest_families()
    cap_mb = float(load_sim_config()["memory_cap_mb"])
    own_cfg = ownership.load_ownership_config()
    own_cfg["field"]["classic_mixtures"]["enabled"] = True  # C19: time the field a run builds once the mixtures are on
    feats = ownership.feature_table(pool, proj, None, cfg=own_cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(own_cfg, "large_gpp"))
    behaviors = fm.behaviors_for_pool("large_gpp", pool, own_cfg)

    # candidates (not timed): 150 distinct lineups from the central objective with noise
    central = fm.Behavior("central", 1.0, 2.0, "none", "projection", 0.0, 1.0)
    cf = ff.sample_fast(pool, util, [central], 400, 99, "cand", proj=proj, feats=feats, cfg=own_cfg)
    seen, cands = set(), []
    for lu, k in zip(cf.lineups, cf.keys):
        if k not in seen:
            seen.add(k)
            cands.append(lu)
    cands = cands[:N_CAND]

    t0 = time.perf_counter()
    fld = ff.sample_fast(pool, util, behaviors, N_FIELD, 7, "large_gpp", proj=proj, feats=feats, cfg=own_cfg)
    t_field = time.perf_counter() - t0
    spec = ob.field_spec(fld.lineups, fld.keys, N_FIELD - 1, risk, n_scenarios=scen.n, seed=7, salt="bench")
    prizes, seats, face, _ = ob.prior_curve("large_gpp", N_FIELD, 100, fam)
    ct = ob.Contest("bench", "bench", "large_gpp", N_FIELD, 100, prizes, seats, face, PayoutSource.PRIOR)
    t1 = time.perf_counter()
    cand_scores = scen.scores(cands, Mode.CLASSIC).full()
    fs, w = spec.scores(scen, Mode.CLASSIC)
    m = ob.contest_metrics(cand_scores, fs, w, ct, cfg=risk)
    t_pay = time.perf_counter() - t1
    total = t_field + t_pay

    legal = sum(1 for lu in fld.lineups if check_lineup([pool.by_role_id[r] for r in lu], Mode.CLASSIC).ok)
    # agreement with the MILP on identical perturbed objectives (first behavior, 100 draws; not timed)
    A = ff.ClassicArrays(pool)
    b = behaviors[0]
    obj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, b, own_cfg["field"]["captain_rules"])
    V = ff.perturbed(A, obj, 100, b.noise_sd, 5)
    got, _ = ff.solve_batch(A, V, np.full(100, -1))
    model = LineupModel(pool, Mode.CLASSIC)
    same, gaps = 0, []
    for i in range(100):
        res = model.solve({r: float(V[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
        if got[i] is None or res.lineup is None:
            continue
        vm = sum(V[i, A.ids.index(r)] for r in res.lineup)
        vf = sum(V[i, A.ids.index(r)] for r in got[i])
        same += lineup_key([pool.by_role_id[r] for r in got[i]], Mode.CLASSIC) == lineup_key(
            [pool.by_role_id[r] for r in res.lineup], Mode.CLASSIC)
        gaps.append((vm - vf) / abs(vm))
    stack_lines = []  # C19: the same agreement for the stack rules (flag 45), 40 draws each, team 0 (and team 1)
    for sb in (x for x in behaviors if x.stack_rule in ("team3", "team4", "double_stack")):
        k = 3 if sb.stack_rule == "team3" else 4
        dbl = sb.stack_rule == "double_stack"
        sobj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, sb, own_cfg["field"]["captain_rules"])
        SV = ff.perturbed(A, sobj, 40, sb.noise_sd, 5)
        sspec = None if sb.stack_rule == "team3" else ff.StackSpec(4, np.full(40, 1) if dbl else None, 3)
        sgot, sbad = ff.solve_batch(A, SV, np.full(40, 0), sspec)
        sk = {t: frozenset(r.role_id for r in pool.rows if r.team == A.team_names[t] and not r.is_goalie) for t in (0, 1)}
        groups = (GroupConstraint(role_ids=sk[0], min_count=k),) + ((GroupConstraint(role_ids=sk[1], min_count=3),) if dbl else ())
        smodel = LineupModel(pool, Mode.CLASSIC, groups=groups)
        same_s, gaps_s = 0, []
        for i in range(40):
            res = smodel.solve({r: float(SV[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
            if sgot[i] is None or res.lineup is None:
                continue
            vm = sum(SV[i, A.ids.index(r)] for r in res.lineup)
            vf = sum(SV[i, A.ids.index(r)] for r in sgot[i])
            same_s += lineup_key([pool.by_role_id[r] for r in sgot[i]], Mode.CLASSIC) == lineup_key(
                [pool.by_role_id[r] for r in res.lineup], Mode.CLASSIC)
            gaps_s.append((vm - vf) / abs(vm))
        stack_lines.append(f"{sb.name} {same_s}/{len(gaps_s)} identical, gap mean {np.mean(gaps_s):.2%} max {np.max(gaps_s):.2%}, "
                           f"{sbad} needing the MILP")
    pk = peak_mb()
    ok_time, ok_mem, ok_legal = total <= BUDGET_S, pk <= cap_mb, legal == fld.n and fld.n == N_FIELD
    print(f"[{label}] rows {len(pool.rows)}; field {fld.n} lineups ({len(set(fld.keys))} distinct) in {t_field:.1f} s "
          f"({'; '.join(fld.detail) or 'no MILP fallback'}); payout evaluation {len(cands)} candidates x {scen.n} scenarios "
          f"against {len(spec.lineups)} weighted lineups in {t_pay:.1f} s; total {total:.1f} s "
          f"({'within' if ok_time else 'OVER'} {BUDGET_S:.0f} s)")
    print(f"[{label}] peak working set {pk:.0f} MB ({'under' if ok_mem else 'OVER'} the {cap_mb:.0f} MB cap, config/sim.yaml "
          f"memory_cap_mb); payout chunks capped at {risk['objectives']['memory_cap_mb']} MB")
    print(f"[{label}] legal field lineups {legal}/{fld.n}; MILP agreement on identical objectives ({b.name}): "
          f"{same}/{len(gaps)} identical, value gap mean {np.mean(gaps):.4%} max {np.max(gaps):.4%}; "
          f"best candidate E[payout] ${m.exp_payout.max():.2f} (synthetic payout prior)")
    print(f"[{label}] field mixture in force: {', '.join(f'{x.name} {x.weight:.2f}' for x in behaviors)}")
    print(f"[{label}] stack-rule agreement with the MILP (40 draws, seed 5): " + "; ".join(stack_lines))
    return ok_time and ok_mem and ok_legal


def main() -> int:
    from nhl_dfs.models.projection import PriorProjection

    ok = True
    pool = synthetic_pool()
    ok &= bench("synthetic", pool, PriorProjection(pool), synthetic_scenarios(pool, N_SCEN))
    rp = real_pool()
    if rp is None:
        print("[real] SKIPPED: tests/fixtures/real/2026-09-29/classic is absent (gitignored); synthetic result stands")
    else:
        scen, table, sim_s = real_scenarios(rp, N_SCEN, 11)
        print(f"[real] simulated {N_SCEN} scenarios in {sim_s:.1f} s (not counted)")
        ok &= bench("real 2026-09-29 classic", rp, table, scen)
    print("BENCH " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
