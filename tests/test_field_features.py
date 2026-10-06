"""C17: the slate's odds, role state, news ages and goalie confirmations reach the ownership prior (B63, B20, B93).

models/ownership.feature_table has weights for implied_total, goalie_start_win, pp1, line1 and news_recent. Before C17 no
run passed any of their inputs in, so each weight always read zero. These tests pin, per weight, that the perceived value
moves by exactly weight x feature when its input is present, does not move when it is absent, and that the sampled field
(the forecast) follows. `field_inputs.collect` is tested for the rules that decide what counts (verified team codes and the
odds age limit, usable pages, news ages, confirmed goalies only). The full-run tests show the wiring: the provisional
pass, the scenario pass fallback (which also now prices a contest on a cached C16 template, B93), the manifest line and
the off-switch.
"""

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import TESTS
from nhl_dfs.build import provisional as prov
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.data.sources.nhl import GameOdds, OddsSnapshot
from nhl_dfs.intake.salary import GameInfo
from nhl_dfs.models import field_inputs, ownership
from nhl_dfs.models.projection import PriorProjection
from nhl_dfs.models.roles import GoalieRole, PersonRole, RoleState
from pool_builder import varied_pool

pytestmark = pytest.mark.c17

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
START = T0 + timedelta(hours=7)
TEAMS = ("BOS", "MTL", "COL", "LAK")  # real DK codes: odds only match a verified team code
FAM = "large_gpp"
CID = "c1"
LS = TESTS / "fixtures" / "late_swap" / "classic"
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}  # the C8 full-run tests' sizes


@pytest.fixture(scope="module")
def cfg():
    c = copy.deepcopy(ownership.load_ownership_config())
    c["field"]["n"]["classic"] = 120
    return c


@pytest.fixture(scope="module")
def pool():
    p = varied_pool(Mode.CLASSIC, teams=TEAMS)
    games = {"BOS@MTL": GameInfo("MTL", "BOS", "7:00PM ET", START), "LAK@COL": GameInfo("COL", "LAK", "7:00PM ET", START)}
    return replace(p, games=games)


@pytest.fixture(scope="module")
def proj(pool):
    return PriorProjection(pool)


def game(home, away, home_ml, away_ml, total):
    return GameOdds("g", home, away, START, home_ml, away_ml, None, None, None, total, None, None, None, None)


def snapshot(*games, source="nhl_partner_odds", as_of=T0 - timedelta(hours=1)):
    return OddsSnapshot(as_of_utc=as_of, as_of_basis="source", book="test", source=source, games=list(games))


# COL@home is the slate's high-total game and a heavy favourite, BOS@MTL a low-total coin flip
ODDS = snapshot(game("MTL", "BOS", -110, -110, 5.0), game("COL", "LAK", -250, 210, 7.0))


def skaters(pool, team):
    return [r for r in pool.rows if r.team == team and not r.is_goalie]


def goalies(pool, team):
    return sorted((r for r in pool.rows if r.team == team and r.is_goalie), key=lambda r: (-r.salary, r.role_id))


def role_state(pool, *, lines=(), pp=(), news=(), confirmed=(), usable=TEAMS):
    """A hand-built role state. lines: (role_id, line); pp: (role_id, unit); news: (role_id, hours before T0);
    confirmed: (team, goalie role_id, hours before T0 of the confirmation)."""
    rs = RoleState(now_utc=T0)
    people = {r.person_key: r for r in pool.rows}
    for pk, r in people.items():
        rs.persons[pk] = PersonRole(pk, r.team, "G" if r.is_goalie else ("D" if r.position == "D" else "F"))
    by_id = pool.by_role_id
    for rid, ln in lines:
        rs.persons[by_id[rid].person_key].line = ln
        rs.persons[by_id[rid].person_key].df_listed = True
    for rid, unit in pp:
        rs.persons[by_id[rid].person_key].pp_unit = unit
    for rid, h in news:
        rs.persons[by_id[rid].person_key].news_utc = T0 - timedelta(hours=h)
    for t in usable:
        rs.team_pages[t] = {"updated_utc": T0, "age_h": 1.0, "usable": True, "source": "test", "low_confidence": False}
    for team in {r.team for r in pool.rows if r.is_goalie}:
        gs = goalies(pool, team)
        rs.goalies[team] = GoalieRole(team, GoalieState.EXPECTED, {g.person_key: 1.0 / len(gs) for g in gs})
    for team, rid, h in confirmed:
        gr = rs.goalies[team]
        pk = by_id[rid].person_key
        gr.state, gr.confirmed, gr.named = GoalieState.CONFIRMED, pk, pk
        gr.p_start = {k: 1.0 if k == pk else 0.0 for k in gr.p_start}
        gr.confirmed_at = T0 - timedelta(hours=h)
    return rs


