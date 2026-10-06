"""C17 (backlog B51, B63): replay the 2026-09-30 Classic slate from what was on disk at the original run's clock, offline,
and re-grade the replayed forecast against the 09-30 standings. Writes only inside --out; reads Ben's data, never changes it.

What it does
  * Time-pinned inputs: data/raw/observations/*.jsonl records every fetch (url, time, body hash). For each URL the newest
    body fetched at or before the cut-off is served by a replay transport into an EMPTY scratch cache; a URL whose newest
    record is a failure (the DraftKings pages that answered 403) answers 403 again; a URL with no record answers 404 and is
    listed ("unpinned"). The real network is blocked by an audit hook, and every write outside --out is refused and counted.
  * Two worlds. A ("as the original run saw it"): no lobby capture and no cached contest tables in the scratch cache, so
    contests keep the family priors as in the recorded run; this world is the gate. B ("as today's code would see it"): the
    lobby capture and cached tables as of the cut-off are copied in, so C16 and C38 act; information only.
  * Arms switch parts of the field inputs (C17) off by wrapping field_inputs.collect: off, odds, roles (lines, PP, news and
    goalie confirmations), all.
  * The same seed everywhere (the run's own default, from the salary file hash).
  * Grading is the grade function settle uses (read_salary, standings.read_all, standings.join, forecast_from_run, grade),
    called read-only on the replay's scenario/fields.json against a copy of the standings. settle itself is never run (it
    writes to the ledger, BACKLOG.md and the standings folder).

    python scripts/c17_replay.py --out <scratch dir> --worlds A B --arms off odds roles all [--scenario-n small]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

RUN_ID = "20260930-210607-classic"  # the recorded run whose inputs are replayed
RECORD = "20260930-233424-classic"  # the settled run whose grades.json is the record (its forecast is the same fields.json)
CLOCK = datetime(2026, 9, 30, 21, 6, 30, tzinfo=timezone.utc)  # after the original run's own odds (21:06:22) and TOR page (21:06:13) reads
SCRATCH_OUT: Path | None = None
WRITES: list[str] = []
NET: list[str] = []


# -- guards ---------------------------------------------------------------------------------------------------------------------

def _is_under(p, roots) -> bool:
    try:
        q = Path(os.fsdecode(p)).resolve()
    except Exception:
        return False
    return any(q == r or r in q.parents for r in roots)


def install_guards(out: Path) -> None:
    """Block the network and refuse (and count) every write outside `out` and the temp folder."""
    global SCRATCH_OUT
    SCRATCH_OUT = out.resolve()
    allowed = [SCRATCH_OUT, Path(os.environ.get("TEMP", "/tmp")).resolve()]
    watched = [(REPO / "data").resolve(), (REPO / "runs").resolve(), (REPO / "outputs").resolve(), REPO / "BACKLOG.md"]

    def hook(event: str, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname"):
            NET.append(f"{event} {args[1:] if event == 'socket.connect' else args[:1]}")
            raise RuntimeError("c17_replay: the network is blocked")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                return
            writing = (mode is not None and any(c in str(mode) for c in "wax+")) or (
                mode is None and isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
            if writing and _is_under(path, watched) and not _is_under(path, allowed):
                WRITES.append(f"open {os.fsdecode(path)}")
                raise PermissionError(f"c17_replay: write outside the scratch folder refused: {os.fsdecode(path)}")
        elif event in ("os.rename", "os.remove", "os.mkdir", "os.rmdir", "shutil.rmtree"):
            for a in args[:2]:
                if isinstance(a, (str, bytes, os.PathLike)) and _is_under(a, watched) and not _is_under(a, allowed):
                    WRITES.append(f"{event} {os.fsdecode(a)}")
                    raise PermissionError(f"c17_replay: {event} outside the scratch folder refused: {os.fsdecode(a)}")

    sys.addaudithook(hook)


# -- pinned inputs --------------------------------------------------------------------------------------------------------------

def observation_rows(cutoff: datetime) -> list[dict]:
    raw = REPO / "data" / "raw"
    rows = []
    for f in sorted((raw / "observations").glob("*.jsonl")):
        if f.stem <= cutoff.strftime("%Y-%m-%d"):
            rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    cut = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    return [r for r in rows if r["fetched_at"][:19] <= cut]


def pinned_bodies(cutoff: datetime) -> tuple[dict[str, tuple[int, bytes]], dict[str, str]]:
    """url -> (status, body): the newest record at or before the cut-off. A failed record answers 403."""
    raw = REPO / "data" / "raw"
    best: dict[str, dict] = {}
    for r in observation_rows(cutoff):
        if r["url"] not in best or r["fetched_at"] >= best[r["url"]]["fetched_at"]:
            best[r["url"]] = r
    out: dict[str, tuple[int, bytes]] = {}
    where: dict[str, str] = {}
    for url, r in best.items():
        if not r.get("raw_hash") or r["status"] == "MISSING":
            out[url] = (403, b"Access Denied")
            continue
        hits = list((raw / r["source"] / r["fetched_at"][:10]).glob(r["raw_hash"] + ".*"))
        if not hits:
            out[url] = (404, b"")
            continue
        out[url] = (200, hits[0].read_bytes())
        where[url] = f"{r['source']} {r['fetched_at'][5:19]} {r['raw_hash'][:8]}"
    return out, where


class ReplayTransport:
    def __init__(self, bodies):
        self.bodies, self.unpinned, self.served = bodies, [], []

    def __call__(self, url, headers, timeout_s):
        hit = self.bodies.get(url)
        if hit is None:
            self.unpinned.append(url)
            return 404, b"", {}
        self.served.append(url)
        return hit[0], hit[1], {}


def seed_world_b(cache_root: Path, cutoff: datetime) -> list[str]:
    """Copy every lobby capture and cached contest table recorded at or before the cut-off into the scratch cache (world B)."""
    raw = REPO / "data" / "raw"
    copied = []
    for r in observation_rows(cutoff):
        if r["source"] not in ("dk_lobby", "dk_contest") or not r.get("raw_hash") or r["status"] == "MISSING":
            continue
        day = r["fetched_at"][:10]
        for f in (raw / r["source"] / day).glob(r["raw_hash"] + ".*"):
            dst = cache_root / r["source"] / day / f.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                copied.append(f"{r['source']}/{day}/{f.name[:8]}")
    return copied


# -- arms -----------------------------------------------------------------------------------------------------------------------

def arm_wrapper(arm: str, real):
    from dataclasses import replace

    def collect(*a, **k):
        got = real(*a, **k)
        if arm == "odds":
            return replace(got, roles={}, news_age_h={}, goalie_start={})
        if arm == "roles":
            return replace(got, odds=None)
        if arm == "noimpute":  # all inputs, but a team with no usable page reads as "not on PP1 or line 1" (the first design)
            return replace(got, imputed={})
        only = {"pp1": lambda g: replace(g, odds=None, news_age_h={}, goalie_start={},
                                         roles={k: {"pp1": v["pp1"], "line": None} for k, v in g.roles.items()}),
                "line1": lambda g: replace(g, odds=None, news_age_h={}, goalie_start={},
                                           roles={k: {"pp1": False, "line": v["line"]} for k, v in g.roles.items()}),
                "news": lambda g: replace(g, odds=None, roles={}, goalie_start={}),
                "goalies": lambda g: replace(g, odds=None, roles={}, news_age_h={})}
        if arm in only:  # diagnostic arms: one input alone
            return only[arm](got)
        return got

    return collect


# -- grading --------------------------------------------------------------------------------------------------------------------

def grade_run(run_path: Path, standings_dir: Path) -> dict:
    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.intake.entries import read_entries
    from nhl_dfs.learn import grade_ownership
    from nhl_dfs.learn import standings as st

    pool = read_salary(run_path / "inputs" / "DKSalaries.csv")
    cids = {str(e.contest_id) for e in read_entries(run_path / "inputs" / "DKEntries.csv").entries}
    all_st, _ = st.read_all(standings_dir)
    own = {}
    actual_stacks = {}
    for s in all_st:
        if str(s.contest_id) not in cids:
            continue
        j = st.join(s, pool)
        f = grade_ownership.forecast_from_run(SimpleNamespace(path=run_path), s.contest_id, pool)
        g = grade_ownership.grade(f, j, pool=pool).record()
        own[str(s.contest_id)] = {k: g[k] for k in ("n_roles", "mae_all", "mae_active", "mae_popular", "weighted_mae", "pearson") if k in g}
        actual_stacks[str(s.contest_id)] = _stack_freq(j, pool)
    return {"ownership": own, "actual_stack_pct": actual_stacks}


def _stack_freq(j, pool) -> dict[str, float]:
    """% of complete lineups holding at least three skaters (goalies excluded) of one team."""
    from collections import Counter

    n, hits = 0, Counter()
    for lu in j.lineups.values():
        if any(r is None for r in lu):
            continue
        n += 1
        by = Counter(pool.by_role_id[r].team for r in lu if not pool.by_role_id[r].is_goalie)
        for t, k in by.items():
            if k >= 3:
                hits[t] += 1
    return {t: round(100.0 * v / n, 1) for t, v in sorted(hits.items())} if n else {}


def summarize_run(r) -> dict:
    m = r.manifest
    out = {"run_id": r.run.run_id, "ok": bool(r.ok), "failed": m.get("failed"), "statuses": dict(r.statuses),
           "field_inputs": (m.get("provisional") or {}).get("field_inputs", {}).get("line"),
           "game_sources": (m.get("scenario") or {}).get("game_sources"),
           "games": (m.get("scenario") or {}).get("games")}
    fj = json.loads((r.run.path / "field.json").read_text(encoding="utf-8")) if (r.run.path / "field.json").exists() else {}
    out["forecast_stack_freq_pct"] = {fam: v.get("stack_freq") for fam, v in (fj.get("families") or {}).items()}
    return out


def portfolio_teams(r) -> dict:
    """Skaters per team in each delivered entry (the replayed portfolio), read from the published file."""
    import re
    from collections import Counter

    from nhl_dfs.intake.entries import read_entries
    from nhl_dfs.intake.salary import read_salary

    pool = read_salary(r.run.path / "inputs" / "DKSalaries.csv")
    res = {}
    for e in read_entries(r.public_path).entries:
        ids = [m.group(1) for c in e.cells if (m := re.search(r"\((\d+)\)\s*$", c))]
        c = Counter(pool.by_role_id[i].team for i in ids if i in pool.by_role_id and not pool.by_role_id[i].is_goalie)
        res[e.entry_id] = dict(sorted(c.items()))
    return res


# -- one arm --------------------------------------------------------------------------------------------------------------------

def run_arm(world: str, arm: str, out: Path, standings_dir: Path, scenario_n: dict | None, own_cfg_off: bool) -> dict:
    from nhl_dfs.build import run as run_mod
    from nhl_dfs.data import http as http_mod
    from nhl_dfs.models import field_inputs, ownership

    t0 = time.perf_counter()
    arm_dir = out / f"world{world}" / arm
    if arm_dir.exists():
        shutil.rmtree(arm_dir)
    cache_root = arm_dir / "raw"
    cache_root.mkdir(parents=True)
    seeded = seed_world_b(cache_root, CLOCK) if world == "B" else []
    bodies, where = pinned_bodies(CLOCK)
    transport = ReplayTransport(bodies)
    cache = http_mod.HttpCache(cache_root, config=http_mod.load_sources_config(), transport=transport, offline=False,
                               clock=lambda: CLOCK, sleep=lambda s: None)
    real_collect, real_cfg = field_inputs.collect, ownership.load_ownership_config
    field_inputs.collect = arm_wrapper(arm, real_collect)
    if arm == "off":
        ownership.load_ownership_config = lambda *a, **k: {**real_cfg(*a, **k), "field_inputs": {"enabled": False}}
    try:
        inputs = REPO / "runs" / RUN_ID / "inputs"
        r = run_mod.run_slate(inputs / "DKSalaries.csv", inputs / "DKEntries.csv", offline=False, baseline_only=False,
                              scenario=True, scenario_n=scenario_n, out_root=arm_dir / "runs", outputs_root=arm_dir / "outputs",
                              cache=cache, clock=lambda: CLOCK)
    finally:
        field_inputs.collect, ownership.load_ownership_config = real_collect, real_cfg
    rec = {"world": world, "arm": arm, "seconds": round(time.perf_counter() - t0, 1), "world_b_files_seeded": len(seeded),
           "pinned_urls": len(bodies), "served": len(transport.served), "unpinned": sorted(set(transport.unpinned))}
    rec.update(summarize_run(r))
    rec["portfolio_skaters_by_team"] = portfolio_teams(r)
    rec.update(grade_run(r.run.path, standings_dir))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="scratch folder (outside data/, runs/ and outputs/)")
    ap.add_argument("--worlds", nargs="+", default=["A"], choices=["A", "B"])
    ap.add_argument("--arms", nargs="+", default=["all"], choices=["off", "odds", "roles", "all", "noimpute", "pp1", "line1", "news", "goalies"])
    ap.add_argument("--scenario-n", default="full", choices=["full", "small"])
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if any(p in (REPO / "data", REPO / "runs", REPO / "outputs") for p in [out, *out.parents]):
        raise SystemExit("--out must be outside data/, runs/ and outputs/")
    out.mkdir(parents=True, exist_ok=True)
    install_guards(out)
    standings = out / "standings_copy"
    if not standings.exists():  # a copy: the real folder is never passed to anything that could write beside it
        shutil.copytree(REPO / "data" / "standings" / "inbox" / "2026-09-30", standings)
    small = {"design": 300, "selection": 800, "referee": 800, "field_target": 400} if args.scenario_n == "small" else None
    results = []
    for world in args.worlds:
        for arm in args.arms:
            rec = run_arm(world, arm, out, standings, small, arm == "off")
            results.append(rec)
            (out / f"result_world{world}_{arm}.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
            print(f"world {world} arm {arm}: {rec['seconds']} s, ok={rec['ok']}, unpinned={len(rec['unpinned'])}, "
                  + ", ".join(f"{c}: {v['mae_all']}" for c, v in rec["ownership"].items()))
    record = json.loads((REPO / "runs" / RECORD / "settle" / "grades.json").read_text(encoding="utf-8"))["ownership"]
    print("recorded mae_all:", {g["contest_id"]: g["mae_all"] for g in record})
    guard = {"writes_outside_scratch_refused": len(WRITES), "network_attempts_blocked": len(NET)}
    (out / "guards.json").write_text(json.dumps({**guard, "writes": WRITES, "net": NET}, indent=1), encoding="utf-8")
    print("guards:", guard)
    return 0 if not WRITES and not NET else 1


if __name__ == "__main__":
    raise SystemExit(main())
