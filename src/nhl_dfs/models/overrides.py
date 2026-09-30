"""Validated override schema for the LLM path (C7; the researcher agent that proposes them is C10).

Every proposal names the person and game, the field, the old and new value, an effective time, a source
URL and claim, a confidence, and an expiry (plan section 11). `validate` is the deterministic gate:
a whitelist of fields and values, `old` must equal the current state, capacity (3 forwards per line,
2 defensemen per pair, 5 per PP unit, one starter per team), effective <= now < expiry, a source URL.
DK status stays the authority for OUT: an override cannot lift it.

`apply` returns a NEW ParamTable. A line or PP change moves that person's ice time to the role's
expectation and rescales his teammates' ice time so the team's expected EV skater time per group
(forwards, defense) and expected PP time are conserved; an OUT hands his expected time to teammates the
same way, capped at certainty. `apply_to_roles` returns the matching RoleState. Nothing here calls a model.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from nhl_dfs.contracts.statuses import GoalieState, Participation
from nhl_dfs.models import roles as roles_mod
from nhl_dfs.models.rates import load_model_config

FIELDS = ("ev_line", "pp_unit", "participation", "goalie_start")
PARTICIPATION_VALUES = ("PLAYING", "QUESTIONABLE", "OUT")


@dataclass(frozen=True)
class Override:
    role_id: str
    nhl_id: int | None
    game_id: str | None  # the game key "AWAY@HOME" (or an NHL game id string the caller maps to it)
    field: str
    old: object
    new: object
    effective_utc: datetime
    source_url: str
    claim: str
    confidence: float
    expiry_utc: datetime


def _current(o: Override, role, roles):
    if o.field == "ev_line":
        return role.line
    if o.field == "pp_unit":
        return role.pp_unit or 0
    if o.field == "participation":
        return role.participation.value
    gr = roles.goalies.get(role.team)
    return bool(gr and gr.confirmed == role.person_key)


def validate(o: Override, roles, now: datetime | None = None) -> list[str]:
    """Reasons the override is rejected; [] means acceptable."""
    now = now or roles.now_utc
    cfg = roles.cfg or roles_mod.load_roles_config()
    cap = cfg["capacity"]
    errs: list[str] = []
    if o.field not in FIELDS:
        return [f"field {o.field!r} is not one of {FIELDS}"]
    pk = roles.role_person.get(o.role_id)
    if pk is None or pk not in roles.persons:
        return [f"unknown role_id {o.role_id!r}"]
    role = roles.persons[pk]
    if not (isinstance(o.source_url, str) and o.source_url.startswith(("http://", "https://"))):
        errs.append("source_url must be an http(s) URL")
    if not (isinstance(o.claim, str) and o.claim.strip()):
        errs.append("claim is empty")
    if not (isinstance(o.confidence, (int, float)) and 0.0 <= o.confidence <= 1.0):
        errs.append("confidence must be between 0 and 1")
    if not (o.effective_utc <= now):
        errs.append("effective_utc is in the future")
    if not (o.expiry_utc > o.effective_utc):
        errs.append("expiry_utc must be after effective_utc")
    elif not (now < o.expiry_utc):
        errs.append("expired")
    team_game = roles.team_games.get(role.team)
    if o.game_id is not None and team_game is not None and o.game_id != team_game:
        errs.append(f"game_id {o.game_id!r} is not {role.team}'s slate game {team_game!r}")
    cur = _current(o, role, roles)
    if o.old != cur:
        errs.append(f"old value {o.old!r} does not match the current {o.field} {cur!r}")
    if o.field == "ev_line":
        top = 4 if role.group == "F" else 3
        if role.group == "G":
            errs.append("a goalie has no EV line")
        elif not (isinstance(o.new, int) and 1 <= o.new <= top):
            errs.append(f"ev_line for a {'forward' if role.group == 'F' else 'defenseman'} must be 1 to {top}")
        else:
            limit = cap["f_line"] if role.group == "F" else cap["d_pair"]
            n = sum(1 for r in roles.persons.values() if r.team == role.team and r.group == role.group and r.line == o.new
                    and r.df_listed and r.person_key != pk)
            if n >= limit:
                errs.append(f"{role.team} {'line' if role.group == 'F' else 'pair'} {o.new} already has {n} of {limit}: the override overfills it")
    elif o.field == "pp_unit":
        if role.group == "G":
            errs.append("a goalie has no PP unit")
        elif o.new not in (0, 1, 2):
            errs.append("pp_unit must be 0 (none), 1 or 2")
        elif o.new:
            n = sum(1 for r in roles.persons.values() if r.team == role.team and r.pp_unit == o.new and r.df_listed and r.person_key != pk)
            if n >= cap["pp_unit"]:
                errs.append(f"{role.team} PP{o.new} already has {n} of {cap['pp_unit']}: the override overfills it")
    elif o.field == "participation":
        if o.new not in PARTICIPATION_VALUES:
            errs.append(f"participation must be one of {PARTICIPATION_VALUES}")
        elif role.participation is Participation.OUT and role.dk_status and o.new != "OUT":
            errs.append("DK status is OUT: an override cannot lift it")
    else:  # goalie_start
        if role.group != "G":
            errs.append("goalie_start applies to a goalie")
        elif o.new is not True:
            errs.append("goalie_start can only be set to true (a confirmation)")
        elif role.participation is Participation.OUT:
            errs.append("DK status is OUT: this goalie cannot be the starter")
        else:
            gr = roles.goalies.get(role.team)
            if gr and gr.confirmed and gr.confirmed != pk:
                errs.append(f"{role.team} already has a confirmed starter: one starter per team")
    return errs


# -- apply --------------------------------------------------------------------------------------------------

def _shift_minutes(out, team: str, groups: tuple[str, ...], key: str, attr: str, new: float, model_cfg: dict) -> str | None:
    """Set one person's `attr` (toi_ev_s or toi_pp_s) and rescale teammates in `groups` so the team's
    expected sum of p_dress x attr is unchanged. Returns a note when it could not be conserved."""
    mine = out.persons[key].opportunity
    others = [p for k, p in out.persons.items() if k != key and p.team == out.persons[key].team and p.group in groups
              and p.opportunity is not None]
    total_others = sum(p.opportunity.p_dress * getattr(p.opportunity, attr) for p in others)
    old_mine = mine.p_dress * getattr(mine, attr)
    target = total_others + old_mine - mine.p_dress * new
    setattr(mine, attr, float(new))
    roles_mod.refresh_moments(out.persons[key], model_cfg)
    if total_others <= 0 or target < 0:
        return f"{team} {attr}: could not conserve team minutes"
    f = target / total_others
    for p in others:
        setattr(p.opportunity, attr, getattr(p.opportunity, attr) * f)
        roles_mod.refresh_moments(p, model_cfg)
    return None


def apply_with_report(params, roles, overrides, now: datetime | None = None, model_cfg: dict | None = None):
    """(new ParamTable, accepted overrides, [(rejected override, reasons)])."""
    model_cfg = model_cfg or load_model_config()
    now = now or roles.now_utc
    out = copy.deepcopy(params)
    out.notes = list(out.notes)
    accepted, rejected = [], []
    role_cfg = model_cfg["roles"]
    pp_clock = float(model_cfg["team"]["pp_clock_s"])
    for o in overrides:
        errs = validate(o, roles, now)
        pk = roles.role_person.get(o.role_id)
        pp = out.persons.get(pk) if pk else None
        if pp is not None and o.nhl_id is not None and pp.nhl_id is not None and int(o.nhl_id) != int(pp.nhl_id):
            errs.append(f"nhl_id {o.nhl_id} does not match the person's {pp.nhl_id}")
        if not errs and pp is None:
            errs.append("person is not in the parameter table")
        if errs:
            rejected.append((o, errs))
            continue
        note = None
        if o.field == "ev_line":
            table = role_cfg[pp.group]["ev_line_toi_s"]
            note = _shift_minutes(out, pp.team, (pp.group,), pk, "toi_ev_s", float(table[min(o.new, len(table)) - 1]), model_cfg)
            pp.opportunity.unit_ev = f"{pp.team}-{pp.group}{o.new}"
            pp.opportunity.source = "role"
        elif o.field == "pp_unit":
            table = role_cfg[pp.group]["pp_unit_toi_s"]
            new = 0.0 if o.new == 0 else float(table[min(o.new, len(table)) - 1])
            note = _shift_minutes(out, pp.team, ("F", "D"), pk, "toi_pp_s", new, model_cfg)
            pp.opportunity.pp_share = min(1.0, new / pp_clock)
            pp.opportunity.unit_pp = f"{pp.team}-PP{o.new}" if o.new else ""
            pp.opportunity.source = "role"
        elif o.field == "participation" and o.new == "OUT" and pp.group != "G":
            note = _hand_over(out, pk, model_cfg)
        elif o.field == "goalie_start":
            for k, q in out.persons.items():
                if q.team == pp.team and q.group == "G" and q.goalie is not None:
                    roles_mod.set_play_prob(q, 1.0 if k == pk else 0.0, model_cfg)
        elif o.field == "participation" and o.new == "OUT" and pp.group == "G":
            roles_mod.set_play_prob(pp, 0.0, model_cfg)
        if note:
            out.notes.append(note)
        accepted.append(o)
    out.notes.append(f"overrides: {len(accepted)} applied, {len(rejected)} rejected")
    return out, accepted, rejected


def _hand_over(out, key: str, model_cfg: dict) -> str | None:
    """A skater ruled OUT: teammates in his group take his expected EV time by a common rise in dressing
    probability (capped at 1), so the team's expected EV skater time is conserved where it can be."""
    me = out.persons[key]
    vacated = me.opportunity.p_dress * me.opportunity.toi_ev_s
    roles_mod.set_play_prob(me, 0.0, model_cfg)
    others = [p for k, p in out.persons.items() if k != key and p.team == me.team and p.group == me.group and p.opportunity is not None]
    total = sum(p.opportunity.p_dress * p.opportunity.toi_ev_s for p in others)
    if total <= 0:
        return f"{me.team}: no teammates to take the vacated minutes"
    f = (total + vacated) / total
    unmet = 0.0
    for p in others:
        p1 = min(1.0, p.opportunity.p_dress * f)
        unmet += (p.opportunity.p_dress * f - p1) * p.opportunity.toi_ev_s
        roles_mod.set_play_prob(p, p1, model_cfg)
    return f"{me.team}: {unmet:.0f} s of vacated EV time could not be handed over (teammates at certainty)" if unmet > 1e-6 else None


