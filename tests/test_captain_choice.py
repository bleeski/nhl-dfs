"""C23 Showdown Captain choice (flags 96 to 100, docs/experiments/captain_2026-10-13.md): the per-cohort Captain report in
scripts/standings_synthesis.py, the goalie gate, the Captain leverage term and the split report. No network in any test."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nhl_dfs.contracts.ids import normalize_name
from nhl_dfs.learn import standings as st

pytestmark = pytest.mark.c23

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("standings_synthesis", REPO / "scripts" / "standings_synthesis.py")
syn = sys.modules.get("standings_synthesis") or importlib.util.module_from_spec(_spec)
if "standings_synthesis" not in sys.modules:
    sys.modules["standings_synthesis"] = syn  # dataclasses resolve string annotations through sys.modules
    _spec.loader.exec_module(syn)

META = {"name": "NHL Fixture Showdown", "fee": 1.0, "max_entries": 100, "max_per_user": 1, "pool": 10.0}


# ---- the report, hand-counted -------------------------------------------------------------------------------------

def _day_map(players: dict[str, tuple[str, str]], winner_goalie: str) -> "syn.DayMap":
    """players: name -> (team, pos F/D/G); one game AAA@BBB-style between the two teams named."""
    teams = sorted({t for t, _ in players.values()})
    opp = {teams[0]: teams[1], teams[1]: teams[0]}
    game = f"{teams[0]}@{teams[1]}"
    by_norm, by_last = {}, {}
    for i, (name, (team, pos)) in enumerate(sorted(players.items())):
        dec = ("W" if name == winner_goalie else "L") if pos == "G" else None
        p = syn.Player(i + 1, name, team, pos, game, decision=dec, dressed=True)
        by_norm[normalize_name(name)] = [p]
        by_last.setdefault(normalize_name(name).split()[-1], []).append(p)
    return syn.DayMap("2026-10-05", [game], {}, opp, {t: game for t in teams}, by_norm, by_last, {})


HAND = {  # 12 lineups, Captain first, ranks 1 to 12 in this order
    "Gary Goalie": ("AAA", "G"), "Adam Alpha": ("AAA", "F"), "Andy Apex": ("AAA", "F"), "Abe Arrow": ("AAA", "F"),
    "Art Axel": ("AAA", "D"), "Ace Atom": ("AAA", "F"),
    "Bob Keeper": ("BBB", "G"), "Ben Bravo": ("BBB", "F"), "Bill Bolt": ("BBB", "F"), "Bo Blaze": ("BBB", "F"),
    "Bud Brick": ("BBB", "D"), "Boyd Beam": ("BBB", "F"),
}
HAND_LINEUPS = [
    ["Adam Alpha", "Andy Apex", "Abe Arrow", "Art Axel", "Ace Atom", "Ben Bravo"],    # AAA 5-1, lean won
    ["Gary Goalie", "Adam Alpha", "Andy Apex", "Abe Arrow", "Ben Bravo", "Bill Bolt"],  # goalie CPT, AAA 4-2
    ["Gary Goalie", "Adam Alpha", "Andy Apex", "Ben Bravo", "Bill Bolt", "Bo Blaze"],   # goalie CPT, 3-3
    ["Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick", "Boyd Beam", "Adam Alpha"],     # BBB 5-1, lean lost
    ["Ben Bravo", "Bill Bolt", "Bo Blaze", "Bob Keeper", "Adam Alpha", "Andy Apex"],    # BBB 4-2, goalie at FLEX
    ["Adam Alpha", "Gary Goalie", "Andy Apex", "Abe Arrow", "Ben Bravo", "Bill Bolt"],  # AAA 4-2, goalie at FLEX
    ["Bob Keeper", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick", "Adam Alpha"],    # goalie CPT, BBB 5-1
    ["Adam Alpha", "Andy Apex", "Abe Arrow", "Ace Atom", "Ben Bravo", "Bill Bolt"],     # AAA 4-2
    ["Andy Apex", "Adam Alpha", "Abe Arrow", "Art Axel", "Ace Atom", "Bob Keeper"],     # AAA 5-1, goalie at FLEX
    ["Ben Bravo", "Adam Alpha", "Andy Apex", "Abe Arrow", "Bill Bolt", "Bo Blaze"],     # 3-3
    ["Adam Alpha", "Andy Apex", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Bud Brick"],     # BBB 4-2
    ["Gary Goalie", "Adam Alpha", "Ben Bravo", "Bill Bolt", "Bo Blaze", "Boyd Beam"],   # goalie CPT, BBB 4-2
]


def _hand_csv() -> bytes:
    rows = ["Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS"]
    own = [(n, "FLEX") for n in sorted(HAND)] + [(n, "CPT") for n in sorted({lu[0] for lu in HAND_LINEUPS})]
    for i in range(max(len(HAND_LINEUPS), len(own))):
        if i < len(HAND_LINEUPS):
            lu = HAND_LINEUPS[i]
            text = f"CPT {lu[0]} " + " ".join(f"FLEX {n}" for n in lu[1:])
            left = f"{i + 1},{5000 + i},entrant{i + 1:02d} (1/1),0,{120 - 5 * i},{text}"
        else:
            left = ",,,,,"
        right = f"{own[i][0]},{own[i][1]},10.00%,{'15' if own[i][1] == 'CPT' else '10'}" if i < len(own) else ",,,"
        rows.append(f"{left},,{right}")
    return ("\n".join(rows) + "\n").encode()


def test_the_captain_report_matches_a_hand_count(tmp_path):
    s = st.parse(_hand_csv(), "contest-standings-990000001.csv")
    c = syn.analyze_contest(s, _day_map(HAND, "Gary Goalie"), META, {}, tmp_path)
    f, t = c["cohorts"]["field"], c["cohorts"]["top1"]
    assert c["slots_unresolved"] == 0 and f["n"] == 12 and t["n"] == 1
    # Captains: Adam 4, Gary 3, Ben 3, Bob 1, Andy 1 of 12 -> CPT own 33.3, 25, 25, 8.3, 8.3; per-lineup mean 300 / 12 = 25.0
    assert f["goalie_cpt_pct"] == 33.3 and f["has_goalie_pct"] == 58.3
    assert f["mean_cpt_own"] == 25.0 and f["cpt_own_le10_pct"] == 16.7
    assert f["cpt_pos"] == {"F": 66.7, "G": 33.3}
    assert f["heavy_team_won_pct"] == 50.0  # 10 leaning lineups, 5 lean to AAA (the W goalie's team)
    assert {x["split"]: x["pct"] for x in f["split_by_team"]} == {"AAA 4-2": 25.0, "BBB 4-2": 25.0, "AAA 5-1": 16.7, "3-3": 16.7,
                                                                  "BBB 5-1": 16.7}
    assert t["goalie_cpt_pct"] == 0.0 and t["mean_cpt_own"] == 33.3 and t["heavy_team_won_pct"] == 100.0
    assert t["split_by_team"] == [{"split": "AAA 5-1", "pct": 100.0}]
    text = "\n".join(syn.render_contest(c))
    assert "CPT own %" in text and "lean team won %" in text


# The committed fixture has no team or position column: a test stub, Hart and Knight as goalies, the W to Hart.
FIXTURE_CHI = {"Anton Frondell", "Frank Nazar", "Jordan Greenway", "Nick Lardis", "Oliver Moore", "Patrick Kane",
               "Roman Kantserov", "Ryan Greene", "Spencer Knight", "Wyatt Kaiser"}
FIXTURE_D = {"Bowen Byram", "Brayden McNabb", "Noah Hanifin", "Rasmus Andersson", "Jeremy Lauzon", "Shea Theodore", "Wyatt Kaiser"}
FIXTURE_G = {"Carter Hart", "Spencer Knight"}


def test_the_captain_report_on_the_committed_fixture_matches_an_independent_count(tmp_path):
    s = st.read(REPO / "tests" / "fixtures" / "standings" / "showdown" / "contest-standings-196048725.csv")
    names = {n for e in s.entries for _, n in e.lineup}
    players = {n: ("CHI" if n in FIXTURE_CHI else "VGK", "G" if n in FIXTURE_G else "D" if n in FIXTURE_D else "F") for n in names}
    c = syn.analyze_contest(s, _day_map(players, "Carter Hart"), META, {}, tmp_path)
    f, t = c["cohorts"]["field"], c["cohorts"]["top1"]
    # counted with csv and a regex over the file, no project code (2026-10-13): 19 entries, 2 blank, 17 lineups
    assert c["entries"] == 19 and f["n"] == 17 and t["n"] == 1
    assert f["goalie_cpt_pct"] == 23.5 and f["has_goalie_pct"] == 94.1  # 4 and 16 of 17
    assert f["mean_cpt_own"] == 35.0 and f["cpt_own_le10_pct"] == 23.5
    assert {x["split"]: x["pct"] for x in f["split_by_team"]} == {"VGK 5-1": 88.2, "VGK 4-2": 5.9, "CHI 5-1": 5.9}
    assert f["heavy_team_won_pct"] == 94.1  # 16 of 17 lean to VGK
    assert t["goalie_cpt_pct"] == 0.0 and t["mean_cpt_own"] == 52.6 and t["split_by_team"] == [{"split": "VGK 5-1", "pct": 100.0}]


def _contest(date: str, top1: tuple, field: tuple) -> dict:
    """top1 / field: (n, goalie_cpt_pct, mean_cpt_own)."""
    mk = lambda n, g, o: {"n": n, "goalie_cpt_pct": g, "mean_cpt_own": o}
    return {"mode": "showdown", "slate_date": date, "cohorts": {"top1": mk(*top1), "field": mk(*field)}}


def test_the_pattern_check_is_the_preregistered_rule():
    held = [_contest("2026-10-05", (12, 0.0, 6.0), (1200, 20.0, 12.0)), _contest("2026-10-06", (10, 10.0, 8.0), (1000, 22.0, 14.0))]
    r = syn.showdown_pattern(held)
    assert r["top1_n"] == 22 and r["top1_goalie_cpt_pct"] == 4.5 and r["verdict"] == "HOLDS"
    assert syn.showdown_pattern(held[:1])["verdict"] == "TOO FEW"  # 12 top-1% lineups < 20
    near = [_contest("2026-10-05", (25, 12.0, 9.0), (2500, 20.0, 12.0))]  # goalie gap 8 < 10
    assert syn.showdown_pattern(near)["verdict"] == "NOT SHOWN"
    with_evidence = held + [_contest("2026-10-02", (4, 0.0, 7.0), (341, 23.0, 15.0))]
    assert syn.showdown_pattern(with_evidence, ("2026-10-02",))["top1_n"] == 22  # the out-of-sample reading drops 10-02
    lines = syn.pattern_lines(with_evidence)
    assert any("without 2026-10-02" in x and "HOLDS" in x for x in lines)
