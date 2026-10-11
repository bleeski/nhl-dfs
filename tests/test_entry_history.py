"""C49 (B110): DraftKings' contest entry history reader and the results report against a random entry. Synthetic export
only (tests/fixtures/entry_history/history_synthetic.csv: invented ids, names and amounts, 32 rows); no network, no real data."""

from __future__ import annotations

import csv
import io
import random
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from nhl_dfs import cli
from nhl_dfs.learn import entry_history as eh
from nhl_dfs.learn import results

pytestmark = pytest.mark.c49

FIX = Path(__file__).parent / "fixtures" / "entry_history"
HISTORY = FIX / "history_synthetic.csv"


def _table() -> list[list[str]]:
    return list(csv.reader(io.StringIO(HISTORY.read_text(encoding="utf-8"), newline="")))


def _write(tmp_path: Path, table: list[list[str]], name: str = "history.csv") -> Path:
    p = tmp_path / name
    with p.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\r\n").writerows(table)
    return p


def _entry(entrants: int, place: int = 1, paid: int = 1) -> eh.HistoryRow:
    return eh.HistoryRow(line=2, game_type="Classic", format="classic", entry_key="9000009999", contest_name="x",
                         contest_key="900009999", contest_date_et=date(2026, 10, 1),
                         contest_utc=datetime(2026, 10, 1, 23, tzinfo=timezone.utc), place=place, points_x100=100,
                         cash_cents=0, ticket_cents=0, entrants=entrants, fee_cents=100, pool_cents=0, places_paid=paid)


def _by_name(hf: eh.HistoryFile, fragment: str) -> list[eh.HistoryRow]:
    return [r for r in hf.rows if fragment in r.contest_name]


# -- the reader -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    (lambda h: h.__setitem__(2, "Entry_Id"), "column 3 is 'Entry_Id', expected 'Entry_Key'"),
    (lambda h: h.__setitem__(slice(6, 8), ["Points", "Place"]), "column 7 is 'Points', expected 'Place'"),
    (lambda h: h.pop(), "column 14 'Places_Paid' is missing"),
    (lambda h: h.append("Notes"), "extra column 15 'Notes'"),
])
def test_any_other_header_is_refused_with_the_column_named(tmp_path, change, message):
    table = _table()
    change(table[0])
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        eh.read_history(_write(tmp_path, table))


def test_money_parses_commas_and_cents_and_a_blank_is_refused_not_zero(tmp_path):
    assert eh.parse_money("$1,000.00") == 100000
    assert eh.parse_money("$0.25") == 25 and eh.parse_money("$6,000.00") == 600000 and eh.parse_money("1000") == 100000
    assert eh.parse_money("$1,000") == 100000 and eh.parse_money("-$1.50") == -150
    for bad in ("", " ", "$1.234", "1,00.00", "$ 5", "abc", "1.000,00"):
        with pytest.raises(ValueError):
            eh.parse_money(bad)
    hf = eh.read_history(HISTORY)
    big = _by_name(hf, "Big Shot")[0]
    assert (big.cash_cents, big.pool_cents, big.fee_cents) == (100000, 425000, 100)
    wrister = _by_name(hf, "Wrister")[0]
    assert (wrister.place, wrister.entrants, wrister.places_paid, wrister.pool_cents) == (1234, 11890, 2732, 1000000)
    table = _table()
    table[2][8] = ""
    with pytest.raises(ValueError, match="line 3, column Winnings_Non_Ticket"):
        eh.read_history(_write(tmp_path, table))


def test_ticket_winnings_count_in_net():
    sat = _by_name(eh.read_history(HISTORY), "Satellite")[0]
    assert (sat.cash_cents, sat.ticket_cents, sat.fee_cents) == (0, 200, 25)
    assert sat.net_cents == 175


def test_points_keep_two_decimals_and_a_zero_is_a_blank():
    hf = eh.read_history(HISTORY)
    assert _by_name(hf, "Synthetic Faceoff (SYN")[0].points_x100 == 6025
    assert [r.blank for r in hf.rows].count(True) == 1


def test_a_10pm_eastern_contest_keeps_its_eastern_date_and_slate():
    hf = eh.read_history(HISTORY)
    late = _by_name(hf, "Synthetic Late")[0]
    early = _by_name(hf, "Synthetic Dime")[0]
    assert late.contest_date_et == date(2026, 10, 1)
    assert late.contest_utc == datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)  # the UTC date is the next day
    assert eh.slate_key(late) == eh.slate_key(early) == (date(2026, 10, 1), "showdown")
    assert eh.parse_eastern("10/01/2026 10:00:00 PM") == (late.contest_date_et, late.contest_utc)
    with pytest.raises(ValueError):
        eh.parse_eastern("Oct 1 2026 10pm")
    showdown = [r for r in hf.rows if r.format == "showdown"]
    assert len(results.by_slate(showdown)) == 4


