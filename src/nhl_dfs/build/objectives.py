"""Scenario objectives by contest family (C8, plan section 7 "Define the objectives correctly").

Every candidate and every opponent lineup is scored in the SAME simulated scenarios (sim/score.py,
integer twentieths). In each scenario a lineup's finish is read off the weighted opponent field
plus the user's own other entries in that contest (own copies count as copies):

    G = weight strictly above the score, E = weight tied with it, T = E + 1 (the entry itself)
    the entry occupies positions G+1 .. G+T; those positions' prizes are pooled and divided by T,
    rounded down to the cent. DK's Terms of Use (docs/payouts.md, checked 2026-09-29) verify only
    the even split; the pooling and the round-down are the plan's assumption ([BEN] flag 9).

The division is integer cents with floor division, which equals Decimal ROUND_DOWN for the
non-negative prizes here (`split_tie` is the Decimal reference; tests compare the two).

Field representation: at most 5,000 distinct sampled lineups (config `objectives.max_distinct`).
A large field uses integer multiplicity weights (largest remainder) summing to the opponents'
count (field size minus the user's entries in that contest). A field with fewer opponents than
`sampled_max_opponents` instead draws its actual opponents per scenario from the sampled lineups
(seeded), because a 9-opponent WTA cannot be represented by fractional weights.

Family objectives (the metric selection maximizes, higher is better):
    large_gpp    exp_payout_top1pct  expected payout from finishes inside the top 1% ($)
    small_field  exp_payout          expected payout under the full curve ($)
    wta          first_place_equity  tie-adjusted share of first place
    cash         p_clear_line        tie-adjusted probability of finishing inside the cash line
    satellite    p_seat              tie-adjusted seats won; the ticket's face value is kept apart
Every figure is a scenario estimate: it carries its Monte Carlo standard error, PAYOUT_SOURCE,
OUTCOME_CALIBRATION and FIELD_CALIBRATION, and a PRIOR payout makes it an uncalibrated scenario
proxy, never a measured fact.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import yaml

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import PayoutSource

REPO_ROOT = Path(__file__).resolve().parents[3]
RISK_YAML = REPO_ROOT / "config" / "risk.yaml"
MASK_STREAM = 7  # SeedSequence stream id for the participation mask (sim purposes are 0, 1, 2)
FAMILY_OBJECTIVE = {
    "large_gpp": "exp_payout_top1pct",
    "small_field": "exp_payout",
    "wta": "first_place_equity",
    "cash": "p_clear_line",
    "satellite": "p_seat",
}


def load_risk_config(path: Path = RISK_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_risk_config(cfg)
    return cfg


def validate_risk_config(cfg: dict) -> None:
    b = cfg.get("budget") or {}
    for mode in ("classic", "showdown"):
        m = b.get(mode) or {}
        for k in ("p_lose80_max", "goalie_fee_share_max", "game_fee_share_max", "captain_fee_share_max"):
            v = m.get(k)
            if v is not None and not 0 < float(v) <= 1:
                raise ValueError(f"risk.yaml budget.{mode}.{k} must be in (0, 1] or null")
    ks = (cfg.get("frontier") or {}).get("kappas") or []
    if len(ks) != 5 or any(float(k) < 0 for k in ks):
        raise ValueError("risk.yaml frontier.kappas must list five non-negative knob settings")
    o = cfg.get("objectives") or {}
    if not 0 < float(o.get("top_pct", 0)) < 1:
        raise ValueError("risk.yaml objectives.top_pct must be in (0, 1)")
    if int(o.get("max_distinct", 0)) < 1 or int(o.get("max_distinct", 0)) > 5000:
        raise ValueError("risk.yaml objectives.max_distinct must be in [1, 5000]")


# -- exact tie arithmetic --------------------------------------------------------------------------

def split_tie(prizes: Sequence[Decimal], tied: int) -> Decimal:
    """DK tie rule, the Decimal reference: the tied positions' prizes pooled, divided equally among
    the tied entries, each share rounded down to the cent."""
    if tied < 1:
        raise ValueError("tied must be >= 1")
    total = sum((Decimal(p) for p in prizes), Decimal("0"))
    return (total / Decimal(tied)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def to_cents(x) -> int:
    return int((Decimal(str(x)) * 100).quantize(Decimal("1"), rounding=ROUND_DOWN))


# -- contests and payout curves --------------------------------------------------------------------

@dataclass(frozen=True)
class Contest:
    """One contest's payout curve in cents. prizes_cents[k] is position k+1's cash prize; seats[k]
    marks a position that awards a ticket (satellite seat)."""

    contest_id: str
    name: str
    family: str
    field_size: int
    fee_cents: int
    prizes_cents: np.ndarray  # int64 (L,), L = paid positions (cash or ticket)
    seats: np.ndarray  # bool (L,)
    ticket_face_cents: int | None
    payout_source: PayoutSource
    detail: str = ""

    @property
    def paid(self) -> int:
        return int(len(self.prizes_cents))

    @property
    def cash_line(self) -> int:
        """Positions 1..cash_line are paid cash (the line p_clear_line is measured against)."""
        nz = np.nonzero(self.prizes_cents > 0)[0]
        return int(nz[-1] + 1) if len(nz) else 0

    @property
    def n_seats(self) -> int:
        return int(self.seats.sum())

    def prefix(self, values: np.ndarray) -> np.ndarray:
        """Length field_size + 1: index k = total over positions 1..k."""
        out = np.zeros(self.field_size + 1, np.int64)
        k = min(len(values), self.field_size)
        out[1:k + 1] = np.cumsum(np.asarray(values[:k], np.int64))
        out[k + 1:] = out[k]
        return out

    def record(self) -> dict:
        return {"contest_id": self.contest_id, "family": self.family, "field_size": self.field_size,
                "fee": self.fee_cents / 100, "paid_positions": self.paid, "cash_line": self.cash_line,
                "first_prize": int(self.prizes_cents[0]) / 100 if self.paid else 0.0,
                "prize_pool": int(self.prizes_cents.sum()) / 100, "seats": self.n_seats,
                "ticket_face": None if self.ticket_face_cents is None else self.ticket_face_cents / 100,
                "PAYOUT_SOURCE": self.payout_source.value, "detail": self.detail}


def _flat(n_paid: int, pool_cents: int) -> np.ndarray:
    each = pool_cents // max(1, n_paid)
    return np.full(n_paid, each, np.int64)


def _power(n_paid: int, pool_cents: int, fee_cents: int, min_mult: float, exponent: float) -> np.ndarray:
    """Top-heavy prior: prize_k = min_cash + extra * (k^-a - n^-a), summing to the pool; the last paid
    position gets exactly min_cash. Flat when the pool cannot pay min_cash to every paid position."""
    min_cash = int(round(fee_cents * min_mult))
    if n_paid <= 1 or pool_cents <= min_cash * n_paid:
        return _flat(n_paid, pool_cents)
    k = np.arange(1, n_paid + 1, dtype=float)
    w = k ** -exponent - float(n_paid) ** -exponent
    extra = (pool_cents - min_cash * n_paid) * w / w.sum()
    out = (min_cash + np.floor(extra)).astype(np.int64)
    out[0] += pool_cents - int(out.sum())  # rounding remainder to first place
    return out


def prior_curve(family: str, field_size: int, fee_cents: int, fam_cfg: dict) -> tuple[np.ndarray, np.ndarray, int | None, str]:
    """Declared family prior (config/contest_families.yaml payout_priors): (prizes, seats, face, detail)."""
    pp = fam_cfg["payout_priors"]
    rake = float(pp["rake"])
    spec = pp["families"][family]
    pool = int(math.floor(fee_cents * field_size * (1.0 - rake)))
    frac = float(fam_cfg["families"][family]["paid_fraction_prior"])
    n_paid = 1 if spec["shape"] == "wta" else max(1, min(field_size, int(math.floor(frac * field_size))))
    detail = f"PRIOR {family}: rake {rake:g}, {n_paid} of {field_size} paid, shape {spec['shape']}"
    if spec["shape"] == "tickets":
        face = pool // n_paid
        return np.zeros(n_paid, np.int64), np.ones(n_paid, bool), face, detail + f", ticket face {face / 100:.2f} (pool / seats)"
    if spec["shape"] in ("flat", "wta"):
        prizes = _flat(n_paid, pool)
    else:
        prizes = _power(n_paid, pool, fee_cents, float(spec["min_cash_mult"]), float(spec["exponent"]))
    return prizes, np.zeros(n_paid, bool), None, detail


_MONEY = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def exact_curve(detail) -> tuple[np.ndarray, np.ndarray, int | None, str]:
    """From a DK ContestDetail payout table. Tier cash is per position (checked against the prize pool
    in tests/test_objectives.py). A tier with non-cash text is a ticket; its face value is read from
    a dollar amount in that text when one is written, else it is missing (None)."""
    tiers = sorted(detail.payout_table, key=lambda t: t.min_pos)
    n = max((t.max_pos for t in tiers), default=0)
    prizes = np.zeros(n, np.int64)
    seats = np.zeros(n, bool)
    faces = set()
    for t in tiers:
        a, b = int(t.min_pos) - 1, int(t.max_pos)
        prizes[a:b] = to_cents(t.cash)
        if t.other:
            seats[a:b] = True
            m = _MONEY.search(t.other)
            faces.add(to_cents(m.group(1).replace(",", "")) if m else None)
    face = next(iter(faces)) if len(faces) == 1 else None
    note = "EXACT from DK contest detail" + ("; ticket face value missing" if seats.any() and face is None else "")
    return prizes, seats, face, note


def contest_from(ctx, fam_cfg: dict) -> Contest:
    """A models.contests.ContestContext -> Contest with its payout curve (EXACT or declared PRIOR)."""
    from nhl_dfs.models.contests import fee_value

    fee = fee_value(ctx.fee)
    fee_cents = to_cents(fee) if fee is not None else 0
    if ctx.payout_source is PayoutSource.EXACT:
        prizes, seats, face, note = exact_curve(ctx.detail)
    else:
        prizes, seats, face, note = prior_curve(ctx.family, int(ctx.field_size), fee_cents, fam_cfg)
    if ctx.family == "satellite" and not seats.any():
        seats = prizes > 0  # an exact satellite described only in cash: its paid positions are the seats
    return Contest(str(ctx.contest_id), ctx.name, ctx.family, int(ctx.field_size), fee_cents, prizes, seats, face,
                   ctx.payout_source, note)


# -- scenarios --------------------------------------------------------------------------------------

def play_mask(n: int, person_keys: Sequence[str], play_prob: Mapping[str, float], seed: int, purpose_code: int) -> np.ndarray:
    """(n, P) bool: False where a person with play probability < 1 sits out that scenario. Its own
    seed stream, so the simulator's draws are untouched and every lineup sees the same mask."""
    mask = np.ones((n, len(person_keys)), bool)
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(seed), MASK_STREAM, int(purpose_code)])))
    for j, k in enumerate(person_keys):  # person axis order, so the draws do not depend on dict order
        p = play_prob.get(k)
        if p is not None and p < 1.0:
            mask[:, j] = rng.random(n) < float(p)
    return mask


