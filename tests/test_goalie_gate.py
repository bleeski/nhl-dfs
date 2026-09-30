"""The goalie gate (backlog B17) and the goalie table, on the C2c late-swap fixtures. Every clock is pinned.

The Daily Faceoff inputs are injected at goalies.gather (so roles.merge runs for real); the synthetic fixture teams
are mapped to themselves in the verified team-code table for these tests only."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TESTS
from nhl_dfs.build import goalies, late_swap, milp, refresh
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.sources.dailyfaceoff import GoalieReport
from nhl_dfs.intake.entries import read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models import roles

pytestmark = pytest.mark.c9

LS = TESTS / "fixtures" / "late_swap"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
STARTS = {"AAA": G1, "BBB": G1, "CCC": datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc),
          "DDD": datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc), "EEE": datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc),
          "FFF": datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)}
OPP = {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC", "EEE": "FFF", "FFF": "EEE"}


@pytest.fixture(autouse=True)
def _fake_codes(monkeypatch):
    codes = {t.lower(): {"nhl": t, "dk": t, "name": t} for t in STARTS}
    monkeypatch.setattr(roles, "team_codes", lambda *a, **k: codes)


def report(team, name, *, created, state=GoalieState.CONFIRMED, starts=STARTS):
    return GoalieReport(team, None, name, None, state, state.value.title(), True, starts[team].date(), starts[team],
                        OPP[team], "test page", "https://www.dailyfaceoff.com/starting-goalies", created, "next_data")


def inject(monkeypatch, reports, fetched=None):
    def fake(pool, cache, *, offline, budget_s, now):
        f = fetched or now - timedelta(minutes=3)
        return {}, list(reports), "next_data", [], {"source": "stored", "fetched_utc": goalies._iso(f),
                                                     "age_min": round((now - f).total_seconds() / 60, 1)}
    monkeypatch.setattr(goalies, "gather", fake)


def base(tmp_path, kind="classic", salary=None):
    d = LS / kind
    r = run_slate(salary or d / "DKSalaries.csv", d / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def swap(tmp_path, b, current, *, as_of, **kw):
    return late_swap.run(b.run.run_id, current, runs_root=tmp_path / "runs", as_of=as_of, offline=True, **kw)


def canon(path, pool):
    return goalies.lineups_of(path, pool)


def without_entry(src, dst, *entry_ids):
    lines = src.read_bytes().split(b"\r\n")
    dst.write_bytes(b"\r\n".join(x for x in lines if not any(x.lstrip(b"\xef\xbb\xbf").startswith(e.encode())
                                                              for e in entry_ids)))
    return dst


def clean_current(tmp_path):
    """The C2c current export without its two entries that need a repair anyway (7100000001 holds Echo C1, OUT in
    the salary file; 7100000005 is blank): what is left changes only if the goalie gate acts."""
    return without_entry(LS / "classic" / "DKEntries.current.csv", tmp_path / "cur.csv", "7100000001", "7100000005")


def test_a_confirmed_other_goalie_makes_the_entry_a_repair_target(tmp_path, monkeypatch, capsys):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    inject(monkeypatch, [report("DDD", "Delta G1", created=BEFORE - timedelta(hours=1))])
    r = swap(tmp_path, b, cur, as_of=BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    before, after = canon(cur, pool), canon(r.run.version_file(1), pool)
    changed = {e for e in before if before[e] != after[e]}
    assert changed == {"7100000002"}  # only the entry with Delta G2 (DDD) is touched
    diff = [k for k in range(9) if before["7100000002"][k] != after["7100000002"][k]]
    assert 8 in diff and len(diff) <= 2  # the goalie cell, plus at most one cell when salary forces it
    assert "Delta G2" not in {pool.by_role_id[x].name for x in after["7100000002"]}
    g = r.manifest["goalies"]
    assert g["GOALIE_GATE"] == "CLEAR" and "Delta G2" in r.manifest["news"]["goalie_gate_exclusions"]
    assert {x["entry_id"] for x in g["rows"]} == set(before)
    # the table ends the CLI output and is in RUN_NOTES
    from nhl_dfs.cli import _print_result

    _print_result(r)
    out = capsys.readouterr().out.strip().splitlines()
    assert any(line.startswith("GOALIE_GATE=CLEAR") for line in out) and out[-1].startswith("  ")
    assert any("GOALIES (" in line for line in out)
    notes = r.notes_path.read_text(encoding="utf-8")
    assert "## Goalies" in notes and "| Entry | Slot | Goalie |" in notes


def test_a_pinned_non_starting_goalie_is_reported_and_unchanged(tmp_path, monkeypatch):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    inject(monkeypatch, [report("BBB", "Bravo G1", created=G1 - timedelta(hours=2))])
    r = swap(tmp_path, b, cur, as_of=G1 + timedelta(minutes=10))  # AAA@BBB has started: entry ...003's goalie is pinned
    assert r.statuses["FILE_VALID"] == "TRUE"
    after = read_entries(r.run.version_file(1))
    before = read_entries(cur)
    e_before = next(e for e in before.entries if e.entry_id == "7100000003")
    e_after = next(e for e in after.entries if e.entry_id == "7100000003")
    assert e_before.cells == e_after.cells  # byte-identical cells
    assert any(m.startswith("PINNED GOALIE NOT STARTING: entry 7100000003 Bravo G2") for m in r.messages)
    assert r.manifest["goalies"]["GOALIE_GATE"] == "NOT_STARTING"
    assert r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    row = next(x for x in r.manifest["goalies"]["rows"] if x["entry_id"] == "7100000003")
    assert row["status"] == "NOT STARTING" and row["pinned"] and row["starter"] == "Bravo G1"
    assert canon(r.run.version_file(1), pool)["7100000003"] == canon(cur, pool)["7100000003"]


def test_nothing_to_repair_still_returns_the_input_bytes(tmp_path, monkeypatch):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    # confirms the goalie already in the lineup; a report created after the clock is ignored (no leak: had it been
    # seen, Echo G1 in entry 7100000004 would be NOT STARTING and repaired)
    inject(monkeypatch, [report("DDD", "Delta G2", created=BEFORE - timedelta(hours=1)),
                         report("EEE", "Echo G2", created=BEFORE + timedelta(hours=1))])
    r = swap(tmp_path, b, cur, as_of=BEFORE)
    assert r.manifest["changed_cells"] == [], (r.manifest["changed_cells"], r.manifest["entries"], r.messages)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert r.manifest["objective"]["kind"] == "not needed"
    rows = {x["entry_id"]: x for x in r.manifest["goalies"]["rows"]}
    assert rows["7100000002"]["status"] == "CONFIRMED"
    assert rows["7100000004"]["status"] == "UNKNOWN"  # Echo G2's report was created after the clock
    assert any("created after" in p for p in r.manifest["goalies"]["problems"])


def test_override_and_daily_faceoff_disagree_is_conflicted_and_not_repaired(tmp_path, monkeypatch):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    g2 = next(r for r in pool.rows if r.name == "Delta G2")
    news = b.run.path / "news"
    news.mkdir(exist_ok=True)
    (news / "accepted_overrides.json").write_text(json.dumps([{
        "role_id": g2.role_id, "nhl_id": None, "game_id": None, "field": "goalie_start", "old": False, "new": True,
        "effective_utc": (BEFORE - timedelta(hours=2)).isoformat(), "expiry_utc": (BEFORE + timedelta(hours=12)).isoformat(),
        "source_url": "https://example.test/beat-writer", "claim": "starter", "confidence": 0.9}]), encoding="utf-8")
    inject(monkeypatch, [report("DDD", "Delta G1", created=BEFORE - timedelta(hours=1))])
    r = swap(tmp_path, b, cur, as_of=BEFORE)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()  # neither goalie is removed automatically
    row = next(x for x in r.manifest["goalies"]["rows"] if x["entry_id"] == "7100000002")
    assert row["status"] == "CONFLICTED" and r.manifest["goalies"]["GOALIE_GATE"] == "CONFLICTED"


def test_no_goalie_news_is_unknown_never_expected(tmp_path, monkeypatch):
    b = base(tmp_path)
    cur = clean_current(tmp_path)

    def none(pool, cache, *, offline, budget_s, now):
        return {}, [], "none", ["goalie page unusable (SourceUnavailable)"], {"source": "none", "fetched_utc": None,
                                                                              "age_min": None}
    monkeypatch.setattr(goalies, "gather", none)
    r = swap(tmp_path, b, cur, as_of=BEFORE)
    g = r.manifest["goalies"]
    assert g["GOALIE_GATE"] == "NO_NEWS" and {x["status"] for x in g["rows"]} == {"UNKNOWN"}
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()


def test_refresh_repairs_a_non_starting_goalie_in_the_delivered_file(tmp_path, monkeypatch):
    b = base(tmp_path)
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    lu = canon(b.run.version_file(b.run.current_version()), pool)
    eid, cells = next(iter(lu.items()))
    g = next(pool.by_role_id[x] for x in cells if pool.by_role_id[x].is_goalie)
    other = next(r for r in pool.rows if r.is_goalie and r.team == g.team and r.person_key != g.person_key)
    inject(monkeypatch, [report(g.team, other.name, created=BEFORE - timedelta(hours=1))])
    r = refresh.run(b.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=BEFORE, objective="baseline")
    after = canon(r.run.version_file(1), pool)
    assert all(g.name not in {pool.by_role_id[x].name for x in cells_} for cells_ in after.values())
    assert r.manifest["goalies"]["GOALIE_GATE"] == "CLEAR"


def _showdown_with_second_goalie(tmp_path):
    """showdown2 plus a second AAA goalie (so a confirmation can bench Alpha G1), and a current export whose entry
    7200000001 has Alpha G1 at CPT (built by the MILP, spliced by the engine's own byte splicer)."""
    src = (LS / "showdown2" / "DKSalaries.csv").read_bytes()
    extra = ("G,Alpha G2 (82000091),Alpha G2,82000091,CPT,9000,AAA@BBB 10/15/2026 07:00PM ET,AAA,6.0,,\r\n"
             "G,Alpha G2 (82000092),Alpha G2,82000092,FLEX,6000,AAA@BBB 10/15/2026 07:00PM ET,AAA,6.0,,\r\n").encode()
    body = src if src.endswith(b"\r\n") else src + b"\r\n"
    sal = tmp_path / "DKSalaries.csv"
    sal.write_bytes(body + extra)
    pool = read_salary(sal)
    cur_in = read_entries(LS / "showdown2" / "DKEntries.current.csv")
    obj = {r.role_id: r.salary / 1000.0 for r in pool.rows}
    res = milp.solve_lineup(pool, Mode.SHOWDOWN, obj, locked={0: "82000001"}, exclude=frozenset({"82000002"}))
    assert res.lineup is not None
    before = late_swap._canonical(cur_in, next(e for e in cur_in.entries if e.entry_id == "7200000001"))
    changes = {"7200000001": {k: rid for k, rid in enumerate(res.lineup) if rid != before[k]}}
    cur = tmp_path / "cur.csv"
    late_swap.splice_cells(cur_in, pool, changes, cur)
    return sal, pool, cur


def test_showdown_captain_goalie_not_starting_is_repaired(tmp_path, monkeypatch):
    sal, pool, cur = _showdown_with_second_goalie(tmp_path)
    b = base(tmp_path, "showdown2", salary=sal)
    inject(monkeypatch, [report("AAA", "Alpha G2", created=BEFORE - timedelta(hours=1))])
    r = swap(tmp_path, b, cur, as_of=BEFORE, salary_path=sal)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages
    before, after = canon(cur, pool), canon(r.run.version_file(1), pool)
    assert pool.by_role_id[before["7200000001"][0]].name == "Alpha G1"
    assert pool.by_role_id[after["7200000001"][0]].name != "Alpha G1"
    assert all("Alpha G1" not in {pool.by_role_id[x].name for x in lu} for lu in after.values())
    rows = r.manifest["goalies"]["rows"]
    assert r.manifest["goalies"]["GOALIE_GATE"] == "CLEAR" and all(x["goalie"] != "Alpha G1" for x in rows)
