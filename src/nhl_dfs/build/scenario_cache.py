"""Persisted scenarios for late swap and refresh (C9).

The C8 scenario pass simulates in memory. So that late swap and refresh can score a repair with the
same scenario objective, the pass also writes runs/<id>/scenario/:

- selection/ and referee/: the first `keep` scenarios of each stream as chunk_XXXX.npy, (size, P)
  int32 base tenths per PERSON, UNMASKED (the DTD participation mask is applied at use, once, from
  the probabilities of that moment; persisting a masked array would price DTD twice);
- meta.json: seed, counts, chunk size, the person axis, the game keys in slate order and one hash
  per game (its rates plus its two teams' ParamTable rows, so a changed team re-simulates only its
  own game: tests/test_late_swap_objective.py proves the games are independent draws);
- contests.json: each contest's payout curve (EXACT or declared PRIOR) exactly as C8 used it;
- fields.json: the field lineups and keys per family, opponents per contest, ownership and
  duplicate counts, FIELD_CALIBRATION; field specs are rebuilt deterministically at the scenario
  count in use (a sampled field's per-scenario draws depend on that count; recorded).

Nothing here is a decision; it is data for build/swap_objective.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.statuses import PayoutSource

CACHE_DIR = "scenario"
FORMAT = 1
PURPOSES = ("selection", "referee")


def game_hashes(slate, params) -> dict[str, str]:
    """game key -> hash of what determines that game's draws: simulator and model configs, the game's
    fitted rates, and every ParamTable column of the persons on its two teams."""
    from nhl_dfs.sim.cache import VERSION

    frame = params.to_frame().sort_values("person_key").reset_index(drop=True)
    cfg = json.dumps({"cfg": slate.cfg, "model_cfg": slate.model_cfg, "version": VERSION}, sort_keys=True, default=str)
    out = {}
    for g in slate.games:
        sub = frame[frame["team"].isin([g.home, g.away])].reset_index(drop=True)
        rows = pd.util.hash_pandas_object(sub, index=False).to_numpy().tobytes() + "|".join(sub.columns).encode()
        spec = json.dumps([g.key, g.home, g.away, g.game_type, asdict(g.rates)], sort_keys=True, default=str)
        out[g.key] = hashlib.sha256(cfg.encode() + spec.encode() + rows).hexdigest()
    return out


def _contest_json(c: ob.Contest) -> dict:
    return {"contest_id": c.contest_id, "name": c.name, "family": c.family, "field_size": c.field_size,
            "fee_cents": c.fee_cents, "prizes_cents": [int(x) for x in c.prizes_cents], "seats": [bool(x) for x in c.seats],
            "ticket_face_cents": c.ticket_face_cents, "payout_source": c.payout_source.value, "detail": c.detail}


def _contest_from_json(d: dict) -> ob.Contest:
    return ob.Contest(d["contest_id"], d["name"], d["family"], int(d["field_size"]), int(d["fee_cents"]),
                      np.asarray(d["prizes_cents"], np.int64), np.asarray(d["seats"], bool), d["ticket_face_cents"],
                      PayoutSource(d["payout_source"]), d["detail"])


def save_base(run_path: Path, purpose: str, base: np.ndarray, keep: int, chunk_size: int) -> dict:
    """Write the first `keep` scenarios of one stream (UNMASKED base), whole chunks where possible, right
    after they are simulated, so the pass never holds a second copy. Returns the stream's meta."""
    d = Path(run_path) / CACHE_DIR / purpose
    d.mkdir(parents=True, exist_ok=True)
    (Path(run_path) / CACHE_DIR / "meta.json").unlink(missing_ok=True)  # absent until save() completes
    for old in d.glob("chunk_*.npy"):
        old.unlink()
    size = int(chunk_size)
    n = min(int(keep), int(base.shape[0]))
    if n >= size:
        n -= n % size
    chunks = 0
    for ci, a in enumerate(range(0, n, size)):
        np.save(d / f"chunk_{ci:04d}.npy", np.ascontiguousarray(base[a:min(n, a + size)], dtype=np.int32))
        chunks += 1
    return {"n": n, "chunks": chunks}


