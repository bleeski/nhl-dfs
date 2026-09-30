"""C11: grades on synthetic forecasts reproduce known errors; gate reports; backlog appends; run notes."""
import math

import numpy as np
import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.learn import grade_ownership as go
from nhl_dfs.learn.standings import Joined
from nhl_dfs.models.field import Marginals
from pool_builder import make_pool, row, sd_person

pytestmark = pytest.mark.c11


def marg(own, dup=None, field_size=100):
    return Marginals("large_gpp", field_size - 1, field_size, own, {}, {}, dup or {}, {}, {}, 0, False)


def joined(own, mode=Mode.CLASSIC, n=100, dup=None, complete=True, lineups=None):
    return Joined(1, mode, n, own, {}, lineups if lineups is not None else {str(i): ["x"] for i in range(n)},
                  dup or {}, complete)


def test_ownership_grade_reproduces_known_errors():
    pool = make_pool(Mode.CLASSIC, [row(i, "AAA" if i <= 2 else "BBB", "C", 4000) for i in range(1, 5)])
    f = marg({"1": 50.0, "2": 10.0, "3": 0.0, "4": 4.0}, dup={"k1": 10.0}, field_size=200)
    a = joined({"1": 40.0, "2": 10.0, "3": 6.0}, dup={"k1": 3, "k2": 7})
    g = go.grade(f, a, pool=pool)
    assert g.mae_all == pytest.approx((10 + 0 + 6 + 4) / 4)
    assert g.mae_active == pytest.approx((10 + 0 + 6 + 4) / 4)  # every role is active (actual > 0 or pred >= 0.5)
    assert g.weighted_mae == pytest.approx((40 * 10 + 10 * 0 + 6 * 6) / 56, abs=1e-3)  # rounded to 3 places
    assert g.zero_observed_mass == pytest.approx(4.0)  # the forecast's mass on the role nobody drafted
    assert g.team_total_mae == pytest.approx((abs(60 - 50) + abs(4 - 6)) / 2)
    band = {b["band"]: b for b in g.band_calibration}
    assert band["40-101%"] == {"band": "40-101%", "n": 1, "mean_pred": 50.0, "mean_actual": 40.0}
    assert g.dup_count_err["scale"] == pytest.approx(0.5)  # 100 actual entries, forecast at 200
    assert g.dup_count_err["top_predicted"][0] == {"lineup": "k1", "pred_scaled": 5.0, "actual": 3}
    assert g.dup_count_err["top_actual"][0]["lineup"] == "k2" and g.dup_count_err["top_actual"][0]["pred_scaled"] == 0
    assert g.pearson == pytest.approx(float(np.corrcoef([50, 10, 0, 4], [40, 10, 6, 0])[0, 1]), abs=1e-3)


def test_without_observed_zeros_only_drafted_roles_are_graded_and_captain_share_is_separate():
    rows = [r for pid in range(1, 4) for r in sd_person(pid, "AAA", "C", 5000)]
    pool = make_pool(Mode.SHOWDOWN, rows)
    f = marg({"1c": 20.0, "1f": 60.0, "2c": 30.0, "2f": 40.0, "3c": 50.0, "3f": 10.0})
    a = joined({"1c": 10.0, "1f": 70.0, "2c": 40.0}, mode=Mode.SHOWDOWN, complete=False)
    g = go.grade(f, a, pool=pool)
    assert g.n_roles == 3 and g.zero_observed_mass is None and not g.complete
    assert g.cpt_share_err == pytest.approx((10 + 10) / 2)  # only the graded CPT roles 1c and 2c
    assert any("not observed" in n for n in g.notes)
    assert math.isnan(g.pearson) or -1 <= g.pearson <= 1


# -- forecasts ---------------------------------------------------------------------------------------------------------

def _fake_run(tmp_path, keys, draws):
    import json
    from types import SimpleNamespace

    (tmp_path / "scenario" / "selection").mkdir(parents=True)
    (tmp_path / "manifest.json").write_text(json.dumps({"mode": "classic"}), encoding="utf-8")
    (tmp_path / "scenario" / "meta.json").write_text(json.dumps({"person_keys": keys}), encoding="utf-8")
    np.save(tmp_path / "scenario" / "selection" / "chunk_0000.npy", np.asarray(draws, dtype=np.int32))
    return SimpleNamespace(path=tmp_path, inputs=tmp_path / "inputs", run_id="fake")


def test_crps_matches_known_values():
    from nhl_dfs.learn.grade_forecasts import crps

    assert crps(np.full(1000, 3.0), 5.0) == pytest.approx(2.0)
    assert crps(np.array([0.0, 1.0] * 500), 0.0) == pytest.approx(0.25)


