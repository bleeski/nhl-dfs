"""Aggregate joint game simulator (C6, plan section 5 "Joint simulation").

Simulate once per PERSON. Per scenario and per game, in this fixed order (one seeded stream per
[seed, purpose, chunk, game]):

  1. a Gamma pace factor per team; PP opportunities per team;
  2. the game result from sim/resolve.py (regulation Poisson goals, equalizers and empty-net goals
     while a goalie is pulled, then overtime or a shootout): the SAME process the market fit inverts;
  3. per team: dressed skaters (Bernoulli p_dress), a TOI multiplier, then every goal is allocated
     to an on-ice unit (forward line + defense pair at even strength, a PP unit, or the team at
     shorthanded), a scorer, and 0 to 2 distinct assisters, all weighted by the per-person rates in
     the ParamTable and restricted to dressed people (a unit that fits no dressed skater degrades to
     team-wide weights);
  4. non-goal shots per person ~ NB(mean * pace) and blocks ~ NB(mean * opponent pace), with the
     dispersion reduced so each person's marginal stays at C5's mu + phi mu^2; SOG = goals + non-goal
     shots, so a scorer always has SOG >= goals and a goalie's saves are exactly the opposing
     non-goal shots (empty-net goals are excluded by construction);
  5. goalies: a starter by start probability, a pull-and-relief process, goals against (empty-net
     goals are never charged), decisions (shootout loss = OTL), shutouts (a sole goalie of record
     with no regulation or overtime goal charged);
  6. a shootout, when there is one, gives each shooter's goals in their own column (never goals,
     SOG, GA or a bonus input).

A team whose pool lists fewer skaters than a lineup dresses gets phantom league-average skaters
(one forward and one defense column, weighted by the missing count) so listed people are not
credited with goals nobody else in the pool could have scored. Phantoms are never in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from nhl_dfs.models import opportunity as opp_mod
from nhl_dfs.models import rates as rates_mod
from nhl_dfs.models.rates import STRENGTHS, load_model_config
from nhl_dfs.sim import outcomes as oc
from nhl_dfs.sim import resolve as rs
from nhl_dfs.sim.market import GameRates, load_sim_config

EV, PP, SH = 0, 1, 2


@dataclass
class GameSpec:
    key: str  # "AWAY@HOME"
    home: str  # team codes as they appear in the ParamTable persons
    away: str
    rates: GameRates
    game_type: str = "regular"


@dataclass
class SlateSpec:
    games: list[GameSpec]
    mode: str = "classic"
    cfg: dict = field(default_factory=load_sim_config)
    model_cfg: dict = field(default_factory=load_model_config)


# -- preparation ---------------------------------------------------------------------------------------

@dataclass
class _Goalies:
    cols: np.ndarray  # output columns
    p_start: np.ndarray
    pull_c: np.ndarray
    unlisted: float  # start probability mass on goalies outside the pool


@dataclass
class _Team:
    code: str
    cols: np.ndarray  # output columns of listed skaters
    scale: np.ndarray  # (Ni,) 1 for listed, missing count for phantoms
    p_dress: np.ndarray
    toi: np.ndarray  # (Ni, 3) seconds
    toi_cv: np.ndarray
    g: np.ndarray  # (Ni, 3) expected goals per dressed game by strength
    a1: np.ndarray
    a2: np.ndarray
    saved: np.ndarray  # (Ni,) expected non-goal SOG per dressed game
    blk: np.ndarray
    phi_saved: np.ndarray  # dispersion of the non-goal shots given TOI and pace (see _conditional_phi)
    phi_blocks: np.ndarray
    is_f: np.ndarray
    rg: np.ndarray  # (Ni, 3) per-hour goal rate by strength
    ra1: np.ndarray
    ra2: np.ndarray
    unit_f: np.ndarray  # (Ni,) forward-line column of each forward (0 for a defenseman)
    unit_d: np.ndarray  # (Ni,) defense-pair column of each defenseman
    unit_pp: np.ndarray  # (Ni,) PP-unit column (0 when in none)
    m_f: np.ndarray  # (Ni, UF) membership of forward lines
    m_d: np.ndarray
    m_pp: np.ndarray
    shares: np.ndarray  # (3,) team goal share by strength
    goalies: _Goalies
    n_listed: int = 0
    a_mult: np.ndarray = None  # (Ni, 3, 2) primary and secondary assist-weight multipliers from the pilot calibration


def _rank_units(idx: list[int], key: np.ndarray, size: int) -> list[list[int]]:
    order = sorted(idx, key=lambda i: (-key[i], i))
    return [order[k:k + size] for k in range(0, len(order), size)]


def _units_from(by_id: dict[str, list[int]], loose: list[int], key: np.ndarray, size: int, min_size: int) -> list[list[int]]:
    """Units from measured ids where teammates actually share one (at least min_size members; a
    person's most-used unit often differs from his linemates', which leaves singletons), and TOI-rank
    chunks of `size` for everyone else. Ids larger than `size` are split by TOI rank."""
    units: list[list[int]] = []
    left = list(loose)
    for members in by_id.values():
        if len(members) >= min_size:
            units.extend(_rank_units(members, key, size))
        else:
            left.extend(members)
    return units + _rank_units(left, key, size)


def _membership(units: list[list[int]], ni: int) -> np.ndarray:
    m = np.zeros((ni, max(1, len(units))))
    for u, members in enumerate(units):
        m[members, u] = 1.0
    return m


def _prep_team(code: str, params, col_of: dict, sim_cfg: dict, model_cfg: dict, var: float) -> _Team:
    persons = [p for p in params.persons.values() if p.team == code and p.group != "G"]
    persons.sort(key=lambda p: p.person_key)
    ns = len(persons)
    budget = model_cfg["team"]["dress_budget"]
    ni = ns + 2
    scale = np.ones(ni)
    p_dress = np.ones(ni)
    toi = np.zeros((ni, 3))
    cv = np.zeros(ni)
    g, a1, a2 = np.zeros((ni, 3)), np.zeros((ni, 3)), np.zeros((ni, 3))
    rg, ra1, ra2 = np.zeros((ni, 3)), np.zeros((ni, 3)), np.zeros((ni, 3))
    saved, blk, phi_s, phi_b = np.zeros(ni), np.zeros(ni), np.zeros(ni), np.zeros(ni)
    is_f = np.zeros(ni, bool)
    floor = float(sim_cfg["skater"]["min_saved_mean"])

    def fill(i: int, o, r):
        toi[i] = [getattr(o, f"toi_{s}_s") for s in STRENGTHS]
        cv[i] = o.sd_toi_s / max(o.toi_s, 1.0)
        for k, s in enumerate(STRENGTHS):
            h = toi[i, k] / 3600.0
            g[i, k], a1[i, k], a2[i, k] = r.g60[s] * h, r.a1_60[s] * h, r.a2_60[s] * h
            rg[i, k], ra1[i, k], ra2[i, k] = r.g60[s], r.a1_60[s], r.a2_60[s]
        sog = sum(r.sog60[s] * toi[i, k] / 3600.0 for k, s in enumerate(STRENGTHS))
        saved[i] = max(floor, sog - g[i].sum())
        blk[i] = sum(r.blk60[s] * toi[i, k] / 3600.0 for k, s in enumerate(STRENGTHS))
        phi_s[i], phi_b[i] = r.phi_sog, r.phi_blk

    for i, p in enumerate(persons):
        p_dress[i] = p.opportunity.p_dress
        is_f[i] = p.group == "F"
        fill(i, p.opportunity, p.rates)
    n_f = float(sum(p.opportunity.p_dress for p in persons if p.group == "F"))
    n_d = float(sum(p.opportunity.p_dress for p in persons if p.group == "D"))
    for k, (grp, need, have) in enumerate((("F", budget["forwards"], n_f), ("D", budget["defense"], n_d))):
        i = ns + k
        fill(i, opp_mod.prior_opportunity(grp, model_cfg), rates_mod.prior_rates(0, grp, model_cfg))
        is_f[i] = grp == "F"
        scale[i] = max(0.0, float(need) - have)
    e = (g * (p_dress * scale)[:, None]).sum(axis=0)
    shares = e / e.sum() if e.sum() > 0 else np.array([0.75, 0.22, 0.03])

    # units: measured line ids where the ParamTable has them, TOI-rank pseudo-units for the rest
    by_line: dict[str, list[int]] = {}
    by_pair: dict[str, list[int]] = {}
    by_pp: dict[str, list[int]] = {}
    loose_f, loose_d, loose_pp = [], [], []
    for i, p in enumerate(persons):
        ue, up = p.opportunity.unit_ev, p.opportunity.unit_pp
        if ue:
            (by_line if is_f[i] else by_pair).setdefault(ue, []).append(i)
        else:
            (loose_f if is_f[i] else loose_d).append(i)
        if up:
            by_pp.setdefault(up, []).append(i)
        else:
            loose_pp.append(i)
    lines = _units_from(by_line, loose_f, toi[:, EV], 3, 3) + [[ns]]
    pairs = _units_from(by_pair, loose_d, toi[:, EV], 2, 2) + [[ns + 1]]
    pps = _units_from(by_pp, [i for i in loose_pp if toi[i, PP] > 0], toi[:, PP], 5, 3) + [[ns, ns + 1]]

    gl = [p for p in params.persons.values() if p.team == code and p.group == "G" and p.goalie is not None]
    gl.sort(key=lambda p: p.person_key)
    lg = float(model_cfg["goalies"]["pull_per_ga"])
    goalies = _Goalies(np.array([col_of[p.person_key] for p in gl], dtype=np.int64),
                       np.array([p.goalie.p_start for p in gl], float),
                       np.array([p.goalie.pull_prob_per_ga for p in gl], float) if gl else np.zeros(0),
                       max(0.0, 1.0 - sum(p.goalie.p_start for p in gl)) if gl else 1.0)
    goalies.pull_c = np.append(goalies.pull_c, lg)  # the last slot is the unlisted goalie
    m_f, m_d, m_pp = _membership(lines, ni), _membership(pairs, ni), _membership(pps, ni)
    k_mix = (1.0 + cv ** 2) * (1.0 + var)  # E[(TOI multiplier x pace)^2]
    # SOG = goals + non-goal shots; give the non-goal part the dispersion that keeps SOG at mu + phi mu^2
    total_sog = saved + g.sum(axis=1)
    phi_saved = _conditional_phi(phi_s * (total_sog / np.maximum(saved, 1e-9)) ** 2, k_mix)
    phi_blocks = _conditional_phi(phi_b, k_mix)
    tp = _Team(code, np.array([col_of[p.person_key] for p in persons], dtype=np.int64), scale, p_dress, toi, cv, g, a1, a2,
                 saved, blk, phi_saved, phi_blocks, is_f, rg, ra1, ra2,
                 m_f.argmax(axis=1), m_d.argmax(axis=1), m_pp.argmax(axis=1), m_f, m_d, m_pp, shares, goalies, n_listed=ns,
                 a_mult=np.ones((ni, 3, 2)))
    _calibrate_assists(tp, sim_cfg)
    return tp


@dataclass
class Prep:
    slate: SlateSpec
    person_keys: list[str]
    is_goalie: np.ndarray
    teams: dict[str, _Team]
    team_order: list[str]
    pace_var: float


def prepare(slate: SlateSpec, params) -> Prep:
    keys = sorted(params.persons)
    col_of = {k: i for i, k in enumerate(keys)}
    is_g = np.array([params.persons[k].group == "G" for k in keys])
    codes = []
    for g in slate.games:
        for c in (g.home, g.away):
            if c not in codes:
                codes.append(c)
    var = float(slate.cfg["pace"]["var"])
    teams = {c: _prep_team(c, params, col_of, slate.cfg, slate.model_cfg, var) for c in codes}
    return Prep(slate, keys, is_g, teams, codes, var)


# -- draws -----------------------------------------------------------------------------------------------

def _choose(rng, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sample a column per row proportional to w (m, K). Returns (index, row has weight)."""
    tot = w.sum(axis=1)
    cum = np.cumsum(w, axis=1)
    u = rng.random(w.shape[0]) * tot
    idx = np.minimum((cum <= u[:, None]).sum(axis=1), w.shape[1] - 1)
    return idx, tot > 0


