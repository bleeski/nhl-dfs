"""C23 Showdown Captain arms (flags 96 to 99, docs/experiments/captain_2026-10-13.md): the goalie gate and the Captain leverage term,
each against both switches off, on the model's referee P(top 1%). Preregistered arms, seeds, metric and verdict; this script only runs
them and reads each run's manifest (`scenario.entries`, `scenario.captain`).

Arms per slate (every run is the whole OFFLINE scenario pass, `run_slate(scenario=True)`, in a scratch folder; config/risk.yaml is never
edited, the arm's switches are set on an in-memory copy):
  O  both off (the shipping tree)      decides as the baseline, seeds 20261013 to 20261017
  G  goalie gate only                  decides against O, same seeds
  L  leverage term only                decides against O, same seeds
  B  both on                           information only, seed 20261013
The noise baseline comes first: O at seed 20261013 twice (B98); the first of the two is O's seed-20261013 run. Entries are cloned to 30.

Set PYTHONHASHSEED=0 (PowerShell: $env:PYTHONHASHSEED="0"). Resumable: a run whose record is already in <out>/results.json is skipped.
Exit code 1 means a write other than the known observation-log append was refused.

Data safety: the four NHL_DFS_* roots point into --out, the network is blocked and every write under the repo's data/, runs/, outputs/ or
BACKLOG.md is refused and counted (the guard of scripts/c17_replay.py). Saved runs' inputs and manifests are copied into --out first.

    python scripts/c23_captain.py --out <scratch dir>                                    # the synthetic bed: decides nothing
    python scripts/c23_captain.py --out <scratch dir> --runs-root runs --since 2026-10-03 # every saved Showdown parent run (B127)
    python scripts/c23_captain.py --out <scratch dir> --saved-run runs/<id> [...]          # named runs

The bed is tests/fixtures/mini/showdown (one game, VAN@CAR) with its entries cloned to 30, priors only. It is synthetic: it says nothing
about any real slate, and any synthetic slate makes the study NOT MEASURED.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))

BED = REPO / "tests" / "fixtures" / "mini" / "showdown"
BED_CLOCK = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
SEEDS = [20261013 + i for i in range(5)]  # preregistered
N_CLONED = 30  # preregistered
BED_N = {"small": {"design": 300, "selection": 800, "referee": 800, "field_target": 400},
         "medium": {"design": 1000, "selection": 3000, "referee": 3000, "field_target": 1000}}
FAMILIES = ("large_gpp", "small_field", "wta")  # the metric's entries
T_CUT, ACCEPT_REL, REJECT_REL, MDE_REL, POS_SEEDS = 2.78, 0.03, 0.01, 0.10, 4  # preregistered (judgment)
ARMS = {"O": (False, False), "G": (True, False), "L": (False, True), "B": (True, True)}  # (gate, leverage)


def set_roots(out: Path) -> dict[str, str]:
    roots = {"NHL_DFS_RUNS_ROOT": out / "roots" / "runs", "NHL_DFS_OUTPUTS_ROOT": out / "roots" / "outputs",
             "NHL_DFS_LEDGER_ROOT": out / "roots" / "ledger", "NHL_DFS_BACKLOG": out / "roots" / "BACKLOG.md"}
    for k, v in roots.items():
        os.environ[k] = str(v)
        (v if k != "NHL_DFS_BACKLOG" else v.parent).mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in roots.items()}


def arm_config(arm: str) -> dict:
    from nhl_dfs.build import objectives as ob

    cfg = copy.deepcopy(ob.load_risk_config())
    gate, lev = ARMS[arm]
    cfg["discovery"]["captain"]["goalie_gate"]["enabled"] = gate
    cfg["discovery"]["captain"]["leverage"]["enabled"] = lev
    return cfg


def run_one(salary: Path, entries: Path, clock: datetime, scenario_n: dict | None, out: Path, tag: str, seed: int, arm: str) -> dict:
    """One whole offline scenario pass under the arm's switches; returns the preregistered figures read from the manifest."""
    import numpy  # noqa: F401  (imported before the guard's hooks matter)

    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import packet
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.intake.salary import read_salary

    cfg = arm_config(arm)
    real = ob.load_risk_config
    ob.load_risk_config = lambda *a, **k: copy.deepcopy(cfg)  # every reader calls through the module
    t0 = time.perf_counter()
    try:
        res = run_slate(salary, entries, offline=True, baseline_only=False, scenario=True, scenario_n=scenario_n, seed=seed,
                        out_root=out / "work" / tag / "runs", outputs_root=out / "work" / tag / "outputs", clock=lambda: clock)
    finally:
        ob.load_risk_config = real
    m = read_manifest(res.run)
    sc = m.get("scenario") or {}
    cap = sc.get("captain") or {}
    pol = cap.get("policy") or {}
    gg, lv = pol.get("goalie_gate") or {}, pol.get("leverage") or {}
    if (bool(gg.get("enabled")), bool(lv.get("enabled"))) != ARMS[arm]:
        raise RuntimeError(f"{tag}: the arm's switches did not reach the run (gate {gg.get('enabled')}, leverage {lv.get('enabled')})")
    v = packet.RunView(res.run)
    pool = read_salary(salary)
    rows = sc.get("entries") or []
    fam_rows = [r for r in rows if r.get("family") in FAMILIES and r.get("p_top1pct") is not None]
    gcpt = {r["entry_id"]: bool(v.lineups.get(r["entry_id"]) and pool.by_role_id[v.lineups[r["entry_id"]][0]].is_goalie) for r in fam_rows}
    by_g = [r["p_top1pct"] for r in fam_rows if gcpt[r["entry_id"]]]
    by_s = [r["p_top1pct"] for r in fam_rows if not gcpt[r["entry_id"]]]
    ok = bool(res.ok and sc.get("version") and sc.get("version") == v.version and "error" not in cap)
    return {
        "tag": tag, "arm": arm, "seed": seed, "ok": ok, "seconds": round(time.perf_counter() - t0, 1),
        "metric": round(statistics.fmean(r["p_top1pct"] for r in fam_rows), 6) if fam_rows else None, "metric_entries": len(fam_rows),
        "risk_budget": (m.get("statuses") or {}).get("RISK_BUDGET"),
        "portfolio": cap.get("portfolio"), "would_bar": gg.get("would_bar"), "barred_roles": gg.get("barred_roles"),
        "goalie_ranks": gg.get("goalies"), "leverage_points": [lv.get("points_min"), lv.get("points_max")],
        "gate_dropped": cap.get("gate_dropped"), "split": cap.get("split"),
        "p_top1_goalie_cpt": round(statistics.fmean(by_g), 6) if by_g else None, "n_goalie_cpt": len(by_g),
        "p_top1_skater_cpt": round(statistics.fmean(by_s), 6) if by_s else None, "n_skater_cpt": len(by_s),
        "lineups": {e: list(lu) for e, lu in sorted(v.lineups.items())},
        "lineups_sha": hashlib.sha256(json.dumps(sorted(v.lineups.items())).encode()).hexdigest()[:16],
        "contests": [{k: c.get(k) for k in ("contest_id", "name", "family", "field_size", "PAYOUT_SOURCE", "FIELD_SIZE_SOURCE")}
                     for c in sc.get("contests") or []],
        "statuses": {k: (m.get("statuses") or {}).get(k) for k in ("MODEL_STATUS", "FIELD_CALIBRATION", "PAYOUT_SOURCE", "RISK_BUDGET")},
        "failed": m.get("failed"),
    }


