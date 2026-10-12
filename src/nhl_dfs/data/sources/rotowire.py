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
from html.parser import HTMLParser
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
    published_day: date | None = None  # the printed date; the page prints no time (flag 94)
    news_id: int | None = None  # the number ending the headline link; rises with each new item
    page_rank: int | None = None  # 0 is the top of the page (the newest)


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

_CONTRACTIONS = ((r"\bwon['’]t\b", "will not"), (r"\bcan['’]t\b", "cannot"), (r"\b([A-Za-z]+)['’]ll\b", r"\1 will"),
                 (r"\b(is|has|was|does|did|are|should|would|could|were|have|had|must|might)n['’]t\b", r"\1 not"))
_ABBREV = re.compile(r"\b(vs|St|Jr|Sr|Dr|Mr|No|Mt|Ft|a\.m|p\.m)\.(?=\s)")
_DOT = "\u2024"  # one-dot leader: an abbreviation's period while sentences are split, restored after
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


_NEG_WORDS = re.compile(r"\b(?:not|never|no longer|yet to|whether|if|unless|nor|cannot|instead of|rather than)\b", re.I)
_HEDGE_WORDS = re.compile(r"\b(?:could|may|might|likely|expected|expects|expecting|probably|possibly|potentially|perhaps|unclear|"
                          r"uncertain|doubtful|hope|hopes|hoping|hopeful|aims?|plans?|planning|slated|projected|probable|"
                          r"tentatively|questionable|should)\b", re.I)
_CONDITIONAL_AFTER = re.compile(r"\b(?:if|unless|provided|assuming|in case|pending|depending)\b", re.I)
_START_NOT_A_GAME = re.compile(r"^\s+(?:[a-z]+ing\b|a\b|an\b|the season\b|his rehab|the year\b|in the AHL\b|for the AHL\b|"
                               r"(?:the |his )?(?:practice|skate|skating|conditioning|rehab|stint|assignment)\b)", re.I)
_MISS_NOT_A_GAME = re.compile(r"^\s+(?:\S+\s+){0,2}?(?:practice|skate|skating|morning skate|session|meeting|workout|"
                              r"optional|team meeting|media)\b", re.I)


def _context_ok(kind: str, clause: str, m: re.Match) -> bool:
    """Flag 86: a statement counts only when nothing in the four words before it or in its own text negates or
    hedges it ('has not been ruled out', 'unclear whether he will start', 'might not start'), and nothing after it
    makes it conditional or about something other than the game ('will start skating', 'will miss practice')."""
    pre = " ".join(clause[: m.start()].split()[-4:])
    if _NEG_WORDS.search(pre) or (kind != "scratch" and _NEG_WORDS.search(m.group(0))):  # 'will not play' is the scratch
        return False
    if kind != "expected" and _HEDGE_WORDS.search(pre):
        return False
    after = clause[m.end():]
    if kind in ("start", "scratch", "cancel") and _CONDITIONAL_AFTER.search(after):
        return False
    if kind == "start" and _START_NOT_A_GAME.search(after):
        return False
    if kind == "scratch" and m.group(0).lower().startswith("will miss") and _MISS_NOT_A_GAME.search(after):
        return False
    return True


def statements(clause: str) -> list[tuple[str, re.Match]]:
    """Every ('start' | 'expected' | 'scratch' | 'cancel', match) in one clause that survives its context. A start or
    expectation inside a negation ("will not start") is dropped, and so is a start inside an expectation."""
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
            if not _context_ok(kind, clause, m):
                continue
            out.append((kind, m))
    return sorted(out, key=lambda km: km[1].start())


def clauses(text: str) -> list[str]:
    out = []
    for s in _SENT.split(_ABBREV.sub(lambda m: m.group(1) + _DOT, _expand(text or ""))):
        out += [c.replace(_DOT, ".") for c in _CLAUSE.split(s) if c and c.strip()]
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


