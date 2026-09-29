"""MoneyPuck listed downloads (C4, Tier A enrichment; plan section 3).

Only files listed on moneypuck.com/data.htm and named in config/sources.yaml `moneypuck.listed`
are fetched; any other (kind, level) is refused. No page is scraped. Credit: every run that
uses this data prints `moneypuck.attribution` (cli history does).

load() normalizes to the columns in docs/features.md. MoneyPuck situations are
{all, 5on5, 5on4, 4on5, other}; game-level player files carry one row per situation and are
pivoted wide here. MoneyPuck's 5on4/4on5 times are NOT the NHL's PP/SH time (they omit 5on3
and similar states), so they keep their own mp_toi_* names; the NHL definition lives in the
nhl_reports columns toi_ev_s / toi_pp_s / toi_sh_s.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import pandas as pd

from nhl_dfs.data.history import nhl_season
from nhl_dfs.data.http import SourceUnavailable, load_sources_config, requests_transport

REPO_ROOT = Path(__file__).resolve().parents[4]
Kind = Literal["skaters", "goalies", "lines", "teams", "shots"]
Level = Literal["season", "game"]
SITUATIONS = ("all", "5on5", "5on4", "4on5", "other")


class NotListed(ValueError):
    """The (kind, level) pair is not a listed MoneyPuck download."""


def _cfg(cfg: dict | None) -> dict:
    return (cfg if cfg is not None else load_sources_config())["moneypuck"]


def _mp_year(season: int) -> int:
    s = int(season)
    return s // 10000 if s > 9999 else s


def listed_url(kind: str, season: int, level: str, cfg: dict | None = None) -> str:
    listed = _cfg(cfg).get("listed") or {}
    url = (listed.get(kind) or {}).get(level)
    if not url:
        raise NotListed(f"MoneyPuck {kind}/{level} is not a listed download; refusing")
    return url.format(season=_mp_year(season))


def raw_path(kind: str, season: int, level: str, *, raw_root=None, cfg: dict | None = None) -> Path:
    root = Path(raw_root) if raw_root is not None else REPO_ROOT / _cfg(cfg)["raw_root"]
    url = listed_url(kind, season, level, cfg)
    ext = ".zip" if url.endswith(".zip") else ".csv"
    name = f"{level}_all{ext}" if "{season}" not in (_cfg(cfg)["listed"][kind][level]) else f"{level}_{_mp_year(season)}{ext}"
    return root / kind / name


def download(kind: Kind, season: int, level: Level, *, cfg: dict | None = None, raw_root=None,
             transport: Callable | None = None, force: bool = False,
             clock: Callable[[], datetime] | None = None) -> Path:
    """Fetch one listed file to data/raw/moneypuck/<kind>/ (reused while younger than max_age_s).
    A sidecar .meta.json records url, sha256, byte count and fetch time."""
    full = cfg if cfg is not None else load_sources_config()
    mp = full["moneypuck"]
    if not mp.get("enabled", False):
        raise SourceUnavailable("moneypuck disabled (sources.moneypuck.enabled: false)")
    url = listed_url(kind, season, level, full)
    path = raw_path(kind, season, level, raw_root=raw_root, cfg=full)
    meta = path.with_name(path.name + ".meta.json")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if path.exists() and meta.exists() and not force:
        m = json.loads(meta.read_text(encoding="utf-8"))
        age = (now - datetime.fromisoformat(m["fetched_utc"])).total_seconds()
        if age < float(mp.get("max_age_s", 86400)):
            return path
    transport = transport or requests_transport
    headers = {"User-Agent": full["http"]["user_agent"], "Accept": "*/*"}
    status, body, _ = transport(url, headers, max(60.0, float(full["http"]["timeout_s"])))
    if status != 200 or not body:
        raise SourceUnavailable(f"MoneyPuck {kind}/{level} {season}: HTTP {status}")
    if url.endswith(".zip") and not zipfile.is_zipfile(io.BytesIO(body)):
        raise SourceUnavailable(f"MoneyPuck {kind}/{level} {season}: response is not a zip file")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    meta.write_text(json.dumps({"url": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                                "fetched_utc": now.isoformat()}, indent=1), encoding="utf-8")
    return path


# -- loading -------------------------------------------------------------------------------------

_SKATER_SIT = {  # MoneyPuck column -> normalized name, read for every situation
    "icetime": "toi_s",
}
_SKATER_ALL = {  # read from the "all" situation only
    "shifts": "mp_shifts",
    "I_F_goals": "goals",
    "I_F_primaryAssists": "a1",
    "I_F_secondaryAssists": "a2",
    "I_F_shotsOnGoal": "sog",
    "I_F_shotAttempts": "attempts",
    "shotsBlockedByPlayer": "blocks",
    "I_F_xGoals": "ixg",
    "I_F_highDangerxGoals": "hd_xg",
    "OnIce_F_xGoals": "onice_xgf",
    "OnIce_A_xGoals": "onice_xga",
}
_SKATER_5ON5 = {"OnIce_F_xGoals": "onice_xgf_5on5", "OnIce_A_xGoals": "onice_xga_5on5"}
_GOALIE_ALL = {"icetime": "mp_toi_s", "xGoals": "xga", "goals": "mp_goals_against", "ongoal": "mp_sog_against"}


def _read_table(path: Path, usecols=None) -> pd.DataFrame:
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            with z.open(z.infolist()[0]) as f:
                return pd.read_csv(f, usecols=usecols)
    return pd.read_csv(path, usecols=usecols)


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype(str), format="%Y%m%d").dt.date


def _wide(df: pd.DataFrame, keys: list[str], per_sit: dict, all_only: dict, five: dict) -> pd.DataFrame:
    base = df[df["situation"] == "all"][keys + list(all_only)].rename(columns=all_only)
    for sit in SITUATIONS:
        part = df[df["situation"] == sit][keys + list(per_sit)]
        # toi_s -> mp_toi_s (all) and mp_toi_5on5_s, mp_toi_5on4_s, ...
        part = part.rename(columns={k: "mp_" + (v if sit == "all" else v.replace("_s", f"_{sit}_s", 1))
                                    for k, v in per_sit.items()})
        base = base.merge(part, on=keys, how="left")
    if five:
        p5 = df[df["situation"] == "5on5"][keys + list(five)].rename(columns=five)
        base = base.merge(p5, on=keys, how="left")
    return base


def _common(df: pd.DataFrame, game: bool) -> pd.DataFrame:
    out = pd.DataFrame({"nhl_id": df["playerId"].astype("int64"), "name": df["name"].astype(str)})
    out["season"] = df["season"].astype(int).map(nhl_season)
    if game:
        out["game_id"] = df["gameId"].astype("int64")
        out["team"] = df["playerTeam"].astype(str)
        out["opponent"] = df["opposingTeam"].astype(str)
        out["home"] = df["home_or_away"].astype(str).str.upper().eq("HOME")
        out["game_date"] = _date(df["gameDate"])
    else:
        out["team"] = df["team"].astype(str)
        out["games_played"] = df["games_played"].astype(int)
    if "position" in df:
        out["position"] = df["position"].astype(str)
    return out


def load(kind: Kind, season: int, level: Level, *, path=None, raw_root=None, cfg: dict | None = None) -> pd.DataFrame:
    """Normalized DataFrame for one listed file (downloaded earlier, or `path`)."""
    listed_url(kind, season, level, cfg)  # refuses unlisted pairs even when a path is given
    p = Path(path) if path is not None else raw_path(kind, season, level, raw_root=raw_root, cfg=cfg)
    game = level == "game"
    if kind == "skaters":
        keys = ["playerId", "name", "season", "position"] + (["gameId", "playerTeam", "opposingTeam", "home_or_away", "gameDate"]
                                                               if game else ["team", "games_played"])
        cols = keys + ["situation"] + sorted(set(_SKATER_SIT) | set(_SKATER_ALL) | set(_SKATER_5ON5))
        raw = _read_table(p, usecols=cols)
        wide = _wide(raw, keys, _SKATER_SIT, _SKATER_ALL, _SKATER_5ON5)
        out = _common(wide, game)
        for c in wide.columns:
            if c not in keys:
                out[c] = wide[c].to_numpy()
        out["assists"] = out["a1"] + out["a2"]
        return out.reset_index(drop=True)
    if kind == "goalies":
        keys = ["playerId", "name", "season", "position"] + (["gameId", "playerTeam", "opposingTeam", "home_or_away", "gameDate"]
                                                               if game else ["team", "games_played"])
        raw = _read_table(p, usecols=keys + ["situation"] + list(_GOALIE_ALL))
        wide = raw[raw["situation"] == "all"].rename(columns=_GOALIE_ALL)
        out = _common(wide, game)
        for v in _GOALIE_ALL.values():
            out[v] = wide[v].to_numpy()
        return out.reset_index(drop=True)
    if kind == "lines":
        cols = ["lineId", "name", "season", "position", "situation", "icetime", "xGoalsFor", "xGoalsAgainst"]
        cols += ["gameId", "playerTeam", "opposingTeam", "gameDate"] if game else ["team", "games_played"]
        raw = _read_table(p, usecols=cols)
        ids = raw["lineId"].astype(str)
        out = pd.DataFrame({
            "line_id": ids,
            "player_ids": ids.map(lambda s: "|".join(s[i:i + 7] for i in range(0, len(s), 7))),
            "unit": raw["position"].astype(str),  # "line" (forwards) or "pairing" (defense)
            "season": raw["season"].astype(int).map(nhl_season),
            "situation": raw["situation"].astype(str),
            "toi_s": raw["icetime"].astype(float),
            "xgf": raw["xGoalsFor"].astype(float),
            "xga": raw["xGoalsAgainst"].astype(float),
        })
        if game:
            out["game_id"] = raw["gameId"].astype("int64")
            out["team"] = raw["playerTeam"].astype(str)
            out["game_date"] = _date(raw["gameDate"])
        else:
            out["team"] = raw["team"].astype(str)
        return out.reset_index(drop=True)
    if kind == "teams":
        cols = ["team", "season", "situation", "iceTime", "xGoalsFor", "xGoalsAgainst", "shotsOnGoalFor",
                "shotsOnGoalAgainst", "goalsFor", "goalsAgainst"]
        if game:
            cols += ["gameId", "opposingTeam", "home_or_away", "gameDate"]
        raw = _read_table(p, usecols=cols)
        raw = raw[raw["season"].astype(int) == _mp_year(season)]
        out = pd.DataFrame({
            "team": raw["team"].astype(str), "season": raw["season"].astype(int).map(nhl_season),
            "situation": raw["situation"].astype(str), "toi_s": raw["iceTime"].astype(float),
            "xgf": raw["xGoalsFor"].astype(float), "xga": raw["xGoalsAgainst"].astype(float),
            "sogf": raw["shotsOnGoalFor"].astype(float), "soga": raw["shotsOnGoalAgainst"].astype(float),
            "gf": raw["goalsFor"].astype(float), "ga": raw["goalsAgainst"].astype(float),
        })
        if game:
            out["game_id"] = raw["gameId"].astype("int64")
            out["opponent"] = raw["opposingTeam"].astype(str)
            out["home"] = raw["home_or_away"].astype(str).str.upper().eq("HOME")
            out["game_date"] = _date(raw["gameDate"])
        return out.reset_index(drop=True)
    if kind == "shots":
        cols = ["game_id", "season", "isPlayoffGame", "shooterPlayerId", "shooterName", "teamCode", "isHomeTeam",
                "period", "time", "event", "shotWasOnGoal", "goal", "xGoal", "homeSkatersOnIce", "awaySkatersOnIce"]
        raw = _read_table(p, usecols=cols)
        yr = raw["season"].astype(int)  # game_id is the id without its year: 20017 = type 02, game 0017
        home = raw["isHomeTeam"].astype(int) == 1
        own = raw["homeSkatersOnIce"].where(home, raw["awaySkatersOnIce"]).astype(int)
        opp = raw["awaySkatersOnIce"].where(home, raw["homeSkatersOnIce"]).astype(int)
        return pd.DataFrame({
            "game_id": (yr * 1_000_000 + raw["game_id"].astype(int)).astype("int64"),
            "season": yr.map(nhl_season),
            "shooter_id": raw["shooterPlayerId"].fillna(0).astype("int64"),
            "shooter_name": raw["shooterName"].astype(str),
            "team": raw["teamCode"].astype(str),
            "period": raw["period"].astype(int),
            "time_s": raw["time"].astype(int),
            "event": raw["event"].astype(str),
            "on_goal": raw["shotWasOnGoal"].astype(int) == 1,
            "goal": raw["goal"].astype(int) == 1,
            "xg": raw["xGoal"].astype(float),
            "situation": own.astype(str) + "on" + opp.astype(str),
        }).reset_index(drop=True)
    raise NotListed(f"unknown MoneyPuck kind {kind!r}")


def shared_ice(lines: pd.DataFrame) -> pd.DataFrame:
    """Per player-game: share of 5on5 unit time spent in the player's most-used unit
    (forward line or defense pairing). A shared-ice proxy, Tier A only."""
    g = lines[lines["situation"] == "5on5"]
    rows = []
    for rec in g.itertuples(index=False):
        for pid in rec.player_ids.split("|"):
            if pid:
                rows.append((int(pid), int(rec.game_id), rec.toi_s))
    if not rows:
        return pd.DataFrame(columns=["nhl_id", "game_id", "shared_ice"])
    df = pd.DataFrame(rows, columns=["nhl_id", "game_id", "toi_s"])
    agg = df.groupby(["nhl_id", "game_id"])["toi_s"].agg(["max", "sum"]).reset_index()
    agg["shared_ice"] = (agg["max"] / agg["sum"]).where(agg["sum"] > 0)
    return agg[["nhl_id", "game_id", "shared_ice"]]
