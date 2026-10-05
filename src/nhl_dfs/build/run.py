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

Provisional pass (C3; skipped with baseline_only=True, i.e. `run --baseline`): after Phase B,
or after the offline skip, on the post-Phase-B pool. Contest detail per distinct contest when
online (PAYOUT_SOURCE=EXACT), else declared family priors (PRIOR) -> ownership prior -> one
sampled field per contest family -> provisional leverage selection -> referee -> publish the
next version with a compare-and-swap on the last published sha. Any failure leaves the current
version in place and is recorded. Every figure it adds is labeled provisional.

Scenario pass (C8; `scenario=True`, which `nhl.ps1 run` without --baseline sets): after the
provisional pass, the slate is simulated (design, selection and referee seed streams), candidates
are discovered and the portfolio is chosen against each contest's family objective, the risk
budget and the caps (build/scenario_pass.py), and the next version is published with a
compare-and-swap. Any failure leaves the current version in place and is recorded.

Started slates (C15, B42 part 3, flag 15): a run that starts after the first game builds the open games. Rows DraftKings
marks In-Progress, and rows whose Game Info start has passed at the run's clock, leave the pool (`SalaryPool.started_rows`);
a cell of the entries file that holds one of those players is pinned and stays as it is, the entry's other cells are
solved around it, and the report names the started teams and the excluded players. A slate with no open game left is
refused (late swap is C2c's job). The optional passes stay off once a game has started (C14).

Lock safety (C14, B53): every publish above goes through `_export_and_publish`, which refuses, under the publish
lock and on the live clock, a version that changes a cell of a game that has started or crossed the edit stop,
judged against the predecessor version. With a predecessor the checked version stays current and the run says why;
with none (Phase A) a started-game change publishes nothing and an edit-stop change ships with a warning (flag 20).
The optional passes (B, provisional, scenario) do not start once a game has started or is inside the edit stop.
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
from nhl_dfs.build import locks as locks_mod
from nhl_dfs.build.assign import Assignment, Caps, assign, load_caps
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.build.manifest import exposures_top20, write_manifest
from nhl_dfs.build.notes import chicago, write_run_notes
from nhl_dfs.build.state import LockCrossed, LockTimeout, PublishRefused, RunDir, new_run, publish, sha256
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, check_lineup, lineup_key
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
from nhl_dfs.intake.salary import PersonRows, SalaryPool, mark_started, parse_game_info, read_salary, row_start, with_started_rows
from nhl_dfs.models.projection import PriorProjection, objective as objective_from
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


def salary_starting(pool: SalaryPool) -> dict[str, str]:
    """role_id -> the salary file's Starting column text (DraftKings marks its starting goalie 'P'); {} if absent."""
    text = pool.raw.decode("utf-8-sig")
    records = list(csv.reader(io.StringIO(text, newline="")))
    if not records:
        return {}
    header = records[0]
    if header.count("Starting") != 1 or header.count("ID") != 1:
        return {}
    si, ii = header.index("Starting"), header.index("ID")
    return {rec[ii].strip(): rec[si].strip() for rec in records[1:]
            if len(rec) > max(si, ii) and rec[ii].strip() in pool.by_role_id}


def dk_goalie_starters(pool: SalaryPool, st: dict | None = None) -> dict[str, str]:
    """DK team -> person_key of the one goalie the salary file marks Starting=P (Ben, 2026-09-30: use DraftKings'
    starting flag). A team with no P goalie, several, or a P goalie DraftKings lists OUT is left out."""
    st = salary_statuses(pool) if st is None else st
    flag = salary_starting(pool)
    marked: dict[str, set[str]] = {}
    for r in pool.rows:
        if r.is_goalie and flag.get(r.role_id, "").upper() == "P":
            marked.setdefault(r.team, set()).add(r.person_key)
    out = {}
    for team, keys in marked.items():
        if len(keys) != 1:
            continue
        k = next(iter(keys))
        rows = [r for r in pool.rows if r.person_key == k]
        if any(st.get(r.role_id, (Participation.PLAYING, ""))[0] is Participation.OUT for r in rows):
            continue
        out[team] = k
    return out


def dk_backup_goalies(pool: SalaryPool, st: dict | None = None) -> set[str]:
    """person_keys of goalies whose team has a DraftKings starter (Starting=P) other than them."""
    starters = dk_goalie_starters(pool, st)
    return {r.person_key for r in pool.rows if r.is_goalie and r.team in starters and r.person_key != starters[r.team]}


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
    The set is the selectable rows plus the rows of games already in progress (C15, B42 part 3): DK's marker moves
    those out of the pool but not out of the draft group, so a file downloaded after a game started has the id of the
    file downloaded before it. Rows excluded for a data conflict stay out, as they always did; adding them would
    change the id of every existing slate. (If every game has started and none carries a start time the date is
    "nodate".)
    """
    ids = ",".join(sorted(set(pool.by_role_id) | set(pool.started_by_role_id)))
    digest = hashlib.sha256(ids.encode("ascii")).hexdigest()[:10]
    starts = sorted([g.start_utc for g in pool.games.values()] + [t for t in map(row_start, pool.started_rows) if t is not None])
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


def load_run_pool(run: RunDir):
    """Rebuild a run's working pool from its input copies, exactly as Phase A did: participation OUT
    (DK OUT or IR) excluded. Returns (pool, entries, work pool, salary statuses). Shared by
    `field --run` and `simulate --run` so both see the same people."""
    pool = read_salary(run.inputs / "DKSalaries.csv")
    entries = read_entries(run.inputs / "DKEntries.csv")
    st = salary_statuses(pool)
    out = {pool.by_role_id[r].person_key for r, (p, _) in st.items() if p is Participation.OUT}
    out |= dk_backup_goalies(pool, st)  # the same exclusion Phase A makes
    return pool, entries, pool_without(pool, _person_rows(pool, out)), st


# -- projection (C5) ----------------------------------------------------------------------

def slate_as_of(pool: SalaryPool, clock) -> "date":
    """Features use games strictly before this date: the earlier of today and the first slate game
    day, both in Eastern time (NHL game dates)."""
    et = ZoneInfo("America/New_York")
    today = clock().astimezone(et).date()
    starts = [g.start_utc.astimezone(et).date() for g in pool.games.values()]
    return min([today] + starts)


def _projection(work: SalaryPool, pool: SalaryPool, clock, m: dict, messages: list[str]):
    """Per-person ParamTable from stored history (C5), or the salary/APPG priors if it cannot be built."""
    from nhl_dfs.models import params

    as_of = slate_as_of(pool, clock)
    try:
        table = params.projection_for(work, as_of)
    except Exception as exc:  # reported; the run continues on priors
        messages.append(f"per-person params unavailable ({type(exc).__name__}: {str(exc)[:120]}); using priors")
        m["model"] = {"as_of": as_of.isoformat(), "status": ModelStatus.PRIOR.value, "error": type(exc).__name__}
        return PriorProjection(work)
    m["statuses"]["MODEL_STATUS"] = table.source().value
    m["model"] = {"as_of": as_of.isoformat(), "status": table.source().value, "counts": table.counts(),
                  "notes": table.notes[:5]}
    return table


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
    scenario: bool = False,
    scenario_n: dict | None = None,
) -> RunResult:
    """Build, check and publish the baseline, then (unless baseline_only) the provisional
    leverage version, then (with scenario=True) the scenario version. outputs_root defaults to
    <out_root>/../outputs. scenario_n overrides config/risk.yaml scenario counts (tests)."""
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
        if m["versions"]:  # B17: the goalie table of the file handed over (a report; it never changes the file)
            from nhl_dfs.build import goalies

            t_g = time.perf_counter()
            try:
                m["goalies"] = goalies.for_run(run, m, now=clock(), offline=offline, cache=cache, budget_s=float(
                    runtime.get("late_swap", {}).get("goalie_gate_budget_s", 8.0)))
            except Exception as exc:
                messages.append(f"goalie table unavailable ({type(exc).__name__}: {str(exc)[:100]})")
            timings["goalie_table_s"] = round(time.perf_counter() - t_g, 3)
            if (m.get("goalies") or {}).get("GOALIE_GATE") == "NOT_STARTING":
                m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
                messages.append("GOALIE_GATE=NOT_STARTING: the current file holds a goalie known not to start; run "
                                "refresh (or late swap with the current export) before lock")
        timings["total_s"] = round(time.perf_counter() - t_start, 3)
        mp = write_manifest(run, m)
        np_ = write_run_notes(run, m)
        public = Path(m["public_path"]) if any(v.get("public_replaced") for v in m["versions"]) else None
        return RunResult(run, slate_id, dict(m["statuses"]), public, m, mp, np_, messages)

    def fail(msg: str, search: SearchStatus | None = None, recommendation: str | None = None) -> RunResult:
        messages.append(msg)
        m["failed"].append(msg)
        if search is not None:
            m["statuses"]["SEARCH_STATUS"] = search.value
        m["recommendation"] = recommendation or "fix the named input problem, then rerun"
        return finish()

    def stopped(label: str) -> bool:
        """Baseline first; optional work stops before lock (B53). True, with the reason in the messages and m["lock_stops"],
        when optional pass `label` must not start. The passes after Phase A rebuild whole portfolios and know nothing of
        pinned cells, so once a game has started or is inside the edit stop their file could only be refused at publish:
        skip them and keep the checked version. Also stops when the next open game leaves too little time (as late swap)."""
        if run.current_version() is None:
            return False
        from nhl_dfs.build import swap_objective

        at = clock().astimezone(timezone.utc)
        ls = locks_mod.compute(read_entries(run.version_file(run.current_version())), pool, None, at,
                               int(runtime["edit_stop_buffer_s"]))
        if ls.started_games or ls.edit_stop_games:
            why = "a game has started or is inside the edit stop (" + ", ".join(sorted(ls.started_games | ls.edit_stop_games)) + ")"
        else:
            ok, why = swap_objective.optional_work_ok(ls, pool, set(pool.games), at, runtime)
            if ok:
                return False
        messages.append(f"{label} skipped: {why}; v{run.current_version()} stays current")
        m.setdefault("lock_stops", []).append({"pass": label, "reason": why, "at_utc": _utc(at)})
        return True

    # Phase A ---------------------------------------------------------------------------------
    if entries.mode is not pool.mode:
        return fail(f"entries file is {entries.mode.value} but the salary file is {pool.mode.value}")
    now = clock().astimezone(timezone.utc)
    n_marker = len(pool.started_rows)
    pool = mark_started(pool, now)  # flag 15: games past their Game Info start leave the pool as DK's marker does
    if pool.started_rows and not pool.rows:
        return fail("every game on this slate has started, so there is nothing left to build; changing started entries "
                    "is late swap (`nhl.ps1 late-swap`, chunk C2c), not a baseline run")
    starts = start_times(pool)
    if starts:
        first_start = min(starts.values())
        if now >= first_start - timedelta(seconds=float(runtime["edit_stop_buffer_s"])):
            messages.append(f"inside the edit-stop buffer: first game starts {chicago(_utc(first_start))}")
    pins: dict[str, dict[int, str]] = {}
    if pool.started_rows:
        pins = _started_slate(pool, entries, n_marker, now, int(runtime["edit_stop_buffer_s"]), m, messages)

    st = salary_statuses(pool)
    out_people = {pool.by_role_id[rid].person_key for rid, (p, _) in st.items() if p is Participation.OUT}
    backups = dk_backup_goalies(pool, st) - out_people  # DraftKings marks another goalie of the team Starting=P
    excluded = _person_rows(pool, out_people | backups)
    questionable = [rid for rid, (p, _) in st.items() if p is Participation.QUESTIONABLE]
    unknown = {rid: raw for rid, (p, raw) in st.items() if p is Participation.UNKNOWN}
    m["news"]["csv"] = {
        "status_column": bool(st),
        "excluded_out": _names(pool, _person_rows(pool, out_people)),
        "excluded_dk_backup_goalies": _names(pool, _person_rows(pool, backups)),
        "questionable": _names(pool, questionable),
        "unknown": {pool.by_role_id[r].name: raw for r, raw in unknown.items()},
    }
    m["news"]["csv_summary"] = (
        f"{len(out_people)} OUT/IR excluded, {len(backups)} goalie(s) excluded because DraftKings marks another goalie "
        f"of their team Starting=P, {len(_names(pool, questionable))} DTD kept as QUESTIONABLE, "
        f"{len(unknown)} unrecognized (UNKNOWN, kept)" if st else "no Status column; nothing excluded"
    )
    if unknown:
        messages.append(f"unrecognized DK status on {len(unknown)} row(s), treated as UNKNOWN (not OUT): "
                        + ", ".join(f"{n}={v!r}" for n, v in sorted(m["news"]["csv"]["unknown"].items())))
    work = pool_without(pool, excluded)
    timings["intake_s"] = round(time.perf_counter() - t_start, 3)

    proj = _projection(work, pool, clock, m, messages)
    objective = objective_from(work, proj)
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
    # The rows of games in progress ride along (spool) so the cells that hold them resolve, write and score; they are
    # never in the bank, and `fixed` lineups are the pinned entries solved around them.
    spool = with_started_rows(pool)
    fixed, unrepaired = {}, []
    if pins:
        fixed, unrepaired, impossible = _pinned_lineups(spool, work, entries, pins, objective, runtime, caps, messages)
        if impossible:
            return fail("no legal lineup keeps the started-game cell(s) of entr" + ("y " if len(impossible) == 1 else "ies ")
                        + ", ".join(impossible[:5]) + " and the entry's current cells are not a complete legal lineup; "
                        "nothing published")
        m["started_slate"]["unrepaired_entries"] = unrepaired
    a = assign(search.bank, entries, spool, pool.mode, caps, seed=seed, later_start_utc=starts, fixed=fixed or None)
    timings["assign_s"] = round(time.perf_counter() - t, 3)
    v1 = _export_and_publish(run, entries, spool, a, slate_id, outputs_root, m, messages, phase="A", expect=None,
                             clock=clock, runtime=runtime)
    timings["phase_a_s"] = round(time.perf_counter() - t_start, 3)
    if v1 is None:
        return fail("phase A produced no checked file", recommendation=(
            "a game started while the file was being built, and a rerun cannot help: use late swap (`nhl.ps1 late-swap`) "
            "with the entries file you have now" if m.get("lock_stops") else None))
    _set_assignment_fields(m, a, spool)
    m["statuses"]["FILE_VALID"] = FileStatus.TRUE.value
    degraded = (search.route != "milp" or any(r.kind == "REPEAT" for r in a.relaxations) or unknown
                or not v1["public_replaced"] or bool(unrepaired))
    m["statuses"]["DELIVERY_STATUS"] = (DeliveryStatus.DEGRADED_REVIEW if degraded else DeliveryStatus.CHECKED).value
    m["worked"].append(f"phase A published v1 in {timings['phase_a_s']:.1f}s from local files only")
    m["recommendation"] = "keep"
    write_manifest(run, m)  # durable before any network call

    # Phase B ---------------------------------------------------------------------------------
    after_b = {"work": work, "sha": v1["sha256"], "bank": search.bank, "details": {}, "proj": proj}
    if offline:
        m["news"]["phase_b_summary"] = "skipped (offline)"
    elif stopped("phase B"):
        m["news"]["phase_b_summary"] = "skipped (a lock boundary was reached); v1 stays current"
    else:
        t = time.perf_counter()
        after_b = _phase_b(run, entries, pool, work, a, objective, excluded, runtime, caps, seed, slate_id,
                           outputs_root, v1, starts, cache, m, messages, clock=clock)
        after_b.setdefault("bank", search.bank)
        after_b["proj"] = proj
        timings["phase_b_s"] = round(time.perf_counter() - t, 3)
        save_contest_details(run, after_b.get("details") or {}, m, "Phase B contest detail")
    if baseline_only or stopped("provisional and scenario passes"):
        return finish()

    # Role state (C7), once, before the provisional and scenario passes (backlog B9, C10) ------------
    t = time.perf_counter()
    rm = _role_model(pool, after_b, st, offline, cache, clock, runtime, m, messages)
    timings["roles_s"] = round(time.perf_counter() - t, 3)

    # Provisional pass (C3) -------------------------------------------------------------------
    t = time.perf_counter()
    prov = None
    try:
        prov = _provisional_pass(run, entries, pool, after_b, st, starts, offline, runtime, caps, seed, slate_id,
                                 outputs_root, cache, m, messages, out_root, rm=rm, clock=clock)
    except Exception as exc:  # the current version stays; never raised out of the run
        m["failed"].append(f"provisional pass: {type(exc).__name__}: {str(exc)[:160]}")
        messages.append(f"provisional pass failed ({type(exc).__name__}); the previous version stays current")
    timings["provisional_s"] = round(time.perf_counter() - t, 3)
    if not scenario or stopped("scenario pass"):
        return finish()

    # Scenario pass (C8) -----------------------------------------------------------------------
    from nhl_dfs.build.scenario_pass import run_scenario_pass

    t = time.perf_counter()
    try:
        run_scenario_pass(run=run, entries=entries, pool=pool, work=after_b["work"], proj=after_b["proj"], st=st,
                          starts=starts, offline=offline, runtime=runtime, seed=seed, slate_id=slate_id,
                          outputs_root=outputs_root, cache=cache, m=m, messages=messages, prov=prov, v1_assignment=a,
                          expect_sha=m["versions"][-1]["sha256"], clock=clock, publish_fn=_export_and_publish,
                          set_fields_fn=_set_assignment_fields, scenario_n=scenario_n,
                          play_prob=rm.play_prob if rm is not None else None,
                          confirmed_at=rm.roles.confirmed_at() if rm is not None else None,
                          roles=rm.roles if rm is not None else None)
    except Exception as exc:  # the current version stays; never raised out of the run
        import traceback

        m["failed"].append(f"scenario pass: {type(exc).__name__}: {str(exc)[:160]}")
        m.setdefault("scenario", {})["error"] = traceback.format_exc()[-1500:]
        messages.append(f"scenario pass failed ({type(exc).__name__}); the previous version stays current")
    timings["scenario_s"] = round(time.perf_counter() - t, 3)
    return finish()


def _started_slate(pool: SalaryPool, entries: EntriesFile, n_marker: int, now: datetime, buffer_s: int, m: dict,
                   messages: list[str]) -> dict[str, dict[int, str]]:
    """C15 (flag 15): name the started games and the excluded players in the report, and find the pinned cells: entry id ->
    canonical slot -> role id for every cell of the entries file that holds a started-game player."""
    started = pool.started_by_role_id
    ls = locks_mod.compute(entries, pool, None, now, buffer_s)
    pins: dict[str, dict[int, str]] = {}
    for e in entries.entries:
        held = {k: rid for k, rid in ls.pinned(e.entry_id).items() if rid in started}
        if held:
            pins[e.entry_id] = held
    by_team: dict[str, list[str]] = {}
    for r in sorted(pool.started_rows, key=lambda x: (x.team, x.name)):
        by_team.setdefault(r.team, []).append(r.name)
    n_clock = len(pool.started_rows) - n_marker
    how = ", ".join(x for x in (f"{n_marker} by the In-Progress marker" if n_marker else "",
                                f"{n_clock} by the Game Info start time and the run's clock" if n_clock else "") if x)
    open_games = sorted(pool.games)
    m["started_slate"] = {
        "started_teams": sorted(by_team), "excluded_players": by_team, "excluded_rows": len(pool.started_rows), "how": how,
        "open_games": open_games, "pinned_cells": sum(len(p) for p in pins.values()), "pinned_entries": sorted(pins),
        "unrepaired_entries": [],
    }
    messages.append(f"STARTED SLATE: games in progress for {', '.join(sorted(by_team))}; {len(pool.started_rows)} player row(s) "
                    f"excluded ({how}) and never added; building the open games: {', '.join(open_games)}")
    for team, names in by_team.items():
        messages.append(f"started, excluded: {team}: {', '.join(names)}")
    if pins:
        messages.append(f"{sum(len(p) for p in pins.values())} cell(s) in {len(pins)} entr{'y' if len(pins) == 1 else 'ies'} hold "
                        "a started-game player and stay as they are; the entry's other cells are rebuilt around them")
    return pins


def _pinned_lineups(spool: SalaryPool, work: SalaryPool, entries: EntriesFile, pins: dict[str, dict[int, str]],
                    objective: dict[str, float], runtime: dict, caps: Caps, messages: list[str]
                    ) -> tuple[dict[str, tuple[str, ...]], list[str], list[str]]:
    """(fixed lineups, entries whose current cells were kept, entries with no legal lineup). Each entry that holds a
    started-game player is solved on its own with those cells pinned (late swap's repair, the baseline objective, only
    the rows of `work` as candidates); the others are filled from the bank by `assign`, counting these as fixed. The
    pinned cells keep their slots. An entry the pins leave without a legal completion keeps its current cells when
    those are a complete legal lineup (reported, DEGRADED_REVIEW); otherwise the run publishes nothing."""
    from collections import Counter

    from nhl_dfs.build.late_swap import _canonical, _place, _solve_entry

    mode = spool.mode
    size = len(CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS)
    exclude = frozenset(spool.by_role_id) - frozenset(work.by_role_id)  # DK-OUT rows and the started rows
    limit = float(runtime.get("late_swap", {}).get("per_entry_time_limit_s", 2.0))
    cap = caps.person_cap(len(entries.entries))
    person_n: Counter[str] = Counter(spool.by_role_id[r].person_key for p in pins.values() for r in p.values())
    fixed: dict[str, tuple[str, ...]] = {}
    kept: list[str] = []
    impossible: list[str] = []
    for e in entries.entries:
        pin = pins.get(e.entry_id)
        if not pin:
            continue
        before = [pin.get(k) for k in range(size)]
        pinned_people = {spool.by_role_id[r].person_key for r in pin.values()}
        capped = frozenset(r.role_id for r in work.rows if person_n[r.person_key] >= cap and r.person_key not in pinned_people)
        lineup, _route, _relax, detail = _solve_entry(spool, mode, objective, before, pin, fast=False, exclude_rows=exclude,
                                                      capped_rows=capped, overlaps=[], time_limit_s=limit)
        placed = _place(before, pin, list(lineup), spool, mode) if lineup is not None else None
        if placed is None or any(placed[k] != rid for k, rid in pin.items()):
            current = _canonical(entries, e)
            rows = [spool.by_role_id.get(r) if r else None for r in current]
            if all(rows) and check_lineup(rows, mode).ok:
                fixed[e.entry_id] = tuple(current)  # type: ignore[arg-type]
                kept.append(e.entry_id)
                messages.append(f"entry {e.entry_id}: no legal rebuild around its started-game cell(s) ({detail or 'solver'}); "
                                "its current cells are kept")
            else:
                impossible.append(e.entry_id)
            continue
        fixed[e.entry_id] = tuple(placed)
        person_n.update(spool.by_role_id[r].person_key for k, r in enumerate(placed) if k not in pin)
    return fixed, kept, impossible


def _set_assignment_fields(m: dict, a: Assignment, pool: SalaryPool) -> None:
    m["exposures_top20"] = exposures_top20(a, pool)
    m["relaxations"] = [{"entry_id": r.entry_id, "kind": r.kind, "detail": r.detail} for r in a.relaxations]
    m["overlap_max"] = a.overlap_max
    m["distinct_lineups"] = len(a.exposures)


def _export_and_publish(run: RunDir, entries: EntriesFile, pool: SalaryPool, a: Assignment, slate_id: str,
                        outputs_root: Path, m: dict, messages: list[str], *, phase: str, expect: str | None,
                        clock: Callable[[], datetime], runtime: dict) -> dict | None:
    """Write the file into the run, referee the written bytes, publish. Returns the version record, or None when
    nothing was published.

    Lock safety (B53): under the publish lock, on `clock()` read there, the new file is diffed against the predecessor
    version (the uploaded entries file when this is the first publish) and refused if it changes a cell of a game that
    has started or is inside the edit stop; the incumbent then stays current. Flag 20: with no predecessor an edit-stop
    change ships with a warning (there is no checked file to keep) and a started-game change publishes nothing."""
    staging = run.path / "staging" / f"DKEntries.{phase}.csv"
    staging.parent.mkdir(exist_ok=True)
    data = write_entries(entries, dict(a.by_entry), pool, staging)
    report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    if not report.ok:
        messages.append("referee rejected the file: " + "; ".join(report.reasons[:5]))
        m["failed"].append(f"phase {phase}: referee rejected the written file")
        return None
    proposed = read_entries(staging)
    buffer_s = int(runtime["edit_stop_buffer_s"])

    def cells(lines) -> int:  # a cell that loses one player and gains another is two lines
        return len({x.split(":", 1)[0] for x in lines})

    def lock_check() -> str | None:
        incumbent = run.current_version()
        reference = read_entries(run.version_file(incumbent)) if incumbent is not None else entries
        found = locks_mod.publish_crossings(reference, proposed, pool, clock(), buffer_s)
        if incumbent is None:
            if found.edit_stop:
                messages.append(f"phase {phase}: {cells(found.edit_stop)} cell(s) are in a game inside the edit stop; "
                                "published as built (nothing earlier to keep): " + "; ".join(found.edit_stop[:3]))
            if found.started:
                return (f"a game started while the file was being built: {cells(found.started)} cell(s) of a started game "
                        "would change (" + "; ".join(found.started[:3]) + "); nothing published. Changing started entries is "
                        "late swap (`nhl.ps1 late-swap` with the current export)")
            return None
        hit = found.started + found.edit_stop
        if hit:
            return (f"a lock boundary was crossed while phase {phase} ran: {cells(hit)} cell(s) of a started or edit-stop game "
                    f"would change ({'; '.join(hit[:3])}); v{incumbent} stays current")
        return None

    try:
        res = publish(run, data, report, slate_id, outputs_root=outputs_root, expect_public_sha=expect, precheck=lock_check)
    except LockCrossed as exc:
        messages.append(f"phase {phase} not published: {exc}")
        m.setdefault("lock_stops", []).append({"pass": f"phase {phase} publish", "reason": str(exc)})
        return None
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


def _fetch_draftables(entries: EntriesFile, cache, pool: SalaryPool, box: dict | None = None):
    """Network part of Phase B (worker thread): contest detail -> draft group -> draftables -> reconcile."""
    contest_id = int(entries.entries[0].contest_id)
    detail = dk_public.contest_detail(contest_id, cache=cache)
    if box is not None:
        box["detail"] = detail  # kept even when draftables then fails (403)
    d = dk_public.draftables(detail.draft_group_id, cache=cache)
    return detail.draft_group_id, dk_public.reconcile(pool, d)


def _phase_b(run, entries, pool, work, a, objective, excluded_a, runtime, caps, seed, slate_id, outputs_root,
             v1, starts, cache, m, messages, *, clock) -> dict:
    """Returns the state the provisional pass builds on: the pool after any new exclusions
    (even when the re-solve failed), the last published sha, and any contest detail fetched."""
    budget = float(runtime["network_pass_budget_s"])
    deadline = time.monotonic() + budget
    box: dict[str, Any] = {}
    state: dict[str, Any] = {"work": work, "sha": v1["sha256"], "details": {}}

    def worker():
        try:
            box["result"] = _fetch_draftables(entries, cache, pool, box)
        except BaseException as exc:  # reported, never raised out of the run
            box["error"] = exc

    th = threading.Thread(target=worker, name="nhl-phase-b", daemon=True)
    th.start()
    th.join(timeout=budget)
    if "detail" in box:
        state["details"][str(entries.entries[0].contest_id)] = box["detail"]
    if th.is_alive():
        m["news"]["phase_b_summary"] = f"network pass exceeded {budget:.0f}s; v1 stays current"
        m["failed"].append("phase B: budget exceeded")
        return state
    if "error" in box:
        exc = box["error"]
        m["news"]["phase_b_summary"] = f"draftables unavailable ({type(exc).__name__}: {str(exc)[:120]}); v1 stays current"
        m["failed"].append("phase B: DK draftables unavailable")
        return state
    group_id, rec = box["result"]
    m["news"]["draft_group_id"] = group_id
    missing = [rid for rid, s in rec.rows.items() if s.obs_status is ObsStatus.MISSING]
    conflicted = rec.conflicted
    m["statuses"]["NEWS_STATE"] = (NewsState.PARTIAL if missing else NewsState.FULL).value
    out_people = {pool.by_role_id[rid].person_key for rid, s in rec.rows.items()
                  if s.participation is Participation.OUT or s.eligibility is Eligibility.DISABLED}
    new_rows = _person_rows(pool, out_people) - set(excluded_a)
    if new_rows:
        state["work"] = pool_without(work, new_rows)
        state["bank"] = None  # the Phase A bank may hold newly excluded rows
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
        return state
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        m["news"]["phase_b_summary"] = f"{len(new_rows)} new exclusion(s) found but no budget left to re-solve; v1 stays current"
        m["failed"].append("phase B: out of budget before re-solve")
        messages.append("REVIEW: newly OUT/DISABLED players remain in v1: " + ", ".join(_names(pool, new_rows)))
        return state
    work2 = pool_without(work, new_rows)
    obj2 = {k: v for k, v in objective.items() if k in work2.by_role_id}
    search = build_bank(work2, obj2, len(affected), runtime, seed=seed + 1, time_limit_s=max(1.0, remaining - 1.0))
    if not search.bank:
        m["news"]["phase_b_summary"] = f"re-solve after new exclusions failed ({search.detail}); v1 stays current"
        m["failed"].append("phase B: re-solve failed")
        messages.append("REVIEW: newly OUT/DISABLED players remain in v1: " + ", ".join(_names(pool, new_rows)))
        return state
    fixed = {e: v for e, v in a.by_entry.items() if e not in affected}
    a2 = assign(search.bank, entries, work2, pool.mode, caps, seed=seed, later_start_utc=starts, fixed=fixed)
    v2 = _export_and_publish(run, entries, pool, a2, slate_id, outputs_root, m, messages, phase="B", expect=v1["sha256"],
                             clock=clock, runtime=runtime)
    if v2 is None:
        m["news"]["phase_b_summary"] = "v2 failed its checks; v1 stays current"
        return state
    state["sha"] = v2["sha256"]
    _set_assignment_fields(m, a2, pool)
    if search.route != "milp" or any(r.kind == "REPEAT" for r in a2.relaxations) or not v2["public_replaced"]:
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["news"]["phase_b_summary"] = (f"{len(new_rows)} row(s) newly OUT/DISABLED ({', '.join(_names(pool, new_rows))}); "
                                    f"{len(affected)} entr{'y' if len(affected) == 1 else 'ies'} re-solved; v{v2['version']} published")
    m["worked"].append("phase B re-solved affected entries")
    return state


# -- provisional pass (C3) -------------------------------------------------------------------

def save_contest_details(run: RunDir, details: dict, m: dict, label: str) -> None:
    """Backlog B24: keep each DK contest detail that answered during the run (its prize table) in
    runs/<id>/contests/dk_contest_<id>.json, before lock, so settle never depends on a later fetch (DK answers 403
    from this machine at times). A file already saved is kept; sources.json labels each one. Never raises."""
    import json

    d = run.path / "contests"
    saved = m.setdefault("contests_saved", {})
    try:
        for det in details.values():
            cid = str(det.contest_id)
            p = d / f"dk_contest_{cid}.json"
            if p.exists():
                continue
            d.mkdir(exist_ok=True)
            p.write_text(json.dumps(det.raw, ensure_ascii=False), encoding="utf-8")
            saved[cid] = f"{label}, saved {_utc(datetime.now(timezone.utc))}"
        if saved:
            (d / "sources.json").write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        m.setdefault("messages", []).append(f"contest detail not saved in the run ({type(exc).__name__})")


def _fetch_contest_details(contest_ids, cache, budget_s: float) -> tuple[dict, list[str]]:
    """DK contest detail per id in a worker thread joined against one budget."""
    box: dict[str, Any] = {"details": {}, "errors": []}

    def worker():
        for cid in contest_ids:
            try:
                box["details"][cid] = dk_public.contest_detail(int(cid), cache=cache)
            except BaseException as exc:  # reported; the contest falls back to its family prior
                box["errors"].append(f"contest {cid}: {type(exc).__name__}: {str(exc)[:80]}")

    th = threading.Thread(target=worker, name="nhl-contest-detail", daemon=True)
    th.start()
    th.join(timeout=budget_s)
    errors = list(box["errors"])
    if th.is_alive():
        errors.append(f"contest detail pass exceeded {budget_s:.0f}s")
    return dict(box["details"]), errors


def _role_model(pool, after_b, st, offline, cache, clock, runtime, m, messages):
    """roles.merge then roles.apply_state exactly once on the run's ParamTable (backlog B9, B11): Daily Faceoff
    pages cache-first online, stored pages offline. The applied table replaces after_b["proj"] for the provisional
    and scenario passes; v1 (Phase A) keeps the plain C5 table because it never waits on anything. Returns the
    RoleModel, or None when there is no ParamTable (priors only) or the role state could not be built (reported)."""
    from nhl_dfs.build import swap_objective

    proj = after_b.get("proj")
    if not hasattr(proj, "persons"):
        return None
    try:
        rm = swap_objective.build_role_model(pool, after_b["work"], st, dk_rec=None, now=clock(), clock=clock,
                                             offline=offline, cache=cache,
                                             budget_s=float(runtime["network_pass_budget_s"]), table0=proj)
    except Exception as exc:  # the run continues on the C5 table, and says so
        messages.append(f"role state unavailable ({type(exc).__name__}: {str(exc)[:120]}); v2 and v3 use the C5 table")
        return None
    after_b["proj"] = rm.table
    m["news"]["roles_news_state"] = rm.news_state
    m["news"]["roles_warnings"] = rm.warnings[:20]
    m["news"]["roles_notes"] = rm.notes[:8]
    return rm


def _provisional_pass(run, entries, pool, after_b, st, starts, offline, runtime, caps, seed, slate_id,
                      outputs_root, cache, m, messages, out_root, rm=None, *, clock) -> dict:
    """Returns what the scenario pass (C8) builds on: contexts, fields, FIELD_CALIBRATION, the
    provisional assignment and the candidate bank (also when its own publish failed)."""
    import json

    from nhl_dfs.build import provisional as prov
    from nhl_dfs.build.state import atomic_write
    from nhl_dfs.models import contests as contests_mod
    from nhl_dfs.models import ownership
    from nhl_dfs.models import prefit as prefit_mod
    from nhl_dfs.models.projection import PriorProjection

    fam_cfg = contests_mod.load_contest_families()
    own_cfg = ownership.load_ownership_config()
    work: SalaryPool = after_b["work"]
    details = dict(after_b.get("details") or {})
    network: list[str] = []
    if not offline:
        ids = [cid for cid in dict.fromkeys(e.contest_id for e in entries.entries) if cid not in details]
        got, network = _fetch_contest_details(ids, cache, float(runtime["network_pass_budget_s"]))
        details.update(got)
        save_contest_details(run, got, m, "provisional pass contest detail")
    contexts = contests_mod.resolve(entries, details, fam_cfg)
    payout = contests_mod.overall_payout_source(contexts.values())

    # Prefit behind the one gate: historical labels count toward the floors, never around them.
    field_cal = FieldCalibration.PRIOR
    history = Path(out_root).resolve().parent / "data" / "standings" / "history"
    try:
        pf = prefit_mod.fit(history, cfg=own_cfg)
        mp = pf.by_mode[pool.mode]
        prefit_note = f"{mp.status}: {mp.reason}"
        if mp.status == "fitted" and mp.improves:
            own_cfg = {**own_cfg, "weights": {**own_cfg["weights"], **mp.weights}}
            field_cal = FieldCalibration.FITTED
    except prefit_mod.LabelFormatError as exc:
        prefit_note = f"error: {exc}"
        messages.append(f"prefit skipped: {exc}")

    statuses = {rid: p for rid, (p, _) in st.items() if rid in work.by_role_id}
    proj = after_b.get("proj") or PriorProjection(work)
    fb = prov.build_fields(work, proj, contexts, seed=seed, statuses=statuses, own_cfg=own_cfg)
    atomic_write(run.path / "field.json",
                 json.dumps(prov.field_summary(work, fb, contexts, model_status=proj.source().value), indent=2).encode("utf-8"))
    bank = after_b.get("bank")
    if not bank:
        bank = build_bank(work, objective_from(work, proj), len(entries.entries), runtime,
                          seed=seed + 2).bank
    if not bank:
        raise RuntimeError("no candidate bank for the provisional pass")
    sel_statuses = statuses
    if rm is not None:  # price participation once: the ranking haircut only where roles did not already price the absence
        from nhl_dfs.build.swap_objective import _absence_priced

        priced = {k for k, r in rm.roles.persons.items() if r.p_play < 1.0 and _absence_priced(k, r, rm.roles)}
        sel_statuses = {rid: (Participation.PLAYING if p is Participation.QUESTIONABLE and work.by_role_id[rid].person_key in priced
                              else p) for rid, p in statuses.items()}
    a_p, scored = prov.select(bank, proj, fb.marginals, contexts, entries, caps, fam_cfg, seed=seed, pool=work,
                              statuses=sel_statuses, later_start_utc=starts, own_cfg=own_cfg)

    evidence = {
        "MODEL_STATUS": proj.source().value,
        "PAYOUT_SOURCE": payout.value,
        "OUTCOME_CALIBRATION": OutcomeCalibration.UNVALIDATED.value,
        "FIELD_CALIBRATION": field_cal.value,
    }
    rows = []
    for e in entries.entries:
        s = scored[e.entry_id]
        ctx = contexts[e.contest_id]
        rows.append({
            "entry_id": e.entry_id,
            "contest_id": e.contest_id,
            "family": ctx.family,
            "PAYOUT_SOURCE": ctx.payout_source.value,
            "mean_pts": round(s.mean_tenths / 10.0, 1),
            "own_sum_pct": round(s.own_pct, 1),
            "dup_proxy": round(s.dup, 2),
            "field_dup_est": round(fb.marginals[e.contest_id].dup_counts.get(s.cand.key, 0.0), 1),
            "band_pts": round(s.band / 10.0, 1),
            "band_index": s.band_index,
            "dtd_players": sum(1 for r in s.cand.role_ids if statuses.get(r) is Participation.QUESTIONABLE),
        })
    fields = {fam: {"n_requested": f.requested, "n_draws": f.n, "degraded": f.degraded, "detail": f.detail,
                    "repeats": f.n - len(set(f.keys))} for fam, f in fb.fields.items()}
    m["provisional"] = {
        "label": f"PROVISIONAL: MODEL_STATUS={proj.source().value}; ownership and duplicate figures are uncalibrated",
        "evidence": evidence,
        "contests": [c.record() for c in contexts.values()],
        "fields": fields,
        "field_s": round(fb.elapsed_s, 3),
        "entries": rows,
        "prefit": prefit_note,
        "network": network,
        "version": None,
    }
    for fam, f in fields.items():
        if f["degraded"]:
            messages.append(f"field for {fam} is short: {f['n_draws']} of {f['n_requested']} draws; ownership is degraded")
    state = {"contexts": contexts, "fb": fb, "field_cal": field_cal, "assignment": a_p, "bank": bank,
             "own_cfg": own_cfg, "statuses": statuses}
    vp = _export_and_publish(run, entries, pool, a_p, slate_id, outputs_root, m, messages, phase="P",
                             expect=after_b["sha"], clock=clock, runtime=runtime)
    if vp is None:
        m["failed"].append("provisional pass: its file was not published; the previous version stays current")
        return state
    m["provisional"]["version"] = vp["version"]
    _set_assignment_fields(m, a_p, pool)
    m["statuses"].update(evidence)
    if (not vp["public_replaced"] or any(r.kind == "REPEAT" for r in a_p.relaxations)
            or any(f["degraded"] for f in fields.values())):
        m["statuses"]["DELIVERY_STATUS"] = DeliveryStatus.DEGRADED_REVIEW.value
    m["worked"].append(f"provisional pass published v{vp['version']} (PROVISIONAL, MODEL_STATUS={proj.source().value}, "
                       f"PAYOUT_SOURCE={payout.value})")
    return state
