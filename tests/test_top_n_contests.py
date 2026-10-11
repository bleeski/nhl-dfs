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
                                  "Top Five Win $10", "NHL $10 Top 5", "NHL [Top 5 Win $10K]", "Top 3 Win $1M"])
def test_a_name_without_both_n_and_x_is_not_read(name):
    assert contests.top_n_from_name(name) is None
    assert contests.family_from_name(name, CFG) == contests.family_from_name(name, contests.without_top_n(CFG))


NO_PHRASE = ["NHL $200 Dime Time", "NHL $5 Double Up", "NHL $1 Triple Up [Top 9 Win $3]", "NHL Satellite [5 Seats]",
             "NHL Showdown $1 Triple Up [Top 9 Win $3] (STL @ COL)",  # a real lobby name (dk_lobby_showdown_1003.json)
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
    """1 x $10 / ($0.10 x 0.80) = 125 exactly; in floats the same division is 124.99999999999997, which floors to 124."""
    assert int(1 * 10.0 / (0.10 * (1 - 0.2))) == 124  # the float trap this guards against
    cfg = {**CFG, "payout_priors": {**CFG["payout_priors"], "rake": 0.2}}
    assert contests.resolve(rows("NHL Mini [Top 1 Win $10]", fee="$0.10"), cfg=cfg)[CID].field_size == 125


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


def test_a_lobby_row_no_larger_than_the_prize_count_never_sizes_a_booster():
    c = ctx_of(lobby={int(CID): lobby_row(max_entries=5)}, n=3)
    assert c.field_size_source == "name_derived" and "do not exceed the name's 5 prizes" in c.size_note


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


def test_a_booster_that_falls_back_still_never_takes_a_template_table(tmp_path):
    """300 own entries: the store's lobby row (237 max) leaves no opponent and the derived 235 no room, so the contest
    routes as before C47, except that the template table the store would match is still refused (the card)."""
    store = _template_store(tmp_path)
    c = contests.resolve(rows(n=300), cfg=CFG, templates=store)[CID]
    assert (c.payout_source, c.template, c.top_n) == (PayoutSource.PRIOR, None, None)
    assert "routed as before C47" in c.payout_note
    old = contests.resolve(rows(n=300), cfg=contests.without_top_n(CFG), templates=store)[CID]
    assert old.payout_source is PayoutSource.TEMPLATE  # what the old routing would have done


def test_a_booster_page_is_exact_and_top_n_and_a_flat_table_without_the_phrase_does_not_move():
    page = detail(NAME, [(1, 5, "10")], 237)
    c = contests.resolve(rows(), {CID: page}, cfg=CFG)[CID]
    assert (c.family, c.payout_source, c.field_size_label) == ("top_n", PayoutSource.EXACT, "EXACT")
    assert ob.contest_from(c, CFG).detail.startswith("EXACT")
    six = contests.resolve(rows(), {CID: detail(NAME, [(1, 6, "10")], 237)}, cfg=CFG)[CID]  # the page pays 6, not 5
    assert "in the top 6)" in six.objective_line()
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


# -- the portfolio objective: P(at least one own entry in the top N) (flag 70) ----------------------------------------

import csv  # noqa: E402  (the second half of the file: selection, figures and the full run)
from datetime import datetime, timezone  # noqa: E402

import numpy as np  # noqa: E402

from conftest import TESTS  # noqa: E402
from nhl_dfs.build import exposure, packet, tiebreak  # noqa: E402
from nhl_dfs.build import portfolio as pf  # noqa: E402
from nhl_dfs.build.run import run_slate  # noqa: E402
from nhl_dfs.cli import _print_result  # noqa: E402
from nhl_dfs.data.http import HttpCache, load_sources_config  # noqa: E402
from pool_builder import classic_pool  # noqa: E402
from test_portfolio import E, disjoint_lineups, scenarios_for  # noqa: E402

RISK = ob.load_risk_config()
EXPO = exposure.load_exposure_config()


def flat(cid, family, field_size, paid, prize_cents, fee=100):
    p = np.full(paid, prize_cents, np.int64)
    return ob.Contest(cid, cid, family, field_size, fee, p, np.zeros(paid, bool), None, PayoutSource.PRIOR)


def _near_copy_case(b_share: float = 0.25):
    """Top 2 win $10 against two opponents who always score alike. A is in the money 30% of the time; B `b_share`, always
    together with A (a near-copy); C 25%, never with A. At b_share 0.25 the pairs {A, B} and {A, C} have the same expected
    payout; above it {A, B} has the higher one."""
    pool = classic_pool(("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"))
    a, b, c, f = disjoint_lineups(pool, 4)
    S = 4000
    i = np.arange(S)
    fld = np.full(S, 500)
    ta = np.where(i < 1200, 900, 100)  # 30%
    tb = np.where(i < int(b_share * S), 850, 100)  # inside A's 30%
    tc = np.where((i >= 2000) & (i < 3000), 900, 100)  # 25%, never with A
    scen = scenarios_for(pool, [a, b, c, f], [ta, tb, tc, fld])
    field = ob.FieldSpec([f.role_ids], [f.key], np.asarray([2]), 2, "weighted", np.asarray([2], np.int64))
    return pool, (a, b, c), scen, field


def _select_two(family, b_share: float = 0.25, risk: dict = RISK):
    pool, (a, b, c), scen, field = _near_copy_case(b_share)
    ct = {"T": flat("T", family, 4, 2, 1000)}
    caps = exposure.caps(EXPO, 2, pool, Mode.CLASSIC, 1, budget=risk["budget"]["classic"])
    sel = pf.select([a, b, c], scen, {"T": field}, ct, [E("e1", "T"), E("e2", "T")], caps,
                    pf.RiskBudget(1.0, None, None, None), seed=1, pool=pool, fam_cfg=CFG, risk_cfg=risk)
    return sel, (a, b, c)


FLAT_RISK = {**RISK, "frontier": {**RISK["frontier"], "kappas": [0.0] * 5}}  # no risk term: the objective alone decides


def test_less_overlap_beats_a_near_copy_on_p_at_least_one():
    """B and C have the same own chance (25%). With the risk term at zero, adding B to A adds nothing to P(at least one)
    and adding C adds 0.25, so C is taken. (The discriminating case, where the old objective takes the near-copy, is
    test_a_near_copy_with_a_higher_own_chance_...; with the default knobs the risk term alone would also spread these.)"""
    sel, (a, b, c) = _select_two("top_n", 0.25, FLAT_RISK)
    assert {sel.by_entry["e1"], sel.by_entry["e2"]} == {a.role_ids, c.role_ids}
    f = sel.portfolio.per_contest["T"]
    assert f["p_at_least_one"] == pytest.approx(0.55) and f["exp_top_n_finishes"] == pytest.approx(0.55)
    assert f["p_at_least_one_if_independent"] == pytest.approx(1 - 0.7 * 0.75)
    assert set(sel.portfolio.per_contest) == {"T"}


def test_summing_each_entry_chance_cannot_tell_the_near_copy_from_the_spread_pair():
    """The control: {A, B} and {A, C} have the same expected payout (what small_field sums), but P(at least one) is
    0.30 against 0.55."""
    pool, (a, b, c), scen, field = _near_copy_case()
    ct = flat("T", "top_n", 4, 2, 1000)
    fs, w = field.scores(scen, Mode.CLASSIC)
    own = scen.scores([a.role_ids, b.role_ids, c.role_ids], Mode.CLASSIC).full()
    ab, ac = (ob.joint_payouts(own[:, cols], fs, w, ct, cfg=RISK) for cols in ([0, 1], [0, 2]))
    assert ab.payout_cents.sum(1).mean() == pytest.approx(ac.payout_cents.sum(1).mean())
    assert ob.top_n_figures(ab, ct)["p_at_least_one"] == pytest.approx(0.30)
    assert ob.top_n_figures(ac, ct)["p_at_least_one"] == pytest.approx(0.55)


def test_a_near_copy_with_a_higher_own_chance_wins_the_summed_objective_and_loses_the_booster_one():
    """B (27%, always with A) has a higher own chance than C (25%, never with A). With the risk term at zero (every knob 0)
    the summed expected payout (small_field) takes B; the Booster objective takes C, for P(at least one) 0.55 not 0.30."""
    old, (a, b, c) = _select_two("small_field", 0.27, FLAT_RISK)
    new, _ = _select_two("top_n", 0.27, FLAT_RISK)
    assert {old.by_entry["e1"], old.by_entry["e2"]} == {a.role_ids, b.role_ids}
    assert {new.by_entry["e1"], new.by_entry["e2"]} == {a.role_ids, c.role_ids}
    assert new.portfolio.per_contest["T"]["p_at_least_one"] == pytest.approx(0.55)
    assert old.portfolio.exp_payout > new.portfolio.exp_payout  # the Booster objective gives up expected finishes for it


def test_other_families_report_no_top_n_figures():
    sel, _ = _select_two("small_field")
    assert sel.portfolio.per_contest == {} and "per_contest" not in sel.portfolio.record()


def test_own_entries_are_ranked_against_each_other():
    """Three own entries all above the opponents with N = 2: two finishes, not three; P(at least one) is 1."""
    S = 50
    own = np.tile(np.asarray([[900, 800, 700]]), (S, 1))
    fs, w = np.full((S, 1), 100), np.asarray([3], np.int64)
    ct = flat("T", "top_n", 6, 2, 1000)
    f = ob.top_n_figures(ob.joint_payouts(own, fs, w, ct, cfg=RISK), ct)
    assert (f["exp_top_n_finishes"], f["p_at_least_one"], f["entries"]) == (2.0, 1.0, 3)


def test_a_tie_at_the_line_counts_as_its_share():
    """One own entry tied with one opponent for place 2 of a top 2: they split place 2 and 3's prizes, $10 + $0 over 2."""
    S = 10
    own = np.full((S, 1), 500)
    fs, w = np.asarray([[900, 500]] * S), np.asarray([1, 1], np.int64)
    ct = flat("T", "top_n", 3, 2, 1000)
    m = ob.joint_payouts(own, fs, w, ct, cfg=RISK)
    assert m.payout_cents[0, 0] == 500 and m.objective[0] == pytest.approx(0.5)  # p_top_n, tie-adjusted
    assert ob.top_n_figures(m, ct)["p_at_least_one"] == pytest.approx(0.5)


def test_the_running_own_payout_of_a_booster_equals_its_recount():
    rng = np.random.default_rng(47)
    S, F, K = 80, 6, 9
    fs, w, cand = rng.integers(0, 6, (S, F)), rng.integers(1, 4, F).astype(np.int64), rng.integers(0, 6, (S, K))
    ct = flat("T", "top_n", int(w.sum()) + 5, 3, 1000)
    G, E_ = ob.ranks(cand, fs, w)
    oc = ob.OwnContest(ob.PayCurve(ct, 0.01).pay_util, G, E_, cand, capacity=2, live_below=ct.paid)
    for k in (3, 0, 7, 3, 5):
        oc.add(k)
        assert all((a == b).all() for a, b in zip(oc.total, oc.totals()))


def test_the_finishes_policy_breaks_a_band_on_expected_finishes_and_never_crosses_one():
    near = [tiebreak.Item("a", 1.000, 0.05, 0, 0, alt=0.20), tiebreak.Item("b", 0.990, 0.05, 0, 0, alt=0.30)]
    assert [r.item.key for r in tiebreak.rank(near, RISK, "finishes")] == ["b", "a"]  # inside one band: more finishes
    far = [tiebreak.Item("a", 1.0, 0.001, 0, 0, alt=0.2), tiebreak.Item("b", 0.5, 0.001, 0, 0, alt=0.9)]
    assert [r.item.key for r in tiebreak.rank(far, RISK, "finishes")] == ["a", "b"]  # outside it: the value
    assert [r.item.key for r in tiebreak.rank([tiebreak.Item("a", 1.0, 0.05, 0, 0), tiebreak.Item("b", 0.99, 0.05, 0, 0)],
                                              RISK, "finishes")] == ["a", "b"]  # no alt (late swap): as "mean"
    assert "finishes" in contests.SELECTIONS and CFG["families"]["top_n"]["selection"] == "finishes"


# -- the full offline run on a Booster-named copy of the late-swap fixture ---------------------------------------------

LS = TESTS / "fixtures" / "late_swap" / "classic"
EARLY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # every fixture game is still ahead
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _renamed(src: Path, dst: Path) -> Path:
    """The fixture's entries file with every entry moved into the Booster: its name and a $0.25 fee (derived size 235)."""
    with open(src, encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    for r in table[1:]:
        if len(r) > 3 and r[0].strip().isdigit():
            r[1], r[3] = NAME, "$0.25"
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(table)
    return dst


@pytest.fixture(scope="module")
def booster_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c47run")
    entries = _renamed(LS / "DKEntries.template.csv", tmp / "DKEntries.csv")
    cache = HttpCache(tmp / "raw", config=load_sources_config(), offline=True)
    r = run_slate(LS / "DKSalaries.csv", entries, offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs", cache=cache, clock=lambda: EARLY)
    return SimpleNamespace(tmp=tmp, r=r)


def test_the_run_prices_the_booster_from_its_name_and_says_so_everywhere(booster_run, capsys):
    r = booster_run.r
    assert r.ok, r.manifest["failed"]
    scen = {c["contest_id"]: c for c in r.manifest["scenario"]["contests"]}["297000001"]
    assert (scen["family"], scen["PAYOUT_SOURCE"], scen["FIELD_SIZE_SOURCE"], scen["field_size"]) == ("top_n", "PRIOR", "PRIOR", 235)
    assert scen["paid_positions"] == 5 and scen["first_prize"] == 10.0 and "table from the contest name" in scen["payout_note"]
    f = scen["top_n_figures"]
    assert f["measured_on"] == "referee scenarios" and f["entries"] == 5 and 0 <= f["p_at_least_one"] <= 1
    assert f["p_at_least_one"] <= f["exp_top_n_finishes"] + 1e-9
    assert any(m.startswith("OBJECTIVE contest 297000001: P(at least one own entry in the top 5)") for m in r.messages)
    notes = r.notes_path.read_text(encoding="utf-8")
    assert "- Booster 297000001: objective P(at least one own entry in the top 5)" in notes
    assert "FIELD_SIZE_SOURCE=PRIOR (derived from the contest name" in notes
    _print_result(r)
    out = capsys.readouterr().out
    assert "note: OBJECTIVE contest 297000001" in out and "note: FIELD_SIZE_SOURCE=PRIOR contest 297000001 (derived" in out


def test_the_booster_run_draws_from_the_large_gpp_field_and_keeps_the_own_goalie_rule(booster_run):
    r = booster_run.r
    fields = json.loads((r.run.path / "scenario" / "cache" / "fields.json").read_text(encoding="utf-8")) \
        if (r.run.path / "scenario" / "cache" / "fields.json").exists() else None
    if fields is None:  # the cache folder name is the scenario cache's own
        found = list(r.run.path.rglob("fields.json"))
        assert found, "no scenario cache was saved"
        fields = json.loads(found[0].read_text(encoding="utf-8"))
    assert fields["contest_family"]["297000001"] == "top_n" and set(fields["families"]) == {"large_gpp"}
    v = packet.RunView(r.run, booster_run.tmp / "runs")
    opp = og.opponent_map(v.pool)
    relaxed = {x.get("entry_id") for x in r.manifest["scenario"].get("relaxations", []) if x.get("kind") == "OWN_GOALIE"}
    for eid, lu in v.lineups.items():
        assert not og.faces_own_goalie(lu, v.pool, opp) or eid in relaxed, eid


def test_the_qa_packet_names_the_booster_table_size_and_objective(booster_run):
    p = packet.build(booster_run.r.run, 1, now=EARLY, runs_root=booster_run.tmp / "runs")
    row = next(c for c in p["portfolio"]["contests"] if c["contest_id"] == "297000001")
    assert row["family"] == "top_n" and row["PAYOUT_SOURCE"] == "PRIOR" and "contest name" in row["table"]
    assert row["FIELD_SIZE_SOURCE"] == "PRIOR" and "derived" in row["field_size_note"]
    assert "at least one own entry" in row["portfolio_objective"] and row["figures"]["entries"] == 5


def test_a_scenario_late_swap_of_a_booster_run_accepts_the_finishes_policy(booster_run, tmp_path):
    """Late swap reads the contest's selection policy ("finishes") and ranks with it; it still scores a Booster entry on
    its own (B114), but it must run."""
    from nhl_dfs.build import late_swap
    from test_started_slate import _in_progress

    cur = _renamed(LS / "DKEntries.current.csv", tmp_path / "current.csv")
    r = late_swap.run(booster_run.r.run.run_id, cur, runs_root=booster_run.tmp / "runs", as_of=EARLY, offline=True,
                      salary_path=_in_progress(tmp_path), objective="scenario")
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:5]
    assert r.manifest["objective"]["kind"] == "scenario"
    assert r.manifest["changed_cells"]  # the swap really ranked candidates
