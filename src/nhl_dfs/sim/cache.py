"""Chunked score arrays under runs/<id>/sim/ (C6): seed, hashes, and a memory cap.

Only the (n, P) integer base scores in tenths are kept, one .npy per fixed-size chunk, plus a
meta.json that names the seed, purpose, chunk size and the hashes of everything that determined
the draws. That is enough to score any lineup later (sim/score.py), and small: 20,000 scenarios
of a 308-person Classic slate are about 25 MB. A candidates x scenarios matrix is never stored.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import yaml

from nhl_dfs.sim import game as game_mod
from nhl_dfs.sim import score as score_mod

VERSION = 1


def spec_hash(slate: game_mod.SlateSpec, params, seed: int, purpose: str, n: int) -> str:
    """Hash of everything that determines the draws: config, games and their fitted rates, the
    person axis and each person's parameters, seed, purpose, n."""
    persons = []
    for k in sorted(params.persons):
        p = params.persons[k]
        persons.append([k, p.team, p.group, int(p.mean_tenths), int(p.sd_tenths)])
    payload = {
        "version": VERSION, "cfg": slate.cfg, "model_cfg_skaters": slate.model_cfg["skaters"],
        "games": [[g.key, g.home, g.away, g.game_type, asdict(g.rates)] for g in slate.games],
        "persons": persons, "seed": int(seed), "purpose": purpose, "n": int(n),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass
class CacheInfo:
    root: Path
    n: int
    seed: int
    purpose: str
    chunk_size: int
    chunks: int
    person_keys: list[str]
    spec_sha256: str
    wall_s: float = 0.0
    summary: dict = field(default_factory=dict)  # per-person and per-team means for the CLI report

    @property
    def bytes(self) -> int:
        return sum(f.stat().st_size for f in self.root.glob("chunk_*.npy"))


def build(root: Path, slate: game_mod.SlateSpec, params, n: int, seed: int, purpose: str = "design") -> CacheInfo:
    """Simulate n scenarios chunk by chunk and write each chunk's base scores. Peak memory is one
    chunk of outcomes, never the whole run."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("chunk_*.npy"):
        old.unlink()
    t0 = time.perf_counter()
    prep = game_mod.prepare(slate, params)
    game_mod.check_memory(prep, slate.cfg)
    sums = {k: np.zeros(len(prep.person_keys)) for k in ("dressed", "goals", "assists", "sog", "blocks", "points")}
    team = {k: np.zeros(len(prep.team_order)) for k in ("goals", "sog", "en")}
    games = {k: np.zeros(len(slate.games)) for k in ("home_win", "tie", "ot", "so")}
    chunks = 0
    for ci, size in enumerate(game_mod.chunk_bounds(n, slate.cfg)):
        o = game_mod.simulate_chunk(prep, size, seed, purpose, ci)
        base = score_mod.base_tenths(o)
        np.save(root / f"chunk_{ci:04d}.npy", base)
        sums["dressed"] += o.dressed.sum(axis=0)
        sums["goals"] += o.goals.sum(axis=0)
        sums["assists"] += o.assists.sum(axis=0)
        sums["sog"] += o.sog.sum(axis=0)
        sums["blocks"] += o.blocks.sum(axis=0)
        sums["points"] += base.sum(axis=0)
        team["goals"] += o.team_goals.sum(axis=0)
        team["sog"] += o.team_sog.sum(axis=0)
        team["en"] += o.team_en.sum(axis=0)
        games["home_win"] += o.game_home_win.sum(axis=0)
        games["tie"] += o.game_tie.sum(axis=0)
        games["ot"] += o.game_ot.sum(axis=0)
        games["so"] += o.game_so.sum(axis=0)
        chunks += 1
        del o, base
    info = CacheInfo(root, n, int(seed), purpose, int(slate.cfg["chunk_size"]), chunks, prep.person_keys,
                     spec_hash(slate, params, seed, purpose, n), time.perf_counter() - t0)
    info.summary = {
        "person_mean": {k: (v / n).tolist() for k, v in sums.items()},
        "team": {"keys": prep.team_order, **{k: (v / n).tolist() for k, v in team.items()}},
        "game": {"keys": [g.key for g in slate.games], **{k: (v / n).tolist() for k, v in games.items()}},
    }
    meta = {"version": VERSION, "n": n, "seed": int(seed), "purpose": purpose, "chunk_size": info.chunk_size, "chunks": chunks,
            "person_keys_sha256": hashlib.sha256("\n".join(prep.person_keys).encode()).hexdigest(),
            "spec_sha256": info.spec_sha256, "cfg_sha256": hashlib.sha256(yaml.safe_dump(slate.cfg, sort_keys=True).encode()).hexdigest(),
            "dtype": "int32", "unit": "tenths", "wall_s": round(info.wall_s, 3), "person_keys": prep.person_keys}
    (root / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(info.summary), encoding="utf-8")
    return info


def read_meta(root: Path) -> dict:
    return json.loads((Path(root) / "meta.json").read_text(encoding="utf-8"))


def is_current(root: Path, expected_spec_sha256: str) -> bool:
    try:
        return read_meta(root)["spec_sha256"] == expected_spec_sha256
    except (OSError, KeyError, ValueError):
        return False


def iter_base(root: Path):
    """Yield each chunk's (size, P) int32 base scores in order."""
    meta = read_meta(root)
    for ci in range(int(meta["chunks"])):
        yield np.load(Path(root) / f"chunk_{ci:04d}.npy")


def load_base(root: Path) -> np.ndarray:
    """All scenarios as one (n, P) int32 array (about 25 MB for 20,000 x 308)."""
    return np.concatenate(list(iter_base(root)), axis=0)