def _conditional_phi(phi_total: np.ndarray, k_mix: np.ndarray) -> np.ndarray:
    """Dispersion of X | TOI multiplier, pace so that the marginal stays at mu + phi_total mu^2.
    With X | m ~ NB(mu m, phi'), E m = 1 and E m^2 = k_mix: Var X = mu + mu^2 (phi' k_mix + k_mix - 1),
    so phi' = (phi_total - (k_mix - 1)) / k_mix, floored at 0 (Poisson) when the mixing alone is wider."""
    return np.maximum(0.0, (phi_total - (k_mix - 1.0)) / k_mix)


def _nb(rng, mean: np.ndarray, phi: np.ndarray) -> np.ndarray:
    """Negative binomial with per-column dispersion (Poisson where phi ~ 0): Gamma-Poisson."""
    shape = 1.0 / np.maximum(phi, 1e-6)
    lam = mean * rng.gamma(shape, 1.0 / shape, size=mean.shape)
    return rng.poisson(lam)


def _pace(rng, n: int, var: float) -> np.ndarray:
    k = 1.0 / var
    return rng.gamma(k, 1.0 / k, size=n)


EVENT_LOG: list | None = None  # tests set this to a list to record (team, state, rows, scorer, a1, a2, dressed) per goal slot


def _allocate(rng, tp: _Team, dm: np.ndarray, counts: dict, sim_cfg: dict, out: dict) -> None:
    """Allocate goals by state to scorers and assisters. dm: (n, Ni) presence x TOI multiplier x scale.
    out: goals, assists, sh arrays (n, Ni), modified in place.

    The scorer is drawn first, proportional to his expected goals per game (rate x TOI), so each
    person's goal expectation follows the ParamTable. The on-ice group is then conditioned on him:
    his own unit, and the partner unit (defense pair for a forward, forward line for a defenseman)
    drawn proportional to its ice-time share times the group's combined scoring rate, since a goal
    is more likely with a stronger group out. Assisters come from that group by per-minute assist
    rates (being on the ice already carries the time), excluding the scorer. A group with no eligible
    assister degrades to team-wide per-game weights."""
    a_p = np.array(sim_cfg["skater"]["assists_per_goal"], float)
    a_cum = np.cumsum(a_p / a_p.sum())
    ni = dm.shape[1]
    pres = (dm > 0).astype(float)
    for state, cnt in counts.items():
        if cnt.max() <= 0:
            continue
        gw = dm * tp.g[:, state]
        team_a = (dm * tp.a1[:, state], dm * tp.a2[:, state])
        rate_a = (pres * tp.ra1[:, state] * tp.a_mult[:, state, 0], pres * tp.ra2[:, state] * tp.a_mult[:, state, 1])
        if state == EV:
            toi_ev = dm * tp.toi[:, EV]
            line_toi, pair_toi = toi_ev @ tp.m_f, toi_ev @ tp.m_d
            r_all = pres * tp.rg[:, EV]
            line_rate, pair_rate = r_all @ tp.m_f, r_all @ tp.m_d
        for j in range(int(cnt.max())):
            rows = np.flatnonzero(cnt > j)
            if rows.size == 0:
                break
            m = np.arange(rows.size)
            ws = gw[rows].copy()
            zero = ws.sum(axis=1) <= 0  # nobody with scoring weight: uniform over the dressed
            if zero.any():
                ws[zero] = pres[rows][zero]
            sc_i, _ = _choose(rng, ws)
            cand = np.zeros((rows.size, ni), bool)
            if state == EV:
                own_f = tp.is_f[sc_i]
                ul, up = tp.unit_f[sc_i], tp.unit_d[sc_i]
                # partner defense pair for a forward scorer, partner forward line for a defenseman
                r_line_own = line_rate[rows][m, ul]
                r_pair_own = pair_rate[rows][m, up]
                pw = pair_toi[rows] * (r_line_own[:, None] + pair_rate[rows])
                lw = line_toi[rows] * (line_rate[rows] + r_pair_own[:, None])
                pair_pick, ok_p = _choose(rng, np.where(own_f[:, None], pw, 1.0))
                line_pick, ok_l = _choose(rng, np.where(~own_f[:, None], lw, 1.0))
                line = np.where(own_f, ul, line_pick)
                pair = np.where(own_f, pair_pick, up)
                cand = ((tp.m_f[:, line].T > 0) | (tp.m_d[:, pair].T > 0))
                five_rate = line_rate[rows][m, line] + pair_rate[rows][m, pair]
            elif state == PP:
                pu = tp.unit_pp[sc_i]
                cand = tp.m_pp[:, pu].T > 0
                five_rate = ((pres * tp.rg[:, PP]) @ tp.m_pp)[rows][m, pu]
            else:
                cand[:] = True
                five_rate = np.ones(rows.size)
            own_share = np.minimum(0.8, tp.rg[None, :, state] / np.maximum(five_rate[:, None], 1e-9))
            squeeze = 1.0 / (1.0 - own_share)  # undo the scorer's own goals crowding out his assists
            cand &= pres[rows] > 0
            cand[m, sc_i] = True
            k = np.searchsorted(a_cum, rng.random(rows.size), side="right")  # assists on the goal: 0, 1, 2
            k = np.minimum(k, a_p.size - 1)
            np.add.at(out["goals"], (rows, sc_i), 1)
            if state == SH:
                np.add.at(out["sh"], (rows, sc_i), 1)
            taken = np.zeros((rows.size, ni), bool)
            taken[m, sc_i] = True
            assist_idx = np.full((2, rows.size), -1)
            for a_no in (1, 2):
                sel = np.flatnonzero(k >= a_no)
                if sel.size == 0:
                    continue
                if state == SH:  # no on-ice group at shorthanded strength: team-wide per-game weights
                    w = np.where(~taken[sel], team_a[a_no - 1][rows[sel]], 0.0)
                else:
                    w = np.where(cand[sel] & ~taken[sel], rate_a[a_no - 1][rows[sel]] * squeeze[sel], 0.0)
                    fall = w.sum(axis=1) <= 0  # degrade to team-wide weights, scorer excluded
                    if fall.any():
                        w[fall] = np.where(~taken[sel][fall], team_a[a_no - 1][rows[sel]][fall], 0.0)
                ai, ok = _choose(rng, w)
                good = sel[ok]
                if good.size:
                    assist_idx[a_no - 1, good] = ai[ok]
                    np.add.at(out["assists"], (rows[good], ai[ok]), 1)
                    if "a_state" in out:
                        np.add.at(out["a_state"], (ai[ok], state, a_no - 1), 1)
                    taken[good, ai[ok]] = True
                    if state == SH:
                        np.add.at(out["sh"], (rows[good], ai[ok]), 1)
            if EVENT_LOG is not None:
                EVENT_LOG.append((tp.code, state, rows.copy(), sc_i.copy(), assist_idx[0].copy(), assist_idx[1].copy(), dm[rows] > 0))


