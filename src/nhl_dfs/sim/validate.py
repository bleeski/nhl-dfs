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
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import person_key
from nhl_dfs.data.features import asof as asof_mod
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.intake.salary import GameInfo, PersonRows, SalaryPool
from nhl_dfs.models import params as params_mod
from nhl_dfs.sim import game as game_mod
from nhl_dfs.sim import outcomes as oc
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


def report(as_of_dates, cfg: dict | None = None, *, store_root=None, n_scenarios: int | None = None, seed: int = 20260929,
           roster_games: int = ROSTER_GAMES, progress=None) -> CalibrationReport:
    cfg = cfg or load_sim_config()
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
        table = params_mod.build(pool, crosswalk, D, features=feats, store_root=store_root)
        slate, _ = build_slate(pool, table, None, now=datetime(D.year, D.month, D.day, tzinfo=timezone.utc))
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
    rep.notes = notes + ["'held out' means only the per-person parameters use games strictly before each date; the league "
                         "constants (config/model.yaml means, config/sim.yaml resolve values and the pace target) were measured on "
                         "both seasons including these dates, which flatters the team-goal, tie-rate and goalie-win rows",
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
    rep = report(dates, cfg, store_root=args.store_root, n_scenarios=args.scenarios, progress=lambda m: print(f"  {m}"))
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
