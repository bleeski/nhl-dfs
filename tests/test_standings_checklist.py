"""scripts/standings_checklist.py: discovery, dating, classification and the HTML checklist.

Every test builds a temp tree and points the module's path constants at it, with a stub entry parser, so
nothing reads this machine's runs, outputs or standings.
"""

import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import standings_checklist as sc  # noqa: E402


def stub_read(path):
    """Stub parser. File format: first line `ENTRIES <mode>`, then `contest_id|name|fee|entry_id` lines."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("ENTRIES "):
        raise ValueError(f"{path}: not an entries file")
    rows = []
    for ln in lines[1:]:
        cid, name, fee, eid = ln.split("|")
        rows.append(SimpleNamespace(contest_id=cid, contest_name=name, fee=fee, entry_id=eid))
    return SimpleNamespace(entries=rows, mode=SimpleNamespace(value=lines[0].split()[1]))


def entries_text(*rows, mode="classic"):
    return "ENTRIES " + mode + "\n" + "\n".join("|".join(r) for r in rows) + "\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name, sub in (("REPO_ROOT", ""), ("RUNS_DIR", "runs"), ("OUTPUTS_DIR", "outputs"), ("ENTERED_DIR", "data/entered"),
                      ("FIXTURES_DIR", "tests/fixtures"), ("REAL_FIXTURES_DIR", "tests/fixtures/real"),
                      ("STANDINGS_DIR", "data/standings"), ("REVIEWS_DIR", "reviews")):
        monkeypatch.setattr(sc, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setenv("NHL_DFS_LEDGER_ROOT", str(tmp_path / "ledger"))
    monkeypatch.setattr(sc, "read_entries", stub_read)
    (tmp_path / "data/standings/inbox").mkdir(parents=True)
    return tmp_path


def put_run(root, run_id, rows, *, slate_id=None, bind=True, mode="classic"):
    inputs = root / "runs" / run_id / "inputs"
    inputs.mkdir(parents=True)
    f = inputs / "DKEntries.csv"
    f.write_text(entries_text(*rows, mode=mode), encoding="utf-8")
    (inputs / "DKSalaries.csv").write_text("Position,Name\n", encoding="utf-8")
    manifest = {"run_id": run_id, "entries_sha256": hashlib.sha256(f.read_bytes()).hexdigest() if bind else "0" * 64}
    if slate_id:
        manifest["slate_id"] = slate_id
    (root / "runs" / run_id / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return f


def by_id(report):
    return {c["id"]: c for c in report["contests"]}


ROW_A = ("1001", "NHL Alpha", "$1", "e1")
ROW_B = ("1002", "NHL Beta", "$5", "e2")


def test_snapshot_and_loose_discovery_and_salary_skipped(env):
    put_run(env, "20260930-120000-classic", [ROW_A], slate_id="classic-20260930-aaaaaaaaaa")
    (env / "DKEntries (1).csv").write_text(entries_text(ROW_B), encoding="utf-8")  # repo root = loose
    r = sc.build_report()
    c = by_id(r)
    assert c["1001"]["loose_only"] is False
    assert c["1002"]["loose_only"] is True and r["loose_only"] == ["1002"]
    skipped = [s["file"] for s in r["skipped_files"]]
    assert "runs/20260930-120000-classic/inputs/DKSalaries.csv" in skipped  # a salary file is skipped, not guessed at


def test_hash_mismatch_makes_a_run_input_loose(env):
    put_run(env, "20260930-120000-classic", [ROW_A], bind=False)
    assert by_id(sc.build_report())["1001"]["loose_only"] is True


def test_dating_uses_folder_name_before_mtime(env):
    f = put_run(env, "20260930-010000-classic", [ROW_A], slate_id="classic-20260929-aaaaaaaaaa")
    import os
    os.utime(f, (0, 0))  # a fresh checkout gives every file one mtime; the slate id must win
    c = by_id(sc.build_report())["1001"]
    assert c["evidence_date"] == "2026-09-29" and "slate" in c["evidence_basis"]


def test_run_id_is_converted_from_utc_to_chicago(env):
    put_run(env, "20260930-013521-showdown", [ROW_A])  # no slate id: 01:35 UTC is the evening before in Chicago
    c = by_id(sc.build_report())["1001"]
    assert c["evidence_date"] == "2026-09-29"


def test_mtime_is_the_fallback_for_loose_files(env):
    f = env / "DKEntries.csv"
    f.write_text(entries_text(ROW_A), encoding="utf-8")
    import os
    ts = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc).timestamp()
    os.utime(f, (ts, ts))
    assert by_id(sc.build_report())["1001"]["evidence_date"] == "2026-09-20"


def test_oldest_first_unknown_last():
    def c(i, day):
        return {"id": i, "status": "awaiting", "evidence_date": day}
    report = {"contests": [c("3", "2026-09-30"), c("9", None), c("1", "2026-09-01"), c("2", "2026-09-15")]}
    assert [d for d, _ in sc.awaiting_groups(report)] == ["2026-09-01", "2026-09-15", "2026-09-30", None]


def test_filed_export_removes_contest_from_awaiting(env):
    put_run(env, "20260930-120000-classic", [ROW_A, ROW_B])
    assert sc.build_report()["counts"]["awaiting"] == 2
    d = env / "data/standings/inbox/2026-09-30"
    d.mkdir()
    (d / "contest-standings-1001.zip").write_bytes(b"x")  # any file with the id counts, even in a dated subfolder
    r = sc.build_report()
    assert by_id(r)["1001"]["status"] == "filed" and by_id(r)["1002"]["status"] == "awaiting"
    assert r["counts"]["awaiting"] == 1
    page = sc.render_html(r, date(2026, 10, 2))
    assert re.findall(r'<li class="row" data-id="(\d+)"', page) == ["1002"]


def test_unmatched_inbox_file_is_named(env):
    put_run(env, "20260930-120000-classic", [ROW_A])
    inbox = env / "data/standings/inbox"
    (inbox / "contest-standings-9999999.csv").write_text("x", encoding="utf-8")
    (inbox / "winnings.csv").write_text("contest_id,entry_id,winnings_usd\n", encoding="utf-8")
    r = sc.build_report()
    assert r["unmatched_inbox_files"] == ["data/standings/inbox/contest-standings-9999999.csv"]


def test_settled_beats_filed_and_reads_graded_json(env):
    put_run(env, "20260930-120000-classic", [ROW_A])
    (env / "ledger").mkdir()
    (env / "ledger/graded.json").write_text(json.dumps({"r": {"contests": {"1001": {}}}}), encoding="utf-8")
    (env / "data/standings/inbox/contest-standings-1001.zip").write_bytes(b"x")
    assert by_id(sc.build_report())["1001"]["status"] == "settled"


def test_duplicate_disposition_refused_and_recorded(env):
    put_run(env, "20260930-120000-classic", [ROW_A])
    sc.mark("1001", "unrecoverable", "export aged out")
    with pytest.raises(ValueError, match="already has a disposition"):
        sc.mark("1001", "placeholder", "again")
    with pytest.raises(ValueError):
        sc.mark("10x", "placeholder", "bad id")
    c = by_id(sc.build_report())["1001"]
    assert c["status"] == "unrecoverable" and c["disposition"]["reason"] == "export aged out"
    assert sc.main(["--mark-placeholder", "1001", "no"]) == 2  # the CLI refuses too


def test_synthetic_fixture_ids_are_excluded_not_owed(env):
    put_run(env, "20260930-120000-classic", [("297", "NHL Synthetic", "$1", "e9")])
    fx = env / "tests/fixtures/mini"
    fx.mkdir(parents=True)
    (fx / "DKEntries.csv").write_text(entries_text(("297", "NHL Synthetic", "$1", "e9")), encoding="utf-8")
    r = sc.build_report()
    assert by_id(r)["297"]["status"] == "fixture" and r["counts"]["awaiting"] == 0 and r["counts"]["total"] == 0


def test_names_are_html_escaped(env):
    put_run(env, "20260930-120000-classic", [("1001", '<img src=x onerror=alert(1)> "q"', "$1", "e1")])
    page = sc.render_html(sc.build_report(), date(2026, 10, 2))
    assert "<img" not in page and "&lt;img" in page and "&quot;q&quot;" in page


def test_every_awaiting_row_has_a_checkbox_and_its_own_link(env):
    put_run(env, "20260930-120000-classic", [ROW_A, ROW_B])
    r = sc.build_report(template="https://example.test/x/{contest_id}")
    page = sc.render_html(r, date(2026, 10, 2))
    rows = re.findall(r'<li class="row" data-id="(\d+)">(.*?)</li>', page, re.S)
    assert [i for i, _ in rows] == ["1001", "1002"]
    for cid, inner in rows:
        assert 'type="checkbox"' in inner
        assert f'href="https://example.test/x/{cid}" target="_blank" rel="noopener"' in inner
    assert "0 / 2 pulled" in page and 'viewport' in page and "<script src" not in page


def test_json_writes_nothing(env, capsys):
    put_run(env, "20260930-120000-classic", [ROW_A])
    before = sorted(p.relative_to(env).as_posix() for p in env.rglob("*"))
    assert sc.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["awaiting"] == 1
    assert sorted(p.relative_to(env).as_posix() for p in env.rglob("*")) == before


def test_json_cannot_be_combined_with_a_mark(env):
    with pytest.raises(SystemExit) as exc:
        sc.main(["--json", "--mark-placeholder", "1001", "x"])
    assert exc.value.code == 2
    assert not (env / "data/standings/dispositions.json").exists()


def test_writes_dated_stable_and_markdown(env):
    put_run(env, "20260930-120000-classic", [ROW_A])
    paths = sc.write_outputs(sc.build_report(), date(2026, 10, 2))
    names = sorted(p.name for p in paths)
    assert names == ["CONTESTS_AWAITING_STANDINGS.html", "CONTESTS_AWAITING_STANDINGS.md", "standings_pulls_2026-10-02.html"]
    d = env / "data/standings"
    assert (d / "CONTESTS_AWAITING_STANDINGS.html").read_bytes() == (d / "standings_pulls_2026-10-02.html").read_bytes()
    assert chr(0x2014) not in (d / "CONTESTS_AWAITING_STANDINGS.md").read_text(encoding="utf-8")  # no em dashes
    assert not any(p.is_file() for p in (d / "inbox").rglob("*"))  # the inbox is never written to


# One test per extra intake location: a contest found there is found and owed.

def test_location_outputs_folder(env):
    f = env / "outputs/classic-20260930-aaaaaaaaaa/DKEntries.csv"
    f.parent.mkdir(parents=True)
    f.write_text(entries_text(ROW_A), encoding="utf-8")
    (f.parent / "published.json").write_text(json.dumps({"sha256": hashlib.sha256(f.read_bytes()).hexdigest()}), encoding="utf-8")
    c = by_id(sc.build_report())["1001"]
    assert c["status"] == "awaiting" and c["loose_only"] is False and c["evidence_date"] == "2026-09-30"


def test_location_repo_root(env):
    (env / "DKEntries_102.csv").write_text(entries_text(ROW_A), encoding="utf-8")
    c = by_id(sc.build_report())["1001"]
    assert c["status"] == "awaiting" and c["loose_only"] is True


def test_location_real_fixtures(env):
    f = env / "tests/fixtures/real/2026-09-29/classic/DKEntries.csv"
    f.parent.mkdir(parents=True)
    f.write_text(entries_text(ROW_A), encoding="utf-8")
    assert by_id(sc.build_report())["1001"]["status"] == "awaiting"


def test_location_non_run_folder_under_runs(env):
    f = env / "runs/c0b-demo/classic_DKEntries.csv"
    f.parent.mkdir(parents=True)
    f.write_text(entries_text(ROW_A), encoding="utf-8")
    assert by_id(sc.build_report())["1001"]["loose_only"] is True


def test_location_also_dir(env, tmp_path_factory):
    extra = tmp_path_factory.mktemp("downloads")
    (extra / "DKEntries (3).csv").write_text(entries_text(ROW_A), encoding="utf-8")
    c = by_id(sc.build_report(also=[extra]))["1001"]
    assert c["status"] == "awaiting" and c["loose_only"] is True


def test_scratch_run_folders_are_counted_not_scanned(env):
    f = env / "runs/_rehearsal/inputs/DKEntries.csv"
    f.parent.mkdir(parents=True)
    f.write_text(entries_text(ROW_A), encoding="utf-8")
    r = sc.build_report()
    assert r["scratch_run_folders_not_scanned"] == 1 and r["contests"] == []


def test_unseen_slate_cited_in_reviews_is_named(env):
    (env / "reviews").mkdir()
    (env / "reviews/r.md").write_text("outputs/classic-20261001-a3565a960f/DKEntries.csv was sent", encoding="utf-8")
    put_run(env, "20260930-120000-classic", [ROW_A], slate_id="classic-20260930-aaaaaaaaaa")
    r = sc.build_report()
    assert [s["slate_id"] for s in r["unseen_slates"]] == ["classic-20261001-a3565a960f"]


def test_blank_winnings_are_reported_as_owed(env):
    d = env / "data/standings/inbox/2026-09-29"
    d.mkdir()
    (d / "winnings.csv").write_text("contest_id,contest_name,entry_id,rank,fee,winnings_usd\n"
                                    "1001,n,e1,5,1.00,\n1002,n,e2,6,1.00,3.00\n", encoding="utf-8")
    assert [(w["contest_id"], w["entry_id"]) for w in sc.build_report()["winnings_blank"]] == [("1001", "e1")]


def test_saved_ticks_are_scoped_to_one_generation(env):
    put_run(env, "20260930-120000-classic", [ROW_A])
    r = sc.build_report()
    a = sc.render_html(r, date(2026, 10, 2), "20261002T150000Z")
    b = sc.render_html(r, date(2026, 10, 2), "20261002T160000Z")
    assert "standings_pulls_ticks_20261002T150000Z" in a and "standings_pulls_ticks_20261002T160000Z" in b
    assert "%STAMP%" not in a
    sc.write_outputs(r, date(2026, 10, 2), "20261002T150000Z")
    d = env / "data/standings"  # the dated and stable copies are one generation, so they share ticks
    assert (d / "CONTESTS_AWAITING_STANDINGS.html").read_bytes() == (d / "standings_pulls_2026-10-02.html").read_bytes()


def test_entered_record_is_listed_as_a_record_and_clears_the_unseen_slate(env):
    (env / "reviews").mkdir()
    (env / "reviews/r.md").write_text("outputs/classic-20261003-42550723c3/DKEntries.csv was sent", encoding="utf-8")
    (env / "data/entered").mkdir(parents=True)
    (env / "data/entered/classic-20261003-42550723c3.csv").write_text(entries_text(ROW_A), encoding="utf-8")
    r = sc.build_report()
    c = by_id(r)["1001"]
    assert c["status"] == "awaiting" and c["loose_only"] is False  # tracked, so it is not a hand-editable loose file
    assert c["evidence_date"] == "2026-10-03" and "file name" in c["evidence_basis"]
    assert c["sources"] == ["data/entered/classic-20261003-42550723c3.csv"]
    assert r["unseen_slates"] == [] and r["scan"]["data/entered"]["parsed"] == 1


DK_HEADER = "Entry ID,Contest Name,Contest ID,Entry Fee,C,C,W,W,W,D,D,G,UTIL,,Instructions"
POOL_ROW = ",,,,,,,,,,,,,,C,Connor McDavid (44364828),Connor McDavid,44364828,C/UTIL,9200,SEA@EDM 10/03/2026 07:00PM ET,EDM,17.9"


def dk_entries(tmp_path, folder="classic-20261003-42550723c3"):
    cells = ",".join(f"Player {i} ({1000 + i})" for i in range(9))
    lines = [DK_HEADER,
             f"7001,NHL Alpha,1001,$1,{cells},,1. Column A lists all of your contest entries",
             f"7002,NHL Beta,1002,$0.25,{cells},,2. Your current lineup is listed next to each entry",
             POOL_ROW]
    d = tmp_path / "src" / folder
    d.mkdir(parents=True)
    f = d / "DKEntries.csv"
    f.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
    return f


def test_save_entered_writes_a_minimized_record_that_reads_back(env, monkeypatch):
    from nhl_dfs.intake.entries import read_entries
    monkeypatch.setattr(sc, "read_entries", read_entries)  # the real reader, not the stub
    dest, n_entries, n_contests = sc.save_entered(dk_entries(env))
    assert dest == env / "data/entered/classic-20261003-42550723c3.csv" and (n_entries, n_contests) == (2, 2)
    text = dest.read_text(encoding="utf-8")
    assert "Connor McDavid" not in text and "Instructions" not in text and "Column A" not in text  # no pool, no notes
    assert "7001,NHL Alpha,1001,$1,Player 0 (1000)" in text
    assert [e.contest_id for e in read_entries(dest).entries] == ["1001", "1002"]
    assert by_id(sc.build_report())["1002"]["sources"] == ["data/entered/classic-20261003-42550723c3.csv"]


def test_save_entered_needs_a_slate_id_and_takes_one_by_flag(env, monkeypatch):
    from nhl_dfs.intake.entries import read_entries
    monkeypatch.setattr(sc, "read_entries", read_entries)
    f = dk_entries(env, folder="Downloads")
    with pytest.raises(ValueError, match="--slate"):
        sc.save_entered(f)
    dest, _, _ = sc.save_entered(f, "classic-20261001-a3565a960f")
    assert dest.name == "classic-20261001-a3565a960f.csv"
    assert sc.main(["--save-entered", str(f), "--slate", "bogus"]) == 2  # refused, not written
    assert not (env / "data/entered/bogus.csv").exists()
