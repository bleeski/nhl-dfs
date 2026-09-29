"""C7: the deterministic role state and its application to the parameter table."""

import dataclasses
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import HTTP
from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import person_key
from nhl_dfs.contracts.statuses import GoalieState, ModelStatus, ObsStatus, Participation
from nhl_dfs.data.sources import dailyfaceoff as df
from nhl_dfs.data.sources import dk_public
from nhl_dfs.data.sources.dk_public import Reconciliation, RowState
from nhl_dfs.intake.salary import GameInfo, PersonRows, SalaryPool
from nhl_dfs.models import goalies as goalie_mod
from nhl_dfs.models import opportunity as opp_mod
from nhl_dfs.models import rates as rates_mod
from nhl_dfs.models import roles
from nhl_dfs.models.params import ParamTable, PersonParams

pytestmark = pytest.mark.c7

CFG = roles.load_roles_config()
MODEL_CFG = rates_mod.load_model_config()
TEAM_HTML = (HTTP / "dailyfaceoff_team_vancouver-canucks.html").read_text(encoding="utf-8")
GOALIE_HTML = (HTTP / "dailyfaceoff_goalies.html").read_text(encoding="utf-8")
START = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)  # the goalie page's dateGmt for VAN@EDM
NOW = datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)  # 20.3 h after the VAN page's updatedAt
VAN = df.parse_team_page(TEAM_HTML, "vancouver-canucks")
REPORTS = df.parse_goalie_page(GOALIE_HTML)


def _group(pos):
    return roles.df_group(pos)


def make_pool(extra_van=(("Bench Forward One", "c"), ("Bench Forward Two", "lw"), ("Bench Defense One", "d"))):
    """A Classic-shaped pool of the VAN fixture roster (plus a few bench players) and EDM's goalies."""
    rows = []

    def add(name, team, pos):
        g = _group(pos)
        p = "G" if g == "G" else ("D" if g == "D" else "C")
        rows.append(PoolRow(role_id=f"{team}-{len(rows):03d}", person_key=person_key(name, team, p), name=name, team=team, position=p,
                            roster_positions=frozenset({"G"} if p == "G" else {p, "UTIL"}), salary=4000, game_info="",
                            appg_raw=5.0, appg_flag="VALUE"))

    for pl in VAN.players.values():
        add(pl.name, "VAN", pl.position)
    for name, pos in extra_van:
        add(name, "VAN", pos)
    for name in ("Tristan Jarry", "Devon Levi", "Frederik Andersen"):
        add(name, "EDM", "g")
    persons = {r.person_key: PersonRows(classic=r) for r in rows}
    game = GameInfo("EDM", "VAN", "", START)
    return SalaryPool(Mode.CLASSIC, rows, {r.role_id: r for r in rows}, persons, frozenset({"VAN", "EDM"}), {"VAN@EDM": game}, [],
                      "test", b"")


POOL = make_pool()
RID = {p.name: p.role_id for p in POOL.rows}


def rotation():
    van = [r.person_key for r in POOL.rows if r.team == "VAN" and r.is_goalie]
    edm = [r.person_key for r in POOL.rows if r.team == "EDM" and r.is_goalie]
    return {"VAN": {van[0]: 0.7, van[1]: 0.3}, "EDM": {edm[0]: 0.3, edm[1]: 0.6, edm[2]: 0.1}}


def merged(*, csv=None, lines=None, reports=None, now=NOW, dk=None, pool=POOL):
    lines = {"VAN": VAN} if lines is None else lines
    reports = REPORTS if reports is None else reports
    return roles.merge(dk, lines, reports, rotation(), now, CFG, pool=pool, csv_status=csv, goalie_path="next_data")


def role_of(rs, name):
    return next(r for k, r in rs.persons.items() if k.startswith(name.lower() + "|"))


def test_dk_out_overrides_a_daily_faceoff_line_listing():
    name = "Elias Pettersson"
    rs = merged(csv={RID[name]: (Participation.OUT, "OUT")})
    r = role_of(rs, name)
    assert r.participation is Participation.OUT and r.p_play == 0.0 and r.line is None and not r.df_listed and r.pp_unit is None
    assert any("DK OUT overrides a Daily Faceoff line listing" in w for w in rs.warnings)
    assert role_of(merged(), name).df_listed


