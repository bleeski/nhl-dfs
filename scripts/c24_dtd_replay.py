"""C24 before-and-after (flag 106, docs/experiments/dtd_participation_2026-10-13.md): how pricing a DTD skater's sit-out inside
the simulated game, instead of masking his score afterwards, moves his priced mean, his teammates' means and the lineups. Information
only: no threshold and no adoption decision.

Each run is the whole scenario pass, `run_slate(scenario=True)`, on a saved run's inputs at that run's own created_utc clock, with
entries cloned to 30 as C22 did, seed 20261013 and the shipping scenario counts. Web inputs are time-pinned the way
scripts/c17_replay.py pins them: every URL answers the newest body recorded at or before the clock, served into an EMPTY scratch
cache, so a tree run today and a tree run tomorrow see the same pages whatever the capture tasks add in between. The history store
is read from the repo's data/features/history (read only), whichever tree's code runs.

WHICH CODE RUNS: the tree on PYTHONPATH. This script never puts the repo's src on sys.path; it imports every nhl_dfs module before
anything else, prints where nhl_dfs came from, and refuses to run when that is not the --expect-src folder. Set PYTHONHASHSEED=0.

Data safety: the four NHL_DFS_* roots point into --out, the network is blocked and every write under the repo's data/, runs/,
outputs/ or BACKLOG.md is refused and counted (the guard of scripts/c17_replay.py). A saved run's inputs and manifest are copied into
--out first and never changed.

    python scripts/c24_dtd_replay.py run --out <scratch> --label master-1 --expect-src <tree>/src \\
        --saved-run runs/20260929-222125-classic --saved-run runs/20260930-214909-classic
    python scripts/c24_dtd_replay.py compare --out <scratch> --before master-1 --after c24-1 [--also master-2 c24-2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pkgutil
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED = 20261013  # preregistered (flag 106)
N_CLONED = 30  # as C22 (flag 95)


def preload(expect_src: Path) -> str:
    """Import every nhl_dfs module now, from the tree on PYTHONPATH, so an edit to the working tree during a long run cannot
    leak into it; refuse when nhl_dfs is not the expected tree's."""
    import importlib

    import nhl_dfs

    where = Path(nhl_dfs.__file__).resolve().parent
    if expect_src.resolve() not in where.parents:
        raise SystemExit(f"nhl_dfs imported from {where}, not from {expect_src}: set PYTHONPATH to the tree's src")
    for m in pkgutil.walk_packages(nhl_dfs.__path__, "nhl_dfs."):
        importlib.import_module(m.name)
    sys.path.append(str(REPO / "tests"))  # after nhl_dfs is loaded: pool_builder imports it from sys.modules
    sys.path.append(str(REPO / "scripts"))
    return str(where)


def set_roots(out: Path) -> dict[str, str]:
    roots = {"NHL_DFS_RUNS_ROOT": out / "roots" / "runs", "NHL_DFS_OUTPUTS_ROOT": out / "roots" / "outputs",
             "NHL_DFS_LEDGER_ROOT": out / "roots" / "ledger", "NHL_DFS_BACKLOG": out / "roots" / "BACKLOG.md"}
    for k, v in roots.items():
        os.environ[k] = str(v)
        (v if k != "NHL_DFS_BACKLOG" else v.parent).mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in roots.items()}


def saved_inputs(src: Path, out: Path) -> tuple[Path, Path, datetime, str]:
    src = Path(src).resolve()
    m = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    if m.get("parent_run_id"):
        raise SystemExit(f"{src}: a late-swap or refresh child; give the run that built the slate")
    dst = out / "saved" / src.name
    if not dst.exists():
        shutil.copytree(src / "inputs", dst / "inputs")
        shutil.copy2(src / "manifest.json", dst / "manifest.json")
    clock = datetime.fromisoformat(m["created_utc"].replace("Z", "+00:00"))
    return dst / "inputs" / "DKSalaries.csv", dst / "inputs" / "DKEntries.csv", clock, src.name


def _team(k: str) -> str:
    return k.split("|")[1]


