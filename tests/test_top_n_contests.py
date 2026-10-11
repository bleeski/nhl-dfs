"""C47 (B108, flags 64 and 68 to 72): a "Top N Win $X" contest (DraftKings' Boosters) pays N flat prizes of $X, and Ben's
goal there is at least one of his entries in the top N. The name is read into the table (PAYOUT_SOURCE=PRIOR with a note,
never EXACT, never a template table), the field size comes from a usable lobby row or is derived from the name with the
placeholder rake, and the contest is family top_n: the own-goalie rule covers it and its field is the large_gpp sample.
The contest of 2026-10-10 (196555565, $0.25, 237 entries, top 5 win $10 each) is the case. No network anywhere."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from nhl_dfs.build import objectives as ob
from nhl_dfs.build import own_goalie as og
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.sources.dk_public import ContestDetail, PayoutTier
from nhl_dfs.learn import entry_history
from nhl_dfs.models import contests
from nhl_dfs.models import field as field_mod
from nhl_dfs.models import ownership
from nhl_dfs.models import payout_templates as pt
from nhl_dfs.models.payout_templates import LobbyRow

pytestmark = pytest.mark.c47

CFG = contests.load_contest_families()
NAME = "NHL 40x Micro Booster [Top 5 Win $10] (Early)"
CID = "196555565"


def rows(name=NAME, fee="$0.25", n=7, cid=CID):
    return [SimpleNamespace(entry_id=str(5289269888 + i), contest_id=cid, contest_name=name, fee=fee) for i in range(n)]


def ctx_of(name=NAME, fee="$0.25", n=7, **kw):
    return contests.resolve(rows(name, fee, n), cfg=CFG, **kw)[CID]


def _booster(entries: int = 7):
    return ob.contest_from(ctx_of(n=entries), CFG)


def lobby_row(max_entries=237, pool="50", fee="0.25", name=NAME):
    return LobbyRow(int(CID), name, Decimal(fee), max_entries, 40, Decimal(pool), True, None, None, "capture-x")


def detail(name, tiers, m, fee="0.25"):
    return ContestDetail(int(CID), name, [PayoutTier(a, b, Decimal(c), None) for a, b, c in tiers], m, 40, Decimal(fee),
                         7, 1, None, {})


# -- the table --------------------------------------------------------------------------------------------------------

def test_top_n_win_name_pays_n_flat_prizes_in_a_small_field():
    """B108's former strict xfail: the name's five $10 prizes, not a 5,000-entry large_gpp paying 1,000 places."""
    c = _booster()
    assert c.prizes_cents.tolist() == [1000] * 5
    assert c.family != "large_gpp"
    assert 7 < c.field_size < 500


@pytest.mark.parametrize("name, paid, prize", [
    ("Top 10 Win $5", 10, "5"), ("NHL [Top 3 Win $25]", 3, "25"), ("Top 5 Win $1.50", 5, "1.50"),
    ("NHL Big Booster [Top 20 Win $1,000]", 20, "1000"), ("TOP 5 WIN $10", 5, "10"), ("top 7 win $2.5 (Late)", 7, "2.50"),
    ("Top 5 Win $10.", 5, "10"), (NAME, 5, "10")])
def test_name_variants_read_into_n_flat_prizes(name, paid, prize):
    tn = contests.top_n_from_name(name)
    assert (tn.paid, tn.prize) == (paid, Decimal(prize))
    assert contests.top_n_of(name, CFG) == tn
    assert contests.family_from_name(name, CFG) == ("top_n", "name_pattern")


@pytest.mark.parametrize("name", ["NHL Top 5 Win", "NHL Win $10", "Top 0 Win $10", "Top 5 Win $1,00", "Top 5 Winners $10",
                                  "Top Five Win $10", "NHL $10 Top 5"])
def test_a_name_without_both_n_and_x_is_not_read(name):
    assert contests.top_n_from_name(name) is None
    assert contests.family_from_name(name, CFG) == contests.family_from_name(name, contests.without_top_n(CFG))


NO_PHRASE = ["NHL $200 Dime Time", "NHL $5 Double Up", "NHL $1 Triple Up [Top 9 Win $3]", "NHL Satellite [5 Seats]",
             "NHL $25 Winner Take All", "NHL Small Field $3", "NHL Single Entry $5", "NHL Synthetic Classic",
             "Top 5 Win $10 Satellite", "NHL Top 5 Win"]


@pytest.mark.parametrize("name", NO_PHRASE)
def test_a_name_without_the_phrase_routes_exactly_as_before(name):
    """Before C47 = the same config without the top_n pattern. Satellite and cash patterns come first, as in C49."""
    new = contests.resolve(rows(name, "$1", 3), cfg=CFG)[CID]
    old = contests.resolve(rows(name, "$1", 3), cfg=contests.without_top_n(CFG))[CID]
    assert new == old and new.top_n is None
    assert ob.contest_from(new, CFG).record() == ob.contest_from(old, CFG).record()


def test_a_triple_up_named_top_9_win_stays_cash_and_a_satellite_stays_a_satellite():
    assert contests.family_from_name("NHL $1 Triple Up [Top 9 Win $3]", CFG) == ("cash", "name_pattern")
    assert contests.family_from_name("Top 5 Win $10 Satellite", CFG) == ("satellite", "name_pattern")


def test_c49_and_the_pricing_agree_on_which_names_are_boosters():
    """C49's results report calls `\\btop\\s+\\d+\\s+win\\b` top_n (after satellite and cash); every such name that writes
    $X is a Booster here too, and a name C49 does not call top_n is not one here."""
    for name in [NAME, "Top 10 Win $5", "NHL [Top 3 Win $25]", "Top 20 Win $1,000 (Late)", "NHL $1 Triple Up [Top 9 Win $3]",
                 "NHL Satellite [Top 5 Win $10]", "NHL $200 Dime Time", "NHL Top 5 Win"]:
        kind = entry_history.kind(name)
        here = contests.top_n_of(name, CFG) is not None
        assert here == (kind == "top_n" and contests.top_n_from_name(name) is not None), name


# -- the field size ---------------------------------------------------------------------------------------------------

def test_the_derived_size_is_235_and_labeled_with_the_placeholder_rake():
    c = ctx_of()
    assert (c.family, c.family_source, c.payout_source) == ("top_n", "name_pattern", PayoutSource.PRIOR)
    assert (c.field_size, c.field_size_source, c.field_size_label) == (235, "name_derived", "PRIOR")
    assert "floor(5 x $10.00 / ($0.25 x (1 - 0.15))) = 235" in c.size_note and "placeholder" in c.size_note
    assert c.size_line().startswith("FIELD_SIZE_SOURCE=PRIOR contest 196555565 (derived from the contest name")
    assert "table from the contest name: 5 paid x $10.00" in c.payout_line() and "never EXACT" in c.payout_line()
    assert c.record()["top_n"] == {"paid": 5, "prize": 10.0, "text": "Top 5 Win $10", "source": "contest name"}
    assert c.record()["FIELD_SIZE_SOURCE"] == "PRIOR" and c.record()["PAYOUT_SOURCE"] == "PRIOR"


def test_the_derived_size_is_exact_decimal_arithmetic():
    """5 x $10 / ($0.50 x 0.80) = 125 exactly: a float could floor it to 124."""
    cfg = {**CFG, "payout_priors": {**CFG["payout_priors"], "rake": 0.2}}
    assert contests.resolve(rows(fee="$0.50"), cfg=cfg)[CID].field_size == 125


def test_a_usable_lobby_row_size_wins_over_the_derived_size():
    c = ctx_of(lobby={int(CID): lobby_row()})
    assert (c.field_size, c.field_size_label) == (237, "LOBBY")
    assert "lobby prize pool agrees" in c.size_note and "capture-x" in c.size_note
    assert ob.contest_from(c, CFG).prizes_cents.tolist() == [1000] * 5
    off = ctx_of(lobby={int(CID): lobby_row(pool="60")})
    assert off.field_size == 237 and "differs from 5 paid x $10.00" in off.size_note
    bad = ctx_of(lobby={int(CID): lobby_row(fee="1")})  # the row disagrees with the entries file: derived instead
    assert (bad.field_size, bad.field_size_source) == (235, "name_derived") and "differs from the lobby fee" in bad.size_note


@pytest.mark.parametrize("fee, n", [("$0", 7), ("", 7), ("$25", 7), ("$0.25", 300)])
def test_a_booster_that_cannot_be_sized_routes_as_before_and_says_why(fee, n):
    c = ctx_of(fee=fee, n=n)
    old = contests.resolve(rows(fee=fee, n=n), cfg=contests.without_top_n(CFG))[CID]
    assert (c.family, c.field_size, c.payout_source) == (old.family, old.field_size, old.payout_source)
    assert c.top_n is None and "routed as before C47" in c.payout_note
    assert ob.contest_from(c, CFG).field_size == c.field_size  # never a KeyError on any outcome


def test_contest_from_works_on_every_way_a_booster_resolves():
    for c in (ctx_of(), ctx_of(lobby={int(CID): lobby_row()}), ctx_of(fee="$0")):
        ct = ob.contest_from(c, CFG)
        assert ct.paid >= 1 and ct.field_size == c.field_size
    assert ob.prior_curve("top_n", 235, 25, CFG)[0].sum() > 0  # the payout_priors guard


# -- never EXACT, never a template ------------------------------------------------------------------------------------

def _template_store(tmp_path: Path):
    """A lobby row and a cached table of the same template, fee and pool: C16 would price the Booster on it."""
    root = tmp_path / "raw"
    (root / "dk_lobby" / "2026-10-10").mkdir(parents=True)
    row = {"id": int(CID), "n": NAME, "a": 0.25, "m": 237, "mec": 40, "po": 50.0, "dg": 1, "gameType": "Classic",
           "sd": "/Date(1790722800000)/", "attr": {"IsGuaranteed": "true"}}
    (root / "dk_lobby" / "2026-10-10" / "a.json").write_text(json.dumps({"Contests": [row]}), encoding="utf-8")
    (root / "dk_contest" / "2026-10-03").mkdir(parents=True)
    body = {"contestDetail": {"contestKey": "196000001", "name": NAME, "maximumEntries": 237, "maximumEntriesPerUser": 40,
                              "entryFee": 0.25, "entries": 237, "draftGroupId": 1, "contestStartTime": "2026-10-03T23:00:00Z",
                              "isGuaranteed": True, "isResizable": False, "wasResized": False, "totalPayouts": 50.0,
                              "payoutSummary": [{"minPosition": 1, "maxPosition": 5,
                                                 "tierPayoutDescriptions": {"Cash": "$10.00"}}]}}
    (root / "dk_contest" / "2026-10-03" / "b.json").write_text(json.dumps(body), encoding="utf-8")
    return pt.load_store(root, None, [int(CID)])


def test_a_booster_is_never_priced_on_a_template_table(tmp_path):
    store = _template_store(tmp_path)
    assert not isinstance(store.match(int(CID), satellite=False, fee=Decimal("0.25")), str)  # C16 alone would take it
    c = contests.resolve(rows(), cfg=CFG, templates=store)[CID]
    assert (c.family, c.payout_source, c.template) == ("top_n", PayoutSource.PRIOR, None)
    assert (c.field_size, c.field_size_label) == (237, "LOBBY")  # the store's own lobby row sizes it
    ct = ob.contest_from(c, CFG)
    assert ct.payout_source is PayoutSource.PRIOR and ct.detail.startswith("NAME: 5 paid x $10.00")


def test_a_booster_page_is_exact_and_top_n_and_a_flat_table_without_the_phrase_does_not_move():
    page = detail(NAME, [(1, 5, "10")], 237)
    c = contests.resolve(rows(), {CID: page}, cfg=CFG)[CID]
    assert (c.family, c.payout_source, c.field_size_label) == ("top_n", PayoutSource.EXACT, "EXACT")
    assert ob.contest_from(c, CFG).detail.startswith("EXACT")
    plain = detail("NHL $1 Flat Nine", [(1, 9, "3")], 31, fee="1")  # 9 of 31 flat, 0.29 < the 0.30 cash cut
    assert contests.family_from_detail(plain, CFG) == contests.family_from_detail(plain, contests.without_top_n(CFG)) == "small_field"
    tiered = detail(NAME, [(1, 1, "20"), (2, 4, "10")], 237)  # a Booster name whose page is not flat keeps the old rule
    assert contests.family_from_detail(tiered, CFG) == "small_field"


# -- family, objective, own goalie, field -----------------------------------------------------------------------------

def test_the_family_is_configured_validated_and_filled_last():
    assert "top_n" in contests.FAMILIES and CFG["selection"]["family_order"][-1] == "top_n"
    assert ob.FAMILY_OBJECTIVE["top_n"] == "p_top_n"
    contests.validate_contest_families(CFG)


def test_the_own_goalie_rule_covers_boosters_in_classic_only():
    """Changed expectation (named in the log): tests/test_goalie_factor.py pinned three families; top_n joins them."""
    risk = ob.load_risk_config()
    assert "top_n" in og.rule_families(risk, Mode.CLASSIC) and og.rule_families(risk, Mode.SHOWDOWN) == frozenset()
    assert "top_n" in og.DEFAULT_FAMILIES


def test_a_booster_field_is_the_large_gpp_sample_and_never_the_retired_mixture():
    own_cfg = ownership.load_ownership_config()
    assert contests.field_family("top_n") == "large_gpp" and contests.field_family("small_field") == "small_field"
    assert field_mod.classic_mixture_name("top_n", own_cfg) == "old"  # why the alias exists
    assert field_mod.classic_mixture_name(contests.field_family("top_n"), own_cfg) != "old"
