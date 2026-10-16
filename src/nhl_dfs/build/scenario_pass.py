"""The scenario version of `run` (C8): after the baseline (v1) and the provisional pass, simulate the
slate in three seed streams, discover candidates on the design scenarios, choose the portfolio on
the selection scenarios, report its figures on the referee scenarios, then publish the next
version through write_entries, the referee and the compare-and-swap publish.

Evidence on every figure: PAYOUT_SOURCE (per contest and overall), OUTCOME_CALIBRATION
(UNVALIDATED: backlog B6 flags the 3+ point tail and model-line co-ceiling, so tail figures are
labeled and carry their Monte Carlo error), FIELD_CALIBRATION (PRIOR until the field fit passes
its gate). Odds: none are usable today (backlog B5), so each game's source is printed; MODEL is
never presented as MARKET. DTD (QUESTIONABLE) is priced once: from C24 a DTD skater sits out
inside the simulated game (objectives.split_play, sim/game.py; his minutes go to teammates), and
only the persons it does not cover (goalies, a play probability of 0) keep the participation mask
(objectives.scenario_set); the C3 0.85 ranking haircut is not applied again. C10 (backlog B9): run
passes the role state's play probabilities and confirmation times when it built one; the configured
0.85 is then replaced, never stacked. C9: the never-masked selection and referee draws, the contests
and the fields are also written to runs/<id>/scenario/ (build/scenario_cache.py) for late swap and refresh.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

import numpy as np

from nhl_dfs.build import captain as cap_mod
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
    # C24 (flags 101, 102): DTD skaters sit out inside the simulated game (their minutes go to teammates); the rest is masked
    in_sim, masked = ob.split_play(play, proj)
    slate.participation = in_sim
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
                                        purpose_code=int(slate.cfg["purposes"][purpose]), play_prob=play, in_sim=in_sim)
        hashes[purpose] = cache_mod.spec_hash(slate, proj, seed, purpose, int(n[purpose]))[:16]
        timings[f"sim_{purpose}_s"] = round(time.perf_counter() - t, 3)
        timings[f"sim_{purpose}_peak_mb"] = peak_mb()
    sec["scenarios"] = {p: {"n": sets[p].n, "seed": seed, "spec_sha256": hashes[p]} for p in sets}
    sec["participation"] = {"questionable_play_prob": q, "source": "role state" if play_prob is not None else "DK status (0.85)",
                            "persons": sorted(play), "in_simulation": sorted(in_sim), "masked": sorted(masked),
                            "semantics": "C24: in_simulation skaters sit out inside the simulated game (their minutes, shots "
                                         "and blocks go to the teammates who dressed); masked persons are zeroed after it",
                            "notes": sets["selection"].notes}

    # contests, payout curves, fields
    if prov is not None:
        contexts, fb, field_cal = prov["contexts"], prov["fb"], prov["field_cal"]
    else:
        from nhl_dfs.build import provisional as prov_mod
        from nhl_dfs.build.run import _contest_facts, _lobby_rows, _payout_templates  # lazy: run.py imports this module
        from nhl_dfs.models import field_inputs, ownership

        # C38 and C16 (B93): the lobby rows size the contests and a cached template table prices them, as in the provisional
        # pass (local files only, never a fetch); the contest pages were not answered here, so no details.
        templates = _payout_templates(entries, cache, run.path.parent, fam_cfg, messages)
        contexts = contests_mod.resolve(entries, {}, fam_cfg, templates, _lobby_rows(entries, cache, templates, fam_cfg, messages),
                                        _contest_facts(fam_cfg, messages), pool.mode.value)  # C50: the same facts rows
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
            fld = fb.fields[contests_mod.field_family(ctx.family)]  # a top_n contest shares large_gpp's (C47)
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
                participation=scache.participation_record(proj, play, roles, in_sim=in_sim))
        except (OSError, TypeError, ValueError) as exc:
            messages.append(f"scenario cache not written ({type(exc).__name__}: {str(exc)[:80]}); late swap will fall back")

    # candidates: discovery on design scenarios, plus the Phase A bank and the provisional picks
    t = time.perf_counter()
    c = runtime["candidates"]
    n_total = min(int(c["bank_per_entry"]) * len(entries.entries) + int(c["bank_extra"]), int(c["bank_max"]))
    big = max(contexts.values(), key=lambda x: x.field_size)
    chalk = _chalk_team(work, fb.marginals[big.contest_id].own)
    from nhl_dfs.build import own_goalie as og_mod

    # C20 (flags 51, 52): the families of this slate's contests that the own-goalie rule covers (empty: off, Showdown, or none)
    rule_fams = og_mod.rule_families(risk_cfg, work.mode) & {ctx.family for ctx in contexts.values()}
    found, disc = pf.discover(work, sets["design"], n_total, runtime, risk_cfg, seed=seed + 7, chalk_team=chalk,
                              goalies=exposure.usable_goalie_keys(proj, work), avoid_own_goalie=bool(rule_fams),
                              own=fb.marginals[big.contest_id].own)
    cands = list(found)
    seen = {x.key for x in cands}
    # C23 (flag 96): with the goalie gate on, a bank or provisional lineup whose Captain is barred is dropped too
    barred = frozenset(((disc.get("captain") or {}).get("goalie_gate") or {}).get("barred_roles") or ())
    gate_dropped = 0
    extra = [(lu, "central:provisional") for lu in (prov["assignment"].by_entry.values() if prov else [])]
    extra += [(x.role_ids, "central:bank") for x in ((prov.get("bank") or []) if prov else [])]
    for lu, tag in extra:
        rows = [work.by_role_id[r] for r in lu if r in work.by_role_id]
        if len(rows) != len(lu):
            continue
        key = lineup_key(rows, work.mode)
        if key not in seen:
            seen.add(key)
            if barred and cap_mod.captain_barred(lu, barred):
                gate_dropped += 1  # each distinct lineup once, whether it came from the bank, the provisional picks or both
                continue
            cands.append(Candidate(tuple(lu), key, 0.0, tag))
    timings["discovery_s"] = round(time.perf_counter() - t, 3)
    timings["discovery_peak_mb"] = peak_mb()
    sec["discovery"] = {**disc, "chalk_team": chalk, "candidates": len(cands),
                        "from_bank_and_provisional": len(cands) - len(found)}
    if barred:
        sec["discovery"]["captain_gate_dropped"] = gate_dropped

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
    from nhl_dfs.models import ownership as own_mod

    band_spec, band_why = band_specs((prov or {}).get("own_cfg") or own_mod.load_ownership_config(), work.mode)  # C41
    t = time.perf_counter()
    sel = pf.select(cands, sets["selection"], fields["selection"], contests, entries, caps, budget, seed=seed, pool=work,
                    fam_cfg=fam_cfg, risk_cfg=risk_cfg, own_by_contest=own_by, dup_by_contest=dup_by,
                    field_cal=field_cal, fees_cents=fees, rule_families=rule_fams, keep_prepared=band_spec is not None)
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
                        **({"field_size_note": ctx.size_note} if ctx.size_note else {}),
                        **({"top_n": ctx.top_n.record()} if ctx.top_n is not None else {}),
                        **({"contest_facts": ctx.facts.record()} if getattr(ctx, "facts", None) is not None else {}),
                        **({"top_n_figures": {**ref.per_contest[cid], "measured_on": "referee scenarios"}}
                           if cid in ref.per_contest else {})}  # C47: the Booster's portfolio objective and its two figures
                       for cid, ctx in contexts.items()]
    sec["frontier_all"] = [p.record() for p in sel.frontier]
    sec["frontier"] = [p.record() for p in pf.frontier_report(sel.frontier)]
    sec["chosen"] = {"kappa": sel.chosen_kappa, "reason": sel.chosen_reason, "measured_on": "selection scenarios"}
    sec["goal"] = ob.goal_of(risk_cfg)  # C55: what large_gpp and small_field entries were chosen for
    sec["accounting"] = {"joint_payout_gap_cents": sel.accounting_gap_cents, "expected": 0,
                         "note": "largest gap, any knob, between the fill's running payout and the joint recount"}
    sec["discovery"]["screened"] = sel.screened
    sec["family_mix"] = {"target": dict(zip(("central", "alternate", "priors_wrong"), sel.families_target)),
                         "selected": sel.family_mix}
    sec["portfolio"] = {**ref.record(), "measured_on": "referee scenarios"}
    # C22 (flag 95): the chalk core and how often the ownership tie-break moved a pick off it; reads only
    core_rows, sec["chalk_core"] = pf.core_report(sel.choices, {c.key: c.role_ids for c in cands}, contests, own_by,
                                                  sets["selection"].means_tenths(), work, fam_cfg)
    if work.mode is Mode.SHOWDOWN:  # C23 (flags 96 to 98): reads only; a failure is reported and never costs the run its file
        sec["captain"] = _captain_section(disc, by_entry, work, entries, contests, own_by, big.contest_id, caps, slate,
                                          risk_cfg, gate_dropped)
    rows = []
    for e in entries.entries:
        ch = sel.choices[e.entry_id]
        pe = ref.per_entry[e.entry_id]
        rows.append({"entry_id": e.entry_id, "contest_id": str(e.contest_id), "family": ch.family,
                     "PAYOUT_SOURCE": contests[str(e.contest_id)].payout_source.value,
                     "candidate_family": ch.cand_family, "band_index": ch.band_index, "own_sum_pct": round(ch.own_pct, 1),
                     "dup": round(ch.dup, 2), "dup_measure": ch.dup_measure, "cap_level": ch.level, **core_rows[e.entry_id],
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
    fam_of = {e.entry_id: contests[str(e.contest_id)].family for e in entries.entries}
    m["families"] = dict(fam_of)
    try:
        m["statuses"]["OWN_GOALIE"] = og_mod.audit(a_s.by_entry, fam_of, work, risk_cfg)
    except Exception as exc:
        m["statuses"]["OWN_GOALIE"] = og_mod.error_line(exc)
    if not vs["public_replaced"] or any(r.kind == "REPEAT" for r in sel.relaxations):
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["worked"].append(f"scenario pass published v{vs['version']} ({sets['selection'].n} selection and "
                       f"{sets['referee'].n} referee scenarios, PAYOUT_SOURCE={payout_overall}, knob {sel.chosen_kappa:g})")
    # C41 (flags 119 to 122): the ownership band report, after the file is published; it reads only and never costs the file
    if band_spec is None:
        sec["ownership_bands"] = {"skipped": band_why}
        return
    t = time.perf_counter()
    try:
        sec["ownership_bands"] = ownership_bands(
            run=run, work=work, proj=proj, contexts=contexts, fb=fb, own_n=own_n, prov=prov, st=st, risk_cfg=risk_cfg,
            fam_cfg=fam_cfg, seed=seed, override=scenario_n or {}, sel=sel, scen=sets["selection"], contests=contests, caps=caps,
            field_cal=field_cal, specs=band_spec)
    except Exception as exc:
        sec["ownership_bands"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        messages.append(f"ownership band report failed ({type(exc).__name__}); the published file is unaffected")
    sel.prepared = None
    timings["ownership_bands_s"] = round(time.perf_counter() - t, 3)


def _grow_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override: dict, *, temperature: float = 1.0):
    """A GPP field's upper tail needs about as many distinct lineups as opponents: a 300-draw field
    weighted up to 5,000 has the top score of 300 entries, which made a $1 GPP look worth $7 on the
    first real run. Grow large_gpp and small_field fields to min(target, opponents) draws with the
    same behaviors (a new seed), join them to the provisional draws, and read ownership and duplicate
    counts off the joined field, so field and ownership stay one object. Cash (a mid-field line) and
    WTA (a handful of opponents) keep the provisional field. temperature (C41): every behavior's noise scale
    (`models.field.scaled_behaviors`); 1.0 is today's field."""
    from nhl_dfs.build.provisional import FieldBuild
    from nhl_dfs.models import contests as contests_mod
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
    for fam in sorted({contests_mod.field_family(c.family) for c in contexts.values()}):
        if fam not in g["families"]:
            continue
        need = min(target, max(c.field_size - own_n[cid] for cid, c in contexts.items()
                               if contests_mod.field_family(c.family) == fam))
        have = fields[fam].n
        if need <= have:
            report[fam] = {"draws": have, "grown_by": 0, "shape_mix": field_mod.shape_report(fields[fam], work, own_cfg)}
            continue
        if feats is None:  # the table the first draws came from (C17: it carries the odds, lines and news), else a plain one
            feats = fb.feats if fb.feats is not None else ownership.feature_table(work, proj, None, cfg=own_cfg, statuses=statuses)
        util = ownership.perceived(work, proj, feats, ownership.family_weights(own_cfg, fam))
        beh = field_mod.scaled_behaviors(field_mod.behaviors_for_pool(fam, work, own_cfg), temperature)
        t = time.perf_counter()
        extra, sampler = None, "milp"
        if g.get("sampler") == "fast" and work.mode is Mode.CLASSIC:
            from nhl_dfs.models import field_fast

            extra = field_fast.sample_fast(work, util, beh, need - have, seed + 9001, fam, proj=proj, feats=feats, cfg=own_cfg)
            sampler = "fast" if extra is not None else "milp (pool not in the compact Classic form)"
        if extra is None:
            extra = field_mod.sample_parallel(work, work.mode, util, beh, need - have,
                                              seed + 9001, fam, proj=proj, feats=feats, cfg=own_cfg,
                                              time_limit_s=float(g["time_limit_s"]), workers=int(g["workers"]),
                                              sub_size=int(g["sub_size"]), mip_rel_gap=g.get("mip_rel_gap"))
        joined = field_mod.join(fields[fam], extra)
        fields[fam] = joined
        for cid, c in contexts.items():
            if contests_mod.field_family(c.family) == fam:
                margs[cid] = field_mod.marginals(joined, work, c.field_size)
        report[fam] = {"draws": joined.n, "requested": joined.requested, "grown_by": extra.n,
                       "distinct": len(set(joined.keys)), "seconds": round(time.perf_counter() - t, 2),
                       "sampler": sampler, "mip_rel_gap": g.get("mip_rel_gap") if sampler.startswith("milp") else None,
                       "detail": extra.detail, "shape_mix": field_mod.shape_report(joined, work, own_cfg)}
    return FieldBuild(fields, margs, fb.elapsed_s, fb.feats, fb.inputs), report


