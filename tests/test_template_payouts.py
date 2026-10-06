"""C16: payout curves from DraftKings template tables, at build time and in settle (B62, B50; flag 14).

A contest whose DraftKings page is unavailable is priced on the cached table of the same template name and max
entries, labeled PAYOUT_SOURCE=TEMPLATE; PRIOR stays for unmatched contests; settle books the matched payouts as
TEMPLATE, below EXACT and above UNKNOWN, with REPORTED still winning. The committed fixtures (tests/fixtures/templates,
tests/fixtures/http) carry the real DraftKings bodies, so the logic runs everywhere; the same matches on this machine's
own data/raw and runs/ are checked when those gitignored folders exist and skip loudly when they do not.
"""

import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import nhl_dfs.data.http as http_mod
from conftest import HTTP, TESTS
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import scenario_cache as sc
from nhl_dfs.build.run import _payout_templates, run_slate
from nhl_dfs.cli import _print_result, main
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.data.sources.dk_public import parse_contest_detail
from nhl_dfs.learn import ledger as lg
from nhl_dfs.learn import standings as st
from nhl_dfs.models import contests as cm
from nhl_dfs.models import payout_templates as pt

pytestmark = pytest.mark.c16

FIX = TESTS / "fixtures" / "templates"
LS = TESTS / "fixtures" / "late_swap" / "classic"
REAL_RAW = TESTS.parent / "data" / "raw"
REAL_RUNS = TESTS.parent / "runs"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
FAM = cm.load_contest_families()

MINI_MAX, MINI_MAX_TABLE = 196228907, 195958176  # the 09-30 contest and the 09-28 table of its template
DAILY, DAILY_TABLE = 196267162, 195958173  # the 10-02 $1K Daily Dollar and the 09-29 table of its template
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}  # the C8 full-run tests' sizes
TIERS = [(1, 1, 100.0), (2, 2, 50.0), (3, 10, 10.0)]  # 10 paid, $230
NAME = "NHL Test Template [20 Entry Max]"


# -- builders ---------------------------------------------------------------------------------------------------------

def lobby_row(cid, name, fee, m, po, *, mec=20, tmpl=None, guaranteed=True):
    r = {"id": cid, "n": name, "a": fee, "m": m, "mec": mec, "po": po, "dg": 1, "gameType": "Classic",
         "sd": "/Date(1790722800000)/"}
    if guaranteed is not None:
        r["attr"] = {"IsGuaranteed": "true" if guaranteed else "false"}
    if tmpl is not None:
        r["tmpl"] = tmpl
    return r


def write_lobby(root: Path, rows, day="2026-10-01", stem="aaaaaaaaaa"):
    d = root / "dk_lobby" / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.json").write_text(json.dumps({"Contests": rows}), encoding="utf-8")


def body(cid, name, m, fee, tiers=TIERS, *, guaranteed=True, resized=False, resizable=False, total=None, ticket=False,
         start="2026-09-28T23:00:00.0000000Z"):
    ps = []
    for a, b, c in tiers:
        desc = {"Cash": f"${c:.2f}"}
        if ticket:
            desc["Ticket"] = "A ticket to something"
        ps.append({"minPosition": a, "maxPosition": b, "tierPayoutDescriptions": desc})
    tot = sum((b - a + 1) * c for a, b, c in tiers) if total is None else total
    d = {"contestKey": str(cid), "name": name, "payoutSummary": ps, "maximumEntries": m, "maximumEntriesPerUser": 20,
         "entryFee": fee, "entries": 7, "draftGroupId": 1, "contestStartTime": start, "isGuaranteed": guaranteed,
         "isResizable": resizable, "wasResized": resized}
    if total is not False:
        d["totalPayouts"] = tot
    return {"contestDetail": d}


def write_table(root: Path, b: dict, day="2026-09-28", stem=None):
    d = root / "dk_contest" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem or b['contestDetail']['contestKey']}.json"
    p.write_text(json.dumps(b), encoding="utf-8")
    return p


def store_for(root: Path, ids, runs=None, **kw):
    return pt.load_store(root, runs, ids, **kw)


def fixture_cache(tmp_path) -> Path:
    """A cache root holding the committed real bodies: the lobby rows and the two template tables."""
    root = tmp_path / "raw"
    (root / "dk_lobby" / "2026-10-02").mkdir(parents=True)
    shutil.copy(FIX / "dk_lobby_rows.json", root / "dk_lobby" / "2026-10-02" / "rows.json")
    (root / "dk_contest" / "2026-09-28").mkdir(parents=True)
    shutil.copy(FIX / "dk_contest_195958176.json", root / "dk_contest" / "2026-09-28" / "a.json")
    shutil.copy(HTTP / "dk_contest_195958173.json", root / "dk_contest" / "2026-09-28" / "b.json")
    return root


