"""C41 ownership band (flags 119 to 122, docs/experiments/ownership_band_2026-10-16.md): the error-band function on the graded
fixture, the scaled field, the anchored refill and the spread report."""

import csv
import json
from pathlib import Path

import pytest

from nhl_dfs.models import ownership

pytestmark = pytest.mark.c41

FIXTURE = Path(__file__).parent / "fixtures" / "c41"


def graded_rows() -> list[ownership.GradedRow]:
    contests = json.loads((FIXTURE / "contests.json").read_text(encoding="utf-8"))
    with open(FIXTURE / "graded_ownership.csv", encoding="utf-8", newline="") as fh:
        return [ownership.GradedRow(contests[r["contest_id"]]["forecast_run"], r["contest_id"], contests[r["contest_id"]]["family"],
                                    contests[r["contest_id"]]["mode"], r["role_id"], float(r["pred_pct"]), float(r["actual_pct"]))
                for r in csv.DictReader(fh)]


# -- the band (flag 119) ------------------------------------------------------------------------------------------------

def test_the_band_function_reproduces_the_0930_mae_of_the_settle_grades():
    band = ownership.error_band(graded_rows())
    got = {c.contest_id: c for c in band.contests}
    # runs/20260930-233424-classic/settle/grades.json, mae_all and mae_popular (the synthesis review's "MAE 3.9 to 4.3")
    want = {"196218433": (4.106, 10.107), "196218434": (4.034, 11.147), "196218438": (4.315, 11.279), "196228907": (3.942, 10.034)}
    for cid, (mae, popular) in want.items():
        c = got[cid]
        assert (c.mae, c.mae_popular, c.forecast_run, c.mode) == (mae, popular, "20260930-214909-classic", "classic")
        assert 3.9 <= c.mae <= 4.33


def test_the_other_graded_forecasts_reproduce_their_grades_too():
    got = {c.contest_id: c for c in ownership.error_band(graded_rows()).contests}
    want = {"195958173": 2.233, "196048725": 2.83, "196048727": 3.047, "196050215": 2.594}  # 09-29 Classic and Showdown
    assert {cid: got[cid].mae for cid in want} == want
    assert set(got) == set(want) | {"196218433", "196218434", "196218438", "196228907"}  # large_gpp only: the cash contest is out


def test_every_graded_contest_is_flatter_than_its_forecast_and_the_band_is_the_mirror_of_the_flattest():
    band = ownership.error_band(graded_rows())
    assert all(0.7 < c.slope < 0.86 for c in band.contests)  # planning saw 0.715 to 0.850 (the preregistration's table)
    assert {f: v["mode"] for f, v in band.pooled.items()} == {"20260929-222125-classic": "classic", "20260930-214909-classic": "classic",
                                                              "20260930-013521-showdown": "showdown"}
    classic = {f: v["slope"] for f, v in band.pooled.items() if v["mode"] == "classic"}
    assert band.beta_low["classic"] == min(classic.values()) == classic["20260930-214909-classic"]
    assert band.beta_high["classic"] == pytest.approx(1.0 / band.beta_low["classic"], abs=1e-4)
    assert 0.72 < band.beta_low["classic"] < 0.76


def test_the_pooled_slope_weights_each_contest_by_its_own_spread():
    rows = [ownership.GradedRow("f", "1", "large_gpp", "classic", "a", 10.0, 8.0),
            ownership.GradedRow("f", "1", "large_gpp", "classic", "b", 0.0, 2.0),
            ownership.GradedRow("f", "2", "large_gpp", "classic", "a", 20.0, 20.0),
            ownership.GradedRow("f", "2", "large_gpp", "classic", "b", 0.0, 0.0),
            ownership.GradedRow("f", "3", "cash", "classic", "a", 30.0, 0.0),
            ownership.GradedRow("f", "3", "cash", "classic", "b", 0.0, 30.0)]
    band = ownership.error_band(rows)
    # contest 1: dp (5, -5), da (3, -3): xy 30, xx 50; contest 2: xy 200, xx 200; cash left out of the pool
    assert [c.slope for c in band.contests] == [0.6, 1.0, -1.0]
    assert band.pooled == {"f": {"mode": "classic", "contests": ["1", "2"], "slope": round(230 / 250, 4)}}
    assert band.beta_low == {"classic": 0.92} and band.beta_high == {"classic": round(1 / 0.92, 4)}
    assert ownership.concentration_slope([1.0, 1.0], [0.0, 5.0]) != ownership.concentration_slope([1.0, 1.0], [0.0, 5.0])  # nan


def test_the_fixture_carries_no_names_or_lineups():
    head = (FIXTURE / "graded_ownership.csv").read_text(encoding="utf-8").splitlines()[0]
    assert head == "contest_id,role_id,pred_pct,actual_pct"
    assert all(set(v) == {"forecast_run", "graded_run", "family", "mode"}
               for v in json.loads((FIXTURE / "contests.json").read_text(encoding="utf-8")).values())