def util(pool, proj, cfg, inputs=None):
    kw = inputs.feature_kwargs() if inputs is not None else {}
    feats = ownership.feature_table(pool, proj, cfg=cfg, **kw)
    return ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, FAM)), feats


def forecast(pool, proj, cfg, inputs=None, seed=17):
    """Sampled ownership (% of lineups holding each role) of one large_gpp contest: the forecast."""
    ctx = {CID: SimpleNamespace(family=FAM, field_size=1000)}
    fb = prov.build_fields(pool, proj, ctx, seed=seed, own_cfg=cfg, inputs=inputs)
    return fb.marginals[CID].own, fb


@pytest.fixture(scope="module")
def base_util(pool, proj, cfg):
    return util(pool, proj, cfg)[0]


@pytest.fixture(scope="module")
def base_own(pool, proj, cfg):
    return forecast(pool, proj, cfg)[0]


def collect(pool, cfg, **kw):
    return field_inputs.collect(pool, now=T0, own_cfg=cfg, **kw)


def same(a, b):
    return a.keys() == b.keys() and all(a[k] == pytest.approx(b[k]) for k in a)


# -- one test per weight: the input moves the perceived value by exactly weight x feature, and the forecast follows ------------

def test_implied_total_moves_the_forecast_and_is_inert_without_odds(pool, proj, cfg, base_util, base_own):
    w = ownership.family_weights(cfg, FAM)["implied_total"]
    inputs = collect(pool, cfg, snapshot=ODDS)
    u, f = util(pool, proj, cfg, inputs)
    col, bos = skaters(pool, "COL")[0], skaters(pool, "BOS")[0]
    assert f[col.role_id]["implied_total"] > 0 > f[bos.role_id]["implied_total"]
    for r in pool.rows:  # exactly weight x feature, plus the goalie weight on a goalie (odds also give win probabilities)
        if not r.is_goalie:
            assert u[r.role_id] - base_util[r.role_id] == pytest.approx(w * f[r.role_id]["implied_total"])
    own, _ = forecast(pool, proj, cfg, inputs)
    assert sum(own[r.role_id] for r in skaters(pool, "COL")) > sum(base_own[r.role_id] for r in skaters(pool, "COL"))
    assert sum(own[r.role_id] for r in skaters(pool, "BOS")) < sum(base_own[r.role_id] for r in skaters(pool, "BOS"))
    # absent: no snapshot, an old one, and the off-switch all leave the perceived value untouched
    assert same(util(pool, proj, cfg, collect(pool, cfg, snapshot=None))[0], base_util)
    old = snapshot(game("COL", "LAK", -250, 210, 7.0), as_of=T0 - timedelta(hours=40))
    assert same(util(pool, proj, cfg, collect(pool, cfg, snapshot=old))[0], base_util)
    off = {**cfg, "field_inputs": {"enabled": False}}
    assert same(util(pool, proj, cfg, field_inputs.collect(pool, snapshot=ODDS, now=T0, own_cfg=off))[0], base_util)


def test_goalie_start_win_follows_the_win_probability_and_a_confirmation(pool, proj, cfg, base_util, base_own):
    wt = ownership.family_weights(cfg, FAM)
    col_top, col_backup = goalies(pool, "COL")[:2]
    # odds alone: COL (a -250 favourite) has the better top goalie; a goalie also carries his team's implied_total
    u, f = util(pool, proj, cfg, collect(pool, cfg, snapshot=ODDS))
    fg = f[col_top.role_id]
    assert fg["goalie_start_win"] > 0
    assert u[col_top.role_id] - base_util[col_top.role_id] == pytest.approx(
        wt["goalie_start_win"] * fg["goalie_start_win"] + wt["implied_total"] * fg["implied_total"])
    # a confirmation: the confirmed backup counts as a certain starter and the other goalie as a certain non-starter
    rs = role_state(pool, confirmed=[("COL", col_backup.role_id, 2.0)])
    inputs = collect(pool, cfg, role_state=rs)
    assert inputs.goalie_start == {col_backup.person_key: 1.0, col_top.person_key: 0.0}
    u2, f2 = util(pool, proj, cfg, inputs)
    even = cfg["goalie_start_prior"]["top_salary_on_team"] * 0.5
    assert f2[col_backup.role_id]["goalie_start_win"] == pytest.approx(0.5 - even)
    assert f2[col_top.role_id]["goalie_start_win"] == pytest.approx(0.0 - even)
    assert u2[col_backup.role_id] > base_util[col_backup.role_id] and u2[col_top.role_id] < base_util[col_top.role_id]
    own, _ = forecast(pool, proj, cfg, inputs)
    assert own[col_backup.role_id] > base_own[col_backup.role_id] and own[col_top.role_id] < base_own[col_top.role_id]
    # absent: every other goalie keeps the expected-starter prior, and an unconfirmed state adds nothing
    assert all(u2[r.role_id] == pytest.approx(base_util[r.role_id]) for r in pool.rows if r.is_goalie and r.team != "COL")
    assert collect(pool, cfg, role_state=role_state(pool)).goalie_start == {}


