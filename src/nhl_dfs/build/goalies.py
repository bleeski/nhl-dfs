"""Goalie gate and goalie table (backlog B17, B23; the fix after C10 on Ben's request).

An open-cell goalie whose team has another CONFIRMED starter (the Daily Faceoff starting-goalies page read through
the C7 role merge, or an accepted override) is a repair target in late swap and refresh: he joins the persons
excluded from free cells, so the fast repair's keep-bonus MILP replaces him with as few changed cells as possible
(a two-player repair when salary forces it). A pinned goalie never changes; he is reported loudly and the file is
DEGRADED_REVIEW. Lock semantics are C2c's, untouched.

Per-goalie status (the table's column):
  CONFIRMED     this goalie is his team's confirmed starter
  NOT STARTING  another goalie of his team is confirmed or is DraftKings' starter (salary file Starting=P, the only P
                of the team), or he is OUT (DK status or an accepted override)
  CONFLICTED    the sources disagree (Daily Faceoff CONFLICTED, or an accepted override confirms a different goalie
                than Daily Faceoff does): never repaired automatically, flagged for Ben
  EXPECTED      he is the named, unconfirmed starter
  NOT EXPECTED  another goalie is named but not confirmed (not a repair target; the team starter column says who)
  UNKNOWN       no report for this game, or goalie news unavailable (missing is missing, never EXPECTED)
Only NOT STARTING changes a lineup.

Cheap by design (the fast late swap stays inside its budget, and a late swap with nothing to repair still returns
the input bytes): the starting-goalies page only (one request, bounded by late_swap.goalie_gate_budget_s, cache
first with the 10-minute Daily Faceoff TTL); on failure the stored copy, labeled with its age; team pages from
stored copies only; roles.merge without a ParamTable and never apply_state (B11). A report created after `now`
is ignored, so a rehearsal clock cannot see later news.

C54 (B121, flags 85 to 92): RotoWire's news page is read beside Daily Faceoff, in parallel and under the same budget.
Its confirmation acts only when no other source names a different goalie (the same goalie: CONFIRMED with both named,
RotoWire first); otherwise the team is CONFLICTED and nothing is repaired for it. Its scratches (`Board.scratches`)
enter late swap's exclusion path as a DK OUT status does; a scratch of the goalie another source names as the starter
makes his team CONFLICTED instead. `Board.available` still means the Daily Faceoff page was read (NO_NEWS otherwise);
RotoWire's state is `Board.rotowire`, printed on its own line. Goalie probabilities never change (flag 92).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from nhl_dfs.contracts.statuses import GoalieState, Participation

CONFIRMED, NOT_STARTING, CONFLICTED, EXPECTED, NOT_EXPECTED, UNKNOWN = (
    "CONFIRMED", "NOT STARTING", "CONFLICTED", "EXPECTED", "NOT EXPECTED", "UNKNOWN")


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if t else None


@dataclass
class GoalieLine:
    person_key: str
    name: str
    team: str
    status: str
    starter: str | None = None  # the team's confirmed or named starter (name)
    starter_state: str = "none"  # CONFIRMED | EXPECTED | CONFLICTED | none
    source: str = ""
    report_utc: str | None = None  # the report's news time (Daily Faceoff) or the override's effective time
    repair: bool = False  # NOT STARTING because another goalie is confirmed (the gate's repair target)


@dataclass
class Board:
    now_utc: datetime
    available: bool  # the starting-goalies page (live or stored) was read
    page: dict[str, Any]  # {"source": live|stored|none, "fetched_utc", "age_min"}
    persons: dict[str, GoalieLine] = field(default_factory=dict)
    conflicted_teams: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    inputs: tuple | None = None  # (reports, path, problems) for swap_objective.build_role_model(goalie_inputs=)
    scratches: dict[str, str] = field(default_factory=dict)  # C54: person_key -> "RotoWire scratch: ..." (late swap excludes)
    rotowire: dict[str, Any] = field(default_factory=dict)  # C54: source, fetched_utc, age_min, counts, notes, line; {} = not read

    def not_starting(self) -> dict[str, str]:
        """person_key -> reason, for goalies whose team has another CONFIRMED starter (the repair targets)."""
        return {k: g.source for k, g in self.persons.items() if g.repair}


# -- inputs --------------------------------------------------------------------------------------------------

def _stored_view(http):
    from nhl_dfs.data.http import HttpCache

    if getattr(http, "offline", True):
        return http
    return HttpCache(root=http.root, config=http.config, offline=True, clock=http.clock)


def gather(pool, cache, *, offline: bool, budget_s: float, now: datetime) -> tuple[dict, list, str, list[str], dict]:
    """(stored team pages by NHL code, goalie reports, path, problems, page info). Never raises."""
    from zoneinfo import ZoneInfo

    from nhl_dfs.build.swap_objective import _bounded
    from nhl_dfs.data.http import HttpCache
    from nhl_dfs.data.sources import dailyfaceoff as df

    et = ZoneInfo("America/New_York")
    days = sorted({g.start_utc.astimezone(et).date() for g in pool.games.values()})
    codes = df.team_codes()
    slate = {c["nhl"]: slug for slug, c in codes.items() if c["dk"] and c["dk"] in pool.teams}
    http = cache if cache is not None else HttpCache(offline=offline)
    problems: list[str] = []

    def goalie_page(c):
        reps, path, fetched, notes = [], "none", None, []
        for d in days:
            got = df.fetch_goalies(d, cache=c, teams=sorted(slate), team_page_fallback=False)
            reps += got.reports
            path = got.path if path == "none" else path
            fetched = got.fetched_utc or fetched
            notes += [f"goalies {d}: {n}" for n in got.notes]
        return reps, path, fetched, notes

    source = "live" if not getattr(http, "offline", True) else "stored"
    try:
        reps, path, fetched, notes = _bounded(lambda: goalie_page(http), budget_s, "nhl-goalie-gate")
    except Exception as exc:  # timeout or network error: the stored copy, labeled
        problems.append(f"starting-goalies page not fetched ({type(exc).__name__}: {str(exc)[:80]}); stored copy used")
        source = "stored"
        try:
            reps, path, fetched, notes = goalie_page(_stored_view(http))
        except Exception as exc2:
            reps, path, fetched, notes = [], "none", None, [f"stored goalie page unusable ({type(exc2).__name__})"]
    problems += notes
    if path == "none":
        source = "none"
    lines = {}
    stored = _stored_view(http)
    for nhl, slug in sorted(slate.items()):
        try:
            lines[nhl] = df.team_lines(slug, cache=stored)
        except Exception:  # a missing team page only removes the depth-order fallback and the conflict check
            continue
    age = round((now - fetched).total_seconds() / 60.0, 1) if fetched else None
    page = {"source": source, "fetched_utc": _iso(fetched), "age_min": age}
    if fetched and fetched > now:
        problems.append("the stored goalie page was fetched after the clock (rehearsal): only reports created before it are used")
    return lines, reps, path, problems, page


def gather_rotowire(pool, cache, *, offline: bool, budget_s: float, now: datetime):
    """C54: the RotoWire rows for this slate (data/sources/rotowire.py), bounded by the goalie-gate budget; on a
    timeout the stored copy, labeled. Never raises: an unreadable page comes back with source 'none' and the reason.
    tests/conftest.py replaces this with a stub returning None (RotoWire not read) for tests not marked c54."""
    from nhl_dfs.build.swap_objective import _bounded
    from nhl_dfs.data.http import HttpCache
    from nhl_dfs.data.sources import rotowire as rw

    http = cache if cache is not None else HttpCache(offline=offline)
    try:
        games = rw.slate_games(pool)
    except Exception as exc:
        return rw.NewsFetch(notes=[f"slate games unreadable ({type(exc).__name__})"])
    try:
        return _bounded(lambda: rw.fetch_news(games, cache=http, now=now), budget_s, "nhl-rotowire")
    except Exception as exc:  # timeout: the stored copy, labeled
        try:
            got = rw.fetch_news(games, cache=_stored_view(http), now=now)
        except Exception as exc2:
            got = rw.NewsFetch(notes=[f"stored RotoWire page unusable ({type(exc2).__name__})"])
        got.notes.insert(0, f"RotoWire page not fetched ({type(exc).__name__}: {str(exc)[:80]}); stored copy used")
        return got


def _match_rotowire(fetch, pool) -> tuple[dict, dict, dict, list[str], list[str]]:
    """(starts: DK team -> [(person_key, report)] in published order, expected: DK team -> (person_key, report),
    scratches: person_key -> Scratch with later cancels applied, unmatched lines, notes). By normalized name plus team
    (flag 88): a start must match exactly one goalie; anything else is reported, never guessed."""
    from nhl_dfs.contracts.ids import normalize_name
    from nhl_dfs.contracts.statuses import GoalieState as GS
    from nhl_dfs.data.sources import dailyfaceoff as df

    dk_of = {c["nhl"]: c["dk"] for c in df.team_codes().values() if c.get("dk")}
    by: dict[tuple[str, str], set[str]] = {}
    is_g: dict[str, bool] = {}
    for r in pool.rows:
        by.setdefault((normalize_name(r.name), r.team), set()).add(r.person_key)
        is_g[r.person_key] = r.is_goalie
    starts: dict[str, list] = {}
    expected: dict[str, tuple] = {}
    scratches: dict[str, Any] = {}
    unmatched: list[str] = []
    notes: list[str] = []

    def one(player: str, nhl: str | None) -> tuple[str | None, str]:
        ks = by.get((normalize_name(player), dk_of.get(nhl or "", "")), set())
        if len(ks) == 1:
            return next(iter(ks)), ""
        return None, f"{player} ({nhl}): " + ("not in the pool" if not ks else "matches two persons")

    for rep in fetch.reports:
        k, why = one(rep.goalie_name, rep.team)
        if k is not None and not is_g.get(k):
            k, why = None, f"{rep.goalie_name} ({rep.team}): not a goalie in the pool"
        if k is None:
            unmatched.append(why)
            continue
        team = dk_of[rep.team]
        if rep.state is GS.CONFIRMED:
            starts.setdefault(team, []).append((k, rep))
        else:
            expected[team] = (k, rep)
    for sc in fetch.scratches:
        k, why = one(sc.player, sc.team)
        if k is None:
            unmatched.append(why)
        elif sc.cancel:
            if scratches.pop(k, None) is not None:
                notes.append(f"{sc.player}: a later RotoWire item says he will play; the earlier scratch is withdrawn")
        else:
            scratches[k] = sc
    return starts, expected, scratches, unmatched, notes


_READ_TIME = ", item dated only, time is when the page was read"  # flag 94 (c)


def _rw_label(rep, read_utc: datetime | None = None) -> str:
    from nhl_dfs.data.sources.rotowire import AGGREGATOR

    who = "aggregator" if rep.source_name in (None, AGGREGATOR) else f"citing {rep.source_name}"
    if read_utc is not None and rep.news_created_utc == read_utc:
        who += _READ_TIME
    return f"{who}: {rep.source_url}" if rep.source_url else who


def _rw_later(fetch, a, b) -> bool:
    """Flag 94 (d): row a is later than row b by time, then by position among the fetch's rows (oldest first)."""
    pos = {id(r): i for i, r in enumerate(fetch.reports)}
    return (a.news_created_utc, pos.get(id(a), -1)) > (b.news_created_utc, pos.get(id(b), -1))


