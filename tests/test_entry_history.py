"""C49 (B110): DraftKings' contest entry history reader and the results report against a random entry. Synthetic export
only (tests/fixtures/entry_history/history_synthetic.csv: invented ids, names and amounts, 32 rows); no network, no real data."""

from __future__ import annotations

import csv
import io
import json
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
ENTERED = FIX / "entered"  # one minimized entered record: two of the 10-03 entries
ENTRIES_HEADER = ["Entry ID", "Contest Name", "Contest ID", "Entry Fee", "C", "C", "W", "W", "W", "D", "D", "G", "UTIL"]


@pytest.fixture(autouse=True)
def _scratch_ledger(tmp_path, monkeypatch):
    """The gate line reads <ledger root>/graded.json; never the real data/ledger in a test."""
    monkeypatch.setenv("NHL_DFS_LEDGER_ROOT", str(tmp_path / "default_ledger"))


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
    assert eh.parse_money("$1,000") == 100000
    for bad in ("", " ", "$1.234", "1,00.00", "$ 5", "abc", "1.000,00", "-$1.50", "$-1.00"):
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
    assert every.net_cents == 120930 and every.fees_cents == 2150  # the NFL row's $500 win would show here


def test_unsettled_and_free_rows_are_counted_and_left_out():
    hf = eh.read_history(HISTORY)
    assert hf.unsettled_rows == 1 and hf.unsettled_dates == (date(2026, 10, 5),) and len(hf.rows) == 29
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
    assert "- format=showdown: finish" not in text  # no interval line for a THIN slice


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
    assert results.parse_by("kind+format,era") == [("format", "kind"), ("era",)]  # one cross, one label, one seed


# -- the command ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for k in ("NHL_DFS_RUNS_ROOT", "NHL_DFS_OUTPUTS_ROOT", "NHL_DFS_LEDGER_ROOT", "NHL_DFS_BACKLOG"):
        monkeypatch.delenv(k, raising=False)


def _roots(tmp_path: Path) -> list[str]:
    return ["--entered", str(ENTERED), "--runs-root", str(tmp_path / "runs"), "--outputs-root", str(tmp_path / "outputs"),
            "--ledger-root", str(tmp_path / "ledger")]


def test_the_command_prints_and_writes_the_report(tmp_path, capsys, clean_env):
    rc = cli.main(["results", "--history", str(HISTORY), "--since", "2026-09-29", "--by", "format"] + _roots(tmp_path))
    printed = capsys.readouterr().out
    assert rc == 0
    assert "## By format" in printed and "| classic | 9 | 7 (7) |" in printed
    assert "2 of 13 engine-era entries in the window join" in printed
    lines = printed.splitlines()
    assert lines[-2].startswith("Ownership gate (C12 field_fit): nothing settled on this machine")
    assert lines[-1].startswith("report: ")
    written = list((tmp_path / "outputs" / "results").glob("*.md"))
    assert len(written) == 1 and written[0].read_text(encoding="utf-8") in printed


def test_the_command_refuses_a_wrong_file_and_says_how_to_pass_one(tmp_path, capsys, clean_env, monkeypatch):
    table = _table()
    table[0][2] = "Entry_Id"
    rc = cli.main(["results", "--history", str(_write(tmp_path, table))] + _roots(tmp_path))
    assert rc == 1 and "RESULTS=REFUSED" in capsys.readouterr().out and not (tmp_path / "outputs").exists()
    monkeypatch.setattr(results, "HISTORY_DIR", tmp_path / "none")
    assert cli.main(["results"] + _roots(tmp_path)) == 2
    assert "results needs --history" in capsys.readouterr().out


# -- the recorded label -----------------------------------------------------------------------------------------------------

def _entries_file(path: Path, ids: list[str]) -> None:
    cells = [f"Synthetic Player {i} ({90000100 + i})" for i in range(9)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\r\n").writerows(
            [ENTRIES_HEADER] + [[i, "NHL $6K Synthetic Hip Check [20 Entry Max]", "900000120", "$1"] + cells for i in ids])


