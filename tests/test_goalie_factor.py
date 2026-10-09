"""C20: goalie as the top-1% factor. B37 (unresolved goalie pairs in the research request), the own-goalie rule
(flags 51 to 53) and the goalie leverage term (flag 54). No network in any test."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import TESTS
from nhl_dfs.build import packet
from nhl_dfs.build.run import run_slate

pytestmark = pytest.mark.c20

LS = TESTS / "fixtures" / "late_swap" / "classic"  # 6 teams in 3 games, 2 goalies per team, real Game Info
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # all three games are ahead
AFTER_FIRST = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # AAA@BBB (7:00PM ET) has started
SLATE_TEAMS = {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}


@pytest.fixture(scope="module")
def run0(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c20_b37")
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=True,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: BEFORE)
    assert r.ok, r.manifest["failed"]
    return r


def _portfolio_goalie_teams(run) -> set[str]:
    v = packet.RunView(run)
    return {v.row(r).team for lu in v.lineups.values() for r in lu if r and v.row(r).is_goalie}


def _unresolved(req) -> list[dict]:
    return [p for p in req["players"] if p["why"] == packet.UNRESOLVED_WHY]


def test_b37_both_goalies_of_an_unresolved_team_outside_the_portfolio_are_listed(run0):
    in_portfolio = _portfolio_goalie_teams(run0.run)
    outside = SLATE_TEAMS - in_portfolio
    assert in_portfolio and outside  # the defect needs a team the lineups do not use
    req = packet.research_request(run0.run, now=BEFORE)
    got = {t: sorted(p["name"] for p in _unresolved(req) if p["team"] == t) for t in outside}
    assert all(len(names) == 2 for names in got.values()), got  # BOTH goalies of each such team
    assert all(not any(p["team"] == t for p in _unresolved(req)) for t in in_portfolio)  # those are listed under today's reason
    assert set(req["unresolved_goalie_teams"]) == outside and req["unresolved_goalie_teams_not_listed"] == []
    for p in _unresolved(req):
        assert p["pos"] == "G" and p["role_id"] and p["entries"] == 0 and p["game_id"] and p["start_utc"]
        assert "current" in p  # the override's `old` value rides along when the role state exists
    # the research URLs now cover those teams' line pages (a slug is looked up per team code)
    assert len(req["urls"]) >= 1


def test_b37_exposed_players_stay_first_and_the_list_never_passes_max_players(run0):
    cfg = packet.load_qa_config()
    full = packet.research_request(run0.run, cfg, now=BEFORE)
    first_unresolved = next(i for i, p in enumerate(full["players"]) if p["why"] == packet.UNRESOLVED_WHY)
    assert all(p["why"] != packet.UNRESOLVED_WHY for p in full["players"][:first_unresolved])
    assert all(p["why"] == packet.UNRESOLVED_WHY for p in full["players"][first_unresolved:])
    starts = [p["start_utc"] for p in full["players"][first_unresolved:]]
    assert starts == sorted(starts)  # soonest game first
    n_tier0 = first_unresolved
    assert n_tier0 >= 1
    for cap in (n_tier0, n_tier0 + 1, n_tier0 + 3, 25):
        c = {**cfg, "research": {**cfg["research"], "max_players": cap}}
        req = packet.research_request(run0.run, c, now=BEFORE)
        assert len(req["players"]) <= cap
        listed_teams = {p["team"] for p in _unresolved(req)}
        assert all(sum(1 for p in _unresolved(req) if p["team"] == t) == 2 for t in listed_teams)  # a pair is never split
        assert set(req["unresolved_goalie_teams"]) == listed_teams
        # today's list is identical to the one without the new pairs: it was never displaced
        assert [p for p in req["players"] if p["why"] != packet.UNRESOLVED_WHY] == full["players"][:n_tier0]
    tight = {**cfg, "research": {**cfg["research"], "max_players": n_tier0 + 1}}
    req = packet.research_request(run0.run, tight, now=BEFORE)
    assert _unresolved(req) == [] and req["unresolved_goalie_teams_not_listed"]  # one slot left: no pair fits, and it is said


def test_b37_a_team_whose_starter_is_confirmed_adds_none(run0, monkeypatch):
    from nhl_dfs.contracts.statuses import GoalieState

    outside = sorted(SLATE_TEAMS - _portfolio_goalie_teams(run0.run))
    confirmed = outside[0]
    real = packet._role_state_now

    def fake(v, now):
        rs, note = real(v, now)
        goalies = dict(rs.goalies) if rs is not None else {}
        goalies[confirmed] = SimpleNamespace(state=GoalieState.CONFIRMED, confirmed="x", p_start={})
        return SimpleNamespace(goalies=goalies, persons=rs.persons if rs is not None else {}), note

    monkeypatch.setattr(packet, "_role_state_now", fake)
    req = packet.research_request(run0.run, now=BEFORE)
    assert confirmed not in {p["team"] for p in _unresolved(req)}
    assert set(outside) - {confirmed} == {p["team"] for p in _unresolved(req)}


def _with_starting(src, dst, name_fragment: str):
    out = []
    for line in src.read_bytes().split(b"\n"):
        body = line.rstrip(b"\r")
        if name_fragment.encode() in body and body.endswith(b",,"):
            line = body[:-1] + b"P" + line[len(body):]
        out.append(line)
    dst.write_bytes(b"\n".join(out))
    return dst


def test_b37_a_dk_starting_p_goalie_resolves_the_team(run0, tmp_path):
    outside = sorted(SLATE_TEAMS - _portfolio_goalie_teams(run0.run))
    team = outside[0]
    name = {"AAA": "Alpha", "BBB": "Bravo", "CCC": "Charlie", "DDD": "Delta", "EEE": "Echo", "FFF": "Foxtrot"}[team]
    sal = _with_starting(LS / "DKSalaries.csv", tmp_path / "DKSalaries.csv", f"{name} G1 (")
    r = run_slate(sal, LS / "DKEntries.template.csv", offline=True, baseline_only=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    assert r.ok, r.manifest["failed"]
    in_portfolio = _portfolio_goalie_teams(r.run)
    req = packet.research_request(r.run, now=BEFORE)
    teams = {p["team"] for p in _unresolved(req)}
    if team not in in_portfolio:  # DK names its starter: nothing to ask about that team
        assert team not in teams
    assert teams <= SLATE_TEAMS - in_portfolio - {team}


def test_b37_a_game_that_started_adds_no_goalies(run0):
    req = packet.research_request(run0.run, now=AFTER_FIRST)
    assert not any(p["team"] in {"AAA", "BBB"} for p in _unresolved(req))  # its rows are not selectable
    later = {"CCC", "DDD", "EEE", "FFF"} - _portfolio_goalie_teams(run0.run)
    assert later <= {p["team"] for p in _unresolved(req)} or req["unresolved_goalie_teams_not_listed"]


def test_b37_without_a_role_state_every_open_team_counts_as_unresolved(run0, monkeypatch):
    monkeypatch.setattr(packet, "_role_state_now", lambda v, now: (None, "role state unavailable (test)"))
    req = packet.research_request(run0.run, now=BEFORE)
    outside = SLATE_TEAMS - _portfolio_goalie_teams(run0.run)
    assert {p["team"] for p in _unresolved(req)} == outside
    assert all(p["current"] is None for p in req["players"]) and "unavailable" in req["state_note"]


# ---- the own-goalie rule: solver, builders, pins (flags 51 to 53) --------------------------------------------

import random

from nhl_dfs.build import candidates as cand
from nhl_dfs.build import milp
from nhl_dfs.build import own_goalie as og
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, Mode
from nhl_dfs.contracts.statuses import SearchStatus
from nhl_dfs.intake.salary import read_salary

G_SLOT = CLASSIC_SLOTS.index("G")


@pytest.fixture(scope="module")
def pool():
    return read_salary(LS / "DKSalaries.csv")


def _rows(pool, team, goalie=None):
    return [r for r in pool.rows if r.team == team and (goalie is None or r.is_goalie == goalie)]


def _invite(pool, team="AAA", opp="BBB", boost=50.0):
    """An objective that rewards a conflict: one goalie of `team` plus every skater of `opp`."""
    g = next(r for r in _rows(pool, team, True) if r.name.endswith("G1"))
    obj = {r.role_id: 1.0 for r in pool.rows}
    obj[g.role_id] += boost
    for r in _rows(pool, opp, False):
        obj[r.role_id] += boost
    return obj, g


def test_the_fixture_pool_knows_who_plays_whom(pool):
    assert og.opponent_map(pool) == {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC", "EEE": "FFF", "FFF": "EEE"}
    assert og.missing_opponents(pool) == set()


def test_without_the_rule_a_conflict_is_the_optimum_and_with_it_none_is_ever_returned(pool):
    obj, _ = _invite(pool)
    off = milp.solve_lineup(pool, Mode.CLASSIC, obj)
    assert off.lineup is not None and og.faces_own_goalie(off.lineup, pool)  # the detector can fail
    on = milp.solve_lineup(pool, Mode.CLASSIC, obj, avoid_own_goalie=True)
    assert on.status in (SearchStatus.FEASIBLE, SearchStatus.TIME_LIMIT_WITH_INCUMBENT) and on.lineup is not None
    assert not og.faces_own_goalie(on.lineup, pool) and on.objective_value <= off.objective_value + 1e-9
    rng = random.Random(7)
    for _ in range(25):
        rnd = {r.role_id: rng.uniform(0.0, 10.0) for r in pool.rows}
        res = milp.solve_lineup(pool, Mode.CLASSIC, rnd, avoid_own_goalie=True)
        assert res.lineup is not None and not og.faces_own_goalie(res.lineup, pool)


def test_the_constrained_optimum_equals_an_independent_formulation(pool):
    """For each goalie g: force g (a group row) and exclude every skater of g's opponent. The best of those 12 solves is the
    best compliant lineup; the rule row must reach the same value."""
    opp = og.opponent_map(pool)
    rng = random.Random(11)
    for trial in range(4):
        obj = {r.role_id: rng.uniform(0.0, 10.0) + (30.0 if trial % 2 else 0.0) * (r.team in ("BBB", "AAA")) for r in pool.rows}
        best = None
        for g in (r for r in pool.rows if r.is_goalie):
            ban = frozenset(r.role_id for r in pool.rows if not r.is_goalie and r.team == opp[g.team])
            res = milp.solve_lineup(pool, Mode.CLASSIC, obj, exclude=ban,
                                    groups=(milp.GroupConstraint(frozenset({g.role_id}), min_count=1),))
            if res.lineup is not None and (best is None or res.objective_value > best):
                best = res.objective_value
        got = milp.solve_lineup(pool, Mode.CLASSIC, obj, avoid_own_goalie=True)
        assert best is not None and got.objective_value == pytest.approx(best, abs=1e-6)


def test_every_family_of_a_groups_menu_gets_the_rule_not_only_the_base_family(pool):
    obj, _ = _invite(pool)
    goalies = sorted((r.role_id for r in pool.rows if r.is_goalie))
    menu = [(f"goalie:{g}", (milp.GroupConstraint(frozenset({g}), min_count=1),)) for g in goalies] + [("base", ())]
    kw = dict(seed=3, perturb_sd=1.5, groups_menu=menu, time_limit_total_s=60.0, min_pairwise_diff=2)
    off = cand.generate(pool, Mode.CLASSIC, obj, 39, **kw)
    assert any(og.faces_own_goalie(c.role_ids, pool) for c in off)  # negative control: the old builder does it
    on = cand.generate(pool, Mode.CLASSIC, obj, 39, avoid_own_goalie=True, **kw)
    assert len(on) >= 26
    assert {c.family for c in on} >= {f"goalie:{g}" for g in goalies[:4]} | {"base"}  # several families, base included
    assert not any(og.faces_own_goalie(c.role_ids, pool) for c in on)  # none of them, in any family


def test_a_pinned_goalie_cannot_be_given_an_opposing_skater_in_an_open_slot(pool):
    obj, g = _invite(pool)
    pins = {G_SLOT: g.role_id}
    res = milp.solve_lineup(pool, Mode.CLASSIC, obj, locked=pins, avoid_own_goalie=True)
    assert res.lineup is not None and res.lineup[G_SLOT] == g.role_id and not og.faces_own_goalie(res.lineup, pool)
    # an open slot only an opposing skater can fill: no legal compliant lineup; unpinned, the solver picks another goalie
    only_bbb_wingers = frozenset(r.role_id for r in pool.rows if r.position in ("LW", "RW") and r.team != "BBB")
    hard = milp.solve_lineup(pool, Mode.CLASSIC, obj, exclude=only_bbb_wingers, locked=pins, avoid_own_goalie=True)
    assert hard.status is SearchStatus.INFEASIBLE and hard.lineup is None
    freed = milp.solve_lineup(pool, Mode.CLASSIC, obj, exclude=only_bbb_wingers, avoid_own_goalie=True)
    assert freed.lineup is not None and not og.faces_own_goalie(freed.lineup, pool)
    assert og.faces_own_goalie(milp.solve_lineup(pool, Mode.CLASSIC, obj, exclude=only_bbb_wingers, locked=pins).lineup, pool)


def test_when_both_ends_are_pinned_the_conflict_is_allowed_and_nothing_more_is_added(pool):
    obj, g = _invite(pool)
    bbb_c = next(r for r in _rows(pool, "BBB", False) if r.position == "C")
    pins = {G_SLOT: g.role_id, 0: bbb_c.role_id}
    model = milp.LineupModel(pool, Mode.CLASSIC, locked=pins, avoid_own_goalie=True)
    assert model.own_goalie_forced == [("AAA", "BBB", 1)]
    res = model.solve(obj)
    assert res.lineup is not None and res.lineup[G_SLOT] == g.role_id and res.lineup[0] == bbb_c.role_id
    assert og.conflict_rows(res.lineup, pool) == [bbb_c.role_id]  # only the pinned skater, no second BBB skater
    # the audit calls it forced, and a conflict that is not between pinned cells RELAXED
    cfg = {"own_goalie": {"enabled": True, "families": ["large_gpp"]}}
    assert og.audit({"e1": res.lineup}, {"e1": "large_gpp"}, pool, cfg, pins={"e1": pins}).startswith("FORCED_BY_PINS(1")
    assert og.audit({"e1": res.lineup}, {"e1": "large_gpp"}, pool, cfg).startswith("RELAXED(1")
    assert og.audit({"e1": res.lineup}, {"e1": "cash"}, pool, cfg).startswith("NOT_APPLICABLE(")  # exempt, only counted


def test_showdown_is_out_the_model_refuses_and_the_default_is_untouched():
    from pool_builder import showdown_pool

    sd = showdown_pool()
    with pytest.raises(ValueError):
        milp.LineupModel(sd, Mode.SHOWDOWN, avoid_own_goalie=True)
    assert og.rule_families({"own_goalie": {"enabled": True, "families": ["large_gpp"]}}, Mode.SHOWDOWN) == frozenset()
    assert og.audit({}, {}, sd, {"own_goalie": {"enabled": True}}) == "NOT_APPLICABLE(Showdown)"
    # the rule row is absent unless asked: the same model, built by the default call, has the same row count
    c = milp.LineupModel(read_salary(LS / "DKSalaries.csv"), Mode.CLASSIC)
    on = milp.LineupModel(read_salary(LS / "DKSalaries.csv"), Mode.CLASSIC, avoid_own_goalie=True)
    assert c.A.shape[0] + 6 == on.A.shape[0] and c.A.shape[1] == on.A.shape[1]  # one row per goalie team, no new variable
    assert c.own_goalie_forced == [] and c.own_goalie_unknown == []


def test_a_pool_without_game_information_is_reported_not_passed_quietly():
    from pool_builder import classic_pool

    pl = classic_pool()
    assert og.opponent_map(pl) == {}
    cfg = {"own_goalie": {"enabled": True, "families": ["large_gpp"]}}
    res = milp.LineupModel(pl, Mode.CLASSIC, avoid_own_goalie=True)
    assert res.own_goalie_unknown and res.A.shape == milp.LineupModel(pl, Mode.CLASSIC).A.shape
    assert og.audit({"e": ["x"]}, {"e": "large_gpp"}, pl, cfg).startswith("NOT_EVALUATED(the pool has no game information")


# ---- selection (flag 51): rule contests take compliant candidates, relax in the written order --------------------

from dataclasses import dataclass, replace

import numpy as np

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import check_lineup, lineup_key
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.intake.salary import GameInfo
from nhl_dfs.models import contests as contests_mod
from pool_builder import classic_pool

RISK = ob.load_risk_config()
FAM = contests_mod.load_contest_families()
EXPO = exposure.load_exposure_config()
RULE = frozenset({"large_gpp", "small_field", "wta"})


@dataclass
class E:
    entry_id: str
    contest_id: str


def games_pool():
    """Four teams, two games (AAA@BBB, CCC@DDD), two goalies per team, flat salaries."""
    pl = classic_pool(("AAA", "BBB", "CCC", "DDD"), per_team={"C": 6, "LW": 4, "RW": 4, "D": 5, "G": 2})
    t = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
    return replace(pl, games={"AAA@BBB": GameInfo("BBB", "AAA", "", t), "CCC@DDD": GameInfo("DDD", "CCC", "", t)})


def mk(pool, used, goalie_team, plan):
    """One legal Classic lineup of rows nobody in `used` holds: plan is the team of each skater slot (C C W W W D D UTIL)."""
    out = []
    for slot, team in zip(CLASSIC_SLOTS, plan[:7] + [goalie_team] + plan[7:]):
        want = {"C": ("C",), "W": ("LW", "RW"), "D": ("D",), "UTIL": ("C",), "G": ("G",)}[slot]
        r = next(x for x in pool.rows if x.team == team and x.position in want and x.person_key not in used)
        used.add(r.person_key)
        out.append(r)
    assert check_lineup(out, Mode.CLASSIC).ok
    return Candidate(tuple(r.role_id for r in out), lineup_key(out, Mode.CLASSIC), 0.0, "central")


def _setup(order):
    """order: names of lineups to build. Returns pool, {name: Candidate}. X: AAA goalie + a BBB skater (a conflict).
    Y1: AAA goalie, no BBB skater. Y2: CCC goalie, no DDD skater. F: the field's lineup."""
    pool = games_pool()
    used: set[str] = set()
    plans = {"X": ("AAA", ["AAA", "BBB", "CCC", "CCC", "DDD", "DDD", "CCC", "AAA"]),
             "Y1": ("AAA", ["CCC", "AAA", "DDD", "DDD", "CCC", "CCC", "DDD", "AAA"]),
             "Y2": ("CCC", ["BBB", "AAA", "AAA", "BBB", "BBB", "AAA", "BBB", "CCC"]),
             "F": ("BBB", ["DDD", "CCC", "CCC", "DDD", "DDD", "CCC", "AAA", "CCC"])}
    c = {n: mk(pool, used, *plans[n]) for n in order}
    return pool, c