def test_dk_unknown_changes_nothing_and_is_reported():
    name = "Brock Boeser"
    base = role_of(merged(), name)
    rs = merged(csv={RID[name]: (Participation.UNKNOWN, "WEIRD")})
    r = role_of(rs, name)
    assert r.participation is Participation.UNKNOWN and r.p_play == 1.0 and not r.conflict
    assert (r.line, r.pp_unit, r.df_listed) == (base.line, base.pp_unit, base.df_listed)
    assert any("UNKNOWN DK status 'WEIRD'" in w and "not as OUT" in w for w in rs.warnings)


def test_questionable_carries_the_haircut_and_df_dtd_or_gtd_is_a_signal_only():
    rs = merged(csv={RID["Brock Boeser"]: (Participation.QUESTIONABLE, "DTD")})
    assert role_of(rs, "Brock Boeser").p_play == CFG["p_play_questionable"]
    deb = role_of(merged(), "Jake DeBrusk")  # a game-time decision tag, DK silent
    assert deb.participation is Participation.QUESTIONABLE and deb.p_play == CFG["p_play_questionable"] and not deb.conflict
    assert deb.df_status == "gtd" and deb.df_listed  # still on the projected lineup


def test_df_out_tag_against_dk_playing_is_a_conflict_with_a_mixture_and_a_warning():
    rs = merged()
    chy = role_of(rs, "Filip Chytil")
    assert chy.conflict and chy.p_play == CFG["p_play_conflict"] and chy.df_status == "ir"
    assert any("CONFLICT Filip Chytil" in w for w in rs.warnings)
    # DK OUT plus a DF out tag is not a conflict: DK already said OUT
    rs2 = merged(csv={RID["Filip Chytil"]: (Participation.OUT, "IR")})
    assert not role_of(rs2, "Filip Chytil").conflict and role_of(rs2, "Filip Chytil").p_play == 0.0


def test_absence_from_a_daily_faceoff_lineup_is_never_out():
    rs = merged()
    for nm in ("Bench Forward One", "Bench Forward Two", "Bench Defense One"):
        r = role_of(rs, nm)
        assert r.participation is Participation.PLAYING and r.p_play == 1.0 and not r.df_listed


def test_isswappable_never_affects_participation():
    d = dk_public.parse_draftables(json.loads((HTTP / "dk_draftables_153983.json").read_text(encoding="utf-8")), 153983)
    rows = [PoolRow(r.draftable_id, person_key(r.name, r.team, "C"), r.name, r.team, "C", frozenset({"C", "UTIL"}), r.salary, "",
                    5.0, "VALUE") for r in d.rows if r.roster_slot_id != 612]
    pool = SalaryPool(Mode.CLASSIC, rows, {r.role_id: r for r in rows}, {r.person_key: PersonRows(classic=r) for r in rows},
                      frozenset(r.team for r in rows), {}, [], "t", b"")
    rec = dk_public.reconcile(pool, d)
    rs = roles.merge(rec, {}, [], {}, NOW, CFG, pool=pool)
    by_id = d.by_id
    for row in rows:
        want = by_id[row.role_id].participation
        assert rs.persons[row.person_key].participation is want
    assert "is_swappable" not in open(roles.__file__, encoding="utf-8").read()  # editability is never read here


