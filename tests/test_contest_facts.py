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
    assert cf.BOOSTER.pattern == kinds["top_n"].pattern
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
        ("Classic", "NHL $1K Synthetic Booster [Top 5 Win $1K]", "1003", "2026-10-01 19:00:00", "300", "$5.00", "$1,250.00", "5"),
        ("Classic", "NHL $2 Synthetic Qualifier", "1004", "2026-10-01 19:00:00", "30", "$0.25", "$6.00", "3"),
        ("Classic", "NHL $85 Synthetic Keeper", "1005", "2026-10-01 19:00:00", "100", "$1.00", "$85.00", "20")])
    d = _distill(p)
    assert [r.name_key for r in d.rows] == ["NHL $85 Synthetic Keeper"]
    assert d.left_out == {"name carries 'vs'": 1, "name does not start with 'NHL '": 1, "Booster (Top N Win, flag 76)": 1,
                          "satellite": 1}


def test_the_last_contest_is_the_latest_start_then_the_highest_id(tmp_path):
    p = _export(tmp_path, [("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (AAA @ BBB)", "1003", "2026-10-01 19:00:00", "150", "$0.10", "$20.00", "40"),
                           ("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (CCC @ DDD)", "1001", "2026-10-01 22:00:00", "120", "$0.10", "$20.00", "40"),
                           ("Showdown Captain Mode", "NHL Showdown $20 Synthetic Dime (EEE @ FFF)", "1002", "2026-10-01 22:00:00", "204", "$0.10", "$20.00", "40")])
    r = _distill(p).rows[0]
    assert (r.mode, r.contests_seen, r.max_entrants, r.last_entrants) == ("showdown", 3, 204, 204)
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
                     (good + [good[1].replace(",1.00,", ",-1.00,", 1)], "line")]:
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
    rc = cli.main(["results", "--history", str(HISTORY), "--facts", "--facts-out", str(out), "--since", "2026-01-01"])
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
