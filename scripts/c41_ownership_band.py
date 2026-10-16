"""C41 ownership band (BEN flags 119 to 122, docs/experiments/ownership_band_2026-10-16.md): calibrate the low and high fields,
replay the saved runs, and apply the preregistered rule.

Every replay is scripts/c55_first_place.py's: a saved run's inputs through the whole scenario pass (`run_slate(scenario=True)`),
entries cloned to 30, the run's created_utc clock, time-pinned web inputs, the c17 write guard with the network blocked, every
NHL_DFS root in scratch, at the seed given. Recording wrappers watch the run and never change what it publishes.

`band`       error_band on tests/fixtures/c41 (beta_low, beta_high per mode).
`calibrate`  replays with the report off, keeps the run's field build (`scenario_pass._grow_fields`: its inputs and the grown
             base field), checks implied_total, then rebuilds the field over t (flag 120: bisection on log t over [0.25, 4], at
             most 12 evaluations per target, within 0.02 of the realized slope) and writes each pool's t_low and t_high and their
             geometric means.
`run`        replays with the report on or off (--report). With the report on it also, after the run, scores the four
             preregistered fields against the run's own selection: low and high at this pool's calibrated t, and two controls at
             t = 1 with the field seed + 1 and + 2 (flag 121), each with the free-running refill and a full select().
`compare`    applies flag 121's rule to the instrument's fields only and checks flag 122's cost bar.

WHICH CODE RUNS: the tree on PYTHONPATH, checked against --expect-src (scripts/c24_dtd_replay.preload). Set PYTHONHASHSEED=0.

    python scripts/c41_ownership_band.py band --out <scratch>
    python scripts/c41_ownership_band.py calibrate --out <scratch> --seed 20261015 --expect-src <tree>/src --saved-run runs/<id> ...
    python scripts/c41_ownership_band.py run --out <scratch> --label c41-on --report on --calibration <scratch>/calibration.json \\
        --seed 20261015 --expect-src <tree>/src --saved-run runs/<id> ...
    python scripts/c41_ownership_band.py compare --out <scratch> --on c41-on --off c41-off [--master master-off]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))  # c24_dtd_replay and c17_replay import only the standard library at module level
import c24_dtd_replay as c24  # noqa: E402

N_CLONED = 30  # as C22, C24 and C55
T_RANGE = (0.25, 4.0)  # flag 120
STEPS = 12
TOL = 0.02
FAMILY = "large_gpp"


def other_python() -> list[str]:
    """Other python processes at this moment (B98: load changes same-seed lineups), from tasklist on Windows."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             timeout=20).stdout
        return [line.split(",")[1].strip('"') for line in out.splitlines() if line.startswith('"python')]
    except Exception as exc:  # recorded, never fatal
        return [f"not recorded: {type(exc).__name__}"]


# -- recording wrappers ----------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.grow: dict | None = None  # the scenario pass's first _grow_fields call: inputs and the grown base field
        self.select: dict | None = None  # the scenario pass's select(): args, kwargs and the Selection
        self.bands: dict | None = None  # the in-run ownership_bands call: kwargs and the prepared context it was given
        self.referee: dict | None = None  # the scenario pass's referee portfolio_metrics call
        self.frozen = False  # after the run: wrappers pass through and record nothing


REC = Recorder()