def _scen(pool, c, totals, S=800, seed=5):
    rng = np.random.default_rng(seed)
    fld = rng.integers(300, 700, S)
    names = list(c)
    noise = rng.integers(-100, 100, S)  # the field's lineup is noisy, so a better candidate is better by degrees
    return ob.ScenarioSet([r.role_id for r in pool.rows],
                          _base(pool, [c[n] for n in names],
                                [fld + totals.get(n, 0) + (noise if n == "F" else 0) for n in names]), "selection", 1)


def _base(pool, lineups, totals):
    ids = [r.role_id for r in pool.rows]
    col = {r: i for i, r in enumerate(ids)}
    base = np.zeros((len(totals[0]), len(ids)), np.int32)
    for lu, t in zip(lineups, totals):
        base[:, col[lu.role_ids[0]]] = t
    return base


def _contest(cid, family, size, prizes):
    p = np.asarray(prizes, np.int64)
    return ob.Contest(cid, cid, family, size, 100, p, np.zeros(len(p), bool), None, PayoutSource.PRIOR)


def _select(pool, c, cands, entries, contests, fields, rule, totals):
    scen = _scen(pool, c, totals)
    caps = exposure.caps(EXPO, len(entries), pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    return pf.select([c[n] for n in cands], scen, fields, contests, entries, caps, pf.RiskBudget(1.0, None, None, None),
                     seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, rule_families=rule)


def _fields(c, with_cash=False):
    f = c["F"]
    out = {"G": ob.FieldSpec([f.role_ids], [f.key], np.asarray([1]), 198, "weighted", np.asarray([198], np.int64))}
    if with_cash:
        out["C"] = ob.FieldSpec([f.role_ids], [f.key], np.asarray([1]), 8, "weighted", np.asarray([8], np.int64))
    return out


GPP = {"G": _contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37)}
GPP_CASH = {**GPP, "C": _contest("C", "cash", 10, [180] * 4)}
TOT = {"X": 60, "Y1": 30, "Y2": 20}