_COMMON_CAPS = {"The", "He", "His", "Him", "With", "After", "Before", "In", "On", "At", "For", "If", "Despite", "Although",
                "While", "Since", "Through", "Over", "During", "This", "That", "It", "Its", "Both", "Neither", "Either", "A",
                "An", "And", "But", "So", "Per", "According", "Coach", "Head", "Game", "Games", "AHL", "NHL", "ECHL", "KHL",
                "GAA", "SV", "Stanley", "Cup", "Calder", "Vezina", "All-Star", "Olympic", "Olympics", "Team", "General",
                "Manager", "GM", "Assistant", "Hockey", "League", "Western", "Eastern", "Conference", "Division", "American"}
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")


def _allowed_caps(names: dict[str, list[str]]) -> set[str]:
    out = {w for ws in names.values() for n in ws for w in n.split()} | {d.title() for d in WEEKDAYS} | _COMMON_CAPS
    out |= {m.title() for m in MONTHS} | set(_MONTH_NAMES)
    return out | {w.rstrip(".") for w in out}


def _caps(text: str) -> list[str]:
    return [re.sub(r"['’]s$", "", c).rstrip(".,'’") for c in re.findall(r"\b[A-Z][A-Za-z.'’-]*", _fold(text))]


def _person_tokens(text: str, allowed: set[str]) -> list[str]:
    """Capitalized words that may name a person (team, day, month and common words aside), in order."""
    return [c for c in _caps(text) if c and c not in allowed]


def _hit_is_player(text: str, end: int, last: str, first: str, allowed: set[str]) -> bool:
    """The last-name hit ending at `end` is the item's player, not a namesake ('Jack Hughes' on Luke Hughes's item):
    the word right before it is his first name, or not a capitalized name at all."""
    words = text[: end - len(last)].split()
    word = re.sub(r"['’]s$", "", words[-1]).rstrip(".,'’") if words else ""
    return not (word[:1].isupper() and word not in allowed and word.lower() != first)


def _subject_ok(before: str, after: str, last: str, first: str, names: dict[str, list[str]], *, pronoun_ok: bool) -> bool:
    """The statement's subject is the item's player: his last name before it (not a namesake's) with no other
    capitalized name in between, or a pronoun when no other person was named since he was; and a start whose object
    is a name ('the Hurricanes will start Kochetkov') counts only for that name."""
    allowed = _allowed_caps(names)
    f, fa = _fold(before), _fold(after)
    obj = re.match(r"\s+([A-Z][A-Za-z.'’-]+)(?:\s+([A-Z][A-Za-z.'’-]+))?", fa)
    if obj:
        toks = [re.sub(r"['’]s$", "", t).rstrip(".,'’") for t in obj.groups() if t]
        people = [t for t in toks if t not in allowed]
        if people:  # 'will start <Name>': the name is the starter
            return last.lower() in {t.lower() for t in people}
    hits = [m.end() for m in re.finditer(r"(?<![A-Za-z])" + re.escape(last) + r"(?![A-Za-z])", f, re.I)] if last else []
    if hits:
        if not _hit_is_player(f, hits[-1], last, first, allowed):
            return False
        between = f[hits[-1]:]
    elif pronoun_ok and _PRONOUN.search(f):
        between = f[_PRONOUN.search(f).end():]
    else:
        return False
    return not _person_tokens(between, allowed)


def read_item(item: NewsItem, game: SlateGame, names: dict[str, list[str]]) -> tuple[str, str, str]:
    """(kind or an 'ignored_*' reason, the statement clause, the matched phrase) for one item about a slate team."""
    last = last_name(item.player)
    first = (normalize_name(item.player).split() or [""])[0]
    allowed = _allowed_caps(names)
    found: list[tuple[str, str, str]] = []
    reasons: list[str] = []
    head_bind = bind(item.headline or "", game, names)
    for source, text in (("news", item.news), ("headline", item.headline)):
        player_last = True  # a pronoun refers to the player until another person is named after him
        for cl in clauses(text):
            stmts = statements(cl)
            for kind, m in stmts:
                if re.search(r"\bAHL\b|\bminor[- ]league\b|\bAmerican Hockey League\b", _fold(cl)):
                    reasons.append("ignored_ahl")
                    continue
                if source == "news" and not _subject_ok(cl[: m.start()], cl[m.end():], last, first, names,
                                                        pronoun_ok=player_last):
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
            if source == "news":
                f = _fold(cl)
                for tok in _person_tokens(f, allowed):
                    player_last = tok.lower() == last
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

