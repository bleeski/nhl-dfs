"""Cell lock state for late swap and refresh (plan sections 10 and 11, "Fast mode").

A cell's state comes from its current occupant:
  LOCKED     the occupant's game has started (now_utc >= start), or DK reports the occupant
             not swappable (draftables isSwappable false)
  EDIT_STOP  start - buffer <= now_utc < start: the engine's own earlier stop. The cell is
             pinned and the report says "edit stop", never "started".
  OPEN       otherwise, and every blank cell
A cell that cannot be read, or holds an ID that is not in the salary pool, is pinned (LOCKED,
reason "unreadable") and its entry is left unchanged.
A cell whose occupant is a started-game row (the salary file's In-Progress marker, C15) is LOCKED "started" by
the marker alone: no start time is needed, and the row having left the selectable pool does not make the entry
unreadable.
A cell DraftKings writes as "Name (ID) (LOCKED)" (its post-lock export, C53 flag 82) is LOCKED "started" by that
marker whatever the clock says, and every row of that player's game is not addable; so is every row of a game the
entries file's embedded player list marks In Progress. The cell is pinned, so it is written back byte for byte.

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
from nhl_dfs.intake.entries import EntriesFile, cell_role_id, embedded_started_ids, parse_cell, template_permutation
from nhl_dfs.intake.salary import SalaryPool, parse_game_info, row_start

REASON_STARTED = "started"
REASON_EDIT_STOP = "edit stop"
REASON_UNSWAPPABLE = "not swappable"
REASON_UNREADABLE = "unreadable"
REASON_OPEN = "open"
REASON_EMPTY = "empty"
LOCK_SOON_S = 1800  # C53 (flag 84, B119): a delivery whose changed cells touch a game starting this soon says so


@dataclass(frozen=True)
class CellState:
    lock: CellLock
    reason: str  # one of the REASON_* prefixes, then detail
    role_id: str | None  # the occupant, None for a blank
    start_utc: datetime | None = None
    dk_locked: bool = False  # the cell carries DraftKings' " (LOCKED)" marker (C53)

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
    marker = pool.started_by_role_id  # a pool that carries its started rows (with_started_rows): the marker decides, below
    for r in pool.rows:
        if r.role_id in marker:
            continue
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


def dk_lock_ids(entries: EntriesFile) -> set[str]:
    """C53 (flag 82): the role IDs DraftKings itself shows as started in an entries file: every roster cell written
    "Name (ID) (LOCKED)" and every embedded-list row whose Game Info reads In Progress."""
    ids = set(embedded_started_ids(entries))
    for e in entries.entries:
        for text in e.cells:
            try:
                rid, marked = parse_cell(text)
            except ValueError:
                continue
            if marked and rid is not None:
                ids.add(rid)
    return ids


def dk_started_games(entries: EntriesFile, pool: SalaryPool) -> frozenset[str]:
    """The AWAY@HOME keys of the selectable rows `dk_lock_ids` names (a row already started has left pool.rows)."""
    out = set()
    for rid in dk_lock_ids(entries):
        row = pool.by_role_id.get(rid)
        if row is None:
            continue
        try:
            out.add(parse_game_info(row.game_info)[0])
        except ValueError:
            continue
    return frozenset(out)


def compute(entries_current: EntriesFile, pool: SalaryPool, draftables: Draftables | None,
            now_utc: datetime, buffer_s: int) -> LockState:
    now = now_utc.astimezone(timezone.utc)
    buffer = timedelta(seconds=buffer_s)
    starts, source, game_of = _game_starts(pool, draftables)
    unswappable = frozenset(
        d.draftable_id for d in (draftables.rows if draftables is not None else [])
        if d.is_swappable is False and d.draftable_id in pool.by_role_id
    )
    marker = pool.started_by_role_id  # started by DK's marker (or by an earlier clock reduction), not by this clock
    # C53 (flag 82): DraftKings' own lock evidence in the entries file, a LOCKED cell or an In Progress row of the
    # embedded list, starts that player's whole game whatever the clock says
    dk_ids = dk_lock_ids(entries_current)
    dk_games = {game_of[r] for r in dk_ids if r in game_of}
    dk_started = frozenset(r for r, g in game_of.items() if g in dk_games) | frozenset(r for r in dk_ids if r in pool.by_role_id)
    started = frozenset(rid for rid, t in starts.items() if now >= t) | frozenset(marker) | dk_started
    edit_stop = frozenset(rid for rid, t in starts.items() if t - buffer <= now < t) - started
    started_games = frozenset(game_of[r] for r in started if r in game_of) | frozenset(_marker_games(pool))
    edit_stop_games = frozenset(game_of[r] for r in edit_stop) - started_games

    perm = template_permutation(entries_current.roster_labels, entries_current.mode)
    cells: dict[tuple[str, int], CellState] = {}
    unreadable: set[str] = set()
    for e in entries_current.entries:
        for col, text in enumerate(e.cells):
            k = perm[col]
            try:
                rid, marked = parse_cell(text)
            except ValueError:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_UNREADABLE}: cell {text!r} kept as is", None)
                unreadable.add(e.entry_id)
                continue
            if rid is None:
                cells[(e.entry_id, k)] = CellState(CellLock.OPEN, REASON_EMPTY, None)
                continue
            if rid in marker:
                row = marker[rid]
                cells[(e.entry_id, k)] = CellState(
                    CellLock.LOCKED, f"{REASON_STARTED}: {row.team} game is in progress (DraftKings marker)", rid, row_start(row),
                    marked)
                continue
            if rid not in pool.by_role_id:
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_UNREADABLE}: ID {rid} is not in the salary pool", rid,
                                                   None, marked)
                unreadable.add(e.entry_id)
                continue
            t = starts.get(rid)
            if marked:  # its own branch: DraftKings' lock stands whatever the clock or the start time says
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_STARTED}: DraftKings marks the cell LOCKED", rid, t,
                                                   True)
            elif rid in started:
                why = f"game started {t.isoformat()}" if t is not None else "DraftKings marks the game In Progress"
                cells[(e.entry_id, k)] = CellState(CellLock.LOCKED, f"{REASON_STARTED}: {why}", rid, t)
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


def locking_soon(reference: EntriesFile, proposed: EntriesFile, pool: SalaryPool, now_utc: datetime,
                 window_s: int = LOCK_SOON_S) -> list[tuple[str, datetime]]:
    """C53 (flag 84): (AWAY@HOME, start) of every game whose player is removed or added by a cell `proposed` changes
    against `reference` (same entry and column, a different role id) and that starts after now_utc and within
    window_s, soonest first. Start times are the salary file's Game Info; a started game has none and is a crossing
    (publish_crossings), not a warning."""
    now = now_utc.astimezone(timezone.utc)
    horizon = now + timedelta(seconds=window_s)
    ref_by_id = {e.entry_id: e for e in reference.entries}
    games: dict[str, datetime] = {}
    for e in proposed.entries:
        ref = ref_by_id.get(e.entry_id)
        if ref is None:
            continue
        for a, b in zip(ref.cells, e.cells):
            try:
                ids = (cell_role_id(a), cell_role_id(b))
            except ValueError:
                continue
            if ids[0] == ids[1]:
                continue
            for rid in ids:
                row = pool.by_role_id.get(rid) if rid else None
                if row is None:
                    continue
                try:
                    key, info = parse_game_info(row.game_info)
                except ValueError:
                    continue
                if now < info.start_utc <= horizon:
                    games[key] = info.start_utc
    return sorted(games.items(), key=lambda kv: (kv[1], kv[0]))


def _marker_games(pool: SalaryPool) -> set[str]:
    """Names of the games the pool's started rows belong to: the game key where the row still has its Game Info, else
    the team ("BOS (in progress)"): DK's marker names no opponent, so no AWAY@HOME is written for it."""
    out: set[str] = set()
    for r in pool.started_rows:
        try:
            out.add(parse_game_info(r.game_info)[0])
        except ValueError:
            out.add(f"{r.team} (in progress)")
    return out


