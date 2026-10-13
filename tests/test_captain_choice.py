"""C23 Showdown Captain choice (flags 96 to 100, docs/experiments/captain_2026-10-13.md): the per-cohort Captain report in
scripts/standings_synthesis.py, the goalie gate, the Captain leverage term and the split report. No network in any test."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nhl_dfs.contracts.ids import normalize_name
from nhl_dfs.learn import standings as st

pytestmark = pytest.mark.c23

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("standings_synthesis", REPO / "scripts" / "standings_synthesis.py")
syn = sys.modules.get("standings_synthesis") or importlib.util.module_from_spec(_spec)
if "standings_synthesis" not in sys.modules:
    sys.modules["standings_synthesis"] = syn  # dataclasses resolve string annotations through sys.modules
    _spec.loader.exec_module(syn)

META = {"name": "NHL Fixture Showdown", "fee": 1.0, "max_entries": 100, "max_per_user": 1, "pool": 10.0}


# ---- the report, hand-counted -------------------------------------------------------------------------------------

def _day_map(players: dict[str, tuple[str, str]], winner_goalie: str) -> "syn.DayMap":
    """players: name -> (team, pos F/D/G); one game AAA@BBB-style between the two teams named."""
    teams = sorted({t for t, _ in players.values()})
    opp = {teams[0]: teams[1], teams[1]: teams[0]}
    game = f"{teams[0]}@{teams[1]}"
    by_norm, by_last = {}, {}
    for i, (name, (team, pos)) in enumerate(sorted(players.items())):
        dec = ("W" if name == winner_goalie else "L") if pos == "G" else None
        p = syn.Player(i + 1, name, team, pos, game, decision=dec, dressed=True)
        by_norm[normalize_name(name)] = [p]
        by_last.setdefault(normalize_name(name).split()[-1], []).append(p)
    return syn.DayMap("2026-10-05", [game], {}, opp, {t: game for t in teams}, by_norm, by_last, {})


HAND = {  # 12 lineups, Captain first, ranks 1 to 12 in this order
    "Gary Goalie": ("AAA", "G"), "Adam Alpha": ("AAA", "F"), "Andy Apex": ("AAA", "F"), "Abe Arrow": ("AAA", "F"),
    "Art Axel": ("AAA", "D"), "Ace Atom": ("AAA", "F"),
    "Bob Keeper": ("BBB", "G"), "Ben Bravo": ("BBB", "F"), "Bill Bolt": ("BBB", "F"), "Bo Blaze": ("BBB", "F"),
    "Bud Brick": ("BBB", "D"), "Boyd Beam": ("BBB", "F"),
}
HAND_LINEUPS = [
    ["Adam Alpha", "Andy Apex", "Abe Arrow", "Art Axel", "Ace Atom", "Ben Bravo"],    # AAA 5-1, lean won
    ["Gary Goalie", "Adam Alpha", "Andy Apex", "Abe Arrow", "Ben Bravo", "Bill Bolt"],  # goalie CPT, AAA 4-2
    ["Gary Goalie", "Adam Alpha", "Andy Apex", "Ben Bravo", "Bill Bolt", "Bo Blaze"],   # goalie CPT, 3-3
    ["Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick", "Boyd Beam", "Adam Alpha"],     # BBB 5-1, lean lost
    ["Ben Bravo", "Bill Bolt", "Bo Blaze", "Bob Keeper", "Adam Alpha", "Andy Apex"],    # BBB 4-2, goalie at FLEX
    ["Adam Alpha", "Gary Goalie", "Andy Apex", "Abe Arrow", "Ben Bravo", "Bill Bolt"],  # AAA 4-2, goalie at FLEX
    ["Bob Keeper", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick", "Adam Alpha"],    # goalie CPT, BBB 5-1
    ["Adam Alpha", "Andy Apex", "Abe Arrow", "Ace Atom", "Ben Bravo", "Bill Bolt"],     # AAA 4-2
    ["Andy Apex", "Adam Alpha", "Abe Arrow", "Art Axel", "Ace Atom", "Bob Keeper"],     # AAA 5-1, goalie at FLEX
    ["Ben Bravo", "Adam Alpha", "Andy Apex", "Abe Arrow", "Bill Bolt", "Bo Blaze"],     # 3-3
    ["Adam Alpha", "Andy Apex", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick"],     # BBB 4-2
    ["Gary Goalie", "Adam Alpha", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Boyd Beam"],   # goalie CPT, BBB 4-2
]


def _hand_csv() -> bytes:
    rows = ["Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS"]
    own = [(n, "FLEX") for n in sorted(HAND)] + [(n, "CPT") for n in sorted({lu[0] for lu in HAND_LINEUPS})]
    for i in range(max(len(HAND_LINEUPS), len(own))):
        if i < len(HAND_LINEUPS):
            lu = HAND_LINEUPS[i]
            text = f"CPT {lu[0]} " + " ".join(f"FLEX {n}" for n in lu[1:])
            left = f"{i + 1},{5000 + i},entrant{i + 1:02d} (1/1),0,{120 - 5 * i},{text}"
        else:
            left = ",,,,,"
        right = f"{own[i][0]},{own[i][1]},10.00%,{'15' if own[i][1] == 'CPT' else '10'}" if i < len(own) else ",,,"
        rows.append(f"{left},,{right}")
    return ("\n".join(rows) + "\n").encode()


def test_the_captain_report_matches_a_hand_count(tmp_path):
    s = st.parse(_hand_csv(), "contest-standings-990000001.csv")
    c = syn.analyze_contest(s, _day_map(HAND, "Gary Goalie"), META, {}, tmp_path)
    f, t = c["cohorts"]["field"], c["cohorts"]["top1"]
    assert c["slots_unresolved"] == 0 and f["n"] == 12 and t["n"] == 1
    # Captains: Adam 4, Gary 3, Ben 3, Bob 1, Andy 1 of 12 -> CPT own 33.3, 25, 25, 8.3, 8.3; per-lineup mean 300 / 12 = 25.0
    assert f["goalie_cpt_pct"] == 33.3 and f["has_goalie_pct"] == 58.3
    assert f["mean_cpt_own"] == 25.0 and f["cpt_own_le10_pct"] == 16.7
    assert f["cpt_pos"] == {"F": 66.7, "G": 33.3}
    assert f["heavy_team_won_pct"] == 50.0  # 10 leaning lineups, 5 lean to AAA (the W goalie's team)
    assert {x["split"]: x["pct"] for x in f["split_by_team"]} == {"AAA 4-2": 25.0, "BBB 4-2": 25.0, "AAA 5-1": 16.7, "3-3": 16.7,
                                                                  "BBB 5-1": 16.7}
    assert t["goalie_cpt_pct"] == 0.0 and t["mean_cpt_own"] == 33.3 and t["heavy_team_won_pct"] == 100.0
    assert t["split_by_team"] == [{"split": "AAA 5-1", "pct": 100.0}]
    text = "\n".join(syn.render_contest(c))
    assert "CPT own %" in text and "lean team won %" in text


# The committed fixture has no team or position column: a test stub, Hart and Knight as goalies, the W to Hart.
FIXTURE_CHI = {"Anton Frondell", "Frank Nazar", "Jordan Greenway", "Nick Lardis", "Oliver Moore", "Patrick Kane",
               "Roman Kantserov", "Ryan Greene", "Spencer Knight", "Wyatt Kaiser"}
FIXTURE_D = {"Bowen Byram", "Brayden McNabb", "Noah Hanifin", "Rasmus Andersson", "Jeremy Lauzon", "Shea Theodore", "Wyatt Kaiser"}
FIXTURE_G = {"Carter Hart", "Spencer Knight"}


def test_the_captain_report_on_the_committed_fixture_matches_an_independent_count(tmp_path):
    s = st.read(REPO / "tests" / "fixtures" / "standings" / "showdown" / "contest-standings-196048725.csv")
    names = {n for e in s.entries for _, n in e.lineup}
    players = {n: ("CHI" if n in FIXTURE_CHI else "VGK", "G" if n in FIXTURE_G else "D" if n in FIXTURE_D else "F") for n in names}
    c = syn.analyze_contest(s, _day_map(players, "Carter Hart"), META, {}, tmp_path)
    f, t = c["cohorts"]["field"], c["cohorts"]["top1"]
    # counted with csv and a regex over the file, no project code (2026-10-13): 19 entries, 2 blank, 17 lineups
    assert c["entries"] == 19 and f["n"] == 17 and t["n"] == 1
    assert f["goalie_cpt_pct"] == 23.5 and f["has_goalie_pct"] == 94.1  # 4 and 16 of 17
    assert f["mean_cpt_own"] == 35.0 and f["cpt_own_le10_pct"] == 23.5
    assert {x["split"]: x["pct"] for x in f["split_by_team"]} == {"VGK 5-1": 88.2, "VGK 4-2": 5.9, "CHI 5-1": 5.9}
    assert f["heavy_team_won_pct"] == 94.1  # 16 of 17 lean to VGK
    assert t["goalie_cpt_pct"] == 0.0 and t["mean_cpt_own"] == 52.6 and t["split_by_team"] == [{"split": "VGK 5-1", "pct": 100.0}]


def _contest(date: str, top1: tuple, field: tuple) -> dict:
    """top1 / field: (n, goalie_cpt_pct, mean_cpt_own)."""
    mk = lambda n, g, o: {"n": n, "goalie_cpt_pct": g, "mean_cpt_own": o}
    return {"mode": "showdown", "slate_date": date, "cohorts": {"top1": mk(*top1), "field": mk(*field)}}


def test_the_pattern_check_is_the_preregistered_rule():
    held = [_contest("2026-10-05", (12, 0.0, 6.0), (1200, 20.0, 12.0)), _contest("2026-10-06", (10, 10.0, 8.0), (1000, 22.0, 14.0))]
    r = syn.showdown_pattern(held)
    assert r["top1_n"] == 22 and r["top1_goalie_cpt_pct"] == 4.5 and r["verdict"] == "HOLDS"
    assert syn.showdown_pattern(held[:1])["verdict"] == "TOO FEW"  # 12 top-1% lineups < 20
    near = [_contest("2026-10-05", (25, 12.0, 9.0), (2500, 20.0, 12.0))]  # goalie gap 8 < 10
    assert syn.showdown_pattern(near)["verdict"] == "NOT SHOWN"
    with_evidence = held + [_contest("2026-10-02", (4, 0.0, 7.0), (341, 23.0, 15.0))]
    assert syn.showdown_pattern(with_evidence, ("2026-10-02",))["top1_n"] == 22  # the out-of-sample reading drops 10-02
    lines = syn.pattern_lines(with_evidence)
    assert any("without 2026-10-02" in x and "HOLDS" in x for x in lines)


# ---- the policy: ceiling ranks, the gate, the leverage term, the split report --------------------------------------

import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import numpy as np  # noqa: E402

from nhl_dfs.build import candidates as cand_mod  # noqa: E402
from nhl_dfs.build import captain as cap_mod  # noqa: E402
from nhl_dfs.build import objectives as ob  # noqa: E402
from nhl_dfs.build import portfolio as pf  # noqa: E402
from nhl_dfs.models import field as _field  # noqa: E402,F401  (bound before any test fakes candidates.generate: field.py binds it at import)
from nhl_dfs.contracts.geometry import Mode  # noqa: E402
from pool_builder import classic_pool, clone_entries, showdown_pool  # noqa: E402

RISK = ob.load_risk_config()
GA, GB = "s1|AAA|G", "s7|BBB|G"  # showdown_pool: person 1 and 7 are the goalies; roles "<pid>c" (CPT) and "<pid>f" (FLEX)


def _with(cfg: dict, gate: bool = False, lev: bool = False, **gate_kw) -> dict:
    c = copy.deepcopy(cfg)
    c["discovery"]["captain"]["goalie_gate"].update({"enabled": gate, **gate_kw})
    c["discovery"]["captain"]["leverage"]["enabled"] = lev
    return c


def _scen(pool, values: dict[str, float], S: int = 200, spike: dict[str, float] | None = None) -> ob.ScenarioSet:
    """values: person_key -> points in every draw; spike: person_key -> points in the top 5% of draws (0 elsewhere)."""
    ids = [r.role_id for r in pool.rows]
    base = np.zeros((S, len(ids)), np.int32)
    for j, r in enumerate(pool.rows):
        base[:, j] = int(round(values.get(r.person_key, 0.0) * 10))
        if spike and r.person_key in spike:
            base[:, j] = 0
            base[S - S // 20:, j] = int(round(spike[r.person_key] * 10))
    return ob.ScenarioSet(ids, base, "design", 1)


def _ranked_pool():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=6)
    # GA: 0 in 95% of draws, 60 in 5%: mean 3 but the 99th percentile is 60 (rank 1). GB 25 is 4th behind 60, 40 and 30.
    vals = {"s2|AAA|F": 40.0, "s3|AAA|F": 30.0, GB: 25.0, **{f"s{i}|{'AAA' if i < 7 else 'BBB'}|F": 10.0 for i in (4, 5, 6, 8, 9, 10, 11, 12)}}
    return pool, vals


def test_the_gate_bars_a_goalie_ranked_fourth_and_allows_one_ranked_first_or_tied_third():
    pool, vals = _ranked_pool()
    on = _with(RISK, gate=True)
    p = cap_mod.policy(_scen(pool, vals, spike={GA: 60.0}), pool, on, None)
    ranks = {g["person"]: g["rank"] for g in p.report["goalie_gate"]["goalies"]}
    assert ranks == {GA: 1, GB: 4}
    assert p.barred == frozenset({"7c"}) and p.report["goalie_gate"]["would_bar"] == [GB]  # his Captain row only, never FLEX
    assert all(pool.by_role_id[r].is_goalie for r in p.barred)  # skater Captain rows are never barred
    tied = cap_mod.policy(_scen(pool, {**vals, GB: 30.0}, spike={GA: 60.0}), pool, on, None)  # GB ties s3 at 30: rank 3
    assert {g["person"]: g["rank"] for g in tied.report["goalie_gate"]["goalies"]}[GB] == 3 and tied.barred == frozenset()


def test_with_the_gate_off_nothing_is_barred_but_the_would_be_barred_goalie_is_reported():
    pool, vals = _ranked_pool()
    p = cap_mod.policy(_scen(pool, vals, spike={GA: 60.0}), pool, RISK, None)
    assert RISK["discovery"]["captain"]["goalie_gate"]["enabled"] is False and RISK["discovery"]["captain"]["leverage"]["enabled"] is False
    assert p.barred == frozenset() and p.term == {} and p.report["goalie_gate"]["would_bar"] == [GB]
    json.dumps(p.report)  # built-in types only


def test_the_leverage_term_is_the_preregistered_arithmetic_on_captain_rows_only():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=6)
    ranks = {r.person_key: (20.0, 5) for r in pool.rows}
    ranks["s2|AAA|F"] = (40.0, 1)
    got = cap_mod.leverage_points(pool, {"2c": 30.0, "2f": 99.0}, ranks, 1.5, 1.0)
    assert set(got) == {r.role_id for r in pool.rows if "CPT" in r.roster_positions}  # no FLEX row
    assert got["2c"] == pytest.approx(1.5 * math.log(100 / 30))  # top ceiling, 30% owned
    assert got["3c"] == pytest.approx(1.5 * 0.5 * math.log(100))  # half the top ceiling, never drawn: the 1% floor
    assert cap_mod.policy(_scen(classic_pool(), {}), classic_pool(), _with(RISK, gate=True, lev=True), None) == cap_mod.CaptainPolicy()


def test_the_split_report_sizes_follow_the_goal_share_and_report_the_published_counts():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=6)
    five_one = ("2c", "1f", "3f", "4f", "5f", "8f")  # 5 AAA + 1 BBB
    four_two = ("8c", "9f", "10f", "11f", "2f", "3f")  # 4 BBB + 2 AAA
    three = ("2c", "3f", "4f", "8f", "9f", "10f")
    r = cap_mod.split_report({"AAA": 0.6, "BBB": 0.4}, [five_one, four_two, three], pool, 30, 0.10, 1)
    assert r["target_lean"] == {"AAA": 18, "BBB": 12} and r["underdog"] == "BBB" and r["sleeve_target"] == 3
    assert r["published"] == {"lean": {"AAA": 1, "BBB": 1}, "five_one": {"AAA": 1, "BBB": 0}, "three_three": 1}
    assert r["enforced"] is False
    assert cap_mod.split_report({"AAA": 0.6, "BBB": 0.4}, [], pool, 5, 0.10, 1)["sleeve_target"] == 0  # floor(0.5) = 0
    assert cap_mod.split_report({"AAA": 0.6, "BBB": 0.4}, [], pool, 30, 0.10, 2)["status"] == "NOT_APPLICABLE"


def test_the_captain_config_ships_off_and_is_validated():
    cp = RISK["discovery"]["captain"]
    assert cp["goalie_gate"] == {"enabled": False, "ceiling_quantile": 0.99, "top_k": 3}
    assert cp["leverage"] == {"enabled": False, "weight": 1.5, "own_floor_pct": 1.0} and cp["split_sleeve_max"] == 0.10
    bad = [("goalie_gate", "enabled", 1), ("goalie_gate", "ceiling_quantile", 1.5), ("goalie_gate", "top_k", 0),
           ("goalie_gate", "top_k", 2.5), ("leverage", "weight", -1), ("leverage", "own_floor_pct", 0), (None, "split_sleeve_max", 2)]
    for blk, key, val in bad:
        c = copy.deepcopy(RISK)
        (c["discovery"]["captain"][blk] if blk else c["discovery"]["captain"])[key] = val
        with pytest.raises(ValueError):
            ob.validate_risk_config(c)


# ---- discovery: off is the same as no captain block; the gate excludes; the term moves Captain rows only -------------

def _discover_calls(monkeypatch, pool, scen, cfg, own=None):
    seen = []

    def fake(pool_, mode, objective, n, **k):
        seen.append((dict(objective), dict(k)))
        return []

    monkeypatch.setattr(cand_mod, "generate", fake)
    runtime = {"candidates": {"perturb_sd_points": 0.5, "time_limit_total_s": 5.0, "min_pairwise_diff": 2}}
    _, report = pf.discover(pool, scen, 6, runtime, cfg, seed=1, goalies=[], own=own)
    return seen, report


def test_discovery_with_both_switches_off_is_the_call_it_was_and_each_switch_moves_only_its_rows(monkeypatch):
    pool, vals = _ranked_pool()
    scen = _scen(pool, vals, spike={GA: 60.0})
    own = {"2c": 30.0, "7c": 20.0}
    off, rep = _discover_calls(monkeypatch, pool, scen, RISK, own)
    absent = copy.deepcopy(RISK)
    del absent["discovery"]["captain"]
    none, _ = _discover_calls(monkeypatch, pool, scen, absent, own)
    assert off and off == none and all("exclude" not in k for _, k in off)  # identical objectives and arguments
    assert rep["captain"]["goalie_gate"]["would_bar"] == [GB]
    gate, _ = _discover_calls(monkeypatch, pool, scen, _with(RISK, gate=True), own)
    assert [o for o, _ in gate] == [o for o, _ in off] and all(k["exclude"] == frozenset({"7c"}) for _, k in gate)
    lev_cfg = _with(RISK, lev=True)
    lev, _ = _discover_calls(monkeypatch, pool, scen, lev_cfg, own)
    term = cap_mod.policy(scen, pool, lev_cfg, own).term
    for (a, _), (b, kb) in zip(off, lev):
        diff = {rid: b[rid] - a[rid] for rid in a if b[rid] != a[rid]}
        assert diff and set(diff) <= {r.role_id for r in pool.rows if "CPT" in r.roster_positions} and "exclude" not in kb
        assert all(diff[rid] == pytest.approx(term[rid]) for rid in diff)


def test_classic_discovery_is_untouched_by_the_captain_switches(monkeypatch):
    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    scen = _scen(pool, {r.person_key: 5.0 + (i % 7) for i, r in enumerate(pool.rows)})
    off, rep = _discover_calls(monkeypatch, pool, scen, RISK)
    on, rep_on = _discover_calls(monkeypatch, pool, scen, _with(RISK, gate=True, lev=True))
    assert off == on and "captain" not in rep and "captain" not in rep_on


def test_the_solver_never_makes_a_barred_goalie_captain_but_still_allows_him_at_flex():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=6)
    obj = {r.role_id: 1.0 for r in pool.rows} | {"1c": 100.0, "1f": 50.0}
    free = cand_mod.generate(pool, Mode.SHOWDOWN, obj, 4, seed=1, perturb_sd=0.0)
    assert free and free[0].role_ids[0] == "1c"  # unbarred, he is the best Captain
    gated = cand_mod.generate(pool, Mode.SHOWDOWN, obj, 4, seed=1, perturb_sd=0.0, exclude=frozenset({"1c"}))
    assert gated and all(c.role_ids[0] != "1c" for c in gated) and any("1f" in c.role_ids for c in gated)


# ---- whole runs: off leaves the picks alone; with both switches on the gate holds and the Captain cap binds ---------

LS2 = REPO / "tests" / "fixtures" / "late_swap" / "showdown2"
MINI_SD = REPO / "tests" / "fixtures" / "mini" / "showdown"
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _run(tmp, salary, entries, clock, cfg, spy: list | None = None):
    from nhl_dfs.build.run import load_runtime_config, run_slate

    rt = copy.deepcopy(load_runtime_config())
    rt["candidates"]["time_limit_total_s"] = 600.0  # the solver budget never binds, so CPU speed cannot change the picks
    real_gen, real_sel = cand_mod.generate, pf.select

    def gen(pool, mode, objective, n, **k):
        if spy is not None:
            spy.append(("generate", n, sorted(objective.items()), sorted((a, repr(b)) for a, b in k.items())))
        return real_gen(pool, mode, objective, n, **k)

    def sel(cands, *a, **k):
        if spy is not None:
            spy.append(("select", [c.key for c in cands]))
        return real_sel(cands, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ob, "load_risk_config", lambda *a, **k: copy.deepcopy(cfg))
        mp.setattr(cand_mod, "generate", gen)
        mp.setattr(pf, "select", sel)
        return run_slate(salary, entries, offline=True, baseline_only=False, scenario=True, scenario_n=SMALL, runtime=rt,
                         out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: clock)


SD2_CLOCK = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sd2_off(tmp_path_factory):
    spy: list = []
    return _run(tmp_path_factory.mktemp("c23_off"), LS2 / "DKSalaries.csv", LS2 / "DKEntries.template.csv", SD2_CLOCK, RISK, spy), spy


@pytest.fixture(scope="module")
def sd2_absent(tmp_path_factory):
    absent = copy.deepcopy(RISK)
    del absent["discovery"]["captain"]
    spy: list = []
    return _run(tmp_path_factory.mktemp("c23_absent"), LS2 / "DKSalaries.csv", LS2 / "DKEntries.template.csv", SD2_CLOCK, absent, spy), spy


@pytest.mark.timeout(900)  # two whole runs in the fixtures (about 20 s on a Linux container)
def test_off_publishes_the_same_showdown_picks_as_no_captain_block(sd2_off, sd2_absent):
    (off, spy_off), (absent, spy_abs) = sd2_off, sd2_absent
    assert off.ok and absent.ok and off.manifest["versions"][-1]["phase"] == "S"
    assert spy_off == spy_abs and any(s[0] == "select" for s in spy_off)  # same discovery calls, same candidates into selection
    assert off.manifest["versions"][-1]["sha256"] == absent.manifest["versions"][-1]["sha256"]  # byte-identical v3
    sec = off.manifest["scenario"]["captain"]
    assert "error" not in sec and sec["policy"]["goalie_gate"]["enabled"] is False and sec["gate_dropped"] == 0
    assert sec["split"]["status"] == "NOT_APPLICABLE"  # showdown2 is two games
    assert absent.manifest["scenario"]["captain"]["policy"]["configured"] is False


@pytest.fixture(scope="module")
def mini_on(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c23_on")
    clone_entries(MINI_SD / "DKEntries.csv", tmp / "DKEntries.csv", 30)
    cfg = _with(RISK, gate=True, lev=True, top_k=1)  # top_k 1: every goalie the simulator does not rank first is barred
    return _run(tmp, MINI_SD / "DKSalaries.csv", tmp / "DKEntries.csv", datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc), cfg)


@pytest.mark.timeout(900)  # one whole 30-entry run in the fixture (about 22 s on a Linux container)
def test_with_both_switches_on_no_barred_goalie_is_captain_and_the_captain_cap_holds(mini_on):
    from nhl_dfs.build import packet

    r = mini_on
    assert r.ok and r.manifest["versions"][-1]["phase"] == "S", r.manifest["failed"]
    s = r.manifest["scenario"]
    sec = s["captain"]
    json.dumps(sec)
    assert "error" not in sec and sec["policy"]["goalie_gate"]["enabled"] is True and sec["policy"]["leverage"]["enabled"] is True
    barred = set(sec["policy"]["goalie_gate"]["barred_roles"])
    assert barred and sec["policy"]["leverage"]["points_max"] > 0
    assert s["discovery"]["captain_gate_dropped"] == sec["gate_dropped"] >= 0
    lineups = packet.RunView(r.run).lineups
    assert len(lineups) == 30 and all(lu[0] not in barred for lu in lineups.values())
    # The Captain count cap holds at selection levels 0 and 1 (levels 2 and up drop it): each Captain has at most `cap` entries placed there
    cap = s["caps"]["captain"]
    assert cap is not None and not any("captain cap raised" in n for n in s["caps"]["notes"])
    # the count cap applies to tournament entries only (portfolio.select: captain_n and its check sit under `eid in tournament`)
    level = {row["entry_id"]: row["cap_level"] for row in s["entries"] if row["family"] != "cash"}
    held = {}
    for e, lu in lineups.items():
        if level.get(e, 9) <= 1:
            held[lu[0]] = held.get(lu[0], 0) + 1
    assert held and max(held.values()) <= cap
    goalies = {g["person"] for g in sec["policy"]["goalie_gate"]["goalies"]}
    assert set(sec["policy"]["goalie_gate"]["would_bar"]) == goalies  # with top_k 1 every goalie is barred on this bed
    assert sum(1 for lu in lineups.values() if lu[0] in barred) == 0 == sec["portfolio"]["goalie_cpt_entries"]


# ---- the experiment script's preregistered verdict and its run finder ----------------------------------------------

_xspec = importlib.util.spec_from_file_location("c23_captain", REPO / "scripts" / "c23_captain.py")
c23 = importlib.util.module_from_spec(_xspec)
_xspec.loader.exec_module(c23)


def _arm_runs(metrics, goalie=0, budget="OK"):
    return [{"ok": True, "metric": m, "risk_budget": budget, "portfolio": {"goalie_cpt_entries": goalie}} for m in metrics]


def test_the_switch_verdict_is_the_preregistered_rule():
    o = _arm_runs([0.0100, 0.0102, 0.0098, 0.0101, 0.0099])  # sd 0.000158: MDE 0.000197, 2% of the mean
    up = _arm_runs([0.0110, 0.0113, 0.0107, 0.0111, 0.0108])  # d about +0.0010 (10%) in every seed
    assert c23.switch_verdict(o, up, "G")["verdict"] == "ACCEPT-TO-SHADOW"
    assert c23.switch_verdict(o, _arm_runs([0.0110, 0.0113, 0.0107, 0.0111, 0.0108], goalie=9), "L")["verdict"] == "INCONCLUSIVE"
    assert c23.switch_verdict(o, _arm_runs([0.0110, 0.0113, 0.0107, 0.0111, 0.0108], budget="BREACHED"), "G")["verdict"] == "INCONCLUSIVE"
    assert c23.switch_verdict(o, _arm_runs([0.0099, 0.0101, 0.0097, 0.0100, 0.0098]), "G")["verdict"] == "REJECT"  # mean d below 0
    noisy = _arm_runs([0.0100, 0.0130, 0.0070, 0.0115, 0.0085])  # MDE above 10% of the mean
    assert c23.switch_verdict(noisy, up, "G")["reason"].startswith("MDE")
    failed = o[:4] + [{"ok": False, "metric": None, "risk_budget": "OK", "portfolio": {}}]
    assert c23.switch_verdict(failed, up, "G")["verdict"] == "NO VERDICT"


def test_the_study_verdict_needs_three_real_slates_and_never_rejects_a_synthetic_one():
    sl = lambda v, syn=False: {"synthetic": syn, "verdicts": {"G": {"verdict": v}, "L": {"verdict": v}}}
    assert c23.study_verdict([sl("REJECT", True)] * 4, "G") == "NOT MEASURED"
    assert c23.study_verdict([sl("REJECT")] * 2, "G") == "NOT MEASURED"
    assert c23.study_verdict([sl("REJECT")] * 2 + [sl("INCONCLUSIVE")], "G") == "REJECT"
    assert c23.study_verdict([sl("ACCEPT-TO-SHADOW")] * 2 + [sl("INCONCLUSIVE")], "L") == "ACCEPT-TO-SHADOW"
    assert c23.study_verdict([sl("ACCEPT-TO-SHADOW"), sl("REJECT"), sl("INCONCLUSIVE")], "G") == "INCONCLUSIVE"


def test_the_run_finder_keeps_the_newest_showdown_parent_per_slate(tmp_path):
    def mk(name, **m):
        d = tmp_path / name
        (d / "inputs").mkdir(parents=True)
        (d / "inputs" / "DKEntries.csv").write_text("x")
        (d / "manifest.json").write_text(json.dumps({"mode": "showdown", "scenario": {"version": 3}, **m}))

    mk("a", slate_id="s1", created_utc="2026-10-05T20:00:00Z")
    mk("b", slate_id="s1", created_utc="2026-10-05T22:00:00Z")  # newer run of the same slate
    mk("c", slate_id="s2", created_utc="2026-10-06T20:00:00Z", parent_run_id="x")  # a late-swap child
    mk("d", slate_id="s3", created_utc="2026-10-02T20:00:00Z")  # before --since
    mk("e", slate_id="s4", created_utc="2026-10-07T20:00:00Z", mode="classic")
    mk("f", slate_id="s5", created_utc="2026-10-08T20:00:00Z", scenario={})  # no scenario version published
    assert [p.name for p in c23.find_runs(tmp_path, "2026-10-03")] == ["b"]
