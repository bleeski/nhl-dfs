"""scripts/standings_synthesis.py: shape and cohort metrics from standings, with a stub player map (no network)."""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

import sys  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("standings_synthesis", REPO / "scripts" / "standings_synthesis.py")
syn = importlib.util.module_from_spec(spec)
sys.modules["standings_synthesis"] = syn  # dataclasses resolve string annotations through sys.modules
spec.loader.exec_module(syn)

from nhl_dfs.contracts.ids import normalize_name  # noqa: E402
from nhl_dfs.learn import standings as st  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "standings"


def _stub_map(s: st.Standings, mode: str) -> syn.DayMap:
    """Every name in the fixture gets a team by a deterministic hash and a position from its roster token."""
    teams = ("AAA", "BBB", "CCC", "DDD") if mode == "classic" else ("AAA", "BBB")
    by_norm, by_last = {}, {}
    seen = {}
    for e in s.entries:
        for slot, raw in e.lineup:
            n = normalize_name(raw)
            if n in seen:
                continue
            pos = {"C": "F", "W": "F", "D": "D", "G": "G"}.get(slot, "F")
            team = teams[sum(map(ord, n)) % len(teams)]
            p = syn.Player(len(seen) + 1, raw, team, pos, "AAA@BBB", decision="W" if pos == "G" else None, dressed=True)
            seen[n] = p
            by_norm[n] = [p]
            by_last.setdefault(n.split()[-1], []).append(p)
    opp = {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC"}
    return syn.DayMap("2026-09-29", ["AAA@BBB", "CCC@DDD"], {}, opp, {t: "AAA@BBB" for t in opp}, by_norm, by_last, {})


@pytest.mark.parametrize("mode,cid", [("classic", "195958173"), ("showdown", "196048725")])
def test_analyze_contest_runs_on_fixture(mode, cid, tmp_path):
    s = st.read(FIX / mode / f"contest-standings-{cid}.csv")
    dm = _stub_map(s, mode)
    meta = {"name": "NHL Fixture Contest", "fee": 1.0, "max_entries": len(s.entries), "max_per_user": 1, "pool": 10.0}
    c = syn.analyze_contest(s, dm, meta, {}, tmp_path)
    assert c["mode"] == mode and c["entries"] == len(s.entries)
    assert c["slots_unresolved"] == 0
    # every lineup's listed Points equals the sum of its players' FPTS (Captain at 1.5x)
    assert c["cohorts"]["field"]["points_check_pct"] == 100.0
    assert c["cohorts"]["top1"]["n"] >= 1 and c["cohorts"]["field"]["n"] >= c["cohorts"]["cash"]["n"]
    shapes = c["cohorts"]["field"]["shape_counts"]
    for shape in shapes:
        total = sum(int(x) for x in shape.split("-"))
        assert total == (8 if mode == "classic" else 6) or shape == "?"
    assert c["ours"], "the fixture keeps our entries"
    assert all(o["cohort"] in ("top1%", "top10%", "cash", "no cash") for o in c["ours"])
    text = "\n".join(syn.render_contest(c))
    assert "NHL Fixture Contest" in text and "Our entries" in text
    # other entrants are never named in the rendered report
    others = {e.entry_name.split(" (")[0] for e in s.entries if not e.entry_name.lower().startswith("bleeski")}
    assert not any(o and o in text for o in others)


def test_paid_places_sources():
    templates = {"NHL $1K Daily Dollar [Single Entry]|1189": {"paid": 285, "tiers": [{"min": 1, "max": 285, "cash": 2.0}], "from_contest": 1}}
    meta = {"name": "NHL $1K Daily Dollar [Single Entry] (CHI @ VGK)", "max_entries": 1189}
    assert syn.paid_places(meta, 1100, templates)[:2] == (285, "TEMPLATE (cached table of contest 1, same name and size)")
    n, src, tiers = syn.paid_places({"name": "NHL Showdown $1 Triple Up [Top 3 Win $3] (ANA @ VGK)", "max_entries": 10}, 9, {})
    assert (n, tiers) == (3, None) and src.startswith("NAME")
    n, src, _ = syn.paid_places({"name": "NHL $200 Dime Time", "max_entries": 2378}, 2317, {})
    assert n == round(0.235 * 2378) and src.startswith("PROXY")
    n, src, _ = syn.paid_places(None, 400, {})
    assert n == 94 and src.startswith("PROXY")


def test_payout_tie_rule_pools_tied_places():
    tiers = [{"min": 1, "max": 1, "cash": 10.0}, {"min": 2, "max": 3, "cash": 4.0}, {"min": 4, "max": 10, "cash": 1.0}]
    assert syn.payout_at(tiers, 1, 1) == 10.0
    assert syn.payout_at(tiers, 3, 2) == pytest.approx((4.0 + 1.0) / 2)  # places 3 and 4 pooled and split
    assert syn.payout_at(tiers, 11, 1) == 0.0
    assert syn.payout_at(None, 1, 1) is None


def test_resolver_hyphenated_first_name_and_fpts_tiebreak():
    f = syn.Player(1, "Elias Pettersson", "VAN", "F", "EDM@VAN", goals=1, assists=1, sog=3)  # 8.5 + 5 + 4.5 = 18.0
    d = syn.Player(2, "Elias Pettersson", "VAN", "D", "EDM@VAN", blocks=2, sog=1)  # 2.6 + 1.5 = 4.1
    dm = syn.DayMap("2026-10-01", ["EDM@VAN"], {}, {"EDM": "VAN", "VAN": "EDM"}, {"EDM": "EDM@VAN", "VAN": "EDM@VAN"},
                    {"elias pettersson": [f, d]}, {"pettersson": [f, d]}, {})
    assert dm.resolve("Elias-Nils Pettersson", "D") is d
    assert dm.resolve("Elias Pettersson", "UTIL", hint_fpts=18.0) is f
    assert dm.resolve("Elias Pettersson", "UTIL", hint_fpts=4.1) is d
    assert dm.resolve("Elias Pettersson", "UTIL") is None and dm.unresolved
    assert syn.box_dk_points(f) == pytest.approx(18.0)


def test_box_dk_points_bonuses():
    hat = syn.Player(3, "X Y", "AAA", "F", None, goals=3, assists=0, sog=5, blocks=3)
    # 25.5 + 7.5 + 3.9 + hat trick 3 + 5 SOG 3 + 3 blocks 3 + 3 points 3
    assert syn.box_dk_points(hat) == pytest.approx(25.5 + 7.5 + 3.9 + 12.0)
    g = syn.Player(4, "G Z", "AAA", "G", None, decision="W", saves=36, goals_against=0, toi_s=3600)
    assert syn.box_dk_points(g) == pytest.approx(0.7 * 36 + 6 + 2 + 3)
