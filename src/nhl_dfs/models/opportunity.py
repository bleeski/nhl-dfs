"""Opportunity per skater (C5, plan section 5 steps 1 and 2): the root of the points DAG.

p_dress: games dressed / team games since the player's current team stint began (a traded
player is not charged for games before he arrived), Beta-shrunk to the position prior. News and
DK status are not used here (C3 prices DTD; C7 owns news).
TOI by strength: decay-weighted means shrunk toward the position prior (pseudo-games).
pp_share: the player's PP time over the TEAM PP clock (team PP clock = summed skater PP TOI /
skaters on ice), clamped to [0, 1].
Units: the most-used 5on5 unit and 5on4 unit in MoneyPuck line_games (Tier A) dated before
as_of; "" with units_missing = 1 otherwise.
RoleState (C7 supplies it; a minimal form lives here): a person with no history who holds a role
gets the role's slot expectation instead of the position bucket (call-ups).
reconcile(): scales depth skaters' EV TOI so a team's expected EV skater-time fits the manpower
budget skaters_on_ice x (3600 - PP clock - PK clock) from the penalty-rate coupling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from nhl_dfs.data.history.combine import group_of
from nhl_dfs.models.rates import STRENGTHS, decay_weights, regular_games


@dataclass(frozen=True)
class RoleState:
    ev_line: dict = field(default_factory=dict)  # person id (nhl_id or person_key) -> line / pair number (1-based)
    pp_unit: dict = field(default_factory=dict)  # person id -> PP unit (1 or 2)


@dataclass
class Opportunity:
    p_dress: float
    toi_ev_s: float
    toi_pp_s: float
    toi_sh_s: float
    sd_toi_s: float
    pp_share: float
    unit_ev: str = ""
    unit_pp: str = ""
    units_missing: int = 1
    source: str = "prior"  # "prior" | "role" | "history"
    n_games: int = 0
    ev_scale: float = 1.0

    @property
    def toi_s(self) -> float:
        return self.toi_ev_s + self.toi_pp_s + self.toi_sh_s


def prior_opportunity(group: str, cfg: dict, role_key=None, roles: RoleState | None = None) -> Opportunity:
    p = cfg["skaters"][group]
    toi = dict(p["toi_s"])
    source = "prior"
    if roles is not None and role_key is not None:
        rp = cfg["roles"][group]
        line = roles.ev_line.get(role_key)
        unit = roles.pp_unit.get(role_key)
        if line is not None:
            toi["ev"] = float(rp["ev_line_toi_s"][min(int(line), len(rp["ev_line_toi_s"])) - 1])
            source = "role"
        if unit is not None:
            toi["pp"] = float(rp["pp_unit_toi_s"][min(int(unit), len(rp["pp_unit_toi_s"])) - 1])
            source = "role"
    pp_clock = float(cfg["team"]["pp_clock_s"])
    return Opportunity(p_dress=float(p["p_dress"]), toi_ev_s=float(toi["ev"]), toi_pp_s=float(toi["pp"]),
                       toi_sh_s=float(toi["sh"]), sd_toi_s=float(p["toi_sd_s"]),
                       pp_share=min(1.0, float(toi["pp"]) / pp_clock), source=source)


def team_pp_clock(sk: pd.DataFrame, cfg: dict) -> pd.Series:
    """(team, game_id) -> team PP clock seconds."""
    return sk.groupby(["team", "game_id"])["toi_pp_s"].sum() / float(cfg["team"]["skaters_on_ice"])


def _units(lines: pd.DataFrame | None, as_of: date | None, last_n: int = 20) -> dict[int, tuple[str, str]]:
    if lines is None or lines.empty:
        return {}
    ln = lines if as_of is None else lines[lines["game_date"] < as_of]
    out: dict[int, dict[str, str]] = {}
    for sit, slot in (("5on5", 0), ("5on4", 1)):
        part = ln[ln["situation"] == sit]
        rows = [(int(pid), r.game_id, r.game_date, r.line_id, r.toi_s)
                for r in part.itertuples(index=False) for pid in r.player_ids.split("|") if pid]
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["nhl_id", "game_id", "game_date", "line_id", "toi_s"])
        recent = df[df.groupby("nhl_id")["game_date"].rank(method="dense", ascending=False) <= last_n]
        best = recent.groupby(["nhl_id", "line_id"])["toi_s"].sum().reset_index().sort_values(
            ["nhl_id", "toi_s", "line_id"], ascending=[True, False, True]).groupby("nhl_id").head(1)
        for r in best.itertuples(index=False):
            out.setdefault(int(r.nhl_id), {"0": "", "1": ""})[str(slot)] = str(r.line_id)
    return {k: (v["0"], v["1"]) for k, v in out.items()}


def estimate(features, roles: RoleState | None, cfg: dict, *, nhl_ids=None, groups: dict | None = None,
             line_games: pd.DataFrame | None = None, as_of: date | None = None) -> dict[int, Opportunity]:
    """Opportunity per nhl_id. features.skaters should hold ALL players' rows (team clocks and team
    game counts need teammates); results are returned for nhl_ids (default: everyone present)."""
    sk = regular_games(features, cfg)
    groups = groups or {}
    want = None if nhl_ids is None else {int(x) for x in nhl_ids}
    out: dict[int, Opportunity] = {}
    units = _units(line_games, as_of)
    if not sk.empty:
        if as_of is not None:
            sk = sk[sk["game_date"] < as_of]
        clock = team_pp_clock(sk, cfg)
        team_games = sk.groupby("team")[["game_id", "game_date"]].apply(
            lambda d: d.drop_duplicates("game_id").sort_values("game_date"))
        kt, kd = cfg["prior_games"]["toi"], cfg["prior_games"]["dress"]
        for pid, g in sk.groupby("nhl_id"):
            pid = int(pid)
            if want is not None and pid not in want:
                continue
            group = group_of(g["position"].iloc[-1])
            if group == "G":
                continue
            p = cfg["skaters"][group]
            g = g.sort_values(["game_date", "game_id"], ascending=False)
            w = decay_weights(len(g), cfg["decay_half_life_games"])
            toi = {s: float((w * g[f"toi_{s}_s"]).sum() + kt * p["toi_s"][s]) / (w.sum() + kt) for s in STRENGTHS}
            tot = g["toi_s"].to_numpy(float)
            m = (w * tot).sum() / w.sum()
            sd = float(np.sqrt(((w * (tot - m) ** 2).sum() + kt * p["toi_sd_s"] ** 2) / (w.sum() + kt)))
            clk = np.array([clock.get((t, gid), 0.0) for t, gid in zip(g["team"], g["game_id"])])
            # prior pseudo-games: PP TOI p["toi_s"]["pp"] against a league team PP clock
            pp_share = float(((w * g["toi_pp_s"]).sum() + kt * p["toi_s"]["pp"]) /
                             ((w * clk).sum() + kt * float(cfg["team"]["pp_clock_s"])))
            # p_dress over the current stint only
            team = g["team"].iloc[0]
            stint = g[g["team_stint"] == g["team_stint"].max()] if "team_stint" in g else g[g["team"] == team]
            start = stint["game_date"].min()
            tg = team_games.loc[team] if team in team_games.index.get_level_values(0) else pd.DataFrame()
            n_team = int((tg["game_date"] >= start).sum()) if len(tg) else len(stint)
            p_dress = (len(stint) + kd * p["p_dress"]) / (max(n_team, len(stint)) + kd)
            ue, up = units.get(pid, ("", ""))
            out[pid] = Opportunity(p_dress=float(p_dress), toi_ev_s=toi["ev"], toi_pp_s=toi["pp"], toi_sh_s=toi["sh"],
                                   sd_toi_s=sd, pp_share=float(min(1.0, max(0.0, pp_share))), unit_ev=ue, unit_pp=up,
                                   units_missing=int(not (ue or up)), source="history", n_games=len(g))
    for pid in want or []:
        if pid not in out:
            out[pid] = prior_opportunity(groups.get(pid, "F"), cfg, pid, roles)
    return out


def reconcile(opps: dict, team_of: dict, cfg: dict) -> dict:
    """Scale below-median EV TOI per team so sum(p_dress * EV TOI) fits the manpower budget."""
    t = cfg["team"]
    budget = float(t["skaters_on_ice"]) * (3600.0 - float(t["pp_clock_s"]) - float(t["pk_clock_s"]))
    by_team: dict[str, list] = {}
    for k, o in opps.items():
        by_team.setdefault(team_of[k], []).append(k)
    for team, keys in by_team.items():
        total = sum(opps[k].p_dress * opps[k].toi_ev_s for k in keys)
        if total <= budget or not keys:
            continue
        med = float(np.median([opps[k].toi_ev_s for k in keys]))
        depth = [k for k in keys if opps[k].toi_ev_s < med]
        top = total - sum(opps[k].p_dress * opps[k].toi_ev_s for k in depth)
        dsum = sum(opps[k].p_dress * opps[k].toi_ev_s for k in depth)
        if dsum <= 0:
            continue
        f = max(float(t["min_scale"]), min(1.0, (budget - top) / dsum))
        for k in depth:
            opps[k].toi_ev_s *= f
            opps[k].ev_scale = f
    return opps


def _shrink_to(ps: list[float], target: float) -> list[float]:
    """Shift every probability by one common amount c <= 0 in log-odds so they sum to target:
    near-certain regulars barely move, uncertain depth players absorb most of the cut."""
    if sum(ps) <= target:
        return ps
    eps = 1e-9
    logits = [math.log(max(p, eps) / max(1 - p, eps)) for p in ps]
    lo, hi = -40.0, 0.0
    for _ in range(80):
        c = (lo + hi) / 2
        total = sum(1 / (1 + math.exp(-(x + c))) for x in logits)
        if total > target:  # the sum falls as c falls: move the upper bound down
            hi = c
        else:
            lo = c
    c = (lo + hi) / 2
    return [1 / (1 + math.exp(-(x + c))) for x in logits]


def dress_budget(opps: dict, team_of: dict, group_of_key: dict, cfg: dict) -> dict:
    """Normalize p_dress per team so expected dressed forwards and defensemen match the budget
    (backlog B4). History players (source "history") keep their evidence first: if they alone
    exceed the budget they are cut by one common log-odds shift (regulars barely move). Players
    without history (prior or role) share the residual the same way, floored at dress_floor.
    Nothing is scaled up."""
    b = cfg["team"]["dress_budget"]
    budget = {"F": float(b["forwards"]), "D": float(b["defense"])}
    floor = float(b["dress_floor"])
    blocks: dict[tuple[str, str], list] = {}
    for k in opps:
        g = group_of_key[k]
        if g in budget:
            blocks.setdefault((team_of[k], g), []).append(k)
    for (team, g), keys in blocks.items():
        hist = [k for k in keys if opps[k].source == "history"]
        rest = [k for k in keys if opps[k].source != "history"]
        new = _shrink_to([opps[k].p_dress for k in hist], budget[g])
        for k, v in zip(hist, new):
            opps[k].p_dress = v
        residual = max(0.0, budget[g] - sum(new))
        new = _shrink_to([opps[k].p_dress for k in rest], residual) if residual > 0 else [0.0] * len(rest)
        for k, v in zip(rest, new):
            opps[k].p_dress = max(floor, v)
    return opps
