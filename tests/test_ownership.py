import copy
from datetime import datetime, timezone

import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.data.sources.nhl import GameOdds, OddsSnapshot
from nhl_dfs.models import ownership
from nhl_dfs.models.contests import family_prior, load_contest_families, resolve
from nhl_dfs.models.projection import PriorProjection, lineup_mean_tenths
from pool_builder import make_pool, row, sd_person, varied_pool

pytestmark = pytest.mark.c3

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return ownership.load_ownership_config()


def odds(games):
    return OddsSnapshot(as_of_utc=T0, as_of_basis="fetch", book="test", source="test", games=games)


def game(home, away, home_ml, away_ml, total):
    return GameOdds("g", home, away, T0, home_ml, away_ml, None, None, None, total, None, None, None, None)


def test_projection_interface_prior_and_captain_once():
    cpt, flex = sd_person(1, "AAA", "C", 10000, appg=12.0)
    pool = make_pool(Mode.SHOWDOWN, [cpt, flex, *sd_person(2, "BBB", "C", 4000)])
    proj = PriorProjection(pool)
    assert proj.source().value == "PRIOR"
    assert proj.mean_tenths(cpt.role_id) == proj.mean_tenths(flex.role_id)  # uncaptained in the interface
    assert lineup_mean_tenths(pool, proj, [cpt.role_id]) == pytest.approx(1.5 * proj.mean_tenths(flex.role_id))


def test_utilities_keyed_by_family_and_default(cfg):
    pool = varied_pool(Mode.CLASSIC)
    proj = PriorProjection(pool)
    fc = load_contest_families()
    contests = [family_prior("1", "NHL $5 Double Up", fc), family_prior("2", "NHL $20K Puck Drop", fc)]
    u = ownership.utilities(pool, proj, contests, None, cfg=cfg)
    assert set(u) == {"cash", "large_gpp"}
    assert set(u["cash"]) == set(pool.by_role_id)
    assert set(ownership.utilities(pool, proj, [], None, cfg=cfg)) == {"large_gpp"}


def test_family_overrides_change_only_that_family(cfg):
    pool = varied_pool(Mode.CLASSIC)
    proj = PriorProjection(pool)
    c = copy.deepcopy(cfg)
    c["families"] = {"cash": {"salary_rank": 0.0, "appg": 0.0, "value_z": 0.0}}
    fc = load_contest_families()
    contests = [family_prior("1", "NHL $5 Double Up", fc), family_prior("2", "NHL $20K Puck Drop", fc)]
    u = ownership.utilities(pool, proj, contests, None, cfg=c)
    r = pool.rows[0]
    assert u["cash"][r.role_id] != u["large_gpp"][r.role_id]


def test_dtd_questionable_is_faded_by_the_field(cfg):
    a = row(1, "AAA", "C", 6000, appg=9.0)
    pool = make_pool(Mode.CLASSIC, [a, row(2, "AAA", "C", 6000, appg=9.0)])
    proj = PriorProjection(pool)
    u = ownership.utilities(pool, proj, [], None, cfg=cfg, statuses={"1": Participation.QUESTIONABLE})["large_gpp"]
    assert u["1"] == pytest.approx(u["2"] + cfg["weights"]["questionable"])
    assert u["1"] < u["2"]


def test_salary_rank_and_appg_raise_perceived_value(cfg):
    rows = [row(1, "AAA", "C", 8000, appg=10.0), row(2, "AAA", "C", 4000, appg=10.0), row(3, "AAA", "C", 6000, appg=10.0)]
    pool = make_pool(Mode.CLASSIC, rows)
    f = ownership.feature_table(pool, PriorProjection(pool), cfg=cfg)
    assert f["1"]["salary_rank"] == pytest.approx(0.5) and f["2"]["salary_rank"] == pytest.approx(-0.5)
    missing = make_pool(Mode.CLASSIC, [row(4, "AAA", "C", 5000, flag="MISSING"), row(5, "AAA", "C", 5000)])
    assert ownership.feature_table(missing, PriorProjection(missing), cfg=cfg)["4"]["appg"] == 0.0