def test_goalie_confirmation_needs_the_exact_strength_and_a_matching_game():
    rs = merged()
    edm = rs.goalies["EDM"]
    jarry = next(k for k in edm.p_start if k.startswith("tristan jarry"))
    assert edm.state is GoalieState.CONFIRMED and edm.confirmed == jarry and edm.p_start[jarry] == 1.0
    assert sum(edm.p_start.values()) == pytest.approx(1.0)
    assert edm.confirmed_at == datetime(2026, 9, 28, 17, 42, 37, 590000, tzinfo=timezone.utc)
    assert rs.confirmed_at() == {"VAN@EDM": edm.confirmed_at}  # the input of the market fit's STALE rule
    van = rs.goalies["VAN"]  # named but unconfirmed: EXPECTED, moved toward Lankinen, not to certainty
    lank = next(k for k in van.p_start if k.startswith("kevin lankinen"))
    assert van.state is GoalieState.EXPECTED and 0.7 < van.p_start[lank] < 1.0
    # the same report against a game that starts a day later is not this game's confirmation
    moved = dataclasses.replace(POOL, games={"VAN@EDM": GameInfo("EDM", "VAN", "", START + timedelta(days=1))})
    rs2 = merged(pool=moved)
    assert rs2.goalies["EDM"].state is GoalieState.EXPECTED and not rs2.confirmed_at()
    assert any("do not match this slate game" in r for r in rs2.reports)


def test_a_goalie_outside_the_pool_is_reported_and_nobody_is_reassigned_his_probability():
    reports = [dataclasses.replace(r, goalie_name="Somebody Else") if r.team == "EDM" else r for r in REPORTS]
    rs = merged(reports=reports)
    edm = rs.goalies["EDM"]
    assert edm.state is GoalieState.EXPECTED and edm.confirmed is None
    assert {k.split("|")[0]: round(v, 2) for k, v in edm.p_start.items()} == {"tristan jarry": 0.3, "devon levi": 0.6, "frederik andersen": 0.1}
    assert any("Somebody Else" in r and "not a goalie the DK pool lists" in r for r in rs.reports)


def test_conflicting_goalie_reports_make_a_mixture_with_a_warning_and_a_confirmation_is_not_a_conflict():
    team_first = [g for g in VAN.goalies if g.name != "Kevin Lankinen"][0]  # depth order names the other goalie
    tl = dataclasses.replace(VAN, goalies=[team_first] + [g for g in VAN.goalies if g is not team_first])
    rs = merged(lines={"VAN": tl})
    van = rs.goalies["VAN"]
    assert van.state is GoalieState.CONFLICTED and sum(van.p_start.values()) == pytest.approx(1.0)
    a, b, _ = CFG["conflict_split"]
    lank = next(k for k in van.p_start if k.startswith("kevin lankinen"))
    other = next(k for k in van.p_start if k.startswith(team_first.name.lower()))
    assert van.p_start[lank] > 0.35 and van.p_start[other] > 0.35
    assert any("goalie sources disagree" in w for w in rs.warnings)
    # an explicit confirmation beats depth order: no conflict
    edm_page = dataclasses.replace(VAN, team="EDM", goalies=[df.DFPlayer(1, "Devon Levi", "g", None, False)])
    rs2 = merged(lines={"VAN": VAN, "EDM": edm_page})
    assert rs2.goalies["EDM"].state is GoalieState.CONFIRMED


def test_dk_out_on_a_confirmed_goalie_is_out_with_a_warning():
    rs = merged(csv={RID["Tristan Jarry"]: (Participation.OUT, "OUT")})
    edm = rs.goalies["EDM"]
    assert edm.state is GoalieState.CONFLICTED and edm.confirmed is None
    jarry = next(k for k in edm.p_start if k.startswith("tristan jarry"))
    assert edm.p_start[jarry] == 0.0 and sum(edm.p_start.values()) == pytest.approx(1.0)
    assert any("DK OUT overrides the Daily Faceoff CONFIRMED" in w for w in rs.warnings)


def test_age_policy_uses_the_argument_time_not_the_wall_clock():
    old = merged(now=VAN.updated_utc + timedelta(hours=25))
    assert not old.team_pages["VAN"]["usable"] and not role_of(old, "Jake DeBrusk").df_listed
    assert any("lines and tags not used" in w for w in old.warnings)
    assert role_of(old, "Filip Chytil").participation is Participation.PLAYING  # a stale tag is not used either
    fresh = merged(now=VAN.updated_utc + timedelta(hours=13))
    assert fresh.team_pages["VAN"]["usable"] and not fresh.team_pages["VAN"]["low_confidence"]
    near = merged(now=START - timedelta(minutes=30))  # 31.7 h after the page: unusable; use a page fresher than the limit
    near_lines = dataclasses.replace(VAN, updated_utc=START - timedelta(hours=20))
    low = merged(now=START - timedelta(minutes=30), lines={"VAN": near_lines})
    assert low.team_pages["VAN"]["low_confidence"] and any("low confidence" in w for w in low.warnings)
    assert not near.team_pages["VAN"]["usable"]


