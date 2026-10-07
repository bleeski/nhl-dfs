"""C40 measurement (first commit, no behavior change): does the Daily Faceoff "Last updated" stamp move when the lines change?

The rule it applies was committed first: docs/experiments/2026-10-07_c40_df_stamp_rule.md. This script only counts and then applies
that rule's verdict steps mechanically; it never chooses a threshold.

Offline. It COPIES the cached team-page bodies, the fetch log and the cached box scores into a scratch folder, reads only the copies,
and writes only the markdown report. An audit hook (the guard of scripts/c39_measure.py) blocks the network and refuses and counts every
write under the repo's data/, runs/ and outputs/ folders.

  python scripts/c40_measure.py --scratch <dir> --out <report.md> [--cutoff 2026-10-07T13:30:28Z] [--raw data/raw]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

MIN_GAP = timedelta(minutes=20)
FOLLOW_MIN, FOLLOW_MAX = timedelta(hours=6), timedelta(hours=24)
BANDS = [("12-24 h", 12, 24), ("24-48 h", 24, 48), ("48-72 h", 48, 72), ("72-96 h", 72, 96)]
EV_KEYS = ("f1", "f2", "f3", "f4", "d1", "d2", "d3", "d4")
PP_KEYS = ("pp1", "pp2")
Z95 = 1.645  # one-sided 95%
# the rule's numbers (docs/experiments/2026-10-07_c40_df_stamp_rule.md); changing one here without changing that file is a defect
M_MAX, U_MAX, MIN_EVENTS, MIN_STALE_EPISODES, MIN_BAND_EPISODES, MIN_PP_EVENTS, R_MARGIN = 0.05, 0.10, 30, 30, 10, 15, 0.10


def load_guard_module():
    spec = importlib.util.spec_from_file_location("c39_measure", REPO / "scripts" / "c39_measure.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def utc(text: str) -> datetime:
    from nhl_dfs.data.sources.dk_public import parse_iso_utc

    return parse_iso_utc(text)


@dataclass
class Fetch:
    team: str
    at: datetime
    hash: str
    stamp: datetime
    groups: dict[str, frozenset]
    names: dict[int, str]
    goalies: tuple
    tags: frozenset
    news_max: datetime | None
    news_after: list  # (name, news time) for non-goalie skaters with a news item newer than the stamp
    regressed: bool = False

    @property
    def ev(self):
        return tuple(self.groups.get(k, frozenset()) for k in EV_KEYS)

    @property
    def pp(self):
        return tuple(self.groups.get(k, frozenset()) for k in PP_KEYS)

    @property
    def age_h(self) -> float:
        return (self.at - self.stamp).total_seconds() / 3600.0


def wilson_upper(c: int, n: int, z: float = Z95) -> float | None:
    if n <= 0:
        return None
    p = c / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def md_table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    return out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


# -- inputs ------------------------------------------------------------------------------------------------------------

def copy_inputs(raw: Path, scratch: Path, cutoff: datetime) -> tuple[list[dict], dict[str, Path], int]:
    """Copy the fetch logs and the needed bodies into scratch; return (observations, hash -> scratch body path, logs copied)."""
    obs_dir = scratch / "obs"
    obs_dir.mkdir(parents=True, exist_ok=True)
    n_logs = 0
    for p in sorted((raw / "observations").glob("*.jsonl")):
        shutil.copy2(p, obs_dir / p.name)
        n_logs += 1
    rows = []
    for p in sorted(obs_dir.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("source") == "dailyfaceoff" and r.get("raw_hash") and utc(r["fetched_at"]) <= cutoff:
                rows.append(r)
    where: dict[str, Path] = {}
    for p in (raw / "dailyfaceoff").glob("*/*.html"):
        where.setdefault(p.stem, p)
    pages = scratch / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    got: dict[str, Path] = {}
    for h in sorted({r["raw_hash"] for r in rows}):
        if h in where:
            dst = pages / f"{h}.html"
            if not dst.exists():
                shutil.copy2(where[h], dst)
            got[h] = dst
    return rows, got, n_logs


def parse_fetches(rows: list[dict], bodies: dict[str, Path]) -> tuple[dict[str, list[Fetch]], dict]:
    from nhl_dfs.data.http import SourceSchemaError
    from nhl_dfs.data.sources import dailyfaceoff as df

    cache: dict[str, object] = {}
    skipped = {"no body": 0, "not a team page": 0}
    by_team: dict[str, list[Fetch]] = defaultdict(list)
    seen = set()
    for r in sorted(rows, key=lambda r: (r["fetched_at"], r["url"])):
        key = (r["url"], r["fetched_at"], r["raw_hash"])
        if key in seen:
            continue
        seen.add(key)
        h = r["raw_hash"]
        if h not in bodies:
            skipped["no body"] += 1
            continue
        if h not in cache:
            try:
                cache[h] = df.parse_team_page(bodies[h].read_text(encoding="utf-8"))
            except SourceSchemaError:
                cache[h] = None
        tl = cache[h]
        if tl is None:
            skipped["not a team page"] += 1
            continue
        groups = {f"f{i}": frozenset(p.player_id for p in g) for i, g in enumerate(tl.f_lines, 1)}
        groups.update({f"d{i}": frozenset(p.player_id for p in g) for i, g in enumerate(tl.d_pairs, 1)})
        groups.update({"pp1": frozenset(p.player_id for p in tl.pp1), "pp2": frozenset(p.player_id for p in tl.pp2)})
        gids = {p.player_id for p in tl.goalies}
        news_after = sorted((p.name, p.news_created_utc) for p in tl.players.values()
                            if p.player_id not in gids and p.news_created_utc and p.news_created_utc > tl.updated_utc)
        news = [p.news_created_utc for p in tl.players.values() if p.news_created_utc]
        tags = frozenset((p.player_id, p.injury_status, p.game_time_decision) for p in tl.players.values()
                         if p.injury_status or p.game_time_decision)
        by_team[tl.team or tl.team_name].append(Fetch(
            tl.team or tl.team_name, utc(r["fetched_at"]), h, tl.updated_utc, groups, {p.player_id: p.name for p in tl.players.values()},
            tuple(p.player_id for p in tl.goalies), tags, max(news) if news else None, news_after))
    return by_team, skipped


def clean(by_team: dict[str, list[Fetch]]) -> tuple[dict[str, list[Fetch]], list[Fetch]]:
    """Per team: order by fetch time, remove fetches whose stamp went backward (stale cached copies), return (clean, removed)."""
    out, removed = {}, []
    for team, fs in by_team.items():
        fs = sorted(fs, key=lambda f: f.at)
        best, keep = None, []
        for f in fs:
            if best is not None and f.stamp < best:
                f.regressed = True
                removed.append(f)
                continue
            best = f.stamp if best is None else max(best, f.stamp)
            keep.append(f)
        out[team] = keep
    return out, removed


def thin(fs: list[Fetch]) -> list[Fetch]:
    out: list[Fetch] = []
    for f in fs:
        if not out or f.at - out[-1].at >= MIN_GAP:
            out.append(f)
    return out


# -- the 2x2 -----------------------------------------------------------------------------------------------------------

def cells(pairs, changed) -> dict[str, int]:
    c = {"A": 0, "B": 0, "C": 0, "D": 0}
    for a, b in pairs:
        moved, ch = a.stamp != b.stamp, changed(a, b)
        c[("A" if ch else "B") if moved else ("C" if ch else "D")] += 1
    return c


SIGS = {
    "EV": lambda a, b: a.ev != b.ev,
    "PP": lambda a, b: a.pp != b.pp,
    "EV+PP": lambda a, b: a.ev != b.ev or a.pp != b.pp,
    "goalie depth (info)": lambda a, b: a.goalies != b.goalies,
}


def two_by_two_rows(label: str, pairs) -> tuple[list[list], dict[str, dict[str, int]]]:
    rows, allc = [], {}
    for name, fn in SIGS.items():
        c = cells(pairs, fn)
        allc[name] = c
        n = c["A"] + c["C"]
        m = c["C"] / n if n else None
        rows.append([label, name, c["A"], c["B"], c["C"], c["D"], n, pct(m), pct(wilson_upper(c["C"], n)),
                     pct(c["A"] / (c["A"] + c["B"]) if c["A"] + c["B"] else None), pct(c["C"] / (c["C"] + c["D"]) if c["C"] + c["D"] else None)])
    return rows, allc


# -- R, the stale-still-right rate over episodes --------------------------------------------------------------------------

def episodes(clean_by_team):
    eps = []
    for team, fs in clean_by_team.items():
        by_stamp = defaultdict(list)
        for f in fs:
            by_stamp[f.stamp].append(f)
        for stamp, group in by_stamp.items():
            eps.append((team, stamp, group))
    return eps


def follow_up(fs: list[Fetch], f: Fetch) -> Fetch | None:
    cand = [g for g in fs if f.at + FOLLOW_MIN <= g.at <= f.at + FOLLOW_MAX]
    return cand[-1] if cand else None


def r_table(clean_by_team):
    res = {}
    stale_with_follow = set()
    for name, lo, hi in BANDS:
        n = eq_ev = eq_pp = 0
        keys = []
        for team, stamp, group in episodes(clean_by_team):
            first = next((f for f in group if lo <= f.age_h < hi), None)
            if first is None:
                continue
            g = follow_up(clean_by_team[team], first)
            if g is None:
                continue
            n += 1
            eq_ev += first.ev == g.ev
            eq_pp += first.pp == g.pp
            keys.append((team, stamp))
            if lo >= 24:
                stale_with_follow.add((team, stamp))
        res[name] = {"n": n, "ev": eq_ev, "pp": eq_pp, "keys": keys}
    return res, len(stale_with_follow)


# -- the verdict, mechanical -------------------------------------------------------------------------------------------

def verdict(allc, r, n_stale_eps) -> tuple[str, list[str]]:
    trace: list[str] = []
    ev, pp = allc["EV"], allc["PP"]
    n_ev, n_pp = ev["A"] + ev["C"], pp["A"] + pp["C"]
    ref = r["12-24 h"]
    r_ref = ref["ev"] / ref["n"] if ref["n"] else None
    trace.append(f"EV lines-change events A+C = {n_ev} (floor {MIN_EVENTS}); stale episodes with a follow-up = {n_stale_eps} (floor {MIN_STALE_EPISODES}); "
                 f"reference band episodes = {ref['n']} (floor {MIN_BAND_EPISODES})")
    if n_ev < MIN_EVENTS or n_stale_eps < MIN_STALE_EPISODES or ref["n"] < MIN_BAND_EPISODES:
        return "BLOCKED (the data cannot answer: a floor is not met)", trace
    m, u = ev["C"] / n_ev, wilson_upper(ev["C"], n_ev)
    trace.append(f"M(EV) = {pct(m)} (limit {pct(M_MAX)}), U(EV) = {pct(u)} (limit {pct(U_MAX)})")
    if m > M_MAX:
        return "REJECT (M(EV) above 5%: the stamp misses too many real changes)", trace
    if u > U_MAX:
        return "BLOCKED (M(EV) is 5% or less but its upper bound is above 10%: inconclusive)", trace
    passing_cap, stopped = 24, False
    for name, lo, hi in BANDS[1:]:
        b = r[name]
        if b["n"] < MIN_BAND_EPISODES:
            trace.append(f"band {name}: {b['n']} episodes with a follow-up: not measured")
            if name == "24-48 h":
                return "BLOCKED (band 24-48 h is not measured)", trace
            stopped = True
        else:
            rb = b["ev"] / b["n"]
            ok = rb >= r_ref - R_MARGIN
            trace.append(f"band {name}: R = {pct(rb)} over {b['n']} episodes against reference {pct(r_ref)} minus {pct(R_MARGIN)}: {'pass' if ok else 'FAIL'}")
            if not ok and name == "24-48 h":
                return "REJECT (band 24-48 h is more than 10 points under the reference)", trace
            if not ok:
                stopped = True
        if not stopped:
            passing_cap = hi
        else:
            break
    pp_ok = n_pp >= MIN_PP_EVENTS and pp["C"] / n_pp <= M_MAX and (wilson_upper(pp["C"], n_pp) or 1) <= U_MAX
    trace.append(f"PP events A+C = {n_pp} (floor {MIN_PP_EVENTS}); M(PP) = {pct(pp['C'] / n_pp if n_pp else None)}, "
                 f"U(PP) = {pct(wilson_upper(pp['C'], n_pp))}: {'pass' if pp_ok else 'not kept'}")
    trace.append(f"cap = {passing_cap} h")
    if passing_cap >= 72 and pp_ok:
        return f"ADOPT (cap {passing_cap} h, lines and PP units)", trace
    why = []
    if passing_cap < 72:
        why.append(f"cap {passing_cap} h")
    if not pp_ok:
        why.append("PP units not kept (keep_pp: false)")
    return "ADOPT WITH A LIMIT (" + "; ".join(why) + ")", trace


# -- information only ------------------------------------------------------------------------------------------------

def news_predicts(clean_by_team):
    c = {"news_change": 0, "news_same": 0, "none_change": 0, "none_same": 0}
    for fs in clean_by_team.values():
        for f in thin(fs):
            g = follow_up(fs, f)
            if g is None:
                continue
            ch = f.ev != g.ev
            c[("news_" if f.news_after else "none_") + ("change" if ch else "same")] += 1
    return c


def live_fields(pairs):
    out = {}
    for moved in (False, True):
        sel = [(a, b) for a, b in pairs if (a.stamp != b.stamp) == moved]
        out[moved] = (len(sel), sum(a.tags != b.tags for a, b in sel), sum(a.news_max != b.news_max for a, b in sel))
    return out


def stay_unchanged(clean_by_team):
    spans = []
    for fs in clean_by_team.values():
        start = None
        for i, f in enumerate(fs):
            if start is None:
                start = f
            nxt = fs[i + 1] if i + 1 < len(fs) else None
            if nxt is None or nxt.ev != f.ev:
                spans.append((f.at - start.at).total_seconds() / 3600.0)
                start = None
    return spans


def played_check(raw: Path, scratch: Path, clean_by_team, cutoff):
    from nhl_dfs.contracts.ids import normalize_name

    box_dir = scratch / "box"
    box_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted((raw / "nhl_boxscore").glob("*/*")):
        dst = box_dir / f"{p.parent.name}_{p.name}"
        shutil.copy2(p, dst)
        try:
            d = json.loads(dst.read_text(encoding="utf-8"))
            start = utc(d["startTimeUTC"])
            if d.get("gameState") not in ("FINAL", "OFF") or start > cutoff:
                continue
            for side in ("awayTeam", "homeTeam"):
                abbr = d[side]["abbrev"]
                pb = d["playerByGameStats"][side]
                dressed = set()
                for g in ("forwards", "defense"):
                    for pl in pb.get(g, []):
                        nm = pl["name"]["default"] if isinstance(pl["name"], dict) else str(pl["name"])
                        dressed.add(normalize_name(nm).split()[-1])
                fs = [f for f in clean_by_team.get(abbr, []) if f.at < start]
                if not fs:
                    continue
                f = fs[-1]
                listed = [normalize_name(f.names[i]).split()[-1] for k in EV_KEYS for i in f.groups.get(k, ())]
                if not listed:
                    continue
                hit = sum(1 for n in listed if n in dressed) / len(listed)
                rows.append((abbr, start, f.age_h, hit))
        except Exception:
            continue
    return rows


# -- spot check ------------------------------------------------------------------------------------------------------

def timeline(fs: list[Fetch], limit: int = 14) -> list[str]:
    out = []
    prev = None
    for f in thin(fs):
        if prev is not None:
            moved, ev, pp = f.stamp != prev.stamp, f.ev != prev.ev, f.pp != prev.pp
            if moved or ev or pp:
                out.append(f"{f.at:%m-%d %H:%MZ}  stamp {f.stamp:%m-%d %H:%MZ} {'MOVED' if moved else 'same '}  EV {'CHANGED' if ev else 'same   '}  PP {'CHANGED' if pp else 'same   '}")
        prev = f
    return out[:limit]


def diff_names(a: Fetch, b: Fetch) -> str:
    bits = []
    for k in EV_KEYS + PP_KEYS:
        x, y = a.groups.get(k, frozenset()), b.groups.get(k, frozenset())
        if x != y:
            nm = {**a.names, **b.names}
            bits.append(f"{k}: -[{', '.join(sorted(nm[i] for i in x - y))}] +[{', '.join(sorted(nm[i] for i in y - x))}]")
    return "; ".join(bits) or "(none)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cutoff", default="2026-10-07T13:30:28Z")
    ap.add_argument("--raw", default=str(REPO / "data" / "raw"))
    a = ap.parse_args()
    scratch, out_path, raw = Path(a.scratch).resolve(), Path(a.out).resolve(), Path(a.raw).resolve()
    guard = load_guard_module()
    guard.install_guards(scratch, False)
    cutoff = utc(a.cutoff)

    rows, bodies, n_logs = copy_inputs(raw, scratch, cutoff)
    by_team, skipped = parse_fetches(rows, bodies)
    cl, removed = clean(by_team)
    n_fetch = sum(len(v) for v in by_team.values())
    pairs, by_team_pairs = [], {}
    for team, fs in cl.items():
        t = thin(fs)
        by_team_pairs[team] = list(zip(t, t[1:]))
        pairs += by_team_pairs[team]
    mid_times = sorted(b.at for _, b in pairs)
    mid = mid_times[len(mid_times) // 2] if mid_times else cutoff
    first_half = [(a_, b) for a_, b in pairs if b.at <= mid]
    second_half = [(a_, b) for a_, b in pairs if b.at > mid]

    L: list[str] = []
    L += [f"# C40 measurement: does the Daily Faceoff stamp move when the lines change? (generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z)", "",
          "Generated by `scripts/c40_measure.py` against the rule committed first in "
          "`docs/experiments/2026-10-07_c40_df_stamp_rule.md`. Sections 0 to 5 (3b included) are the script's output; any section after them "
          "was written by hand after reading the raw bodies (re-running the script overwrites it). Nothing here was tuned: the verdict in "
          "section 3 is the rule's steps applied mechanically.", ""]
    L += ["## 0. What was read", "",
          f"- fetch logs copied: {n_logs}; Daily Faceoff fetches at or before the cutoff {cutoff:%Y-%m-%d %H:%M:%SZ}: {len(rows)}; "
          f"distinct bodies found: {len(bodies)}; fetches skipped: {skipped}",
          f"- team-page fetches parsed: {n_fetch} over {len(by_team)} teams; stamps going backward (stale cached copies, removed first): {len(removed)}",
          f"- after the 20-minute thinning: {sum(len(thin(v)) for v in cl.values())} fetches, {len(pairs)} consecutive pairs; "
          f"first/second half split at {mid:%m-%d %H:%MZ}",
          "- how pairs are built: the rule says \"two consecutive fetches of one team at least 20 minutes apart\"; the script reads that as greedy "
          "thinning in fetch order (a fetch is kept only if it is 20 minutes or more after the last kept fetch), a reading fixed when the script was "
          "written, before its first run. A different reading could move the counts: some of the misses below are 20 to 30 minute pairs.", ""]
    for f in removed[:10]:
        L.append(f"  - backward stamp: {f.team} fetched {f.at:%m-%d %H:%MZ} with stamp {f.stamp:%m-%d %H:%MZ}")
    L += ["", "## 1. The 2x2 over pairs", "",
          "A = stamp moved and lines changed; B = moved, unchanged; **C = stamp NOT moved and lines changed (dangerous)**; D = neither. "
          "M = C / (A + C); U = one-sided 95% Wilson upper bound of M. `P(ch|mv)` = A/(A+B); `P(ch|no mv)` = C/(C+D).", ""]
    head = ["scope", "signature", "A", "B", "C", "D", "A+C", "M", "U", "P(ch|mv)", "P(ch|no mv)"]
    t_all, allc = two_by_two_rows("all pairs", pairs)
    t1, _ = two_by_two_rows("first half", first_half)
    t2, _ = two_by_two_rows("second half", second_half)
    L += md_table(head, t_all + t1 + t2) + ["", "Per team (EV+PP signature; teams with at least one C pair are marked):", ""]
    team_rows = []
    for team, ps in sorted(by_team_pairs.items()):
        c = cells(ps, SIGS["EV+PP"])
        team_rows.append([team, len(ps), c["A"], c["B"], c["C"], c["D"]])
    L += md_table(["team", "pairs", "A", "B", "C", "D"], team_rows)
    c_pairs = [(a_, b) for a_, b in pairs if a_.stamp == b.stamp and (a_.ev != b.ev or a_.pp != b.pp)]
    L += ["", f"The C pairs (stamp not moved, lines changed), up to 12 of {len(c_pairs)}:", ""]
    for a_, b in c_pairs[:12]:
        L.append(f"- {a_.team}: fetched {a_.at:%m-%d %H:%MZ} and {b.at:%m-%d %H:%MZ}, stamp {a_.stamp:%m-%d %H:%MZ} both times: {diff_names(a_, b)}")

    r, n_stale = r_table(cl)
    L += ["", "## 2. R, the stale-still-right rate (episodes)", "",
          "An episode is one (team, stamp). R = share of episodes whose first fetch in the band has the same lines as the team's last fetch "
          "6 to 24 h later. Episodes with no such follow-up are left out.", ""]
    L += md_table(["stamp age band", "episodes with a follow-up", "R (EV)", "R (PP)"],
                  [[n, r[n]["n"], pct(r[n]["ev"] / r[n]["n"] if r[n]["n"] else None), pct(r[n]["pp"] / r[n]["n"] if r[n]["n"] else None)] for n, _, _ in BANDS])
    L += ["", f"Stale episodes (stamp age 24 h or more) with a follow-up, counted once: {n_stale}."]

    v, trace = verdict(allc, r, n_stale)
    L += ["", "## 3. Verdict (the rule's steps, applied mechanically)", ""] + [f"- {t}" for t in trace] + ["", f"**{v}**"]

    n_ev = allc["EV"]["A"] + allc["EV"]["C"]
    L += ["", "## 3b. Exploratory, written after the verdict (it cannot change the verdict)", "",
          "Where do the changes the stamp moved for, and the ones it missed, sit? One row per group; a pair counts once per group that changed.", ""]
    grp_rows = []
    for k in EV_KEYS + PP_KEYS:
        mv = sum(1 for a_, b in pairs if a_.groups.get(k, frozenset()) != b.groups.get(k, frozenset()) and a_.stamp != b.stamp)
        nm = sum(1 for a_, b in pairs if a_.groups.get(k, frozenset()) != b.groups.get(k, frozenset()) and a_.stamp == b.stamp)
        grp_rows.append([k, mv, nm])
    L += md_table(["group", "changed, stamp moved", "changed, stamp NOT moved"], grp_rows)
    if n_ev and allc["EV"]["C"]:
        L += ["", f"Fragility: M(EV) is {allc['EV']['C']} of {n_ev}. With one miss fewer ({allc['EV']['C'] - 1} of {n_ev}) U would be "
              f"{pct(wilson_upper(allc['EV']['C'] - 1, n_ev))}; with one more ({allc['EV']['C'] + 1} of {n_ev}), {pct(wilson_upper(allc['EV']['C'] + 1, n_ev))}. "
              "The rule's 10% bound sits between those, so this verdict rests on single events."]
    L += ["", "## 4. Information only (decides nothing)", ""]
    nc = news_predicts(cl)
    nn, no = nc["news_change"] + nc["news_same"], nc["none_change"] + nc["none_same"]
    L += [f"- A skater news item after the stamp (rule (c)) against an EV change at the 6-to-24 h follow-up (unit: a fetch with a follow-up): "
          f"with news {nc['news_change']} of {nn} changed ({pct(nc['news_change'] / nn if nn else None)}); without news {nc['none_change']} of {no} "
          f"changed ({pct(nc['none_change'] / no if no else None)}). This cannot relax rule (c).",
          ""]
    lf = live_fields(pairs)
    L += ["- Are tags and news live fields (do they move without the stamp)? Pairs, then pairs where the tag set changed, then where the newest news time changed:",
          f"  - stamp not moved: {lf[False][0]} pairs, tags changed {lf[False][1]}, newest news changed {lf[False][2]}",
          f"  - stamp moved: {lf[True][0]} pairs, tags changed {lf[True][1]}, newest news changed {lf[True][2]}", ""]
    spans = stay_unchanged(cl)
    if spans:
        q = statistics.quantiles(spans, n=4) if len(spans) >= 4 else [min(spans), statistics.median(spans), max(spans)]
        L += [f"- How long EV lines stay unchanged (runs of equal lines, first to last fetch of the run): {len(spans)} runs, median {statistics.median(spans):.1f} h, "
              f"quartiles {q[0]:.1f} / {q[1]:.1f} / {q[2]:.1f} h, longest {max(spans):.1f} h; runs of 24 h or more: {sum(s >= 24 for s in spans)}", ""]
    played = played_check(raw, scratch, cl, cutoff)
    fresh = [h for *_, ag, h in played if ag <= 24]
    stale = [h for *_, ag, h in played if ag > 24]
    L += [f"- Listed skaters (f1-f4, d1-d3) of the last page fetched before the game who dressed (cached box scores, 25 at most, last-name match): "
          f"{len(played)} team-games; fresh stamp (24 h or less) {len(fresh)} team-games, mean {pct(statistics.mean(fresh) if fresh else None)}; "
          f"stale stamp {len(stale)} team-games, mean {pct(statistics.mean(stale) if stale else None)}", ""]

    L += ["## 5. Spot check timelines (only fetches where the stamp or the lines moved; the first 14 per team)", ""]
    worst = max(by_team_pairs, key=lambda t: cells(by_team_pairs[t], SIGS["EV+PP"])["C"]) if by_team_pairs else None
    for team in dict.fromkeys(["CHI", "COL", worst or "FLA"]):
        L += [f"### {team}", "", "```"] + timeline(cl.get(team, [])) + ["```", ""]
    L += [f"_Guard: {len(guard.WRITES)} write(s) outside the scratch folder refused, {len(guard.NET)} network attempt(s) blocked._", ""]
    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {out_path}; verdict: {v}; guard refused writes {len(guard.WRITES)}, network {len(guard.NET)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