def test_the_fixture_lineups_are_what_the_tests_say():
    pool, c = _setup(["X", "Y1", "Y2", "F"])
    assert og.faces_own_goalie(c["X"].role_ids, pool) and not og.faces_own_goalie(c["Y1"].role_ids, pool)
    assert not og.faces_own_goalie(c["Y2"].role_ids, pool)


def test_a_rule_family_entry_never_takes_a_conflicted_candidate_and_without_the_rule_it_does():
    pool, c = _setup(["X", "Y1", "Y2", "F"])
    entries = [E("e1", "G"), E("e2", "G")]
    off = _select(pool, c, ["X", "Y1", "Y2"], entries, GPP, _fields(c), frozenset(), TOT)
    assert c["X"].role_ids in set(off.by_entry.values())  # negative control: X is the best and is chosen
    on = _select(pool, c, ["X", "Y1", "Y2"], entries, GPP, _fields(c), RULE, TOT)
    assert set(on.by_entry.values()) == {c["Y1"].role_ids, c["Y2"].role_ids}
    assert not any(r.kind == "OWN_GOALIE" for r in on.relaxations)
    assert on.screened["candidates_in"] == 3


def test_a_cash_entry_is_exempt_even_when_the_rule_is_on():
    pool, c = _setup(["X", "Y1", "Y2", "F"])
    entries = [E("e1", "G"), E("e2", "C")]
    sel = _select(pool, c, ["X", "Y1", "Y2"], entries, GPP_CASH, _fields(c, True), RULE, {"X": 80, "Y1": 30, "Y2": 20})
    assert sel.by_entry["e1"] != c["X"].role_ids  # the GPP entry cannot hold it
    assert og.faces_own_goalie(sel.by_entry["e2"], pool)  # the cash entry may, and the best candidate is X


