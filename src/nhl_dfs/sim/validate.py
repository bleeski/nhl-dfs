"""Calibration harness (C6, plan section 5 "Simulation scale and calibration"): grade the simulator
against held-out games.

For each held-out date D the harness rebuilds what a slate run would have known: per-person
parameters from history strictly before D (models.params.build on a synthetic pool of each
team's recent roster), the model-source game intensities, and the same simulator. It then compares
the simulation with the games actually played on D.

What is graded, and what is not:
  * Grading is CONDITIONAL on who actually dressed and started: each person's simulated
    distribution is taken over the scenarios in which he dressed (a goalie: started). It checks the
    STAT model (rates, allocation, dispersion, bonuses), not the participation model.
  * The pre-game roster is every skater and goalie who appeared for the team in its last
    `roster_games` games. A person who played on D but is not in that roster is counted and left out.
  * There are no historical odds in the store, so "team totals vs market" and "goalie win vs
    implied" are reported UNAVAILABLE. The model-source counterparts are labeled MODEL, never MARKET.
  * PIT values for count stats are randomized (uniform within the probability mass at the observed
    value). Independent draws across people in one game are not assumed; the chi-square flag is a
    screen, not a test of record.

The report is written to docs/calibration/<date>.md and .json; `deficiencies` is the machine-
readable list a later gate (C13) reads.

Clean constants (C25, [BEN] flag 114): `clean_constants` recomputes the league values config/model.yaml and
config/sim.yaml record as measured, from the same window with every game on a held-out date removed, and hands
them to `report` through `cfg` and `model_cfg`. The shipped files are never written.
"""

from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import optimize

from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import person_key
from nhl_dfs.data.features import asof as asof_mod
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.intake.salary import GameInfo, PersonRows, SalaryPool
from nhl_dfs.models import params as params_mod
from nhl_dfs.models.rates import load_model_config
from nhl_dfs.sim import game as game_mod
from nhl_dfs.sim import outcomes as oc
from nhl_dfs.sim import resolve as rs
from nhl_dfs.sim.market import load_sim_config
from nhl_dfs.sim.slate import build_slate

REPO_ROOT = Path(__file__).resolve().parents[3]
CAL_DIR = REPO_ROOT / "docs" / "calibration"
ROSTER_GAMES = 10
STATS = {"goals": "goals", "assists": "assists", "points": None, "sog": "sog", "blocks": "blocks"}
# DK bonus thresholds first, then the shoulders that show whether a miss is in the tail or the body.
BONUSES = [("sog>=5", "sog", 5), ("blocks>=3", "blocks", 3), ("points>=3", "points", 3), ("goals>=3", "goals", 3),
           ("points>=2", "points", 2), ("goals>=2", "goals", 2), ("sog>=3", "sog", 3), ("blocks>=2", "blocks", 2)]