def test_recorded_joins_entered_records_and_local_runs_by_entry_id(tmp_path):
    runs, outs = tmp_path / "runs", tmp_path / "outputs"
    run = runs / "20261003-180000-classic" / "inputs"
    _entries_file(run / "DKEntries.csv", ["9000000022"])
    _entries_file(run / "DKSalaries.csv", ["9000000016"])  # never read, whatever it holds
    _entries_file(runs / "_scratch" / "inputs" / "DKEntries.csv", ["9000000017"])  # scratch runs are skipped
    _entries_file(outs / "classic-20261004-0000000000" / "DKEntries.csv", ["9000000023"])
    rec = eh.recorded_ids(ENTERED, runs, outs)
    assert rec.ids == {"9000000020", "9000000021", "9000000022", "9000000023"}
    assert len(rec.files) == 3 and not rec.unreadable
    text, built = results.build(HISTORY, since=date(2026, 9, 29), by="recorded,era", records=rec,
                                ledger_root=tmp_path / "ledger")
    assert (_slice(built, "recorded", "recorded=recorded").n, _slice(built, "recorded", "recorded=unrecorded").n) == (4, 9)
    assert "4 of 13 engine-era entries in the window join" in text and "RECORDED_JOIN=FAILED" not in text
    sep29 = [r for r in eh.read_history(HISTORY).rows if r.entry_key == "9000000016"][0]
    assert eh.era(sep29) == "engine" and sep29.entry_key not in rec.ids  # the era never comes from the join


def test_a_join_that_matches_nothing_is_reported_as_failed_not_as_unrecorded(tmp_path):
    _entries_file(tmp_path / "entered" / "classic-20261003-1111111111.csv", ["9999999999"])
    rec = eh.recorded_ids(tmp_path / "entered", None, None)
    text, built = results.build(HISTORY, by="recorded", records=rec, ledger_root=tmp_path / "ledger")
    assert "RECORDED_JOIN=FAILED" in text
    assert [s.label for s in built[0][1]] == ["all", "recorded=join failed"]
    _, none = results.build(HISTORY, by="recorded", records=eh.recorded_ids(tmp_path / "nothing", None, None),
                            ledger_root=tmp_path / "ledger")
    assert [s.label for s in none[0][1]] == ["all", "recorded=no records"]


# -- in the money -----------------------------------------------------------------------------------------------------------

def test_in_the_money_and_its_expectation_including_places_paid_above_the_field():
    assert [_entry(100, p, paid=20).in_money for p in (20, 21)] == [True, False]
    assert _entry(100, paid=20).exp_in_money == Fraction(20, 100)
    over = _entry(9, paid=12)
    assert over.exp_in_money == 1 and over.exp_in_money_card == Fraction(12, 9)
    _, built = results.build(HISTORY, by="format")
    sd = _slice(built, "format", "format=showdown")
    assert (sd.in_money, sd.exp_in_money) == (3, 1)  # places 5, 80, 1, 90 and 1 of 100 with 20 paid


def test_the_definition_checks_print_both_forms(tmp_path):
    table = _table()
    table[2][13] = "12"  # the 9-entry Double Up paying 12 places
    table[10][8] = "$0.00"  # an entry in the money with no winnings
    text, _ = results.build(_write(tmp_path, table), by="format", ledger_root=tmp_path / "ledger")
    line = next(ln for ln in text.splitlines() if ln.startswith("- all: rake ROI"))
    assert "(used), entry mean" in line and "cash only" in line and "P / N" in line
    assert "1 entries with places paid above entrants" in line
    assert "1 entries where place <= places paid disagrees with winnings above $0" in line


# -- the own-entry spread ---------------------------------------------------------------------------------------------------

def test_the_own_entry_spread_against_random_draws():
    hf = eh.read_history(HISTORY)
    rows = results.select(hf, None)
    [hip] = results.spread(rows)  # the only contest with 3 or more own entries
    assert (hip.contest_key, hip.entrants, hip.own) == ("900000120", 1000, 3)
    assert hip.sd == pytest.approx(0.000816, abs=1e-6) and hip.share_at_or_below == 0.0  # places 1, 2, 3: clustered
    assert results.spread(rows) == [hip]  # seeded by the contest key
    wide = [_entry(10000, p) for p in range(250, 10001, 500)]  # 20 entries spread over the field
    [ref] = results.spread(wide)
    assert ref.own == 20 and ref.random_median_sd == pytest.approx(0.280, abs=0.004)
    assert results.spread(wide[:2]) == []


