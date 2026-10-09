"""C20 goalie leverage experiment (flag 54, docs/experiments/goalie_leverage_2026-10-09.md): does the discovery goalie term add
modeled top-1% equity on top of the own-goalie rule? Preregistered arms, seeds, metric and verdict rule; this script only runs them.

Arms (two keys of config/risk.yaml, flipped in memory for each run; the file is never edited):
  A0  own_goalie off, goalie_term off   today's tree (the sha256 check against origin/master is the proof)
  A1  own_goalie on,  goalie_term off   the rule only: what ships
  A2  own_goalie off, goalie_term on    the term only
  A3  own_goalie on,  goalie_term on    the rule and the term
The decision is A3 against A1, paired by seed. Every run is the whole OFFLINE scenario pass (`run_slate(scenario=True)`) in a
scratch folder, because the scenario cache keeps only the selection and referee draws, not the design draws discovery uses.

Data safety: the four NHL_DFS_* roots point into --out, the network is blocked and every write under the repo's data/, runs/,
outputs/ or BACKLOG.md is refused and counted (the guard of scripts/c17_replay.py). Inputs are copied, never changed.

    python scripts/c20_goalie_experiment.py --out <scratch dir>                       # the synthetic bed, preregistered settings
    python scripts/c20_goalie_experiment.py --out <scratch dir> --salary <DKSalaries.csv> --entries <DKEntries.csv> \\
        --clock 2026-09-29T20:00:00Z                                                  # a real slate's inputs, offline (B102)

The bed is the committed late_swap fixture (6 teams in 3 games, 2 goalies per team) with its entries file cloned to --entries rows
in one contest. It is synthetic: it says nothing about any real slate and cannot promote the term.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))

BED = REPO / "tests" / "fixtures" / "late_swap" / "classic"
BED_CLOCK = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SEEDS = [20261009 + i for i in range(5)]  # preregistered
ARMS = {"A0": (False, False), "A1": (True, False), "A2": (False, True), "A3": (True, True)}  # (own_goalie, goalie_term)
SCENARIO_N = {
    "small": {"design": 300, "selection": 800, "referee": 800, "field_target": 400},
    "medium": {"design": 1000, "selection": 3000, "referee": 3000, "field_target": 1000},
}
T_CUT = 2.78  # 95% two-sided t cutoff, 4 degrees of freedom (preregistered)


def set_roots(out: Path) -> dict[str, str]:
    roots = {"NHL_DFS_RUNS_ROOT": out / "roots" / "runs", "NHL_DFS_OUTPUTS_ROOT": out / "roots" / "outputs",
             "NHL_DFS_LEDGER_ROOT": out / "roots" / "ledger", "NHL_DFS_BACKLOG": out / "roots" / "BACKLOG.md"}
    for k, v in roots.items():
        os.environ[k] = str(v)
        (v if k != "NHL_DFS_BACKLOG" else v.parent).mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in roots.items()}


def run_arm(arm: str, seed: int, salary: Path, entries: Path, clock: datetime, scenario_n: dict, out: Path, tag: str) -> dict:
    """One whole offline scenario pass under the arm's two keys. Returns the figures the preregistration names."""
    import numpy  # noqa: F401  (imported before the guard's hooks matter)

    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build import own_goalie as og
    from nhl_dfs.build import packet
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.run import run_slate

    rule, term = ARMS[arm]
    cfg = ob.load_risk_config()
    cfg["own_goalie"]["enabled"] = rule
    cfg["discovery"].setdefault("goalie_term", {})["enabled"] = term
    real_loader = ob.load_risk_config
    ob.load_risk_config = lambda *a, **k: cfg  # the same function every module reads through (run.py, scenario_pass.py)
    t0 = time.perf_counter()
    try:
        res = run_slate(salary, entries, offline=True, baseline_only=False, scenario=True, scenario_n=scenario_n, seed=seed,
                        out_root=out / "work" / tag / "runs", outputs_root=out / "work" / tag / "outputs", clock=lambda: clock)
    finally:
        ob.load_risk_config = real_loader
    m = read_manifest(res.run)
    sc = m.get("scenario") or {}
    rows = [e for e in sc.get("entries", []) if e["family"] == "large_gpp"]
    v = packet.RunView(res.run)
    lineups = list(v.lineups.values())
    conc = (sc.get("portfolio") or {}).get("concentration") or {}
    goalies = [next(v.row(r).person_key for r in lu if v.row(r).is_goalie) for lu in lineups]
    return {
        "arm": arm, "seed": seed, "tag": tag, "ok": bool(res.ok and sc.get("version")), "seconds": round(time.perf_counter() - t0, 1),
        "metric": (sum(e["p_top1pct"] for e in rows) / len(rows)) if rows else None, "n_entries": len(rows),
        "statuses": {k: m["statuses"].get(k) for k in ("RISK_BUDGET", "GOALIE_CAP", "GAME_CAP", "OWN_GOALIE", "DELIVERY_STATUS")},
        "own_goalie_conflicts": sum(1 for lu in lineups if og.faces_own_goalie(lu, v.pool)),
        "goalie_entries": {g.split("|")[0]: goalies.count(g) for g in sorted(set(goalies))},
        "shared_failure_worst": (conc.get("shared_failure") or {}).get("worst"),
        "relaxations": sorted({r["kind"] for r in sc.get("relaxations", [])}),
        "lineups_sha": hashlib.sha256(json.dumps([list(lu) for lu in lineups]).encode()).hexdigest()[:16],
        "failed": m.get("failed"),
    }


