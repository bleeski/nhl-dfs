"""C49 (B110): DraftKings' contest entry history download, read strictly, and the labels the results report slices by.

One manual download lists every contest entry: place, points, cash and ticket winnings, entrants, fee, prize pool and places
paid. `read_history` keeps the NHL rows and only counts the others (never parsed or stored). The export lives under
data/standings/history/ (gitignored) and is never committed, nor is any row of it. Definitions: docs/CONTRACTS.md, "Results
report (C49)". Booking money stays with settle (C29); nothing here tunes or selects.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

HEADER = ("Sport", "Game_Type", "Entry_Key", "Entry", "Contest_Key", "Contest_Date_EST", "Place", "Points",
          "Winnings_Non_Ticket", "Winnings_Ticket", "Contest_Entries", "Entry_Fee", "Prize_Pool", "Places_Paid")
SPORT = "NHL"
EASTERN = ZoneInfo("America/New_York")
ENGINE_ERA_START = date(2026, 9, 29)  # the first real slate the engine built; the era never comes from the record join

_MONEY = re.compile(r"^\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?$")  # DraftKings never writes a negative amount
_INT = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)$")
_POINTS = re.compile(r"^(-)?(\d+)(?:\.(\d{1,2}))?$")
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S",
                 "%m/%d/%Y %H:%M")

KIND_SOURCE = "NAME_RULE"
# First match wins: a satellite or cash payout shape outranks a "Single Entry" tag in the same name.
KIND_RULES = (("satellite", re.compile(r"satellite|qualifier", re.I)),
              ("cash", re.compile(r"double up|triple up", re.I)),
              ("top_n", re.compile(r"\btop\s+\d+\s+win\b", re.I)),
              ("single_entry_gpp", re.compile(r"single entry", re.I)))
FIELD_BUCKETS = ((25, "up to 25"), (100, "26 to 100"), (1000, "101 to 1,000"), (10000, "1,001 to 10,000"))
FIELD_TOP = "over 10,000"
ENTRIES_BUCKETS = ((1, "1"), (5, "2 to 5"), (20, "6 to 20"))
ENTRIES_TOP = "over 20"


@dataclass(frozen=True)
class HistoryRow:
    line: int  # physical line in the file; the header is line 1
    game_type: str  # as written
    format: str  # classic, showdown, or the raw game type lowercased
    entry_key: str
    contest_name: str
    contest_key: str
    contest_date_et: date  # the Eastern date as written: slate, month and era
    contest_utc: datetime  # the same instant in UTC; feeds nothing else
    place: int
    points_x100: int
    cash_cents: int
    ticket_cents: int
    entrants: int
    fee_cents: int
    pool_cents: int
    places_paid: int

    @property
    def paid(self) -> bool:
        return self.fee_cents > 0

    @property
    def net_cents(self) -> int:  # ticket winnings count, so a satellite seat is not understated
        return self.cash_cents + self.ticket_cents - self.fee_cents

    # Observed and random-entry figures, as fractions of the field (x 100 for a percentile). Integer floors throughout.
    @property
    def pct(self) -> Fraction:
        return Fraction(self.place, self.entrants)

    @property
    def random_pct(self) -> Fraction:
        return Fraction(self.entrants + 1, 2 * self.entrants)

    @property
    def in_money(self) -> bool:
        return self.place <= self.places_paid

    @property
    def exp_in_money(self) -> Fraction:
        return Fraction(min(self.places_paid, self.entrants), self.entrants)

    @property
    def exp_in_money_card(self) -> Fraction:  # P / N as the card writes it, above 1 when P exceeds N (definition check)
        return Fraction(self.places_paid, self.entrants)

    @property
    def top1(self) -> bool:
        return self.place <= self.entrants // 100

    @property
    def exp_top1(self) -> Fraction:
        return Fraction(self.entrants // 100, self.entrants)

    @property
    def top10(self) -> bool:
        return self.place <= self.entrants // 10

    @property
    def exp_top10(self) -> Fraction:
        return Fraction(self.entrants // 10, self.entrants)

    @property
    def bottom10(self) -> bool:
        return self.place > 9 * self.entrants // 10

    @property
    def exp_bottom10(self) -> Fraction:
        return Fraction(self.entrants - 9 * self.entrants // 10, self.entrants)

    @property
    def blank(self) -> bool:
        return self.points_x100 == 0


@dataclass(frozen=True)
class HistoryFile:
    path: Path
    sha256: str
    rows: tuple[HistoryRow, ...]  # NHL rows with a result, file order
    other_sport_rows: int  # counted, never parsed or stored
    unsettled_rows: int  # NHL rows with a blank Place (the contest was still open)
    unsettled_dates: tuple[date, ...] = ()  # their Eastern dates, so an old one stands out


def check_header(header: list[str], where: str) -> None:
    for i, want in enumerate(HEADER):
        if i >= len(header):
            raise ValueError(f"{where}: column {i + 1} {want!r} is missing; this is not DraftKings' contest entry history "
                             f"export ({len(HEADER)} columns expected)")
        if header[i] != want:
            raise ValueError(f"{where}: column {i + 1} is {header[i]!r}, expected {want!r}; this is not DraftKings' contest "
                             f"entry history export, or its columns moved")
    if len(header) > len(HEADER):
        raise ValueError(f"{where}: extra column {len(HEADER) + 1} {header[len(HEADER)]!r}; expected exactly "
                         f"{len(HEADER)} columns")


def parse_money(text: str) -> int:
    """'$1,000.00' -> 100000 cents. A blank, a negative or any other form is refused, never read as $0 (a negative fee
    would otherwise turn a paid entry into a free one)."""
    m = _MONEY.match(text.strip())
    if not m:
        raise ValueError(f"not a dollar amount: {text!r}")
    return int(m.group(1).replace(",", "")) * 100 + int((m.group(2) or "0").ljust(2, "0"))


def parse_int(text: str) -> int:
    m = _INT.match(text.strip())
    if not m:
        raise ValueError(f"not a whole number: {text!r}")
    return int(m.group(1).replace(",", ""))


def parse_points(text: str) -> int:
    """'145.3' -> 14530 hundredths (Showdown points carry two decimals, so tenths would round)."""
    m = _POINTS.match(text.strip())
    if not m:
        raise ValueError(f"not a points total: {text!r}")
    v = int(m.group(2)) * 100 + int((m.group(3) or "0").ljust(2, "0"))
    return -v if m.group(1) else v


def parse_eastern(text: str) -> tuple[date, datetime]:
    """(the Eastern date as written, the UTC instant). The date never comes from the UTC conversion, so a 10 PM ET contest
    stays on its own slate."""
    s = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            wall = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return wall.date(), wall.replace(tzinfo=EASTERN).astimezone(timezone.utc)
    raise ValueError(f"not a contest date and time: {text!r}")


def format_of(game_type: str) -> str:
    g = game_type.strip().lower()
    if g == "classic":
        return "classic"
    if "showdown" in g or "captain" in g:
        return "showdown"
    return game_type.strip() or "unknown"  # the raw text, its own slice


def read_history(path) -> HistoryFile:
    p = Path(path)
    raw = p.read_bytes()
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    try:
        records = [(reader.line_num, rec) for rec in reader]
    except csv.Error as exc:
        raise ValueError(f"{p.name} line {reader.line_num}: unreadable CSV ({exc})") from None
    if not records:
        raise ValueError(f"{p.name}: empty file")
    check_header(records[0][1], p.name)
    rows: list[HistoryRow] = []
    seen: set[str] = set()
    other = 0
    open_dates: list[date] = []
    for line, rec in records[1:]:
        if not any(c.strip() for c in rec):
            continue
        if len(rec) != len(HEADER):
            raise ValueError(f"{p.name} line {line}: {len(rec)} fields, expected {len(HEADER)}")
        cell = dict(zip(HEADER, rec))
        if cell["Sport"].strip().upper() != SPORT:
            other += 1
            continue
        if not cell["Place"].strip():  # a contest still open; a filled Place with blank Points is refused below
            try:
                open_dates.append(parse_eastern(cell["Contest_Date_EST"])[0])
            except ValueError as exc:
                raise ValueError(f"{p.name} line {line}, column Contest_Date_EST: {exc}") from None
            continue
        col = "Entry_Key"
        try:
            key = cell["Entry_Key"].strip()
            if not key.isdigit():
                raise ValueError(f"not an entry key: {key!r}")
            if key in seen:
                raise ValueError(f"entry key {key} appears twice")
            col = "Contest_Key"
            ckey = cell["Contest_Key"].strip()
            if not ckey.isdigit():
                raise ValueError(f"not a contest key: {ckey!r}")
            col = "Contest_Date_EST"
            d_et, t_utc = parse_eastern(cell["Contest_Date_EST"])
            vals = {}
            for col in ("Place", "Contest_Entries", "Places_Paid"):
                vals[col] = parse_int(cell[col])
            col = "Points"
            pts = parse_points(cell["Points"])
            for col in ("Winnings_Non_Ticket", "Winnings_Ticket", "Entry_Fee", "Prize_Pool"):
                vals[col] = parse_money(cell[col])
            col = "Contest_Entries"
            if vals["Contest_Entries"] < 1:
                raise ValueError("no entrants")
            col = "Place"
            if not 1 <= vals["Place"] <= vals["Contest_Entries"]:
                raise ValueError(f"place {vals['Place']} outside 1 to {vals['Contest_Entries']}")
        except ValueError as exc:
            raise ValueError(f"{p.name} line {line}, column {col}: {exc}") from None
        seen.add(key)
        rows.append(HistoryRow(
            line=line, game_type=cell["Game_Type"], format=format_of(cell["Game_Type"]), entry_key=key,
            contest_name=cell["Entry"], contest_key=ckey, contest_date_et=d_et, contest_utc=t_utc,
            place=vals["Place"], points_x100=pts, cash_cents=vals["Winnings_Non_Ticket"],
            ticket_cents=vals["Winnings_Ticket"], entrants=vals["Contest_Entries"], fee_cents=vals["Entry_Fee"],
            pool_cents=vals["Prize_Pool"], places_paid=vals["Places_Paid"]))
    return HistoryFile(path=p, sha256=hashlib.sha256(raw).hexdigest(), rows=tuple(rows), other_sport_rows=other,
                       unsettled_rows=len(open_dates), unsettled_dates=tuple(sorted(open_dates)))


# -- labels ---------------------------------------------------------------------------------------------------------------

def kind(contest_name: str) -> str:
    for name, rx in KIND_RULES:
        if rx.search(contest_name):
            return name
    return "gpp"


def era(row: HistoryRow) -> str:
    return "engine" if row.contest_date_et >= ENGINE_ERA_START else "pre-engine"


def slate_key(row: HistoryRow) -> tuple[date, str]:
    """The review's slate: the Eastern date plus the format (one Showdown slate per date, whatever the games)."""
    return row.contest_date_et, row.format


