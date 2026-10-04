"""Run directories and atomic publishing (plan section 10, ladder step 2).

runs/<YYYYMMDD-HHMMSS>-<mode>/ (UTC) holds inputs/, versions/, sim/, qa/, news/. Every version
is written as versions/v<N>/DKEntries.csv through a temp file in the same directory, fsync, and
os.replace. The run's `current` file names the current version ("v<N>"); it is a pointer file,
not a symlink, because Windows symlinks need admin rights or Developer Mode.

The public file outputs/<slate_id>/DKEntries.csv is replaced the same way (temp file in that
directory, then os.replace) under two OS file locks: the slate lock outputs/<slate_id>/.lock
and the run lock runs/<id>/.lock. OS locks are released by the OS if the process dies.
publish refuses unless the referee report passed and is bound to these exact bytes and inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.referee.check_file import RefereeReport

RUN_SUBDIRS = ("inputs", "versions", "sim", "qa", "news")
PUBLIC_NAME = "DKEntries.csv"


class PublishRefused(Exception):
    """The export is not bound to a passing referee report; nothing was written."""


class LockCrossed(PublishRefused):
    """A lock boundary (a game start or the edit stop) was crossed before the new version could be committed;
    nothing was written and the incumbent stands. Raised from `publish`'s precheck, under the publish locks."""


class LockTimeout(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Temp file in the destination directory, fsync, os.replace. The temp is removed on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class FileLock:
    """Exclusive OS lock on a lock file (msvcrt on Windows, flock elsewhere)."""

    def __init__(self, path: Path, *, timeout_s: float = 60.0, poll_s: float = 0.05) -> None:
        self.path = Path(path)
        self.timeout_s, self.poll_s = timeout_s, poll_s
        self._f = None

    def _try(self) -> bool:
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_s
        while not self._try():
            if time.monotonic() > deadline:
                self._f.close()
                self._f = None
                raise LockTimeout(f"could not lock {self.path} within {self.timeout_s}s")
            time.sleep(self.poll_s)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        finally:
            self._f.close()
            self._f = None


@dataclass
class RunDir:
    path: Path
    run_id: str
    mode: Mode

    @property
    def inputs(self) -> Path:
        return self.path / "inputs"

    @property
    def versions(self) -> Path:
        return self.path / "versions"

    @property
    def lock_path(self) -> Path:
        return self.path / ".lock"

    def version_file(self, n: int) -> Path:
        return self.versions / f"v{n}" / PUBLIC_NAME

    def current_version(self) -> int | None:
        p = self.path / "current"
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8").strip()
        return int(text[1:]) if text.startswith("v") and text[1:].isdigit() else None

    def version_numbers(self) -> list[int]:
        if not self.versions.exists():
            return []
        return sorted(int(d.name[1:]) for d in self.versions.iterdir() if d.name[:1] == "v" and d.name[1:].isdigit())


def _mode_of(run_id: str) -> Mode:
    for m in Mode:
        if run_id.endswith("-" + m.value) or f"-{m.value}-" in run_id:
            return m
    raise ValueError(f"run id {run_id!r} does not name a mode")


def new_run(root="runs", *, mode: Mode, clock: Callable[[], datetime] | None = None) -> RunDir:
    """runs/<YYYYMMDD-HHMMSS>-<mode>/ in UTC; a second run in the same second gets a -2 suffix."""
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = f"{now:%Y%m%d-%H%M%S}-{mode.value}"
    run_id, k = base, 1
    while True:
        try:
            (root / run_id).mkdir()
            break
        except FileExistsError:
            k += 1
            run_id = f"{base}-{k}"
    run = RunDir(root / run_id, run_id, mode)
    for sub in RUN_SUBDIRS:
        (run.path / sub).mkdir()
    return run


def open_run(root, run_id: str) -> RunDir:
    path = Path(root) / run_id
    if not path.is_dir():
        raise FileNotFoundError(f"no run directory {path}")
    return RunDir(path, run_id, _mode_of(run_id))


def latest_run(root) -> RunDir | None:
    root = Path(root)
    if not root.is_dir():
        return None
    ids = sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "manifest.json").exists())
    return open_run(root, ids[-1]) if ids else None


@dataclass
class PublishResult:
    version: int
    version_path: Path
    public_path: Path
    public_replaced: bool
    public_detail: str  # why the public file was not replaced, else ""


def publish(
    run: RunDir,
    export_bytes: bytes,
    report: RefereeReport,
    slate_id: str,
    *,
    outputs_root="outputs",
    expect_public_sha: str | None = None,
    lock_timeout_s: float = 60.0,
    precheck: Callable[[], str | None] | None = None,
) -> PublishResult:
    """Write the next version and replace the public file.

    Refuses (PublishRefused, nothing written) unless report.ok and the report is bound to these
    bytes and to the run's input copies. expect_public_sha: replace the public file only if it
    still has this hash (a later pass never clobbers a newer run's publish). A public file held
    open by another program (Excel on Windows) keeps the version and reports why.
    precheck: called once both locks are held (so after any wait for them) and before the first byte is
    written; a returned reason refuses the publish (LockCrossed). It must read the clock and the lock state
    itself: a value computed before the call can be a minute stale (lock_timeout_s).
    """
    if not report.ok:
        raise PublishRefused("referee report failed: " + "; ".join(report.reasons[:5]))
    if sha256(export_bytes) != report.out_sha256:
        raise PublishRefused("referee report is for different bytes than the export")
    for name, want in (("DKSalaries.csv", report.salary_sha256), ("DKEntries.csv", report.entries_sha256)):
        src = run.inputs / name
        if not src.exists() or sha256(src.read_bytes()) != want:
            raise PublishRefused(f"referee report is not bound to this run's {name}")

    public_dir = Path(outputs_root) / slate_id
    public = public_dir / PUBLIC_NAME
    with FileLock(public_dir / ".lock", timeout_s=lock_timeout_s), FileLock(run.lock_path, timeout_s=lock_timeout_s):
        if precheck is not None:
            why = precheck()
            if why:
                raise LockCrossed(why)
        n = (run.version_numbers() or [0])[-1] + 1
        vfile = run.version_file(n)
        atomic_write(vfile, export_bytes)
        atomic_write(vfile.parent / "referee.json", json.dumps(asdict(report), indent=2).encode("utf-8"))
        atomic_write(run.path / "current", f"v{n}\n".encode("ascii"))

        detail = ""
        if expect_public_sha is not None:
            have = sha256(public.read_bytes()) if public.exists() else None
            if have != expect_public_sha:
                detail = "public file changed since this run's last publish (a newer publish owns it); left as is"
        if not detail:
            try:
                atomic_write(public, export_bytes)
            except PermissionError:
                detail = f"could not replace {public}: the file is open in another program (close it in Excel, then rerun)"
            except OSError as exc:
                detail = f"could not replace {public}: {exc}"
        if not detail:
            stamp = {
                "run_id": run.run_id,
                "version": n,
                "sha256": sha256(export_bytes),
                "published_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            atomic_write(public_dir / "published.json", json.dumps(stamp, indent=2).encode("utf-8"))
    return PublishResult(n, vfile, public, not detail, detail)
