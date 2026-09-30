"""NHL per-game reports (C4 Tier B, and the backbone of Tier A; plan section 3).

Skaters: stats REST skater/summary, skater/timeonice and skater/realtime with isGame=true, one
limit=-1 call per report per date window. Goalies: goalie/summary with isGame=true (decisions,
saves, shots and goals against). Box scores cross-check a few games per season (SOG, blocks,
goals, assists, saves, decision) instead of being crawled for every game.

The stats REST reports `total` at most 10,000: a window whose total reaches that cap, or whose
row count differs from its total, is split in half until it fits. A single day that still does
not fit raises rather than silently truncating.

Provides goals, assists (total only), SOG, blocks, PP and SH points, TOI by strength (the NHL
definition) and shifts. It does NOT provide A1/A2, shot quality or shared ice; the combiner
(data/history/combine.py) fills those from league priors with <col>_missing = 1.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from nhl_dfs.data.history import nhl_season, regime_of, season_bounds
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.http import HttpCache, SourceUnavailable, load_sources_config
from nhl_dfs.data.sources import nhl


class TruncatedWindow(RuntimeError):
    pass


@dataclass
class BackfillStats:
    seasons: list[int]
    windows: int = 0
    splits: int = 0
    calls: int = 0
    rows: dict[str, int] = field(default_factory=dict)  # "<kind>/<season>" -> rows written
    tiers: dict[str, dict[str, int]] = field(default_factory=dict)  # season -> {"A": n, "B": n}
    moneypuck: dict[str, str] = field(default_factory=dict)  # "<kind>/<season>" -> status
    crosscheck: dict[str, dict[str, int]] = field(default_factory=dict)  # game_id -> {"checked": n, "mismatch": n}
    elapsed_s: float = 0.0
    messages: list[str] = field(default_factory=list)
    raw_root: str = ""  # the HTTP cache root the backfill used (B2 eviction)


# -- windows ---------------------------------------------------------------------------------------

def windows_for(season: int, *, days: int, since: date | None = None, today: date | None = None) -> list[tuple[date, date]]:
    """Date windows covering the season (Sept 1 to July 31), clipped to [since, today - 1]."""
    lo, hi = season_bounds(season)
    if since is not None:
        lo = max(lo, since)
    if today is not None:
        hi = min(hi, today - timedelta(days=1))  # completed days only
    out = []
    d = lo
    while d <= hi:
        e = min(d + timedelta(days=days - 1), hi)
        out.append((d, e))
        d = e + timedelta(days=1)
    return out


def fetch_window(entity: str, report: str, d0: date, d1: date, cache: HttpCache, cap: int, stats: BackfillStats) -> list[dict]:
    rows, total = nhl.report_window(entity, report, d0, d1, cache=cache)
    if total < cap and len(rows) == total:
        return rows
    if d0 >= d1:
        raise TruncatedWindow(f"{entity}/{report} {d0}: {len(rows)} rows of total {total} (cap {cap}) in one day")
    stats.splits += 1
    mid = d0 + (d1 - d0) // 2
    return (fetch_window(entity, report, d0, mid, cache, cap, stats)
            + fetch_window(entity, report, mid + timedelta(days=1), d1, cache, cap, stats))


# -- normalization -------------------------------------------------------------------------------

def _game_keys(df: pd.DataFrame) -> pd.DataFrame:
    gid = df["game_id"].astype("int64")
    df["season"] = (gid // 1_000_000).map(nhl_season)
    df["game_type"] = (gid // 10_000 % 100).astype(int)
    df["regime"] = gid.map(regime_of)
    return df


def normalize_skaters(toi: list[dict], realtime: list[dict], summary: list[dict]) -> pd.DataFrame:
    """One row per (nhl_id, game_id) from the three per-game skater reports."""
    t = pd.DataFrame(toi)
    if t.empty:
        return pd.DataFrame()
    base = pd.DataFrame({
        "nhl_id": t["playerId"].astype("int64"), "game_id": t["gameId"].astype("int64"),
        "name": t.get("skaterFullName", pd.Series([""] * len(t))).astype(str),
        "game_date": pd.to_datetime(t["gameDate"]).dt.date,
        "team": t.get("teamAbbrev", pd.Series([None] * len(t))), "opponent": t.get("opponentTeamAbbrev"),
        "home": t.get("homeRoad", pd.Series([""] * len(t))).astype(str).eq("H"),
        "position": t.get("positionCode"),
        "toi_s": t.get("timeOnIce", t["evTimeOnIce"] + t["ppTimeOnIce"] + t["shTimeOnIce"]).astype(float),
        "toi_ev_s": t["evTimeOnIce"].astype(float), "toi_pp_s": t["ppTimeOnIce"].astype(float),
        "toi_sh_s": t["shTimeOnIce"].astype(float), "shifts": t["shifts"].astype(float),
    })
    s = pd.DataFrame(summary)
    if not s.empty:
        s = pd.DataFrame({"nhl_id": s["playerId"].astype("int64"), "game_id": s["gameId"].astype("int64"),
                          "goals": s["goals"].astype(float), "assists": s["assists"].astype(float),
                          "sog": s["shots"].astype(float), "pp_points": s["ppPoints"].astype(float),
                          "sh_points": s["shPoints"].astype(float)})
        base = base.merge(s, on=["nhl_id", "game_id"], how="left")
    r = pd.DataFrame(realtime)
    if not r.empty:
        r = pd.DataFrame({"nhl_id": r["playerId"].astype("int64"), "game_id": r["gameId"].astype("int64"),
                          "blocks": r["blockedShots"].astype(float)})
        base = base.merge(r, on=["nhl_id", "game_id"], how="left")
    for c in ("goals", "assists", "sog", "pp_points", "sh_points", "blocks"):
        if c not in base:
            base[c] = float("nan")
    return _game_keys(base).drop_duplicates(["nhl_id", "game_id"]).reset_index(drop=True)


def normalize_goalies(rows: list[dict]) -> pd.DataFrame:
    g = pd.DataFrame(rows)
    if g.empty:
        return pd.DataFrame()
    dec = pd.Series([None] * len(g), dtype=object)
    dec[g["wins"].astype(int) > 0] = "W"
    dec[g["losses"].astype(int) > 0] = "L"
    dec[g["otLosses"].astype(int) > 0] = "O"
    out = pd.DataFrame({
        "nhl_id": g["playerId"].astype("int64"), "game_id": g["gameId"].astype("int64"),
        "name": g.get("goalieFullName", pd.Series([""] * len(g))).astype(str),
        "game_date": pd.to_datetime(g["gameDate"]).dt.date, "team": g["teamAbbrev"],
        "opponent": g.get("opponentTeamAbbrev"), "home": g.get("homeRoad", pd.Series([""] * len(g))).astype(str).eq("H"),
        "started": g["gamesStarted"].astype(int) > 0, "toi_s": g["timeOnIce"].astype(float), "decision": dec.to_numpy(),
        "saves": g["saves"].astype(float), "shots_against": g["shotsAgainst"].astype(float),
        "goals_against": g["goalsAgainst"].astype(float), "shutout": g["shutouts"].astype(int) > 0,
    })
    return _game_keys(out).drop_duplicates(["nhl_id", "game_id"]).reset_index(drop=True)


# -- cross-check -----------------------------------------------------------------------------------

def crosscheck(box: nhl.BoxScore, skaters: pd.DataFrame, goalies: pd.DataFrame) -> dict[str, int]:
    """Compare report rows with one box score. Counts players checked and fields that disagree."""
    out = {"checked": 0, "mismatch": 0}
    sk = skaters[skaters["game_id"] == box.game_id].set_index("nhl_id")
    for p in box.skaters:
        if p.nhl_id not in sk.index:
            continue
        r = sk.loc[p.nhl_id]
        out["checked"] += 1
        out["mismatch"] += sum(int(float(r[c]) != float(v)) for c, v in
                               (("goals", p.goals), ("assists", p.assists), ("sog", p.sog), ("blocks", p.blocks)))
    gl = goalies[goalies["game_id"] == box.game_id].set_index("nhl_id")
    for g in box.goalies:
        if g.nhl_id not in gl.index:
            continue
        r = gl.loc[g.nhl_id]
        out["checked"] += 1
        out["mismatch"] += int(float(r["saves"]) != g.saves) + int((r["decision"] or None) != (g.decision or None))
    return out


# -- backfill ----------------------------------------------------------------------------------------

def _merge_since(old: pd.DataFrame, new: pd.DataFrame, since: date | None) -> pd.DataFrame:
    if since is None or old.empty:
        return new
    keep = old[old["game_date"] < since]
    return pd.concat([keep, new], ignore_index=True).drop_duplicates(["nhl_id", "game_id"], keep="last")


def backfill(seasons, *, since: date | None = None, cache: HttpCache | None = None, store_root=None,
             cfg: dict | None = None, today: date | None = None, crosscheck_games: int | None = None,
             moneypuck: bool | None = None, raw_root=None, mp_transport=None) -> BackfillStats:
    """Fetch per-game reports for each season (incremental from `since`), store them, then build
    the combined skater_games / goalie_games tables (Tier A where MoneyPuck is enabled and has the
    season, Tier B otherwise)."""
    from nhl_dfs.data.history import combine

    t0 = time.perf_counter()
    cfg = cfg if cfg is not None else load_sources_config()
    h = cfg["history"]
    today = today or datetime.now(timezone.utc).date()
    if cache is None:
        c2 = {**cfg, "http": {**cfg["http"], "max_calls_per_run": int(h["max_calls_per_backfill"])}}
        cache = HttpCache(config=c2)
    stats = BackfillStats(seasons=[int(s) for s in seasons])
    cap = int(h["total_cap"])
    n_check = int(h["crosscheck_boxscores_per_season"] if crosscheck_games is None else crosscheck_games)
    for season in stats.seasons:
        wins = windows_for(season, days=int(h["window_days"]), since=since, today=today)
        stats.windows += len(wins)
        got: dict[str, list[dict]] = {"timeonice": [], "realtime": [], "summary": [], "goalie": []}
        for d0, d1 in wins:
            for rep in ("timeonice", "realtime", "summary"):
                got[rep] += fetch_window("skater", rep, d0, d1, cache, cap, stats)
            got["goalie"] += fetch_window("goalie", "summary", d0, d1, cache, cap, stats)
        sk = normalize_skaters(got["timeonice"], got["realtime"], got["summary"])
        gl = normalize_goalies(got["goalie"])
        sk = _merge_since(store_mod.read("nhl_skater_games", [season], root=store_root), sk, since)
        gl = _merge_since(store_mod.read("nhl_goalie_games", [season], root=store_root), gl, since)
        if not sk.empty:
            store_mod.write("nhl_skater_games", season, sk, root=store_root)
        if not gl.empty:
            store_mod.write("nhl_goalie_games", season, gl, root=store_root)
        stats.rows[f"nhl_skater_games/{season}"] = len(sk)
        stats.rows[f"nhl_goalie_games/{season}"] = len(gl)
        for gid in sorted(sk["game_id"].unique())[:n_check] if not sk.empty else []:
            try:
                box = nhl.boxscore(int(gid), cache=cache)
                stats.crosscheck[str(gid)] = crosscheck(box, sk, gl)
            except Exception as exc:  # a cross-check failure is reported, never fatal
                stats.messages.append(f"box score {gid}: {type(exc).__name__}: {str(exc)[:80]}")
        combine.moneypuck_season(season, stats, cfg=cfg, store_root=store_root, raw_root=raw_root,
                                 enabled=moneypuck, transport=mp_transport)
        res = combine.combine_season(season, store_root=store_root)
        stats.rows.update({f"{k}/{season}": v for k, v in res.rows.items()})
        stats.tiers[str(season)] = res.tiers
    stats.calls = cache.calls_made  # network requests actually made (cache hits excluded)
    stats.raw_root = str(cache.root)  # B2: the cache the eviction pass cleans after a clean backfill
    stats.elapsed_s = round(time.perf_counter() - t0, 1)
    return stats


# -- raw cache eviction (backlog B2) --------------------------------------------------------------------------------

REPORT_SOURCES = {"nhl_report": ("skater", ("timeonice", "realtime", "summary"), "nhl_skater_games"),
                  "nhl_goalie_report": ("goalie", ("summary",), "nhl_goalie_games")}


@dataclass
class EvictStats:
    """Per report source: bytes before and after, index entries dropped, body files deleted, and what was kept."""
    sources: dict[str, dict] = field(default_factory=dict)
    moneypuck_tmp_deleted: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        out = []
        for src, v in sorted(self.sources.items()):
            out.append(f"raw {src}: {v['bytes_before'] / 1e6:.1f} MB -> {v['bytes_after'] / 1e6:.1f} MB"
                       f"{' (dry run: would be)' if self.dry_run else ''}; index entries dropped {v['entries_dropped']}, "
                       f"files deleted {v['files_deleted']}; kept {v['kept_canonical']} completed-season window(s), "
                       f"{v['kept_fresh']} within TTL, {v['kept_unstored']} not yet in the store")
        if self.moneypuck_tmp_deleted:
            out.append(f"raw moneypuck: {self.moneypuck_tmp_deleted} partial download(s) (.tmp) deleted")
        return out


def _split_tree(d0: date, d1: date):
    """Every window fetch_window can request for (d0, d1): the window and each half it may split into."""
    yield d0, d1
    if d0 < d1:
        mid = d0 + (d1 - d0) // 2
        yield from _split_tree(d0, mid)
        yield from _split_tree(mid + timedelta(days=1), d1)


def canonical_urls(season: int, cfg: dict) -> dict[str, set[str]]:
    """source -> the report URLs a full backfill of a COMPLETED season can request (full-season windows and all their
    splits). That set never changes once the season is over, so its bodies rebuild the season's store offline."""
    days = int(cfg["history"]["window_days"])
    out: dict[str, set[str]] = {}
    for src, (entity, reports, _) in REPORT_SOURCES.items():
        urls = out.setdefault(src, set())
        for w0, w1 in windows_for(season, days=days):
            for d0, d1 in _split_tree(w0, w1):
                for rep in reports:
                    urls.add(nhl._report_url(cfg, entity, rep, d0, d1, 0, -1))
    return out