@dataclass
class CalibrationReport:
    as_of_dates: list[str]
    n_scenarios: int
    created_utc: str
    coverage: dict = field(default_factory=dict)
    pit: dict = field(default_factory=dict)
    rates: list[dict] = field(default_factory=list)
    co_ceiling: dict = field(default_factory=dict)
    team: dict = field(default_factory=dict)
    unavailable: dict = field(default_factory=dict)
    deficiencies: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    wall_s: float = 0.0
    constants: dict = field(default_factory=dict)  # C25: the clean constants table (empty: the shipped files were used)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, default=float)

    def to_markdown(self) -> str:
        c = self.coverage
        out = [f"# Simulator calibration ({self.created_utc[:10]})", "",
               f"Held-out dates: {', '.join(self.as_of_dates)}. {self.n_scenarios} scenarios per date. "
               f"MODEL_STATUS of the simulated parameters: from history strictly before each date. "
               f"Wall clock {self.wall_s:.0f} s.", "",
               "Graded CONDITIONAL on who actually dressed and started: this checks the stat model, not participation.", "",
               "## Coverage", "",
               f"- games {c.get('games', 0)}, skater-games observed {c.get('skaters_observed', 0)}, "
               f"matched to the pre-game roster {c.get('skaters_matched', 0)}; goalie starts {c.get('goalies_matched', 0)} "
               f"of {c.get('goalies_observed', 0)}",
               f"- not in the pre-game roster (left out): {c.get('skaters_unmatched', 0)} skaters, "
               f"{c.get('goalies_unmatched', 0)} goalies", "",
               "## Probability integral transform (randomized), by stat", "",
               "| stat | n | chi-square / df | flag | histogram (deciles) |", "|---|---:|---:|---|---|"]
        for stat, p in self.pit.items():
            out.append(f"| {stat} | {p['n']} | {p['chi2_per_df']:.2f} | {'FLAG' if p['flagged'] else 'ok'} | "
                       f"{' '.join(str(x) for x in p['hist'])} |")
        out += ["", "## Bonus and tail rates (observed vs simulated expectation)", "",
                "| event | observed | expected | ratio | n | flag |", "|---|---:|---:|---:|---:|---|"]
        for r in self.rates:
            out.append(f"| {r['event']} | {r['observed']} | {r['expected']:.1f} | {r['ratio']:.2f} | {r['n']} | {r['flag']} |")
        cc = self.co_ceiling
        out += ["", "## Line-pair co-ceiling (both linemates with 2+ points)", "",
                f"- model lines (predicted from ice time and shared-ice ids, not actual lines): {cc.get('pairs', 0)} pair-games, "
                f"observed {cc.get('observed', 0)}, expected {cc.get('expected', 0.0):.1f}, ratio {cc.get('ratio', 0.0):.2f} "
                f"({cc.get('flag', 'n/a')})",
                f"- control (forward teammates on different model lines): {cc.get('control_pairs', 0)} pair-games, observed "
                f"{cc.get('control_observed', 0)}, expected {cc.get('control_expected', 0.0):.1f}, ratio {cc.get('control_ratio', 0.0):.2f}; "
                "a co-ceiling miss that shows up equally in the control is not about line structure", "", "## Team goals", ""]
        t = self.team
        out.append(f"- MODEL intensities (no odds): observed mean {t.get('obs_mean', 0):.2f}, simulated {t.get('sim_mean', 0):.2f}; "
                   f"observed sd {t.get('obs_sd', 0):.2f}, simulated {t.get('sim_sd', 0):.2f}; PIT chi-square/df {t.get('chi2_per_df', 0):.2f}")
        out += ["", "## Not available", ""]
        out += [f"- {k}: UNAVAILABLE ({v})" for k, v in self.unavailable.items()]
        out += ["", "## Deficiencies (machine-readable copy in the .json)", ""]
        out += [f"- {d['kind']} {d['what']}: {d['detail']}" for d in self.deficiencies] or ["- none flagged"]
        k = self.constants
        if k:
            out += ["", "## League constants (C25, flag 114)", "",
                    f"Recomputed from the regular seasons {', '.join(str(s) for s in k['seasons'])} without the "
                    f"{len(k['excluded_dates'])} held-out dates ({k['games_all'] - k['games_used']} of {k['games_all']} games "
                    "removed). 'all dates' is the same computation with nothing removed; 'moved' is clean minus all dates, the "
                    "held-out effect alone. Only CLEANED rows reach the simulation; the shipped config files are unchanged.", "",
                    "| constant | kind | shipped | all dates | clean | moved | status |", "|---|---|---:|---:|---:|---:|---|"]
            num = lambda x: "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.6g}"  # noqa: E731
            for r in k["rows"]:
                out.append(f"| {r['name']} | {r['kind']} | {num(r['shipped'])} | {num(r['all_dates'])} | {num(r['clean'])} | "
                           f"{num(r['moved'])} | {r['status']} |")
            out += [f"- not cleaned, {n['name']}: {n['why']}" for n in k["not_cleaned"]]
        if self.notes:
            out += ["", "## Notes", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(out) + "\n"


# -- building a pre-game slate from history -----------------------------------------------------------

def _pool_for(D: date, roster: pd.DataFrame, games: list[tuple[str, str]]) -> tuple[SalaryPool, dict[str, int]]:
    """A synthetic Classic pool of the pre-game rosters; constant salaries (only PRIOR persons'
    salary-based means use them, and the calibration never reads those)."""
    rows, persons, crosswalk = [], {}, {}
    for r in roster.itertuples(index=False):
        pos = "G" if r.position == "G" else ("D" if r.position == "D" else "C")
        key = person_key(r.name, r.team, "G" if pos == "G" else pos)
        row = PoolRow(role_id=str(int(r.nhl_id)), person_key=key, name=r.name, team=r.team, position=pos,
                      roster_positions=frozenset({"G"} if pos == "G" else {pos, "UTIL"}), salary=4000, game_info="",
                      appg_raw=None, appg_flag="MISSING")
        rows.append(row)
        persons[key] = PersonRows(classic=row)
        crosswalk[key] = int(r.nhl_id)
    start = datetime(D.year, D.month, D.day, 23, 0, tzinfo=timezone.utc)
    gi = {f"{a}@{h}": GameInfo(h, a, "", start) for h, a in games}
    pool = SalaryPool(Mode.CLASSIC, rows, {r.role_id: r for r in rows}, persons, frozenset(r.team for r in rows), gi, [],
                      "calibration", b"")
    return pool, crosswalk


def _roster(sk: pd.DataFrame, gl: pd.DataFrame, team: str, D: date, k: int) -> pd.DataFrame:
    """Everyone who appeared for `team` in its last k regular-season games before D."""
    def recent(df):
        t = df[(df["team"] == team) & (df["game_date"] < D)]
        ids = t.drop_duplicates("game_id").sort_values("game_date", ascending=False)["game_id"].head(k)
        return t[t["game_id"].isin(set(ids))].sort_values("game_date").drop_duplicates("nhl_id", keep="last")
    s, g = recent(sk), recent(gl)
    a = s[["nhl_id", "name", "team", "position"]]
    b = g[["nhl_id", "name", "team"]].assign(position="G")
    return pd.concat([a, b], ignore_index=True)


def pick_dates(seasons: int, n_dates: int, *, store_root=None, min_history_days: int = 30) -> list[date]:
    """Evenly spaced game dates from each of the most recent `seasons` seasons in the store, skipping the
    first weeks (there is no history yet)."""
    present = sorted(store_mod.seasons_present("skater_games", root=store_root))
    out: list[date] = []
    for season in present[-seasons:]:
        df = store_mod.read("skater_games", [season], root=store_root)
        df = df[df["regime"] == "regular"]
        days = sorted(set(df["game_date"]))
        days = days[min_history_days:] if len(days) > min_history_days + n_dates else days[len(days) // 2:]
        if not days:
            continue
        idx = np.linspace(0, len(days) - 1, min(n_dates, len(days))).round().astype(int)
        out += [days[i] for i in sorted(set(idx))]
    return out


# -- statistics ---------------------------------------------------------------------------------------

def _pit(x_sim: np.ndarray, x_obs: float, rng) -> float:
    below = float((x_sim < x_obs).mean())
    equal = float((x_sim == x_obs).mean())
    return below + rng.random() * equal


def _chi2_per_df(values: list[float], bins: int) -> tuple[float, list[int]]:
    hist, _ = np.histogram(values, bins=bins, range=(0.0, 1.0))
    exp = len(values) / bins
    chi2 = float(((hist - exp) ** 2 / exp).sum()) if exp > 0 else 0.0
    return chi2 / (bins - 1), hist.tolist()


# -- league constants without the held-out dates (C25, [BEN] flag 114) ---------------------------------------

# The window config/model.yaml (C5, 2026-09-28) and config/sim.yaml (C6, 2026-09-29) were measured on. Fixed: a newer
# season in the store never changes what "the shipped value, recomputed" means.
CONSTANT_SEASONS = (20242025, 20252026)
# (file, path into that config, printed decimals). Each is a ratio of season sums over per-game rows (no season
# summaries, no caches); `league_constants` holds the definitions. Rows sharing a list (assists_per_goal) are cleaned
# together or not at all.
MEASURED: list[tuple[str, tuple, int]] = (
    [("model", ("skaters", g, k, s), 0 if k == "toi_s" else 3) for g in ("F", "D") for k in ("toi_s", "pts60")
     for s in ("ev", "pp", "sh")]
    + [("model", ("skaters", g, k), d) for g in ("F", "D")
       for k, d in (("goal_share", 3), ("a1_share", 3), ("sog60", 3), ("blk60", 3), ("ixg_per_sog", 4))]
    + [("model", ("team", "pp_clock_s"), 0), ("model", ("team", "pk_clock_s"), 0), ("model", ("team", "sog_for"), 2),
       ("model", ("goalies", "sv_pct"), 4), ("model", ("goalies", "shots_against"), 2), ("model", ("goalies", "p_win"), 3),
       ("model", ("goalies", "p_otl"), 3), ("model", ("goalies", "pull_per_ga"), 3),
       ("sim", ("resolve", "goals_league"), 3), ("sim", ("resolve", "ot_goal_p"), 4)]
    + [("sim", ("skater", "assists_per_goal", i), 3) for i in range(3)])
DERIVED: list[tuple[str, tuple, int]] = ([("sim", ("resolve", "lambda0_league"), 4), ("sim", ("resolve", "q_eq"), 4)]
                                         + [("sim", ("resolve", "q_en", i), 4) for i in range(4)])
# The inputs the DERIVED values are solved from, as config/sim.yaml's comment prints them (they are not config keys).
INPUTS_SHIPPED = {"in.reg_goals": 2.970, "in.tie_rate": 0.2275, "in.en_goals": 0.196, "in.ot_goals": 0.076}
PACE_TARGET_SHIPPED = 0.2205  # config/sim.yaml pace comment: team SOG cv after removing team-season means


def constant_name(file: str, path: tuple) -> str:
    keys = [str(p) for p in path if not isinstance(p, int)]
    idx = "".join(f"[{p}]" for p in path if isinstance(p, int))
    return f"{file}." + ".".join(keys) + idx


def _get(cfg: dict, path: tuple):
    x = cfg
    for p in path:
        x = x[p]
    return float(x)


def _set(cfg: dict, path: tuple, value: float) -> None:
    x = cfg
    for p in path[:-1]:
        x = x[p]
    x[path[-1]] = float(value)


def _div(a, b) -> float:
    a, b = float(a), float(b)
    return a / b if b != 0 else float("nan")


def _reproduces(value: float | None, shipped: float, decimals: int) -> bool:
    return value is not None and not math.isnan(value) and abs(value - shipped) <= 0.5 * 10.0 ** -decimals + 1e-12


def league_constants(sk: pd.DataFrame, gl: pd.DataFrame, *, exclude_dates=()) -> dict[str, float]:
    """Every MEASURED value at full precision from regular-season per-game rows, minus every game dated in
    exclude_dates; plus the inputs the DERIVED values are solved from (in.*), the pace target and counts (n.*)."""
    ex = set(exclude_dates)
    sk = sk[(sk["regime"] == "regular") & ~sk["game_date"].isin(ex)]
    gl = gl[(gl["regime"] == "regular") & ~gl["game_date"].isin(ex)]
    out: dict[str, float] = {}
    pts = sk["goals"] + sk["assists"]
    for g, part in (("F", sk["position"] != "D"), ("D", sk["position"] == "D")):
        d, p = sk[part], pts[part]
        a = d[d["tier"] == "A"]  # a1 and ixg are MoneyPuck (Tier A) columns
        name = lambda *k: constant_name("model", ("skaters", g) + k)  # noqa: E731
        for s in ("ev", "pp", "sh"):
            out[name("toi_s", s)] = float(d[f"toi_{s}_s"].mean())
        out[name("pts60", "ev")] = 3600 * _div((p - d["pp_points"] - d["sh_points"]).sum(), d["toi_ev_s"].sum())
        out[name("pts60", "pp")] = 3600 * _div(d["pp_points"].sum(), d["toi_pp_s"].sum())
        out[name("pts60", "sh")] = 3600 * _div(d["sh_points"].sum(), d["toi_sh_s"].sum())
        out[name("goal_share")] = _div(d["goals"].sum(), p.sum())
        out[name("a1_share")] = _div(a["a1"].sum(), a["assists"].sum())
        out[name("sog60")] = 3600 * _div(d["sog"].sum(), d["toi_s"].sum())
        out[name("blk60")] = 3600 * _div(d["blocks"].sum(), d["toi_s"].sum())
        out[name("ixg_per_sog")] = _div(a["ixg"].sum(), a["sog"].sum())
    tg = sk.groupby(["game_id", "team"], as_index=False).agg(
        pp=("toi_pp_s", "sum"), sh=("toi_sh_s", "sum"), sog=("sog", "sum"), goals=("goals", "sum"), season=("season", "first"))
    out["model.team.pp_clock_s"] = float(tg["pp"].mean()) / 5  # five skaters on the ice on a power play
    out["model.team.pk_clock_s"] = float(tg["sh"].mean()) / 4  # four shorthanded
    out["model.team.sog_for"] = float(tg["sog"].mean())
    played = gl[gl["toi_s"] > 0]
    st = gl[gl["started"].astype(bool)]
    out["model.goalies.sv_pct"] = _div(played["saves"].sum(), played["shots_against"].sum())
    out["model.goalies.shots_against"] = float(st["shots_against"].mean())
    out["model.goalies.p_win"] = float((st["decision"] == "W").mean())
    out["model.goalies.p_otl"] = float((st["decision"] == "O").mean())
    two = played.groupby(["game_id", "team"]).size() >= 2  # a second goalie played: the starter was pulled
    out["model.goalies.pull_per_ga"] = _div(two.mean(), st["goals_against"].mean())
    n_games = int(tg["game_id"].nunique())
    out["sim.resolve.goals_league"] = float(tg["goals"].mean())  # skater goals per team-game, OT goals included
    past = set(gl.loc[gl["decision"] == "O", "game_id"])  # an overtime or shootout loss: tied after regulation
    spread = tg.groupby("game_id")["goals"].agg(lambda s: s.max() - s.min())
    ot_goal_games = int((spread[spread.index.isin(past)] > 0).sum())  # a shootout goal is not a skater goal
    out["sim.resolve.ot_goal_p"] = _div(ot_goal_games, len(past))
    ga = gl.groupby(["game_id", "team"], as_index=False)["goals_against"].sum()
    opp = tg.merge(ga, on="game_id", suffixes=("", "_opp"))
    opp = opp[opp["team"] != opp["team_opp"]]
    out["in.tie_rate"] = _div(len(past), n_games)
    out["in.ot_goals"] = _div(ot_goal_games, 2 * n_games)
    out["in.reg_goals"] = out["sim.resolve.goals_league"] - out["in.ot_goals"]
    out["in.en_goals"] = float((opp["goals"] - opp["goals_against"]).mean())  # skater goals minus the opposing goalies' GA
    a = sk[sk["tier"] == "A"]
    g_all, a1, a2 = float(a["goals"].sum()), float(a["a1"].sum()), float(a["a2"].sum())
    for i, v in enumerate((1 - _div(a1, g_all), _div(a1 - a2, g_all), _div(a2, g_all))):  # a secondary needs a primary
        out[f"sim.skater.assists_per_goal[{i}]"] = v
    resid = tg["sog"] - tg.groupby(["team", "season"])["sog"].transform("mean")
    out["pace.target_cv"] = _div(resid.std(), tg["sog"].mean())
    out["pace.resid_sd"], out["pace.sog_mean"] = float(resid.std()), float(tg["sog"].mean())
    out["n.games"] = n_games
    out["n.skater_rows"] = int(len(sk))
    out["n.goalie_starts"] = int(len(st))
    return out


def derive_resolve(reg_goals: float, tie_rate: float, en_goals: float, sim_cfg: dict) -> dict | None:
    """lambda0_league, q_eq and q_en (q_en_shape scaled) that make the resolver reproduce per-team regulation goals
    (empty-net goals included), the regulation tie rate and the empty-net goals per team-game, with equal strengths;
    None when the solve fails or leaves the probabilities outside [0, 1]."""
    r = sim_cfg["resolve"]
    shape = [float(x) for x in r["q_en_shape"]]

    def rules(q_eq: float, c: float) -> rs.Rules:
        return rs.Rules(q_eq, tuple(c * s for s in shape), float(r["ot_goal_p"]), float(r["ot_slope"]),
                        float(r["so_home_p"]), bool(r["so_adds_goal"]))

    def res(x):
        lam = math.exp(x[0])
        s = rs.summary(lam, lam, rules(x[1], x[2]))
        return [s.e_home_goals - reg_goals, s.p_tie - tie_rate, s.e_en / 2 - en_goals]

    if any(math.isnan(v) for v in (reg_goals, tie_rate, en_goals)):
        return None
    sol = optimize.root(res, [math.log(2.75), 0.17, 0.69], method="hybr", tol=1e-13)
    lam, q_eq, c = math.exp(sol.x[0]), float(sol.x[1]), float(sol.x[2])
    if not sol.success or max(abs(v) for v in res(sol.x)) > 1e-9 or not (0 <= q_eq <= 1 and 0 <= c * max(shape) <= 1):
        return None
    return {"lambda0_league": lam, "q_eq": q_eq, "q_en": [c * s for s in shape]}


def _derived_values(d: dict | None) -> list[float | None]:
    return [None] * 6 if d is None else [d["lambda0_league"], d["q_eq"], *d["q_en"]]


def clean_constants(exclude_dates, *, store_root=None, sim_cfg: dict | None = None, model_cfg: dict | None = None,
                    seasons=CONSTANT_SEASONS) -> tuple[dict, dict, dict]:
    """(sim config, model config, table): deep copies of the given (default: shipped) configs in which every value that
    reproduces from the store to its printed precision with no date removed is replaced by its value without
    exclude_dates, at full precision. Values that do not reproduce stay as given and are listed NOT_CLEANED. With no
    dates excluded this is the all-dates recomputation. The config files are never written."""
    given = {"sim": copy.deepcopy(sim_cfg if sim_cfg is not None else load_sim_config()),
             "model": copy.deepcopy(model_cfg if model_cfg is not None else load_model_config())}
    out = copy.deepcopy(given)
    present = set(store_mod.seasons_present("skater_games", root=store_root))
    ss = [s for s in seasons if s in present]
    if not ss:
        raise ValueError(f"the history store has none of the seasons {', '.join(str(s) for s in seasons)}; nothing to recompute")
    sk = store_mod.read("skater_games", ss, root=store_root)
    gl = store_mod.read("goalie_games", ss, root=store_root)
    ex = sorted(set(exclude_dates))
    allv, clean = league_constants(sk, gl), league_constants(sk, gl, exclude_dates=ex)
    rows: list[dict] = []
    for file, path, dec in MEASURED:
        nm = constant_name(file, path)
        rows.append({"name": nm, "file": file, "path": list(path), "kind": "MEASURED", "decimals": dec,
                     "shipped": _get(given[file], path), "all_dates": allv[nm], "clean": clean[nm]})
    printed = derive_resolve(round(allv["sim.resolve.goals_league"], 3) - round(allv["in.ot_goals"], 3),
                             round(allv["in.tie_rate"], 4), round(allv["in.en_goals"], 3), given["sim"])
    solved = {k: derive_resolve(v["in.reg_goals"], v["in.tie_rate"], v["in.en_goals"], given["sim"]) for k, v in
              (("all", allv), ("clean", clean))}
    for (file, path, dec), p, a, c in zip(DERIVED, _derived_values(printed), _derived_values(solved["all"]),
                                          _derived_values(solved["clean"])):
        rows.append({"name": constant_name(file, path), "file": file, "path": list(path), "kind": "DERIVED", "decimals": dec,
                     "shipped": _get(given[file], path), "all_dates": a, "clean": c, "solved_from_printed_inputs": p})
    for r in rows:
        ref = r.get("solved_from_printed_inputs", r["all_dates"]) if r["kind"] == "DERIVED" else r["all_dates"]
        r["reproduced"] = _reproduces(ref, r["shipped"], r["decimals"])
        r["moved"] = None if r["clean"] is None or r["all_dates"] is None else r["clean"] - r["all_dates"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = "sim.resolve (derived)" if r["kind"] == "DERIVED" else r["name"].split("[")[0]
        groups.setdefault(key, []).append(r)
    cleaned, not_cleaned = [], []
    for key, members in groups.items():
        ok = all(r["reproduced"] and r["clean"] is not None and not math.isnan(r["clean"]) for r in members)
        for r in members:
            r["status"] = "CLEANED" if ok else "NOT_CLEANED"
            if ok:
                _set(out[r["file"]], tuple(r["path"]), r["clean"])
        if ok:
            cleaned.append(key)
        else:
            bad = [r for r in members if not r["reproduced"]]
            why = ("does not reproduce from the store: " + "; ".join(
                f"{r['name']} recomputes to {r.get('solved_from_printed_inputs', r['all_dates'])} against {r['shipped']} shipped"
                for r in bad)) if bad else "the value without the held-out dates could not be computed"
            not_cleaned.append({"name": key, "why": why})
    # The comment's own arithmetic: regulation goals 3.046 minus 0.076; the pace target 6.186 / 28.05.
    as_printed = {"in.reg_goals": round(allv["sim.resolve.goals_league"], 3) - round(allv["in.ot_goals"], 3),
                  "in.tie_rate": allv["in.tie_rate"], "in.en_goals": allv["in.en_goals"], "in.ot_goals": allv["in.ot_goals"],
                  "sim.pace.target_cv": _div(round(allv["pace.resid_sd"], 3), round(allv["pace.sog_mean"], 2))}
    for nm in ("in.reg_goals", "in.tie_rate", "in.en_goals", "in.ot_goals"):
        rows.append({"name": nm, "file": "sim", "path": [], "kind": "INPUT", "decimals": 4 if nm == "in.tie_rate" else 3,
                     "shipped": INPUTS_SHIPPED[nm], "all_dates": allv[nm], "clean": clean[nm], "moved": clean[nm] - allv[nm],
                     "status": "INPUT", "reproduced": _reproduces(as_printed[nm], INPUTS_SHIPPED[nm], 4 if nm == "in.tie_rate" else 3)})
    rows.append({"name": "sim.pace.target_cv", "file": "sim", "path": [], "kind": "TARGET", "decimals": 4,
                 "shipped": PACE_TARGET_SHIPPED, "all_dates": allv["pace.target_cv"], "clean": clean["pace.target_cv"],
                 "moved": clean["pace.target_cv"] - allv["pace.target_cv"], "status": "TARGET",
                 "reproduced": _reproduces(as_printed["sim.pace.target_cv"], PACE_TARGET_SHIPPED, 4)})
    rows.append({"name": "sim.pace.var", "file": "sim", "path": ["pace", "var"], "kind": "CALIBRATED", "decimals": 4,
                 "shipped": _get(given["sim"], ("pace", "var")), "all_dates": None, "clean": None, "moved": None,
                 "status": "NOT_CLEANED", "reproduced": False})
    not_cleaned.append({"name": "sim.pace.var", "why": (
        "calibrated by a synthetic simulation that is not in the repo, so it cannot be re-solved here; its target, the team "
        f"SOG cv after removing team-season means, is {allv['pace.target_cv']:.4f} on all dates and "
        f"{clean['pace.target_cv']:.4f} without the held-out dates ({PACE_TARGET_SHIPPED} in the config comment)")})
    table = {"seasons": ss, "excluded_dates": [d.isoformat() for d in ex], "games_all": allv["n.games"],
             "games_used": clean["n.games"], "rows": rows, "cleaned": cleaned, "not_cleaned": not_cleaned}
    return out["sim"], out["model"], table


def report(as_of_dates, cfg: dict | None = None, *, model_cfg: dict | None = None, constants: dict | None = None,
           store_root=None, n_scenarios: int | None = None, seed: int = 20260929, roster_games: int = ROSTER_GAMES,
           progress=None) -> CalibrationReport:
    """cfg and model_cfg (default: the shipped files) reach the per-person parameters and the simulation as a fresh copy
    per date. constants: the clean_constants table behind them, embedded in the report; it must exclude every graded
    date, or the report would call constants clean that saw the games it grades."""
    cfg = cfg or load_sim_config()
    model_cfg = model_cfg if model_cfg is not None else load_model_config()
    if constants is not None:
        missing = sorted({d.isoformat() for d in as_of_dates} - set(constants["excluded_dates"]))
        if missing:
            raise ValueError(f"the constants table does not exclude the graded dates {', '.join(missing)}")
    cal = cfg["calibrate"]
    n = int(n_scenarios or cal["n_scenarios"])
    bins = int(cal["pit_bins"])
    t0 = time.perf_counter()
    seasons = sorted({s for k in ("skater_games", "goalie_games") for s in store_mod.seasons_present(k, root=store_root)})
    sk_all = store_mod.read("skater_games", seasons, root=store_root)
    gl_all = store_mod.read("goalie_games", seasons, root=store_root)
    sk_all = sk_all[sk_all["regime"] == "regular"]
    gl_all = gl_all[gl_all["regime"] == "regular"]
    rng = np.random.default_rng(seed)
    pits: dict[str, list[float]] = {k: [] for k in ("goals", "assists", "points", "sog", "blocks", "saves", "ga", "team_goals")}
    counts = {k: 0 for k in ("games", "skaters_observed", "skaters_matched", "goalies_observed", "goalies_matched")}
    obs_ev = {e[0]: 0 for e in BONUSES} | {"saves>=35": 0, "goalie win": 0}
    exp_ev = {k: 0.0 for k in obs_ev}
    n_ev = {k: 0 for k in obs_ev}
    co_obs = co_pairs = 0
    co_exp = 0.0
    ctl_obs = ctl_pairs = 0  # control: forward teammates from DIFFERENT model lines
    ctl_exp = 0.0
    team_obs: list[float] = []
    team_sim_mean: list[float] = []
    team_sim_var: list[float] = []
    dates_used: list[str] = []
    notes: list[str] = []

    for D in sorted(as_of_dates):
        day_sk = sk_all[sk_all["game_date"] == D]
        day_gl = gl_all[gl_all["game_date"] == D]
        if day_sk.empty:
            notes.append(f"{D}: no games in the store")
            continue
        games = []
        for gid, g in day_sk.groupby("game_id"):
            home = g[g["home"].astype(bool)]["team"].iloc[0] if g["home"].astype(bool).any() else g["team"].iloc[0]
            away = next((t for t in g["team"].unique() if t != home), None)
            if away is not None:
                games.append((home, away))
        teams = sorted({t for hg in games for t in hg})
        roster = pd.concat([_roster(sk_all, gl_all, t, D, roster_games) for t in teams], ignore_index=True)
        if roster.empty:
            notes.append(f"{D}: no pre-game roster (no earlier games in the store)")
            continue
        pool, crosswalk = _pool_for(D, roster, games)
        feats = asof_mod.frame(D, None, store_root=store_root)
        table = params_mod.build(pool, crosswalk, D, copy.deepcopy(model_cfg), features=feats, store_root=store_root)
        slate, _ = build_slate(pool, table, None, cfg=copy.deepcopy(cfg), model_cfg=copy.deepcopy(model_cfg),
                               now=datetime(D.year, D.month, D.day, tzinfo=timezone.utc))
        prep = game_mod.prepare(slate, table)
        chunks, left, ci = [], n, 0
        while left > 0:
            size = min(int(cfg["chunk_size"]), left)
            chunks.append(game_mod.simulate_chunk(prep, size, seed + int(D.strftime("%Y%m%d")), "referee", ci))
            left -= size
            ci += 1
        o = oc.concat(chunks)
        col_of = {k: i for i, k in enumerate(o.person_keys)}
        key_of_id = {int(v): k for k, v in crosswalk.items()}
        pts = o.goals.astype(int) + o.assists.astype(int)
        sim = {"goals": o.goals, "assists": o.assists, "points": pts, "sog": o.sog, "blocks": o.blocks}
        counts["games"] += len(games)
        dates_used.append(D.isoformat())
        # skaters
        for r in day_sk.itertuples(index=False):
            counts["skaters_observed"] += 1
            key = key_of_id.get(int(r.nhl_id))
            if key is None or key not in col_of or o.is_goalie[col_of[key]]:
                continue
            c = col_of[key]
            mask = o.dressed[:, c]
            if mask.sum() < 50:
                continue
            counts["skaters_matched"] += 1
            obs = {"goals": r.goals, "assists": r.assists, "points": r.goals + r.assists, "sog": r.sog, "blocks": r.blocks}
            for stat, x in obs.items():
                pits[stat].append(_pit(sim[stat][mask, c], x, rng))
            for name, stat, thr in BONUSES:
                obs_ev[name] += int(obs[stat] >= thr)
                exp_ev[name] += float((sim[stat][mask, c] >= thr).mean())
                n_ev[name] += 1
        # goalies
        for r in day_gl[day_gl["started"].astype(bool)].itertuples(index=False):
            counts["goalies_observed"] += 1
            key = key_of_id.get(int(r.nhl_id))
            if key is None or key not in col_of or not o.is_goalie[col_of[key]]:
                continue
            c = col_of[key]
            mask = o.dressed[:, c]
            if mask.sum() < 50:
                continue
            counts["goalies_matched"] += 1
            pits["saves"].append(_pit(o.saves[mask, c], r.saves, rng))
            pits["ga"].append(_pit(o.ga[mask, c], r.goals_against, rng))
            obs_ev["saves>=35"] += int(r.saves >= 35)
            exp_ev["saves>=35"] += float((o.saves[mask, c] >= 35).mean())
            n_ev["saves>=35"] += 1
            obs_ev["goalie win"] += int(r.decision == "W")
            exp_ev["goalie win"] += float((o.decision[mask, c] == oc.DECISIONS["W"]).mean())
            n_ev["goalie win"] += 1
        # team goals and line-pair co-ceiling
        tix = {t: i for i, t in enumerate(o.teams)}
        for gid, g in day_sk.groupby("game_id"):
            for team, tg in g.groupby("team"):
                if team in tix:
                    x = float(tg["goals"].sum())
                    sim_g = o.team_goals[:, tix[team]]
                    pits["team_goals"].append(_pit(sim_g, x, rng))
                    team_obs.append(x)
                    team_sim_mean.append(float(sim_g.mean()))
                    team_sim_var.append(float(sim_g.var()))
        obs_pts = {(int(r.nhl_id)): int(r.goals + r.assists) for r in day_sk.itertuples(index=False)}
        for team, tp in prep.teams.items():
            line_of = {i: int(np.argmax(tp.m_f[i])) for i in range(tp.n_listed) if tp.is_f[i]}
            fwd = sorted(line_of)
            for x in range(len(fwd)):
                for y in range(x + 1, len(fwd)):
                    i, j = fwd[x], fwd[y]
                    same = line_of[i] == line_of[j]
                    ka, kb = o.person_keys[tp.cols[i]], o.person_keys[tp.cols[j]]
                    ia, ib = crosswalk.get(ka), crosswalk.get(kb)
                    if ia not in obs_pts or ib not in obs_pts:
                        continue
                    both = o.dressed[:, tp.cols[i]] & o.dressed[:, tp.cols[j]]
                    if both.sum() < 50:
                        continue
                    pa, pb = pts[:, tp.cols[i]], pts[:, tp.cols[j]]
                    e = float(((pa >= 2) & (pb >= 2))[both].mean())
                    hit = int(obs_pts[ia] >= 2 and obs_pts[ib] >= 2)
                    if same:
                        co_exp, co_obs, co_pairs = co_exp + e, co_obs + hit, co_pairs + 1
                    else:
                        ctl_exp, ctl_obs, ctl_pairs = ctl_exp + e, ctl_obs + hit, ctl_pairs + 1
        if progress:
            progress(f"{D}: {len(games)} games, {len(roster)} rostered persons")

    rep = CalibrationReport(dates_used, n, datetime.now(timezone.utc).isoformat())
    c = counts
    rep.coverage = {**c, "skaters_unmatched": c["skaters_observed"] - c["skaters_matched"],
                    "goalies_unmatched": c["goalies_observed"] - c["goalies_matched"]}
    flag_min = int(cal["min_expected"])
    for stat, vals in pits.items():
        if not vals:
            continue
        chi, hist = _chi2_per_df(vals, bins)
        flagged = chi > float(cal["pit_chi2_flag"]) and len(vals) >= 200
        rep.pit[stat] = {"n": len(vals), "chi2_per_df": chi, "hist": hist, "flagged": flagged}
        if flagged:
            rep.deficiencies.append({"kind": "pit", "what": stat, "detail": f"chi-square/df {chi:.2f} over {len(vals)} observations",
                                     "value": chi})
    lo, hi = cal["rate_ratio_band"]
    for name in obs_ev:
        if not n_ev[name]:
            continue
        exp, obs = exp_ev[name], obs_ev[name]
        ratio = obs / exp if exp > 0 else float("nan")
        out_of_band = exp >= flag_min and not (lo <= ratio <= hi)
        flag = "FLAG" if out_of_band else ("ok" if exp >= flag_min else "too few to flag")
        rep.rates.append({"event": name, "observed": obs, "expected": exp, "ratio": ratio, "n": n_ev[name], "flag": flag})
        if out_of_band:
            rep.deficiencies.append({"kind": "rate", "what": name, "detail": f"observed {obs} vs expected {exp:.1f} (ratio {ratio:.2f})",
                                     "value": ratio})
    ratio = co_obs / co_exp if co_exp > 0 else float("nan")
    cflag = "FLAG" if co_exp >= flag_min and not (lo <= ratio <= hi) else ("ok" if co_exp >= flag_min else "too few to flag")
    cratio = ctl_obs / ctl_exp if ctl_exp > 0 else float("nan")
    rep.co_ceiling = {"pairs": co_pairs, "observed": co_obs, "expected": co_exp, "ratio": ratio, "flag": cflag,
                      "control_pairs": ctl_pairs, "control_observed": ctl_obs, "control_expected": ctl_exp, "control_ratio": cratio}
    if cflag == "FLAG":
        rep.deficiencies.append({"kind": "co_ceiling", "what": "model-line pairs, both 2+ points",
                                 "detail": f"observed/expected {ratio:.2f} within model lines, {cratio:.2f} across model lines. "
                                           "Hypothesis, not established: the rank-built model lines do not identify actual "
                                           "linemates; the same ratios are also consistent with within-line correlation in the "
                                           "simulation being too strong", "value": ratio})
    if team_obs:
        chi = rep.pit.get("team_goals", {}).get("chi2_per_df", 0.0)
        rep.team = {"obs_mean": float(np.mean(team_obs)), "obs_sd": float(np.std(team_obs)), "sim_mean": float(np.mean(team_sim_mean)),
                    "sim_sd": float(np.sqrt(np.mean(team_sim_var))), "chi2_per_df": chi, "n": len(team_obs)}
    rep.unavailable = {
        "team totals vs market": "the store holds no historical odds; the rows above are the MODEL-source intensities only",
        "goalie win vs implied": "no historical moneylines; 'goalie win' above is graded against the simulator's own probability",
    }
    if constants is None:
        league = ("'held out' means only the per-person parameters use games strictly before each date; the league "
                  "constants (config/model.yaml means, config/sim.yaml resolve values and the pace target) were measured on "
                  "both seasons including these dates, which flatters the team-goal, tie-rate and goalie-win rows")
    else:
        rep.constants = constants
        league = (f"'held out' means the per-person parameters use games strictly before each date and the league constants "
                  f"were recomputed without the {len(constants['excluded_dates'])} held-out dates "
                  f"({len(constants['cleaned'])} cleaned groups; not cleaned, so still measured on these dates: "
                  f"{', '.join(n['name'] for n in constants['not_cleaned']) or 'none'}); the clean constants still use games "
                  "played after each held-out date, though not the graded games themselves")
        if constants["not_cleaned"]:
            rep.deficiencies.append({"kind": "constants", "what": "league constants not cleaned",
                                     "detail": "; ".join(f"{n['name']}: {n['why']}" for n in constants["not_cleaned"]),
                                     "value": float(len(constants["not_cleaned"]))})
    rep.notes = notes + [league,
                         "persons with no history use position priors (PRIOR); the calibration includes them",
                         "team SOG-to-goal correlation is higher in the simulation than in history (score effects are not modeled)"]
    rep.wall_s = time.perf_counter() - t0
    return rep


def write_report(rep: CalibrationReport, out_dir: Path | None = None) -> tuple[Path, Path]:
    out = Path(out_dir) if out_dir else CAL_DIR
    out.mkdir(parents=True, exist_ok=True)
    stem = rep.created_utc[:10]
    md, js = out / f"{stem}.md", out / f"{stem}.json"
    md.write_text(rep.to_markdown(), encoding="utf-8")
    js.write_text(rep.to_json(), encoding="utf-8")
    return md, js


def run_cli(args) -> int:
    cfg = load_sim_config()
    n_dates = int(args.dates or cfg["calibrate"]["n_dates"])
    dates = pick_dates(int(args.seasons), n_dates, store_root=args.store_root)
    if not dates:
        print("calibrate: the history store has no games; run `history --backfill 2` first")
        return 1
    print(f"calibrate: {len(dates)} held-out dates across the last {args.seasons} season(s), "
          f"{args.scenarios or cfg['calibrate']['n_scenarios']} scenarios each; graded conditional on who dressed and started")
    model_cfg, table = None, None
    if getattr(args, "clean_constants", False):
        cfg, model_cfg, table = clean_constants(dates, store_root=args.store_root, sim_cfg=cfg)
        print(f"constants: recomputed without the {len(dates)} held-out dates ({table['games_all'] - table['games_used']} of "
              f"{table['games_all']} games); cleaned {len(table['cleaned'])} groups; not cleaned: "
              f"{', '.join(n['name'] for n in table['not_cleaned']) or 'none'}")
    rep = report(dates, cfg, model_cfg=model_cfg, constants=table, store_root=args.store_root, n_scenarios=args.scenarios,
                 progress=lambda m: print(f"  {m}"))
    md, js = write_report(rep, args.out_dir)
    for stat, p in rep.pit.items():
        print(f"PIT {stat}: n {p['n']}, chi-square/df {p['chi2_per_df']:.2f}{' FLAG' if p['flagged'] else ''}")
    for r in rep.rates:
        print(f"rate {r['event']}: observed {r['observed']}, expected {r['expected']:.1f}, ratio {r['ratio']:.2f} ({r['flag']})")
    cc = rep.co_ceiling
    print(f"co-ceiling: observed {cc['observed']}, expected {cc['expected']:.1f} ({cc['flag']}); control pairs observed "
          f"{cc['control_observed']}, expected {cc['control_expected']:.1f}")
    print(f"deficiencies flagged: {len(rep.deficiencies)}; UNAVAILABLE: {', '.join(rep.unavailable)}")
    print(f"wall clock {rep.wall_s:.0f} s; wrote {md} and {js}")
    return 0
