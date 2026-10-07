"""Permanent boundaries from CLAUDE.md, asserted instead of described (the CI job `boundaries`).

Each test pins one rule CLAUDE.md already states and that a scan of the repository can check. A red test here is not a style
disagreement: the change touches what the engine is allowed to do, so it belongs in a pull request Ben looks at. These are tripwires,
not proofs: they overlap the behavior tests elsewhere and show only that no NEW path was added.

Not covered, on purpose:
  - "the model never edits DKEntries.csv": that is held by the byte-splicing writer and the independent referee, and a source scan
    cannot establish it.
  - import layering between the packages of src/nhl_dfs: it has cycles today (models and build, models and learn), so pinning it
    would invent a rule rather than assert one.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "nhl_dfs"

# CLAUDE.md, "Full personal DK exports and standings are gitignored ... Commit only minimized fixtures".
PERSONAL_DATA_PREFIXES = ("tests/fixtures/real/", "data/raw/", "data/standings/", "data/ledger/", "runs/", "outputs/")

# CLAUDE.md, "DraftKings login, upload, entry, and money actions are manual and Ben's": nothing in the engine may send data out
# or drive a browser. Network reads go through the one HTTP layer (data/http.py), and only through `requests`.
NETWORK_MODULES = ("requests", "httpx", "aiohttp", "urllib.request", "http.client", "socket", "ftplib", "smtplib",
                   "websocket", "websockets", "selenium", "playwright", "pyautogui", "pexpect")
HTTP_LAYER = "data/http.py"
SENDING_CALLS = {"post", "put", "patch", "delete"}
SENDING_METHODS = {"POST", "PUT", "PATCH"}

# CLAUDE.md, "no new dependencies without a tracker note".
PINNED_DEPENDENCIES = {"numpy", "pandas", "pyarrow", "scipy", "pyyaml", "requests", "pytest", "pytest-timeout", "tzdata"}

# CLAUDE.md, "No em dashes in prose or docs". archive/ is superseded and reviews/ is history: neither is edited.
EM_DASH = "—"
PROSE_EXEMPT = ("archive/", "reviews/")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        msg = "not a git checkout (an exported tree has no index): the boundary checks over tracked files cannot run"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(msg)  # in CI a skip would hide the check
        pytest.skip(msg)
    return [p for p in out.decode("utf-8").split("\0") if p]


def _sources() -> dict[str, ast.AST]:
    return {p.relative_to(SRC).as_posix(): ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in sorted(SRC.rglob("*.py"))}


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module)
            out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


def _network_imports(tree: ast.AST) -> set[str]:
    return {m for m in _imports(tree) if any(m == b or m.startswith(b + ".") for b in NETWORK_MODULES)}


def _sending(tree: ast.AST) -> list[str]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SENDING_CALLS:
            hits.append(f"line {n.lineno}: .{n.func.attr}(...)")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in SENDING_METHODS:
            hits.append(f"line {n.lineno}: the string {n.value!r}")
    return hits


# -- the detectors work (so a green result is not an empty scan) ---------------------------------------------------------------

def test_the_detectors_flag_a_violation_and_pass_a_clean_module():
    assert _network_imports(ast.parse("import requests")) == {"requests"}
    assert _network_imports(ast.parse("import urllib.request")) == {"urllib.request"}
    assert _network_imports(ast.parse("from urllib import request")) == {"urllib.request"}
    assert _network_imports(ast.parse("import socket as s")) == {"socket"}
    assert not _network_imports(ast.parse("from urllib.parse import quote\nimport json"))
    assert _sending(ast.parse("s.post(url, data=x)")) and _sending(ast.parse("m = 'POST'"))
    assert not _sending(ast.parse("d = {}\nx = d.get('a')\nrows.append(1)"))
    assert len(_sources()) > 50  # the scan really reads the package


# -- the boundaries ------------------------------------------------------------------------------------------------------------

def test_no_personal_data_is_tracked_and_the_gitignore_still_names_each_folder():
    bad = [p for p in _tracked() if p.startswith(PERSONAL_DATA_PREFIXES)]
    assert not bad, f"personal DK exports, standings or run output are tracked (commit only minimized fixtures): {bad[:5]}"
    lines = {ln.strip().lstrip("/") for ln in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()}
    missing = [p for p in PERSONAL_DATA_PREFIXES if p not in lines]
    assert not missing, f".gitignore no longer lists: {missing}"


def test_only_the_http_layer_touches_the_network_and_only_through_requests():
    sources = _sources()
    assert HTTP_LAYER in sources
    outside = {rel: sorted(_network_imports(t)) for rel, t in sources.items() if rel != HTTP_LAYER and _network_imports(t)}
    assert not outside, f"network or browser-automation modules imported outside {HTTP_LAYER}: {outside}"
    assert _network_imports(sources[HTTP_LAYER]) <= {"requests"}, f"{HTTP_LAYER} imports more than `requests`"


def test_nothing_in_the_engine_sends_data_out():
    bad = {rel: hits for rel, t in _sources().items() if (hits := _sending(t))}
    assert not bad, f"a POST, PUT, PATCH or DELETE call appeared (DraftKings login, upload, entry and money actions are Ben's): {bad}"


def test_runtime_dependencies_are_the_pinned_set():
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].lower().replace("_", "-") for d in deps}
    assert names == PINNED_DEPENDENCIES, (
        f"dependencies changed: added {sorted(names - PINNED_DEPENDENCIES)}, removed {sorted(PINNED_DEPENDENCIES - names)}. "
        "A new dependency needs a tracker note (a BUILD_STATUS.md log line) and an update of PINNED_DEPENDENCIES in this file, in the same pull request.")


def test_no_em_dash_in_tracked_prose():
    bad = []
    for p in _tracked():
        if not p.endswith(".md") or p.startswith(PROSE_EXEMPT) or not (REPO / p).is_file():
            continue
        if EM_DASH in (REPO / p).read_text(encoding="utf-8", errors="replace"):
            bad.append(p)
    assert not bad, f"em dash in prose or docs (CLAUDE.md style rule): {bad}"