def test_an_unrelated_rule_family_changes_nothing():
    pool, c = _setup(["X", "Y1", "Y2", "F"])
    entries = [E("e1", "G"), E("e2", "G")]
    a = _select(pool, c, ["X", "Y1", "Y2"], entries, GPP, _fields(c), frozenset(), TOT)
    b = _select(pool, c, ["X", "Y1", "Y2"], entries, GPP, _fields(c), frozenset({"wta"}), TOT)  # no wta contest here
    assert (a.by_entry, a.chosen_kappa, [p.record() for p in a.frontier]) == (b.by_entry, b.chosen_kappa, [p.record() for p in b.frontier])


def test_the_rule_is_relaxed_before_a_lineup_is_repeated_and_the_entry_always_gets_a_pick():
    pool, c = _setup(["X", "Y1", "F"])
    entries = [E("e1", "G"), E("e2", "G")]
    sel = _select(pool, c, ["X", "Y1"], entries, GPP, _fields(c), RULE, {"X": 60, "Y1": 30})
    assert set(sel.by_entry.values()) == {c["X"].role_ids, c["Y1"].role_ids}  # a conflicted lineup beats a duplicate
    kinds = [r.kind for r in sel.relaxations]
    assert kinds.count("OWN_GOALIE") == 1 and "REPEAT" not in kinds
    # only conflicted candidates: both entries are served (the second is a repeat), nothing raises
    only = _select(pool, c, ["X"], entries, GPP, _fields(c), RULE, {"X": 60})
    assert set(only.by_entry.values()) == {c["X"].role_ids}
    assert [r.kind for r in only.relaxations].count("OWN_GOALIE") == 2
    # only a compliant candidate and three entries: repeats stay compliant (level 4 tries compliant first)
    pool2, c2 = _setup(["Y1", "F"])
    rep = _select(pool2, c2, ["Y1"], [E("e1", "G"), E("e2", "G"), E("e3", "G")], GPP, _fields(c2), RULE, {"Y1": 30})
    assert set(rep.by_entry.values()) == {c2["Y1"].role_ids} and not any(r.kind == "OWN_GOALIE" for r in rep.relaxations)


