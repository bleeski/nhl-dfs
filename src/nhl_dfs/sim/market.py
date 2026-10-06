"""Market fit (C6, plan section 5 "Vegas integration"): from paired prices to game intensities.

Prices are de-vigged pairwise (never best-of-book across markets). The two-way moneyline is the
full-game winner (overtime and shootout included); the total is a betting threshold, converted
to an expected total by inverting a Poisson total through the line and its de-vigged over price
(a whole-number line conditions on no push). `fit_game` then solves the two team intensities so
that the shared resolution process (sim/resolve.py) reproduces the implied home win probability
and the implied expected total, with empty-net goals, overtime and the shootout settlement in
the total. Total x win probability is not used as a team total.

With no usable odds the game takes the model's intensities (source MODEL). A market that
predates a goalie confirmation is STALE: the fit is blended toward the model. The fitted
intensities are clipped to the model within a limit that depends on how much of that side's model rests on player
history (C17, B66: max_discrepancy for a history-backed side, max_discrepancy_prior for a salary-prior side); both facts
are logged in `notes`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml
from scipy import optimize, stats

from nhl_dfs.sim.resolve import Rules, summary

REPO_ROOT = Path(__file__).resolve().parents[3]
SIM_YAML = REPO_ROOT / "config" / "sim.yaml"


def load_sim_config(path: Path = SIM_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class TeamStrength:
    """Model-only intensities (pre-empty-net Poisson goals per team) for one game. history_*: the share (0 to 1) of that
    side's modeled goals that rest on player history rather than a salary prior or the league rate (C17, B66); 1 keeps the
    full-strength clip, which is what a caller that does not know gets."""
    lam_home: float
    lam_away: float
    notes: tuple[str, ...] = ()
    history_home: float = 1.0
    history_away: float = 1.0


@dataclass(frozen=True)
class GameRates:
    lambda_home: float
    lambda_away: float
    p_home_reg_win: float
    p_ot: float  # probability of a tie after regulation (goes to OT or a shootout)
    source: str  # "MARKET" | "MODEL"
    stale: bool = False
    p_home_win: float = 0.5  # full game
    e_total: float = 0.0  # book total, empty-net goals, overtime and shootout convention included
    target_p_home: float | None = None
    target_total: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# -- prices ------------------------------------------------------------------------------------------

def _raw_prob(american: int) -> float:
    a = int(american)
    if a == 0:
        raise ValueError("American price 0")
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def implied(ml_a: int, ml_b: int) -> tuple[float, float]:
    """De-vigged probabilities for a paired two-way market (proportional removal)."""
    pa, pb = _raw_prob(ml_a), _raw_prob(ml_b)
    z = pa + pb
    return pa / z, pb / z


def implied_total(line: float, over_price: int | None, under_price: int | None) -> float:
    """Expected total implied by a totals line and its paired prices: the Poisson mean whose
    de-vigged P(over) matches. A whole-number line conditions on no push. Missing prices mean an
    even market (P(over) = 0.5 conditional)."""
    q = implied(over_price, under_price)[0] if over_price is not None and under_price is not None else 0.5
    whole = float(line).is_integer()

    def f(mu: float) -> float:
        if whole:
            k = int(line)
            over, push = float(stats.poisson.sf(k, mu)), float(stats.poisson.pmf(k, mu))
            return over / max(1e-12, 1.0 - push) - q
        return float(stats.poisson.sf(math.floor(line), mu)) - q

    return float(optimize.brentq(f, 0.3, 20.0))


# -- model strength ----------------------------------------------------------------------------------

def _team_goal_stats(params, team: str, opp: str, model_cfg: dict) -> tuple[float, float, float, float]:
    """(expected goals for, expected dressed F, expected dressed D, opposing goalie miss rate)."""
    from nhl_dfs.models.rates import STRENGTHS

    g_for = f_dressed = d_dressed = 0.0
    for p in params.persons.values():
        if p.team != team or p.group == "G" or p.opportunity is None or p.rates is None:
            continue
        o, r = p.opportunity, p.rates
        g_for += o.p_dress * sum(r.g60[s] * getattr(o, f"toi_{s}_s") / 3600.0 for s in STRENGTHS)
        if p.group == "F":
            f_dressed += o.p_dress
        else:
            d_dressed += o.p_dress
    goalies = [p.goalie for p in params.persons.values() if p.team == opp and p.group == "G" and p.goalie is not None]
    tot = sum(g.p_start for g in goalies)
    league_miss = 1.0 - float(model_cfg["goalies"]["sv_pct"])
    miss = sum(g.p_start * (1.0 - g.sv_pct) for g in goalies) / tot if tot > 0 else league_miss
    return g_for, f_dressed, d_dressed, miss


def _history_share(params, team: str) -> float:
    """Share of a team's modeled goals that rest on player history: each listed skater's expected goals weighted by 1 for a
    HISTORY person, his history exposure over his total exposure for a MIXED one, 0 for a PRIOR one (C17, B66)."""
    from nhl_dfs.contracts.statuses import ModelStatus
    from nhl_dfs.models.rates import STRENGTHS

    total = hist = 0.0
    for p in params.persons.values():
        if p.team != team or p.group == "G" or p.opportunity is None or p.rates is None:
            continue
        o, r = p.opportunity, p.rates
        g = o.p_dress * sum(r.g60[s] * getattr(o, f"toi_{s}_s") / 3600.0 for s in STRENGTHS)
        if p.source is ModelStatus.HISTORY:
            h = 1.0
        elif p.source is ModelStatus.MIXED:
            e = p.history_exposure + p.prior_exposure
            h = p.history_exposure / e if e > 0 else 0.5
        else:
            h = 0.0
        total += g
        hist += g * h
    return hist / total if total > 0 else 0.0


def model_strength(params, home: str, away: str, cfg: dict | None = None, model_cfg: dict | None = None) -> TeamStrength:
    """Model intensities from the per-person ParamTable. A team whose listed skaters cannot make up a lineup (an incomplete
    pool: fewer than 10 F or 5 D expected to dress) takes its own recent goal rate when the table has one (C17, B51), else
    the league goal rate, and says so. Each side also carries the share of its goals that rest on player history, which sets
    the market clip (B66); a side on its own recent rate counts as history-backed, a side on the league rate as not."""
    from nhl_dfs.models.rates import load_model_config

    cfg = cfg or load_sim_config()
    model_cfg = model_cfg or load_model_config()
    r = cfg["resolve"]
    league_miss = 1.0 - float(model_cfg["goalies"]["sv_pct"])
    min_games = int(((model_cfg.get("team") or {}).get("team_rate") or {}).get("min_games", 20))
    own_rates = getattr(params, "team_goals", None) or {}
    notes = []
    lam = []
    shares = []
    for team, opp in ((home, away), (away, home)):
        g_for, f_d, d_d, miss = _team_goal_stats(params, team, opp, model_cfg)
        share = _history_share(params, team)
        if f_d < 10.0 or d_d < 5.0:
            own = own_rates.get(team)
            if own is not None and own[1] >= min_games:
                g_for, share = float(own[0]), 1.0
                notes.append(f"{team}: listed skaters cover {f_d:.1f} F and {d_d:.1f} D, its own goal rate {own[0]:.2f} over its "
                             f"last {own[1]} games used")
            else:
                g_for, share = float(r["goals_league"]), 0.0
                notes.append(f"{team}: listed skaters cover {f_d:.1f} F and {d_d:.1f} D, league goal rate used")
        lam.append(float(r["lambda0_league"]) * g_for / float(r["goals_league"]) * miss / league_miss)
        shares.append(share)
    return TeamStrength(lam[0], lam[1], tuple(notes), shares[0], shares[1])


# -- fit ---------------------------------------------------------------------------------------------

def _solve(target_p: float, target_total: float, rules: Rules, start: tuple[float, float], lo: float, hi: float):
    def res(x):
        s = summary(math.exp(x[0]), math.exp(x[1]), rules)
        return [s.e_total - target_total, s.p_home_win - target_p]

    x0 = [math.log(max(lo, min(hi, v))) for v in start]
    sol = optimize.root(res, x0, method="hybr", tol=1e-12)
    if not sol.success or max(abs(v) for v in res(sol.x)) > 1e-6:
        ls = optimize.least_squares(res, x0, bounds=([math.log(lo)] * 2, [math.log(hi)] * 2), xtol=1e-12, ftol=1e-12)
        sol = ls
    return math.exp(sol.x[0]), math.exp(sol.x[1]), max(abs(v) for v in res(sol.x))


def _rates(lh: float, la: float, rules: Rules, source: str, stale: bool, notes, tp, tt) -> GameRates:
    s = summary(lh, la, rules)
    return GameRates(lh, la, s.p_home_reg_win, s.p_tie, source, stale, s.p_home_win, s.e_total, tp, tt, tuple(notes))


def fit_game(odds, strength: TeamStrength, asof_utc: datetime | None, goalie_confirmed_at: datetime | None,
             *, cfg: dict | None = None, game_type: str = "regular") -> GameRates:
    """odds: a sources.nhl.GameOdds or None. asof_utc: when the market snapshot was taken.
    goalie_confirmed_at: when the game's starters were last confirmed (None if unconfirmed)."""
    cfg = cfg or load_sim_config()
    rules = Rules.from_config(cfg, game_type)
    m = cfg["market"]
    lo, hi = float(m["min_lambda"]), float(m["max_lambda"])
    lm, la_m = strength.lam_home, strength.lam_away
    notes = list(strength.notes)
    stale = bool(goalie_confirmed_at is not None and asof_utc is not None and asof_utc < goalie_confirmed_at)
    has = (odds is not None and odds.home_ml is not None and odds.away_ml is not None and odds.total_line is not None)
    if not has:
        why = "no odds for this game" if odds is None else "odds lack a two-way moneyline or a total"
        notes.append(f"MODEL: {why}")
        return _rates(lm, la_m, rules, "MODEL", False, notes, None, None)
    p_h = implied(odds.home_ml, odds.away_ml)[0]
    total = implied_total(odds.total_line, odds.over_price, odds.under_price)
    lh, la, err = _solve(p_h, total, rules, (lm, la_m), lo, hi)
    notes.append(f"market: p_home {p_h:.3f}, total {total:.2f} (line {odds.total_line:g}); fit residual {err:.1e}")
    if stale:
        w = float(m["stale_weight"])
        lh = math.exp((1 - w) * math.log(lh) + w * math.log(lm))
        la = math.exp((1 - w) * math.log(la) + w * math.log(la_m))
        notes.append(f"STALE: odds at {asof_utc:%H:%M}Z predate goalie confirmation {goalie_confirmed_at:%H:%M}Z; "
                     f"blended {w:.0%} toward the model")
    cap_hist = float(m["max_discrepancy"])
    cap_prior = float(m.get("max_discrepancy_prior", cap_hist))
    clipped, capped = {}, []
    for name, val, mod, share in (("home", lh, lm, strength.history_home), ("away", la, la_m, strength.history_away)):
        ratio = val / mod
        if abs(ratio - 1.0) > 0.10:
            notes.append(f"{name} intensity {val:.2f} vs model {mod:.2f} (x{ratio:.2f})")
        share = min(1.0, max(0.0, float(share)))
        cap = cap_hist + (cap_prior - cap_hist) * (1.0 - share)  # B66: wide when the model side is a prior, cap_hist on history
        clipped[name] = min(max(val, mod * (1 - cap)), mod * (1 + cap))
        if clipped[name] != val:
            capped.append(f"{name} market {val:.2f} clipped to {clipped[name]:.2f} (model {mod:.2f} +/- {cap:.0%}, "
                          f"model side {share:.0%} history-backed)")
    if capped:
        notes.append("CAPPED: " + "; ".join(capped) + "; the fit no longer reproduces the market")
    return _rates(clipped["home"], clipped["away"], rules, "MARKET", stale, notes, p_h, total)


