"""C55 first place (BEN flags 108 to 112, docs/experiments/first_place_2026-10-15.md): can the engine tell lineups apart on first place,
and does aiming at it pass the preregistered bar?

`run` replays a saved run's inputs through the whole scenario pass (`run_slate(scenario=True)`) exactly as scripts/c24_dtd_replay.py does:
entries cloned to 30, each run's created_utc clock, time-pinned web inputs, the c17 write guard with the network blocked, every NHL_DFS root
in scratch, at the seed given. Three recording wrappers watch the run and never change it: `portfolio.select` keeps its inputs (the
candidates, the selection scenarios, each contest's field), `portfolio._greedy` keeps each knob's fill order, and `tiebreak.rank` counts the
first band of every call it makes. After the run, per large_gpp and small_field contest, every candidate is ranked against that contest's
field alone and flag 108's figures are computed: the exact and the smoothed first-place estimates at 20,000, 8,000 and 4,000 scenarios,
beside today's figure (the top-1% payout for large_gpp, the payout for small_field), the simulated winning score and the prize shares.

`standings` reads the real winning score and entrant count of each contest from scratch copies of the standings exports.
`compare` applies the bar to the recorded labels and writes the report's numbers.

WHICH CODE RUNS: the tree on PYTHONPATH, checked against --expect-src (scripts/c24_dtd_replay.preload). Set PYTHONHASHSEED=0.

    python scripts/c55_first_place.py run --out <scratch> --label master-1 --seed 20261015 --expect-src <tree>/src \\
        --saved-run runs/20260929-222125-classic --saved-run runs/20260930-210607-classic
    python scripts/c55_first_place.py standings --out <scratch> --day 2026-09-29 --day 2026-09-30
    python scripts/c55_first_place.py compare --out <scratch> --part-a master-1 --top1pct c55-top-a --first-place c55-fp-a \\
        --pair master-1 master-2 --pair master-1 master-b ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))  # c24_dtd_replay and c17_replay import only the standard library at module level
import c24_dtd_replay as c24  # noqa: E402

N_CLONED = 30  # as C22 and C24
COUNTS = (20000, 8000, 4000)  # selection, full late swap, late swap --fast (flags 108 and 110)
TOP = 20
BAR = 0.25
FAMILIES = ("large_gpp", "small_field")
COL_BLOCK = 128  # candidates per block when the per-scenario figures are formed (bounded memory)


# -- recording wrappers (they return what the wrapped function returns, unchanged) ------------------------------------------

class Recorder:
    def __init__(self):
        self.local = threading.local()
        self.reset()

    def reset(self) -> None:
        self.inputs: dict | None = None
        self.fills: dict[float, dict] = {}


def install(rec: Recorder) -> None:
    from nhl_dfs.build import portfolio as pf
    from nhl_dfs.build import tiebreak

    real_select, real_greedy, real_rank = pf.select, pf._greedy, tiebreak.rank

    def select(candidates, role_base, fields, contests, entries, *a, **kw):
        rec.inputs = {"candidates": list(candidates), "scen": role_base, "fields": dict(fields), "contests": dict(contests),
                      "pool": kw.get("pool"), "risk_cfg": kw.get("risk_cfg")}
        return real_select(candidates, role_base, fields, contests, entries, *a, **kw)

    def greedy(kappa, order, entry_contest, *a, **kw):
        rec.local.calls = []
        try:
            out = real_greedy(kappa, order, entry_contest, *a, **kw)
        finally:
            calls, rec.local.calls = rec.local.calls, None
        rec.fills[float(kappa)] = {"order": list(order), "contest": dict(entry_contest), "calls": calls}
        return out

    def rank(items, cfg, policy, *a, **kw):
        out = real_rank(items, cfg, policy, *a, **kw)
        calls = getattr(rec.local, "calls", None)
        if calls is not None:
            calls.append({"policy": policy, "first_band": sum(1 for r in out if r.band_index == 0), "items": len(items)})
        return out

    pf.select, pf._greedy, tiebreak.rank = select, greedy, rank


REC = Recorder()


def band_share(fill: dict, family_of: dict[str, str]) -> dict:
    """Flag 108 part B for one knob's fill: `_greedy` calls `rank` twice per entry, the entry's policy first and "mean" second (the
    anchor), so call 2i is entry i's pick. A pick is decided inside the band when that call's first band holds two or more candidates."""
    order, calls = fill["order"], fill["calls"]
    aligned = len(calls) == 2 * len(order) and all(calls[2 * i + 1]["policy"] == "mean" for i in range(len(order)))
    out = {"aligned": aligned, "calls": len(calls), "entries": len(order)}
    if not aligned:
        return out
    gpp = [calls[2 * i] for i, e in enumerate(order) if family_of.get(fill["contest"][e]) == "large_gpp"]
    inside = sum(1 for c in gpp if c["first_band"] >= 2)
    return {**out, "large_gpp_picks": len(gpp), "inside_band": inside,
            "share": round(inside / len(gpp), 4) if gpp else None, "first_band_sizes": [c["first_band"] for c in gpp]}


# -- the first-place figures ---------------------------------------------------------------------------------------------

def smoothed(G, E, N: int):
    """Flag 109: the chance of beating N independent draws from the weighted field, ties shared: with b, t the field shares strictly
    below and tied, ((b + t)^(N + 1) - b^(N + 1)) / ((N + 1) t), and b^N when t = 0."""
    import numpy as np

    G = G.astype(np.float64)
    E = E.astype(np.float64)
    b = np.clip((N - G - E) / N, 0.0, 1.0)
    t = E / N
    out = b ** N
    m = t > 0
    if m.any():
        bm, tm = b[m], t[m]
        out[m] = ((bm + tm) ** (N + 1) - bm ** (N + 1)) / ((N + 1) * tm)
    return out


def _top(v, se, fin, keys: list[str]) -> dict:
    """The TOP candidates by value (ties by key) and the median of their SE / value (a value of 0 counts as infinite)."""
    order = sorted(range(len(keys)), key=lambda k: (-float(v[k]), keys[k]))[:TOP]
    ratios = [float(se[k] / v[k]) if v[k] > 0 else math.inf for k in order]
    srt = sorted(ratios)
    mid = len(srt) // 2
    med = (srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2) if srt else math.inf
    f = [int(fin[k]) for k in order]
    return {"median_se_over_value": None if math.isinf(med) else round(med, 4), "passes": bool(med <= BAR),
            "ratio_min": round(min(srt), 4) if srt and not math.isinf(min(srt)) else None,
            "ratio_max": None if not srt or math.isinf(max(srt)) else round(max(srt), 4),
            "zero_value_in_top": sum(1 for r in ratios if math.isinf(r)),
            "finishes_min": min(f) if f else None, "finishes_median": sorted(f)[len(f) // 2] if f else None,
            "finishes_max": max(f) if f else None, "values": [round(float(v[k]), 7) for k in order],
            "keys": [keys[k] for k in order]}


def resolution(rec: Recorder, mode) -> dict:
    import numpy as np

    from nhl_dfs.build import objectives as ob

    inp = rec.inputs
    cands, scen, cfg = inp["candidates"], inp["scen"], inp["risk_cfg"]
    keys = [c.key for c in cands]
    cand = scen.scores([c.role_ids for c in cands], mode).full()
    S, K = cand.shape
    tp = float(cfg["objectives"]["top_pct"])
    out = {}
    for cid, ct in inp["contests"].items():
        if ct.family not in FAMILIES:
            continue
        t0 = time.perf_counter()
        spec = inp["fields"][cid]
        fs, w = spec.scores(scen, mode)
        w = np.asarray(w, np.int64)
        N = int(w.sum())
        G = np.zeros((S, K), np.int32)
        E = np.zeros((S, K), np.int32)
        best = np.zeros(S, np.int64)
        live = w > 0
        step = ob._chunk_rows(S, fs.shape[1] * 64 + K * 96, float(cfg["objectives"]["memory_cap_mb"]))
        for a in range(0, S, step):
            b = min(S, a + step)
            f = np.asarray(fs[a:b])
            g, e = ob.ranks(cand[a:b], f, w)
            G[a:b], E[a:b] = g, e
            best[a:b] = (f[:, live] if spec.mode == "weighted" else f).max(axis=1)
        curve = ob.PayCurve(ct, tp)  # no goal argument: today's large_gpp (top-1%) and small_field (payout) utility on both trees
        stats = {name: {n: {"v": np.zeros(K), "se": np.zeros(K)} for n in COUNTS if n <= S} for name in ("exact", "smoothed", "today")}
        fin = {n: (G[:n] == 0).sum(axis=0) for n in COUNTS if n <= S}
        for c0 in range(0, K, COL_BLOCK):
            c1 = min(K, c0 + COL_BLOCK)
            g, e = G[:, c0:c1], E[:, c0:c1]
            _, util = curve.pay_util(g, e)
            figs = {"exact": (g == 0) / (e + 1.0), "smoothed": smoothed(g, e, N), "today": util / 100.0}
            for name, x in figs.items():
                for n, st in stats[name].items():
                    st["v"][c0:c1] = x[:n].mean(axis=0)
                    st["se"][c0:c1] = x[:n].std(axis=0) / math.sqrt(n)
        pool = int(ct.prizes_cents.sum())
        top_k = max(1, int(math.floor(tp * ct.field_size)))
        pts = best / 20.0  # twentieths to DK points
        rec_c = {"name": ct.name, "family": ct.family, "field_size": ct.field_size, "opponents": N, "field_mode": spec.mode,
                 "field_distinct": len(spec.lineups), "scenarios": S, "candidates": K,
                 "payout_source": ct.payout_source.value, "detail": ct.detail,
                 "first_prize": int(ct.prizes_cents[0]) / 100 if ct.paid else 0.0, "prize_pool": pool / 100,
                 "first_share_of_pool": round(int(ct.prizes_cents[0]) / pool, 4) if pool and ct.paid else None,
                 "top1pct_positions": top_k,
                 "top1pct_share_of_pool": round(int(ct.prizes_cents[:top_k].sum()) / pool, 4) if pool else None,
                 "sim_winning_score": {"median": round(float(np.median(pts)), 1), "p10": round(float(np.quantile(pts, 0.1)), 1),
                                       "p90": round(float(np.quantile(pts, 0.9)), 1)},
                 "candidates_with_a_finish": {str(n): int((f > 0).sum()) for n, f in fin.items()}}
        for name, per_n in stats.items():
            rec_c[name] = {str(n): _top(st["v"], st["se"], fin[n], keys) for n, st in per_n.items()}
        ex, sm = set(rec_c["exact"][str(S)]["keys"]), set(rec_c["smoothed"][str(S)]["keys"])
        rec_c["top_overlap_exact_smoothed"] = len(ex & sm)
        rec_c["seconds"] = round(time.perf_counter() - t0, 1)
        out[cid] = rec_c
        del G, E
    return out


# -- one replay ------------------------------------------------------------------------------------------------------------

def _goal() -> str:
    from nhl_dfs.build import objectives as ob

    goal_of = getattr(ob, "goal_of", None)
    return goal_of(ob.load_risk_config()) if goal_of else "top1pct (no switch in this tree)"


def _figures(m: dict) -> dict:
    """The run's own reported figures (referee scenarios): the portfolio, the chosen knob, the frontier and per contest the sums of the
    per-entry figures."""
    sec = m.get("scenario") or {}
    pf = sec.get("portfolio") or {}
    per: dict[str, dict] = {}
    for e in sec.get("entries", []):
        c = per.setdefault(e["contest_id"], {"family": e["family"], "entries": 0, "objective": e.get("objective"), "exp_payout": 0.0,
                                             "p_cash_sum": 0.0, "first_place_equity_sum": 0.0, "p_top1pct_sum": 0.0,
                                             "exp_payout_top1pct": 0.0 if "exp_payout_top1pct" in e else None})
        c["entries"] += 1
        c["exp_payout"] += float(e.get("exp_payout", 0.0))
        c["p_cash_sum"] += float(e.get("p_cash", 0.0))
        c["first_place_equity_sum"] += float(e.get("first_place_equity", 0.0))
        c["p_top1pct_sum"] += float(e.get("p_top1pct", 0.0))
        if c["exp_payout_top1pct"] is not None:
            c["exp_payout_top1pct"] += float(e.get("exp_payout_top1pct", 0.0))
    for c in per.values():
        for k in ("exp_payout", "p_cash_sum", "first_place_equity_sum", "p_top1pct_sum", "exp_payout_top1pct"):
            if c[k] is not None:
                c[k] = round(c[k], 6)
    return {"chosen": sec.get("chosen"), "goal_recorded": sec.get("goal"),
            "portfolio": {k: pf.get(k) for k in ("fees", "exp_payout", "exp_payout_se", "p_zero_payout", "p_net_loss", "p_lose80",
                                                 "p_lose80_se", "tail_utility", "tail_utility_se", "top1pct_utility",
                                                 "top1pct_utility_se")},
            "frontier": [{k: p.get(k) for k in ("kappas", "kappa", "tail_utility", "p_lose80", "exp_payout", "feasible",
                                                "top1pct_utility")} for p in sec.get("frontier", [])],
            "per_contest": per}


def run_one(salary: Path, entries: Path, clock, out: Path, tag: str, seed: int, mode) -> dict:
    import c17_replay as guard
    import numpy as np

    from nhl_dfs.build import packet
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
    rec = REC
    rec.reset()
    t0 = time.perf_counter()
    res = run_slate(salary, entries, offline=False, baseline_only=False, scenario=True, seed=seed,
                    out_root=work / "runs", outputs_root=work / "outputs", cache=cache, clock=lambda: clock)
    secs = round(time.perf_counter() - t0, 1)
    m = read_manifest(res.run)
    sec = m.get("scenario") or {}
    out_rec = {"tag": tag, "ok": bool(res.ok), "seconds": secs, "failed": m.get("failed"), "version": sec.get("version"),
               "timings": sec.get("timings"), "statuses": {k: (m.get("statuses") or {}).get(k) for k in (
                   "MODEL_STATUS", "FIELD_CALIBRATION", "PAYOUT_SOURCE", "RISK_BUDGET")},
               "pinned_urls": len(bodies), "served": len(transport.served), "unpinned": sorted(set(transport.unpinned))[:20],
               "figures": _figures(m)}
    try:
        out_rec["lineups"] = {str(e): list(lu) for e, lu in packet.RunView(res.run).lineups.items()}
    except Exception as exc:  # reported, as c24 does
        out_rec["lineups"], out_rec["lineups_error"] = {}, f"{type(exc).__name__}: {str(exc)[:120]}"
    if rec.inputs is None:
        out_rec["resolution"] = None
        return out_rec
    base = np.ascontiguousarray(rec.inputs["scen"].base)
    out_rec["selection_draws_sha1"] = hashlib.sha1(base.tobytes()).hexdigest()
    family_of = {cid: ct.family for cid, ct in rec.inputs["contests"].items()}
    chosen = (sec.get("chosen") or {}).get("kappa")
    out_rec["bands"] = {f"{k:g}": band_share(f, family_of) for k, f in sorted(rec.fills.items())}
    out_rec["band_chosen"] = out_rec["bands"].get(f"{float(chosen):g}") if chosen is not None else None
    t1 = time.perf_counter()
    out_rec["resolution"] = resolution(rec, mode)
    out_rec["resolution_seconds"] = round(time.perf_counter() - t1, 1)
    return out_rec


def cmd_run(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = c24.set_roots(out)  # before any nhl_dfs import: cli reads the runs root at import
    where = c24.preload(Path(args.expect_src))
    import c17_replay as guard
    from pool_builder import clone_entries

    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.data.history import store as store_mod

    store_mod.DEFAULT_ROOT = REPO / "data" / "features" / "history"  # read only; looked up at call time, so every tree reads it
    guard.install_guards(out)
    install(REC)  # once per process; run_one resets it per slate
    goal = _goal()
    others = os.environ.get("C55_OTHER_PROCESSES", "not recorded")
    print(f"{args.label}: nhl_dfs from {where}; goal {goal}; seed {args.seed}; PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}; "
          f"other processes: {others}", flush=True)
    slates = [c24.saved_inputs(Path(src), out) for src in args.saved_run]  # every input refused up front, before any run
    results = []
    path = out / "results" / f"{args.label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for salary, entries, clock, rid in slates:
        cloned = out / "saved" / rid / f"DKEntries_cloned{N_CLONED}.csv"
        if not cloned.exists():
            clone_entries(entries, cloned, N_CLONED)
        rec = run_one(salary, cloned, clock, out, f"{args.label}-{rid}", int(args.seed), Mode.CLASSIC)
        rec.update({"slate": rid, "clock": clock.isoformat(), "nhl_dfs": where, "goal": goal, "seed": int(args.seed),
                    "other_processes": others})
        results.append(rec)
        bc = rec.get("band_chosen") or {}
        print(f"{rid}: ok={rec['ok']} {rec['seconds']} s (+{rec.get('resolution_seconds')} s figures), version {rec['version']}, "
              f"failed {rec['failed']}, chosen knob {((rec['figures'] or {}).get('chosen') or {}).get('kappa')}, large_gpp picks inside "
              f"the band {bc.get('inside_band')}/{bc.get('large_gpp_picks')}", flush=True)
        for cid, r in (rec.get("resolution") or {}).items():
            ex, sm = r["exact"], r["smoothed"]
            print(f"  {cid} {r['family']} {r['name']}: median SE/value exact " + ", ".join(
                f"{n} {ex[n]['median_se_over_value']}" for n in ex) + "; smoothed " + ", ".join(
                f"{n} {sm[n]['median_se_over_value']}" for n in sm), flush=True)
        path.write_text(json.dumps({"label": args.label, "nhl_dfs": where, "goal": goal, "seed": int(args.seed), "roots": roots,
                                    "runs": results, "writes_refused": guard.WRITES, "network_blocked": guard.NET}, indent=1),
                        encoding="utf-8")
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; wrote {path}", flush=True)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


# -- real standings (flag 112, information only) ----------------------------------------------------------------------------

def cmd_standings(args) -> int:
    from nhl_dfs.learn import standings as st

    out = Path(args.out).resolve()
    res = {}
    for day in args.day:
        src = REPO / "data" / "standings" / "inbox" / day
        dst = out / "standings" / day
        for f in sorted(src.glob("*/contest-standings-*.csv")):  # scratch copies; the originals are only read
            (dst / f.parent.name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / f.parent.name / f.name)
        for f in sorted(dst.glob("*/contest-standings-*.csv")):
            s = st.read(f)
            live = [e for e in s.entries if not e.blank]
            top = max((e.points for e in live), default=None)
            res[f.parent.name] = {"day": day, "entrants": len(s.entries), "lineups": len(live),
                                  "winning_score": round(float(top), 2) if top is not None else None,
                                  "tied_at_top": sum(1 for e in live if e.points == top), "source": f.name}
    path = out / "results" / "standings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for cid, r in res.items():
        print(f"{cid}: {r['entrants']} entrants, winning score {r['winning_score']}")
    return 0


# -- the bar ---------------------------------------------------------------------------------------------------------------

def _load(out: Path, label: str) -> dict:
    return {r["slate"]: r for r in json.loads((out / "results" / f"{label}.json").read_text(encoding="utf-8"))["runs"]}


def _changed(a: dict, b: dict) -> int:
    return c24._changed(a, b)


def compare(out: Path, part_a: str, top1: str | None, first: str | None, pairs: list[tuple[str, str]], extra: list[str]) -> dict:
    A = _load(out, part_a)
    std_path = out / "results" / "standings.json"
    std = json.loads(std_path.read_text(encoding="utf-8")) if std_path.exists() else {}
    labels = sorted({part_a, *(x for x in (top1, first) if x), *(x for p in pairs for x in p), *extra})
    runs = {lb: _load(out, lb) for lb in labels}
    rep: dict = {"part_a_label": part_a, "bar": BAR, "top": TOP, "slates": {}}
    a_ok = {str(n): True for n in COUNTS}
    sm_ok = {str(n): True for n in COUNTS}
    for slate, r in A.items():
        s_rep = rep["slates"].setdefault(slate, {"contests": {}})
        for cid, c in (r.get("resolution") or {}).items():
            if c["family"] != "large_gpp":
                continue
            row = {k: c[k] for k in ("name", "field_size", "opponents", "field_mode", "candidates", "first_prize", "prize_pool",
                                     "first_share_of_pool", "top1pct_share_of_pool", "sim_winning_score", "candidates_with_a_finish",
                                     "top_overlap_exact_smoothed")}
            for name in ("exact", "smoothed", "today"):
                row[name] = {n: {k: v[k] for k in ("median_se_over_value", "passes", "finishes_min", "finishes_median",
                                                   "finishes_max", "zero_value_in_top")} for n, v in c[name].items()}
            for n, v in c["exact"].items():
                a_ok[n] = a_ok[n] and v["passes"]
            for n, v in c["smoothed"].items():
                sm_ok[n] = sm_ok[n] and v["passes"]
            row["real"] = std.get(cid)
            row["others"] = {}
            for lb, rs in runs.items():
                oc = ((rs.get(slate) or {}).get("resolution") or {}).get(cid) or {}
                if lb != part_a and oc:
                    row["others"][lb] = {n: (oc["exact"].get(n) or {}).get("median_se_over_value") for n in map(str, COUNTS)}
            s_rep["contests"][cid] = row
    rep["part_a"] = {"exact_passes": a_ok, "smoothed_passes": sm_ok}
    b = {}
    for goal, lb in (("top1pct", top1), ("first_place", first)):
        if not lb:
            continue
        picks = inside = 0
        for slate, r in runs[lb].items():
            bc = r.get("band_chosen") or {}
            if not bc.get("aligned"):
                b.setdefault("errors", []).append(f"{lb} {slate}: the band record did not align with the fill")
                continue
            picks += bc["large_gpp_picks"]
            inside += bc["inside_band"]
            rep["slates"].setdefault(slate, {"contests": {}}).setdefault("band", {})[goal] = {
                k: bc[k] for k in ("large_gpp_picks", "inside_band", "share")}
        b[goal] = {"label": lb, "picks": picks, "inside": inside, "share": round(inside / picks, 4) if picks else None}
    if "top1pct" in b and "first_place" in b and b["top1pct"]["share"] is not None and b["first_place"]["share"] is not None:
        b["passes"] = b["first_place"]["share"] <= 2 * b["top1pct"]["share"]
    rep["part_b"] = b
    rep["lineups_changed"] = {f"{x} vs {y}": {s: _changed(runs[x].get(s) or {}, runs[y].get(s) or {}) for s in runs[x]}
                              for x, y in pairs}
    rep["draws_identical"] = {f"{x} vs {y}": {s: (runs[x].get(s) or {}).get("selection_draws_sha1") ==
                                              (runs[y].get(s) or {}).get("selection_draws_sha1") for s in runs[x]}
                              for x, y in pairs}
    rep["figures"] = {lb: {s: {"goal": r.get("goal"), "seed": r.get("seed"), "seconds": r.get("seconds"), **(r.get("figures") or {})}
                           for s, r in rs.items()} for lb, rs in runs.items()}
    rep["guard"] = {lb: {"writes_refused": len(json.loads((out / "results" / f"{lb}.json").read_text(encoding="utf-8"))["writes_refused"])}
                    for lb in labels}
    on = bool(a_ok[str(COUNTS[0])] and b.get("passes"))
    rep["verdict"] = {"switch": "ON" if on else "OFF",
                      "full_late_swap_goal": "first_place" if on and a_ok["8000"] else "top1pct",
                      "fast_late_swap_goal": "first_place" if on and a_ok["4000"] else "top1pct",
                      "smoothed_only": bool(not a_ok[str(COUNTS[0])] and sm_ok[str(COUNTS[0])])}
    return rep


def cmd_compare(args) -> int:
    out = Path(args.out).resolve()
    rep = compare(out, args.part_a, args.top1pct, args.first_place, [tuple(p) for p in args.pair], args.extra)
    path = out / "results" / "compare.json"
    path.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(f"PART_A exact {rep['part_a']['exact_passes']} smoothed {rep['part_a']['smoothed_passes']}")
    print(f"PART_B {rep['part_b']}")
    print(f"VERDICT {rep['verdict']}")
    print(f"wrote {path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--seed", required=True, type=int)
    r.add_argument("--expect-src", required=True, help="the src folder of the tree on PYTHONPATH")
    r.add_argument("--saved-run", action="append", required=True)
    s = sub.add_parser("standings")
    s.add_argument("--out", required=True)
    s.add_argument("--day", action="append", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--out", required=True)
    c.add_argument("--part-a", required=True, help="the origin/master label at seed 20261015 that decides part A")
    c.add_argument("--top1pct", help="the C55 tree's goal top1pct label (part B's reference)")
    c.add_argument("--first-place", help="the C55 tree's goal first_place label at the same seed (part B)")
    c.add_argument("--pair", nargs=2, action="append", default=[], help="two labels whose lineups and draws are compared")
    c.add_argument("--extra", nargs="*", default=[], help="more labels whose figures are reported")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "standings":
        return cmd_standings(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
