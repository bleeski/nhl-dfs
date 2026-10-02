"""C6: market de-vig, implied total, the fit against the shared resolution process, staleness."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy import stats

from nhl_dfs.data.sources.nhl import GameOdds, OddsSnapshot
from nhl_dfs.sim import market
from nhl_dfs.sim import resolve as rs

pytestmark = pytest.mark.c6

CFG = market.load_sim_config()
T0 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)


def _odds(home_ml, away_ml, line=5.5, over=-110, under=-110, **kw):
    return GameOdds("g1", "AAA", "BBB", None, home_ml, away_ml, None, None, None, line, over, under, None, None, **kw)


def _price(p: float) -> int:
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def test_implied_removes_the_vig_pairwise():
    a, b = market.implied(-110, -110)
    assert a == pytest.approx(0.5) and b == pytest.approx(0.5)
    a, b = market.implied(-180, 150)
    assert a + b == pytest.approx(1.0) and a > 0.6
    # raw probabilities sum above 1 (the vig); the de-vigged pair does not
    assert market._raw_prob(-180) + market._raw_prob(150) > 1.0


@pytest.mark.parametrize("line,mu", [(5.5, 5.9), (6.0, 6.3), (6.5, 6.6), (5.5, 5.4)])
def test_implied_total_inverts_the_poisson_over_price(line, mu):
    if float(line).is_integer():
        k = int(line)
        q = stats.poisson.sf(k, mu) / (1 - stats.poisson.pmf(k, mu))
    else:
        q = stats.poisson.sf(int(line), mu)
    over, under = _price(q), _price(1 - q)
    got = market.implied_total(line, over, under)
    assert got == pytest.approx(mu, abs=0.06)  # price rounding to whole American odds


def test_league_constants_reproduce_the_measured_rates():
    """config/sim.yaml's resolve block is DERIVED from three measured values; hold it to them."""
    rules = rs.Rules.from_config(CFG)
    lam = float(CFG["resolve"]["lambda0_league"])
    s = rs.summary(lam, lam, rules)
    assert (s.e_home_goals + s.e_away_goals) / 2 == pytest.approx(2.970, abs=0.01)  # regulation goals per team
    assert s.p_tie == pytest.approx(597 / 2624, abs=0.002)
    assert s.e_en / 2 == pytest.approx(0.196, abs=0.003)
    assert s.p_home_win == pytest.approx(0.5, abs=1e-9)


def _simulated(lh, la, rules, n=400_000, seed=5):
    d = rs.sample(np.random.default_rng(seed), lh, la, n, rules)
    total = d.reg_h + d.reg_a + d.eq_h + d.eq_a + d.en_h + d.en_a + d.ot_goal + d.so_extra
    return float(d.home_win.mean()), float(total.mean()), float((d.en_h + d.en_a).mean()), float(d.tie.mean())


def test_resolution_sampler_matches_its_analytic_summary():
    rules = rs.Rules.from_config(CFG)
    for lh, la in ((2.9, 2.6), (2.2, 3.4), (3.1, 3.0)):
        s = rs.summary(lh, la, rules)
        win, total, en, tie = _simulated(lh, la, rules)
        assert win == pytest.approx(s.p_home_win, abs=0.005)
        assert total == pytest.approx(s.e_total, abs=0.03)
        assert en == pytest.approx(s.e_en, abs=0.01)
        assert tie == pytest.approx(s.p_tie, abs=0.005)


@pytest.mark.parametrize("home_ml,away_ml,line", [(-150, 130, 6.0), (-110, -110, 5.5), (120, -140, 6.5), (-260, 215, 5.5)])
def test_fit_reproduces_implied_win_probability_and_total_in_simulation(home_ml, away_ml, line):
    odds = _odds(home_ml, away_ml, line)
    strength = market.TeamStrength(2.75, 2.75)
    gr = market.fit_game(odds, strength, T0, None, cfg=CFG)
    assert gr.source == "MARKET" and not gr.stale
    p_h = market.implied(home_ml, away_ml)[0]
    target_total = market.implied_total(line, odds.over_price, odds.under_price)
    assert gr.target_p_home == pytest.approx(p_h) and gr.target_total == pytest.approx(target_total)
    # analytic
    assert gr.p_home_win == pytest.approx(p_h, abs=0.01)
    assert gr.e_total == pytest.approx(target_total, abs=0.1)
    # and through the sampler the simulator uses
    win, total, _, tie = _simulated(gr.lambda_home, gr.lambda_away, rs.Rules.from_config(CFG))
    assert win == pytest.approx(p_h, abs=0.01)
    assert total == pytest.approx(target_total, abs=0.1)
    assert gr.p_ot == pytest.approx(tie, abs=0.005)


def test_stale_rule_flips_when_a_goalie_confirmation_postdates_the_odds():
    strength = market.TeamStrength(2.5, 3.0)
    odds = _odds(-150, 130, 6.0)
    fresh = market.fit_game(odds, strength, T0, T0 - timedelta(minutes=30), cfg=CFG)
    stale = market.fit_game(odds, strength, T0, T0 + timedelta(minutes=30), cfg=CFG)
    unconfirmed = market.fit_game(odds, strength, T0, None, cfg=CFG)
    assert not fresh.stale and not unconfirmed.stale and stale.stale
    assert any("STALE" in n for n in stale.notes)
    # a stale market moves toward the model: its home intensity sits between the fresh fit and the model
    lo, hi = sorted((fresh.lambda_home, strength.lam_home))
    assert lo <= stale.lambda_home <= hi