def entries_of(*rows):
    return SimpleNamespace(entries=[SimpleNamespace(contest_id=str(c), contest_name=n, fee=f) for c, n, f in rows])


# -- keys, lobby rows, table refusals -----------------------------------------------------------------------------------

def test_template_key_drops_the_game_suffix_and_stray_spaces_but_keeps_late():
    assert pt.template_key("NHL Showdown $150 Quarter Jukebox (CHI @ VGK)", 713) == ("NHL Showdown $150 Quarter Jukebox", 713)
    assert pt.template_key("NHL Showdown $150 Quarter Jukebox", 713) == ("NHL Showdown $150 Quarter Jukebox", 713)
    assert pt.template_key("NHL $1K Daily Dollar [Single Entry] ", 1189) == ("NHL $1K Daily Dollar [Single Entry]", 1189)
    assert pt.template_key("NHL  $100   Dime Time (Late)", 1189) == ("NHL $100 Dime Time (Late)", 1189)
    assert pt.template_key("NHL $100 Dime Time (Late)", 1189) != pt.template_key("NHL $100 Dime Time", 1189)


def test_lobby_rows_take_the_newest_capture_skip_bad_files_and_stop_when_found(tmp_path):
    write_lobby(tmp_path, [lobby_row(1, "Old name", 1, 10, 5.0)], day="2026-10-01", stem="old")
    write_lobby(tmp_path, [lobby_row(1, "New name", 1, 10, 5.0, tmpl=77), lobby_row(2, "Two", 2, 20, 30.0)], day="2026-10-03",
                stem="new")
    bad = tmp_path / "dk_lobby" / "2026-10-04"
    bad.mkdir()
    (bad / "broken.json").write_text("{not json", encoding="utf-8")
    (bad / "other.json").write_text(json.dumps({"Contests": [{"id": "x"}, {"id": 3}]}), encoding="utf-8")
    got = pt.lobby_rows(tmp_path, [1, 2, 9])
    assert got[1].name == "New name" and got[1].template_id == 77 and got[1].guaranteed is True
    assert got[2].fee == Decimal("2") and got[2].max_entries == 20 and 9 not in got
    assert pt.lobby_rows(tmp_path / "nowhere", [1]) == {}
    assert pt.lobby_rows(tmp_path, []) == {}


def test_lobby_row_without_the_guarantee_attribute_says_none(tmp_path):
    write_lobby(tmp_path, [lobby_row(1, "A", 1, 10, 5.0, guaranteed=None)])
    assert pt.lobby_rows(tmp_path, [1])[1].guaranteed is None


@pytest.mark.parametrize("kw,why", [
    ({"resized": True}, "resized"),
    ({"resizable": True}, "resizable"),
    ({"ticket": True}, "ticket"),
    ({"total": 999.0}, "sum"),
    ({"total": False}, "totalPayouts"),
])
def test_a_table_that_cannot_be_a_template_is_refused_with_its_reason(kw, why):
    r = pt.read_table(body(5001, NAME, 100, 1, **kw), "test")
    assert isinstance(r, str) and why in r


def test_a_good_table_carries_its_paid_places_and_a_tiers_hash():
    t = pt.read_table(body(5001, NAME, 100, 1), "test")
    assert (t.contest_id, t.paid, t.max_entries, t.fee, t.total_payouts) == (5001, 10, 100, Decimal("1"), Decimal("230"))
    same = pt.read_table(body(5002, NAME, 100, 1), "other")
    other = pt.read_table(body(5003, NAME, 100, 1, [(1, 1, 80.0), (2, 2, 60.0), (3, 10, 11.25)]), "x")
    assert t.sha256 == same.sha256 != other.sha256


# -- the match: the two real cases, then every guard --------------------------------------------------------------------