def save(run_path: Path, *, purposes: dict[str, dict], person_keys: list[str], slate, params, seed: int,
         chunk_size: int, contests: dict[str, ob.Contest], contest_family: dict[str, str], fields: dict[str, tuple[list, list]],
         n_opponents: dict[str, int], own_by: dict[str, dict], dup_by: dict[str, dict], field_cal: str,
         model_status: str, play_prob: dict | None = None) -> dict:
    """Write the cache's meta, contests and fields (after save_base wrote each stream); meta.json last,
    so a cache without it is absent, never half-read."""
    root = Path(run_path) / CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    purposes = {p: {**v, "code": int(slate.cfg["purposes"][p])} for p, v in purposes.items()}
    meta = {"format": FORMAT, "seed": int(seed), "chunk_size": int(chunk_size), "purposes": purposes,
            "person_keys": list(person_keys), "games": [g.key for g in slate.games], "game_sha256": game_hashes(slate, params),
            "game_sources": {g.key: g.rates.source for g in slate.games},
            "game_rates": {g.key: asdict(g.rates) for g in slate.games}, "model_status": model_status,
            "field_calibration": field_cal,
            "play_prob": play_prob}  # the participation the selection priced (C10: the controller reuses it)
    (root / "contests.json").write_text(json.dumps({cid: _contest_json(c) for cid, c in contests.items()}), encoding="utf-8")
    fam_fields = {fam: {"lineups": [list(x) for x in lus], "keys": list(ks)} for fam, (lus, ks) in fields.items()}
    (root / "fields.json").write_text(json.dumps({
        "families": fam_fields, "contest_family": contest_family, "n_opponents": n_opponents,
        "own_by": own_by, "dup_by": dup_by}), encoding="utf-8")
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"path": str(root), "purposes": purposes, "bytes": sum(f.stat().st_size for f in root.rglob("*") if f.is_file())}


@dataclass
class Loaded:
    root: Path
    run_id: str
    meta: dict
    contests: dict[str, ob.Contest]
    families: dict[str, tuple[list[tuple[str, ...]], list[str]]]
    contest_family: dict[str, str]
    n_opponents: dict[str, int]
    own_by: dict[str, dict]
    dup_by: dict[str, dict]
    notes: list[str] = field(default_factory=list)

    @property
    def seed(self) -> int:
        return int(self.meta["seed"])

    @property
    def person_keys(self) -> list[str]:
        return list(self.meta["person_keys"])

    def n(self, purpose: str) -> int:
        return int(self.meta["purposes"].get(purpose, {}).get("n", 0))

    def base(self, purpose: str, n: int, start: int = 0) -> np.ndarray:
        """Scenarios start..start+n of a stream, (n, P) int32 on meta person_keys (QA rounds read their own
        referee block, C10)."""
        d = self.root / purpose
        parts, pos, end = [], 0, start + n
        for ci in range(int(self.meta["purposes"][purpose]["chunks"])):
            if pos >= end:
                break
            a = np.load(d / f"chunk_{ci:04d}.npy")
            lo, hi = max(start, pos), min(end, pos + len(a))
            if hi > lo:
                parts.append(a[lo - pos: hi - pos])
            pos += len(a)
        have = sum(len(p) for p in parts)
        if have < n:
            raise ValueError(f"scenario cache holds {pos} {purpose} scenarios, rows {start}..{end} requested")
        return np.concatenate(parts, axis=0)


def load(run_path: Path, run_id: str) -> Loaded:
    """Raises (OSError, KeyError, ValueError) when the cache is absent or unreadable."""
    root = Path(run_path) / CACHE_DIR
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    if int(meta.get("format", 0)) != FORMAT:
        raise ValueError(f"scenario cache format {meta.get('format')} is not {FORMAT}")
    contests = {cid: _contest_from_json(d) for cid, d in json.loads((root / "contests.json").read_text(encoding="utf-8")).items()}
    f = json.loads((root / "fields.json").read_text(encoding="utf-8"))
    fams = {fam: ([tuple(x) for x in v["lineups"]], list(v["keys"])) for fam, v in f["families"].items()}
    for purpose in PURPOSES:
        spec = meta["purposes"][purpose]
        for ci in range(int(spec["chunks"])):
            if not (root / purpose / f"chunk_{ci:04d}.npy").exists():
                raise OSError(f"scenario cache chunk {purpose}/{ci} missing")
    return Loaded(root, run_id, meta, contests, fams, dict(f["contest_family"]), {k: int(v) for k, v in f["n_opponents"].items()},
                  f["own_by"], f["dup_by"])


def find(runs_root: Path, run_id: str, max_depth: int = 8) -> tuple[Loaded | None, list[str]]:
    """The nearest scenario cache on run_id or its parent chain (manifest parent_run_id). Returns
    (cache or None, one note per run looked at)."""
    notes = []
    rid = run_id
    for _ in range(max_depth):
        path = Path(runs_root) / rid
        try:
            got = load(path, rid)
            notes.append(f"scenario cache found on run {rid}")
            return got, notes
        except FileNotFoundError:
            notes.append(f"run {rid}: no scenario cache")
        except (OSError, KeyError, ValueError) as exc:
            notes.append(f"run {rid}: scenario cache unreadable ({type(exc).__name__}: {str(exc)[:100]})")
        try:
            parent = json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("parent_run_id")
        except (OSError, ValueError):
            parent = None
        if not parent:
            break
        rid = parent
    return None, notes