def _presence(rng, tp: _Team, n: int, sim_cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Dressed flags and dm = dressed x TOI multiplier x scale, both (n, Ni)."""
    ni = tp.scale.size
    listed = np.zeros(ni, bool)
    listed[:tp.n_listed] = True
    dressed = np.ones((n, ni), bool)
    dressed[:, :tp.n_listed] = rng.random((n, tp.n_listed)) < tp.p_dress[:tp.n_listed]
    cvs = tp.toi_cv[None, :] * listed[None, :]
    toi_mult = np.clip(1.0 + cvs * rng.standard_normal((n, ni)), sim_cfg["skater"]["toi_floor"], sim_cfg["skater"]["toi_cap"])
    return dressed, dressed * toi_mult * tp.scale[None, :]


def _calibrate_assists(tp: _Team, sim_cfg: dict) -> None:
    """Scale each person's assist weights so a pilot allocation reproduces the ParamTable's expected
    assists per person and strength. The on-ice group structure (who is out with whom) is kept; only
    the marginal is pinned, because a scorer cannot assist his own goal and that alone pushes assists
    from frequent scorers to their linemates. Deterministic: fixed pilot seed, common draws each round."""
    c = sim_cfg["allocation"]
    if not c["calibrate"]:
        return
    ni = tp.scale.size
    w = tp.p_dress * tp.scale
    lam = [float((w * tp.g[:, s]).sum()) for s in range(3)]
    target = w[:, None, None] * np.stack([tp.a1, tp.a2], axis=2)  # (Ni, 3, 2) expected assists per game
    n = int(c["pilot_scenarios"])
    for _ in range(int(c["pilot_rounds"])):
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(c["pilot_seed"]), 1])))
        _, dm = _presence(rng, tp, n, sim_cfg)
        counts = {s: rng.poisson(lam[s], n) for s in range(3)}
        out = {"goals": np.zeros((n, ni), np.int32), "assists": np.zeros((n, ni), np.int32),
               "sh": np.zeros((n, ni), np.int32), "a_state": np.zeros((ni, 3, 2))}
        _allocate(rng, tp, dm, counts, sim_cfg, out)
        got = out["a_state"] / n
        eps = float(c["pilot_eps"]) * target.mean(axis=0, keepdims=True)  # per strength and assist number
        ratio = np.clip((target + eps) / (got + eps), 0.5, 2.0)
        tp.a_mult = np.clip(tp.a_mult * ratio ** float(c["pilot_step"]), float(c["mult_min"]), float(c["mult_max"]))


def _team_skaters(rng, tp: _Team, n: int, goals_by_state: dict, pace_own: np.ndarray, pace_opp: np.ndarray,
                  sim_cfg: dict, var: float) -> dict:
    ni = tp.scale.size
    dressed, dm = _presence(rng, tp, n, sim_cfg)
    out = {"goals": np.zeros((n, ni), np.int32), "assists": np.zeros((n, ni), np.int32), "sh": np.zeros((n, ni), np.int32)}
    _allocate(rng, tp, dm, goals_by_state, sim_cfg, out)
    saved = _nb(rng, dm * tp.saved[None, :] * pace_own[:, None], tp.phi_saved[None, :])
    blocks = _nb(rng, dm * tp.blk[None, :] * pace_opp[:, None], tp.phi_blocks[None, :])
    out.update(dressed=dressed, saved=saved, blocks=blocks, dm=dm)
    return out


def _goalies(rng, gp: _Goalies, n: int, ga: np.ndarray, saves: np.ndarray, win: np.ndarray, beyond_reg: np.ndarray,
             sim_cfg: dict, o: oc.Outcomes) -> None:
    """Fill one team's goalie columns. ga: charged goals against; saves: opposing non-goal shots."""
    c = sim_cfg["goalie"]
    m = gp.cols.size
    w = np.append(gp.p_start, gp.unlisted)
    w = w / w.sum()
    start = np.minimum((np.cumsum(w)[None, :] <= rng.random(n)[:, None]).sum(axis=1), m)  # m = unlisted
    pc = gp.pull_c[start]
    p_pull = np.where(ga >= c["pull_min_ga"], np.minimum(c["pull_max"], pc * ga.astype(float) ** 2 / c["pull_ga_ref"]), 0.0)
    pulled = rng.random(n) < p_pull
    f = np.where(pulled, rng.uniform(c["pull_frac"][0], c["pull_frac"][1], n), 1.0)
    ga_s = np.where(pulled, np.clip(rng.binomial(ga, f), np.minimum(ga, c["pull_min_ga"]), ga), ga)
    sv_s = np.where(pulled, rng.binomial(saves, f), saves)
    # relief: another listed goalie by start weight, else the unlisted slot
    rw = np.tile(w, (n, 1))
    rw[np.arange(n), start] = 0.0
    relief, has = _choose(rng, rw)
    relief = np.where(has, relief, m)  # no other listed goalie: the unlisted slot relieves
    of_record = np.where(pulled & (f < 0.5), relief, start)
    for slot in range(m + 1):
        st = start == slot
        re = pulled & (relief == slot)
        if slot < m:
            col = gp.cols[slot]
            o.dressed[:, col] = st | re
            o.saves[:, col] = np.where(st, sv_s, 0) + np.where(re, saves - sv_s, 0)
            o.ga[:, col] = np.where(st, ga_s, 0) + np.where(re, ga - ga_s, 0)
            rec = of_record == slot
            o.decision[:, col] = np.where(rec, np.where(win, oc.DECISIONS["W"],
                                                        np.where(beyond_reg, oc.DECISIONS["OTL"], oc.DECISIONS["L"])), 0)
            o.shutout[:, col] = (st & ~pulled & (ga == 0)).astype(np.int8)


def _shootout(rng, tp_home: _Team, tp_away: _Team, dm_h: np.ndarray, dm_a: np.ndarray, so: np.ndarray, home_win: np.ndarray,
              p_goal: float, o: oc.Outcomes) -> None:
    rows = np.flatnonzero(so)
    if rows.size == 0:
        return
    m = rows.size
    a, b = rng.binomial(3, p_goal, m), rng.binomial(3, p_goal, m)
    for _ in range(12):
        tie = a == b
        if not tie.any():
            break
        a = a + np.where(tie, rng.random(m) < p_goal, 0)
        b = b + np.where(tie, rng.random(m) < p_goal, 0)
    tie = a == b
    a = a + tie  # a residual tie (probability ~ 0) is broken for the round's first shooter
    hw = home_win[rows]
    win_k, lose_k = np.maximum(a, b), np.minimum(a, b)
    k_home = np.where(hw, win_k, lose_k)
    k_away = np.where(hw, lose_k, win_k)
    for tp, dm, k in ((tp_home, dm_h, k_home), (tp_away, dm_a, k_away)):
        w = dm[rows] * tp.g[:, EV] * tp.is_f[None, :]
        w = np.where(w.sum(axis=1, keepdims=True) > 0, w, dm[rows] * tp.is_f[None, :])
        keys = np.log(np.maximum(w, 1e-300)) - np.log(-np.log(rng.random(w.shape)))  # Gumbel top-k
        keys = np.where(w > 0, keys, -np.inf)
        order = np.argsort(-keys, axis=1)
        for r in range(int(k.max()) if k.size else 0):
            sel = np.flatnonzero(k > r)
            cols = order[sel, r]
            ok = cols < tp.n_listed
            o.so_goals[rows[sel[ok]], tp.cols[cols[ok]]] += 1


# -- simulation ------------------------------------------------------------------------------------------

def _rng(seed: int, purpose: int, chunk: int, game: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(seed), int(purpose), int(chunk), int(game)])))


