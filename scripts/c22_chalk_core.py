"""C22 chalk core measurement (flag 95, docs/experiments/chalk_core_2026-10-13.md): how often does the ownership tie-break move a pick
off the contest's top two chalk skaters? Preregistered arms, seeds, counts and verdict; this script only runs them and reads the
`scenario.chalk_core` section every run's manifest carries (build/portfolio.core_report).

Arms per slate (every run is the whole OFFLINE scenario pass, `run_slate(scenario=True)`, in a scratch folder, because the scenario cache
keeps no candidates and no design draws):
  cloned     decides: the entries cloned to 30 (a real slate) or 40 (the bed), the shipping tree, seeds 20261013 to 20261017
  as_entered information only: the entries file as saved, seed 20261013
  mean       information only: the cloned inputs with large_gpp, small_field and wta set to policy "mean" in memory, seed 20261013
The noise baseline comes first: the cloned arm at seed 20261013 twice (B98); the first of the two is that arm's seed-20261013 run.

Set PYTHONHASHSEED=0 for the runs (PowerShell: $env:PYTHONHASHSEED="0"). Exit code 1 means a write other than the known observation-log
append was refused.

Data safety: the four NHL_DFS_* roots point into --out, the network is blocked and every write under the repo's data/, runs/, outputs/ or
BACKLOG.md is refused and counted (the guard of scripts/c17_replay.py). A saved run's inputs and manifest are copied into --out first and
never changed.

    python scripts/c22_chalk_core.py --out <scratch dir>                               # the synthetic bed: decides nothing
    python scripts/c22_chalk_core.py --out <scratch dir> --saved-run runs/20260929-222125-classic \\
        --saved-run runs/20260930-214909-classic                                      # the real slates (B126), shipping scenario counts

The bed is the committed late_swap fixture (6 teams in 3 games, 2 goalies per team) with its entries cloned to 40 in its one contest. It is
synthetic and priors-only: it says nothing about any real slate, and any synthetic slate makes the study NOT MEASURED on real caches.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
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
SEEDS = [20261013 + i for i in range(5)]  # preregistered
N_CLONED_REAL, N_CLONED_BED = 30, 40  # preregistered
BED_N = {
    "small": {"design": 300, "selection": 800, "referee": 800, "field_target": 400},
    "medium": {"design": 1000, "selection": 3000, "referee": 3000, "field_target": 1000},
}
WARRANT_RATE, WARRANT_SEEDS, REJECT_RATE = 0.10, 4, 0.03  # preregistered thresholds (judgment)
MEAN_FAMILIES = ("large_gpp", "small_field", "wta")
COUNT_KEYS = ("N", "M", "M_own", "M_dup", "D", "U")


def set_roots(out: Path) -> dict[str, str]:
    roots = {"NHL_DFS_RUNS_ROOT": out / "roots" / "runs", "NHL_DFS_OUTPUTS_ROOT": out / "roots" / "outputs",
             "NHL_DFS_LEDGER_ROOT": out / "roots" / "ledger", "NHL_DFS_BACKLOG": out / "roots" / "BACKLOG.md"}
    for k, v in roots.items():
        os.environ[k] = str(v)
        (v if k != "NHL_DFS_BACKLOG" else v.parent).mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in roots.items()}


def run_one(salary: Path, entries: Path, clock: datetime, scenario_n: dict | None, out: Path, tag: str, seed: int,
            policy_mean: bool = False) -> dict:
    """One whole offline scenario pass; returns the preregistered counts read from the manifest. policy_mean: the three
    ownership-ordered families take policy "mean" in memory (config/contest_families.yaml is never edited)."""
    import numpy  # noqa: F401  (imported before the guard's hooks matter)

    from nhl_dfs.build import packet
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.run import run_slate
    from nhl_dfs.models import contests as contests_mod

    real_loader = contests_mod.load_contest_families
    if policy_mean:
        cfg = copy.deepcopy(real_loader())
        for fam in MEAN_FAMILIES:
            cfg["families"][fam]["selection"] = "mean"
        contests_mod.load_contest_families = lambda *a, **k: copy.deepcopy(cfg)  # every reader calls through the module
    t0 = time.perf_counter()
    try:
        res = run_slate(salary, entries, offline=True, baseline_only=False, scenario=True, scenario_n=scenario_n, seed=seed,
                        out_root=out / "work" / tag / "runs", outputs_root=out / "work" / tag / "outputs", clock=lambda: clock)
    finally:
        contests_mod.load_contest_families = real_loader
    m = read_manifest(res.run)
    sc = m.get("scenario") or {}
    cc = sc.get("chalk_core") or {}
    v = packet.RunView(res.run)
    lineups = [list(lu) for lu in v.lineups.values()]
    ok = bool(res.ok and cc and sc.get("version") and sc.get("version") == v.version)
    policies = {c["family"]: c["policy"] for c in (cc.get("contests") or {}).values()}
    if policy_mean and any(policies.get(f, "mean") != "mean" for f in MEAN_FAMILIES):
        raise RuntimeError(f"{tag}: the mean patch did not reach the run (policies {policies})")
    total = cc.get("total") or {}
    return {
        "tag": tag, "seed": seed, "policy_mean": policy_mean, "ok": ok, "seconds": round(time.perf_counter() - t0, 1),
        **{k: int(total.get(k, 0)) for k in COUNT_KEYS},
        "counts_by_family": cc.get("counts"), "retention": cc.get("retention"), "cores_agree": cc.get("cores_agree"),
        "retention_ownership_families": _retention(cc, MEAN_FAMILIES),
        "core": {cid: {"family": c["family"], "policy": c["policy"], "core": [f"{p['name']} ({p['team']}) {p['mean_pts']} pts x "
                                                                             f"{p['own_pct']}%" for p in c["core"]]}
                 for cid, c in (cc.get("contests") or {}).items()},
        "contests": [{k: c.get(k) for k in ("contest_id", "name", "family", "field_size", "PAYOUT_SOURCE", "FIELD_SIZE_SOURCE")}
                     for c in sc.get("contests") or []],
        "entries": len(sc.get("entries") or []),
        "statuses": {k: m.get("statuses", {}).get(k) for k in ("MODEL_STATUS", "FIELD_CALIBRATION", "PAYOUT_SOURCE", "RISK_BUDGET")},
        "lineups_sha": hashlib.sha256(json.dumps(lineups).encode()).hexdigest()[:16],
        "failed": m.get("failed"),
    }


def _retention(cc: dict, families) -> dict:
    """The picks' core retention over the families whose policy the mean arm replaces, whatever policy they ran under."""
    by = cc.get("retention_by_family") or {}
    return {w: sum(int((by.get(f) or {}).get(w, 0)) for f in families) for w in ("both", "one", "none")}


