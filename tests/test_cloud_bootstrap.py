"""C39 commit 3 (B71, flag 18): tools/cloud_bootstrap.sh starts the history backfill in the background when the store is absent,
and a run says what the bootstrap is doing.

The dry-run tests need bash (Git Bash on Ben's Windows box, bash in CI) and skip LOUDLY when it is missing or is the WSL
launcher. The test that really starts the background job runs where nohup and executable stubs are reliable (Linux, macOS)
and skips loudly on Windows; CI runs it.
"""

import os
import shutil
import subprocess
import sys
import time
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nhl_dfs.data.history import status

pytestmark = pytest.mark.c39

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "cloud_bootstrap.sh"
NOW = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)


def _bash() -> str:
    exe = shutil.which("bash")
    if exe:
        try:
            ok = subprocess.run([exe, "-c", "echo ok"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            ok = None
        if ok is not None and ok.stdout.strip() == "ok":
            return exe
    msg = "BASH MISSING (or only the WSL launcher): the cloud_bootstrap.sh tests did not run on this machine; CI runs them"
    warnings.warn(msg)
    pytest.skip(msg)


def _posix(p: Path) -> str:
    return Path(p).as_posix()


def _run(script: Path, args, tmp_path, *, store: Path, remote: bool = True, timeout: int = 60):
    env = {**os.environ, "NHL_HISTORY_STORE": _posix(store), "NHL_BOOTSTRAP_STATE": _posix(tmp_path / "boot.state"),
           "NHL_BOOTSTRAP_LOG": _posix(tmp_path / "boot.log")}
    env.pop("CLAUDE_CODE_REMOTE", None)
    env.pop("NHL_BOOTSTRAP_DRY_RUN", None)
    if remote:
        env["CLAUDE_CODE_REMOTE"] = "true"
    return subprocess.run([_bash(), _posix(script), *args], capture_output=True, text=True, env=env, timeout=timeout)


def _store(tmp_path, *, present: bool) -> Path:
    store = tmp_path / "history"
    if present:
        (store / "skater_games").mkdir(parents=True)
        (store / "skater_games" / "20252026.parquet").write_bytes(b"x")
    return store


def _store_at(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return _store(root, present=True)


# -- the dry run (every machine with bash) ------------------------------------------------------------------------------------

def test_dry_run_starts_the_backfill_only_when_the_store_is_absent(tmp_path):
    absent = _run(SCRIPT, ["--dry-run"], tmp_path, store=_store(tmp_path, present=False))
    assert absent.returncode == 0
    assert "WOULD START backfill (history --backfill 2) in the background" in absent.stderr
    assert not (tmp_path / "boot.state").exists() and not (tmp_path / "boot.log").exists()  # nothing was started
    present = _run(SCRIPT, ["--dry-run"], tmp_path, store=_store_at(tmp_path / "p"))
    assert present.returncode == 0 and "SKIP backfill" in present.stderr and "WOULD START" not in present.stderr
    assert not (tmp_path / "boot.state").exists()



def test_dry_run_through_the_environment_variable(tmp_path):
    env_run = subprocess.run([_bash(), _posix(SCRIPT)], capture_output=True, text=True, timeout=60, env={
        **os.environ, "CLAUDE_CODE_REMOTE": "true", "NHL_BOOTSTRAP_DRY_RUN": "1", "NHL_HISTORY_STORE": _posix(tmp_path / "none"),
        "NHL_BOOTSTRAP_STATE": _posix(tmp_path / "s"), "NHL_BOOTSTRAP_LOG": _posix(tmp_path / "l")})
    assert env_run.returncode == 0 and "WOULD START" in env_run.stderr and not (tmp_path / "s").exists()


def test_a_local_session_does_nothing_and_says_nothing(tmp_path):
    r = _run(SCRIPT, [], tmp_path, store=_store(tmp_path, present=False), remote=False)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "" and not (tmp_path / "boot.state").exists()


def test_the_script_keeps_its_line_endings_and_exit_zero_contract():
    raw = SCRIPT.read_bytes()
    assert b"\r\n" not in raw  # .gitattributes forces LF on *.sh; a CRLF script would not run under bash
    assert raw.rstrip().endswith(b"exit 0")


# -- really starting the job (Linux and macOS) ----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="LOUD SKIP: the real background start needs nohup and executable stubs; "
                    "not reliable under Git Bash on Windows, CI runs it")
def test_the_background_backfill_starts_once_when_absent_never_when_present_and_returns_at_once(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "tools" / "cloud_bootstrap.sh")
    record = tmp_path / "stub_args.txt"
    stub = repo / ".venv" / "bin" / "python"
    stub.write_text(f'#!/usr/bin/env bash\nsleep 2\necho "$@" >> "{record}"\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    t0 = time.perf_counter()
    r = _run(repo / "tools" / "cloud_bootstrap.sh", [], tmp_path, store=_store(tmp_path, present=False))
    assert r.returncode == 0 and "STARTED backfill in the background" in r.stderr
    assert time.perf_counter() - t0 < 1.5  # the stub sleeps 2 s: the hook did not wait for it
    state = tmp_path / "boot.state"
    deadline = time.time() + 15
    while time.time() < deadline and "state=DONE" not in (state.read_text() if state.exists() else ""):
        time.sleep(0.1)
    assert "state=DONE" in state.read_text() and "exit=0" in state.read_text()
    assert record.read_text().split() == ["-m", "nhl_dfs.cli", "history", "--backfill", "2"]
    # the store is there now: a second session start does not start another one
    record.unlink()
    again = _run(repo / "tools" / "cloud_bootstrap.sh", [], tmp_path, store=_store_at(tmp_path / "now_present"))
    assert again.returncode == 0 and "SKIP backfill" in again.stderr
    time.sleep(0.5)
    assert not record.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="LOUD SKIP: see above")
def test_a_failed_backfill_is_recorded_with_its_exit_code(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "tools" / "cloud_bootstrap.sh")
    stub = repo / ".venv" / "bin" / "python"
    stub.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    stub.chmod(0o755)
    assert _run(repo / "tools" / "cloud_bootstrap.sh", [], tmp_path, store=_store(tmp_path, present=False)).returncode == 0
    state = tmp_path / "boot.state"
    deadline = time.time() + 15
    while time.time() < deadline and "state=FAILED" not in (state.read_text() if state.exists() else ""):
        time.sleep(0.1)
    assert "state=FAILED" in state.read_text() and "exit=3" in state.read_text()


# -- what a run says about it (any machine) --------------------------------------------------------------------------------------

def _state(path: Path, **kv) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")
    return path


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_bootstrap_state_reads_running_done_failed_and_abandoned(tmp_path):
    fresh = _iso(NOW - timedelta(minutes=2))
    running = status.read_bootstrap_state(_state(tmp_path / "a", state="RUNNING", pid=os.getpid(), started=fresh), now=NOW)
    assert running["state"] == "RUNNING" and running["age_min"] == 2.0
    old = status.read_bootstrap_state(_state(tmp_path / "b", state="RUNNING", pid=os.getpid(), started=_iso(NOW - timedelta(minutes=45))),
                                      now=NOW)
    assert old["state"] == "ABANDONED" and "no longer running" in old["note"]
    done = status.read_bootstrap_state(_state(tmp_path / "c", state="DONE", started=fresh, exit=0), now=NOW)
    assert done["state"] == "DONE" and done["exit"] == "0"
    failed = status.read_bootstrap_state(_state(tmp_path / "d", state="FAILED", started=fresh, exit=3), now=NOW)
    assert failed["state"] == "FAILED" and failed["exit"] == "3"
    assert status.read_bootstrap_state(tmp_path / "none", now=NOW) is None


@pytest.mark.skipif(sys.platform == "win32", reason="LOUD SKIP: a dead process id cannot be told safely on Windows (os.kill would end it)")
def test_a_running_record_whose_process_is_gone_reads_abandoned(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    r = status.read_bootstrap_state(_state(tmp_path / "e", state="RUNNING", pid=p.pid, started=_iso(NOW - timedelta(minutes=1))), now=NOW)
    assert r["state"] == "ABANDONED"


def test_an_absent_store_names_what_the_background_backfill_is_doing(tmp_path):
    empty = tmp_path / "nowhere"
    started = _iso(NOW - timedelta(minutes=3))
    lines = {}
    for name, kv in {"RUNNING": dict(state="RUNNING", pid=os.getpid(), started=started),
                     "DONE": dict(state="DONE", started=started, exit=0), "FAILED": dict(state="FAILED", started=started, exit=2),
                     "ABANDONED": dict(state="RUNNING", pid=os.getpid(), started=_iso(NOW - timedelta(minutes=50)))}.items():
        p = _state(tmp_path / name, **kv)
        lines[name] = status.measure(date(2026, 10, 6), store_root=empty, bootstrap_path=p, now=NOW).line()
    assert "A background backfill started at 19:57 UTC is still running" in lines["RUNNING"]
    assert "finished, but no store was written" in lines["DONE"]
    assert "failed with exit 2" in lines["FAILED"]
    assert "no longer running" in lines["ABANDONED"]
    none = status.measure(date(2026, 10, 6), store_root=empty, bootstrap_path=tmp_path / "missing", now=NOW).line()
    assert "ABSENT" in none and "history --backfill 2" in none