def test_other_sports_are_counted_and_never_read_or_reported():
    hf = eh.read_history(HISTORY)
    assert hf.other_sport_rows == 2
    assert all(r.contest_name.startswith("NHL") for r in hf.rows)
    text, built = results.build(HISTORY)
    assert "Blitz" not in text and "Dinger" not in text and "2 rows of other sports counted, not read" in text
    every = built[0][1][0]
    assert every.net_cents == sum(r.net_cents for r in hf.rows if r.paid)


def test_unsettled_and_free_rows_are_counted_and_left_out():
    hf = eh.read_history(HISTORY)
    assert hf.unsettled_rows == 1 and len(hf.rows) == 29
    assert sum(not r.paid for r in hf.rows) == 1
    assert len(results.select(hf, None)) == 28


def test_a_duplicate_entry_key_and_a_place_outside_the_field_are_refused(tmp_path):
    table = _table()
    table[3][2] = table[2][2]
    with pytest.raises(ValueError, match="appears twice"):
        eh.read_history(_write(tmp_path, table))
    table = _table()
    table[2][6] = "10"  # of 9
    with pytest.raises(ValueError, match="column Place: place 10 outside 1 to 9"):
        eh.read_history(_write(tmp_path, table, "b.csv"))


# -- the random baseline ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n, random_pct, top1, top10, bottom10", [
    (9, Fraction(10, 18), 0, 0, 1),
    (25, Fraction(26, 50), 0, 2, 3),
    (237, Fraction(238, 474), 2, 23, 24),
    (30, Fraction(31, 60), 0, 3, 3),
    (70, Fraction(71, 140), 0, 7, 7),
])
def test_the_random_baseline_is_each_entrys_own_expectation(n, random_pct, top1, top10, bottom10):
    e = _entry(n)
    assert e.random_pct == random_pct
    assert (e.exp_top1, e.exp_top10, e.exp_bottom10) == (Fraction(top1, n), Fraction(top10, n), Fraction(bottom10, n))
    # observed indicators sit on the same integer cuts as the expectations
    assert [_entry(n, p).top10 for p in (max(top10, 1), top10 + 1)] == ([True, False] if top10 else [False, False])
    assert [_entry(n, p).bottom10 for p in (n - bottom10, n - bottom10 + 1)] == [False, True]
    assert sum(_entry(n, p).pct for p in range(1, n + 1)) / n == random_pct  # a uniform place averages the baseline


def test_small_fields_are_not_judged_against_a_flat_50_or_10():
    assert float(_entry(9).random_pct) * 100 == pytest.approx(55.56, abs=0.01)
    assert float(_entry(25).exp_bottom10) == pytest.approx(0.12)


# -- labels -------------------------------------------------------------------------------------------------------------------

def test_kind_by_name_first_match_wins():
    assert eh.kind("NHL $5K Synthetic Slapshot") == "gpp"
    assert eh.kind("NHL $1K Synthetic Double Up") == "cash" and eh.kind("NHL $2 Triple Up") == "cash"
    assert eh.kind("NHL Synthetic Single Entry $500") == "single_entry_gpp"
    assert eh.kind("NHL 10x Synthetic Booster [Top 5 Win $10]") == "top_n"
    assert eh.kind("NHL $2 Synthetic Satellite") == "satellite" and eh.kind("NHL Qualifier") == "satellite"
    assert eh.kind("NHL Single Entry Double Up") == "cash"
    assert eh.kind("NHL $3 Satellite Single Entry") == "satellite"
    assert eh.KIND_SOURCE == "NAME_RULE"


def test_field_and_own_entry_buckets():
    assert [eh.field_bucket(n) for n in (9, 25, 26, 100, 101, 1000, 1001, 10000, 11890)] == [
        "up to 25", "up to 25", "26 to 100", "26 to 100", "101 to 1,000", "101 to 1,000", "1,001 to 10,000",
        "1,001 to 10,000", "over 10,000"]
    assert [eh.entries_bucket(k) for k in (1, 2, 5, 6, 20, 21)] == ["1", "2 to 5", "2 to 5", "6 to 20", "6 to 20", "over 20"]


