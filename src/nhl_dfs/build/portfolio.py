"""Objective-aware portfolio: candidate discovery, frontier and fee-weighted allocation (C8, plan
section 7 "Portfolio policies", "Contest treatment and optimization").

Discovery (`discover`, design scenarios only; the referee stream never feeds it): a search
allocation of about 65% central-case candidates (scenario-mean objective with perturbation), 25%
alternate viable cores (conditional means in the scenarios where one team's skaters score in their
top 20%, cycling teams) and 10% "priors wrong" (conditional means where the field's chalk team
scores in its bottom 20%). The mix is a search allocation, not a claim about probabilities.

Selection (`select`, selection scenarios): every candidate is scored once in the shared scenarios
and ranked against each contest's field (objectives.ranks). Entries are filled in family order
(config/contest_families.yaml selection.family_order), then by fee (dearest first), then file order;
each takes the candidate with the best
    score = E[family utility in dollars] - kappa x total fees x P(portfolio loses >= 80% of fees)
among those that pass the caps, ordered by the deterministic tie-break inside one band (band >= the
score's Monte Carlo SE; ownership never moves a choice past one band; cash and satellite use no
ownership). The user's entries already placed in a contest count as copies and opponents. Caps are
relaxed in the plan's order when nothing passes (OVERLAP, then EXPOSURE, then REPEAT), recorded.

Frontier: one portfolio per knob setting (five kappas, config/risk.yaml), each measured jointly
(own entries ranked together). Dominated points are dropped from the report; the choice is the
highest tail utility with P(lose >= 80%) and the fee concentrations inside the budget, else the
least-risk point, recorded. Monotonicity in the knob is not assumed. WTA entries are allocated by
first-place equity (their family utility).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from nhl_dfs.build import exposure, tiebreak
from nhl_dfs.build import objectives as ob
from nhl_dfs.build.assign import Relaxation
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import Mode, lineup_key


@dataclass(frozen=True)
class RiskBudget:
    p_lose80_max: float
    goalie_fee_share_max: float | None
    game_fee_share_max: float | None
    captain_fee_share_max: float | None

    @classmethod
    def from_config(cls, risk_cfg: dict, mode: Mode, caps: exposure.Caps) -> "RiskBudget":
        b = risk_cfg["budget"][mode.value]
        return cls(float(b["p_lose80_max"]), caps.goalie_fee_share, caps.game_fee_share, caps.captain_fee_share)


@dataclass
class Choice:
    entry_id: str
    contest_id: str
    family: str  # contest family
    cand: int
    key: str
    cand_family: str  # discovery family
    score: float  # dollars
    score_se: float
    band: float
    band_index: int
    own_pct: float
    dup: float
    dup_measure: str
    level: int  # 0 all caps, 1 overlap dropped, 2 exposure dropped, 3 repeat


@dataclass
class FrontierPoint:
    kappa: float
    tail_utility: float
    tail_utility_se: float
    p_lose80: float
    p_lose80_se: float
    exp_payout: float
    goalie_share_max: float
    game_share_max: float | None
    captain_share_max: float | None
    feasible: bool
    dominated: bool = False
    reasons: list[str] = field(default_factory=list)
    portfolio_key: str = ""
    kappas: list[float] = field(default_factory=list)  # every knob that produced this portfolio

    def record(self) -> dict:
        return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Selection:
    by_entry: dict[str, tuple[str, ...]]
    choices: dict[str, Choice]
    frontier: list[FrontierPoint]
    chosen_kappa: float
    chosen_reason: str
    relaxations: list[Relaxation]
    family_mix: dict[str, int]
    families_target: tuple[float, ...]
    portfolio: ob.PortfolioMetrics | None = None
    screened: dict = field(default_factory=dict)


# -- discovery ------------------------------------------------------------------------------------

def role_objective(scen: ob.ScenarioSet, pool, rows: np.ndarray | None = None) -> dict[str, float]:
    """Per-role objective in points from scenario means (the Captain row at 1.5x); rows restricts to a
    scenario subset (a cluster)."""
    base = scen.base if rows is None else scen.base[rows]
    m = base.mean(axis=0) / 10.0
    out = {}
    for i, r in enumerate(scen.role_ids):
        v = float(m[i])
        if pool.mode is Mode.SHOWDOWN and "CPT" in pool.by_role_id[r].roster_positions:
            v *= 1.5
        out[r] = v
    return out


def team_points(scen: ob.ScenarioSet, pool) -> dict[str, np.ndarray]:
    """Per team: its skaters' summed base points per scenario (one column per person)."""
    cols: dict[str, list[int]] = defaultdict(list)
    seen = set()
    for r in pool.rows:
        if r.is_goalie or r.person_key in seen or r.role_id not in scen.col:
            continue
        if pool.mode is Mode.SHOWDOWN and "CPT" in r.roster_positions:
            continue
        seen.add(r.person_key)
        cols[r.team].append(scen.col[r.role_id])
    return {t: scen.base[:, c].sum(axis=1) for t, c in sorted(cols.items())}