def test_unverified_dk_codes_are_reported_and_no_df_data_is_used_for_them():
    pool = make_pool()
    rows = [dataclasses.replace(r, team="XYZ", person_key=r.person_key.replace("|VAN|", "|XYZ|")) if r.team == "VAN" else r for r in pool.rows]
    p2 = dataclasses.replace(pool, rows=rows, by_role_id={r.role_id: r for r in rows}, teams=frozenset({"XYZ", "EDM"}),
                             persons={r.person_key: PersonRows(classic=r) for r in rows}, games={})
    rs = roles.merge(None, {"VAN": VAN}, REPORTS, {}, NOW, CFG, pool=p2)
    assert any("not verified" in r and "XYZ" in r for r in rs.reports) and not any(r.df_listed for r in rs.persons.values())


# -- apply_state --------------------------------------------------------------------------------------------

def params_for(pool, *, history=("Elias Pettersson", "Brock Boeser", "Jake DeBrusk", "Marco Rossi", "Bench Forward One",
                                 "Bench Forward Two", "Bench Defense One")):
    persons = {}
    rot = rotation()
    for k, r in {r.person_key: r for r in pool.rows}.items():
        g = "G" if r.is_goalie else ("D" if r.position == "D" else "F")
        if g == "G":
            gp = goalie_mod.GoalieParams(k, r.team, rot.get(r.team, {}).get(k, 0.1), 0.0, MODEL_CFG["goalies"]["sv_pct"],
                                         MODEL_CFG["goalies"]["shots_against"], 1.0, MODEL_CFG["goalies"]["pull_per_ga"])
            persons[k] = PersonParams(k, r.name, r.team, "G", None, None, None, gp, ModelStatus.HISTORY, 90, 100)
            continue
        o = opp_mod.prior_opportunity(g, MODEL_CFG)
        hist = r.name in history or g == "D"
        if hist:
            o.source = "history"
        rt = rates_mod.prior_rates(0, g, MODEL_CFG)
        status = ModelStatus.HISTORY if hist else ModelStatus.PRIOR
        pp = PersonParams(k, r.name, r.team, g, None, o, rt, None, status, 40, 60)
        persons[k] = pp
    opp_mod.dress_budget({k: p.opportunity for k, p in persons.items() if p.group != "G"},
                         {k: p.team for k, p in persons.items() if p.group != "G"},
                         {k: p.group for k, p in persons.items() if p.group != "G"}, MODEL_CFG)
    table = ParamTable(persons, {}, date(2026, 9, 28))
    for p in table.persons.values():
        roles.set_play_prob(p, p.goalie.p_start if p.group == "G" else p.opportunity.p_dress, MODEL_CFG)
    return table


