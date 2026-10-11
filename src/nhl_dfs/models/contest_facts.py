"""C50 (B111; flags 65 and 73 to 79): recurring contest facts distilled from Ben's DraftKings contest entry history.

A contest that no DraftKings page, cached template table or lobby row describes (every contest of a cloud run, B91) was sized
and priced on its family prior: large_gpp 5,000 entries, 20% paid. The entry history already describes each contest Ben
enters again and again. `distill` turns the export's rows (C49's `learn/entry_history.HistoryRow`) into one row per
(mode, name key, fee): the most entrants seen, the last contest's entrants, the prize pool, the places paid, the contests seen
and their first and last dates. Nothing about Ben's entries is kept (no entry id, place, points or winnings). The table is
committed as data/contest_facts/nhl.csv (flag 65); `models/contests.resolve` uses a matching row after the page, a template,
the lobby row and C47's Booster rule fail (FIELD_SIZE_SOURCE=HISTORY, PAYOUT_SOURCE stays PRIOR).

The key (flag 78): the contest name without its game suffix "(AAA @ BBB)", its "(k/n)" counter and its "(Early)" or
"(Late)" tag, spaces collapsed, case kept; with the mode (classic or showdown) and the fee in cents. A key whose prize pool or
places paid changed between contests, a contest that lists two values, or a key with a ticket prize is refused (flag 79).
Satellites, Boosters ("Top N Win" names that are not cash, flag 76), freerolls, and names that do not start "NHL ", carry
"vs" or carry no dollar amount (head-to-head and private leagues, so no other person's name is committed) are left out and only counted. Reads and writes local files
only; it never fetches.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

from nhl_dfs.models.payout_templates import GAME_SUFFIX

REPO_ROOT = Path(__file__).resolve().parents[3]
# Read at call time (default_path), so a test can point it elsewhere (tests/conftest.py keeps every test off the real table)
DEFAULT_PATH = REPO_ROOT / "data" / "contest_facts" / "nhl.csv"
SOURCE = "data/contest_facts/nhl.csv"
COLUMNS = ("mode", "name_key", "fee", "max_entrants", "last_entrants", "prize_pool", "places_paid", "contests_seen",
           "first_date", "last_date")
MODES = ("classic", "showdown")

COUNTER = re.compile(r"\s*\(\d+/\d+\)")
SESSION_TAG = re.compile(r"\s*\((?:Early|Late)\)", re.IGNORECASE)
# C49's kind patterns (learn/entry_history.KIND_RULES, first match wins: satellite, cash, top_n) plus the config's satellite
# pattern; tests check they agree
SATELLITE = re.compile(r"satellite|qualifier|\bsat\b", re.IGNORECASE)
CASH = re.compile(r"double up|triple up", re.IGNORECASE)
BOOSTER = re.compile(r"\btop\s+\d+\s+win\b", re.IGNORECASE)
VS = re.compile(r"\bvs\b", re.IGNORECASE)
DOLLAR = re.compile(r"\$\s*\d")  # DraftKings' public contest names carry a dollar amount; a user-named league need not
NAME_PREFIX = "NHL "


def default_path() -> Path:
    return DEFAULT_PATH


def is_booster_name(name: str) -> bool:
    """A Booster by C49's order (flag 76): the words Top N Win, and not a satellite or a C49 cash name (Double Up, Triple Up;
    the config's wider cash words such as Quintuple Up are not read here, which errs toward leaving a name out). A Triple Up named
    "[Top 9 Win $3]" is cash (C49, flag 71) and may use a row; a "Top 5 Win $1K" name C47 cannot read is still a Booster."""
    return bool(BOOSTER.search(name)) and not SATELLITE.search(name) and not CASH.search(name)


def name_key(name: str) -> str:
    """The contest name without "(AAA @ BBB)", "(k/n)" and "(Early)" or "(Late)", spaces collapsed, case kept (flag 78)."""
    s = SESSION_TAG.sub("", COUNTER.sub("", GAME_SUFFIX.sub("", str(name))))
    return " ".join(s.split())


def usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _dollars(cents: int) -> str:  # the table's money format: dollars with two decimals, no sign or commas
    return f"{cents // 100}.{cents % 100:02d}"


def _cents(text: str) -> int:
    try:
        d = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"not an amount: {text!r}") from None
    c = d * 100
    if d < 0 or c != c.to_integral_value():
        raise ValueError(f"not a dollar amount in cents: {text!r}")
    return int(c)


@dataclass(frozen=True)
class FactsRow:
    mode: str
    name_key: str
    fee_cents: int
    max_entrants: int
    last_entrants: int
    pool_cents: int
    places_paid: int
    contests_seen: int
    first_date: date
    last_date: date

    @property
    def key(self) -> tuple[str, str, int]:
        return self.mode, self.name_key, self.fee_cents

    def label(self) -> str:
        return f"'{self.name_key}' ({self.mode}, {usd(self.fee_cents)})"

    def seen(self) -> str:
        n = f"{self.contests_seen} contest{'s' if self.contests_seen != 1 else ''}"
        span = (f"{self.first_date}" if self.first_date == self.last_date else f"{self.first_date} to {self.last_date}")
        return f"{n} {span}"

    def size_note(self) -> str:
        """The FIELD_SIZE_SOURCE=HISTORY note: the row's size, pool and paid places, and which row (flags 73 and 74)."""
        return (f"contest facts row {self.label()}: {self.max_entrants:,} max entrants seen (last {self.last_entrants:,}), "
                f"pool {usd(self.pool_cents)}, {self.places_paid:,} places paid, {self.seen()}; {SOURCE}")

    def payout_note(self, family: str) -> str:
        """The PAYOUT_SOURCE=PRIOR note of a contest priced on the row (flag 77)."""
        return (f"prior {family} curve on the contest facts row's pool {usd(self.pool_cents)} and {self.places_paid:,} places "
                f"paid; row {self.label()}, {self.contests_seen} contest{'s' if self.contests_seen != 1 else ''}, "
                f"last {self.last_date}")

    def record(self) -> dict:
        return {"mode": self.mode, "name_key": self.name_key, "fee": self.fee_cents / 100,
                "max_entrants": self.max_entrants, "last_entrants": self.last_entrants, "prize_pool": self.pool_cents / 100,
                "places_paid": self.places_paid, "contests_seen": self.contests_seen,
                "first_date": self.first_date.isoformat(), "last_date": self.last_date.isoformat(), "source": SOURCE}

    def cells(self) -> list[str]:
        return [self.mode, self.name_key, _dollars(self.fee_cents), str(self.max_entrants), str(self.last_entrants),
                _dollars(self.pool_cents), str(self.places_paid), str(self.contests_seen), self.first_date.isoformat(),
                self.last_date.isoformat()]


