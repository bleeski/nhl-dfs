"""Baseline assignment of candidate lineups to reserved entries (C2b).

Entries are filled in file order, each with the best-objective candidate that satisfies the
preferences. When none does, preferences are relaxed in the plan's order (section 10, ladder
step 5) and each relaxation is recorded per entry:
  OVERLAP   Classic pairwise overlap cap (classic_max_overlap people) dropped
  EXPOSURE  person and captain caps dropped
  REPEAT    last resort: the best legal lineup is repeated
Legality is never relaxed: every candidate is already a checked legal lineup.

UTIL late-swap rule (Classic): inside a lineup, UTIL holds the latest-starting skater that a
legal slot swap allows (objective-neutral). Across candidates, one within util_tie_band_points
of the best allowed candidate is preferred when its UTIL starts later.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, Mode, check_lineup, lineup_key, slot_accepts
from nhl_dfs.intake.salary import SalaryPool

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPOSURE_YAML = REPO_ROOT / "config" / "exposure.yaml"
_UTIL = CLASSIC_SLOTS.index("UTIL")
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Caps:
    person_max_share: float = 0.6
    captain_max_share: float = 0.4
    classic_max_overlap: int = 7
    util_tie_band_points: float = 0.25

    def person_cap(self, n_entries: int) -> int:
        """Count cap with the feasibility floor: never below 1."""
        return max(1, math.ceil(self.person_max_share * n_entries))

    def captain_cap(self, n_entries: int) -> int:
        return max(1, math.ceil(self.captain_max_share * n_entries))


def load_caps(path: Path = EXPOSURE_YAML) -> Caps:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    caps = Caps(
        person_max_share=float(cfg["person_max_share"]),
        captain_max_share=float(cfg["captain_max_share"]),
        classic_max_overlap=int(cfg["classic_max_overlap"]),
        util_tie_band_points=float(cfg["util_tie_band_points"]),
    )
    for share in (caps.person_max_share, caps.captain_max_share):
        if not 0.0 < share <= 1.0:
            raise ValueError(f"exposure shares must be in (0, 1], got {share}")
    return caps


@dataclass(frozen=True)
class Relaxation:
    entry_id: str
    kind: str  # "OVERLAP" | "EXPOSURE" | "REPEAT"
    detail: str


@dataclass
class Assignment:
    by_entry: dict[str, tuple[str, ...]]  # entry id -> role ids, canonical slot order
    exposures: dict[str, int]  # lineup key -> entries holding it
    person_exposures: dict[str, int]  # person_key -> entries (any slot)
    captain_exposures: dict[str, int]  # person_key -> entries as Showdown Captain
    overlap_max: int  # most people shared by any two entries
    relaxations: list[Relaxation] = field(default_factory=list)


def arrange_util(role_ids: Sequence[str], pool: SalaryPool, starts: Mapping[str, datetime] | None) -> tuple[str, ...]:
    """Classic: move the latest-starting skater a legal swap allows into UTIL. Same players."""
    ids = list(role_ids)
    if pool.mode is not Mode.CLASSIC or not starts:
        return tuple(ids)
    rows = [pool.by_role_id[r] for r in ids]
    u = rows[_UTIL]
    best_k, best_t = None, starts.get(u.role_id, _EARLIEST)
    for k, s in enumerate(rows):
        if k == _UTIL or s.is_goalie:
            continue
        t = starts.get(s.role_id, _EARLIEST)
        if t > best_t and slot_accepts("UTIL", s, Mode.CLASSIC) and slot_accepts(CLASSIC_SLOTS[k], u, Mode.CLASSIC):
            best_k, best_t = k, t
    if best_k is None:
        return tuple(ids)
    ids[_UTIL], ids[best_k] = ids[best_k], ids[_UTIL]
    if not check_lineup([pool.by_role_id[r] for r in ids], Mode.CLASSIC).ok:  # never trade legality
        return tuple(role_ids)
    return tuple(ids)


def _entry_ids(entries) -> list[str]:
    items = getattr(entries, "entries", entries)
    return [e if isinstance(e, str) else e.entry_id for e in items]


def assign(
    candidates: Sequence[Candidate],
    entries,
    pool: SalaryPool,
    mode: Mode,
    caps: Caps,
    *,
    seed: int,
    later_start_utc: Mapping[str, datetime] | None = None,
    fixed: Mapping[str, Sequence[str]] | None = None,
) -> Assignment:
    """Fill every entry. `entries` is an EntriesFile, EntryRows, or entry IDs (file order).

    later_start_utc: role_id -> game start (UTC) for the UTIL rule. fixed: entries already
    decided (Phase B keeps unaffected entries); they count toward caps and overlap and are
    returned unchanged. seed only breaks exact objective ties.
    """
    if not candidates:
        raise ValueError("empty candidate bank; route to feasible.find_one")
    fixed = {k: tuple(v) for k, v in (fixed or {}).items()}
    todo = [e for e in _entry_ids(entries) if e not in fixed]
    n_total = len(todo) + len(fixed)
    person_cap, captain_cap = caps.person_cap(n_total), caps.captain_cap(n_total)

    rng = random.Random(seed)
    tiebreak = {c.key: rng.random() for c in candidates}
    ordered = sorted(candidates, key=lambda c: (-c.objective_value, tiebreak[c.key]))
    arranged = {c.key: arrange_util(c.role_ids, pool, later_start_utc) for c in ordered}

    persons_of = {c.key: {pool.by_role_id[r].person_key for r in c.role_ids} for c in ordered}
    captain_of = {c.key: pool.by_role_id[c.role_ids[0]].person_key if mode is Mode.SHOWDOWN else None for c in ordered}

    person_n: Counter[str] = Counter()
    captain_n: Counter[str] = Counter()
    key_n: Counter[str] = Counter()
    placed: list[set[str]] = []

    def account(role_ids: Sequence[str]) -> None:
        rows = [pool.by_role_id[r] for r in role_ids]
        ps = {r.person_key for r in rows}
        person_n.update(ps)
        if mode is Mode.SHOWDOWN:
            captain_n[rows[0].person_key] += 1
        key_n[lineup_key(rows, mode)] += 1
        placed.append(ps)

    for lineup in fixed.values():
        account(lineup)

    def util_start(c: Candidate) -> datetime:
        if mode is not Mode.CLASSIC or not later_start_utc:
            return _EARLIEST
        return later_start_utc.get(arranged[c.key][_UTIL], _EARLIEST)

    def passes(c: Candidate, level: int) -> bool:
        if key_n[c.key]:
            return False
        if level < 2:
            if any(person_n[p] >= person_cap for p in persons_of[c.key]):
                return False
            cap_p = captain_of[c.key]
            if cap_p is not None and captain_n[cap_p] >= captain_cap:
                return False
        if level < 1 and mode is Mode.CLASSIC:
            ps = persons_of[c.key]
            if any(len(ps & other) > caps.classic_max_overlap for other in placed):
                return False
        return True

    by_entry: dict[str, tuple[str, ...]] = dict(fixed)
    relaxations: list[Relaxation] = []
    for eid in todo:
        choice, level = None, 0
        for level in (0, 1, 2):
            for i, c in enumerate(ordered):
                if not passes(c, level):
                    continue
                choice = c
                # UTIL tie band: a later UTIL start within the band beats a sliver of objective.
                for d in ordered[i + 1:]:
                    if d.objective_value < c.objective_value - caps.util_tie_band_points:
                        break
                    if util_start(d) > util_start(choice) and passes(d, level):
                        choice = d
                break
            if choice is not None:
                break
        if choice is None:
            choice, level = ordered[0], 3
        if level == 1:
            relaxations.append(Relaxation(eid, "OVERLAP", f"no candidate within {caps.classic_max_overlap} shared people"))
        elif level == 2:
            relaxations.append(Relaxation(eid, "EXPOSURE", f"person cap {person_cap} / captain cap {captain_cap} dropped"))
        elif level == 3:
            relaxations.append(Relaxation(eid, "REPEAT", f"bank of {len(ordered)} exhausted; best legal lineup repeated"))
        lineup = arranged[choice.key]
        by_entry[eid] = lineup
        account(lineup)

    overlap_max = max((len(a & b) for i, a in enumerate(placed) for b in placed[i + 1:]), default=0)
    ordered_by_entry = {e: by_entry[e] for e in _entry_ids(entries)}
    return Assignment(
        by_entry=ordered_by_entry,
        exposures=dict(key_n),
        person_exposures=dict(person_n),
        captain_exposures=dict(captain_n),
        overlap_max=overlap_max,
        relaxations=relaxations,
    )
