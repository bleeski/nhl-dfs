"""Objective selection for late swap and refresh (C9; plan section 11, "Fast mode").

Lock semantics are C2c's and are not touched here: this module only says how open cells are valued.
Order: scenario when a scenario cache exists on the run or its parent chain, provisional otherwise,
baseline as the last resort. Every step down is recorded with its reason (manifest `objective`,
RUN_NOTES `OBJECTIVE=`), never silent.

- scenario: the parent's cached selection draws (build/scenario_cache.py). Games whose hash changed
  (roles applied, a status change) and that have not started are re-simulated with the same seed
  streams; started games keep their cached draws. Each target entry's repair candidates are scored by
  its contest family's C8 objective against the cached field and the user's other entries in that
  contest, then ordered by tiebreak.rank. Reported figures are measured on the referee stream.
- provisional: the MILP on the role-applied ParamTable mean times the play probability.
- baseline: C2's salary/APPG prior (models.priors.prior_objective), exactly the C2c path.

Roles (backlog B9, B11): the ParamTable is built once, roles.merge then roles.apply_state are called
exactly once on it (RoleModel), and RoleState.confirmed_at() goes to sim.slate.build_slate.
Participation is priced once per person: a person whose p_play < 1 (QUESTIONABLE, conflict) takes
the scenario mask (and the provisional haircut) at the RoleState p_play instead of C8's 0.85, unless
apply_state already lowered his dressing or start probability (his absence from a projected lineup is
then already priced; the mask is not stacked on top).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import numpy as np

from nhl_dfs.build import objectives as ob
from nhl_dfs.build import scenario_cache as scache
from nhl_dfs.build import tiebreak
from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.contracts.statuses import FieldCalibration, Participation

KINDS = ("scenario", "provisional", "baseline")
ORDER = {"auto": KINDS, "scenario": KINDS, "provisional": KINDS[1:], "baseline": KINDS[2:]}


# -- role state (built once per run) -----------------------------------------------------------------

@dataclass
class RoleModel:
    table0: Any  # the C5 ParamTable before roles
    table: Any  # after roles.apply_state (applied exactly once)
    roles: Any  # models.roles.RoleState
    play_prob: dict[str, float]  # person_key -> probability used once (mask / haircut)
    news_state: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def gather_role_inputs(pool, cache, goalies: tuple | None = None) -> tuple[dict, list, str, list[str]]:
    """Daily Faceoff team pages and goalie reports through the cache (offline: stored pages only).
    Returns (lines by NHL code, goalie reports, goalie path, problems). Never raises.
    goalies: (reports, path, problems) the goalie gate already gathered (build.goalies); then the goalie
    page is not fetched again."""
    from zoneinfo import ZoneInfo

    from nhl_dfs.data.http import SourceSchemaError, SourceUnavailable
    from nhl_dfs.data.sources import dailyfaceoff as df

    et = ZoneInfo("America/New_York")
    days = sorted({g.start_utc.astimezone(et).date() for g in pool.games.values()})
    codes = df.team_codes()
    slate = {c["nhl"]: (slug, c) for slug, c in codes.items() if c["dk"] and c["dk"] in pool.teams}
    lines, problems = {}, []
    for nhl, (slug, c) in sorted(slate.items()):
        try:
            lines[nhl] = df.team_lines(slug, cache=cache)
        except (SourceSchemaError, SourceUnavailable) as exc:
            problems.append(f"{c['dk']}: team page unavailable ({type(exc).__name__})")
        except Exception as exc:  # a network error must never strand a late swap
            problems.append(f"{c['dk']}: team page error ({type(exc).__name__})")
    if goalies is not None:
        reports, path, gprob = goalies
        return lines, list(reports), path, problems + list(gprob)
    reports, path = [], "none"
    for d in days:
        try:
            got = df.fetch_goalies(d, cache=cache, teams=sorted(slate))
        except Exception as exc:
            problems.append(f"goalies {d}: error ({type(exc).__name__})")
            continue
        reports += got.reports
        path = got.path if path == "none" else path
        problems += [f"goalies {d}: {n}" for n in got.notes]
    return lines, reports, path, problems


def _bounded(fn: Callable, budget_s: float, name: str):
    box: dict[str, Any] = {}

    def worker():
        try:
            box["v"] = fn()
        except BaseException as exc:
            box["e"] = exc

    th = threading.Thread(target=worker, name=name, daemon=True)
    th.start()
    th.join(timeout=budget_s)
    if th.is_alive():
        raise TimeoutError(f"{name} exceeded {budget_s:.0f}s")
    if "e" in box:
        raise box["e"]
    return box["v"]


def play_probs(table0, table, roles) -> tuple[dict[str, float], list[str]]:
    """person_key -> play probability for the one participation pricing (see the module docstring). A person
    is left unmasked only when roles priced his absence from evidence: a skater missing from his team's usable
    projected lineup (apply_state moved his dressing toward 0), or a goalie whose team report names or confirms
    another starter. A listed player whose dressing merely drifted (toward df_listed_p, or a budget cut) is
    still masked. table0 is kept for the caller's comparison and reports."""
    out, skipped = {}, []
    for k, r in roles.persons.items():
        p = float(r.p_play)
        if k not in table.persons or p >= 1.0:
            continue
        if _absence_priced(k, r, roles):
            skipped.append(k)
            continue
        out[k] = p
    notes = [f"participation priced once: {len(out)} person(s) at their role-state play probability"]
    if skipped:
        notes.append(f"{len(skipped)} person(s) with p_play < 1 not masked: absent from a usable projected lineup (or another "
                     "goalie named), which roles already priced in dressing or start probability; the mask is not stacked")
    return out, notes