def slate_verdict(runs: list[dict], n_seeds: int) -> dict:
    """The preregistered per-slate rule on the cloned arm, pooled over its seeds."""
    out = {"seeds": len(runs), **{k: sum(r[k] for r in runs) for k in COUNT_KEYS},
           "seeds_with_D": sum(1 for r in runs if r["D"] >= 1)}
    out["D_rate"] = out["D"] / out["N"] if out["N"] else None
    if len(runs) != n_seeds or not runs or not all(r["ok"] for r in runs):
        out["verdict"] = "NO VERDICT: a run failed"
    elif out["N"] == 0:
        out["verdict"] = "NO VERDICT: no eligible pick"
    elif n_seeds != len(SEEDS):
        out["verdict"] = f"NOT THE PREREGISTERED SAMPLE ({n_seeds} of {len(SEEDS)} seeds): no verdict"
    elif out["D_rate"] >= WARRANT_RATE and out["seeds_with_D"] >= WARRANT_SEEDS:
        out["verdict"] = "WARRANTED"
    elif out["D_rate"] < REJECT_RATE:
        out["verdict"] = "REJECT"
    else:
        out["verdict"] = "INCONCLUSIVE"
    return out


def study_verdict(slates: list[dict]) -> str:
    if any(s["synthetic"] for s in slates):
        return "NOT MEASURED on real caches (a synthetic slate is in the run; its statuses decide nothing)"
    real = [s for s in slates if not s["synthetic"]]
    if len(real) < 2:
        return "NOT MEASURED (fewer than two real slates)"
    verdicts = [s["cloned"]["verdict"] for s in real]
    if all(v == "WARRANTED" for v in verdicts):
        return "WARRANTED"
    if all(v == "REJECT" for v in verdicts):
        return "REJECT"
    return "INCONCLUSIVE"