def apply(params, roles, overrides, now: datetime | None = None):
    return apply_with_report(params, roles, overrides, now)[0]


def apply_to_roles(roles, overrides, now: datetime | None = None):
    """The RoleState after the accepted overrides (a copy)."""
    out = copy.deepcopy(roles)
    now = now or roles.now_utc
    for o in overrides:
        if validate(o, roles, now):
            continue
        r = out.persons[roles.role_person[o.role_id]]
        if o.field == "ev_line":
            r.line, r.df_listed = o.new, True
        elif o.field == "pp_unit":
            r.pp_unit = o.new or None
        elif o.field == "participation":
            r.participation = Participation(o.new)
            r.p_play = {"OUT": 0.0, "QUESTIONABLE": float(out.cfg["p_play_questionable"]), "PLAYING": 1.0}[o.new]
            r.conflict = False if o.new != "QUESTIONABLE" else r.conflict
        else:
            gr = out.goalies[r.team]
            gr.state, gr.confirmed, gr.named, gr.confirmed_at = GoalieState.CONFIRMED, r.person_key, r.person_key, o.effective_utc
            gr.p_start = {k: (1.0 if k == r.person_key else 0.0) for k in gr.p_start}
            gr.notes.append(f"override: {r.person_key.split('|')[0]} confirmed starter")  # B26 source label
    return out
