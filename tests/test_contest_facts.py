"""C50 (B111; flags 65 and 73 to 79): recurring contest facts from the entry history size and price contests that no page,
template or lobby row describes. Synthetic exports only (C49's tests/fixtures/entry_history/history_synthetic.csv and small
hand-built ones: invented ids, names and amounts); no network, no real data."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import pytest

from nhl_dfs import cli
from nhl_dfs.learn import entry_history as eh
from nhl_dfs.learn import results
from nhl_dfs.models import contest_facts as cf
from nhl_dfs.models import contests

pytestmark = pytest.mark.c50

FIX = Path(__file__).parent / "fixtures" / "entry_history"
HISTORY = FIX / "history_synthetic.csv"
COMMITTED = Path(__file__).resolve().parents[1] / "data" / "contest_facts" / "nhl.csv"  # the repo's own, never DEFAULT_PATH
ALL_DATES = date(2026, 1, 1)
ENTRY_FIELDS = {"entry", "entry_key", "entry_id", "place", "points", "winnings", "winnings_non_ticket", "winnings_ticket"}


def _export(tmp_path: Path, rows: list[list], name: str = "history.csv") -> Path:
    """An export in the exact header from short rows: (game type, name, contest key, date, entrants, fee, pool, paid[,
    ticket winnings])."""
    p = tmp_path / name
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(eh.HEADER)
        for i, r in enumerate(rows):
            gt, nm, ck, when, n, fee, pool, paid = r[:8]
            ticket = r[8] if len(r) > 8 else "$0.00"
            w.writerow(["NHL", gt, str(8000000000 + i), nm, ck, when, "1", "100.5", "$0.00", ticket, n, fee, pool, paid])
    return p


def _distill(path: Path, since: date | None = ALL_DATES) -> cf.Distilled:
    return cf.distill(eh.read_history(path).rows, since=since)


# -- the key ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, key", [
    ("NHL $200 Dime Time", "NHL $200 Dime Time"),
    ("NHL $20 Dime Time (Late)", "NHL $20 Dime Time"),
    ("NHL $100 Dime Time (Early)", "NHL $100 Dime Time"),
    ("NHL Showdown $20 Dime Time (ANA @ WPG)", "NHL Showdown $20 Dime Time"),
    ("NHL Showdown $1 Triple Up [Top 9 Win $3] (MIN @ FLA)", "NHL Showdown $1 Triple Up [Top 9 Win $3]"),
    ("NHL $5K mini-MAX [150 Entry Max] (2/3)", "NHL $5K mini-MAX [150 Entry Max]"),
    ("NHL $100 Daily Dollar [Single Entry] (Late) (1/2)", "NHL $100 Daily Dollar [Single Entry]"),
    ("NHL Showdown $150 Quarter Jukebox (UTA @ BOS) (Early)", "NHL Showdown $150 Quarter Jukebox"),
    ("NHL  $1K   Hip Check [20 Entry Max] ", "NHL $1K Hip Check [20 Entry Max]"),
    ("NHL $1.25K Quarter Jukebox", "NHL $1.25K Quarter Jukebox"),
    ("NHL Showdown $500 Synthetic Late (TOY @ MOK)", "NHL Showdown $500 Synthetic Late"),  # a bare word Late stays
    ("NHL $20 Dime Time (late)", "NHL $20 Dime Time"),
])
def test_the_key_strips_each_suffix_form_and_keeps_the_rest(name, key):
    assert cf.name_key(name) == key


def test_the_key_keeps_case_and_every_bracketed_tag_that_is_not_a_suffix():
    assert cf.name_key("NHL $4K Hip Check [20 Entry Max]") != cf.name_key("NHL $4K Hip Check")
    assert cf.name_key("nhl $200 dime time") != cf.name_key("NHL $200 Dime Time")
    assert cf.name_key("NHL $20 Dime Time (Late Swap)") == "NHL $20 Dime Time (Late Swap)"


def test_the_name_patterns_agree_with_c49_and_the_config():
    kinds = dict(eh.KIND_RULES)
    assert cf.BOOSTER.pattern == kinds["top_n"].pattern and cf.CASH.pattern == kinds["cash"].pattern
    assert [k for k, _ in eh.KIND_RULES][:3] == ["satellite", "cash", "top_n"]  # is_booster_name follows this order
    for name in ("NHL Showdown $1 Triple Up [Top 9 Win $3] (MIN @ FLA)", "NHL 40x Micro Booster [Top 5 Win $10]",
                 "NHL $1K Synthetic Booster [Top 5 Win $1K]", "NHL $2 Satellite [Top 3 Win $10]", "NHL $200 Dime Time"):
        assert cf.is_booster_name(name) == (eh.kind(name) == "top_n"), name
    assert set(kinds["satellite"].pattern.split("|")) <= set(cf.SATELLITE.pattern.split("|"))
    sat = next(p["pattern"] for p in contests.load_contest_families()["name_patterns"] if p["family"] == "satellite")
    assert sat == cf.SATELLITE.pattern


# -- distilling C49's synthetic export --------------------------------------------------------------------------------------

def test_the_synthetic_export_gives_two_recurring_keys_and_refuses_the_changed_pools():
    d = _distill(HISTORY)
    rows = {(r.mode, r.name_key, r.fee_cents): r for r in d.rows}
    faceoff = rows[("showdown", "NHL Showdown $1K Synthetic Faceoff", 100)]
    assert (faceoff.contests_seen, faceoff.max_entrants, faceoff.last_entrants, faceoff.pool_cents, faceoff.places_paid) == \
        (3, 100, 100, 8500, 20)  # three contests over two game suffixes
    assert (faceoff.first_date, faceoff.last_date) == (date(2026, 9, 28), date(2026, 10, 2))
    single = rows[("classic", "NHL Synthetic Single Entry $500", 100)]
    assert (single.contests_seen, single.max_entrants, single.pool_cents, single.places_paid) == (2, 25, 2200, 5)
    assert {r.name_key for r in d.recurring} == {"NHL Showdown $1K Synthetic Faceoff", "NHL Synthetic Single Entry $500"}
    refused = dict(d.refused)
    assert "prize pool changed" in refused["'NHL $5K Synthetic Slapshot' (classic, $1.00)"]
    assert "prize pool changed" in refused["'NHL $5K Synthetic Slapshot' (classic, $0.25)"]
    # the same name at another fee is its own row and never shares one
    assert rows[("classic", "NHL $5K Synthetic Slapshot", 50)].pool_cents == 12750
    assert ("classic", "NHL $5K Synthetic Slapshot", 100) not in rows
    names = {r.name_key for r in d.rows}
    assert not any("Satellite" in n or "Freeroll" in n or "Booster" in n or n.startswith(("MLB", "NFL")) for n in names)
    assert d.left_out == {"satellite": 1, "freeroll": 1, "Booster (Top N Win, flag 76)": 1}


def test_the_default_window_is_the_engine_era(tmp_path):
    text, out = results.facts(HISTORY, out=tmp_path / "facts.csv")
    t = cf.load(out)
    rec = {(r.name_key, r.fee_cents): r.contests_seen for r in t.rows.values() if r.contests_seen >= 2}
    assert rec == {("NHL Showdown $1K Synthetic Faceoff", 100): 2, ("NHL $5K Synthetic Slapshot", 25): 2}
    assert "from 2026-09-29 (the engine era, the default" in text and "2 recurring (2 or more contests) covering 4" in text
    assert "'NHL $5K Synthetic Slapshot' (classic, $1.00): prize pool changed between contests ($85.00, $200.00)" in text


# -- refusals and filters on hand-built exports ----------------------------------------------------------------------------

def test_a_key_whose_places_paid_changed_is_refused(tmp_path):
    p = _export(tmp_path, [("Classic", "NHL $200 Synthetic Dime", "1001", "2026-10-01 19:00:00", "2000", "$0.10", "$200.00", "540"),
                           ("Classic", "NHL $200 Synthetic Dime", "1002", "2026-10-02 19:00:00", "2100", "$0.10", "$200.00", "545")])
    d = _distill(p)
    assert d.rows == [] and d.refused == [("'NHL $200 Synthetic Dime' (classic, $0.10)",
                                           "places paid changed between contests (540, 545)")]


def test_a_contest_with_a_ticket_prize_refuses_its_key(tmp_path):
    p = _export(tmp_path, [("Classic", "NHL $50 Synthetic Ticket Race", "1001", "2026-10-01 19:00:00", "100", "$1.00", "$85.00", "20"),
                           ("Classic", "NHL $50 Synthetic Ticket Race", "1002", "2026-10-02 19:00:00", "100", "$1.00", "$85.00", "20", "$3.00")])
    assert _distill(p).refused == [("'NHL $50 Synthetic Ticket Race' (classic, $1.00)", "contest 1002 paid a ticket prize")]


def test_a_contest_listing_two_values_refuses_its_key(tmp_path):
    p = _export(tmp_path, [("Classic", "NHL $50 Synthetic Odd", "1001", "2026-10-01 19:00:00", "100", "$1.00", "$85.00", "20"),
                           ("Classic", "NHL $50 Synthetic Odd", "1001", "2026-10-01 19:00:00", "101", "$1.00", "$85.00", "20")])
    assert "contest 1001 lists two values" in _distill(p).refused[0][1]


def test_no_other_persons_name_and_no_booster_is_written(tmp_path):
    p = _export(tmp_path, [
        ("Classic", "NHL $10 vs. somebody", "1001", "2026-10-01 19:00:00", "2", "$5.00", "$9.00", "1"),
        ("Classic", "Ben's Friday League", "1002", "2026-10-01 19:00:00", "8", "$5.00", "$36.00", "3"),
        ("Classic", "NHL Smith Family League", "1007", "2026-10-01 19:00:00", "8", "$5.00", "$36.00", "3"),
        ("Classic", "NHL $50 Synthetic Empty", "1008", "2026-10-01 19:00:00", "8", "$5.00", "$0.00", "3"),
        ("Classic", "NHL $1K Synthetic Booster [Top 5 Win $1K]", "1003", "2026-10-01 19:00:00", "300", "$5.00", "$1,250.00", "5"),
        ("Classic", "NHL $2 Synthetic Qualifier", "1004", "2026-10-01 19:00:00", "30", "$0.25", "$6.00", "3"),
        ("Classic", "NHL $85 Synthetic Keeper", "1005", "2026-10-01 19:00:00", "100", "$1.00", "$85.00", "20"),
        ("Showdown Captain Mode", "NHL Showdown $1 Triple Up [Top 9 Win $3] (MIN @ FLA)", "1006", "2026-10-01 19:00:00", "31",
         "$1.00", "$27.00", "9")])
    d = _distill(p)
    assert [r.name_key for r in d.rows] == ["NHL $85 Synthetic Keeper", "NHL Showdown $1 Triple Up [Top 9 Win $3]"]
    assert d.left_out == {"name carries 'vs'": 1, "name does not start with 'NHL '": 1, "Booster (Top N Win, flag 76)": 1,
                          "satellite": 1, "name carries no dollar amount": 1}
    assert d.refused == [("'NHL $50 Synthetic Empty' (classic, $5.00)", "no prize pool or no places paid ($0.00, 3 paid)")]


def test_the_last_contest_is_the_latest_start_then_the_highest_id(tmp_path):
    p = _export(tmp_path, [("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (AAA @ BBB)", "1003", "2026-10-01 19:00:00", "150", "$0.10", "$20.00", "40"),
                           ("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (CCC @ DDD)", "1001", "2026-10-01 22:00:00", "120", "$0.10", "$20.00", "40"),
                           ("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (EEE @ FFF)", "1002", "2026-10-01 22:00:00", "130", "$0.10", "$20.00", "40")])
    r = _distill(p).rows[0]
    assert (r.mode, r.contests_seen, r.max_entrants, r.last_entrants) == ("showdown", 3, 150, 130)
    assert r.first_date == r.last_date == date(2026, 10, 1)


# -- the table --------------------------------------------------------------------------------------------------------------

def test_the_table_has_exactly_the_fact_columns_and_rewrites_byte_for_byte(tmp_path):
    rows = _distill(HISTORY).rows
    sha1 = cf.write(tmp_path / "a.csv", rows)
    sha2 = cf.write(tmp_path / "b.csv", list(reversed(rows)))
    assert sha1 == sha2 and (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()
    header = (tmp_path / "a.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    assert tuple(header) == cf.COLUMNS and not ENTRY_FIELDS & set(header)
    assert b"\r\n" not in (tmp_path / "a.csv").read_bytes()
    t = cf.load(tmp_path / "a.csv")
    assert sorted(t.rows.values(), key=lambda r: r.key) == sorted(rows, key=lambda r: r.key)


def test_load_refuses_a_bad_header_a_duplicate_key_and_a_bad_value(tmp_path):
    good = cf.render(_distill(HISTORY).rows).decode("utf-8").splitlines()
    for bad, why in [(["mode,name_key,fee"] + good[1:], "header"), (good + [good[1]], "appears twice"),
                     (good + [good[1].replace("classic", "tiers", 1)], "mode"),
                     (good + [good[1].replace(",1.00,", ",-1.00,", 1)], "not a dollar amount")]:
        p = tmp_path / "bad.csv"
        p.write_text("\n".join(bad) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match=why):
            cf.load(p)


def test_the_committed_table_loads_holds_recurring_keys_and_no_entry_fields():
    if not COMMITTED.is_file():
        pytest.skip(f"SKIPPED LOUDLY: {COMMITTED} is not committed yet (it is distilled from Ben's export, flag 65)")
    t = cf.load(COMMITTED)
    header = COMMITTED.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert tuple(header) == cf.COLUMNS and not ENTRY_FIELDS & set(header)
    assert any(r.contests_seen >= 2 for r in t.rows.values())
    for r in t.rows.values():
        assert cf.left_out_reason(r.name_key) is None, r.label()


# -- the command ------------------------------------------------------------------------------------------------------------

def test_the_results_command_writes_the_table_only_where_it_is_told(tmp_path, capsys):
    out = tmp_path / "facts.csv"
    rc = cli.main(["results", "--history", str(HISTORY), "--facts", "--facts-out", str(out), "--since", "2026-01-01",
                   "--outputs-root", str(tmp_path)])
    text = capsys.readouterr().out
    assert rc == 0 and out.is_file() and "CONTEST FACTS (C50" in text and "No entry id, place, points or winnings" in text
    assert "Written: 9 rows (classic 6, showdown 3); 2 recurring (2 or more contests) covering 5 contests." in text
    assert not (tmp_path / "results").exists()  # the report is not written in this mode
    rc = cli.main(["results", "--history", str(HISTORY), "--facts", "--facts-out", str(out), "--by", "format"])
    assert rc == 2 and "--facts takes --history" in capsys.readouterr().out


def test_a_window_with_no_facts_is_refused(tmp_path, capsys):
    rc = cli.main(["results", "--history", str(HISTORY), "--facts", "--facts-out", str(tmp_path / "f.csv"),
                   "--since", "2027-01-01"])
    assert rc == 1 and "RESULTS=REFUSED" in capsys.readouterr().out and not (tmp_path / "f.csv").exists()


# == pricing a contest from a matching row (the second commit) =============================================================

import json  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402

from conftest import TESTS  # noqa: E402
from nhl_dfs.build import objectives as ob  # noqa: E402
from nhl_dfs.build import packet  # noqa: E402
from nhl_dfs.build.run import run_slate  # noqa: E402
from nhl_dfs.cli import _print_result  # noqa: E402
from nhl_dfs.contracts.statuses import PayoutSource  # noqa: E402
from nhl_dfs.data.http import HttpCache, load_sources_config  # noqa: E402
from nhl_dfs.models import payout_templates as pt  # noqa: E402
from nhl_dfs.models.payout_templates import LobbyRow  # noqa: E402

CFG = contests.load_contest_families()
CID = "7001"
D1, D9 = date(2026, 10, 1), date(2026, 10, 9)
DIME = cf.FactsRow("classic", "NHL $20 Synthetic Dime", 10, 204, 180, 2000, 45, 6, D1, D9)
BIG = cf.FactsRow("classic", "NHL $200 Synthetic Dime", 10, 2378, 2100, 20000, 545, 6, D1, D9)
TRIPLE = cf.FactsRow("showdown", "NHL Showdown $1 Synthetic Triple Up", 100, 31, 31, 2700, 9, 3, D1, D9)
WTA = cf.FactsRow("classic", "NHL $10 Synthetic Winner Take All", 100, 12, 12, 1000, 3, 2, D1, D9)
SAT = cf.FactsRow("classic", "NHL $2 Synthetic Satellite", 25, 30, 30, 600, 3, 2, D1, D9)
BOOST = cf.FactsRow("classic", "NHL 40x Micro Booster [Top 5 Win $10]", 25, 237, 237, 5000, 5, 2, D1, D9)
BOOST_K = cf.FactsRow("classic", "NHL $1K Synthetic Booster [Top 5 Win $1K]", 500, 300, 300, 125000, 5, 2, D1, D9)
ODD = cf.FactsRow("classic", "NHL $40 Synthetic Odd", 100, 40, 40, 3400, 50, 2, D1, D9)  # pays more places than entrants seen
TRIPLE_TOP9 = cf.FactsRow("showdown", "NHL Showdown $1 Triple Up [Top 9 Win $3]", 100, 31, 31, 2700, 9, 2, D1, D9)
TABLE = cf.FactsTable(Path("facts.csv"), "0" * 64,
                      {r.key: r for r in (DIME, BIG, TRIPLE, TRIPLE_TOP9, WTA, SAT, BOOST, BOOST_K, ODD)})


def _rows(name, fee, n=5, cid=CID):
    return [SimpleNamespace(entry_id=str(7100000001 + i), contest_id=cid, contest_name=name, fee=fee) for i in range(n)]


def _ctx(name, fee="$0.10", n=5, *, mode="classic", facts=TABLE, cfg=CFG, cid=CID, **kw):
    return contests.resolve(_rows(name, fee, n, cid), cfg=cfg, facts=facts, mode=mode, **kw)[cid]


def _same_route(a, b) -> bool:
    """Everything a contest is priced on, and the notes, equal."""
    ca, cb = ob.contest_from(a, CFG), ob.contest_from(b, CFG)
    return a.record() == b.record() and np.array_equal(ca.prizes_cents, cb.prizes_cents) and ca.detail == cb.detail


def test_a_contest_nothing_else_describes_prices_at_the_rows_size_pool_and_paid_places():
    c = _ctx("NHL $20 Synthetic Dime (Late)")
    assert (c.field_size, c.field_size_source, c.field_size_label, c.family, c.family_source) == \
        (204, "history", "HISTORY", "small_field", "history")  # flag 75: 204 is under the 500 cut
    assert c.payout_source is PayoutSource.PRIOR and c.facts == DIME and c.template is None and c.top_n is None
    line = c.size_line()
    assert line.startswith("FIELD_SIZE_SOURCE=HISTORY contest 7001 (contest facts row 'NHL $20 Synthetic Dime' (classic, $0.10): "
                           "204 max entrants seen (last 180), pool $20.00, 45 places paid, 6 contests 2026-10-01 to 2026-10-09")
    assert c.payout_line() == ("PAYOUT_SOURCE=PRIOR contest 7001 (prior small_field curve on the contest facts row's pool "
                               "$20.00 and 45 places paid; row 'NHL $20 Synthetic Dime' (classic, $0.10), 6 contests, last "
                               "2026-10-09)")
    rec = c.record()
    assert rec["FIELD_SIZE_SOURCE"] == "HISTORY" and rec["contest_facts"]["contests_seen"] == 6
    assert rec["contest_facts"]["last_date"] == "2026-10-09" and rec["contest_facts"]["prize_pool"] == 20.0
    ct = ob.contest_from(c, CFG)
    assert (ct.field_size, ct.paid, int(ct.prizes_cents.sum())) == (204, 45, 2000)  # power: the remainder goes to first
    assert int(ct.prizes_cents[-1]) == 20 and ct.payout_source is PayoutSource.PRIOR  # last paid place = 2 x fee
    assert "pool $20.00 (given), 45 of 204 paid (given), shape power" in ct.detail and "6 contests" in ct.detail


def test_a_large_row_is_large_gpp_and_a_cash_row_pays_its_places_flat():
    big = _ctx("NHL $200 Synthetic Dime")
    ct = ob.contest_from(big, CFG)
    assert (big.family, big.field_size, ct.paid, int(ct.prizes_cents.sum())) == ("large_gpp", 2378, 545, 20000)
    tri = _ctx("NHL Showdown $1 Synthetic Triple Up (AAA @ BBB)", "$1", 1, mode="showdown")
    ct = ob.contest_from(tri, CFG)
    assert (tri.family, tri.family_source, tri.field_size) == ("cash", "name_pattern", 31)  # the name's family is kept
    assert ct.paid == 9 and set(ct.prizes_cents.tolist()) == {300}  # B94's case: 9 places of $3 on a $27 pool
    top9 = _ctx("NHL Showdown $1 Triple Up [Top 9 Win $3] (STL @ COL)", "$1", 1, mode="showdown")
    ct = ob.contest_from(top9, CFG)  # a cash name that also says Top 9 Win is cash (C49, flag 71) and uses its row (flag 76)
    assert (top9.family, top9.field_size_label, top9.top_n, ct.paid, set(ct.prizes_cents.tolist())) == \
        ("cash", "HISTORY", None, 9, {300})
    wta = _ctx("NHL $10 Synthetic Winner Take All", "$1", 1)
    ct = ob.contest_from(wta, CFG)
    assert (wta.family, ct.paid, int(ct.prizes_cents[0])) == ("wta", 1, 1000)  # flag 77: a winner-take-all keeps 1 paid


@pytest.mark.parametrize("name, fee, n, mode, why", [
    ("NHL $20 Synthetic Dime", "$0.25", 5, "classic", "has fee $0.10, not the entries file's $0.25"),
    ("NHL $20 Synthetic Dime", "$0.10", 5, "showdown", "no contest facts row for 'NHL $20 Synthetic Dime' (showdown, $0.10)"),
    ("NHL $2 Synthetic Satellite", "$0.25", 1, "classic", "never used for a satellite"),
    ("NHL $1K Synthetic Booster [Top 5 Win $1K]", "$5", 1, "classic", "never used for a Top N Win name (flag 76)"),
    ("NHL $40 Synthetic Odd", "$1", 1, "classic", "pays 50 places, more than its 40 entrants seen"),
    ("NHL $20 Synthetic Dime", "$0.10", 204, "classic", "leave no opponent"),
    ("NHL $20 Synthetic Dime", "10 cents", 5, "classic", "cannot match a contest facts row"),
])
def test_a_row_that_disagrees_leaves_the_contest_on_its_prior_and_says_why(name, fee, n, mode, why):
    c = _ctx(name, fee, n, mode=mode)
    old = _ctx(name, fee, n, mode=mode, facts=None)
    assert c.field_size_label == "PRIOR" and c.facts is None and why in c.size_note, c.size_note
    assert (c.family, c.field_size, c.payout_source) == (old.family, old.field_size, old.payout_source)
    assert c.size_note.startswith(old.size_note + "; ")  # the facts reason is only appended
    assert np.array_equal(ob.contest_from(c, CFG).prizes_cents, ob.contest_from(old, CFG).prizes_cents)


def test_a_booster_keeps_c47s_route_with_or_without_a_row():
    name = "NHL 40x Micro Booster [Top 5 Win $10] (Early)"
    assert _same_route(_ctx(name, "$0.25", 7), _ctx(name, "$0.25", 7, facts=None))
    c = _ctx(name, "$0.25", 7)
    assert (c.family, c.field_size_source, c.facts) == ("top_n", "name_derived", None)
    fb = _ctx(name, "$0.25", 300)  # C47 cannot size it: routed as before C47, and still no row (flag 76)
    assert fb.facts is None and fb.top_n is None and "never used for a Top N Win name (flag 76)" in fb.size_note


def test_a_lobby_row_with_another_pool_or_too_few_max_entries_refuses_the_row():
    name = "NHL $20 Synthetic Dime"
    other_name = LobbyRow(int(CID), "NHL $20 Something Else", Decimal("0.10"), 300, 1, Decimal("27"), None, None, None, "x")
    c = _ctx(name, lobby={int(CID): other_name})  # unusable for size (another name), and its pool is $27
    assert c.field_size_label == "PRIOR" and "the lobby prize pool $27.00 differs" in c.size_note
    small = LobbyRow(int(CID), name, Decimal("0.10"), 4, 1, Decimal("20"), None, None, None, "x")
    c = _ctx(name, lobby={int(CID): small})  # five own entries in a 4-entry lobby row: no LOBBY size, and 4 < 204
    assert c.field_size_label == "PRIOR" and "the lobby's 4 max entries are below the 204 entrants" in c.size_note


def test_the_switch_off_and_no_table_give_the_earlier_result_exactly():
    off = {**CFG, "contest_facts": {"enabled": False}}
    for name in ("NHL $20 Synthetic Dime (Late)", "NHL Unknown Contest"):
        assert _same_route(_ctx(name, cfg=off), _ctx(name, facts=None))
        assert _same_route(_ctx(name, cfg=off), contests.resolve(_rows(name, "$0.10"), cfg=CFG)[CID])
    with pytest.raises(ValueError, match="contest_facts.enabled"):
        contests.validate_contest_families({**CFG, "contest_facts": {"enabled": "yes"}})


def test_a_name_without_a_row_routes_exactly_as_before_but_for_the_note():
    c, old = _ctx("NHL Unknown Contest"), _ctx("NHL Unknown Contest", facts=None)
    assert c.size_note == old.size_note + "; no contest facts row for 'NHL Unknown Contest' (classic, $0.10)"
    assert {k: v for k, v in c.record().items() if k != "field_size_note"} == \
        {k: v for k, v in old.record().items() if k != "field_size_note"}


def test_the_page_and_the_lobby_row_still_win_over_a_row():
    name = "NHL $20 Synthetic Dime"
    det = contests.ContestDetail(int(CID), name, [], 150, 1, Decimal("0.10"), 0, 1, None, {})
    assert _same_route(_ctx(name, details={CID: det}), _ctx(name, details={CID: det}, facts=None))
    assert _ctx(name, details={CID: det}).field_size_label == "EXACT"
    usable = LobbyRow(int(CID), name, Decimal("0.10"), 300, 1, Decimal("20"), None, None, None, "x")
    got = _ctx(name, lobby={int(CID): usable})
    assert got.field_size_label == "LOBBY" and got.field_size == 300 and got.facts is None
    assert _same_route(got, _ctx(name, lobby={int(CID): usable}, facts=None))


def _template_store(tmp_path: Path, name: str):
    """A lobby row and a cached table of the same template, fee and pool (C16 matches it)."""
    root = tmp_path / "raw"
    (root / "dk_lobby" / "2026-10-10").mkdir(parents=True)
    row = {"id": int(CID), "n": name, "a": 0.10, "m": 204, "mec": 20, "po": 20.0, "dg": 1, "gameType": "Classic",
           "sd": "/Date(1790722800000)/", "attr": {"IsGuaranteed": "true"}}
    (root / "dk_lobby" / "2026-10-10" / "a.json").write_text(json.dumps({"Contests": [row]}), encoding="utf-8")
    (root / "dk_contest" / "2026-10-03").mkdir(parents=True)
    body = {"contestDetail": {"contestKey": "196000001", "name": name, "maximumEntries": 204, "maximumEntriesPerUser": 20,
                              "entryFee": 0.10, "entries": 204, "draftGroupId": 1, "contestStartTime": "2026-10-03T23:00:00Z",
                              "isGuaranteed": True, "isResizable": False, "wasResized": False, "totalPayouts": 20.0,
                              "payoutSummary": [{"minPosition": 1, "maxPosition": 1, "tierPayoutDescriptions": {"Cash": "$5.00"}},
                                                {"minPosition": 2, "maxPosition": 51, "tierPayoutDescriptions": {"Cash": "$0.30"}}]}}
    (root / "dk_contest" / "2026-10-03" / "b.json").write_text(json.dumps(body), encoding="utf-8")
    return pt.load_store(root, None, [int(CID)])


def test_a_template_table_still_wins_over_a_row(tmp_path):
    name = "NHL $20 Synthetic Dime"
    store = _template_store(tmp_path, name)
    got = _ctx(name, templates=store)
    assert got.payout_source is PayoutSource.TEMPLATE and got.facts is None and got.field_size_label == "LOBBY"
    assert _same_route(got, _ctx(name, templates=store, facts=None))


def test_prior_curve_without_the_new_arguments_is_unchanged_and_with_them_pays_the_given_places():
    prizes, _, _, detail = ob.prior_curve("large_gpp", 5000, 100, CFG)
    assert detail == "PRIOR large_gpp: rake 0.15, 1000 of 5000 paid, shape power" and int(prizes.sum()) == 425000
    flat, _, _, d = ob.prior_curve("cash", 31, 100, CFG, pool_cents=2701, n_paid=9)
    assert len(flat) == 9 and 2701 - 9 < int(flat.sum()) <= 2701 and "(given)" in d
    fb, _, _, d = ob.prior_curve("small_field", 100, 100, CFG, pool_cents=5000, n_paid=40)  # 40 x $2 > $50: flat fallback
    assert len(fb) == 40 and set(fb.tolist()) == {125} and "flat (the power shape's fallback" in d
    w, _, _, d = ob.prior_curve("wta", 12, 100, CFG, pool_cents=1000, n_paid=3)
    assert w.tolist() == [1000] and "1 of 12 paid (winner-take-all: 1)" in d


# -- the full offline run: HISTORY on the contest line, in the manifest, RUN_NOTES and the QA packet -------------------------

LS = TESTS / "fixtures" / "late_swap" / "classic"  # five entries in contest 297000001
EARLY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # every fixture game is still ahead
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _renamed(src: Path, dst: Path, name: str, fee: str) -> Path:
    with open(src, encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    for r in table[1:]:
        if len(r) > 3 and r[0].strip().isdigit():
            r[1], r[3] = name, fee
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(table)
    return dst


@pytest.fixture(scope="module")
def facts_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c50run")
    table = tmp / "contest_facts" / "nhl.csv"
    cf.write(table, [DIME])
    entries = _renamed(LS / "DKEntries.template.csv", tmp / "DKEntries.csv", "NHL $20 Synthetic Dime (Late)", "$0.10")
    cache = HttpCache(tmp / "raw", config=load_sources_config(), offline=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cf, "DEFAULT_PATH", table)
        r = run_slate(LS / "DKSalaries.csv", entries, offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                      out_root=tmp / "runs", outputs_root=tmp / "outputs", cache=cache, clock=lambda: EARLY)
    return SimpleNamespace(tmp=tmp, r=r)


def test_the_run_sizes_and_prices_the_contest_from_its_row_and_says_so_everywhere(facts_run, capsys):
    r = facts_run.r
    assert r.ok, r.manifest["failed"]
    prov = {c["contest_id"]: c for c in r.manifest["provisional"]["contests"]}["297000001"]
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]
    for c in (prov, scen):
        assert (c["FIELD_SIZE_SOURCE"], c["field_size"], c["family"], c["PAYOUT_SOURCE"]) == ("HISTORY", 204, "small_field", "PRIOR")
        assert c["contest_facts"]["name_key"] == "NHL $20 Synthetic Dime" and c["contest_facts"]["contests_seen"] == 6
    assert (scen["paid_positions"], scen["prize_pool"]) == (45, 20.0)
    line = ("FIELD_SIZE_SOURCE=HISTORY contest 297000001 (contest facts row 'NHL $20 Synthetic Dime' (classic, $0.10): 204 max "
            "entrants seen (last 180), pool $20.00, 45 places paid, 6 contests 2026-10-01 to 2026-10-09")
    assert any(m.startswith(line) for m in r.messages)
    notes = r.notes_path.read_text(encoding="utf-8")
    assert "204 (HISTORY)" in notes and line in notes and "row 'NHL $20 Synthetic Dime' (classic, $0.10), 6 contests, last 2026-10-09" in notes
    _print_result(r)
    assert f"note: {line}" in capsys.readouterr().out


def test_the_qa_packet_names_the_row(facts_run):
    p = packet.build(facts_run.r.run, 1, now=EARLY, runs_root=facts_run.tmp / "runs")
    row = next(c for c in p["portfolio"]["contests"] if c["contest_id"] == "297000001")
    assert row["FIELD_SIZE_SOURCE"] == "HISTORY" and row["PAYOUT_SOURCE"] == "PRIOR" and "45 places paid" in row["field_size_note"]
    assert (row["contest_facts"]["name_key"], row["contest_facts"]["contests_seen"], row["contest_facts"]["last_date"]) == \
        ("NHL $20 Synthetic Dime", 6, "2026-10-09")