def test_pp1_moves_the_forecast_only_for_unit_one(pool, proj, cfg, base_util, base_own):
    w = ownership.family_weights(cfg, FAM)["pp1"]
    a, b, c = skaters(pool, "LAK")[:3]
    rs = role_state(pool, pp=[(a.role_id, 1), (b.role_id, 2)])
    inputs = collect(pool, cfg, role_state=rs)
    u, f = util(pool, proj, cfg, inputs)
    assert (f[a.role_id]["pp1"], f[b.role_id]["pp1"], f[c.role_id]["pp1"]) == (1.0, 0.0, 0.0)
    assert u[a.role_id] - base_util[a.role_id] == pytest.approx(w)
    assert all(u[r.role_id] == pytest.approx(base_util[r.role_id]) for r in pool.rows if r.role_id != a.role_id)
    own, _ = forecast(pool, proj, cfg, inputs)
    assert own[a.role_id] > base_own[a.role_id]
    assert same(util(pool, proj, cfg, collect(pool, cfg, role_state=role_state(pool)))[0], base_util)  # a state with no units


def test_line1_moves_the_forecast_for_line_one_forwards_and_first_pairs_only(pool, proj, cfg, base_util, base_own):
    w = ownership.family_weights(cfg, FAM)["line1"]
    sk = skaters(pool, "MTL")
    fwd = next(r for r in sk if r.position != "D")
    d = next(r for r in sk if r.position == "D")
    third = next(r for r in sk if r.position != "D" and r.role_id != fwd.role_id)
    rs = role_state(pool, lines=[(fwd.role_id, 1), (d.role_id, 1), (third.role_id, 3)])
    inputs = collect(pool, cfg, role_state=rs)
    u, f = util(pool, proj, cfg, inputs)
    assert (f[fwd.role_id]["line1"], f[d.role_id]["line1"], f[third.role_id]["line1"]) == (1.0, 1.0, 0.0)
    assert u[fwd.role_id] - base_util[fwd.role_id] == pytest.approx(w)
    assert u[third.role_id] == pytest.approx(base_util[third.role_id])
    own, _ = forecast(pool, proj, cfg, inputs)
    assert own[fwd.role_id] > base_own[fwd.role_id]
    assert same(util(pool, proj, cfg, collect(pool, cfg, role_state=role_state(pool)))[0], base_util)


def test_news_recent_counts_only_news_inside_the_window(pool, proj, cfg, base_util, base_own):
    w = ownership.family_weights(cfg, FAM)["news_recent"]
    hours = cfg["news_recent_hours"]
    fresh, stale, edge, future = skaters(pool, "BOS")[:4]
    rs = role_state(pool, news=[(fresh.role_id, 1.0), (stale.role_id, hours + 7.0), (edge.role_id, hours), (future.role_id, -2.0)])
    inputs = collect(pool, cfg, role_state=rs)
    assert inputs.news_age_h[future.role_id] == 0.0  # a timestamp ahead of the clock is "now", never negative
    u, f = util(pool, proj, cfg, inputs)
    assert [f[r.role_id]["news_recent"] for r in (fresh, stale, edge, future)] == [1.0, 0.0, 1.0, 1.0]
    assert u[fresh.role_id] - base_util[fresh.role_id] == pytest.approx(w)
    assert u[stale.role_id] == pytest.approx(base_util[stale.role_id])
    own, _ = forecast(pool, proj, cfg, inputs)
    assert own[fresh.role_id] > base_own[fresh.role_id]
    assert same(util(pool, proj, cfg, collect(pool, cfg, role_state=role_state(pool)))[0], base_util)  # no news anywhere


def test_a_goalie_confirmation_is_news_and_the_latest_item_wins(pool, cfg):
    g = goalies(pool, "LAK")[1]
    rs = role_state(pool, news=[(g.role_id, 5.0)], confirmed=[("LAK", g.role_id, 0.5)])
    assert collect(pool, cfg, role_state=rs).news_age_h[g.role_id] == pytest.approx(0.5)


# -- collect: what counts -------------------------------------------------------------------------------------------------------

