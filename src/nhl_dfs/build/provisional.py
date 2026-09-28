"""Provisional leverage-aware selection on priors (C3, plan section 7 "Deterministic tie-break").

Candidates are ranked by projected lineup mean (DTD-adjusted, see below). Tie bands are
anchored: the best remaining candidate opens a band of width
    band = max(tie_band_pct * anchor mean, anchor lineup prior sd / sqrt(roster slots)),
every remaining candidate within it joins, and inside the band the contest family's policy
orders them:
    mean          cash and satellite: highest mean, no leverage
    own_then_dup  large GPP: lower lineup ownership, then lower duplicate proxy
    dup_first     WTA and small field: lower duplicate proxy, then lower ownership
The next band opens at the best candidate left, which is below every member of the band
before it. So no candidate is ever preferred over one more than one band better.

DTD (QUESTIONABLE) prices both sides: the ranking mean counts a QUESTIONABLE person at
questionable_play_prob (risk), and the field's fade of DTD players lowers their sampled
ownership (leverage).

Every figure here is provisional: MODEL_STATUS=PRIOR, OUTCOME_CALIBRATION=UNVALIDATED,
FIELD_CALIBRATION=PRIOR, PAYOUT_SOURCE per contest. No probability or ceiling is computed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from nhl_dfs.build.assign import Assignment, Caps, assign
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.intake.salary import SalaryPool
from nhl_dfs.models.contests import ContestContext
from nhl_dfs.models.field import Marginals, dup_proxy, lineup_own
from nhl_dfs.models.priors import CAPTAIN_MULTIPLIER
from nhl_dfs.models.projection import Projection, lineup_sd_tenths


@dataclass(frozen=True)
class Scored:
    cand: Candidate
    mean_tenths: float  # DTD-adjusted, Captain at 1.5x
    own_pct: float  # sum of role ownership in this contest's field
    dup: float  # pre-fit duplicate proxy
    band: float  # width of the band this candidate was ranked in (tenths)
    band_index: int
    anchor_tenths: float


def adjusted_mean_tenths(pool: SalaryPool, proj: Projection, role_ids: Sequence[str],
                         statuses: Mapping[str, Participation], q_play: float) -> float:
    total = 0.0
    for rid in role_ids:
        m = proj.mean_tenths(rid)
        if pool.mode is Mode.SHOWDOWN and "CPT" in pool.by_role_id[rid].roster_positions:
            m *= CAPTAIN_MULTIPLIER
        if statuses.get(rid) is Participation.QUESTIONABLE:
            m *= q_play
        total += m
    return total


def band_width(pool: SalaryPool, proj: Projection, role_ids: Sequence[str], anchor_mean: float, pct: float) -> float:
    floor = lineup_sd_tenths(pool, proj, role_ids) / math.sqrt(len(role_ids))
    return max(pct * anchor_mean, floor)


def _policy_key(policy: str, s_mean: float, own: float, dup: float) -> tuple:
    if policy == "mean":
        return (-s_mean,)
    if policy == "dup_first":
        return (dup, own, -s_mean)
    if policy == "own_then_dup":
        return (own, dup, -s_mean)
    raise ValueError(f"unknown selection policy {policy!r}")


def rank(candidates: Sequence[Candidate], pool: SalaryPool, proj: Projection, marg: Marginals | None,
         policy: str, cfg: dict, *, statuses: Mapping[str, Participation] | None = None,
         own_cfg: dict | None = None) -> list[Scored]:
    """Candidates in provisional preference order for one contest."""
    statuses = statuses or {}
    sel = cfg["selection"]
    q, pct = float(sel["questionable_play_prob"]), float(sel["tie_band_pct"])
    own = marg.own if marg is not None else {}
    rows = []
    for c in candidates:
        m = adjusted_mean_tenths(pool, proj, c.role_ids, statuses, q)
        rows.append((c, m, lineup_own(c.role_ids, own), dup_proxy(c.role_ids, own, pool.mode, pool=pool, cfg=own_cfg)))
    remaining = sorted(rows, key=lambda r: (-r[1], r[0].key))
    out: list[Scored] = []
    k = 0
    while remaining:
        anchor = remaining[0]
        width = band_width(pool, proj, anchor[0].role_ids, anchor[1], pct) if policy != "mean" else 0.0
        members = [r for r in remaining if r[1] >= anchor[1] - width] if policy != "mean" else remaining
        members.sort(key=lambda r: (_policy_key(policy, r[1], r[2], r[3]), r[0].key))
        out += [Scored(c, m, o, d, width, k, anchor[1]) for c, m, o, d in members]
        taken = {r[0].key for r in members}
        remaining = [r for r in remaining if r[0].key not in taken]
        k += 1
    return out


def select(
    candidates: Sequence[Candidate],
    proj: Projection,
    marginals_by_contest: Mapping[str, Marginals],
    contests: Mapping[str, ContestContext],
    entries,
    caps: Caps,
    cfg: dict,
    *,
    seed: int,
    pool: SalaryPool,
    statuses: Mapping[str, Participation] | None = None,
    later_start_utc: Mapping[str, datetime] | None = None,
    own_cfg: dict | None = None,
) -> tuple[Assignment, dict[str, Scored]]:
    """Fill every entry by its contest's provisional order. Returns the assignment and, per
    entry, the scored candidate it received (for the manifest and RUN_NOTES)."""
    rows = list(getattr(entries, "entries", entries))
    by_contest: dict[str, list[str]] = defaultdict(list)
    for e in rows:
        by_contest[str(e.contest_id)].append(e.entry_id)
    order = {f: i for i, f in enumerate(cfg["selection"]["family_order"])}
    first_seen = {cid: i for i, cid in enumerate(by_contest)}
    contest_order = sorted(by_contest, key=lambda cid: (order[contests[cid].family], first_seen[cid]))

    fixed: dict[str, tuple[str, ...]] = {}
    scored_by_entry: dict[str, Scored] = {}
    relaxations = []
    for cid in contest_order:
        ctx = contests[cid]
        policy = cfg["families"][ctx.family]["selection"]
        ranked = rank(candidates, pool, proj, marginals_by_contest.get(cid), policy, cfg,
                      statuses=statuses, own_cfg=own_cfg)
        pos = {s.cand.key: i for i, s in enumerate(ranked)}
        a = assign(candidates, by_contest[cid], pool, pool.mode, caps, seed=seed, later_start_utc=later_start_utc,
                   fixed=fixed, order_key=lambda c: pos[c.key], cap_entries=len(rows))
        by_key = {s.cand.key: s for s in ranked}
        for eid in by_contest[cid]:
            lineup = a.by_entry[eid]
            fixed[eid] = lineup
            scored_by_entry[eid] = by_key[lineup_key([pool.by_role_id[r] for r in lineup], pool.mode)]
        relaxations += [r for r in a.relaxations if r.entry_id in by_contest[cid]]
    final = assign(candidates, [e.entry_id for e in rows], pool, pool.mode, caps, seed=seed,
                   later_start_utc=later_start_utc, fixed=fixed)
    final.relaxations = relaxations
    return final, scored_by_entry


# -- field building (shared by the run's provisional pass and `cli field`) ----------------------

@dataclass
class FieldBuild:
    fields: dict  # family -> models.field.Field
    marginals: dict[str, Marginals]  # contest id -> Marginals at that contest's field size
    elapsed_s: float


def build_fields(pool: SalaryPool, proj: Projection, contexts: Mapping[str, ContestContext], *, seed: int,
                 statuses: Mapping[str, Participation] | None = None, odds=None,
                 own_cfg: dict | None = None) -> FieldBuild:
    """One sampled field per contest family present; marginals per contest at its field size."""
    import time

    from nhl_dfs.models import field as field_mod
    from nhl_dfs.models import ownership

    t0 = time.perf_counter()
    own_cfg = own_cfg if own_cfg is not None else ownership.load_ownership_config()
    feats = ownership.feature_table(pool, proj, odds, cfg=own_cfg, statuses=statuses)
    n = int(own_cfg["field"]["n"][pool.mode.value])
    fields = {}
    for fam in sorted({c.family for c in contexts.values()}):
        util = ownership.perceived(pool, proj, feats, ownership.family_weights(own_cfg, fam))
        fields[fam] = field_mod.sample(pool, pool.mode, util, field_mod.behaviors_for(fam, own_cfg), n, seed, fam,
                                       proj=proj, feats=feats, cfg=own_cfg)
    margs = {cid: field_mod.marginals(fields[c.family], pool, c.field_size) for cid, c in contexts.items()}
    return FieldBuild(fields, margs, time.perf_counter() - t0)


def field_summary(pool: SalaryPool, fb: FieldBuild, contexts: Mapping[str, ContestContext], top: int = 15) -> dict:
    """JSON-ready provisional field summary per family (saved as <run>/field.json)."""
    out: dict = {"label": "PROVISIONAL (priors only; FIELD_CALIBRATION=PRIOR)", "families": {}}
    for fam, fld in fb.fields.items():
        cid = next(c for c, ctx in contexts.items() if ctx.family == fam)
        m = fb.marginals[cid]
        own = sorted(m.own.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        r = pool.by_role_id
        out["families"][fam] = {
            "contests": [c for c, ctx in contexts.items() if ctx.family == fam],
            "n_requested": fld.requested,
            "n_draws": fld.n,
            "degraded": fld.degraded,
            "detail": fld.detail,
            "repeats": m.repeats,
            "distinct_lineups": len(set(fld.keys)),
            "mass": m.mass(pool.mode, pool),
            "top_ownership": [{"role_id": k, "name": r[k].name, "team": r[k].team, "slot": _slot_label(pool, r[k]),
                               "own_pct": round(v, 1)} for k, v in own],
            "cpt_share_top": [{"name": _name_of(pool, p), "cpt_pct": round(v, 1)}
                              for p, v in sorted(m.cpt_share.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
            "stack_freq": {t: round(v, 1) for t, v in m.stack_freq.items()},
            "salary_left_hist": {b: round(v, 1) for b, v in m.salary_left_hist.items()},
            "top_duplicates_per_draws": sorted((fld.keys.count(k) for k in set(fld.keys)), reverse=True)[:10],
        }
    return out


def _slot_label(pool: SalaryPool, row) -> str:
    if pool.mode is Mode.SHOWDOWN:
        return "CPT" if "CPT" in row.roster_positions else "FLEX"
    return row.position


def _name_of(pool: SalaryPool, person_key: str) -> str:
    p = pool.persons.get(person_key)
    r = p and (p.classic or p.flex or p.cpt)
    return f"{r.name} ({r.team})" if r else person_key
