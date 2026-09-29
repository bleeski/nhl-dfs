import json
import re
from datetime import date
from urllib.parse import unquote

import numpy as np
import pandas as pd
import pytest

from conftest import TESTS, FakeTransport
from nhl_dfs.data.history import combine, nhl_reports, store
from nhl_dfs.data.history import moneypuck as mp
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.data.sources import nhl

pytestmark = pytest.mark.c4

H = TESTS / "fixtures" / "history"
GAME = 2025020017
TODAY = date(2025, 10, 20)  # pinned: windows Sept 1 to Oct 19, 2025
SEASON = 20252026


def _window(url):
    d = re.findall(r'gameDate[<>]="(\d{4}-\d{2}-\d{2})"', unquote(url))
    return date.fromisoformat(d[0]), date.fromisoformat(d[1])


def report_route(name):
    body = json.loads((H / name).read_bytes())

    def respond(url):
        d0, d1 = _window(url)
        if d0 <= date(2025, 10, 9) <= d1:
            return json.dumps(body).encode()
        return b'{"data": [], "total": 0}'

    return respond


ROUTES = [
    ("skater/timeonice", 200, report_route("nhl_skater_timeonice_20251009.json")),
    ("skater/realtime", 200, report_route("nhl_skater_realtime_20251009.json")),
    ("skater/summary", 200, report_route("nhl_skater_summary_20251009.json")),
    ("goalie/summary", 200, report_route("nhl_goalie_summary_20251009.json")),
    (f"gamecenter/{GAME}/boxscore", 200, (H / f"nhl_boxscore_{GAME}.json").read_bytes()),
]
MP_FILES = {"skaters": "mp_skaters_game_2025.zip", "goalies": "mp_goalies_game_2025.zip", "lines": "mp_lines_game_2025.zip"}


def mp_transport(url, headers, timeout_s):
    for kind, name in MP_FILES.items():
        if f"/{kind}/2025.zip" in url:
            return 200, (H / name).read_bytes(), {}
    return 404, b"", {}


def run(tmp_path, *, moneypuck, routes=ROUTES, since=None, sub="a"):
    cfg = load_sources_config()
    cfg = {**cfg, "http": {**cfg["http"], "max_calls_per_run": cfg["history"]["max_calls_per_backfill"] * 5}}
    transport = FakeTransport(routes)
    cache = HttpCache(tmp_path / sub / "cache", config=cfg, transport=transport, sleep=lambda s: None)
    stats = nhl_reports.backfill([SEASON], cache=cache, store_root=tmp_path / sub / "store", cfg=cfg, today=TODAY,
                                 moneypuck=moneypuck, raw_root=tmp_path / sub / "raw", mp_transport=mp_transport,
                                 since=since)
    return stats, store.read("skater_games", [SEASON], root=tmp_path / sub / "store"), \
        store.read("goalie_games", [SEASON], root=tmp_path / sub / "store"), transport