def _absence_priced(k: str, r, roles) -> bool:
    if r.group == "G":
        g = roles.goalies.get(r.team)
        return g is not None and any(x is not None and x != k for x in (g.confirmed, g.named))
    page = roles.team_pages.get(r.team)
    return bool(page and page.get("usable") and not r.df_listed)


def _avail(pp) -> float:
    if pp.goalie is not None:
        return float(pp.goalie.p_start)
    return float(pp.opportunity.p_dress) if pp.opportunity is not None else 1.0


def build_role_model(pool, work, st, *, dk_rec, now: datetime, clock, offline: bool, cache, budget_s: float,
                     apply_state: Callable | None = None, overrides: list | None = None, table0=None,
                     goalie_inputs: tuple | None = None) -> RoleModel:
    """ParamTable (C5), then roles.merge and roles.apply_state exactly once (B11). Raises when the
    ParamTable cannot be built (the caller falls back and says so)."""
    from nhl_dfs.build import news
    from nhl_dfs.build.run import salary_statuses, slate_as_of
    from nhl_dfs.data.http import HttpCache
    from nhl_dfs.models import params as params_mod
    from nhl_dfs.models import roles as roles_mod

    apply_state = apply_state or roles_mod.apply_state
    table0 = table0 if table0 is not None else params_mod.projection_for(work, slate_as_of(pool, clock))
    notes = []
    http = cache if cache is not None else HttpCache(offline=offline)
    try:
        lines, reports, path, problems = _bounded(lambda: gather_role_inputs(pool, http, goalie_inputs), budget_s,
                                                     "nhl-roles-fetch")
    except TimeoutError as exc:
        lines, reports, path, problems = {}, [], "none", [str(exc)]
    notes.append(f"roles: {'stored' if offline else 'cache-first'} Daily Faceoff pages, {len(lines)} team page(s) read, "
                 f"goalie path {path}")
    rs = roles_mod.merge(dk_rec, lines, reports, roles_mod.rotation_from(table0), now, pool=pool,
                         csv_status=salary_statuses(pool), goalie_path=path)
    table = apply_state(table0, rs)  # exactly once per run (B11: a second apply mixes dressing again)
    if overrides:  # C10: accepted, still-valid overrides (validated again here against this state), once, after roles
        from nhl_dfs.models import overrides as overrides_mod

        table, ok, bad = overrides_mod.apply_with_report(table, rs, overrides, now)
        rs = overrides_mod.apply_to_roles(rs, ok, now)
        notes.append(f"overrides: {len(ok)} applied, {len(bad)} no longer valid")
    pp, pnotes = play_probs(table0, table, rs)
    return RoleModel(table0, table, rs, pp, news.state(rs, pool).value, notes + pnotes + table.notes[-1:],
                     list(rs.warnings) + [f"roles note: {p}" for p in problems[:8]])


# -- a field sorted once per contest -------------------------------------------------------------------

