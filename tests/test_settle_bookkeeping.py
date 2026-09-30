"""Backlog B31 and B30: settling a run and its refresh or late-swap child books each DraftKings entry once and counts a
contest's labels once; a child without its own pre-lock forecast is graded against its nearest pre-lock ancestor."""
import pytest

from nhl_dfs.learn import ledger as lg
from nhl_dfs.learn.settle import evidence_counts

pytestmark = pytest.mark.c11


def _sl(run_id, settled, payouts, fee=100):
    entries = [lg.EntryResult("297000001", "NHL Synthetic Classic", eid, fee, i + 1, None, "50", p, "EXACT", "t")
               for i, (eid, p) in enumerate(payouts.items())]
    return lg.SlateLedger(run_id, "classic-20261015-x", "classic", "2026-10-15", settled, entries)


PAY = {"7100000001": 400, "7100000002": 0, "7100000003": 0}


@pytest.mark.parametrize("order", [("parent", "child"), ("child", "parent")])
def test_a_run_and_its_child_book_each_entry_once_whichever_settles_first(tmp_path, order):
    for i, rid in enumerate(order):
        sl = _sl(rid, f"2026-10-16T0{i}:00:00Z", PAY)
        before = lg.replaced_by(sl, tmp_path)
        lg.append(sl, tmp_path)
        if i == 1:
            assert before == {order[0]: 3}  # the note settle prints names the run it replaced
    df = lg.read(tmp_path)
    assert len(df) == 3 and set(df["run_id"]) == {order[1]}  # one row per entry, from the newest settle
    dd = lg.drawdown(tmp_path)
    assert dd["runs"] == 1 and dd["cum_net_known_cents"] == 400 - 300  # fees booked once


def test_the_same_run_settled_twice_replaces_its_own_rows(tmp_path):
    lg.append(_sl("parent", "2026-10-16T00:00:00Z", PAY), tmp_path)
    sl = _sl("parent", "2026-10-16T01:00:00Z", PAY)
    assert lg.replaced_by(sl, tmp_path) == {}
    lg.append(sl, tmp_path)
    assert len(lg.read(tmp_path)) == 3
    lg.append(_sl("other", "2026-10-16T02:00:00Z", {"7100000009": 0}), tmp_path)  # a different entry stays booked
    assert len(lg.read(tmp_path)) == 4


def test_a_contest_settled_on_two_runs_counts_its_labels_once():
    def entry(rid, when):
        return {"run_id": rid, "mode": "classic", "slate_date": "2026-10-15", "slate_id": "s1", "games": ["A@B"],
                "frozen": True, "complete_payout": True, "settled_utc": when,
                "contests": {"297000001": {"family": "large_gpp", "labels": 250}},
                "played": [["2026-10-15", "A@B", "p|A|F", "F"]]}

    one = evidence_counts({"parent": entry("parent", "2026-10-16T00:00:00Z")}, "classic")
    both = evidence_counts({"parent": entry("parent", "2026-10-16T00:00:00Z"),
                            "child": entry("child", "2026-10-16T01:00:00Z")}, "classic")
    assert both.ownership_labels == one.ownership_labels == 250
    assert (both.slate_groups, both.skater_games) == (one.slate_groups, one.skater_games) == (1, 1)