def test_the_0930_mini_max_matches_the_0928_table_and_the_1002_daily_dollar_matches_195958173(tmp_path):
    s = store_for(fixture_cache(tmp_path), [MINI_MAX, DAILY])
    m = s.match(MINI_MAX, satellite=False, fee=Decimal("0.5"))
    assert not isinstance(m, str), m
    assert (m.contest_id, m.table.paid, m.lobby.max_entries) == (MINI_MAX_TABLE, 2732, 11890)
    prizes, seats, face, note = ob.template_curve(m)
    assert len(prizes) == 2732 and int(prizes[0]) == 50000 and int(prizes.sum()) == 500000  # $500 first, the $5,000 pool
    assert not seats.any() and face is None and "TEMPLATE" in note and str(MINI_MAX_TABLE) in note
    assert "360567" in m.note  # the same DraftKings template id on both lobby rows
    d = s.match(DAILY, satellite=False, fee=Decimal("1"))
    assert not isinstance(d, str), d
    assert (d.contest_id, d.table.paid) == (DAILY_TABLE, 285) and "295271" in d.note
    assert int(ob.template_curve(d)[0].sum()) == 100000


def test_unmatched_contests_report_why_and_a_satellite_never_matches(tmp_path):
    s = store_for(fixture_cache(tmp_path), [MINI_MAX, 196218438, 196218433, 424242])
    for cid in (196218438, 196218433):  # 891 and 7,134 max entries: no cached table of that name and size
        assert "no cached table" in s.match(cid, satellite=False)
    assert "no lobby capture" in s.match(424242, satellite=False)
    assert "satellite" in s.match(MINI_MAX, satellite=True)


def _one_template(tmp_path, *, row=None, table=None, extra_tables=(), row_kw=None):
    root = tmp_path / "raw"
    write_lobby(root, [row or lobby_row(7001, NAME, 1, 100, 230.0, **(row_kw or {}))])
    write_table(root, table or body(5001, NAME, 100, 1))
    for i, b in enumerate(extra_tables):
        write_table(root, b, stem=f"extra{i}")
    return store_for(root, [7001])


def test_the_guards_come_from_the_contest_being_priced(tmp_path):
    ok = _one_template(tmp_path / "a")
    assert not isinstance(ok.match(7001, satellite=False, fee=Decimal("1")), str)
    assert "fee" in _one_template(tmp_path / "b", row=lobby_row(7001, NAME, 2, 100, 230.0)).match(7001, satellite=False)
    assert "prize pool" in _one_template(tmp_path / "c", row=lobby_row(7001, NAME, 1, 100, 400.0)).match(7001, satellite=False)
    assert "no cached table" in _one_template(tmp_path / "d", row=lobby_row(7001, NAME, 1, 101, 230.0)).match(7001, satellite=False)
    assert "no cached table" in _one_template(tmp_path / "e", row=lobby_row(7001, NAME + " (Late)", 1, 100, 230.0)).match(
        7001, satellite=False)
    assert "entries file fee" in ok.match(7001, satellite=False, fee=Decimal("3"))


def test_a_template_id_that_differs_vetoes_and_one_that_matches_only_adds_a_note(tmp_path):
    root = tmp_path / "raw"
    write_lobby(root, [lobby_row(7001, NAME, 1, 100, 230.0, tmpl=111), lobby_row(5001, NAME, 1, 100, 230.0, tmpl=222)])
    write_table(root, body(5001, NAME, 100, 1))
    assert "template id" in store_for(root, [7001]).match(7001, satellite=False)
    root2 = tmp_path / "raw2"
    write_lobby(root2, [lobby_row(7001, NAME, 1, 100, 230.0, tmpl=111), lobby_row(5001, NAME, 1, 100, 230.0, tmpl=111)])
    write_table(root2, body(5001, NAME, 100, 1))
    m = store_for(root2, [7001]).match(7001, satellite=False)
    assert not isinstance(m, str) and "template id 111" in m.note
    root3 = tmp_path / "raw3"  # the table contest's own row is gone from the captures: name, size, fee and pool decide
    write_lobby(root3, [lobby_row(7001, NAME, 1, 100, 230.0, tmpl=111)])
    write_table(root3, body(5001, NAME, 100, 1))
    assert not isinstance(store_for(root3, [7001]).match(7001, satellite=False), str)


def test_resized_ticket_and_inconsistent_tables_are_never_used(tmp_path):
    for i, kw in enumerate(({"resized": True}, {"resizable": True}, {"ticket": True}, {"total": 5.0})):
        s = _one_template(tmp_path / str(i), table=body(5001, NAME, 100, 1, **kw))
        assert "no cached table" in s.match(7001, satellite=False)
        assert s.refused and s.refused[0][1]


