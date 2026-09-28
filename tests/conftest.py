import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

MINI = TESTS / "fixtures" / "mini"
REAL = TESTS / "fixtures" / "real"
HTTP = TESTS / "fixtures" / "http"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any test that forgets to inject a transport fails instead of going online."""
    def refuse(*args, **kwargs):
        raise RuntimeError("real network call attempted in a test; inject a transport")

    import nhl_dfs.data.http as http_mod

    monkeypatch.setattr(http_mod, "requests_transport", refuse)
    monkeypatch.setattr(http_mod, "_DEFAULT", None)
    try:
        import requests

        monkeypatch.setattr(requests, "get", refuse)
        monkeypatch.setattr(requests.Session, "request", refuse)
    except ImportError:
        pass


def fixture_bytes(name: str) -> bytes:
    return (HTTP / name).read_bytes()


class FakeTransport:
    """Replays recorded responses. routes: list of (url substring, status, body or callable(url))."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []
        self.sleeps: list[float] = []

    def __call__(self, url, headers, timeout_s):
        self.calls.append(url)
        for needle, status, body in self.routes:
            if needle in url:
                if isinstance(body, BaseException):
                    raise body
                data = body(url) if callable(body) else body
                return status, data, {}
        raise AssertionError(f"no recorded response for {url}")


@pytest.fixture
def make_cache(tmp_path):
    from nhl_dfs.data.http import HttpCache, load_sources_config

    def build(routes, *, config=None, offline=False, clock=None):
        transport = FakeTransport(routes)
        cache = HttpCache(
            tmp_path / "raw",
            config=config or load_sources_config(),
            transport=transport,
            offline=offline,
            clock=clock,
            sleep=transport.sleeps.append,
        )
        return cache, transport

    return build


def real_pair(mode: str, date: str | None = None) -> tuple[Path, Path]:
    """Real (DKSalaries.csv, DKEntries.csv) for a mode: the latest date, or exactly `date`
    for tests whose assertions depend on one slate's data. Skips loudly when absent."""
    pattern = f"{date or '*'}/{mode}/DKSalaries.csv"
    dirs = sorted(p.parent for p in REAL.glob(pattern) if (p.parent / "DKEntries.csv").exists())
    if not dirs:
        msg = f"REAL FIXTURE MISSING: tests/fixtures/real/{date or '<date>'}/{mode}/DKSalaries.csv + DKEntries.csv"
        warnings.warn(msg)
        pytest.skip(msg)
    return dirs[-1] / "DKSalaries.csv", dirs[-1] / "DKEntries.csv"


def mini_pair(mode: str) -> tuple[Path, Path]:
    return MINI / mode / "DKSalaries.csv", MINI / mode / "DKEntries.csv"
