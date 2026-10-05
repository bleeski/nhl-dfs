"""Payout templates (card C16, backlog B62 and B50, [BEN] flag 14).

A DraftKings contest page that answers 403 leaves a contest without its prize table. DraftKings publishes the same
table for every contest of one template (same name without the game suffix, same max entries), and earlier days'
tables are on disk. This module finds that table: the lobby capture names the contest's template and size, a cached
or saved `dk_contest_*.json` of the same name and size supplies the tiers. One matcher serves the build
(`models/contests.resolve`) and settle (`learn/ledger.prize_tables`). It reads local files only; it never fetches.

A match needs the lobby row of the contest, a non-satellite, the same (name key, max entries), the same fee and
the same prize pool as the table (the contest being priced, not flags on the old table, is what proves it is the
same table), and, when both lobby rows carry DraftKings' own template id (`tmpl`), the same id (a veto only: it
never widens a match). A table that was resized, holds a ticket tier or fails its own totals is refused. Anything
else is a refusal with its reason; the caller keeps PRIOR (build) or UNKNOWN (settle).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from nhl_dfs.data.sources.dk_public import ContestDetail, parse_contest_detail, parse_ms_date

GAME_SUFFIX = re.compile(r"\s*\([A-Z]{2,3} @ [A-Z]{2,3}[^)]*\)")


def template_key(name: str, max_entries: int) -> tuple[str, int]:
    """(name without the game suffix and stray whitespace, max entries). "(Late)" and every other word stay: a Late
    contest is another template. Case is kept (a stricter match, never a wider one)."""
    clean = GAME_SUFFIX.sub("", str(name))
    return " ".join(clean.split()), int(max_entries)


def _money(x) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return None


# -- the lobby capture -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LobbyRow:
    """One contest as the lobby capture listed it (the fields the matcher and settle use)."""

    contest_id: int
    name: str
    fee: Decimal
    max_entries: int
    max_per_user: int
    prize_pool: Decimal
    guaranteed: bool | None  # the row's own attr.IsGuaranteed; None when the row does not say
    template_id: int | None  # DraftKings' own template id (`tmpl`)
    start_utc: datetime | None
    snapshot: str = ""  # the capture it came from, for the record

    @property
    def key(self) -> tuple[str, int]:
        return template_key(self.name, self.max_entries)


def _lobby_row(c: dict, snapshot: str) -> LobbyRow | None:
    try:
        cid = int(c["id"])
        fee, pool = _money(c["a"]), _money(c["po"])
        if fee is None or pool is None:
            return None
        attr = c.get("attr") or {}
        g = attr.get("IsGuaranteed")
        try:
            start = parse_ms_date(c["sd"]) if c.get("sd") else None
        except Exception:
            start = None
        return LobbyRow(cid, str(c["n"]), fee, int(c["m"]), int(c.get("mec") or 0), pool,
                        None if g is None else str(g).lower() == "true",
                        int(c["tmpl"]) if c.get("tmpl") not in (None, "") else None, start, snapshot)
    except (KeyError, TypeError, ValueError):
        return None


def lobby_rows(cache_root: Path, want: Iterable[int]) -> dict[int, LobbyRow]:
    """contest id -> its lobby row from the newest capture that lists it (`<cache_root>/dk_lobby/<date>/*.json`).
    Scans newest first and stops once every wanted id is found. A malformed capture is skipped."""
    left = {int(x) for x in want}
    out: dict[int, LobbyRow] = {}
    d = Path(cache_root) / "dk_lobby"
    if not left or not d.is_dir():
        return out
    files = [p for p in d.rglob("*.json") if p.name != "index.json"]
    files.sort(key=lambda p: (p.parent.name, p.stat().st_mtime, p.name), reverse=True)
    for p in files:
        try:
            js = json.loads(p.read_bytes().decode("utf-8-sig", errors="ignore"))
        except (OSError, ValueError):
            continue
        for c in js.get("Contests", []) if isinstance(js, dict) else []:
            try:
                cid = int(c.get("id"))
            except (TypeError, ValueError):
                continue
            if cid in left:
                row = _lobby_row(c, f"{p.parent.name}/{p.stem[:10]}")
                if row is not None:
                    out[cid] = row
                    left.discard(cid)
        if not left:
            break
    return out


# -- cached and saved tables -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class TemplateTable:
    contest_id: int
    name: str
    key: tuple[str, int]
    max_entries: int
    fee: Decimal
    total_payouts: Decimal
    guaranteed: bool
    start_utc: datetime
    paid: int
    sha256: str  # of the tiers (min, max, cash cents): what a later check compares
    source: str
    detail: ContestDetail = field(repr=False, compare=False)
    template_id: int | None = None  # the table contest's own lobby `tmpl`, when its row is still captured


def _tiers_sha(detail: ContestDetail) -> str:
    rows = [[t.min_pos, t.max_pos, int(t.cash * 100)] for t in sorted(detail.payout_table, key=lambda t: t.min_pos)]
    return hashlib.sha256(json.dumps(rows).encode("ascii")).hexdigest()[:16]


def read_table(data, source: str) -> TemplateTable | str:
    """A TemplateTable from a parsed contest-detail body, or the reason it cannot serve as a template."""
    try:
        detail = parse_contest_detail(data)
    except Exception as exc:  # SourceSchemaError, TypeError on a body that is not a contest detail
        return f"not a contest detail ({type(exc).__name__})"
    d = detail.raw.get("contestDetail") or {}
    if d.get("wasResized"):
        return "the contest was resized"
    if d.get("isResizable"):
        return "the contest is resizable (its table moves with the entries)"
    if any(t.other for t in detail.payout_table):
        return "a ticket (non-cash) tier: a satellite is never priced on a template"
    paid = max((t.max_pos for t in detail.payout_table if t.cash > 0), default=0)
    if paid < 1:
        return "no cash tier"
    total = _money(d.get("totalPayouts"))
    if total is None:
        return "no totalPayouts to check the tiers against"
    tiers_total = sum((t.cash * (t.max_pos - t.min_pos + 1) for t in detail.payout_table), Decimal(0))
    if int(tiers_total * 100) != int(total * 100):
        return f"the tiers sum to {tiers_total} but totalPayouts is {total}"
    return TemplateTable(detail.contest_id, detail.name, template_key(detail.name, detail.maximum_entries),
                         detail.maximum_entries, detail.entry_fee, total, bool(d.get("isGuaranteed")), detail.start_utc,
                         paid, _tiers_sha(detail), source, detail)


def _scan(paths: Iterable[tuple[Path, str]], out: dict[int, TemplateTable], refused: list[tuple[str, str]]) -> None:
    for p, label in paths:
        try:
            data = json.loads(p.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "payoutSummary" not in (data.get("contestDetail") or {}):
            continue  # an index, a lobby body or something else: not a table, not worth a refusal line
        t = read_table(data, label)
        if isinstance(t, str):
            refused.append((label, t))
        elif t.contest_id not in out:
            out[t.contest_id] = t


def table_sources(cache_root: Path, runs_root: Path | None, extra_dirs: Iterable[Path] = ()) -> list[tuple[Path, str]]:
    """Every place a saved table can be, each with its label: the raw DK cache, every run's pre-lock copy and settle
    copy, and browser-saved `dk_contest_*.json` files in the given folders."""
    out: list[tuple[Path, str]] = []
    d = Path(cache_root) / "dk_contest"
    if d.is_dir():
        for p in sorted(d.rglob("*.json")):
            if p.name != "index.json":
                out.append((p, f"raw DK cache {p.parent.name}"))
    if runs_root is not None and Path(runs_root).is_dir():
        for p in sorted(Path(runs_root).glob("*/contests/dk_contest_*.json")):
            out.append((p, f"run {p.parent.parent.name}, saved before lock"))
        for p in sorted(Path(runs_root).glob("*/settle/prize_tables/dk_contest_*.json")):
            out.append((p, f"run {p.parent.parent.parent.name}, settle copy"))
    for e in extra_dirs:
        if Path(e).is_dir():
            for p in sorted(Path(e).rglob("dk_contest_*.json")):
                out.append((p, f"saved from the browser: {p.name}"))
    return out


# -- the match -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TemplateMatch:
    table: TemplateTable
    lobby: LobbyRow
    note: str = ""

    @property
    def contest_id(self) -> int:
        return self.table.contest_id

    def label(self) -> str:
        t = self.table
        return f"table of contest {t.contest_id} ({t.paid:,} paid of {t.max_entries:,}; {t.source})"

    def record(self) -> dict:
        t = self.table
        return {"template_contest_id": t.contest_id, "name": t.name, "max_entries": t.max_entries, "paid_places": t.paid,
                "source": t.source, "tiers_sha256": t.sha256, "template_id": self.lobby.template_id or t.template_id,
                "lobby_snapshot": self.lobby.snapshot, "note": self.note}


@dataclass
class TemplateStore:
    """Lobby rows and candidate tables, loaded once for a run or a settle. `match` is pure over them."""

    lobby: dict[int, LobbyRow] = field(default_factory=dict)
    tables: dict[tuple[str, int], list[TemplateTable]] = field(default_factory=dict)
    refused: list[tuple[str, str]] = field(default_factory=list)

    def match(self, contest_id, *, satellite: bool, fee=None) -> TemplateMatch | str:
        """The template of a contest, or the reason it has none (never an invented table)."""
        row = self.lobby.get(int(contest_id))
        if row is None:
            return "no lobby capture lists this contest, so its template and size are unknown"
        if satellite:
            return "a satellite (tickets) is never priced on a template"
        cands = self.tables.get(row.key)
        if not cands:
            return f"no cached table for {row.key[0]!r} with {row.key[1]:,} max entries"
        entry_fee = _money(fee) if fee is not None else None
        if entry_fee is not None and entry_fee != row.fee:
            return f"the entries file fee {entry_fee} differs from the lobby fee {row.fee}"
        why: list[str] = []
        ok: list[TemplateTable] = []
        for t in cands:
            if t.fee != row.fee:
                why.append(f"{t.contest_id}: fee {t.fee} differs from {row.fee}")
            elif t.total_payouts != row.prize_pool:
                why.append(f"{t.contest_id}: prize pool {t.total_payouts} differs from the lobby's {row.prize_pool}")
            elif row.template_id is not None and t.template_id is not None and row.template_id != t.template_id:
                why.append(f"{t.contest_id}: DraftKings template id {t.template_id} differs from {row.template_id}")
            else:
                ok.append(t)
        if not ok:
            return "same name and size but not the same table (" + "; ".join(why) + ")"
        ok.sort(key=lambda t: (t.start_utc, t.contest_id), reverse=True)  # newest start wins
        best = ok[0]
        notes = []
        if row.template_id is not None and best.template_id == row.template_id:
            notes.append(f"same DraftKings template id {row.template_id}")
        if len({t.sha256 for t in ok}) > 1:
            notes.append(f"{len(ok)} cached tables of this template disagree; the newest start ({best.contest_id}) is used")
        return TemplateMatch(best, row, "; ".join(notes))


def load_store(cache_root: Path, runs_root: Path | None, contest_ids: Iterable[int], *,
               extra_dirs: Iterable[Path] = ()) -> TemplateStore:
    """Read the roots NOW (never an import-time constant: tests point the cache root at an empty folder), find the
    lobby rows of `contest_ids`, then the tables sharing a row's template and the rows of those tables' contests (for
    DraftKings' template id)."""
    tables: dict[int, TemplateTable] = {}
    refused: list[tuple[str, str]] = []
    _scan(table_sources(cache_root, runs_root, extra_dirs), tables, refused)
    rows = lobby_rows(cache_root, contest_ids)
    keys = {r.key for r in rows.values()}
    by_key: dict[tuple[str, int], list[TemplateTable]] = {}
    for t in tables.values():
        if t.key in keys:
            by_key.setdefault(t.key, []).append(t)
    cand_rows = lobby_rows(cache_root, [t.contest_id for ts in by_key.values() for t in ts])
    for k, ts in by_key.items():
        by_key[k] = [replace(t, template_id=cand_rows[t.contest_id].template_id) if t.contest_id in cand_rows else t
                     for t in ts]
    return TemplateStore(rows, by_key, refused)