def test_several_tables_of_one_template_use_the_newest_start_and_say_when_they_disagree(tmp_path):
    older = body(5001, NAME, 100, 1, start="2026-09-28T23:00:00.0000000Z")
    newer_same = body(5002, NAME, 100, 1, start="2026-09-30T23:00:00.0000000Z")
    m = _one_template(tmp_path / "a", table=older, extra_tables=[newer_same]).match(7001, satellite=False)
    assert m.contest_id == 5002 and "disagree" not in m.note
    newer_diff = body(5003, NAME, 100, 1, [(1, 1, 80.0), (2, 2, 60.0), (3, 10, 11.25)], start="2026-10-01T23:00:00.0000000Z")
    m2 = _one_template(tmp_path / "b", table=older, extra_tables=[newer_same, newer_diff]).match(7001, satellite=False)
    assert m2.contest_id == 5003 and "disagree" in m2.note


def test_tables_come_from_run_copies_and_browser_saved_files_too_and_dedupe_by_contest_id(tmp_path):
    root, runs, saved = tmp_path / "raw", tmp_path / "runs", tmp_path / "inbox"
    write_lobby(root, [lobby_row(7001, NAME, 1, 100, 230.0)])
    b = body(5001, NAME, 100, 1)
    (runs / "r1" / "settle" / "prize_tables").mkdir(parents=True)
    (runs / "r1" / "settle" / "prize_tables" / "dk_contest_5001.json").write_text(json.dumps(b), encoding="utf-8")
    (runs / "r2" / "contests").mkdir(parents=True)
    (runs / "r2" / "contests" / "dk_contest_5001.json").write_text(json.dumps(b), encoding="utf-8")
    saved.mkdir()
    (saved / "dk_contest_5001.json").write_text(json.dumps(b), encoding="utf-8")
    s = pt.load_store(root, runs, [7001], extra_dirs=[saved])
    assert len(s.tables[pt.template_key(NAME, 100)]) == 1  # one contest id, one table
    assert not isinstance(s.match(7001, satellite=False), str)
    assert pt.load_store(root, None, [7001]).tables == {}


# -- the same matches on this machine's own files (skip loudly when the gitignored folders are absent) ----------------------

def _real_tables(cid: int) -> bool:
    return (any(REAL_RAW.glob("dk_contest/*/*.json")) and
            (any(cid_ in p.read_text(encoding="utf-8-sig", errors="ignore")[:6000] for p in REAL_RAW.glob("dk_contest/*/*.json")
                 for cid_ in (f'"contestKey":"{cid}"', f'"contestKey": "{cid}"'))
             or any(REAL_RUNS.glob(f"*/settle/prize_tables/dk_contest_{cid}.json"))))


def test_real_mini_max_on_this_machines_files():
    if not (REAL_RAW / "dk_lobby").is_dir() or not _real_tables(MINI_MAX_TABLE):
        pytest.skip("REAL DATA MISSING: data/raw/dk_lobby and the cached 09-28 table 195958176 (gitignored); the "
                    "committed-fixture test covers the same match")
    m = pt.load_store(REAL_RAW, REAL_RUNS, [MINI_MAX]).match(MINI_MAX, satellite=False, fee=Decimal("0.5"))
    assert not isinstance(m, str) and (m.contest_id, m.table.paid) == (MINI_MAX_TABLE, 2732)


def test_real_daily_dollar_on_this_machines_files():
    if not (REAL_RAW / "dk_lobby").is_dir() or not _real_tables(DAILY_TABLE):
        pytest.skip("REAL DATA MISSING: data/raw/dk_lobby and a saved table of contest 195958173 (gitignored); the "
                    "committed-fixture test covers the same match")
    m = pt.load_store(REAL_RAW, REAL_RUNS, [DAILY]).match(DAILY, satellite=False, fee=Decimal("1"))
    assert not isinstance(m, str) and (m.contest_id, m.table.paid) == (DAILY_TABLE, 285)


# -- build: resolve, the curve, the overall label ------------------------------------------------------------------------