def install(rec: Recorder) -> None:
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.build import scenario_pass as sp

    real_grow, real_select = sp._grow_fields, pf.select

    def grow(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override, **kw):
        out = real_grow(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override, **kw)
        if rec.grow is None and not rec.frozen:
            rec.grow = {"args": (work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override), "base": out[0]}
        return out

    def select(*a, **kw):
        sel = real_select(*a, **kw)
        if rec.select is None and not rec.frozen:
            rec.select = {"args": a, "kwargs": kw, "sel": sel}
        return sel

    from nhl_dfs.build import objectives as ob

    real_pm = ob.portfolio_metrics

    def pm(assignment, contests, scenarios, **kw):  # the scenario pass's referee figures: keep the referee draws
        if rec.referee is None and not rec.frozen and getattr(scenarios, "purpose", None) == "referee":
            rec.referee = {"assignment": assignment, "contests": contests, "scen": scenarios, "kwargs": kw}
        return real_pm(assignment, contests, scenarios, **kw)

    sp._grow_fields, pf.select, ob.portfolio_metrics = grow, select, pm
    if hasattr(sp, "ownership_bands"):  # absent on origin/master: the report-off comparison runs there too
        real_bands = sp.ownership_bands

        def bands(**kw):
            if rec.bands is None and not rec.frozen:
                rec.bands = {"kwargs": kw, "prepared": kw["sel"].prepared}  # the run clears sel.prepared after the report
            return real_bands(**kw)

        sp.ownership_bands = bands


def report_switch(on: bool | None) -> None:
    """--report on|off: the run's ownership config with field.error_band.report forced (on needs calibrated t in the config)."""
    from nhl_dfs.models import ownership

    if on is None:
        return
    real = ownership.load_ownership_config

    def load(path=ownership.OWNERSHIP_YAML):
        cfg = copy.deepcopy(real(path))
        eb = (cfg.get("field") or {}).get("error_band")
        if eb is not None:
            eb["report"] = bool(on)
            ownership.validate_ownership_config(cfg)
        elif on:
            raise SystemExit("--report on: this tree has no field.error_band")
        return cfg

    ownership.load_ownership_config = load


# -- one replay ------------------------------------------------------------------------------------------------------------

def replay(salary: Path, entries: Path, clock, out: Path, tag: str, seed: int):
    import c17_replay as guard

    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.data import http as http_mod

    work = out / "work" / tag
    if work.exists():
        shutil.rmtree(work)
    (work / "raw").mkdir(parents=True)
    bodies, _ = guard.pinned_bodies(clock)
    transport = guard.ReplayTransport(bodies)
    cache = http_mod.HttpCache(work / "raw", config=http_mod.load_sources_config(), transport=transport, offline=False,
                               clock=lambda: clock, sleep=lambda s: None)
    REC.reset()
    others = other_python()
    t0 = time.perf_counter()
    res = run_slate(salary, entries, offline=False, baseline_only=False, scenario=True, seed=seed,
                    out_root=work / "runs", outputs_root=work / "outputs", cache=cache, clock=lambda: clock)
    secs = round(time.perf_counter() - t0, 1)
    REC.frozen = True
    m = read_manifest(res.run)
    rec = {"tag": tag, "ok": bool(res.ok), "seconds": secs, "failed": m.get("failed"), "other_python_at_start": others,
           "version": (m.get("scenario") or {}).get("version"), "pinned_urls": len(bodies), "served": len(transport.served)}
    return res, m, rec


def _draws_sha1() -> str | None:
    import numpy as np

    if REC.select is None:
        return None
    return hashlib.sha1(np.ascontiguousarray(REC.select["args"][1].base).tobytes()).hexdigest()


# -- band -------------------------------------------------------------------------------------------------------------------

def cmd_band(args) -> int:
    sys.path.insert(0, str(REPO / "tests"))
    from test_ownership_band import graded_rows

    from nhl_dfs.models import ownership

    band = ownership.error_band(graded_rows())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "band.json").write_text(json.dumps(band.record(), indent=1), encoding="utf-8")
    print(json.dumps({"pooled": band.pooled, "beta_low": band.beta_low, "beta_high": band.beta_high}, indent=1))
    return 0


# -- calibrate --------------------------------------------------------------------------------------------------------------