def test_apply_state_moves_goalies_and_dressing_and_keeps_the_team_budget():
    table = params_for(POOL)
    rs = merged()
    before = json.dumps({k: (p.mean_tenths, round(p.opportunity.p_dress if p.opportunity else p.goalie.p_start, 6))
                         for k, p in table.persons.items()})
    new = roles.apply_state(table, rs, CFG, MODEL_CFG)
    assert json.dumps({k: (p.mean_tenths, round(p.opportunity.p_dress if p.opportunity else p.goalie.p_start, 6))
                       for k, p in table.persons.items()}) == before  # the input table is untouched
    jarry = next(k for k in new.persons if k.startswith("tristan jarry"))
    levi = next(k for k in new.persons if k.startswith("devon levi"))
    assert new.persons[jarry].goalie.p_start == 1.0 and new.persons[levi].goalie.p_start == 0.0
    assert new.persons[jarry].mean_tenths > table.persons[jarry].mean_tenths and new.persons[levi].mean_tenths == 0
    van = [p for p in new.persons.values() if p.team == "VAN" and p.group in ("F", "D")]
    f_sum = sum(p.opportunity.p_dress for p in van if p.group == "F")
    d_sum = sum(p.opportunity.p_dress for p in van if p.group == "D")
    assert f_sum <= 12.0 + 1e-6 and d_sum <= 6.0 + 1e-6
    # DF-listed history forwards rise; unlisted history bench forwards fall
    listed = [p for k, p in new.persons.items() if rs.persons[k].df_listed and p.group == "F" and p.opportunity.source == "history"]
    assert listed and all(p.opportunity.p_dress >= table.persons[p.person_key].opportunity.p_dress - 1e-9 for p in listed)
    bench = new.persons[next(k for k in new.persons if k.startswith("bench forward one"))]
    assert bench.opportunity.p_dress < table.persons[bench.person_key].opportunity.p_dress
    # units for the simulator
    deb = new.persons[next(k for k in new.persons if k.startswith("jake debrusk"))].opportunity
    assert deb.unit_ev == "VAN-F1" and deb.unit_pp == "VAN-PP1" and deb.units_missing == 0


def test_a_listed_call_up_is_not_sunk_to_the_floor_by_the_budget():
    """Budget order: listed first. A DF-listed no-history skater keeps a high probability while unlisted history skaters give way."""
    table = params_for(POOL, history=("Elias Pettersson",))  # nearly everyone else has no history
    new = roles.apply_state(table, merged(), CFG, MODEL_CFG)
    listed_prior = [p for k, p in new.persons.items() if p.team == "VAN" and p.group == "F" and merged().persons[k].df_listed
                    and table.persons[k].opportunity.source == "prior"]
    assert listed_prior and all(p.opportunity.p_dress > 0.5 for p in listed_prior)


def test_call_up_role_ice_time_is_applied_once():
    table = params_for(POOL, history=())
    rs = merged()
    once = roles.apply_state(table, rs, CFG, MODEL_CFG)
    marked = [p for p in once.persons.values() if p.opportunity is not None and p.opportunity.source == "role"]
    assert marked
    k = marked[0].person_key
    line = rs.persons[k].line
    role = MODEL_CFG["roles"][marked[0].group]["ev_line_toi_s"]
    assert marked[0].opportunity.toi_ev_s == role[min(line, len(role)) - 1]
    twice = roles.apply_state(once, rs, CFG, MODEL_CFG)
    assert twice.persons[k].opportunity.toi_ev_s == marked[0].opportunity.toi_ev_s  # not applied again
    assert twice.persons[k].opportunity.source == "role"


def test_role_state_satisfies_the_opportunity_role_interface():
    rs = merged()
    assert rs.ev_line and rs.pp_unit and set(rs.ev_line) <= set(rs.persons)
    o = opp_mod.prior_opportunity("F", MODEL_CFG, next(iter(rs.pp_unit)), rs)
    assert o.source == "role"


def test_news_state_counts_only_usable_second_signals():
    from nhl_dfs.build import news
    from nhl_dfs.contracts.statuses import NewsState

    assert news.state(None, POOL) is NewsState.NONE
    nothing = merged(lines={}, reports=[], csv={RID["Brock Boeser"]: (Participation.QUESTIONABLE, "DTD")})
    assert news.state(nothing, POOL) is NewsState.NONE  # DK status alone is intake data
    partial = merged()  # VAN page and both goalie reports, but no EDM lines
    assert news.state(partial, POOL) is NewsState.PARTIAL
    edm = dataclasses.replace(VAN, team="EDM")
    full = merged(lines={"VAN": VAN, "EDM": edm})
    assert news.state(full, POOL) is NewsState.FULL
    stale = merged(lines={"VAN": VAN, "EDM": edm}, now=VAN.updated_utc + timedelta(hours=30))
    assert news.state(stale, POOL) is NewsState.PARTIAL  # goalie reports are still matched; the pages are too old
