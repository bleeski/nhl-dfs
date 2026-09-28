"""Late swap from a current DK entries export (card C2c; plan section 11, "Fast mode").

The parent is the export Ben supplies (never the last generated file). Each late swap is a new
child run: inputs/ holds the salary file used and that export; the manifest names the parent
run. The last delivered version is found and diffed against the export (manual changes are
recorded), but it is never the base.

Pins: LOCKED and EDIT_STOP cells (build.locks). Exclusions for free cells: participation OUT
(salary Status column, draftables) and DK-disabled rows, players who may not be added (game
started or inside the edit-stop window, unswappable), and persons at their exposure cap.
The re-solve is the full-lineup MILP with pinned rows fixed, so the team rules span pinned and
free slots.

fast=True (the default near lock): only entries that need repair are touched (a blank open
cell, an excluded player in an open cell, or an illegal lineup), with as few changed cells as
possible: current open occupants get a keep bonus larger than any objective difference, so a
forced goalie swap that breaks the cap becomes a two-player repair, and Ben's manual edits stay.
fast=False: every entry with an open cell is re-optimized for the baseline objective with
residual exposure caps and pairwise distinctness (relaxed in the plan's order, recorded).

Only changed cells are rewritten (other bytes, including bare-ID cells, stay identical). The
lock state is recomputed with a fresh clock right before writing; if any change crossed a lock
boundary during the computation, nothing is published and the predecessor stands.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nhl_dfs.build import feasible, milp
from nhl_dfs.build import locks as locks_mod
from nhl_dfs.build.assign import Assignment, Caps, load_caps
from nhl_dfs.build.manifest import exposures_top20, read_manifest, write_manifest
from nhl_dfs.build.notes import chicago, write_run_notes
from nhl_dfs.build.run import (
    RunResult,
    load_runtime_config,
    salary_statuses,
    slate_id_for,
)
from nhl_dfs.build.state import LockTimeout, PublishRefused, RunDir, new_run, open_run, publish, sha256
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, check_lineup, lineup_key, slot_accepts
from nhl_dfs.contracts.statuses import (
    DeliveryStatus,
    Eligibility,
    FeasibleStatus,
    FieldCalibration,
    FileStatus,
    ModelStatus,
    NewsState,
    ObsStatus,
    OutcomeCalibration,
    Participation,
    PayoutSource,
    SearchStatus,
)
from nhl_dfs.data.sources import dk_public
from nhl_dfs.export.writer import field_spans, format_cell
from nhl_dfs.intake.entries import EntriesFile, cell_role_id, physical_lines, read_entries, template_permutation
from nhl_dfs.intake.salary import SalaryPool, read_salary
from nhl_dfs.models.priors import prior_objective, prior_table
from nhl_dfs.referee.check_file import check_file

KEEP_BONUS = 1000.0  # points; larger than any lineup's objective, so fewer changes always win


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- file helpers --------------------------------------------------------------------------

def splice_cells(entries: EntriesFile, pool: SalaryPool, changes: dict[str, dict[int, str]], out_path) -> bytes:
    """Rewrite only the changed roster cells (entry id -> canonical slot -> role id). Zero changes
    returns the input bytes exactly (BOM and line endings included)."""
    perm = template_permutation(entries.roster_labels, entries.mode)
    col_of = {k: entries.roster_cols[col] for col, k in enumerate(perm)}
    bom, lines = physical_lines(entries.raw)
    by_line = {e.line_no: e for e in entries.entries}
    out: list[bytes] = []
    for idx, line in enumerate(lines):
        e = by_line.get(idx)
        todo = changes.get(e.entry_id) if e is not None else None
        if not todo:
            out.append(line)
            continue
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        spans = field_spans(body)
        edits = sorted((spans[col_of[k]], format_cell(pool.by_role_id[rid]).encode("utf-8")) for k, rid in todo.items())
        pieces, cursor = [], 0
        for (start, end), text in edits:
            pieces += [body[cursor:start], text]
            cursor = end
        pieces.append(body[cursor:])
        out.append(b"".join(pieces) + ending)
    data = (b"\xef\xbb\xbf" if bom else b"") + b"".join(out)
    Path(out_path).write_bytes(data)
    return data


def _canonical(entries: EntriesFile, e) -> list[str | None]:
    """Canonical role ids for an entry; unreadable cells become None (they are pinned anyway)."""
    perm = template_permutation(entries.roster_labels, entries.mode)
    out: list[str | None] = [None] * len(perm)
    for col, k in enumerate(perm):
        try:
            out[k] = cell_role_id(e.cells[col])
        except ValueError:
            out[k] = None
    return out


def last_delivered(runs_root: Path, outputs_root: Path, slate_id: str, parent: RunDir) -> Path | None:
    stamp = outputs_root / slate_id / "published.json"
    if stamp.exists():
        s = json.loads(stamp.read_text(encoding="utf-8"))
        p = runs_root / s["run_id"] / "versions" / f"v{s['version']}" / "DKEntries.csv"
        if p.exists():
            return p
    n = parent.current_version()
    return parent.version_file(n) if n else None


def manual_changes(current: EntriesFile, delivered_path: Path | None) -> list[dict[str, Any]]:
    """Cells in the current export that differ from the last delivered version."""
    if delivered_path is None:
        return []
    delivered = read_entries(delivered_path)
    slots = CLASSIC_SLOTS if current.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    before = {e.entry_id: _canonical(delivered, e) for e in delivered.entries}
    out = []
    for e in current.entries:
        now = _canonical(current, e)
        was = before.get(e.entry_id)
        if was is None:
            out.append({"entry_id": e.entry_id, "slot": None, "from": None, "to": None, "note": "not in the delivered file"})
            continue
        for k, (a, b) in enumerate(zip(was, now)):
            if a != b:
                out.append({"entry_id": e.entry_id, "slot": f"{slots[k]}#{k}", "from": a, "to": b})
    return out


def fetch_draftables_bounded(entries: EntriesFile, cache, budget_s: float):
    """(Draftables | None, summary). Network runs in a worker thread joined against the budget."""
    box: dict[str, Any] = {}

    def worker():
        try:
            detail = dk_public.contest_detail(int(entries.entries[0].contest_id), cache=cache)
            box["d"] = dk_public.draftables(detail.draft_group_id, cache=cache)
        except BaseException as exc:
            box["error"] = exc

    th = threading.Thread(target=worker, name="nhl-late-swap-draftables", daemon=True)
    th.start()
    th.join(timeout=budget_s)
    if th.is_alive():
        return None, f"draftables exceeded {budget_s:.0f}s; locks use Game Info only"
    if "error" in box:
        exc = box["error"]
        return None, (f"draftables unavailable ({type(exc).__name__}: {str(exc)[:100]}); locks use Game Info only, "
                      "so early starts and unswappable players cannot be seen")
    return box["d"], f"draftables fetched ({len(box['d'].rows)} rows)"


# -- re-solve ------------------------------------------------------------------------------

@dataclass
class EntryOutcome:
    entry_id: str
    action: str  # "kept" | "repaired" | "reoptimized" | "unrepairable" | "unreadable"
    before: list[str | None]
    after: list[str | None]
    detail: str = ""
    relaxations: tuple[str, ...] = ()
    route: str = ""


def _place(before: list[str | None], pins: dict[int, str], lineup: list[str], pool: SalaryPool, mode: Mode) -> list[str]:
    """Keep each kept player in his original cell where legal; fill the rest; else solver order."""
    slots = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    chosen = set(lineup) - set(pins.values())
    out: list[str | None] = [None] * len(slots)
    for k, rid in pins.items():
        out[k] = rid
    for k, rid in enumerate(before):
        if out[k] is None and rid in chosen:
            out[k] = rid
            chosen.discard(rid)
    free = [k for k in range(len(slots)) if out[k] is None]
    rows = sorted(chosen)

    def fill(i: int) -> bool:
        if i == len(rows):
            return True
        for k in free:
            if out[k] is None and slot_accepts(slots[k], pool.by_role_id[rows[i]], mode):
                out[k] = rows[i]
                if fill(i + 1):
                    return True
                out[k] = None
        return False

    if len(rows) == len(free) and fill(0) and check_lineup([pool.by_role_id[r] for r in out], mode).ok:
        return out  # type: ignore[return-value]
    return list(lineup)


def _solve_entry(pool, mode, objective, before, pins, *, fast, exclude_rows, capped_rows, overlaps, time_limit_s):
    """(lineup | None, route, relaxations, detail)."""
    keep = {rid for k, rid in enumerate(before) if rid is not None and k not in pins and rid not in exclude_rows}
    obj = dict(objective)
    if fast:
        for rid in keep:
            obj[rid] = obj.get(rid, 0.0) + KEEP_BONUS
        capped_rows = capped_rows - keep  # a current occupant is never forced out by a cap
    ladder = [((), capped_rows, overlaps), (("OVERLAP",), capped_rows, ()), (("OVERLAP", "EXPOSURE"), frozenset(), ())]
    if not overlaps:
        ladder = [ladder[0], (("EXPOSURE",), frozenset(), ())]
    if milp.solver_available():
        last = ""
        for relax, caps_ex, ovl in ladder:
            res = milp.solve_lineup(pool, mode, obj, exclude=frozenset(exclude_rows | caps_ex), locked=pins,
                                    max_overlap_with=ovl, time_limit_s=time_limit_s)
            if res.lineup is not None:
                return res.lineup, "milp", relax, ""
            last = res.detail or res.status.value
            if res.status is not SearchStatus.INFEASIBLE:
                break  # ERROR: go to the feasibility fallback
        else:
            return None, "milp", (), f"no legal repair with the pinned cells ({last})"
    # Fallback: pin the good open occupants too, then only the locks.
    for extra in ({k: rid for k, rid in enumerate(before) if rid in keep}, {}):
        locked = {**pins, **extra}
        r = feasible.find_one(pool, mode, exclude=frozenset(exclude_rows), locked=locked, budget_s=2.0)
        if r.status is FeasibleStatus.FOUND:
            return r.lineup, "feasible", ("EXPOSURE",) if capped_rows else (), ""
    return None, "feasible", (), f"feasibility search found no legal repair ({r.status.value}: {r.detail})"


# -- the core ------------------------------------------------------------------------------

def swap_core(
    parent: RunDir,
    current_path,
    *,
    kind: str,
    offline: bool,
    fast: bool,
    assumed_parent: bool,
    runs_root: Path,
    outputs_root: Path,
    clock: Callable[[], datetime],
    cache=None,
    salary_path=None,
    runtime: dict | None = None,
    caps: Caps | None = None,
    rehearsal: datetime | None = None,
) -> RunResult:
    t_start = time.perf_counter()
    runtime = runtime or load_runtime_config()
    caps = caps or load_caps()
    parent_m = read_manifest(parent)
    slate_id = parent_m["slate_id"]
    salary_file = Path(salary_path) if salary_path else parent.inputs / "DKSalaries.csv"
    pool = read_salary(salary_file)
    current = read_entries(current_path)
    # A rehearsal clock drives locks only; the run id and created time are always real.
    real_clock = (lambda: datetime.now(timezone.utc)) if rehearsal is not None else clock
    run = new_run(runs_root, mode=pool.mode, clock=real_clock)
    (run.inputs / "DKSalaries.csv").write_bytes(pool.raw)
    (run.inputs / "DKEntries.csv").write_bytes(current.raw)
    messages: list[str] = []
    timings: dict[str, float] = {}
    m: dict[str, Any] = {
        "run_id": run.run_id, "created_utc": _utc(real_clock()), "mode": pool.mode.value, "slate_id": slate_id,
        "kind": kind, "parent_run_id": parent.run_id, "fast": fast, "assumed_parent": assumed_parent,
        "parent_file": {"path": str(current_path), "sha256": current.sha256},
        "salary_sha256": pool.sha256, "entries_sha256": current.sha256, "export_sha256": None,
        "statuses": {
            "FILE_VALID": FileStatus.FALSE.value, "NEWS_STATE": NewsState.NONE.value,
            "MODEL_STATUS": ModelStatus.PRIOR.value, "SEARCH_STATUS": SearchStatus.ERROR.value,
            "DELIVERY_STATUS": DeliveryStatus.FAILED.value, "PAYOUT_SOURCE": PayoutSource.PRIOR.value,
            "OUTCOME_CALIBRATION": OutcomeCalibration.UNVALIDATED.value, "FIELD_CALIBRATION": FieldCalibration.PRIOR.value,
        },
        "entry_count": len(current.entries), "entry_fees": [e.fee for e in current.entries],
        "exposures_top20": [], "relaxations": [], "phase_timings": timings, "versions": [],
        "public_path": str(outputs_root / slate_id / "DKEntries.csv"),
        "news": {}, "messages": messages, "worked": [], "failed": [], "search_route": "milp" if milp.solver_available() else "feasible",
    }
    if rehearsal is not None:
        m["rehearsal_clock"] = _utc(rehearsal)
        messages.append(f"REHEARSAL CLOCK: locks computed as of {chicago(_utc(rehearsal))}, not the real time")
    if assumed_parent:
        messages.append(f"assumed parent (no current DK export supplied): {current_path}; this is NOT the live account")

    def finish() -> RunResult:
        timings["total_s"] = round(time.perf_counter() - t_start, 3)
        mp, np_ = write_manifest(run, m), write_run_notes(run, m)
        public = Path(m["public_path"]) if any(v.get("public_replaced") for v in m["versions"]) else None
        return RunResult(run, slate_id, dict(m["statuses"]), public, m, mp, np_, messages)

    def fail(msg: str) -> RunResult:
        messages.append(msg)
        m["failed"].append(msg)
        m["recommendation"] = "fix the named problem, then rerun; the previous file stands"
        return finish()

    if slate_id_for(pool) != slate_id:
        return fail(f"salary file is a different slate ({slate_id_for(pool)}) from run {parent.run_id} ({slate_id})")
    if current.mode is not pool.mode:
        return fail(f"entries export is {current.mode.value} but the salary file is {pool.mode.value}")

    # Statuses and locks.
    draftables = None
    if not offline:
        draftables, summary = fetch_draftables_bounded(current, cache, float(runtime["network_pass_budget_s"]))
        m["news"]["phase_b_summary"] = summary
        if draftables is None:
            m["failed"].append("draftables unavailable")
    else:
        m["news"]["phase_b_summary"] = "skipped (offline); statuses are as of the salary file's download"
    st = salary_statuses(pool)
    out_people = {pool.by_role_id[r].person_key for r, (p, _) in st.items() if p is Participation.OUT}
    unknown = {pool.by_role_id[r].name: raw for r, (p, raw) in st.items() if p is Participation.UNKNOWN}
    if draftables is not None:
        rec = dk_public.reconcile(pool, draftables)
        missing = sum(1 for s in rec.rows.values() if s.obs_status is ObsStatus.MISSING)
        m["statuses"]["NEWS_STATE"] = (NewsState.PARTIAL if missing else NewsState.FULL).value
        out_people |= {pool.by_role_id[r].person_key for r, s in rec.rows.items()
                       if s.participation is Participation.OUT or s.eligibility is Eligibility.DISABLED}
    excluded_rows = frozenset(r.role_id for r in pool.rows if r.person_key in out_people)
    m["news"]["csv_summary"] = (f"{len(out_people)} OUT/IR/DISABLED person(s) excluded from free cells, "
                                f"{len(unknown)} unrecognized status(es) kept as UNKNOWN")
    if unknown:
        messages.append("unrecognized DK status treated as UNKNOWN (not OUT): " + ", ".join(f"{k}={v!r}" for k, v in sorted(unknown.items())))

    buffer_s = int(runtime["edit_stop_buffer_s"])
    ls = locks_mod.compute(current, pool, draftables, clock(), buffer_s)
    m["locks"] = {
        "now_utc": _utc(ls.now_utc), "buffer_s": buffer_s, "counts": ls.counts(),
        "started_games": sorted(ls.started_games), "edit_stop_games": sorted(ls.edit_stop_games),
        "start_sources": dict(Counter(ls.start_source.values())),
    }
    if ls.edit_stop_games:
        messages.append("edit stop (not started): " + ", ".join(sorted(ls.edit_stop_games))
                        + f" start within {buffer_s}s; those cells are pinned and those players are not added")

    delivered = last_delivered(runs_root, outputs_root, slate_id, parent)
    m["last_delivered"] = str(delivered) if delivered else None
    m["manual_changes"] = manual_changes(current, delivered)
    if m["manual_changes"]:
        messages.append(f"{len(m['manual_changes'])} cell(s) in the current export differ from the last delivered file "
                        "(manual changes kept as the starting point)")

    # Re-solve.
    t = time.perf_counter()
    objective = prior_objective(pool, prior_table(pool))
    mode = pool.mode
    size = len(CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS)
    befores = {e.entry_id: _canonical(current, e) for e in current.entries}
    pins = {e.entry_id: ls.pinned(e.entry_id) for e in current.entries}

    def needs_repair(eid: str) -> bool:
        before, pin = befores[eid], pins[eid]
        open_ids = [rid for k, rid in enumerate(before) if k not in pin]
        if any(rid is None or rid in excluded_rows for rid in open_ids):
            return True
        return not check_lineup([pool.by_role_id[r] for r in before], mode).ok

    targets = []
    for e in current.entries:
        if e.entry_id in ls.unreadable_entries:
            continue
        if len(pins[e.entry_id]) == size:
            continue
        if not fast or needs_repair(e.entry_id):
            targets.append(e.entry_id)
    final: dict[str, list[str | None]] = {eid: list(b) for eid, b in befores.items()}
    person_n: Counter[str] = Counter()
    for eid, lineup in final.items():
        keep = lineup if eid not in targets else [pins[eid].get(k) for k in range(size)]
        person_n.update(pool.by_role_id[r].person_key for r in keep if r is not None and r in pool.by_role_id)
    cap = caps.person_cap(len(current.entries))
    outcomes: dict[str, EntryOutcome] = {}
    decided: list[list[str]] = [lu for eid, lu in final.items() if eid not in targets and all(lu)]
    time_limit = float(runtime.get("late_swap", {}).get("per_entry_time_limit_s", 2.0))
    for eid in targets:
        before, pin = befores[eid], pins[eid]
        pinned_people = {pool.by_role_id[r].person_key for r in pin.values()}
        capped = frozenset(r.role_id for r in pool.rows if person_n[r.person_key] >= cap and r.person_key not in pinned_people)
        overlaps = [] if fast else [(lu, size - 2) for lu in decided]
        lineup, route, relax, detail = _solve_entry(
            pool, mode, objective, before, pin, fast=fast,
            exclude_rows=excluded_rows | ls.not_addable, capped_rows=capped, overlaps=overlaps, time_limit_s=time_limit)
        if lineup is None:
            outcomes[eid] = EntryOutcome(eid, "unrepairable", before, before, detail, route=route)
            if all(before):
                decided.append(list(before))
            continue
        placed = _place(before, pin, list(lineup), pool, mode)
        for k, rid in enumerate(placed):
            if k not in pin:
                person_n[pool.by_role_id[rid].person_key] += 1
        final[eid] = placed
        decided.append(placed)
        action = "reoptimized" if not fast else "repaired"
        outcomes[eid] = EntryOutcome(eid, action, before, placed, detail, relax, route)
    timings["solve_s"] = round(time.perf_counter() - t, 3)

    changes = {eid: {k: rid for k, rid in enumerate(final[eid]) if rid != befores[eid][k] and rid is not None}
               for eid in final}
    changes = {eid: c for eid, c in changes.items() if c}
    slots = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    m["changed_cells"] = [{"entry_id": eid, "slot": f"{slots[k]}#{k}", "from": befores[eid][k], "to": rid}
                          for eid, c in changes.items() for k, rid in sorted(c.items())]
    m["entries"] = [{"entry_id": o.entry_id, "action": o.action, "route": o.route, "detail": o.detail,
                     "relaxations": list(o.relaxations)} for o in outcomes.values()]
    m["relaxations"] = [{"entry_id": o.entry_id, "kind": k, "detail": "late swap"} for o in outcomes.values() for k in o.relaxations]
    for eid in sorted(ls.unreadable_entries):
        messages.append(f"entry {eid}: a cell could not be read or is not in the salary pool; entry left unchanged")
    unrepaired = [o for o in outcomes.values() if o.action == "unrepairable"]
    for o in unrepaired:
        bad = [pool.by_role_id[r].name for r in o.before if r in excluded_rows]
        tail = f"; unavoidable nonplaying slot(s): {', '.join(bad)}" if bad else ""
        messages.append(f"entry {o.entry_id}: {o.detail}; cells kept{tail}")

    # Pinned exposures over cap (never removed; reported).
    pinned_n = Counter(pool.by_role_id[r].person_key for p in pins.values() for r in p.values())
    names = {r.person_key: r.name for r in pool.rows}
    m["pinned_over_cap"] = [{"name": names[pk], "entries": n, "cap": cap} for pk, n in pinned_n.most_common() if n > cap]
    if m["pinned_over_cap"]:
        messages.append("pinned exposure over cap (locked, cannot be removed): "
                        + ", ".join(f"{x['name']} {x['entries']}/{x['cap']}" for x in m["pinned_over_cap"]))

    # Lock-boundary recheck right before writing.
    ls2 = locks_mod.compute(current, pool, draftables, clock(), buffer_s)
    crossed = [(eid, k) for eid, c in changes.items() for k, rid in c.items()
               if ls2.cells[(eid, k)].pinned or not ls2.addable(rid)]
    if crossed:
        return fail(f"lock boundary crossed during compute ({len(crossed)} changed cell(s) now pinned or no longer "
                    "addable); nothing published, the previous file stands; rerun")

    # Write, referee, publish.
    staging = run.path / "staging" / "DKEntries.csv"
    staging.parent.mkdir(exist_ok=True)
    data = splice_cells(current, pool, changes, staging)
    perm = template_permutation(current.roster_labels, mode)
    col_of = {k: col for col, k in enumerate(perm)}
    locked_text = {(e.entry_id, col_of[k]): e.cells[col_of[k]]
                   for e in current.entries for k in range(size) if ls.cells[(e.entry_id, k)].pinned}
    report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv", locked=locked_text)
    if not report.ok:
        return fail("referee rejected the late-swap file: " + "; ".join(report.reasons[:5]))
    try:
        res = publish(run, data, report, slate_id, outputs_root=outputs_root)
    except (PublishRefused, LockTimeout) as exc:
        return fail(f"publish failed: {exc}")
    m["versions"].append({"version": res.version, "phase": kind, "sha256": report.out_sha256,
                          "created_utc": _utc(datetime.now(timezone.utc)), "path": str(res.version_path),
                          "public_replaced": res.public_replaced, "public_detail": res.public_detail})
    if res.public_detail:
        messages.append(res.public_detail)
    m["export_sha256"] = report.out_sha256
    m["statuses"]["FILE_VALID"] = FileStatus.TRUE.value
    m["statuses"]["SEARCH_STATUS"] = (SearchStatus.INFEASIBLE if unrepaired else SearchStatus.FEASIBLE).value
    degraded = bool(unrepaired or unknown or assumed_parent or ls.unreadable_entries or not res.public_replaced
                    or any(o.route == "feasible" for o in outcomes.values()))
    m["statuses"]["DELIVERY_STATUS"] = (DeliveryStatus.DEGRADED_REVIEW if degraded else DeliveryStatus.CHECKED).value
    assignment = Assignment(
        by_entry={eid: tuple(lu) for eid, lu in final.items()},
        exposures=dict(Counter(lineup_key([pool.by_role_id[r] for r in lu], mode) for lu in final.values() if all(lu))),
        person_exposures=dict(Counter(pool.by_role_id[r].person_key for lu in final.values() for r in lu if r)),
        captain_exposures=dict(Counter(pool.by_role_id[lu[0]].person_key for lu in final.values() if lu[0]))
        if mode is Mode.SHOWDOWN else {},
        overlap_max=0,
    )
    m["exposures_top20"] = exposures_top20(assignment, pool)
    n_changed = len(m["changed_cells"])
    m["worked"].append(f"{kind}: {n_changed} cell(s) changed in {len(changes)} entr{'y' if len(changes) == 1 else 'ies'}; "
                       f"{ls.counts().get('LOCKED', 0)} locked and {ls.counts().get('EDIT_STOP', 0)} edit-stop cells pinned")
    if n_changed == 0:
        messages.append("no cells changed; the file equals the current export, so there is nothing new to upload")
    m["recommendation"] = "keep"
    return finish()


def run(
    run_id: str,
    entries_current_path,
    *,
    offline: bool,
    fast: bool = True,
    runs_root="runs",
    outputs_root=None,
    clock: Callable[[], datetime] | None = None,
    cache=None,
    salary_path=None,
    runtime: dict | None = None,
    caps: Caps | None = None,
    as_of: datetime | None = None,
) -> RunResult:
    """Late swap run `run_id`'s slate from the current DK export. as_of: rehearsal clock (labeled)."""
    runs_root = Path(runs_root)
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    if as_of is not None:
        clock = lambda: as_of  # noqa: E731
    clock = clock or (lambda: datetime.now(timezone.utc))
    parent = open_run(runs_root, run_id)
    return swap_core(parent, Path(entries_current_path), kind="late_swap", offline=offline, fast=fast,
                     assumed_parent=False, runs_root=runs_root, outputs_root=outputs_root, clock=clock, cache=cache,
                     salary_path=salary_path, runtime=runtime, caps=caps, rehearsal=as_of)
