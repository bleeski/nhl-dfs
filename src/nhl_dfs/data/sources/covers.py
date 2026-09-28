"""Covers NHL odds board (server-rendered HTML), the backup to partner odds.

Fails closed: any row whose structure or price text cannot be parsed raises
SourceSchemaError and nothing is returned. An empty book cell (no price shown)
is not a parse failure; it is recorded as missing and counted. Page times are
Eastern and carry no year, so the year is taken from the fetch date. Covers
gives no page timestamp, so as_of_utc is the fetch time (as_of_basis "fetch").
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nhl_dfs.data.http import HttpCache, SourceSchemaError, default_cache
from nhl_dfs.data.sources.nhl import GameOdds, OddsSnapshot

_EASTERN = ZoneInfo("America/New_York")
_ROW = re.compile(r'<tr[^>]*class="oddsGameRow"[^>]*>(.*?)</tr>', re.S)
_MATCHUP = re.compile(r'/sport/hockey/nhl/matchup/(\d+)')
_TEAM = re.compile(r'<div class="td-cell (away|home)-cell\s*">\s*<a[^>]*>.*?<strong>([A-Z]{2,4})</strong>', re.S)
_TIME = re.compile(r'<div class="td-cell game-time">\s*<span>([A-Za-z]{3} \d{1,2}),(?:&nbsp;|\s)*</span>\s*<span>(\d{1,2}:\d{2})</span>', re.S)
_AMERICAN = re.compile(r'<span class="American __american"[^>]*>(.*?)</span>', re.S)
_SIDE_CELL = re.compile(r'<div class="td-cell (away|home)-cell">(.*?)</div>', re.S)
_TOTAL_LINE = re.compile(r'\b([ou])\s*(\d+(?:\.\d+)?)')
_SPREAD_LINE = re.compile(r'([+-]\d+(?:\.\d+)?)')


@dataclass(frozen=True)
class CoversParse:
    snapshot: OddsSnapshot
    empty_cells: int  # book cells present with no price, per market and side


def _table(page: str, table_id: str) -> str:
    start = page.find(f'id="{table_id}"')
    if start < 0:
        raise SourceSchemaError(f"covers: table {table_id!r} missing")
    end = page.find("</table>", start)
    if end < 0:
        raise SourceSchemaError(f"covers: table {table_id!r} not closed")
    return page[start:end]


def _american(text: str) -> int:
    t = htmllib.unescape(re.sub(r"<[^>]+>", "", text)).strip().replace("\u2212", "-")
    if t.upper() in ("EV", "EVEN"):
        return 100
    if not re.fullmatch(r"[+-]?\d{3,5}", t):
        raise SourceSchemaError(f"covers: unparseable price {t!r}")
    return int(t)


def _book_cell(row: str, game: str, book: str) -> str | None:
    m = re.search(rf'<td[^>]*data-book="{re.escape(book)}"[^>]*data-game="{game}"[^>]*>(.*?)</td>', row, re.S)
    return m.group(1) if m else None


def _sides(cell: str) -> dict[str, str]:
    return {side: body for side, body in _SIDE_CELL.findall(cell)}


def _row_identity(row: str, fetched: datetime) -> tuple[str, str, str, datetime]:
    m = _MATCHUP.search(row)
    teams = dict(_TEAM.findall(row))
    t = _TIME.search(row)
    if not m or set(teams) != {"away", "home"} or not t:
        raise SourceSchemaError("covers: row without matchup id, both teams, or game time")
    local_fetch = fetched.astimezone(_EASTERN)
    candidates = []
    for year in (local_fetch.year - 1, local_fetch.year, local_fetch.year + 1):
        stamp = datetime.strptime(f"{t.group(1)} {year} {t.group(2)}", "%b %d %Y %H:%M").replace(tzinfo=_EASTERN)
        candidates.append((abs((stamp - local_fetch).total_seconds()), stamp))
    start = min(candidates)[1].astimezone(timezone.utc)
    return m.group(1), teams["home"], teams["away"], start


def parse_covers(page: Any, fetched: datetime, books: list[str]) -> CoversParse:
    if not isinstance(page, str):
        raise SourceSchemaError("covers: expected HTML text")
    ml_table, total_table = _table(page, "moneyline-table"), _table(page, "total-table")
    spread_table = _table(page, "spread-table")
    ml_rows = _ROW.findall(ml_table)
    if not ml_rows:
        raise SourceSchemaError("covers: moneyline table has no game rows")
    by_game = {
        name: {_row_identity(r, fetched)[0]: r for r in _ROW.findall(tbl)}
        for name, tbl in (("total", total_table), ("spread", spread_table))
    }

    empty = 0
    chosen_books: set[str] = set()
    games = []

    def first_priced(row: str, game: str) -> tuple[str, dict[str, str]] | None:
        nonlocal empty
        for book in books:
            cell = _book_cell(row, game, book)
            if cell is None:
                continue
            sides = _sides(cell)
            if all(_AMERICAN.search(sides.get(s, "")) for s in ("away", "home")):
                return book, sides
            empty += 2 - sum(bool(_AMERICAN.search(sides.get(s, ""))) for s in ("away", "home"))
        return None

    for row in ml_rows:
        game, home, away, start = _row_identity(row, fetched)
        home_ml = away_ml = total_line = over = under = None
        home_puck = away_puck = None
        ml = first_priced(row, game)
        if ml:
            chosen_books.add(ml[0])
            away_ml = _american(_AMERICAN.search(ml[1]["away"]).group(1))
            home_ml = _american(_AMERICAN.search(ml[1]["home"]).group(1))
        if game in by_game["total"]:
            tot = first_priced(by_game["total"][game], game)
            if tot:
                lines = []
                for side in ("away", "home"):
                    text = htmllib.unescape(tot[1][side])
                    lm = _TOTAL_LINE.search(re.sub(r"<[^>]+>", " ", text))
                    if not lm:
                        raise SourceSchemaError(f"covers: game {game} total line unreadable")
                    lines.append((lm.group(1), float(lm.group(2)), _american(_AMERICAN.search(tot[1][side]).group(1))))
                if {l[0] for l in lines} != {"o", "u"} or lines[0][1] != lines[1][1]:
                    raise SourceSchemaError(f"covers: game {game} total sides inconsistent")
                total_line = lines[0][1]
                over = next(p for s, _, p in lines if s == "o")
                under = next(p for s, _, p in lines if s == "u")
        if game in by_game["spread"]:
            spr = first_priced(by_game["spread"][game], game)
            if spr:
                pucks = {}
                for side in ("away", "home"):
                    text = re.sub(r"<[^>]+>", " ", htmllib.unescape(_AMERICAN.sub("", spr[1][side])))
                    sm = _SPREAD_LINE.search(text)
                    if not sm:
                        raise SourceSchemaError(f"covers: game {game} puck line unreadable")
                    pucks[side] = (float(sm.group(1)), _american(_AMERICAN.search(spr[1][side]).group(1)))
                away_puck, home_puck = pucks["away"], pucks["home"]
        games.append(
            GameOdds(game, home, away, start, home_ml, away_ml, None, None, None,
                     total_line, over, under, home_puck, away_puck)
        )
    book = ",".join(sorted(chosen_books)) if chosen_books else ""
    snap = OddsSnapshot(fetched.astimezone(timezone.utc), "fetch", book, "covers", games)
    return CoversParse(snap, empty)


def odds(*, cache: HttpCache | None = None) -> OddsSnapshot:
    cache = cache or default_cache()
    cfg = cache.config
    books = cfg.get("covers_preferred_books", ["DraftKings"])
    f = cache.get_text(cfg["urls"]["covers"], source="covers", ttl_s=cfg["ttl_s"]["covers"],
                       schema=lambda page: parse_covers(page, datetime.now(timezone.utc), books))
    return parse_covers(f.data, f.fetched_at_utc, books).snapshot
