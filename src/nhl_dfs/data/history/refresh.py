"""The in-run incremental refresh of the history store (C39, backlog B70).

After a run has published its first file, Phase B can bring the store up to the last finished day: fetch the NHL per-game
reports for the season in progress from a couple of days before the newest stored game, and merge them in. Rules:

  * only the regular season in progress at the as-of date (a season over, or not yet begun, has nothing to fetch);
  * NHL reports only: no MoneyPuck zips (flag 33), no box-score cross-check; the new rows are Tier B;
  * completed Eastern days only (`status.completed_through`), and the last `refresh_overlap_days` are fetched again so a
    game stored while still in progress is corrected;
  * its own call budget (`history.refresh_max_calls`) on its own HttpCache, so it never spends the run's calls;
  * STAGED: the season's files are copied to a temp folder, the fetch and the combine write only there, and the changed tables
    are promoted into the real store from the calling thread only after the worker finished inside the budget and the result
    holds at least what the store held. A timeout or a failure leaves the real store byte for byte as it was, and a worker
    that outlives the budget can only ever touch the temp folder;
  * merge by key and never delete (`nhl_reports.backfill(upsert=True)`).

It returns a RefreshResult and never raises.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from nhl_dfs.data.history import nhl_reports, regular_season_complete, season_bounds, season_of
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.history import status as status_mod
from nhl_dfs.data.http import HttpCache, load_sources_config

STAGED_KINDS = ("nhl_skater_games", "nhl_goalie_games", "mp_skater_games", "mp_goalie_games", "line_games",
                "skater_games", "goalie_games")
PROMOTED_KINDS = ("nhl_skater_games", "nhl_goalie_games", "skater_games", "goalie_games")  # the combined tables last


@dataclass
class RefreshResult:
    outcome: str  # REFRESHED, NO_NEW_GAMES, SKIPPED, FAILED or TIMED_OUT
    reason: str = ""
    season: int | None = None
    since: date | None = None
    through: date | None = None  # the last Eastern day counted complete
    games_added: int = 0  # distinct regular-season games new to the store
    rows_added: int = 0  # skater-game rows new to the store
    last_regular_before: date | None = None
    last_regular_after: date | None = None
    calls: int = 0
    elapsed_s: float = 0.0

    def as_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d is not None else None
        return {"outcome": self.outcome, "reason": self.reason, "season": self.season, "since": iso(self.since),
                "through": iso(self.through), "games_added": self.games_added, "rows_added": self.rows_added,
                "last_regular_before": iso(self.last_regular_before), "last_regular_after": iso(self.last_regular_after),
                "calls": self.calls, "elapsed_s": round(self.elapsed_s, 1)}

    def line(self) -> str:
        return render_line(self.as_dict())


def render_line(d: dict) -> str:
    """One plain sentence for the outcome of a refresh, from the manifest dict form."""
    o = d["outcome"]
    if o == "REFRESHED":
        return (f"REFRESHED: +{d['games_added']} regular-season game(s) ({d['rows_added']} skater rows), newest regular game "
                f"{d['last_regular_before']} -> {d['last_regular_after']}; {d['calls']} request(s) in {d['elapsed_s']}s; NHL "
                "reports only, so the new rows carry league priors for the MoneyPuck-only columns.")
    if o == "NO_NEW_GAMES":
        return (f"NO_NEW_GAMES: fetched the NHL reports from {d['since']} through {d['through']} ({d['calls']} request(s), "
                f"{d['elapsed_s']}s) and found nothing the store did not hold.")
    return f"{o}: {d.get('reason') or 'no reason recorded'}"


def _season_games(store_root, season: int, regimes) -> tuple[set[int], date | None, int]:
    """(distinct regular game ids, newest regular game date, skater rows) of one season in the combined skater table."""
    sk = store_mod.read("skater_games", [season], root=store_root, columns=["game_id", "game_date", "regime"])
    if sk.empty:
        return set(), None, 0
    reg = sk[sk["regime"].isin(list(regimes))]
    return {int(g) for g in reg["game_id"]}, (max(reg["game_date"]) if len(reg) else None), len(sk)


def _keys(df: pd.DataFrame) -> set:
    return set(zip(df["nhl_id"], df["game_id"])) if len(df) else set()


def _same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    if a.empty and b.empty:
        return True
    if a.shape != b.shape or list(a.columns) != list(b.columns):
        return False
    keys = [k for k in ("nhl_id", "game_id") if k in a.columns]
    return a.sort_values(keys).reset_index(drop=True).equals(b.sort_values(keys).reset_index(drop=True))


def _own_cache(cache: HttpCache | None, cfg: dict, max_calls: int) -> HttpCache:
    """Same cache folder, transport and clocks as the run's cache, but its own call counter and cap."""
    base = cache.config if cache is not None else cfg
    cfg2 = {**base, "http": {**base["http"], "max_calls_per_run": int(max_calls)}}
    if cache is None:
        return HttpCache(config=cfg2)
    return HttpCache(cache.root, config=cfg2, transport=cache.transport, offline=cache.offline, clock=cache.clock,
                     sleep=cache.sleep, force_refresh=cache.force_refresh)


