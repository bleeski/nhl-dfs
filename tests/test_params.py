from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import ModelStatus
from nhl_dfs.models import opportunity as opp
from nhl_dfs.models import params
from nhl_dfs.models.priors import prior_table
from nhl_dfs.models.rates import load_model_config
from pool_builder import varied_pool

pytestmark = pytest.mark.c5

AS_OF = date(2025, 12, 10)


@pytest.fixture(scope="module")
def cfg():
    return load_model_config()


def skater_rows(pid, team, pos, n, *, tier="B", start=date(2025, 10, 5)):
    return [{"nhl_id": pid, "game_id": 2025020000 + i, "game_date": start + timedelta(days=i), "team": team,
             "position": pos, "regime": "regular", "team_stint": 0, "toi_ev_s": 900, "toi_pp_s": 100,
             "toi_sh_s": 20, "toi_s": 1020, "goals": 1 if i % 3 == 0 else 0, "assists": 1 if i % 2 == 0 else 0,
             "pp_points": 0, "sh_points": 0, "sog": 3, "blocks": 1, "a1": 0.3, "a1_missing": 0 if tier == "A" else 1,
             "ixg": 0.3, "ixg_missing": 0 if tier == "A" else 1, "tier": tier} for i in range(n)]


def features(rows, goalie_rows=()):
    return SimpleNamespace(skaters=pd.DataFrame(rows), goalies=pd.DataFrame(list(goalie_rows)),
                           seasons=[20252026], notes=[])


def setup(mode, n_games):
    pool = varied_pool(mode)
    people = {pk: (pr.classic or pr.flex) for pk, pr in pool.persons.items()}
    skaters = [pk for pk, r in people.items() if not r.is_goalie][:3]
    xw = {pk: 8_000_000 + i for i, pk in enumerate(skaters)}
    rows = []
    for pk in skaters:
        r = people[pk]
        rows += skater_rows(xw[pk], r.team, "D" if r.position == "D" else "C", n_games)
    return pool, xw, features(rows)


@pytest.mark.parametrize("mode", list(Mode))
def test_every_person_one_row_and_role_map(mode, cfg):
    pool, xw, f = setup(mode, 10)
    t = params.build(pool, xw, AS_OF, cfg, features=f, line_games=pd.DataFrame())
    assert set(t.persons) == set(pool.persons)
    assert set(t.role_map) == set(pool.by_role_id) and all(t.role_map[r] == pool.by_role_id[r].person_key for r in t.role_map)
    if mode is Mode.SHOWDOWN:
        for pr in pool.persons.values():
            assert t.role_map[pr.cpt.role_id] == t.role_map[pr.flex.role_id]
            assert t.mean_tenths(pr.cpt.role_id) == t.mean_tenths(pr.flex.role_id)  # captain 1.5x applied later
    df = t.to_frame()
    assert len(df) == len(pool.persons) and int(df.isna().sum().sum()) == 0


def test_statuses_per_person_and_run_level(cfg):
    pool, xw, f = setup(Mode.CLASSIC, 10)
    t = params.build(pool, xw, AS_OF, cfg, features=f, line_games=pd.DataFrame())
    assert all(t.persons[pk].source is ModelStatus.MIXED for pk in xw)  # 10 games < 400 prior minutes
    assert t.source() is ModelStatus.MIXED and t.counts()["PRIOR"] == len(pool.persons) - len(xw)
    pool2, xw2, f2 = setup(Mode.CLASSIC, 40)  # 40 x 17 min > 400 prior minutes
    t2 = params.build(pool2, xw2, AS_OF, cfg, features=f2, line_games=pd.DataFrame())
    assert all(t2.persons[pk].source is ModelStatus.HISTORY for pk in xw2)
    none = params.build(pool, {}, AS_OF, cfg, features=features([]), line_games=pd.DataFrame())
    assert none.source() is ModelStatus.PRIOR
    pri = prior_table(pool)
    assert all(none.mean_tenths(r) == pri[r].mean_tenths for r in pool.by_role_id)  # all-prior run = C2 baseline


def test_prior_person_is_dress_weighted_prior_table(cfg):
    pool, xw, f = setup(Mode.CLASSIC, 10)
    t = params.build(pool, xw, AS_OF, cfg, features=f, line_games=pd.DataFrame())
    pri = prior_table(pool)
    pk = next(pk for pk, p in t.persons.items() if p.source is ModelStatus.PRIOR and p.group != "G")
    rid = pool.persons[pk].classic.role_id
    assert t.persons[pk].mean_tenths == round(cfg["skaters"][t.persons[pk].group]["p_dress"] * pri[rid].mean_tenths)


