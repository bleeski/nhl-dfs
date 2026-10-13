"""Showdown Captain policy (C23, [BEN] flags 96 to 98; docs/experiments/captain_2026-10-13.md). Both switches ship OFF.

- `ceiling_ranks`: per person, the ceiling_quantile (0.99) of his simulated points over the design scenarios and his rank among
  every player on the slate, skaters and goalies: 1 + the number with a strictly higher ceiling, so ties share the better rank.
  One column per person; the Captain's 1.5x multiplies every draw and does not change the order.
- Goalie gate (flag 96): with `discovery.captain.goalie_gate.enabled` a goalie ranked above top_k cannot be Captain. His Captain
  row is excluded from every scenario-pass discovery job and candidates with him as Captain are dropped before selection. The
  would-be-barred goalies are reported on every Showdown run, switch on or off.
- Leverage term (flag 97): with `discovery.captain.leverage.enabled` every Captain row gains
  weight x (ceiling / the slate's highest ceiling) x ln(100 / max(CPT own %, own_floor_pct)) points. Hand-set, never fitted (C12).
- Split report (flag 98): report only; nothing is enforced.

Classic pools get an empty policy. Every report value is a built-in type (the manifest is plain json).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from nhl_dfs.contracts.geometry import Mode

DEFAULT_QUANTILE = 0.99
DEFAULT_TOP_K = 3


@dataclass(frozen=True)
class CaptainPolicy:
    barred: frozenset[str] = frozenset()  # Captain role ids no candidate may use (gate on)
    term: dict = field(default_factory=dict)  # Captain role id -> points added to its discovery objective (term on)
    report: dict = field(default_factory=dict)


def config(risk_cfg: Mapping) -> dict:
    return dict((risk_cfg.get("discovery") or {}).get("captain") or {})


def is_captain_row(r) -> bool:
    return "CPT" in r.roster_positions


def ceiling_ranks(scen, pool, quantile: float) -> dict[str, tuple[float, int]]:
    """person_key -> (ceiling in points, rank). Persons with no scenario column are left out."""
    cols: dict[str, int] = {}
    for r in sorted(pool.rows, key=lambda x: (x.person_key, is_captain_row(x), x.role_id)):  # a FLEX column first
        if r.role_id in scen.col and r.person_key not in cols:
            cols[r.person_key] = scen.col[r.role_id]
    if not cols:
        return {}
    keys = sorted(cols)
    pts = scen.base[:, [cols[k] for k in keys]].astype(np.float64) / 10.0
    q = np.round(np.quantile(pts, float(quantile), axis=0), 6)
    return {k: (float(q[j]), 1 + int(np.sum(q > q[j]))) for j, k in enumerate(keys)}


def leverage_points(pool, own: Mapping[str, float] | None, ranks: Mapping[str, tuple[float, int]], weight: float,
                    own_floor_pct: float) -> dict[str, float]:
    """Captain role id -> weight x (ceiling / top ceiling) x ln(100 / max(own %, floor)); a missing ownership counts 0 (the floor)."""
    top = max((c for c, _ in ranks.values()), default=0.0)
    if top <= 0:
        return {}
    out = {}
    for r in pool.rows:
        if not is_captain_row(r) or r.person_key not in ranks:
            continue
        o = max(float((own or {}).get(r.role_id, 0.0)), float(own_floor_pct))
        out[r.role_id] = float(weight) * max(0.0, ranks[r.person_key][0]) / top * math.log(100.0 / o)
    return out


def policy(scen, pool, risk_cfg: Mapping, own: Mapping[str, float] | None) -> CaptainPolicy:
    """The policy for one Showdown slate (Classic: empty). The ranks and the would-be-barred goalies are always reported."""
    if pool.mode is not Mode.SHOWDOWN:
        return CaptainPolicy()
    cfg = config(risk_cfg)
    gate, lev = dict(cfg.get("goalie_gate") or {}), dict(cfg.get("leverage") or {})
    q = float(gate.get("ceiling_quantile", DEFAULT_QUANTILE))
    k = int(gate.get("top_k", DEFAULT_TOP_K))
    ranks = ceiling_ranks(scen, pool, q)
    goalie_keys = sorted({r.person_key for r in pool.rows if r.is_goalie and r.person_key in ranks})
    would = sorted(g for g in goalie_keys if ranks[g][1] > k)
    gate_on, lev_on = bool(gate.get("enabled")), bool(lev.get("enabled"))
    barred = frozenset(r.role_id for r in pool.rows if gate_on and is_captain_row(r) and r.person_key in would)
    term = leverage_points(pool, own, ranks, float(lev.get("weight", 0.0)), float(lev.get("own_floor_pct", 1.0))) if lev_on else {}
    top = sorted(ranks.items(), key=lambda kv: (-kv[1][0], kv[0]))[:5]
    report = {
        "configured": bool(cfg),
        "goalie_gate": {"enabled": gate_on, "ceiling_quantile": q, "top_k": k,
                        "goalies": [{"person": g, "ceiling": round(ranks[g][0], 2), "rank": int(ranks[g][1])} for g in goalie_keys],
                        "would_bar": list(would), "barred_roles": sorted(barred)},
        "leverage": {"enabled": lev_on, "weight": float(lev.get("weight", 0.0)), "own_floor_pct": float(lev.get("own_floor_pct", 1.0)),
                     "points_min": round(min(term.values()), 3) if term else None,
                     "points_max": round(max(term.values()), 3) if term else None},
        "top_ceilings": [{"person": p, "ceiling": round(c, 2), "rank": int(rk)} for p, (c, rk) in top],
    }
    return CaptainPolicy(barred, term, report)


def captain_barred(role_ids: Sequence[str], barred: frozenset[str] | set[str]) -> bool:
    """True when the lineup's Captain (canonical slot 0, as exposure.captain_of reads it) is barred."""
    return bool(barred) and bool(role_ids) and role_ids[0] in barred


