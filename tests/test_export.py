import hashlib

import pytest
from conftest import mini_pair, real_pair
from lineup_helpers import fill_assignment, first_diff, spans, strip_ending

from nhl_dfs.contracts.geometry import PoolRow
from nhl_dfs.export.writer import field_spans, format_cell, write_entries
from nhl_dfs.intake.entries import canonical_to_template, physical_lines, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c0b


def assert_spliced_faithfully(entries, pool, assignment, out: bytes):
    """Every byte outside roster cells of entry lines is identical; roster cells
    hold exactly f"{name} ({id})" in template order."""
    bom_in, lines_in = physical_lines(entries.raw)
    bom_out, lines_out = physical_lines(out)
    assert bom_in == bom_out, "BOM changed"
    assert len(lines_in) == len(lines_out), f"{len(lines_in)} lines in, {len(lines_out)} out"
    entry_at = {e.line_no: e for e in entries.entries}
    for n, (a, b) in enumerate(zip(lines_in, lines_out)):
        if n not in entry_at:
            assert a == b, f"non-entry line {n} changed ({first_diff(a, b)})"
            continue
        assert a[len(strip_ending(a)) :] == b[len(strip_ending(b)) :], f"line {n} ending changed"
        sa, sb = spans(strip_ending(a)), spans(strip_ending(b))
        assert len(sa) == len(sb), f"line {n}: field count {len(sa)} -> {len(sb)}"
        body_a, body_b = strip_ending(a), strip_ending(b)
        roster = set(entries.roster_cols)
        for col, ((a0, a1), (b0, b1)) in enumerate(zip(sa, sb)):
            if col not in roster:
                assert body_a[a0:a1] == body_b[b0:b1], f"line {n} column {col} changed"
        # separators between fields are single commas in both
        entry = entry_at[n]
        rows = [pool.by_role_id[rid] for rid in assignment[entry.entry_id]]
        expected = canonical_to_template([format_cell(r) for r in rows], entries.roster_labels, pool.mode)
        got = [body_b[b0:b1].decode("utf-8") for col, (b0, b1) in enumerate(sb) if col in roster]
        assert got == list(expected), f"line {n}: roster cells {got} != {list(expected)}"


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_mini_round_trip_is_byte_faithful(mode, tmp_path):
    sal, ent = mini_pair(mode)
    pool, entries = read_salary(sal), read_entries(ent)
    assignment = fill_assignment(entries, pool)
    out = write_entries(entries, assignment, pool, tmp_path / "out.csv")
    assert out == (tmp_path / "out.csv").read_bytes()
    assert_spliced_faithfully(entries, pool, assignment, out)
    report = check_file(tmp_path / "out.csv", sal, ent)
    assert report.ok, report.reasons[:5]


def test_existing_lineups_rewrite_to_the_same_cells(tmp_path):
    sal, ent = mini_pair("classic")
    pool, entries = read_salary(sal), read_entries(ent)
    out = write_entries(entries, fill_assignment(entries, pool), pool, tmp_path / "out.csv")
    # Entry 1 already used "Name (ID)" cells, so its line is byte-identical after the write.
    line_no = entries.entries[0].line_no
    assert physical_lines(out)[1][line_no] == physical_lines(entries.raw)[1][line_no]


def test_bom_and_lf_variant_preserved(tmp_path):
    sal, ent = mini_pair("showdown")
    variant = b"\xef\xbb\xbf" + ent.read_bytes().replace(b"\r\n", b"\n")
    ent_v = tmp_path / "DKEntries.csv"
    ent_v.write_bytes(variant)
    pool, entries = read_salary(sal), read_entries(ent_v)
    assert entries.bom and entries.newline == "\n"
    assignment = fill_assignment(entries, pool)
    out = write_entries(entries, assignment, pool, tmp_path / "out.csv")
    assert out.startswith(b"\xef\xbb\xbf") and b"\r" not in out
    assert_spliced_faithfully(entries, pool, assignment, out)
    assert check_file(tmp_path / "out.csv", sal, ent_v).ok


def test_writer_refuses_incomplete_unknown_excluded_and_wrong_mode(tmp_path):
    sal, ent = mini_pair("classic")
    pool, entries = read_salary(sal), read_entries(ent)
    good = fill_assignment(entries, pool)
    missing = dict(good)
    missing.pop("7000000003")
    with pytest.raises(ValueError, match="missing"):
        write_entries(entries, missing, pool, tmp_path / "o.csv")
    with pytest.raises(ValueError, match="unknown"):
        write_entries(entries, {**good, "9999": good["7000000001"]}, pool, tmp_path / "o.csv")
    excluded_id = sorted(pool.excluded_role_ids)[0]
    bad = dict(good)
    bad["7000000003"] = good["7000000003"][:2] + (excluded_id,) + good["7000000003"][3:]
    with pytest.raises(ValueError, match="not selectable"):
        write_entries(entries, bad, pool, tmp_path / "o.csv")
    with pytest.raises(ValueError, match="expected 9"):
        write_entries(entries, {**good, "7000000003": good["7000000003"][:8]}, pool, tmp_path / "o.csv")
    showdown_pool = read_salary(mini_pair("showdown")[0])
    with pytest.raises(ValueError, match="classic"):
        write_entries(entries, good, showdown_pool, tmp_path / "o.csv")
    assert not (tmp_path / "o.csv").exists()


def test_format_cell_quotes_only_when_needed():
    row = PoolRow("1", "k", "Smith, Jr. \"J\"", "T", "C", frozenset({"C"}), 1, "", None, "MISSING")
    assert format_cell(row) == '"Smith, Jr. ""J"" (1)"'
    plain = PoolRow("2", "k", "Drew O'Connor", "T", "C", frozenset({"C"}), 1, "", None, "MISSING")
    assert format_cell(plain) == "Drew O'Connor (2)"


def test_field_spans_agrees_with_independent_tokenizer():
    for line in (
        b'a,"b, c",,d',
        b'7,"x ""q"", y",1,$1,,,',
        b",,,",
        b"",
        b'"only"',
    ):
        assert field_spans(line) == spans(line), line


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_round_trip_is_byte_faithful(mode, tmp_path):
    sal, ent = real_pair(mode)
    pool, entries = read_salary(sal), read_entries(ent)
    raw_before = ent.read_bytes()
    assignment = fill_assignment(entries, pool)
    out = write_entries(entries, assignment, pool, tmp_path / "out.csv")
    assert_spliced_faithfully(entries, pool, assignment, out)
    assert ent.read_bytes() == raw_before, "the real template itself must never be modified"
    report = check_file(tmp_path / "out.csv", sal, ent)
    assert report.ok, report.reasons[:5]
    assert report.entries_sha256 == hashlib.sha256(raw_before).hexdigest()
