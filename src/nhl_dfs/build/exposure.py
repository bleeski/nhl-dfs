"""Mode-aware exposure caps and fee concentration (C8, plan section 7 "Portfolio policies").

Count caps apply to TOURNAMENT entries only (cash entries need no diversity rule). With at least
`tournament.min_entries` (20) tournament entries they start at the plan's defaults (Classic person
45%, goalie 35%; Showdown person 60%, Captain 25%) as floor(cap x entries), then a binding cap is
raised to its feasibility floor and the raise is recorded:
    goalie   >= ceil(entries / usable goalies)
    captain  >= ceil(entries / usable Captains)
    person   >= ceil(roster size x entries / usable persons)
Below that, a small portfolio is chosen deliberately, not by rounded percentages: no person or
goalie cap (two lineups may share a star or a goalie), and in Showdown different viable Captains
are preferred (Captain cap 1 for up to three entries when enough viable Captains exist).

Fee concentration (the risk budget, config/risk.yaml): the share of ALL slate fees in entries that
use one goalie, whose primary game is one game (only when the slate has more than one game), or
whose Showdown Captain is one person. A budget below what the entries can achieve is compared
against max(budget, achievable floor); the floor is computed by longest-processing-time packing
of the fees and recorded.

A lineup's primary game is the game holding the largest share of its scenario mean points.
The shared failure scenario (reported, plan section 7): for each team, the scenarios where its
skaters' summed DK points fall in their bottom quintile; the fee share of entries that cash
nothing there, against the same share in all scenarios.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import yaml

from nhl_dfs.contracts.geometry import Mode

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPOSURE_YAML = REPO_ROOT / "config" / "exposure.yaml"


def load_exposure_config(path: Path = EXPOSURE_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Caps:
    n_entries: int
    n_tournament: int
    person: int | None  # max tournament entries holding one person (any slot); None = no cap
    goalie: int | None  # Classic goalie slot
    captain: int | None  # Showdown Captain
    max_overlap: int | None  # Classic: most people two tournament entries may share
    goalie_fee_share: float | None  # effective budget = max(budget, floor)
    game_fee_share: float | None  # None when the slate has one game
    captain_fee_share: float | None
    floors: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def record(self) -> dict:
        return {k: getattr(self, k) for k in ("n_entries", "n_tournament", "person", "goalie", "captain", "max_overlap",
                                              "goalie_fee_share", "game_fee_share", "captain_fee_share", "floors")} | \
            {"notes": list(self.notes)}


def usable_goalies(proj, pool, rel: float = 0.5) -> int:
    """Goalies in the pool whose start probability is at least `rel` x their team's highest. Relative to the
    team, because p_start is split across three to five camp goalies per team (an absolute 0.5 cut left 3
    usable goalies on the real 8-team 2026-09-29 slate and raised the goalie cap on a false floor)."""
    persons = getattr(proj, "persons", {})
    in_pool = {r.person_key: r.team for r in pool.rows if r.is_goalie}
    by: dict[str, list[float]] = defaultdict(list)
    for k, team in in_pool.items():
        p = persons.get(k)
        by[team].append(float(p.goalie.p_start) if p is not None and p.goalie is not None else 0.0)
    n = 0
    for vals in by.values():
        top = max(vals)
        n += sum(1 for v in vals if top > 0 and v >= rel * top) if top > 0 else len(vals)
    return max(1, n)


def fee_floor(fees: Sequence[int], bins: int) -> float:
    """Smallest achievable max share of total fees on one of `bins` choices (LPT packing)."""
    fees = sorted((int(f) for f in fees), reverse=True)
    total = sum(fees)
    if total <= 0 or bins <= 0:
        return 1.0
    load = [0] * bins
    for f in fees:
        i = min(range(bins), key=lambda k: (load[k], k))
        load[i] += f
    return max(load) / total


def caps(cfg: dict, n_entries: int, pool, mode: Mode, n_games: int, *, fees_cents: Sequence[int] | None = None,
         tournament_entries: int | None = None, usable_goalies: int | None = None, usable_captains: int | None = None,
         usable_persons: int | None = None, budget: dict | None = None) -> Caps:
    """cfg: config/exposure.yaml (its `tournament` block). fees_cents: every entry's fee (all families)."""
    t = cfg["tournament"]
    nt = int(n_entries if tournament_entries is None else tournament_entries)
    fees = list(fees_cents) if fees_cents is not None else [100] * int(n_entries)
    budget = budget or {}
    goalies = usable_goalies if usable_goalies is not None else sum(1 for r in pool.rows if r.is_goalie)
    persons = usable_persons if usable_persons is not None else len(pool.persons)
    captains = usable_captains if usable_captains is not None else persons
    roster = 6 if mode is Mode.SHOWDOWN else 9
    notes: list[str] = []
    floors: dict = {}
    person = goalie = captain = None
    overlap = int(cfg["classic_max_overlap"]) if mode is Mode.CLASSIC and nt >= 2 else None
    shares = t[mode.value]
    if nt >= int(t["min_entries"]):
        def capped(name: str, share: float, floor_n: int) -> int:
            c = max(1, int(math.floor(share * nt)))
            floors[name] = floor_n
            if c < floor_n:
                notes.append(f"{name} cap raised from {c} to the feasibility floor {floor_n} "
                             f"({share:.0%} of {nt} tournament entries)")
                c = floor_n
            return c
        person = capped("person", float(shares["person"]), math.ceil(roster * nt / max(1, persons)))
        if mode is Mode.CLASSIC:
            goalie = capped("goalie", float(shares["goalie"]), math.ceil(nt / max(1, goalies)))
        else:
            captain = capped("captain", float(shares["captain"]), math.ceil(nt / max(1, captains)))
    elif nt >= 1:
        notes.append(f"{nt} tournament entr{'y' if nt == 1 else 'ies'} (< {t['min_entries']}): chosen deliberately, "
                     "no person or goalie cap")
        if mode is Mode.SHOWDOWN and nt >= 2:
            captain = 1 if nt <= 3 and captains >= nt else max(1, math.ceil(float(shares["captain"]) * nt))
            notes.append(f"Showdown: Captain cap {captain} (prefer different viable Captains)")

    def share_cap(name: str, bins: int) -> float | None:
        b = budget.get(name)
        if b is None:
            return None
        fl = fee_floor(fees, bins)
        floors[name] = round(fl, 4)
        if fl > float(b):
            notes.append(f"{name} budget {float(b):.0%} is below the achievable floor {fl:.0%} for these fees; "
                         f"using {fl:.0%}")
        return max(float(b), fl)

    g_share = share_cap("goalie_fee_share_max", goalies)
    game_share = share_cap("game_fee_share_max", n_games) if n_games > 1 else None
    if n_games <= 1 and budget.get("game_fee_share_max") is not None:
        notes.append("single-game slate: no per-game cap")
    c_share = share_cap("captain_fee_share_max", captains) if mode is Mode.SHOWDOWN else None
    return Caps(int(n_entries), nt, person, goalie, captain, overlap, g_share, game_share, c_share, floors, tuple(notes))


