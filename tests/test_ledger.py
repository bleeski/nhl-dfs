"""C11: financial settlement by Entry ID, DK tie rounding, reported winnings, the ledger file and drawdown."""
import json
from datetime import datetime, timezone

import pytest

from conftest import TESTS
from nhl_dfs.build.run import run_slate
from nhl_dfs.data.sources.dk_public import parse_contest_detail
from nhl_dfs.learn import ledger as lg
from nhl_dfs.learn import standings as st

pytestmark = pytest.mark.c11

LS = TESTS / "fixtures" / "late_swap" / "classic"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
HDR = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\r\n"


def detail(tiers, *, max_entries=10, guaranteed=True, cid=297000001):
    return parse_contest_detail({"contestDetail": {
        "contestKey": str(cid), "name": "NHL Synthetic Classic", "maximumEntries": max_entries,
        "maximumEntriesPerUser": 5, "entryFee": 1.0, "entries": 3, "draftGroupId": 1,
        "contestStartTime": "2026-10-15T23:00:00.0000000Z", "isGuaranteed": guaranteed,
        "payoutSummary": [{"minPosition": a, "maxPosition": b, "tierPayoutDescriptions": {"Cash": f"${c:.2f}"}}
                          for a, b, c in tiers]}})


def standings(rows, cid=297000001):
    body = "".join(f"{r},{e},u{i},0,{p},,,,,,\r\n" for i, (r, e, p) in enumerate(rows))
    return st.parse(("﻿" + HDR + body).encode("utf-8"), f"contest-standings-{cid}.csv")


@pytest.fixture
def run(tmp_path):
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: BEFORE)
    return r.run


def test_own_entries_settle_by_entry_id_with_a_tie_rounded_down(run):
    # our 7100000001 and 7100000002 tie with an outsider at rank 2: places 2 to 4 pay $5 + $3 + $2 = $10, / 3 = $3.33
    s = standings([(1, "9", "50"), (2, "7100000001", "40"), (2, "7100000002", "40"), (2, "8", "40"),
                   (5, "7100000003", "30"), (6, "7100000004", "20"), (7, "7100000005", "0")])
    t = lg.PrizeTable(297000001, detail([(1, 1, 20), (2, 2, 5), (3, 3, 3), (4, 4, 2)]), "test table", True, "guaranteed")
    sl = lg.settle(run, [s], {297000001: t}, now=NOW)
    pay = {e.entry_id: e.payout_cents for e in sl.entries}
    assert pay == {"7100000001": 333, "7100000002": 333, "7100000003": 0, "7100000004": 0, "7100000005": 0}
    assert all(e.payout_source == "EXACT" for e in sl.entries) and sl.complete
    tot = sl.totals()
    assert tot["gross_known_cents"] == 666 and tot["fees_cents"] == 500 and tot["net_cents"] == 666 - 500
    assert tot["net_known_cents"] == tot["gross_known_cents"] - tot["fees_known_cents"]


def test_unknown_payout_is_null_and_reported_winnings_are_booked_with_a_cross_check(run, tmp_path):
    s = standings([(1, "7100000001", "40"), (2, "7100000002", "30"), (3, "7100000003", "20"), (4, "7100000004", "10"),
                   (5, "7100000005", "0")])
    sl = lg.settle(run, [s], {}, now=NOW)  # no table: nothing is invented
    assert all(e.payout_cents is None and e.payout_source == "UNKNOWN" for e in sl.entries)
    assert sl.totals()["net_cents"] is None and sl.totals()["fees_unknown_cents"] == 500 and not sl.complete
    w = tmp_path / "winnings.csv"
    w.write_text("contest_id,entry_id,winnings_usd,source,noted_utc\r\n297000001,7100000001,$12.50,DraftKings My Contests,"
                 "2026-10-16T12:00Z\r\n297000001,7100000002,,DraftKings My Contests,\r\n", encoding="utf-8")
    t = lg.PrizeTable(297000001, detail([(1, 1, 10)]), "test table", True, "guaranteed")
    sl = lg.settle(run, [s], {297000001: t}, reported=lg.read_winnings(w), now=NOW)
    e1 = next(e for e in sl.entries if e.entry_id == "7100000001")
    assert (e1.payout_cents, e1.payout_source, e1.table_cents) == (1250, "REPORTED", 1000)
    assert any("reported $12.50 but the table gives $10.00" in n for n in sl.notes)
    e2 = next(e for e in sl.entries if e.entry_id == "7100000002")
    assert e2.payout_source == "EXACT" and e2.payout_cents == 0  # a blank reported amount falls back to the table


