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
simulator's dressing is untouched. C10 (backlog B9): run passes the role state's play probabilities and
confirmation times when it built one; the configured 0.85 is then replaced, never stacked.
C9: the unmasked selection and referee draws, the contests and the fields are also written to
runs/<id>/scenario/ (build/scenario_cache.py) for late swap and refresh.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import numpy as np

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build import scenario_cache as scache
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


def simulate_base(slate, params, n: int, seed: int, purpose: str, flags_out: list | None = None,
                  flags_keep: int = 0) -> tuple[np.ndarray, list[str]]:
    """(n, P) int32 base tenths in the simulator's person order, chunk by chunk (nothing written to disk).
    flags_out: a list that receives the (<= flags_keep, P, F) bool indicators (sim/score.bonus_flags) of the
    first flags_keep draws (backlog B25, B28), taken from the same chunks' outcomes."""
    from nhl_dfs.sim import game, score

    parts, fl, have = [], [], 0
    for o in game.iter_chunks(slate, params, n, seed, purpose):
        parts.append(score.base_tenths(o))
        if flags_out is not None and have < flags_keep:
            f = score.bonus_flags(o)
            fl.append(f[: flags_keep - have])
            have += len(fl[-1])
    if flags_out is not None and fl:
        flags_out.append(np.concatenate(fl, axis=0))
    return np.concatenate(parts, axis=0), sorted(params.persons)


def peak_mb() -> float | None:
    """Process peak working set in MB (Windows GetProcessMemoryInfo; ru_maxrss elsewhere); None if unreadable."""
    import os

    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                   "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                   "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return round(pmc.PeakWorkingSetSize / 1e6, 1)
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3, 1)
    except Exception:
        return None


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


def risk_statuses(sel, caps) -> dict[str, str]:
    """B36/B40 report lines: the goalie and game cap mode, and whether the chosen frontier point (selection
    scenarios) is inside the risk budget. BREACHED names each breach and any CONCENTRATION relaxation."""
    chosen = next((p for p in sel.frontier if p.kappa == sel.chosen_kappa), None)
    out = dict(caps.cap_status)
    if chosen is None:
        out["RISK_BUDGET"] = "NOT_EVALUATED"
        return out
    why = list(chosen.reasons)
    n_conc = sum(1 for r in sel.relaxations if r.kind == "CONCENTRATION")
    if n_conc:
        why.append(f"{n_conc} entr{'y' if n_conc == 1 else 'ies'} relaxed the goalie/game cap")
    out["RISK_BUDGET"] = "OK" if chosen.feasible and not n_conc else f"BREACHED({'; '.join(why)})"
    return out


def prior_persons(by_entry: dict, pool, proj) -> int:
    """Distinct PRIOR (no usable history) persons across a portfolio."""
    persons = getattr(proj, "persons", {})
    keys = {pool.by_role_id[r].person_key for lu in by_entry.values() for r in lu if r in pool.by_role_id}
    return sum(1 for k in keys if k in persons and persons[k].source is ModelStatus.PRIOR)


