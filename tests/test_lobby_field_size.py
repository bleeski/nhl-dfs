"""C38: field size and family from the lobby row when the contest page is unavailable (B72, [BEN] flag 29).

A contest whose DraftKings page answers 403 and that no template matched used to take the family prior as its field
size (5,000 for a large_gpp, 200 for cash) although the lobby capture on disk holds its real max entries and max per
user. It now takes the lobby row's max entries, labeled FIELD_SIZE_SOURCE=LOBBY, and a contest whose family came only
from the declared default is small_field at or under the exact path's own cut. PAYOUT_SOURCE stays PRIOR. The committed
fixture is three real DraftKings lobby rows (tests/fixtures/templates/dk_lobby_showdown_1003.json); the same facts on
this machine's own data/raw are checked when that gitignored folder exists and skip loudly when it does not.
"""

import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import HTTP, TESTS
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.run import _lobby_rows, run_slate
from nhl_dfs.cli import _print_result
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.data.sources.dk_public import parse_contest_detail
from nhl_dfs.models import contests as cm
from nhl_dfs.models import payout_templates as pt

pytestmark = pytest.mark.c38

FIX = TESTS / "fixtures" / "templates"
LS = TESTS / "fixtures" / "late_swap" / "classic"  # five entries in contest 297000001 ($1, "NHL Synthetic Classic")
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}  # the C8 full-run tests' sizes
REAL_RAW = TESTS.parent / "data" / "raw"
FAM = cm.load_contest_families()
RISK = ob.load_risk_config()
SNAP = "2026-10-04/abcdef0123"

# The 10-03 STL @ COL Showdown contests (run record 2026-10-03, lines 63 to 67) as the entries file names them.
QUARTER, DAILY, TRIPLE = 196302350, 196302351, 196302810
SHOWDOWN = {
    QUARTER: ("NHL Showdown $100 Quarter Jukebox (STL @ COL)", "$0.25"),
    DAILY: ("NHL Showdown $75 Daily Dollar [Single Entry] (STL @ COL)", "$1"),
    TRIPLE: ("NHL Showdown $1 Triple Up [Top 9 Win $3] (STL @ COL)", "$1"),
}
CLASSIC = {  # the 10-03 Classic contests, same source
    196302143: ("NHL $4K Hip Check [20 Entry Max]", "$1", 4756),
    196302144: ("NHL $1K Quarter Jukebox", "$0.25", 4756),
    196302148: ("NHL $500 Daily Dollar [Single Entry]", "$1", 594),
    196302151: ("NHL $5K mini-MAX [150 Entry Max]", "$0.50", 11890),
    196333587: ("NHL $200 Dime Time", "$0.10", 2378),
}


def row(cid, name, fee, m, mec=1, snap=SNAP):
    return pt.LobbyRow(int(cid), name, Decimal(str(fee)), m, mec, Decimal("27"), None, None, None, snap)


def entries_of(*rows):
    """(contest id, name, fee) per entry; repeat a tuple for several entries in one contest."""
    return SimpleNamespace(entries=[SimpleNamespace(contest_id=str(c), contest_name=n, fee=f) for c, n, f in rows])


def one(name, fee, m, *, mec=1, cfg=FAM, cid=7001, own=1, lobby_name=None, lobby_fee=None):
    """resolve one contest of `own` entries against a lobby row of `m` max entries; returns its ContestContext."""
    ef = entries_of(*[(cid, name, fee)] * own)
    r = row(cid, lobby_name or name, lobby_fee if lobby_fee is not None else fee.replace("$", ""), m, mec)
    return cm.resolve(ef, None, cfg, None, {cid: r})[str(cid)]


def real_lobby_cache(tmp_path):
    """A cache root holding the committed real lobby rows, read through the same reader C16 uses."""
    d = tmp_path / "raw" / "dk_lobby" / "2026-10-04"
    d.mkdir(parents=True)
    shutil.copy(FIX / "dk_lobby_showdown_1003.json", d / "rows.json")
    return tmp_path / "raw"