def test_tier_b_params_carry_missingness(cfg):
    pool, xw, f = setup(Mode.CLASSIC, 30)
    df = params.build(pool, xw, AS_OF, cfg, features=f, line_games=pd.DataFrame()).to_frame().set_index("person_key")
    for pk in xw:
        row = df.loc[pk]
        assert row["a1_share_missing"] == 1 and row["shot_quality_missing"] == 1 and row["units_missing"] == 1
        assert row["nhl_id_missing"] == 0 and row["unit_ev"] == ""
    unmatched = df[df["nhl_id_missing"] == 1]
    assert len(unmatched) and (unmatched["nhl_id"] == 0).all()


def test_history_moves_the_mean_toward_the_player(cfg):
    pool, xw, _ = setup(Mode.CLASSIC, 0)
    pk = next(iter(xw))
    r = (pool.persons[pk].classic)
    hot = features([dict(x, goals=2, assists=2, sog=8) for x in skater_rows(xw[pk], r.team, "C", 60)])
    cold = features([dict(x, goals=0, assists=0, sog=0) for x in skater_rows(xw[pk], r.team, "C", 60)])
    m_hot = params.build(pool, xw, AS_OF, cfg, features=hot, line_games=pd.DataFrame()).persons[pk].mean_tenths
    m_cold = params.build(pool, xw, AS_OF, cfg, features=cold, line_games=pd.DataFrame()).persons[pk].mean_tenths
    assert m_hot > 2 * m_cold > 0


def test_call_up_role_state_reaches_the_person(cfg):
    pool, _, _ = setup(Mode.CLASSIC, 0)
    pk = next(pk for pk, pr in pool.persons.items() if not pr.classic.is_goalie and pr.classic.position != "D")
    roles = opp.RoleState(ev_line={pk: 1}, pp_unit={pk: 1})
    t = params.build(pool, {}, AS_OF, cfg, features=features([]), line_games=pd.DataFrame(), roles=roles)
    o = t.persons[pk].opportunity
    assert o.source == "role" and o.toi_pp_s == cfg["roles"]["F"]["pp_unit_toi_s"][0]


def test_run_reports_mixed_and_never_writes_identity_files(tmp_path, monkeypatch):
    from conftest import mini_pair
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.data.history import store as store_mod
    from nhl_dfs.data.identity import crosswalk as cw
    from test_nhl_reports import run as backfill_fixture

    backfill_fixture(tmp_path, moneypuck=False)  # store holding 8 real skaters from 2025-10-09
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", tmp_path / "a" / "store")
    acc = tmp_path / "accepted.csv"
    acc.write_bytes((",".join(cw.ACCEPTED_HEADER) + "\n" + ",".join(
        [cw.key_sha("Connor McDavid", "EDM", "F"), "Connor McDavid", "EDM", "F", "8478449", "exact", "t"]) + "\n").encode())
    monkeypatch.setattr(cw, "ACCEPTED_CSV", acc)
    before = acc.read_bytes()
    r = run_slate(*mini_pair("classic"), offline=True, baseline_only=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert r.ok and r.statuses["MODEL_STATUS"] == "MIXED"
    assert r.manifest["model"]["counts"]["MIXED"] + r.manifest["model"]["counts"]["HISTORY"] >= 1
    assert r.manifest["model"]["as_of"] == "2026-09-28"
    assert acc.read_bytes() == before and not (cw.PROPOSALS_JSON).exists()


def test_slate_as_of_is_the_earlier_of_today_and_first_game():
    from conftest import mini_pair
    from nhl_dfs.build.run import slate_as_of
    from nhl_dfs.intake.salary import read_salary

    pool = read_salary(mini_pair("classic")[0])  # first game 2026-09-29 (ET)
    assert slate_as_of(pool, lambda: datetime(2026, 9, 28, 18, tzinfo=timezone.utc)) == date(2026, 9, 28)
    assert slate_as_of(pool, lambda: datetime(2026, 10, 3, 18, tzinfo=timezone.utc)) == date(2026, 9, 29)


def test_cli_params_writes_parquet_without_nulls(tmp_path, capsys):
    from conftest import mini_pair
    from nhl_dfs import cli

    out = tmp_path / "params.parquet"
    assert cli.main(["params", "--salary", str(mini_pair("showdown")[0]), "--as-of", "2026-09-28", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "PRIOR=" in text and "HISTORY=0" in text and "MODEL_STATUS=PRIOR" in text and "nulls 0" in text
    df = pd.read_parquet(out)
    assert len(df) and int(df.isna().sum().sum()) == 0
    assert cli.main(["params"]) == 2
