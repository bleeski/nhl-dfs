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
relaxed in the plan's order when nothing passes (OVERLAP, then EXPOSURE, then CONCENTRATION, then
REPEAT), recorded. The goalie and game caps (build/exposure.py, DOLLARS or LINEUPS mode) survive
EXPOSURE; CONCENTRATION relaxes them only by the smallest step some candidate needs (B36).

Top-N flat contests (C47, flags 64 and 70): in a "Top N Win $X" contest the family utility of a step is the change in
E[min($X, the contest's own payout)], so its own entries are added for P(at least one own entry in the top N) (a tie at
the line counts as its share), not for each entry's own chance summed; inside the band the candidate adding more expected
top-N finishes wins (tiebreak policy "finishes"). The risk term, the caps and every other family are unchanged.

Frontier: one portfolio per knob setting (five kappas, config/risk.yaml), each measured jointly
(own entries ranked together). Dominated points are dropped from the report; the choice is the
highest tail utility with P(lose >= 80%) and the fee concentrations inside the budget, else the
least-risk point, recorded. Monotonicity in the knob is not assumed. WTA entries are allocated by
first-place equity (their family utility).

The goal (C55, BEN flags 107 to 112, `config/risk.yaml` `objectives.goal`): with first_place the family utility of a
large_gpp or small_field entry is its first-place dollars (the tie-adjusted share of first place x the first prize, cents,
`objectives.first_place_cents`), so "tail utility" on the frontier is the portfolio's first-place utility / fees and the
top-1% utility is carried beside it as information (`FrontierPoint.top1pct_utility`); with top1pct (or no key) it is
origin/master's top-1% payout and expected payout. The risk term, the knobs, the budget and the caps are the same under
either goal; a smaller utility makes the same kappa weigh the risk term more.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from nhl_dfs.build import exposure, own_goalie, tiebreak
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
    goalie_lineups_max: int | None = None  # LINEUPS mode (build/exposure.py): entries per goalie
    game_lineups_max: int | None = None  # LINEUPS mode: entries per primary game

    @classmethod
    def from_config(cls, risk_cfg: dict, mode: Mode, caps: exposure.Caps) -> "RiskBudget":
        b = risk_cfg["budget"][mode.value]
        return cls(float(b["p_lose80_max"]), caps.goalie_fee_share, caps.game_fee_share, caps.captain_fee_share,
                   caps.goalie_lineups, caps.game_lineups)


@dataclass
class Choice:
    entry_id: str
    contest_id: str
    family: str  # contest family
    cand: int
    key: str
    cand_family: str  # discovery family
    score: float  # dollars: the knob score of this entry's change to its whole contest (joint, C18); a second entry's can be 0 or negative
    score_se: float
    band: float
    band_index: int
    own_pct: float
    dup: float
    dup_measure: str
    level: int  # 0 all caps, 1 overlap dropped, 2 exposure dropped, 3 goalie/game cap relaxed, 4 repeat
    # C22 (flag 95): the band's anchor, the pick with no ownership order (rank under policy "mean" on the same items).
    # The tie-break moved this pick when anchor_key differs from key.
    anchor_key: str = ""
    anchor_own: float = 0.0
    anchor_dup: float = 0.0
    # C41 (flag 121, B130): the anchor's knob score and SE, so a report can say whether the 3% floor or one SE set the band
    anchor_value: float = 0.0
    anchor_se: float = 0.0


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
    goalie_lineups_max: int = 0  # most entries holding one goalie
    game_lineups_max: int | None = None  # most entries sharing a primary game (multi-game slates)
    top1pct_utility: float | None = None  # C55: the top-1% payout / fees, information when the goal is first place

    def record(self) -> dict:
        return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Prepared:
    """What `select` prepared before its knob loop, kept only when asked (`keep_prepared=True`) for the C41 band report: every
    candidate it received with their selection scores and field ranks against the base field (before the screen), the
    screen's indices and the fill's fixed inputs."""
    candidates: list
    cand_scores: np.ndarray
    states: dict
    keep: list[int]
    order: list[str]
    entry_contest: dict[str, str]
    fees: dict[str, int]
    tournament: set[str]
    role_mean: Mapping[str, float]
    rule_cids: frozenset[str]
    faced: list[bool] | None
    sleeve: int
    n_in: Counter
    goalie_of: list


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
    accounting_gap_cents: int = 0  # largest gap, any knob, between the fill's running payout and the joint recount (0: exact)
    prepared: Prepared | None = None  # C41: select(..., keep_prepared=True) only


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


def goalie_term_points(scen: ob.ScenarioSet, pool, cfg: Mapping, own: Mapping[str, float] | None) -> dict[str, float]:
    """(C20, flag 54, the preregistered term) Points added to each goalie row's discovery objective:
    ceiling_weight * (q90 - mean) + leverage_weight * (top_share - own), where over the design scenarios q90 is the
    ceiling_quantile of the goalie's points, top_share the fraction of scenarios in which he scores the most of any goalie
    in the pool (ties split equally) and own his ownership as a fraction in the biggest contest's sampled field (`own` is
    role_id -> percent; a missing goalie counts 0). Skaters get nothing. Hand-set weights, never fitted (C12)."""
    rows = [r for r in pool.rows if r.is_goalie and r.role_id in scen.col]
    if len(rows) < 2:
        return {}
    pts = scen.base[:, [scen.col[r.role_id] for r in rows]].astype(np.float64) / 10.0
    mean = pts.mean(axis=0)
    hi = np.quantile(pts, float(cfg["ceiling_quantile"]), axis=0)
    tops = pts == pts.max(axis=1, keepdims=True)
    share = (tops / tops.sum(axis=1, keepdims=True)).mean(axis=0)
    wc, wl = float(cfg["ceiling_weight"]), float(cfg["leverage_weight"])
    return {r.role_id: wc * float(hi[j] - mean[j]) + wl * (float(share[j]) - float((own or {}).get(r.role_id, 0.0)) / 100.0)
            for j, r in enumerate(rows)}


def discover(pool, design: ob.ScenarioSet, n_total: int, runtime: dict, risk_cfg: dict, *, seed: int,
             chalk_team: str | None = None, families: Sequence[float] | None = None,
             time_limit_s: float | None = None, goalies: Sequence[str] = (),
             avoid_own_goalie: bool = False, own: Mapping[str, float] | None = None) -> tuple[list[Candidate], dict]:
    """Candidates tagged central / alternate:<team> / priors_wrong:<team>, plus (Classic, B36) up to
    discovery.per_goalie central-objective candidates forced to hold each usable goalie (person keys in
    `goalies`), tagged by goalie_tag, on top of n_total and its time share. Without them the
    perturbed central search may return almost no lineup without the projected top goalie, and the
    goalie cap can only relax. avoid_own_goalie (C20, flags 51 to 53): every job, the per-goalie menu included, forbids a skater who
    plays against the lineup's goalie (Classic only; the caller asks when the slate has a contest of a rule family).
    own: role_id -> percent ownership in the biggest contest's field, for the goalie term (C20, flag 54), which
    `discovery.goalie_term.enabled` switches on (shipped off; Classic only): every job's goalie rows gain
    `goalie_term_points`, and with it off nothing here changes.
    Showdown (C23, flags 96 and 97): `captain.policy` reads `discovery.captain`; with the goalie gate on every job excludes the
    barred Captain rows, with the leverage term on every job's Captain rows gain its points, and with both off nothing here
    changes. The policy's report (the goalie ranks and would-be-barred goalies) is `report["captain"]` on every Showdown slate.
    Returns (candidates, report)."""
    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.build.milp import GroupConstraint
    from nhl_dfs.models.field import split_counts

    d = risk_cfg["discovery"]
    fam = tuple(families if families is not None else d["families"])
    c = runtime["candidates"]
    per = split_counts(list(fam), int(n_total))
    budget = float(time_limit_s if time_limit_s is not None else c["time_limit_total_s"])
    tp = team_points(design, pool)
    gt = d.get("goalie_term") or {}
    term = goalie_term_points(design, pool, gt, own) if gt.get("enabled") and pool.mode is Mode.CLASSIC else {}
    cp = _captain_policy(design, pool, risk_cfg, own)

    def objective(rows=None):
        obj = role_objective(design, pool, rows)
        for rid, v in term.items():
            obj[rid] += v
        for rid, v in cp.term.items():
            obj[rid] += v
        return obj

    jobs: list[tuple[str, dict, int]] = [("central", objective(), per[0])]
    teams = sorted(tp)
    for t, k in zip(teams, split_counts([1.0] * len(teams), per[1])):
        if k:
            rows = tp[t] >= np.quantile(tp[t], float(d["cluster_quantile"]))
            jobs.append((f"alternate:{t}", objective(rows), k))
    if per[2] and teams:
        t = chalk_team if chalk_team in tp else max(teams, key=lambda x: float(tp[x].mean()))
        rows = tp[t] <= np.quantile(tp[t], float(d["failure_quantile"]))
        jobs.append((f"priors_wrong:{t}", objective(rows), per[2]))
    total_k = sum(k for _, _, k in jobs) or 1
    menu = []
    if pool.mode is Mode.CLASSIC and int(d.get("per_goalie", 0)) > 0:
        rows_of: dict[str, list[str]] = defaultdict(list)
        for r in pool.rows:
            if r.is_goalie:
                rows_of[r.person_key].append(r.role_id)
        menu = [(goalie_tag(g), (GroupConstraint(frozenset(rows_of[g]), min_count=1),)) for g in sorted(set(goalies))
                if g in rows_of]
    if menu:  # one draw per goalie in turn (candidates.generate cycles the menu)
        jobs.append(("goalie", objective(), int(d["per_goalie"]) * len(menu)))
    out: list[Candidate] = []
    seen: set[str] = set()
    report = {"target": dict(zip(("central", "alternate", "priors_wrong"), per)), "made": Counter(), "avoid_own_goalie": bool(avoid_own_goalie),
              "goalie_term": {"enabled": bool(term), "points": {k: round(v, 3) for k, v in sorted(term.items())}}}
    if menu:
        report["target"]["goalie"] = jobs[-1][2]
    if pool.mode is Mode.SHOWDOWN:
        report["captain"] = cp.report
    gate_kw = {"exclude": cp.barred} if cp.barred else {}  # passed only when the gate bars a row: off, the call is as before
    for i, (name, obj, k) in enumerate(jobs):
        try:
            got = cand_mod.generate(pool, pool.mode, obj, k, seed=seed + 101 * i, perturb_sd=float(c["perturb_sd_points"]),
                                    time_limit_total_s=max(1.0, budget * k / total_k),
                                    min_pairwise_diff=int(c["min_pairwise_diff"]),
                                    groups_menu=menu if name == "goalie" else (), avoid_own_goalie=avoid_own_goalie,
                                    **gate_kw)
        except cand_mod.SolverUnavailable:
            got = []
        for g in got:
            if g.key in seen:
                continue
            seen.add(g.key)
            out.append(Candidate(g.role_ids, g.key, g.objective_value, g.family if name == "goalie" else name))
            report["made"][name.split(":")[0]] += 1
    report["made"] = dict(report["made"])
    return out, report


def _captain_policy(design: ob.ScenarioSet, pool, risk_cfg: Mapping, own: Mapping[str, float] | None):
    """C23: the Showdown Captain policy. With both switches off it only reports, so a failure there is reported, never raised;
    with a switch on it must work and raises."""
    from nhl_dfs.build import captain as cap_mod

    if pool.mode is not Mode.SHOWDOWN:
        return cap_mod.CaptainPolicy()
    try:
        p = cap_mod.policy(design, pool, risk_cfg, own)
        json.dumps(p.report)  # the report rides in the manifest, which is plain json
        return p
    except Exception as exc:
        c = cap_mod.config(risk_cfg)
        if (c.get("goalie_gate") or {}).get("enabled") or (c.get("leverage") or {}).get("enabled"):
            raise
        return cap_mod.CaptainPolicy(report={"error": f"{type(exc).__name__}: {str(exc)[:120]}"})


def goalie_tag(person_key: str) -> str:
    """Family tag of a forced-goalie candidate, "goalie:<name> (<team>)": no "|", which RUN_NOTES tables split on."""
    name, team = (person_key.split("|") + [""])[:2]
    return f"goalie:{name} ({team})"


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
        step = ob._chunk_rows(S, fs.shape[1] * 96 + K * (96 + 16 * len(cids)), float(risk_cfg["objectives"]["memory_cap_mb"]))
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


_Curve = ob.PayCurve  # moved to objectives.py with the joint-accounting primitive (C18); the name stays


def _greedy(kappa: float, order: list, entry_contest: Mapping[str, str], fees: Mapping[str, int], cand_scores: np.ndarray,
            candidates: Sequence[Candidate], states: dict, caps: exposure.Caps, pool, risk_cfg: dict,
            role_mean: Mapping[str, float], tournament: set[str], sleeve_max: int, mem_share: float = 1.0,
            faced: Sequence[bool] | None = None, rule_cids: frozenset[str] = frozenset(),
            forced: Mapping[str, int] | None = None):
    """One knob's fill. The prepared field ranks (states) are shared across knobs and never copied. Each contest's
    own entries are kept in an `objectives.OwnContest` (C18, B55): a step scores every candidate by the change to
    the WHOLE contest's payout and utility (its own plus what it takes from the entries already placed there),
    aggregated over scenario chunks (bounded memory), and `pay_total` stays the exact joint payout of everything
    placed (never prizes that cannot be paid together). Returns (choices, relaxations, pay_total).

    forced (C41, flag 121): entry -> candidate index. Each entry is still ranked as usual and its Choice records that
    pick and its band's anchor, but the forced candidate is the one placed, so every later entry sees the state the
    forced fill would have (the anchored refill of the ownership band report). None places the pick, as always."""
    S, K = cand_scores.shape
    tp = float(risk_cfg["objectives"]["top_pct"])
    curves = {cid: _Curve(st.contest, tp, ob.goal_of(risk_cfg)) for cid, st in states.items()}
    cap_mb = float(risk_cfg["objectives"]["memory_cap_mb"]) * mem_share
    step = ob._chunk_rows(S, K * 160, cap_mb)  # ~20 temporaries x 8 bytes
    n_in = Counter(entry_contest[e] for e in order)
    books: dict[str, ob.OwnContest] = {}  # per contest, made when its first entry is placed
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
    lineups_g, lineups_game = Counter(), Counter()  # every entry, for the LINEUPS-mode caps
    placed: list[set] = []
    stress = 0
    choices: dict[str, Choice] = {}
    relax: list[Relaxation] = []

    def excess(k: int, eid: str) -> tuple[int, float]:
        """How far candidate k would push the goalie and game caps: (entries over a LINEUPS cap, fee share
        over a DOLLARS cap). (0, 0.0) passes."""
        f = int(fees[eid])
        n_over, share_over = 0, 0.0
        for g in goalies[k]:
            if caps.goalie_lineups is not None:
                n_over += max(0, lineups_g[g] + 1 - caps.goalie_lineups)
            if caps.goalie_fee_share is not None:
                share_over += max(0.0, (fee_g[g] + f) / total_fees - caps.goalie_fee_share - 1e-9)
        if pgame[k]:
            if caps.game_lineups is not None:
                n_over += max(0, lineups_game[pgame[k]] + 1 - caps.game_lineups)
            if caps.game_fee_share is not None:
                share_over += max(0.0, (fee_game[pgame[k]] + f) / total_fees - caps.game_fee_share - 1e-9)
        return n_over, share_over

    def passes(k: int, eid: str, level: int) -> bool:
        """Levels 0 to 2 hold the goalie and game caps (B36: hard when feasible); level 3 is filtered to the
        smallest excess by the caller; level 4 allows a repeat and drops every cap."""
        if used[candidates[k].key] and level < 4:
            return False
        if candidates[k].family.startswith("priors_wrong") and stress >= sleeve_max and level < 4:
            return False
        if level >= 3:
            return True
        if excess(k, eid) != (0, 0.0):
            return False
        if level >= 2:
            return True
        f = int(fees[eid])
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
        oc = books.get(cid)
        if oc is None:
            oc = books[cid] = ob.OwnContest(cv.pay_util, st.G, st.E, cand_scores, capacity=n_in[cid], chunk=step,
                                            live_below=st.contest.paid)
        m = len(oc.cols)
        rows = step if 2 * m <= K else ob._chunk_rows(S, 2 * m * 160, cap_mb)  # the placed-entry temporaries stay inside the cap
        su, sq, n80 = np.zeros(K), np.zeros(K), np.zeros(K)
        top_n = st.contest.family == "top_n" and st.contest.paid > 0  # C47 (flags 64, 70): at least one own entry in the top N
        if top_n:
            prize = int(st.contest.prizes_cents[0])  # one flat prize, cents
            held = oc.total[0] if oc.total is not None else np.zeros(S, np.int64)  # the contest's own payout so far
            fin = np.zeros(K)
        for a in range(0, S, rows):
            b = min(S, a + rows)
            pay, util = oc.marginal(a, b)  # the whole contest's change, not just the newcomer's
            if top_n:  # change in min($X, own payout): the first own entry paid counts, a tie at the line counts its share
                h = held[a:b, None].astype(np.int64)
                u = (np.minimum(h + pay, prize) - np.minimum(h, prize)) / 100.0
                fin += pay.sum(axis=0)
            else:
                u = util / 100.0
            su += u.sum(axis=0)
            sq += (u * u).sum(axis=0)
            n80 += ((pay_total[a:b, None] + pay) <= thr).sum(axis=0)
        mean_u = su / S
        se_u = np.sqrt(np.maximum(sq / S - mean_u ** 2, 0.0) / S)
        p80 = n80 / S
        score = mean_u - kappa * total_fees / 100.0 * p80
        se = np.sqrt(se_u ** 2 + (kappa * total_fees / 100.0) ** 2 * p80 * (1 - p80) / S)
        # C20 (flag 51): an entry of a rule family takes a candidate without a skater against its goalie. The order held:
        # compliant at levels 0 to 2, any candidate at levels 0 to 2, compliant at 3, any at 3, then level 4 (repeat) compliant
        # first and any last. Level 4 with no rule accepts every candidate, so an entry is never left without a pick. Entries of
        # other families run exactly the five levels they always did.
        rule = faced is not None and cid in rule_cids
        attempts = ([(0, True), (1, True), (2, True), (0, False), (1, False), (2, False), (3, True), (3, False), (4, True), (4, False)]
                    if rule else [(lv, False) for lv in (0, 1, 2, 3, 4)])
        pick, anchor, level, over = None, None, 0, (0, 0.0)
        for level, strict in attempts:
            ok = [k for k in range(K) if (not strict or not faced[k]) and passes(k, eid, level)]
            if ok and level == 3:  # relax the goalie and game caps by the smallest step any candidate needs
                ex = {k: excess(k, eid) for k in ok}
                over = min(ex.values())
                ok = [k for k in ok if ex[k] == over]
            if not ok:
                continue
            items = [tiebreak.Item(candidates[k].key, float(score[k]), float(se[k]), float(st.own_pct[k]), float(st.dup[k]), k,
                                   float(fin[k]) / (S * prize) if top_n else 0.0)  # expected top-N finishes added (C47)
                     for k in ok]
            pick = tiebreak.rank(items, risk_cfg, st.policy)[0]
            anchor = tiebreak.rank(items, risk_cfg, "mean")[0].item  # C22: what the pick is without the ownership order
            break
        k = pick.item.ref
        placed_k = k if forced is None else int(forced[eid])
        if level:
            kind = {1: "OVERLAP", 2: "EXPOSURE", 3: "CONCENTRATION", 4: "REPEAT"}[level]
            detail = f"kappa {kappa:g}: no candidate passed the caps at level {level - 1}"
            if level == 3:
                detail += (f"; goalie/game cap exceeded by {over[0]} lineup(s)" if over[0] else "") + \
                          (f"; fee share exceeded by {over[1]:.2f}" if over[1] else "")
            relax.append(Relaxation(eid, kind, detail))
        if rule and faced[k]:
            relax.append(Relaxation(eid, "OWN_GOALIE", f"kappa {kappa:g}: no candidate without a skater against its goalie "
                                                       f"passed the caps; taken at level {level}"))
        choices[eid] = Choice(eid, cid, st.contest.family, k, candidates[k].key, candidates[k].family, float(score[k]),
                              float(se[k]), pick.band, pick.band_index, float(st.own_pct[k]), float(st.dup[k]),
                              st.dup_name, level, anchor.key, anchor.own, anchor.dup, anchor.value, anchor.se)
        k = placed_k
        # update state: fees, counts, own copies in this contest
        f = int(fees[eid])
        for g in goalies[k]:
            fee_g[g] += f
            lineups_g[g] += 1
        if pgame[k]:
            fee_game[pgame[k]] += f
            lineups_game[pgame[k]] += 1
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
        pay_total += oc.add(k)[0]  # exactly what the contest's payout changed by, entries displaced included
    return choices, relax, pay_total


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


def _accounting_gap(running: np.ndarray, joint: Mapping[str, tuple]) -> int:
    """Largest difference, in cents over scenarios, between the fill's running payout and the joint payout of the finished
    portfolio recomputed from scratch (0 when the accounting is exact)."""
    total = np.zeros(len(running), np.int64)
    for _eids, m in joint.values():
        total += m.payout_cents.astype(np.int64).sum(axis=1)
    return int(np.abs(running - total).max()) if len(running) else 0


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
    g_n, game_n = lineup_counts(by_entry, pool, scen.means_tenths())
    n = len(by_entry)
    g_top = max(g_n.values(), default=0)
    game_top = max(game_n.values(), default=0) if c["n_games"] > 1 else None
    if budget.goalie_lineups_max is not None and g_top > budget.goalie_lineups_max:
        reasons.append(f"goalie in {g_top} of {n} lineups > {budget.goalie_lineups_max}")
    if budget.game_lineups_max is not None and game_top is not None and game_top > budget.game_lineups_max:
        reasons.append(f"game primary in {game_top} of {n} lineups > {budget.game_lineups_max}")
    return FrontierPoint(kappa, pm.tail_utility, pm.tail_utility_se, pm.p_lose80, pm.p_lose80_se, pm.exp_payout, gmax,
                         game_max, cap_max, not reasons, False, reasons, goalie_lineups_max=g_top,
                         game_lineups_max=game_top, top1pct_utility=pm.top1pct_utility), pm


def lineup_counts(by_entry: Mapping[str, Sequence[str]], pool, role_mean: Mapping[str, float]) -> tuple[Counter, Counter]:
    """(entries per goalie, entries per primary game) across every entry."""
    games = exposure.game_of(pool)
    g_n, game_n = Counter(), Counter()
    for lu in by_entry.values():
        g_n.update(exposure.goalies_in(lu, pool))
        pg = exposure.primary_game(lu, role_mean, games, pool)
        if pg:
            game_n[pg] += 1
    return g_n, game_n


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
           fees_cents: Mapping[str, int] | None = None, rule_families: frozenset[str] = frozenset(),
           keep_prepared: bool = False) -> Selection:
    """Fill every entry from the candidates on the selection scenarios; see the module docstring. rule_families (C20,
    flags 51 and 52): the contest families whose entries take a candidate without a skater against its goalie (empty: the
    rule is off and nothing below changes; Classic only). keep_prepared (C41): keep what the fill was prepared from on
    `Selection.prepared` for `band_report` (it holds the unscreened field ranks; no pick changes)."""
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
    goalie_of = [exposure.goalies_in(c.role_ids, pool) for c in candidates]
    rule_cids = (frozenset(cid for cid, ct in contests.items() if ct.family in rule_families)
                 if rule_families and pool.mode is Mode.CLASSIC else frozenset())
    faced = None
    if rule_cids:
        opp = own_goalie.opponent_map(pool)
        faced = [own_goalie.faces_own_goalie(c.role_ids, pool, opp) for c in candidates]
    keep = screen(states, candidates, n_in, risk_cfg, S=cand_scores.shape[0], goalie_of=goalie_of,
                  rule_cids=rule_cids, faced=faced)
    screened = {"candidates_in": len(candidates), "kept": len(keep),
                "goalies_in": len({g for gs in goalie_of for g in gs}),
                "goalies_kept": len({g for i in keep for g in goalie_of[i]})}
    prepared = (Prepared(list(candidates), cand_scores, states, list(keep), order, entry_contest, fees, tournament, role_mean,
                         rule_cids, list(faced) if faced is not None else None, 0, n_in, goalie_of)
                if keep_prepared else None)
    if len(keep) < len(candidates):
        if faced is not None:
            faced = [faced[i] for i in keep]
        candidates = [candidates[i] for i in keep]
        cand_scores = np.ascontiguousarray(cand_scores[:, keep])
        states = {cid: _ContestState(st.contest, np.ascontiguousarray(st.G[:, keep]), np.ascontiguousarray(st.E[:, keep]),
                                     st.own_pct[keep], st.dup[keep], st.dup_name, st.policy) for cid, st in states.items()}
    sleeve = max(1, int(round(float(risk_cfg["discovery"]["stress_sleeve_max"]) * len(order))))
    if prepared is not None:
        prepared.sleeve = sleeve
    contests_eval = {cid: ob.ContestEval(ct, fields[cid], [e for e in order if entry_contest[e] == cid])
                     for cid, ct in contests.items()}
    kappas = [float(k) for k in risk_cfg["frontier"]["kappas"]]
    threads = max(1, min(len(kappas), int(risk_cfg.get("selection", {}).get("threads", 1))))

    def fill(kappa: float):
        return _greedy(kappa, order, entry_contest, fees, cand_scores, candidates, states, caps, pool, risk_cfg,
                       role_mean, tournament, sleeve, mem_share=1.0 / len(kappas),  # not threads: chunking fixed
                       faced=faced, rule_cids=rule_cids)

    if threads > 1:  # only the fills run concurrently; the joint frontier evaluation runs one knob at a time
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=threads) as ex:
            fills = list(ex.map(fill, kappas))  # map keeps knob order: the result does not depend on threads
    else:
        fills = [fill(k) for k in kappas]
    outs = []
    gap = 0
    for kappa, (ch, rl, running) in zip(kappas, fills):
        by_entry = {e: candidates[ch[e].cand].role_ids for e in order}
        joint = _joint_from_states(ch, states, cand_scores, contests_eval, risk_cfg)
        gap = max(gap, _accounting_gap(running, joint))
        pt, pm = _frontier_point(kappa, by_entry, contests_eval, role_base, pool, fees, risk, risk_cfg, joint)
        pt.portfolio_key = "|".join(sorted(lineup_keys(by_entry, pool).values()))
        outs.append((pt, (ch, rl, by_entry, pm)))
    points = [o[0] for o in outs]
    results = [o[1] for o in outs]
    mark_dominated(points)
    best, why = choose(points)
    ch, rl, by_entry, pm = results[points.index(best)]
    mix = Counter(c.cand_family.split(":")[0] for c in ch.values())
    ordered = {e.entry_id: by_entry[e.entry_id] for e in rows}
    sel = Selection(ordered, ch, points, best.kappa, why, rl, dict(mix), tuple(families), pm)
    sel.screened = screened
    sel.accounting_gap_cents = gap
    sel.prepared = prepared
    return sel


