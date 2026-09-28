import json
import re
from datetime import datetime, timezone

import pytest
from conftest import fixture_bytes

from nhl_dfs.data.http import SourceSchemaError
from nhl_dfs.data.sources import covers
from nhl_dfs.data.sources.nhl import parse_partner_odds

pytestmark = pytest.mark.c1

FETCHED = datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)
BOOKS = ["DraftKings", "FanDuel", "BetMGM"]


def _page() -> str:
    return fixture_bytes("covers_odds.html").decode("utf-8")


def _parse(page):
    return covers.parse_covers(page, FETCHED, BOOKS)


def test_fixture_parses_and_agrees_with_partner_odds():
    result = _parse(_page())
    snap = result.snapshot
    assert snap.source == "covers" and snap.as_of_basis == "fetch" and snap.as_of_utc == FETCHED
    assert [(g.away_abbrev, g.home_abbrev) for g in snap.games] == [("FLA", "CAR"), ("MON", "TOR")]
    fla = snap.games[0]
    assert fla.start_utc == datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)  # "Sep 29, 17:00" Eastern
    partner = next(g for g in parse_partner_odds(json.loads(fixture_bytes("nhl_partner_odds.json"))).games
                   if g.game_id == "2026020001")
    for field in ("home_ml", "away_ml", "total_line", "over_price", "under_price", "home_puck", "away_puck"):
        assert getattr(fla, field) == getattr(partner, field), field
    assert result.empty_cells == 0 and snap.book == "DraftKings"


def test_mutated_price_fails_closed():
    page = _page()
    mutated = page.replace(">&#x2B;105<", ">abc<", 1)
    assert mutated != page
    with pytest.raises(SourceSchemaError, match="price"):
        _parse(mutated)


@pytest.mark.parametrize(
    "old, new",
    [
        ("<strong>CAR</strong>", "<b>CAR</b>"),  # team structure gone
        ('id="total-table"', 'id="totals-renamed"'),  # a market table gone
        ('class="oddsGameRow"', 'class="renamedRow"'),  # every game row gone
    ],
)
def test_structural_changes_fail_closed(old, new):
    page = _page()
    assert old in page
    with pytest.raises(SourceSchemaError):
        _parse(page.replace(old, new))


def test_empty_book_cell_falls_back_and_is_counted():
    page = _page()
    table_start = page.find('id="moneyline-table"')
    cell = re.compile(r'(<td[^>]*data-book="DraftKings"[^>]*data-game="382277"[^>]*>)(.*?)(</td>)', re.S)
    m = cell.search(page, table_start)
    emptied = page[: m.start(2)] + re.sub(r'<span class="American __american"[^>]*>.*?</span>', "", m.group(2)) + page[m.end(2):]
    result = _parse(emptied)
    fla = result.snapshot.games[0]
    assert result.empty_cells == 2
    fanduel = cell.pattern.replace("DraftKings", "FanDuel")
    fd = re.search(fanduel, emptied[table_start:], re.S).group(2)
    prices = [covers._american(x) for x in re.findall(r'<span class="American __american"[^>]*>(.*?)</span>', fd)]
    assert (fla.away_ml, fla.home_ml) == tuple(prices)
    assert "FanDuel" in result.snapshot.book


def test_even_money_and_year_inference():
    assert covers._american("EV") == 100
    assert covers._american("&#x2B;120") == 120 and covers._american("-110") == -110
    early_january = datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc)
    g = covers.parse_covers(_page(), early_january, BOOKS).snapshot.games[0]
    assert g.start_utc.year == 2026  # "Sep 29" is nearer to 2026-09-29 than 2027-09-29