def field_bucket(entrants: int) -> str:
    for top, label in FIELD_BUCKETS:
        if entrants <= top:
            return label
    return FIELD_TOP


def entries_bucket(own_entries: int) -> str:
    for top, label in ENTRIES_BUCKETS:
        if own_entries <= top:
            return label
    return ENTRIES_TOP


# -- the recorded label: the join to the engine's own entry records -------------------------------------------------------

@dataclass(frozen=True)
class RecordSources:
    ids: frozenset[str]
    files: tuple[str, ...]  # files read
    unreadable: tuple[str, ...]  # "path: reason"


def recorded_ids(entered_dir, runs_root, outputs_root) -> RecordSources:
    """Entry IDs in data/entered/*.csv, a local run's runs/<run id>/inputs/DKEntries.csv (scratch runs/_* skipped; the folder
    also holds DKSalaries.csv, never read) and outputs/*/DKEntries.csv, read with the repo's own entries parser."""
    from nhl_dfs.intake.entries import read_entries

    cands: list[Path] = []
    if entered_dir is not None and Path(entered_dir).is_dir():
        cands += sorted(Path(entered_dir).glob("*.csv"))
    if runs_root is not None and Path(runs_root).is_dir():
        cands += sorted(d / "inputs" / "DKEntries.csv" for d in Path(runs_root).iterdir()
                        if d.is_dir() and not d.name.startswith("_") and (d / "inputs" / "DKEntries.csv").is_file())
    if outputs_root is not None and Path(outputs_root).is_dir():
        cands += sorted(d / "DKEntries.csv" for d in Path(outputs_root).iterdir()
                        if d.is_dir() and (d / "DKEntries.csv").is_file())
    ids: set[str] = set()
    files: list[str] = []
    bad: list[str] = []
    for f in cands:
        try:
            ids |= {e.entry_id.strip() for e in read_entries(f).entries}
            files.append(str(f))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            bad.append(f"{f}: {exc}"[:200])
    return RecordSources(ids=frozenset(ids), files=tuple(files), unreadable=tuple(bad))