@dataclass
class ScenarioSet:
    """Base DK scores per role row in shared scenarios: (S, R) int32 tenths, participation applied."""

    role_ids: list[str]
    base: np.ndarray
    purpose: str
    seed: int
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.col = {r: i for i, r in enumerate(self.role_ids)}

    @property
    def n(self) -> int:
        return int(self.base.shape[0])

    def cols(self, lineups: Sequence[Sequence[str]]) -> np.ndarray:
        return np.asarray([[self.col[r] for r in lu] for lu in lineups], dtype=np.int64).reshape(len(lineups), -1)

    def scores(self, lineups: Sequence[Sequence[str]], mode: Mode) -> "LineupScores":
        return LineupScores(self.base, self.cols(lineups), 0 if mode is Mode.SHOWDOWN else None)

    def means_tenths(self) -> dict[str, float]:
        m = self.base.mean(axis=0)
        return {r: float(m[i]) for i, r in enumerate(self.role_ids)}


def scenario_set(base_person: np.ndarray, person_keys: Sequence[str], pool, *, purpose: str, seed: int,
                 purpose_code: int, play_prob: Mapping[str, float] | None = None) -> ScenarioSet:
    """Role-row scores from the simulator's per-person base scores. play_prob: person_key -> probability
    of playing given the simulator's own dressing (DTD / QUESTIONABLE today; RoleState.p_play later).
    It is applied once, here, to every lineup alike; the missing player's minutes are NOT handed to
    his teammates (a labeled simplification)."""
    from nhl_dfs.sim.score import role_map_for

    notes = []
    base = np.asarray(base_person)
    if play_prob:
        mask = play_mask(base.shape[0], person_keys, play_prob, seed, purpose_code)
        base = np.where(mask, base, 0).astype(np.int32)
        sat = {k: float(1.0 - mask[:, j].mean()) for j, k in enumerate(person_keys) if k in play_prob}
        notes.append(f"participation mask on {len(sat)} person(s) (play probability "
                     + ", ".join(sorted({f'{play_prob[k]:g}' for k in sat})) + "); minutes not reallocated")
    ids, cols = role_map_for(pool, person_keys)
    return ScenarioSet(ids, np.ascontiguousarray(base[:, cols]), purpose, int(seed), notes)


