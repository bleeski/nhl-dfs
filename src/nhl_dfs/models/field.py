"""Opponent field sampler (C3, plan section 6): ownership and the field are one object.

Each behavior builds legal lineups as argmax(its objective + Gumbel noise) through
build.candidates.generate(distinct=False), so draws are WITH replacement and the same lineup
can appear many times; multiplicities are kept. Ownership, Captain shares, duplicate counts,
stack frequencies and salary-left shares are all read off the sampled lineups.

Determinism: every behavior (and every stack team) has its own derived seed and a draw count
fixed by largest-remainder rounding. If the time budget or solver errors cut a behavior short,
the actual count is recorded, the field is marked degraded, and every rate divides by the
draws actually made.
"""

from __future__ import annotations

import math
import time
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from nhl_dfs.build.candidates import generate
from nhl_dfs.build.milp import GroupConstraint
from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.intake.salary import SalaryPool
from nhl_dfs.models.ownership import (CLASSIC_ONLY_RULES, STACK_RULES, bucket_range, feature_table,
                                      load_ownership_config, log_floor)
from nhl_dfs.models.priors import CAPTAIN_MULTIPLIER
from nhl_dfs.models.projection import Projection

SALARY_CAP = 50_000
SALARY_LEFT_EDGES = (0, 100, 300, 500, 1000, 2000)  # histogram bucket upper edges; last is open


@dataclass(frozen=True)
class Behavior:
    name: str
    weight: float
    noise_sd: float
    # "none" | "team3" (Classic: >= 3 skaters of one team; Showdown: >= 4 of 6) | Classic only (C19):
    # "team4" (>= 4 skaters of one team) | "double_stack" (>= 4 of one team and >= 3 of another, so 4-3-1) |
    # "team5" (C46: >= 5 skaters of one team, so 5-2-1, 5-1-1-1 or 6-1-1)
    stack_rule: str
    captain_rule: str  # key of field.captain_rules
    salary_left_pref: float  # points gained per $1,000 unspent
    popularity_weight: float = 1.0  # objective = mean + popularity_weight * (u - mean)


@dataclass
class Field:
    family: str
    lineups: list[tuple[str, ...]]  # one row per draw, canonical slot order
    keys: list[str]
    behavior_id: list[str]  # behavior name per draw
    requested: int
    detail: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.lineups)

    @property
    def multiplicity(self) -> list[int]:
        """Per draw: how many draws share its lineup."""
        c = Counter(self.keys)
        return [c[k] for k in self.keys]

    @property
    def degraded(self) -> bool:
        return self.n < self.requested


@dataclass
class Marginals:
    family: str
    n_draws: int
    field_size: int
    own: dict[str, float]  # role_id -> % of lineups (0 for never drawn)
    person_own: dict[str, float]  # person_key -> % of lineups in any slot
    cpt_share: dict[str, float]  # Showdown person_key -> % of lineups as Captain
    dup_counts: dict[str, float]  # lineup key -> expected entries at field_size
    stack_freq: dict[str, float]  # team -> % of lineups stacking it (team3 threshold)
    salary_left_hist: dict[str, float]  # bucket label -> % of lineups
    repeats: int  # draws that repeat an earlier draw
    degraded: bool

    def mass(self, mode: Mode, pool: SalaryPool) -> dict[str, float]:
        if mode is Mode.CLASSIC:
            g = sum(v for r, v in self.own.items() if pool.by_role_id[r].is_goalie)
            return {"total": sum(self.own.values()), "goalie": g, "skater": sum(self.own.values()) - g}
        cpt = sum(v for r, v in self.own.items() if "CPT" in pool.by_role_id[r].roster_positions)
        return {"total": sum(self.own.values()), "cpt": cpt, "flex": sum(self.own.values()) - cpt}


