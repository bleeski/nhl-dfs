"""Role state (C7, plan section 11): tonight's lines, PP units, goalie states and participation.

`merge` is deterministic. Signals, in precedence order:
  1. DK status per person (draftables when reachable, else the salary file's Status column; a
     Showdown person's CPT and FLEX rows are one person). OUT wins over everything, including a
     Daily Faceoff line listing. QUESTIONABLE carries a play-probability haircut. UNKNOWN changes
     nothing and is reported.
  2. Daily Faceoff team pages: injury tags (out / ir / dtd / game-time decision), projected lines,
     PP units. Absence from a projected lineup never means OUT (that would invent an injury).
     A DF out / ir tag against a DK status that is not OUT is a conflict: a play-probability mixture
     and a warning until resolved. dtd and game-time decisions are QUESTIONABLE signals only.
  3. Goalies: only the goalie page's strength name exactly "Confirmed", on a report whose team pair
     and dateGmt match the slate game, gives CONFIRMED (start probability 1). Any other named starter
     moves the rotation toward him (EXPECTED); two fresh sources naming different unconfirmed starters
     give CONFLICTED with a mixture; DK OUT on a DF-confirmed goalie is OUT with a warning. A goalie
     the DK pool does not list for that team is reported and nobody is re-assigned his probability.
Age policy (config/roles.yaml): a team page older than max_line_age_h at `now_utc` is reported and not
used; inside the last hour before a team's start, lines older than low_confidence_age_h are flagged.
`isSwappable` is editability only and never appears here.

`apply_state` turns the state into a new ParamTable: goalie start probabilities from the state, dressing
from the projected lineups (listed first, then history, then no-history players, under the same
12 F / 6 D team budget), line and PP unit ids for the simulator, and role-expectation ice time for
no-history persons, once. It never edits the input table.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.ids import normalize_name, person_key
from nhl_dfs.contracts.statuses import GoalieState, Participation
from nhl_dfs.data.sources.dailyfaceoff import GoalieReport, TeamLines, team_codes
from nhl_dfs.models import opportunity as opp_mod
from nhl_dfs.models.rates import load_model_config

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLES_YAML = REPO_ROOT / "config" / "roles.yaml"
SEVERITY = {Participation.PLAYING: 0, Participation.UNKNOWN: 1, Participation.QUESTIONABLE: 2, Participation.OUT: 3}


def load_roles_config(path: Path = ROLES_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def df_group(position: str) -> str:
    """F, D or G from a Daily Faceoff position code: c, lw, rw; d, ld, rd; g, g1, g2. Injured-group entries
    (ir1, ir2) carry no real position: they default to F and are found across groups by name (_find_person)."""
    p = (position or "").lower()
    return "G" if p.startswith("g") else ("D" if p in ("d", "ld", "rd") else "F")


@dataclass
class PersonRole:
    person_key: str
    team: str  # DK code
    group: str
    participation: Participation = Participation.PLAYING
    p_play: float = 1.0
    conflict: bool = False
    dk_status: str = ""  # raw DK status text, "" when none was seen
    df_status: str | None = None  # Daily Faceoff tag: out, ir, dtd, gtd
    line: int | None = None  # EV line (forwards 1-4) or pair (defense 1-3), from a usable DF page
    pp_unit: int | None = None
    pk_unit: int | None = None
    df_listed: bool = False  # on a usable projected lineup
    notes: list[str] = field(default_factory=list)


@dataclass
class GoalieRole:
    team: str  # DK code
    state: GoalieState
    p_start: dict[str, float]  # person_key -> probability (sums to 1 over the pool's goalies unless all are out)
    confirmed: str | None = None  # person_key
    named: str | None = None  # person_key of the reported starter
    report: GoalieReport | None = None
    confirmed_at: datetime | None = None
    game_key: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class RoleState:
    now_utc: datetime
    persons: dict[str, PersonRole] = field(default_factory=dict)
    goalies: dict[str, GoalieRole] = field(default_factory=dict)
    team_pages: dict[str, dict] = field(default_factory=dict)  # DK team -> {updated_utc, age_h, usable, source, low_confidence}
    role_person: dict[str, str] = field(default_factory=dict)  # role_id -> person_key
    team_games: dict[str, str] = field(default_factory=dict)  # DK team -> game key (AWAY@HOME)
    warnings: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)  # informational lines (unmatched names, unverified codes, ...)
    goalie_path: str = "none"
    cfg: dict = field(default_factory=dict)

    @property  # opportunity.RoleState interface: no-history persons take the role's ice-time expectation
    def ev_line(self) -> dict[str, int]:
        return {k: p.line for k, p in self.persons.items() if p.line is not None and p.df_listed}

    @property
    def pp_unit_map(self) -> dict[str, int]:
        return {k: p.pp_unit for k, p in self.persons.items() if p.pp_unit is not None and p.df_listed}

    @property
    def pp_unit(self) -> dict[str, int]:  # the name opportunity.prior_opportunity reads
        return self.pp_unit_map

    def confirmed_at(self) -> dict[str, datetime]:
        """game key -> latest confirmation time: the input of sim.slate.build_slate(confirmed_at=...)."""
        out: dict[str, datetime] = {}
        for g in self.goalies.values():
            if g.state is GoalieState.CONFIRMED and g.confirmed_at and g.game_key:
                out[g.game_key] = max(out.get(g.game_key, g.confirmed_at), g.confirmed_at)
        return out

    def participation_probs(self) -> dict[str, float]:
        """person_key -> play probability (1 unless OUT, QUESTIONABLE or CONFLICTED)."""
        return {k: p.p_play for k, p in self.persons.items()}


# -- helpers ---------------------------------------------------------------------------------------------

def _dk_of_nhl(pool_teams) -> dict[str, str]:
    """NHL code -> DK code for teams whose DK code is verified and present in the pool."""
    return {t["nhl"]: t["dk"] for t in team_codes().values() if t["dk"] and t["dk"] in pool_teams}


def _team_game(pool):
    out = {}
    for key, g in pool.games.items():
        out[g.home] = (key, g)
        out[g.away] = (key, g)
    return out


def _dk_signal(pool, dk, csv_status) -> dict[str, tuple[Participation, str, list[str]]]:
    """person_key -> (participation, raw text, notes), most severe across the person's rows."""
    per: dict[str, list[tuple[Participation, str]]] = {}
    notes: dict[str, list[str]] = {}
    for r in pool.rows:
        got = []
        if dk is not None and r.role_id in dk.rows and dk.rows[r.role_id].status_raw is not None:
            rs = dk.rows[r.role_id]
            got.append((rs.participation, rs.status_raw))
        if csv_status and r.role_id in csv_status:
            part, raw = csv_status[r.role_id]
            got.append((part, raw))
        if len({g[0] for g in got}) > 1:
            notes.setdefault(r.person_key, []).append(f"draftables and salary file disagree on {r.name}: "
                                                      + " vs ".join(f"{g[1] or 'None'}" for g in got))
        per.setdefault(r.person_key, []).extend(got)
    out = {}
    for k, rows in per.items():
        if not rows:
            out[k] = (Participation.PLAYING, "", [])
            continue
        best = max(rows, key=lambda x: SEVERITY[x[0]])
        out[k] = (best[0], best[1], notes.get(k, []))
    return out


