"""Grade the frozen pre-lock player forecasts (card C11; plan section 12 layer table).

Forecast: the run's own scenario cache, `runs/<id>/scenario/selection/` (the first 8,000 draws per person, DK
points in tenths, FLEX scale in Showdown), saved by the C8 pass before lock. The draws are UNMASKED for
QUESTIONABLE players (the run's participation mask is separate), but the simulator draws dressing per scenario
(sim/game.py: Bernoulli p_dress), so a skater's column includes his not-dressed zeros, and a goalie's column
includes his not-started zeros. Nothing is re-simulated or rebuilt.

Caches written from backlog B25/B28 on keep per-draw indicators beside the points (scenario_cache flags: dressed,
started, one per DK bonus): skaters are then graded on their dressed draws, a goalie who started on his started
draws (a relief goalie on his relief draws), and bonus rates are calibrated. An older cache holds points only, so
"not dressed" cannot be told from "dressed, 0 points": skater grades stay UNCONDITIONAL on dressing and goalies are
graded on nonzero draws, exactly as before (labeled). Saved probabilities (B26, meta.json `participation`) are
graded when present; older runs fall back to the share of nonzero draws (labeled).

Actual points: the standings' FPTS (every drafted player; Showdown FLEX scale). FPTS cannot tell "did not play"
from "played and scored 0" and has nothing for undrafted players, so participation comes from NHL box scores
(C4 adapter, joined to persons through the accepted identity crosswalk). The box score lacks shorthanded points
and shootout goals, so it cannot reproduce DK scoring: it is used for participation only. Without box scores the
grade says PARTICIPATION=UNKNOWN and drafted non-players are graded as zeros (labeled).

Metrics per group (F, D, G) and overall, on persons who played: MAE of the mean, bias, CRPS (sample form),
p10-p90 coverage (target 0.80), correlation. Goalies are graded on their nonzero draws (conditional on starting).
Goalie decisions: the saved start probability (older runs: implied by the frozen draws, share of nonzero draws)
against the actual starter (most time on ice). Bonus-rate calibration: predicted rate from the frozen indicators
(skaters on dressed draws, goalies on started draws) against the rate observed in NHL box scores for every pool
person who played, per bonus the box score can decide: hat trick, 5+ shots, 3+ blocked shots, 3+ points (skaters),
35+ saves and shutout (the starter, goals against 0 and no other goalie of his team on the ice). The shorthanded
point bonus is not graded: the box score has no shorthanded points. Older caches: NOT_AVAILABLE.
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


def load_flags(run, n_rows: int, notes: list[str]) -> tuple[dict | None, np.ndarray | None]:
    """(meta, (n, P, F) bool selection indicators) or (meta, None) for an older cache or any mismatch (noted)."""
    from nhl_dfs.build.scenario_cache import read_flags

    d = run.path / "scenario"
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("flags"):
        return meta, None
    if int(meta["purposes"]["selection"]["n"]) != n_rows:
        notes.append(f"selection indicators describe {meta['purposes']['selection']['n']} draws, the points {n_rows}: "
                     "indicators not used")
        return meta, None
    fl = read_flags(d, meta, "selection", n_rows, 0, notes)
    return meta, fl


BONUS_GRADED = {  # flag -> (who, decides from the box line); the rules' bonus list, minus what box scores lack
    "hat_trick": ("skaters", lambda x: x["goals"] >= 3),
    "sog_5": ("skaters", lambda x: x["sog"] >= 5),
    "blocks_3": ("skaters", lambda x: x["blocks"] >= 3),
    "points_3": ("skaters", lambda x: x["goals"] + x["assists"] >= 3),
    "saves_35": ("goalie starters", lambda x: x["saves"] >= 35),
    "shutout": ("goalie starters", lambda x: x["ga"] == 0 and x["sole_goalie"]),
}
BONUS_NOT_GRADED = {"sh_point": "the NHL box score has no shorthanded points"}

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


def box_lines(pool, boxes: list, accepted: dict[str, int] | None = None) -> dict[str, dict]:
    """person_key -> his box-score line (players on the ice only), joined through the accepted crosswalk. Goalies
    carry started (most time on ice for his team) and sole_goalie (no other goalie of his team on the ice)."""
    from nhl_dfs.data.identity.crosswalk import accepted as accepted_ids
    from nhl_dfs.data.identity.crosswalk import key_sha

    acc = accepted if accepted is not None else accepted_ids()
    by_id: dict[int, str] = {}
    seen = set()
    for r in pool.rows:
        if r.person_key in seen:
            continue
        seen.add(r.person_key)
        group = "G" if r.is_goalie else ("D" if r.position == "D" else "F")
        nid = acc.get(key_sha(r.name, r.team, group))
        if nid is not None:
            by_id[nid] = r.person_key
    out: dict[str, dict] = {}
    for b in boxes:
        for x in b.skaters:
            if x.toi_s > 0 and x.nhl_id in by_id:
                out[by_id[x.nhl_id]] = {"goals": x.goals, "assists": x.assists, "sog": x.sog, "blocks": x.blocks,
                                        "goalie": False}
        for team in (b.home, b.away):
            gs = sorted((g for g in b.goalies if g.team == team and g.toi_s > 0), key=lambda g: -g.toi_s)
            for i, g in enumerate(gs):
                if g.nhl_id in by_id:
                    out[by_id[g.nhl_id]] = {"saves": g.saves, "ga": g.goals_against, "goalie": True,
                                            "started": i == 0, "sole_goalie": len(gs) == 1}
    return out


def bonus_calibration(keys: list[str], flags: np.ndarray, names: list[str], group: dict, lines: dict) -> dict:
    """Per graded bonus: predicted rate (mean indicator on the person's dressed draws, a goalie starter's started
    draws) against the observed rate, over every pool person the box scores have; Brier score and a z statistic
    (observed minus expected count over the Poisson-binomial sd)."""
    ix = {n: i for i, n in enumerate(names)}
    col = {k: i for i, k in enumerate(keys)}
    out: dict = {}
    for b, (who, decide) in BONUS_GRADED.items():
        ps, ys = [], []
        for k, x in lines.items():
            if k not in col:
                continue
            j = col[k]
            if who == "skaters":
                if x["goalie"] or group.get(k) == "G":
                    continue
                cond = flags[:, j, ix["dressed"]]
            else:
                if not x["goalie"] or not x["started"]:
                    continue
                cond = flags[:, j, ix["started"]]
            if not cond.any():
                continue
            ps.append(float(flags[cond, j, ix[b]].mean()))
            ys.append(1.0 if decide(x) else 0.0)
        if not ps:
            out[b] = {"who": who, "n": 0}
            continue
        p, y = np.array(ps), np.array(ys)
        sd = float(np.sqrt(np.sum(p * (1 - p))))
        out[b] = {"who": who, "n": len(p), "expected": round(float(p.sum()), 2), "observed": int(y.sum()),
                  "pred_rate": round(float(p.mean()), 4), "obs_rate": round(float(y.mean()), 4),
                  "brier": round(float(np.mean((p - y) ** 2)), 4),
                  "z": round(float((y.sum() - p.sum()) / sd), 2) if sd > 0 else None}
    for b, why in BONUS_NOT_GRADED.items():
        out[b] = {"status": f"NOT_GRADED: {why}"}
    return out


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
    bonus_rate_calibration: str | dict  # a NOT_AVAILABLE label (older cache) or one record per bonus
    played: list[tuple[str, str, str]]  # (game key, person_key, group) of GRADED persons who played
    excluded: dict
    worst: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skater_conditioning: str = ""
    indicators: str = ""  # B25/B28: whether the frozen cache has per-draw indicators
    start_probability_check: dict = field(default_factory=dict)  # B26: saved p_start vs the draws' share
    play_probability: dict = field(default_factory=dict)  # B26: saved (or implied) play probability vs box scores

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
    meta, flags = load_flags(run, len(draws), notes)
    names = list(meta["flags"]["names"]) if flags is not None else []
    fi = {n: i for i, n in enumerate(names)}
    saved = meta.get("participation") if isinstance(meta.get("participation"), dict) else None
    lines = box_lines(pool, boxscores, accepted) if (boxscores and flags is not None) else {}
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
        j = keys.index(pk)
        d = draws[:, j].astype(float) / 10.0
        if flags is not None:  # B28: the frozen indicators say which draws he played in
            if group.get(pk) == "G":
                ln = lines.get(pk)
                cond = flags[:, j, fi["started"]] if (ln is None or ln.get("started", True)) \
                    else flags[:, j, fi["dressed"]] & ~flags[:, j, fi["started"]]  # a relief appearance
            else:
                cond = flags[:, j, fi["dressed"]]
            d = d[cond]
            if len(d) == 0:
                continue
        elif group.get(pk) == "G":
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
    saved_g = (saved or {}).get("goalies") or {}
    if boxscores:
        starters = actual_starters(pool, boxscores, accepted)
        teams, correct, brier = 0, 0, []
        misses = []
        for team, starter in sorted(starters.items()):
            gks = [k for k in keys if group.get(k) == "G" and k.split("|")[1] == team]
            if not gks or starter is None:
                continue
            if saved_g:  # B26: the start probability the run saved at build
                implied = {k: float((saved_g.get(k) or {}).get("p_start", 0.0)) for k in gks}
            else:
                implied = {k: float((draws[:, keys.index(k)] != 0).mean()) for k in gks}
            pred = max(implied, key=implied.get)
            teams += 1
            correct += int(pred == starter)
            brier += [(implied[k] - (k == starter)) ** 2 for k in gks]
            if pred != starter:
                misses.append(f"{team}: predicted {pred.split('|')[0]} ({implied[pred]:.2f}), started "
                              f"{starter.split('|')[0]} ({implied.get(starter, 0.0):.2f})")
        dec = {"status": ("saved at build (scenario/meta.json participation)" if saved_g else
                          "implied from the frozen draws (share of nonzero draws)"), "teams": teams,
               "accuracy": round(correct / teams, 3) if teams else None,
               "brier": round(float(np.mean(brier)), 4) if brier else None, "misses": misses}
        if saved_g:
            dec["sources"] = dict(sorted({s: sum(1 for v in saved_g.values() if v.get("source") == s)
                                          for s in {v.get("source") for v in saved_g.values()}}.items(), key=str))
    check = _start_check(keys, draws, flags, fi, saved_g, group)
    play = _play_grade(keys, draws, flags, fi, saved, group, part) if boxscores else \
        {"status": "NOT_AVAILABLE (no box scores)"}
    if flags is not None:
        bonus = bonus_calibration(keys, flags, names, group, lines) if lines else \
            {"status": "NOT_AVAILABLE (no box scores)"}
        conditioning = ("CONDITIONAL on dressing: skaters graded on their dressed draws and goalies on their started "
                        "draws (a relief goalie on his relief draws), from the run's frozen per-draw indicators")
        indicators = f"per-draw indicators in the frozen cache: {', '.join(names)}"
    else:
        bonus = ("NOT_AVAILABLE: the scenario cache holds DK points, not event counts (5+ SOG, 3+ blocks, "
                 "3+ points, 35+ saves)")
        conditioning = ("UNCONDITIONAL on dressing: skater draws include the simulator's not-dressed zeros (a per-draw "
                        "dressed indicator is not cached), which pulls means and quantiles down for players who played; "
                        "goalies are graded on their nonzero draws (conditional on starting)")
        indicators = "NONE: an older cache (points only); graded exactly as before B25/B28"
    graded = {r["person_key"] for r in rows}
    played = sorted({(games[k], k, group.get(k, "F")) for k, v in part.items() if v and k in games and k in graded})
    worst = sorted(rows, key=lambda r: -abs(r["mean"] - r["actual"]))[:8]
    return ForecastGrade(run.run_id, m["mode"], status, _summ(rows), by_group, dec, bonus, played, excluded,
                         [{"person": r["person_key"].split("|")[0], "team": r["person_key"].split("|")[1],
                           "mean": round(r["mean"], 2), "actual": r["actual"]} for r in worst], notes,
                         conditioning, indicators, check, play)


def bonus_summary(bonus) -> str:
    """One line for notes and the CLI: the label as is, or per graded bonus observed / expected."""
    if isinstance(bonus, str):
        return bonus
    if "status" in bonus and len(bonus) == 1:
        return bonus["status"]
    parts = []
    for b, v in bonus.items():
        if v.get("n"):
            parts.append(f"{b} {v['observed']} observed / {v['expected']:.1f} expected (n {v['n']}, z {v['z']})")
        elif "status" in v:
            parts.append(f"{b} {v['status']}")
        else:
            parts.append(f"{b} n 0")
    return "; ".join(parts)


def _start_check(keys, draws, flags, fi, saved_g: dict, group: dict) -> dict:
    """B26 acceptance: the saved start probabilities against the frozen draws' share (started indicator, else the
    nonzero share), in Monte Carlo standard errors."""
    if not saved_g:
        return {"status": "NOT_AVAILABLE: no start probability saved at build (older run); decisions use the share "
                          "of nonzero draws"}
    n = draws.shape[0]
    rows = []
    for k, v in saved_g.items():
        if k not in keys:
            continue
        j = keys.index(k)
        share = float(flags[:, j, fi["started"]].mean()) if flags is not None else float((draws[:, j] != 0).mean())
        p = float(v.get("p_start", 0.0))
        se = max(np.sqrt(p * (1 - p) / n), 1.0 / n)
        rows.append((abs(share - p) / se, k, p, share))
    if not rows:
        return {"status": "no saved goalie in the cache's person axis"}
    worst = max(rows)
    return {"status": "saved p_start vs " + ("the started share" if flags is not None else "the nonzero share"),
            "goalies": len(rows), "max_abs_z": round(float(worst[0]), 2),
            "worst": {"goalie": worst[1].split("|")[0], "p_start": round(worst[2], 4), "share": round(worst[3], 4)},
            "within_3_se": bool(worst[0] <= 3.0)}


def _play_grade(keys, draws, flags, fi, saved, group: dict, part: dict) -> dict:
    """B26: Brier score of the play probability against box-score participation, per group. Saved p_play (the
    simulator's dress or start probability times the participation mask) when the run saved it; else the share of
    nonzero draws (older runs; understates a skater's dressing, since dressed zero-point games are zeros too)."""
    persons = (saved or {}).get("persons") or {}
    by: dict[str, list] = {"F": [], "D": [], "G": []}
    for k, v in part.items():
        if v is None or k not in keys:
            continue
        if persons:
            if k not in persons:
                continue
            p = float(persons[k]["p_play"])
        else:
            p = float((draws[:, keys.index(k)] != 0).mean())
        by.setdefault(group.get(k, "F"), []).append((p - (1.0 if v else 0.0)) ** 2)
    out = {"status": ("saved at build (p_play = dress or start probability x participation mask)" if persons else
                      "IMPLIED: share of nonzero draws (older run; no play probability saved)")}
    for g, xs in by.items():
        out[g] = {"n": len(xs), "brier": round(float(np.mean(xs)), 4) if xs else None}
    return out
