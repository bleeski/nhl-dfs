"""One cached, schema-checked, budgeted HTTP layer for every keyless source.

Raw bodies: <root>/<source>/<YYYY-MM-DD>/<sha256>.json|html, root = data/raw.
Per-source index: <root>/<source>/index.json maps URL -> latest valid body.
NHL_DFS_OFFLINE=1 serves the cache only (any age) and raises SourceUnavailable
on a miss. HTTP 403/404/5xx raise SourceUnavailable (never partial data); a
schema failure raises SourceSchemaError and the body is kept only for
diagnosis, never indexed or returned. Every fetch appends an Observation.
Times are UTC; timeouts and intervals are seconds.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from nhl_dfs.contracts.statuses import ObsStatus
from nhl_dfs.data.observations import DEFINITION_VERSION, Observation, append

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = REPO_ROOT / "data" / "raw"
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"

Transport = Callable[[str, dict, float], tuple[int, bytes, dict]]


class SourceUnavailable(Exception):
    """The source could not supply data: HTTP failure, offline miss, or budget."""


class SourceSchemaError(Exception):
    """The source answered, but not in the shape the parser requires.
    raw_path is the stored (unindexed) body when one was received."""

    def __init__(self, message: str, raw_path: Path | None = None):
        super().__init__(message)
        self.raw_path = raw_path


@dataclass(frozen=True)
class Fetched:
    data: Any
    fetched_at_utc: datetime
    from_cache: bool
    raw_path: Path
    raw_hash: str
    status: ObsStatus  # CURRENT, or STALE when served from cache past its TTL
    url: str


def load_sources_config(path: Path = SOURCES_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def requests_transport(url: str, headers: dict, timeout_s: float) -> tuple[int, bytes, dict]:
    import requests

    r = requests.get(url, headers=headers, timeout=timeout_s)
    return r.status_code, r.content, dict(r.headers)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class HttpCache:
    def __init__(
        self,
        root: Path | None = None,
        *,
        config: dict | None = None,
        transport: Transport | None = None,
        offline: bool | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        force_refresh: bool = False,
    ):
        cfg = config if config is not None else load_sources_config()
        self.config = cfg
        self.http = cfg["http"]
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.transport = transport if transport is not None else requests_transport
        self.offline = offline if offline is not None else os.environ.get("NHL_DFS_OFFLINE") == "1"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.force_refresh = force_refresh  # ignore cached bodies (probe, capture)
        self.calls_made = 0
        self._last_call_at: dict[str, float] = {}

    # -- public -------------------------------------------------------------------

    def get_json(self, url: str, *, source: str, ttl_s: float, schema: Callable[[Any], None]) -> Fetched:
        return self._get(url, source=source, ttl_s=ttl_s, schema=schema, kind="json")

    def get_text(self, url: str, *, source: str, ttl_s: float, schema: Callable[[Any], None]) -> Fetched:
        return self._get(url, source=source, ttl_s=ttl_s, schema=schema, kind="html")

    # -- internals ------------------------------------------------------------------

    def _index_path(self, source: str) -> Path:
        return self.root / source / "index.json"

    def _load_index(self, source: str) -> dict:
        p = self._index_path(source)
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _save_index(self, source: str, index: dict) -> None:
        p = self._index_path(source)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(index, f, indent=1, sort_keys=True)
        os.replace(tmp, p)

    def _observe(self, source, url, now, status, raw_hash=None, detail=None, published_at=None):
        append(
            Observation(
                source=source,
                source_player_id=None,
                game_id=None,
                observed_at=_iso(now),
                published_at=published_at,
                fetched_at=_iso(now),
                valid_from=_iso(now),
                raw_hash=raw_hash,
                definition_version=DEFINITION_VERSION,
                status=status,
                url=url,
                detail=detail,
            ),
            self.root,
        )

    @staticmethod
    def _decode(body: bytes, kind: str) -> Any:
        if kind == "json":
            return json.loads(body.decode("utf-8-sig"))
        return body.decode("utf-8", errors="replace")

    def _store(self, source: str, now: datetime, body: bytes, kind: str) -> tuple[Path, str]:
        sha = hashlib.sha256(body).hexdigest()
        path = self.root / source / now.strftime("%Y-%m-%d") / f"{sha}.{kind}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(body)
        return path, sha

    def _from_cache(self, url, source, ttl_s, schema, kind, now) -> Fetched | None:
        entry = self._load_index(source).get(url)
        if entry is None:
            return None
        fetched_at = datetime.fromisoformat(entry["fetched_at"].replace("Z", "+00:00"))
        age = (now - fetched_at).total_seconds()
        if not self.offline and age > ttl_s:
            return None
        path = self.root / entry["raw_path"]
        if not path.exists():
            return None
        try:
            data = self._decode(path.read_bytes(), kind)
            schema(data)
        except Exception as exc:
            raise SourceSchemaError(f"{source}: cached body no longer passes schema: {exc}") from exc
        status = ObsStatus.CURRENT if age <= ttl_s else ObsStatus.STALE
        self._observe(source, url, now, status, entry["raw_hash"], detail="cache")
        return Fetched(data, fetched_at, True, path, entry["raw_hash"], status, url)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        intervals = self.http.get("min_interval_s", {})
        gap = float(intervals.get(host, intervals.get("default", 0.0)))
        last = self._last_call_at.get(host)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_call_at[host] = time.monotonic()

    def _fetch(self, url: str) -> tuple[int, bytes]:
        headers = {"User-Agent": self.http["user_agent"], "Accept": "application/json, text/html;q=0.9"}
        retries = int(self.http.get("retries", 1))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if self.calls_made >= int(self.http["max_calls_per_run"]):
                raise SourceUnavailable("budget: max_calls_per_run reached")
            self.calls_made += 1
            self._throttle(url)
            try:
                status, body, _ = self.transport(url, headers, float(self.http["timeout_s"]))
            except Exception as exc:  # timeouts and connection errors are retried
                last_error = exc
            else:
                if status < 500:
                    return status, body
                last_error = SourceUnavailable(f"HTTP {status}")
            if attempt < retries:
                self.sleep(float(self.http.get("retry_after_s", 1.0)))
        raise SourceUnavailable(f"{type(last_error).__name__}: {last_error}")

    def _get(self, url, *, source, ttl_s, schema, kind) -> Fetched:
        now = self.clock()
        cached = None if (self.force_refresh and not self.offline) else self._from_cache(url, source, ttl_s, schema, kind, now)
        if cached is not None:
            return cached
        if self.offline:
            self._observe(source, url, now, ObsStatus.MISSING, detail="offline: not cached")
            raise SourceUnavailable(f"offline and not cached: {url}")
        try:
            status, body = self._fetch(url)
        except SourceUnavailable as exc:
            self._observe(source, url, now, ObsStatus.MISSING, detail=str(exc))
            raise
        if status != 200:
            self._observe(source, url, now, ObsStatus.MISSING, detail=f"HTTP {status}")
            raise SourceUnavailable(f"HTTP {status}: {url}")
        path, sha = self._store(source, now, body, kind)
        try:
            data = self._decode(body, kind)
            schema(data)
        except Exception as exc:
            self._observe(source, url, now, ObsStatus.MISSING, sha, detail=f"schema: {exc}")
            raise SourceSchemaError(f"{source}: {exc}", raw_path=path) from exc
        index = self._load_index(source)
        index[url] = {"raw_path": path.relative_to(self.root).as_posix(), "fetched_at": _iso(now), "raw_hash": sha}
        self._save_index(source, index)
        self._observe(source, url, now, ObsStatus.CURRENT, sha)
        return Fetched(data, now, False, path, sha, ObsStatus.CURRENT, url)


_DEFAULT: HttpCache | None = None


def default_cache() -> HttpCache:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = HttpCache()
    return _DEFAULT