# -- dependence of one lineup ---------------------------------------------------------------------

def game_of(pool) -> dict[str, str]:
    """role_id -> game key ("AWAY@HOME")."""
    from nhl_dfs.intake.salary import parse_game_info

    out = {}
    for r in pool.rows:
        try:
            out[r.role_id] = parse_game_info(r.game_info)[0]
        except ValueError:
            continue
    return out


def primary_game(role_ids: Sequence[str], role_mean: Mapping[str, float], games: Mapping[str, str], pool) -> str | None:
    """The game holding the largest share of the lineup's scenario mean points (Captain at 1.5x)."""
    by: dict[str, float] = defaultdict(float)
    for i, r in enumerate(role_ids):
        g = games.get(r)
        if g is None:
            continue
        m = max(0.0, float(role_mean.get(r, 0.0)))
        by[g] += 1.5 * m if pool.mode is Mode.SHOWDOWN and i == 0 else m
    return max(sorted(by), key=lambda g: by[g]) if by else None


def goalies_in(role_ids: Sequence[str], pool) -> list[str]:
    return sorted({pool.by_role_id[r].person_key for r in role_ids if pool.by_role_id[r].is_goalie})


def captain_of(role_ids: Sequence[str], pool) -> str | None:
    return pool.by_role_id[role_ids[0]].person_key if pool.mode is Mode.SHOWDOWN and role_ids else None


def fee_shares(assignment: Mapping[str, Sequence[str]], pool, fees_cents: Mapping[str, int], role_mean: Mapping[str, float]) -> dict:
    """Fee share by goalie, primary game and Captain across every entry."""
    games = game_of(pool)
    total = sum(int(fees_cents.get(e, 0)) for e in assignment) or 1
    g, gm, cp = defaultdict(int), defaultdict(int), defaultdict(int)
    for e, lu in assignment.items():
        f = int(fees_cents.get(e, 0))
        for pk in goalies_in(lu, pool):
            g[pk] += f
        pg = primary_game(lu, role_mean, games, pool)
        if pg:
            gm[pg] += f
        c = captain_of(lu, pool)
        if c:
            cp[c] += f
    def top(d):
        return {k: round(v / total, 4) for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))}
    return {"goalie": top(g), "game": top(gm), "captain": top(cp)}


def concentration(assignment: Mapping[str, Sequence[str]], pool, fees_cents: Mapping[str, int], scenarios,
                  pay_by_entry: Mapping[str, np.ndarray]) -> dict:
    """Fee shares (goalie, primary game, Captain) plus the shared failure scenario per team."""
    means = scenarios.means_tenths()
    out = fee_shares(assignment, pool, fees_cents, means)
    out["n_games"] = len(pool.games)
    total = sum(int(fees_cents.get(e, 0)) for e in assignment) or 1
    # one column per person (FLEX / Classic rows; a CPT row copies its FLEX row's base)
    team_cols: dict[str, list[int]] = defaultdict(list)
    seen = set()
    for r in pool.rows:
        if r.is_goalie or r.person_key in seen or r.role_id not in scenarios.col:
            continue
        if pool.mode is Mode.SHOWDOWN and "CPT" in r.roster_positions:
            continue
        seen.add(r.person_key)
        team_cols[r.team].append(scenarios.col[r.role_id])
    zero = {e: (pay_by_entry[e] == 0) for e in assignment if e in pay_by_entry}
    base_share = sum(int(fees_cents.get(e, 0)) * float(z.mean()) for e, z in zero.items()) / total
    failures = []
    for team in sorted(team_cols):
        pts = scenarios.base[:, team_cols[team]].sum(axis=1)
        cut = np.quantile(pts, 0.2)
        mask = pts <= cut
        if not mask.any():
            continue
        share = sum(int(fees_cents.get(e, 0)) * float(z[mask].mean()) for e, z in zero.items()) / total
        failures.append({"team": team, "fee_share_cashing_nothing": round(share, 4), "scenarios": int(mask.sum())})
    failures.sort(key=lambda d: (-d["fee_share_cashing_nothing"], d["team"]))
    out["shared_failure"] = {"baseline_fee_share_cashing_nothing": round(base_share, 4), "worst": failures[:3],
                             "definition": "team skaters' summed DK points in their bottom 20% of scenarios"}
    return out