# ---- late swap and the QA re-solve (flags 51, 52): pins, the fallback order, the alternatives ---------------------

from nhl_dfs.build import late_swap


def _solve(pool, obj, pins, *, own, exclude=frozenset(), steps=None, fast=False, before=None):
    return late_swap._solve_entry(pool, Mode.CLASSIC, obj, before or [None] * 9, pins, fast=fast, exclude_rows=exclude,
                                  capped_rows=frozenset(), overlaps=[], time_limit_s=5.0, steps=steps, own_goalie=own)


def test_a_late_swap_solve_keeps_the_pinned_goalie_and_avoids_the_opposing_skaters(pool):
    obj, g = _invite(pool)
    pins = {G_SLOT: g.role_id}
    steps: list = []
    lineup, route, relax, _ = _solve(pool, obj, pins, own=True, steps=steps)
    assert route == "milp" and lineup[G_SLOT] == g.role_id and not og.faces_own_goalie(lineup, pool) and relax == ()
    assert steps and steps[-1][2] is True  # the step that produced it held the rule
    off, _, _, _ = _solve(pool, obj, pins, own=False)
    assert og.faces_own_goalie(off, pool)  # negative control: the solve without the rule takes the conflict
    # the alternatives (the QA and late swap choice set) are built under the same rule
    alts = late_swap._alternatives(pool, Mode.CLASSIC, obj, [None] * 9, pins, lineup, fast=False, exclude_rows=frozenset(),
                                   step=steps[-1], k=4, time_limit_s=5.0)
    assert len(alts) == 4 and all(a[G_SLOT] == g.role_id and not og.faces_own_goalie(a, pool) for a in alts)