def test_resolve_prices_a_matched_contest_on_its_template_and_labels_it(tmp_path):
    s = store_for(fixture_cache(tmp_path), [MINI_MAX, 196218438])
    ef = entries_of((MINI_MAX, "NHL $5K mini-MAX [150 Entry Max]", "$0.50"), (196218438, "NHL $750 Daily Dollar [Single Entry] ", "$1"))
    ctx = cm.resolve(ef, None, FAM, s)
    a, b = ctx[str(MINI_MAX)], ctx["196218438"]
    assert (a.payout_source, a.family, a.family_source, a.field_size, a.field_size_source) == (
        PayoutSource.TEMPLATE, "large_gpp", "template", 11890, "lobby")
    assert a.template.contest_id == MINI_MAX_TABLE and a.record()["payout_template"]["paid_places"] == 2732
    assert a.record()["PAYOUT_SOURCE"] == "TEMPLATE"
    assert a.payout_line().startswith(f"PAYOUT_SOURCE=TEMPLATE contest {MINI_MAX} (table of contest {MINI_MAX_TABLE} (2,732 paid")
    # C38 (B72): no cached table keeps the payout PRIOR, but the lobby row's 891 max entries now sizes the contest (it was
    # the 5,000 family placeholder); 891 is over the small-field cut, so the family stays large_gpp
    assert b.payout_source is PayoutSource.PRIOR and b.field_size == 891 and b.field_size_source == "lobby"
    assert b.family == "large_gpp" and b.field_size_label == "LOBBY"
    assert "no cached table" in b.payout_note and b.payout_line().startswith("PAYOUT_SOURCE=PRIOR contest 196218438 (no cached")
    assert cm.overall_payout_source(ctx.values()) is PayoutSource.PRIOR  # the weakest contest
    c = ob.contest_from(a, FAM)
    assert (c.payout_source, c.paid, c.cash_line, c.field_size, int(c.prizes_cents[0])) == (PayoutSource.TEMPLATE, 2732, 2732, 11890, 50000)
    assert "table of contest 195958176" in c.detail and c.record()["PAYOUT_SOURCE"] == "TEMPLATE"


def test_an_unmatched_contest_is_priced_exactly_as_before(tmp_path):
    ef = entries_of((7001, NAME, "$1"))
    base = cm.resolve(ef, None, FAM)["7001"]
    with_store = cm.resolve(ef, None, FAM, pt.TemplateStore())["7001"]
    assert base.payout_source is PayoutSource.PRIOR and with_store.payout_source is PayoutSource.PRIOR
    assert base.payout_note == "" and "no lobby capture" in with_store.payout_note
    pa = ob.contest_from(base, FAM)
    pb = ob.contest_from(with_store, FAM)
    assert np.array_equal(pa.prizes_cents, pb.prizes_cents) and pa.field_size == pb.field_size


def test_a_detail_from_the_endpoint_beats_a_template_and_the_switch_turns_templates_off(tmp_path):
    s = store_for(fixture_cache(tmp_path), [MINI_MAX])
    ef = entries_of((MINI_MAX, "NHL $5K mini-MAX [150 Entry Max]", "$0.50"))
    det = parse_contest_detail(json.loads((FIX / "dk_contest_195958176.json").read_text(encoding="utf-8-sig")))
    got = cm.resolve(ef, {str(MINI_MAX): det}, FAM, s)[str(MINI_MAX)]
    assert got.payout_source is PayoutSource.EXACT and got.template is None
    off = {**FAM, "payout_templates": {"enabled": False}}
    c = cm.resolve(ef, None, off, s)[str(MINI_MAX)]
    assert c.payout_source is PayoutSource.PRIOR and c.payout_note == ""
    assert cm.templates_enabled(FAM) is True and cm.templates_enabled({}) is True and cm.templates_enabled(off) is False
    with pytest.raises(ValueError, match="payout_templates.enabled"):
        cm.validate_contest_families({**FAM, "payout_templates": {"enabled": "yes"}})


def test_a_satellite_name_never_gets_a_template_and_a_non_numeric_id_does_not_crash(tmp_path):
    root = tmp_path / "raw"
    write_lobby(root, [lobby_row(7001, "NHL Satellite to the Hip Check", 1, 100, 230.0)])
    write_table(root, body(5001, "NHL Satellite to the Hip Check", 100, 1))
    ef = entries_of((7001, "NHL Satellite to the Hip Check", "$1"), ("abc", "NHL Odd", "$1"))
    ctx = cm.resolve(ef, None, FAM, store_for(root, [7001]))
    assert ctx["7001"].payout_source is PayoutSource.PRIOR and "satellite" in ctx["7001"].payout_note
    assert ctx["abc"].payout_source is PayoutSource.PRIOR


def test_the_overall_label_is_the_weakest_contest_and_a_template_only_slate_is_template():
    E, T, P = PayoutSource.EXACT, PayoutSource.TEMPLATE, PayoutSource.PRIOR
    c = cm.combine_payout_sources
    assert c([E, E]) is E and c([E, T]) is T and c([T, T]) is T
    assert c([E, P]) is P and c([T, P]) is P and c([P]) is P and c([]) is P


