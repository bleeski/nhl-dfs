import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from conftest import REPO_ROOT
from nhl_dfs.build import state
from nhl_dfs.build.state import FileLock, LockTimeout, PublishRefused, new_run, open_run, publish, sha256
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.referee.check_file import RefereeReport

pytestmark = pytest.mark.c2b

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
SLATE = "classic-20260929-abc"


def _run(tmp_path):
    run = new_run(tmp_path / "runs", mode=Mode.CLASSIC, clock=lambda: NOW)
    (run.inputs / "DKSalaries.csv").write_bytes(b"salary")
    (run.inputs / "DKEntries.csv").write_bytes(b"entries")
    return run


def _report(data: bytes, ok=True):
    return RefereeReport(ok=ok, reasons=[] if ok else ["bad"], mode="classic", entries_checked=1,
                         out_sha256=sha256(data), salary_sha256=sha256(b"salary"),
                         entries_sha256=sha256(b"entries"), parent_sha256=None)


def test_new_run_layout_and_same_second_suffix(tmp_path):
    a = new_run(tmp_path, mode=Mode.CLASSIC, clock=lambda: NOW)
    b = new_run(tmp_path, mode=Mode.CLASSIC, clock=lambda: NOW)
    assert a.run_id == "20260929-120000-classic" and b.run_id == "20260929-120000-classic-2"
    assert sorted(p.name for p in a.path.iterdir()) == ["inputs", "news", "qa", "sim", "versions"]
    assert open_run(tmp_path, b.run_id).mode is Mode.CLASSIC


def test_publish_writes_versions_pointer_and_public_file(tmp_path):
    run, out = _run(tmp_path), tmp_path / "outputs"
    r1 = publish(run, b"one", _report(b"one"), SLATE, outputs_root=out)
    assert r1.version == 1 and r1.public_replaced and run.current_version() == 1
    assert (out / SLATE / "DKEntries.csv").read_bytes() == b"one"
    stamp = json.loads((out / SLATE / "published.json").read_text())
    assert stamp["run_id"] == run.run_id and stamp["sha256"] == sha256(b"one")
    r2 = publish(run, b"two", _report(b"two"), SLATE, outputs_root=out, expect_public_sha=sha256(b"one"))
    assert r2.version == 2 and run.current_version() == 2
    assert run.version_file(1).read_bytes() == b"one" and run.version_file(2).read_bytes() == b"two"
    assert (out / SLATE / "DKEntries.csv").read_bytes() == b"two"


@pytest.mark.parametrize("make, match", [
    (lambda: (b"x", _report(b"x", ok=False)), "referee report failed"),
    (lambda: (b"x", _report(b"y")), "different bytes"),
])
def test_publish_refuses_unbound_or_failing_reports(tmp_path, make, match):
    run, out = _run(tmp_path), tmp_path / "outputs"
    data, report = make()
    with pytest.raises(PublishRefused, match=match):
        publish(run, data, report, SLATE, outputs_root=out)
    assert run.version_numbers() == [] and not (out / SLATE / "DKEntries.csv").exists()


def test_publish_refuses_a_report_for_other_inputs(tmp_path):
    run, out = _run(tmp_path), tmp_path / "outputs"
    (run.inputs / "DKEntries.csv").write_bytes(b"other entries")
    with pytest.raises(PublishRefused, match="DKEntries.csv"):
        publish(run, b"x", _report(b"x"), SLATE, outputs_root=out)


def test_crash_between_temp_write_and_replace_leaves_public_unchanged(tmp_path, monkeypatch):
    run, out = _run(tmp_path), tmp_path / "outputs"
    publish(run, b"one", _report(b"one"), SLATE, outputs_root=out)
    public = out / SLATE / "DKEntries.csv"
    real_replace = state.os.replace

    def crash(src, dst):
        if str(dst) == str(public):
            raise RuntimeError("power cut")
        return real_replace(src, dst)

    monkeypatch.setattr(state.os, "replace", crash)
    with pytest.raises(RuntimeError, match="power cut"):
        publish(run, b"two", _report(b"two"), SLATE, outputs_root=out)
    assert public.read_bytes() == b"one"
    assert not [p for p in public.parent.iterdir() if p.name.endswith(".tmp")]