def test_no_odds_takes_the_model_and_says_so():
    strength = market.TeamStrength(2.9, 2.6)
    for odds in (None, _odds(None, None, 5.5)):
        gr = market.fit_game(odds, strength, T0, None, cfg=CFG)
        assert gr.source == "MODEL" and gr.lambda_home == 2.9 and gr.lambda_away == 2.6
        assert any(n.startswith("MODEL") for n in gr.notes)
        assert 0.0 < gr.p_home_reg_win < 1.0 and 0.0 < gr.p_ot < 1.0


def test_extreme_market_is_capped_and_logged():
    gr = market.fit_game(_odds(-900, 600, 7.5), market.TeamStrength(2.7, 2.7), T0, None, cfg=CFG)
    assert any("CAPPED" in n for n in gr.notes)
    assert gr.lambda_home <= 2.7 * 1.35 + 1e-9 and gr.lambda_away >= 2.7 * 0.65 - 1e-9


def test_match_odds_uses_only_verified_codes():
    snap = OddsSnapshot(T0, "source", "book", "nhl_partner_odds", [
        GameOdds("1", "WPG", "DAL", None, -120, 100, None, None, None, 5.5, -110, -110, None, None),
        GameOdds("2", "OTT", "DAL", None, -120, 100, None, None, None, 5.5, -110, -110, None, None)])
    games = {"DAL@WPG": ("WPG", "DAL"), "DAL@OTT": ("OTT", "DAL"), "X@Y": ("YYY", "XXX")}
    got, why = market.match_odds(snap, games)
    assert got["DAL@WPG"].home_ml == -120
    assert why["DAL@OTT"] == "unverified team code OTT for source nhl_partner_odds"  # OTT: no DK file has shown it yet
    assert "unverified" in why["X@Y"]
    assert market.match_odds(None, games)[1]["X@Y"] == "no odds snapshot"


def _lobby_codes() -> dict[str, set[str]]:
    """DK code -> nicknames, from the recorded DK lobby GameSets (tests/fixtures/http/SOURCES.md)."""
    import json

    from conftest import fixture_bytes

    out: dict[str, set[str]] = {}
    for day in ("2026-09-29", "2026-09-30", "2026-10-01"):
        for gs in json.loads(fixture_bytes(f"dk_lobby_gamesets_{day}.json"))["GameSets"]:
            for c in gs["Competitions"]:
                if c.get("Sport") != "NHL":
                    continue
                away, home = (x.strip() for x in c["Description"].split("@"))
                out.setdefault(away, set()).add(c["AwayTeamName"])
                out.setdefault(home, set()).add(c["HomeTeamName"])
    return out


def test_every_dk_lobby_code_is_verified_in_teams_yaml():
    """B35: a DK code seen in a DK file is filled and verified; the row's name ends in DK's nickname."""
    import yaml

    teams = {t["dk"]: t for t in yaml.safe_load((market.REPO_ROOT / "config" / "teams.yaml").read_text(encoding="utf-8"))["teams"]
             if t["dk_verified"]}
    seen = _lobby_codes()
    assert len(seen) == 31
    for code, names in seen.items():
        assert code in teams, code
        assert all(teams[code]["name"].endswith(n) for n in names), (code, names)


def test_2026_10_01_slate_matches_every_game_on_market():
    """B35 acceptance: the run's cached DK feed (trimmed to the slate's four games) matches all four
    games, and each fits as MARKET. On 2026-10-01 three of the four fell back to MODEL."""
    import json

    from conftest import fixture_bytes
    from nhl_dfs.data.sources import nhl

    snap = nhl.parse_partner_odds(json.loads(fixture_bytes("nhl_partner_odds_2026-10-01_slate.json")))
    games = {"SEA@CGY": ("CGY", "SEA"), "CHI@UTA": ("UTA", "CHI"), "EDM@VAN": ("VAN", "EDM"), "FLA@SJS": ("SJS", "FLA")}
    got, why = market.match_odds(snap, games)
    assert why == {} and set(got) == set(games)
    for key, odds in got.items():
        gr = market.fit_game(odds, market.TeamStrength(3.0, 3.0), snap.as_of_utc, None, cfg=CFG)
        assert gr.source == "MARKET", (key, gr.notes)
    assert market.implied(got["FLA@SJS"].home_ml, got["FLA@SJS"].away_ml)[0] < 0.5  # DK has Florida favored


def test_slate_uses_a_fresh_snapshot_for_verified_games_and_refuses_an_old_one():
    from conftest import MINI
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.sim.slate import build_slate
    from sim_helpers import synthetic_params

    pool = read_salary(MINI / "classic" / "DKSalaries.csv")
    teams = sorted({g.home for g in pool.games.values()} | {g.away for g in pool.games.values()})
    params = synthetic_params(tuple(teams))
    game_odds = GameOdds("1", "EDM", "VAN", None, -130, 110, None, None, None, 6.0, -105, -115, None, None)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    fresh = OddsSnapshot(now - timedelta(hours=2), "source", "book", "nhl_partner_odds", [game_odds])
    slate, lines = build_slate(pool, params, fresh, now=now)
    by_key = {g.key: g for g in slate.games}
    assert by_key["VAN@EDM"].rates.source == "MARKET"
    assert by_key["VAN@EDM"].rates.p_home_win == pytest.approx(market.implied(-130, 110)[0], abs=0.01)
    others = [g for k, g in by_key.items() if k != "VAN@EDM"]
    assert others and all(g.rates.source == "MODEL" for g in others)
    assert len(lines) == len(pool.games)
    old = OddsSnapshot(now - timedelta(days=32), "source", "book", "nhl_partner_odds", [game_odds])
    slate, lines = build_slate(pool, params, old, now=now)
    assert all(g.rates.source == "MODEL" for g in slate.games)
    assert "days old" in lines[0]