@dataclass(frozen=True)
class FactsTable:
    path: Path
    sha256: str
    rows: Mapping[tuple[str, str, int], FactsRow]

    def get(self, mode: str, key: str, fee_cents: int) -> FactsRow | None:
        return self.rows.get((mode, key, fee_cents))

    def other_fees(self, mode: str, key: str) -> list[FactsRow]:
        return sorted((r for r in self.rows.values() if r.mode == mode and r.name_key == key), key=lambda r: r.fee_cents)

    def describe(self) -> str:
        return f"{self.path} ({len(self.rows)} rows, sha256 {self.sha256[:12]})"


def render(rows: Iterable[FactsRow]) -> bytes:
    """The table's bytes: the fixed columns, rows sorted by mode, name key and fee, LF line ends (equal input, equal bytes)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in sorted(rows, key=lambda r: r.key):
        w.writerow(r.cells())
    return buf.getvalue().encode("utf-8")


def write(path, rows: Iterable[FactsRow]) -> str:
    data = render(rows)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load(path) -> FactsTable:
    """The table at `path`. An unknown header, a bad value or a duplicate key is refused with the line named."""
    p = Path(path)
    raw = p.read_bytes()
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    try:
        recs = [(reader.line_num, rec) for rec in reader]
    except csv.Error as exc:
        raise ValueError(f"{p.name} line {reader.line_num}: unreadable CSV ({exc})") from None
    if not recs or tuple(recs[0][1]) != COLUMNS:
        raise ValueError(f"{p.name}: the header is not {','.join(COLUMNS)}")
    rows: dict[tuple[str, str, int], FactsRow] = {}
    for line, rec in recs[1:]:
        if not any(c.strip() for c in rec):
            continue
        try:
            if len(rec) != len(COLUMNS):
                raise ValueError(f"{len(rec)} fields, expected {len(COLUMNS)}")
            c = dict(zip(COLUMNS, rec))
            if c["mode"] not in MODES:
                raise ValueError(f"mode {c['mode']!r} is not one of {MODES}")
            if not c["name_key"].strip() or c["name_key"] != name_key(c["name_key"]):
                raise ValueError(f"name key {c['name_key']!r} is not a stripped key")
            ints = {k: int(c[k]) for k in ("max_entrants", "last_entrants", "places_paid", "contests_seen")}
            if min(ints.values()) < 1 or ints["last_entrants"] > ints["max_entrants"]:
                raise ValueError("entrants, places paid and contests seen must be positive, last at most max")
            row = FactsRow(c["mode"], c["name_key"], _cents(c["fee"]), ints["max_entrants"], ints["last_entrants"],
                           _cents(c["prize_pool"]), ints["places_paid"], ints["contests_seen"],
                           date.fromisoformat(c["first_date"]), date.fromisoformat(c["last_date"]))
            if row.fee_cents < 1 or row.pool_cents < 1 or row.first_date > row.last_date:
                raise ValueError("fee and pool must be above $0 and the first date at most the last")
            if row.key in rows:
                raise ValueError(f"key {row.label()} appears twice")
        except ValueError as exc:
            raise ValueError(f"{p.name} line {line}: {exc}") from None
        rows[row.key] = row
    return FactsTable(p, hashlib.sha256(raw).hexdigest(), rows)


# -- distilling the export ------------------------------------------------------------------------------------------------

@dataclass
class Distilled:
    rows: list[FactsRow]
    refused: list[tuple[str, str]]  # (row label, reason)
    left_out: Counter = field(default_factory=Counter)  # reason -> contests
    contests_read: int = 0  # contests in the window that passed the filters

    @property
    def recurring(self) -> list[FactsRow]:
        return [r for r in self.rows if r.contests_seen >= 2]


def left_out_reason(name: str) -> str | None:
    """Why a contest name never makes a row (flags 76 and 78), or None."""
    if not name.startswith(NAME_PREFIX):
        return "name does not start with 'NHL '"
    if VS.search(name):
        return "name carries 'vs'"
    if not DOLLAR.search(name):
        return "name carries no dollar amount"
    if SATELLITE.search(name):
        return "satellite"
    if is_booster_name(name):
        return "Booster (Top N Win, flag 76)"
    return None


def distill(rows: Iterable, *, since: date | None = None, until: date | None = None) -> Distilled:
    """One FactsRow per (mode, name key, fee) from C49's HistoryRow objects: paid, Classic or Showdown, dated inside
    [since, until] (Eastern dates), not left out by name; keys with a changed pool or places paid, a contest listing two
    values, or a ticket prize are refused with the reason (flag 79)."""
    left: dict[str, set[str]] = defaultdict(set)
    groups: dict[tuple[str, str, int], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if (since is not None and r.contest_date_et < since) or (until is not None and r.contest_date_et > until):
            continue
        if r.fee_cents <= 0:
            left["freeroll"].add(r.contest_key)
            continue
        if r.format not in MODES:
            left[f"game type {r.game_type.strip()!r}"].add(r.contest_key)
            continue
        why = left_out_reason(r.contest_name)
        if why is not None:
            left[why].add(r.contest_key)
            continue
        groups[(r.format, name_key(r.contest_name), r.fee_cents)][r.contest_key].append(r)
    out, refused, read = [], [], 0
    for key in sorted(groups):
        contests = groups[key]
        read += len(contests)
        mode, nk, fee = key
        label = f"'{nk}' ({mode}, {usd(fee)})"
        facts = {}
        why = None
        for ck, rs in sorted(contests.items(), key=lambda kv: int(kv[0])):
            vals = {(x.entrants, x.pool_cents, x.places_paid) for x in rs}
            if len(vals) > 1:
                why = f"contest {ck} lists two values for entrants, pool or places paid"
                break
            if any(x.ticket_cents > 0 for x in rs):
                why = f"contest {ck} paid a ticket prize"
                break
            facts[ck] = (*vals.pop(), min(x.contest_utc for x in rs), rs[0].contest_date_et)
        if why is None:
            pools = sorted({v[1] for v in facts.values()})
            paid = sorted({v[2] for v in facts.values()})
            if len(pools) > 1:
                why = "prize pool changed between contests (" + ", ".join(usd(p) for p in pools) + ")"
            elif len(paid) > 1:
                why = "places paid changed between contests (" + ", ".join(f"{p:,}" for p in paid) + ")"
            elif pools[0] < 1 or paid[0] < 1:
                why = f"no prize pool or no places paid ({usd(pools[0])}, {paid[0]} paid)"
        if why is not None:
            refused.append((label, why))
            continue
        # facts[ck] = (entrants, pool, places paid, UTC start, Eastern date); the last contest is the latest start, then id
        last = max(facts, key=lambda ck: (facts[ck][3], int(ck)))
        out.append(FactsRow(mode, nk, fee, max(v[0] for v in facts.values()), facts[last][0], pools[0], paid[0],
                            len(facts), min(v[4] for v in facts.values()), max(v[4] for v in facts.values())))
    return Distilled(out, refused, Counter({k: len(v) for k, v in left.items()}), read)