# -- the card's exit case -----------------------------------------------------------------------------------------------

def test_a_27_entry_showdown_contest_is_small_field_with_size_27_and_the_lobby_label():
    name = "NHL Showdown $3 Test Contest (STL @ COL)"  # no family word in the name: the family is the declared default
    assert cm.family_from_name(name, FAM) == ("large_gpp", "default")
    c = one(name, "$3", 27)
    assert (c.family, c.family_source, c.field_size, c.field_size_source) == ("small_field", "lobby", 27, "lobby")
    assert c.field_size_label == "LOBBY" and c.payout_source is PayoutSource.PRIOR  # only size and family moved
    assert c.size_line() == f"FIELD_SIZE_SOURCE=LOBBY contest 7001 (27 max entries, 1 per user; lobby capture {SNAP})"
    rec = c.record()
    assert rec["FIELD_SIZE_SOURCE"] == "LOBBY" and rec["field_size_source"] == "lobby" and rec["PAYOUT_SOURCE"] == "PRIOR"
    assert rec["field_size"] == 27 and rec["field_size_note"].startswith("27 max entries, 1 per user")
    assert c.payout_line() == "PAYOUT_SOURCE=PRIOR contest 7001"  # the payout line is exactly what it was


# -- the three real rows -------------------------------------------------------------------------------------------------

def test_the_three_real_showdown_rows_are_sized_and_routed_by_their_own_numbers(tmp_path):
    rows = pt.lobby_rows(real_lobby_cache(tmp_path), list(SHOWDOWN))  # C16's reader, unchanged
    assert {k: (r.max_entries, r.max_per_user) for k, r in rows.items()} == {QUARTER: (475, 14), DAILY: (89, 1), TRIPLE: (31, 1)}
    ef = entries_of(*[(cid, n, f) for cid, (n, f) in SHOWDOWN.items()])
    got = cm.resolve(ef, None, FAM, None, rows)
    shape = {cid: (c.family, c.family_source, c.field_size, c.field_size_label) for cid, c in got.items()}
    # 475 and 89 were large_gpp at 5,000; the Triple Up was cash at 200. Its name says cash, so only its size moves: the
    # run record's "about 27" was the $27 prize pool, the lobby row says 31 max entries.
    assert shape == {
        str(QUARTER): ("small_field", "lobby", 475, "LOBBY"),
        str(DAILY): ("small_field", "lobby", 89, "LOBBY"),
        str(TRIPLE): ("cash", "name_pattern", 31, "LOBBY"),
    }
    assert {c.payout_source for c in got.values()} == {PayoutSource.PRIOR}
    assert "1 per user; lobby capture 2026-10-04/" in got[str(TRIPLE)].size_line()


def test_a_default_family_contest_over_the_cut_stays_large_gpp_at_its_real_size():
    c = one("NHL $4K Hip Check [20 Entry Max]", "$1", 4756, mec=20)
    assert (c.family, c.family_source, c.field_size, c.field_size_source) == ("large_gpp", "default", 4756, "lobby")
    edge = one("NHL Test At The Cut", "$1", int(FAM["exact_rules"]["small_field_max_entries"]))
    over = one("NHL Test Over The Cut", "$1", int(FAM["exact_rules"]["small_field_max_entries"]) + 1)
    assert (edge.family, over.family) == ("small_field", "large_gpp")  # the exact path's own cut: <= 500


def test_a_family_the_name_gives_is_kept_and_only_the_size_comes_from_the_row():
    for name, fam in [("NHL $5 Double Up", "cash"), ("NHL $3 Winner Take All", "wta"), ("NHL $10 Satellite to the Hip Check", "satellite"),
                      ("NHL Small Field Special", "small_field")]:
        c = one(name, "$1", 4756)
        assert (c.family, c.family_source, c.field_size, c.field_size_label) == (fam, "name_pattern", 4756, "LOBBY"), name
    big = one("NHL $5 Double Up", "$1", 40)  # a name never gets re-cut by the size rule, in either direction
    assert (big.family, big.field_size) == ("cash", 40)