def _classic_table(family: str, cfg: dict) -> tuple[str, dict] | None:
    """The Classic mixture table in force for `family`: ("team5", its buckets) when field.classic_mixtures_team5
    (C46) is on, field.classic_mixtures is on too and the team5 table lists the family; ("classic", the C19 buckets)
    when the C19 switch is on and lists it; None otherwise (the caller then uses field.mixtures)."""
    fld = cfg["field"]
    cm = fld.get("classic_mixtures") or {}
    if not cm.get("enabled"):
        return None
    t5 = fld.get("classic_mixtures_team5") or {}
    if t5.get("enabled") and family in t5:
        return "team5", t5[family]
    return ("classic", cm[family]) if family in cm else None


def classic_mixture_name(family: str, cfg: dict) -> str:
    """"team5", "classic" or "old": which table `classic_mixture` reads for a Classic `family`."""
    tab = _classic_table(family, cfg)
    return tab[0] if tab else "old"


def classic_mixture(family: str, games: int | None, cfg: dict) -> dict | None:
    """The C19 Classic mixture of a family for a slate of `games` games (the C46 team5 table when its switch is
    on), or None when the switch is off or the family has none (the caller then uses field.mixtures). `default`
    catches every pool, including one whose game count is unknown (None or 0); a bucket such as "1-3" or "7+"
    overrides it for its game counts."""
    tab = _classic_table(family, cfg)
    if tab is None:
        return None
    buckets = tab[1]
    if games:
        for key, mix in buckets.items():
            if key != "default":
                lo, hi = bucket_range(key)
                if games >= lo and (hi is None or games <= hi):
                    return mix
    return buckets["default"]


def behaviors_for(family: str, cfg: dict | None = None, *, mode: Mode | None = None, games: int | None = None) -> list[Behavior]:
    """The behaviors of a contest family with their mixture weights. With no `mode` (or the C19 switch off, or
    Showdown) this is field.mixtures exactly as before; Classic with field.classic_mixtures enabled and a mixture
    for the family uses that one, by game count."""
    cfg = cfg if cfg is not None else load_ownership_config()
    fld = cfg["field"]
    mix = classic_mixture(family, games, cfg) if mode is Mode.CLASSIC else None
    mix = mix or fld["mixtures"].get(family) or fld["mixtures"]["large_gpp"]
    out = []
    for name, w in mix.items():
        b = fld["behaviors"][name]
        out.append(Behavior(name, float(w), float(b["noise_sd"]), b["stack_rule"], b["captain_rule"],
                            float(b["salary_left_pref"]), float(b.get("popularity_weight", 1.0))))
    return out


def behaviors_for_pool(family: str, pool: SalaryPool, cfg: dict | None = None) -> list[Behavior]:
    """behaviors_for for the slate in hand: its mode and its game count. What the run's field builders call."""
    return behaviors_for(family, cfg, mode=pool.mode, games=len(pool.games) or None)


