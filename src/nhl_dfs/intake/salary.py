"""Read a DKSalaries.csv into a SalaryPool. The file bytes are authoritative:
nothing here rewrites an ID, salary, position, or AvgPointsPerGame value.

Salaries are integer dollars. Game start times are parsed from DK's
Eastern-time text and stored as UTC.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import normalize_name, person_key

REQUIRED_COLUMNS = (
    "Position",
    "Name + ID",
    "Name",
    "ID",
    "Roster Position",
    "Salary",
    "Game Info",
    "TeamAbbrev",
    "AvgPointsPerGame",
)
SHOWDOWN_ROSTER_POSITIONS = {"CPT", "FLEX"}

_EASTERN = ZoneInfo("America/New_York")
_GAME_INFO_RE = re.compile(r"^([A-Z]+)@([A-Z]+) (\d{2}/\d{2}/\d{4} \d{2}:\d{2}[AP]M) ET$")


@dataclass(frozen=True)
class GameInfo:
    home: str
    away: str
    start_et_text: str
    start_utc: datetime


@dataclass(frozen=True)
class PersonRows:
    cpt: PoolRow | None = None
    flex: PoolRow | None = None
    classic: PoolRow | None = None


@dataclass(frozen=True)
class Conflict:
    """kind: DUPLICATE_ROLE and PERSON_KEY_COLLISION exclude their rows from
    selection; SAME_NAME, UNPAIRED, CPT_SALARY_RATIO, CPT_APPG_MISMATCH are
    reported only."""

    kind: str
    role_ids: tuple[str, ...]
    detail: str
    excluded: bool


@dataclass
class SalaryPool:
    mode: Mode
    rows: list[PoolRow]  # selectable rows only, file order
    by_role_id: dict[str, PoolRow]
    persons: dict[str, PersonRows]
    teams: frozenset[str]
    games: dict[str, GameInfo]  # keyed by "AWAY@HOME"
    conflicts: list[Conflict]
    sha256: str
    raw: bytes

    @property
    def excluded_role_ids(self) -> frozenset[str]:
        return frozenset(rid for c in self.conflicts if c.excluded for rid in c.role_ids)


def parse_game_info(text: str) -> tuple[str, GameInfo]:
    m = _GAME_INFO_RE.match(text.strip())
    if not m:
        raise ValueError(f"unrecognized Game Info: {text!r}")
    away, home, et_text = m.group(1), m.group(2), m.group(3)
    local = datetime.strptime(et_text, "%m/%d/%Y %I:%M%p").replace(tzinfo=_EASTERN)
    return f"{away}@{home}", GameInfo(home, away, f"{et_text} ET", local.astimezone(timezone.utc))


def _parse_appg(text: str) -> tuple[float | None, str]:
    t = text.strip()
    if t == "":
        return None, "MISSING"
    value = float(t)
    if value == 0.0:
        return 0.0, "APPG_ZERO"
    return value, "VALUE"


def read_salary(path) -> SalaryPool:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig")
    records = list(csv.reader(io.StringIO(text, newline="")))
    if not records:
        raise ValueError(f"{path}: empty salary file")
    header = records[0]
    col: dict[str, int] = {}
    for name in REQUIRED_COLUMNS:
        hits = [i for i, h in enumerate(header) if h == name]
        if len(hits) != 1:
            raise ValueError(f"{path}: required column {name!r} found {len(hits)} times")
        col[name] = hits[0]

    parsed: list[PoolRow] = []
    seen_ids: set[str] = set()
    for line_no, rec in enumerate(records[1:], start=2):
        if not any(cell.strip() for cell in rec):
            continue
        get = lambda name: rec[col[name]] if col[name] < len(rec) else ""  # noqa: E731
        role_id = get("ID").strip()
        if not role_id.isdigit():
            raise ValueError(f"{path}:{line_no}: non-numeric ID {role_id!r}")
        if role_id in seen_ids:
            raise ValueError(f"{path}:{line_no}: duplicate ID {role_id}")
        seen_ids.add(role_id)
        name, team, position = get("Name"), get("TeamAbbrev"), get("Position")
        try:
            pkey = person_key(name, team, position)
            salary = int(get("Salary"))
            appg_raw, appg_flag = _parse_appg(get("AvgPointsPerGame"))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
        parsed.append(
            PoolRow(
                role_id=role_id,
                person_key=pkey,
                name=name,
                team=team,
                position=position,
                roster_positions=frozenset(p.strip() for p in get("Roster Position").split("/")),
                salary=salary,
                game_info=get("Game Info"),
                appg_raw=appg_raw,
                appg_flag=appg_flag,
            )
        )

    showdown_flags = {bool(r.roster_positions & SHOWDOWN_ROSTER_POSITIONS) for r in parsed}
    if showdown_flags == {True, False}:
        raise ValueError(f"{path}: mixes CPT/FLEX rows with Classic rows")
    mode = Mode.SHOWDOWN if showdown_flags == {True} else Mode.CLASSIC

    games: dict[str, GameInfo] = {}
    for r in parsed:
        key, info = parse_game_info(r.game_info)
        games.setdefault(key, info)

    conflicts = _find_conflicts(parsed, mode)
    excluded = {rid for c in conflicts if c.excluded for rid in c.role_ids}
    rows = [r for r in parsed if r.role_id not in excluded]

    persons: dict[str, PersonRows] = {}
    for r in rows:
        current = persons.get(r.person_key, PersonRows())
        if mode is Mode.CLASSIC:
            current = PersonRows(classic=r)
        elif "CPT" in r.roster_positions:
            current = PersonRows(cpt=r, flex=current.flex)
        else:
            current = PersonRows(cpt=current.cpt, flex=r)
        persons[r.person_key] = current

    if mode is Mode.SHOWDOWN:
        conflicts.extend(_showdown_pair_reports(persons))
    conflicts.extend(_same_name_reports(persons))

    return SalaryPool(
        mode=mode,
        rows=rows,
        by_role_id={r.role_id: r for r in rows},
        persons=persons,
        teams=frozenset(r.team for r in rows),
        games=games,
        conflicts=conflicts,
        sha256=hashlib.sha256(raw).hexdigest(),
        raw=raw,
    )


def _find_conflicts(rows: list[PoolRow], mode: Mode) -> list[Conflict]:
    conflicts: list[Conflict] = []
    by_triple: dict[tuple[str, str, str], list[PoolRow]] = defaultdict(list)
    for r in rows:
        by_triple[(r.name, r.team, r.position)].append(r)

    overflowed: set[str] = set()
    for (name, team, position), group in by_triple.items():
        if mode is Mode.CLASSIC:
            over = len(group) > 1
        else:
            cpt = sum(1 for r in group if "CPT" in r.roster_positions)
            over = cpt > 1 or (len(group) - cpt) > 1
        if over:
            ids = tuple(r.role_id for r in group)
            overflowed.update(ids)
            conflicts.append(
                Conflict("DUPLICATE_ROLE", ids, f"{name} {team} {position}: {len(group)} rows", True)
            )

    by_key: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for triple, group in by_triple.items():
        for r in group:
            if r.role_id not in overflowed:
                by_key[r.person_key].add(triple)
    for key, triples in by_key.items():
        if len(triples) > 1:
            ids = tuple(r.role_id for t in sorted(triples) for r in by_triple[t])
            conflicts.append(
                Conflict("PERSON_KEY_COLLISION", ids, f"{key}: {sorted(triples)}", True)
            )
    return conflicts


def _showdown_pair_reports(persons: dict[str, PersonRows]) -> list[Conflict]:
    out: list[Conflict] = []
    for key, p in persons.items():
        if p.cpt is None or p.flex is None:
            present = tuple(r.role_id for r in (p.cpt, p.flex) if r is not None)
            out.append(Conflict("UNPAIRED", present, f"{key}: missing CPT or FLEX row", False))
            continue
        if p.cpt.salary * 2 != p.flex.salary * 3:
            out.append(
                Conflict(
                    "CPT_SALARY_RATIO",
                    (p.cpt.role_id, p.flex.role_id),
                    f"{key}: CPT {p.cpt.salary} vs FLEX {p.flex.salary}",
                    False,
                )
            )
        if (p.cpt.appg_raw, p.cpt.appg_flag) != (p.flex.appg_raw, p.flex.appg_flag):
            out.append(
                Conflict(
                    "CPT_APPG_MISMATCH",
                    (p.cpt.role_id, p.flex.role_id),
                    f"{key}: CPT {p.cpt.appg_raw} vs FLEX {p.flex.appg_raw}",
                    False,
                )
            )
    return out


def _same_name_reports(persons: dict[str, PersonRows]) -> list[Conflict]:
    by_name: dict[str, list[str]] = defaultdict(list)
    for key, p in persons.items():
        any_row = p.classic or p.cpt or p.flex
        by_name[normalize_name(any_row.name)].append(key)
    out: list[Conflict] = []
    for name, keys in by_name.items():
        if len(keys) > 1:
            ids = tuple(
                r.role_id for k in sorted(keys) for r in (persons[k].classic, persons[k].cpt, persons[k].flex) if r
            )
            out.append(Conflict("SAME_NAME", ids, f"{name}: distinct persons {sorted(keys)}", False))
    return out