def screen(states: Mapping[str, _ContestState], candidates: Sequence[Candidate], n_in: Mapping[str, int], risk_cfg: dict,
           *, S: int, goalie_of: Sequence[Sequence[str]] | None = None, rule_cids: frozenset[str] = frozenset(),
           faced: Sequence[bool] | None = None) -> list[int]:
    """Candidates kept for the fill: per contest the best max(screen_min, screen_per_entry x its entries) by the
    field-only family utility, plus each discovery family's best screen_per_family there and each goalie's best
    screen_per_goalie (coverage, so the goalie cap has choices; B36), unioned. A contest in rule_cids (C20) also gets
    the same three cuts taken from the candidates without a skater against their goalie (`faced` False), ADDED to the
    union above, so the fallback still has candidates and, with no rule contest, nothing changes."""
    sc = risk_cfg.get("selection", {})
    lo, per, fam_n = int(sc.get("screen_min", 40)), float(sc.get("screen_per_entry", 2)), int(sc.get("screen_per_family", 3))
    g_n = int(sc.get("screen_per_goalie", 0))
    K = len(candidates)
    keep: set[int] = set()
    fam_of = [c.family.split(":")[0] for c in candidates]
    for cid, st in states.items():
        if not n_in.get(cid):
            continue
        su = ob.field_utility(st.contest, st.G, st.E, risk_cfg).total
        order = sorted(range(K), key=lambda k: (-su[k], candidates[k].key))
        keep.update(order[:max(lo, int(math.ceil(per * n_in[cid])))])
        for f in set(fam_of):
            keep.update([k for k in order if fam_of[k] == f][:fam_n])
        if goalie_of is not None and g_n > 0:
            for g in {x for gs in goalie_of for x in gs}:
                keep.update([k for k in order if g in goalie_of[k]][:g_n])
        if faced is not None and cid in rule_cids:
            comp = [k for k in order if not faced[k]]
            keep.update(comp[:max(lo, int(math.ceil(per * n_in[cid])))])
            for f in set(fam_of):
                keep.update([k for k in comp if fam_of[k] == f][:fam_n])
            if goalie_of is not None and g_n > 0:
                for g in {x for gs in goalie_of for x in gs}:
                    keep.update([k for k in comp if g in goalie_of[k]][:g_n])
    return sorted(keep)