def test_public_file_open_in_excel_keeps_the_version_and_says_why(tmp_path, monkeypatch):
    run, out = _run(tmp_path), tmp_path / "outputs"
    publish(run, b"one", _report(b"one"), SLATE, outputs_root=out)
    public = out / SLATE / "DKEntries.csv"
    real_replace = state.os.replace

    def held(src, dst):
        if str(dst) == str(public):
            raise PermissionError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(state.os, "replace", held)
    r = publish(run, b"two", _report(b"two"), SLATE, outputs_root=out)
    assert not r.public_replaced and "Excel" in r.public_detail
    assert run.version_file(2).read_bytes() == b"two" and public.read_bytes() == b"one"


def test_expect_public_sha_never_clobbers_a_newer_publish(tmp_path):
    out = tmp_path / "outputs"
    old = _run(tmp_path)
    publish(old, b"old v1", _report(b"old v1"), SLATE, outputs_root=out)
    newer = new_run(tmp_path / "runs", mode=Mode.CLASSIC, clock=lambda: NOW)
    (newer.inputs / "DKSalaries.csv").write_bytes(b"salary")
    (newer.inputs / "DKEntries.csv").write_bytes(b"entries")
    publish(newer, b"new v1", _report(b"new v1"), SLATE, outputs_root=out)
    r = publish(old, b"old v2", _report(b"old v2"), SLATE, outputs_root=out, expect_public_sha=sha256(b"old v1"))
    assert not r.public_replaced and "newer publish" in r.public_detail
    assert (out / SLATE / "DKEntries.csv").read_bytes() == b"new v1"
    assert old.current_version() == 2


_HOLDER = """
import sys, time
sys.path.insert(0, "src")
from nhl_dfs.build.state import FileLock
lock, log, tag, hold = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
with FileLock(lock, timeout_s=30):
    with open(log, "a") as f:
        f.write(f"{tag} enter {time.time()}\\n")
    time.sleep(hold)
    with open(log, "a") as f:
        f.write(f"{tag} exit {time.time()}\\n")
"""


def test_two_processes_serialize_on_the_slate_lock(tmp_path):
    lock, log = tmp_path / "outputs" / SLATE / ".lock", tmp_path / "log.txt"
    procs = [subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(log), tag, "0.6"], cwd=REPO_ROOT)
             for tag in ("a", "b")]
    for p in procs:
        assert p.wait(timeout=60) == 0
    events = [line.split() for line in log.read_text().splitlines()]
    assert len(events) == 4
    # enter/exit must alternate by process: a enter, a exit, b enter, b exit (in some order)
    tags = [e[0] for e in events]
    assert tags[0] == tags[1] and tags[2] == tags[3] and tags[0] != tags[2]
    assert float(events[2][2]) >= float(events[1][2])


def test_lock_times_out_while_another_process_holds_it(tmp_path):
    lock, log = tmp_path / ".lock", tmp_path / "log.txt"
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(log), "h", "3"], cwd=REPO_ROOT)
    try:
        deadline = time.monotonic() + 20
        while not (log.exists() and "enter" in log.read_text()):
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.05)
        with pytest.raises(LockTimeout):
            with FileLock(lock, timeout_s=0.3):
                pass
    finally:
        p.wait(timeout=30)
    with FileLock(lock, timeout_s=5):  # released when the holder exits
        pass


def _wait_for_holder(log):
    deadline = time.monotonic() + 20
    while not (log.exists() and "enter" in log.read_text()):
        assert time.monotonic() < deadline, "holder never took the lock"
        time.sleep(0.05)


def test_publish_takes_the_slate_lock(tmp_path):
    run, out = _run(tmp_path), tmp_path / "outputs"
    publish(run, b"one", _report(b"one"), SLATE, outputs_root=out)
    lock, log = out / SLATE / ".lock", tmp_path / "log.txt"
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(log), "h", "3"], cwd=REPO_ROOT)
    try:
        _wait_for_holder(log)
        with pytest.raises(LockTimeout):
            publish(run, b"two", _report(b"two"), SLATE, outputs_root=out, lock_timeout_s=0.3)
        assert run.version_numbers() == [1]
        assert (out / SLATE / "DKEntries.csv").read_bytes() == b"one"
    finally:
        p.wait(timeout=30)
    r = publish(run, b"two", _report(b"two"), SLATE, outputs_root=out)
    assert r.version == 2 and (out / SLATE / "DKEntries.csv").read_bytes() == b"two"
