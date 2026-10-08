import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.c0a

REPO_ROOT = Path(__file__).resolve().parent.parent
NEXT_CHUNK = REPO_ROOT / "tools" / "next_chunk.py"

TRACKER = """# Build status

Tracker for tests.

## Chunks

| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |
|---|---|---|---|---|---|---|---|
| A0 | TODO | | | | | | first chunk |
| A1 | BLOCKED | A0 | | | | | needs a fixture file; flip to TODO when in place |
| A2 | TODO | A0 | | | | | |

## Open [BEN] flags

| # | Flag | Default in force | Where it lands |
|---|---|---|---|
| 1 | Some question with unicode: le 5, ge 3 | Assumed none | nowhere |
"""

CHUNKS_YAML = """
version: 2
chunks:
  - id: A0
    title: First chunk
    depends: []
    marker: a0
    checks:
      - python -c "raise SystemExit(0)"
  - id: A1
    title: Second chunk, needs a file
    depends: [A0]
    marker: a1
    requires_files:
      - inbox/*.csv
    checks:
      - python -c "raise SystemExit(0)"
  - id: A2
    title: Third chunk
    depends: [A0]
    marker: a2
    checks:
      - python -c "raise SystemExit(1)"
"""


@pytest.fixture()
def fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "BUILD_STATUS.md").write_text(TRACKER, encoding="utf-8", newline="\n")
    (tmp_path / "chunks.yaml").write_text(CHUNKS_YAML, encoding="utf-8", newline="\n")
    # --done validates --commit against a real commit at --root; give the fixture its own tiny repo.
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture init",
        ],
        cwd=str(tmp_path),
        check=True,
        env=dict(os.environ),
    )
    return tmp_path


