"""`settle`: one run against its contests' standings (card C11; plan sections 6 and 12). Changes no parameter.

Order: hash the frozen files; decide whether the run's forecast is PRE_LOCK (created before the slate's first
game) or POST_LOCK (then it is graded as a plumbing check only and never counted as evidence); read and join the
standings; settle the money (learn.ledger) and write the ledger row; grade ownership and forecasts from the frozen
files only; update the evidence index and report the gate tier (learn.gates, nothing tuned); add backlog rows for
deterministic defects and missing frozen artifacts; write runs/<id>/settle/grades.json and the Settlement
section of RUN_NOTES.md; hash the frozen files again (FREEZE_CHECK). A contest without a known payout gets a
pre-filled winnings template for Ben (DraftKings "My Contests").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.learn import backlog as backlog_mod
from nhl_dfs.learn import grade_forecasts, grade_ownership
from nhl_dfs.learn import ledger as ledger_mod
from nhl_dfs.learn import notes as notes_mod
from nhl_dfs.learn import standings as standings_mod

REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN = ("manifest.json", "field.json", "inputs", "versions", "scenario", "contests")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def frozen_hashes(run) -> dict[str, str]:
    out = {}
    for name in FROZEN:
        p = run.path / name
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.exists() else []
        for f in files:
            out[f.relative_to(run.path).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def forecast_status(run, m: dict, pool) -> tuple[str, str]:
    """PRE_LOCK only when the manifest's creation time AND the forecast files' write times (the file system's
    clock, which a rehearsal clock cannot set back) are all before the slate's first game."""
    first = min(g.start_utc for g in pool.games.values())
    created = datetime.fromisoformat(m["created_utc"].replace("Z", "+00:00"))
    files = [run.path / "field.json", run.path / "scenario" / "meta.json", run.path / "scenario" / "fields.json"]
    written = [datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) for p in files if p.exists()]
    last = max(written) if written else None
    if created < first and (last is None or last < first):
        return "PRE_LOCK", (f"run created {_iso(created)}, forecast files written by {_iso(last) if last else 'n/a'}, "
                            f"first game {_iso(first)}")
    return "POST_LOCK", (f"run created {_iso(created)}, forecast files written {_iso(last) if last else 'n/a'}, first game "
                         f"{_iso(first)}: not a pre-lock forecast; graded as a plumbing check, never counted as evidence")


# -- evidence index and gates ------------------------------------------------------------------------------------------

def _index_path(root: Path) -> Path:
    return Path(root) / "graded.json"


def update_index(root: Path, entry: dict) -> dict:
    p = _index_path(root)
    idx = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    idx[entry["run_id"]] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
    return idx


def evidence_counts(idx: dict, mode: str):
    """learn.gates.EvidenceCounts for one mode from settled PRE_LOCK runs only. Thousands of labels from one slate
    are one group; the Showdown contests of one game are one group; a (date, game, person) outcome counts once
    across runs and modes (plan section 12)."""
    from nhl_dfs.learn.gates import EvidenceCounts

    frozen = [e for e in idx.values() if e.get("frozen")]
    mine = [e for e in frozen if e["mode"] == mode]
    groups: dict[str, set] = {}
    all_groups = set()
    labels: dict[str, tuple[str, int]] = {}  # B31: a contest's labels count once (newest settle), not once per run
    for e in mine:
        gs = {e["slate_id"]} if mode == Mode.CLASSIC.value else {f"{e['slate_date']}|{g}" for g in e["games"]}
        all_groups |= gs
        for cid, c in e["contests"].items():
            groups.setdefault(c["family"], set()).update(gs)
            when, key = str(e.get("settled_utc") or ""), f"{e['slate_date']}|{cid}"
            if key not in labels or when >= labels[key][0]:
                labels[key] = (when, int(c["labels"]))
    outcomes = {(p[0], p[1], p[2], p[3]) for e in frozen for p in e.get("played", [])}
    by_date: dict[str, bool] = {}
    for e in frozen:
        by_date[e["slate_date"]] = by_date.get(e["slate_date"], True) and bool(e.get("complete_payout"))
    return EvidenceCounts(
        slate_dates=len({e["slate_date"] for e in mine}),
        skater_games=sum(1 for o in outcomes if o[3] != "G"),
        goalie_starts=sum(1 for o in outcomes if o[3] == "G"),
        slate_groups=len(all_groups),
        ownership_labels=sum(n for _, n in labels.values()),
        groups_by_family={k: len(v) for k, v in groups.items()},
        complete_payout_dates=sum(1 for v in by_date.values() if v))


