"""Persisted scenarios for late swap and refresh (C9).

The C8 scenario pass simulates in memory. So that late swap and refresh can score a repair with the
same scenario objective, the pass also writes runs/<id>/scenario/:

- selection/ and referee/: the first `keep` scenarios of each stream as chunk_XXXX.npy, (size, P)
  int32 base tenths per PERSON, UNMASKED (the DTD participation mask is applied at use, once, from
  the probabilities of that moment; persisting a masked array would price DTD twice);
- selection/flags_XXXX.npy and referee/flags_XXXX.npy (backlog B25, B28; optional, absent in older caches):
  the same draws' indicators, sim/score.FLAG_NAMES (dressed, started, then one per DK bonus), packed along
  the draw axis: (F, P, ceil(size / 8)) uint8, np.packbits(bitorder="little"); read with `Loaded.flags`;
- meta.json: seed, counts, chunk size, the person axis, the game keys in slate order and one hash
  per game (its rates plus its two teams' ParamTable rows, so a changed team re-simulates only its
  own game: tests/test_late_swap_objective.py proves the games are independent draws);
- contests.json: each contest's payout curve (EXACT or declared PRIOR) exactly as C8 used it;
- meta.json `participation` (B26; optional): each person's play probability as the draws priced it (dress or
  start probability from the applied ParamTable, the participation mask, their product) and each goalie's start
  probability with its source (rotation, Daily Faceoff state, override);
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


def goalie_source(roles, team: str, person_key: str) -> tuple[str, str | None]:
    """(source, state) of a goalie's start probability: override, a Daily Faceoff state, the team page depth
    order, or the rotation (no report, or no role state)."""
    gr = (getattr(roles, "goalies", None) or {}).get(team)
    if gr is None:
        return "rotation (no role state)", None
    state = gr.state.value if gr.state is not None else None
    if any(str(n).startswith("override") for n in gr.notes):
        return "override", state
    if gr.report is not None and (gr.named or gr.confirmed or state in ("CONFLICTED", "OUT")):
        return f"Daily Faceoff {state}", state
    if gr.named and any("team page" in str(n) for n in gr.notes):
        return f"Daily Faceoff team page {state}", state
    if state == "CONFLICTED":
        return "Daily Faceoff CONFLICTED", state
    return "rotation", state


def participation_record(params, play: dict | None, roles=None) -> dict:
    """B26: what the draws priced, per person: the dress (skater) or start (goalie) probability of the applied
    ParamTable (the simulator's Bernoulli), the participation mask (1.0 when none) and their product; per goalie
    the start probability with its source. Settle grades these saved values."""
    play = play or {}
    persons, goalies = {}, {}
    for k, pp in sorted(params.persons.items()):
        g = pp.group
        if g == "G" and pp.goalie is not None:
            base = float(pp.goalie.p_start)
        elif pp.opportunity is not None:
            base = float(pp.opportunity.p_dress)
        else:
            continue
        mask = float(play.get(k, 1.0))
        persons[k] = {"group": g, "p_sim": round(base, 6), "mask": round(mask, 6), "p_play": round(base * mask, 6)}
        if g == "G":
            src, state = goalie_source(roles, pp.team, k)
            goalies[k] = {"team": pp.team, "p_start": round(base, 6), "source": src, "state": state}
    return {"format": 1, "persons": persons, "goalies": goalies,
            "note": "p_sim is the simulator's dress or start probability (UNMASKED draws); p_play = p_sim x mask"}


def _contest_json(c: ob.Contest) -> dict:
    return {"contest_id": c.contest_id, "name": c.name, "family": c.family, "field_size": c.field_size,
            "fee_cents": c.fee_cents, "prizes_cents": [int(x) for x in c.prizes_cents], "seats": [bool(x) for x in c.seats],
            "ticket_face_cents": c.ticket_face_cents, "payout_source": c.payout_source.value, "detail": c.detail}


def _contest_from_json(d: dict) -> ob.Contest:
    return ob.Contest(d["contest_id"], d["name"], d["family"], int(d["field_size"]), int(d["fee_cents"]),
                      np.asarray(d["prizes_cents"], np.int64), np.asarray(d["seats"], bool), d["ticket_face_cents"],
                      PayoutSource(d["payout_source"]), d["detail"])


def pack_flags(flags: np.ndarray) -> np.ndarray:
    """(size, P, F) bool -> (F, P, ceil(size / 8)) uint8, bits along the draw axis (little bit order)."""
    return np.packbits(np.ascontiguousarray(np.transpose(np.asarray(flags, bool), (2, 1, 0))), axis=-1, bitorder="little")


def unpack_flags(packed: np.ndarray, size: int) -> np.ndarray:
    """Inverse of pack_flags: (size, P, F) bool."""
    return np.transpose(np.unpackbits(packed, axis=-1, count=int(size), bitorder="little").astype(bool), (2, 1, 0))


def save_base(run_path: Path, purpose: str, base: np.ndarray, keep: int, chunk_size: int,
              flags: np.ndarray | None = None) -> dict:
    """Write the first `keep` scenarios of one stream (UNMASKED base), whole chunks where possible, right
    after they are simulated, so the pass never holds a second copy. flags: (>= n, P, F) bool indicators of the
    same draws (sim/score.bonus_flags), written with exactly the same chunking. Returns the stream's meta."""
    d = Path(run_path) / CACHE_DIR / purpose
    d.mkdir(parents=True, exist_ok=True)
    (Path(run_path) / CACHE_DIR / "meta.json").unlink(missing_ok=True)  # absent until save() completes
    for old in list(d.glob("chunk_*.npy")) + list(d.glob("flags_*.npy")):
        old.unlink()
    size = int(chunk_size)
    n = min(int(keep), int(base.shape[0]))
    if flags is not None:
        n = min(n, int(flags.shape[0]))
    if n >= size:
        n -= n % size
    chunks = 0
    for ci, a in enumerate(range(0, n, size)):
        b = min(n, a + size)
        np.save(d / f"chunk_{ci:04d}.npy", np.ascontiguousarray(base[a:b], dtype=np.int32))
        if flags is not None:
            np.save(d / f"flags_{ci:04d}.npy", pack_flags(flags[a:b]))
        chunks += 1
    out = {"n": n, "chunks": chunks}
    if flags is not None:
        out["flags"] = True
    return out


def save(run_path: Path, *, purposes: dict[str, dict], person_keys: list[str], slate, params, seed: int,
         chunk_size: int, contests: dict[str, ob.Contest], contest_family: dict[str, str], fields: dict[str, tuple[list, list]],
         n_opponents: dict[str, int], own_by: dict[str, dict], dup_by: dict[str, dict], field_cal: str,
         model_status: str, play_prob: dict | None = None, participation: dict | None = None) -> dict:
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
    if all(v.get("flags") for v in purposes.values()):
        from nhl_dfs.sim.score import FLAG_NAMES

        meta["flags"] = {"names": list(FLAG_NAMES), "layout": "(F, P, ceil(size/8)) uint8, np.packbits axis=-1 "
                         "bitorder=little over the draws of chunk_XXXX.npy", "file": "flags_XXXX.npy"}
    if participation is not None:
        meta["participation"] = participation
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

    @property
    def flag_names(self) -> list[str] | None:
        """sim/score.FLAG_NAMES as written, or None: an older cache without indicators (B25, B28)."""
        f = self.meta.get("flags")
        return list(f["names"]) if isinstance(f, dict) and f.get("names") else None

    def flags(self, purpose: str, n: int, start: int = 0) -> np.ndarray | None:
        """Indicators of scenarios start..start+n, (n, P, F) bool on meta person_keys; None when this cache has
        none or they cannot be read in full (the caller labels the fallback; never raises)."""
        return read_flags(self.root, self.meta, purpose, n, start, self.notes)

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


def read_flags(root: Path, meta: dict, purpose: str, n: int, start: int = 0, notes: list | None = None) -> np.ndarray | None:
    """(n, P, F) bool indicators of rows start..start+n of one stream, or None (older cache, a missing or
    mismatched chunk: reported in notes, never raised)."""
    notes = notes if notes is not None else []
    f = meta.get("flags")
    if not (isinstance(f, dict) and f.get("names")):
        return None
    names = list(f["names"])
    d = Path(root) / purpose
    try:
        chunk = int(meta["chunk_size"])
        total = int(meta["purposes"][purpose]["n"])
        parts, pos, end = [], 0, start + n
        for ci in range(int(meta["purposes"][purpose]["chunks"])):
            if pos >= end:
                break
            size = min(chunk, total - pos)
            lo, hi = max(start, pos), min(end, pos + size)
            if hi > lo:
                a = unpack_flags(np.load(d / f"flags_{ci:04d}.npy"), size)
                if a.shape[1:] != (len(meta["person_keys"]), len(names)):
                    notes.append(f"{purpose} flags chunk {ci}: shape {a.shape} does not match the cache")
                    return None
                parts.append(a[lo - pos: hi - pos])
            pos += size
    except (OSError, ValueError, KeyError, TypeError) as exc:
        notes.append(f"{purpose} flags unreadable ({type(exc).__name__}: {str(exc)[:80]})")
        return None
    if sum(len(p) for p in parts) < n:
        notes.append(f"{purpose} flags hold fewer than the {n} rows requested")
        return None
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