@pytest.fixture(scope="module")
def both(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hist")
    return run(tmp, moneypuck=False, sub="b"), run(tmp, moneypuck=True, sub="a")


def test_tier_b_columns_are_priors_flagged_missing(both):
    (stats, sk, gl, transport), _ = both
    assert len(sk) == 8 and set(sk["tier"]) == {"B"} and set(sk["game_id"]) == {GAME}
    assert stats.moneypuck == {f"{k}/{SEASON}": "disabled" for k in ("skaters", "goalies", "lines")}
    for c in ("a1", "a2", "ixg", "hd_xg", "shared_ice", "onice_xgf", "attempts"):
        assert (sk[f"{c}_missing"] == 1).all(), c
        assert sk[c].notna().all(), c  # filled with the league prior, not left blank
    assert sk["mp_toi_5on4_s"].isna().all() and (sk["mp_toi_5on4_s_missing"] == 1).all()
    pri = combine.load_priors()["skaters"]
    fwd = sk[sk["position"] != "D"].iloc[0]
    assert fwd["a1"] == pytest.approx(fwd["assists"] * pri["F"]["a1_share"])
    assert fwd["ixg"] == pytest.approx(fwd["sog"] * pri["F"]["ixg_per_sog"])
    assert (sk["toi_strength_source"] == "nhl").all() and set(sk["regime"]) == {"regular"}
    assert len(gl) == 2 and set(gl["decision"]) == {"W", "L"} and (gl["gsax_missing"] == 1).all()


def test_tier_a_fills_the_same_columns_from_moneypuck(both):
    _, (stats, sk, gl, _) = both
    assert set(sk["tier"]) == {"A"} and stats.tiers[str(SEASON)] == {"A": 8, "B": 0}
    for c in ("a1", "a2", "ixg", "hd_xg", "onice_xgf", "attempts", "mp_toi_5on4_s"):
        assert (sk[f"{c}_missing"] == 0).all(), c
    assert (sk["a1"] + sk["a2"] == sk["assists"]).all()
    assert (gl["xga_missing"] == 0).all() and np.allclose(gl["gsax"], gl["xga"] - gl["goals_against"])
    assert all(v.startswith("ok") for v in stats.moneypuck.values())


def test_both_tiers_agree_on_toi_by_strength_sog_and_blocks(both):
    (_, b, _, _), (_, a, _, _) = both
    assert list(a.columns) == list(b.columns)  # Tier B builds the same columns
    cols = ["toi_s", "toi_ev_s", "toi_pp_s", "toi_sh_s", "sog", "blocks", "goals", "assists"]
    left = b.set_index("nhl_id")[cols].sort_index()
    right = a.set_index("nhl_id")[cols].sort_index()
    pd.testing.assert_frame_equal(left, right)
    # and the MoneyPuck file itself agrees with the NHL reports on SOG, blocks, goals, assists and total TOI
    m = mp.load("skaters", 2025, "game", path=H / MP_FILES["skaters"]).set_index("nhl_id").sort_index()
    for c in ("sog", "blocks", "goals", "assists"):
        assert (m[c] == right[c]).all(), c
    assert (m["mp_toi_s"] == right["toi_s"]).all()


def test_boxscore_crosscheck_and_goalie_decisions(both):
    (stats, sk, gl, transport), _ = both
    assert stats.crosscheck[str(GAME)]["checked"] == 10 and stats.crosscheck[str(GAME)]["mismatch"] == 0
    other = nhl.parse_boxscore(json.loads((H / "nhl_boxscore_2025020018.json").read_bytes()))
    assert nhl_reports.crosscheck(other, sk, gl) == {"checked": 0, "mismatch": 0}  # no rows for that game
    box = nhl.parse_boxscore(json.loads((H / f"nhl_boxscore_{GAME}.json").read_bytes()))
    wrong = sk.copy()
    wrong.loc[wrong.index[0], "sog"] += 1
    assert nhl_reports.crosscheck(box, wrong, gl)["mismatch"] == 1


def test_windows_cover_completed_days_only():
    w = nhl_reports.windows_for(SEASON, days=21, today=TODAY)
    assert w[0][0] == date(2025, 9, 1) and w[-1][1] == date(2025, 10, 19)
    assert all((b - a).days <= 20 for a, b in w)
    w2 = nhl_reports.windows_for(SEASON, days=21, since=date(2025, 10, 10), today=TODAY)
    assert w2 == [(date(2025, 10, 10), date(2025, 10, 19))]


def test_capped_window_is_split_and_a_capped_day_raises(tmp_path):
    def capped(url):
        d0, d1 = _window(url)
        if d0 != d1:
            return b'{"data": [], "total": 10000}'  # looks truncated: must split
        return b'{"data": [], "total": 0}'

    routes = [(p, 200, capped) for p in ("skater/timeonice", "skater/realtime", "skater/summary", "goalie/summary")]
    stats, sk, _, _ = run(tmp_path, moneypuck=False, routes=routes)
    assert stats.splits > 0 and sk.empty
    routes = [(p, 200, lambda url: b'{"data": [], "total": 10000}') for p in
              ("skater/timeonice", "skater/realtime", "skater/summary", "goalie/summary")]
    with pytest.raises(nhl_reports.TruncatedWindow):
        run(tmp_path, moneypuck=False, routes=routes, sub="c")


def test_incremental_since_keeps_earlier_rows(tmp_path):
    run(tmp_path, moneypuck=False)
    root = tmp_path / "a" / "store"
    old = store.read("nhl_skater_games", [SEASON], root=root)
    planted = old.iloc[[0]].copy()
    planted["game_id"], planted["game_date"] = 2025020001, date(2025, 10, 7)
    store.write("nhl_skater_games", SEASON, pd.concat([old, planted]), root=root)
    stats, sk, _, transport = run(tmp_path, moneypuck=False, since=date(2025, 10, 9))
    assert stats.windows == 1 and set(sk["game_id"]) == {GAME, 2025020001}


def test_moneypuck_only_game_uses_situation_mapping():
    m = mp.load("skaters", 2025, "game", path=H / MP_FILES["skaters"])
    out = combine.combine_skaters(pd.DataFrame(), m, pd.DataFrame(), combine.load_priors())
    assert len(out) == 8 and (out["toi_strength_source"] == "moneypuck_situations").all()
    j = out.set_index("nhl_id").join(m.set_index("nhl_id")[["mp_toi_5on4_s"]], rsuffix="_src")
    assert (j["toi_pp_s"] == j["mp_toi_5on4_s_src"]).all() and (j["mp_toi_5on4_s_missing"] == 0).all()
    assert (out["pp_points_missing"] == 1).all() and out["pp_points"].isna().all()
    assert np.allclose(out["toi_ev_s"] + out["toi_pp_s"] + out["toi_sh_s"], out["toi_s"])
