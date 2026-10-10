"""C21: stack size study (flags 56 to 63, docs/experiments/stack_size_2026-10-10.md). Shapes forced with solver group rows and counted from
role IDs, the study as a pure function of a frozen cache, the scorer checked against the engine's own figures, and the preregistered verdict pinned on
every branch. The study runs here only on the synthetic bed in a small configuration (its numbers decide nothing and no test asserts one). No network
in any test."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import TESTS

pytestmark = [pytest.mark.c21, pytest.mark.timeout(900)]  # a bed replay is a whole offline scenario pass: about 30 s here

ROOT = TESTS.parent
BED = TESTS / "fixtures" / "late_swap" / "classic"  # 6 teams in 3 games, 2 goalies per team, real Game Info


@pytest.fixture(scope="module")
def c21():
    sys.path.insert(0, str(ROOT / "scripts"))
    import c21_stack_study as m

    return m


@pytest.fixture(scope="module")
def bed(c21, tmp_path_factory):
    out = tmp_path_factory.mktemp("c21_bed")
    run = c21.replay_run(BED / "DKSalaries.csv", BED / "DKEntries.template.csv", c21.BED_CLOCK, c21.SCENARIO_N["bed_small"], out, "bed")
    view = c21.load_view(run.path.parent, run.run_id)
    assert view.cache is not None, "the bed replay wrote no scenario cache"
    return SimpleNamespace(run=run, view=view, cache=view.cache, out=out)


@pytest.fixture(scope="module")
def world(c21, bed):
    """The pool restricted to the cache's person axis, the cached streams, the design objective and the eligible primary teams."""
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.build.run import pool_without

    full = bed.view.pool
    sets = c21.build_sets(bed.cache, full)
    pool = pool_without(full, [r for r in full.by_role_id if r not in sets["referee"].col])
    cfg = c21.Config.small()
    return SimpleNamespace(pool=pool, full=full, sets=sets, objective=pf.role_objective(sets["design"], pool), cfg=cfg,
                           primaries=c21.eligible_primaries(sets["design"], pool, cfg.n_primary), teams=sorted(c21.skater_roles(pool)),
                           risk=ob.load_risk_config())


@pytest.fixture(scope="module")
def study_a(c21, bed):
    return c21.run_slate_study("bed", True, bed.view, bed.cache, bed.cache, c21.Config.small(), log=lambda m: None)


@pytest.fixture(scope="module")
def study_b(c21, bed):
    return c21.run_slate_study("bed", True, bed.view, bed.cache, bed.cache, c21.Config.small(), log=lambda m: None)


# -- shapes are counted from role IDs -----------------------------------------------------------------------------------

def test_shape_is_counted_from_role_ids_skaters_only(c21, world):
    pool = world.pool
    a, b, c, d = world.teams[:4]
    sk = {t: sorted(c21.skater_roles(pool)[t]) for t in (a, b, c)}
    goalie_of = lambda t: next(r.role_id for r in pool.rows if r.is_goalie and r.team == t)  # noqa: E731
    lineup = sk[a][:4] + sk[b][:3] + sk[c][:1]
    assert c21.shape_of(lineup + [goalie_of(a)], pool) == (4, 3, 1)
    assert c21.shape_of(lineup + [goalie_of(d)], pool) == (4, 3, 1)  # a goalie of any team never changes the shape
    assert c21.shape_name(c21.shape_of(sk[a][:6] + sk[b][:1] + sk[c][:1], pool)) == "6-1-1"
    assert c21.shape_name(c21.shape_of(sk[a][:3] + sk[b][:3] + sk[c][:2], pool)) == "3-3-2"


def test_the_study_counts_shapes_like_the_samplers_own_helper(c21, world):
    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.models.field import skater_shape

    got = cand_mod.generate(world.pool, Mode.CLASSIC, world.objective, 15, seed=3, perturb_sd=2.0, time_limit_total_s=60)
    assert got and all(c21.shape_of(g.role_ids, world.pool) == skater_shape(world.pool, g.role_ids) for g in got)


@pytest.mark.parametrize("rule", [True, False])
@pytest.mark.parametrize("shape", ["4-3-1", "5-2-1", "6-1-1", "3-3-2"])
def test_a_forced_shape_comes_out_exactly_as_asked(c21, world, shape, rule):
    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.build import own_goalie as og
    from nhl_dfs.contracts.geometry import Mode

    got = c21.rank_tuples(world.pool, world.objective, shape, world.primaries, world.teams, rule, 10)
    assert len(got["menu"]) == len(world.primaries), got["status"]  # one tuple per eligible primary team
    cands = cand_mod.generate(world.pool, Mode.CLASSIC, world.objective, 10, seed=5, perturb_sd=2.0, groups_menu=got["menu"], time_limit_total_s=60,
                              avoid_own_goalie=rule)
    assert len(cands) == 10
    for c in cands:  # counted from the role IDs, not from any sampler figure
        assert c21.shape_name(c21.shape_of(c.role_ids, world.pool)) == shape
        if rule:
            assert not og.faces_own_goalie(c.role_ids, world.pool)