# -- tiny fields and the guards ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("size", [2, 3, 31])
def test_a_tiny_lobby_field_prices_and_samples_without_a_crash(size):
    c = one("NHL Test Head Up", "$5", size)
    assert (c.family, c.field_size, c.field_size_label) == ("small_field", size, "LOBBY")
    k = ob.contest_from(c, FAM)
    assert k.field_size == size and len(k.prizes_cents) >= 1 and int(k.prizes_cents.sum()) > 0  # at least one place is paid
    lineups, keys = [("a",), ("b",), ("a",)], ["A", "B", "A"]
    spec = ob.field_spec(lineups, keys, size - 1, RISK, n_scenarios=4, seed=1, salt="t")  # the scenario pass's opponents
    assert spec.n_opponents == size - 1 and spec.draws.shape == (4, size - 1)
    none = ob.field_spec(lineups, keys, 0, RISK, n_scenarios=4, seed=1, salt="t")  # clamped, never negative
    assert none.n_opponents == 0 and none.draws.shape == (4, 0)


@pytest.mark.parametrize("kw, why", [
    ({"lobby_fee": "2"}, "the entries file fee 1 differs from the lobby fee 2"),
    ({"lobby_name": "NHL Some Other Contest"}, "differs from the lobby name 'NHL Some Other Contest'"),
    ({"m": 0}, "no usable max entries (0)"),
    ({"own": 3, "m": 3}, "holds 3 of this contest's 3 max entries, so the lobby size leaves no opponent"),
    ({"own": 5, "m": 3}, "holds 5 of this contest's 3 max entries"),
])
def test_a_lobby_row_that_disagrees_with_the_entries_file_keeps_the_family_prior_and_says_why(kw, why):
    kw = {"m": 40, **kw}
    c = one("NHL Test Contest", "$1", **kw)
    assert (c.family, c.family_source, c.field_size, c.field_size_source) == ("large_gpp", "default", 5000, "family_prior")
    assert c.field_size_label == "PRIOR" and why in c.size_note and c.size_line().startswith("FIELD_SIZE_SOURCE=PRIOR contest 7001 (")


def test_a_game_suffix_and_stray_spaces_do_not_make_the_names_differ():
    c = one("NHL  Showdown $3 Test  Contest (STL @ COL) ", "$3", 27, lobby_name="NHL Showdown $3 Test Contest (CHI @ VGK)")
    assert (c.field_size, c.field_size_label) == (27, "LOBBY")


# -- an unseen contest, the switches, the other branches -------------------------------------------------------------------

def test_an_unseen_contest_keeps_the_prior_and_says_prior():
    ef = entries_of((7001, "NHL Test Contest", "$1"), (7002, "NHL $5 Double Up", "$5"))
    got = cm.resolve(ef, None, FAM, None, {})
    a, b = got["7001"], got["7002"]
    assert (a.family, a.field_size, a.field_size_source, a.field_size_label) == ("large_gpp", 5000, "family_prior", "PRIOR")
    assert (b.family, b.field_size, b.field_size_source) == ("cash", 200, "family_prior")
    assert a.size_line() == "FIELD_SIZE_SOURCE=PRIOR contest 7001 (no lobby capture lists this contest)"
    assert cm.field_size_lines(got.values())[1].startswith("FIELD_SIZE_SOURCE=PRIOR contest 7002")
    assert cm.resolve(ef, None, FAM)["7001"].size_note == "no lobby capture was read here"  # nothing was passed at all
    assert cm.resolve(entries_of((0, "x", "$1")), None, FAM, None, {})["0"].field_size_source == "family_prior"
    nonnum = cm.resolve(entries_of(("abc", "NHL Test", "$1")), None, FAM, None, {7001: row(7001, "NHL Test", 1, 40)})["abc"]
    assert nonnum.field_size_source == "family_prior" and "not numeric" in nonnum.size_note


