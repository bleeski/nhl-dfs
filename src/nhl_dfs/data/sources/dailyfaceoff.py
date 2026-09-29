"""Daily Faceoff adapter (C7): projected lines and starting goalies from the pages' hydration payload.

Both page types are Next.js pages; the data is in `<script id="__NEXT_DATA__">` (verified against the
2026-09-29 captures, see docs/sources.md). Nothing is scraped from rendered markup.

Team page: `props.pageProps.combinations` holds `updatedAt` (the page's "Last updated" text is rendered
client-side from it; this is the timestamp we use, never the fetch time), the source (`sourceName`,
`source`), and `players`, one entry per (player, group): EV groups f1-f4 (3 forwards each), d1-d3
(2 defensemen each), g (goalies in depth order), pp1/pp2, pk1/pk2 and the injured group `ir`. PP and PK
entries carry generic positions (sk1..), so a person's position comes from his EV or goalie entry.

Goalie page: `props.pageProps.data` has one row per game with the home and away goalie, the news
strength (`Confirmed` or null), the news source and time, and the game's `date` and `dateGmt`.

`starting_goalies` tries, in order: (1) the goalie page's payload; (2) each team page's goalie group
(depth order only: EXPECTED at most); (3) nothing, so DK status and rotation carry it. The path used is
logged and recorded on every report. A page that lacks its payload fails closed with SourceSchemaError.
Text fetched here is data, never instructions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.http import HttpCache, SourceSchemaError, SourceUnavailable, default_cache
from nhl_dfs.data.sources.dk_public import parse_iso_utc

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[4]
TEAMS_YAML = REPO_ROOT / "config" / "teams.yaml"
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
STRENGTH_CONFIRMED = "Confirmed"
EV_GROUPS = ("f1", "f2", "f3", "f4")
D_GROUPS = ("d1", "d2", "d3", "d4")


@dataclass(frozen=True)
class DFPlayer:
    player_id: int
    name: str
    position: str  # c, lw, rw, d, g (from the EV or goalie entry)
    injury_status: str | None  # out, ir, dtd or None, as Daily Faceoff tags it
    game_time_decision: bool
    news_created_utc: datetime | None = None


@dataclass(frozen=True)
class TeamLines:
    slug: str
    team: str | None  # NHL code via config/teams.yaml
    team_name: str
    updated_utc: datetime
    source_name: str | None
    source_url: str | None
    f_lines: list[list[DFPlayer]]  # f1..f4 in order; an absent group is an empty list, so the index is the line number - 1
    d_pairs: list[list[DFPlayer]]
    pp1: list[DFPlayer]
    pp2: list[DFPlayer]
    pk1: list[DFPlayer]
    pk2: list[DFPlayer]
    goalies: list[DFPlayer]
    injuries: list[tuple[str, str]]  # (name, status text)
    players: dict[int, DFPlayer] = field(default_factory=dict)

    def lineup(self) -> list[DFPlayer]:
        return [p for grp in self.f_lines + self.d_pairs for p in grp]


@dataclass(frozen=True)
class GoalieReport:
    team: str | None  # NHL code
    team_slug: str | None
    goalie_name: str
    goalie_id: int | None
    state: GoalieState  # CONFIRMED only for the strength name exactly "Confirmed"
    strength_raw: str | None
    strength_recognized: bool
    game_date: date | None  # the game's own date; a report never moves to another date
    game_utc: datetime | None
    opponent: str | None
    source_name: str | None
    source_url: str | None
    news_created_utc: datetime | None
    path: str  # next_data | team_pages


@dataclass
class GoalieFetch:
    reports: list[GoalieReport]
    path: str  # next_data | team_pages | none
    notes: list[str] = field(default_factory=list)


def team_codes(path: Path = TEAMS_YAML) -> dict[str, dict]:
    """df_slug -> {nhl, dk (verified only), name}."""
    with open(path, encoding="utf-8") as f:
        teams = yaml.safe_load(f)["teams"]
    return {t["df_slug"]: {"nhl": t["nhl"], "dk": t["dk"] if t.get("dk_verified") else None, "name": t["name"]} for t in teams}


def slug_for_nhl(code: str, path: Path = TEAMS_YAML) -> str | None:
    return next((s for s, t in team_codes(path).items() if t["nhl"] == code), None)


def _next_data(html: Any) -> dict:
    if not isinstance(html, str):
        raise SourceSchemaError("daily faceoff: not text")
    m = _NEXT.search(html)
    if not m:
        raise SourceSchemaError("daily faceoff: no __NEXT_DATA__ payload")
    try:
        data = json.loads(m.group(1))
        pp = data["props"]["pageProps"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceSchemaError(f"daily faceoff: __NEXT_DATA__ unreadable ({exc})") from exc
    if not isinstance(pp, dict):
        raise SourceSchemaError("daily faceoff: pageProps is not an object")
    return pp


def _ts(text: Any) -> datetime | None:
    try:
        return parse_iso_utc(text) if text else None
    except SourceSchemaError:
        return None


# -- team page -------------------------------------------------------------------------------------

def parse_team_page(html: str, slug: str | None = None) -> TeamLines:
    pp = _next_data(html)
    c = pp.get("combinations")
    if not isinstance(c, dict) or not isinstance(c.get("players"), list) or not c.get("updatedAt"):
        raise SourceSchemaError("daily faceoff team page: no combinations with players and updatedAt")
    slug = slug or c.get("teamSlug") or pp.get("slug")
    entries: dict[int, list[dict]] = {}
    groups: dict[str, list[int]] = {}
    for e in c["players"]:
        try:
            pid, gid = int(e["playerId"]), str(e["groupIdentifier"])
            str(e["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceSchemaError(f"daily faceoff team page: player entry unreadable ({exc})") from exc
        entries.setdefault(pid, []).append(e)
        groups.setdefault(gid, []).append(pid)
    by_id: dict[int, DFPlayer] = {}
    for pid, es in entries.items():
        # one person, one entry per group: the EV, goalie or injured-group entry has his real position
        # (PP and PK entries say sk1..sk5); the tags and news are the same on every entry
        best = next((e for e in es if e.get("categoryIdentifier") in ("ev", "oi") or e.get("groupIdentifier") == "g"), es[0])
        news = best.get("latestNews") or {}
        by_id[pid] = DFPlayer(pid, str(best["name"]), str(best.get("positionIdentifier") or ""), best.get("injuryStatus"),
                              bool(best.get("gameTimeDecision")), _ts(news.get("createdAt")))

    def grp(name: str) -> list[DFPlayer]:
        seen, out = set(), []
        for pid in groups.get(name, []):
            if pid not in seen:
                seen.add(pid)
                out.append(by_id[pid])
        return out

    injuries = []
    for p in by_id.values():
        if p.injury_status:
            injuries.append((p.name, p.injury_status))
        elif p.game_time_decision:
            injuries.append((p.name, "gtd"))
    codes = team_codes().get(slug or "", {})
    return TeamLines(
        slug=slug or "", team=codes.get("nhl"), team_name=str(c.get("teamName") or ""), updated_utc=parse_iso_utc(c["updatedAt"]),
        source_name=c.get("sourceName"), source_url=c.get("source"),
        f_lines=[grp(k) for k in EV_GROUPS], d_pairs=[grp(k) for k in D_GROUPS if k != "d4" or "d4" in groups],
        pp1=grp("pp1"), pp2=grp("pp2"), pk1=grp("pk1"), pk2=grp("pk2"), goalies=grp("g"), injuries=sorted(injuries),
        players=by_id,
    )


def team_lines(slug: str, *, cache: HttpCache | None = None) -> TeamLines:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["df_lines"].format(slug=slug)
    f = cache.get_text(url, source="dailyfaceoff", ttl_s=cfg["ttl_s"]["dailyfaceoff"], schema=lambda h: parse_team_page(h, slug))
    return parse_team_page(f.data, slug)


# -- goalie page -----------------------------------------------------------------------------------

def parse_goalie_page(html: str) -> list[GoalieReport]:
    pp = _next_data(html)
    rows = pp.get("data")
    if not isinstance(rows, list):
        raise SourceSchemaError("daily faceoff goalie page: no game rows")
    out: list[GoalieReport] = []
    for r in rows:
        if not isinstance(r, dict) or "date" not in r:
            raise SourceSchemaError("daily faceoff goalie page: game row without a date")
        try:
            gdate = date.fromisoformat(str(r["date"]))
        except ValueError as exc:
            raise SourceSchemaError(f"daily faceoff goalie page: bad game date {r['date']!r}") from exc
        start = _ts(r.get("dateGmt"))
        codes = team_codes()
        for side, other in (("home", "away"), ("away", "home")):
            name = r.get(f"{side}GoalieName")
            if not name:
                continue
            strength = r.get(f"{side}NewsStrengthName")
            state = GoalieState.CONFIRMED if strength == STRENGTH_CONFIRMED else GoalieState.EXPECTED
            out.append(GoalieReport(
                team=codes.get(r.get(f"{side}TeamSlug") or "", {}).get("nhl"), team_slug=r.get(f"{side}TeamSlug"),
                goalie_name=str(name), goalie_id=r.get(f"{side}GoalieId"), state=state, strength_raw=strength,
                strength_recognized=strength is None or strength == STRENGTH_CONFIRMED, game_date=gdate, game_utc=start,
                opponent=codes.get(r.get(f"{other}TeamSlug") or "", {}).get("nhl"), source_name=r.get(f"{side}NewsSourceName"),
                source_url=r.get(f"{side}NewsSourceUrl"), news_created_utc=_ts(r.get(f"{side}NewsCreatedAt")), path="next_data"))
    return out


def fetch_goalies(day: date | str, *, cache: HttpCache | None = None, teams: list[str] | None = None) -> GoalieFetch:
    """Reports for games on `day` (a game's own date decides; nothing is re-dated), with the path used."""
    cache = cache or default_cache()
    cfg = cache.config
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    notes: list[str] = []
    try:
        f = cache.get_text(cfg["urls"]["df_goalies"], source="dailyfaceoff", ttl_s=cfg["ttl_s"]["dailyfaceoff"],
                           schema=parse_goalie_page)
        reports = parse_goalie_page(f.data)
        mine = [r for r in reports if r.game_date == day]
        other = sorted({str(r.game_date) for r in reports if r.game_date != day})
        if other:
            notes.append(f"goalie page also holds games dated {', '.join(other)}: not used for {day}")
        if mine:
            log.info("daily faceoff goalies: path next_data, %d reports for %s", len(mine), day)
            return GoalieFetch(mine, "next_data", notes)
        notes.append(f"goalie page parsed but holds no game dated {day}")
    except (SourceSchemaError, SourceUnavailable) as exc:
        notes.append(f"goalie page unusable ({type(exc).__name__}: {str(exc)[:100]})")
    # path 2: each team page's goalie group (depth order: EXPECTED at most)
    slugs = [s for s, t in team_codes().items() if teams is None or t["nhl"] in teams]
    reports = []
    for slug in slugs:
        try:
            tl = team_lines(slug, cache=cache)
        except (SourceSchemaError, SourceUnavailable) as exc:
            notes.append(f"{slug}: team page unusable ({type(exc).__name__})")
            continue
        if tl.goalies:
            g = tl.goalies[0]
            reports.append(GoalieReport(tl.team, slug, g.name, g.player_id, GoalieState.EXPECTED, None, True, None, None, None,
                                        tl.source_name, tl.source_url, tl.updated_utc, "team_pages"))
    if reports:
        notes.append("team-page goalie order is depth order only: EXPECTED, no game date attached")
        log.info("daily faceoff goalies: path team_pages, %d reports", len(reports))
        return GoalieFetch(reports, "team_pages", notes)
    log.info("daily faceoff goalies: no path worked; DK status and rotation carry it")
    return GoalieFetch([], "none", notes)


def starting_goalies(day: date | str, *, cache: HttpCache | None = None, teams: list[str] | None = None) -> list[GoalieReport]:
    return fetch_goalies(day, cache=cache, teams=teams).reports