def gate_report(idx: dict, mode: str) -> dict:
    from nhl_dfs.learn import gates

    counts = evidence_counts(idx, mode)
    rep = gates.tier(counts, Mode(mode))
    return {"tier": rep.tier, "met": rep.met, "allowed": rep.allowed,
            "shortfalls": {k: v for k, v in rep.shortfalls.items() if v}, "counts": asdict(counts)}


# -- the command ---------------------------------------------------------------------------------------------------------

def _backlog_rows(rec: dict, sl, joins, fg) -> list[backlog_mod.Row]:
    ev = f"{sl.slate_date} settle of {sl.run_id}"
    rows = []
    if fg is not None and isinstance(fg.bonus_rate_calibration, str) and fg.bonus_rate_calibration.startswith("NOT_AVAILABLE"):
        rows.append(backlog_mod.Row(
            "cache_event_counts_missing", ev,
            "Gap: the C8 scenario cache stores DK points per draw, not event counts, so the plan section 12 bonus "
            "calibration (5+ SOG, 3+ blocks, 3+ points, 35+ saves) cannot be graded from a frozen forecast",
            "Bonus-rate calibration (C11 grade_forecasts)", "Store per-draw bonus indicators (or event counts) for "
            "the selection draws in scenario/ at build; grade them at settle", "High: read the code",
            "A settle of a new run reports bonus-rate calibration per bonus from its own frozen files", "Medium"))
    if fg is not None and fg.goalie_decisions.get("teams") and str(fg.goalie_decisions.get("status", "")).startswith("implied"):
        rows.append(backlog_mod.Row(
            "frozen_goalie_p_start_missing", ev,
            "Gap: no goalie start probability is saved at build; settle uses the share of nonzero goalie draws as "
            "the implied probability", "Goalie decision accuracy and Brier score",
            "Write each goalie's start probability (and its source: rotation, Daily Faceoff, override) into "
            "scenario/meta.json at build", "High: read the code",
            "Settle grades the saved start probabilities and they match the draws' nonzero share within Monte Carlo "
            "error", "Low"))
    if fg is not None and fg.skater_conditioning.startswith("UNCONDITIONAL"):
        rows.append(backlog_mod.Row(
            "cache_dressed_indicator_missing", ev,
            "Gap: the scenario cache stores points only, so a skater's not-dressed zeros (the simulator's Bernoulli "
            "p_dress) cannot be told from dressed zero-point games; settle grades skaters unconditional on dressing",
            "Forecast bias, CRPS and coverage for skaters who played",
            "Cache a per-draw dressed indicator (one bit per person and draw) with the selection draws; grade skaters "
            "on their dressed draws", "High: read the code (sim/game.py step 3)",
            "Settle reports skater grades conditional on dressing from the run's own frozen files", "Medium"))
    missing = sorted({u.split(" (")[0] for j in joins.values() for u in j.unmatched})
    if missing:
        rows.append(backlog_mod.Row(
            "salary_missing_players", ev + f" ({', '.join(missing[:4])})",
            "Deterministic observation: players in the final standings are absent from the run's salary file "
            "(DraftKings added them after the download); their ownership is outside the pool, and they could "
            "not be rostered (see B1)", "Pool completeness; ownership mass outside the pool",
            "Re-download the salary file shortly before lock and accept added rows (B1); report added players",
            "High: observed", "A settle reports no standings player outside the pool on a slate re-downloaded "
            "within an hour of lock", "Medium"))
    if any(j.conflicts for j in joins.values()):
        rows.append(backlog_mod.Row(
            "standings_join_conflicts", ev, "Deterministic defect: CONFLICTED standings names (same name and roster "
            "token for two pool persons) cannot be graded", "Ownership labels", "Resolve with the lineup rows' team "
            "context or the identity crosswalk", "High: observed", "Zero CONFLICTED names on the settled slate", "Low"))
    if any(e.payout_source == ledger_mod.UNKNOWN for e in sl.entries):
        rows.append(backlog_mod.Row("prize_table_missing", ev, "", "", "", "", "", ""))
    return rows