def test_the_switch_turns_the_lobby_size_off_and_the_old_output_comes_back_exactly():
    off = {**FAM, "lobby_field_size": {"enabled": False}}
    assert cm.lobby_enabled(FAM) is True and cm.lobby_enabled({}) is True and cm.lobby_enabled(off) is False
    ef = entries_of((7001, "NHL Showdown $3 Test Contest (STL @ COL)", "$3"))
    r = {7001: row(7001, "NHL Showdown $3 Test Contest (STL @ COL)", 3, 27)}
    c = cm.resolve(ef, None, off, None, r)["7001"]
    base = cm.resolve(ef, None, FAM)["7001"]
    keys = ("family", "family_source", "payout_source", "field_size", "field_size_source", "payout_note")
    assert all(getattr(c, k) == getattr(base, k) for k in keys)
    assert (c.family, c.field_size, c.field_size_source) == ("large_gpp", 5000, "family_prior")
    assert "switched off" in c.size_note and cm.resolve(ef, None, FAM, None, r)["7001"].field_size == 27  # and on, it moves
    with pytest.raises(ValueError, match="lobby_field_size.enabled"):
        cm.validate_contest_families({**FAM, "lobby_field_size": {"enabled": "yes"}})
    cm.validate_contest_families({k: v for k, v in FAM.items() if k != "lobby_field_size"})  # the key is optional


def test_a_store_alone_still_sizes_from_its_lobby_rows_with_templates_on_or_off():
    ef = entries_of((7001, "NHL Test Contest", "$1"))
    store = pt.TemplateStore(lobby={7001: row(7001, "NHL Test Contest", 1, 89)})
    for cfg in (FAM, {**FAM, "payout_templates": {"enabled": False}}):  # lobby size is independent of flag 14
        c = cm.resolve(ef, None, cfg, store)["7001"]
        assert (c.family, c.field_size, c.field_size_label, c.payout_source) == ("small_field", 89, "LOBBY", PayoutSource.PRIOR)
    given = cm.resolve(ef, None, FAM, store, {7001: row(7001, "NHL Test Contest", 1, 500)})["7001"]
    assert given.field_size == 500  # an explicit `lobby` wins over the store's rows


def test_the_endpoint_detail_beats_the_lobby_and_a_template_contest_still_says_lobby(tmp_path):
    det = parse_contest_detail(json.loads((FIX / "dk_contest_195958176.json").read_text(encoding="utf-8-sig")))
    mini = 196228907  # the 09-30 mini-MAX, whose template table is the 09-28 contest 195958176
    ef = entries_of((mini, "NHL $5K mini-MAX [150 Entry Max]", "$0.50"))
    bogus = {mini: row(mini, "NHL $5K mini-MAX [150 Entry Max]", "0.50", 40)}
    exact = cm.resolve(ef, {str(mini): det}, FAM, None, bogus)[str(mini)]
    assert exact.payout_source is PayoutSource.EXACT and exact.field_size_label == "EXACT" and exact.field_size == det.maximum_entries
    assert exact.size_line().endswith("(max entries from the contest page)")
    root = tmp_path / "raw"
    (root / "dk_lobby" / "2026-10-02").mkdir(parents=True)
    shutil.copy(FIX / "dk_lobby_rows.json", root / "dk_lobby" / "2026-10-02" / "rows.json")
    (root / "dk_contest" / "2026-09-28").mkdir(parents=True)
    shutil.copy(FIX / "dk_contest_195958176.json", root / "dk_contest" / "2026-09-28" / "a.json")
    shutil.copy(HTTP / "dk_contest_195958173.json", root / "dk_contest" / "2026-09-28" / "b.json")
    tpl = cm.resolve(ef, None, FAM, pt.load_store(root, None, [mini]))[str(mini)]
    assert (tpl.payout_source, tpl.field_size, tpl.field_size_source, tpl.field_size_label) == (PayoutSource.TEMPLATE, 11890, "lobby", "LOBBY")
    assert tpl.size_line().startswith(f"FIELD_SIZE_SOURCE=LOBBY contest {mini} (11,890 max entries, 150 per user")