def band_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed: int, override: dict, *, temperature: float):
    """C41 (flag 120): the run's field rebuilt at a temperature and a seed by the same two steps as the base field: the
    provisional draws (`provisional.build_fields` at `seed`, on the base field's feature table, own_cfg and statuses) and the
    growth draws (`_grow_fields`, seed + 9001). Temperature 1.0 at the run's seed gives the base field draw for draw.
    Returns (FieldBuild, growth report)."""
    from nhl_dfs.build import provisional as prov_mod
    from nhl_dfs.models import ownership

    own_cfg = (prov or {}).get("own_cfg") or ownership.load_ownership_config()
    statuses = (prov or {}).get("statuses") or {rid: p for rid, (p, _) in st.items() if rid in work.by_role_id}
    first = prov_mod.build_fields(work, proj, contexts, seed=seed, statuses=statuses, own_cfg=own_cfg, inputs=fb.inputs,
                                  feats=fb.feats, temperature=temperature)
    return _grow_fields(work, proj, contexts, first, own_n, prov, st, risk_cfg, seed, override, temperature=temperature)


def band_specs(own_cfg: dict, mode: Mode) -> tuple[list[tuple[str, float, int]] | None, str | None]:
    """C41 (flag 122): the report's alternative fields from config/ownership.yaml field.error_band, as (label, temperature, seed
    shift), or (None, why not)."""
    eb = (own_cfg.get("field") or {}).get("error_band") or {}
    if mode is not Mode.CLASSIC:
        return None, "SKIPPED (C41 calibrated Classic only)"
    if not eb.get("report"):
        return None, "report off (config/ownership.yaml field.error_band.report)"
    c = eb.get("classic") or {}
    if c.get("t_low") is None or c.get("t_high") is None:
        return None, "no calibrated temperatures (config/ownership.yaml field.error_band.classic t_low, t_high)"
    return [("low", float(c["t_low"]), 0), ("high", float(c["t_high"]), 0)], None