def split_counts(weights: Sequence[float], n: int) -> list[int]:
    """Largest-remainder rounding of n * weights; ties go to the earlier entry."""
    total = sum(weights)
    if n <= 0 or total <= 0:
        return [0] * len(weights)
    raw = [n * w / total for w in weights]
    base = [math.floor(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[: n - sum(base)]:
        base[i] += 1
    return base


def _seed(seed: int, *parts: str) -> int:
    return zlib.crc32("|".join((str(seed),) + parts).encode("utf-8"))


def _stack_min(mode: Mode) -> int:
    return 3 if mode is Mode.CLASSIC else 4


Requirements = tuple[tuple[str, int], ...]  # ((team, minimum skaters), ...) one draw must satisfy


def stack_jobs(rule: str, mode: Mode, n: int, pool: SalaryPool, skaters_by_team: Mapping[str, Sequence[str]],
               team_total: Mapping[str, float]) -> tuple[list[tuple[Requirements, int]], str | None]:
    """(requirements, draws) jobs for one behavior's n draws, and a note when the rule had to be dropped.

    "none": one unconstrained job. "team3": one job per team with enough skaters, draws split by
    largest remainder in proportion to exp(implied total). "team4" and "team5" (C46): the same with a minimum of
    4 and 5; a 5-stack leaves 3 skaters that must come from 2 other teams (Classic needs 3 skater teams).
    "double_stack": one job per ordered pair (A with a minimum of 4, B with a minimum of 3), draws split in
    proportion to exp(total A + total B); DraftKings needs 3 skater teams, so every such lineup is 4-3-1.
    A rule no team or pair can satisfy falls back to one unconstrained job (as team3 always has) and says so.
    Both samplers use this one allocation, so they draw the same mix."""
    if rule not in STACK_RULES:
        raise ValueError(f"unknown stack rule {rule!r}")
    if rule in CLASSIC_ONLY_RULES and mode is not Mode.CLASSIC:
        raise ValueError(f"stack rule {rule!r} is Classic only")
    if rule == "none" or n <= 0:
        return [((), n)], None
    size = {t: len({pool.by_role_id[x].person_key for x in rids}) for t, rids in skaters_by_team.items()}
    if rule in ("team3", "team4", "team5"):
        m = _stack_min(mode) if rule == "team3" else int(rule[-1])
        teams = sorted(t for t, s in size.items() if s >= m)
        if not teams:
            return [((), n)], (None if rule == "team3" else f"no team has {m} skaters; {n} draws unstacked")
        per = split_counts([math.exp(team_total[t]) for t in teams], n)
        return [(((t, m),), k) for t, k in zip(teams, per) if k], None
    pairs = [(a, b) for a in sorted(t for t, s in size.items() if s >= 4) for b in sorted(size) if b != a and size[b] >= 3]
    if len(size) < 3 or not pairs:
        return [((), n)], f"no legal 4-3 pair of teams; {n} draws unstacked"
    per = split_counts([math.exp(team_total[a] + team_total[b]) for a, b in pairs], n)
    return [(((a, 4), (b, 3)), k) for (a, b), k in zip(pairs, per) if k], None


def _label(reqs: Requirements) -> str:
    """Seed and menu label of a job: the team for one, "A+B" for a pair, empty for none."""
    return "+".join(t for t, _ in reqs)


def _groups(reqs: Requirements, skaters_by_team: Mapping[str, Sequence[str]]) -> tuple[GroupConstraint, ...]:
    return tuple(GroupConstraint(role_ids=frozenset(skaters_by_team[t]), min_count=m) for t, m in reqs)


def behavior_objective(pool: SalaryPool, mode: Mode, util: Mapping[str, float], proj: Projection,
                       feats: Mapping[str, Mapping[str, float]], b: Behavior, captain_rules: Mapping[str, float]) -> dict[str, float]:
    out = {}
    bonus = float(captain_rules[b.captain_rule])
    for r in pool.rows:
        mean = proj.mean_tenths(r.role_id) / 10.0
        v = mean + b.popularity_weight * (util[r.role_id] - mean)
        if mode is Mode.SHOWDOWN and "CPT" in r.roster_positions:
            v = CAPTAIN_MULTIPLIER * v + bonus * feats[r.role_id]["salary_rank"]
        v -= b.salary_left_pref * r.salary / 1000.0
        out[r.role_id] = v
    return out


def sample(
    pool: SalaryPool,
    mode: Mode,
    util: Mapping[str, float],
    behaviors: Sequence[Behavior],
    n: int,
    seed: int,
    contest_family: str,
    *,
    proj: Projection,
    feats: Mapping[str, Mapping[str, float]] | None = None,
    cfg: dict | None = None,
    time_limit_s: float | None = None,
) -> Field:
    """n field lineups for one contest family, drawn with replacement across behaviors."""
    cfg = cfg if cfg is not None else load_ownership_config()
    feats = feats if feats is not None else feature_table(pool, proj, cfg=cfg)
    budget = float(time_limit_s if time_limit_s is not None else cfg["field"]["time_limit_s"])
    deadline = time.perf_counter() + budget
    counts = split_counts([b.weight for b in behaviors], n)
    lineups: list[tuple[str, ...]] = []
    beh: list[str] = []
    detail: list[str] = []

    skaters_by_team: dict[str, list[str]] = defaultdict(list)
    for r in pool.rows:
        if mode is Mode.SHOWDOWN or not r.is_goalie:
            skaters_by_team[r.team].append(r.role_id)
    team_total = {t: next((feats[rid]["implied_total"] for rid in rids), 0.0) for t, rids in skaters_by_team.items()}

    for b, n_b in zip(behaviors, counts):
        if n_b == 0:
            continue
        obj = behavior_objective(pool, mode, util, proj, feats, b, cfg["field"]["captain_rules"])
        jobs, note = stack_jobs(b.stack_rule, mode, n_b, pool, skaters_by_team, team_total)
        if note:
            detail.append(f"behavior {b.name}: {note}")
        got = 0
        for reqs, k in jobs:
            label = _label(reqs)
            menu = ((f"stack:{label}", _groups(reqs, skaters_by_team)),) if reqs else ()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            cands = generate(pool, mode, obj, k, seed=_seed(seed, b.name, label), perturb_sd=b.noise_sd,
                             groups_menu=menu, time_limit_total_s=remaining, distinct=False)
            for c in cands:
                lineups.append(c.role_ids)
                beh.append(b.name)
            got += len(cands)
        if got < n_b:
            detail.append(f"behavior {b.name}: {got} of {n_b} draws (time budget or solver errors)")
    keys = [lineup_key([pool.by_role_id[r] for r in lu], mode) for lu in lineups]
    return Field(contest_family, lineups, keys, beh, n, detail)


def sample_parallel(
    pool: SalaryPool,
    mode: Mode,
    util: Mapping[str, float],
    behaviors: Sequence[Behavior],
    n: int,
    seed: int,
    contest_family: str,
    *,
    proj: Projection,
    feats: Mapping[str, Mapping[str, float]] | None = None,
    cfg: dict | None = None,
    time_limit_s: float = 120.0,
    workers: int = 8,
    sub_size: int = 50,
    mip_rel_gap: float | None = None,
) -> Field:
    """The same behaviors and draw counts as `sample`, split into fixed sub-jobs of `sub_size` draws,
    each seeded by (seed, behavior, team, sub-job index), run on a thread pool (HiGHS releases the
    GIL). The draws depend on the seed and n only, never on the worker count. Used by the C8
    scenario pass to grow a GPP field to thousands of lineups; C3's `sample` is unchanged.
    mip_rel_gap loosens each field draw's MILP (a noisy field model needs no proven optimum;
    every draw is still a legal lineup)."""
    from concurrent.futures import ThreadPoolExecutor

    cfg = cfg if cfg is not None else load_ownership_config()
    feats = feats if feats is not None else feature_table(pool, proj, cfg=cfg)
    deadline = time.perf_counter() + float(time_limit_s)
    counts = split_counts([b.weight for b in behaviors], n)
    skaters_by_team: dict[str, list[str]] = defaultdict(list)
    for r in pool.rows:
        if mode is Mode.SHOWDOWN or not r.is_goalie:
            skaters_by_team[r.team].append(r.role_id)
    team_total = {t: next((feats[rid]["implied_total"] for rid in rids), 0.0) for t, rids in skaters_by_team.items()}
    jobs = []  # (behavior, objective, requirements, sub index, draws)
    notes: list[str] = []
    for b, n_b in zip(behaviors, counts):
        if n_b == 0:
            continue
        obj = behavior_objective(pool, mode, util, proj, feats, b, cfg["field"]["captain_rules"])
        per, note = stack_jobs(b.stack_rule, mode, n_b, pool, skaters_by_team, team_total)
        if note:
            notes.append(f"behavior {b.name}: {note}")
        for reqs, k in per:
            for j, a in enumerate(range(0, k, sub_size)):
                jobs.append((b, obj, reqs, j, min(sub_size, k - a)))

    def run(job):
        b, obj, reqs, j, k = job
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return []
        label = _label(reqs)
        menu = ((f"stack:{label}", _groups(reqs, skaters_by_team)),) if reqs else ()
        return generate(pool, mode, obj, k, seed=_seed(seed, b.name, label, f"sub{j}"), perturb_sd=b.noise_sd,
                        groups_menu=menu, time_limit_total_s=remaining, distinct=False, mip_rel_gap=mip_rel_gap)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        results = list(ex.map(run, jobs))  # map keeps job order: deterministic assembly
    lineups: list[tuple[str, ...]] = []
    beh: list[str] = []
    short: Counter[str] = Counter()
    for (b, _, team, _, k), got in zip(jobs, results):
        lineups += [c.role_ids for c in got]
        beh += [b.name] * len(got)
        short[b.name] += k - len(got)
    detail = notes + [f"behavior {name}: {k} draws short (time budget or solver errors)" for name, k in sorted(short.items()) if k]
    keys = [lineup_key([pool.by_role_id[r] for r in lu], mode) for lu in lineups]
    return Field(contest_family, lineups, keys, beh, n, detail)


def join(a: Field, b: Field) -> Field:
    """Two samples of one family's field as one (ownership and duplicates are read off the union)."""
    return Field(a.family, a.lineups + b.lineups, a.keys + b.keys, a.behavior_id + b.behavior_id,
                 a.requested + b.requested, a.detail + b.detail)


def _salary_bucket(left: int) -> str:
    lo = 0
    for edge in SALARY_LEFT_EDGES:
        if left <= edge:
            return f"{lo}-{edge}" if edge else "0"
        lo = edge + 1
    return f">{SALARY_LEFT_EDGES[-1]}"


_SALARY_LEFT_LABELS = tuple(dict.fromkeys(_salary_bucket(x) for x in (0, *(e + 1 for e in SALARY_LEFT_EDGES))))


def marginals(fld: Field, pool: SalaryPool, field_size: int) -> Marginals:
    mode = pool.mode
    n = fld.n
    role_n: Counter[str] = Counter()
    person_n: Counter[str] = Counter()
    cpt_n: Counter[str] = Counter()
    stack_n: Counter[str] = Counter()
    left_n: Counter[str] = Counter()
    for lu in fld.lineups:
        rows = [pool.by_role_id[r] for r in lu]
        role_n.update(lu)
        person_n.update({r.person_key for r in rows})
        if mode is Mode.SHOWDOWN:
            cpt_n[rows[0].person_key] += 1
        team_n = Counter(r.team for r in rows if mode is Mode.SHOWDOWN or not r.is_goalie)
        stack_n.update(t for t, k in team_n.items() if k >= _stack_min(mode))
        left_n[_salary_bucket(SALARY_CAP - sum(r.salary for r in rows))] += 1
    pct = (lambda k: 100.0 * k / n) if n else (lambda k: 0.0)
    key_n = Counter(fld.keys)
    return Marginals(
        family=fld.family,
        n_draws=n,
        field_size=int(field_size),
        own={r.role_id: pct(role_n[r.role_id]) for r in pool.rows},
        person_own={p: pct(k) for p, k in person_n.items()},
        cpt_share={p: pct(k) for p, k in cpt_n.items()},
        dup_counts={k: (c / n) * field_size for k, c in key_n.items()} if n else {},
        stack_freq={t: pct(k) for t, k in sorted(stack_n.items())},
        salary_left_hist={b: pct(left_n[b]) for b in _SALARY_LEFT_LABELS},
        repeats=n - len(key_n),
        degraded=fld.degraded,
    )


def skater_shape(pool: SalaryPool, lineup: Sequence[str]) -> tuple[int, ...]:
    """Skaters per team of one Classic lineup, largest first (goalie excluded): (4, 3, 1) is a 4-3-1."""
    c: Counter[str] = Counter()
    for rid in lineup:
        row = pool.by_role_id[rid]
        if not row.is_goalie:
            c[row.team] += 1
    return tuple(sorted(c.values(), reverse=True))


def stack_shape_mix(lineups: Sequence[Sequence[str]], pool: SalaryPool) -> dict:
    """Shares (percent of lineups) of the stack shapes in a Classic field, the numbers of
    reviews/2026-10-03_standings_synthesis.md section 3: stack3, stack4, stack5 (a team with at least that many
    skaters), two3 (two teams with at least 3), and every shape such as "4-3-1" by share, largest first."""
    if pool.mode is not Mode.CLASSIC:
        raise ValueError("stack shapes are defined for Classic fields only")
    n = len(lineups)
    shapes: Counter[tuple[int, ...]] = Counter(skater_shape(pool, lu) for lu in lineups)
    pct = (lambda k: 100.0 * k / n) if n else (lambda k: 0.0)
    return {
        "n": n,
        "stack3": pct(sum(c for s, c in shapes.items() if s and s[0] >= 3)),
        "stack4": pct(sum(c for s, c in shapes.items() if s and s[0] >= 4)),
        "stack5": pct(sum(c for s, c in shapes.items() if s and s[0] >= 5)),
        "two3": pct(sum(c for s, c in shapes.items() if len(s) > 1 and s[1] >= 3)),
        "shapes": {"-".join(map(str, s)): pct(c) for s, c in sorted(shapes.items(), key=lambda kv: (-kv[1], kv[0]))},
    }


def shape_report(fld: Field, pool: SalaryPool, cfg: dict) -> dict | None:
    """What RUN_NOTES prints about a Classic field's stack shapes (C19): the sampled shares, the pooled table they
    are compared with (a prior, not a fit), the six most common shapes and which mixture was in force. None for
    Showdown, whose shapes the table does not describe."""
    if pool.mode is not Mode.CLASSIC:
        return None
    mix = stack_shape_mix(fld.lineups, pool)
    games = len(pool.games) or None
    return {
        "draws": mix["n"],
        "games": games,
        "mixture": classic_mixture_name(fld.family, cfg),
        "stack3": round(mix["stack3"], 1), "stack4": round(mix["stack4"], 1), "stack5": round(mix["stack5"], 1),
        "two3": round(mix["two3"], 1),
        "top_shapes": {s: round(v, 1) for s, v in list(mix["shapes"].items())[:6]},
        "table": cfg["field"].get("classic_stack_table"),
    }


def salary_left_term(left: int, cfg: dict) -> float:
    for b in cfg["dup_proxy"]["salary_left_buckets"]:
        if b["max_left"] is None or left <= int(b["max_left"]):
            return float(b["term"])
    raise AssertionError("unreachable: validated buckets end open")


def dup_proxy(role_ids: Sequence[str], own: Mapping[str, float], mode: Mode, *, pool: SalaryPool | None = None,
              salary_left: int | None = None, cfg: dict | None = None) -> float:
    """Pre-fit duplicate-risk proxy (plan section 7): sum of log ownership across the roster,
    plus a salary-left bucket term, plus (Showdown) the Captain's log ownership again.
    Higher means more likely duplicated. Ownership is floored, so never-drawn players count."""
    cfg = cfg if cfg is not None else load_ownership_config()
    dp = cfg["dup_proxy"]
    floor = float(dp["own_floor_pct"])
    s = sum(log_floor(float(own.get(r, 0.0)), floor) for r in role_ids)
    if salary_left is None and pool is not None:
        salary_left = SALARY_CAP - sum(pool.by_role_id[r].salary for r in role_ids)
    if salary_left is not None:
        s += salary_left_term(int(salary_left), cfg)
    if mode is Mode.SHOWDOWN and role_ids:
        s += float(dp["captain_weight"]) * log_floor(float(own.get(role_ids[0], 0.0)), floor)
    return s


def lineup_own(role_ids: Sequence[str], own: Mapping[str, float]) -> float:
    """Sum of role ownership across the lineup, in percent (a provisional leverage measure)."""
    return sum(float(own.get(r, 0.0)) for r in role_ids)