def test_when_no_compliant_repair_exists_the_rule_is_dropped_last_and_named(pool):
    obj, g = _invite(pool)
    pins = {G_SLOT: g.role_id}
    only_bbb_wingers = frozenset(r.role_id for r in pool.rows if r.position in ("LW", "RW") and r.team != "BBB")
    steps: list = []
    lineup, route, relax, detail = _solve(pool, obj, pins, own=True, exclude=only_bbb_wingers, steps=steps)
    assert route == "milp" and lineup is not None and lineup[G_SLOT] == g.role_id  # a legal entry, never an empty one
    assert relax == ("OWN_GOALIE",) and og.faces_own_goalie(lineup, pool) and steps[-1][2] is False
    alts = late_swap._alternatives(pool, Mode.CLASSIC, obj, [None] * 9, pins, lineup, fast=False, exclude_rows=only_bbb_wingers,
                                   step=steps[-1], k=3, time_limit_s=5.0)
    assert len(alts) == 3 and all(a[G_SLOT] == g.role_id for a in alts)
    # the family is not a rule family: today's behavior, no relaxation recorded
    plain, _, relax2, _ = _solve(pool, obj, pins, own=False, exclude=only_bbb_wingers)
    assert plain is not None and relax2 == ()


def test_a_late_swap_with_both_ends_pinned_returns_a_legal_entry_and_changes_no_pinned_cell(pool):
    obj, g = _invite(pool)
    bbb_c = next(r for r in _rows(pool, "BBB", False) if r.position == "C")
    pins = {G_SLOT: g.role_id, 0: bbb_c.role_id}
    lineup, route, relax, _ = _solve(pool, obj, pins, own=True)
    assert route == "milp" and lineup[G_SLOT] == g.role_id and lineup[0] == bbb_c.role_id
    assert og.conflict_rows(lineup, pool) == [bbb_c.role_id] and relax == ()  # forced by pins: kept, no second one