def verdict(a1: list[dict], a3: list[dict]) -> dict:
    """The preregistered rule, in its order. a1 and a3 are the five seed runs of each arm, in seed order."""
    n = len(a1)
    x1 = [r["metric"] for r in a1]
    d = [b["metric"] - a["metric"] for a, b in zip(a1, a3)]
    mean1, mean_d = statistics.fmean(x1), statistics.fmean(d)
    sd1 = statistics.stdev(x1) if n > 1 else float("nan")
    sd_d = statistics.stdev(d) if n > 1 else float("nan")
    se_d = sd_d / math.sqrt(n)
    mde = T_CUT * sd1 / math.sqrt(n)
    breached = [i for i, (a, b) in enumerate(zip(a1, a3))
                if str(b["statuses"]["RISK_BUDGET"]).startswith("BREACHED") and not str(a["statuses"]["RISK_BUDGET"]).startswith("BREACHED")]
    mode = lambda r: str(r["statuses"].get("GOALIE_CAP") or "").split(" ")[0]  # noqa: E731
    cap_same = all(mode(a) == mode(b) for a, b in zip(a1, a3))
    out = {"n_seeds": n, "mean_A1": mean1, "mean_diff": mean_d, "se_diff": se_d, "sd_A1": sd1, "mde": mde, "mde_over_10pct": mde > 0.10 * mean1,
           "diffs": d, "positive_seeds": sum(1 for x in d if x > 0), "risk_budget_breached_seeds": breached, "goalie_cap_mode_same": cap_same}
    if n != 5:
        out["verdict"] = "NOT THE PREREGISTERED SAMPLE (5 seeds): no verdict"
    elif out["mde_over_10pct"]:
        out["verdict"] = "INCONCLUSIVE (the minimum detectable effect exceeds 10 percent of A1's mean)"
    elif (mean_d >= T_CUT * se_d and mean_d >= 0.03 * mean1 and not breached and cap_same and out["positive_seeds"] >= 4):
        out["verdict"] = "ACCEPT-TO-SHADOW"
    elif mean_d <= 0 or mean_d < 0.01 * mean1:
        out["verdict"] = "REJECT"
    else:
        out["verdict"] = "INCONCLUSIVE"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="scratch folder (all writes go here)")
    ap.add_argument("--salary", type=Path, default=None, help="a real DKSalaries.csv (default: the committed synthetic bed)")
    ap.add_argument("--entries", type=Path, default=None, help="a real DKEntries.csv (with --salary)")
    ap.add_argument("--clock", default=None, help="UTC clock for a real slate, e.g. 2026-09-29T20:00:00Z (before its first game)")
    ap.add_argument("--n-entries", type=int, default=40, help="entries cloned into the bed's contest")
    ap.add_argument("--scenario-n", choices=sorted(SCENARIO_N), default="medium")
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--seeds", type=int, default=5, help="5 is the preregistered sample")
    ap.add_argument("--noise-only", action="store_true", help="run only the noise baseline (A0 twice at seed 0)")
    ap.add_argument("--one", metavar="ARM", choices=list(ARMS), help="run one arm at seed 0 and print it (timing)")
    args = ap.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = set_roots(out)
    import c17_replay as guard  # the repo's write guard and network block

    guard.install_guards(out)
    others = [p for p in os.popen("ps -eo pid,cmd | grep -E 'python|pytest' | grep -v grep | grep -v c20_goalie").read().splitlines()]
    print(f"other python processes at start: {len(others)}", flush=True)
    if args.salary:
        salary, entries = args.salary.resolve(), args.entries.resolve()
        clock = datetime.fromisoformat(args.clock.replace("Z", "+00:00")) if args.clock else BED_CLOCK
        source = f"real inputs {salary.name} / {entries.name} (in-sample if it is a 09-29 or 09-30 slate)"
    else:
        from pool_builder import clone_entries

        salary, entries, clock = BED / "DKSalaries.csv", out / "bed_DKEntries.csv", BED_CLOCK
        clone_entries(BED / "DKEntries.template.csv", entries, args.n_entries)
        source = f"synthetic bed (late_swap fixture, {args.n_entries} entries): decides nothing about adoption"
    n = SCENARIO_N[args.scenario_n]
    print(f"source: {source}; scenario_n {args.scenario_n} {n}", flush=True)
    results: list[dict] = []

    def go(arm, seed, tag):
        r = run_arm(arm, seed, salary, entries, clock, n, out, tag)
        results.append(r)
        print(f"{tag}: ok={r['ok']} metric={r['metric']} conflicts={r['own_goalie_conflicts']} {r['seconds']}s "
              f"{r['statuses']['RISK_BUDGET']} | {r['statuses']['OWN_GOALIE']}", flush=True)
        (out / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        return r

    if args.one:
        go(args.one, SEEDS[0], f"{args.one}-s0")
        return 0
    # noise baseline first: A0 twice on identical inputs (B98)
    a, b = go("A0", SEEDS[0], "noise-A0-first"), go("A0", SEEDS[0], "noise-A0-second")
    same = a["lineups_sha"] == b["lineups_sha"]
    print(f"NOISE: same tree twice, identical lineups: {same}; metric difference {abs(a['metric'] - b['metric']):.6f}", flush=True)
    if not args.noise_only:
        for arm in args.arms:
            for i, s in enumerate(SEEDS[:args.seeds]):
                go(arm, s, f"{arm}-s{i}")
        by = {arm: [r for r in results if r["tag"].startswith(f"{arm}-s")] for arm in args.arms}
        summary = {"source": source, "scenario_n": args.scenario_n, "noise_identical_lineups": same,
                   "noise_metric_difference": abs(a["metric"] - b["metric"]),
                   "arm_means": {k: statistics.fmean(r["metric"] for r in v) for k, v in by.items() if v}}
        if "A1" in by and "A3" in by and len(by["A1"]) == len(by["A3"]) > 1:
            summary["A3_vs_A1"] = verdict(by["A1"], by["A3"])
        for x, y in (("A1", "A0"), ("A2", "A0")):
            if x in by and y in by and by[x] and by[y]:
                summary[f"{x}_vs_{y}_mean_diff_information_only"] = statistics.fmean(
                    p["metric"] - q["metric"] for p, q in zip(by[x], by[y]))
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        print(json.dumps(summary, indent=1, default=str))
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; roots: {roots}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