SITE = "https://www.rotowire.com"
_ZONES = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York", "CT": "America/Chicago",
          "CST": "America/Chicago", "CDT": "America/Chicago", "MT": "America/Denver", "MST": "America/Denver",
          "MDT": "America/Denver", "PT": "America/Los_Angeles", "PST": "America/Los_Angeles",
          "PDT": "America/Los_Angeles", "UTC": "UTC", "GMT": "UTC"}
_STAMP = re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2}),\s*(?P<y>\d{4})"
                    r"(?:,?\s*(?:at\s+)?(?P<h>\d{1,2}):(?P<mi>\d{2})\s*(?P<ap>[AaPp])\.?[Mm]\.?"
                    r"(?:\s+(?P<z>[A-Z]{2,3}))?)?$")
_FIELDS = {"news-update__logo": "logo", "news-update__player-link": "player", "news-update__headline": "headline",
           "news-update__pos": "pos", "news-update__timestamp": "stamp", "news-update__news": "news",
           "news-update__analysis": "analysis"}
_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


_MONTH_WORDS = {**{n.lower(): k + 1 for k, n in enumerate(_MONTH_NAMES)}, **MONTHS, "sept": 9}


def parse_stamp(text: str) -> tuple[datetime | None, date | None]:
    """(an aware UTC time, the printed date) from an item's timestamp. The recorded page prints a date only
    ("October 11, 2026"): the time is None then and the date is the item's day (flag 94). A time, if one is ever
    printed, is read in its stated zone, America/New_York when none is given. Anything else raises ValueError."""
    m = _STAMP.match(" ".join((text or "").split()))
    if not m:
        raise ValueError(f"timestamp not recognized: {text!r}")
    month = _MONTH_WORDS.get(m["mon"].lower())
    if month is None:
        raise ValueError(f"month not recognized: {text!r}")
    day = date(int(m["y"]), month, int(m["d"]))
    if m["h"] is None:
        return None, day
    zone = _ZONES.get(m["z"] or "ET")
    if zone is None:
        raise ValueError(f"time zone not recognized: {text!r}")
    hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "p" else 0)
    local = datetime(day.year, day.month, day.day, hour, int(m["mi"]), tzinfo=ZoneInfo(zone))
    return local.astimezone(timezone.utc), day