# -- matching a snapshot to slate games ----------------------------------------------------------------

def _code_maps(path: Path | None = None) -> dict[str, dict[str, str]]:
    """DK code -> the code each odds source uses, for DK codes marked verified in teams.yaml."""
    p = path or REPO_ROOT / "config" / "teams.yaml"
    with open(p, encoding="utf-8") as f:
        teams = yaml.safe_load(f)["teams"]
    out: dict[str, dict[str, str]] = {"nhl_partner_odds": {}, "covers": {}}
    for t in teams:
        if t.get("dk") and t.get("dk_verified"):
            out["nhl_partner_odds"][t["dk"]] = t["nhl"]
            if t.get("covers"):
                out["covers"][t["dk"]] = t["covers"]
    return out


def match_odds(snapshot, games: dict[str, tuple[str, str]], teams_path: Path | None = None) -> tuple[dict, dict[str, str]]:
    """games: "AWAY@HOME" -> (home DK code, away DK code). Returns ({game key: GameOdds}, {game
    key: reason it is unmatched}). A DK code that is not verified against the source's code list
    never matches: that game falls back to the model."""
    matched, reasons = {}, {}
    if snapshot is None:
        return matched, {k: "no odds snapshot" for k in games}
    cmap = _code_maps(teams_path).get(snapshot.source, {})
    by_pair = {(g.home_abbrev, g.away_abbrev): g for g in snapshot.games}
    for key, (home, away) in games.items():
        h, a = cmap.get(home), cmap.get(away)
        if h is None or a is None:
            bad = [c for c, v in ((home, h), (away, a)) if v is None]
            reasons[key] = f"unverified team code {'/'.join(bad)} for source {snapshot.source}"
        elif (h, a) in by_pair:
            matched[key] = by_pair[(h, a)]
        else:
            reasons[key] = f"{a}@{h} not on the {snapshot.source} board"
    return matched, reasons
