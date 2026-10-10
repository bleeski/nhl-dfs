"""B108: a "Top N Win $X" contest (DraftKings' Boosters) pays N flat prizes of $X, and Ben's goal there is at least one
of his entries in the top N (flag 64). Today the name is not read: with no lobby row the contest falls to the default
family and is priced as a PRIOR large_gpp of 5,000 entries paying 1,000 places on a power curve. The contest of
2026-10-10 (196555565, $0.25, 237 entries, top 5 win $10 each) is the case. Strict xfail until the chunk that owns
B108 makes it pass."""

from types import SimpleNamespace

import pytest

from nhl_dfs.build import objectives as ob
from nhl_dfs.models import contests

pytestmark = pytest.mark.c47

NAME = "NHL 40x Micro Booster [Top 5 Win $10] (Early)"
B108 = ("B108: the 'Top N Win $X' name is not read, so the contest is priced as a PRIOR large_gpp "
        "(5,000 entries, 1,000 paid on a power curve) instead of N flat prizes of $X")


def _booster(entries: int = 7):
    rows = [SimpleNamespace(entry_id=str(5289269888 + i), contest_id="196555565", contest_name=NAME, fee="$0.25")
            for i in range(entries)]
    cfg = contests.load_contest_families()
    ctx = contests.resolve(rows, cfg=cfg)["196555565"]
    return ob.contest_from(ctx, cfg)


def test_booster_is_priced_as_a_default_large_gpp_today():
    """Pins today's behavior, so the xfail below fails on its assertions and not on its setup; B108's fix replaces it."""
    c = _booster()
    assert (c.family, c.field_size) == ("large_gpp", 5000)
    assert c.paid == 1000


@pytest.mark.xfail(strict=True, reason=B108)
def test_top_n_win_name_pays_n_flat_prizes_in_a_small_field():
    c = _booster()
    assert c.prizes_cents.tolist() == [1000] * 5
    assert c.family != "large_gpp"
    assert 7 < c.field_size < 500
