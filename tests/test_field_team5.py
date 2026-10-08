"""C46 (flags 47 and 49): the team5 rule, its own mixture table and switch, the solve and the real-pool gate.

Run with `pytest -m c46`. The real-pool gate reads the file named in NHL_DFS_C46_POOL and is skipped, loudly, without it."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from nhl_dfs.build.milp import GroupConstraint, LineupModel
from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.models import field as fm
from nhl_dfs.models import field_fast as ff
from nhl_dfs.models import ownership
from nhl_dfs.models.projection import PriorProjection
from pool_builder import stand_in_pool, varied_pool

pytestmark = pytest.mark.c46

CAP = 50_000


def skater_shape(pool, lineup) -> tuple[int, ...]:
    """Skaters per team, largest first, counted here from the role ids (never from the sampler's own helpers)."""
    c = Counter()
    for rid in lineup:
        row = pool.by_role_id[rid]
        if not row.is_goalie:
            c[row.team] += 1
    return tuple(sorted(c.values(), reverse=True))


def setup_for(pool, family="large_gpp"):
    proj = PriorProjection(pool)
    cfg = ownership.load_ownership_config()
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, family))
    return proj, cfg, feats, util


def beh(name, rule, weight=1.0, noise=3.0):
    return fm.Behavior(name, weight, noise, rule, "projection", 0.0, 1.0)


@pytest.fixture(scope="module")
def big():
    pool = stand_in_pool(6)
    return (pool, *setup_for(pool))


@pytest.fixture(scope="module")
def small():
    pool = varied_pool(Mode.CLASSIC, seed=3)
    return (pool, *setup_for(pool))


def legal(pool, lu) -> bool:
    rows = [pool.by_role_id[r] for r in lu]
    return check_lineup(rows, Mode.CLASSIC).ok and sum(r.salary for r in rows) <= CAP


def fast(setup, behaviors, n, seed=7, **kw):
    pool, proj, cfg, feats, util = setup
    return ff.sample_fast(pool, util, behaviors, n, seed, "large_gpp", proj=proj, feats=feats, cfg=cfg, **kw)


# -- the rule ---------------------------------------------------------------------------------------------------

def test_team5_draws_have_five_skaters_of_one_team_and_three_skater_teams(big):
    pool = big[0]
    f = fast(big, [beh("stacker5", "team5")], 600)
    assert f.n == 600, f.detail
    assert "dropped" not in " ".join(f.detail)
    for lu in f.lineups:
        s = skater_shape(pool, lu)
        assert legal(pool, lu) and s[0] >= 5 and len(s) >= 3  # 5-3, 6-2 and 7-1 are never drawn
    assert {skater_shape(pool, lu)[:2] for lu in f.lineups} <= {(5, 2), (5, 1), (6, 1)}


def test_team5_fallbacks_are_few(big):
    f = fast(big, [beh("stacker5", "team5")], 600)
    m = re.search(r"(\d+) draw\(s\) failed a check", " ".join(f.detail))
    assert m is None or int(m.group(1)) <= 30  # at most 5 percent of the draws went to the MILP (2 percent measured)


def test_team5_is_classic_only_and_a_typo_still_fails():
    sd = varied_pool(Mode.SHOWDOWN, seed=0)
    with pytest.raises(ValueError, match="Classic only"):
        fm.stack_jobs("team5", Mode.SHOWDOWN, 5, sd, {}, {})
    with pytest.raises(ValueError, match="unknown stack rule"):
        fm.stack_jobs("team6", Mode.CLASSIC, 5, sd, {}, {})
    cfg = copy.deepcopy(ownership.load_ownership_config())
    cfg["field"]["behaviors"]["stacker5"]["stack_rule"] = "team5"
    ownership.validate_ownership_config(cfg)
    cfg["field"]["behaviors"]["stacker5"]["stack_rule"] = "team55"
    with pytest.raises(ValueError, match="stack_rule must be one of"):
        ownership.validate_ownership_config(cfg)