def read_cache(run) -> dict:
    """Per person, from the scenario cache's selection stream: priced mean (the cached draws times any mask still applied at use),
    its Monte Carlo standard error, the column's hash, plus what the draws carry."""
    import numpy as np

    from nhl_dfs.build import scenario_cache as sc

    got = sc.load(run.path, run.run_id)
    n = got.n("selection")
    base = got.base("selection", n).astype(np.float64)
    meta = got.meta
    play = {k: float(v) for k, v in (meta.get("play_prob") or {}).items()}
    persons = (meta.get("participation") or {}).get("persons") or {}
    in_sim = {k: float(v["draw"]) for k, v in persons.items() if float(v.get("draw", 1.0)) < 1.0}
    games = {t: g for g in meta["games"] for t in g.split("@")}
    out = {}
    for j, k in enumerate(got.person_keys):
        col = base[:, j]
        f = 1.0 if k in in_sim else play.get(k, 1.0)  # the mask's expectation where a mask is still applied
        out[k] = {"mean_tenths": round(float(col.mean()) * f, 3), "se_tenths": round(float(col.std(ddof=1)) * f / np.sqrt(n), 3),
                  "group": (persons.get(k) or {}).get("group"), "game": games.get(_team(k)),
                  "col_sha": hashlib.sha1(np.ascontiguousarray(base[:, j]).tobytes()).hexdigest()[:16]}
    return {"n": n, "in_sim": in_sim, "masked": {k: v for k, v in play.items() if k not in in_sim}, "play": play,
            "participation_format": (meta.get("participation") or {}).get("format"), "persons": out}


def run_one(salary: Path, entries: Path, clock: datetime, out: Path, tag: str) -> dict:
    import c17_replay as guard

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
    t0 = time.perf_counter()
    res = run_slate(salary, entries, offline=False, baseline_only=False, scenario=True, seed=SEED,
                    out_root=work / "runs", outputs_root=work / "outputs", cache=cache, clock=lambda: clock)
    secs = round(time.perf_counter() - t0, 1)
    m = read_manifest(res.run)
    sec = m.get("scenario") or {}
    rec = {"tag": tag, "ok": bool(res.ok), "seconds": secs, "failed": m.get("failed"), "version": sec.get("version"),
           "participation": sec.get("participation"), "timings": sec.get("timings"),
           "statuses": {k: (m.get("statuses") or {}).get(k) for k in ("MODEL_STATUS", "FIELD_CALIBRATION", "PAYOUT_SOURCE",
                                                                       "RISK_BUDGET", "MARKET_COVERAGE")},
           "pinned_urls": len(bodies), "served": len(transport.served), "unpinned": sorted(set(transport.unpinned))[:20]}
    try:
        v = packet.RunView(res.run)
        rec["lineups"] = {str(e): list(lu) for e, lu in v.lineups.items()}
    except Exception as exc:  # a run without a published scenario version still reports its draws
        rec["lineups"], rec["lineups_error"] = {}, f"{type(exc).__name__}: {str(exc)[:120]}"
    try:
        rec["cache"] = read_cache(res.run)
    except Exception as exc:
        rec["cache"], rec["cache_error"] = None, f"{type(exc).__name__}: {str(exc)[:120]}"
    return rec