def _slope_at(t: float, g: dict, base_own: dict) -> tuple[float, float, dict]:
    from nhl_dfs.build import scenario_pass as sp
    from nhl_dfs.models import field as field_mod
    from nhl_dfs.models import ownership

    work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override = g["args"]
    t0 = time.perf_counter()
    fb_t, _ = sp.band_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override, temperature=t)
    f = fb_t.fields[FAMILY]
    own = field_mod.marginals(f, work, 1).own
    roles = sorted(base_own)
    b, a = [base_own[r] for r in roles], [own[r] for r in roles]
    top = max(roles, key=lambda r: (base_own[r], r))
    info = {"t": t, "slope": round(ownership.concentration_slope(b, a), 4),
            "mae_vs_base": round(sum(abs(x - y) for x, y in zip(b, a)) / len(roles), 3), "top_base": round(base_own[top], 2),
            "top_alt": round(own[top], 2), "draws": f.n, "distinct": len(set(f.keys)), "seconds": round(time.perf_counter() - t0, 1),
            "keys_equal_base": f.keys == g["base"].fields[FAMILY].keys}
    return info["slope"], t, info


def calibrate_pool(g: dict, beta_low: float, beta_high: float) -> dict:
    """Flag 120: per target, the endpoint first (an unreachable target takes the bound), then bisection on log t."""
    from nhl_dfs.models import field as field_mod

    work = g["args"][0]
    base_own = field_mod.marginals(g["base"].fields[FAMILY], work, 1).own
    feats = g["args"][3].feats or {}
    implied = sorted({round(float(f.get("implied_total", 0.0)), 3) for f in feats.values()})
    out = {"implied_total_values": implied[:12], "implied_total_nonzero": any(v != 0 for v in implied)}
    one = _slope_at(1.0, g, base_own)[2]
    out["t_one"] = one  # flag 120's identity: t = 1 at the run's seed is the base field
    for side, target, (lo, hi) in (("low", beta_low, (1.0, T_RANGE[1])), ("high", beta_high, (T_RANGE[0], 1.0))):
        steps = []
        end = hi if side == "low" else lo  # the far end: the flattest or the chalkiest field the range allows
        s_end, _, info = _slope_at(end, g, base_own)
        steps.append(info)
        reach = s_end <= target + TOL if side == "low" else s_end >= target - TOL
        if not reach:
            out[side] = {"target": target, "t": end, "realized": s_end, "reached": False, "steps": steps}
            continue
        best = info
        if abs(s_end - target) > TOL:
            a, b = math.log(lo), math.log(hi)
            while len(steps) < STEPS:
                t = round(math.exp((a + b) / 2), 4)
                s, _, info = _slope_at(t, g, base_own)
                steps.append(info)
                if abs(s - target) < abs(best["slope"] - target):
                    best = info
                if abs(s - target) <= TOL:
                    break
                if s > target:  # not flat enough: more noise
                    a = math.log(t)
                else:
                    b = math.log(t)
        out[side] = {"target": target, "t": best["t"], "realized": best["slope"], "reached": abs(best["slope"] - target) <= TOL,
                     "steps": steps}
    return out