def _hours(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 3600.0


# -- merge -----------------------------------------------------------------------------------------------

def merge(dk, df_lines: dict[str, TeamLines] | None, df_goalies: list[GoalieReport] | None, rotation: dict, now_utc: datetime,
          cfg: dict | None = None, *, pool, csv_status: dict | None = None, goalie_path: str = "none") -> RoleState:
    """dk: a dk_public.Reconciliation or None. df_lines: NHL code -> TeamLines. df_goalies: goalie reports.
    rotation: DK team -> {person_key: start probability} (the ParamTable's goalie rotation).
    pool: the SalaryPool (Classic or Showdown; persons are the unit). csv_status: run.salary_statuses(pool),
    the DK signal while draftables is unreachable."""
    cfg = cfg or load_roles_config()
    rs = RoleState(now_utc=now_utc, cfg=cfg, goalie_path=goalie_path)
    df_lines = df_lines or {}
    df_goalies = df_goalies or []
    dk_signal = _dk_signal(pool, dk, csv_status)
    rs.role_person = {r.role_id: r.person_key for r in pool.rows}
    people = {}
    for r in pool.rows:
        people.setdefault(r.person_key, r)
    for k, r in people.items():
        part, raw, notes = dk_signal.get(k, (Participation.PLAYING, "", []))
        role = PersonRole(k, r.team, "G" if r.is_goalie else ("D" if r.position == "D" else "F"), part, 1.0, False, raw,
                          notes=list(notes))
        if part is Participation.OUT:
            role.p_play = 0.0
        elif part is Participation.QUESTIONABLE:
            role.p_play = float(cfg["p_play_questionable"])
        elif part is Participation.UNKNOWN:
            rs.warnings.append(f"UNKNOWN DK status {raw!r} for {r.name} ({r.team}): treated as no change, not as OUT")
        rs.persons[k] = role

    dk_of = _dk_of_nhl(pool.teams)
    game_of = _team_game(pool)
    rs.team_games = {t: g[0] for t, g in game_of.items()}
    unverified = sorted(t for t in pool.teams if t not in dk_of.values())
    if unverified:
        rs.reports.append("DK team code not verified in config/teams.yaml, so no Daily Faceoff data is used for: " + ", ".join(unverified))

    # Daily Faceoff team pages
    first_game_start = {t: game_of[t][1].start_utc for t in game_of}
    for nhl, tl in sorted(df_lines.items()):
        dk_team = dk_of.get(nhl)
        if dk_team is None:
            continue
        age = _hours(now_utc, tl.updated_utc)
        usable = age <= float(cfg["max_line_age_h"])
        start = first_game_start.get(dk_team)
        low = bool(usable and start is not None and timedelta(0) <= start - now_utc <= timedelta(minutes=cfg["low_confidence_min"])
                   and age > float(cfg["low_confidence_age_h"]))
        rs.team_pages[dk_team] = {"updated_utc": tl.updated_utc, "age_h": age, "usable": usable, "source": tl.source_name,
                                  "source_url": tl.source_url, "low_confidence": low}
        if not usable:
            rs.warnings.append(f"{dk_team}: Daily Faceoff page is {age:.1f} h old (limit {cfg['max_line_age_h']} h): lines and tags not used")
            continue
        if low:
            rs.warnings.append(f"{dk_team}: lines are {age:.1f} h old inside the hour before the game: low confidence, check this team first")
        _apply_team_page(rs, tl, dk_team, pool, cfg)

    _goalies(rs, pool, df_lines, df_goalies, rotation, dk_of, game_of, cfg)
    return rs


def _find_person(pool, name: str, dk_team: str, group: str) -> str | None:
    for g in ([group] + [x for x in ("F", "D", "G") if x != group]):
        k = f"{normalize_name(name)}|{dk_team}|{g}"
        if k in pool.persons:
            return k
    return None


def _apply_team_page(rs: RoleState, tl: TeamLines, dk_team: str, pool, cfg: dict) -> None:
    unmatched = []

    def person(p):
        k = _find_person(pool, p.name, dk_team, df_group(p.position))
        if k is None:
            unmatched.append(p.name)
        return rs.persons.get(k) if k else None

    listed = {}
    for i, grp in enumerate(tl.f_lines, start=1):
        for p in grp:
            r = person(p)
            if r is not None:
                r.line, r.df_listed = i, True
                listed[r.person_key] = True
    for i, grp in enumerate(tl.d_pairs, start=1):
        for p in grp:
            r = person(p)
            if r is not None:
                r.line, r.df_listed = i, True
    for unit, grp in ((1, tl.pp1), (2, tl.pp2)):
        for p in grp:
            r = person(p)
            if r is not None:
                r.pp_unit = unit
    for unit, grp in ((1, tl.pk1), (2, tl.pk2)):
        for p in grp:
            r = person(p)
            if r is not None:
                r.pk_unit = unit
    # injury tags (only where the tag exists; absence from a lineup is never an injury)
    for p in tl.players.values():
        tag = p.injury_status or ("gtd" if p.game_time_decision else None)
        if tag is None:
            continue
        r = rs.persons.get(_find_person(pool, p.name, dk_team, df_group(p.position)) or "")
        if r is None:
            continue
        r.df_status = tag
        if r.participation is Participation.OUT:
            continue
        if tag in ("out", "ir"):
            if r.participation is Participation.QUESTIONABLE:
                r.notes.append(f"DK QUESTIONABLE but Daily Faceoff says {tag}")
            r.conflict = True
            r.p_play = min(r.p_play, float(cfg["p_play_conflict"]))
            if r.participation is Participation.PLAYING:
                r.participation = Participation.QUESTIONABLE
            rs.warnings.append(f"CONFLICT {p.name} ({dk_team}): Daily Faceoff {tag}, DK status {r.dk_status or 'None'}: "
                               f"play probability {r.p_play:.2f} until resolved")
        elif tag in ("dtd", "gtd") and r.participation is Participation.PLAYING:
            r.participation = Participation.QUESTIONABLE
            r.p_play = min(r.p_play, float(cfg["p_play_questionable"]))
            r.notes.append(f"Daily Faceoff {tag}")
    # a person DK calls OUT keeps no role even if the page lists him
    for r in rs.persons.values():
        if r.team == dk_team and r.participation is Participation.OUT:
            if r.df_listed or r.pp_unit or r.pk_unit:
                rs.warnings.append(f"{r.person_key.split('|')[0]} ({dk_team}): DK OUT overrides a Daily Faceoff line listing")
            r.line = r.pp_unit = r.pk_unit = None
            r.df_listed = False
    if unmatched:
        rs.reports.append(f"{dk_team}: {len(set(unmatched))} Daily Faceoff name(s) not in the DK pool (scratches, IR, or spelling): "
                          + ", ".join(sorted(set(unmatched))[:6]) + (" ..." if len(set(unmatched)) > 6 else ""))


def _onehot(keys, key):
    return {k: (1.0 if k == key else 0.0) for k in keys}


def _norm(d: dict[str, float]) -> dict[str, float]:
    s = sum(d.values())
    return {k: v / s for k, v in d.items()} if s > 0 else d


def _goalies(rs: RoleState, pool, df_lines, reports, rotation, dk_of, game_of, cfg) -> None:
    tol = float(cfg["game_match_tolerance_s"])
    nhl_of_dk = {v: k for k, v in dk_of.items()}
    for team in sorted(pool.teams):
        goalies = {k: r for k, r in rs.persons.items() if r.team == team and r.group == "G"}
        if not goalies:
            continue
        rot = dict(rotation.get(team, {}))
        base = _norm({k: max(0.0, rot.get(k, 0.0)) for k in goalies if goalies[k].participation is not Participation.OUT})
        if not base:
            alive = [k for k in goalies if goalies[k].participation is not Participation.OUT]
            base = {k: 1.0 / len(alive) for k in alive} if alive else {}
        gr = GoalieRole(team, GoalieState.EXPECTED, {k: base.get(k, 0.0) for k in goalies})
        game = game_of.get(team)
        if game:
            gr.game_key = game[0]
        nhl = nhl_of_dk.get(team)
        name_to_key = {k.split("|")[0]: k for k in goalies}
        mine = [r for r in reports if r.team == nhl]
        matched = None
        for r in mine:
            if r.game_utc is None or game is None:
                continue
            opp_ok = r.opponent is None or r.opponent in {nhl_of_dk.get(game[1].home), nhl_of_dk.get(game[1].away)} - {nhl}
            if abs((r.game_utc - game[1].start_utc).total_seconds()) <= tol and opp_ok:
                matched = r
        if mine and matched is None and any(r.game_utc is not None for r in mine):
            rs.reports.append(f"{team}: goalie report(s) {', '.join(r.goalie_name for r in mine)} do not match this slate game's team pair and "
                              f"start time: ignored")
        if matched is not None:
            gr.report = matched
            key = name_to_key.get(normalize_name(matched.goalie_name))
            if not matched.strength_recognized:
                rs.reports.append(f"{team}: unrecognized goalie strength {matched.strength_raw!r} for {matched.goalie_name}: treated as unconfirmed")
            if key is None:
                rs.reports.append(f"{team}: {matched.goalie_name} ({matched.state.value}) is not a goalie the DK pool lists for {team}: "
                                  "his probability is not reassigned")
            elif goalies[key].participation is Participation.OUT:
                gr.state = GoalieState.CONFLICTED if matched.state is GoalieState.CONFIRMED else GoalieState.EXPECTED
                gr.notes.append(f"DK OUT overrides the Daily Faceoff {matched.state.value} report for {matched.goalie_name}")
                rs.warnings.append(f"{team}: DK OUT overrides the Daily Faceoff {matched.state.value} goalie report for {matched.goalie_name}")
            elif matched.state is GoalieState.CONFIRMED:
                gr.state, gr.confirmed, gr.named = GoalieState.CONFIRMED, key, key
                gr.p_start = _onehot(goalies, key)
                gr.confirmed_at = matched.news_created_utc
            else:
                w = float(cfg["expected_weight"]["next_data"])
                gr.named = key
                gr.p_start = _norm({k: (1 - w) * base.get(k, 0.0) + w * (1.0 if k == key else 0.0) for k in goalies})
        elif not mine or all(r.game_utc is None for r in mine):
            # team-page depth order only: EXPECTED at most, and only from a usable page
            tl = df_lines.get(nhl)
            page = rs.team_pages.get(team)
            if tl is not None and tl.goalies and page and page["usable"]:
                key = name_to_key.get(normalize_name(tl.goalies[0].name))
                if key is not None and goalies[key].participation is not Participation.OUT:
                    w = float(cfg["expected_weight"]["team_pages"])
                    gr.named = key
                    gr.p_start = _norm({k: (1 - w) * base.get(k, 0.0) + w * (1.0 if k == key else 0.0) for k in goalies})
                    gr.notes.append("team page depth order (first listed goalie)")
        # contradictions between two fresh sources that both name an unconfirmed starter
        tl = df_lines.get(nhl)
        page = rs.team_pages.get(team)
        if (gr.state is GoalieState.EXPECTED and matched is not None and gr.named and tl is not None and tl.goalies and page
                and page["usable"]):
            k2 = name_to_key.get(normalize_name(tl.goalies[0].name))
            if k2 and k2 != gr.named and goalies[k2].participation is not Participation.OUT:
                a, b, c = cfg["conflict_split"]
                gr.state = GoalieState.CONFLICTED
                gr.p_start = _norm({k: a * (k == gr.named) + b * (k == k2) + c * base.get(k, 0.0) for k in goalies})
                msg = (f"{team}: goalie sources disagree (goalie page {gr.named.split('|')[0]}, team page first goalie "
                       f"{k2.split('|')[0]}): CONFLICTED mixture until resolved")
                gr.notes.append(msg)
                rs.warnings.append(msg)
        rs.goalies[team] = gr
        for k, r in goalies.items():
            r.notes.append(f"start probability {gr.p_start.get(k, 0.0):.2f} ({gr.state.value})")


# -- from state to parameters ------------------------------------------------------------------------------

def rotation_from(params) -> dict[str, dict[str, float]]:
    """DK team -> {person_key: start probability} from a ParamTable's goalies (the rotation)."""
    out: dict[str, dict[str, float]] = {}
    for k, p in params.persons.items():
        if p.group == "G" and p.goalie is not None:
            out.setdefault(p.team, {})[k] = float(p.goalie.p_start)
    return out


def set_play_prob(pp, p1: float, model_cfg: dict) -> None:
    """Set a person's dress (skater) or start (goalie) probability and bring mean_tenths and sd_tenths
    with it. History persons recompute their analytic moments; PRIOR persons' mean is the per-game prior
    times the probability (PersonParams.prior_mean_tenths, recorded at build), so it is rebuilt from that, not
    from the rounded stored mean. After this a PRIOR person's mean is p x his per-game prior."""
    from nhl_dfs.contracts.statuses import ModelStatus
    from nhl_dfs.models import params as params_mod

    goalie = pp.group == "G"
    p0 = float(pp.goalie.p_start if goalie else pp.opportunity.p_dress)
    p1 = min(1.0, max(0.0, float(p1)))
    if goalie:
        pp.goalie.p_start = p1
    else:
        pp.opportunity.p_dress = p1
    if pp.source is not ModelStatus.PRIOR:
        mean, sd = params_mod.goalie_moments(pp.goalie, model_cfg) if goalie else params_mod.skater_moments(pp.opportunity, pp.rates)[:2]
    else:
        if pp.prior_mean_tenths > 0:  # the per-game prior recorded at build: exact, no rounding
            m, v = pp.prior_mean_tenths, pp.prior_sd_tenths ** 2
        elif p0 > 1e-9:  # a table built without it: rescale the stored mean
            m = pp.mean_tenths / p0
            v = max(0.0, (pp.sd_tenths ** 2 + pp.mean_tenths ** 2) / p0 - m * m)
        else:
            return  # no per-game base to work from
        mean = p1 * m
        sd = math.sqrt(max(p1 * (v + m * m) - mean * mean, 0.0))
    pp.mean_tenths, pp.sd_tenths = int(round(mean)), max(1, int(round(sd)))


def team_minutes(params, group: str | None = None) -> dict[tuple[str, str], dict[str, float]]:
    """(team, group) -> expected EV skater seconds (sum of p_dress x EV TOI) and PP seconds."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    for p in params.persons.values():
        if p.group == "G" or p.opportunity is None or (group and p.group != group):
            continue
        d = out.setdefault((p.team, p.group), {"ev": 0.0, "pp": 0.0})
        d["ev"] += p.opportunity.p_dress * p.opportunity.toi_ev_s
        d["pp"] += p.opportunity.p_dress * p.opportunity.toi_pp_s
    return out


def dress_budget_listed(p: dict[str, float], listed: set[str], history: set[str], budget: float, floor: float) -> dict[str, float]:
    """Cut probabilities to a team total of `budget`, protecting in this order: Daily Faceoff listed players,
    then other history players, then no-history players (who take the residual, floored)."""
    out = dict(p)
    groups = [[k for k in p if k in listed], [k for k in p if k not in listed and k in history],
              [k for k in p if k not in listed and k not in history]]
    left = budget
    for i, keys in enumerate(groups):
        if not keys:
            continue
        vals = opp_mod._shrink_to([p[k] for k in keys], max(0.0, left)) if left > 0 else [0.0] * len(keys)
        for k, v in zip(keys, vals):
            out[k] = max(floor, v) if i == 2 else v
        left = max(0.0, left - sum(out[k] for k in keys))
    return out


def refresh_moments(pp, model_cfg: dict) -> None:
    """Recompute mean and sd after an ice-time change. PRIOR persons' mean is a per-game prior times a
    probability and does not depend on ice time, so it stays."""
    from nhl_dfs.contracts.statuses import ModelStatus
    from nhl_dfs.models import params as params_mod

    if pp.group == "G" or pp.source is ModelStatus.PRIOR or pp.opportunity is None or pp.rates is None:
        return
    mean, sd = params_mod.skater_moments(pp.opportunity, pp.rates)[:2]
    pp.mean_tenths, pp.sd_tenths = int(round(mean)), max(1, int(round(sd)))


def reconcile_minutes(params, model_cfg: dict) -> int:
    """opportunity.reconcile on every team again (dressing moved, so expected EV skater time can exceed the
    manpower budget); persons whose EV time was scaled get their moments recomputed. Returns how many."""
    opps = {k: p.opportunity for k, p in params.persons.items() if p.group != "G" and p.opportunity is not None}
    before = {k: o.toi_ev_s for k, o in opps.items()}
    opp_mod.reconcile(opps, {k: params.persons[k].team for k in opps}, model_cfg)
    n = 0
    for k, o in opps.items():
        if abs(o.toi_ev_s - before[k]) > 1e-9:
            refresh_moments(params.persons[k], model_cfg)
            n += 1
    return n


def apply_state(params, roles: RoleState, cfg: dict | None = None, model_cfg: dict | None = None):
    """A new ParamTable with the role state applied. Goalie start probabilities come from the state;
    skater dressing from usable projected lineups; line and PP unit ids for the simulator; role ice time
    for no-history persons once (their Opportunity.source becomes "role", so a second apply skips them)."""
    cfg = cfg or roles.cfg or load_roles_config()
    model_cfg = model_cfg or load_model_config()
    out = copy.deepcopy(params)
    out.notes = list(out.notes)
    # goalies
    changed_g = 0
    for team, gr in roles.goalies.items():
        for k, pn in gr.p_start.items():
            pp = out.persons.get(k)
            if pp is None or pp.goalie is None or abs(pp.goalie.p_start - pn) < 1e-9:
                continue
            set_play_prob(pp, pn, model_cfg)
            changed_g += 1
    # skater dressing and units
    budget = model_cfg["team"]["dress_budget"]
    floor = float(budget["dress_floor"])
    n_moved = 0
    for team, page in roles.team_pages.items():
        if not page["usable"]:
            continue
        w = float(cfg["df_weight"]) * (1.0 - 0.5 * page["age_h"] / float(cfg["max_line_age_h"]))
        for group, need in (("F", float(budget["forwards"])), ("D", float(budget["defense"]))):
            keys = [k for k, p in out.persons.items() if p.team == team and p.group == group and p.opportunity is not None
                    and k in roles.persons and roles.persons[k].participation is not Participation.OUT]
            if not keys:
                continue
            listed = {k for k in keys if roles.persons[k].df_listed}
            if not listed:
                continue
            new = {}
            for k in keys:
                p0 = out.persons[k].opportunity.p_dress
                new[k] = w * float(cfg["df_listed_p"]) + (1 - w) * p0 if k in listed else (1 - w) * p0
            hist = {k for k in keys if out.persons[k].opportunity.source == "history"}
            new = dress_budget_listed(new, listed, hist, need, floor)
            for k, pn in new.items():
                if abs(out.persons[k].opportunity.p_dress - pn) > 1e-9:
                    set_play_prob(out.persons[k], pn, model_cfg)
                    n_moved += 1
    n_role = 0
    for k, r in roles.persons.items():
        pp = out.persons.get(k)
        if pp is None or pp.opportunity is None or not r.df_listed:
            continue
        o = pp.opportunity
        tag = "F" if pp.group == "F" else "D"
        o.unit_ev = f"{pp.team}-{tag}{r.line}"
        if r.pp_unit:
            o.unit_pp = f"{pp.team}-PP{r.pp_unit}"
        o.units_missing = 0
        if o.source == "prior":  # a call-up or a player with no history: the role's ice-time expectation, once
            role = opp_mod.prior_opportunity(pp.group, model_cfg, k, roles)
            if role.source == "role":
                o.toi_ev_s, o.toi_pp_s, o.toi_sh_s, o.pp_share, o.source = role.toi_ev_s, role.toi_pp_s, role.toi_sh_s, role.pp_share, "role"
                n_role += 1
    n_rec = reconcile_minutes(out, model_cfg)
    out.notes.append(f"roles applied: {n_rec} depth persons rescaled to the manpower budget; {changed_g} goalie start probabilities, {n_moved} dressing probabilities moved, "
                     f"{n_role} no-history persons given role ice time")
    return out
