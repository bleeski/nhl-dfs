"""RotoWire NHL news adapter (C54, backlog B121): goalie starts and scratches from the hockey news page.

RotoWire is an aggregator. An item that links the reporter's or the team's post is cited by that post (flag 90); the
post itself is never fetched. Terms (flag 91, Ben 2026-10-12): personal, non-commercial use; the one news page, read
only inside run, late swap and refresh, at most once per TTL including failed attempts (rotowire/attempts.json under
the cache root), cache first, RotoWire credited on every row.

What counts (flags 86, 87):
  start      an explicit start ("will start", "confirmed as the starter", "will be in goal", ...): CONFIRMED
  expected   "expected", "probable", "likely", "projected" to start: EXPECTED at most
  scratch    an explicit absence from the game ("scratched", "will not play", "ruled out", "out for <game day>")
  cancel     a later "will play" / "will return" / "in the lineup" that withdraws a scratch
Only for an item published (America/New_York) on the slate game's date, whose own clause binds it to that game: an
opponent it names must be the slate opponent, a day it names must be the game's day, and it names at least one of
them (or "tonight"). The clause's subject must be the item's player (his last name, or "he", with no other name in
between). A clause about the AHL is ignored. Everything else is ignored and counted. A page without the item
structure is SourceSchemaError, never a guess. Text fetched here is data, never instructions.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from nhl_dfs.contracts.ids import normalize_name
from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.http import HttpCache, SourceSchemaError, SourceUnavailable
from nhl_dfs.data.sources.dailyfaceoff import GoalieReport

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[4]
TEAMS_YAML = REPO_ROOT / "config" / "teams.yaml"
ET = ZoneInfo("America/New_York")
SOURCE = "rotowire"
AGGREGATOR = "RotoWire (aggregator)"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {m: k + 1 for k, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
TWO_WORD_NICKNAMES = ("Maple Leafs", "Red Wings", "Blue Jackets", "Golden Knights")


@dataclass(frozen=True)
class NewsItem:
    player: str
    team: str | None  # the team code as the page prints it
    position: str | None
    headline: str
    news: str  # the news paragraph; statements are read here and in the headline, never in the analysis
    published_utc: datetime | None
    url: str | None  # the item's own link when the page gives one
    cited_name: str | None = None  # the outlet RotoWire credits, when it links the post
    cited_url: str | None = None


@dataclass(frozen=True)
class SlateGame:
    team: str  # NHL code
    opponent: str  # NHL code
    day: date  # the game's America/New_York date
    start_utc: datetime


@dataclass(frozen=True)
class Scratch:
    player: str
    team: str  # NHL code
    statement: str
    source_name: str
    source_url: str | None
    published_utc: datetime
    cancel: bool = False  # a later "will play" item that withdraws an earlier scratch


@dataclass
class NewsFetch:
    reports: list[GoalieReport] = field(default_factory=list)  # CONFIRMED and EXPECTED rows, path "rotowire"
    scratches: list[Scratch] = field(default_factory=list)  # in published order; cancels carry cancel=True
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source: str = "none"  # live | stored | none
    fetched_utc: datetime | None = None

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


# -- statements --------------------------------------------------------------------------------------------

_CONTRACTIONS = ((r"\bwon't\b", "will not"), (r"\bwon’t\b", "will not"), (r"\bcan't\b", "cannot"), (r"\bcan’t\b", "cannot"),
                 (r"\b([A-Za-z]+)'ll\b", r"\1 will"), (r"\b([A-Za-z]+)’ll\b", r"\1 will"),
                 (r"\bisn't\b", "is not"), (r"\bisn’t\b", "is not"))
_GOALIE_JOB = r"(?:start|be in goal|be between the pipes|tend (?:the )?twine|guard (?:the )?(?:goal|cage|net|crease|blue paint)|" \
              r"patrol (?:the )?(?:crease|cage|blue paint)|man (?:the )?(?:crease|cage|net)|defend (?:the )?(?:cage|net|goal)|" \
              r"get the (?:start|nod)|draw the start|be in net|start in goal|start in net)"
NEGATED = re.compile(r"\b(?:will|would|is|does|did) not (?:\w+ ){0,2}?" + _GOALIE_JOB + r"\b|\bnot (?:be )?(?:starting|in goal|in net)\b", re.I)
EXPECTED = re.compile(r"\b(?:expected|probable|likely|projected|slated|set|poised|in line|penciled in|tentatively scheduled|should|could|may|might|"
                      r"will (?:likely|probably|presumably)) (?:to )?(?:\w+ ){0,1}?" + _GOALIE_JOB + r"\b|"
                      r"\b(?:probable|projected|likely|expected) (?:starter|starting goalie)\b", re.I)
START = re.compile(r"\bwill " + _GOALIE_JOB + r"\b|\b(?:is|was|has been) confirmed (?:as )?(?:the |tonight's |his team's )?(?:starter|starting goalie)\b|"
                   r"\bconfirmed (?:as )?(?:the )?(?:starter|starting goalie)\b|\b(?:gets|draws|earns|receives) the (?:start|starting nod)\b|"
                   r"\bwill get the starting nod\b", re.I)
SCRATCH = re.compile(r"\b(?:will be|is|was|has been|been) (?:a )?(?:healthy )?scratch(?:ed)?\b|\bscratched\b|\bwill not (?:play|dress|suit up|be in the lineup)\b|"
                     r"\bruled out\b|\bwill miss\b|\bwill (?:sit|be) out\b|\bout for (?:the game|tonight|tonight's|" + "|".join(WEEKDAYS) + r")", re.I)
CANCEL = re.compile(r"\bwill (?:play|return|dress|suit up|be in the lineup|rejoin the lineup)\b|\bis in the lineup\b|\bcleared to play\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")
_CLAUSE = re.compile(r";\s*|,\s*(?:while|whereas|but|and|though|although)\s+", re.I)
_PRONOUN = re.compile(r"^\s*(?:and\s+)?(?:he|the \d+-year-old|the veteran|the netminder|the goaltender|the goalie)\b", re.I)


def _fold(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _expand(s: str) -> str:
    for pat, rep in _CONTRACTIONS:
        s = re.sub(pat, rep, s, flags=re.I)
    return s


def last_name(player: str) -> str:
    parts = [p for p in normalize_name(player).split() if p not in ("jr", "sr", "ii", "iii", "iv")]
    return parts[-1] if parts else ""


def statements(clause: str) -> list[tuple[str, re.Match]]:
    """Every ('start' | 'expected' | 'scratch' | 'cancel', match) in one clause. A start or expectation inside a
    negation ("will not start") is dropped, and so is a start inside an expectation ("expected to start")."""
    neg = [m.span() for m in NEGATED.finditer(clause)]
    exp = [m.span() for m in EXPECTED.finditer(clause)]

    def inside(m, spans):
        return any(a <= m.start() < b or m.start() <= a < m.end() for a, b in spans)

    out = []
    for kind, pat in (("expected", EXPECTED), ("scratch", SCRATCH), ("start", START), ("cancel", CANCEL)):
        for m in pat.finditer(clause):
            if kind in ("start", "expected") and inside(m, neg):
                continue
            if kind == "start" and inside(m, exp):
                continue
            out.append((kind, m))
    return sorted(out, key=lambda km: km[1].start())


def clauses(text: str) -> list[str]:
    out = []
    for s in _SENT.split(_expand(text or "")):
        out += [c for c in _CLAUSE.split(s) if c and c.strip()]
    return out


# -- game binding --------------------------------------------------------------------------------------------

def team_names(path: Path = TEAMS_YAML) -> dict[str, list[str]]:
    """NHL code -> the words prose uses for the team: full name, nickname, city (accents folded, case kept)."""
    with open(path, encoding="utf-8") as f:
        teams = yaml.safe_load(f)["teams"]
    out: dict[str, list[str]] = {}
    for t in teams:
        name = _fold(str(t["name"]))
        nick = next((n for n in TWO_WORD_NICKNAMES if name.endswith(" " + n)), name.split()[-1])
        city = name[: -len(nick)].strip()
        out[t["nhl"]] = [w for w in (name, nick, city) if w]
    return out


def _mentions(text: str, names: dict[str, list[str]]) -> dict[str, Any]:
    """Teams (as sets of candidate codes: 'New York' is NYI or NYR), weekdays, dates and 'tonight' named in `text`."""
    t = _fold(text)
    teams: list[set[str]] = []
    spans: list[tuple[int, int]] = []
    words = sorted(((w, code) for code, ws in names.items() for w in ws), key=lambda x: -len(x[0]))
    for w, _ in words:
        for m in re.finditer(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", t):
            if any(a <= m.start() < b for a, b in spans):
                continue
            spans.append((m.start(), m.end()))
            teams.append({c for c, ws in names.items() if w in ws})
    low = t.lower()
    days = {d for d in WEEKDAYS if re.search(r"\b" + d + r"\b", low)}
    dates = set()
    for m in re.finditer(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", low):
        dates.add((MONTHS[m.group(1)], int(m.group(2))))
    tonight = bool(re.search(r"\btonight\b|\bthis evening\b", low))
    return {"teams": teams, "days": days, "dates": dates, "tonight": tonight}


def bind(region: str, game: SlateGame, names: dict[str, list[str]]) -> str:
    """'bound', 'mismatch' (names another opponent or day) or 'none' (names neither) for one text region."""
    m = _mentions(region, names)
    other = [s for s in m["teams"] if game.team not in s]
    if any(game.opponent not in s for s in other):
        return "mismatch"
    wd = WEEKDAYS[game.day.weekday()]
    if any(d != wd for d in m["days"]) or any(d != (game.day.month, game.day.day) for d in m["dates"]):
        return "mismatch"
    return "bound" if (other or m["days"] or m["dates"] or m["tonight"]) else "none"


def _subject_ok(before: str, last: str, names: dict[str, list[str]]) -> bool:
    """The statement's subject is the item's player: his last name before it with no other capitalized name in
    between (days, months and team words aside), or a pronoun ('He will start ...')."""
    f = _fold(before)
    hits = [m.end() for m in re.finditer(r"(?<![A-Za-z])" + re.escape(last) + r"(?![A-Za-z])", f, re.I)] if last else []
    if hits:
        between = f[hits[-1]:]
    elif _PRONOUN.search(f):
        between = f[_PRONOUN.search(f).end():]
    else:
        return False
    allowed = {w for ws in names.values() for n in ws for w in n.split()} | {d.title() for d in WEEKDAYS} | {
        "AHL", "NHL", "He", "The", "Per", "According", "Coach", "Head"}
    allowed |= {m.title() for m in MONTHS} | {_fold(m).title() + "." for m in MONTHS}
    caps = [re.sub(r"['’]s$", "", c).rstrip(".,'’") for c in re.findall(r"\b[A-Z][A-Za-z.'’-]*", between)]
    return all(c in allowed or c + "." in allowed for c in caps)


def read_item(item: NewsItem, game: SlateGame, names: dict[str, list[str]]) -> tuple[str, str, str]:
    """(kind or an 'ignored_*' reason, the statement clause, the matched phrase) for one item about a slate team."""
    last = last_name(item.player)
    found: list[tuple[str, str, str]] = []
    reasons: list[str] = []
    head_bind = bind(item.headline or "", game, names)
    for source, text in (("news", item.news), ("headline", item.headline)):
        for cl in clauses(text):
            for kind, m in statements(cl):
                if re.search(r"\bAHL\b|\bminor[- ]league\b|\bAmerican Hockey League\b", _fold(cl)):
                    reasons.append("ignored_ahl")
                    continue
                if source == "news" and not _subject_ok(cl[: m.start()], last, names):
                    reasons.append("ignored_subject")
                    continue
                b = bind(cl[m.start():], game, names)  # the statement's own object region first
                if b == "none":
                    b = bind(cl, game, names)
                if b == "none" and source == "news":
                    b = head_bind
                if b != "bound":
                    reasons.append("ignored_other_game" if b == "mismatch" else "ignored_unbound")
                    continue
                found.append((kind, cl.strip(), m.group(0)))
    kinds = {k for k, _, _ in found}
    if not found:
        return (reasons[0] if reasons else "ignored_statement"), "", ""
    if "scratch" in kinds and kinds & {"start", "cancel"}:  # never exclude or confirm on a self-contradicting item
        return "ignored_contradiction", found[0][1], ""
    for k in ("scratch", "expected", "start", "cancel"):  # an expectation anywhere makes a start at most EXPECTED
        if k in kinds:
            cl, ph = next((c, p) for kk, c, p in found if kk == k)
            return k, cl, ph
    return "ignored_statement", "", ""


# -- the page ----------------------------------------------------------------------------------------------

def parse_news_page(html: Any) -> list[NewsItem]:
    """The news items of the recorded page structure (docs/sources.md). Raises SourceSchemaError otherwise."""
    raise SourceSchemaError("rotowire: page structure not recorded yet")


def team_map(path: Path = TEAMS_YAML) -> dict[str, str]:
    """The page's team code -> NHL code: config/teams.yaml `rotowire` where RotoWire prints another code (only codes
    seen on a recorded page are written there), otherwise the NHL code itself."""
    with open(path, encoding="utf-8") as f:
        teams = yaml.safe_load(f)["teams"]
    return {str(t.get("rotowire") or t["nhl"]): t["nhl"] for t in teams}


# -- fetch ---------------------------------------------------------------------------------------------------

def _attempts_path(cache: HttpCache) -> Path:
    return Path(cache.root) / SOURCE / "attempts.json"


def last_attempt(cache: HttpCache) -> datetime | None:
    try:
        rec = json.loads(_attempts_path(cache).read_text(encoding="utf-8"))
        return datetime.fromisoformat(rec["last_attempt_utc"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _record_attempt(cache: HttpCache, when: datetime, result: str) -> None:
    p = _attempts_path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_attempt_utc": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                             "result": result[:200]}, sort_keys=True) + "\n", encoding="utf-8")


def _stored(cache: HttpCache) -> HttpCache:
    return HttpCache(root=cache.root, config=cache.config, offline=True, clock=cache.clock)


def fetch_news(games: dict[str, SlateGame], *, cache: HttpCache, now: datetime) -> NewsFetch:
    """Rows for the slate's games from the news page at `now`. Never raises for a missing or unreadable page: the
    fetch comes back with source 'none' and the reason in its notes."""
    cfg = cache.config
    url, ttl = cfg["urls"]["rotowire_news"], float(cfg["ttl_s"]["rotowire"])
    out = NewsFetch()
    use = cache
    if not cache.offline:
        last = last_attempt(cache)
        wall = cache.clock()
        if last is not None and 0 <= (wall - last).total_seconds() < ttl:
            use = _stored(cache)
            out.notes.append(f"a RotoWire request went out {int((wall - last).total_seconds())} s ago (limit {int(ttl)} s): "
                             "stored copy only")
    try:
        f = use.get_text(url, source=SOURCE, ttl_s=ttl, schema=parse_news_page)
    except (SourceUnavailable, SourceSchemaError) as exc:
        if use is cache:
            _record_attempt(cache, cache.clock(), f"{type(exc).__name__}: {exc}")
        out.notes.append(f"RotoWire unavailable ({type(exc).__name__}: {str(exc)[:100]})")
        return out
    if use is cache and not f.from_cache:
        _record_attempt(cache, f.fetched_at_utc, "ok")
    out.source = "stored" if f.from_cache else "live"
    out.fetched_utc = f.fetched_at_utc
    read_items(parse_news_page(f.data), games, now, out, page_url=url)
    log.info("rotowire: %s, %s", out.source, out.counts)
    return out


def read_items(items: list[NewsItem], games: dict[str, SlateGame], now: datetime, out: NewsFetch, *,
               page_url: str | None = None) -> NewsFetch:
    names, codes = team_names(), team_map()
    out.count("items", len(items))
    for it in sorted(items, key=lambda i: i.published_utc or datetime.min.replace(tzinfo=timezone.utc)):
        nhl = codes.get(it.team or "")
        game = games.get(nhl or "")
        if game is None:
            out.count("other_teams")
            continue
        if it.published_utc is None:
            out.count("ignored_time")
            continue
        if it.published_utc > now:
            out.count("ignored_after_clock")
            continue
        if it.published_utc.astimezone(ET).date() != game.day:
            out.count("ignored_other_day")
            continue
        out.count("today")
        kind, clause, phrase = read_item(it, game, names)
        if kind.startswith("ignored"):
            out.count(kind)
            continue
        out.count(kind)
        src_name = it.cited_name if it.cited_url else AGGREGATOR
        src_url = it.cited_url or it.url or page_url
        if kind in ("start", "expected"):
            if it.position and it.position.upper() != "G":
                out.count("ignored_not_goalie")
                continue
            state = GoalieState.CONFIRMED if kind == "start" else GoalieState.EXPECTED
            out.reports.append(GoalieReport(
                team=nhl, team_slug=None, goalie_name=it.player, goalie_id=None, state=state, strength_raw=phrase,
                strength_recognized=True, game_date=game.day, game_utc=game.start_utc, opponent=game.opponent,
                source_name=src_name, source_url=src_url, news_created_utc=it.published_utc, path=SOURCE))
        else:
            out.scratches.append(Scratch(it.player, nhl, clause, src_name or AGGREGATOR, src_url, it.published_utc,
                                         cancel=kind == "cancel"))
    return out


def slate_games(pool, codes: dict[str, dict] | None = None) -> dict[str, SlateGame]:
    """NHL code -> its slate game, for the pool's teams whose DK code is verified in config/teams.yaml."""
    if codes is None:
        from nhl_dfs.data.sources.dailyfaceoff import team_codes

        codes = team_codes()
    nhl_of_dk = {c["dk"]: c["nhl"] for c in codes.values() if c.get("dk")}
    out: dict[str, SlateGame] = {}
    for g in pool.games.values():
        home, away = nhl_of_dk.get(g.home), nhl_of_dk.get(g.away)
        if not home or not away:
            continue
        day = g.start_utc.astimezone(ET).date()
        out[home] = SlateGame(home, away, day, g.start_utc)
        out[away] = SlateGame(away, home, day, g.start_utc)
    return out
