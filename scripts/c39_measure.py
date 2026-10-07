"""C39 measurement (first commit, no behavior change): how stale is the history store, and what would current games change.

Offline by default. It READS a scratch copy of the store (--store) and the cached NHL schedule bodies (--raw, read only),
and WRITES only the markdown report (--out) and, for --network-look, folders under --scratch. An audit hook blocks the
network (unless --network-look) and refuses and counts every write under the repo's data/, runs/ and outputs/ folders
(the guard of scripts/c17_replay.py).

Sections of the report:
  1. the store today: state, newest games, days behind, file write times, rows per season
  2. the games the store lacks, from the cached NHL schedule (regular season only), and tonight's back-to-back teams
  3. what the as-of features hold today (players with a game in the last 14 days, back-to-back flags)
  4. a replay of "frozen store versus current store" on real 2025-26 history: for a few in-season dates, the features built
     from a store that stops at the 2024-25 close (today's situation, one season earlier) against the real store as of
     that date. It compares FEATURES (back-to-back flags, goalie start shares, shot rates), not points won.
  5. optionally one real look at the NHL reports for the new season, into scratch folders only

  python scripts/c39_measure.py --store <scratch copy of data/features/history> --out <report.md> [--as-of 2026-10-06]
                                [--network-look --scratch <dir>]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

WRITES: list[str] = []
NET: list[str] = []
ET = ZoneInfo("America/New_York")


def install_guards(scratch: Path, allow_network: bool) -> None:
    allowed = [scratch.resolve(), Path(os.environ.get("TEMP", "/tmp")).resolve()]
    watched = [(REPO / "data").resolve(), (REPO / "runs").resolve(), (REPO / "outputs").resolve(), (REPO / "BACKLOG.md")]

    def under(p, roots) -> bool:
        try:
            q = Path(os.fsdecode(p)).resolve()
        except Exception:
            return False
        return any(q == r or r in q.parents for r in roots)

    def hook(event: str, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname") and not allow_network:
            NET.append(event)
            raise RuntimeError("c39_measure: the network is blocked")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                return
            writing = (mode is not None and any(c in str(mode) for c in "wax+")) or (
                mode is None and isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
            if writing and under(path, watched) and not under(path, allowed):
                WRITES.append(f"open {os.fsdecode(path)}")
                raise PermissionError(f"c39_measure: write outside the scratch folder refused: {os.fsdecode(path)}")
        elif event in ("os.rename", "os.remove", "os.mkdir", "os.rmdir", "shutil.rmtree"):
            for a in args[:2]:
                if isinstance(a, (str, bytes, os.PathLike)) and under(a, watched) and not under(a, allowed):
                    WRITES.append(f"{event} {os.fsdecode(a)}")
                    raise PermissionError(f"c39_measure: {event} outside the scratch folder refused: {os.fsdecode(a)}")

    sys.addaudithook(hook)


def md_table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


# -- 1 and 3: the store and what the features hold --------------------------------------------------------------------

def store_facts(store: Path, as_of: date) -> list[str]:
    import pandas as pd

    from nhl_dfs.data.features import asof
    from nhl_dfs.data.history import status
    from nhl_dfs.data.history import store as store_mod

    st = status.measure(as_of, store_root=store)
    out = ["## 1. The store today", "", f"- `status.measure({as_of})`: **{st.state}**", f"- {st.line()}", ""]
    rows = []
    for kind in ("skater_games", "goalie_games"):
        for s in store_mod.seasons_present(kind, root=store):
            df = store_mod.read(kind, [s], root=store, columns=["game_date", "regime"])
            p = store_mod.path_for(kind, s, root=store)
            mt = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M")
            reg = df[df["regime"] == "regular"]
            rows.append([kind, s, len(df), max(df["game_date"]), max(reg["game_date"]) if len(reg) else "none", mt])
    out += md_table(["table", "season", "rows", "newest game (any type)", "newest regular game", "file written (UTC)"], rows)
    f = asof.frame(as_of, None, store_root=store)
    recent = int((pd.to_datetime(f.players["last_game_date"]).dt.date >= as_of - timedelta(days=14)).sum()) if len(f.players) else 0
    out += ["", "## 3. What the as-of features hold today", "",
            f"- as-of date {as_of}; seasons read {f.seasons}; players with an aggregate row: {len(f.players)}",
            f"- players whose newest regular game is within 14 days of the as-of date: **{recent}**",
            f"- newest game any player has in the feature frame: {max(f.skaters['game_date']) if len(f.skaters) else 'none'}", ""]
    return out


# -- 2: the games the store lacks (cached NHL schedule) ------------------------------------------------------------------

def schedule_facts(raw: Path, as_of: date, store_last: date | None) -> list[str]:
    idx_path = raw / "nhl_schedule" / "index.json"
    out = ["## 2. The games the store lacks (cached NHL schedule, regular season only)", ""]
    if not idx_path.is_file():
        return out + ["- no cached NHL schedule on this machine; not measured.", ""]
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    games: dict[int, tuple] = {}
    for _, e in sorted(idx.items(), key=lambda kv: kv[1]["fetched_at"]):
        try:
            body = json.loads((raw / e["raw_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for wk in body.get("gameWeek", []):
            for g in wk.get("games", []):
                games[g["id"]] = (wk["date"], g.get("gameType"), g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])
    reg = {k: v for k, v in games.items() if v[1] == 2}
    if not reg:
        return out + ["- the cached schedule holds no regular-season game; not measured.", ""]
    by_day: dict[str, set] = {}
    for d, _, a, h in reg.values():
        by_day.setdefault(d, set()).update({a, h})
    cnt: dict[str, int] = {}
    for d, *_ in reg.values():
        cnt[d] = cnt.get(d, 0) + 1
    before = {d: n for d, n in cnt.items() if d < as_of.isoformat() and (store_last is None or d > store_last.isoformat())}
    first = min(cnt)
    out += [f"- first regular-season game day in the cache: **{first}**; games dated before {as_of} that the store lacks "
            f"(newest stored game {store_last}): **{sum(before.values())}** over {len(before)} day(s)",
            "- the cache is a set of schedule snapshots taken at different times, so a day's count can be incomplete", ""]
    out += md_table(["day", "regular-season games in the cache"], [[d, cnt[d]] for d in sorted(before)])
    today, yest = by_day.get(as_of.isoformat(), set()), by_day.get((as_of - timedelta(days=1)).isoformat(), set())
    out += ["", f"- teams playing {as_of}: {len(today)}; of those, also playing the day before (back-to-back): "
                f"**{len(today & yest)}** {sorted(today & yest)}", ""]
    return out


# -- 4: frozen store versus current store (replay on real history) ---------------------------------------------------------

def drift(store: Path, scratch: Path, dates: list[date]) -> list[str]:
    import pandas as pd

    from nhl_dfs.data.features import asof
    from nhl_dfs.data.history import store as store_mod
    from nhl_dfs.models import goalies as goalie_mod
    from nhl_dfs.models import rates as rates_mod
    from nhl_dfs.models.rates import load_model_config

    cfg = load_model_config()
    stale = scratch / "frozen_store"
    for kind in ("skater_games", "goalie_games"):
        src = store_mod.path_for(kind, 20242025, root=store)
        dst = store_mod.path_for(kind, 20242025, root=stale)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    real_sk = store_mod.read("skater_games", [20252026], root=store, columns=["nhl_id", "team", "opponent", "game_date", "regime"])
    real_gl = store_mod.read("goalie_games", [20252026], root=store, columns=["nhl_id", "team", "opponent", "game_date", "started", "regime"])
    out = ["## 4. Frozen store versus current store (replay on real 2025-26 history; features, not points)", "",
           "The frozen store holds only the 2024-25 season, as today's store holds only 2025-26 at the 2026-27 opener. "
           "For each date the slate is the set of teams and skaters who actually played that day; each team's goalies are "
           "everyone who played for it in the 14 days up to that day (the DraftKings pool lists them all). "
           "`b2b flags` is the model's own back-to-back flag (`models/goalies.py`); `p_start shift` is the mean absolute change "
           "of a goalie's start share; `ev shots/60 shift` compares the model's even-strength shots per 60 for skaters who "
           "played that day. This measures how much the FEATURES move, not how many points a lineup wins.", ""]
    rows = []
    for want in dates:
        days = sorted(d for d in set(real_gl["game_date"]) if d >= want)
        if not days:
            continue
        d = days[0]
        sk_day = real_sk[(real_sk["game_date"] == d) & (real_sk["regime"] == "regular")]
        gl_day = real_gl[(real_gl["game_date"] == d) & (real_gl["regime"] == "regular")]
        cur = asof.frame(d, None, store_root=store)
        old = asof.frame(d, None, store_root=stale)
        sched = {r.team: {"opponent": r.opponent, "game_date": d} for r in gl_day.itertuples()}
        # The DK pool lists every goalie of a team, not only tonight's starter: each team's goalies are everyone who played
        # for it in the 14 days up to and including d. (A first version listed only the goalies who played on d, which can
        # never hold last night's starter who then rests, so it could not show a back-to-back flag; see the experiment note.)
        pool = real_gl[(real_gl["game_date"] >= d - timedelta(days=14)) & (real_gl["game_date"] <= d) & real_gl["team"].isin(sched)]
        tg: dict[str, list] = {}
        for r in pool.drop_duplicates(["team", "nhl_id"]).itertuples():
            tg.setdefault(r.team, []).append(int(r.nhl_id))
        gc = goalie_mod.estimate(cur, sched, cfg, team_goalies=tg, as_of=d)
        go = goalie_mod.estimate(old, sched, cfg, team_goalies=tg, as_of=d)
        b2b_c, b2b_o = sum(g.b2b for g in gc.values()), sum(g.b2b for g in go.values())
        shift = statistics.mean(abs(gc[k].p_start - go[k].p_start) for k in gc) if gc else 0.0
        ids = sorted({int(x) for x in sk_day["nhl_id"]})
        rc, ro = rates_mod.estimate(cur, cfg, nhl_ids=ids), rates_mod.estimate(old, cfg, nhl_ids=ids)
        rel = []
        for i in ids:
            a, b = rc[i].sog60.get("ev"), ro[i].sog60.get("ev")
            if a and b:
                rel.append(abs(a - b) / b)
        recent_c = int((pd.to_datetime(cur.players["last_game_date"]).dt.date >= d - timedelta(days=14)).sum()) if len(cur.players) else 0
        have_c = sum(1 for i in ids if rc[i].n_games > 0)
        have_o = sum(1 for i in ids if ro[i].n_games > 0)
        rows.append([d, len(tg), len(gc), f"{b2b_o} -> {b2b_c}", f"{shift:.3f}", len(ids), f"{have_o} -> {have_c}",
                     f"{statistics.median(rel):.1%}" if rel else "n/a",
                     f"{sum(1 for x in rel if x > 0.10) / len(rel):.0%}" if rel else "n/a"])
    out += md_table(["slate day", "teams", "goalies", "b2b flags (frozen -> current)", "p_start shift", "skaters",
                     "skaters with any history (frozen -> current)", "median ev shots/60 shift", "share shifted > 10%"], rows)
    out.append("")
    return out


# -- 5: one real look at the NHL reports (scratch only) ----------------------------------------------------------------------

def network_look(store: Path, scratch: Path, as_of: date) -> list[str]:
    from nhl_dfs.data.history import nhl_reports, status
    from nhl_dfs.data.history import store as store_mod
    from nhl_dfs.data.http import HttpCache, load_sources_config

    cfg = load_sources_config()
    cfg2 = {**cfg, "http": {**cfg["http"], "max_calls_per_run": 40}}
    look = scratch / "look_store"
    shutil.copytree(store, look, dirs_exist_ok=True)
    cache = HttpCache(scratch / "look_raw", config=cfg2)
    now = datetime.now(timezone.utc)
    through = status.completed_through(now, cfg)
    out = ["## 5. One real look at the NHL reports (scratch store and scratch raw folder only)", ""]
    try:
        stats = nhl_reports.backfill([20262027], since=date(2026, 9, 1), cache=cache, store_root=look, cfg=cfg,
                                     today=through + timedelta(days=1), moneypuck=False, raw_root=scratch / "look_mp",
                                     crosscheck_games=0)
    except Exception as exc:
        return out + [f"- NOT RUN to completion: {type(exc).__name__}: {str(exc)[:200]}", ""]
    sk = store_mod.read("skater_games", [20262027], root=look, columns=["game_id", "game_date", "regime", "team"])
    gl = store_mod.read("goalie_games", [20262027], root=look, columns=["game_id", "game_date", "regime", "started", "team", "nhl_id"])
    st = status.measure(as_of, store_root=look)
    out += [f"- requests made {stats.calls}, windows {stats.windows}, elapsed {stats.elapsed_s}s; days counted complete through {through}",
            f"- 2026-27 rows fetched: {len(sk)} skater rows over {sk['game_id'].nunique() if len(sk) else 0} games; "
            f"{len(gl)} goalie rows",
            f"- scratch store afterwards: **{st.state}**; {st.line()}"]
    if len(gl):
        last = max(gl["game_date"])
        out.append(f"- newest 2026-27 game date fetched: {last}")
    out += tonight(store, look, scratch, as_of)
    out.append("")
    return out


def tonight(old: Path, new: Path, raw_parent: Path, as_of: date) -> list[str]:
    """The as-of features for the as-of date's slate, built from the stale store and from the freshly fetched scratch store."""
    from nhl_dfs.data.features import asof
    from nhl_dfs.data.history import store as store_mod
    from nhl_dfs.models import goalies as goalie_mod
    from nhl_dfs.models import rates as rates_mod
    from nhl_dfs.models.rates import load_model_config

    raw = REPO / "data" / "raw"
    sched: dict[str, dict] = {}
    try:
        idx = json.loads((raw / "nhl_schedule" / "index.json").read_text(encoding="utf-8"))
        for _, e in sorted(idx.items(), key=lambda kv: kv[1]["fetched_at"]):
            body = json.loads((raw / e["raw_path"]).read_text(encoding="utf-8"))
            for wk in body.get("gameWeek", []):
                for g in wk.get("games", []):
                    if wk["date"] == as_of.isoformat() and g.get("gameType") == 2:
                        a, h = g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]
                        sched[a] = {"opponent": h, "game_date": as_of}
                        sched[h] = {"opponent": a, "game_date": as_of}
    except (OSError, ValueError, KeyError):
        pass
    if not sched:
        return ["- tonight's slate: not measured (no cached schedule for the as-of date)"]
    cfg = load_model_config()
    f_new, f_old = asof.frame(as_of, None, store_root=new), asof.frame(as_of, None, store_root=old)
    g_new = store_mod.read("goalie_games", [20262027], root=new, columns=["nhl_id", "team"])
    tg = {t: sorted({int(x) for x in g_new[g_new["team"] == t]["nhl_id"]}) for t in sched}
    tg = {t: v for t, v in tg.items() if v}
    gc = goalie_mod.estimate(f_new, sched, cfg, team_goalies=tg, as_of=as_of)
    go = goalie_mod.estimate(f_old, sched, cfg, team_goalies=tg, as_of=as_of)
    sk_new = store_mod.read("skater_games", [20262027], root=new, columns=["nhl_id"])
    ids = sorted({int(x) for x in sk_new["nhl_id"]})
    rc, ro = rates_mod.estimate(f_new, cfg, nhl_ids=ids), rates_mod.estimate(f_old, cfg, nhl_ids=ids)
    rel = [abs(rc[i].sog60["ev"] - ro[i].sog60["ev"]) / ro[i].sog60["ev"] for i in ids
           if rc[i].sog60.get("ev") and ro[i].sog60.get("ev")]
    return ["", f"- the {as_of} slate ({len(sched)} teams) with the stale store against the fetched store (features, not points):",
            f"  - back-to-back goalie flags: **{sum(g.b2b for g in go.values())} -> {sum(g.b2b for g in gc.values())}**; "
            f"mean absolute change in a goalie's start share {statistics.mean(abs(gc[k].p_start - go[k].p_start) for k in gc):.3f} "
            f"over {len(gc)} goalies",
            f"  - skaters with a 2026-27 game: {len(ids)}; median change in even-strength shots per 60 "
            f"{statistics.median(rel):.1%}; share changed by more than 10%: {sum(1 for x in rel if x > 0.10) / len(rel):.0%}"
            if rel else "  - no skater rates to compare"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, help="a scratch COPY of data/features/history (copy with cp -rp to keep write times)")
    ap.add_argument("--out", required=True, help="markdown report to write (outside data/, runs/ and outputs/)")
    ap.add_argument("--raw", default=str(REPO / "data" / "raw"), help="read-only: where the cached NHL schedule lives")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD, default today (ET)")
    ap.add_argument("--scratch", default=None, help="scratch folder for the frozen store and the network look")
    ap.add_argument("--network-look", action="store_true")
    ap.add_argument("--dates", nargs="*", default=["2025-10-14", "2025-10-21", "2026-01-15", "2026-03-01"])
    a = ap.parse_args()
    out_path = Path(a.out).resolve()
    scratch = Path(a.scratch).resolve() if a.scratch else out_path.parent
    install_guards(scratch, a.network_look)
    as_of = date.fromisoformat(a.as_of) if a.as_of else datetime.now(ET).date()
    store = Path(a.store).resolve()
    from nhl_dfs.data.history import status

    lines = [f"# C39 measurement output (generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, as-of {as_of})", ""]
    st = status.measure(as_of, store_root=store)
    lines += store_facts(store, as_of)
    lines += schedule_facts(Path(a.raw), as_of, st.last_game)
    lines += drift(store, scratch, [date.fromisoformat(x) for x in a.dates])
    if a.network_look:
        lines += network_look(store, scratch, as_of)
    lines += [f"_Guard: {len(WRITES)} write(s) outside the scratch folder refused, {len(NET)} network attempt(s) blocked._", ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