def _rw_line(fetch, counts_extra: dict, now: datetime) -> str:
    if fetch.source == "none":
        return "RotoWire unavailable (" + ("; ".join(fetch.notes[:1]) or "not read") + "): board from Daily Faceoff alone"
    c = fetch.counts
    age = round((now - fetch.fetched_utc).total_seconds() / 60.0) if fetch.fetched_utc else None
    ignored = sum(v for k, v in c.items() if k.startswith("ignored"))
    return (f"RotoWire news: {fetch.source}" + (f", {age} min old" if age is not None else "") +
            f"; today {c.get('today', 0)} item(s) on slate teams: {c.get('start', 0)} start(s), {c.get('expected', 0)} expected, "
            f"{c.get('scratch', 0)} scratch(es), {c.get('cancel', 0)} cancel(s), {ignored} ignored, "
            f"{counts_extra.get('unmatched', 0)} unmatched" +
            ("; items carry dates only, so RotoWire times are when the page was read" if c.get("dated_only") else "") +
            (f"; {c['unknown_team_code']} item(s) with a team code not in config/teams.yaml" if c.get("unknown_team_code") else ""))


# -- the board -------------------------------------------------------------------------------------------------

def build(pool, *, dk_rec=None, csv_status=None, now: datetime, cache=None, offline: bool, budget_s: float = 8.0,
          overrides: list | None = None, inputs: tuple | None = None, rotowire=None) -> Board:
    """The goalie board for this slate at `now`. inputs: (lines, reports, path, problems, page) from gather()
    (tests and callers that already hold them); otherwise gathered here. rotowire: a rotowire.NewsFetch (tests);
    otherwise read here, in parallel with Daily Faceoff (C54). Never raises."""
    from nhl_dfs.build.run import dk_goalie_starters, salary_statuses
    from nhl_dfs.models import roles as roles_mod

    rw_box: dict[str, Any] = {}
    rw_thread = None
    if rotowire is None:
        def _read_rotowire():
            try:
                rw_box["v"] = gather_rotowire(pool, cache, offline=offline, budget_s=budget_s, now=now)
            except Exception as exc:  # gather_rotowire never raises; this keeps the board alive if it ever does
                rw_box["e"] = exc

        rw_thread = threading.Thread(target=_read_rotowire, name="nhl-rotowire-gather", daemon=True)
        rw_thread.start()
    try:
        lines, reps, path, problems, page = inputs if inputs is not None else gather(
            pool, cache, offline=offline, budget_s=budget_s, now=now)
    except Exception as exc:
        lines, reps, path, problems, page = {}, [], "none", [f"goalie gate inputs failed ({type(exc).__name__})"], \
            {"source": "none", "fetched_utc": None, "age_min": None}
    later = [r for r in reps if r.news_created_utc is not None and r.news_created_utc > now]
    if later:  # a rehearsal clock (or a stored page from after it) cannot see later news
        problems = list(problems) + [f"{len(later)} goalie report(s) created after {_iso(now)} ignored (the clock cannot "
                                     "see later news)"]
        reps = [r for r in reps if r not in later]
    board = Board(now, path == "next_data", page, problems=list(problems), inputs=(reps, path, list(problems)))
    if rw_thread is not None:  # the same budget as the Daily Faceoff read, which ran meanwhile; a stored read may follow
        rw_thread.join(timeout=budget_s + 2.0)
        if rw_thread.is_alive() or "e" in rw_box:
            from nhl_dfs.data.sources.rotowire import NewsFetch

            why = "read did not finish within the goalie-gate budget" if rw_thread.is_alive() else \
                f"read failed ({type(rw_box['e']).__name__})"
            rotowire = NewsFetch(notes=[f"RotoWire {why}"])
        else:
            rotowire = rw_box.get("v")  # None: RotoWire not read (the test stub)
    rw_starts, rw_exp, rw_scr, rw_unmatched, rw_notes = {}, {}, {}, [], []
    if rotowire is not None:
        try:
            rw_starts, rw_exp, rw_scr, rw_unmatched, rw_notes = _match_rotowire(rotowire, pool)
        except Exception as exc:  # the board never raises: RotoWire's rows are dropped and said so
            rotowire.notes.insert(0, f"RotoWire rows not matched to the pool ({type(exc).__name__}: {str(exc)[:80]})")
    try:
        rs = roles_mod.merge(dk_rec, lines, reps, {}, now, pool=pool,
                             csv_status=csv_status if csv_status is not None else salary_statuses(pool), goalie_path=path)
    except Exception as exc:
        board.problems.append(f"role merge failed ({type(exc).__name__}: {str(exc)[:80]})")
        board.available = False
        rs = None
    name = {}
    for r in pool.rows:
        name.setdefault(r.person_key, r.name)
    goalies = {}
    for r in pool.rows:
        if r.is_goalie:
            goalies.setdefault(r.team, set()).add(r.person_key)
    try:  # Ben, 2026-09-30: DraftKings' own starting flag (salary file Starting=P) names the team's starter
        dk_p = dk_goalie_starters(pool, csv_status)
    except Exception as exc:
        dk_p = {}
        board.problems.append(f"salary file Starting column unreadable ({type(exc).__name__})")

    # accepted overrides still valid at now: goalie_start confirmations and participation OUT
    ovr_start: dict[str, tuple[str, Any]] = {}
    ovr_out: dict[str, Any] = {}
    for o in overrides or []:
        row = pool.by_role_id.get(o.role_id)
        if row is None or not row.is_goalie:
            continue
        if o.field == "goalie_start" and o.new is True:
            ovr_start[row.team] = (row.person_key, o)
        elif o.field == "participation" and o.new == "OUT":
            ovr_out[row.person_key] = o

    when = page.get("fetched_utc")
    page_label = {"live": "Daily Faceoff starting goalies", "stored": "Daily Faceoff starting goalies (stored copy)",
                  "none": "no goalie page"}[page.get("source", "none")]
    if page.get("age_min") is not None:
        page_label += f", page {page['age_min']:.0f} min old"
    for team, keys in sorted(goalies.items()):
        gr = rs.goalies.get(team) if rs is not None else None
        df_conf = gr.confirmed if gr is not None and gr.state is GoalieState.CONFIRMED else None
        rep_utc = _iso(gr.report.news_created_utc) if gr is not None and gr.report is not None else None
        starter, s_state, s_source, s_time = None, "none", "", None
        conflict = ""
        if team in ovr_start:
            k, o = ovr_start[team]
            if df_conf and df_conf != k:
                conflict = (f"accepted override confirms {name[k]}, Daily Faceoff confirms {name[df_conf]}: CONFLICTED, "
                            "no automatic repair")
            else:
                starter, s_state, s_time = k, "CONFIRMED", _iso(o.effective_utc)
                s_source = f"accepted override ({_override_source(o)})"
        if not conflict and starter is None and gr is not None:
            if gr.state is GoalieState.CONFLICTED:
                conflict = "; ".join(gr.notes[-1:]) or "Daily Faceoff sources disagree"
            elif df_conf:
                starter, s_state, s_source, s_time = df_conf, "CONFIRMED", page_label, rep_utc
            elif team in dk_p:
                starter, s_state, s_source = dk_p[team], "DK", "DraftKings salary file Starting=P"
            elif gr.named and board.available:
                via = "team page depth order" if gr.report is None else page_label
                starter, s_state, s_source, s_time = gr.named, "EXPECTED", via, rep_utc
        if not conflict and starter is None and team in dk_p:  # no role state at all: DraftKings' flag alone
            starter, s_state, s_source = dk_p[team], "DK", "DraftKings salary file Starting=P"
        if not conflict and s_state == "CONFIRMED" and team in dk_p and dk_p[team] != starter:
            board.problems.append(f"{team}: {name[starter]} confirmed ({s_source}) but DraftKings marks {name[dk_p[team]]} "
                                  "Starting=P; the confirmation is used")
        if rw_starts.get(team):  # C54 (flag 89): RotoWire acts only when no other source names a different goalie
            k_rw, rep = rw_starts[team][-1]
            earlier = sorted({name[k] for k, _ in rw_starts[team][:-1] if k != k_rw})
            if earlier:
                rw_notes.append(f"{team}: an earlier RotoWire item named {', '.join(earlier)}; the later one ({name[k_rw]}) is used")
            rw_src, rw_time = f"RotoWire ({_rw_label(rep, rotowire.fetched_utc)})", _iso(rep.news_created_utc)
            others = []
            if team in ovr_start:
                others.append((f"accepted override ({_override_source(ovr_start[team][1])})", ovr_start[team][0]))
            if df_conf:
                others.append((f"{page_label}, {rep_utc or 'no time'}", df_conf))
            elif gr is not None and gr.named and board.available and gr.state is not GoalieState.CONFLICTED:
                others.append(("Daily Faceoff expected" + ("" if gr.report is not None else " (team page depth order)"), gr.named))
            confirmed_agree = df_conf == k_rw or (team in ovr_start and ovr_start[team][0] == k_rw)
            if team in dk_p and not confirmed_agree:  # a confirmation already beats Starting=P (the line above says so)
                others.append(("DraftKings salary file Starting=P", dk_p[team]))
            if team in rw_exp and rw_exp[team][0] != k_rw and _rw_later(rotowire, rw_exp[team][1], rep):
                others.append((f"a later RotoWire item (expected, {_iso(rw_exp[team][1].news_created_utc)})", rw_exp[team][0]))
            diff = [(lab, k) for lab, k in others if k != k_rw]
            if conflict:
                conflict += f"; RotoWire confirms {name[k_rw]} ({rw_time})"
            elif diff:
                conflict = (f"RotoWire confirms {name[k_rw]} ({rw_time}) but " +
                            "; ".join(f"{lab} names {name[k]}" for lab, k in diff) + ": CONFLICTED, no automatic repair")
            else:
                agree = [lab for lab, k in others if k == k_rw]
                starter, s_state, s_time = k_rw, "CONFIRMED", rw_time
                s_source = rw_src + "".join(f" + {lab}" for lab in agree)
        elif team in rw_exp and not conflict and starter is None:
            k_rw, rep = rw_exp[team]
            starter, s_state, s_source, s_time = k_rw, "EXPECTED", f"RotoWire expected ({_rw_label(rep, rotowire.fetched_utc)})", _iso(rep.news_created_utc)
        for k in sorted(keys):  # C54: a scratch of the goalie another source names as the starter is a disagreement
            if k in rw_scr and starter == k and not conflict:
                conflict = (f"{name[k]} named starter ({s_source}) but RotoWire reports him out "
                            f"({_iso(rw_scr[k].published_utc)}): CONFLICTED, no automatic repair")
        if conflict:
            board.conflicted_teams.add(team)
            board.problems.append(f"{team}: {conflict}")
        for k in sorted(keys):
            part = rs.persons[k].participation if rs is not None and k in rs.persons else Participation.PLAYING
            gl = GoalieLine(k, name[k], team, UNKNOWN, name.get(starter) if starter else None, s_state)
            if k in ovr_out:
                gl.status, gl.source, gl.report_utc = NOT_STARTING, f"accepted override OUT ({_override_source(ovr_out[k])})", \
                    _iso(ovr_out[k].effective_utc)
            elif part is Participation.OUT:
                gl.status, gl.source = NOT_STARTING, f"DK status OUT ({rs.persons[k].dk_status or 'OUT'})"
            elif k in rw_scr and not conflict:
                sc = rw_scr[k]
                gl.status, gl.report_utc = NOT_STARTING, _iso(sc.published_utc)
                gl.source = board.scratches[k] = _scratch_label(sc, rotowire.fetched_utc)
            elif conflict:
                gl.status, gl.source, gl.starter_state = CONFLICTED, conflict, "CONFLICTED"
            elif starter is not None and s_state == "CONFIRMED":
                gl.status = CONFIRMED if k == starter else NOT_STARTING
                gl.source, gl.report_utc = s_source, s_time
                gl.repair = k != starter
                if gl.repair:
                    gl.source = f"{name[starter]} confirmed for {team} ({s_source})"
            elif starter is not None and s_state == "DK":
                gl.status = EXPECTED if k == starter else NOT_STARTING
                gl.repair = k != starter
                gl.source = s_source if k == starter else f"DraftKings marks {name[starter]} as {team}'s starter (Starting=P)"
            elif starter is not None:
                gl.status = EXPECTED if k == starter else NOT_EXPECTED
                gl.source, gl.report_utc = s_source, s_time
            elif not board.available:
                gl.source = "goalie news unavailable: " + ("; ".join(board.problems[:1]) or "no goalie page")
            else:
                gl.source = f"no Daily Faceoff report for this game ({page_label})"
            board.persons[k] = gl
    if rotowire is not None:  # C54: skaters RotoWire reports out join the exclusion path; the board's own line
        goalie_keys = {k for ks in goalies.values() for k in ks}
        for k, sc in rw_scr.items():
            if k not in goalie_keys:
                board.scratches[k] = _scratch_label(sc, rotowire.fetched_utc)
        fetched = rotowire.fetched_utc
        board.rotowire = {"source": rotowire.source, "fetched_utc": _iso(fetched),
                          "age_min": round((now - fetched).total_seconds() / 60.0, 1) if fetched else None,
                          "counts": dict(sorted(rotowire.counts.items())), "notes": (rotowire.notes + rw_notes)[:8],
                          "unmatched": rw_unmatched[:10],
                          "scratches": {_who(pool, k): v for k, v in sorted(board.scratches.items())},
                          "line": _rw_line(rotowire, {"unmatched": len(rw_unmatched)}, now)}
    if not board.available:
        board.problems.insert(0, f"goalie news unavailable (page source {page.get('source')}, fetched {when or 'never'})")
    return board