def discover(pool, design: ob.ScenarioSet, n_total: int, runtime: dict, risk_cfg: dict, *, seed: int,
             chalk_team: str | None = None, families: Sequence[float] | None = None,
             time_limit_s: float | None = None) -> tuple[list[Candidate], dict]:
    """Candidates tagged central / alternate:<team> / priors_wrong:<team>. Returns (candidates, report)."""
    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.models.field import split_counts

    d = risk_cfg["discovery"]
    fam = tuple(families if families is not None else d["families"])
    c = runtime["candidates"]
    per = split_counts(list(fam), int(n_total))
    budget = float(time_limit_s if time_limit_s is not None else c["time_limit_total_s"])
    tp = team_points(design, pool)
    jobs: list[tuple[str, dict, int]] = [("central", role_objective(design, pool), per[0])]
    teams = sorted(tp)
    for t, k in zip(teams, split_counts([1.0] * len(teams), per[1])):
        if k:
            rows = tp[t] >= np.quantile(tp[t], float(d["cluster_quantile"]))
            jobs.append((f"alternate:{t}", role_objective(design, pool, rows), k))
    if per[2] and teams:
        t = chalk_team if chalk_team in tp else max(teams, key=lambda x: float(tp[x].mean()))
        rows = tp[t] <= np.quantile(tp[t], float(d["failure_quantile"]))
        jobs.append((f"priors_wrong:{t}", role_objective(design, pool, rows), per[2]))
    out: list[Candidate] = []
    seen: set[str] = set()
    report = {"target": dict(zip(("central", "alternate", "priors_wrong"), per)), "made": Counter()}
    total_k = sum(k for _, _, k in jobs) or 1
    for i, (name, obj, k) in enumerate(jobs):
        try:
            got = cand_mod.generate(pool, pool.mode, obj, k, seed=seed + 101 * i, perturb_sd=float(c["perturb_sd_points"]),
                                    time_limit_total_s=max(1.0, budget * k / total_k),
                                    min_pairwise_diff=int(c["min_pairwise_diff"]))
        except cand_mod.SolverUnavailable:
            got = []
        for g in got:
            if g.key in seen:
                continue
            seen.add(g.key)
            out.append(Candidate(g.role_ids, g.key, g.objective_value, name))
            report["made"][name.split(":")[0]] += 1
    report["made"] = dict(report["made"])
    return out, report


# -- selection ------------------------------------------------------------------------------------

@dataclass
class _ContestState:
    contest: ob.Contest
    G: np.ndarray  # (S, K) int32 weight strictly above each candidate: field plus own entries placed
    E: np.ndarray
    own_pct: np.ndarray  # (K,) lineup ownership sum in this contest's field (percent)
    dup: np.ndarray
    dup_name: str
    policy: str