def test_odds_match_only_verified_codes_and_are_relabelled_with_the_pools_codes(pool, cfg):
    covers = snapshot(game("MON", "BOS", -110, -110, 5.0), game("AAA", "BBB", -150, 130, 6.0), source="covers")  # Covers calls MTL "MON"
    got = collect(pool, cfg, snapshot=covers)
    assert [(g.home_abbrev, g.away_abbrev) for g in got.odds.games] == [("MTL", "BOS")]
    assert got.coverage["odds"]["games"] == 1 and got.coverage["odds"]["of"] == 2
    assert "LAK@COL" in got.coverage["odds"]["unmatched"]
    assert "not on the nhl_partner_odds board" in collect(pool, cfg, snapshot=snapshot(game("AAA", "BBB", -150, 130, 6.0))).coverage["odds"]["why"]
    ok = collect(pool, cfg, snapshot=ODDS)
    assert ok.coverage["odds"]["games"] == 2 and "MARKET=2/2" in ok.coverage_line()
    stale = collect(pool, cfg, snapshot=snapshot(game("COL", "LAK", -250, 210, 7.0), as_of=T0 - timedelta(hours=40)))
    assert stale.odds is None and "older than 36 h" in stale.coverage["odds"]["why"]  # market.max_age_h, as the simulator


def test_a_team_code_that_is_not_verified_never_matches(pool, cfg, monkeypatch):
    from nhl_dfs.sim import market

    monkeypatch.setattr(market, "_code_maps", lambda path=None: {"nhl_partner_odds": {"MTL": "MTL", "BOS": "BOS"}, "covers": {}})
    got = collect(pool, cfg, snapshot=ODDS)  # COL and LAK are not in the verified map
    assert got.coverage["odds"]["games"] == 1
    assert "unverified team code" in got.coverage["odds"]["unmatched"]["LAK@COL"]


def test_roles_come_from_usable_pages_and_the_line_says_how_much_there_is(pool, cfg):
    a, b = skaters(pool, "COL")[:2]
    rs = role_state(pool, lines=[(a.role_id, 1)], pp=[(a.role_id, 1), (b.role_id, 2)], news=[(b.role_id, 1.0)],
                    usable=("COL", "LAK"), confirmed=[("COL", goalies(pool, "COL")[0].role_id, 0.2)])
    got = collect(pool, cfg, snapshot=ODDS, role_state=rs)
    assert got.roles[a.role_id] == {"pp1": True, "line": 1} and got.roles[b.role_id] == {"pp1": False, "line": None}
    line = got.coverage_line()
    assert line.startswith("FIELD_INPUTS MARKET=2/2 ROLES=2/4 ")
    assert "1 players on PP1 and 1 on line 1" in line and "2 of 2 players with news are within 3 h" in line
    assert "1 of 4 goalie teams confirmed" in line
    assert json.loads(json.dumps(got.record()))["line"] == line  # the manifest copy is plain JSON
    assert "no odds snapshot" in collect(pool, cfg).coverage_line() and "MARKET=0/2" in collect(pool, cfg).coverage_line()


def test_a_team_without_a_usable_page_reads_as_the_covered_average_not_as_a_non_member(pool, proj, cfg, base_util):
    """Only COL and LAK have usable pages. Scoring BOS and MTL skaters as "not on PP1 or line 1" would hand COL and LAK a
    boost that is only our data gap (on 09-30 three of six pages were dropped by the age policy), so each of their skaters
    reads as the covered share of his position group."""
    cov_f = [r for t in ("COL", "LAK") for r in skaters(pool, t) if r.position != "D"]
    pp, ln = cov_f[0], cov_f[1]
    rs = role_state(pool, pp=[(pp.role_id, 1)], lines=[(ln.role_id, 1)], usable=("COL", "LAK"))
    got = collect(pool, cfg, role_state=rs)
    unknown = {r.role_id for t in ("BOS", "MTL") for r in skaters(pool, t)}
    assert set(got.imputed) == unknown and got.coverage["roles"]["imputed_teams"] == 2
    _, f = util(pool, proj, cfg, got)
    fwd = next(r for r in skaters(pool, "BOS") if r.position != "D")
    d = next(r for r in skaters(pool, "BOS") if r.position == "D")
    assert f[fwd.role_id]["pp1"] == pytest.approx(1 / len(cov_f)) and f[fwd.role_id]["line1"] == pytest.approx(1 / len(cov_f))
    assert f[d.role_id]["pp1"] == 0.0 and f[d.role_id]["line1"] == 0.0  # no covered defenseman is on PP1 or pair 1 here
    assert f[pp.role_id]["pp1"] == 1.0  # a covered player keeps his own value
    assert "2 team(s) without a usable page read as the covered average" in got.coverage_line()
    # every team covered, or none: nothing to impute (a constant shift of the whole pool changes no lineup)
    assert collect(pool, cfg, role_state=role_state(pool, pp=[(pp.role_id, 1)])).imputed == {}
    none = collect(pool, cfg, role_state=role_state(pool, usable=()))
    assert none.imputed == {} and same(util(pool, proj, cfg, none)[0], base_util)