@dataclass(frozen=True)
class Crossings:
    """Changed cells a lock boundary forbids a new version to contain: one line per cell, by the state of the game of
    the player removed or added (see publish_crossings)."""

    started: tuple[str, ...] = ()
    edit_stop: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.started or self.edit_stop)


def publish_crossings(reference: EntriesFile, proposed: EntriesFile, pool: SalaryPool, now_utc: datetime,
                      buffer_s: int) -> Crossings:
    """B53: the cells `proposed` changes against `reference` (the predecessor version, or the uploaded entries file when
    there is none) that touch a game which has started or is inside the edit stop at `now_utc`: the player removed or
    the player added. A cell left as it was is never reported, so a started game's lineup cells may stay in a new
    version and only changing them, or adding a player from such a game, is a crossing (the pinned-cell diff late
    swap makes before it writes). Unlike late swap's `not_addable` this does not treat a row with no known start time
    as unaddable and ignores DK swappability: an initial run's salary file can carry neither and must still publish."""
    ls = compute(reference, pool, None, now_utc, buffer_s)
    buffer = timedelta(seconds=buffer_s)
    in_edit_stop = frozenset(rid for rid, t in ls.start_utc.items() if t - buffer <= ls.now_utc < t)
    perm = template_permutation(reference.roster_labels, reference.mode)
    proposed_by_id = {e.entry_id: e for e in proposed.entries}

    def role_of(text: str) -> str | None:
        try:
            return cell_role_id(text)
        except ValueError:
            return None  # an unreadable old cell is not a started game's cell

    started: list[str] = []
    edit_stop: list[str] = []
    for e in reference.entries:
        new_entry = proposed_by_id.get(e.entry_id)
        if new_entry is None:
            continue
        for col, old_text in enumerate(e.cells):
            old, new = role_of(old_text), role_of(new_entry.cells[col])
            if old == new:
                continue
            for rid, verb in ((old, "replaces"), (new, "adds")):
                row = (pool.by_role_id.get(rid) or pool.started_by_role_id.get(rid)) if rid is not None else None
                if row is None:
                    continue
                line = f"entry {e.entry_id} slot {perm[col]}: {verb} {row.name} ({row.team})"
                if rid in ls.started_role_ids:
                    started.append(line)
                elif rid in in_edit_stop:
                    edit_stop.append(line)
    return Crossings(tuple(started), tuple(edit_stop))
