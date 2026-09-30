"""Grade the frozen pre-lock player forecasts (card C11; plan section 12 layer table).

Forecast: the run's own scenario cache, `runs/<id>/scenario/selection/` (the first 8,000 draws per person, DK
points in tenths, FLEX scale in Showdown), saved by the C8 pass before lock. The draws are UNMASKED for
QUESTIONABLE players (the run's participation mask is separate), but the simulator draws dressing per scenario
(sim/game.py: Bernoulli p_dress), so a skater's column includes his not-dressed zeros, and a goalie's column
includes his not-started zeros. The cache holds points only, so "not dressed" cannot be told from "dressed, 0
points": skater grades are UNCONDITIONAL on dressing (labeled; a per-draw dressed indicator would fix it, backlog).
Nothing is re-simulated or rebuilt.

Actual points: the standings' FPTS (every drafted player; Showdown FLEX scale). FPTS cannot tell "did not play"
from "played and scored 0" and has nothing for undrafted players, so participation comes from NHL box scores
(C4 adapter, joined to persons through the accepted identity crosswalk). The box score lacks shorthanded points
and shootout goals, so it cannot reproduce DK scoring: it is used for participation only. Without box scores the
grade says PARTICIPATION=UNKNOWN and drafted non-players are graded as zeros (labeled).

Metrics per group (F, D, G) and overall, on persons who played: MAE of the mean, bias, CRPS (sample form),
p10-p90 coverage (target 0.80), correlation. Goalies are graded on their nonzero draws (conditional on starting).
Goalie decisions: the start probability implied by the frozen draws (share of nonzero draws) against the actual
starter (most time on ice). Bonus-rate calibration is NOT_AVAILABLE: the cache holds points, not event counts.
One game outcome counts once: `played` lists (game, person) pairs for the evidence counts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")


def load_draws(run) -> tuple[list[str], np.ndarray] | None:
    d = run.path / "scenario"
    meta_p = d / "meta.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    chunks = sorted((d / "selection").glob("chunk_*.npy"))
    if not chunks:
        return None
    return list(meta["person_keys"]), np.concatenate([np.load(c) for c in chunks])


def crps(draws: np.ndarray, y: float) -> float:
    """Sample CRPS: E|X - y| - E|X - X'| / 2, with E|X - X'| from the sorted sample."""
    x = np.sort(np.asarray(draws, dtype=float))
    n = len(x)
    i = np.arange(1, n + 1)
    e_xx = 2.0 * float(np.sum((2 * i - n - 1) * x)) / (n * n)
    return float(np.mean(np.abs(x - y))) - 0.5 * e_xx


# -- box scores and participation ----------------------------------------------------------------------------------

def fetch_boxscores(pool, *, cache=None, offline: bool = False) -> tuple[list, list[str]]:
    """Final box scores of the pool's games (schedule by the games' Eastern dates). Never raises."""
    from nhl_dfs.data.http import HttpCache
    from nhl_dfs.data.identity.crosswalk import verified_team_map
    from nhl_dfs.data.sources import nhl

    http = cache if cache is not None else HttpCache(offline=offline)
    tm = verified_team_map()
    want = {(tm.get(g.away), tm.get(g.home)): key for key, g in pool.games.items()}
    boxes, notes = [], []
    for day in sorted({g.start_utc.astimezone(ET).date() for g in pool.games.values()}):
        try:
            games = nhl.schedule(day, cache=http)
        except Exception as exc:
            notes.append(f"schedule {day}: unavailable ({type(exc).__name__})")
            continue
        for g in games:
            if (g.away, g.home) not in want:
                continue
            try:
                b = nhl.boxscore(g.game_id, cache=http)
            except Exception as exc:
                notes.append(f"box score {g.game_id}: unavailable ({type(exc).__name__})")
                continue
            if b.game_state not in ("OFF", "FINAL"):
                notes.append(f"box score {g.game_id}: game state {b.game_state}, not final (not used)")
                continue
            boxes.append(b)
    missing = len(want) - len(boxes)
    if missing:
        notes.append(f"{missing} of {len(want)} slate game(s) without a final box score")
    return boxes, notes


def participation(pool, boxes: list, accepted: dict[str, int] | None = None) -> tuple[dict[str, bool | None], dict[str, str]]:
    """person_key -> True (dressed and on the ice), False (his team's final box score does not have him), None
    (no box score for his game, or no accepted crosswalk id). Also person_key -> game key for those with a game."""
    from nhl_dfs.data.identity.crosswalk import accepted as accepted_ids
    from nhl_dfs.data.identity.crosswalk import key_sha, verified_team_map

    acc = accepted if accepted is not None else accepted_ids()
    tm = verified_team_map()
    on_ice: dict[str, set[int]] = {}
    for b in boxes:
        for x in list(b.skaters) + list(b.goalies):
            if x.toi_s > 0:
                on_ice.setdefault(x.team, set()).add(x.nhl_id)
    game_of = {}
    for key, g in pool.games.items():
        game_of[g.home] = key
        game_of[g.away] = key
    out: dict[str, bool | None] = {}
    games: dict[str, str] = {}
    seen = set()
    for r in pool.rows:
        if r.person_key in seen:
            continue
        seen.add(r.person_key)
        group = "G" if r.is_goalie else ("D" if r.position == "D" else "F")
        nhl_team = tm.get(r.team)
        nid = acc.get(key_sha(r.name, r.team, group))
        if r.team in game_of:
            games[r.person_key] = game_of[r.team]
        if nhl_team is None or nhl_team not in on_ice or nid is None:
            out[r.person_key] = None
        else:
            out[r.person_key] = nid in on_ice[nhl_team]
    return out, games


def actual_starters(pool, boxes: list, accepted: dict[str, int] | None = None) -> dict[str, str | None]:
    """DK team -> person_key of the goalie with the most time on ice (None when unknown)."""
    from nhl_dfs.data.identity.crosswalk import accepted as accepted_ids
    from nhl_dfs.data.identity.crosswalk import key_sha, verified_team_map

    acc = accepted if accepted is not None else accepted_ids()
    tm = verified_team_map()
    by_id = {}
    for r in pool.rows:
        if r.is_goalie:
            nid = acc.get(key_sha(r.name, r.team, "G"))
            if nid is not None:
                by_id[nid] = r.person_key
    out: dict[str, str | None] = {}
    for b in boxes:
        for team in (b.home, b.away):
            gs = sorted((g for g in b.goalies if g.team == team), key=lambda g: -g.toi_s)
            dk = next((d for d, n in tm.items() if n == team and d in pool.teams), None)
            if dk is not None:
                out[dk] = by_id.get(gs[0].nhl_id) if gs else None
    return out


# -- the grade ------------------------------------------------------------------------------------------------------

@dataclass
class ForecastGrade:
    run_id: str
    mode: str
    participation_status: str  # BOX_SCORES | UNKNOWN
    overall: dict
    by_group: dict
    goalie_decisions: dict
    bonus_rate_calibration: str
    played: list[tuple[str, str, str]]  # (game key, person_key, group) of GRADED persons who played
    excluded: dict
    worst: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skater_conditioning: str = ""

    def record(self) -> dict:
        return asdict(self)


def _summ(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    m = np.array([r["mean"] for r in rows])
    y = np.array([r["actual"] for r in rows])
    out = {"n": len(rows), "mae": round(float(np.mean(np.abs(m - y))), 3), "bias": round(float(np.mean(m - y)), 3),
           "crps": round(float(np.mean([r["crps"] for r in rows])), 3),
           "cover_p10_p90": round(float(np.mean([r["covered"] for r in rows])), 3)}
    if len(rows) >= 3 and np.std(m) > 0 and np.std(y) > 0:
        out["pearson"] = round(float(np.corrcoef(m, y)[0, 1]), 3)
    return out


def grade(run, boxscores: list | None = None, *, actual_points: dict[str, int], pool=None,
          accepted: dict[str, int] | None = None) -> ForecastGrade | None:
    """actual_points: person_key -> DK points in tenths (standings FPTS, FLEX scale). None when the run saved no
    scenario cache (no frozen forecast to grade)."""
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.intake.salary import read_salary

    got = load_draws(run)
    if got is None:
        return None
    keys, draws = got
    m = read_manifest(run)
    pool = pool or read_salary(run.inputs / "DKSalaries.csv")
    group = {}
    for r in pool.rows:
        group.setdefault(r.person_key, "G" if r.is_goalie else ("D" if r.position == "D" else "F"))
    notes = []
    if boxscores:
        part, games = participation(pool, boxscores, accepted)
        status = "BOX_SCORES"
    else:
        part, games = {k: None for k in group}, {}
        status = "UNKNOWN"
        notes.append("PARTICIPATION=UNKNOWN: no box scores, so drafted players who did not play are graded as zeros")
    rows, excluded = [], {"no_actual": 0, "did_not_play": 0, "participation_unknown": 0, "not_in_forecast": 0}
    for pk, y10 in actual_points.items():
        if pk not in keys:
            excluded["not_in_forecast"] += 1
            continue
        p = part.get(pk)
        if p is False:
            excluded["did_not_play"] += 1
            continue
        if p is None and status == "BOX_SCORES":
            excluded["participation_unknown"] += 1
            continue
        d = draws[:, keys.index(pk)].astype(float) / 10.0
        if group.get(pk) == "G":
            d = d[d != 0]  # conditional on starting
            if len(d) == 0:
                continue
        y = y10 / 10.0
        q10, q90 = np.quantile(d, [0.1, 0.9])
        rows.append({"person_key": pk, "group": group.get(pk, "F"), "mean": float(d.mean()), "actual": y,
                     "crps": crps(d, y), "covered": bool(q10 <= y <= q90), "q10": float(q10), "q90": float(q90)})
    excluded["no_actual"] = sum(1 for k in keys if k not in actual_points)
    by_group = {g: _summ([r for r in rows if r["group"] == g]) for g in ("F", "D", "G")}
    # goalie decisions: the frozen draws' implied start probability vs the actual starter
    dec: dict = {"status": "NOT_AVAILABLE (no box scores)"}
    if boxscores:
        starters = actual_starters(pool, boxscores, accepted)
        teams, correct, brier = 0, 0, []
        misses = []
        for team, starter in sorted(starters.items()):
            gks = [k for k in keys if group.get(k) == "G" and k.split("|")[1] == team]
            if not gks or starter is None:
                continue
            implied = {k: float((draws[:, keys.index(k)] != 0).mean()) for k in gks}
            pred = max(implied, key=implied.get)
            teams += 1
            correct += int(pred == starter)
            brier += [(implied[k] - (k == starter)) ** 2 for k in gks]
            if pred != starter:
                misses.append(f"{team}: predicted {pred.split('|')[0]} ({implied[pred]:.2f}), started "
                              f"{starter.split('|')[0]} ({implied.get(starter, 0.0):.2f})")
        dec = {"status": "implied from the frozen draws (share of nonzero draws)", "teams": teams,
               "accuracy": round(correct / teams, 3) if teams else None,
               "brier": round(float(np.mean(brier)), 4) if brier else None, "misses": misses}
    graded = {r["person_key"] for r in rows}
    played = sorted({(games[k], k, group.get(k, "F")) for k, v in part.items() if v and k in games and k in graded})
    worst = sorted(rows, key=lambda r: -abs(r["mean"] - r["actual"]))[:8]
    return ForecastGrade(run.run_id, m["mode"], status, _summ(rows), by_group, dec,
                         "NOT_AVAILABLE: the scenario cache holds DK points, not event counts (5+ SOG, 3+ blocks, "
                         "3+ points, 35+ saves)", played, excluded,
                         [{"person": r["person_key"].split("|")[0], "team": r["person_key"].split("|")[1],
                           "mean": round(r["mean"], 2), "actual": r["actual"]} for r in worst], notes,
                         "UNCONDITIONAL on dressing: skater draws include the simulator's not-dressed zeros (a per-draw "
                         "dressed indicator is not cached), which pulls means and quantiles down for players who played; "
                         "goalies are graded on their nonzero draws (conditional on starting)")