def cmd_calibrate(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = c24.set_roots(out)
    where = c24.preload(Path(args.expect_src))
    import c17_replay as guard
    from pool_builder import clone_entries

    from nhl_dfs.data.history import store as store_mod

    store_mod.DEFAULT_ROOT = REPO / "data" / "features" / "history"  # read only
    guard.install_guards(out)
    install(REC)
    report_switch(False)
    band = json.loads((out / "band.json").read_text(encoding="utf-8"))
    beta_low, beta_high = float(band["beta_low"]["classic"]), float(band["beta_high"]["classic"])
    print(f"calibrate: nhl_dfs from {where}; seed {args.seed}; targets {beta_low} / {beta_high}; "
          f"PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}", flush=True)
    pools = {}
    for src in args.saved_run:
        salary, entries, clock, rid = c24.saved_inputs(Path(src), out)
        cloned = out / "saved" / rid / f"DKEntries_cloned{N_CLONED}.csv"
        if not cloned.exists():
            clone_entries(entries, cloned, N_CLONED)
        res, m, rec = replay(salary, cloned, clock, out, f"calibrate-{rid}", int(args.seed))
        if REC.grow is None:
            raise SystemExit(f"{rid}: the run grew no field (failed: {m.get('failed')})")
        t0 = time.perf_counter()
        cal = calibrate_pool(REC.grow, beta_low, beta_high)
        rec.update(cal)
        rec["calibration_seconds"] = round(time.perf_counter() - t0, 1)
        pools[rid] = rec
        print(f"{rid}: implied_total non-zero {cal['implied_total_nonzero']}; t=1 keys equal base {cal['t_one']['keys_equal_base']} "
              f"(slope {cal['t_one']['slope']}); low t {cal['low']['t']} slope {cal['low']['realized']} reached "
              f"{cal['low']['reached']} ({len(cal['low']['steps'])} steps); high t {cal['high']['t']} slope {cal['high']['realized']} "
              f"reached {cal['high']['reached']} ({len(cal['high']['steps'])} steps); {rec['calibration_seconds']} s", flush=True)
    geo = {side: round(math.exp(sum(math.log(p[side]["t"]) for p in pools.values()) / len(pools)), 4) for side in ("low", "high")}
    path = out / "calibration.json"
    path.write_text(json.dumps({"nhl_dfs": where, "seed": int(args.seed), "roots": roots, "targets": [beta_low, beta_high],
                                "pools": pools, "config": {"t_low": geo["low"], "t_high": geo["high"]},
                                "writes_refused": guard.WRITES, "network_blocked": guard.NET}, indent=1, default=str),
                    encoding="utf-8")
    print(f"config t_low {geo['low']}, t_high {geo['high']}; writes refused {len(guard.WRITES)}, network blocked {len(guard.NET)}; "
          f"wrote {path}", flush=True)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


# -- run --------------------------------------------------------------------------------------------------------------------

def _instrument(specs: list, base_choices: dict) -> dict:
    """After the run: the four preregistered fields against the run's own selection, each with the free-running refill and a
    full select() (does the chosen knob move), at an unbounded time budget, writing nothing into the run."""
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.build import scenario_pass as sp

    kw = dict(REC.bands["kwargs"])
    sel = kw["sel"]
    sel.prepared = REC.bands["prepared"]
    sa, skw = REC.select["args"], REC.select["kwargs"]
    full: dict[str, dict] = {}

    def hook(label, fields_alt, own_alt, dup_alt):
        t0 = time.perf_counter()
        k2 = dict(skw, own_by_contest=own_alt, dup_by_contest=dup_alt, keep_prepared=False)
        s2 = pf.select(sa[0], sa[1], fields_alt, *sa[3:], **k2)
        full[label] = {"chosen_kappa": s2.chosen_kappa, "knob_moved": s2.chosen_kappa != sel.chosen_kappa,
                       "lineups_changed": sum(1 for e, lu in s2.by_entry.items() if tuple(lu) != tuple(sel.by_entry[e])),
                       "seconds": round(time.perf_counter() - t0, 1)}

    t0 = time.perf_counter()
    kw.update(specs=specs, free=True, write=False, budget_s=1e9, hook=hook)
    rep = sp.ownership_bands(**kw)
    sel.prepared = None
    rep["full_select"] = full
    rep["instrument_seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def _referee_bands(t_low: float, t_high: float) -> dict:
    """Flag 123's information: the published portfolio on the referee draws against the base field and the low and high fields
    at the config's t (referee-purpose field specs, salt cid|referee), E[payout], P(lose >= 80%), first-place utility / fees and
    the large_gpp first-place equity sum, with SEs."""
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import scenario_pass as sp
    from nhl_dfs.models import contests as contests_mod

    work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override = REC.grow["args"]
    r = REC.referee
    out = {}
    for label, t in (("base", None), ("low", t_low), ("high", t_high)):
        ev = r["contests"]
        if t is not None:
            fb_t, _ = sp.band_fields(work, proj, contexts, fb, own_n, prov, st, risk_cfg, seed, override, temperature=t)
            ev = {}
            for cid, ce in r["contests"].items():
                f = fb_t.fields[contests_mod.field_family(contexts[cid].family)]
                spec = ob.field_spec(f.lineups, f.keys, contexts[cid].field_size - own_n[cid], risk_cfg, n_scenarios=r["scen"].n,
                                     seed=seed, salt=f"{cid}|referee")
                ev[cid] = ob.ContestEval(ce.contest, spec, ce.entry_ids)
            del fb_t
        pm = ob.portfolio_metrics(r["assignment"], ev, r["scen"], **r["kwargs"])
        gpp = [e for e, x in pm.per_entry.items() if x.get("family") == "large_gpp"]
        out[label] = {k: pm.record()[k] for k in ("fees", "exp_payout", "exp_payout_se", "p_lose80", "p_lose80_se", "tail_utility",
                                                   "tail_utility_se", "top1pct_utility")}
        out[label]["large_gpp_first_place_equity_sum"] = round(sum(pm.per_entry[e].get("first_place_equity", 0.0) for e in gpp), 6)
    return out


def switch_on(on: bool | None) -> None:
    """--switch on|off: config/risk.yaml selection.ownership_band forced for the run (flag 123)."""
    from nhl_dfs.build import objectives as ob

    if on is None:
        return
    real = ob.load_risk_config

    def load(path=ob.RISK_YAML):
        cfg = copy.deepcopy(real(path))
        cfg.setdefault("selection", {})["ownership_band"] = bool(on)
        return cfg

    ob.load_risk_config = load


def cmd_run(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = c24.set_roots(out)
    where = c24.preload(Path(args.expect_src))
    import c17_replay as guard
    from pool_builder import clone_entries

    from nhl_dfs.build import packet
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.data.history import store as store_mod

    store_mod.DEFAULT_ROOT = REPO / "data" / "features" / "history"
    guard.install_guards(out)
    install(REC)
    on = {"on": True, "off": False, "as-is": None}[args.report]
    report_switch(on)
    switch_on({"on": True, "off": False, "as-is": None}[args.switch])
    cal = json.loads(Path(args.calibration).read_text(encoding="utf-8")) if args.calibration else None
    print(f"{args.label}: nhl_dfs from {where}; report {args.report}; seed {args.seed}; "
          f"PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}", flush=True)
    results = {}
    path = out / "results" / f"{args.label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for src in args.saved_run:
        salary, entries, clock, rid = c24.saved_inputs(Path(src), out)
        cloned = out / "saved" / rid / f"DKEntries_cloned{N_CLONED}.csv"
        if not cloned.exists():
            clone_entries(entries, cloned, N_CLONED)
        res, m, rec = replay(salary, cloned, clock, out, f"{args.label}-{rid}", int(args.seed))
        sec = m.get("scenario") or {}
        rec.update({"slate": rid, "nhl_dfs": where, "seed": int(args.seed), "report": args.report,
                    "timings": sec.get("timings"), "chosen_kappa": (sec.get("chosen") or {}).get("kappa"),
                    "selection_draws_sha1": _draws_sha1(), "in_run_report": sec.get("ownership_bands")})
        try:
            rec["lineups"] = {str(e): list(lu) for e, lu in packet.RunView(res.run).lineups.items()}
        except Exception as exc:
            rec["lineups"], rec["lineups_error"] = {}, f"{type(exc).__name__}: {str(exc)[:120]}"
        sel = REC.select["sel"] if REC.select else None
        if sel is not None:
            from nhl_dfs.build.objectives import load_risk_config

            risk = REC.select["kwargs"].get("risk_cfg") or load_risk_config()
            rec["base_picks"] = {e: {"family": c.family, "contest_id": c.contest_id, "key": c.key, "anchor": c.anchor_key,
                                     "band_index": c.band_index, "band_term": pf.band_term(c, risk),
                                     "anchor_value": c.anchor_value, "anchor_se": c.anchor_se, "score": c.score, "se": c.score_se}
                                 for e, c in sel.choices.items()} if hasattr(sel.choices[next(iter(sel.choices))], "anchor_se") else None
        if args.referee_bands and REC.referee is not None and REC.grow is not None:
            from nhl_dfs.models import ownership as own_mod

            c = own_mod.load_ownership_config()["field"]["error_band"]["classic"]
            t0 = time.perf_counter()
            rec["referee_bands"] = _referee_bands(float(c["t_low"]), float(c["t_high"]))
            rec["referee_bands_seconds"] = round(time.perf_counter() - t0, 1)
            rec["selection_ownership_band"] = sec.get("selection_ownership_band")
            rec["screened"] = (sec.get("discovery") or {}).get("screened")
            print("  referee " + "; ".join(f"{k}: E[payout] {v['exp_payout']:.2f}, P(lose80) {v['p_lose80']:.4f}, first-place "
                                           f"utility/fees {v['tail_utility']:.4f}" for k, v in rec["referee_bands"].items()), flush=True)
        if on and cal is not None and REC.bands is not None:
            p = cal["pools"][rid]
            specs = [("low", float(p["low"]["t"]), 0), ("high", float(p["high"]["t"]), 0), ("control1", 1.0, 1), ("control2", 1.0, 2)]
            rec["instrument"] = _instrument(specs, sel.choices)
            rec["instrument_specs"] = specs
        results[rid] = rec
        ib = rec.get("in_run_report") or {}
        print(f"{rid}: ok={rec['ok']} {rec['seconds']} s, version {rec['version']}, failed {rec['failed']}, knob {rec['chosen_kappa']}, "
              f"in-run report {ib.get('seconds', ib.get('skipped'))} s, peak {(rec['timings'] or {}).get('peak_working_set_mb')} MB",
              flush=True)
        for label, p in ((rec.get("instrument") or {}).get("picks") or {}).items():
            fs = (rec["instrument"]["full_select"] or {}).get(label, {})
            print(f"  {label}: large_gpp pick flips {p['large_gpp_pick_flips']}, anchor flips {p['large_gpp_anchor_flips']} of "
                  f"{p['large_gpp_picks']}; free flips {p.get('free_flips')}; full select knob {fs.get('chosen_kappa')} "
                  f"(changed {fs.get('lineups_changed')})", flush=True)
        path.write_text(json.dumps({"label": args.label, "nhl_dfs": where, "seed": int(args.seed), "roots": roots, "runs": results,
                                    "writes_refused": guard.WRITES, "network_blocked": guard.NET}, indent=1, default=str),
                        encoding="utf-8")
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; wrote {path}", flush=True)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


# -- compare ----------------------------------------------------------------------------------------------------------------

def _load(out: Path, label: str) -> dict:
    return json.loads((out / "results" / f"{label}.json").read_text(encoding="utf-8"))


def _changed(a: dict, b: dict) -> int | None:
    if not a.get("lineups") or not b.get("lineups"):
        return None
    return sum(1 for e in a["lineups"] if a["lineups"][e] != b["lineups"].get(e))


def compare(out: Path, on_label: str, off_label: str, master: str | None) -> dict:
    on, off = _load(out, on_label), _load(out, off_label)
    need = ("low", "high", "control1", "control2")
    slates = sorted(on["runs"])
    for rid in slates:
        have = set(((on["runs"][rid].get("instrument") or {}).get("picks") or {}))
        if not set(need) <= have:
            raise SystemExit(f"{rid}: the instrument holds {sorted(have)}, not all of {need}: no verdict")
    per = {}
    for rid in slates:
        r = on["runs"][rid]
        p = r["instrument"]["picks"]
        alt = max(p["low"]["large_gpp_anchor_flips"], p["high"]["large_gpp_anchor_flips"])
        ctl = max(p["control1"]["large_gpp_anchor_flips"], p["control2"]["large_gpp_anchor_flips"])
        terms = {}
        for e, c in (r.get("base_picks") or {}).items():
            if c["family"] == "large_gpp":
                terms[c["band_term"]] = terms.get(c["band_term"], 0) + 1
        ro, rf = r.get("timings") or {}, (off["runs"][rid].get("timings") or {})
        per[rid] = {"anchor_flips": {k: p[k]["large_gpp_anchor_flips"] for k in need},
                    "pick_flips": {k: p[k]["large_gpp_pick_flips"] for k in need},
                    "free_flips": {k: p[k].get("free_flips") for k in need},
                    "large_gpp_picks": p["low"]["large_gpp_picks"], "max_alt": alt, "max_control": ctl, "moves": alt > ctl,
                    "full_select": r["instrument"].get("full_select"), "spread": r["instrument"].get("spread"),
                    "fields": {k: v["families"].get(FAMILY) for k, v in r["instrument"]["fields"].items()},
                    "band_terms_large_gpp": terms,
                    "cost": {"report_s": ro.get("ownership_bands_s"),
                             "peak_on_mb": ro.get("peak_working_set_mb"), "peak_off_mb": rf.get("peak_working_set_mb"),
                             "added_peak_mb": (round(ro["peak_working_set_mb"] - rf["peak_working_set_mb"], 1)
                                               if ro.get("peak_working_set_mb") and rf.get("peak_working_set_mb") else None),
                             "selection_peak_on_mb": ro.get("selection_peak_mb"), "selection_peak_off_mb": rf.get("selection_peak_mb")},
                    "lineups_changed_on_vs_off": _changed(r, off["runs"][rid]),
                    "draws_identical_on_vs_off": r.get("selection_draws_sha1") == off["runs"][rid].get("selection_draws_sha1"),
                    "other_python": {"on": r.get("other_python_at_start"), "off": off["runs"][rid].get("other_python_at_start")}}
        if master:
            mrun = _load(out, master)["runs"][rid]
            per[rid]["lineups_changed_master_vs_off"] = _changed(mrun, off["runs"][rid])
            per[rid]["draws_identical_master_vs_off"] = mrun.get("selection_draws_sha1") == off["runs"][rid].get("selection_draws_sha1")
    moves = all(v["moves"] for v in per.values())
    cost_ok = all((v["cost"]["report_s"] or 1e9) <= 60.0 and v["cost"]["added_peak_mb"] is not None
                  and v["cost"]["added_peak_mb"] <= 1024.0 for v in per.values())
    return {"slates": per, "verdict": "the spread moves a pick (flag 121)" if moves else "the spread does not move a pick (flag 121)",
            "moves": moves, "report_cost_ok": cost_ok,
            "report_ships": "ON" if cost_ok else "OFF (flag 122: over 60 s or 1 GB on a slate)",
            "guard": {lab: {"writes_refused": len(d["writes_refused"]), "network_blocked": len(d["network_blocked"])}
                      for lab, d in ((on_label, on), (off_label, off))}}


def cmd_compare(args) -> int:
    out = Path(args.out).resolve()
    res = compare(out, args.on, args.off, args.master)
    (out / "compare.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    for rid, v in res["slates"].items():
        print(f"{rid}: anchor flips {v['anchor_flips']} of {v['large_gpp_picks']}; pick flips {v['pick_flips']}; moves {v['moves']}; "
              f"cost {v['cost']}; on vs off lineups changed {v['lineups_changed_on_vs_off']}", flush=True)
    print(res["verdict"], "; report", res["report_ships"], flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("band")
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_band)
    for name, fn in (("calibrate", cmd_calibrate), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--seed", type=int, default=20261015)
        p.add_argument("--expect-src", required=True)
        p.add_argument("--saved-run", action="append", required=True)
        if name == "run":
            p.add_argument("--label", required=True)
            p.add_argument("--report", choices=("on", "off", "as-is"), default="on")
            p.add_argument("--calibration", default=None)
            p.add_argument("--switch", choices=("on", "off", "as-is"), default="as-is")
            p.add_argument("--referee-bands", action="store_true")
        p.set_defaults(func=fn)
    c = sub.add_parser("compare")
    c.add_argument("--out", required=True)
    c.add_argument("--on", required=True)
    c.add_argument("--off", required=True)
    c.add_argument("--master", default=None)
    c.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