class _PageParser(HTMLParser):
    """Collects each <div class="news-update"> element's fields by class name; the analysis is read and dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self.cur: dict[str, Any] | None = None
        self.stack: list[tuple[str, str | None]] = []  # open elements inside the current item: (tag, field)

    def _field(self) -> str | None:
        return next((f for _, f in reversed(self.stack) if f), None)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if self.cur is None:
            if tag == "div" and "news-update" in classes:
                self.cur = {"text": {}, "links": [], "logo": None, "href": None}
                self.stack = [(tag, None)]
            return
        field_ = next((_FIELDS[c] for c in classes if c in _FIELDS), None)
        if field_ == "logo":
            self.cur["logo"] = a.get("alt")
        if field_ == "headline":
            self.cur["href"] = a.get("href")
        if tag == "a" and self._field() == "news" and a.get("href"):
            self.cur["links"].append([a["href"], ""])
        if tag not in _VOID:
            self.stack.append((tag, field_))

    def handle_endtag(self, tag):
        if self.cur is None or tag in _VOID:
            return
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break
        if not self.stack:
            self.items.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cur is None:
            return
        f = self._field()
        if f:
            self.cur["text"][f] = self.cur["text"].get(f, "") + data
            if f == "news" and self.cur["links"] and any(t == "a" for t, _ in self.stack):
                self.cur["links"][-1][1] += data


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def parse_news_page(html: Any) -> list[NewsItem]:
    """The news items of the recorded page structure (docs/sources.md, flag 94), newest first as the page lists them.
    An item without a player or a readable date comes back with player "" or no date, and read_items skips and counts
    it. Raises SourceSchemaError when the page has no news item, or no item has both."""
    if not isinstance(html, str):
        raise SourceSchemaError("rotowire: the page is not text")
    p = _PageParser()
    p.feed(html)
    p.close()
    if not p.items:
        raise SourceSchemaError("rotowire: no news items")
    out: list[NewsItem] = []
    for rank, raw in enumerate(p.items):
        t = raw["text"]
        player = _clean(t.get("player"))
        try:
            when, day = parse_stamp(t.get("stamp", ""))
        except ValueError:
            when, day = None, None  # read_items skips and counts it (flag 94 e)
        href = raw["href"] or ""
        url = SITE + href if href.startswith("/") else (href or None)
        nid = re.search(r"-(\d+)/?$", href)
        cited = next(((h, _clean(x)) for h, x in raw["links"]
                      if re.match(r"https?://", h) and not re.match(r"https?://([\w-]+\.)*rotowire\.com(/|$)", h)), None)
        out.append(NewsItem(
            player=player, team=_clean(raw["logo"]).upper() or None, position=_clean(t.get("pos")) or None,
            headline=_clean(t.get("headline")), news=_clean(t.get("news")), published_utc=when, url=url,
            cited_name=(cited[1] or None) if cited else None, cited_url=cited[0] if cited else None,
            published_day=day, news_id=int(nid.group(1)) if nid else None, page_rank=rank))
    if not any(i.player and (i.published_day or i.published_utc) for i in out):
        raise SourceSchemaError("rotowire: no news item has a player and a readable date")
    return out


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


def _no_retry(cache: HttpCache) -> HttpCache:
    """The live view for this one page: no retry, so an attempt is one request (flag 91)."""
    cfg = dict(cache.config)
    cfg["http"] = {**cfg["http"], "retries": 0}
    return HttpCache(root=cache.root, config=cfg, transport=cache.transport, offline=False, clock=cache.clock,
                     sleep=cache.sleep)


def fetch_news(games: dict[str, SlateGame], *, cache: HttpCache, now: datetime) -> NewsFetch:
    """Rows for the slate's games from the news page at `now`. Never raises for a missing or unreadable page: the
    fetch comes back with source 'none' and the reason in its notes. Online, a request goes out only when the stored
    copy is older than the TTL and no attempt was made within the TTL; the attempt is written before the request
    (an abandoned, overlapping or killed read cannot send a second one), and a failed request falls back to the
    stored copy. Offline reads never write an attempt."""
    cfg = cache.config
    url, ttl = cfg["urls"]["rotowire_news"], float(cfg["ttl_s"]["rotowire"])
    out = NewsFetch()
    stored = cache if cache.offline else _stored(cache)
    go_live = False
    if not cache.offline:
        wall = cache.clock()
        entry = cache._load_index(SOURCE).get(url)
        age = (wall - datetime.fromisoformat(entry["fetched_at"].replace("Z", "+00:00"))).total_seconds() if entry else None
        last = last_attempt(cache)
        if age is not None and age <= ttl:
            pass  # the stored copy is current: no request
        elif last is not None and (wall - last).total_seconds() < ttl:  # a clock behind the record counts as inside
            out.notes.append(f"a RotoWire request went out {int((wall - last).total_seconds())} s ago (limit {int(ttl)} s): "
                             "stored copy only")
        else:
            go_live = True
    f = None
    if go_live:
        _record_attempt(cache, wall, "pending")
        try:
            f = _no_retry(cache).get_text(url, source=SOURCE, ttl_s=ttl, schema=parse_news_page)
            _record_attempt(cache, wall, "ok")
        except Exception as exc:  # any failure: one attempt spent, the stored copy if there is one
            _record_attempt(cache, wall, f"{type(exc).__name__}: {exc}")
            out.notes.append(f"RotoWire request failed ({type(exc).__name__}: {str(exc)[:100]}); stored copy used if any")
    if f is None:
        try:
            f = stored.get_text(url, source=SOURCE, ttl_s=ttl, schema=parse_news_page)
        except (SourceUnavailable, SourceSchemaError) as exc:
            out.notes.append(f"RotoWire unavailable ({type(exc).__name__}: {str(exc)[:100]})")
            return out
    out.source = "live" if (go_live and not f.from_cache) else "stored"
    out.fetched_utc = f.fetched_at_utc
    read_items(parse_news_page(f.data), games, now, out, page_url=url, fetched_utc=f.fetched_at_utc,
               live=out.source == "live")
    log.info("rotowire: %s, %s", out.source, out.counts)
    return out


READ_GRACE_S = 60.0  # flag 94 (b): a stored page read at most this long after the run clock still counts
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def chronological(items: list[NewsItem]) -> list[NewsItem]:
    """Oldest first (flag 94 d): by the news id when every item has one (the page's own order), otherwise by time and
    then by reversed page position."""
    if items and all(i.news_id is not None for i in items):
        return sorted(items, key=lambda i: i.news_id)
    return sorted(items, key=lambda i: (i.published_utc if i.published_utc is not None and i.published_utc.tzinfo
                                        else _EPOCH, -(i.page_rank or 0)))


def read_items(items: list[NewsItem], games: dict[str, SlateGame], now: datetime, out: NewsFetch, *,
               page_url: str | None = None, fetched_utc: datetime | None = None, live: bool = False) -> NewsFetch:
    """The slate's rows from parsed items, oldest first. A timed item counts on its New York date and not after the
    clock (flag 87); a dated item (the page prints dates only, flag 94) counts on its printed date when the page was
    read in this pass or at most READ_GRACE_S after the clock, and carries the page's read time."""
    names, codes = team_names(), team_map()
    out.count("items", len(items))
    page_known = fetched_utc is not None and fetched_utc.tzinfo is not None and (
        live or (fetched_utc - now).total_seconds() <= READ_GRACE_S)
    unknown: set[str] = set()
    for it in chronological(items):
        if not it.player:
            out.count("skipped_malformed")
            continue
        nhl = codes.get(it.team or "")
        if nhl is None:
            out.count("unknown_team_code")
            unknown.add(it.team or "(none)")
            continue
        game = games.get(nhl)
        if game is None:
            out.count("skipped_other_teams")
            continue
        if it.published_utc is not None and it.published_utc.tzinfo is not None:
            when = it.published_utc
            if when > now:
                out.count("skipped_after_clock")
                continue
            if when.astimezone(ET).date() != game.day:
                out.count("skipped_other_day")
                continue
        elif it.published_utc is None and it.published_day is not None:
            if fetched_utc is None or fetched_utc.tzinfo is None:
                out.count("skipped_no_time")
                continue
            if not page_known:
                out.count("skipped_after_clock")
                continue
            if it.published_day != game.day:
                out.count("skipped_other_day")
                continue
            when = fetched_utc
            out.count("dated_only")
        else:
            out.count("skipped_no_time")
            continue
        out.count("today")
        kind, clause, phrase = read_item(it, game, names)
        if kind in ("start", "expected") and it.position and it.position.upper() != "G":
            kind = "ignored_not_goalie"
        out.count(kind)
        if kind.startswith("ignored"):
            continue
        src_name = (it.cited_name or "cited post") if it.cited_url else AGGREGATOR
        src_url = it.cited_url or it.url or page_url
        if kind in ("start", "expected"):
            state = GoalieState.CONFIRMED if kind == "start" else GoalieState.EXPECTED
            out.reports.append(GoalieReport(
                team=nhl, team_slug=None, goalie_name=it.player, goalie_id=None, state=state, strength_raw=phrase,
                strength_recognized=True, game_date=game.day, game_utc=game.start_utc, opponent=game.opponent,
                source_name=src_name, source_url=src_url, news_created_utc=when, path=SOURCE))
        else:
            out.scratches.append(Scratch(it.player, nhl, clause, src_name or AGGREGATOR, src_url, when,
                                         cancel=kind == "cancel"))
    for code in sorted(unknown):
        out.notes.append(f"RotoWire team code {code!r} is not in config/teams.yaml: its items are not read")
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