def test_a_table_that_is_not_final_is_not_used(run):
    s = standings([(1, "7100000001", "40")])
    tabs, _ = lg.prize_tables([297000001], entries_n={297000001: 3}, paths=[], cache_root=TESTS / "nowhere")
    assert tabs == {}
    p = TESTS / "fixtures" / "http" / "dk_contest_196048725.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["contestDetail"]["isGuaranteed"] = False
    t = lg.PrizeTable(297000001, parse_contest_detail(raw), "x", False, "not guaranteed and not filled")
    sl = lg.settle(run, [s], {297000001: t}, now=NOW)
    assert next(e for e in sl.entries if e.entry_id == "7100000001").payout_source == "UNKNOWN"


def test_prize_tables_are_final_when_guaranteed_or_filled(tmp_path):
    p = TESTS / "fixtures" / "http" / "dk_contest_196048725.json"
    tabs, _ = lg.prize_tables([196048725], entries_n={196048725: 713}, paths=[p], cache_root=tmp_path)
    t = tabs[196048725]
    assert t.final and "filled (713 of 713)" in t.final_note and "prize table file" in t.source
    assert lg.payout_from_table(t, 101, 1)[0] == 50  # the known result: rank 101 of 713 paid $0.50
    assert lg.payout_from_table(t, 171, 2)[0] == 25  # a tie across the last paid place: ($0.50 + $0) / 2


def test_ledger_rows_replace_a_resettle_and_drawdown_updates_across_two_slates(run, tmp_path):
    root = tmp_path / "ledger"
    t = lg.PrizeTable(297000001, detail([(1, 1, 3)]), "test table", True, "guaranteed")
    s_win = standings([(1, "7100000001", "50"), (2, "7100000002", "4"), (3, "7100000003", "3"), (4, "7100000004", "2"),
                       (5, "7100000005", "1")])
    sl1 = lg.settle(run, [s_win], {297000001: t}, now=NOW)
    sl1.slate_date = "2026-10-15"
    lg.append(sl1, root)
    lg.append(sl1, root)  # a re-settle replaces, never duplicates
    assert len(lg.read(root)) == 5
    s_lose = standings([(1, "9", "50"), (2, "7100000001", "4"), (3, "7100000002", "3"), (4, "7100000003", "2"),
                        (5, "7100000004", "1"), (6, "7100000005", "0")])
    sl2 = lg.settle(run, [s_lose], {297000001: t}, now=NOW)
    sl2.run_id, sl2.slate_date = "second-run", "2026-10-16"
    lg.append(sl2, root)
    dd = lg.drawdown(root)
    assert [r["net_known_cents"] for r in dd["series"]] == [300 - 500, -500]
    assert dd["cum_net_known_cents"] == -700 and dd["peak_cents"] == 0 and dd["max_drawdown_cents"] == 700
    assert dd["complete"] is True


def test_the_winnings_template_keeps_bens_lines_and_appends_only_missing_entries(tmp_path):
    p = tmp_path / "winnings.csv"
    assert lg.write_winnings_template(p, [{"contest_id": "1", "entry_id": "2", "source": "DraftKings My Contests"}]) == 1
    typed = p.read_text(encoding="utf-8").replace("1,,2,,,,DraftKings", "1,,2,,,3.50,DraftKings")
    assert "3.50" in typed
    p.write_text(typed, encoding="utf-8", newline="")
    assert lg.write_winnings_template(p, [{"contest_id": "1", "entry_id": "2"}]) == 0  # already listed
    assert lg.write_winnings_template(p, [{"contest_id": "7", "entry_id": "8"}]) == 1  # another run's unknown entry
    text = p.read_text(encoding="utf-8")
    assert text.startswith(typed) and text.count("contest_id") == 1
    got = lg.read_winnings(p)
    assert got[("1", "2")][0] == 350 and got[("7", "8")][0] is None