def switch_verdict(o_runs: list[dict], a_runs: list[dict], arm: str) -> dict:
    """The preregistered per-slate rule for one switch (G or L) against O, paired by seed."""
    if not o_runs or len(o_runs) != len(a_runs) or any(not r["ok"] or r["metric"] is None for r in o_runs + a_runs):
        return {"verdict": "NO VERDICT", "reason": "a run failed or has no entry of the three families"}
    n = len(o_runs)
    o = [r["metric"] for r in o_runs]
    d = [a["metric"] - b["metric"] for a, b in zip(a_runs, o_runs)]
    mo = statistics.fmean(o)
    mde = T_CUT * statistics.stdev(o) / math.sqrt(n) if n > 1 else float("inf")
    mean_d = statistics.fmean(d)
    se_d = statistics.stdev(d) / math.sqrt(n) if n > 1 else float("inf")
    pos = sum(x > 0 for x in d)
    budget_ok = not any(a["risk_budget"] == "BREACHED" and b["risk_budget"] != "BREACHED" for a, b in zip(a_runs, o_runs))
    g_a = sum((r["portfolio"] or {}).get("goalie_cpt_entries", 0) for r in a_runs)
    g_o = sum((r["portfolio"] or {}).get("goalie_cpt_entries", 0) for r in o_runs)
    goalie_ok = arm != "L" or g_a <= g_o
    out = {"n_seeds": n, "mean_O": round(mo, 6), "mde": round(mde, 6), "mean_d": round(mean_d, 6), "se_d": round(se_d, 6),
           "seeds_d_positive": pos, "d": [round(x, 6) for x in d], "risk_budget_ok": budget_ok,
           "goalie_cpt_entries_arm": g_a, "goalie_cpt_entries_O": g_o, "goalie_ok": goalie_ok}
    if mde > MDE_REL * mo:
        return out | {"verdict": "INCONCLUSIVE", "reason": "MDE above 10% of O's mean"}
    if (mean_d >= T_CUT * se_d and mean_d >= ACCEPT_REL * mo and pos >= (POS_SEEDS if n == 5 else n) and budget_ok and goalie_ok):
        return out | {"verdict": "ACCEPT-TO-SHADOW", "reason": "every acceptance condition holds"}
    if mean_d <= 0 or mean_d < REJECT_REL * mo:
        return out | {"verdict": "REJECT", "reason": "mean difference at or below 0, or under 1% of O's mean"}
    return out | {"verdict": "INCONCLUSIVE", "reason": "neither accepted nor rejected"}