def _prepare(cand_scores: np.ndarray, fields: Mapping[str, tuple], contests: Mapping[str, ob.Contest],
             candidates: Sequence[Candidate], pool, fam_cfg: dict, risk_cfg: dict, own_by_contest, dup_by_contest,
             field_cal, specs: Mapping[str, ob.FieldSpec]) -> dict[str, _ContestState]:
    S, K = cand_scores.shape
    out = {}
    GE: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    groups: dict = defaultdict(list)
    for cid in contests:
        spec = specs[cid]
        groups[("w", tuple(spec.keys)) if spec.mode == "weighted" else ("s", cid)].append(cid)
    for key, cids in groups.items():
        fs = fields[cids[0]][0]
        wl = [fields[cid][1] for cid in cids]
        for cid in cids:  # ranks never exceed the field size: int16 below 32,000 entries halves the memory
            dt = np.int16 if contests[cid].field_size < 32000 else np.int32
            GE[cid] = (np.zeros((S, K), dt), np.zeros((S, K), dt))
        step = ob._chunk_rows(S, fs.shape[1] * 64 + K * (96 + 16 * len(cids)), float(risk_cfg["objectives"]["memory_cap_mb"]))
        for a in range(0, S, step):
            b = min(S, a + step)
            for cid, (g, e) in zip(cids, ob.ranks_multi(cand_scores[a:b], np.asarray(fs[a:b]), wl)):
                GE[cid][0][a:b], GE[cid][1][a:b] = g, e
    for cid, ct in contests.items():
        G, E = GE[cid]
        own = own_by_contest.get(cid, {})
        counts = dup_by_contest.get(cid, {})
        own_pct = np.asarray([sum(float(own.get(r, 0.0)) for r in c.role_ids) for c in candidates])
        dups = [tiebreak.dup_measure(c.role_ids, c.key, own, counts, field_cal, pool) for c in candidates]
        out[cid] = _ContestState(ct, G, E, own_pct, np.asarray([d for d, _ in dups]), dups[0][1] if dups else "proxy",
                                 fam_cfg["families"][ct.family]["selection"])
    return out