def test_forecast_grade_uses_box_scores_for_participation_and_implied_goalie_starts(tmp_path):
    import dataclasses

    from nhl_dfs.data.identity.crosswalk import key_sha
    from nhl_dfs.data.sources.nhl import BoxGoalie, BoxScore, BoxSkater
    from nhl_dfs.intake.salary import GameInfo
    from nhl_dfs.learn import grade_forecasts as gf

    def named(r, name, pk):
        return dataclasses.replace(r, name=name, person_key=pk)

    rows = [named(row(1, "VGK", "C", 5000), "Skater One", "skater one|VGK|F"),
            named(row(2, "VGK", "C", 4000), "Skater Two", "skater two|VGK|F"),
            named(row(3, "VGK", "G", 8000), "Goalie A", "goalie a|VGK|G"),
            named(row(4, "VGK", "G", 7000), "Goalie B", "goalie b|VGK|G")]
    pool = make_pool(Mode.CLASSIC, rows)
    from datetime import datetime, timezone

    pool = dataclasses.replace(pool, games={"CHI@VGK": GameInfo("VGK", "CHI", "", datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))},
                               teams=frozenset({"VGK", "CHI"}))
    keys = [r.person_key for r in rows]
    n = 1000
    ga = np.where(np.arange(n) < 700, 60, 0)  # Goalie A: nonzero in 70% of draws (implied start 0.70)
    gb = np.where(np.arange(n) < 300, 80, 0)
    draws = np.stack([np.full(n, 50), np.full(n, 20), ga, gb], axis=1)
    run = _fake_run(tmp_path, keys, draws)
    acc = {key_sha("Skater One", "VGK", "F"): 1, key_sha("Skater Two", "VGK", "F"): 2,
           key_sha("Goalie A", "VGK", "G"): 3, key_sha("Goalie B", "VGK", "G"): 4}
    box = BoxScore(2026020005, "OFF", "VGK", "CHI", 3, 2, [BoxSkater(1, "VGK", "C", 1, 0, 3, 0, 0, 1000)],
                   [BoxGoalie(4, "VGK", "W", 30, 32, 2, 3600), BoxGoalie(3, "VGK", None, 0, 0, 0, 0)])
    pts = {"skater one|VGK|F": 70, "skater two|VGK|F": 0, "goalie b|VGK|G": 100}
    g = gf.grade(run, [box], actual_points=pts, pool=pool, accepted=acc)
    assert g.participation_status == "BOX_SCORES" and g.excluded["did_not_play"] == 1  # Skater Two was not in the box
    assert g.by_group["F"] == {"n": 1, "mae": 2.0, "bias": -2.0, "crps": 2.0, "cover_p10_p90": 0.0}
    assert g.by_group["G"]["n"] == 1 and g.by_group["G"]["mae"] == pytest.approx(2.0)  # nonzero draws only: 8.0 vs 10
    d = g.goalie_decisions
    assert d["teams"] == 1 and d["accuracy"] == 0.0 and d["brier"] == pytest.approx((0.7 ** 2 + 0.7 ** 2) / 2)
    assert ("CHI@VGK", "skater one|VGK|F", "F") in g.played and ("CHI@VGK", "goalie b|VGK|G", "G") in g.played
    assert g.bonus_rate_calibration.startswith("NOT_AVAILABLE")
    g0 = gf.grade(run, None, actual_points=pts, pool=pool, accepted=acc)
    assert g0.participation_status == "UNKNOWN" and g0.by_group["F"]["n"] == 2 and g0.played == []


# -- backlog -----------------------------------------------------------------------------------------------------------

def test_backlog_add_appends_without_duplicates_and_never_rewrites_rows(tmp_path):
    from nhl_dfs.learn import backlog

    p = tmp_path / "BACKLOG.md"
    head = ("# Backlog\r\n\r\n| ID | Date / evidence | Problem | Metric | Change | Confidence | Acceptance | Priority | Status "
            "| Result |\r\n|---|---|---|---|---|---|---|---|---|---|\r\n| B1 | old | x | y | z | c | a | Low | NEW | |\r\n"
            "| B7 | old | x | y | z | c | a | Low | NEW | |\r\n")
    p.write_bytes(head.encode("utf-8"))
    r = backlog.Row("cache_event_counts_missing", "2026-09-29 settle", "Gap: no event counts | in the cache", "bonus rates",
                    "store them", "High", "a test", "Medium")
    assert backlog.add(r, p) == ("B8", True)
    assert backlog.add(r, p) == ("B8", False)  # deduplicated by key
    assert backlog.add(backlog.Row("ownership_prior_error", "e", "p", "m", "c", "c", "a", "High"), p) == ("B20", False)
    raw = p.read_bytes()
    assert raw.startswith(head.encode("utf-8")) and raw.count(b"[key: cache_event_counts_missing]") == 1
    line = raw.decode("utf-8").splitlines()[-1]
    assert line.startswith("| B8 | 2026-09-29 settle [key: cache_event_counts_missing] |") and "no event counts / in" in line
    assert raw.endswith(b"\r\n")


# -- gates and the settle command ----------------------------------------------------------------------------------------