def study_verdict(slates: list[dict], arm: str) -> str:
    if any(s["synthetic"] for s in slates):
        return "NOT MEASURED"
    vs = [s["verdicts"][arm]["verdict"] for s in slates if s["verdicts"][arm]["verdict"] != "NO VERDICT"]
    if len(vs) < 3:
        return "NOT MEASURED"
    acc, rej = vs.count("ACCEPT-TO-SHADOW"), vs.count("REJECT")
    if acc >= len(vs) / 2 and rej == 0:
        return "ACCEPT-TO-SHADOW"
    if rej >= len(vs) / 2 and acc == 0:
        return "REJECT"
    return "INCONCLUSIVE"


def saved_inputs(src: Path, out: Path) -> tuple[Path, Path, datetime, str, dict]:
    """Copy a saved run's inputs and manifest into --out; returns (salary, entries, clock, run id, manifest)."""
    src = Path(src).resolve()
    if not (src / "inputs" / "DKSalaries.csv").exists() or not (src / "inputs" / "DKEntries.csv").exists():
        raise SystemExit(f"{src}: no inputs/DKSalaries.csv and inputs/DKEntries.csv (give the run that built the slate, not a child)")
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    if man.get("parent_run_id"):  # a late swap or refresh child keeps its own inputs and a near-lock clock
        raise SystemExit(f"{src}: a late-swap or refresh child of {man['parent_run_id']}; give runs/{man['parent_run_id']}")
    if man.get("mode") != "showdown":
        raise SystemExit(f"{src}: mode {man.get('mode')}, not a Showdown run")
    dst = out / "saved" / src.name
    shutil.copytree(src / "inputs", dst / "inputs", dirs_exist_ok=True)
    shutil.copy2(src / "manifest.json", dst / "manifest.json")
    clock = datetime.fromisoformat(man["created_utc"].replace("Z", "+00:00"))
    return dst / "inputs" / "DKSalaries.csv", dst / "inputs" / "DKEntries.csv", clock, src.name, man


def find_runs(root: Path, since: str) -> list[Path]:
    """The newest parent Showdown run per slate id, created on or after `since`, that published a scenario version."""
    best: dict[str, tuple[str, Path]] = {}
    for d in sorted(Path(root).iterdir() if Path(root).is_dir() else []):
        try:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        created = str(man.get("created_utc") or "")
        if (man.get("mode") != "showdown" or man.get("parent_run_id") or created[:10] < since
                or not (man.get("scenario") or {}).get("version") or not (d / "inputs" / "DKEntries.csv").exists()):
            continue
        sid = str(man.get("slate_id") or d.name)
        if sid not in best or created > best[sid][0]:
            best[sid] = (created, d)
    return [p for _, p in sorted(best.values())]