def test_the_era_is_a_dated_constant():
    hf = eh.read_history(HISTORY)
    assert eh.ENGINE_ERA_START == date(2026, 9, 29)
    sep28 = [r for r in hf.rows if r.contest_date_et == date(2026, 9, 28) and r.format == "classic"][0]
    sep29 = [r for r in hf.rows if r.contest_date_et == date(2026, 9, 29) and r.format == "classic"][0]
    assert (eh.era(sep28), eh.era(sep29)) == ("pre-engine", "engine")


# -- THIN, the slate sign and the bootstrap --------------------------------------------------------------------------------

def _slice(built, table: str, label: str) -> results.Slice:
    return next(s for name, sl in built if name == table for s in sl if s.label == label)


def test_a_slate_is_better_when_its_summed_finish_is_below_random():
    assert results.slate_sign([_entry(100, 1)]) == -1
    assert results.slate_sign([_entry(100, 100)]) == 1
    assert results.slate_sign([_entry(9, 5)]) == 0  # 5 / 9 is exactly (9 + 1) / 18


def test_under_20_slates_a_slice_is_thin_with_slate_counts_and_no_interval():
    text, built = results.build(HISTORY, by="format")
    sd = _slice(built, "format", "format=showdown")
    assert sd.slates == 4 and sd.thin and not sd.intervals
    assert (sd.better, sd.worse, sd.even) == (3, 1, 0)
    assert "| showdown | 5 | 4 (4) |" in text and "3 / 1 / 0 THIN |" in text
    assert "- format=showdown:" not in text


def test_at_20_slates_the_interval_is_seeded_and_independent_of_by_and_row_order(tmp_path):
    _, built = results.build(HISTORY, by="format")
    cl = _slice(built, "format", "format=classic")
    assert cl.slates == 21 and not cl.thin and set(cl.intervals) == set(results.STATISTICS)
    point, lo, hi = cl.intervals["finish excess"]
    assert lo < point < hi
    assert results.build(HISTORY, by="format")[1][0][1][1].intervals == cl.intervals
    _, wide = results.build(HISTORY, by="kind,format,month,field+era")
    assert _slice(wide, "format", "format=classic").intervals == cl.intervals
    table = _table()
    body = table[1:]
    random.Random(7).shuffle(body)
    _, shuffled = results.build(_write(tmp_path, [table[0]] + body), by="format")
    assert _slice(shuffled, "format", "format=classic").intervals == cl.intervals


def test_the_since_filter_and_a_cross_of_two_dimensions():
    _, built = results.build(HISTORY, since=date(2026, 9, 29), by="format+kind")
    every = built[0][1][0]
    assert every.n == 13 and every.slates == 10  # Classic 09-29 to 10-05, Showdown 09-29, 10-01, 10-02
    labels = [s.label for s in built[0][1][1:]]
    assert "format + kind=classic / gpp" in labels and "format + kind=showdown / gpp" in labels
    with pytest.raises(ValueError, match="unknown dimension colour"):
        results.parse_by("format,colour")


# -- the command ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for k in ("NHL_DFS_RUNS_ROOT", "NHL_DFS_OUTPUTS_ROOT", "NHL_DFS_LEDGER_ROOT", "NHL_DFS_BACKLOG"):
        monkeypatch.delenv(k, raising=False)


def test_the_command_prints_and_writes_the_report(tmp_path, capsys, clean_env):
    out = tmp_path / "outputs"
    rc = cli.main(["results", "--history", str(HISTORY), "--since", "2026-09-29", "--by", "format", "--outputs-root",
                   str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    assert "## By format" in printed and "| classic | 9 | 7 (7) |" in printed
    written = list((out / "results").glob("*.md"))
    assert len(written) == 1 and written[0].read_text(encoding="utf-8") in printed


def test_the_command_refuses_a_wrong_file_and_says_how_to_pass_one(tmp_path, capsys, clean_env, monkeypatch):
    table = _table()
    table[0][2] = "Entry_Id"
    rc = cli.main(["results", "--history", str(_write(tmp_path, table)), "--outputs-root", str(tmp_path / "o")])
    assert rc == 1 and "RESULTS=REFUSED" in capsys.readouterr().out and not (tmp_path / "o").exists()
    monkeypatch.setattr(results, "HISTORY_DIR", tmp_path / "none")
    assert cli.main(["results", "--outputs-root", str(tmp_path / "o")]) == 2
    assert "results needs --history" in capsys.readouterr().out