def test_team5_jobs_split_the_draws_over_teams_with_five_skaters(big):
    pool = big[0]
    sk = {}
    for r in pool.rows:
        if not r.is_goalie:
            sk.setdefault(r.team, []).append(r.role_id)
    sk["AAA"] = sk["AAA"][:4]  # one team too short for a 5-stack
    total = {t: 0.0 for t in sk}
    total["BBB"] = 1.0
    jobs, note = fm.stack_jobs("team5", Mode.CLASSIC, 100, pool, sk, total)
    assert note is None and sum(k for _, k in jobs) == 100
    teams = [reqs[0][0] for reqs, _ in jobs]
    assert "AAA" not in teams and all(reqs[0][1] == 5 for reqs, _ in jobs)
    per = {reqs[0][0]: k for reqs, k in jobs}
    assert per["BBB"] == max(per.values())  # the team with the higher implied total gets more draws
    jobs, note = fm.stack_jobs("team5", Mode.CLASSIC, 7, pool, {}, {})
    assert jobs == [((), 7)] and note == "no team has 5 skaters; 7 draws unstacked"


def test_forcing_five_never_makes_an_uncompletable_set():
    pool = stand_in_pool(6)
    A = ff.ClassicArrays(pool)
    cols0 = A.skaters[A.team[A.skaters] == 0]
    # four centers and a winger rank first: at most 3 centers can ever be forced (C, C and the UTIL)
    U = np.zeros((1, A.R))
    U[0, cols0[A.grp[cols0] == ff.C][:4]] = 100.0 - np.arange(4)
    U[0, cols0[A.grp[cols0] == ff.W][:2]] = 10.0
    forced, block = ff._force(A, U, np.array([0]), ff.StackSpec(5))
    cols = np.nonzero(forced[0])[0]
    c, w, d = (int((A.grp[cols] == g).sum()) for g in (ff.C, ff.W, ff.D))
    assert block is None and forced.sum() == 5 and forced[0, cols0].sum() == 5
    assert c <= 3 and max(c, 2) + max(w, 3) + max(d, 2) <= 8