# -- C41 ownership band report (flags 119 to 122, docs/experiments/ownership_band_2026-10-16.md) --------------------------

def _subset(states: Mapping[str, _ContestState], idx: Sequence[int]) -> dict[str, _ContestState]:
    return {cid: _ContestState(st.contest, np.ascontiguousarray(st.G[:, idx]), np.ascontiguousarray(st.E[:, idx]),
                               st.own_pct[idx], st.dup[idx], st.dup_name, st.policy) for cid, st in states.items()}


def band_report(sel: Selection, fields_alt: Mapping[str, ob.FieldSpec], own_alt: Mapping[str, Mapping[str, float]] | None,
                dup_alt: Mapping[str, Mapping[str, float]] | None, role_base: ob.ScenarioSet, contests: Mapping[str, ob.Contest],
                caps: exposure.Caps, *, pool, fam_cfg: dict, risk_cfg: dict, field_cal=None, free: bool = False) -> dict:
    """C41: one alternative field (per contest its FieldSpec, ownership and duplicate counts) against the base selection `sel`,
    made with keep_prepared=True, on the same selection scenarios. Reads only: no pick, field or file changes.

    - candidates: the union of the base screen and the alternative field's own screen (`screen`); the count it adds;
    - per candidate select received and contest with entries: the field-only family utility against the base field (mean,
      SE) and the mean per-scenario change against the alternative field with its paired SE, under the run's goal and under
      top1pct (information), in dollars (`objectives.field_utility`);
    - anchored: at the chosen knob, each entry ranked under the alternative field with the state advanced by the base fill's
      own earlier picks (`_greedy(forced=...)`): the pick and the band's anchor there, against the base pick and anchor;
    - free=True also fills from scratch under the alternative field at the chosen knob (information)."""
    from nhl_dfs.contracts.statuses import FieldCalibration

    P = sel.prepared
    if P is None:
        raise ValueError("band_report needs select(..., keep_prepared=True)")
    field_cal = field_cal or FieldCalibration.PRIOR
    S = P.cand_scores.shape[0]
    fs = {cid: fields_alt[cid].scores(role_base, pool.mode) for cid in contests}
    alt = _prepare(P.cand_scores, fs, contests, P.candidates, pool, fam_cfg, risk_cfg, own_alt or {}, dup_alt or {}, field_cal,
                   fields_alt)
    del fs
    keep_alt = screen(alt, P.candidates, P.n_in, risk_cfg, S=S, goalie_of=P.goalie_of, rule_cids=P.rule_cids, faced=P.faced)
    union = sorted(set(P.keep) | set(keep_alt))
    in_base, in_alt = set(P.keep), set(keep_alt)
    per_contest = {}
    for cid, st in P.states.items():
        if not P.n_in.get(cid):
            continue
        g = ob.field_utility(st.contest, st.G, st.E, risk_cfg, other=(alt[cid].G, alt[cid].E))
        t = ob.field_utility(st.contest, st.G, st.E, risk_cfg, goal="top1pct", other=(alt[cid].G, alt[cid].E))
        per_contest[cid] = {"family": st.contest.family, "goal": ob.family_objective(risk_cfg)[st.contest.family],
                            "utility": utility_label(st.contest.family, ob.goal_of(risk_cfg)), "base": g.mean / 100.0, "base_se": g.se / 100.0, "d": g.diff / 100.0, "d_se": g.diff_se / 100.0,
                            "top1_base": t.mean / 100.0, "top1_d": t.diff / 100.0, "top1_d_se": t.diff_se / 100.0}
    pos = {k: j for j, k in enumerate(union)}
    forced = {e: pos[P.keep[sel.choices[e].cand]] for e in P.order}
    sub = _subset(alt, union)
    del alt
    cands = [P.candidates[i] for i in union]
    faced = [P.faced[i] for i in union] if P.faced is not None else None
    args = (P.order, P.entry_contest, P.fees, np.ascontiguousarray(P.cand_scores[:, union]), cands, sub, caps, pool, risk_cfg,
            P.role_mean, P.tournament, P.sleeve)
    anchored, _, _ = _greedy(sel.chosen_kappa, *args, faced=faced, rule_cids=P.rule_cids, forced=forced)
    entries = {}
    for e in P.order:
        b, a = sel.choices[e], anchored[e]
        entries[e] = {"contest_id": b.contest_id, "family": b.family, "base_key": b.key, "alt_key": a.key,
                      "base_anchor": b.anchor_key, "alt_anchor": a.anchor_key, "pick_flip": a.key != b.key,
                      "anchor_flip": a.anchor_key != b.anchor_key, "alt_pick_in_base_screen": union[a.cand] in in_base,
                      "alt_band_index": a.band_index}
    if free:
        ch, _, _ = _greedy(sel.chosen_kappa, *args, faced=faced, rule_cids=P.rule_cids)
        for e in P.order:
            entries[e]["free_key"] = ch[e].key
            entries[e]["free_flip"] = ch[e].key != sel.choices[e].key
    return {"candidates": len(P.candidates), "base_screen": len(in_base), "alt_screen": len(in_alt),
            "added_by_alt_screen": len(union) - len(in_base), "keys": [c.key for c in P.candidates],
            "families": [c.family for c in P.candidates], "in_base_screen": [i in in_base for i in range(len(P.candidates))],
            "per_contest": per_contest, "entries": entries}


