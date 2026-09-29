"""The scenario version of `run` (C8): after the baseline (v1) and the provisional pass, simulate the
slate in three seed streams, discover candidates on the design scenarios, choose the portfolio on
the selection scenarios, report its figures on the referee scenarios, then publish the next
version through write_entries, the referee and the compare-and-swap publish.

Evidence on every figure: PAYOUT_SOURCE (per contest and overall), OUTCOME_CALIBRATION
(UNVALIDATED: backlog B6 flags the 3+ point tail and model-line co-ceiling, so tail figures are
labeled and carry their Monte Carlo error), FIELD_CALIBRATION (PRIOR until the field fit passes
its gate). Odds: none are usable today (backlog B5), so each game's source is printed; MODEL is
never presented as MARKET. DTD (QUESTIONABLE) is priced once, as a participation mask in the
scenarios (objectives.scenario_set); the C3 0.85 ranking haircut is not applied again and the
simulator's dressing is untouched. The C7 role state is not wired in here (backlog B9: C9, C10).
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import numpy as np

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build.assign import Assignment, arrange_util
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.contracts.statuses import (
    DeliveryStatus,
    FieldCalibration,
    ModelStatus,
    OutcomeCalibration,
    Participation,
)

DISCLAIMER = ("uncalibrated scenario proxies: OUTCOME_CALIBRATION=UNVALIDATED (B6: the simulated 3+ point tail is thin), "
              "and 20,000 scenarios cannot resolve massive-field win rates precisely; never measured ROI, EV or ruin")


def simulate_base(slate, params, n: int, seed: int, purpose: str) -> tuple[np.ndarray, list[str]]:
    """(n, P) int32 base tenths in the simulator's person order, chunk by chunk (nothing written to disk)."""
    from nhl_dfs.sim import game, score

    parts = [score.base_tenths(o) for o in game.iter_chunks(slate, params, n, seed, purpose)]
    return np.concatenate(parts, axis=0), sorted(params.persons)


def assignment_from(by_entry: dict[str, tuple[str, ...]], pool, relaxations) -> Assignment:
    keys, persons, caps_ = Counter(), Counter(), Counter()
    placed = []
    for lu in by_entry.values():
        rows = [pool.by_role_id[r] for r in lu]
        keys[lineup_key(rows, pool.mode)] += 1
        ps = {r.person_key for r in rows}
        persons.update(ps)
        if pool.mode is Mode.SHOWDOWN:
            caps_[rows[0].person_key] += 1
        placed.append(ps)
    overlap = max((len(a & b) for i, a in enumerate(placed) for b in placed[i + 1:]), default=0)
    return Assignment(dict(by_entry), dict(keys), dict(persons), dict(caps_), overlap, list(relaxations))


def prior_persons(by_entry: dict, pool, proj) -> int:
    """Distinct PRIOR (no usable history) persons across a portfolio."""
    persons = getattr(proj, "persons", {})
    keys = {pool.by_role_id[r].person_key for lu in by_entry.values() for r in lu if r in pool.by_role_id}
    return sum(1 for k in keys if k in persons and persons[k].source is ModelStatus.PRIOR)