def _who(pool, k: str) -> str:
    """'Name (TEAM)': two players can share a name (C54 review)."""
    row = next((r for r in pool.rows if r.person_key == k), None)
    return f"{row.name} ({row.team})" if row is not None else k


def _scratch_label(sc, read_utc: datetime | None = None) -> str:
    from nhl_dfs.data.sources.rotowire import AGGREGATOR

    who = "aggregator" if sc.source_name in (None, AGGREGATOR) else f"citing {sc.source_name}"
    if read_utc is not None and sc.published_utc == read_utc:
        who += _READ_TIME
    return f"RotoWire scratch ({who}): {sc.statement[:120]}" + (f" {sc.source_url}" if sc.source_url else "")


# -- the table -------------------------------------------------------------------------------------------------

def table(board: Board, pool, lineups: dict[str, list], pins: dict[str, dict[int, str]] | None = None) -> list[dict]:
    """One row per goalie cell of each entry (Classic G; Showdown CPT or FLEX)."""
    from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode

    slots = CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    out = []
    for eid, lu in lineups.items():
        for k, rid in enumerate(lu):
            row = pool.by_role_id.get(rid) if rid else None
            if row is None or not row.is_goalie:
                continue
            g = board.persons.get(row.person_key) or GoalieLine(row.person_key, row.name, row.team, UNKNOWN,
                                                                source="not on the board")
            out.append({"entry_id": eid, "slot": str(slots[k]), "goalie": g.name, "team": g.team, "status": g.status,
                        "starter": g.starter, "starter_state": g.starter_state, "source": g.source,
                        "report_utc": g.report_utc, "pinned": bool(pins and k in (pins.get(eid) or {}))})
    return out