def utility_label(family: str, goal: str) -> str:
    """What `objectives.PayCurve.pay_util` counts as a family's utility, in words (C41's report prints it in dollars)."""
    if family == "wta" or (goal == "first_place" and family in ob.FIRST_PLACE_FAMILIES):
        return "first-place dollars"
    return {"large_gpp": "top-1% payout dollars", "satellite": "seat value plus payout dollars"}.get(family, "payout dollars")


def band_term(ch: Choice, risk_cfg: dict) -> str:
    """C41 (B130): which term set the width of the pick's band: "floor" (band_pct x |anchor score|) or "se" (se_mult x the
    anchor's SE); "floor" on a tie."""
    tb = risk_cfg["tiebreak"]
    return "floor" if float(tb["band_pct"]) * abs(ch.anchor_value) >= float(tb["se_mult"]) * ch.anchor_se else "se"


def lineup_keys(by_entry: Mapping[str, Sequence[str]], pool) -> dict[str, str]:
    return {e: lineup_key([pool.by_role_id[r] for r in lu], pool.mode) for e, lu in by_entry.items()}


# -- C22 chalk core measurement (flag 95, docs/experiments/chalk_core_2026-10-13.md) ---------------------------------------

CORE_POLICIES = ("own_then_dup", "dup_first")  # the in-band orders that read ownership or duplicates (large_gpp, small_field, wta)