def test_gate_report_shows_the_tier_for_planted_counts():
    from nhl_dfs.learn.settle import evidence_counts, gate_report

    idx = {}
    for i in range(25):  # 25 Classic slate dates, 6,250 skater-games and 400 goalie starts, 250 labels per slate
        played = [[f"2026-10-{i:02d}", "A@B", f"s{j}|A|F", "F"] for j in range(250)]
        played += [[f"2026-10-{i:02d}", "A@B", f"g{j}|A|G", "G"] for j in range(16)]
        idx[f"r{i}"] = {"run_id": f"r{i}", "mode": "classic", "slate_date": f"2026-10-{i:02d}", "slate_id": f"s{i}",
                        "games": ["A@B"], "frozen": True, "complete_payout": i % 2 == 0,
                        "contests": {"1": {"family": "large_gpp", "labels": 250}}, "played": played}
    idx["late"] = dict(idx["r0"], run_id="late", frozen=False, slate_date="2027-01-01")  # a POST_LOCK run never counts
    c = evidence_counts(idx, "classic")
    assert (c.slate_dates, c.slate_groups, c.skater_games, c.goalie_starts, c.ownership_labels) == (25, 25, 6250, 400, 6250)
    assert c.complete_payout_dates == 13 and c.groups_by_family == {"large_gpp": 25}
    g = gate_report(idx, "classic")
    assert g["tier"] == "rate_correction" and g["allowed"]["rate_correction"] and not g["allowed"]["field_fit"]
    assert any("slate_groups 25 < 30" in s for s in g["shortfalls"]["ownership_fit"])
    s = gate_report(idx, "showdown")
    assert s["tier"] == "every_run" and s["counts"]["slate_groups"] == 0


def test_settle_command_writes_grades_ledger_notes_and_backlog_and_keeps_the_frozen_files(tmp_path, capsys):
    from datetime import datetime, timezone

    from conftest import TESTS
    from nhl_dfs.build.notes import write_run_notes
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.cli import main

    ls = TESTS / "fixtures" / "late_swap" / "classic"
    r = run_slate(ls / "DKSalaries.csv", ls / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
    sdir = tmp_path / "standings"
    sdir.mkdir()
    hdr = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\r\n"
    body = "".join(f"{i + 1},{e},bleeski,0,{50 - i},,,,,,\r\n" for i, e in enumerate(
        ["7100000001", "7100000002", "7100000003", "7100000004", "7100000005"]))
    (sdir / "contest-standings-297000001.csv").write_bytes(("﻿" + hdr + body).encode("utf-8"))
    bl = tmp_path / "BACKLOG.md"
    bl.write_bytes(b"# Backlog\r\n\r\n| ID | a | b | c | d | e | f | g | h | i |\r\n|---|---|---|---|---|---|---|---|---|---|\r\n"
                   b"| B1 | x | x | x | x | x | x | x | x | |\r\n")
    args = ["settle", r.run.run_id, str(sdir), "--runs-root", str(tmp_path / "runs"), "--ledger-root",
            str(tmp_path / "ledger"), "--backlog", str(bl), "--no-boxscores"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "SETTLE=OK" in out and "FREEZE_CHECK=OK" in out and "FORECAST=PRE_LOCK" in out
    assert "PAYOUT_SOURCE=UNKNOWN" in out and "UNKNOWN_FEES=$5.00" in out and "BACKLOG B24: already holds" in out
    assert (r.run.path / "settle" / "grades.json").exists() and (tmp_path / "ledger" / "ledger.parquet").exists()
    assert (tmp_path / "ledger" / "winnings_needed" / f"{r.run.run_id}.csv").exists()  # not in a fixture folder
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    assert notes.count("## Settlement") == 1 and "Forecasts: not graded" in notes
    write_run_notes(r.run, read_manifest(r.run))  # a later rewrite keeps the Settlement section
    assert (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8").count("## Settlement") == 1
    assert main(args) == 0  # a second settle: one ledger copy, one notes section, no duplicate backlog rows
    from nhl_dfs.learn import ledger as lg

    assert len(lg.read(tmp_path / "ledger")) == 5
    assert (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8").count("## Settlement") == 1
    assert bl.read_bytes().count(b"| B") == 1


def test_a_rehearsal_clocked_run_written_after_the_first_game_is_not_pre_lock(tmp_path):
    import json
    import os
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from nhl_dfs.intake.salary import GameInfo
    from nhl_dfs.learn.settle import forecast_status

    first = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
    pool = SimpleNamespace(games={"A@B": GameInfo("B", "A", "", first)})
    (tmp_path / "scenario").mkdir()
    meta = tmp_path / "scenario" / "meta.json"
    meta.write_text(json.dumps({}), encoding="utf-8")
    run = SimpleNamespace(path=tmp_path)
    m = {"created_utc": "2026-09-29T20:00:00Z"}  # a pinned clock, before the first game
    os.utime(meta, (datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc).timestamp(),) * 2)
    assert forecast_status(run, m, pool)[0] == "PRE_LOCK"
    os.utime(meta, (datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc).timestamp(),) * 2)  # written the next day
    assert forecast_status(run, m, pool)[0] == "POST_LOCK"
