"""Cell lock state for late swap and refresh (plan sections 10 and 11, "Fast mode").

A cell's state comes from its current occupant:
  LOCKED     the occupant's game has started (now_utc >= start), or DK reports the occupant
             not swappable (draftables isSwappable false)
  EDIT_STOP  start - buffer <= now_utc < start: the engine's own earlier stop. The cell is
             pinned and the report says "edit stop", never "started".
  OPEN       otherwise, and every blank cell
A cell that cannot be read, or holds an ID that is not in the salary pool, is pinned (LOCKED,
reason "unreadable") and its entry is left unchanged.

Start time = draftables competition start (matched by draftable ID == role ID), else the
salary file's Game Info. Participation and eligibility are NOT lock inputs; they are
exclusion inputs for the re-solve. A player may be ADDED only if his game is outside the
edit-stop window (start - buffer > now) and DK does not report him unswappable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from nhl_dfs.contracts.statuses import CellLock
from nhl_dfs.data.sources.dk_public import Draftables
from nhl_dfs.intake.entries import EntriesFile, cell_role_id, template_permutation
from nhl_dfs.intake.salary import SalaryPool, parse_game_info

REASON_STARTED = "started"
REASON_EDIT_STOP = "edit stop"
REASON_UNSWAPPABLE = "not swappable"
REASON_UNREADABLE = "unreadable"
REASON_OPEN = "open"
REASON_EMPTY = "empty"


@dataclass(frozen=True)
class CellState:
    lock: CellLock
    reason: str  # one of the REASON_* prefixes, then detail
    role_id: str | None  # the occupant, None for a blank
    start_utc: datetime | None = None

    @property
    def pinned(self) -> bool:
        return self.lock is not CellLock.OPEN


@dataclass
class LockState:
    now_utc: datetime
    buffer_s: int
    cells: dict[tuple[str, int], CellState]  # (entry id, canonical slot index)
    started_role_ids: frozenset[str]
    started_games: frozenset[str]  # "AWAY@HOME"
    edit_stop_games: frozenset[str]
    start_utc: dict[str, datetime]  # role id -> start used
    start_source: dict[str, str]  # role id -> "draftables" | "game_info"
    unswappable: frozenset[str]
    unreadable_entries: frozenset[str]
    not_addable: frozenset[str] = field(default_factory=frozenset)  # pool role ids that may not be added

    def pinned(self, entry_id: str) -> dict[int, str]:
        """Canonical slot index -> occupant for every pinned, readable cell of the entry."""
        return {k: c.role_id for (e, k), c in self.cells.items()
                if e == entry_id and c.pinned and c.role_id is not None and not c.reason.startswith(REASON_UNREADABLE)}

    def addable(self, role_id: str) -> bool:
        return role_id not in self.not_addable

    def counts(self) -> dict[str, int]:
        return dict(Counter(c.lock.value for c in self.cells.values()))


def _game_starts(pool: SalaryPool, draftables: Draftables | None) -> tuple[dict[str, datetime], dict[str, str], dict[str, str]]:
    by_id = draftables.by_id if draftables is not None else {}
    starts: dict[str, datetime] = {}
    source: dict[str, str] = {}
    game_of: dict[str, str] = {}
    for r in pool.rows:
        try:
            key, info = parse_game_info(r.game_info)
        except ValueError:
            key, info = r.game_info, None
        game_of[r.role_id] = key
        d = by_id.get(r.role_id)
        if d is not None and d.start_utc is not None:
            starts[r.role_id], source[r.role_id] = d.start_utc.astimezone(timezone.utc), "draftables"
        elif info is not None:
            starts[r.role_id], source[r.role_id] = info.start_utc, "game_info"
    return starts, source, game_of


def compute(entries_current: EntriesFile, pool: SalaryPool, draftables: Draftables | None,
            now_utc: datetime, buffer_s: int) -> LockState:
    now = now_utc.astimezone(timezone.utc)
    buffer = timedelta(seconds=buffer_s)
    starts, source, game_of = _game_starts(pool, draftables)
    unswappable = frozenset(
        d.draftable_id for d in (draftables.rows if draftables is not None else [])
        if d.is_swappable is False and d.draftable_id in pool.by_role_id
    )
    started = frozenset(rid for rid, t in starts.items() if now >= t)
    edit_stop = frozenset(rid for rid, t in starts.items() if t - buffer <= now < t)
    started_games = frozenset(game_of[r] for r in started)
    edit_stop_games = frozenset(game_of[r] for r in edit_stop) - started_games

    perm = template_permutation(entries_current.roster_labels, entries_current.mode)
    cells: dict[tuple[str, int], CellState] = {}
    unreadable: set[str] = set()
    for e in entries_current.entries:
        for col, text in enumerate(e.cells):
            k = perm[col]
            try:
                rid = cell_role_id(text)
            except ValueError:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_UNREADABLE}: cell {text!r} kept as is", None)
                unreadable.add(e.entry_id)
                continue
            if rid is None:
                cells[(e.entry_id, k)] = CellState(CellLock.OPEN, REASON_EMPTY, None)
                continue
            if rid not in pool.by_role_id:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_UNREADABLE}: ID {rid} is not in the salary pool", rid)
                unreadable.add(e.entry_id)
                continue
            t = starts.get(rid)
            if rid in started:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_STARTED}: game started {t.isoformat()}", rid, t)
            elif rid in unswappable:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_UNSWAPPABLE}: DK reports isSwappable false", rid, t)
            elif rid in edit_stop:
                cells[(e.entry_id, k)] = CellState(
                    CellLock.EDIT_STOP, f"{REASON_EDIT_STOP}: game starts {t.isoformat()}, inside the {buffer_s}s buffer", rid, t)
            else:
                cells[(e.entry_id, k)] = CellState(CellLock.OPEN, REASON_OPEN, rid, t)

    no_start = frozenset(r.role_id for r in pool.rows if r.role_id not in starts)  # unknown start: never added
    return LockState(
        now_utc=now,
        buffer_s=buffer_s,
        cells=cells,
        started_role_ids=started,
        started_games=started_games,
        edit_stop_games=edit_stop_games,
        start_utc=starts,
        start_source=source,
        unswappable=unswappable,
        unreadable_entries=frozenset(unreadable),
        not_addable=started | edit_stop | unswappable | no_start,
    )