class LineupScores:
    """Lazy (S, L) int32 twentieths: rows are computed per slice, so a 5,000-lineup field over 20,000
    scenarios never exists as one 400 MB array. Captain applied exactly once (sim/score.py rule)."""

    def __init__(self, base: np.ndarray, cols: np.ndarray, captain_col: int | None):
        self.base, self.cols, self.captain_col = base, cols, captain_col
        self.shape = (base.shape[0], cols.shape[0])

    def __getitem__(self, sl) -> np.ndarray:
        from nhl_dfs.sim.score import lineup_twentieths

        if not isinstance(sl, slice):
            raise TypeError("LineupScores supports row slices only")
        return lineup_twentieths(self.base[sl], self.cols, self.captain_col)

    def full(self) -> np.ndarray:
        return self[0:self.shape[0]]


# -- the opponent field -----------------------------------------------------------------------------

def largest_remainder(counts: Sequence[float], total: int) -> np.ndarray:
    c = np.asarray(counts, float)
    if total <= 0 or c.sum() <= 0:
        return np.zeros(len(c), np.int64)
    raw = total * c / c.sum()
    base = np.floor(raw).astype(np.int64)
    order = sorted(range(len(c)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[: total - int(base.sum())]:
        base[i] += 1
    return base


@dataclass
class FieldSpec:
    """Opponents for one contest. mode 'weighted': distinct lineups with integer weights summing to
    n_opponents. mode 'sampled': per scenario, `draws[s]` indexes the n_opponents actual opponents."""

    lineups: list[tuple[str, ...]]
    keys: list[str]
    counts: np.ndarray  # sampled draws per distinct lineup (the field model's multiplicity)
    n_opponents: int
    mode: str
    weights: np.ndarray | None = None
    draws: np.ndarray | None = None
    detail: str = ""

    def scores(self, scen: ScenarioSet, pool_mode: Mode) -> tuple["LineupScores | np.ndarray", np.ndarray]:
        """(field scores (S, F), weights (F,)) for contest_metrics."""
        distinct = scen.scores(self.lineups, pool_mode)
        if self.mode == "weighted":
            return distinct, self.weights
        full = distinct.full()
        return np.take_along_axis(full, self.draws, axis=1), np.ones(self.n_opponents, np.int64)


def field_spec(lineups: Sequence[Sequence[str]], keys: Sequence[str], n_opponents: int, cfg: dict, *, n_scenarios: int,
               seed: int, salt: str) -> FieldSpec:
    """From a sampled field (models.field.Field lineups and keys, draws with replacement)."""
    import zlib

    o = cfg["objectives"]
    order: dict[str, int] = {}
    distinct: list[tuple[str, ...]] = []
    counts: list[int] = []
    for lu, k in zip(lineups, keys):
        if k not in order:
            order[k] = len(distinct)
            distinct.append(tuple(lu))
            counts.append(0)
        counts[order[k]] += 1
    cap = int(o["max_distinct"])
    idx = sorted(range(len(distinct)), key=lambda i: (-counts[i], i))[:cap]
    idx.sort()
    distinct = [distinct[i] for i in idx]
    kk = [list(order)[i] for i in idx]
    cnt = np.asarray([counts[i] for i in idx], np.int64)
    n_opp = max(0, int(n_opponents))
    if n_opp <= int(o["sampled_max_opponents"]):
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(seed), MASK_STREAM + 1,
                                                                           zlib.crc32(salt.encode())])))
        p = cnt / cnt.sum()
        draws = rng.choice(len(distinct), size=(int(n_scenarios), n_opp), p=p) if n_opp else np.zeros((n_scenarios, 0), np.int64)
        return FieldSpec(distinct, kk, cnt, n_opp, "sampled", None, draws,
                         f"{n_opp} opponents drawn per scenario from {len(distinct)} distinct sampled lineups")
    w = largest_remainder(cnt, n_opp)
    return FieldSpec(distinct, kk, cnt, n_opp, "weighted", w, None,
                     f"{len(distinct)} distinct sampled lineups, integer weights summing to {n_opp} opponents")