# -- the same facts on this machine's own lobby captures ----------------------------------------------------------------------

def test_the_real_10_03_contests_on_this_machine_resolve_as_the_lobby_rows_say():
    ids = [*SHOWDOWN, *CLASSIC]
    rows = pt.lobby_rows(REAL_RAW, ids) if (REAL_RAW / "dk_lobby").is_dir() else {}
    if len(rows) != len(ids):
        pytest.skip(f"REAL DATA MISSING: data/raw/dk_lobby has {len(rows)} of the {len(ids)} 10-03 contests; run on the "
                    "machine that captured them")
    spec = [(cid, n, f) for cid, (n, f) in SHOWDOWN.items()] + [(cid, n, f) for cid, (n, f, _) in CLASSIC.items()]
    got = cm.resolve(entries_of(*spec), None, FAM, None, rows)
    expect = {QUARTER: ("small_field", 475), DAILY: ("small_field", 89), TRIPLE: ("cash", 31),
              **{cid: ("large_gpp", m) for cid, (_, _, m) in CLASSIC.items()}}
    assert {int(k): (c.family, c.field_size) for k, c in got.items()} == expect
    assert {c.field_size_label for c in got.values()} == {"LOBBY"} and {c.payout_source for c in got.values()} == {PayoutSource.PRIOR}


# -- the run: the helper, the manifest, RUN_NOTES, the printed line, the scenario fallback ----------------------------------

def test_the_run_helper_reads_the_local_capture_and_never_fetches(tmp_path):
    ef = entries_of(*[(cid, n, f) for cid, (n, f) in SHOWDOWN.items()])
    msgs = []
    rows = _lobby_rows(ef, SimpleNamespace(root=real_lobby_cache(tmp_path)), None, FAM, msgs)
    assert set(rows) == set(SHOWDOWN) and msgs == []
    store = pt.TemplateStore(lobby={1: row(1, "x", 1, 5)})
    assert _lobby_rows(ef, None, store, FAM, msgs) is store.lobby  # one read of the capture serves both
    assert _lobby_rows(ef, None, None, {**FAM, "lobby_field_size": {"enabled": False}}, msgs) is None
    assert _lobby_rows(ef, SimpleNamespace(root=None), None, FAM, msgs) is None  # unreadable: reported, never raised
    assert len(msgs) == 1 and msgs[0].startswith("lobby rows unavailable (TypeError")
    assert _lobby_rows(ef, SimpleNamespace(root=tmp_path / "nowhere"), None, FAM, []) == {}  # no capture: no rows, no error


def _lobby_json(cid, name, fee, m, po=27.0, mec=1):
    return {"id": cid, "n": name, "a": fee, "m": m, "mec": mec, "po": po, "dg": 1, "gameType": "Classic",
            "sd": "/Date(1790722800000)/"}


def _full_run(tmp, raw_rows):
    """The baseline, provisional and scenario versions on the late-swap fixture, offline, with a cache holding `raw_rows`."""
    raw = tmp / "raw"
    if raw_rows:
        d = raw / "dk_lobby" / "2026-10-14"
        d.mkdir(parents=True)
        (d / "rows.json").write_text(json.dumps({"Contests": raw_rows}), encoding="utf-8")
    raw.mkdir(exist_ok=True)
    cache = HttpCache(raw, config=load_sources_config(), offline=True)
    return run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=False, scenario=True,
                     scenario_n=SMALL, out_root=tmp / "runs", outputs_root=tmp / "outputs", cache=cache, clock=lambda: BEFORE)