def run(run_id: str, standings_path, *, runs_root, prize_paths=(), winnings_path=None, boxscores: bool = True,
        offline: bool = False, ledger_root=None, backlog_path=None, cache=None, now: datetime | None = None,
        accepted: dict | None = None) -> dict:
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.state import open_run
    from nhl_dfs.intake.entries import read_entries
    from nhl_dfs.intake.salary import read_salary

    now = now or datetime.now(timezone.utc)
    ledger_root = Path(ledger_root) if ledger_root is not None else ledger_mod.default_root()
    backlog_path = Path(backlog_path) if backlog_path is not None else backlog_mod.default_path()
    run = open_run(Path(runs_root), run_id)
    m = read_manifest(run)
    pool = read_salary(run.inputs / "DKSalaries.csv")
    own_status, own_detail = forecast_status(run, m, pool)
    frun, status, detail = forecast_run(Path(runs_root), run, pool)
    if frun.run_id != run.run_id:  # B30: this run has no pre-lock forecast of its own
        detail = (f"graded from run {frun.run_id}, its nearest pre-lock ancestor ({detail}); this run itself: {own_status}; "
                  "money and entered lineups from this run")

    def hashes() -> dict[str, str]:
        out = frozen_hashes(run)
        if frun.run_id != run.run_id:
            out.update({f"{frun.run_id}/{k}": v for k, v in frozen_hashes(frun).items()})
        return out

    before = hashes()
    notes: list[str] = []
    all_st, st_notes = standings_mod.read_all(standings_path)
    notes += st_notes
    own_cids = {str(e.contest_id) for e in read_entries(run.inputs / "DKEntries.csv").entries}
    mine = [s for s in all_st if str(s.contest_id) in own_cids]
    if not mine:
        raise ValueError(f"no standings for this run's contests ({', '.join(sorted(own_cids))}) under {standings_path}")
    joins = {s.contest_id: standings_mod.join(s, pool) for s in mine}
    for s in mine:
        for n in joins[s.contest_id].notes:
            notes.append(f"contest {s.contest_id}: {n}")
    # money
    sdir = Path(standings_path) if Path(standings_path).is_dir() else Path(standings_path).parent
    saved = run.path / "settle" / "prize_tables"
    tables, t_notes = ledger_mod.prize_tables([s.contest_id for s in mine], entries_n={s.contest_id: len(s.entries) for s in mine},
                                              search_dirs=[sdir], paths=prize_paths, saved_dir=saved,
                                              run_dirs=run_contest_dirs(Path(runs_root), run))
    notes += t_notes
    ledger_mod.save_tables(tables, saved)
    wpath = Path(winnings_path) if winnings_path else sdir / "winnings.csv"
    reported = ledger_mod.read_winnings(wpath)
    final = final_lineups(run, pool)
    sl = ledger_mod.settle(run, mine, tables, reported=reported, pool=pool, final_lineups=final, now=now)
    notes += sl.notes
    for rid, n in sorted(ledger_mod.replaced_by(sl, ledger_root).items()):
        notes.append(f"ledger: this settle replaces {n} row(s) of run {rid} for the same DraftKings entries (each entry is "
                     "booked once, from the newest settle)")
    lpath = ledger_mod.append(sl, ledger_root)
    dd = ledger_mod.drawdown(ledger_root)
    template = None
    unknown = [e for e in sl.entries if e.payout_source == ledger_mod.UNKNOWN]
    if unknown:
        in_inbox = (REPO_ROOT / "data" / "standings").resolve() in sdir.resolve().parents
        template = (sdir / "winnings.csv") if in_inbox else ledger_root / "winnings_needed" / f"{run.run_id}.csv"
        wrote = ledger_mod.write_winnings_template(template, [
            {"contest_id": e.contest_id, "contest_name": e.contest_name, "entry_id": e.entry_id, "rank": e.rank or "",
             "fee": f"{e.fee_cents / 100:.2f}", "winnings_usd": "", "source": "DraftKings My Contests", "noted_utc": ""}
            for e in unknown])
        notes.append(f"winnings template {template}: {wrote} row(s) added (fill winnings_usd from DraftKings My "
                     "Contests, save, and settle again)")
    # grades (frozen files only)
    own_grades = []
    for s in mine:
        f = grade_ownership.forecast_from_run(frun, s.contest_id, pool)
        if f is None:
            notes.append(f"contest {s.contest_id}: the run saved no field forecast, ownership not graded")
            continue
        own_grades.append(grade_ownership.grade(f, joins[s.contest_id], pool=pool).record())
    pts: dict[str, int] = {}
    for j in joins.values():
        for k, v in j.points_tenths.items():
            pts.setdefault(k, v)
    boxes, b_notes = (grade_forecasts.fetch_boxscores(pool, cache=cache, offline=offline) if boxscores else ([], []))
    notes += b_notes
    fg = grade_forecasts.grade(frun, boxes or None, actual_points=pts, pool=pool, accepted=accepted)
    # evidence and gates (PRE_LOCK forecasts only)
    entry = {"run_id": run.run_id, "mode": pool.mode.value, "slate_date": sl.slate_date, "slate_id": m["slate_id"],
             "games": sorted(pool.games), "frozen": status == "PRE_LOCK", "complete_payout": sl.complete,
             "forecast_run_id": frun.run_id,
             "contests": {g["contest_id"]: {"family": g["family"], "labels": g["n_roles"]} for g in own_grades},
             "played": [[sl.slate_date, *p] for p in (fg.played if fg else [])], "settled_utc": _iso(now)}
    idx = update_index(ledger_root, entry)
    gates = {pool.mode.value: gate_report(idx, pool.mode.value)}
    rec = {"run_id": run.run_id, "mode": pool.mode.value, "slate_id": m["slate_id"], "settled_utc": _iso(now),
           "forecast": {"status": status, "detail": detail, "run_id": frun.run_id, "own_status": own_status,
                        "own_detail": own_detail},
           "standings": {"files": len(mine), "contests": [s.contest_id for s in mine],
                         "sources": {str(s.contest_id): s.source for s in mine}},
           "ledger": sl.record(), "drawdown": dd, "ledger_path": str(lpath),
           "ownership": own_grades, "forecasts": fg.record() if fg else None, "gates": gates,
           "prize_tables": {str(k): {"source": t.source, "final": t.final, "note": t.final_note} for k, t in tables.items()},
           "winnings_template": str(template) if template else None, "notes": notes}
    srcs = sorted({e.payout_source for e in sl.entries})
    rec["statuses"] = {"PAYOUT_SOURCE": "/".join(srcs), "OUTCOME_CALIBRATION": "UNVALIDATED",
                       "FIELD_CALIBRATION": "PRIOR"}
    rec["backlog"] = []
    for r in _backlog_rows(rec, sl, joins, fg):
        rid, added = backlog_mod.add(r, backlog_path)
        rec["backlog"].append({"key": r.key, "id": rid, "added": added})
    rec["recommendation"] = (f"keep; no parameter change (evidence tier {gates[pool.mode.value]['tier']}: the gate allows "
                             "no fit or promotion)")
    d = run.path / "settle"
    d.mkdir(exist_ok=True)
    text = notes_mod.render(rec)
    (d / "grades.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    notes_mod.write(run, rec, text)
    after = hashes()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    rec["freeze_check"] = {"ok": not changed, "files": len(before), "changed": changed}
    (d / "grades.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    return rec


def ancestry(runs_root: Path, run, max_depth: int = 8) -> list[Path]:
    """The run's folder, then each run's on its parent_run_id chain (existing folders only; one walk shared by the
    contest-detail lookup, B24, and the forecast choice, B30)."""
    out, path = [], run.path
    for _ in range(max_depth):
        if not path.is_dir():
            break
        out.append(path)
        try:
            parent = json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("parent_run_id")
        except (OSError, ValueError):
            parent = None
        if not parent:
            break
        path = Path(runs_root) / parent
    return out


def run_contest_dirs(runs_root: Path, run, max_depth: int = 8) -> list[Path]:
    """runs/<id>/contests of the run, then of each run on its parent_run_id chain (a refresh or late swap child
    uses its parent's pre-lock contest details, backlog B24)."""
    return [p / "contests" for p in ancestry(runs_root, run, max_depth)]


def forecast_run(runs_root: Path, run, pool):
    """B30: the run whose frozen forecast settle grades: the nearest run on the chain (the run itself first) that has a
    scenario cache written before the first game (PRE_LOCK). A late-swap child (created after first lock, no cache)
    grades against its pre-lock ancestor; with no pre-lock run on the chain, the run itself, as before.
    Returns (forecast run, its status, its detail)."""
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.state import open_run

    for p in ancestry(runs_root, run):
        if not (p / "scenario" / "meta.json").exists():
            continue
        cand = run if p == run.path else open_run(Path(runs_root), p.name)
        try:
            status, detail = forecast_status(cand, read_manifest(cand), pool)
        except (OSError, ValueError, KeyError):
            continue
        if status == "PRE_LOCK":
            return cand, status, detail
    status, detail = forecast_status(run, read_manifest(run), pool)
    return run, status, detail


def final_lineups(run, pool) -> dict[str, list]:
    """entry_id -> role ids of the run's final (current) version, for the entered-lineup check."""
    from nhl_dfs.build.goalies import lineups_of

    v = run.current_version()
    return lineups_of(run.version_file(v), pool) if v else {}