def _promote(stage, store_root, season: int) -> None:
    """Move the changed tables from the staging folder into the store, one atomic replace per file, combined tables last."""
    for kind in PROMOTED_KINDS:
        src = store_mod.path_for(kind, season, root=stage)
        if not src.exists():
            continue
        dst = store_mod.path_for(kind, season, root=store_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dst.parent, suffix=".parquet.tmp")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def refresh_incremental(as_of: date, *, now: datetime | None = None, store_root=None, cache: HttpCache | None = None,
                        cfg: dict | None = None, budget_s: float = 20.0, regimes=None) -> RefreshResult:
    """Bring the store current to the last finished day; see the module docstring. Never raises."""
    t0 = time.perf_counter()
    res = RefreshResult("FAILED")
    stage = None
    try:
        cfg = cfg if cfg is not None else load_sources_config()
        h = cfg["history"]
        now = now or datetime.now(timezone.utc)
        if regimes is None:
            from nhl_dfs.models.rates import load_model_config

            regimes = load_model_config().get("regimes", ["regular"])
        season = season_of(as_of)
        res.season = season
        if regular_season_complete(season, as_of):
            res.outcome, res.reason = "SKIPPED", f"the regular season is over at {as_of}; playoff games feed no model"
            return res
        through = status_mod.completed_through(now, cfg)
        res.through = through
        start = season_bounds(season)[0]
        if through < start:
            res.outcome, res.reason = "SKIPPED", f"no finished day of the season yet (through {through})"
            return res
        before_games, before_last, before_rows = _season_games(store_root, season, regimes)
        res.last_regular_before = before_last
        overlap = int(h.get("refresh_overlap_days", 2))
        since = max(start, before_last - timedelta(days=overlap)) if before_last is not None else start
        res.since = since
        if since > through:
            res.outcome, res.reason = "SKIPPED", f"newest stored game {before_last} is already the last finished day ({through})"
            return res

        stage = tempfile.mkdtemp(prefix="nhl_history_refresh_")
        for kind in STAGED_KINDS:
            src = store_mod.path_for(kind, season, root=store_root)
            if src.exists():
                dst = store_mod.path_for(kind, season, root=stage)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        rc = _own_cache(cache, cfg, int(h.get("refresh_max_calls", 40)))
        box: dict = {}

        def worker() -> None:
            try:
                box["stats"] = nhl_reports.backfill([season], since=since, cache=rc, store_root=stage, cfg=cfg,
                                                    today=through + timedelta(days=1), moneypuck=False, crosscheck_games=0,
                                                    upsert=True)
            except BaseException as exc:  # reported, never raised out of the refresh
                box["error"] = exc

        th = threading.Thread(target=worker, name="nhl-history-refresh", daemon=True)
        th.start()
        th.join(timeout=budget_s)
        res.calls = rc.calls_made
        if th.is_alive():
            res.outcome, res.reason = "TIMED_OUT", (f"the NHL reports did not answer within {budget_s:.0f}s; the store is unchanged "
                                                    "and the next run tries again")
            stage = None  # the worker may still be writing there; it can only ever touch this temp folder
            return res
        if "error" in box:
            exc = box["error"]
            res.reason = f"{type(exc).__name__}: {str(exc)[:160]}; the store is unchanged"
            return res

        after_games, after_last, after_rows = _season_games(stage, season, regimes)
        frames = {k: (store_mod.read(k, [season], root=store_root), store_mod.read(k, [season], root=stage))
                  for k in ("skater_games", "goalie_games")}
        lost = sum(len(_keys(old) - _keys(new)) for old, new in frames.values())
        if lost or (before_last is not None and (after_last is None or after_last < before_last)):
            res.reason = (f"the refreshed tables lost {lost} stored game row(s) or moved the newest game backwards; "
                          "the store is unchanged")
            return res
        changed = not all(_same(new, old) for old, new in frames.values())
        res.games_added = len(after_games) - len(before_games)
        res.rows_added = after_rows - before_rows
        res.last_regular_after = after_last
        if not changed:
            res.outcome = "NO_NEW_GAMES"
            return res
        _promote(stage, store_root, season)
        res.outcome = "REFRESHED"
        return res
    except Exception as exc:  # a refresh must never stop a run
        res.outcome, res.reason = "FAILED", f"{type(exc).__name__}: {str(exc)[:160]}; the store is unchanged"
        return res
    finally:
        res.elapsed_s = time.perf_counter() - t0
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
