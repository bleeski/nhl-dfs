"""Local-first baseline run (card C2b; plan section 10 ladder and status list).

Phase A (local only, no network): intake -> DK status from the salary file's Status column ->
priors -> candidates (MILP when available, else feasible.find_one) -> assign -> write ->
referee -> publish v1 -> manifest -> RUN_NOTES.md.

Phase B (skipped when offline; one wall-clock budget, network_pass_budget_s): contest detail
-> draft group -> draftables -> reconcile -> exclude participation OUT and eligibility DISABLED
-> re-solve only the affected entries -> referee -> publish v2 if anything changed. Network
fetches run in a worker thread joined against the budget, so a slow source cannot hold the run;
any Phase B failure leaves v1 current and NEWS_STATE=NONE.

DK status: the salary file's Status column is read here (C0b kept it only in raw bytes) and
mapped through dk_public.map_status. OUT and IR exclude the person; DTD is QUESTIONABLE and
stays in the pool (priors only, no haircut; C3/C8 price it); an unrecognized status is UNKNOWN,
never OUT, and is reported. It is intake data, so Phase A leaves NEWS_STATE=NONE.

A run refuses to publish once any slate game has started: late swap is C2c's job.
"""

from __future__ import annotations

import csv
import hashlib
import io
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from nhl_dfs.build import candidates as cand_mod
from nhl_dfs.build import feasible, milp
from nhl_dfs.build.assign import Assignment, Caps, assign, load_caps
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.build.manifest import exposures_top20, write_manifest
from nhl_dfs.build.notes import chicago, write_run_notes
from nhl_dfs.build.state import LockTimeout, PublishRefused, RunDir, new_run, publish, sha256
from nhl_dfs.contracts.geometry import Mode, lineup_key
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
from nhl_dfs.export.writer import write_entries
from nhl_dfs.intake.entries import EntriesFile, read_entries
from nhl_dfs.intake.salary import PersonRows, SalaryPool, parse_game_info, read_salary
from nhl_dfs.models.priors import prior_objective, prior_table
from nhl_dfs.referee.check_file import check_file

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_YAML = REPO_ROOT / "config" / "runtime.yaml"