class _Curve:
    """A contest's prefix arrays, built once."""

    def __init__(self, ct: ob.Contest, top_pct: float):
        self.ct = ct
        self.L = ct.field_size
        self.cash = ct.prefix(ct.prizes_cents)
        self.seatp = ct.prefix(ct.seats.astype(np.int64))
        self.top_k = max(1, int(math.floor(top_pct * self.L)))
        self.first = int(ct.prizes_cents[0]) if ct.paid else 0
        self.face = ct.ticket_face_cents or 0

    def pay_util(self, G: np.ndarray, E: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(payout cents, family utility cents) for rank blocks G, E (own copies included)."""
        G = G.astype(np.int64)
        T = E.astype(np.int64) + 1
        hi, lo = np.minimum(G + T, self.L), np.minimum(G, self.L)
        pay = (self.cash[hi] - self.cash[lo]) // T
        fam = self.ct.family
        if fam == "large_gpp":
            util = (self.cash[np.minimum(G + T, self.top_k)] - self.cash[np.minimum(G, self.top_k)]) // T
        elif fam == "wta":
            util = np.floor((G == 0) / T * self.first).astype(np.int64)
        elif fam == "satellite":
            util = np.floor((self.seatp[hi] - self.seatp[lo]) / T * self.face).astype(np.int64) + pay
        else:
            util = pay
        return pay, util


def _greedy(kappa: float, order: list, entry_contest: Mapping[str, str], fees: Mapping[str, int], cand_scores: np.ndarray,
            candidates: Sequence[Candidate], states: dict, caps: exposure.Caps, pool, risk_cfg: dict,
            role_mean: Mapping[str, float], tournament: set[str], sleeve_max: int, mem_share: float = 1.0):
    """One knob's fill. The prepared field ranks (states) are shared across knobs and never copied; the
    user's own entries placed so far are kept as int16 increments. Each step aggregates per candidate
    over scenario chunks (bounded memory), then computes the chosen candidate's payout column."""
    S, K = cand_scores.shape
    tp = float(risk_cfg["objectives"]["top_pct"])
    curves = {cid: _Curve(st.contest, tp) for cid, st in states.items()}
    inc = {cid: (np.zeros((S, K), st.G.dtype), np.zeros((S, K), st.G.dtype)) for cid, st in states.items()}
    step = ob._chunk_rows(S, K * 80, float(risk_cfg["objectives"]["memory_cap_mb"]) * mem_share)
    total_fees = sum(int(fees[e]) for e in order)
    thr = 0.2 * total_fees  # losing >= 80% of fees <=> total payout <= 20% of fees
    pay_total = np.zeros(S, np.int64)
    games = exposure.game_of(pool)
    persons = [{pool.by_role_id[r].person_key for r in c.role_ids} for c in candidates]
    goalies = [exposure.goalies_in(c.role_ids, pool) for c in candidates]
    captains = [exposure.captain_of(c.role_ids, pool) for c in candidates]
    pgame = [exposure.primary_game(c.role_ids, role_mean, games, pool) for c in candidates]
    person_n, goalie_n, captain_n, used = Counter(), Counter(), Counter(), Counter()
    fee_g, fee_game, fee_c = Counter(), Counter(), Counter()
    placed: list[set] = []
    stress = 0
    choices: dict[str, Choice] = {}
    relax: list[Relaxation] = []

    def passes(k: int, eid: str, level: int) -> bool:
        if used[candidates[k].key] and level < 3:
            return False
        if candidates[k].family.startswith("priors_wrong") and stress >= sleeve_max and level < 3:
            return False
        if level >= 2:
            return True
        f = int(fees[eid])
        if caps.goalie_fee_share is not None and any((fee_g[g] + f) / total_fees > caps.goalie_fee_share + 1e-9 for g in goalies[k]):
            return False
        if caps.game_fee_share is not None and pgame[k] and (fee_game[pgame[k]] + f) / total_fees > caps.game_fee_share + 1e-9:
            return False
        if caps.captain_fee_share is not None and captains[k] and (fee_c[captains[k]] + f) / total_fees > caps.captain_fee_share + 1e-9:
            return False
        if eid in tournament:
            if caps.person is not None and any(person_n[p] >= caps.person for p in persons[k]):
                return False
            if caps.goalie is not None and any(goalie_n[g] >= caps.goalie for g in goalies[k]):
                return False
            if caps.captain is not None and captains[k] and captain_n[captains[k]] >= caps.captain:
                return False
            if level < 1 and caps.max_overlap is not None and any(len(persons[k] & o) > caps.max_overlap for o in placed):
                return False
        return True

    for eid in order:
        cid = entry_contest[eid]
        st, cv = states[cid], curves[cid]
        gi, ei = inc[cid]
        su, sq, n80 = np.zeros(K), np.zeros(K), np.zeros(K)
        for a in range(0, S, step):
            b = min(S, a + step)
            pay, util = cv.pay_util(st.G[a:b] + gi[a:b], st.E[a:b] + ei[a:b])
            u = util / 100.0
            su += u.sum(axis=0)
            sq += (u * u).sum(axis=0)
            n80 += ((pay_total[a:b, None] + pay) <= thr).sum(axis=0)
        mean_u = su / S
        se_u = np.sqrt(np.maximum(sq / S - mean_u ** 2, 0.0) / S)
        p80 = n80 / S
        score = mean_u - kappa * total_fees / 100.0 * p80
        se = np.sqrt(se_u ** 2 + (kappa * total_fees / 100.0) ** 2 * p80 * (1 - p80) / S)
        pick, level = None, 0
        for level in (0, 1, 2, 3):
            ok = [k for k in range(K) if passes(k, eid, level)]
            if not ok:
                continue
            items = [tiebreak.Item(candidates[k].key, float(score[k]), float(se[k]), float(st.own_pct[k]), float(st.dup[k]), k)
                     for k in ok]
            pick = tiebreak.rank(items, risk_cfg, st.policy)[0]
            break
        k = pick.item.ref
        if level:
            kind = {1: "OVERLAP", 2: "EXPOSURE", 3: "REPEAT"}[level]
            relax.append(Relaxation(eid, kind, f"kappa {kappa:g}: no candidate passed the caps at level {level - 1}"))
        choices[eid] = Choice(eid, cid, st.contest.family, k, candidates[k].key, candidates[k].family, float(score[k]),
                              float(se[k]), pick.band, pick.band_index, float(st.own_pct[k]), float(st.dup[k]),
                              st.dup_name, level)
        # update state: fees, counts, own copies in this contest
        f = int(fees[eid])
        for g in goalies[k]:
            fee_g[g] += f
        if pgame[k]:
            fee_game[pgame[k]] += f
        if captains[k]:
            fee_c[captains[k]] += f
        if eid in tournament:
            person_n.update(persons[k])
            goalie_n.update(goalies[k])
            if captains[k]:
                captain_n[captains[k]] += 1
            placed.append(persons[k])
        used[candidates[k].key] += 1
        stress += candidates[k].family.startswith("priors_wrong")
        pay_k, _ = cv.pay_util(st.G[:, [k]] + gi[:, [k]], st.E[:, [k]] + ei[:, [k]])
        pay_total += pay_k[:, 0]
        for a in range(0, S, step):
            b = min(S, a + step)
            own = cand_scores[a:b, [k]]
            gi[a:b] += own > cand_scores[a:b]
            ei[a:b] += own == cand_scores[a:b]
    return choices, relax, inc


def _joint_from_states(choices: Mapping[str, Choice], states: Mapping[str, _ContestState], cand_scores: np.ndarray,
                       contests_eval: Mapping[str, ob.ContestEval], risk_cfg: dict) -> dict:
    """(Uses only the field ranks; the own-entry terms are recomputed exactly, pairwise.)"""
    """Each contest's own entries ranked jointly, from the field ranks prepared for every candidate."""
    out = {}
    for cid, ce in contests_eval.items():
        eids = [e for e in ce.entry_ids if e in choices]
        if not eids:
            continue
        idx = [choices[e].cand for e in eids]
        st = states[cid]
        pg, pe = ob.own_pairwise(cand_scores[:, idx])
        out[cid] = (eids, ob.metrics_from_ranks(st.G[:, idx].astype(np.int64) + pg, st.E[:, idx].astype(np.int64) + pe,
                                                st.contest, risk_cfg))
    return out


def _frontier_point(kappa: float, by_entry: dict, contests_eval: dict, scen: ob.ScenarioSet, pool, fees: Mapping[str, int],
                    budget: RiskBudget, risk_cfg: dict, joint: dict | None = None) -> tuple[FrontierPoint, ob.PortfolioMetrics]:
    pm = ob.portfolio_metrics(by_entry, contests_eval, scen, pool=pool, fees_cents=fees, cfg=risk_cfg, joint=joint)
    c = pm.concentration
    gmax = max(c["goalie"].values(), default=0.0)
    game_max = max(c["game"].values(), default=0.0) if c["n_games"] > 1 else None
    cap_max = max(c["captain"].values(), default=0.0) if pool.mode is Mode.SHOWDOWN else None
    reasons = []
    if pm.p_lose80 > budget.p_lose80_max + 1e-12:
        reasons.append(f"P(lose >= 80%) {pm.p_lose80:.3f} > {budget.p_lose80_max:.2f}")
    if budget.goalie_fee_share_max is not None and gmax > budget.goalie_fee_share_max + 1e-9:
        reasons.append(f"goalie fee share {gmax:.2f} > {budget.goalie_fee_share_max:.2f}")
    if budget.game_fee_share_max is not None and game_max is not None and game_max > budget.game_fee_share_max + 1e-9:
        reasons.append(f"game fee share {game_max:.2f} > {budget.game_fee_share_max:.2f}")
    if budget.captain_fee_share_max is not None and cap_max is not None and cap_max > budget.captain_fee_share_max + 1e-9:
        reasons.append(f"Captain fee share {cap_max:.2f} > {budget.captain_fee_share_max:.2f}")
    return FrontierPoint(kappa, pm.tail_utility, pm.tail_utility_se, pm.p_lose80, pm.p_lose80_se, pm.exp_payout, gmax,
                         game_max, cap_max, not reasons, False, reasons), pm


def mark_dominated(points: list[FrontierPoint]) -> None:
    """A point is dominated when another has tail utility >= and P(lose >= 80%) <= with one strict."""
    for p in points:
        p.dominated = any((q.tail_utility >= p.tail_utility and q.p_lose80 <= p.p_lose80)
                          and (q.tail_utility > p.tail_utility or q.p_lose80 < p.p_lose80) for q in points if q is not p)


def frontier_report(points: list[FrontierPoint]) -> list[FrontierPoint]:
    """The reported frontier: non-dominated points by increasing risk (call mark_dominated first); knob
    settings that produced the same portfolio are one row listing every such knob."""
    rows: dict[str, FrontierPoint] = {}
    for p in sorted(points, key=lambda p: p.kappa):
        if p.dominated:
            continue
        key = p.portfolio_key or f"k{p.kappa}"
        if key in rows:
            rows[key].kappas.append(p.kappa)
        else:
            p.kappas = [p.kappa]
            rows[key] = p
    return sorted(rows.values(), key=lambda p: (p.p_lose80, -p.tail_utility, p.kappa))


def choose(points: list[FrontierPoint]) -> tuple[FrontierPoint, str]:
    ok = [p for p in points if p.feasible]
    if ok:
        best = max(ok, key=lambda p: (p.tail_utility, -p.p_lose80, -p.kappa))
        return best, f"highest tail utility inside the risk budget (kappa {best.kappa:g})"
    best = min(points, key=lambda p: (p.p_lose80, -p.tail_utility, p.kappa))
    return best, ("no knob setting meets the risk budget; least-risk point chosen (kappa "
                  f"{best.kappa:g}: " + "; ".join(best.reasons) + ")")


def select(candidates: Sequence[Candidate], role_base: ob.ScenarioSet, fields: Mapping[str, ob.FieldSpec],
           contests: Mapping[str, ob.Contest], entries, caps: exposure.Caps, risk: RiskBudget,
           families: tuple[float, ...] = (0.65, 0.25, 0.10), *, seed: int, pool, fam_cfg: dict, risk_cfg: dict,
           own_by_contest: Mapping[str, Mapping[str, float]] | None = None,
           dup_by_contest: Mapping[str, Mapping[str, float]] | None = None, field_cal=None,
           fees_cents: Mapping[str, int] | None = None) -> Selection:
    """Fill every entry from the candidates on the selection scenarios; see the module docstring."""
    from nhl_dfs.contracts.statuses import FieldCalibration

    if not candidates:
        raise ValueError("no candidates to select from")
    field_cal = field_cal or FieldCalibration.PRIOR
    rows = list(getattr(entries, "entries", entries))
    entry_contest = {e.entry_id: str(e.contest_id) for e in rows}
    fees = dict(fees_cents) if fees_cents is not None else {e.entry_id: contests[str(e.contest_id)].fee_cents for e in rows}
    fam_order = {f: i for i, f in enumerate(fam_cfg["selection"]["family_order"])}
    file_pos = {e.entry_id: i for i, e in enumerate(rows)}
    order = sorted(entry_contest, key=lambda e: (fam_order[contests[entry_contest[e]].family], -fees[e], file_pos[e]))
    tournament = {e for e in entry_contest if contests[entry_contest[e]].family not in ("cash",)}
    cand_scores = role_base.scores([c.role_ids for c in candidates], pool.mode).full()
    fscores = {cid: fields[cid].scores(role_base, pool.mode) for cid in contests}
    states = _prepare(cand_scores, fscores, contests, candidates, pool, fam_cfg, risk_cfg, own_by_contest or {},
                      dup_by_contest or {}, field_cal, fields)
    role_mean = role_base.means_tenths()
    n_in = Counter(entry_contest.values())
    keep = screen(states, candidates, n_in, risk_cfg, S=cand_scores.shape[0])
    screened = {"candidates_in": len(candidates), "kept": len(keep)}
    if len(keep) < len(candidates):
        candidates = [candidates[i] for i in keep]
        cand_scores = np.ascontiguousarray(cand_scores[:, keep])
        states = {cid: _ContestState(st.contest, np.ascontiguousarray(st.G[:, keep]), np.ascontiguousarray(st.E[:, keep]),
                                     st.own_pct[keep], st.dup[keep], st.dup_name, st.policy) for cid, st in states.items()}
    sleeve = max(1, int(round(float(risk_cfg["discovery"]["stress_sleeve_max"]) * len(order))))
    contests_eval = {cid: ob.ContestEval(ct, fields[cid], [e for e in order if entry_contest[e] == cid])
                     for cid, ct in contests.items()}
    kappas = [float(k) for k in risk_cfg["frontier"]["kappas"]]
    threads = max(1, min(len(kappas), int(risk_cfg.get("selection", {}).get("threads", 1))))

    def one(kappa: float):
        ch, rl, inc = _greedy(kappa, order, entry_contest, fees, cand_scores, candidates, states, caps, pool, risk_cfg,
                              role_mean, tournament, sleeve, mem_share=1.0 / len(kappas))  # not threads: chunking fixed
        del inc
        by_entry = {e: candidates[ch[e].cand].role_ids for e in order}
        joint = _joint_from_states(ch, states, cand_scores, contests_eval, risk_cfg)
        pt, pm = _frontier_point(kappa, by_entry, contests_eval, role_base, pool, fees, risk, risk_cfg, joint)
        pt.portfolio_key = "|".join(sorted(lineup_keys(by_entry, pool).values()))
        return pt, (ch, rl, by_entry, pm)

    if threads > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=threads) as ex:
            outs = list(ex.map(one, kappas))  # map keeps knob order: the result does not depend on threads
    else:
        outs = [one(k) for k in kappas]
    points = [o[0] for o in outs]
    results = [o[1] for o in outs]
    mark_dominated(points)
    best, why = choose(points)
    ch, rl, by_entry, pm = results[points.index(best)]
    mix = Counter(c.cand_family.split(":")[0] for c in ch.values())
    ordered = {e.entry_id: by_entry[e.entry_id] for e in rows}
    sel = Selection(ordered, ch, points, best.kappa, why, rl, dict(mix), tuple(families), pm)
    sel.screened = screened
    return sel