def test_the_started_game_rows_keep_their_opponents_so_the_audit_still_sees_a_pinned_conflict(pool):
    """A game that has started leaves pool.games; its pinned rows still carry Game Info."""
    from dataclasses import replace

    trimmed = replace(pool, games={k: g for k, g in pool.games.items() if k != "AAA@BBB"})
    assert og.opponent_map(trimmed)["AAA"] == "BBB"


# ---- end to end: the old tree builds a skater against its own goalie, the new tree cannot -----------------------

from nhl_dfs.build.manifest import read_manifest
from pool_builder import clone_entries

SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _full_run(tmp, monkeypatch, rule_on: bool):
    from nhl_dfs.build import objectives as obj_mod

    cfg = obj_mod.load_risk_config()
    cfg["own_goalie"]["enabled"] = rule_on
    monkeypatch.setattr(obj_mod, "load_risk_config", lambda *a, **k: cfg)
    many = tmp / "DKEntries.csv"
    clone_entries(LS / "DKEntries.template.csv", many, 40)
    r = run_slate(LS / "DKSalaries.csv", many, offline=True, out_root=tmp / "runs", outputs_root=tmp / "outputs",
                  clock=lambda: BEFORE, baseline_only=False, scenario=True, scenario_n=SMALL)
    assert r.ok, r.manifest["failed"]
    v = packet.RunView(r.run)
    return r, [og.conflict_rows(lu, v.pool) for lu in v.lineups.values()]


def test_end_to_end_the_old_behavior_holds_conflicts_and_the_rule_removes_them(tmp_path_factory, monkeypatch):
    off, bad_off = _full_run(tmp_path_factory.mktemp("e2e_off"), monkeypatch, False)
    on, bad_on = _full_run(tmp_path_factory.mktemp("e2e_on"), monkeypatch, True)
    st_on = read_manifest(on.run)["statuses"]["OWN_GOALIE"]
    assert read_manifest(off.run)["statuses"]["OWN_GOALIE"].startswith("OFF(")
    assert st_on.startswith("OK (") and "0 face their own goalie" in st_on, st_on
    assert any(p["phase"] == "A" for p in on.manifest["versions"])  # v1 is family-blind: it says so, and counts, but judges nothing
    assert not any(bad_on), "the new tree must not publish a skater against its own goalie in a rule family"
    from nhl_dfs.intake.entries import read_entries

    v2 = read_entries(on.run.version_file(2))  # the provisional version obeys it too (the first, family-blind baseline need not)
    pool_on = packet.RunView(on.run).pool
    assert not any(og.conflict_rows(late_swap._canonical(v2, e), pool_on) for e in v2.entries)
    assert any(bad_off), "negative control: with the rule off this slate's lineups do hold one"
    assert on.statuses["OWN_GOALIE"] == st_on  # it is in the printed statuses


# ---- the QA controller: a swap that creates a conflict is refused (flag 52) -------------------------------------

import json
import shutil

from nhl_dfs.build import controller
from nhl_dfs.build.state import open_run
from nhl_dfs.contracts.geometry import check_lineup, slot_accepts