def run_next_chunk(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(NEXT_CHUNK), "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_bare_prints_first_eligible_todo_chunk(fixture_root: Path):
    result = run_next_chunk(fixture_root)
    assert result.returncode == 0
    assert "A0" in result.stdout


def test_status_exits_zero_and_prints_table_and_flags(fixture_root: Path):
    result = run_next_chunk(fixture_root, "--status")
    assert result.returncode == 0
    assert "A1" in result.stdout
    assert "Open [BEN] flags" in result.stdout


def test_status_is_a_noop_round_trip(fixture_root: Path):
    before = (fixture_root / "BUILD_STATUS.md").read_bytes()
    run_next_chunk(fixture_root, "--status")
    after = (fixture_root / "BUILD_STATUS.md").read_bytes()
    assert before == after


def test_check_passes_for_a0(fixture_root: Path):
    result = run_next_chunk(fixture_root, "--check", "A0")
    assert result.returncode == 0


def test_check_fails_for_a2(fixture_root: Path):
    result = run_next_chunk(fixture_root, "--check", "A2")
    assert result.returncode == 1


def test_start_sets_in_progress_and_touches_only_that_line(fixture_root: Path):
    before_lines = (fixture_root / "BUILD_STATUS.md").read_text(encoding="utf-8").splitlines()
    result = run_next_chunk(fixture_root, "--start", "A0")
    assert result.returncode == 0
    after_lines = (fixture_root / "BUILD_STATUS.md").read_text(encoding="utf-8").splitlines()
    assert len(before_lines) == len(after_lines)
    changed = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
    assert changed == [before_lines.index("| A0 | TODO | | | | | | first chunk |")]
    assert "IN_PROGRESS" in after_lines[changed[0]]


def test_done_refuses_without_passing_check(fixture_root: Path):
    # A2's check always fails, so --done must refuse and must not touch the tracker.
    before = (fixture_root / "BUILD_STATUS.md").read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(NEXT_CHUNK),
            "--root",
            str(fixture_root),
            "--done",
            "A2",
            "--commit",
            "HEAD",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    after = (fixture_root / "BUILD_STATUS.md").read_bytes()
    assert before == after


def test_done_succeeds_and_records_commit(fixture_root: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(NEXT_CHUNK),
            "--root",
            str(fixture_root),
            "--done",
            "A0",
            "--commit",
            "HEAD",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    content = (fixture_root / "BUILD_STATUS.md").read_text(encoding="utf-8")
    assert "| A0 | DONE |" in content


def test_bare_after_a0_done_reports_blocked_a1_with_missing_files(fixture_root: Path):
    subprocess.run(
        [sys.executable, str(NEXT_CHUNK), "--root", str(fixture_root), "--done", "A0", "--commit", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    result = run_next_chunk(fixture_root)
    assert result.returncode == 0
    assert "A1" in result.stdout
    assert "BLOCKED" in result.stdout


def test_bare_refuses_when_a_done_predecessor_check_now_fails(fixture_root: Path, monkeypatch):
    # Mark A0 done, then corrupt its check in chunks.yaml so it now fails; next_chunk must refuse.
    subprocess.run(
        [sys.executable, str(NEXT_CHUNK), "--root", str(fixture_root), "--done", "A0", "--commit", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    broken_yaml = CHUNKS_YAML.replace(
        '- python -c "raise SystemExit(0)"\n  - id: A1',
        '- python -c "raise SystemExit(1)"\n  - id: A1',
        1,
    )
    (fixture_root / "chunks.yaml").write_text(broken_yaml, encoding="utf-8", newline="\n")
    result = run_next_chunk(fixture_root)
    assert result.returncode == 0  # bare mode prints a refusal message, not a nonzero exit
    assert "REFUSED" in result.stdout


def test_peek_names_the_next_chunk_without_rerunning_checks(fixture_root: Path):
    # The same setup as the refusal test: a DONE predecessor whose check now fails. --peek must not run it.
    subprocess.run(
        [sys.executable, str(NEXT_CHUNK), "--root", str(fixture_root), "--done", "A0", "--commit", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    broken_yaml = CHUNKS_YAML.replace(
        '- python -c "raise SystemExit(0)"\n  - id: A1',
        '- python -c "raise SystemExit(1)"\n  - id: A1',
        1,
    )
    (fixture_root / "chunks.yaml").write_text(broken_yaml, encoding="utf-8", newline="\n")
    result = run_next_chunk(fixture_root, "--peek")
    assert result.returncode == 0 and "REFUSED" not in result.stdout
    assert "A1" in result.stdout or "A2" in result.stdout
    assert "NOT rerun" in result.stderr  # a preview is never mistaken for the real selector


def test_block_sets_blocked_with_reason(fixture_root: Path):
    result = run_next_chunk(fixture_root, "--block", "A2", "--reason", "needs Ben's input")
    assert result.returncode == 0
    content = (fixture_root / "BUILD_STATUS.md").read_text(encoding="utf-8")
    assert "| A2 | BLOCKED |" in content
    assert "needs Ben's input" in content


def test_start_preserves_crlf_line_endings(tmp_path: Path):
    # BUILD_STATUS.md is CRLF on disk once core.autocrlf checks it out; next_chunk.py must
    # not normalize every other line to LF while editing the one row it touches.
    crlf_tracker = TRACKER.replace("\n", "\r\n")
    (tmp_path / "BUILD_STATUS.md").write_bytes(crlf_tracker.encode("utf-8"))
    (tmp_path / "chunks.yaml").write_text(CHUNKS_YAML, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture init",
        ],
        cwd=str(tmp_path),
        check=True,
        env=dict(os.environ),
    )

    before_raw = (tmp_path / "BUILD_STATUS.md").read_bytes()
    result = run_next_chunk(tmp_path, "--start", "A0")
    assert result.returncode == 0
    after_raw = (tmp_path / "BUILD_STATUS.md").read_bytes()

    before_lines = before_raw.split(b"\r\n")
    after_lines = after_raw.split(b"\r\n")
    assert len(before_lines) == len(after_lines)
    changed = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
    assert len(changed) == 1
    assert b"IN_PROGRESS" in after_lines[changed[0]]
    # Every line, including the edited one, is still CRLF-terminated (no line lost its \r).
    assert after_raw.count(b"\r\n") == before_raw.count(b"\r\n")
    assert b"\n" not in after_raw.replace(b"\r\n", b"")


QUEUE_TRACKER = """# Build status

## Chunks

| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |
|---|---|---|---|---|---|---|---|
| A0 | DONE | | 2026-10-01 | 2026-10-01 | abc | PASS | |
| Q1 | BLOCKED | A0 | | | | | Needs: flag 11 |
| Q2 | TODO | A0 | | | | | |
| Q3 | GATED | A0 | | | | | |
| Q4 | TODO | Q2 | | | | | |

## Open [BEN] flags

| # | Flag | Default in force | Where it lands |
|---|---|---|---|
| 11 | Which goalie rule applies when fees are unequal? | lineup-count cap | build/exposure.py |
"""

QUEUE_YAML = """
version: 3
chunks:
  - id: A0
    title: Done already
    depends: []
    marker: a0
    checks:
      - python -c "raise SystemExit(0)"
  - id: Q1
    title: Needs Ben
    depends: [A0]
    marker: q1
    band: 1
    size: S
    effort: low
    breakpoint: after the first test
    impact: judgment only
    needs: [11]
    backlog: [B45]
    findings: [R07]
    checks:
      - python -c "raise SystemExit(0)"
  - id: Q2
    title: Ready to go
    depends: [A0]
    marker: q2
    band: 0
    size: M
    effort: medium
    breakpoint: commit after R01
    impact: a legal file wins; observed on no slate yet
    backlog: []
    findings: [R01, R02]
    checks:
      - python -c "raise SystemExit(0)"
  - id: Q3
    title: Gated measurement
    depends: [A0]
    marker: q3
    gated_on: 30 settled slates
    checks:
      - python -c "raise SystemExit(0)"
  - id: Q4
    title: Later
    depends: [Q2]
    marker: q4
    band: 3
    size: S
    effort: low
    breakpoint: one commit
    impact: hygiene
    checks:
      - python -c "raise SystemExit(0)"
deferred:
  - item: B27 re-download near lock
    trigger: a real slate re-downloaded near lock
    backlog: [B27]
flag_recommendations:
  11: keep the lineup-count cap
"""

BUILD_CHUNKS = """# Build chunks

Hand-written text above the markers stays.

<!-- QUEUE:BEGIN -->
old generated text
<!-- QUEUE:END -->

Hand-written text below the markers stays too.
"""


@pytest.fixture()
def queue_root(tmp_path: Path) -> Path:
    (tmp_path / "BUILD_STATUS.md").write_text(QUEUE_TRACKER, encoding="utf-8", newline="\n")
    (tmp_path / "chunks.yaml").write_text(QUEUE_YAML, encoding="utf-8", newline="\n")
    (tmp_path / "BUILD_CHUNKS.md").write_bytes(BUILD_CHUNKS.replace("\n", "\r\n").encode("utf-8"))
    return tmp_path


def test_next_skips_gated_and_flag_blocked_chunks_but_names_them(queue_root: Path):
    result = run_next_chunk(queue_root)
    assert result.returncode == 0
    first = result.stdout.splitlines()[0]
    assert first == "Q2", result.stdout
    assert "skipped Q1" in result.stdout and "flag" in result.stdout
    # Q3 (GATED) ranks after Q2, so it is not reported as skipped ahead of it
    assert "skipped Q3" not in result.stdout


def test_render_queue_rewrites_only_between_markers_and_keeps_crlf(queue_root: Path):
    before = (queue_root / "BUILD_CHUNKS.md").read_bytes()
    result = run_next_chunk(queue_root, "--render-queue")
    assert result.returncode == 0, result.stderr
    after = (queue_root / "BUILD_CHUNKS.md").read_bytes()
    text = after.decode("utf-8")
    assert "Hand-written text above the markers stays." in text
    assert "Hand-written text below the markers stays too." in text
    assert "old generated text" not in text
    assert b"\n" not in after.replace(b"\r\n", b"")  # still CRLF throughout
    # rank is chunks.yaml file order among queued chunks, whatever the band: Q1 (blocked), Q2, Q4; A0 DONE; Q3 deferred
    i_q1, i_q2, i_q4 = text.index("| 1 | Q1 |"), text.index("| 2 | Q2 |"), text.index("| 3 | Q4 |")
    assert i_q1 < i_q2 < i_q4
    assert "DONE: A0." in text
    assert "| 11 | Q1 (#1) |" in text and "keep the lineup-count cap" in text
    assert "Q3: Gated measurement | 30 settled slates" in text
    assert "B27 re-download near lock | a real slate re-downloaded near lock | B27" in text
    # a second render is a no-op apart from nothing: idempotent
    run_next_chunk(queue_root, "--render-queue")
    assert (queue_root / "BUILD_CHUNKS.md").read_bytes() == after
    assert before != after


def test_render_queue_dry_run_prints_and_does_not_write(queue_root: Path):
    before = (queue_root / "BUILD_CHUNKS.md").read_bytes()
    result = run_next_chunk(queue_root, "--render-queue", "--dry-run")
    assert result.returncode == 0
    assert "### Ranked chunks" in result.stdout
    assert (queue_root / "BUILD_CHUNKS.md").read_bytes() == before


def test_render_queue_refuses_without_markers(queue_root: Path):
    (queue_root / "BUILD_CHUNKS.md").write_text("# no markers here\n", encoding="utf-8")
    result = run_next_chunk(queue_root, "--render-queue")
    assert result.returncode == 1
    assert "QUEUE:BEGIN" in result.stderr


def test_chunks_yaml_validation_rejects_xl_and_bad_ids(queue_root: Path):
    bad = QUEUE_YAML.replace("size: M", "size: XL", 1)
    (queue_root / "chunks.yaml").write_text(bad, encoding="utf-8", newline="\n")
    result = run_next_chunk(queue_root, "--status")
    assert result.returncode == 1 and "XL must be split" in result.stderr
    bad = QUEUE_YAML.replace("backlog: [B45]", "backlog: [B45a]", 1)
    (queue_root / "chunks.yaml").write_text(bad, encoding="utf-8", newline="\n")
    result = run_next_chunk(queue_root, "--status")
    assert result.returncode == 1 and "B-row ids" in result.stderr


def test_flip_to_todo_message_when_requires_files_now_present(fixture_root: Path):
    subprocess.run(
        [sys.executable, str(NEXT_CHUNK), "--root", str(fixture_root), "--done", "A0", "--commit", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    inbox = fixture_root / "inbox"
    inbox.mkdir()
    (inbox / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    result = run_next_chunk(fixture_root)
    assert result.returncode == 0
    assert "flip to TODO" in result.stdout


# --- --lint: chunks.yaml, the tracker, BACKLOG.md, pytest.ini and the Queue block agree ----------

LINT_BACKLOG = """# Backlog

| ID | Date / evidence | Problem or hypothesis | Affected metric | Proposed bounded change | Confidence / sample | Acceptance test | Priority | Status | Result / version |
|---|---|---|---|---|---|---|---|---|---|
| B27 | e | p | m | c | conf | acc | Medium | READY | |
| B45 | e | p | m | c | conf | acc | High | NEW | |
"""

LINT_PYTEST_INI = """[pytest]
addopts = --strict-markers
markers =
    a0: chunk A0
    q1: chunk Q1
    q2: chunk Q2
    q3: chunk Q3
    q4: chunk Q4
"""


@pytest.fixture()
def lint_root(queue_root: Path) -> Path:
    (queue_root / "BACKLOG.md").write_text(LINT_BACKLOG, encoding="utf-8", newline="\n")
    (queue_root / "pytest.ini").write_text(LINT_PYTEST_INI, encoding="utf-8", newline="\n")
    cards = "".join(f"\n### {cid} · card\n\nbody\n" for cid in ("Q1", "Q2", "Q4"))
    path = queue_root / "BUILD_CHUNKS.md"
    path.write_bytes((path.read_bytes().decode("utf-8") + cards.replace("\n", "\r\n")).encode("utf-8"))
    assert run_next_chunk(queue_root, "--render-queue").returncode == 0
    return queue_root


def lint(root: Path) -> subprocess.CompletedProcess:
    return run_next_chunk(root, "--lint")


def test_lint_passes_on_a_consistent_root(lint_root: Path):
    result = lint(lint_root)
    assert result.returncode == 0, result.stdout
    assert "LINT=OK" in result.stdout


def test_lint_flags_an_open_backlog_row_that_no_chunk_carries(lint_root: Path):
    with open(lint_root / "BACKLOG.md", "a", encoding="utf-8", newline="\n") as f:
        f.write("| B99 | e | p | m | c | conf | acc | Low | NEW | |\n")
    result = lint(lint_root)
    assert result.returncode == 1
    assert "B99 is NEW but no chunk or deferred item carries it" in result.stdout


def test_lint_flags_a_done_row_that_a_live_chunk_still_carries_unless_qualified(lint_root: Path):
    path = lint_root / "BACKLOG.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("| High | NEW |", "| High | DONE |"), encoding="utf-8", newline="\n")
    bad = lint(lint_root)
    assert bad.returncode == 1 and "B45 is DONE" in bad.stdout
    path.write_text(text.replace("| High | NEW |", "| High | DONE (partial) |"), encoding="utf-8", newline="\n")
    assert lint(lint_root).returncode == 0


def test_lint_flags_a_backlog_id_that_appears_twice(lint_root: Path):
    with open(lint_root / "BACKLOG.md", "a", encoding="utf-8", newline="\n") as f:
        f.write("| B45 | e | p | m | c | conf | acc | Low | NEW | |\n")
    result = lint(lint_root)
    assert result.returncode == 1 and "B45 appears twice" in result.stdout


def test_lint_flags_a_needs_flag_missing_from_the_flags_table(lint_root: Path):
    path = lint_root / "chunks.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("needs: [11]", "needs: [12]"), encoding="utf-8", newline="\n")
    result = lint(lint_root)
    assert result.returncode == 1 and "needs flag 12" in result.stdout


def test_lint_flags_a_stale_queue_block(lint_root: Path):
    path = lint_root / "chunks.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("title: Ready to go", "title: Ready to roll"),
                    encoding="utf-8", newline="\n")
    result = lint(lint_root)
    assert result.returncode == 1 and "Queue block is stale" in result.stdout
    assert run_next_chunk(lint_root, "--render-queue").returncode == 0
    assert lint(lint_root).returncode == 0


def test_lint_flags_an_unregistered_marker_and_a_missing_card(lint_root: Path):
    ini = lint_root / "pytest.ini"
    ini.write_text(ini.read_text(encoding="utf-8").replace("    q2: chunk Q2\n", ""), encoding="utf-8", newline="\n")
    chunks = lint_root / "BUILD_CHUNKS.md"
    chunks.write_bytes(chunks.read_bytes().replace(b"### Q4 ", b"### Qx "))
    result = lint(lint_root)
    assert result.returncode == 1
    assert "Q2: marker q2 is not registered" in result.stdout
    assert "Q4: no card heading" in result.stdout