def test_a_template_contest_survives_the_scenario_cache_and_late_swap_uses_the_shared_label():
    import inspect

    from nhl_dfs.build import late_swap

    ct = ob.Contest("7001", "x", "large_gpp", 100, 100, np.array([5000, 2500, 1000], np.int64), np.zeros(3, bool), None,
                    PayoutSource.TEMPLATE, "TEMPLATE from the table of contest 5001")
    back = sc._contest_from_json(sc._contest_json(ct))
    assert back.payout_source is PayoutSource.TEMPLATE and back.detail == ct.detail
    src = inspect.getsource(late_swap)  # the evidence line used to be a hand-written EXACT-or-PRIOR test
    assert "combine_payout_sources" in src and 'payout_source.value == "EXACT"' not in src


# -- the run: manifest, RUN_NOTES, the printed line ---------------------------------------------------------------------

def _full_run(tmp, cache):
    """The baseline, provisional and scenario versions on the late-swap fixture, offline, with the cache given."""
    return run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=False, scenario=True,
                     scenario_n=SMALL, out_root=tmp / "runs", outputs_root=tmp / "outputs", cache=cache, clock=lambda: BEFORE)


@pytest.fixture(scope="module")
def templated(tmp_path_factory):
    """One offline run of the late-swap Classic fixture whose single contest (297000001, $1, 1,189 max) has a lobby row
    and a cached table (the real 09-29 Daily Dollar body renamed to this contest's template)."""
    tmp = tmp_path_factory.mktemp("c16run")
    raw = tmp / "raw"
    real = json.loads((HTTP / "dk_contest_195958173.json").read_text(encoding="utf-8-sig"))
    real["contestDetail"]["name"] = "NHL Synthetic Classic"
    real["contestDetail"]["contestKey"] = "297000009"
    write_table(raw, real)
    write_lobby(raw, [lobby_row(297000001, "NHL Synthetic Classic", 1, 1189, 1000.0, guaranteed=True, tmpl=5),
                      lobby_row(297000009, "NHL Synthetic Classic", 1, 1189, 1000.0, tmpl=5)])
    cache = HttpCache(raw, config=load_sources_config(), offline=True)
    r = _full_run(tmp, cache)
    return SimpleNamespace(tmp=tmp, raw=raw, cache=cache, r=r)


def test_the_run_prices_the_contest_on_the_template_and_says_so_everywhere(templated, capsys):
    r = templated.r
    assert r.ok, r.manifest["failed"]
    prov = {c["contest_id"]: c for c in r.manifest["provisional"]["contests"]}["297000001"]
    assert prov["PAYOUT_SOURCE"] == "TEMPLATE" and prov["field_size"] == 1189 and prov["field_size_source"] == "lobby"
    assert prov["payout_template"]["template_contest_id"] == 297000009 and prov["payout_template"]["paid_places"] == 285
    assert r.manifest["scenario"]["version"] and r.manifest["provisional"]["version"]  # v2 and v3 really ran
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]
    assert scen["PAYOUT_SOURCE"] == "TEMPLATE" and scen["payout_template"]["tiers_sha256"] == prov["payout_template"]["tiers_sha256"]
    assert scen["paid_positions"] == 285 and scen["first_prize"] == 100.0
    assert r.statuses["PAYOUT_SOURCE"] == "TEMPLATE"  # the only contest
    notes = r.notes_path.read_text(encoding="utf-8")
    assert "TEMPLATE (table of contest 297000009, 285 paid)" in notes and "PAYOUT_SOURCE=TEMPLATE" in notes
    assert "uncalibrated scenario proxy" not in notes.split("## Scenario portfolio")[1].split("Frontier")[0]
    _print_result(r)
    out = capsys.readouterr().out
    assert "PAYOUT_SOURCE=TEMPLATE\n" in out and "note: PAYOUT_SOURCE=TEMPLATE contest 297000001 (table of contest 297000009" in out


def test_without_a_lobby_row_the_same_run_stays_prior_and_names_the_reason(tmp_path):
    cache = HttpCache(tmp_path / "raw", config=load_sources_config(), offline=True)
    r = _full_run(tmp_path, cache)
    assert r.ok and r.statuses["PAYOUT_SOURCE"] == "PRIOR"
    assert any(m.startswith("PAYOUT_SOURCE=PRIOR contest 297000001 (no lobby capture") for m in r.messages)
    assert "PRIOR (no lobby capture" in r.notes_path.read_text(encoding="utf-8")