# -- the gate line ----------------------------------------------------------------------------------------------------------

def _graded(root: Path, entry: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "graded.json").write_text(json.dumps({entry.get("run_id", "r"): entry}), encoding="utf-8")


def test_the_gate_line_reads_the_settled_evidence_and_is_the_last_line(tmp_path):
    led = tmp_path / "ledger"
    assert "nothing settled on this machine" in results.gate_line(led)
    _graded(led, {"run_id": "20261001-220000-classic", "frozen": True, "mode": "classic",
                  "slate_id": "classic-20261001-aaaaaaaaaa", "slate_date": "2026-10-01", "games": ["SYN@FAK"],
                  "contests": {"900000101": {"family": "large_gpp", "labels": 1200}}, "played": [],
                  "complete_payout": False, "settled_utc": "2026-10-02T12:00:00Z"})
    line = results.gate_line(led)
    assert "classic slate_groups 1 of 30, ownership_labels 1,200 of 15,000, holdout_groups not counted yet (B82) of 10" in line
    assert "showdown slate_groups 0 of 50" in line and "(met)" not in line
    text, _ = results.build(HISTORY, by="format", ledger_root=led)
    assert text.rstrip("\n").splitlines()[-1] == line
    _graded(led, {"run_id": "old", "frozen": True})  # an older or damaged index: the reason, never a crash
    assert "unreadable (KeyError" in results.gate_line(led)
    (led / "graded.json").write_text("{not json", encoding="utf-8")
    assert "unreadable (JSONDecodeError" in results.gate_line(led)


# -- refusals the review asked for ------------------------------------------------------------------------------------------

def test_a_row_shifted_by_an_unquoted_comma_is_refused(tmp_path):
    text = HISTORY.read_text(encoding="utf-8").replace('"$10,000.00"', "$10,000.00")
    p = tmp_path / "shifted.csv"
    p.write_text(text, encoding="utf-8", newline="")
    with pytest.raises(ValueError, match="line 9: 15 fields, expected 14"):
        eh.read_history(p)


def test_a_place_with_blank_points_is_refused_not_dropped_and_a_negative_fee_is_refused(tmp_path):
    table = _table()
    table[9][7] = ""  # the 0-point lineup of 09-22, place 100 of 100
    with pytest.raises(ValueError, match="line 10, column Points"):
        eh.read_history(_write(tmp_path, table))
    table = _table()
    table[2][11] = "-$1.00"
    with pytest.raises(ValueError, match="line 3, column Entry_Fee"):
        eh.read_history(_write(tmp_path, table, "neg.csv"))


def test_a_damaged_csv_is_refused_with_the_line(tmp_path):
    p = tmp_path / "huge.csv"  # a field over the csv module's size limit: csv.Error, refused as a ValueError
    p.write_bytes(HISTORY.read_bytes().replace(b"Synthetic Slapshot", b'"' + b"x" * 200_000 + b'"', 1))
    with pytest.raises(ValueError, match="unreadable CSV"):
        eh.read_history(p)


def test_other_date_forms_and_game_types():
    assert eh.parse_eastern("10/01/2026 22:00") == (date(2026, 10, 1), datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc))
    assert eh.parse_eastern("2026-10-01 19:00:00.000")[0] == date(2026, 10, 1)
    assert [eh.format_of(g) for g in ("Classic", "Showdown Captain Mode", "Captain Showdown", "Tiers")] == [
        "classic", "showdown", "showdown", "Tiers"]


def test_the_definition_checks_count_the_kinds_and_flag_a_file_where_every_name_reads_gpp(tmp_path):
    text, _ = results.build(HISTORY, by="format", ledger_root=tmp_path / "ledger")
    assert "kinds cash 1, gpp 23, satellite 1, single_entry_gpp 2, top_n 1." in text
    table = _table()
    for row in table[1:]:
        row[3] = "Synthetic Entry Label"
    flagged, _ = results.build(_write(tmp_path, table), by="format", ledger_root=tmp_path / "ledger")
    assert "KIND_CHECK=ALL_GPP" in flagged