def test_odds_feed_implied_total_and_goalie_win(cfg):
    rows = [row(1, "AAA", "C", 5000), row(2, "BBB", "C", 5000), row(3, "AAA", "G", 8000), row(4, "BBB", "G", 8000),
            row(5, "AAA", "G", 5000)]
    pool = make_pool(Mode.CLASSIC, rows)
    proj = PriorProjection(pool)
    snap = odds([game("AAA", "BBB", -200, 170, 6.0), game("XXX", "YYY", -110, -110, 5.5)])  # second game not in pool
    totals, wins = ownership.team_odds(pool, snap, cfg)
    assert set(totals) == {"AAA", "BBB"} and totals["AAA"] > totals["BBB"]
    assert totals["AAA"] + totals["BBB"] == pytest.approx(6.0)
    f = ownership.feature_table(pool, proj, snap, cfg=cfg)
    assert f["1"]["implied_total"] > 0 > f["2"]["implied_total"]
    assert f["3"]["goalie_start_win"] > f["4"]["goalie_start_win"]  # favourite's starter
    assert f["5"]["goalie_start_win"] < f["4"]["goalie_start_win"]  # backup (lower salary on AAA)
    no = ownership.feature_table(pool, proj, None, cfg=cfg)
    assert no["1"]["implied_total"] == 0.0 and no["3"]["goalie_start_win"] == pytest.approx(0.0)


def test_roles_and_news_hooks(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "C", 5000), row(2, "AAA", "C", 5000)])
    f = ownership.feature_table(pool, PriorProjection(pool), roles={"1": {"pp1": True, "line": 1}},
                                news_age_h={"1": 1.0, "2": 10.0}, cfg=cfg)
    assert (f["1"]["pp1"], f["1"]["line1"], f["1"]["news_recent"]) == (1.0, 1.0, 1.0)
    assert (f["2"]["pp1"], f["2"]["line1"], f["2"]["news_recent"]) == (0.0, 0.0, 0.0)


def test_showdown_cpt_row_carries_person_features(cfg):
    pool = varied_pool(Mode.SHOWDOWN)
    f = ownership.feature_table(pool, PriorProjection(pool), cfg=cfg)
    for p in pool.persons.values():
        assert f[p.cpt.role_id] == f[p.flex.role_id]


def test_contest_resolution_labels_priors():
    fc = load_contest_families()
    assert family_prior("1", "NHL Single Entry $1 Double Up", fc).family == "cash"
    p = family_prior("2", "NHL $1K Daily Dollar [Single Entry]", fc)
    assert (p.family, p.family_source) == ("large_gpp", "default")  # unmatched: declared default, reported
    assert family_prior("3", "NHL $3 Winner Take All", fc).family == "wta"
    assert family_prior("4", "NHL $10 Satellite", fc).family == "satellite"


def test_resolve_marks_exact_and_prior(tmp_path):
    import json

    from conftest import fixture_bytes
    from nhl_dfs.data.sources.dk_public import parse_contest_detail
    from nhl_dfs.intake.entries import EntryRow

    d = parse_contest_detail(json.loads(fixture_bytes("dk_contest_195958173.json")))
    rows = [EntryRow("1", "NHL $1K Daily Dollar [Single Entry] ", "195958173", "$1", (), 1, b""),
            EntryRow("2", "NHL Single Entry $1 Double Up", "195958188", "$1", (), 2, b"")]
    ctx = resolve(rows, {"195958173": d})
    a, b = ctx["195958173"], ctx["195958188"]
    assert a.payout_source.value == "EXACT" and a.field_size == d.maximum_entries and a.family_source == "contest_detail"
    assert b.payout_source.value == "PRIOR" and b.family == "cash" and b.field_size_source == "family_prior"


def test_config_validation(cfg):
    bad = copy.deepcopy(cfg)
    del bad["weights"]["appg"]
    with pytest.raises(ValueError):
        ownership.validate_ownership_config(bad)
    bad = copy.deepcopy(cfg)
    bad["field"]["mixtures"]["cash"]["optimizer"] = 0.9
    with pytest.raises(ValueError):
        ownership.validate_ownership_config(bad)
