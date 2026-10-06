"""What the ownership prior is told about tonight (C17, backlog B63, B20).

models/ownership.feature_table has weights for the slate's game totals and win odds, PP1 and line 1 status, fresh
news and a confirmed goalie, but until C17 no run passed any of that in, so those weights always read zero. `collect`
turns the run's own inputs into the four mappings the feature table takes:

  odds         the partner or Covers snapshot, matched with market.match_odds (a DK code that is not verified
               against the source's code list never matches) and relabelled with the pool's DK team codes; a
               snapshot older than config/sim.yaml market.max_age_h is not used, the same gate as the simulator
  roles        role_id -> {"pp1": bool, "line": int} from the role state (C7), usable Daily Faceoff pages only
  news_age_h   role_id -> hours since the player's latest Daily Faceoff news item, or since a goalie confirmation
  goalie_start person_key -> start probability, only for a CONFIRMED team (starter 1, the team's other goalies 0);
               every other goalie keeps the expected-starter prior (config/ownership.yaml goalie_start_prior)

Nothing here fetches anything. Missing is missing: no snapshot, no role state or a switched-off config leaves the
mapping empty and the coverage line says so. `config/ownership.yaml field_inputs.enabled: false` restores the
pre-C17 behavior exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.sources.nhl import OddsSnapshot
from nhl_dfs.intake.salary import SalaryPool


def enabled(own_cfg: dict | None) -> bool:
    """config/ownership.yaml field_inputs.enabled (default true)."""
    return bool(((own_cfg or {}).get("field_inputs") or {}).get("enabled", True))


@dataclass(frozen=True)
class FieldInputs:
    odds: OddsSnapshot | None = None
    roles: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    news_age_h: Mapping[str, float] = field(default_factory=dict)
    goalie_start: Mapping[str, float] = field(default_factory=dict)
    imputed: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def feature_kwargs(self) -> dict:
        """The keyword arguments of ownership.feature_table that these inputs fill."""
        return {"odds": self.odds, "roles": dict(self.roles), "news_age_h": dict(self.news_age_h),
                "goalie_start": dict(self.goalie_start), "imputed": dict(self.imputed)}

    def record(self) -> dict:
        """JSON-ready coverage for the manifest (`provisional.field_inputs`)."""
        return {**self.coverage, "line": self.coverage_line()}

    def coverage_line(self) -> str:
        """The one printed line: FIELD_INPUTS MARKET=3/3 ROLES=6/6 (details)."""
        c = self.coverage
        if not c.get("enabled", True):
            return "FIELD_INPUTS=OFF (config/ownership.yaml field_inputs.enabled is false: the field sees no odds, lines or news)"
        o, r, n, g = c.get("odds") or {}, c.get("roles") or {}, c.get("news") or {}, c.get("goalies") or {}
        odds_txt = (f"{o.get('source')} as of {o.get('as_of')}" if o.get("games") else o.get("why", "no odds snapshot"))
        return (f"FIELD_INPUTS MARKET={o.get('games', 0)}/{o.get('of', 0)} ROLES={r.get('teams_usable', 0)}/{r.get('of', 0)} "
                f"(odds: {odds_txt}; {r.get('pp1', 0)} players on PP1 and {r.get('line1', 0)} on line 1 from usable Daily Faceoff pages; "
                f"{n.get('recent', 0)} of {n.get('players', 0)} players with news are within {n.get('hours', 0):g} h; "
                f"{g.get('confirmed', 0)} of {g.get('of', 0)} goalie teams confirmed"
                + (f"; {r['imputed_teams']} team(s) without a usable page read as the covered average" if r.get("imputed_teams") else "")
                + ")")


def collect(pool: SalaryPool, *, snapshot: OddsSnapshot | None = None, role_state=None, now: datetime | None = None,
            own_cfg: dict | None = None, sim_cfg: dict | None = None) -> FieldInputs:
    """FieldInputs for a (work) pool from the run's odds snapshot and role state. now: the clock the news ages are read at
    (default: the role state's own time)."""
    if not enabled(own_cfg):
        return FieldInputs(coverage={"enabled": False})
    from nhl_dfs.sim import market

    sim_cfg = sim_cfg or market.load_sim_config()
    ref = now or (role_state.now_utc if role_state is not None else None)
    odds, odds_cov = _odds(pool, snapshot, ref, sim_cfg)
    roles, news, goalie_start, role_cov, news_cov, goalie_cov = _roles(pool, role_state, ref, own_cfg or {})
    imputed = _impute(pool, role_state, roles, news, role_cov, own_cfg or {})
    return FieldInputs(odds, roles, news, goalie_start, imputed,
                       {"enabled": True, "odds": odds_cov, "roles": role_cov, "news": news_cov, "goalies": goalie_cov})


def _odds(pool: SalaryPool, snapshot: OddsSnapshot | None, now: datetime | None, sim_cfg: dict) -> tuple[OddsSnapshot | None, dict]:
    from nhl_dfs.sim import market

    pairs = {k: (g.home, g.away) for k, g in pool.games.items()}
    cov: dict = {"games": 0, "of": len(pairs), "source": None, "as_of": None, "unmatched": {}}
    if snapshot is None:
        cov["why"] = "no odds snapshot (offline, or no source answered)"
        return None, cov
    cov["source"], cov["as_of"] = snapshot.source, f"{snapshot.as_of_utc:%Y-%m-%d %H:%MZ}"
    limit = float(sim_cfg["market"]["max_age_h"])
    if now is not None and (now - snapshot.as_of_utc).total_seconds() / 3600.0 > limit:
        cov["why"] = f"snapshot older than {limit:g} h (as of {cov['as_of']}): not used"
        return None, cov
    matched, why = market.match_odds(snapshot, pairs)
    usable = {k: g for k, g in matched.items() if g.total_line is not None or (g.home_ml is not None and g.away_ml is not None)}
    cov["unmatched"] = {k: why.get(k, "no moneyline or total") for k in pairs if k not in usable}
    cov["games"] = len(usable)
    if not usable:
        cov["why"] = "no slate game matched the snapshot: " + "; ".join(f"{k}: {v}" for k, v in sorted(cov["unmatched"].items())[:4])
        return None, cov
    games = [replace(g, home_abbrev=pairs[k][0], away_abbrev=pairs[k][1]) for k, g in sorted(usable.items())]
    return OddsSnapshot(snapshot.as_of_utc, snapshot.as_of_basis, snapshot.book, snapshot.source, games), cov


def _impute(pool: SalaryPool, rs, roles: dict, news: dict, role_cov: dict, own_cfg: dict) -> dict[str, dict[str, float]]:
    """Unknown is the average player, not a non-member. Daily Faceoff pages the age policy dropped (or that never arrived)
    leave their teams' skaters with no PP1, line 1 or news data; scoring them as "not on PP1" would hand the covered teams
    an advantage that is only our data gap. Each such skater gets the share of covered skaters of his position group (F or D)
    on PP1, on line 1 and with news inside the window. With no covered team nothing is imputed (every skater reads the
    same zero, so nothing is shifted)."""
    covered = {t for t in pool.teams if rs is not None and (rs.team_pages.get(t) or {}).get("usable")}
    if not covered or len(covered) == len(pool.teams):
        role_cov["imputed_teams"] = 0
        return {}
    recent_h = float(own_cfg.get("news_recent_hours", 3))
    share: dict[str, dict[str, list[float]]] = {}
    for r in pool.rows:
        if r.is_goalie or r.team not in covered:
            continue
        s = share.setdefault("D" if r.position == "D" else "F", {"pp1": [], "line1": [], "news_recent": []})
        role = roles.get(r.role_id) or {}
        age = news.get(r.role_id)
        s["pp1"].append(1.0 if role.get("pp1") else 0.0)
        s["line1"].append(1.0 if role.get("line") == 1 else 0.0)
        s["news_recent"].append(1.0 if age is not None and age <= recent_h else 0.0)
    out: dict[str, dict[str, float]] = {}
    for r in pool.rows:
        if r.is_goalie or r.team in covered:
            continue
        s = share.get("D" if r.position == "D" else "F")
        if s:
            out[r.role_id] = {k: sum(v) / len(v) for k, v in s.items()}
    role_cov["imputed_teams"] = len(pool.teams) - len(covered)
    return out


def _roles(pool: SalaryPool, rs, now: datetime | None, own_cfg: dict):
    roles: dict[str, dict] = {}
    news: dict[str, float] = {}
    goalie_start: dict[str, float] = {}
    recent_h = float(own_cfg.get("news_recent_hours", 3))
    role_cov = {"teams_usable": 0, "of": len(pool.teams), "pp1": 0, "line1": 0}
    news_cov = {"players": 0, "recent": 0, "hours": recent_h}
    goalie_cov = {"confirmed": 0, "of": 0}
    if rs is None:
        return roles, news, goalie_start, role_cov, news_cov, goalie_cov
    role_cov["teams_usable"] = sum(1 for t in pool.teams if (rs.team_pages.get(t) or {}).get("usable"))
    confirmed_at: dict[str, datetime] = {}
    for gr in rs.goalies.values():
        goalie_cov["of"] += 1
        if gr.state is GoalieState.CONFIRMED and gr.confirmed:
            goalie_cov["confirmed"] += 1
            for pk in gr.p_start:
                goalie_start[pk] = 1.0 if pk == gr.confirmed else 0.0
            if gr.confirmed_at is not None:
                confirmed_at[gr.confirmed] = gr.confirmed_at
    seen_news: set[str] = set()
    for r in pool.rows:
        p = rs.persons.get(r.person_key)
        if p is None:
            continue
        if p.line is not None or p.pp_unit is not None:
            roles[r.role_id] = {"pp1": p.pp_unit == 1, "line": p.line}
        when = [t for t in (p.news_utc, confirmed_at.get(r.person_key)) if t is not None]
        if when and now is not None:
            news[r.role_id] = max(0.0, (now - max(when)).total_seconds() / 3600.0)
        if r.person_key not in seen_news:
            seen_news.add(r.person_key)
            if p.pp_unit == 1:
                role_cov["pp1"] += 1
            if p.line == 1:
                role_cov["line1"] += 1
            if r.role_id in news:
                news_cov["players"] += 1
                news_cov["recent"] += int(news[r.role_id] <= recent_h)
    return roles, news, goalie_start, role_cov, news_cov, goalie_cov