def _names(pool, role_ids) -> list[str]:
    """Name and team: two players can share a name (Sebastian Aho of CAR and of NYI)."""
    return sorted(f"{pool.by_role_id[r].name} ({pool.by_role_id[r].team})" for r in role_ids)


def _plain(a: np.ndarray, digits: int) -> list:
    """A float array as a json list: rounded, a non-finite value (a change identical in every scenario has no z) as null."""
    return [round(float(v), digits) if np.isfinite(v) else None for v in a]


def ownership_bands(*, run, work, proj, contexts, fb, own_n, prov, st, risk_cfg, fam_cfg, seed: int, override: dict, sel, scen,
                    contests, caps, field_cal, specs: list[tuple[str, float, int]], free: bool = False,
                    write: bool = True) -> dict:
    """C41 (flags 119 to 122): the spread report. For each (label, temperature, seed shift) the field is rebuilt
    (`band_fields`, built and freed one at a time) and `portfolio.band_report` scores it against the base selection `sel`
    (select(..., keep_prepared=True)) on the selection scenarios. Reads only. Writes every candidate's figures to
    runs/<id>/scenario/ownership_bands.json and returns the summary the manifest keeps."""
    from nhl_dfs.models import contests as contests_mod
    from nhl_dfs.models import field as field_mod
    from nhl_dfs.models import ownership

    t0 = time.perf_counter()
    peak0 = peak_mb()
    own_cfg = (prov or {}).get("own_cfg") or ownership.load_ownership_config()
    eb = (own_cfg.get("field") or {}).get("error_band") or {}
    budget = float(eb.get("time_limit_s", 300))
    base_own = {fam: field_mod.marginals(f, work, 1).own for fam, f in fb.fields.items()}
    keys = None
    by_key = {c.key: c.role_ids for c in sel.prepared.candidates}
    fields_rec, reports, notes = {}, {}, []
    for label, temp, shift in specs:
        if time.perf_counter() - t0 > budget:
            notes.append(f"{label}: not built (time budget {budget:g} s spent)")
            continue
        t = time.perf_counter()
        fb_alt, grown = band_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed + shift, override,
                                    temperature=temp)
        fams = {}
        for fam, f in fb_alt.fields.items():
            own = field_mod.marginals(f, work, 1).own
            roles = sorted(base_own[fam])
            b, a = [base_own[fam][r] for r in roles], [own[r] for r in roles]
            top = max(roles, key=lambda r: (base_own[fam][r], r))
            fams[fam] = {"draws": f.n, "distinct": len(set(f.keys)),
                         "slope_vs_base": round(ownership.concentration_slope(b, a), 4),
                         "mae_vs_base": round(sum(abs(x - y) for x, y in zip(b, a)) / len(roles), 3),
                         "top_role": top, "top_base": round(base_own[fam][top], 2), "top_alt": round(own[top], 2)}
        fields_alt, own_alt, dup_alt = {}, {}, {}
        for cid, ctx in contexts.items():
            f = fb_alt.fields[contests_mod.field_family(ctx.family)]
            fields_alt[cid] = ob.field_spec(f.lineups, f.keys, ctx.field_size - own_n[cid], risk_cfg, n_scenarios=scen.n,
                                            seed=seed, salt=f"{cid}|selection")
            own_alt[cid], dup_alt[cid] = fb_alt.marginals[cid].own, fb_alt.marginals[cid].dup_counts
        del fb_alt
        rep = pf.band_report(sel, fields_alt, own_alt, dup_alt, scen, contests, caps, pool=work, fam_cfg=fam_cfg,
                             risk_cfg=risk_cfg, field_cal=field_cal, free=free)
        del fields_alt, own_alt, dup_alt
        keys = rep.pop("keys")
        reports[label] = rep
        fields_rec[label] = {"temperature": temp, "seed_shift": shift, "families": fams, "growth": {
            fam: {k: v for k, v in g.items() if k in ("draws", "distinct", "seconds", "sampler")} for fam, g in grown.items()},
            "seconds": round(time.perf_counter() - t, 2)}
    # spread: low and high against base, per candidate and contest with entries
    spread = {}
    lo, hi = reports.get("low"), reports.get("high")
    if lo and hi:
        for cid, pl in lo["per_contest"].items():
            ph = hi["per_contest"][cid]
            d = np.vstack([pl["d"], ph["d"]])
            se = np.vstack([pl["d_se"], ph["d_se"]])
            sp = np.maximum(d.max(axis=0), 0.0) - np.minimum(d.min(axis=0), 0.0)
            with np.errstate(divide="ignore", invalid="ignore"):
                z = np.where(se > 0, np.abs(d) / se, np.where(d != 0, np.inf, 0.0)).max(axis=0)
            spread[cid] = {"family": pl["family"], "goal": pl["goal"], "spread": sp, "z": z, "base": pl["base"],
                           "base_se": pl["base_se"]}
    # flips, per alternative field
    picks, flips = {}, []
    order = sel.prepared.order
    for label, rep in reports.items():
        ent = rep["entries"]
        gpp = [e for e in order if ent[e]["family"] == "large_gpp"]
        picks[label] = {"entries": len(order), "pick_flips": sum(ent[e]["pick_flip"] for e in order),
                        "anchor_flips": sum(ent[e]["anchor_flip"] for e in order), "large_gpp_picks": len(gpp),
                        "large_gpp_pick_flips": sum(ent[e]["pick_flip"] for e in gpp),
                        "large_gpp_anchor_flips": sum(ent[e]["anchor_flip"] for e in gpp),
                        "added_by_alt_screen": rep["added_by_alt_screen"]}
        if free:
            picks[label]["free_flips"] = sum(ent[e]["free_flip"] for e in order)
        for e in order:
            x = ent[e]
            if x["pick_flip"]:
                b, a = set(by_key[x["base_key"]]), set(by_key[x["alt_key"]])
                flips.append({"entry_id": e, "contest_id": x["contest_id"], "family": x["family"], "field": label,
                              "anchor_moved": x["anchor_flip"], "out": _names(work, b - a), "in": _names(work, a - b)})
    terms = Counter(pf.band_term(sel.choices[e], risk_cfg) for e in order
                    if sel.choices[e].family == "large_gpp")
    pick_rows = {}
    for e in order:
        ch = sel.choices[e]
        sp = spread.get(ch.contest_id)
        if sp is not None:
            k = sel.prepared.keep[ch.cand]
            pick_rows[e] = {"spread": round(float(sp["spread"][k]), 4), "z": _plain(sp["z"][k:k + 1], 2)[0],
                            "base": round(float(sp["base"][k]), 4), "base_se": round(float(sp["base_se"][k]), 4)}
    summary_spread = {}
    for cid, sp in spread.items():
        z = sp["z"][np.isfinite(sp["z"])]
        summary_spread[cid] = {"family": sp["family"], "goal": sp["goal"], "candidates": int(len(sp["spread"])),
                               "median_spread": round(float(np.median(sp["spread"])), 4),
                               "max_spread": round(float(sp["spread"].max()), 4),
                               "median_z": round(float(np.median(z)), 2) if len(z) else None,
                               "share_z_over_2": round(float((sp["z"] > 2).mean()), 3)}
    path = None
    if write and keys is not None:
        rows = {"keys": keys, "in_base_screen": next(iter(reports.values()))["in_base_screen"],
                "families": next(iter(reports.values()))["families"],
                "by_field": {label: {cid: {k: _plain(v, 5) for k, v in pc.items() if isinstance(v, np.ndarray)}
                                     for cid, pc in rep["per_contest"].items()} for label, rep in reports.items()},
                "spread": {cid: {"spread": _plain(sp["spread"], 5), "z": _plain(sp["z"], 3)} for cid, sp in spread.items()},
                "entries": {label: rep["entries"] for label, rep in reports.items()}}
        path = run.path / "scenario" / "ownership_bands.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, allow_nan=False), encoding="utf-8")
    c = eb.get("classic") or {}
    return {"label": "C41 ownership band, report only (FIELD_CALIBRATION=PRIOR; selection unchanged)",
            "band": {k: c.get(k) for k in ("beta_low", "beta_high", "t_low", "t_high")} | {"source": eb.get("source")},
            "fields": fields_rec, "picks": picks, "flips": flips, "spread": summary_spread, "pick_spread": pick_rows,
            "band_terms_large_gpp": dict(terms), "chosen_kappa": sel.chosen_kappa, "notes": notes,
            "file": path.relative_to(run.path).as_posix() if path else None,
            "seconds": round(time.perf_counter() - t0, 2), "peak_mb_before": peak0, "peak_mb_after": peak_mb()}


