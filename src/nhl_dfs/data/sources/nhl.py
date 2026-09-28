"""NHL keyless endpoints (api-web.nhle.com and api.nhle.com/stats/rest).

Undocumented public interfaces (plan section 3). Times are UTC; time on ice
is integer seconds; odds are integer American prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import quote

from nhl_dfs.data.http import HttpCache, SourceSchemaError, default_cache
from nhl_dfs.data.sources.dk_public import parse_iso_utc


def _require(obj: Any, keys: tuple[str, ...], where: str) -> None:
    if not isinstance(obj, dict):
        raise SourceSchemaError(f"{where}: expected an object")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SourceSchemaError(f"{where}: missing {missing}")


def _mmss(text: str) -> int:
    try:
        minutes, seconds = str(text).split(":")
        return int(minutes) * 60 + int(seconds)
    except ValueError as exc:
        raise SourceSchemaError(f"unparseable time on ice {text!r}") from exc



# --- schedule --------------------------------------------------------------------------


@dataclass(frozen=True)
class Game:
    game_id: int
    season: int
    game_type: int  # 1 preseason, 2 regular season, 3 playoffs
    start_utc: datetime
    away: str
    home: str
    game_state: str


def parse_schedule(data: Any, on_date: str | None = None) -> list[Game]:
    _require(data, ("gameWeek",), "schedule")
    out = []
    for day in data["gameWeek"]:
        _require(day, ("date", "games"), "schedule day")
        if on_date is not None and day["date"] != on_date:
            continue
        for g in day["games"]:
            _require(g, ("id", "season", "gameType", "startTimeUTC", "awayTeam", "homeTeam", "gameState"), "game")
            out.append(
                Game(
                    game_id=int(g["id"]),
                    season=int(g["season"]),
                    game_type=int(g["gameType"]),
                    start_utc=parse_iso_utc(g["startTimeUTC"]),
                    away=str(g["awayTeam"]["abbrev"]),
                    home=str(g["homeTeam"]["abbrev"]),
                    game_state=str(g["gameState"]),
                )
            )
    return out


def schedule(on: date | str, *, cache: HttpCache | None = None) -> list[Game]:
    cache = cache or default_cache()
    cfg = cache.config
    day = on if isinstance(on, str) else on.isoformat()
    url = cfg["urls"]["nhl_schedule"].format(date=day)
    f = cache.get_json(url, source="nhl_schedule", ttl_s=cfg["ttl_s"]["nhl_schedule"], schema=parse_schedule)
    return parse_schedule(f.data, on_date=day)


def week_schedule(start: date | str, *, cache: HttpCache | None = None) -> list[Game]:
    """Every game in the 7-day window the schedule endpoint returns from `start`."""
    cache = cache or default_cache()
    cfg = cache.config
    day = start if isinstance(start, str) else start.isoformat()
    url = cfg["urls"]["nhl_schedule"].format(date=day)
    f = cache.get_json(url, source="nhl_schedule", ttl_s=cfg["ttl_s"]["nhl_schedule"], schema=parse_schedule)
    return parse_schedule(f.data)


# --- roster ----------------------------------------------------------------------------


@dataclass(frozen=True)
class NhlPlayer:
    nhl_id: int
    first_name: str
    last_name: str
    position: str  # C, L, R, D, G
    sweater: int | None
    team: str


def parse_roster(data: Any, team: str = "") -> list[NhlPlayer]:
    _require(data, ("forwards", "defensemen", "goalies"), "roster")
    out = []
    for group in ("forwards", "defensemen", "goalies"):
        for p in data[group]:
            _require(p, ("id", "firstName", "lastName", "positionCode"), "roster player")
            out.append(
                NhlPlayer(
                    nhl_id=int(p["id"]),
                    first_name=str(p["firstName"].get("default", "")),
                    last_name=str(p["lastName"].get("default", "")),
                    position=str(p["positionCode"]),
                    sweater=p.get("sweaterNumber"),
                    team=team,
                )
            )
    return out


def roster(team_abbrev: str, *, cache: HttpCache | None = None) -> list[NhlPlayer]:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["nhl_roster"].format(team=team_abbrev)
    f = cache.get_json(url, source="nhl_roster", ttl_s=cfg["ttl_s"]["nhl_roster"], schema=parse_roster)
    return parse_roster(f.data, team_abbrev)


# --- box score ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoxSkater:
    nhl_id: int
    team: str
    position: str
    goals: int
    assists: int
    sog: int
    blocks: int
    pp_goals: int
    toi_s: int


@dataclass(frozen=True)
class BoxGoalie:
    nhl_id: int
    team: str
    decision: str | None  # "W", "L", "O" as the NHL reports it; None when absent
    saves: int
    shots_against: int
    goals_against: int
    toi_s: int


@dataclass(frozen=True)
class BoxScore:
    game_id: int
    game_state: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    skaters: list[BoxSkater]
    goalies: list[BoxGoalie]


def parse_boxscore(data: Any) -> BoxScore:
    _require(data, ("id", "gameState", "homeTeam", "awayTeam", "playerByGameStats"), "boxscore")
    skaters, goalies = [], []
    for side in ("homeTeam", "awayTeam"):
        team = str(data[side]["abbrev"])
        stats = data["playerByGameStats"][side]
        _require(stats, ("forwards", "defense", "goalies"), f"boxscore {side}")
        for p in stats["forwards"] + stats["defense"]:
            _require(p, ("playerId", "position", "goals", "assists", "sog", "blockedShots", "toi"), "box skater")
            skaters.append(
                BoxSkater(int(p["playerId"]), team, str(p["position"]), int(p["goals"]), int(p["assists"]),
                          int(p["sog"]), int(p["blockedShots"]), int(p.get("powerPlayGoals", 0)), _mmss(p["toi"]))
            )
        for g in stats["goalies"]:
            _require(g, ("playerId", "saves", "shotsAgainst", "goalsAgainst", "toi"), "box goalie")
            goalies.append(
                BoxGoalie(int(g["playerId"]), team, g.get("decision"), int(g["saves"]), int(g["shotsAgainst"]),
                          int(g["goalsAgainst"]), _mmss(g["toi"]))
            )
    return BoxScore(
        game_id=int(data["id"]),
        game_state=str(data["gameState"]),
        home=str(data["homeTeam"]["abbrev"]),
        away=str(data["awayTeam"]["abbrev"]),
        home_score=data["homeTeam"].get("score"),
        away_score=data["awayTeam"].get("score"),
        skaters=skaters,
        goalies=goalies,
    )


def boxscore(game_id: int, *, cache: HttpCache | None = None) -> BoxScore:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["nhl_boxscore"].format(game_id=int(game_id))
    f = cache.get_json(url, source="nhl_boxscore", ttl_s=cfg["ttl_s"]["nhl_boxscore"], schema=parse_boxscore)
    return parse_boxscore(f.data)


# --- game log ------------------------------------------------------------------------------


@dataclass(frozen=True)
class GameLogRow:
    game_id: int
    game_date: str
    team: str
    opponent: str
    home_road: str
    goals: int
    assists: int
    shots: int
    pp_points: int
    sh_points: int
    toi_s: int


def parse_game_log(data: Any) -> list[GameLogRow]:
    _require(data, ("gameLog",), "game log")
    out = []
    for r in data["gameLog"]:
        _require(r, ("gameId", "gameDate", "teamAbbrev", "opponentAbbrev", "homeRoadFlag", "goals", "assists", "toi"), "game log row")
        out.append(
            GameLogRow(int(r["gameId"]), str(r["gameDate"]), str(r["teamAbbrev"]), str(r["opponentAbbrev"]),
                       str(r["homeRoadFlag"]), int(r["goals"]), int(r["assists"]), int(r.get("shots", 0)),
                       int(r.get("powerPlayPoints", 0)), int(r.get("shorthandedPoints", 0)), _mmss(r["toi"]))
        )
    return out


def game_log(nhl_id: int, season: int, game_type: int = 2, *, cache: HttpCache | None = None) -> list[GameLogRow]:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["nhl_gamelog"].format(nhl_id=int(nhl_id), season=int(season), game_type=int(game_type))
    f = cache.get_json(url, source="nhl_gamelog", ttl_s=cfg["ttl_s"]["nhl_gamelog"], schema=parse_game_log)
    return parse_game_log(f.data)


# --- per-game skater reports ------------------------------------------------------------------

REPORT_FIELDS = {
    "timeonice": ("playerId", "gameId", "gameDate", "evTimeOnIce", "ppTimeOnIce", "shTimeOnIce", "shifts"),
    "realtime": ("playerId", "gameId", "gameDate", "blockedShots", "emptyNetGoals"),
    "summary": ("playerId", "gameId", "gameDate", "goals", "assists", "shots", "ppPoints", "shPoints"),
}


def parse_report_page(data: Any, report: str) -> tuple[list[dict], int]:
    _require(data, ("data", "total"), f"{report} report")
    for row in data["data"]:
        _require(row, REPORT_FIELDS[report], f"{report} row")
    return list(data["data"]), int(data["total"])


def skater_report(
    report: Literal["summary", "timeonice", "realtime"],
    date_from: date | str,
    date_to: date | str,
    *,
    cache: HttpCache | None = None,
) -> list[dict]:
    """Per-game rows for every skater, paginated by start/limit until total is reached."""
    if report not in REPORT_FIELDS:
        raise ValueError(f"unknown report {report!r}")
    cache = cache or default_cache()
    cfg = cache.config
    d0 = date_from if isinstance(date_from, str) else date_from.isoformat()
    d1 = date_to if isinstance(date_to, str) else date_to.isoformat()
    exp = quote(f'gameDate>="{d0}" and gameDate<="{d1}"')
    limit = int(cfg.get("report_page_limit", 100))
    rows: list[dict] = []
    start = 0
    while True:
        url = cfg["urls"]["nhl_report"].format(report=report, start=start, limit=limit, exp=exp)
        f = cache.get_json(url, source="nhl_report", ttl_s=cfg["ttl_s"]["nhl_report"],
                           schema=lambda d: parse_report_page(d, report))
        page, total = parse_report_page(f.data, report)
        rows.extend(page)
        start += len(page)
        if not page or start >= total:
            return rows


# --- partner odds -------------------------------------------------------------------------------


@dataclass(frozen=True)
class GameOdds:
    game_id: str  # NHL gameId for partner odds; the source's own matchup id for Covers
    home_abbrev: str
    away_abbrev: str
    start_utc: datetime | None
    home_ml: int | None
    away_ml: int | None
    home_ml_3way: int | None
    away_ml_3way: int | None
    draw_ml: int | None
    total_line: float | None
    over_price: int | None
    under_price: int | None
    home_puck: tuple[float, int] | None  # (line, price)
    away_puck: tuple[float, int] | None


@dataclass(frozen=True)
class OddsSnapshot:
    as_of_utc: datetime  # the source's own update time when it gives one
    as_of_basis: str  # "source" (lastUpdatedUTC) or "fetch" (no source timestamp)
    book: str
    source: str
    games: list[GameOdds]


def _price(value: Any) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(f"unparseable price {value!r}") from exc
    if f != int(f):
        raise SourceSchemaError(f"non-integer American price {value!r}")
    return int(f)


def _team_markets(odds: list[dict], where: str) -> dict:
    out: dict = {}
    for o in odds:
        _require(o, ("description", "value", "qualifier"), where)
        desc, q, price = o["description"], str(o["qualifier"]), _price(o["value"])
        if desc == "MONEY_LINE_2_WAY":
            out["ml"] = price
        elif desc == "MONEY_LINE_3_WAY":
            out["draw" if q == "Draw" else "ml3"] = price
        elif desc == "OVER_UNDER":
            side = q[:1].upper()
            if side not in ("O", "U"):
                raise SourceSchemaError(f"{where}: total qualifier {q!r}")
            out["total_" + side] = (float(q[1:]), price)
        elif desc == "PUCK_LINE":
            out["puck"] = (float(q), price)
    return out


def parse_partner_odds(data: Any) -> OddsSnapshot:
    _require(data, ("lastUpdatedUTC", "games", "bettingPartner"), "partner odds")
    games = []
    for g in data["games"]:
        _require(g, ("gameId", "homeTeam", "awayTeam"), "partner odds game")
        home = _team_markets(g["homeTeam"].get("odds", []), "partner odds home")
        away = _team_markets(g["awayTeam"].get("odds", []), "partner odds away")
        if "draw" in home and "draw" in away and home["draw"] != away["draw"]:
            raise SourceSchemaError(f"game {g['gameId']}: draw price differs between teams")
        totals = [v for d in (home, away) for k, v in d.items() if k.startswith("total_")]
        over = next((v for d in (home, away) for k, v in d.items() if k == "total_O"), None)
        under = next((v for d in (home, away) for k, v in d.items() if k == "total_U"), None)
        if len({line for line, _ in totals}) > 1:
            raise SourceSchemaError(f"game {g['gameId']}: over and under lines differ")
        games.append(
            GameOdds(
                game_id=str(g["gameId"]),
                home_abbrev=str(g["homeTeam"]["abbrev"]),
                away_abbrev=str(g["awayTeam"]["abbrev"]),
                start_utc=parse_iso_utc(g["startTimeUTC"]) if g.get("startTimeUTC") else None,
                home_ml=home.get("ml"),
                away_ml=away.get("ml"),
                home_ml_3way=home.get("ml3"),
                away_ml_3way=away.get("ml3"),
                draw_ml=home.get("draw", away.get("draw")),
                total_line=totals[0][0] if totals else None,
                over_price=over[1] if over else None,
                under_price=under[1] if under else None,
                home_puck=home.get("puck"),
                away_puck=away.get("puck"),
            )
        )
    return OddsSnapshot(
        as_of_utc=parse_iso_utc(data["lastUpdatedUTC"]),
        as_of_basis="source",
        book=str(data["bettingPartner"].get("name", "")),
        source="nhl_partner_odds",
        games=games,
    )


def partner_odds(*, cache: HttpCache | None = None) -> OddsSnapshot:
    cache = cache or default_cache()
    cfg = cache.config
    f = cache.get_json(cfg["urls"]["nhl_partner_odds"], source="nhl_partner_odds",
                       ttl_s=cfg["ttl_s"]["nhl_partner_odds"], schema=parse_partner_odds)
    return parse_partner_odds(f.data)
