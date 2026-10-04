"""Synthesize filed DraftKings standings: who finished in the top 1%, who cashed, and how they were built.

    .venv\\Scripts\\python.exe scripts\\standings_synthesis.py --dates 2026-09-30 2026-10-01 2026-10-02 --out <md> --json <json>

Reads only what is on disk plus the public NHL schedule, box scores and rosters through the repo's HttpCache
(`--offline` uses the cache only). Nothing here edits a run, the ledger, or BACKLOG.md; the output is a report.

Sources, in order of authority:
- `data/standings/inbox/<date>/**/contest-standings-<id>.csv|zip` (DraftKings' export; parsed by learn/standings).
- `data/raw/dk_lobby/**/*.json` (the lobby capture): contest name, fee, max entries, max entries per user, pool, draft group.
- `data/raw/dk_contest/**/*.json` (cached contest details): payout tables of DraftKings templates, reused by
  (name without the game suffix, max entries). A contest with no template match gets a PROXY paid-places count
  (23.5% of max entries, the share every cached GPP template pays within 1 point) and is labeled so.
- NHL box scores and rosters: team, position, goals, decision for each player; also the integrity check that
  each entry's listed Points equals the sum of its players' FPTS (CPT at 1.5x).
- `runs/*/inputs/DKSalaries.csv` when a local run holds the contest: salary used per lineup, and that run's frozen
  field forecast `field.json` stack frequencies, compared with the field that showed up.

Other entrants' usernames are personal data and are never written; only aggregates, plus Ben's own entries.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nhl_dfs.contracts.geometry import Mode  # noqa: E402
from nhl_dfs.contracts.ids import normalize_name  # noqa: E402
from nhl_dfs.learn import standings as st  # noqa: E402

OURS = "bleeski"
PROXY_PAID_FRACTION = 0.235
CHALK = {"classic": 25.0, "showdown": 40.0}
PUNT = {"classic": 5.0, "showdown": 10.0}
_FIRST_ALIASES = {
    "mitch": "mitchell", "alex": "alexander", "matt": "matthew", "josh": "joshua", "nick": "nicholas", "zach": "zachary",
    "will": "william", "mike": "michael", "jake": "jacob", "tom": "thomas", "cam": "cameron", "sam": "samuel",
    "max": "maxim", "alexei": "alexey", "evgeni": "evgeny", "nikolaj": "nikolai", "jj": "john jason", "tj": "t j",
    "pat": "patrick", "chris": "christopher", "dan": "daniel", "ben": "benjamin", "rob": "robert", "joe": "joseph",
    "mikey": "michael", "nic": "nicolas", "vasily": "vasili", "yegor": "egor", "kirill": "kirill",
}
_GAME_SUFFIX = re.compile(r"\s*\([A-Z]{2,3} @ [A-Z]{2,3}[^)]*\)")


# -- player map --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Player:
    nhl_id: int
    name: str
    team: str
    pos: str  # F, D, G
    game: str | None
    goals: int = 0
    assists: int = 0
    sog: int = 0
    blocks: int = 0
    pp_goals: int = 0
    toi_s: int = 0
    decision: str | None = None
    saves: int = 0
    goals_against: int = 0
    dressed: bool = False


@dataclass
class DayMap:
    day: str
    games: list[str]  # "AWAY@HOME"
    scores: dict[str, str]
    opp: dict[str, str]  # team -> opponent
    game_of: dict[str, str]  # team -> "AWAY@HOME"
    by_norm: dict[str, list[Player]]
    by_last: dict[str, list[Player]]
    team_goals: dict[str, int]
    unresolved: Counter = field(default_factory=Counter)

    def resolve(self, raw: str, token: str, hint_fpts: float | None = None) -> Player | None:
        """DK name + roster token -> one NHL player. Exact normalized name first, then a first-name alias, then a
        hyphenated first name reduced to its first part (DK's "Elias-Nils Pettersson"), then a unique last name in
        the position class. Two candidates left (Vancouver's two Elias Petterssons): the one whose box-score DK points
        reproduce the FPTS DraftKings listed for this name, when exactly one does within half a point."""
        want = {"C": "F", "W": "F", "D": "D", "G": "G"}.get(token)
        n = normalize_name(raw)
        parts = n.split()
        cands = self.by_norm.get(n, [])
        if not cands and len(parts) >= 2 and parts[0] in _FIRST_ALIASES:
            cands = self.by_norm.get(" ".join([_FIRST_ALIASES[parts[0]], *parts[1:]]), [])
        if not cands and len(parts) >= 2:
            rev = {v: k for k, v in _FIRST_ALIASES.items()}
            if parts[0] in rev:
                cands = self.by_norm.get(" ".join([rev[parts[0]], *parts[1:]]), [])
        if not cands and len(parts) >= 3 and "-" in raw.split()[0]:
            cands = self.by_norm.get(" ".join([parts[0], *parts[2:]]), [])
        if want:
            typed = [c for c in cands if c.pos == want]
            cands = typed or cands
        if len(cands) == 1:
            return cands[0]
        if not cands:
            last = parts[-1] if parts else ""
            pool = [c for c in self.by_last.get(last, []) if (want is None or c.pos == want)]
            if len(pool) == 1:
                return pool[0]
        if len(cands) > 1 and hint_fpts is not None:
            close = [c for c in cands if abs(box_dk_points(c) - hint_fpts) <= 0.5]
            if len(close) == 1:
                return close[0]
        self.unresolved[f"{raw} [{token}]"] += 1
        return None


def box_dk_points(p: Player) -> float:
    """DK points from the NHL box score (no shorthanded or shootout detail; a tiebreak, not a settlement)."""
    if p.pos == "G":
        if p.toi_s <= 0:
            return 0.0
        pts = 0.7 * p.saves - 3.5 * p.goals_against + (6.0 if p.decision == "W" else 0.0)
        return pts + (2.0 if p.goals_against == 0 and p.decision == "W" else 0.0) + (3.0 if p.saves >= 35 else 0.0)
    pts = 8.5 * p.goals + 5.0 * p.assists + 1.5 * p.sog + 1.3 * p.blocks
    return pts + 3.0 * ((p.goals >= 3) + (p.sog >= 5) + (p.blocks >= 3) + (p.goals + p.assists >= 3))


def _pos_class(p: str) -> str:
    p = p.upper()
    return "G" if p == "G" else "D" if p == "D" else "F"


def build_day_map(day: str, *, offline: bool) -> DayMap:
    from nhl_dfs.data.http import HttpCache
    from nhl_dfs.data.sources import nhl

    http = HttpCache(offline=offline)
    sched = nhl.schedule(date.fromisoformat(day), cache=http)
    games, scores, opp, game_of, team_goals = [], {}, {}, {}, Counter()
    stats: dict[int, dict] = {}
    for g in sched:
        key = f"{g.away}@{g.home}"
        games.append(key)
        opp[g.away], opp[g.home] = g.home, g.away
        game_of[g.away] = game_of[g.home] = key
        try:
            b = nhl.boxscore(g.game_id, cache=http)
        except Exception as exc:  # the report still runs; teams stay, stats are blank
            scores[key] = f"box score unavailable ({type(exc).__name__})"
            continue
        scores[key] = f"{b.away_score}-{b.home_score}" if b.game_state in ("OFF", "FINAL") else f"state {b.game_state}"
        for s in b.skaters:
            stats[s.nhl_id] = {"goals": s.goals, "assists": s.assists, "sog": s.sog, "blocks": s.blocks,
                               "pp_goals": s.pp_goals, "toi_s": s.toi_s, "dressed": True}
            team_goals[s.team] += s.goals
        for gl in b.goalies:
            stats[gl.nhl_id] = {"decision": gl.decision, "saves": gl.saves, "goals_against": gl.goals_against,
                                "toi_s": gl.toi_s, "dressed": gl.toi_s > 0}
    by_norm: dict[str, list[Player]] = defaultdict(list)
    by_last: dict[str, list[Player]] = defaultdict(list)
    for team in sorted(set(opp)):
        for p in nhl.roster(team, cache=http):
            extra = stats.get(p.nhl_id, {})
            pl = Player(p.nhl_id, f"{p.first_name} {p.last_name}", team, _pos_class(p.position), game_of.get(team), **extra)
            by_norm[normalize_name(pl.name)].append(pl)
            by_last[normalize_name(p.last_name)].append(pl)
    return DayMap(day, games, scores, opp, game_of, dict(by_norm), dict(by_last), dict(team_goals))


# -- contest meta and payout templates -------------------------------------------------------------------

def lobby_meta(contest_ids: set[int], root: Path) -> dict[int, dict]:
    """Name, fee, max entries, max entries per user, pool, draft group, start (UTC) from the lobby captures."""
    want = {str(c) for c in contest_ids}
    out: dict[int, dict] = {}
    for p in sorted((root / "data" / "raw" / "dk_lobby").rglob("*.json")):
        if not want - {str(k) for k in out}:
            break
        try:
            js = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue
        for c in js.get("Contests", []) if isinstance(js, dict) else []:
            cid = str(c.get("id"))
            if cid in want and int(cid) not in out:
                ms = re.search(r"(\d{10,})", str(c.get("sd", "")))
                out[int(cid)] = {"name": c.get("n"), "fee": float(c.get("a") or 0), "max_entries": int(c.get("m") or 0),
                                 "max_per_user": int(c.get("mec") or 0), "pool": float(c.get("po") or 0),
                                 "draft_group": c.get("dg"), "game_type": c.get("gameType"),
                                 "start_utc": datetime.fromtimestamp(int(ms.group(1)) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if ms else None}
    return out


def template_tables(root: Path) -> dict[str, dict]:
    """(name without the game suffix | max entries) -> {paid, tiers} from every cached DK contest detail."""
    out: dict[str, dict] = {}
    files = sorted((root / "data" / "raw" / "dk_contest").rglob("*.json")) + sorted((root / "runs").glob("*/settle/prize_tables/dk_contest_*.json"))
    for p in files:
        try:
            js = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        d = js.get("contestDetail", js)
        if not isinstance(d, dict) or "payoutSummary" not in d or "maximumEntries" not in d:
            continue
        tiers = []
        for t in d["payoutSummary"]:
            amt = None
            for desc in t.get("payoutDescriptions", []):
                if desc.get("value") is not None:
                    amt = float(desc["value"])
            if amt is None:
                m = re.search(r"\$([\d,]+(?:\.\d+)?)", str(t.get("tierPayoutDescriptions", {}).get("Cash", "")))
                amt = float(m.group(1).replace(",", "")) if m else None
            tiers.append({"min": int(t["minPosition"]), "max": int(t["maxPosition"]), "cash": amt})
        if not tiers:
            continue
        key = f"{_GAME_SUFFIX.sub('', str(d.get('name', ''))).strip()}|{int(d['maximumEntries'])}"
        out.setdefault(key, {"paid": max(t["max"] for t in tiers), "tiers": tiers, "from_contest": d.get("contestKey") or d.get("id")})
    return out


def paid_places(meta: dict | None, entries_n: int, templates: dict[str, dict]) -> tuple[int, str, list | None]:
    """Paid places and the label of where the number came from."""
    if not meta:
        return max(1, round(PROXY_PAID_FRACTION * entries_n)), "PROXY (no lobby record; 23.5% of entries)", None
    name, mx = _GAME_SUFFIX.sub("", meta["name"]).strip(), meta["max_entries"]
    t = templates.get(f"{name}|{mx}")
    if t:
        return t["paid"], f"TEMPLATE (cached table of contest {t['from_contest']}, same name and size)", t["tiers"]
    m = re.search(r"top (\d+) win", name, re.I)
    if m:
        return int(m.group(1)), "NAME (the contest name states the paid places)", None
    if re.search(r"winner take all|\bwta\b", name, re.I):
        return 1, "NAME (winner take all)", None
    if re.search(r"double up|50/50", name, re.I):
        return max(1, round(0.45 * mx)), "PROXY (double up: 45% of max entries)", None
    return max(1, round(PROXY_PAID_FRACTION * mx)), "PROXY (23.5% of max entries; cached GPP templates pay 22.9% to 24.0%)", None


def payout_at(tiers: list | None, rank: int, tied: int) -> float | None:
    """DK tie rule: the tied places' prizes pooled and split evenly (ledger.py has the cent rounding; this is a report)."""
    if not tiers:
        return None
    total = 0.0
    for pos in range(rank, rank + tied):
        for t in tiers:
            if t["min"] <= pos <= t["max"]:
                total += t["cash"] or 0.0
                break
    return total / tied


# -- salary and forecast from local runs ----------------------------------------------------------------

def local_run_for(contest_id: int, runs_root: Path) -> tuple[dict[str, dict] | None, dict | None, str | None]:
    """Salary by normalized name from the newest local run holding the contest, and the frozen field forecast
    (field.json) from that run or its nearest ancestor that saved one."""
    hits = []
    for r in sorted(runs_root.glob("2026*"), reverse=True):
        f = r / "inputs" / "DKEntries.csv"
        if not f.is_file():
            continue
        try:
            txt = f.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        if re.search(rf"(^|,){contest_id}(,|$)", txt, re.M):
            hits.append(r)
    if not hits:
        return None, None, None
    run = hits[0]
    salary: dict[str, dict] = {}
    sal = run / "inputs" / "DKSalaries.csv"
    if sal.is_file():
        for row in csv.DictReader(io.StringIO(sal.read_text(encoding="utf-8-sig", errors="ignore"))):
            if row.get("Name") and row.get("Salary", "").strip().isdigit():
                salary[normalize_name(row["Name"])] = {"salary": int(row["Salary"]), "team": row.get("TeamAbbrev"),
                                                      "pos": row.get("Position"), "id": row.get("ID")}
    forecast, fid = None, None
    seen, cur = set(), run
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        fj = cur / "field.json"
        if fj.is_file():
            try:
                forecast, fid = json.loads(fj.read_text(encoding="utf-8")), cur.name
            except json.JSONDecodeError:
                pass
            break
        try:
            parent = json.loads((cur / "manifest.json").read_text(encoding="utf-8")).get("parent_run_id")
        except (OSError, json.JSONDecodeError):
            parent = None
        cur = runs_root / parent if parent else None
    return salary or None, forecast, fid


# -- per-entry metrics -------------------------------------------------------------------------------------

@dataclass
class EntryMetrics:
    entry_id: str
    rank: int
    points: float
    ours: bool
    names: tuple[str, ...]
    players: tuple[Player | None, ...]
    slots: tuple[str, ...]
    shape: str
    primary: int
    secondary: int
    n_teams: int
    n_games: int
    goalie_team: str | None
    goalie_with_stack: bool
    goalie_vs_own_skaters: bool
    goalie_won: bool | None
    bringback: bool  # Classic: at least one skater from the primary stack's opponent
    d_in_util: bool
    cpt: str | None
    cpt_pos: str | None
    cpt_team: str | None
    split: str | None
    has_goalie: bool
    goalie_cpt: bool
    n_d: int
    own_sum: float
    n_chalk: int
    n_punt: int
    dup: int
    salary_used: int | None
    points_check: bool | None
    one_team_sweep: bool  # Classic: 5+ skaters from one team; Showdown: 5+ of 6 from one team


def entry_metrics(s: st.Standings, dm: DayMap, mode: str, own_any: dict[str, float], fpts: dict[str, Decimal],
                  dups: Counter, salary: dict[str, dict] | None) -> list[EntryMetrics]:
    out = []
    chalk, punt = CHALK[mode], PUNT[mode]
    for e in s.entries:
        if e.blank:
            continue
        slots = tuple(sl for sl, _ in e.lineup)
        names = tuple(normalize_name(n) for _, n in e.lineup)
        players = tuple(dm.resolve(n, sl, float(fpts[normalize_name(n)]) if normalize_name(n) in fpts else None)
                        for sl, n in e.lineup)
        teams_sk = Counter(p.team for p, sl in zip(players, slots) if p and p.pos != "G")
        counts = sorted(teams_sk.values(), reverse=True)
        shape = "-".join(str(c) for c in counts) or "?"
        primary = counts[0] if counts else 0
        secondary = counts[1] if len(counts) > 1 else 0
        goalies = [p for p in players if p and p.pos == "G"]
        g = goalies[0] if goalies else None
        gt = g.team if g else None
        own_sum = sum(own_any.get(n, 0.0) for n in names)
        n_chalk = sum(1 for n in names if own_any.get(n, 0.0) >= chalk)
        n_punt = sum(1 for n in names if own_any.get(n, 0.0) <= punt)
        key = frozenset(names)
        sal_used = None
        if salary and all(n in salary for n in names):
            sal_used = sum(salary[n]["salary"] for n in names)
        # points integrity: sum of FPTS, Captain at 1.5x
        if all(n in fpts for n in names):
            tot = sum((fpts[n] * (Decimal("1.5") if sl == "CPT" else 1)) for sl, n in zip(slots, names))
            pc = abs(float(tot) - float(e.points)) < 0.2
        else:
            pc = None
        if mode == "classic":
            util_idx = slots.index("UTIL") if "UTIL" in slots else None
            d_util = bool(util_idx is not None and players[util_idx] and players[util_idx].pos == "D")
            gws = bool(gt and teams_sk.get(gt, 0) >= 2)
            gvs = bool(gt and dm.opp.get(gt) and teams_sk.get(dm.opp[gt], 0) >= 1)
            games = {p.game for p in players if p and p.game}
            primary_team = max(teams_sk, key=lambda t: (teams_sk[t], t)) if teams_sk else None
            bringback = bool(primary_team and dm.opp.get(primary_team) and teams_sk.get(dm.opp[primary_team], 0) >= 1)
            m = EntryMetrics(e.entry_id, e.rank, float(e.points), e.entry_name.lower().startswith(OURS), names, players, slots,
                             shape, primary, secondary, len(teams_sk), len(games), gt, gws, gvs,
                             (g.decision == "W") if g and g.decision else None, bringback, d_util, None, None, None, None, True,
                             False, sum(1 for p in players if p and p.pos == "D"), own_sum, n_chalk, n_punt, dups[key],
                             sal_used, pc, primary >= 5)
        else:
            cpt_i = slots.index("CPT") if "CPT" in slots else 0
            cp = players[cpt_i]
            teams_all = Counter(p.team for p in players if p)
            split = "-".join(str(c) for c in sorted(teams_all.values(), reverse=True)) or "?"
            m = EntryMetrics(e.entry_id, e.rank, float(e.points), e.entry_name.lower().startswith(OURS), names, players, slots,
                             shape, primary, secondary, len(teams_all), 1, gt, False, False,
                             (g.decision == "W") if g and g.decision else None, False, False, names[cpt_i],
                             cp.pos if cp else None, cp.team if cp else None, split, bool(goalies), bool(cp and cp.pos == "G"),
                             sum(1 for p in players if p and p.pos == "D"), own_sum, n_chalk, n_punt, dups[key], sal_used, pc,
                             max(teams_all.values(), default=0) >= 5)
        out.append(m)
    return out


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def cohort_summary(ms: list[EntryMetrics], mode: str) -> dict:
    if not ms:
        return {"n": 0}
    n = len(ms)
    shape_counts = Counter(m.shape for m in ms)
    out = {"n": n, "mean_points": round(mean(m.points for m in ms), 1),
           "shapes": [{"shape": s, "pct": _pct(c, n)} for s, c in shape_counts.most_common(6)],
           "shape_counts": dict(shape_counts),
           "primary_ge3_pct": _pct(sum(m.primary >= 3 for m in ms), n),
           "primary_ge4_pct": _pct(sum(m.primary >= 4 for m in ms), n),
           "primary_ge5_pct": _pct(sum(m.primary >= 5 for m in ms), n),
           "mean_primary": round(mean(m.primary for m in ms), 2),
           "mean_teams": round(mean(m.n_teams for m in ms), 2),
           "mean_own_sum": round(mean(m.own_sum for m in ms), 1),
           "mean_chalk": round(mean(m.n_chalk for m in ms), 2),
           "mean_punt": round(mean(m.n_punt for m in ms), 2),
           "dup_pct": _pct(sum(m.dup > 1 for m in ms), n),
           "mean_d": round(mean(m.n_d for m in ms), 2),
           "points_check_pct": _pct(sum(bool(m.points_check) for m in ms), sum(m.points_check is not None for m in ms))}
    sal = [m.salary_used for m in ms if m.salary_used is not None]
    out["mean_salary_used"] = round(mean(sal)) if sal else None
    if mode == "classic":
        gw = [m.goalie_won for m in ms if m.goalie_won is not None]
        out.update({"mean_games": round(mean(m.n_games for m in ms), 2),
                    "goalie_with_stack_pct": _pct(sum(m.goalie_with_stack for m in ms), n),
                    "goalie_vs_own_skaters_pct": _pct(sum(m.goalie_vs_own_skaters for m in ms), n),
                    "goalie_won_pct": _pct(sum(gw), len(gw)) if gw else None,
                    "bringback_pct": _pct(sum(m.bringback for m in ms), n),
                    "d_in_util_pct": _pct(sum(m.d_in_util for m in ms), n),
                    "two_stacks_3plus_pct": _pct(sum(m.primary >= 3 and m.secondary >= 3 for m in ms), n)})
    else:
        out.update({"splits": [{"split": s, "pct": _pct(c, n)} for s, c in Counter(m.split for m in ms).most_common(5)],
                    "has_goalie_pct": _pct(sum(m.has_goalie for m in ms), n),
                    "goalie_cpt_pct": _pct(sum(m.goalie_cpt for m in ms), n),
                    "cpt_pos": {k: _pct(c, n) for k, c in Counter(m.cpt_pos or "?" for m in ms).items()},
                    "sweep_5plus_pct": _pct(sum(m.one_team_sweep for m in ms), n)})
    return out


# -- one contest -------------------------------------------------------------------------------------------

def analyze_contest(s: st.Standings, dm: DayMap, meta: dict | None, templates: dict[str, dict], runs_root: Path) -> dict:
    mode = s.mode.value
    n_all = len(s.entries)
    active = [e for e in s.entries if not e.blank]
    # actual ownership reconstructed from the lineups (DK's denominator is every entry, blanks included)
    cnt_any: Counter = Counter()
    cnt_cpt: Counter = Counter()
    dups: Counter = Counter()
    for e in active:
        names = [normalize_name(n) for _, n in e.lineup]
        for sl, n in zip((sl for sl, _ in e.lineup), names):
            cnt_any[n] += 1
            if sl == "CPT":
                cnt_cpt[n] += 1
        dups[frozenset(names)] += 1
    own_any = {k: 100.0 * v / n_all for k, v in cnt_any.items()}
    own_cpt = {k: 100.0 * v / n_all for k, v in cnt_cpt.items()}
    # actual FPTS per person from the ownership block (FLEX basis in Showdown)
    fpts: dict[str, Decimal] = {}
    for r in s.ownership:
        n = normalize_name(r.name)
        v = r.fpts / Decimal("1.5") if r.roster_position == "CPT" else r.fpts
        if r.roster_position != "CPT" or n not in fpts:
            fpts[n] = v
    salary, forecast, forecast_run = local_run_for(s.contest_id, runs_root)
    ms = entry_metrics(s, dm, mode, own_any, fpts, dups, salary)
    by_rank = sorted(ms, key=lambda m: (m.rank, m.entry_id))
    k1 = max(1, round(0.01 * n_all))
    k10 = max(1, round(0.10 * n_all))
    paid, paid_src, tiers = paid_places(meta, n_all, templates)
    top1 = [m for m in by_rank if m.rank <= k1]
    top10 = [m for m in by_rank if m.rank <= k10]
    cash = [m for m in by_rank if m.rank <= paid]
    rest = [m for m in by_rank if m.rank > paid]
    cash_line = min((m.points for m in by_rank if m.rank <= paid), default=None)
    top1_line = min((m.points for m in top1), default=None)
    # per-player table
    def own_in(cohort: list[EntryMetrics], name: str) -> float:
        return _pct(sum(name in m.names for m in cohort), len(cohort))
    players_tbl = []
    for n, pct in sorted(own_any.items(), key=lambda kv: -kv[1]):
        p = dm.resolve(n, "UTIL", float(fpts[n]) if n in fpts else None)
        players_tbl.append({"name": n, "team": p.team if p else None, "pos": p.pos if p else None,
                            "own_pct": round(pct, 1), "cpt_pct": round(own_cpt.get(n, 0.0), 1) if mode == "showdown" else None,
                            "fpts": float(fpts.get(n, Decimal(0))), "top1_pct": own_in(top1, n), "cash_pct": own_in(cash, n),
                            "goalie": bool(p and p.pos == "G"), "decision": p.decision if p and p.pos == "G" else None})
    goalies = [r for r in players_tbl if r["goalie"]]
    lev = [r | {"leverage": round(r["top1_pct"] - r["own_pct"], 1)} for r in players_tbl if r["own_pct"] >= 1.0 or r["top1_pct"] >= 10.0]
    lev_pos = sorted(lev, key=lambda r: -r["leverage"])[:8]
    lev_neg = sorted(lev, key=lambda r: r["leverage"])[:8]
    # team stacks (Classic: 3+ skaters of a team; Showdown: 4+ of 6 players of a team)
    def stack_share(cohort: list[EntryMetrics], team: str) -> float:
        thr = 3 if mode == "classic" else 4
        hits = 0
        for m in cohort:
            c = Counter(p.team for p in m.players if p and (p.pos != "G" or mode == "showdown"))
            hits += c.get(team, 0) >= thr
        return _pct(hits, len(cohort))
    teams = sorted({p.team for m in ms for p in m.players if p})
    fc_stack = None
    if forecast:
        fams = forecast.get("families", {})
        for fam in fams.values():
            if str(s.contest_id) in [str(c) for c in fam.get("contests", [])]:
                fc_stack = fam.get("stack_freq")
    stacks = [{"team": t, "goals": dm.team_goals.get(t), "game": dm.game_of.get(t), "score": dm.scores.get(dm.game_of.get(t, ""), ""),
               "field_pct": stack_share(ms, t), "top1_pct": stack_share(top1, t), "cash_pct": stack_share(cash, t),
               "forecast_pct": (fc_stack or {}).get(t)} for t in teams]
    # captains
    captains = None
    if mode == "showdown":
        captains = []
        for n, pct in sorted(own_cpt.items(), key=lambda kv: -kv[1])[:12]:
            p = dm.resolve(n, "CPT", float(fpts[n]) if n in fpts else None)
            captains.append({"name": n, "team": p.team if p else None, "pos": p.pos if p else None, "cpt_pct": round(pct, 1),
                             "top1_cpt_pct": _pct(sum(m.cpt == n for m in top1), len(top1)),
                             "cash_cpt_pct": _pct(sum(m.cpt == n for m in cash), len(cash)),
                             "fpts": float(fpts.get(n, Decimal(0)))})
    # our entries and the winner
    def describe(m: EntryMetrics) -> dict:
        tied = s.tied(m.rank)
        d = {"entry_id": m.entry_id, "rank": m.rank, "tied": tied, "of": n_all, "pct": round(100.0 * m.rank / n_all, 1),
             "points": m.points, "shape": m.shape, "split": m.split, "cpt": m.cpt, "own_sum": round(m.own_sum, 1),
             "n_chalk": m.n_chalk, "n_punt": m.n_punt, "dup": m.dup, "salary_used": m.salary_used,
             "goalie": m.goalie_team, "goalie_with_stack": m.goalie_with_stack, "goalie_vs_own_skaters": m.goalie_vs_own_skaters,
             "n_teams": m.n_teams, "n_games": m.n_games,
             "cohort": "top1%" if m.rank <= k1 else "top10%" if m.rank <= k10 else "cash" if m.rank <= paid else "no cash",
             "cashed": m.rank <= paid, "payout": payout_at(tiers, m.rank, tied),
             "lineup": [f"{sl}:{(p.name if p else n)}({p.team if p else '?'},{own_any.get(n, 0):.0f}%,{float(fpts.get(n, 0)):.1f})"
                        for sl, n, p in zip(m.slots, m.names, m.players)]}
        return d
    ours = [describe(m) for m in by_rank if m.ours]
    winner = describe(by_rank[0]) if by_rank else None
    unresolved = sum(1 for m in ms for p in m.players if p is None)
    return {"contest_id": s.contest_id, "mode": mode, "meta": meta, "entries": n_all, "blank": n_all - len(active),
            "distinct_lineups": len(dups), "max_dup": max(dups.values(), default=0),
            "paid_places": paid, "paid_source": paid_src, "cash_line": cash_line, "top1_k": k1, "top1_line": top1_line,
            "top_score": by_rank[0].points if by_rank else None,
            "slots_unresolved": unresolved, "slots_total": sum(len(m.players) for m in ms),
            "cohorts": {"top1": cohort_summary(top1, mode), "top10": cohort_summary(top10, mode),
                        "cash": cohort_summary(cash, mode), "rest": cohort_summary(rest, mode), "field": cohort_summary(ms, mode)},
            "players_top": players_tbl[:15], "goalies": goalies, "leverage_pos": lev_pos, "leverage_neg": lev_neg,
            "stacks": stacks, "captains": captains, "forecast_run": forecast_run,
            "forecast_top8": [x.get("name") for x in (forecast or {}).get("families", {}).get("large_gpp", {}).get("top_ownership", [])[:8]] if forecast else None,
            "actual_top8": [r["name"] for r in players_tbl[:8]], "ours": ours, "winner": winner}


# -- report ------------------------------------------------------------------------------------------------

def _row(cells: list) -> str:
    return "| " + " | ".join("" if c is None else str(c) for c in cells) + " |"


def render_contest(c: dict) -> list[str]:
    meta = c["meta"] or {}
    name = meta.get("name") or f"contest {c['contest_id']} (name not in the lobby cache)"
    L = [f"### {name} (`{c['contest_id']}`)", ""]
    L.append(f"- Mode {c['mode']}; fee ${meta.get('fee', '?')}; entries {c['entries']} (max {meta.get('max_entries', '?')}, "
             f"{meta.get('max_per_user', '?')} per user); blank lineups {c['blank']}; distinct lineups {c['distinct_lineups']} "
             f"(largest duplicate group {c['max_dup']}).")
    L.append(f"- Top score {c['top_score']}; top 1% = {c['top1_k']} place(s), line {c['top1_line']}; paid places {c['paid_places']} "
             f"({c['paid_source']}), cash line {c['cash_line']}.")
    L.append(f"- Player map: {c['slots_unresolved']} of {c['slots_total']} lineup slots unresolved; points check passed in "
             f"{c['cohorts']['field']['points_check_pct']}% of lineups.")
    if c.get("forecast_run"):
        L.append(f"- Frozen field forecast from run {c['forecast_run']}: predicted top 8 by ownership {c['forecast_top8']}; "
                 f"actual top 8 {c['actual_top8']}.")
    L.append("")
    cols = ["cohort", "n", "mean pts", "shapes (top 3)", "primary >=3 %", ">=4 %", ">=5 %", "teams", "own sum", "chalk", "punts", "dup %"]
    if c["mode"] == "classic":
        cols += ["G with stack %", "G vs own skaters %", "G won %", "bring-back %", "D at UTIL %", "salary"]
    else:
        cols += ["splits (top 3)", "has G %", "G as CPT %", "CPT pos"]
    L.append(_row(cols))
    L.append(_row(["---"] * len(cols)))
    for k in ("top1", "top10", "cash", "rest", "field"):
        s = c["cohorts"][k]
        if not s.get("n"):
            L.append(_row([k, 0] + [""] * (len(cols) - 2)))
            continue
        shapes = ", ".join(f"{x['shape']} {x['pct']}%" for x in s["shapes"][:3])
        row = [k, s["n"], s["mean_points"], shapes, s["primary_ge3_pct"], s["primary_ge4_pct"], s["primary_ge5_pct"], s["mean_teams"],
               s["mean_own_sum"], s["mean_chalk"], s["mean_punt"], s["dup_pct"]]
        if c["mode"] == "classic":
            row += [s["goalie_with_stack_pct"], s["goalie_vs_own_skaters_pct"], s["goalie_won_pct"], s["bringback_pct"],
                    s["d_in_util_pct"], s["mean_salary_used"]]
        else:
            row += [", ".join(f"{x['split']} {x['pct']}%" for x in s["splits"][:3]), s["has_goalie_pct"], s["goalie_cpt_pct"],
                    " ".join(f"{k2} {v}%" for k2, v in sorted(s["cpt_pos"].items()))]
        L.append(_row(row))
    L.append("")
    L.append("Most owned (field % / top-1% % / cash % / FPTS):")
    L.append("")
    L.append(_row(["player", "team", "pos", "field %", "top1 %", "cash %", "FPTS"]))
    L.append(_row(["---"] * 7))
    for r in c["players_top"][:12]:
        L.append(_row([r["name"], r["team"], r["pos"], r["own_pct"], r["top1_pct"], r["cash_pct"], r["fpts"]]))
    L.append("")
    L.append("Leverage (top-1% ownership minus field ownership), best and worst:")
    L.append("")
    L.append(_row(["player", "team", "pos", "field %", "top1 %", "leverage", "FPTS"]))
    L.append(_row(["---"] * 7))
    for r in c["leverage_pos"][:6] + c["leverage_neg"][:6]:
        L.append(_row([r["name"], r["team"], r["pos"], r["own_pct"], r["top1_pct"], r["leverage"], r["fpts"]]))
    L.append("")
    if c["mode"] == "classic":
        L.append("Goalies (field % / top-1% % / cash % / decision / FPTS):")
        L.append("")
        L.append(_row(["goalie", "team", "field %", "top1 %", "cash %", "decision", "FPTS"]))
        L.append(_row(["---"] * 7))
        for r in c["goalies"]:
            L.append(_row([r["name"], r["team"], r["own_pct"], r["top1_pct"], r["cash_pct"], r["decision"], r["fpts"]]))
        L.append("")
        L.append("Team stacks, 3+ skaters (share of lineups):")
    else:
        L.append("Captains (field CPT % / top-1% CPT % / cash CPT % / FPTS at FLEX scale):")
        L.append("")
        L.append(_row(["captain", "team", "pos", "field CPT %", "top1 CPT %", "cash CPT %", "FPTS"]))
        L.append(_row(["---"] * 7))
        for r in c["captains"] or []:
            L.append(_row([r["name"], r["team"], r["pos"], r["cpt_pct"], r["top1_cpt_pct"], r["cash_cpt_pct"], r["fpts"]]))
        L.append("")
        L.append("Team stacks, 4+ of 6 (share of lineups):")
    L.append("")
    L.append(_row(["team", "game", "score", "goals", "field %", "top1 %", "cash %", "forecast %"]))
    L.append(_row(["---"] * 8))
    for r in c["stacks"]:
        L.append(_row([r["team"], r["game"], r["score"], r["goals"], r["field_pct"], r["top1_pct"], r["cash_pct"], r["forecast_pct"]]))
    L.append("")
    if c["winner"]:
        w = c["winner"]
        L.append(f"Winner: {w['points']} pts, shape {w['shape']}{(' split ' + w['split']) if w['split'] else ''}, own sum {w['own_sum']}%, "
                 f"chalk {w['n_chalk']}, punts {w['n_punt']}, dup {w['dup']}: {', '.join(w['lineup'])}")
        L.append("")
    if c["ours"]:
        L.append("Our entries:")
        L.append("")
        for o in c["ours"]:
            pay = "" if o["payout"] is None else f", payout ${o['payout']:.2f} by the template table"
            L.append(f"- rank {o['rank']}{(' tied ' + str(o['tied'])) if o['tied'] > 1 else ''} of {o['of']} (top {o['pct']}%), "
                     f"{o['points']} pts, {o['cohort']}{pay}; shape {o['shape']}{(' split ' + o['split']) if o['split'] else ''}, "
                     f"own sum {o['own_sum']}%, chalk {o['n_chalk']}, punts {o['n_punt']}, dup {o['dup']}, teams {o['n_teams']}, "
                     f"goalie {o['goalie']}{' with stack' if o['goalie_with_stack'] else ''}{' vs own skaters' if o['goalie_vs_own_skaters'] else ''}"
                     f"{(', salary ' + str(o['salary_used'])) if o['salary_used'] else ''}: {', '.join(o['lineup'])}")
        L.append("")
    return L


def pooled(contests: list[dict], mode: str) -> list[str]:
    """Cross-contest view: cohort means weighted by cohort size, per mode."""
    cs = [c for c in contests if c["mode"] == mode]
    if not cs:
        return []
    L = [f"### Pooled {mode} ({len(cs)} contests)", ""]
    keys = ["primary_ge3_pct", "primary_ge4_pct", "primary_ge5_pct", "mean_teams", "mean_own_sum", "mean_chalk", "mean_punt", "dup_pct"]
    keys += ["goalie_with_stack_pct", "goalie_vs_own_skaters_pct", "goalie_won_pct", "bringback_pct", "d_in_util_pct",
             "two_stacks_3plus_pct"] if mode == "classic" \
        else ["has_goalie_pct", "goalie_cpt_pct", "sweep_5plus_pct"]
    L.append(_row(["cohort", "lineups"] + keys))
    L.append(_row(["---"] * (2 + len(keys))))
    for k in ("top1", "top10", "cash", "rest", "field"):
        rows = [c["cohorts"][k] for c in cs if c["cohorts"][k].get("n")]
        n = sum(r["n"] for r in rows)
        vals = []
        for key in keys:
            xs = [(r[key], r["n"]) for r in rows if r.get(key) is not None]
            vals.append(round(sum(v * w for v, w in xs) / sum(w for _, w in xs), 1) if xs else None)
        L.append(_row([k, n] + vals))
    L.append("")
    shapes = {k: Counter() for k in ("top1", "cash", "field")}
    for c in cs:
        for k in shapes:
            shapes[k].update(c["cohorts"][k].get("shape_counts", {}))
    tot = {k: sum(v.values()) for k, v in shapes.items()}
    L.append("Shapes (skaters per team, Classic; players per team, Showdown): share of lineups per cohort, every lineup counted:")
    L.append("")
    L.append(_row(["shape", "top1 %", "cash %", "field %"]))
    L.append(_row(["---"] * 4))
    for sh, _ in shapes["top1"].most_common(10):
        L.append(_row([sh] + [round(100 * shapes[k][sh] / tot[k], 1) if tot[k] else None for k in ("top1", "cash", "field")]))
    L.append("")
    return L


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inbox", default=str(REPO_ROOT / "data" / "standings" / "inbox"))
    ap.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    ap.add_argument("--dates", nargs="+", required=True, help="slate dates (inbox subfolders)")
    ap.add_argument("--out", default=None, help="markdown report path (default: stdout)")
    ap.add_argument("--json", default=None, help="write the full per-contest JSON here")
    ap.add_argument("--offline", action="store_true", help="NHL data from the local cache only")
    a = ap.parse_args(argv)
    inbox, runs_root = Path(a.inbox), Path(a.runs_root)
    templates = template_tables(REPO_ROOT)
    report: list[str] = [f"# Standings synthesis ({datetime.now().strftime('%Y-%m-%d')})", "",
                         "Generated by scripts/standings_synthesis.py. Aggregates only; other entrants are never named.", ""]
    all_contests: list[dict] = []
    out_json: dict = {"dates": {}}
    for day in a.dates:
        folder = inbox / day
        sts, notes = st.read_all(folder)
        if not sts:
            report += [f"## {day}: no standings under {folder}", ""]
            continue
        dm = build_day_map(day, offline=a.offline)
        meta = lobby_meta({s.contest_id for s in sts}, REPO_ROOT)
        report += [f"## {day}", "", f"Games: {', '.join(f'{g} {dm.scores.get(g, '')}' for g in dm.games)}", ""]
        for n in notes:
            report.append(f"- note: {n}")
        groups: dict = defaultdict(list)
        for s in sts:
            groups[(meta.get(s.contest_id) or {}).get("draft_group") or "unknown draft group"].append(s)
        day_out = []
        for dg, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
            starts = sorted({(meta.get(s.contest_id) or {}).get("start_utc") or "" for s in group})
            report += [f"### Draft group {dg} (start {', '.join(x for x in starts if x) or 'unknown'}; {len(group)} contest(s))", ""]
            for s in sorted(group, key=lambda x: (x.mode.value, -len(x.entries))):
                c = analyze_contest(s, dm, meta.get(s.contest_id), templates, runs_root)
                c["slate_date"], c["draft_group"] = day, dg
                all_contests.append(c)
                day_out.append(c)
                report += render_contest(c)
        if dm.unresolved:
            report.append(f"Unresolved names this day (count): {dict(dm.unresolved.most_common(12))}")
            report.append("")
        out_json["dates"][day] = {"games": dm.games, "scores": dm.scores, "contests": day_out,
                                  "unresolved": dict(dm.unresolved)}
    report += ["## Pooled", ""]
    report += pooled(all_contests, "classic") + pooled(all_contests, "showdown")
    text = "\n".join(report)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out} ({len(all_contests)} contests)")
    else:
        print(text)
    if a.json:
        Path(a.json).write_text(json.dumps(out_json, indent=1, default=lambda o: o.__dict__ if hasattr(o, "__dict__") else str(o)),
                                encoding="utf-8")
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