def test_the_off_switch_hands_over_nothing_and_the_forecast_is_the_old_one(pool, proj, cfg, base_own):
    off = {**cfg, "field_inputs": {"enabled": False}}
    rs = role_state(pool, pp=[(skaters(pool, "LAK")[0].role_id, 1)])
    got = field_inputs.collect(pool, snapshot=ODDS, role_state=rs, now=T0, own_cfg=off)
    assert got.odds is None and not got.roles and not got.news_age_h and not got.goalie_start
    assert got.coverage_line().startswith("FIELD_INPUTS=OFF")
    own, fb = forecast(pool, proj, off, got)
    assert same(own, base_own)  # same seed, same table: the pre-C17 forecast exactly
    with pytest.raises(ValueError, match="field_inputs.enabled"):
        ownership.validate_ownership_config({**cfg, "field_inputs": {"enabled": "yes"}})


def test_the_field_build_keeps_its_feature_table_for_the_scenario_pass_to_grow_on(pool, proj, cfg):
    _, fb = forecast(pool, proj, cfg, collect(pool, cfg, snapshot=ODDS))
    assert fb.feats is not None and fb.inputs is not None
    col = skaters(pool, "COL")[0]
    assert fb.feats[col.role_id]["implied_total"] > 0  # the table the first draws came from carries the odds


# -- the run: provisional pass, scenario fallback, manifest, RUN_NOTES, the off-switch -------------------------------------------

def _stub_roles(monkeypatch):
    """Make the run's role model a hand-built state on the late-swap fixture (AAA to FFF, no Daily Faceoff pages offline):
    one forward on PP1 and line 1 with news an hour old, and the first team's backup goalie confirmed."""
    seen = {}

    def fake(pool, after_b, st, offline, cache, clock, runtime, m, messages):
        work = after_b["work"]
        aaa = [r for r in work.rows if r.team == "AAA"]
        star = next(r for r in aaa if not r.is_goalie)
        top, backup = sorted((r for r in aaa if r.is_goalie), key=lambda r: (-r.salary, r.role_id))[:2]
        rs = role_state(work, lines=[(star.role_id, 1)], pp=[(star.role_id, 1)], news=[(star.role_id, 1.0)],
                        confirmed=[("AAA", backup.role_id, 2.0)], usable=tuple(work.teams))
        seen.update(star=star.role_id, top=top.role_id, backup=backup.role_id)
        return SimpleNamespace(table=after_b["proj"], roles=rs, play_prob={}, news_state="NONE", notes=[], warnings=[])

    monkeypatch.setattr(run_mod, "_role_model", fake)
    return seen


def _verified_aaa_to_fff(monkeypatch):
    from nhl_dfs.sim import market

    codes = {c: c for c in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")}
    monkeypatch.setattr(market, "_code_maps", lambda path=None: {"nhl_partner_odds": codes, "covers": {}})


def _fixture_odds(clock_time):
    """A snapshot for the late-swap fixture's three games; AAA@BBB is the high-total game with AAA the favourite."""
    mk = lambda h, a, hm, am, t: GameOdds("g", h, a, clock_time, hm, am, None, None, None, t, -110, -110, None, None)
    return OddsSnapshot(clock_time - timedelta(hours=1), "source", "test", "nhl_partner_odds",
                        [mk("BBB", "AAA", 190, -230, 7.5), mk("DDD", "CCC", -110, -110, 5.5), mk("FFF", "EEE", -110, -110, 5.5)])


def _full_run(tmp):
    raw = tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    cache = HttpCache(raw, config=load_sources_config(), offline=True)
    return run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=False, scenario=True,
                     scenario_n=SMALL, out_root=tmp / "runs", outputs_root=tmp / "outputs", cache=cache,
                     clock=lambda: T0)


def _own(r, cid="297000001"):
    return json.loads((r.run.path / "scenario" / "fields.json").read_text(encoding="utf-8"))["own_by"][cid]


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """The same offline late-swap Classic run three ways: the inputs on, the off-switch set, and with the provisional pass
    forced to fail (the scenario pass then rebuilds the fields itself). The clock reads 12:00Z, before every game."""
    mp = pytest.MonkeyPatch()
    seen = _stub_roles(mp)
    _verified_aaa_to_fff(mp)
    mp.setattr(run_mod, "_run_odds", lambda cache, offline, clock, runtime, messages: (_fixture_odds(clock()), ["odds: test snapshot"]))
    calls: list[tuple[str, bool]] = []
    real_table = ownership.feature_table
    label = ["on"]

    def counting(*a, **k):
        calls.append((label[0], k.get("odds") is not None))
        return real_table(*a, **k)

    mp.setattr(ownership, "feature_table", counting)
    try:
        on = _full_run(tmp_path_factory.mktemp("c17_on"))
        real = ownership.load_ownership_config
        mp.setattr(ownership, "load_ownership_config", lambda *a, **k: {**real(*a, **k), "field_inputs": {"enabled": False}})
        label[0] = "off"
        off = _full_run(tmp_path_factory.mktemp("c17_off"))
        mp.setattr(ownership, "load_ownership_config", real)

        def broken(*a, **k):
            raise RuntimeError("forced: the provisional pass did not finish")

        mp.setattr(run_mod, "_provisional_pass", broken)
        label[0] = "fallback"
        fb = _full_run(tmp_path_factory.mktemp("c17_fallback"))
    finally:
        mp.undo()
    return SimpleNamespace(on=on, off=off, fallback=fb, seen=seen, calls=calls)