def run_scenario_pass(*, run, entries, pool, work, proj, st, starts, offline, runtime, seed, slate_id, outputs_root,
                      cache, m: dict, messages: list[str], prov: dict | None, v1_assignment, expect_sha: str, clock,
                      publish_fn, set_fields_fn, scenario_n: dict | None = None) -> None:
    from nhl_dfs.models import contests as contests_mod
    from nhl_dfs.sim import cache as cache_mod
    from nhl_dfs.sim.slate import build_slate, fetch_odds

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    risk_cfg = ob.load_risk_config()
    fam_cfg = contests_mod.load_contest_families()
    expo_cfg = exposure.load_exposure_config()
    sec: dict[str, Any] = {"version": None, "label": "SCENARIO: " + DISCLAIMER}
    m["scenario"] = sec
    if not hasattr(proj, "persons"):
        sec["skipped"] = "the per-person ParamTable could not be built, so the slate cannot be simulated"
        m["failed"].append("scenario pass: no ParamTable; the previous version stays current")
        return

    # games and odds (B5: no usable odds today; every game says which source it used)
    if offline:
        snapshot, odds_msgs = None, ["odds: skipped (offline); every game takes the model intensities"]
    else:
        snapshot, odds_msgs = fetch_odds(cache=cache, now=clock())
    slate, game_lines = build_slate(work, proj, snapshot, now=clock())
    sec["odds"] = odds_msgs
    sec["games"] = game_lines
    sec["game_sources"] = {g.key: g.rates.source + (" STALE" if g.rates.stale else "") for g in slate.games}

    # participation priced once: QUESTIONABLE (DTD) persons sit out a share of scenarios
    q = float(fam_cfg["selection"]["questionable_play_prob"])
    play = {pool.by_role_id[r].person_key: q for r, (p, _) in st.items()
            if p is Participation.QUESTIONABLE and r in work.by_role_id}
    n = dict(risk_cfg["scenarios"])
    n.update(scenario_n or {})
    sets: dict[str, ob.ScenarioSet] = {}
    hashes = {}
    for purpose in ("design", "selection", "referee"):
        t = time.perf_counter()
        base, keys = simulate_base(slate, proj, int(n[purpose]), seed, purpose)
        sets[purpose] = ob.scenario_set(base, keys, work, purpose=purpose, seed=seed,
                                        purpose_code=int(slate.cfg["purposes"][purpose]), play_prob=play)
        hashes[purpose] = cache_mod.spec_hash(slate, proj, seed, purpose, int(n[purpose]))[:16]
        timings[f"sim_{purpose}_s"] = round(time.perf_counter() - t, 3)
    sec["scenarios"] = {p: {"n": sets[p].n, "seed": seed, "spec_sha256": hashes[p]} for p in sets}
    sec["participation"] = {"questionable_play_prob": q, "persons": sorted(play), "notes": sets["selection"].notes}

    # contests, payout curves, fields
    if prov is not None:
        contexts, fb, field_cal = prov["contexts"], prov["fb"], prov["field_cal"]
    else:
        from nhl_dfs.build import provisional as prov_mod

        contexts = contests_mod.resolve(entries, {}, fam_cfg)
        statuses = {rid: p for rid, (p, _) in st.items() if rid in work.by_role_id}
        fb = prov_mod.build_fields(work, proj, contexts, seed=seed, statuses=statuses)
        field_cal = FieldCalibration.PRIOR
        messages.append("scenario pass rebuilt the fields because the provisional pass did not finish")
    contests = {cid: ob.contest_from(ctx, fam_cfg) for cid, ctx in contexts.items()}
    own_n = Counter(str(e.contest_id) for e in entries.entries)
    fields = {}
    for purpose in ("selection", "referee"):
        fields[purpose] = {}
        for cid, ctx in contexts.items():
            fld = fb.fields[ctx.family]
            fields[purpose][cid] = ob.field_spec(fld.lineups, fld.keys, ctx.field_size - own_n[cid], risk_cfg,
                                                 n_scenarios=sets[purpose].n, seed=seed, salt=f"{cid}|{purpose}")
    own_by = {cid: fb.marginals[cid].own for cid in contexts}
    dup_by = {cid: fb.marginals[cid].dup_counts for cid in contexts}

    # candidates: discovery on design scenarios, plus the Phase A bank and the provisional picks
    t = time.perf_counter()
    c = runtime["candidates"]
    n_total = min(int(c["bank_per_entry"]) * len(entries.entries) + int(c["bank_extra"]), int(c["bank_max"]))
    big = max(contexts.values(), key=lambda x: x.field_size)
    chalk = _chalk_team(work, fb.marginals[big.contest_id].own)
    found, disc = pf.discover(work, sets["design"], n_total, runtime, risk_cfg, seed=seed + 7, chalk_team=chalk)
    cands = list(found)
    seen = {x.key for x in cands}
    extra = [(lu, "central:provisional") for lu in (prov["assignment"].by_entry.values() if prov else [])]
    extra += [(x.role_ids, "central:bank") for x in ((prov.get("bank") or []) if prov else [])]
    for lu, tag in extra:
        rows = [work.by_role_id[r] for r in lu if r in work.by_role_id]
        if len(rows) != len(lu):
            continue
        key = lineup_key(rows, work.mode)
        if key not in seen:
            seen.add(key)
            cands.append(Candidate(tuple(lu), key, 0.0, tag))
    timings["discovery_s"] = round(time.perf_counter() - t, 3)
    sec["discovery"] = {**disc, "chalk_team": chalk, "candidates": len(cands),
                        "from_bank_and_provisional": len(cands) - len(found)}

    # caps and the risk budget
    fees = {e.entry_id: ob.to_cents(contests_mod.fee_value(e.fee) or 0) for e in entries.entries}
    tournament = [e for e in entries.entries if contests[str(e.contest_id)].family != "cash"]
    goalies = sum(1 for k, p in proj.persons.items() if p.group == "G" and p.goalie is not None and p.goalie.p_start >= 0.5
                  and any(r.person_key == k for r in work.rows))
    captains = len({work.by_role_id[x.role_ids[0]].person_key for x in cands}) if work.mode is Mode.SHOWDOWN else None
    persons = len({work.by_role_id[r].person_key for x in cands for r in x.role_ids})
    caps = exposure.caps(expo_cfg, len(entries.entries), work, work.mode, len(work.games),
                         fees_cents=[fees[e.entry_id] for e in entries.entries], tournament_entries=len(tournament),
                         usable_goalies=max(1, goalies), usable_captains=captains, usable_persons=persons,
                         budget=risk_cfg["budget"][work.mode.value])
    budget = pf.RiskBudget.from_config(risk_cfg, work.mode, caps)
    sec["caps"] = caps.record()
    sec["budget"] = budget.__dict__

    # selection, then the referee figures
    t = time.perf_counter()
    sel = pf.select(cands, sets["selection"], fields["selection"], contests, entries, caps, budget, seed=seed, pool=work,
                    fam_cfg=fam_cfg, risk_cfg=risk_cfg, own_by_contest=own_by, dup_by_contest=dup_by,
                    field_cal=field_cal, fees_cents=fees)
    timings["selection_s"] = round(time.perf_counter() - t, 3)
    by_entry = {e: arrange_util(lu, work, starts) for e, lu in sel.by_entry.items()}
    ev = {cid: ob.ContestEval(ct, fields["referee"][cid], [e.entry_id for e in entries.entries if str(e.contest_id) == cid])
          for cid, ct in contests.items()}
    t = time.perf_counter()
    ref = ob.portfolio_metrics(by_entry, ev, sets["referee"], pool=work, fees_cents=fees, cfg=risk_cfg)
    timings["referee_eval_s"] = round(time.perf_counter() - t, 3)

    payout_overall = contests_mod.overall_payout_source(contexts.values()).value
    evidence = {"PAYOUT_SOURCE": payout_overall, "OUTCOME_CALIBRATION": OutcomeCalibration.UNVALIDATED.value,
                "FIELD_CALIBRATION": field_cal.value, "MODEL_STATUS": proj.source().value}
    sec["evidence"] = evidence
    sec["contests"] = [{**contests[cid].record(), "name": ctx.name, "family_source": ctx.family_source,
                        "field": fields["selection"][cid].detail} for cid, ctx in contexts.items()]
    sec["frontier_all"] = [p.record() for p in sel.frontier]
    sec["frontier"] = [p.record() for p in pf.frontier_report(sel.frontier)]
    sec["chosen"] = {"kappa": sel.chosen_kappa, "reason": sel.chosen_reason, "measured_on": "selection scenarios"}
    sec["family_mix"] = {"target": dict(zip(("central", "alternate", "priors_wrong"), sel.families_target)),
                         "selected": sel.family_mix}
    sec["portfolio"] = {**ref.record(), "measured_on": "referee scenarios"}
    rows = []
    for e in entries.entries:
        ch = sel.choices[e.entry_id]
        pe = ref.per_entry[e.entry_id]
        rows.append({"entry_id": e.entry_id, "contest_id": str(e.contest_id), "family": ch.family,
                     "PAYOUT_SOURCE": contests[str(e.contest_id)].payout_source.value,
                     "candidate_family": ch.cand_family, "band_index": ch.band_index, "own_sum_pct": round(ch.own_pct, 1),
                     "dup": round(ch.dup, 2), "dup_measure": ch.dup_measure, "cap_level": ch.level,
                     "dtd_players": sum(1 for r in by_entry[e.entry_id] if st.get(r, (None,))[0] is Participation.QUESTIONABLE),
                     **pe})
    sec["entries"] = rows
    sec["prior_persons_selected"] = {"baseline_v1": prior_persons(v1_assignment.by_entry, work, proj),
                                     "provisional": prior_persons(prov["assignment"].by_entry, work, proj) if prov else None,
                                     "scenario": prior_persons(by_entry, work, proj)}
    sec["relaxations"] = [{"entry_id": r.entry_id, "kind": r.kind, "detail": r.detail} for r in sel.relaxations]
    a_s = assignment_from(by_entry, work, sel.relaxations)
    vs = publish_fn(run, entries, pool, a_s, slate_id, outputs_root, m, messages, phase="S", expect=expect_sha)
    timings["total_s"] = round(time.perf_counter() - t0, 3)
    sec["timings"] = timings
    if vs is None:
        m["failed"].append("scenario pass: its file was not published; the previous version stays current")
        return
    sec["version"] = vs["version"]
    set_fields_fn(m, a_s, pool)
    m["statuses"].update({k: evidence[k] for k in ("PAYOUT_SOURCE", "OUTCOME_CALIBRATION", "FIELD_CALIBRATION")})
    if not vs["public_replaced"] or any(r.kind == "REPEAT" for r in sel.relaxations):
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["worked"].append(f"scenario pass published v{vs['version']} ({sets['selection'].n} selection and "
                       f"{sets['referee'].n} referee scenarios, PAYOUT_SOURCE={payout_overall}, knob {sel.chosen_kappa:g})")


def _chalk_team(pool, own: dict) -> str | None:
    by = Counter()
    for r in pool.rows:
        if not r.is_goalie:
            by[r.team] += float(own.get(r.role_id, 0.0))
    return max(sorted(by), key=lambda t: by[t]) if by else None
