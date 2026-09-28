"""Observation log: one JSON line per fetch (and later per parsed fact).

Times are ISO-8601 UTC strings. Stored under data/raw/observations/ so every
captured byte stays inside data/raw/.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from nhl_dfs.contracts.statuses import ObsStatus

DEFINITION_VERSION = "c1.1"


@dataclass(frozen=True)
class Observation:
    source: str
    source_player_id: str | None
    game_id: str | None
    observed_at: str  # when the fact was true per the source (fetch time if unknown)
    published_at: str | None  # the source's own timestamp, if it gives one
    fetched_at: str
    valid_from: str
    raw_hash: str | None
    definition_version: str
    status: ObsStatus
    url: str | None = None
    detail: str | None = None


def append(obs: Observation, root: Path) -> Path:
    """Append to <root>/observations/<fetched date>.jsonl; returns the file path."""
    path = Path(root) / "observations" / f"{obs.fetched_at[:10]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(obs)
    record["status"] = obs.status.value
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def read_all(root: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((Path(root) / "observations").glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            out.extend(json.loads(line) for line in f if line.strip())
    return out