def simulate_chunk(prep: Prep, n: int, seed: int, purpose: str = "design", chunk: int = 0) -> oc.Outcomes:
    cfg = prep.slate.cfg
    sim_cfg = cfg
    o = oc.empty(prep.person_keys, prep.is_goalie, n)
    tix = {c: i for i, c in enumerate(prep.team_order)}
    o.teams = list(prep.team_order)
    o.games = [g.key for g in prep.slate.games]
    T, G = len(prep.team_order), len(prep.slate.games)
    o.team_goals = np.zeros((n, T), np.int16)
    o.team_en = np.zeros((n, T), np.int16)
    o.team_sog = np.zeros((n, T), np.int16)
    o.team_pace = np.zeros((n, T), np.float32)
    o.game_tie = np.zeros((n, G), bool)
    o.game_ot = np.zeros((n, G), bool)
    o.game_so = np.zeros((n, G), bool)
    o.game_home_win = np.zeros((n, G), bool)
    o.seed, o.purpose = int(seed), purpose
    pp_mean = float(cfg["skater"]["pp_opps_mean"])
    cap = float(cfg["skater"]["pp_share_cap"])
    for gi, spec in enumerate(prep.slate.games):
        rng = _rng(seed, cfg["purposes"][purpose], chunk, gi)
        rules = rs.Rules.from_config(cfg, spec.game_type)
        th, ta = prep.teams[spec.home], prep.teams[spec.away]
        pace_h, pace_a = _pace(rng, n, prep.pace_var), _pace(rng, n, prep.pace_var)
        opps_h, opps_a = rng.poisson(pp_mean, n), rng.poisson(pp_mean, n)
        d = rs.sample(rng, spec.rates.lambda_home, spec.rates.lambda_away, n, rules)
        ot_h = d.ot_goal & d.home_win
        ot_a = d.ot_goal & ~d.home_win
        res = {}
        for tp, opp_tp, pace_own, pace_opp, n_own_pp, n_opp_pp, reg, extra_ev, key in (
                (th, ta, pace_h, pace_a, opps_h, opps_a, d.reg_h, d.eq_h + d.en_h + ot_h, "h"),
                (ta, th, pace_a, pace_h, opps_a, opps_h, d.reg_a, d.eq_a + d.en_a + ot_a, "a")):
            p_pp = np.minimum(cap, tp.shares[PP] * n_own_pp / pp_mean)
            p_sh = tp.shares[SH] * n_opp_pp / pp_mean
            n_pp = rng.binomial(reg, p_pp)
            n_sh = rng.binomial(reg - n_pp, np.minimum(1.0, p_sh / np.maximum(1e-9, 1.0 - p_pp)))
            counts = {EV: (reg - n_pp - n_sh) + extra_ev, PP: n_pp, SH: n_sh}
            res[key] = _team_skaters(rng, tp, n, counts, pace_own, pace_opp, sim_cfg, prep.pace_var)
        team_goals = {"h": d.reg_h + d.eq_h + d.en_h + ot_h, "a": d.reg_a + d.eq_a + d.en_a + ot_a}
        team_en = {"h": d.en_h, "a": d.en_a}
        beyond = d.tie
        for key, tp, opp_key, is_home in (("h", th, "a", True), ("a", ta, "h", False)):
            r_own, r_opp = res[key], res[opp_key]
            cols = tp.cols
            k = tp.n_listed
            o.dressed[:, cols] = r_own["dressed"][:, :k]
            o.goals[:, cols] = r_own["goals"][:, :k]
            o.assists[:, cols] = r_own["assists"][:, :k]
            o.sh_pts[:, cols] = r_own["sh"][:, :k]
            o.sog[:, cols] = r_own["goals"][:, :k] + r_own["saved"][:, :k]
            o.blocks[:, cols] = r_own["blocks"][:, :k]
            ti = tix[tp.code]
            o.team_goals[:, ti] = team_goals[key]
            o.team_en[:, ti] = team_en[key]
            o.team_sog[:, ti] = (r_own["goals"] + r_own["saved"]).sum(axis=1)
            o.team_pace[:, ti] = pace_h if is_home else pace_a
            win = d.home_win if is_home else ~d.home_win
            ga = (team_goals[opp_key] - team_en[opp_key]).astype(np.int64)
            saves = r_opp["saved"].sum(axis=1).astype(np.int64)
            _goalies(rng, tp.goalies, n, ga, saves, win, beyond, sim_cfg, o)
        _shootout(rng, th, ta, res["h"]["dm"], res["a"]["dm"], d.so, d.home_win, float(cfg["skater"]["so_goal_p"]), o)
        o.game_tie[:, gi], o.game_ot[:, gi], o.game_so[:, gi], o.game_home_win[:, gi] = d.tie, d.ot_goal, d.so, d.home_win
    return o