def gate_status(rows: list[dict], board: Board) -> str:
    """NOT_STARTING (a goalie known not to start is still in the file), CONFLICTED, NO_NEWS, or CLEAR."""
    if any(r["status"] == NOT_STARTING for r in rows):
        return "NOT_STARTING"
    if any(r["status"] == CONFLICTED for r in rows):
        return "CONFLICTED"
    if not board.available:
        return "NO_NEWS"
    return "CLEAR"


def record(board: Board, rows: list[dict]) -> dict:
    rec = {"GOALIE_GATE": gate_status(rows, board), "now_utc": _iso(board.now_utc), "page": board.page,
           "problems": board.problems[:12], "rows": rows,
           "persons": {k: asdict(g) for k, g in sorted(board.persons.items())}}
    if board.rotowire:  # C54; absent when RotoWire was not read (the test stub), so older records keep their shape
        rec["rotowire"] = board.rotowire
    return rec


def _chi(iso: str | None) -> str:
    from nhl_dfs.build.notes import chicago

    return chicago(iso) if iso else "no time"


def cli_lines(rec: dict | None) -> list[str]:
    """Compact: one line per goalie with his entry count; per-entry lines only for NOT STARTING and CONFLICTED."""
    if not rec:
        return []
    rows = rec.get("rows", [])
    out = [f"GOALIE_GATE={rec['GOALIE_GATE']}"]
    if rec["GOALIE_GATE"] == "NO_NEWS":
        out.append("  " + "; ".join(rec.get("problems", [])[:2]))
    if rec.get("rotowire"):  # C54
        out.append("  " + rec["rotowire"]["line"])
        if rec["rotowire"].get("in_file"):
            out.append("  RotoWire reports out, still in this file: " + ", ".join(rec["rotowire"]["in_file"]))
    by: dict[tuple, list] = {}
    for r in rows:
        by.setdefault((r["goalie"], r["team"], r["status"], r["starter"], r["starter_state"], r["source"],
                       r["report_utc"]), []).append(r)
    out.append("GOALIES (goalie | team | status | entries | team starter | source | time, Chicago):")
    for (g, t, s, st, sst, src, ts), rs in sorted(by.items(), key=lambda kv: (kv[0][2] != NOT_STARTING, kv[0][0])):
        pinned = sum(r["pinned"] for r in rs)
        starter = f"{st} ({sst})" if st else sst
        out.append(f"  {g} | {t} | {s} | {len(rs)}{f' ({pinned} pinned)' if pinned else ''} | {starter} | {src[:90]} | {_chi(ts)}")
    for r in rows:
        if r["status"] in (NOT_STARTING, CONFLICTED):
            tag = "PINNED, cannot change" if r["pinned"] else "open cell"
            out.append(f"  !! entry {r['entry_id']} {r['slot']} {r['goalie']}: {r['status']} ({tag})")
    return out