def test_an_infeasible_shape_is_reported_not_a_crash_and_is_never_a_challenger(c21, world, bed, monkeypatch):
    monkeypatch.setitem(c21.SHAPES, "6-1-1", (11, 1, 1))  # no team has 11 skaters in the pool
    got = c21.rank_tuples(world.pool, world.objective, "6-1-1", world.primaries, world.teams, True, 10)
    assert got["menu"] == [] and sum(got["status"].values()) >= len(world.primaries)
    res = c21.run_slate_study("bed", True, bed.view, bed.cache, bed.cache, c21.Config.small(), log=lambda m: None)
    assert res["tuple_counts"]["6-1-1"] == 0 and res["equal_depth"] is False
    assert res["arms"]["6-1-1"]["mean_p_top1pct"] is None and res["label_only_6_1_1"] is None
    assert set(res["challengers"]) == {"5-2-1", "3-3-2"}  # 6-1-1 is never among the challengers


# -- the study is a pure function of the frozen cache ---------------------------------------------------------------------

def test_every_shape_has_equal_depth(study_a):
    assert study_a["failed"] is None, study_a["failed"]
    counts = study_a["tuple_counts"]
    assert set(counts) == {"4-3-1", "5-2-1", "6-1-1", "3-3-2"} and set(counts.values()) == {study_a["primaries"].__len__()} and study_a["equal_depth"]


def test_the_study_reproduces_its_numbers_with_a_fixed_seed(study_a, study_b):
    def stable(r):
        return json.dumps({k: v for k, v in r.items() if k not in ("seconds", "generation")}, sort_keys=True, default=str)

    assert stable(study_a) == stable(study_b)
    assert all(v["identical"] for v in study_a["noise_baseline"].values()), study_a["noise_baseline"]  # B98: identical inputs, identical lists


def test_the_scorer_agrees_with_the_engines_own_figures(study_a):
    assert study_a["plumbing"]["indicator_mean_vs_engine_max_abs_diff"] < 1e-6  # the per-scenario indicator's mean is objectives' p_top1pct
    assert study_a["plumbing"]["reps_base_vs_sorted_field_max_abs_diff"] < 1e-5  # the re-weighted ranks reproduce the sorted-field ranks


def test_the_scorer_reproduces_the_runs_published_top1pct_figure(c21, bed, world):
    """The run's own manifest reports each entry's modeled P(top 1%) on the referee draws, all of the user's entries ranked together. The study's
    scorer, given the saved field and draws, must give the same figures for the same lineups (the bed's saved cache and replay are one run)."""
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.contracts.geometry import Mode

    cid, _ = c21.pick_contest(bed.cache, world.risk)
    contest, n_opp = bed.cache.contests[cid], int(bed.cache.n_opponents[cid])
    lus, ks = bed.cache.families[bed.cache.contest_family[cid]]
    ref = world.sets["referee"]
    spec = ob.field_spec(lus, ks, n_opp, world.risk, n_scenarios=ref.n, seed=bed.cache.seed, salt=f"{cid}|referee")
    scorer = c21.Scorer(ref, spec, contest, world.risk, Mode.CLASSIC)
    ids = sorted(bed.view.lineups)
    own = ref.scores([bed.view.lineups[e] for e in ids], Mode.CLASSIC).full()
    G, E = scorer.sf.ranks(own)
    pg, pe = ob.own_pairwise(own)
    mine = ob.metrics_from_ranks(G + pg, E + pe, contest, world.risk).p_top1pct
    sc = read_manifest(bed.run)["scenario"]
    published = {str(e["entry_id"]): float(e["p_top1pct"]) for e in sc["entries"]}
    assert set(published) == set(ids) and sc["version"] == bed.view.version
    assert max(abs(float(mine[i]) - published[e]) for i, e in enumerate(ids)) < 2e-4  # the manifest rounds to 4 decimals


# -- the preregistered verdict ---------------------------------------------------------------------------------------------

def slate(c21, name="A", *, d52=0.0008, d33=0.0008, se=0.0001, b52=1e-4, b33=1e-4, base=0.02, synthetic=False, fid=True, failed=None, label=None):
    s = {"name": name, "synthetic": synthetic, "failed": failed, "fidelity_ok": fid, "mean_base": base,
         "challengers": {"5-2-1": {"d": d52, "se": se, "best10_diff": b52}, "3-3-2": {"d": d33, "se": se, "best10_diff": b33}}}
    if label is not None:
        s["label_only_6_1_1"] = {"d": label, "se": se, "best10_diff": label}
    return s