@pytest.fixture(scope="module")
def sized(tmp_path_factory):
    """The same offline run twice: with no lobby capture (the old behavior) and with a lobby row of 27 max entries for its
    one contest (297000001, five of our entries, $1)."""
    without = _full_run(tmp_path_factory.mktemp("c38_without"), [])
    tmp = tmp_path_factory.mktemp("c38_with")
    with_row = _full_run(tmp, [_lobby_json(297000001, "NHL Synthetic Classic", 1, 27)])
    return SimpleNamespace(without=without, with_row=with_row, tmp=tmp)


def test_without_a_lobby_capture_the_run_is_what_it_was_and_says_prior(sized):
    r = sized.without
    assert r.ok, r.manifest["failed"]
    prov = {c["contest_id"]: c for c in r.manifest["provisional"]["contests"]}["297000001"]
    assert (prov["family"], prov["field_size"], prov["field_size_source"], prov["FIELD_SIZE_SOURCE"]) == (
        "large_gpp", 5000, "family_prior", "PRIOR")
    assert prov["field_size_note"] == "no lobby capture lists this contest"
    assert "FIELD_SIZE_SOURCE=PRIOR contest 297000001 (no lobby capture lists this contest)" in r.manifest["messages"]


def test_the_run_sizes_the_contest_from_its_lobby_row_and_says_so_everywhere(sized, capsys):
    r = sized.with_row
    assert r.ok, r.manifest["failed"]
    prov = {c["contest_id"]: c for c in r.manifest["provisional"]["contests"]}["297000001"]
    assert (prov["family"], prov["family_source"], prov["field_size"], prov["field_size_source"]) == ("small_field", "lobby", 27, "lobby")
    assert prov["FIELD_SIZE_SOURCE"] == "LOBBY" and prov["PAYOUT_SOURCE"] == "PRIOR"
    assert prov["field_size_note"].startswith("27 max entries, 1 per user; lobby capture 2026-10-14/")
    assert r.manifest["scenario"]["version"] and r.manifest["provisional"]["version"]  # v2 and v3 really ran on a 27 entry field
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]  # where the final portfolio is reported
    assert (scen["family"], scen["family_source"], scen["field_size"], scen["field_size_source"], scen["FIELD_SIZE_SOURCE"]) == (
        "small_field", "lobby", 27, "lobby", "LOBBY")
    assert scen["PAYOUT_SOURCE"] == "PRIOR" and scen["field_size_note"] == prov["field_size_note"]
    assert r.statuses["PAYOUT_SOURCE"] == "PRIOR"  # the payout curve is still the prior: only size and family moved
    notes = r.notes_path.read_text(encoding="utf-8")
    assert notes.count("| 27 (LOBBY) |") == 2  # the provisional table and the scenario table
    assert "FIELD_SIZE_SOURCE=LOBBY contest 297000001 (27 max entries, 1 per user" in notes  # the Messages section
    scen_table = notes.split("## Scenario portfolio")[1].split("Frontier")[0]
    assert "27 (LOBBY)" in scen_table and "small_field (lobby)" in scen_table
    _print_result(r)
    out = capsys.readouterr().out
    assert "note: FIELD_SIZE_SOURCE=LOBBY contest 297000001 (27 max entries, 1 per user; lobby capture 2026-10-14/" in out
    assert "note: PAYOUT_SOURCE=PRIOR contest 297000001" in out


def test_the_scenario_pass_fallback_sizes_from_the_lobby_too(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("forced: the provisional pass did not finish")

    monkeypatch.setattr(run_mod, "_provisional_pass", broken)
    r = _full_run(tmp_path, [_lobby_json(297000001, "NHL Synthetic Classic", 1, 27)])
    assert r.ok, r.manifest["failed"]
    assert any("provisional pass failed" in m for m in r.manifest["messages"])
    assert any("scenario pass rebuilt the fields because the provisional pass did not finish" in m for m in r.manifest["messages"])
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]
    assert (scen["family"], scen["field_size"], scen["FIELD_SIZE_SOURCE"]) == ("small_field", 27, "LOBBY")