def saved_inputs(src: Path, out: Path) -> tuple[Path, Path, datetime, str]:
    """Copy a saved run's inputs and manifest into --out; returns (salary, entries, clock, run id)."""
    src = Path(src).resolve()
    if not (src / "inputs" / "DKSalaries.csv").exists() or not (src / "inputs" / "DKEntries.csv").exists():
        raise SystemExit(f"{src}: no inputs/DKSalaries.csv and inputs/DKEntries.csv (give the run that built the slate, not a child)")
    dst = out / "saved" / src.name
    shutil.copytree(src / "inputs", dst / "inputs", dirs_exist_ok=True)
    shutil.copy2(src / "manifest.json", dst / "manifest.json")
    created = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))["created_utc"]
    return (dst / "inputs" / "DKSalaries.csv", dst / "inputs" / "DKEntries.csv",
            datetime.fromisoformat(created.replace("Z", "+00:00")), src.name)


def run_slate_arms(name: str, salary: Path, entries: Path, clock: datetime, n_clone: int, scenario_n: dict | None, out: Path,
                   seeds: list[int], info: bool, synthetic: bool, results: list[dict]) -> dict:
    from pool_builder import clone_entries

    cloned = out / f"{name}_DKEntries_cloned{n_clone}.csv"
    clone_entries(entries, cloned, n_clone)

    def go(arm: str, seed: int, ents: Path, tag: str, policy_mean: bool = False) -> dict:
        r = run_one(salary, ents, clock, scenario_n, out, f"{name}-{tag}", seed, policy_mean)
        r.update({"slate": name, "arm": arm, "synthetic": synthetic})
        results.append(r)
        print(f"{name} {tag}: ok={r['ok']} N={r['N']} M={r['M']} (own {r['M_own']}, dup {r['M_dup']}) D={r['D']} U={r['U']} "
              f"entries={r['entries']} {r['seconds']}s", flush=True)
        (out / "results.json").write_text(json.dumps({"runs": results}, indent=1, default=str), encoding="utf-8")
        return r

    first = go("cloned", seeds[0], cloned, "noise-first")
    second = go("noise", seeds[0], cloned, "noise-second")
    noise = {"identical_lineups": first["lineups_sha"] == second["lineups_sha"],
             "identical_counts": all(first[k] == second[k] for k in COUNT_KEYS)}
    print(f"{name} NOISE: identical lineups {noise['identical_lineups']}, identical counts {noise['identical_counts']}", flush=True)
    runs = [first] + [go("cloned", s, cloned, f"cloned-s{i}") for i, s in enumerate(seeds[1:], start=1)]
    slate = {"slate": name, "synthetic": synthetic, "clock": clock.isoformat(), "entries_cloned_to": n_clone,
             "scenario_n": scenario_n or "shipping (config/risk.yaml)", "noise": noise,
             "cloned": slate_verdict(runs, len(seeds)), "contests": first["contests"], "core": first["core"],
             "cores_agree": first["cores_agree"]}
    if info:
        a = go("as_entered", seeds[0], entries, "as-entered")
        b = go("mean", seeds[0], cloned, "mean-policy", policy_mean=True)
        slate["information_only"] = {
            "as_entered": {k: a[k] for k in (*COUNT_KEYS, "entries", "retention")},
            "core_retention_without_the_ownership_order": b["retention_ownership_families"],
            "core_retention_shipping_seed0": first["retention_ownership_families"],
        }
    return slate