def screen(states: Mapping[str, _ContestState], candidates: Sequence[Candidate], n_in: Mapping[str, int], risk_cfg: dict,
           *, S: int) -> list[int]:
    """Candidates kept for the fill: per contest the best max(screen_min, screen_per_entry x its entries) by the
    field-only family utility, plus each discovery family's best screen_per_family there (coverage), unioned."""
    sc = risk_cfg.get("selection", {})
    lo, per, fam_n = int(sc.get("screen_min", 40)), float(sc.get("screen_per_entry", 2)), int(sc.get("screen_per_family", 3))
    tp = float(risk_cfg["objectives"]["top_pct"])
    K = len(candidates)
    keep: set[int] = set()
    fam_of = [c.family.split(":")[0] for c in candidates]
    for cid, st in states.items():
        if not n_in.get(cid):
            continue
        cv = _Curve(st.contest, tp)
        su = np.zeros(K)
        step = ob._chunk_rows(S, K * 80, float(risk_cfg["objectives"]["memory_cap_mb"]))
        for a in range(0, S, step):
            b = min(S, a + step)
            _, util = cv.pay_util(st.G[a:b], st.E[a:b])
            su += util.sum(axis=0)
        order = sorted(range(K), key=lambda k: (-su[k], candidates[k].key))
        keep.update(order[:max(lo, int(math.ceil(per * n_in[cid])))])
        for f in set(fam_of):
            keep.update([k for k in order if fam_of[k] == f][:fam_n])
    return sorted(keep)


def lineup_keys(by_entry: Mapping[str, Sequence[str]], pool) -> dict[str, str]:
    return {e: lineup_key([pool.by_role_id[r] for r in lu], pool.mode) for e, lu in by_entry.items()}