def notes_lines(rec: dict | None) -> list[str]:
    """RUN_NOTES: every entry's goalie."""
    if not rec:
        return []
    page = rec.get("page") or {}
    out = ["", "## Goalies", "",
           f"- GOALIE_GATE={rec['GOALIE_GATE']}; goalie page: {page.get('source', 'none')}, fetched "
           f"{_chi(page.get('fetched_utc'))}" + (f" ({page['age_min']:.0f} min before the run clock)"
                                                  if page.get("age_min") is not None else "")]
    out += [f"- {p}" for p in rec.get("problems", [])[:6]]
    rw = rec.get("rotowire")
    if rw:  # C54
        out.append(f"- {rw['line']}")
        out += [f"- RotoWire: {n}" for n in rw.get("notes", [])[:4]]
        out += [f"- RotoWire unmatched: {u}" for u in rw.get("unmatched", [])[:4]]
        out += [f"- {who}: {why}" for who, why in rw.get("scratches", {}).items()]
    out += ["", "| Entry | Slot | Goalie | Team | Status | Team starter | Source | Time (Chicago) | Pinned |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in rec.get("rows", []):
        starter = f"{r['starter']} ({r['starter_state']})" if r["starter"] else r["starter_state"]
        out.append(f"| {r['entry_id']} | {r['slot']} | {r['goalie']} | {r['team']} | {r['status']} | {starter} | "
                   f"{r['source']} | {_chi(r['report_utc'])} | {'yes' if r['pinned'] else 'no'} |")
    return out


# -- a run's current file ----------------------------------------------------------------------------------------

def lineups_of(path, pool) -> dict[str, list]:
    """entry_id -> role ids in slot order, from a DKEntries file."""
    from nhl_dfs.build.late_swap import _canonical
    from nhl_dfs.intake.entries import read_entries

    ef = read_entries(path)
    return {e.entry_id: _canonical(ef, e) for e in ef.entries}


def for_run(run, m: dict, *, now: datetime, offline: bool, cache=None, budget_s: float = 8.0,
            with_overrides: bool = True) -> dict | None:
    """The goalie record for a run's current (last) version; None when nothing is published."""
    from nhl_dfs.intake.salary import read_salary

    versions = m.get("versions") or []
    if not versions:
        return None
    pool = read_salary(run.inputs / "DKSalaries.csv")
    overrides = []
    if with_overrides:
        from nhl_dfs.build.late_swap import accepted_overrides

        overrides, _ = accepted_overrides(run, pool, now)
    board = build(pool, now=now, cache=cache, offline=offline, budget_s=budget_s, overrides=overrides)
    lineups = lineups_of(versions[-1]["path"], pool)
    rec = record(board, table(board, pool, lineups))
    if board.scratches:  # C54: who RotoWire reports out is still in the file (the run applies no scratch; refresh does)
        held = {pool.by_role_id[rid].person_key for lu in lineups.values() for rid in lu if rid in pool.by_role_id}
        rec["rotowire"]["in_file"] = sorted(_who(pool, k) for k in board.scratches if k in held)
    return rec


def refresh_run_record(run, *, now: datetime, offline: bool = True, cache=None) -> dict | None:
    """Recompute a run's goalie record after a controller publish (qa-apply, overrides-apply); rewrites the
    manifest's `goalies` and RUN_NOTES. Stored pages by default (these commands run right after the run)."""
    from nhl_dfs.build.manifest import read_manifest, write_manifest
    from nhl_dfs.build.notes import write_run_notes

    m = read_manifest(run)
    rec = for_run(run, m, now=now, offline=offline, cache=cache)
    if rec is None:
        return None
    m["goalies"] = rec
    write_manifest(run, m)
    write_run_notes(run, m)
    return rec


def _override_source(o) -> str:
    """The label of an override's source: its URL, or Ben's report (C53, flag 83: a BEN override has no URL)."""
    return f"Ben in session: {o.claim}" if getattr(o, "source", "") == "BEN" else o.source_url