def _captain_section(disc, by_entry, work, entries, contests, own_by, big_cid, caps, slate, risk_cfg, gate_dropped) -> dict:
    """C23: the Captain policy's report, the published portfolio's Captains and the split report, in built-in types only."""
    try:
        cfg = cap_mod.config(risk_cfg)
        contest_of = {e.entry_id: str(e.contest_id) for e in entries.entries}
        share = {}
        for g in slate.games:
            lh, la = float(g.rates.lambda_home), float(g.rates.lambda_away)
            if lh + la > 0:
                share[g.home], share[g.away] = lh / (lh + la), la / (lh + la)
        lineups = [tuple(lu) for _, lu in sorted(by_entry.items())]  # flag 98: the slate's entries, every family
        out = {"policy": disc.get("captain") or {},
               "portfolio": cap_mod.portfolio_report(by_entry, work, contest_of, own_by, str(big_cid), caps.captain),
               "split": cap_mod.split_report(share, lineups, work, len(lineups), float(cfg.get("split_sleeve_max", 0.10)),
                                             len(slate.games)),
               "gate_dropped": int(gate_dropped)}
        json.dumps(out)  # the manifest is plain json: refuse anything else here, not at publish
        return out
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _chalk_team(pool, own: dict) -> str | None:
    by = Counter()
    for r in pool.rows:
        if not r.is_goalie:
            by[r.team] += float(own.get(r.role_id, 0.0))
    return max(sorted(by), key=lambda t: by[t]) if by else None