def run_slate_arms(name: str, salary: Path, entries: Path, clock: datetime, scenario_n: dict | None, out: Path, seeds: list[int],
                   synthetic: bool, done: dict, results: list[dict]) -> dict:
    from pool_builder import clone_entries

    cloned = out / f"{name}_DKEntries_cloned{N_CLONED}.csv"
    clone_entries(entries, cloned, N_CLONED)

    def go(arm: str, seed: int, tag: str) -> dict:
        key = f"{name}-{tag}"
        if key in done and done[key].get("ok"):
            print(f"{key}: already done, skipped", flush=True)
            return done[key]
        r = run_one(salary, cloned, clock, scenario_n, out, key, seed, arm) | {"slate": name, "synthetic": synthetic, "key": key}
        done[key] = r
        results.append(r)
        p = r["portfolio"] or {}
        routes = "; ".join(f"{c['contest_id']} {c['family']} {c['PAYOUT_SOURCE']}/{c['FIELD_SIZE_SOURCE']}" for c in r["contests"])
        print(f"{key}: ok={r['ok']} P(top1%)={r['metric']} goalie CPT {p.get('goalie_cpt_entries')} CPT own {p.get('mean_cpt_own_pct')}% "
              f"captains {p.get('distinct_captains')} max {p.get('max_captain_count')}/{p.get('captain_cap')} barred {r['barred_roles']} "
              f"dropped {r['gate_dropped']} {r['seconds']}s | {routes}", flush=True)
        (out / "results.json").write_text(json.dumps({"runs": list(done.values())}, indent=1, default=str), encoding="utf-8")
        return r

    first = go("O", seeds[0], "O-s0")
    second = go("O", seeds[0], "O-s0-noise")
    noise = {"identical_lineups": first["lineups_sha"] == second["lineups_sha"], "metric_first": first["metric"],
             "metric_second": second["metric"]}
    print(f"{name} NOISE: identical lineups {noise['identical_lineups']}, P(top1%) {first['metric']} and {second['metric']}", flush=True)
    runs = {arm: [first if (arm == "O" and i == 0) else go(arm, s, f"{arm}-s{i}") for i, s in enumerate(seeds)] for arm in ("O", "G", "L")}
    both = go("B", seeds[0], "B-s0")

    def changed(a: dict, b: dict) -> int:
        return sum(1 for e, lu in a["lineups"].items() if lu != b["lineups"].get(e))

    info = {arm: {"lineups_changed_vs_O": [changed(r, o) for r, o in zip(runs[arm], runs["O"])],
                  "goalie_cpt_entries": [(r["portfolio"] or {}).get("goalie_cpt_entries") for r in runs[arm]],
                  "mean_cpt_own_pct": [(r["portfolio"] or {}).get("mean_cpt_own_pct") for r in runs[arm]],
                  "distinct_captains": [(r["portfolio"] or {}).get("distinct_captains") for r in runs[arm]],
                  "gate_dropped": [r["gate_dropped"] for r in runs[arm]]} for arm in ("O", "G", "L")}
    info["B"] = {"metric": both["metric"], "lineups_changed_vs_O": changed(both, first), "portfolio": both["portfolio"]}
    info["O_goalie_vs_skater_captain_p_top1"] = [(r["p_top1_goalie_cpt"], r["n_goalie_cpt"], r["p_top1_skater_cpt"], r["n_skater_cpt"])
                                                 for r in runs["O"]]
    return {"slate": name, "synthetic": synthetic, "clock": clock.isoformat(), "entries_cloned_to": N_CLONED,
            "scenario_n": scenario_n or "shipping (config/risk.yaml)", "noise": noise,
            "verdicts": {arm: switch_verdict(runs["O"], runs[arm], arm) for arm in ("G", "L")},
            "goalie_ranks": first["goalie_ranks"], "would_bar": first["would_bar"],
            "field_goalie_cpt_pct": (first["portfolio"] or {}).get("field_goalie_cpt_pct"), "split_O": first["split"],
            "contests": first["contests"], "information_only": info,
            "routing_same_in_every_run": all([(c["contest_id"], c["family"]) for c in r["contests"]]
                                             == [(c["contest_id"], c["family"]) for c in first["contests"]]
                                             for rs in runs.values() for r in rs)}