# -- metrics ----------------------------------------------------------------------------------------

@dataclass
class Metrics:
    contest_id: str
    family: str
    n_scenarios: int
    objective_name: str
    objective: np.ndarray  # (K,) the family metric (higher is better)
    objective_se: np.ndarray
    exp_payout: np.ndarray  # (K,) dollars
    exp_payout_se: np.ndarray
    exp_payout_top1pct: np.ndarray
    exp_payout_top1pct_se: np.ndarray
    p_cash: np.ndarray
    p_top1pct: np.ndarray
    p_top1pct_se: np.ndarray
    first_place_equity: np.ndarray
    first_place_equity_se: np.ndarray
    p_clear_line: np.ndarray
    p_clear_line_se: np.ndarray
    p_seat: np.ndarray
    p_seat_se: np.ndarray
    exp_ticket_value: np.ndarray  # dollars; tickets are not cash (kept out of payout_cents)
    payout_cents: np.ndarray  # (S, K) int32 cash per scenario
    utility_cents: np.ndarray  # (S, K) int32 family utility per scenario (the dollars the family objective values)
    top_k: int = 0

    def row(self, k: int) -> dict:
        """JSON-ready figures for candidate k (dollars and probabilities with their standard errors)."""
        def r(x, nd=4):
            return round(float(x), nd)
        return {"objective": self.objective_name, "value": r(self.objective[k]), "se": r(self.objective_se[k]),
                "exp_payout": r(self.exp_payout[k]), "exp_payout_se": r(self.exp_payout_se[k]),
                "p_cash": r(self.p_cash[k]), "p_top1pct": r(self.p_top1pct[k]), "p_top1pct_se": r(self.p_top1pct_se[k]),
                "first_place_equity": r(self.first_place_equity[k], 5), "first_place_equity_se": r(self.first_place_equity_se[k], 5),
                "p_clear_line": r(self.p_clear_line[k]), "p_clear_line_se": r(self.p_clear_line_se[k]),
                "p_seat": r(self.p_seat[k]), "p_seat_se": r(self.p_seat_se[k]),
                "exp_ticket_value": r(self.exp_ticket_value[k]), "top1pct_positions": self.top_k}


