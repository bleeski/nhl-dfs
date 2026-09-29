"""Optional current-score conditioning for late swap (C9; plan section 11, "Fast mode").

No reliable standings snapshot source exists yet (C11 is blocked on DK standings exports), so the
normal result is a no-op: the scenarios come back unchanged with LIVE_STATUS=NO_SNAPSHOT (or
UNRELIABLE with the reason), every entry's state is UNKNOWN, and nothing may pivot on a slow start.

A snapshot is RELIABLE only when all three hold: its source is declared, it is no older than
live.max_age_min (config/runtime.yaml) at `now`, and it covers every one of the user's entries being
re-solved. Then:
- a person in a game with progress f (fraction of regulation played, 1.0 = final) scores his observed
  points plus his scenario draw scaled by the time left, observed + round(draw x (1 - f)); a final
  game's score is the observation alone. An approximation of "remaining upside": the draw is not
  re-conditioned on the game state (recorded);
- each entry is TRAILING when its current rank is outside its contest's cash line, else AHEAD. A
  TRAILING tournament entry's tie band prefers lower duplication (policy dup_first); an AHEAD one's prefers
  the higher metric (mean). Cash and satellite keep their own policy (they ignore ownership, C8). Outside
  the band the family objective still decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from nhl_dfs.build import objectives as ob

NO_SNAPSHOT = "NO_SNAPSHOT"
UNRELIABLE = "UNRELIABLE"
CONDITIONED = "CONDITIONED"


@dataclass(frozen=True)
class EntryStanding:
    entry_id: str
    contest_id: str
    points_tenths: int
    rank: int


@dataclass
class StandingsSnapshot:
    as_of_utc: datetime
    source: str  # declared origin (for example a DK standings export path); empty means undeclared
    entries: dict[str, EntryStanding] = field(default_factory=dict)
    players: dict[str, int] = field(default_factory=dict)  # role_id -> base points so far, tenths (no Captain multiple)
    game_progress: dict[str, float] = field(default_factory=dict)  # "AWAY@HOME" -> fraction of regulation played


@dataclass
class Conditioned:
    scenarios: ob.ScenarioSet
    status: str
    entry_state: dict[str, str]
    notes: list[str] = field(default_factory=list)

    @property
    def conditioned(self) -> bool:
        return self.status == CONDITIONED

    def record(self) -> dict:
        return {"LIVE_STATUS": self.status, "entry_state": dict(self.entry_state), "notes": list(self.notes)}


def reliability(snapshot: StandingsSnapshot | None, *, now: datetime | None, needed_entries=None,
                max_age_min: float = 15.0) -> tuple[str, str]:
    """(status, reason): NO_SNAPSHOT, UNRELIABLE or CONDITIONED (reliable)."""
    if snapshot is None:
        return NO_SNAPSHOT, "no standings snapshot supplied; the original objective is used and no chase pivot is made"
    reasons = []
    if not (snapshot.source or "").strip():
        reasons.append("source not declared")
    if now is None:
        reasons.append("no clock to age it against")
    else:
        age = (now.astimezone(timezone.utc) - snapshot.as_of_utc.astimezone(timezone.utc)).total_seconds() / 60.0
        if age > max_age_min or age < -1.0:
            reasons.append(f"{age:.0f} min old (limit {max_age_min:g})")
    missing = sorted(set(needed_entries or ()) - set(snapshot.entries))
    if missing:
        reasons.append(f"{len(missing)} of the user's entries missing from it")
    if reasons:
        return UNRELIABLE, "standings snapshot not reliable (" + "; ".join(reasons) + "); treated as absent"
    return CONDITIONED, f"standings snapshot from {snapshot.source} as of {snapshot.as_of_utc:%H:%MZ}"


def condition(scenarios: ob.ScenarioSet, standings_snapshot: StandingsSnapshot | None, *, pool=None, contests=None,
              now: datetime | None = None, needed_entries=None, max_age_min: float = 15.0) -> Conditioned:
    """Restrict remaining upside to unplayed time and current scores when a reliable snapshot exists;
    otherwise return the scenarios unchanged with the status that says why."""
    status, reason = reliability(standings_snapshot, now=now, needed_entries=needed_entries, max_age_min=max_age_min)
    if status != CONDITIONED:
        return Conditioned(scenarios, status, {}, [f"LIVE_STATUS={status}: {reason}"])
    snap = standings_snapshot
    notes = [f"LIVE_STATUS={status}: {reason}"]
    game_of = {}
    if pool is not None:
        for key, g in pool.games.items():
            game_of[g.home] = key
            game_of[g.away] = key
    base = scenarios.base.copy()
    touched, unobserved = 0, 0
    for i, r in enumerate(scenarios.role_ids):
        row = pool.by_role_id.get(r) if pool is not None else None
        g = game_of.get(row.team) if row is not None else None
        f = float(snap.game_progress.get(g, 0.0)) if g else 0.0
        if f <= 0.0:
            continue
        f = min(1.0, f)
        obs = snap.players.get(r)
        if obs is None:
            unobserved += 1
            obs = 0
        base[:, i] = int(obs) + np.rint(base[:, i] * (1.0 - f)).astype(np.int32)
        touched += 1
    notes.append(f"{touched} role row(s) in started games conditioned on observed points"
                 + (f"; {unobserved} had no observation and count 0 so far" if unobserved else ""))
    states = {}
    for eid, es in snap.entries.items():
        ct = (contests or {}).get(str(es.contest_id))
        if ct is None:
            states[eid] = "UNKNOWN"
            continue
        states[eid] = "TRAILING" if es.rank > max(1, ct.cash_line) else "AHEAD"
    return Conditioned(ob.ScenarioSet(scenarios.role_ids, base, scenarios.purpose, scenarios.seed,
                                      list(scenarios.notes) + ["conditioned on a standings snapshot"]),
                       status, states, notes)
