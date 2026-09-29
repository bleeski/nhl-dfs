"""History (C4): season keys, regimes and date windows shared by every history module.

The one canonical season key is the NHL eight-digit id (20252026). MoneyPuck names the same
season by its start year (2025). The regime comes from the game id's type digits
(YYYY TT NNNN: 01 preseason, 02 regular season, 03 playoffs), never from the date.
"""

from __future__ import annotations

from datetime import date

REGIMES = {"01": "preseason", "02": "regular", "03": "playoffs"}


def nhl_season(mp_year: int) -> int:
    return int(mp_year) * 10000 + int(mp_year) + 1


def mp_year(season: int) -> int:
    return int(season) // 10000


def season_of(d: date) -> int:
    """The season a calendar date belongs to (September starts a season)."""
    start = d.year if d.month >= 9 else d.year - 1
    return nhl_season(start)


def regime_of(game_id: int) -> str:
    return REGIMES.get(str(int(game_id)).zfill(10)[4:6], "other")


def season_bounds(season: int) -> tuple[date, date]:
    y = mp_year(season)
    return date(y, 9, 1), date(y + 1, 7, 31)


def regular_season_complete(season: int, as_of: date) -> bool:
    """True once the season's regular season is over (May 1 of its end year) before as_of."""
    return as_of >= date(mp_year(season) + 1, 5, 1)


def history_seasons(as_of: date, n_complete: int) -> list[int]:
    """The n most recent seasons whose regular season finished before as_of, plus the season
    containing as_of when it is still in progress. Ascending."""
    cur = season_of(as_of)
    out = [] if regular_season_complete(cur, as_of) else [cur]
    s = cur if regular_season_complete(cur, as_of) else nhl_season(mp_year(cur) - 1)
    for _ in range(n_complete):
        out.append(s)
        s = nhl_season(mp_year(s) - 1)
    return sorted(out)
