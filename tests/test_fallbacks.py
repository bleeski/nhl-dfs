"""Fallback routes of the baseline run (plan section 10, ladder steps 3 and 4)."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import HTTP, mini_pair, real_pair
from nhl_dfs.build import candidates as cand_mod
from nhl_dfs.build import feasible
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.statuses import FeasibleStatus
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary

pytestmark = pytest.mark.c2b

BEFORE = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)


def _run(tmp_path, pair, **kw):
    kw.setdefault("offline", True)
    kw.setdefault("clock", lambda: BEFORE)
    return run_slate(*pair, out_root=tmp_path / "runs", **kw)


def _lineups(path) -> dict[str, set[str]]:
    return {e.entry_id: {cell_role_id(c) for c in e.cells} for e in read_entries(path).entries}


def test_solver_import_failure_routes_to_find_one_and_still_publishes(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    r = _run(tmp_path, mini_pair("classic"))
    assert r.manifest["search_route"] == "feasible" and "solver unavailable" in r.manifest["search_detail"]
    assert r.statuses["FILE_VALID"] == "TRUE" and r.statuses["SEARCH_STATUS"] == "FEASIBLE"
    assert r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    assert r.public_path.exists()


def test_empty_candidate_bank_routes_to_find_one(tmp_path, monkeypatch):
    monkeypatch.setattr(cand_mod, "generate", lambda *a, **k: [])
    r = _run(tmp_path, mini_pair("showdown"))
    assert r.manifest["search_route"] == "feasible" and "empty" in r.manifest["search_detail"]
    assert r.statuses["FILE_VALID"] == "TRUE" and r.public_path.exists()
    kinds = {x["kind"] for x in r.manifest["relaxations"]}
    assert kinds <= {"OVERLAP", "EXPOSURE", "REPEAT"}


def test_proven_infeasible_pool_reports_infeasible_and_publishes_nothing(tmp_path):
    salary, entries = mini_pair("classic")
    pool = read_salary(salary)
    goalies = {r.role_id for r in pool.rows if r.is_goalie}
    raw = Path(salary).read_bytes().decode("utf-8-sig")
    lines = raw.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        parts = line.rstrip("\r\n").split(",")
        if len(parts) > 4 and parts[3] in goalies:
            parts[-2] = "OUT"
        out.append(",".join(parts) + line[len(line.rstrip("\r\n")):])
    edited = tmp_path / "DKSalaries.csv"
    edited.write_bytes(("﻿" + "".join(out)).encode("utf-8"))
    r = _run(tmp_path, (edited, entries))
    assert r.statuses["SEARCH_STATUS"] == "INFEASIBLE" and r.statuses["FILE_VALID"] == "FALSE"
    assert r.statuses["DELIVERY_STATUS"] == "FAILED" and r.public_path is None
    assert any("INFEASIBLE" in msg and "scope" in msg for msg in r.messages)


def test_timeout_twice_is_error_not_infeasible(tmp_path, monkeypatch):
    calls = []

    def slow(*a, **k):
        calls.append(k.get("budget_s"))
        return feasible.FeasibleResult(FeasibleStatus.TIMEOUT, None, 10, k.get("budget_s", 0))

    monkeypatch.setattr(cand_mod, "generate", lambda *a, **k: [])
    monkeypatch.setattr(feasible, "find_one", slow)
    r = _run(tmp_path, mini_pair("classic"))
    assert calls == [2.0, 10.0]  # first try, then one retry with the larger budget
    assert r.statuses["SEARCH_STATUS"] == "ERROR" and r.public_path is None
    assert "not proof of infeasibility" in r.manifest["search_detail"]


def test_phase_b_http_outage_leaves_v1_current_with_news_none(tmp_path, make_cache):
    # contest detail answers; draftables returns DK's 403 page, as it does from Ben's machine
    cache, transport = make_cache([
        ("contests/v1/contests/196048725", 200, (HTTP / "dk_contest_196048725.json").read_bytes()),
        ("draftables", 403, (HTTP / "dk_draftables_403.html").read_bytes()),
    ])
    r = _run(tmp_path, real_pair("showdown", "2026-09-29"), offline=False, cache=cache)
    assert transport.calls, "phase B should have tried the network"
    assert r.statuses["NEWS_STATE"] == "NONE" and r.statuses["FILE_VALID"] == "TRUE"
    assert r.run.current_version() == 1 and [v["version"] for v in r.manifest["versions"]] == [1]
    assert "unavailable" in r.manifest["news"]["phase_b_summary"]


def test_phase_b_budget_is_enforced(tmp_path, monkeypatch):
    def hang(*a, **k):
        time.sleep(5)

    runtime = run_mod.load_runtime_config()
    runtime["network_pass_budget_s"] = 0.3
    monkeypatch.setattr(run_mod, "_fetch_draftables", hang)
    t0 = time.perf_counter()
    r = _run(tmp_path, mini_pair("classic"), offline=False, runtime=runtime)
    assert r.manifest["phase_timings"]["phase_b_s"] < 2.0 and time.perf_counter() - t0 < 5.0
    assert r.run.current_version() == 1 and r.statuses["NEWS_STATE"] == "NONE"
    assert "exceeded" in r.manifest["news"]["phase_b_summary"]


def test_phase_b_new_out_players_resolve_only_affected_entries(tmp_path, make_cache):
    salary, entries = real_pair("showdown", "2026-09-29")
    data = json.loads((HTTP / "dk_draftables_153983.json").read_bytes())
    for d in data["draftables"]:
        d["status"] = "OUT"  # every person in the recorded (trimmed) response is now out
    out_ids = {str(d["draftableId"]) for d in data["draftables"]}
    cache, _ = make_cache([
        ("contests/v1/contests/196048725", 200, (HTTP / "dk_contest_196048725.json").read_bytes()),
        ("draftables", 200, json.dumps(data).encode()),
    ])
    r = _run(tmp_path, (salary, entries), offline=False, cache=cache)
    assert r.statuses["NEWS_STATE"] == "PARTIAL"  # the trimmed response lacks most rows
    assert [v["version"] for v in r.manifest["versions"]] == [1, 2] and r.run.current_version() == 2
    v1, v2 = _lineups(r.run.version_file(1)), _lineups(r.run.version_file(2))
    pool = read_salary(salary)
    out_people = {pool.by_role_id[i].person_key for i in out_ids if i in pool.by_role_id}
    affected = {e for e, ids in v1.items() if {pool.by_role_id[i].person_key for i in ids} & out_people}
    assert affected, "fixture choice should hit at least one v1 lineup"
    for e, ids in v2.items():
        assert not {pool.by_role_id[i].person_key for i in ids} & out_people
        if e not in affected:
            assert ids == v1[e]
    assert r.public_path.read_bytes() == r.run.version_file(2).read_bytes()
    assert r.statuses["FILE_VALID"] == "TRUE"