def test_the_store_reads_the_cache_root_at_call_time_never_this_machines_data(tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    write_lobby(root, [lobby_row(7001, NAME, 1, 100, 230.0)])
    write_table(root, body(5001, NAME, 100, 1))
    ef = entries_of((7001, NAME, "$1"))
    msgs: list[str] = []
    assert http_mod.DEFAULT_ROOT != REAL_RAW  # conftest points the default root at an empty folder
    assert _payout_templates(ef, None, tmp_path / "runs", FAM, msgs).lobby == {}
    monkeypatch.setattr(http_mod, "DEFAULT_ROOT", root)  # read when the pass runs, not when the module loads
    assert 7001 in _payout_templates(ef, None, tmp_path / "runs", FAM, msgs).lobby
    assert _payout_templates(ef, None, tmp_path / "runs", {**FAM, "payout_templates": {"enabled": False}}, msgs) is None


# -- settle: REPORTED > EXACT > TEMPLATE > UNKNOWN -----------------------------------------------------------------------

HDR = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\r\n"
OWN = ["7100000001", "7100000002", "7100000003", "7100000004", "7100000005"]


def standings(cid=297000001, entries=1189):
    rows = [(i + 1, e, 50 - i) for i, e in enumerate(OWN)] + [(i + 6, f"9{i:06d}", 40 - i % 30) for i in range(entries - 5)]
    text = "".join(f"{r},{e},u{i},0,{p},,,,,,\r\n" for i, (r, e, p) in enumerate(rows))
    return st.parse(("﻿" + HDR + text).encode("utf-8"), f"contest-standings-{cid}.csv")


def meta(sat=False):
    return {297000001: ("NHL Synthetic Classic", Decimal("1"), sat)}


def table_for(templated, *, entries=1189, sat=False, store=None, **kw):
    s = store or pt.load_store(templated.raw, None, [297000001])
    return lg.prize_tables([297000001], entries_n={297000001: entries}, templates=s, contest_meta=meta(sat),
                           cache_root=templated.tmp / "nowhere", **kw)


def test_settle_books_template_payouts_below_exact_and_above_unknown(templated):
    tabs, notes = table_for(templated)
    t = tabs[297000001]
    assert (t.kind, t.final) == (lg.TEMPLATE, True) and "guaranteed" in t.final_note and t.template["paid_places"] == 285
    assert t.source.startswith("TEMPLATE: table of contest 297000009")
    sl = lg.settle(templated.r.run, [standings()], tabs, now=NOW)
    pays = {e.entry_id: e.payout_cents for e in sl.entries}
    assert pays["7100000001"] == 10000 and all(isinstance(v, int) for v in pays.values())  # rank 1 pays $100.00
    assert all(e.payout_source == "TEMPLATE" for e in sl.entries) and sl.complete
    assert sl.by_contest()["297000001"]["sources"] == ["TEMPLATE"]
    # an EXACT table for the contest itself wins over the template
    exact = templated.tmp / "exact.json"
    exact.write_text(json.dumps(body(297000001, "NHL Synthetic Classic", 1189, 1, [(1, 1, 7.0)])), encoding="utf-8")
    tabs2, _ = table_for(templated, paths=[exact])
    assert tabs2[297000001].kind == lg.EXACT
    sl2 = lg.settle(templated.r.run, [standings()], tabs2, now=NOW)
    assert {e.payout_source for e in sl2.entries} == {"EXACT"} and sl2.entries[0].payout_cents == 700
    # no table at all: UNKNOWN, nothing invented
    sl3 = lg.settle(templated.r.run, [standings()], {}, now=NOW)
    assert {e.payout_source for e in sl3.entries} == {"UNKNOWN"} and not sl3.complete


def test_a_reported_amount_still_wins_and_a_mismatch_with_the_template_is_noted(templated):
    tabs, _ = table_for(templated)
    rep = {("297000001", "7100000001"): (1234, "reported by Ben (DraftKings My Contests)")}
    sl = lg.settle(templated.r.run, [standings()], tabs, reported=rep, now=NOW)
    first = sl.entries[0]
    assert first.payout_source == "REPORTED" and first.payout_cents == 1234 and first.table_cents == 10000
    assert any("reported $12.34 but the table gives $100.00" in n for n in sl.notes)


def test_a_template_table_is_final_only_by_the_contests_own_facts(templated):
    raw = templated.tmp / "raw_unguaranteed"
    write_lobby(raw, [lobby_row(297000001, "NHL Synthetic Classic", 1, 1189, 1000.0, guaranteed=False)])
    write_table(raw, json.loads((templated.raw / "dk_contest" / "2026-09-28" / "297000009.json").read_text(encoding="utf-8")))
    s = pt.load_store(raw, None, [297000001])
    tabs, _ = table_for(templated, entries=900, store=s)  # not guaranteed, not filled: it may have been resized
    t = tabs[297000001]
    assert t.final is False and "may have been resized" in t.final_note
    sl = lg.settle(templated.r.run, [standings(entries=900)], tabs, now=NOW)
    assert {e.payout_source for e in sl.entries} == {"UNKNOWN"} and "may have been resized" in sl.entries[0].source_detail
    tabs2, _ = table_for(templated, entries=1189, store=s)  # filled: final
    assert tabs2[297000001].final and "filled (1189 of 1189)" in tabs2[297000001].final_note


def test_a_satellite_and_a_missing_template_leave_the_contest_unknown_with_the_reason(templated):
    tabs, notes = table_for(templated, sat=True)
    assert tabs == {} and any("satellite" in n for n in notes)
    tabs, notes = lg.prize_tables([297000001], entries_n={297000001: 1189}, templates=pt.TemplateStore(), contest_meta=meta(),
                                  cache_root=templated.tmp / "nowhere")
    assert tabs == {} and any("no lobby capture" in n for n in notes)


def test_a_template_table_is_never_saved_under_the_settled_contests_id(templated):
    tabs, _ = table_for(templated)
    saved = templated.tmp / "saved_tables"
    lg.save_tables(tabs, saved)
    assert not list(saved.glob("dk_contest_*.json"))  # it would read back as this contest's EXACT table


def test_settle_command_books_template_asks_ben_for_the_amount_and_notes_a_changed_table(templated, tmp_path, monkeypatch, capsys):
    run = templated.r.run
    monkeypatch.setattr(http_mod, "DEFAULT_ROOT", templated.raw)
    sdir = tmp_path / "standings"
    sdir.mkdir()
    (sdir / "contest-standings-297000001.csv").write_bytes(
        ("﻿" + HDR + "".join(f"{i + 1},{e},bleeski,0,{50 - i},,,,,,\r\n" for i, e in enumerate(OWN))).encode("utf-8"))
    bl = tmp_path / "BACKLOG.md"
    bl.write_bytes(b"# Backlog\r\n\r\n| ID | a | b | c | d | e | f | g | h | i |\r\n|---|---|---|---|---|---|---|---|---|---|\r\n")
    base = ["settle", run.run_id, str(sdir), "--runs-root", str(templated.tmp / "runs"), "--ledger-root", str(tmp_path / "ledger"),
            "--backlog", str(bl), "--no-boxscores"]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "PAYOUT_SOURCE=TEMPLATE" in out
    g = json.loads((run.path / "settle" / "grades.json").read_text(encoding="utf-8"))
    pt_rec = g["prize_tables"]["297000001"]
    assert pt_rec["kind"] == "TEMPLATE" and pt_rec["template"]["template_contest_id"] == 297000009 and pt_rec["final"]
    assert g["statuses"]["PAYOUT_SOURCE"] == "TEMPLATE"
    assert any("TEMPLATE table of contest 297000009" in n for n in g["notes"])
    assert not any("changed since the run priced it" in n for n in g["notes"])  # same table as at build time
    asked = (tmp_path / "ledger" / "winnings_needed" / f"{run.run_id}.csv").read_text(encoding="utf-8")
    assert all(e in asked for e in OWN)  # Ben is still asked: his amount is the only check that the template holds
    # DraftKings changes the table (same total, other shape): the next settle says so
    tbl = templated.raw / "dk_contest" / "2026-09-28" / "297000009.json"
    old = tbl.read_text(encoding="utf-8")
    d = json.loads(old)
    d["contestDetail"]["payoutSummary"][0]["tierPayoutDescriptions"]["Cash"] = "$90.00"
    d["contestDetail"]["payoutSummary"][1]["tierPayoutDescriptions"]["Cash"] = "$85.00"
    tbl.write_text(json.dumps(d), encoding="utf-8")
    try:
        assert main(base) == 0
        g2 = json.loads((run.path / "settle" / "grades.json").read_text(encoding="utf-8"))
        assert any("changed since the run priced it" in n for n in g2["notes"])
    finally:
        tbl.write_text(old, encoding="utf-8")
