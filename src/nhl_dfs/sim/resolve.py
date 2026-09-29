"""One game-resolution process shared by the market fit and the simulator (C6).

Goals before empty-net play are independent Poisson(lambda_home) and Poisson(lambda_away). Then:
  * a team trailing by one pulls its goalie: the trailing team scores an equalizer with
    probability q_eq, or the leader scores into the empty net with probability q_en[0];
  * a team trailing by k >= 2 goals: the leader scores into the empty net with probability
    q_en[min(k, 4) - 1];
  * a regulation tie goes to overtime: a goal with probability ot_goal_p (always 1 in the
    playoffs), else a shootout. The overtime winner is the home team with probability
    0.5 + ot_slope * (lambda share - 0.5); the shootout winner with probability so_home_p.
The book total is regulation goals plus the overtime goal, plus one for the shootout winner
when so_adds_goal is true (the common US settlement; a labeled assumption in config/sim.yaml).
`summary` is the analytic form (used by the market fit); `sample` draws the same process
(used by the simulator), so a fit that reproduces a target analytically reproduces it in the
simulation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class Rules:
    q_eq: float
    q_en: tuple[float, float, float, float]
    ot_goal_p: float
    ot_slope: float
    so_home_p: float
    so_adds_goal: bool

    @staticmethod
    def from_config(cfg: dict, game_type: str = "regular") -> "Rules":
        r = cfg["resolve"]
        ot = 1.0 if game_type == "playoff" else float(r["ot_goal_p"])
        return Rules(float(r["q_eq"]), tuple(float(x) for x in r["q_en"]), ot,
                     float(r["ot_slope"]), float(r["so_home_p"]), bool(r["so_adds_goal"]))


@dataclass(frozen=True)
class Summary:
    p_home_win: float  # full game (regulation, overtime, shootout)
    p_home_reg_win: float
    p_away_reg_win: float
    p_tie: float  # tied after regulation, empty-net play included
    e_total: float  # book total goals
    e_en: float  # empty-net goals, both teams
    e_home_goals: float
    e_away_goals: float


def _ot_home(lh: float, la: float, rules: Rules) -> float:
    return 0.5 + rules.ot_slope * (lh / (lh + la) - 0.5)


def summary(lh: float, la: float, rules: Rules) -> Summary:
    p = lambda d: float(stats.skellam.pmf(d, lh, la))  # noqa: E731
    p0, up1, dn1 = p(0), p(1), p(-1)
    up = [up1, p(2), p(3), float(stats.skellam.sf(3, lh, la))]  # P(margin = 1, 2, 3, >= 4)
    dn = [dn1, p(-2), p(-3), float(stats.skellam.cdf(-4, lh, la))]
    p_up = float(stats.skellam.sf(0, lh, la))  # P(margin >= 1)
    p_dn = float(stats.skellam.cdf(-1, lh, la))
    reg_h = p_up - rules.q_eq * up1
    reg_a = p_dn - rules.q_eq * dn1
    tie = p0 + rules.q_eq * (up1 + dn1)
    en_h = sum(q * x for q, x in zip(rules.q_en, up))
    en_a = sum(q * x for q, x in zip(rules.q_en, dn))
    eq_h = rules.q_eq * dn1  # home trailing by one equalizes
    eq_a = rules.q_eq * up1
    ot_h = _ot_home(lh, la, rules)
    tie_home = rules.ot_goal_p * ot_h + (1 - rules.ot_goal_p) * rules.so_home_p
    extra = rules.ot_goal_p + (1 - rules.ot_goal_p) * (1.0 if rules.so_adds_goal else 0.0)
    e_home = lh + en_h + eq_h
    e_away = la + en_a + eq_a
    return Summary(p_home_win=reg_h + tie * tie_home, p_home_reg_win=reg_h, p_away_reg_win=reg_a, p_tie=tie,
                   e_total=e_home + e_away + tie * extra, e_en=en_h + en_a, e_home_goals=e_home, e_away_goals=e_away)


@dataclass
class Draw:
    """Per-scenario results (arrays of length n). Goal counts by team, split by origin."""
    reg_h: np.ndarray  # regulation goals before empty-net play (Poisson draws)
    reg_a: np.ndarray
    eq_h: np.ndarray  # equalizers scored while the goalie was pulled (0 or 1)
    eq_a: np.ndarray
    en_h: np.ndarray  # empty-net goals by the home team (leader)
    en_a: np.ndarray
    tie: np.ndarray  # tied after regulation
    ot_goal: np.ndarray  # overtime goal scored (bool; tie only)
    so: np.ndarray  # shootout (bool; tie only)
    home_win: np.ndarray  # full-game winner
    so_extra: np.ndarray  # shootout winner's book goal (0/1)


def sample(rng: np.random.Generator, lh, la, n: int, rules: Rules) -> Draw:
    """lh, la: scalars or arrays of length n (per-scenario intensities)."""
    h0 = rng.poisson(lh, n)
    a0 = rng.poisson(la, n)
    m = h0 - a0
    u = rng.random(n)
    k = np.minimum(np.abs(m), 4)
    q_en = np.array((0.0,) + tuple(rules.q_en))[k]  # EN probability by lead size
    lead_h, lead_a = m > 0, m < 0
    eq_h = lead_a & (k == 1) & (u < rules.q_eq)
    eq_a = lead_h & (k == 1) & (u < rules.q_eq)
    shift = np.where(k == 1, rules.q_eq, 0.0)
    en_go = (k >= 1) & (u >= shift) & (u < shift + q_en)
    en_h = lead_h & en_go
    en_a = lead_a & en_go
    h = h0 + eq_h + en_h
    a = a0 + eq_a + en_a
    tie = h == a
    u2 = rng.random(n)
    share = np.asarray(lh, float) / (np.asarray(lh, float) + np.asarray(la, float))
    ot_h = 0.5 + rules.ot_slope * (share - 0.5)
    ot_goal = tie & (u2 < rules.ot_goal_p)
    so = tie & ~ot_goal
    u3 = rng.random(n)
    home_win = np.where(tie, np.where(ot_goal, u3 < ot_h, u3 < rules.so_home_p), h > a)
    so_extra = (so & bool(rules.so_adds_goal)).astype(np.int16)
    return Draw(h0, a0, eq_h.astype(np.int16), eq_a.astype(np.int16), en_h.astype(np.int16), en_a.astype(np.int16),
                tie, ot_goal, so, home_win, so_extra)