def chunk_bounds(n: int, cfg: dict) -> list[int]:
    size = int(cfg["chunk_size"])
    return [min(size, n - i) for i in range(0, n, size)]


def check_memory(prep: Prep, cfg: dict) -> float:
    """Estimated MB for one chunk of outcomes and scratch; raises when it cannot fit the cap."""
    p = len(prep.person_keys)
    est = int(cfg["chunk_size"]) * p * (10 * 2 + 4 * 8) / 1e6
    if est > float(cfg["memory_cap_mb"]):
        raise MemoryError(f"one chunk needs about {est:.0f} MB, over the {cfg['memory_cap_mb']} MB cap; "
                          "lower chunk_size in config/sim.yaml (changing it changes the draws)")
    return est


def iter_chunks(slate: SlateSpec, params, n: int, seed: int, purpose: str = "design"):
    prep = prepare(slate, params)
    check_memory(prep, slate.cfg)
    for ci, size in enumerate(chunk_bounds(n, slate.cfg)):
        yield simulate_chunk(prep, size, seed, purpose, ci)


def simulate(slate: SlateSpec, params, n: int, seed: int, purpose: str = "design") -> oc.Outcomes:
    """All n scenarios in memory (use iter_chunks or sim.cache for a large n)."""
    return oc.concat(list(iter_chunks(slate, params, n, seed, purpose)))