class SortedField:
    """(S, F) field scores sorted per scenario once; ranks of any candidate set by binary search, in row
    blocks (a 5,000-lineup field is never re-sorted per repaired entry)."""

    def __init__(self, scores, weights: np.ndarray, block: int = 500):
        """scores: (S, F) array or a lazy objectives.LineupScores / SampledScores, read in row blocks so
        no (S, F) int64 intermediate ever exists (sorted scores and cumulative weights are int32: 160 MB
        for 4,000 scenarios x 5,000 lineups)."""
        S, F = scores.shape
        self.S, self.F, self.block = S, F, block
        if F:
            w = np.asarray(weights, np.int32)
            self.fs = np.empty((S, F), np.int32)
            self.cw = np.zeros((S, F + 1), np.int32)
            for a in range(0, S, block):
                b = min(S, a + block)
                sc = np.asarray(scores[a:b])
                order = np.argsort(sc, axis=1, kind="stable")
                self.fs[a:b] = np.take_along_axis(sc, order, axis=1)
                np.cumsum(w[order], axis=1, out=self.cw[a:b, 1:])
            self.lo, self.hi = int(self.fs.min()), int(self.fs.max())

    def ranks(self, cand: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        S, K = cand.shape
        G = np.zeros((S, K), np.int64)
        E = np.zeros((S, K), np.int64)
        if not self.F:
            return G, E
        lo = min(self.lo, int(cand.min()))
        span = max(self.hi, int(cand.max())) - lo + 1
        F = self.F
        for a in range(0, S, self.block):
            b = min(S, a + self.block)
            rows = np.arange(b - a, dtype=np.int64)[:, None]
            flat = (self.fs[a:b].astype(np.int64) - lo + rows * span).ravel()
            q = (cand[a:b].astype(np.int64) - lo + rows * span).ravel()
            base = rows * F
            right = np.searchsorted(flat, q, side="right").reshape(b - a, K) - base
            left = np.searchsorted(flat, q, side="left").reshape(b - a, K) - base
            cw = self.cw[a:b].astype(np.int64)
            le = np.take_along_axis(cw, right, axis=1)
            G[a:b] = cw[:, F][:, None] - le
            E[a:b] = le - np.take_along_axis(cw, left, axis=1)
        return G, E


def own_terms(cand: np.ndarray, others: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """(G, E) contributions of the user's other entries in the same contest (copies and opponents)."""
    if others is None or not others.shape[1]:
        return 0, 0
    return ((others[:, None, :] > cand[:, :, None]).sum(axis=2), (others[:, None, :] == cand[:, :, None]).sum(axis=2))


# -- the scenario objective ------------------------------------------------------------------------------

@dataclass
class Pick:
    index: int
    figures: dict
    policy: str


class ScenarioObjective:
    kind = "scenario"

    def __init__(self, cache: scache.Loaded, *, pool, work, rm: RoleModel | None, st, started_games, n_use: int,
                 now: datetime, odds_snapshot, live=None, risk_cfg=None, fam_cfg=None, notes: list[str],
                 persist_to=None, referee_rows: tuple[int, int] | None = None):
        """referee_rows: read that block of the cached referee stream instead of its first rows (QA rounds, C10;
        only without re-simulation, rm=None)."""
        from nhl_dfs.models import contests as contests_mod
        from nhl_dfs.sim.score import role_map_for

        self.cache, self.pool, self.mode = cache, pool, pool.mode
        self.risk_cfg = risk_cfg or ob.load_risk_config()
        self.fam_cfg = fam_cfg or contests_mod.load_contest_families()
        self.notes = notes
        self.live = live
        t = time.perf_counter()
        keys_all = sorted(pool.persons)
        self.keys = keys_all
        col = {k: i for i, k in enumerate(keys_all)}
        chunk = int(cache.meta["chunk_size"])
        n = {}
        for purpose in scache.PURPOSES:
            have = cache.n(purpose)
            use = min(int(n_use), have)
            if use < have and use >= chunk:
                use -= use % chunk  # whole chunks, so re-simulated games draw exactly the cached rows
            elif use < have:
                use = have if have <= chunk else chunk
            n[purpose] = use
        start = {"selection": 0, "referee": 0}
        if referee_rows is not None:
            if rm is not None:
                raise ValueError("referee_rows reads cached draws only (rm=None)")
            start["referee"], n["referee"] = int(referee_rows[0]), int(referee_rows[1]) - int(referee_rows[0])
        self.n = n
        changed, self.resim = self._changed_games(rm, work, started_games, now, odds_snapshot)
        self.record: dict[str, Any] = {"cache_run": cache.run_id, "n": n, "games_resimulated": sorted(self.resim),
                                       "games_changed_but_started": sorted(changed - self.resim)}
        out_people = {pool.by_role_id[r].person_key for r, (p, _) in st.items() if p is Participation.OUT}
        if rm is not None:
            play = dict(rm.play_prob)
        elif cache.meta.get("play_prob") is not None:  # the pricing the run's selection used (C10 controller)
            play = {k: float(v) for k, v in cache.meta["play_prob"].items()}
            notes.append(f"cached draws with the run's own participation pricing ({len(play)} person(s))")
        else:  # no role state: C8's rule, DTD at the configured probability
            q = float(self.fam_cfg["selection"]["questionable_play_prob"])
            play = {pool.by_role_id[r].person_key: q for r, (p, _) in st.items() if p is Participation.QUESTIONABLE}
            notes.append(f"scenario objective without a role state: cached draws, DTD masked at {q:g}")
        self.sets = {}
        kept: dict[str, dict] = {}
        from nhl_dfs.sim.score import FLAG_NAMES

        persist = persist_to is not None and self.slate is not None
        carry = persist and cache.flag_names == list(FLAG_NAMES)  # B25, B28: the child keeps the indicators too
        if persist:
            self.record["flags"] = "carried to the child cache" if carry else "none (the parent cache has no indicators)"
        for purpose in scache.PURPOSES:
            base = np.zeros((n[purpose], len(keys_all)), np.int32)
            cached = cache.base(purpose, n[purpose], start[purpose])
            src = [(i, col[k]) for i, k in enumerate(cache.person_keys) if k in col]
            if src:
                a, b = zip(*src)
                base[:, list(b)] = cached[:, list(a)]
            del cached
            fl = None
            if carry:
                cf = cache.flags(purpose, n[purpose], start[purpose])
                if cf is None:
                    carry = False
                    self.record["flags"] = "none (the parent's indicators could not be read: " + "; ".join(cache.notes[-1:]) + ")"
                    kept = {p: {k: v for k, v in rec.items() if k != "flags"} for p, rec in kept.items()}
                else:
                    fl = np.zeros((n[purpose], len(keys_all), cf.shape[2]), bool)
                    if src:
                        fl[:, list(b)] = cf[:, list(a)]
                    del cf
            if self.resim:
                self._splice(base, col, purpose, n[purpose], flags=fl)
            for k in out_people:
                base[:, col[k]] = 0  # ruled OUT since the draws were made: never scores
                if fl is not None:
                    fl[:, col[k]] = False
            if persist:  # refresh: the child run carries the updated draws
                kept[purpose] = scache.save_base(persist_to, purpose, base, n[purpose], chunk, flags=fl)
            del fl
            if play:
                mask = ob.play_mask(n[purpose], keys_all, play, cache.seed, int(cache.meta["purposes"][purpose]["code"]))
                base = np.where(mask, base, 0).astype(np.int32)
            ids, cols = role_map_for(pool, keys_all)
            self.sets[purpose] = ob.ScenarioSet(ids, np.ascontiguousarray(base[:, cols]), purpose, cache.seed)
        self.record["participation_masked"] = len(play)
        if len(kept) == len(scache.PURPOSES):
            info = scache.save(persist_to, purposes=kept, person_keys=keys_all, slate=self.slate, params=self.table,
                               seed=cache.seed, chunk_size=chunk, contests=cache.contests,
                               contest_family=cache.contest_family, fields=cache.families, n_opponents=cache.n_opponents,
                               own_by=cache.own_by, dup_by=cache.dup_by,
                               field_cal=cache.meta.get("field_calibration", "PRIOR"),
                               model_status=self.table.source().value, play_prob=play,
                               participation=scache.participation_record(self.table, play, rm.roles if rm is not None else None))
            self.record["persisted"] = info
        self.record["build_s"] = round(time.perf_counter() - t, 3)
        self._fields: dict[tuple[str, str], tuple[SortedField, ob.FieldSpec]] = {}
        self.linear = self.surrogate()
        self.field_cal = FieldCalibration(cache.meta.get("field_calibration", "PRIOR"))

    # games -------------------------------------------------------------------------------------------

    def _changed_games(self, rm, work, started_games, now, snapshot):
        from nhl_dfs.sim import market
        from nhl_dfs.sim.slate import build_slate

        cached = self.cache.meta["game_sha256"]
        if rm is None:
            self.notes.append("games not re-simulated: no ParamTable in this run, so changes cannot be detected")
            self.slate, self.table = None, None
            return set(), set()
        slate, lines = build_slate(work, rm.table, snapshot, now=now, confirmed_at=rm.roles.confirmed_at())
        reused = []
        for g in slate.games:  # odds were not re-fetched: keep the parent's MARKET price rather than drop to MODEL
            d = self.cache.meta.get("game_rates", {}).get(g.key)
            if snapshot is None and d and d.get("source") == "MARKET" and g.rates.source != "MARKET":
                g.rates = market.GameRates(**{**d, "notes": tuple(d.get("notes") or ())})
                reused.append(g.key)
        if reused:
            self.notes.append("odds not re-fetched: the parent's MARKET rates reused for " + ", ".join(reused))
        self.slate, self.table = slate, rm.table
        self.game_sources = {g.key: g.rates.source + (" STALE" if g.rates.stale else "") for g in slate.games}
        new = scache.game_hashes(slate, rm.table)
        changed = {k for k, h in new.items() if cached.get(k) != h}
        started = {k for k in changed if k in started_games}
        return changed, changed - started

    def _splice(self, base: np.ndarray, col: dict, purpose: str, n: int, flags: np.ndarray | None = None) -> None:
        """Replace the re-simulated games' columns of base (and of flags, the (n, P, F) indicators, when given)."""
        from nhl_dfs.sim import game, score

        teams = {c for g in self.slate.games if g.key in self.resim for c in (g.home, g.away)}
        for k in self.keys:
            rows = self.pool.persons[k]
            if (rows.classic or rows.flex or rows.cpt).team in teams:
                base[:, col[k]] = 0
                if flags is not None:
                    flags[:, col[k]] = False
        if getattr(self, "_prep", None) is None:  # one preparation (assist pilot included) for both streams
            self._prep = game.prepare(self.slate, self.table, self.resim)
            game.check_memory(self._prep, self.slate.cfg)
        parts, fparts = [], []
        for ci, size in enumerate(game.chunk_bounds(n, self.slate.cfg)):
            o = game.simulate_chunk(self._prep, size, self.cache.seed, purpose, ci, self.resim)
            parts.append(score.base_tenths(o))
            if flags is not None:
                fparts.append(score.bonus_flags(o))
        new = np.concatenate(parts, axis=0)
        newf = np.concatenate(fparts, axis=0) if flags is not None else None
        for j, k in enumerate(sorted(self.table.persons)):
            if k in col and self.table.persons[k].team in teams:
                base[:, col[k]] = new[:, j]
                if flags is not None:
                    flags[:, col[k]] = newf[:, j]

    def surrogate(self) -> dict[str, float]:
        """The MILP's linear objective: scenario mean points per role (Captain at 1.5x); it only proposes
        candidates, the family objective chooses among them."""
        from nhl_dfs.build.portfolio import role_objective

        return role_objective(self.sets["selection"], self.pool)

    # scoring -----------------------------------------------------------------------------------------

    def _field(self, cid: str, purpose: str):
        key = (cid, purpose)
        if key not in self._fields:
            self._fields.clear()  # bound memory (B16): one sorted field alive at a time; targets come sorted by contest
            fam = self.cache.contest_family[cid]
            lus, ks = self.cache.families[fam]
            scen = self.sets[purpose]
            spec = ob.field_spec(lus, ks, self.cache.n_opponents[cid], self.risk_cfg, n_scenarios=scen.n,
                                 seed=self.cache.seed, salt=f"{cid}|{purpose}")
            scores, weights = spec.scores(scen, self.mode)
            self._fields[key] = (SortedField(scores, weights), spec)
        return self._fields[key]

    def has_contest(self, cid: str) -> bool:
        return cid in self.cache.contests and cid in self.cache.contest_family

    def policy(self, entry_id: str, cid: str) -> str:
        fam = self.cache.contests[cid].family
        base = self.fam_cfg["families"][fam]["selection"]
        if self.live is not None and fam not in ("cash", "satellite"):  # C8: cash and satellite ignore ownership
            state = self.live.entry_state.get(entry_id)
            if state == "TRAILING":
                return "dup_first"
            if state == "AHEAD":
                return "mean"
        return base

    def metrics(self, cid: str, lineups: list, others: list, purpose: str) -> ob.Metrics:
        scen = self.sets[purpose]
        sf, _ = self._field(cid, purpose)
        cand = scen.scores(lineups, self.mode).full()
        oth = scen.scores(others, self.mode).full() if others else None
        G, E = sf.ranks(cand)
        g2, e2 = own_terms(cand, oth)
        return ob.metrics_from_ranks(G + g2, E + e2, self.cache.contests[cid], self.risk_cfg)

    def joint(self, cid: str, lineups: list, purpose: str) -> ob.Metrics:
        """All of the user's entries in one contest ranked together (each entry's finish counts the field and
        every other own entry), one field sort per contest: column j is lineups[j] (C10 controller)."""
        scen = self.sets[purpose]
        sf, _ = self._field(cid, purpose)
        own = scen.scores(lineups, self.mode).full()
        G, E = sf.ranks(own)
        pg, pe = ob.own_pairwise(own)
        return ob.metrics_from_ranks(G + pg, E + pe, self.cache.contests[cid], self.risk_cfg)

    def choose(self, entry_id: str, cid: str, cands: list[list[str]], others: list[list[str]]) -> Pick:
        m = self.metrics(cid, cands, others, "selection")
        own = self.cache.own_by.get(cid, {})
        counts = self.cache.dup_by.get(cid, {})
        items = []
        for k, lu in enumerate(cands):
            key = lineup_key([self.pool.by_role_id[r] for r in lu], self.mode)
            dup, _ = tiebreak.dup_measure(lu, key, own, counts, self.field_cal, self.pool)
            items.append(tiebreak.Item(key, float(m.objective[k]), float(m.objective_se[k]),
                                       sum(float(own.get(r, 0.0)) for r in lu), float(dup), k))
        policy = self.policy(entry_id, cid)
        top = tiebreak.rank(items, self.risk_cfg, policy)[0]
        k = int(top.item.ref)
        return Pick(k, {**m.row(k), "own_sum_pct": round(top.item.own, 2), "dup": round(top.item.dup, 3),
                        "band_index": top.band_index, "candidates": len(cands), "policy": policy,
                        "measured_on": "selection scenarios (the choosing draws)"}, policy)

    def referee(self, by_contest: dict[str, list[tuple[str, list[str]]]], all_by_contest: dict[str, dict[str, list[str]]]) -> dict[str, dict]:
        """Figures for each changed entry on the referee stream (independent of the choosing draws)."""
        out = {}
        for cid, rows in by_contest.items():
            if not self.has_contest(cid):
                continue
            for eid, lu in rows:
                others = [x for e, x in all_by_contest.get(cid, {}).items() if e != eid and all(x)]
                m = self.metrics(cid, [lu], others, "referee")
                out[eid] = {**m.row(0), "measured_on": "referee scenarios"}
        return out


def _peak():
    from nhl_dfs.build.scenario_pass import peak_mb

    return peak_mb()


# -- resolution ------------------------------------------------------------------------------------------

@dataclass
class Resolved:
    kind: str
    requested: str
    linear: dict[str, float]
    scenario: ScenarioObjective | None = None
    role_model: RoleModel | None = None
    fallbacks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def record(self) -> dict:
        r = {"kind": self.kind, "requested": self.requested, "fallbacks": self.fallbacks, "notes": self.notes,
             "timings": self.timings}
        if self.scenario is not None:
            r["scenario"] = self.scenario.record
        return r


def baseline_linear(pool) -> dict[str, float]:
    from nhl_dfs.models.priors import prior_objective, prior_table

    return prior_objective(pool, prior_table(pool))


def provisional_linear(pool, rm: RoleModel) -> dict[str, float]:
    from nhl_dfs.models.projection import objective

    obj = objective(pool, rm.table)
    return {r: v * rm.play_prob.get(pool.by_role_id[r].person_key, 1.0) for r, v in obj.items()}


def resolve(requested: str, *, pool, work, st, started_games, dk_rec, runs_root, run_id: str, offline: bool, cache,
            clock, now: datetime, runtime: dict, fast: bool, optional_ok: tuple[bool, str], live=None,
            entry_ids=(), odds_snapshot=None, apply_state: Callable | None = None, persist_to=None,
            overrides: list | None = None, goalie_inputs: tuple | None = None, score_pool=None) -> Resolved:
    """Try the objectives in the documented order from `requested`; never raises. score_pool (C15): the pool whose
    role rows the scenario scorer maps, `pool` plus the rows of games in progress, so a lineup that holds a pinned
    started-game player is scored (default: `pool`)."""
    if requested not in ORDER:
        raise ValueError(f"objective must be one of auto, scenario, provisional, baseline (got {requested!r})")
    cfg = runtime.get("late_swap", {})
    res = Resolved("baseline", requested, {})
    order = ORDER[requested]
    ok, why_not = optional_ok
    t0 = time.perf_counter()

    def down(kind: str, reason: str):
        res.fallbacks.append({"from": kind, "reason": reason})

    if not ok:
        for kind in order:
            if kind != "baseline":
                down(kind, why_not)
        order = ("baseline",)
    rm = None
    if any(k in order for k in ("scenario", "provisional")):
        t = time.perf_counter()
        try:
            rm = build_role_model(pool, work, st, dk_rec=dk_rec, now=now, clock=clock, offline=offline, cache=cache,
                                  budget_s=float(runtime.get("network_pass_budget_s", 25)), apply_state=apply_state,
                                  overrides=overrides, goalie_inputs=goalie_inputs)
            res.role_model = rm
            res.notes += rm.notes
        except Exception as exc:  # reported; the objective steps down
            res.notes.append(f"ParamTable or role state unavailable ({type(exc).__name__}: {str(exc)[:120]})")
        res.timings["roles_s"] = round(time.perf_counter() - t, 3)
        res.timings["roles_peak_mb"] = _peak()
    for kind in order:
        if kind == "scenario":
            got, looked = scache.find(runs_root, run_id)
            if got is None:
                down("scenario", "no scenario cache on the run or its parents (" + "; ".join(looked) + ")")
                continue
            t = time.perf_counter()
            try:
                n_use = int(cfg.get("fast_scenarios", 4000) if fast else cfg.get("full_scenarios", 8000))
                notes: list[str] = []
                so = ScenarioObjective(got, pool=score_pool or pool, work=work, rm=rm, st=st, started_games=started_games, n_use=n_use,
                                       now=now, odds_snapshot=odds_snapshot, live=None, notes=notes,
                                       persist_to=persist_to)
                if live is not None:
                    from nhl_dfs.build import live as live_mod

                    cond = live_mod.condition(so.sets["selection"], live, pool=score_pool or pool, contests=got.contests, now=now,
                                              needed_entries=entry_ids,
                                              max_age_min=float(runtime.get("live", {}).get("max_age_min", 15)))
                    notes += cond.notes
                    so.record["live"] = cond.record()
                    if cond.conditioned:  # an absent or unreliable snapshot changes nothing at all
                        so.live = cond
                        so.sets["selection"] = cond.scenarios
                        so.linear = so.surrogate()
                res.scenario, res.kind, res.linear = so, "scenario", so.linear
                res.notes += notes
                res.timings["scenario_build_s"] = round(time.perf_counter() - t, 3)
                res.timings["scenario_build_peak_mb"] = _peak()
                break
            except Exception as exc:
                down("scenario", f"scenario objective failed ({type(exc).__name__}: {str(exc)[:160]})")
                continue
        if kind == "provisional":
            if rm is None:
                down("provisional", "no ParamTable (see notes)")
                continue
            res.kind, res.linear = "provisional", provisional_linear(work, rm)
            break
        if kind == "baseline":
            res.kind, res.linear = "baseline", baseline_linear(pool)
            break
    res.timings["resolve_s"] = round(time.perf_counter() - t0, 3)
    return res


def optional_work_ok(ls, pool, targets_games: set[str], now: datetime, runtime: dict, llm: bool = False) -> tuple[bool, str]:
    """Plan section 13: optional work stops at T-8 (LLM paths) or T-5 (engine-only) before the next lock
    that matters, here the earliest start among games with an open cell to repair. Separate from the
    edit-stop buffer (lock semantics unchanged): this only chooses a cheaper objective."""
    cfg = runtime.get("late_swap", {})
    stop_min = float((cfg.get("optional_work_stop_min") or {}).get("llm" if llm else "engine", 8 if llm else 5))
    est = float(cfg.get("optional_estimate_s", 25))
    starts = [g.start_utc for key, g in pool.games.items() if key in targets_games]
    if not starts:
        return True, ""
    left = (min(starts) - now).total_seconds() - stop_min * 60.0
    if left < est:
        return False, (f"optional work stop: T-{stop_min:g} before the next open game leaves {max(0.0, left):.0f}s, "
                       f"less than the {est:g}s the scenario/provisional objective needs")
    return True, ""