def test_a_run_hands_the_field_its_odds_roles_news_and_confirmed_goalie(runs):
    r = runs.on
    assert r.ok, r.manifest["failed"]
    fi = r.manifest["provisional"]["field_inputs"]
    assert (fi["odds"]["games"], fi["odds"]["of"]) == (3, 3) and fi["roles"]["pp1"] == 1 and fi["roles"]["line1"] == 1
    assert fi["news"]["recent"] >= 1 and fi["goalies"]["confirmed"] == 1
    assert fi["line"].startswith("FIELD_INPUTS MARKET=3/3 ROLES=6/6 ")
    assert fi["line"] in r.messages  # the printed note
    assert fi["line"] in r.notes_path.read_text(encoding="utf-8")  # RUN_NOTES
    assert r.manifest["scenario"]["version"]  # v3 ran on the same snapshot


def test_the_run_forecast_moves_with_its_inputs_and_the_switch_restores_the_old_one(runs):
    on, off = _own(runs.on), _own(runs.off)
    s = runs.seen
    assert on[s["star"]] > off[s["star"]]  # PP1, line 1 and fresh news
    assert on[s["backup"]] > off[s["backup"]] and on[s["top"]] < off[s["top"]]  # the confirmed goalie
    fi = runs.off.manifest["provisional"]["field_inputs"]
    assert fi["enabled"] is False and fi["line"].startswith("FIELD_INPUTS=OFF")


def test_the_grown_field_uses_the_same_feature_table_as_the_first_draws(runs):
    # one feature table per run: the scenario pass grows the field on fb.feats instead of building its own with no odds
    # (which would have drawn the grown part from a field that cannot see the game totals)
    assert [c for c in runs.calls if c[0] == "on"] == [("on", True)]
    assert [c for c in runs.calls if c[0] == "fallback"] == [("fallback", True)]
    own_on, own_off = _own(runs.on), _own(runs.off)
    work = run_mod.load_run_pool(runs.on.run)[2]
    team = lambda own, *ts: sum(own[r.role_id] for r in work.rows if r.team in ts and not r.is_goalie)
    assert team(own_on, "AAA", "BBB") > team(own_off, "AAA", "BBB")  # the high-total game (7.5 against 5.5) gains ownership


def test_the_scenario_fallback_rebuilds_the_field_with_the_same_inputs(runs):
    r = runs.fallback
    assert r.ok, r.manifest["failed"]
    assert any("scenario pass rebuilt the fields because the provisional pass did not finish" in m for m in r.messages)
    fi = r.manifest["scenario"]["field_inputs"]
    assert (fi["odds"]["games"], fi["roles"]["pp1"], fi["goalies"]["confirmed"]) == (3, 1, 1)
    assert fi["line"] in r.messages
    s = runs.seen
    assert _own(r)[s["backup"]] > _own(runs.off)[s["backup"]]


def test_the_scenario_fallback_prices_a_contest_on_its_cached_template_b93(tmp_path, monkeypatch):
    """B93: with the provisional pass failed, the fallback looked up no C16 table, so a contest with a cached template was
    priced on the PRIOR curve. It now uses the same store as the provisional pass."""
    from test_template_payouts import lobby_row, write_lobby, write_table
    from conftest import HTTP

    raw = tmp_path / "raw"
    real = json.loads((HTTP / "dk_contest_195958173.json").read_text(encoding="utf-8-sig"))
    real["contestDetail"]["name"] = "NHL Synthetic Classic"
    real["contestDetail"]["contestKey"] = "297000009"
    write_table(raw, real)
    write_lobby(raw, [lobby_row(297000001, "NHL Synthetic Classic", 1, 1189, 1000.0, guaranteed=True, tmpl=5),
                      lobby_row(297000009, "NHL Synthetic Classic", 1, 1189, 1000.0, tmpl=5)])

    def broken(*a, **k):
        raise RuntimeError("forced: the provisional pass did not finish")

    monkeypatch.setattr(run_mod, "_provisional_pass", broken)
    r = _full_run(tmp_path)
    assert r.ok, r.manifest["failed"]
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]
    assert scen["PAYOUT_SOURCE"] == "TEMPLATE" and scen["field_size"] == 1189
    assert scen["payout_template"]["template_contest_id"] == 297000009 and scen["paid_positions"] == 285
    assert r.statuses["PAYOUT_SOURCE"] == "TEMPLATE"