def _url_start(url: str) -> date | None:
    import re
    from urllib.parse import unquote

    m = re.search(r'gameDate>="(\d{4}-\d{2}-\d{2})"', unquote(url))
    return date.fromisoformat(m.group(1)) if m else None


def _tree_bytes(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.is_dir() else 0


def evict_raw(cache_root, *, cfg: dict | None = None, store_root=None, today: date | None = None,
              now: datetime | None = None, dry_run: bool = False, mp_root=None) -> EvictStats:
    """After a clean backfill: drop report index entries that are past their TTL, are not a completed season's
    canonical window, and whose season's Parquet store was written after the body was fetched (the rows are in the
    store); then delete every body no index entry of that source references (a body shared by several entries goes
    with its last reference; an unreferenced body younger than history.keep_unindexed_days is kept for diagnosis).
    MoneyPuck keeps one file per season by design: only stray .tmp partial downloads are deleted. Never touches any
    other source (DK contest bodies, capture/, observations/)."""
    import os

    from nhl_dfs.data.history import season_bounds, season_of

    cfg = cfg if cfg is not None else load_sources_config()
    today = today or datetime.now(timezone.utc).date()
    now = now or datetime.now(timezone.utc)
    keep_days = float(cfg["history"].get("keep_unindexed_days", 7))
    root = Path(cache_root)
    st = EvictStats(dry_run=dry_run)
    canon_by_season: dict[int, dict[str, set[str]]] = {}
    for src, (_, _, kind) in REPORT_SOURCES.items():
        sdir = root / src
        rec = {"bytes_before": _tree_bytes(sdir), "bytes_after": 0, "entries_dropped": 0, "files_deleted": 0,
               "kept_canonical": 0, "kept_fresh": 0, "kept_unstored": 0}
        st.sources[src] = rec
        ip = sdir / "index.json"
        if not ip.exists():
            rec["bytes_after"] = rec["bytes_before"]
            continue
        index = json.loads(ip.read_text(encoding="utf-8"))
        ttl = float(cfg["ttl_s"][src])
        keep: dict = {}
        for url, e in index.items():
            fetched = datetime.fromisoformat(e["fetched_at"].replace("Z", "+00:00"))
            d0 = _url_start(url)
            season = season_of(d0) if d0 else None
            if season is not None and season_bounds(season)[1] < today:  # a completed season: canonical windows stay
                canon = canon_by_season.setdefault(season, canonical_urls(season, cfg))
                if url in canon[src]:
                    keep[url] = e
                    rec["kept_canonical"] += 1
                    continue
            if (now - fetched).total_seconds() <= ttl:
                keep[url] = e
                rec["kept_fresh"] += 1
                continue
            sp = store_mod.path_for(kind, season, root=store_root) if season is not None else None
            if sp is None or not sp.exists() or datetime.fromtimestamp(sp.stat().st_mtime, timezone.utc) <= fetched:
                keep[url] = e
                rec["kept_unstored"] += 1
                continue
            rec["entries_dropped"] += 1
        refs = {e["raw_path"] for e in keep.values()}
        dropped_refs = {e["raw_path"] for u, e in index.items() if u not in keep}
        victims = []
        for f in sdir.rglob("*"):
            if not f.is_file() or f.name == "index.json" or f.suffix == ".tmp":
                continue
            rel = f.relative_to(root).as_posix()
            if rel in refs:
                continue
            age_d = (now.timestamp() - f.stat().st_mtime) / 86400.0
            if rel in dropped_refs or age_d > keep_days:
                victims.append(f)
        rec["files_deleted"] = len(victims)
        if dry_run:
            rec["bytes_after"] = rec["bytes_before"] - sum(f.stat().st_size for f in victims)
            continue
        tmp = ip.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(keep, fh, indent=1, sort_keys=True)
        os.replace(tmp, ip)  # the index first: a crash after this leaves orphans, never a dangling entry
        for f in victims:
            f.unlink()
        for d in sorted((x for x in sdir.iterdir() if x.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        rec["bytes_after"] = _tree_bytes(sdir)
    mp_dir = Path(mp_root) if mp_root is not None else None
    if mp_dir is not None and mp_dir.is_dir():
        for f in mp_dir.rglob("*.tmp"):
            if (now.timestamp() - f.stat().st_mtime) > 86400:
                st.moneypuck_tmp_deleted += 1
                if not dry_run:
                    f.unlink()
    return st