def split_report(goal_share: Mapping[str, float], lineups: Sequence[Sequence[str]], pool, n_entries: int, cap: float,
                 n_games: int) -> dict:
    """Flag 98, report only: target leans by goal share (largest remainder), the underdog's 5-1 sleeve target
    min(floor(cap x entries), its lean target), and the published leans and 5-1 counts per team."""
    from nhl_dfs.models.field import split_counts

    teams = sorted(goal_share)
    if n_games != 1 or len(teams) != 2:
        return {"status": "NOT_APPLICABLE", "games": int(n_games), "enforced": False}
    shares = [float(goal_share[t]) for t in teams]
    lean = dict(zip(teams, split_counts(shares, int(n_entries))))
    underdog = min(teams, key=lambda t: (float(goal_share[t]), t))
    sleeve = min(int(math.floor(float(cap) * int(n_entries))), lean[underdog])
    pub_lean, pub_51, three = Counter(), Counter(), 0
    for lu in lineups:
        c = Counter(pool.by_role_id[r].team for r in lu)
        heavy = next((t for t, v in c.items() if v >= 4), None)
        if heavy is None:
            three += 1
            continue
        pub_lean[heavy] += 1
        pub_51[heavy] += int(c[heavy] == 5)
    return {"status": "OK", "goal_share": {t: round(float(goal_share[t]), 4) for t in teams}, "target_lean": lean,
            "underdog": underdog, "sleeve_cap": float(cap), "sleeve_target": int(sleeve),
            "published": {"lean": {t: int(pub_lean[t]) for t in teams}, "five_one": {t: int(pub_51[t]) for t in teams},
                          "three_three": int(three)},
            "enforced": False}


def portfolio_report(by_entry: Mapping[str, Sequence[str]], pool, contest_of: Mapping[str, str],
                     own_by_contest: Mapping[str, Mapping[str, float]], big_contest: str, captain_cap: int | None) -> dict:
    """The published portfolio's Captains: goalie-Captain entries, mean CPT ownership (each entry's Captain in its own contest's
    sampled field), distinct Captains, the most-used Captain against the cap, and the biggest contest's modeled goalie-Captain share."""
    caps = Counter()
    goalie_cpt, owns = 0, []
    for e, lu in sorted(by_entry.items()):
        if not lu:
            continue
        r = pool.by_role_id[lu[0]]
        caps[r.person_key] += 1
        goalie_cpt += int(r.is_goalie)
        owns.append(float((own_by_contest.get(contest_of.get(e, "")) or {}).get(lu[0], 0.0)))
    big = own_by_contest.get(big_contest) or {}
    field_goalie = sum(float(v) for rid, v in big.items()
                       if rid in pool.by_role_id and pool.by_role_id[rid].is_goalie and is_captain_row(pool.by_role_id[rid]))
    top = caps.most_common(1)
    return {"entries": int(sum(caps.values())), "goalie_cpt_entries": int(goalie_cpt),
            "mean_cpt_own_pct": round(sum(owns) / len(owns), 2) if owns else None,
            "distinct_captains": int(len(caps)), "max_captain": top[0][0] if top else None,
            "max_captain_count": int(top[0][1]) if top else 0, "captain_cap": None if captain_cap is None else int(captain_cap),
            "field_goalie_cpt_pct": round(field_goalie, 2), "field_contest": str(big_contest)}