def _moved_by(ch: Choice, policy: str) -> str:
    """Which component of the policy's in-band key made the pick differ from the band's anchor ("" when it did not)."""
    if ch.anchor_key == ch.key:
        return ""
    if policy == "finishes":
        return "finishes"
    first, second = ("dup", "own") if policy == "dup_first" else ("own", "dup")
    a = {"own": ch.anchor_own, "dup": ch.anchor_dup}
    p = {"own": ch.own_pct, "dup": ch.dup}
    return first if p[first] != a[first] else second


def core_report(choices: Mapping[str, Choice], role_ids_by_key: Mapping[str, Sequence[str]], contests: Mapping[str, ob.Contest],
                own_by_contest: Mapping[str, Mapping[str, float]], role_mean: Mapping[str, float], pool,
                fam_cfg: dict) -> tuple[dict[str, dict], dict]:
    """C22: per entry, the core pieces its pick and its band's anchor hold and what moved the pick; for the run, each
    contest's core and the counts the preregistration decides on (N eligible picks, M moved, M_own and M_dup, D moved off
    the core, U moved onto it) with the core retention of the picks beside the anchors'. Reads only; changes no pick."""
    policy_of = {cid: fam_cfg["families"][ct.family]["selection"] for cid, ct in contests.items()}
    cores = {cid: tiebreak.chalk_core(role_mean, own_by_contest.get(cid, {}), pool) for cid in contests}
    rows: dict[str, dict] = {}
    blank = {"N": 0, "M": 0, "M_own": 0, "M_dup": 0, "D": 0, "U": 0}
    counts: dict[str, dict[str, int]] = {}
    retention = {"picks": Counter(), "anchors": Counter()}
    by_family: dict[str, Counter] = {}  # every pick with a core, whatever its policy (a "mean" fill's retention too)
    word = {0: "none", 1: "one", 2: "both"}
    for eid, ch in choices.items():
        policy, core = policy_of[ch.contest_id], cores[ch.contest_id]
        moved = _moved_by(ch, policy)
        held = tiebreak.core_held(role_ids_by_key[ch.key], core, pool) if core else None
        untied = tiebreak.core_held(role_ids_by_key[ch.anchor_key], core, pool) if core else None
        rows[eid] = {"core_held": held, "core_held_untied": untied, "tiebreak_moved": moved}
        if not core:
            continue
        by_family.setdefault(ch.family, Counter())[word[held]] += 1
        if policy not in CORE_POLICIES:
            continue
        c = counts.setdefault(ch.family, dict(blank))
        c["N"] += 1
        c["M"] += bool(moved)
        c["M_own"] += moved == "own"
        c["M_dup"] += moved == "dup"
        c["D"] += held < untied
        c["U"] += held > untied
        retention["picks"][word[held]] += 1
        retention["anchors"][word[untied]] += 1
    total = {k: sum(c[k] for c in counts.values()) for k in blank}

    def person(pk: str, cid: str) -> dict:
        row = pool.persons[pk].classic  # a core exists only in Classic, one row per person
        rid = row.role_id
        mean, own = float(role_mean.get(rid, 0.0)), float(own_by_contest.get(cid, {}).get(rid, 0.0))
        return {"person": pk, "name": row.name, "team": row.team, "mean_pts": round(mean / 10.0, 2), "own_pct": round(own, 1),
                "product": round(mean / 10.0 * own, 1)}

    eligible = sorted({ch.contest_id for ch in choices.values() if cores[ch.contest_id] and policy_of[ch.contest_id] in CORE_POLICIES})
    summary = {
        "definition": "flag 95: per contest the two skaters with the largest selection mean x field ownership; a pick is counted when "
                      "its contest's policy is own_then_dup or dup_first (Classic); D = the pick holds fewer core pieces than the "
                      "band's anchor (the pick with no ownership order)",
        "mode": pool.mode.value,
        "contests": {cid: {"family": ct.family, "policy": policy_of[cid],
                           "core": [person(pk, cid) for pk in cores[cid]]} for cid, ct in sorted(contests.items())},
        "eligible_contests": eligible,
        "cores_agree": len({frozenset(cores[cid]) for cid in eligible}) <= 1 if eligible else None,  # None: no eligible contest
        "counts": dict(sorted(counts.items())),
        "total": total,
        "retention": {k: {w: int(v[w]) for w in ("both", "one", "none")} for k, v in retention.items()},
        "retention_by_family": {f: {w: int(v[w]) for w in ("both", "one", "none")} for f, v in sorted(by_family.items())},
    }
    if pool.mode is not Mode.CLASSIC:
        summary["skipped"] = "Showdown: no core (Classic only, flag 95); tiebreak_moved is still recorded per entry"
    return rows, summary
