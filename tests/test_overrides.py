"""C7: the override schema, its validator, and apply."""

import copy
from datetime import timedelta

import pytest

from nhl_dfs.contracts.statuses import GoalieState, Participation
from nhl_dfs.models import overrides as ov
from nhl_dfs.models import roles
from test_roles import CFG, MODEL_CFG, NOW, POOL, RID, merged, params_for, role_of

pytestmark = pytest.mark.c7


def make(name, field, old, new, **kw):
    base = dict(role_id=RID[name], nhl_id=None, game_id="VAN@EDM", field=field, old=old, new=new,
                effective_utc=NOW - timedelta(minutes=5), source_url="https://example.com/report", claim="coach said so",
                confidence=0.8, expiry_utc=NOW + timedelta(hours=6))
    base.update(kw)
    return ov.Override(**base)


def roles_with_room():
    """VAN's line 4 has one open spot (a listed forward removed from the lineup)."""
    rs = merged()
    line4 = [r for r in rs.persons.values() if r.team == "VAN" and r.group == "F" and r.line == 4]
    line4[0].line, line4[0].df_listed = None, False
    return rs


def test_a_well_formed_override_validates_and_each_bad_field_is_named():
    rs = roles_with_room()
    ok = make("Bench Forward One", "ev_line", None, 4)
    assert ov.validate(ok, rs) == []
    bad = {
        "unknown role": make("Bench Forward One", "ev_line", None, 4, role_id="nope"),
        "field": make("Bench Forward One", "salary", None, 4),
        "old": make("Bench Forward One", "ev_line", 2, 4),
        "value": make("Bench Forward One", "ev_line", None, 9),
        "source": make("Bench Forward One", "ev_line", None, 4, source_url="ftp://x"),
        "claim": make("Bench Forward One", "ev_line", None, 4, claim="  "),
        "confidence": make("Bench Forward One", "ev_line", None, 4, confidence=1.5),
        "future": make("Bench Forward One", "ev_line", None, 4, effective_utc=NOW + timedelta(minutes=1)),
        "expired": make("Bench Forward One", "ev_line", None, 4, effective_utc=NOW - timedelta(hours=8), expiry_utc=NOW - timedelta(hours=1)),
        "game": make("Bench Forward One", "ev_line", None, 4, game_id="CHI@VGK"),
    }
    for label, o in bad.items():
        assert ov.validate(o, rs), label
    assert any("old value" in e for e in ov.validate(bad["old"], rs))
    assert any("expired" in e for e in ov.validate(bad["expired"], rs))


def test_an_override_that_overfills_a_unit_is_rejected():
    rs = merged()  # every forward line has 3 and PP2 has 5
    e = ov.validate(make("Bench Forward One", "ev_line", None, 2), rs)
    assert any("overfills" in x for x in e)
    e = ov.validate(make("Bench Forward One", "pp_unit", 0, 2), rs)
    assert any("PP2 already has 5 of 5" in x for x in e)
    # a defense pair too
    e = ov.validate(make("Bench Defense One", "ev_line", None, 1), rs)
    assert any("overfills" in x for x in e)
    # moving within a unit the person is already in is not an overfill
    pp1 = next(r for r in rs.persons.values() if r.team == "VAN" and r.pp_unit == 1)
    name = next(p.name for p in POOL.rows if p.person_key == pp1.person_key)
    assert ov.validate(make(name, "pp_unit", 1, 1), rs) == []


def test_dk_out_cannot_be_lifted_and_a_goalie_cannot_have_a_second_starter():
    rs = merged(csv={RID["Brock Boeser"]: (Participation.OUT, "OUT")})
    assert any("cannot lift" in e for e in ov.validate(make("Brock Boeser", "participation", "OUT", "PLAYING"), rs))
    rs = merged()  # EDM's Jarry is confirmed
    assert any("one starter per team" in e for e in ov.validate(make("Devon Levi", "goalie_start", False, True, game_id="VAN@EDM"), rs))
    assert ov.validate(make("Leevi Merilainen", "goalie_start", False, True), rs) == []


def team_group_minutes(table):
    return roles.team_minutes(table)


def test_apply_conserves_team_ev_and_pp_minutes():
    table = params_for(POOL)
    rs = roles_with_room()
    before = team_group_minutes(table)
    out, accepted, rejected = ov.apply_with_report(
        table, rs, [make("Bench Forward One", "ev_line", None, 4), make("Jake DeBrusk", "pp_unit", 1, 0)], NOW, MODEL_CFG)
    assert len(accepted) == 2 and not rejected
    after = team_group_minutes(out)
    for k in before:
        assert after[k]["ev"] == pytest.approx(before[k]["ev"], abs=1e-6)
    van_pp = sum(v["pp"] for (t, g), v in before.items() if t == "VAN")
    assert sum(v["pp"] for (t, g), v in after.items() if t == "VAN") == pytest.approx(van_pp, abs=1e-6)
    bench = out.persons[next(k for k in out.persons if k.startswith("bench forward one"))].opportunity
    assert bench.unit_ev == "VAN-F4" and bench.source == "role"
    deb = out.persons[next(k for k in out.persons if k.startswith("jake debrusk"))].opportunity
    assert deb.toi_pp_s == 0.0 and deb.unit_pp == ""
    assert team_group_minutes(table) == before  # the input table was not mutated
    assert out is not table and out.notes[-1].startswith("overrides: 2 applied")


def test_apply_skips_rejected_overrides_and_says_so():
    table = params_for(POOL)
    rs = merged()
    good = make("Leevi Merilainen", "goalie_start", False, True)
    bad = make("Bench Forward One", "ev_line", None, 2)
    out, accepted, rejected = ov.apply_with_report(table, rs, [good, bad], NOW, MODEL_CFG)
    assert accepted == [good] and rejected[0][0] is bad and any("overfills" in e for e in rejected[0][1])
    mer = next(k for k in out.persons if k.startswith("leevi merilainen"))
    lank = next(k for k in out.persons if k.startswith("kevin lankinen"))
    assert out.persons[mer].goalie.p_start == 1.0 and out.persons[lank].goalie.p_start == 0.0
    assert out.persons[lank].mean_tenths == 0 and out.persons[mer].mean_tenths > table.persons[mer].mean_tenths
    assert ov.apply(table, rs, [bad], NOW).persons.keys() == table.persons.keys()


def test_out_override_hands_minutes_to_teammates_and_updates_the_state():
    table = params_for(POOL)
    rs = merged()
    o = make("Bench Forward One", "participation", "PLAYING", "OUT")
    out = ov.apply(table, rs, [o], NOW)
    k = next(k for k in out.persons if k.startswith("bench forward one"))
    assert out.persons[k].opportunity.p_dress == 0.0 and out.persons[k].mean_tenths == 0
    b, a = roles.team_minutes(table)[("VAN", "F")]["ev"], roles.team_minutes(out)[("VAN", "F")]["ev"]
    conserved = a == pytest.approx(b, rel=1e-6)
    assert conserved or (a < b and any("could not be handed over" in n for n in out.notes))  # teammates at certainty: said so
    new_roles = ov.apply_to_roles(rs, [o], NOW)
    assert new_roles.persons[k].participation is Participation.OUT and new_roles.persons[k].p_play == 0.0
    assert rs.persons[k].participation is Participation.PLAYING  # the input state is unchanged
    g = make("Leevi Merilainen", "goalie_start", False, True)
    st = ov.apply_to_roles(rs, [g], NOW)
    assert st.goalies["VAN"].state is GoalieState.CONFIRMED and st.confirmed_at()["VAN@EDM"] == g.effective_utc
