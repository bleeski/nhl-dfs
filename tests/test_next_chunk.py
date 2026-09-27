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
        ["git", "commit", "-q", "-m", "fixture init"],
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


def test_block_sets_blocked_with_reason(fixture_root: Path):
    result = run_next_chunk(fixture_root, "--block", "A2", "--reason", "needs Ben's input")
    assert result.returncode == 0
    content = (fixture_root / "BUILD_STATUS.md").read_text(encoding="utf-8")
    assert "| A2 | BLOCKED |" in content
    assert "needs Ben's input" in content


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