# -- B51: the team-rate source that replaces the league rate -------------------------------------------------------------------

def _games_frame():
    """Skater goal rows for two teams over six games (one playoff game, which never counts): AAA scores 4, 2, 3, 5, 1 in its
    regular-season games in date order, BBB 0 every game. NHL code AAA is DK code ZZZ in the map."""
    import pandas as pd

    rows = []
    for i, (gaa, gbb, regime) in enumerate([(4, 0, "regular"), (2, 0, "regular"), (3, 0, "regular"), (5, 0, "regular"),
                                             (1, 0, "regular"), (9, 0, "playoffs")]):
        day = datetime(2026, 4, 1 + i).date()
        rows += [{"team": "AAA", "game_id": 100 + i, "game_date": day, "goals": gaa - 1, "regime": regime},
                 {"team": "AAA", "game_id": 100 + i, "game_date": day, "goals": 1, "regime": regime},  # two skaters, one team-game
                 {"team": "BBB", "game_id": 100 + i, "game_date": day, "goals": gbb, "regime": regime}]
    return SimpleNamespace(skaters=pd.DataFrame(rows))


def test_team_goal_rate_is_goals_per_team_game_over_the_last_regular_season_games():
    from nhl_dfs.models import params as params_mod
    from nhl_dfs.models.rates import load_model_config

    cfg = load_model_config()
    feats = _games_frame()
    full = params_mod.team_goal_rates(feats, cfg, {"ZZZ", "BBB", "NOPE"}, {"ZZZ": "AAA"})
    assert full["ZZZ"] == (pytest.approx((4 + 2 + 3 + 5 + 1) / 5), 5)  # the playoff game is not a team-game here
    assert full["BBB"] == (0.0, 5) and "NOPE" not in full  # a team with no games is absent, never zero
    last3 = params_mod.team_goal_rates(feats, {"team": {"team_rate": {"games": 3}}}, {"ZZZ"}, {"ZZZ": "AAA"})
    assert last3["ZZZ"] == (pytest.approx((3 + 5 + 1) / 3), 3)  # the last three by date
    assert params_mod.team_goal_rates(SimpleNamespace(skaters=feats.skaters.iloc[0:0]), cfg, {"ZZZ"}) == {}


def test_a_thin_pool_team_takes_its_own_goal_rate_instead_of_the_league_rate():
    from sim_helpers import MODEL_CFG, synthetic_params
    from nhl_dfs.sim import market

    league = float(market.load_sim_config()["resolve"]["goals_league"])
    thin = synthetic_params(("AAA", "BBB"), n_f=6, n_d=4)  # fewer than 10 F and 5 D expected to dress: the old fallback
    old = market.model_strength(thin, "AAA", "BBB", model_cfg=MODEL_CFG)
    assert "league goal rate used" in old.notes[0] and "AAA" in old.notes[0]
    thin.team_goals = {"AAA": (league * 1.2, 82), "BBB": (league * 0.8, 10)}
    got = market.model_strength(thin, "AAA", "BBB", model_cfg=MODEL_CFG)
    assert got.lam_home == pytest.approx(old.lam_home * 1.2)  # AAA's own rate, 20% above the league rate
    assert any("AAA: listed skaters cover" in n and "its own goal rate" in n and "last 82 games" in n for n in got.notes)
    assert got.lam_away == pytest.approx(old.lam_away)  # BBB has only 10 games (below min_games 20): league rate, said so
    assert any("BBB" in n and "league goal rate used" in n for n in got.notes)
    full = synthetic_params(("AAA", "BBB"), n_f=13, n_d=7)
    full.team_goals = {"AAA": (6.0, 82)}
    assert not market.model_strength(full, "AAA", "BBB", model_cfg=MODEL_CFG).notes  # a complete roster never needs it



# -- B66: the market clip depends on how much of the model side rests on player history ---------------------------------------------

def _edm_van():
    """The 2026-10-01 EDM @ VAN game from the run's cached partner feed (VAN -218/EDM +180... as priced, total 6.5). B66: its away
    market intensity 3.64 sat 36% above the model's 2.67, so the fixed 35% clip binds and the fit stops reproducing the market."""
    from conftest import fixture_bytes
    from nhl_dfs.data.sources import nhl

    snap = nhl.parse_partner_odds(json.loads(fixture_bytes("nhl_partner_odds_2026-10-01_slate.json")))
    return snap, next(g for g in snap.games if {g.home_abbrev, g.away_abbrev} == {"VAN", "EDM"})


