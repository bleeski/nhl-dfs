"""How current is the history store (C39, backlog B70 and B71).

`measure` looks at the stored per-game tables (local files only, never the network, never raises) and says
how far the newest regular-season game is behind the date the as-of features are built for. The states:

  ABSENT             no skater_games file at all (a cloud session before its backfill, a new machine)
  PRIOR_SEASON_ONLY  no regular-season game of the as-of date's own season is stored yet
  STALE              the newest regular-season game is more than `history.fresh_days` before the as-of date; a no-game
                     day makes it read STALE falsely, and the cost is one small fetch that finds nothing
  CURRENT            within `history.fresh_days` (1: the store holds the last finished day; B70's acceptance, 'within 2 days',
                     is what a run leaves after its refresh, which starts from 2 days behind)
  SEASON_COMPLETE    the regular season is over at the as-of date; playoffs feed no model (model.yaml regimes)
  UNREADABLE         a stored file could not be read; the reason is in `error`

Staleness is judged on the regimes the model reads (model.yaml `regimes`, regular only): a preseason or a
playoff row never makes the store look current. The newest game of any type is reported next to it.
Reads only the date, game id and regime columns.

`completed_through(now)` is the last date whose games count as finished: a day completes `complete_after_h`
hours after midnight Eastern, the same clock as `slate_as_of`. The incremental refresh fetches no later day.

`read_bootstrap_state` reads the file tools/cloud_bootstrap.sh writes while its background backfill runs, so
an ABSENT store can say "a backfill started HH:MM is still running" (B71).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nhl_dfs.data.history import mp_year, regular_season_complete, season_of
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.http import load_sources_config

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_STATE = REPO_ROOT / "data" / "cache" / "history_bootstrap.state"

ABSENT, PRIOR_SEASON_ONLY, STALE, CURRENT = "ABSENT", "PRIOR_SEASON_ONLY", "STALE", "CURRENT"
SEASON_COMPLETE, UNREADABLE = "SEASON_COMPLETE", "UNREADABLE"
NEEDS_REFRESH = (PRIOR_SEASON_ONLY, STALE)  # the states in which the in-run incremental refresh starts


def _history_cfg(cfg: dict | None = None) -> dict:
    return (cfg if cfg is not None else load_sources_config()).get("history", {})


def completed_through(now: datetime, cfg: dict | None = None) -> date:
    """The last Eastern date whose games are all finished at `now` (a day completes complete_after_h after midnight)."""
    hours = float(_history_cfg(cfg).get("complete_after_h", 3))
    return (now.astimezone(ET) - timedelta(hours=hours)).date() - timedelta(days=1)


# -- the bootstrap's background backfill (tools/cloud_bootstrap.sh) ------------------------------------------------

def _pid_alive(pid: int) -> bool | None:
    """True or False on Linux; None where it cannot be told safely (os.kill on Windows would end the process)."""
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def read_bootstrap_state(path=None, *, now: datetime | None = None, cfg: dict | None = None) -> dict | None:
    """None when the bootstrap never started a backfill. Else {state, started_utc, exit, age_min, note}; a RUNNING record
    whose process is gone, or older than `history.bootstrap_stale_min`, reads ABANDONED so it can never block anything."""
    p = Path(path) if path is not None else BOOTSTRAP_STATE
    if not p.is_file():
        return None
    kv: dict[str, str] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    except OSError:
        return None
    now = now or datetime.now(timezone.utc)
    state = kv.get("state", "").upper() or "UNKNOWN"
    started = kv.get("started", "")
    age_min = None
    try:
        age_min = round((now - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() / 60.0, 1)
    except ValueError:
        pass
    out = {"state": state, "started_utc": started or None, "exit": kv.get("exit") or None, "age_min": age_min, "note": ""}
    if state == "RUNNING":
        limit = float(_history_cfg(cfg).get("bootstrap_stale_min", 30))
        pid = int(kv["pid"]) if kv.get("pid", "").isdigit() else None
        alive = _pid_alive(pid) if pid is not None else None
        if alive is False or (age_min is not None and age_min > limit):
            out["state"] = "ABANDONED"
            out["note"] = "the background backfill is no longer running and never reported a result"
    return out


# -- measurement -------------------------------------------------------------------------------------------------------

@dataclass
class StoreStatus:
    state: str
    as_of: date
    fresh_days: int = 2
    last_game: date | None = None  # newest game of any type
    last_regular: date | None = None  # newest game of a regime the model reads
    goalie_last_regular: date | None = None
    days_behind: int | None = None  # as_of minus last_regular
    seasons: list[int] = field(default_factory=list)
    current_season_games: int = 0  # distinct regular-season games stored for the as-of date's own season
    written_utc: str | None = None  # when the newest skater_games file was written
    bootstrap: dict | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d is not None else None
        return {"state": self.state, "as_of": iso(self.as_of), "fresh_days": self.fresh_days, "last_game": iso(self.last_game),
                "last_regular_game": iso(self.last_regular), "goalie_last_regular_game": iso(self.goalie_last_regular),
                "days_behind": self.days_behind, "seasons": list(self.seasons), "current_season_games": self.current_season_games,
                "written_utc": self.written_utc, "bootstrap": self.bootstrap, "error": self.error}

    def line(self, model: dict | None = None) -> str:
        return render_line(self.as_dict(), model)


def measure(as_of: date, *, store_root=None, fresh_days: int | None = None, regimes=None, bootstrap_path=None,
            now: datetime | None = None, cfg: dict | None = None) -> StoreStatus:
    """Local files only. Never raises: a failure is state UNREADABLE with the reason."""
    fd = int(fresh_days if fresh_days is not None else _history_cfg(cfg).get("fresh_days", 2))
    st = StoreStatus(state=UNREADABLE, as_of=as_of, fresh_days=fd)
    try:
        if regimes is None:
            from nhl_dfs.models.rates import load_model_config

            regimes = load_model_config().get("regimes", ["regular"])
        present = store_mod.seasons_present("skater_games", root=store_root)
        st.seasons = present
        if not present:
            st.state = ABSENT
            st.bootstrap = read_bootstrap_state(bootstrap_path, now=now, cfg=cfg)
            return st
        newest = store_mod.path_for("skater_games", present[-1], root=store_root)
        st.written_utc = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sk = store_mod.read("skater_games", present[-3:], root=store_root, columns=["game_date", "game_id", "regime"])
        reg = sk[sk["regime"].isin(list(regimes))] if len(sk) else sk
        st.last_game = max(sk["game_date"]) if len(sk) else None
        st.last_regular = max(reg["game_date"]) if len(reg) else None
        gl = store_mod.read("goalie_games", store_mod.seasons_present("goalie_games", root=store_root)[-3:], root=store_root,
                            columns=["game_date", "regime"])
        glr = gl[gl["regime"].isin(list(regimes))] if len(gl) else gl
        st.goalie_last_regular = max(glr["game_date"]) if len(glr) else None
        now_season = season_of(as_of)
        year = mp_year(now_season)
        in_season = reg[(reg["game_id"].astype("int64") // 1_000_000) == year] if len(reg) else reg
        st.current_season_games = int(in_season["game_id"].nunique()) if len(in_season) else 0
        if st.last_regular is not None:
            st.days_behind = (as_of - st.last_regular).days
        if regular_season_complete(now_season, as_of):
            st.state = SEASON_COMPLETE
        elif st.current_season_games == 0:
            st.state = PRIOR_SEASON_ONLY
        else:
            st.state = CURRENT if st.days_behind is not None and st.days_behind <= fd else STALE
        return st
    except Exception as exc:  # reported in the notes; a measurement must never stop a run
        st.state = UNREADABLE
        st.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        return st


# -- the words -------------------------------------------------------------------------------------------------------

def _season_label(season: int) -> str:
    return f"{mp_year(season)}-{str(mp_year(season) + 1)[2:]}"


def render_line(h: dict, model: dict | None = None) -> str:
    """One plain-English line for RUN_NOTES and `history --status`, from the manifest dict form."""
    state = h["state"]
    own = _season_label(season_of(date.fromisoformat(h["as_of"])))
    behind = f"{h['days_behind']} day(s) before the as-of date {h['as_of']}" if h.get("days_behind") is not None else None
    if state == ABSENT:
        text = "ABSENT: no history store on this machine, so every player is priced on priors."
        b = h.get("bootstrap")
        if b:
            started = (b.get("started_utc") or "?")[11:16]
            if b["state"] == "RUNNING":
                text += f" A background backfill started at {started} UTC is still running; the next run will see what it has written."
            elif b["state"] == "DONE":
                text += f" The session's background backfill (started {started} UTC) finished, but no store was written."
            elif b["state"] == "FAILED":
                text += f" The session's background backfill (started {started} UTC) failed with exit {b.get('exit')}."
            else:
                text += f" The session's background backfill (started {started} UTC) {b.get('note') or 'is in state ' + b['state']}."
        else:
            text += " Run `history --backfill 2` once (about two minutes) to build it."
    elif state == UNREADABLE:
        text = f"UNREADABLE: the stored games could not be read ({h.get('error')}); priors are used."
    else:
        newest = (f"newest regular-season game {h['last_regular_game']} ({behind})" if h.get("last_regular_game")
                  else "no regular-season game of any season is stored")
        any_type = (f"; newest game of any type {h['last_game']}" if h.get("last_game") and h["last_game"] != h.get("last_regular_game")
                    else "")
        seasons = ", ".join(_season_label(s) for s in h["seasons"])
        if state == PRIOR_SEASON_ONLY:
            text = (f"PRIOR_SEASON_ONLY: no {own} regular-season game is stored; {newest}{any_type}. Recent form and the "
                    "back-to-back goalie flag cannot see any game after that.")
        elif state == STALE:
            text = (f"STALE: {newest}{any_type}; {h['current_season_games']} {own} regular-season game(s) stored. Games after "
                    "that date are missing from recent form and the back-to-back goalie flag.")
        elif state == CURRENT:
            text = f"CURRENT: {newest}{any_type}; {h['current_season_games']} {own} regular-season game(s) stored."
        else:
            text = f"SEASON_COMPLETE: the {own} regular season is over at {h['as_of']}; playoff games feed no model. {newest}{any_type}."
        text += f" Seasons stored: {seasons}"
        if h.get("written_utc"):
            text += f"; files last written {h['written_utc'][:16].replace('T', ' ')} UTC"
        text += "."
        gl = h.get("goalie_last_regular_game")
        if gl and h.get("last_regular_game") and gl != h["last_regular_game"]:
            text += f" Goalie table newest regular game {gl} differs from the skater table."
    r = h.get("refresh")
    if r:
        from nhl_dfs.data.history.refresh import render_line as render_refresh

        text += f" Refresh after the first publish: {render_refresh(r)}"
        a = h.get("after")
        if a and a.get("last_regular_game"):
            text += (f" The store now reads {a['state']}: newest regular-season game {a['last_regular_game']} "
                     f"({a['days_behind']} day(s) before the as-of date).")
        if h.get("rebuilt_projection"):
            text += " The projection the provisional and scenario passes use was rebuilt from the refreshed store (v1 used the earlier one)."
    c = (model or {}).get("counts")
    if c:
        total = sum(int(v) for v in c.values())
        label = "Model inputs after the rebuild" if h.get("rebuilt_projection") else "Model inputs"
        text += (f" {label}: HISTORY {c.get('HISTORY', 0)}, MIXED {c.get('MIXED', 0)}, PRIOR {c.get('PRIOR', 0)} of "
                 f"{total} persons.")
    return text