def _chunk_rows(n: int, per_row_bytes: int, cap_mb: float) -> int:
    return max(1, min(n, int(cap_mb * 1e6 // max(1, per_row_bytes))))


def ranks(cand: np.ndarray, field: np.ndarray, weights: np.ndarray, own: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """One scenario block. cand (c, K), field (c, F), own (c, J) or None. Returns (G, E): weight strictly
    above and weight tied with each candidate, own copies counted with weight 1."""
    c, K = cand.shape
    F = field.shape[1]
    G = np.zeros((c, K), np.int64)
    E = np.zeros((c, K), np.int64)
    if F:
        w = np.asarray(weights, np.int64)
        order = np.argsort(field, axis=1, kind="stable")
        fs = np.take_along_axis(field, order, axis=1).astype(np.int64)
        cw = np.zeros((c, F + 1), np.int64)
        np.cumsum(w[order], axis=1, out=cw[:, 1:])
        lo = min(int(fs.min()), int(cand.min()))
        span = max(int(fs.max()), int(cand.max())) - lo + 1
        rows = np.arange(c, dtype=np.int64)[:, None]
        flat = (fs - lo + rows * span).ravel()
        q = (cand.astype(np.int64) - lo + rows * span).ravel()
        base = rows * F
        right = np.searchsorted(flat, q, side="right").reshape(c, K) - base
        left = np.searchsorted(flat, q, side="left").reshape(c, K) - base
        le = np.take_along_axis(cw, right, axis=1)
        G = cw[:, F][:, None] - le
        E = le - np.take_along_axis(cw, left, axis=1)
    if own is not None and own.shape[1]:
        G += (own[:, None, :] > cand[:, :, None]).sum(axis=2)
        E += (own[:, None, :] == cand[:, :, None]).sum(axis=2)
    return G, E


def _se(x: np.ndarray, n: int) -> np.ndarray:
    return x.std(axis=0) / math.sqrt(max(1, n))


def contest_metrics(cand, field, weights, contest: Contest, own_copies=None, *, cfg: dict | None = None) -> Metrics:
    """cand (S, K) and field (S, F <= 5,000) in twentieths from the same scenarios (arrays or
    LineupScores); weights (F,) integer multiplicities; own_copies (S, J) scores of the user's OTHER
    entries in this contest (or None). Chunked by scenario within objectives.memory_cap_mb."""
    cfg = cfg if cfg is not None else load_risk_config()
    S, K = cand.shape
    J = 0 if own_copies is None else own_copies.shape[1]

    def block(a: int, b: int):
        return ranks(np.asarray(cand[a:b]), np.asarray(field[a:b]), weights,
                     None if own_copies is None else np.asarray(own_copies[a:b]))
    return _evaluate(S, K, field.shape[1] * 28 + K * (J + 96), block, contest, cfg)


def joint_payouts(own, field, weights, contest: Contest, *, cfg: dict | None = None) -> Metrics:
    """All of the user's entries in one contest ranked together: own (S, J) scores; each entry's
    finish counts the field and every OTHER own entry (they are opponents of each other)."""
    cfg = cfg if cfg is not None else load_risk_config()
    S, J = own.shape

    def block(a: int, b: int):
        o = np.asarray(own[a:b])
        G, E = ranks(o, np.asarray(field[a:b]), weights)
        G += (o[:, None, :] > o[:, :, None]).sum(axis=2)
        E += (o[:, None, :] == o[:, :, None]).sum(axis=2) - 1  # every other own entry tied with it
        return G, E
    return _evaluate(S, J, field.shape[1] * 28 + J * (J + 96), block, contest, cfg)


def _evaluate(S: int, K: int, per_row_bytes: int, block, contest: Contest, cfg: dict) -> Metrics:
    o = cfg["objectives"]
    L = contest.field_size
    cash = contest.prefix(contest.prizes_cents)
    seatp = contest.prefix(contest.seats.astype(np.int64))
    top_k = max(1, int(math.floor(float(o["top_pct"]) * L)))
    line = contest.cash_line
    first = int(contest.prizes_cents[0]) if contest.paid else 0
    face = contest.ticket_face_cents or 0
    step = _chunk_rows(S, per_row_bytes, float(o["memory_cap_mb"]))
    pay = np.zeros((S, K), np.int32)
    util = np.zeros((S, K), np.int32)
    topd = np.zeros((S, K), np.int32)
    top = np.zeros((S, K), np.float32)
    eq = np.zeros((S, K), np.float32)
    clear = np.zeros((S, K), np.float32)
    seat = np.zeros((S, K), np.float32)
    fam = contest.family
    for a in range(0, S, step):
        b = min(S, a + step)
        G, E = block(a, b)
        T = E + 1
        hi = np.minimum(G + T, L)
        lo = np.minimum(G, L)
        share = (cash[hi] - cash[lo]) // T  # pooled over the tied positions, rounded down to the cent
        pay[a:b] = share
        topd[a:b] = (cash[np.minimum(G + T, top_k)] - cash[np.minimum(G, top_k)]) // T
        top[a:b] = np.clip(top_k - G, 0, T) / T
        eq[a:b] = (G == 0) / T
        clear[a:b] = np.clip(line - G, 0, T) / T
        seat[a:b] = (seatp[hi] - seatp[lo]) / T
        if fam == "large_gpp":
            util[a:b] = topd[a:b]
        elif fam == "wta":
            util[a:b] = np.floor(eq[a:b] * first)
        elif fam == "satellite":
            util[a:b] = np.floor(seat[a:b] * face) + share
        else:
            util[a:b] = share
    dollars = pay / 100.0
    top_d = topd / 100.0
    vals = {
        "exp_payout": (dollars.mean(axis=0), _se(dollars, S)),
        "exp_payout_top1pct": (top_d.mean(axis=0), _se(top_d, S)),
        "first_place_equity": (eq.mean(axis=0), _se(eq, S)),
        "p_clear_line": (clear.mean(axis=0), _se(clear, S)),
        "p_seat": (seat.mean(axis=0), _se(seat, S)),
    }
    name = FAMILY_OBJECTIVE[fam]
    return Metrics(
        contest.contest_id, fam, S, name, vals[name][0], vals[name][1],
        vals["exp_payout"][0], vals["exp_payout"][1], vals["exp_payout_top1pct"][0], vals["exp_payout_top1pct"][1],
        (pay > 0).mean(axis=0), top.mean(axis=0), _se(top, S), vals["first_place_equity"][0], vals["first_place_equity"][1],
        vals["p_clear_line"][0], vals["p_clear_line"][1], vals["p_seat"][0], vals["p_seat"][1],
        seat.mean(axis=0) * face / 100.0, pay, util, top_k)


def _stack(ms: list[Metrics]) -> Metrics:
    m0 = ms[0]
    def cat(name):
        arrs = [getattr(m, name) for m in ms]
        return np.concatenate(arrs, axis=1 if arrs[0].ndim == 2 else 0)
    kw = {f: cat(f) for f in ("objective", "objective_se", "exp_payout", "exp_payout_se", "exp_payout_top1pct",
                              "exp_payout_top1pct_se", "p_cash", "p_top1pct", "p_top1pct_se", "first_place_equity",
                              "first_place_equity_se", "p_clear_line", "p_clear_line_se", "p_seat", "p_seat_se",
                              "exp_ticket_value", "payout_cents", "utility_cents")}
    return Metrics(m0.contest_id, m0.family, m0.n_scenarios, m0.objective_name, top_k=m0.top_k, **kw)


# -- portfolio --------------------------------------------------------------------------------------

@dataclass
class ContestEval:
    contest: Contest
    field: FieldSpec
    entry_ids: list[str]


@dataclass
class PortfolioMetrics:
    n_scenarios: int
    fees: float
    exp_payout: float
    exp_payout_se: float
    p_zero_payout: float
    p_net_loss: float
    p_lose80: float
    p_lose80_se: float
    es5: float  # mean of the worst 5% of R(s), dollars
    tail_utility: float  # sum of per-entry family utilities / fees
    tail_utility_se: float
    recovery_quantiles: dict[str, float]  # payout / fees
    concentration: dict
    per_entry: dict[str, dict]
    tickets_value: float

    def record(self) -> dict:
        return {k: getattr(self, k) for k in ("n_scenarios", "fees", "exp_payout", "exp_payout_se", "p_zero_payout",
                                              "p_net_loss", "p_lose80", "p_lose80_se", "es5", "tail_utility",
                                              "tail_utility_se", "recovery_quantiles", "concentration", "tickets_value")}


def return_stats(payout_cents: np.ndarray, fees_cents: int, util_cents: np.ndarray | None = None) -> dict:
    """R(s) = sum(entry payouts in s) - total fees. payout_cents (S,) summed over entries."""
    S = len(payout_cents)
    R = (payout_cents.astype(np.int64) - int(fees_cents)) / 100.0
    fees = fees_cents / 100.0
    lose80 = (R <= -0.8 * fees).astype(float)
    k = max(1, int(math.ceil(0.05 * S)))
    worst = np.sort(R)[:k]
    rec = payout_cents / max(1, fees_cents)
    out = {
        "exp_payout": float(payout_cents.mean() / 100.0), "exp_payout_se": float(payout_cents.std() / 100.0 / math.sqrt(S)),
        "p_zero_payout": float((payout_cents == 0).mean()), "p_net_loss": float((R < 0).mean()),
        "p_lose80": float(lose80.mean()), "p_lose80_se": float(lose80.std() / math.sqrt(S)),
        "es5": float(worst.mean()),
        "recovery_quantiles": {f"q{int(q * 100)}": round(float(np.quantile(rec, q)), 3) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
    }
    if util_cents is not None:
        u = util_cents / max(1, fees_cents)
        out["tail_utility"] = float(u.mean())
        out["tail_utility_se"] = float(u.std() / math.sqrt(S))
    return out


def portfolio_metrics(assignment: Mapping[str, Sequence[str]], contests: Mapping[str, ContestEval], scenarios: ScenarioSet,
                      *, pool, fees_cents: Mapping[str, int], cfg: dict | None = None) -> PortfolioMetrics:
    """Every entry scored jointly with the user's other entries in its contest, against that contest's
    field, in one scenario set. Concentration by goalie, game (when the slate has more than one) and
    Captain, in fees; plus the shared failure scenario (build.exposure.concentration)."""
    from nhl_dfs.build import exposure

    cfg = cfg if cfg is not None else load_risk_config()
    S = scenarios.n
    total_pay = np.zeros(S, np.int64)
    total_util = np.zeros(S, np.int64)
    per_entry: dict[str, dict] = {}
    pay_by_entry: dict[str, np.ndarray] = {}
    tickets = 0.0
    for cid, ce in contests.items():
        eids = [e for e in ce.entry_ids if e in assignment]
        if not eids:
            continue
        own = scenarios.scores([assignment[e] for e in eids], pool.mode).full()
        fs, w = ce.field.scores(scenarios, pool.mode)
        m = joint_payouts(own, fs, w, ce.contest, cfg=cfg)
        for j, e in enumerate(eids):
            per_entry[e] = {"contest_id": cid, "family": ce.contest.family, **m.row(j)}
            pay_by_entry[e] = m.payout_cents[:, j]
            tickets += float(m.exp_ticket_value[j])
        total_pay += m.payout_cents.sum(axis=1)
        total_util += m.utility_cents.sum(axis=1)
    fees = int(sum(fees_cents.get(e, 0) for e in assignment))
    st = return_stats(total_pay, fees, total_util)
    conc = exposure.concentration(assignment, pool, fees_cents, scenarios, pay_by_entry)
    return PortfolioMetrics(S, fees / 100.0, st["exp_payout"], st["exp_payout_se"], st["p_zero_payout"], st["p_net_loss"],
                            st["p_lose80"], st["p_lose80_se"], st["es5"], st["tail_utility"], st["tail_utility_se"],
                            st["recovery_quantiles"], conc, per_entry, tickets)