def test_the_clip_is_wide_when_the_model_side_is_a_prior_and_035_when_it_is_history():
    from nhl_dfs.sim import market

    cfg = market.load_sim_config()
    snap, g = _edm_van()
    fit = lambda hh, ha: market.fit_game(g, market.TeamStrength(2.55, 2.67, (), hh, ha), snap.as_of_utc, None, cfg=cfg)
    hist, prior, mixed = fit(1.0, 1.0), fit(0.0, 0.0), fit(0.5, 0.5)
    assert hist.lambda_away == pytest.approx(2.67 * 1.35) and any("CAPPED: away market 3.64 clipped to 3.60" in n for n in hist.notes)
    assert "(model 2.67 +/- 35%, model side 100% history-backed)" in next(n for n in hist.notes if n.startswith("CAPPED"))
    assert abs(hist.e_total - hist.target_total) > 0.03  # the 10-01 report: total 6.84 against the market's 6.88
    assert abs(prior.e_total - prior.target_total) < 0.1 and abs(prior.p_home_win - prior.target_p_home) < 0.01  # B66 acceptance
    assert not any("CAPPED" in n for n in prior.notes) and prior.lambda_away > hist.lambda_away
    assert not any("CAPPED" in n for n in mixed.notes)  # halfway: the limit is 0.35 + 0.65 x 0.5 = 68%, above the 36% gap
    # a caller that does not say (every older TeamStrength) keeps the old clip
    assert market.fit_game(g, market.TeamStrength(2.55, 2.67), snap.as_of_utc, None, cfg=cfg).lambda_away == pytest.approx(hist.lambda_away)
    # sides are independent: only the history-backed away side is held
    one = fit(0.0, 1.0)
    assert one.lambda_away == pytest.approx(hist.lambda_away)


def test_the_clip_still_binds_on_a_history_backed_game_with_a_stale_feed():
    from nhl_dfs.sim import market

    cfg = market.load_sim_config()
    snap, g = _edm_van()
    confirmed_after = snap.as_of_utc + timedelta(hours=1)  # a goalie confirmation that postdates the market: STALE
    gr = market.fit_game(g, market.TeamStrength(1.6, 1.6, (), 1.0, 1.0), snap.as_of_utc, confirmed_after, cfg=cfg)
    assert gr.stale and gr.source == "MARKET" and any("STALE" in n for n in gr.notes) and any("CAPPED" in n for n in gr.notes)
    assert gr.lambda_away == pytest.approx(1.6 * 1.35)  # half-blended toward the model and still above the 35% limit
    wide = market.fit_game(g, market.TeamStrength(1.6, 1.6, (), 0.0, 0.0), snap.as_of_utc, confirmed_after, cfg=cfg)
    assert wide.stale and wide.lambda_away > gr.lambda_away  # the same stale feed, a prior model: not clipped


def test_a_sides_history_share_comes_from_its_persons_statuses_and_its_rate_source():
    from dataclasses import replace as dc_replace

    from nhl_dfs.contracts.statuses import ModelStatus
    from sim_helpers import MODEL_CFG, synthetic_params
    from nhl_dfs.sim import market

    full = synthetic_params(("AAA", "BBB"), n_f=13, n_d=7)  # every person PRIOR
    assert market.model_strength(full, "AAA", "BBB", model_cfg=MODEL_CFG).history_home == 0.0
    for p in full.persons.values():
        if p.team == "AAA":
            p.source = ModelStatus.HISTORY
        elif p.group != "G":
            p.source, p.history_exposure, p.prior_exposure = ModelStatus.MIXED, 300.0, 100.0  # BBB: 75% history by exposure
    s = market.model_strength(full, "AAA", "BBB", model_cfg=MODEL_CFG)
    assert s.history_home == pytest.approx(1.0) and s.history_away == pytest.approx(0.75)
    thin = synthetic_params(("AAA", "BBB"), n_f=6, n_d=4)
    for p in thin.persons.values():
        p.source = ModelStatus.HISTORY
    assert market.model_strength(thin, "AAA", "BBB", model_cfg=MODEL_CFG).history_home == 0.0  # the league rate rests on no history
    thin.team_goals = {"AAA": (3.3, 82)}
    s = market.model_strength(thin, "AAA", "BBB", model_cfg=MODEL_CFG)
    assert s.history_home == 1.0 and s.history_away == 0.0  # its own measured rate counts as history, BBB still on the league's
    assert dc_replace(s, history_home=0.2).history_home == 0.2
