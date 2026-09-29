"""Parquet history store (C4): one file per (kind, season) under data/features/history/.

Kinds are normalized tables (docs/features.md): skater_games, goalie_games, line_games, and the
raw per-source tables the combiner reads (nhl_skater_games, nhl_goalie_games, mp_skater_games,
mp_goalie_games). season is the NHL eight-digit id, for example 20252026. The store is
gitignored: MoneyPuck data is licensed for non-commercial use and the files are large.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ROOT = REPO_ROOT / "data" / "features" / "history"


def _root(root) -> Path:
    return Path(root) if root is not None else DEFAULT_ROOT


def path_for(kind: str, season: int, *, root=None) -> Path:
    if not kind.replace("_", "").isalnum():
        raise ValueError(f"bad kind {kind!r}")
    return _root(root) / kind / f"{int(season)}.parquet"


def write(kind: str, season: int, df: pd.DataFrame, *, root=None) -> Path:
    """Replace (kind, season) atomically: write a temp file, then rename over the old one."""
    p = path_for(kind, season, root=root)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".parquet.tmp")
    os.close(fd)
    try:
        df.reset_index(drop=True).to_parquet(tmp, engine="pyarrow", index=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return p


def read(kind: str, seasons: Iterable[int], *, root=None, columns: list[str] | None = None) -> pd.DataFrame:
    """Concatenate the stored seasons; seasons with no file are skipped (see seasons_present)."""
    frames = []
    for s in sorted({int(x) for x in seasons}):
        p = path_for(kind, s, root=root)
        if p.exists():
            frames.append(pd.read_parquet(p, engine="pyarrow", columns=columns))
    if not frames:
        return pd.DataFrame(columns=columns) if columns else pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def seasons_present(kind: str, *, root=None) -> list[int]:
    d = _root(root) / kind
    if not d.is_dir():
        return []
    return sorted(int(p.stem) for p in d.glob("*.parquet") if p.stem.isdigit())