def run_scenario_pass(*, run, entries, pool, work, proj, st, starts, offline, runtime, seed, slate_id, outputs_root,
                      cache, m: dict, messages: list[str], prov: dict | None, v1_assignment, expect_sha: str, clock,
                      publish_fn, set_fields_fn, scenario_n: dict | None = None, play_prob: dict | None = None,
                      confirmed_at: dict | None = None, roles=None, odds: tuple | None = None) -> None:
    """play_prob, confirmed_at (C10, backlog B9): from the run's role state when it was built; play_prob then
    replaces the configured DTD probability (priced once, see build/swap_objective.play_probs). roles: the run's
    RoleState, for each goalie's start-probability source in scenario/meta.json (B26) and, when the provisional
    pass did not finish, the field's lines and news (C17). odds: the run's one (snapshot, messages) from
    run._run_odds (C17); None fetches it here (online) or skips it (offline), as before."""
    from nhl_dfs.models import contests as contests_mod
    from nhl_dfs.sim import cache as cache_mod
    from nhl_dfs.sim.slate import build_slate, fetch_odds

    t0 = time.perf_counter()
    timings: dict[str, float] = {"start_peak_mb": peak_mb()}
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
    if odds is not None:
        snapshot, odds_msgs = odds
    elif offline:
        snapshot, odds_msgs = None, ["odds: skipped (offline); every game takes the model intensities"]
    else:
        snapshot, odds_msgs = fetch_odds(cache=cache, now=clock())
    slate, game_lines = build_slate(work, proj, snapshot, now=clock(), confirmed_at=confirmed_at)
    sec["odds"] = odds_msgs
    sec["games"] = game_lines
    sec["game_sources"] = {g.key: g.rates.source + (" STALE" if g.rates.stale else "") for g in slate.games}
    # B40: odds coverage is a reported status, not only a RUN_NOTES line
    on_market = sum(1 for g in slate.games if g.rates.source == "MARKET" and not g.rates.stale)
    stale = sum(1 for g in slate.games if g.rates.source == "MARKET" and g.rates.stale)
    m["statuses"]["MARKET_COVERAGE"] = f"{on_market}/{len(slate.games)}" + (f" ({stale} STALE)" if stale else "")

    # participation priced once: QUESTIONABLE (DTD) persons sit out a share of scenarios
    q = float(fam_cfg["selection"]["questionable_play_prob"])
    play = {pool.by_role_id[r].person_key: q for r, (p, _) in st.items()
            if p is Participation.QUESTIONABLE and r in work.by_role_id}
    if play_prob is not None:
        play = {k: v for k, v in play_prob.items() if k in work.persons}
    n = dict(risk_cfg["scenarios"])
    n.update(scenario_n or {})
    sets: dict[str, ob.ScenarioSet] = {}
    hashes = {}
    # C9: the unmasked selection and referee draws are kept for late swap and refresh (build/scenario_cache.py)
    keep = int(runtime.get("late_swap", {}).get("cache_scenarios", 8000))
    kept: dict[str, dict] = {}
    for purpose in ("design", "selection", "referee"):
        t = time.perf_counter()
        fl: list = []
        base, keys = simulate_base(slate, proj, int(n[purpose]), seed, purpose,
                                   flags_out=fl if purpose in scache.PURPOSES else None, flags_keep=keep)
        if purpose in scache.PURPOSES:
            try:
                kept[purpose] = scache.save_base(run.path, purpose, base, keep, int(slate.cfg["chunk_size"]),
                                                 flags=fl[0] if fl else None)
            except OSError as exc:
                messages.append(f"scenario cache not written ({type(exc).__name__}); late swap will fall back")
        sets[purpose] = ob.scenario_set(base, keys, work, purpose=purpose, seed=seed,
                                        purpose_code=int(slate.cfg["purposes"][purpose]), play_prob=play)
        hashes[purpose] = cache_mod.spec_hash(slate, proj, seed, purpose, int(n[purpose]))[:16]
        timings[f"sim_{purpose}_s"] = round(time.perf_counter() - t, 3)
        timings[f"sim_{purpose}_peak_mb"] = peak_mb()
    sec["scenarios"] = {p: {"n": sets[p].n, "seed": seed, "spec_sha256": hashes[p]} for p in sets}
    sec["participation"] = {"questionable_play_prob": q, "source": "role state" if play_prob is not None else "DK status (0.85)",
                            "persons": sorted(play), "notes": sets["selection"].notes}

    # contests, payout curves, fields
    if prov is not None:
        contexts, fb, field_cal = prov["contexts"], prov["fb"], prov["field_cal"]
    else:
        from nhl_dfs.build import provisional as prov_mod
        from nhl_dfs.build.run import _lobby_rows, _payout_templates  # lazy: run.py imports this module
        from nhl_dfs.models import field_inputs, ownership

        # C38 and C16 (B93): the lobby rows size the contests and a cached template table prices them, as in the provisional
        # pass (local files only, never a fetch); the contest pages were not answered here, so no details.
        templates = _payout_templates(entries, cache, run.path.parent, fam_cfg, messages)
        contexts = contests_mod.resolve(entries, {}, fam_cfg, templates, _lobby_rows(entries, cache, templates, fam_cfg, messages))
        statuses = {rid: p for rid, (p, _) in st.items() if rid in work.by_role_id}
        own_cfg_fb = ownership.load_ownership_config()
        inputs = field_inputs.collect(work, snapshot=snapshot, role_state=roles, now=clock(), own_cfg=own_cfg_fb)  # C17
        messages.append(inputs.coverage_line())
        fb = prov_mod.build_fields(work, proj, contexts, seed=seed, statuses=statuses, own_cfg=own_cfg_fb, inputs=inputs)
        sec["field_inputs"] = inputs.record()
        field_cal = FieldCalibration.PRIOR
        messages.append("scenario pass rebuilt the fields because the provisional pass did not finish")
    contests = {cid: ob.contest_from(ctx, fam_cfg) for cid, ctx in contexts.items()}
    own_n = Counter(str(e.contest_id) for e in entries.entries)
    t = time.perf_counter()
    fb, grown = _grow_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, scenario_n or {})
    timings["field_growth_s"] = round(time.perf_counter() - t, 3)
    timings["field_growth_peak_mb"] = peak_mb()
    sec["field_growth"] = grown
    fields = {}
    for purpose in ("selection", "referee"):
        fields[purpose] = {}
        for cid, ctx in contexts.items():
            fld = fb.fields[ctx.family]
            fields[purpose][cid] = ob.field_spec(fld.lineups, fld.keys, ctx.field_size - own_n[cid], risk_cfg,
                                                 n_scenarios=sets[purpose].n, seed=seed, salt=f"{cid}|{purpose}")
    own_by = {cid: fb.marginals[cid].own for cid in contexts}
    dup_by = {cid: fb.marginals[cid].dup_counts for cid in contexts}
    if set(kept) == set(scache.PURPOSES):
        try:
            sec["cache"] = scache.save(
                run.path, purposes=kept, person_keys=keys, slate=slate, params=proj, seed=seed,
                chunk_size=int(slate.cfg["chunk_size"]), contests=contests,
                contest_family={cid: ctx.family for cid, ctx in contexts.items()},
                fields={fam: (list(f.lineups), list(f.keys)) for fam, f in fb.fields.items()},
                n_opponents={cid: int(ctx.field_size - own_n[cid]) for cid, ctx in contexts.items()},
                own_by={cid: {r: float(v) for r, v in own_by[cid].items()} for cid in contexts},
                dup_by={cid: {k: float(v) for k, v in dup_by[cid].items()} for cid in contexts},
                field_cal=field_cal.value, model_status=proj.source().value, play_prob=dict(play),
                participation=scache.participation_record(proj, play, roles))
        except (OSError, TypeError, ValueError) as exc:
            messages.append(f"scenario cache not written ({type(exc).__name__}: {str(exc)[:80]}); late swap will fall back")

    # candidates: discovery on design scenarios, plus the Phase A bank and the provisional picks
    t = time.perf_counter()
    c = runtime["candidates"]
    n_total = min(int(c["bank_per_entry"]) * len(entries.entries) + int(c["bank_extra"]), int(c["bank_max"]))
    big = max(contexts.values(), key=lambda x: x.field_size)
    chalk = _chalk_team(work, fb.marginals[big.contest_id].own)
    found, disc = pf.discover(work, sets["design"], n_total, runtime, risk_cfg, seed=seed + 7, chalk_team=chalk,
                              goalies=exposure.usable_goalie_keys(proj, work))
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
    timings["discovery_peak_mb"] = peak_mb()
    sec["discovery"] = {**disc, "chalk_team": chalk, "candidates": len(cands),
                        "from_bank_and_provisional": len(cands) - len(found)}

    # caps and the risk budget
    fees = {e.entry_id: ob.to_cents(contests_mod.fee_value(e.fee) or 0) for e in entries.entries}
    tournament = [e for e in entries.entries if contests[str(e.contest_id)].family != "cash"]
    goalies = exposure.usable_goalies(proj, work)
    captains = len({work.by_role_id[x.role_ids[0]].person_key for x in cands}) if work.mode is Mode.SHOWDOWN else None
    persons = len({work.by_role_id[r].person_key for x in cands for r in x.role_ids})
    caps = exposure.caps(expo_cfg, len(entries.entries), work, work.mode, len(work.games),
                         fees_cents=[fees[e.entry_id] for e in entries.entries], tournament_entries=len(tournament),
                         usable_goalies=goalies, usable_captains=captains, usable_persons=persons,
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
    timings["selection_peak_mb"] = peak_mb()
    by_entry = {e: arrange_util(lu, work, starts) for e, lu in sel.by_entry.items()}
    ev = {cid: ob.ContestEval(ct, fields["referee"][cid], [e.entry_id for e in entries.entries if str(e.contest_id) == cid])
          for cid, ct in contests.items()}
    t = time.perf_counter()
    ref = ob.portfolio_metrics(by_entry, ev, sets["referee"], pool=work, fees_cents=fees, cfg=risk_cfg)
    timings["referee_eval_s"] = round(time.perf_counter() - t, 3)
    timings["referee_eval_peak_mb"] = peak_mb()

    payout_overall = contests_mod.overall_payout_source(contexts.values()).value
    evidence = {"PAYOUT_SOURCE": payout_overall, "OUTCOME_CALIBRATION": OutcomeCalibration.UNVALIDATED.value,
                "FIELD_CALIBRATION": field_cal.value, "MODEL_STATUS": proj.source().value}
    sec["evidence"] = evidence
    sec["contests"] = [{**contests[cid].record(), "name": ctx.name, "family_source": ctx.family_source,
                        "field": fields["selection"][cid].detail,
                        "field_size_source": ctx.field_size_source, "FIELD_SIZE_SOURCE": ctx.field_size_label,
                        **({"payout_template": ctx.template.record()} if ctx.template is not None else {}),
                        **({"payout_note": ctx.payout_note} if ctx.payout_note else {}),
                        **({"field_size_note": ctx.size_note} if ctx.size_note else {})}
                       for cid, ctx in contexts.items()]
    sec["frontier_all"] = [p.record() for p in sel.frontier]
    sec["frontier"] = [p.record() for p in pf.frontier_report(sel.frontier)]
    sec["chosen"] = {"kappa": sel.chosen_kappa, "reason": sel.chosen_reason, "measured_on": "selection scenarios"}
    sec["accounting"] = {"joint_payout_gap_cents": sel.accounting_gap_cents, "expected": 0,
                         "note": "largest gap, any knob, between the fill's running payout and the joint recount"}
    sec["discovery"]["screened"] = sel.screened
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
    vs = publish_fn(run, entries, pool, a_s, slate_id, outputs_root, m, messages, phase="S", expect=expect_sha,
                    clock=clock, runtime=runtime)
    timings["total_s"] = round(time.perf_counter() - t0, 3)
    timings["peak_working_set_mb"] = peak_mb()
    sec["timings"] = timings
    if vs is None:
        m["failed"].append("scenario pass: its file was not published; the previous version stays current")
        return
    sec["version"] = vs["version"]
    set_fields_fn(m, a_s, pool)
    m["statuses"].update({k: evidence[k] for k in ("PAYOUT_SOURCE", "OUTCOME_CALIBRATION", "FIELD_CALIBRATION")})
    m["statuses"].update(risk_statuses(sel, caps))
    if not vs["public_replaced"] or any(r.kind == "REPEAT" for r in sel.relaxations):
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["worked"].append(f"scenario pass published v{vs['version']} ({sets['selection'].n} selection and "
                       f"{sets['referee'].n} referee scenarios, PAYOUT_SOURCE={payout_overall}, knob {sel.chosen_kappa:g})")


def _grow_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override: dict):
    """A GPP field's upper tail needs about as many distinct lineups as opponents: a 300-draw field
    weighted up to 5,000 has the top score of 300 entries, which made a $1 GPP look worth $7 on the
    first real run. Grow large_gpp and small_field fields to min(target, opponents) draws with the
    same behaviors (a new seed), join them to the provisional draws, and read ownership and duplicate
    counts off the joined field, so field and ownership stay one object. Cash (a mid-field line) and
    WTA (a handful of opponents) keep the provisional field."""
    from nhl_dfs.build.provisional import FieldBuild
    from nhl_dfs.models import field as field_mod
    from nhl_dfs.models import ownership

    g = risk_cfg["field_growth"]
    target = int(override.get("field_target", g["target"]))
    own_cfg = (prov or {}).get("own_cfg") or ownership.load_ownership_config()
    statuses = (prov or {}).get("statuses") or {rid: p for rid, (p, _) in st.items() if rid in work.by_role_id}
    fields = dict(fb.fields)
    margs = dict(fb.marginals)
    report = {}
    feats = None
    for fam in sorted({c.family for c in contexts.values()}):
        if fam not in g["families"]:
            continue
        need = min(target, max(c.field_size - own_n[cid] for cid, c in contexts.items() if c.family == fam))
        have = fields[fam].n
        if need <= have:
            report[fam] = {"draws": have, "grown_by": 0}
            continue
        if feats is None:  # the table the first draws came from (C17: it carries the odds, lines and news), else a plain one
            feats = fb.feats if fb.feats is not None else ownership.feature_table(work, proj, None, cfg=own_cfg, statuses=statuses)
        util = ownership.perceived(work, proj, feats, ownership.family_weights(own_cfg, fam))
        t = time.perf_counter()
        extra, sampler = None, "milp"
        if g.get("sampler") == "fast" and work.mode is Mode.CLASSIC:
            from nhl_dfs.models import field_fast

            extra = field_fast.sample_fast(work, util, field_mod.behaviors_for_pool(fam, work, own_cfg), need - have, seed + 9001, fam,
                                           proj=proj, feats=feats, cfg=own_cfg)
            sampler = "fast" if extra is not None else "milp (pool not in the compact Classic form)"
        if extra is None:
            extra = field_mod.sample_parallel(work, work.mode, util, field_mod.behaviors_for_pool(fam, work, own_cfg), need - have,
                                              seed + 9001, fam, proj=proj, feats=feats, cfg=own_cfg,
                                              time_limit_s=float(g["time_limit_s"]), workers=int(g["workers"]),
                                              sub_size=int(g["sub_size"]), mip_rel_gap=g.get("mip_rel_gap"))
        joined = field_mod.join(fields[fam], extra)
        fields[fam] = joined
        for cid, c in contexts.items():
            if c.family == fam:
                margs[cid] = field_mod.marginals(joined, work, c.field_size)
        report[fam] = {"draws": joined.n, "requested": joined.requested, "grown_by": extra.n,
                       "distinct": len(set(joined.keys)), "seconds": round(time.perf_counter() - t, 2),
                       "sampler": sampler, "mip_rel_gap": g.get("mip_rel_gap") if sampler.startswith("milp") else None,
                       "detail": extra.detail}
    return FieldBuild(fields, margs, fb.elapsed_s, fb.feats, fb.inputs), report


def _chalk_team(pool, own: dict) -> str | None:
    by = Counter()
    for r in pool.rows:
        if not r.is_goalie:
            by[r.team] += float(own.get(r.role_id, 0.0))
    return max(sorted(by), key=lambda t: by[t]) if by else None