def test_the_milp_sampler_draws_obey_team5(small):
    pool, proj, cfg, feats, util = small
    fld = fm.sample(pool, Mode.CLASSIC, util, [beh("stacker5", "team5")], 12, 5, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    assert fld.n == 12 and not fld.degraded
    for lu in fld.lineups:
        assert legal(pool, lu) and skater_shape(pool, lu)[0] >= 5


# -- the two samplers agree on team5 (flag 49, flag 45's rule extended) --------------------------------------------

_AGREE: dict = {}


def agreement(which: str, seed: int):
    """Identical perturbed objectives, 40 draws, team 0 with a minimum of 5: (draws the vectorized solve could not
    finish, mean relative value gap to the MILP). Asserts legality and that no vectorized lineup beats the MILP.
    Cached: the 40 MILP solves are the cost."""
    if (which, seed) in _AGREE:
        return _AGREE[(which, seed)]
    pool = stand_in_pool(6) if which == "big" else varied_pool(Mode.CLASSIC, seed=3)
    proj, cfg, feats, util = setup_for(pool)
    A = ff.ClassicArrays(pool)
    assert A.ok
    b = beh("stacker5", "team5")
    obj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, b, cfg["field"]["captain_rules"])
    V = ff.perturbed(A, obj, 40, b.noise_sd, seed)
    got, bad = ff.solve_batch(A, V, np.full(40, 0), ff.StackSpec(5))
    sk0 = frozenset(r.role_id for r in pool.rows if r.team == A.team_names[0] and not r.is_goalie)
    model = LineupModel(pool, Mode.CLASSIC, groups=(GroupConstraint(role_ids=sk0, min_count=5),))
    gaps = []
    for i in range(40):
        if got[i] is None:
            continue
        assert legal(pool, got[i]) and skater_shape(pool, got[i])[0] >= 5
        res = model.solve({r: float(V[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
        vm = sum(V[i, A.ids.index(r)] for r in res.lineup)
        vf = sum(V[i, A.ids.index(r)] for r in got[i])
        assert vf <= vm + 1e-6  # a legal lineup is never better than the optimum
        gaps.append((vm - vf) / abs(vm))
    _AGREE[(which, seed)] = (bad, float(np.mean(gaps)))
    return _AGREE[(which, seed)]


@pytest.mark.parametrize("which", ["small", "big"])
def test_team5_vectorized_lineups_rarely_need_the_milp(which):
    bad, _ = agreement(which, 3)
    assert bad <= 2


@pytest.mark.parametrize("which", ["small", "big"])
def test_team5_mean_value_gap_to_the_milp_is_under_one_percent(which):
    """Flag 49 as written: team 0, 40 draws, seed 3 on the two pools of the flag 45 test."""
    assert agreement(which, 3)[1] < 0.01


@pytest.mark.parametrize("seed", [23, 41, 77])
@pytest.mark.parametrize("which", ["small", "big"])
def test_team5_fresh_seeds_are_legal_and_never_beat_the_milp(which, seed):
    """Reported, not gated (flag 49): the gaps print with -s. Legality and the optimum bound are asserted inside."""
    bad, gap = agreement(which, seed)
    print(f"team5 {which} seed {seed}: {bad} of 40 to the MILP, mean gap {100 * gap:.2f} percent")


# -- the older rules are unchanged -------------------------------------------------------------------------------

GOLDEN_C19_MIX_SHA256 = "b827282fec362cbb5ca7558e96e2ff71c4f85339b6f0a125a448e7f06b72e335"  # origin/master d3b788d (C19 merged)


def test_the_c19_mixture_gives_the_same_vectorized_lineups_as_before_c46():
    """Flag 49: varied_pool(CLASSIC, seed=3), n=300, seed 11, large_gpp, the shipped C19 table (team3, team4, double
    stack and the four others), MILP fallback off. The expected hash was computed on a clean export of origin/master
    d3b788d before any C46 edit and equals this tree's. If numpy changes its Generator stream this test fails for that
    reason alone: say so, do not edit the hash to match new code."""
    pool = varied_pool(Mode.CLASSIC, seed=3)
    proj, cfg, feats, util = setup_for(pool)
    on = copy.deepcopy(cfg)
    on["field"]["classic_mixtures"]["enabled"] = True
    on["field"]["classic_mixtures_team5"]["enabled"] = False
    f = ff.sample_fast(pool, util, fm.behaviors_for("large_gpp", on, mode=Mode.CLASSIC, games=None), 300, 11, "large_gpp",
                       proj=proj, feats=feats, cfg=on, milp_fallback=False)
    h = hashlib.sha256()
    for lu, b in zip(f.lineups, f.behavior_id):
        h.update((",".join(lu) + "|" + b + "\n").encode())
    assert f.n == 298
    assert h.hexdigest() == GOLDEN_C19_MIX_SHA256


# -- the second switch and its table (flag 49) -------------------------------------------------------------------

def cfg_copy(c19_on: bool, team5_on: bool):
    cfg = copy.deepcopy(ownership.load_ownership_config())
    cfg["field"]["classic_mixtures"]["enabled"] = c19_on
    cfg["field"]["classic_mixtures_team5"]["enabled"] = team5_on
    return cfg


def weights(behaviors) -> dict:
    return {b.name: b.weight for b in behaviors}


def shipped_team5():
    return {k: v for k, v in ownership.load_ownership_config()["field"]["classic_mixtures_team5"].items() if k != "enabled"}


def test_the_team5_switch_ships_on_after_the_gate_and_the_c19_table_is_untouched():
    cfg = ownership.load_ownership_config()
    assert cfg["field"]["classic_mixtures_team5"]["enabled"] is True  # switched on 2026-10-08 after the flag 49 gate passed
    assert cfg["field"]["classic_mixtures"]["large_gpp"]["default"] == {
        "optimizer": 0.07, "stacker": 0.21, "stacker4": 0.31, "double_stack": 0.31, "stars_value": 0.03, "casual": 0.05,
        "contrarian": 0.02}


def test_team5_off_is_the_c19_table_even_when_the_team5_table_is_filled():
    pool = stand_in_pool(6)
    c19_default = {"optimizer": 0.07, "stacker": 0.21, "stacker4": 0.31, "double_stack": 0.31, "stars_value": 0.03,
                   "casual": 0.05, "contrarian": 0.02}  # the weights C19 shipped, written here and not read from the YAML
    assert set(shipped_team5()) == {"large_gpp", "small_field"}  # the table is filled
    for fam in ("large_gpp", "small_field"):
        off = cfg_copy(True, False)
        got = weights(fm.behaviors_for_pool(fam, pool, off))
        assert got == pytest.approx(c19_default) and "stacker5" not in got and fm.classic_mixture_name(fam, off) == "classic"


def test_team5_on_uses_its_table_only_with_the_c19_switch_on_and_only_for_listed_families():
    pool = stand_in_pool(6)
    sd = varied_pool(Mode.SHOWDOWN, seed=0)
    both = cfg_copy(True, True)
    for fam, table in shipped_team5().items():
        got = weights(fm.behaviors_for_pool(fam, pool, both))
        assert got == pytest.approx({k: v for k, v in table["default"].items()}) and got["stacker5"] > 0
        assert fm.classic_mixture_name(fam, both) == "team5"
        assert weights(fm.behaviors_for_pool(fam, sd, both)) == weights(fm.behaviors_for(fam, both))  # Showdown never moves
        off = cfg_copy(False, True)  # the C19 switch is off: the old mixture, whatever the team5 switch says
        assert weights(fm.behaviors_for_pool(fam, pool, off)) == weights(fm.behaviors_for(fam, off))
        assert fm.classic_mixture_name(fam, off) == "old"
    for fam in both["field"]["mixtures"]:
        if fam not in shipped_team5():  # a family without a team5 table keeps what it had
            assert weights(fm.behaviors_for_pool(fam, pool, both)) == weights(fm.behaviors_for_pool(fam, pool, cfg_copy(True, False)))


def test_the_team5_table_is_validated_like_the_c19_one():
    base = cfg_copy(True, True)
    base["field"]["classic_mixtures_team5"]["large_gpp"] = {"default": {"optimizer": 0.5, "stacker5": 0.5}}
    ownership.validate_ownership_config(base)
    for edit, why in [
        (lambda m: m["large_gpp"].pop("default"), "default bucket"),
        (lambda m: m["large_gpp"].update({"default": {"optimizer": 0.5}}), "sum to 1"),
        (lambda m: m["large_gpp"].update({"default": {"nobody": 1.0}}), "unknown behaviors"),
        (lambda m: m["large_gpp"].update({"1-3": {"optimizer": 1.0}, "3-5": {"optimizer": 1.0}, "default": {"optimizer": 1.0}}), "overlap"),
        (lambda m: m.update({"enabled": "yes"}), "true or false"),
    ]:
        cfg = copy.deepcopy(base)
        edit(cfg["field"]["classic_mixtures_team5"])
        with pytest.raises(ValueError, match=why):
            ownership.validate_ownership_config(cfg)


def test_the_run_notes_line_names_the_table_in_force(big):
    from nhl_dfs.build import notes

    pool, proj, _, feats, util = big
    for on, label in ((cfg_copy(True, True), "Classic mixture ON, team5 table"), (cfg_copy(True, False), "Classic mixture ON;"),
                      (cfg_copy(False, True), "old mixture, Classic mixtures off")):
        if on["field"]["classic_mixtures_team5"]["enabled"] and on["field"]["classic_mixtures"]["enabled"] and not shipped_team5():
            continue  # no team5 table filled yet: nothing to name
        f = ff.sample_fast(pool, util, fm.behaviors_for_pool("large_gpp", pool, on), 300, 11, "large_gpp", proj=proj, feats=feats, cfg=on)
        line = notes._shape_line("large_gpp", fm.shape_report(f, pool, on))
        assert f"({label}" in line or label in line


# -- the solve and the gate ------------------------------------------------------------------------------------------

def script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name[:-3]] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_solve_finds_a_known_mixture_and_is_deterministic(monkeypatch):
    mod = script("c46_weights.py")
    rng = np.random.default_rng(5)
    vec = {b: {k: float(v) for k, v in zip(mod.KEYS, rng.uniform(0, 100, len(mod.KEYS)))} for b in (*mod.NON_STACK, *mod.STACKERS)}
    w3, w4, wd, w5 = 20, 30, 10, 15
    m = 100 - w3 - w4 - wd - w5
    ns_total = sum(mod.NON_STACK.values())
    ns = {k: sum(mod.NON_STACK[b] / ns_total * vec[b][k] for b in mod.NON_STACK) for k in mod.KEYS}
    target = {k: (w3 * vec["stacker"][k] + w4 * vec["stacker4"][k] + wd * vec["double_stack"][k] + w5 * vec["stacker5"][k]
                  + m * ns[k]) / 100 for k in mod.KEYS}
    monkeypatch.setattr(mod, "TARGETS", target)
    best, inside = mod.solve(vec)
    assert best is not None and inside >= 1
    assert best[1:6] == (w3, w4, wd, w5, m) and best[0] == pytest.approx(0.0, abs=1e-9)
    assert mod.solve(vec)[0] == best  # the walk order is fixed
    monkeypatch.setattr(mod, "TARGETS", {k: v + 50 for k, v in target.items()})  # nothing within the bound
    assert mod.solve(vec)[0] is None


def test_the_yaml_block_sums_to_one_and_names_known_behaviors():
    import yaml

    mod = script("c46_weights.py")
    got = yaml.safe_load(mod.yaml_block(21, 22, 20, 17, 20))
    assert sum(got.values()) == pytest.approx(1.0) and set(got) <= set(ownership.load_ownership_config()["field"]["behaviors"])
    assert got["stacker5"] == pytest.approx(0.17) and got["stacker"] == pytest.approx(0.21)


def test_the_shipped_team5_weights_are_the_scripts_solve_in_whole_percent():
    """The block printed by `python scripts/c46_weights.py` (flag 49's one pass, stand-in pool) is what is shipped."""
    import yaml

    shipped = shipped_team5()
    assert set(shipped) == {"large_gpp", "small_field"}
    for fam, buckets in shipped.items():
        assert set(buckets) == {"default"} and sum(buckets["default"].values()) == pytest.approx(1.0)
    assert shipped["large_gpp"] == shipped["small_field"]
    mod = script("c46_weights.py")
    w = shipped["large_gpp"]["default"]
    block = mod.yaml_block(*(round(100 * w[b]) for b in ("stacker", "stacker4", "double_stack", "stacker5")),
                           round(100 * sum(w[b] for b in ("optimizer", "stars_value", "casual", "contrarian"))))
    assert yaml.safe_load(block) == pytest.approx(w)


def test_the_gate_machinery_runs_on_the_stand_in_pool(capsys):
    """Not the gate: it shows `check` runs end to end and prints the numbers on a pool that exists in CI."""
    mod = script("c46_weights.py")
    ok = mod.check(stand_in_pool(6), ownership.load_ownership_config())
    out = capsys.readouterr().out
    assert "draws 5000 legal 5000" in out and "mixture in force (team5)" in out and ok


def test_the_gate_never_reads_the_shipped_switches():
    mod = script("c46_weights.py")
    cfg = cfg_copy(False, False)
    on = mod.gate_config(cfg)
    assert on["field"]["classic_mixtures"]["enabled"] and on["field"]["classic_mixtures_team5"]["enabled"]
    assert not cfg["field"]["classic_mixtures_team5"]["enabled"]  # the input config is not touched


def test_the_real_pool_gate():
    """Flag 49: the card's gate. NHL_DFS_C46_POOL names a scratch copy of the 2026-09-30 DKSalaries.csv (it lives only
    on Ben's machine, in runs/20260930-210607-classic/inputs/). Absent: skipped loudly. A skip is not a pass."""
    raw = os.environ.get("NHL_DFS_C46_POOL")
    if not raw or not Path(raw).exists():
        pytest.skip("C46 GATE NOT RUN: set NHL_DFS_C46_POOL to a copy of the 2026-09-30 DKSalaries.csv (flag 49); "
                    "the chunk is not DONE until this ran and passed")
    mod = script("c46_weights.py")
    assert mod.check(script("c19_weights.py").real_pool(Path(raw)), ownership.load_ownership_config())