def test_a_challenger_passes_only_with_all_three_conditions(c21):
    st = lambda **k: c21.slate_status(slate(c21, **k), "5-2-1")  # noqa: E731
    assert st() == "PASS"  # 2.78 standard errors (0.000278), 3 percent of 0.02 (0.0006), best-10 above zero
    assert st(d52=0.0006 - 1e-6) == "INCONCLUSIVE"  # clears 2.78 se and 1 percent but not 3 percent
    assert st(b52=0.0) == "INCONCLUSIVE"  # the best-10 gain must be above zero
    assert st(d52=0.0008, se=0.0004) == "INCONCLUSIVE"  # under 2.78 standard errors (0.00111), but the MDE gate (0.00111 < 0.002) is not what fires
    assert st(d52=0.00015) == "FAIL"  # under 1 percent of the baseline
    assert st(d52=-0.001) == "FAIL"
    assert st(se=0.001) == "INCONCLUSIVE"  # MDE 0.00278 is above 10 percent of 0.02: INCONCLUSIVE by construction, however large the gain
    assert st(fid=False) == "NO VERDICT" and st(failed="no contest") == "NO VERDICT"


def test_the_study_verdict_in_its_preregistered_order(c21):
    both = [slate(c21, "A"), slate(c21, "B")]
    assert c21.verdict(both)["study"] == "ACCEPT-TO-SHADOW(5-2-1)"  # both challengers pass both slates and tie: 5-2-1
    assert c21.verdict([slate(c21, "A", d33=0.0012), slate(c21, "B", d33=0.0012)])["study"] == "ACCEPT-TO-SHADOW(3-3-2)"  # the larger gain wins
    one_fails = [slate(c21, "A", d52=0.0008, d33=-0.001), slate(c21, "B", d52=-0.001, d33=0.0008)]  # each challenger fails somewhere
    assert c21.verdict(one_fails)["study"] == "REJECT"
    mixed = [slate(c21, "A", d52=0.0008, d33=0.0004), slate(c21, "B", d52=0.0004, d33=0.0004)]  # nobody passes both, nobody fails everywhere
    assert c21.verdict(mixed)["study"] == "INCONCLUSIVE"
    assert c21.verdict([slate(c21, "A")])["study"] == "NOT MEASURED (fewer than two real slates)"
    synthetic = c21.verdict([slate(c21, "bed", synthetic=True)])
    assert synthetic["study"].startswith("NOT MEASURED on real caches") and synthetic["per_slate"]["bed"]["5-2-1"] == "PASS"  # shown, decides nothing
    assert c21.verdict([*both, slate(c21, "bed", synthetic=True)])["study"].startswith("NOT MEASURED on real caches")  # a bed in the run never promotes
    assert c21.verdict([slate(c21, "A", fid=False), slate(c21, "B")])["study"] == "NO VERDICT"
    assert "REJECT" not in c21.verdict([slate(c21, "A", fid=False), slate(c21, "B", d52=-1, d33=-1)])["study"]


def test_6_1_1_alone_can_never_promote(c21):
    huge = [slate(c21, n, d52=-0.001, d33=-0.001, label=0.5) for n in "AB"]
    v = c21.verdict(huge)
    assert v["study"] == "REJECT" and "6-1-1" not in v["per_slate"]["A"] and "challenger" not in v
    assert "against a field that has none" in v["label_6_1_1"]


# -- the whole script, in a subprocess: the write guard and the network block hold ------------------------------------------

def test_the_script_runs_the_bed_under_the_write_guard(tmp_path):
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "c21_stack_study.py"), "--out", str(tmp_path), "--bed", "--small"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=900)
    assert r.returncode == 0, (r.stdout[-1500:] + r.stderr[-1500:])
    guard = json.loads((tmp_path / "guard.json").read_text(encoding="utf-8"))
    assert guard["network_blocked"] == []
    assert all("observations" in w for w in guard["writes_refused"]), guard["writes_refused"]  # the known gap, nothing else
    res = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert res["verdict"]["study"].startswith("NOT MEASURED on real caches") and res["slates"][0]["synthetic"] is True
    assert (tmp_path / "report.md").read_text(encoding="utf-8").count("SYNTHETIC BED") >= 1


def test_the_real_cache_study_runs_when_a_saved_run_is_given(tmp_path):
    saved = os.environ.get("NHL_DFS_C21_SAVED_RUN")
    if not saved:
        pytest.skip("NHL_DFS_C21_SAVED_RUN is not set: the real-cache study did NOT run here (backlog B107); a skip is not a pass")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "c21_stack_study.py"), "--out", str(tmp_path), "--saved-run", saved, "--small"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"}, cwd=ROOT, timeout=3600)
    assert r.returncode == 0, (r.stdout[-1500:] + r.stderr[-1500:])
    res = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert res["slates"] and res["slates"][0]["synthetic"] is False