def load_runtime_config(path: Path = RUNTIME_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- intake helpers ------------------------------------------------------------------------

def salary_statuses(pool: SalaryPool) -> dict[str, tuple[Participation, str]]:
    """role_id -> (participation, raw text) from the salary file's Status column; {} if absent."""
    text = pool.raw.decode("utf-8-sig")
    records = list(csv.reader(io.StringIO(text, newline="")))
    if not records:
        return {}
    header = records[0]
    if header.count("Status") != 1 or header.count("ID") != 1:
        return {}
    si, ii = header.index("Status"), header.index("ID")
    out: dict[str, tuple[Participation, str]] = {}
    for rec in records[1:]:
        if len(rec) > max(si, ii) and rec[ii].strip() in pool.by_role_id:
            raw = rec[si].strip()
            part, _ = dk_public.map_status(raw or None)
            out[rec[ii].strip()] = (part, raw)
    return out


def start_times(pool: SalaryPool) -> dict[str, datetime]:
    """role_id -> scheduled game start (UTC) from Game Info."""
    out: dict[str, datetime] = {}
    for r in pool.rows:
        try:
            out[r.role_id] = parse_game_info(r.game_info)[1].start_utc
        except ValueError:
            continue
    return out


def slate_id_for(pool: SalaryPool) -> str:
    """Local draft-group key: mode, first start date (ET), and a hash of the sorted role IDs.

    Neither DK file carries the draft group ID, and the slate lock is taken before any network
    call, so the key comes from the ID set: stable across re-downloads of the same group.
    """
    ids = ",".join(sorted(pool.by_role_id))
    digest = hashlib.sha256(ids.encode("ascii")).hexdigest()[:10]
    starts = sorted(g.start_utc for g in pool.games.values())
    day = starts[0].astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d") if starts else "nodate"
    return f"{pool.mode.value}-{day}-{digest}"


def pool_without(pool: SalaryPool, role_ids) -> SalaryPool:
    """A copy of the pool without these rows (persons kept consistent). The input is unchanged."""
    drop = set(role_ids)
    if not drop:
        return pool
    rows = [r for r in pool.rows if r.role_id not in drop]
    persons: dict[str, PersonRows] = {}
    for pk, pr in pool.persons.items():
        kept = PersonRows(
            cpt=pr.cpt if pr.cpt and pr.cpt.role_id not in drop else None,
            flex=pr.flex if pr.flex and pr.flex.role_id not in drop else None,
            classic=pr.classic if pr.classic and pr.classic.role_id not in drop else None,
        )
        if kept.cpt or kept.flex or kept.classic:
            persons[pk] = kept
    return replace(
        pool,
        rows=rows,
        by_role_id={r.role_id: r for r in rows},
        persons=persons,
        teams=frozenset(r.team for r in rows),
    )


def _person_rows(pool: SalaryPool, person_keys) -> set[str]:
    keys = set(person_keys)
    return {r.role_id for r in pool.rows if r.person_key in keys}


# -- search --------------------------------------------------------------------------------

@dataclass
class SearchOutcome:
    bank: list[Candidate]
    status: SearchStatus
    route: str  # "milp" | "feasible"
    detail: str = ""


def build_bank(pool: SalaryPool, objective: dict[str, float], n_entries: int, runtime: dict, *, seed: int,
               time_limit_s: float | None = None) -> SearchOutcome:
    """MILP candidate bank, or the find_one fallback when the solver is missing or the bank is empty."""
    c = runtime["candidates"]
    n = min(int(c["bank_per_entry"]) * n_entries + int(c["bank_extra"]), int(c["bank_max"]))
    bank: list[Candidate] = []
    why = ""
    if milp.solver_available():
        try:
            bank = cand_mod.generate(
                pool, pool.mode, objective, n, seed=seed, perturb_sd=float(c["perturb_sd_points"]),
                time_limit_total_s=float(time_limit_s if time_limit_s is not None else c["time_limit_total_s"]),
                min_pairwise_diff=int(c["min_pairwise_diff"]),
            )
        except cand_mod.SolverUnavailable:
            why = "solver import failed"
        if not bank and not why:
            why = "MILP candidate bank came back empty"
    else:
        why = "solver unavailable"
    if bank:
        return SearchOutcome(bank, SearchStatus.FEASIBLE, "milp")
    return _fallback_bank(pool, objective, n_entries, runtime, why)


def _fallback_bank(pool, objective, n_entries, runtime, why) -> SearchOutcome:
    """feasible.find_one per entry, with deterministic variety: each new lineup bans one more row."""
    fb = runtime["fallback"]
    first = feasible.find_one(pool, pool.mode, budget_s=float(fb["budget_s"]))
    if first.status is FeasibleStatus.TIMEOUT:
        first = feasible.find_one(pool, pool.mode, budget_s=float(fb["retry_budget_s"]))
    if first.status is FeasibleStatus.INFEASIBLE_PROVEN:
        return SearchOutcome([], SearchStatus.INFEASIBLE, "feasible",
                             f"{why}; no legal lineup exists in the pool after exclusions ({first.detail})")
    if first.status is FeasibleStatus.TIMEOUT:
        return SearchOutcome([], SearchStatus.ERROR, "feasible",
                             f"{why}; feasibility search timed out twice ({fb['retry_budget_s']}s); not proof of infeasibility")
    found = [first.lineup]
    banned: set[str] = set()
    for k in range(1, n_entries):
        prev = found[-1]
        banned.add(prev[k % len(prev)])
        r = feasible.find_one(pool, pool.mode, exclude=frozenset(banned), budget_s=float(fb["budget_s"]))
        if r.status is not FeasibleStatus.FOUND:
            break
        found.append(r.lineup)
    bank, seen = [], set()
    for lineup in found:
        rows = [pool.by_role_id[x] for x in lineup]
        key = lineup_key(rows, pool.mode)
        if key in seen:
            continue
        seen.add(key)
        bank.append(Candidate(tuple(lineup), key, sum(objective.get(x, 0.0) for x in lineup), "fallback"))
    return SearchOutcome(bank, SearchStatus.FEASIBLE, "feasible", f"{why}; {len(bank)} fallback lineup(s)")


# -- run -----------------------------------------------------------------------------------

@dataclass
class RunResult:
    run: RunDir
    slate_id: str
    statuses: dict[str, str]
    public_path: Path | None
    manifest: dict[str, Any]
    manifest_path: Path
    notes_path: Path
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.statuses["FILE_VALID"] == FileStatus.TRUE.value


def _names(pool: SalaryPool, role_ids) -> list[str]:
    seen, out = set(), []
    for rid in role_ids:
        r = pool.by_role_id.get(rid)
        if r and r.person_key not in seen:
            seen.add(r.person_key)
            out.append(f"{r.name} ({r.team})")
    return sorted(out)


def run_slate(
    salary_path,
    entries_path,
    *,
    offline: bool,
    baseline_only: bool = True,
    out_root="runs",
    outputs_root=None,
    cache=None,
    clock: Callable[[], datetime] | None = None,
    seed: int | None = None,
    runtime: dict | None = None,
    caps: Caps | None = None,
) -> RunResult:
    """Build, check and publish the baseline. outputs_root defaults to <out_root>/../outputs."""
    if not baseline_only:
        raise NotImplementedError("only the baseline run exists until C8")
    clock = clock or (lambda: datetime.now(timezone.utc))
    runtime = runtime or load_runtime_config()
    caps = caps or load_caps()
    out_root = Path(out_root)
    outputs_root = Path(outputs_root) if outputs_root is not None else out_root.parent / "outputs"
    t_start = time.perf_counter()
    timings: dict[str, float] = {}
    messages: list[str] = []

    pool = read_salary(salary_path)
    entries = read_entries(entries_path)
    run = new_run(out_root, mode=pool.mode, clock=clock)
    (run.inputs / "DKSalaries.csv").write_bytes(pool.raw)
    (run.inputs / "DKEntries.csv").write_bytes(entries.raw)
    slate_id = slate_id_for(pool)
    seed = seed if seed is not None else int(pool.sha256[:8], 16)
    m: dict[str, Any] = {
        "run_id": run.run_id,
        "created_utc": _utc(clock()),
        "mode": pool.mode.value,
        "slate_id": slate_id,
        "salary_sha256": pool.sha256,
        "entries_sha256": entries.sha256,
        "export_sha256": None,
        "statuses": {
            "FILE_VALID": FileStatus.FALSE.value,
            "NEWS_STATE": NewsState.NONE.value,
            "MODEL_STATUS": ModelStatus.PRIOR.value,
            "SEARCH_STATUS": SearchStatus.ERROR.value,
            "DELIVERY_STATUS": DeliveryStatus.FAILED.value,
            "PAYOUT_SOURCE": PayoutSource.PRIOR.value,
            "OUTCOME_CALIBRATION": OutcomeCalibration.UNVALIDATED.value,
            "FIELD_CALIBRATION": FieldCalibration.PRIOR.value,
        },
        "entry_count": len(entries.entries),
        "entry_fees": [e.fee for e in entries.entries],
        "exposures_top20": [],
        "relaxations": [],
        "phase_timings": timings,
        "versions": [],
        "seed": seed,
        "public_path": str(outputs_root / slate_id / "DKEntries.csv"),
        "news": {},
        "messages": messages,
        "worked": [],
        "failed": [],
    }

    def finish() -> RunResult:
        timings["total_s"] = round(time.perf_counter() - t_start, 3)
        mp = write_manifest(run, m)
        np_ = write_run_notes(run, m)
        public = Path(m["public_path"]) if any(v.get("public_replaced") for v in m["versions"]) else None
        return RunResult(run, slate_id, dict(m["statuses"]), public, m, mp, np_, messages)

    def fail(msg: str, search: SearchStatus | None = None) -> RunResult:
        messages.append(msg)
        m["failed"].append(msg)
        if search is not None:
            m["statuses"]["SEARCH_STATUS"] = search.value
        m["recommendation"] = "fix the named input problem, then rerun"
        return finish()

    # Phase A ---------------------------------------------------------------------------------
    if entries.mode is not pool.mode:
        return fail(f"entries file is {entries.mode.value} but the salary file is {pool.mode.value}")
    now = clock().astimezone(timezone.utc)
    starts = start_times(pool)
    if starts:
        first_start = min(starts.values())
        if now >= first_start:
            return fail(f"slate started at {chicago(_utc(first_start))}; changing started entries is late swap "
                        "(`nhl.ps1 late-swap`, chunk C2c), not a baseline run")
        if now >= first_start - timedelta(seconds=float(runtime["edit_stop_buffer_s"])):
            messages.append(f"inside the edit-stop buffer: first game starts {chicago(_utc(first_start))}")

    st = salary_statuses(pool)
    out_people = {pool.by_role_id[rid].person_key for rid, (p, _) in st.items() if p is Participation.OUT}
    excluded = _person_rows(pool, out_people)
    questionable = [rid for rid, (p, _) in st.items() if p is Participation.QUESTIONABLE]
    unknown = {rid: raw for rid, (p, raw) in st.items() if p is Participation.UNKNOWN}
    m["news"]["csv"] = {
        "status_column": bool(st),
        "excluded_out": _names(pool, excluded),
        "questionable": _names(pool, questionable),
        "unknown": {pool.by_role_id[r].name: raw for r, raw in unknown.items()},
    }
    m["news"]["csv_summary"] = (
        f"{len(out_people)} OUT/IR excluded, {len(_names(pool, questionable))} DTD kept as QUESTIONABLE, "
        f"{len(unknown)} unrecognized (UNKNOWN, kept)" if st else "no Status column; nothing excluded"
    )
    if unknown:
        messages.append(f"unrecognized DK status on {len(unknown)} row(s), treated as UNKNOWN (not OUT): "
                        + ", ".join(f"{n}={v!r}" for n, v in sorted(m["news"]["csv"]["unknown"].items())))
    work = pool_without(pool, excluded)
    timings["intake_s"] = round(time.perf_counter() - t_start, 3)

    objective = prior_objective(work, prior_table(work))
    t = time.perf_counter()
    search = build_bank(work, objective, len(entries.entries), runtime, seed=seed)
    timings["candidates_s"] = round(time.perf_counter() - t, 3)
    m["search_route"] = search.route
    m["search_detail"] = search.detail
    m["bank_size"] = len(search.bank)
    m["statuses"]["SEARCH_STATUS"] = search.status.value
    if search.detail:
        messages.append(search.detail)
    if not search.bank:
        scope = "SEARCH_STATUS=INFEASIBLE (scope: whole pool after OUT/IR exclusions)" if search.status is SearchStatus.INFEASIBLE \
            else "SEARCH_STATUS=ERROR"
        return fail(f"no lineup to publish: {scope}", search.status)

    t = time.perf_counter()
    a = assign(search.bank, entries, work, pool.mode, caps, seed=seed, later_start_utc=starts)
    timings["assign_s"] = round(time.perf_counter() - t, 3)
    v1 = _export_and_publish(run, entries, pool, a, slate_id, outputs_root, m, messages, phase="A", expect=None)
    timings["phase_a_s"] = round(time.perf_counter() - t_start, 3)
    if v1 is None:
        return fail("phase A produced no checked file")
    _set_assignment_fields(m, a, pool)
    m["statuses"]["FILE_VALID"] = FileStatus.TRUE.value
    degraded = (search.route != "milp" or any(r.kind == "REPEAT" for r in a.relaxations) or unknown
                or not v1["public_replaced"])
    m["statuses"]["DELIVERY_STATUS"] = (DeliveryStatus.DEGRADED_REVIEW if degraded else DeliveryStatus.CHECKED).value
    m["worked"].append(f"phase A published v1 in {timings['phase_a_s']:.1f}s from local files only")
    m["recommendation"] = "keep"
    write_manifest(run, m)  # durable before any network call

    # Phase B ---------------------------------------------------------------------------------
    if offline:
        m["news"]["phase_b_summary"] = "skipped (offline)"
        return finish()
    t = time.perf_counter()
    _phase_b(run, entries, pool, work, a, objective, excluded, runtime, caps, seed, slate_id, outputs_root,
             v1, starts, cache, m, messages)
    timings["phase_b_s"] = round(time.perf_counter() - t, 3)
    return finish()


def _set_assignment_fields(m: dict, a: Assignment, pool: SalaryPool) -> None:
    m["exposures_top20"] = exposures_top20(a, pool)
    m["relaxations"] = [{"entry_id": r.entry_id, "kind": r.kind, "detail": r.detail} for r in a.relaxations]
    m["overlap_max"] = a.overlap_max
    m["distinct_lineups"] = len(a.exposures)


def _export_and_publish(run: RunDir, entries: EntriesFile, pool: SalaryPool, a: Assignment, slate_id: str,
                        outputs_root: Path, m: dict, messages: list[str], *, phase: str, expect: str | None) -> dict | None:
    """Write the file into the run, referee the written bytes, publish. Returns the version record."""
    staging = run.path / "staging" / f"DKEntries.{phase}.csv"
    staging.parent.mkdir(exist_ok=True)
    data = write_entries(entries, dict(a.by_entry), pool, staging)
    report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    if not report.ok:
        messages.append("referee rejected the file: " + "; ".join(report.reasons[:5]))
        m["failed"].append(f"phase {phase}: referee rejected the written file")
        return None
    try:
        res = publish(run, data, report, slate_id, outputs_root=outputs_root, expect_public_sha=expect)
    except PublishRefused as exc:
        messages.append(f"publish refused: {exc}")
        m["failed"].append(f"phase {phase}: publish refused")
        return None
    except LockTimeout as exc:  # another run holds this slate; never wait forever, never half-publish
        messages.append(f"publish skipped: {exc} (another run on this slate is publishing; rerun when it finishes)")
        m["failed"].append(f"phase {phase}: slate lock busy")
        return None
    rec = {
        "version": res.version,
        "phase": phase,
        "sha256": report.out_sha256,
        "created_utc": _utc(datetime.now(timezone.utc)),
        "path": str(res.version_path),
        "public_replaced": res.public_replaced,
        "public_detail": res.public_detail,
    }
    m["versions"].append(rec)
    m["export_sha256"] = report.out_sha256
    if res.public_detail:
        messages.append(res.public_detail)
    return rec


def _fetch_draftables(entries: EntriesFile, cache, pool: SalaryPool):
    """Network part of Phase B (worker thread): contest detail -> draft group -> draftables -> reconcile."""
    contest_id = int(entries.entries[0].contest_id)
    detail = dk_public.contest_detail(contest_id, cache=cache)
    d = dk_public.draftables(detail.draft_group_id, cache=cache)
    return detail.draft_group_id, dk_public.reconcile(pool, d)


def _phase_b(run, entries, pool, work, a, objective, excluded_a, runtime, caps, seed, slate_id, outputs_root,
             v1, starts, cache, m, messages) -> None:
    budget = float(runtime["network_pass_budget_s"])
    deadline = time.monotonic() + budget
    box: dict[str, Any] = {}

    def worker():
        try:
            box["result"] = _fetch_draftables(entries, cache, pool)
        except BaseException as exc:  # reported, never raised out of the run
            box["error"] = exc

    th = threading.Thread(target=worker, name="nhl-phase-b", daemon=True)
    th.start()
    th.join(timeout=budget)
    if th.is_alive():
        m["news"]["phase_b_summary"] = f"network pass exceeded {budget:.0f}s; v1 stays current"
        m["failed"].append("phase B: budget exceeded")
        return
    if "error" in box:
        exc = box["error"]
        m["news"]["phase_b_summary"] = f"draftables unavailable ({type(exc).__name__}: {str(exc)[:120]}); v1 stays current"
        m["failed"].append("phase B: DK draftables unavailable")
        return
    group_id, rec = box["result"]
    m["news"]["draft_group_id"] = group_id
    missing = [rid for rid, s in rec.rows.items() if s.obs_status is ObsStatus.MISSING]
    conflicted = rec.conflicted
    m["statuses"]["NEWS_STATE"] = (NewsState.PARTIAL if missing else NewsState.FULL).value
    out_people = {pool.by_role_id[rid].person_key for rid, s in rec.rows.items()
                  if s.participation is Participation.OUT or s.eligibility is Eligibility.DISABLED}
    new_rows = _person_rows(pool, out_people) - set(excluded_a)
    unknown = [rid for rid, s in rec.rows.items() if s.participation is Participation.UNKNOWN and s.obs_status is not ObsStatus.MISSING]
    m["news"]["draftables"] = {
        "missing": len(missing),
        "conflicted": [f"{pool.by_role_id[r].name}: {', '.join(rec.rows[r].notes)}" for r in conflicted],
        "newly_excluded": _names(pool, new_rows),
        "unknown": _names(pool, unknown),
    }
    if conflicted:
        messages.append(f"{len(conflicted)} row(s) disagree between the salary file and draftables; the file governs")
    affected = [e.entry_id for e in entries.entries if set(a.by_entry[e.entry_id]) & new_rows]
    if not affected:
        m["news"]["phase_b_summary"] = f"draftables reconciled (group {group_id}); no new OUT/DISABLED in any lineup; no v2"
        m["worked"].append("phase B reconciled draftables")
        return
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        m["news"]["phase_b_summary"] = f"{len(new_rows)} new exclusion(s) found but no budget left to re-solve; v1 stays current"
        m["failed"].append("phase B: out of budget before re-solve")
        messages.append("REVIEW: newly OUT/DISABLED players remain in v1: " + ", ".join(_names(pool, new_rows)))
        return
    work2 = pool_without(work, new_rows)
    obj2 = {k: v for k, v in objective.items() if k in work2.by_role_id}
    search = build_bank(work2, obj2, len(affected), runtime, seed=seed + 1, time_limit_s=max(1.0, remaining - 1.0))
    if not search.bank:
        m["news"]["phase_b_summary"] = f"re-solve after new exclusions failed ({search.detail}); v1 stays current"
        m["failed"].append("phase B: re-solve failed")
        messages.append("REVIEW: newly OUT/DISABLED players remain in v1: " + ", ".join(_names(pool, new_rows)))
        return
    fixed = {e: v for e, v in a.by_entry.items() if e not in affected}
    a2 = assign(search.bank, entries, work2, pool.mode, caps, seed=seed, later_start_utc=starts, fixed=fixed)
    v2 = _export_and_publish(run, entries, pool, a2, slate_id, outputs_root, m, messages, phase="B", expect=v1["sha256"])
    if v2 is None:
        m["news"]["phase_b_summary"] = "v2 failed its checks; v1 stays current"
        return
    _set_assignment_fields(m, a2, pool)
    if search.route != "milp" or any(r.kind == "REPEAT" for r in a2.relaxations) or not v2["public_replaced"]:
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["news"]["phase_b_summary"] = (f"{len(new_rows)} row(s) newly OUT/DISABLED ({', '.join(_names(pool, new_rows))}); "
                                    f"{len(affected)} entr{'y' if len(affected) == 1 else 'ies'} re-solved; v{v2['version']} published")
    m["worked"].append("phase B re-solved affected entries")