def report_md(summary: dict) -> str:
    L = [f"# C23 Showdown Captain arms: gate {summary['verdict']['G']}, leverage {summary['verdict']['L']}", "",
         f"Run {summary['finished_utc']}; PYTHONHASHSEED={summary['pythonhashseed']}; seeds {summary['seeds']}.", "",
         "| Slate | Synthetic | Switch | mean O | mean d | se d | MDE | seeds d>0 | Verdict |", "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for s in summary["slates"]:
        for arm in ("G", "L"):
            v = s["verdicts"][arm]
            L.append(f"| {s['slate']} | {s['synthetic']} | {arm} | {v.get('mean_O')} | {v.get('mean_d')} | {v.get('se_d')} | {v.get('mde')} | "
                     f"{v.get('seeds_d_positive')} | {v['verdict']} |")
    for s in summary["slates"]:
        L += ["", f"## {s['slate']}", "", f"- Noise: {s['noise']}", f"- Goalie ranks (O seed 1): {s['goalie_ranks']}; would bar {s['would_bar']}",
              f"- Modeled field goalie-Captain share: {s['field_goalie_cpt_pct']}% (observed once: 23%)", f"- Split (O seed 1): {s['split_O']}",
              f"- Routing the same in every run: {s['routing_same_in_every_run']}"]
        for c in s["contests"]:
            L.append(f"- contest {c['contest_id']} {c.get('name')}: {c['family']}, field {c['field_size']}, "
                     f"PAYOUT_SOURCE={c['PAYOUT_SOURCE']}, FIELD_SIZE_SOURCE={c['FIELD_SIZE_SOURCE']}")
        L.append(f"- Information only: {json.dumps(s['information_only'])}")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="scratch folder (all writes go here)")
    ap.add_argument("--saved-run", type=Path, action="append", default=[], help="a saved Showdown run folder; repeatable")
    ap.add_argument("--runs-root", type=Path, default=None, help="find every saved Showdown parent run here (newest per slate)")
    ap.add_argument("--since", default="2026-10-03", help="with --runs-root: runs created on or after this date (UTC)")
    ap.add_argument("--small", action="store_true", help="the bed at the small scenario counts (tests); never on real slates")
    ap.add_argument("--seeds", type=int, default=5, help="seeds per arm; 5 is the preregistered sample")
    ap.add_argument("--time-shipping", action="store_true", help="the bed: also time one O run at the shipping counts (information)")
    args = ap.parse_args(argv)
    if not 1 <= args.seeds <= len(SEEDS):
        ap.error(f"--seeds takes 1 to {len(SEEDS)} (the preregistered seeds)")
    real = list(args.saved_run) + (find_runs(args.runs_root, args.since) if args.runs_root else [])
    if args.small and real:
        ap.error("--small is for the bed; real slates run the shipping scenario counts")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = set_roots(out)
    import c17_replay as guard  # the repo's write guard and network block

    guard.install_guards(out)
    listing = os.popen("tasklist" if os.name == "nt" else "ps -eo pid,cmd").read().splitlines()
    others = [x.strip() for x in listing if ("python" in x.lower() or "pytest" in x.lower()) and "c23_captain" not in x]
    print(f"other python processes at start: {len(others)} {others[:3]}; PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}", flush=True)
    try:
        done = {r["key"]: r for r in json.loads((out / "results.json").read_text(encoding="utf-8")).get("runs", []) if r.get("key")}
    except (OSError, ValueError):
        done = {}
    seeds = SEEDS[:args.seeds]
    results: list[dict] = []
    slates = []
    if real:
        print(f"real Showdown runs: {[p.name for p in real]} (out of sample for the pattern: the question came from 2026-10-02)", flush=True)
        for src in real:
            salary, entries, clock, rid, _ = saved_inputs(src, out)
            slates.append(run_slate_arms(rid, salary, entries, clock, None, out, seeds, False, done, results))
    else:
        n = BED_N["small" if args.small else "medium"]
        print(f"synthetic bed (mini Showdown fixture, {N_CLONED} entries), scenario_n {n}: decides nothing", flush=True)
        slates.append(run_slate_arms("bed", BED / "DKSalaries.csv", BED / "DKEntries.csv", BED_CLOCK, n, out, seeds, True, done, results))
    timing = None
    if args.time_shipping and not real:
        from pool_builder import clone_entries

        clone_entries(BED / "DKEntries.csv", out / "bed_shipping_entries.csv", N_CLONED)
        r = run_one(BED / "DKSalaries.csv", out / "bed_shipping_entries.csv", BED_CLOCK, None, out, "bed-shipping-O-s0", seeds[0], "O")
        timing = {"seconds": r["seconds"], "ok": r["ok"], "metric": r["metric"]}
        print(f"shipping-count O run on the bed: {timing}", flush=True)
    summary = {"verdict": {arm: study_verdict(slates, arm) for arm in ("G", "L")}, "slates": slates, "seeds": seeds,
               "thresholds": {"t": T_CUT, "accept_rel": ACCEPT_REL, "reject_rel": REJECT_REL, "mde_rel": MDE_REL, "pos_seeds": POS_SEEDS},
               "shipping_timing_on_bed": timing, "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
               "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (out / "results.json").write_text(json.dumps({"summary": summary, "runs": list(done.values())}, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(report_md(summary), encoding="utf-8")
    print(f"VERDICT: gate {summary['verdict']['G']}, leverage {summary['verdict']['L']}", flush=True)
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; roots: {roots}", flush=True)
    (out / "guard.json").write_text(json.dumps({"writes_refused": guard.WRITES, "network_blocked": guard.NET,
                                                "other_python_at_start": others}), encoding="utf-8")
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


if __name__ == "__main__":
    raise SystemExit(main())