def cmd_run(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    roots = set_roots(out)  # before any nhl_dfs import: cli reads the runs root at import
    where = preload(Path(args.expect_src))
    import c17_replay as guard
    from pool_builder import clone_entries

    from nhl_dfs.data.history import store as store_mod

    store_mod.DEFAULT_ROOT = REPO / "data" / "features" / "history"  # read only; looked up at call time, so every tree reads it
    guard.install_guards(out)
    others = os.environ.get("C24_OTHER_PROCESSES", "not recorded")
    print(f"{args.label}: nhl_dfs from {where}; PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}; other processes: {others}", flush=True)
    slates = [saved_inputs(Path(src), out) for src in args.saved_run]  # every input refused up front, before any run
    results = []
    path = out / "results" / f"{args.label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for salary, entries, clock, rid in slates:
        cloned = out / "saved" / rid / f"DKEntries_cloned{N_CLONED}.csv"
        if not cloned.exists():
            clone_entries(entries, cloned, N_CLONED)
        rec = run_one(salary, cloned, clock, out, f"{args.label}-{rid}")
        rec.update({"slate": rid, "clock": clock.isoformat(), "nhl_dfs": where})
        results.append(rec)
        part = rec.get("participation") or {}
        print(f"{rid}: ok={rec['ok']} {rec['seconds']} s, version {rec['version']}, failed {rec['failed']}, priced "
              f"{len(part.get('persons') or [])}, in simulation {len(part.get('in_simulation') or [])}", flush=True)
        path.write_text(json.dumps({"label": args.label, "nhl_dfs": where, "roots": roots, "runs": results,
                                    "writes_refused": guard.WRITES, "network_blocked": guard.NET}, indent=1), encoding="utf-8")
    print(f"writes refused: {len(guard.WRITES)}; network attempts blocked: {len(guard.NET)}; wrote {path}", flush=True)
    return 1 if [w for w in guard.WRITES if "observations" not in w] else 0


def _load(out: Path, label: str) -> dict:
    return {r["slate"]: r for r in json.loads((out / "results" / f"{label}.json").read_text(encoding="utf-8"))["runs"]}


def _changed(a: dict, b: dict) -> int:
    la, lb = a.get("lineups") or {}, b.get("lineups") or {}
    return sum(1 for e in set(la) | set(lb) if sorted(la.get(e) or []) != sorted(lb.get(e) or []))


def _z(d: float, se: float) -> float | None:
    return round(d / se, 2) if se > 0 else None


def compare(before: dict, after: dict, pairs: dict, out: Path | None = None) -> dict:
    """Per slate: the covered skaters and their teammates, before (origin/master) and after (C24), on the selection stream's
    priced means. Teammates' means in a DTD skater's game come from two independent streams (the game's draws change by
    design), so each change carries the standard error of the difference and its z; |z| < 2 is labeled within noise. The
    expected lift of a linemate is small (his would-be ice time over about 11 dressed forwards, in 15% of games), so most
    single changes are within noise. Then, C24 only and paired (no cross-tree noise): in the C24 run's own cache, each
    teammate's mean when the DTD skater was not dressed against when he was."""
    import numpy as np

    res = {}
    for slate, b in before.items():
        a = after.get(slate)
        if a is None or not b.get("cache") or not a.get("cache"):
            res[slate] = {"status": "NOT RUN: a run or its cache is missing", "before_ok": b.get("ok"), "after_ok": (a or {}).get("ok")}
            continue
        bc, ac = b["cache"], a["cache"]
        covered = sorted(ac["in_sim"])
        dtd_games = {ac["persons"][k]["game"] for k in covered if k in ac["persons"]}
        paired = _paired(out, a, covered) if out is not None else {}
        rows = []
        for k in covered:
            pb, pa = bc["persons"].get(k), ac["persons"][k]
            mates = []
            for t, pt in ac["persons"].items():
                if t == k or _team(t) != _team(k) or pt["group"] not in ("F", "D") or t not in bc["persons"]:
                    continue
                x = bc["persons"][t]
                d = pt["mean_tenths"] - x["mean_tenths"]
                se = float(np.hypot(pt["se_tenths"], x["se_tenths"]))
                z = _z(d, se)
                mates.append({"person": t, "group": pt["group"], "before": x["mean_tenths"], "after": pt["mean_tenths"],
                              "change": round(d, 3), "se_change": round(se, 3), "z": z,
                              "label": "within noise" if z is None or abs(z) < 2 else "beyond 2 SE",
                              "paired_when_he_sat": (paired.get(k) or {}).get(t)})
            mates.sort(key=lambda m: -abs(m["change"]))
            d = pa["mean_tenths"] - (pb["mean_tenths"] if pb else 0.0)
            se = float(np.hypot(pa["se_tenths"], pb["se_tenths"] if pb else 0.0))
            rows.append({"person": k, "p_play": ac["in_sim"][k], "before_tenths": pb["mean_tenths"] if pb else None,
                         "before_se": pb["se_tenths"] if pb else None, "after_tenths": pa["mean_tenths"], "after_se": pa["se_tenths"],
                         "change": round(d, 3), "se_change": round(se, 3), "z": _z(d, se),
                         "teammates_top3": mates[:3], "teammates_beyond_2se": [m for m in mates if m["label"] == "beyond 2 SE"],
                         "teammates_total_change": round(sum(m["change"] for m in mates), 3),
                         "teammates_total_se": round(float(np.sqrt(sum(m["se_change"] ** 2 for m in mates))), 3)})
        teams = {}
        for t in sorted({_team(k) for k in covered}):
            mine = [k for k in covered if _team(k) == t]
            exp_gain = sum((bc["persons"][k]["mean_tenths"] / ac["in_sim"][k]) * (1.0 - ac["in_sim"][k]) for k in mine if k in bc["persons"])
            mates = [k for k, p in ac["persons"].items() if _team(k) == t and k not in mine and p["group"] in ("F", "D")
                     and k in bc["persons"]]
            obs = sum(ac["persons"][k]["mean_tenths"] - bc["persons"][k]["mean_tenths"] for k in mates)
            se = float(np.sqrt(sum(ac["persons"][k]["se_tenths"] ** 2 + bc["persons"][k]["se_tenths"] ** 2 for k in mates)))
            teams[t] = {"covered": mine, "expected_gain_tenths": round(exp_gain, 3), "observed_gain_tenths": round(obs, 3),
                        "se": round(se, 3), "z_vs_zero": _z(obs, se), "z_vs_expected": _z(obs - exp_gain, se)}
        same = [k for k, p in ac["persons"].items() if p["game"] not in dtd_games and k in bc["persons"]]
        ident = sum(1 for k in same if bc["persons"][k]["col_sha"] == ac["persons"][k]["col_sha"])
        res[slate] = {"covered_after": covered, "priced_before": sorted(bc["play"]), "masked_after": sorted(ac["masked"]),
                      "dtd": rows, "teams": teams, "no_dtd_game_columns_identical": f"{ident}/{len(same)}",
                      "entries_changed": {name: _changed(x.get(slate) or {}, y.get(slate) or {}) for name, (x, y) in pairs.items()},
                      "entries": len(b.get("lineups") or {}), "seconds": {"before": b.get("seconds"), "after": a.get("seconds")},
                      "sim_seconds_before": {k: v for k, v in (b.get("timings") or {}).items() if k.startswith("sim_") and k.endswith("_s")},
                      "sim_seconds_after": {k: v for k, v in (a.get("timings") or {}).items() if k.startswith("sim_") and k.endswith("_s")}}
    return res


def _paired(out: Path, rec: dict, covered: list[str]) -> dict:
    """C24 only, information: in the run's own saved cache (selection stream), each same-team skater's mean in the draws where
    the covered skater's participation draw said sit against those where it said play, with the SE of that difference. The
    draw is recomputed from its own stream (sim/game._plays: [seed, purpose code, chunk, game index, 9, crc32(key)], u < p),
    so the split is independent of everything else in the game; where he was not dressed anyway nothing moved, so the
    difference is diluted by his dress probability, never confounded by it."""
    import numpy as np

    import zlib

    from nhl_dfs.build import scenario_cache as sc
    from nhl_dfs.sim.game import PARTICIPATION_STREAM

    runs = sorted((out / "work" / rec["tag"] / "runs").glob("*/scenario/meta.json"))
    if not runs:
        return {}
    run_path = runs[0].parent.parent
    got = sc.load(run_path, run_path.name)
    n = got.n("selection")
    base = got.base("selection", n).astype(float)
    keys = got.person_keys
    persons = got.meta["participation"]["persons"]
    size = int(got.meta["chunk_size"])
    code = int(got.meta["purposes"]["selection"]["code"])
    out_: dict = {}
    for k in covered:
        gi = next(i for i, g in enumerate(got.meta["games"]) if _team(k) in g.split("@"))
        u = np.concatenate([np.random.Generator(np.random.PCG64(np.random.SeedSequence(
            [int(got.seed), code, c, gi, PARTICIPATION_STREAM, zlib.crc32(k.encode())]))).random(min(size, n - c * size))
            for c in range(-(-n // size))])
        sat = u >= float(persons[k]["draw"])
        out_[k] = {}
        for i, t in enumerate(keys):
            if t == k or _team(t) != _team(k) or persons.get(t, {}).get("group") not in ("F", "D"):
                continue
            x, y = base[sat, i], base[~sat, i]
            if x.size < 2 or y.size < 2:
                continue
            d = float(x.mean() - y.mean())
            se = float(np.sqrt(x.var(ddof=1) / x.size + y.var(ddof=1) / y.size))
            out_[k][t] = {"sat_mean": round(float(x.mean()), 3), "played_mean": round(float(y.mean()), 3), "change": round(d, 3),
                          "se": round(se, 3), "z": _z(d, se), "n_sat": int(x.size)}
    return out_


def cmd_compare(args) -> int:
    out = Path(args.out).resolve()
    before, after = _load(out, args.before), _load(out, args.after)
    pairs = {f"{args.before} vs {args.after}": (before, after)}
    for a, b in zip(args.also[0::2], args.also[1::2]):
        pairs[f"{a} vs {b}"] = (_load(out, a), _load(out, b))
    res = compare(before, after, pairs, out)
    (out / "results" / "compare.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"wrote {out / 'results' / 'compare.json'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--expect-src", required=True, help="the src folder of the tree on PYTHONPATH")
    r.add_argument("--saved-run", action="append", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--out", required=True)
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)
    c.add_argument("--also", nargs="*", default=[], help="more label pairs whose lineups are compared: a b [c d ...]")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