def report_md(summary: dict) -> str:
    lines = [f"# C22 chalk core: {summary['verdict']}", "", f"Run {summary['finished_utc']}; PYTHONHASHSEED={summary['pythonhashseed']}.", ""]
    lines += ["| Slate | Synthetic | Seeds | N | M | M_own | M_dup | D | U | D/N | Seeds with D | Verdict |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for s in summary["slates"]:
        c = s["cloned"]
        rate = f"{c['D_rate']:.3f}" if c["D_rate"] is not None else "n/a"
        lines.append(f"| {s['slate']} | {s['synthetic']} | {c['seeds']} | {c['N']} | {c['M']} | {c['M_own']} | {c['M_dup']} | {c['D']} | "
                     f"{c['U']} | {rate} | {c['seeds_with_D']} | {c['verdict']} |")
    for s in summary["slates"]:
        lines += ["", f"## {s['slate']}", "", f"Noise baseline: {s['noise']}. Cores agree across eligible contests: {s['cores_agree']}.", ""]
        for cid, c in s["core"].items():
            lines.append(f"- contest {cid} ({c['family']}, {c['policy']}): " + ("; ".join(c["core"]) or "no core"))
        for c in s["contests"]:
            lines.append(f"- contest {c['contest_id']} {c.get('name')}: family {c['family']}, field {c['field_size']}, "
                         f"PAYOUT_SOURCE={c['PAYOUT_SOURCE']}, FIELD_SIZE_SOURCE={c['FIELD_SIZE_SOURCE']}")
        if "information_only" in s:
            lines.append(f"- information only: {json.dumps(s['information_only'])}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="scratch folder (all writes go here)")
    ap.add_argument("--saved-run", type=Path, action="append", default=[],
                    help="a real run folder (runs/<id>) whose inputs are replayed; repeatable. Default: the synthetic bed")
    ap.add_argument("--small", action="store_true", help="the bed at the small scenario counts (tests); never with --saved-run")
    ap.add_argument("--seeds", type=int, default=5, help="seeds of the deciding arm; 5 is the preregistered sample")
    ap.add_argument("--no-info", action="store_true", help="skip the two information-only arms")
    args = ap.parse_args(argv)
    if args.small and args.saved_run:
        ap.error("--small is for the bed; real slates run the shipping scenario counts")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = set_roots(out)
    import c17_replay as guard  # the repo's write guard and network block

    guard.install_guards(out)
    listing = os.popen("tasklist" if os.name == "nt" else "ps -eo pid,cmd").read().splitlines()
    others = [x.strip() for x in listing if ("python" in x.lower() or "pytest" in x.lower()) and "c22_chalk" not in x]
    print(f"other python processes at start: {len(others)} {others[:3]}; PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}", flush=True)
    seeds = SEEDS[:args.seeds]
    results: list[dict] = []
    slates = []
    if args.saved_run:
        for src in args.saved_run:
            salary, entries, clock, rid = saved_inputs(src, out)
            print(f"{rid}: real inputs, clock {clock.isoformat()}, entries cloned to {N_CLONED_REAL} (IN-SAMPLE: the 09-29 and 09-30 "
                  "slates sit inside the standings that suggested the question)", flush=True)
            slates.append(run_slate_arms(rid, salary, entries, clock, N_CLONED_REAL, None, out, seeds, not args.no_info, False, results))
    else:
        n = BED_N["small" if args.small else "medium"]
        print(f"synthetic bed (late_swap fixture, {N_CLONED_BED} entries), scenario_n {n}: decides nothing", flush=True)
        slates.append(run_slate_arms("bed", BED / "DKSalaries.csv", BED / "DKEntries.template.csv", BED_CLOCK, N_CLONED_BED, n, out,
                                     seeds, not args.no_info, True, results))
    summary = {"verdict": study_verdict(slates), "slates": slates, "seeds": seeds,
               "thresholds": {"warrant_rate": WARRANT_RATE, "warrant_seeds": WARRANT_SEEDS, "reject_rate": REJECT_RATE},
               "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
               "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (out / "results.json").write_text(json.dumps({"summary": summary, "runs": results}, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(report_md(summary), encoding="utf-8")
    print(f"VERDICT: {summary['verdict']}", flush=True)
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; roots: {roots}", flush=True)
    (out / "guard.json").write_text(json.dumps({"writes_refused": guard.WRITES, "network_blocked": guard.NET,
                                                "other_python_at_start": others}), encoding="utf-8")
    # the only write the offline run tries outside --out is the observation log (the known gap, refused and counted)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


if __name__ == "__main__":
    raise SystemExit(main())