@pytest.fixture(scope="module")
def ctrl_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c20_ctrl")
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=False, scenario=True,
                  scenario_n=SMALL, out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: BEFORE)
    assert r.ok and r.manifest["scenario"]["version"], r.manifest["failed"]
    return tmp, r


def _swap_reply(eid, out_r, in_r):
    return json.dumps([{"kind": "strategic", "target": {"entry_id": eid},
                        "change": {"type": "swap", "entry_id": eid, "out_role_id": out_r, "in_role_id": in_r},
                        "evidence": "", "source_url": None}])


def _legal_swap(v, creates_conflict: bool):
    """A legal one-cell swap in some entry of the run that does (or does not) put a skater against the entry's goalie."""
    for eid, lu in v.lineups.items():
        for k, out_r in enumerate(lu):
            if v.row(out_r).is_goalie:
                continue
            for cand in v.pool.rows:
                if cand.is_goalie or cand.role_id in lu or cand.person_key == v.row(out_r).person_key:
                    continue
                if not slot_accepts(v.slots[k], cand, Mode.CLASSIC):
                    continue
                new = [cand.role_id if x == out_r else x for x in lu]
                if not check_lineup([v.row(x) for x in new], Mode.CLASSIC).ok:
                    continue
                made = bool(set(og.conflict_rows(new, v.pool)) - set(og.conflict_rows(lu, v.pool)))
                if made == creates_conflict:
                    return eid, out_r, cand.role_id
    return None


def test_a_qa_swap_that_puts_a_skater_against_the_entrys_own_goalie_is_refused(ctrl_run, tmp_path):
    tmp, r = ctrl_run
    runs = tmp_path / "runs"
    shutil.copytree(r.run.path, runs / r.run.run_id)
    run = open_run(runs, r.run.run_id)
    v = packet.RunView(run, runs)
    bad = _legal_swap(v, True)
    assert bad, "the fixture should allow a legal swap that creates a conflict"
    res = controller.apply_round(run, 1, _swap_reply(*bad), now=BEFORE, runs_root=runs, outputs_root=tmp_path / "outputs",
                                 clock=lambda: BEFORE)
    d = res.decisions[0]
    assert d["status"] == "rejected" and "own goalie" in d["reason"], d
    assert res.published_version is None
    # a legal swap that makes no conflict is never refused for this reason (the comparison may still decline it); a round
    # is applied once per run, so this goes to a fresh copy
    runs2 = tmp_path / "runs2"
    shutil.copytree(r.run.path, runs2 / r.run.run_id)
    ok = _legal_swap(v, False)
    res2 = controller.apply_round(open_run(runs2, r.run.run_id), 1, _swap_reply(*ok), now=BEFORE, runs_root=runs2,
                                  outputs_root=tmp_path / "outputs2", clock=lambda: BEFORE)
    assert res2.decisions and "own goalie" not in res2.decisions[0]["reason"]


# ---- what must not change: defaults, the field sampler, the printed lines -------------------------------------

def test_the_rule_is_off_by_default_in_every_builder_and_the_field_sampler_never_asks():
    import inspect
    from pathlib import Path

    for fn in (milp.LineupModel.__init__, milp.solve_lineup, cand.generate):
        assert inspect.signature(fn).parameters["avoid_own_goalie"].default is False
    assert inspect.signature(late_swap._solve_entry).parameters["own_goalie"].default is False
    models = Path(__file__).resolve().parents[1] / "src" / "nhl_dfs" / "models"
    assert [p.name for p in models.glob("*.py") if "avoid_own_goalie" in p.read_text(encoding="utf-8")] == []


def test_the_report_line_is_printed_beside_the_other_risk_lines():
    from nhl_dfs import cli

    assert "OWN_GOALIE" in cli.REPORTED_EXTRA and "RISK_BUDGET" in cli.REPORTED_EXTRA and "MARKET_COVERAGE" in cli.REPORTED_EXTRA


def test_the_config_validates_and_the_rule_ships_on_for_the_three_families():
    from nhl_dfs.build import objectives as obj_mod

    cfg = obj_mod.load_risk_config()
    assert og.settings(cfg) == (True, frozenset({"large_gpp", "small_field", "wta"}))
    assert "cash" not in og.rule_families(cfg, Mode.CLASSIC) and "satellite" not in og.rule_families(cfg, Mode.CLASSIC)
    bad = {**cfg, "own_goalie": {"enabled": "yes", "families": ["large_gpp"]}}
    with pytest.raises(ValueError):
        obj_mod.validate_risk_config(bad)
    with pytest.raises(ValueError):
        obj_mod.validate_risk_config({**cfg, "own_goalie": {"enabled": True, "families": ["lottery"]}})
