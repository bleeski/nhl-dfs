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


def template_curve(match) -> tuple[np.ndarray, np.ndarray, int | None, str]:
    """From a models.payout_templates.TemplateMatch: DraftKings' table of the same template and size (C16). The
    tiers are the matched contest's, so the curve is the exact_curve of that table; only the label says whose it is.
    A ticket tier cannot occur (the matcher refuses it), so no face value is read."""
    prizes, seats, face, _ = exact_curve(match.table.detail)
    return prizes, seats, face, f"TEMPLATE from the {match.label()}"


def contest_from(ctx, fam_cfg: dict) -> Contest:
    """A models.contests.ContestContext -> Contest with its payout curve (EXACT, TEMPLATE or declared PRIOR)."""
    from nhl_dfs.models.contests import fee_value

    fee = fee_value(ctx.fee)
    fee_cents = to_cents(fee) if fee is not None else 0
    if ctx.payout_source is PayoutSource.EXACT:
        prizes, seats, face, note = exact_curve(ctx.detail)
    elif ctx.payout_source is PayoutSource.TEMPLATE:
        prizes, seats, face, note = template_curve(ctx.template)
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
        return SampledScores(distinct, self.draws), np.ones(self.n_opponents, np.int64)


class SampledScores:
    """Lazy (S, n_opponents): each scenario's own opponents, gathered per row slice (no full matrix)."""

    def __init__(self, distinct: "LineupScores", draws: np.ndarray):
        self.distinct, self.draws = distinct, draws
        self.shape = (distinct.shape[0], draws.shape[1])

    def __getitem__(self, sl) -> np.ndarray:
        return np.take_along_axis(self.distinct[sl], self.draws[sl], axis=1)


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
    all_keys = list(order)
    kk = [all_keys[i] for i in idx]
    cnt = np.asarray([counts[i] for i in idx], np.int64)
    n_opp = max(0, int(n_opponents))
    if n_opp <= int(o["sampled_max_opponents"]):
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(seed), MASK_STREAM + 1,
                                                                           zlib.crc32(salt.encode())])))
        p = cnt / cnt.sum()
        draws = (rng.choice(len(distinct), size=(int(n_scenarios), n_opp), p=p).astype(np.int32) if n_opp
                 else np.zeros((n_scenarios, 0), np.int32))
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
    top_payout_cents: np.ndarray | None = None  # (S, K) int32 payout from finishes inside the top 1%, per scenario (C18)

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
    return ranks_multi(cand, field, [weights], own)[0]


def ranks_multi(cand: np.ndarray, field: np.ndarray, weights_list: Sequence[np.ndarray],
                own: np.ndarray | None = None) -> list[tuple[np.ndarray, np.ndarray]]:
    """`ranks` for several weight vectors over the same field lineups: the field is sorted once."""
    c, K = cand.shape
    F = field.shape[1]
    out = []
    if F:
        order = np.argsort(field, axis=1, kind="stable")
        fs = np.take_along_axis(field, order, axis=1).astype(np.int64)
        lo = min(int(fs.min()), int(cand.min()))
        span = max(int(fs.max()), int(cand.max())) - lo + 1
        rows = np.arange(c, dtype=np.int64)[:, None]
        flat = (fs - lo + rows * span).ravel()
        q = (cand.astype(np.int64) - lo + rows * span).ravel()
        base = rows * F
        right = np.searchsorted(flat, q, side="right").reshape(c, K) - base
        left = np.searchsorted(flat, q, side="left").reshape(c, K) - base
        del flat, q
    for weights in weights_list:
        if F:
            w = np.asarray(weights, np.int64)
            cw = np.zeros((c, F + 1), np.int64)
            np.cumsum(w[order], axis=1, out=cw[:, 1:])
            le = np.take_along_axis(cw, right, axis=1)
            G = cw[:, F][:, None] - le
            E = le - np.take_along_axis(cw, left, axis=1)
        else:
            G = np.zeros((c, K), np.int64)
            E = np.zeros((c, K), np.int64)
        if own is not None and own.shape[1]:
            G = G + (own[:, None, :] > cand[:, :, None]).sum(axis=2)
            E = E + (own[:, None, :] == cand[:, :, None]).sum(axis=2)
        out.append((G, E))
    return out


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
    return _evaluate(S, K, field.shape[1] * 64 + K * (J + 96), block, contest, cfg)


def joint_payouts(own, field, weights, contest: Contest, *, cfg: dict | None = None) -> Metrics:
    """All of the user's entries in one contest ranked together: own (S, J) scores; each entry's
    finish counts the field and every OTHER own entry (they are opponents of each other)."""
    cfg = cfg if cfg is not None else load_risk_config()
    S, J = own.shape

    def block(a: int, b: int):
        o = np.asarray(own[a:b])
        G, E = ranks(o, np.asarray(field[a:b]), weights)
        pg, pe = own_pairwise(o)  # every other own entry above it / tied with it
        return G + pg, E + pe
    return _evaluate(S, J, field.shape[1] * 64 + J * (J + 96), block, contest, cfg)


def metrics_from_ranks(G: np.ndarray, E: np.ndarray, contest: Contest, cfg: dict) -> Metrics:
    """Metrics from precomputed ranks (S, K): G weight strictly above, E tied (own copies included)."""
    S, K = G.shape
    return _evaluate(S, K, K * 96, lambda a, b: (G[a:b].astype(np.int64), E[a:b].astype(np.int64)), contest, cfg)


def own_pairwise(own: np.ndarray, cap_mb: float = 64.0) -> tuple[np.ndarray, np.ndarray]:
    """(S, J) own scores -> (G, E) from the user's other entries in the same contest, chunked by scenario
    (the J x J comparison of 150 entries over 20,000 scenarios would otherwise be about 450 MB)."""
    S, J = own.shape
    G = np.zeros((S, J), np.int64)
    E = np.zeros((S, J), np.int64)
    step = _chunk_rows(S, J * J * 2 + J * 32, cap_mb)
    for a in range(0, S, step):
        o = own[a:a + step]
        G[a:a + step] = (o[:, None, :] > o[:, :, None]).sum(axis=2)
        E[a:a + step] = (o[:, None, :] == o[:, :, None]).sum(axis=2) - 1
    return G, E


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
        seat.mean(axis=0) * face / 100.0, pay, util, top_k, topd)


def _stack(ms: list[Metrics]) -> Metrics:
    m0 = ms[0]
    def cat(name):
        arrs = [getattr(m, name) for m in ms]
        return np.concatenate(arrs, axis=1 if arrs[0].ndim == 2 else 0)
    kw = {f: cat(f) for f in ("objective", "objective_se", "exp_payout", "exp_payout_se", "exp_payout_top1pct",
                              "exp_payout_top1pct_se", "p_cash", "p_top1pct", "p_top1pct_se", "first_place_equity",
                              "first_place_equity_se", "p_clear_line", "p_clear_line_se", "p_seat", "p_seat_se",
                              "exp_ticket_value", "payout_cents", "utility_cents", "top_payout_cents")}
    return Metrics(m0.contest_id, m0.family, m0.n_scenarios, m0.objective_name, top_k=m0.top_k, **kw)


# -- joint own-entry accounting (C18, backlog B55, review R04) --------------------------------------------------

class PayCurve:
    """A contest's prefix arrays, built once: the selection fill's payout and utility kernel."""

    def __init__(self, ct: Contest, top_pct: float):
        self.ct = ct
        self.L = ct.field_size
        self.cash = ct.prefix(ct.prizes_cents)
        self.seatp = ct.prefix(ct.seats.astype(np.int64))
        self.top_k = max(1, int(math.floor(top_pct * self.L)))
        self.first = int(ct.prizes_cents[0]) if ct.paid else 0
        self.face = ct.ticket_face_cents or 0

    def pay_util(self, G: np.ndarray, E: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(payout cents, family utility cents) for rank blocks G, E (own copies included)."""
        G = G.astype(np.int64)
        T = E.astype(np.int64) + 1
        hi, lo = np.minimum(G + T, self.L), np.minimum(G, self.L)
        pay = (self.cash[hi] - self.cash[lo]) // T
        fam = self.ct.family
        if fam == "large_gpp":
            util = (self.cash[np.minimum(G + T, self.top_k)] - self.cash[np.minimum(G, self.top_k)]) // T
        elif fam == "wta":
            util = np.floor((G == 0) / T * self.first).astype(np.int64)
        elif fam == "satellite":
            util = np.floor((self.seatp[hi] - self.seatp[lo]) / T * self.face).astype(np.int64) + pay
        else:
            util = pay
        return pay, util


class OwnContest:
    """One contest's own entries ranked jointly and filled one at a time: the joint-accounting primitive.

    The candidates are the columns of three shared (S, K) arrays that are never copied or changed: `scores`
    (scenario scores) and `G`, `E` (field weight strictly above and tied with each candidate; the user's own
    entries are not in them). Placed own entries are kept as per-candidate counts (`gi` above, `ei` tied) and, per
    scenario, as each placed entry's position in the ascending score order (`rank`). `kernel(G, E)` maps rank blocks
    to a tuple of per-scenario measures (payout and utility cents for `PayCurve.pay_util`).

    `marginal(a, b)` is, for scenario rows a..b and EVERY candidate k, what adding k does to the contest's total own
    measures: the new entry's own measure (the same expression the fill used before), plus what k takes from each
    placed entry it passes (one place down) and each it ties (one more sharer). Those two sums are read off prefix
    sums over the placed order, so a step costs O(rows x (m + K)) and never O(rows x m x K); no per-entry (S, K)
    arrays exist (the per-entry rank is a column of G, E, gi, ei). Every term is the rank function evaluated at the
    ranks the joint ranking implies, so running totals are exact integer cents and equal a from-scratch recount.

    `live_below`: a placed entry ranked at or below this many places (the contest's paid places) has every measure 0
    whether or not it is passed or tied, so those (scenario, entry) pairs are skipped and a scenario row with none
    left is skipped entirely. A speed-up only (None skips nothing); the tests hold both settings to the same numbers.
    Not thread-safe, one instance per fill; the shared arrays are only read."""

    def __init__(self, kernel, G: np.ndarray, E: np.ndarray, scores: np.ndarray, *, capacity: int = 16,
                 chunk: int | None = None, live_below: int | None = None):
        self.kernel = kernel
        self.G, self.E, self.scores = G, E, scores
        self.S, self.K = G.shape
        self.chunk = max(1, min(self.S, chunk or self.S))
        self.live_below = live_below
        self.gi = np.zeros((self.S, self.K), G.dtype)
        self.ei = np.zeros((self.S, self.K), G.dtype)
        self.cols: list[int] = []
        self._cj = np.zeros(0, np.intp)
        self.rank = np.zeros((self.S, max(1, capacity)), np.int16)  # place in the ascending score order, per scenario
        self.total: tuple[np.ndarray, ...] | None = None  # the placed entries' measures summed, (S,) each

    def _live(self, Gj: np.ndarray) -> np.ndarray:
        return np.ones(Gj.shape, bool) if self.live_below is None else Gj < self.live_below

    @staticmethod
    def _distinct(vals) -> list[int]:
        """Positions of the measures that are different arrays (a family whose utility is its payout returns one array twice)."""
        return [i for i, v in enumerate(vals) if not any(v is vals[j] for j in range(i))]

    def _pairs(self, a: int, b: int, rows: np.ndarray | None, Gj: np.ndarray, live: np.ndarray):
        """The live (scenario, entry) pairs of a block: flat positions in the block, scenario and placement index of each,
        and their G and E (the entry's own ties exclude itself)."""
        m = len(self.cols)
        ii = np.flatnonzero(live)
        rr = ii // m
        jj = ii - rr * m
        row = rr if rows is None else rows[rr]
        col = self._cj[jj]
        e = self.E[a:b][row, col].astype(np.int64) + self.ei[a:b][row, col] - 1
        return ii, rr, jj, row, Gj.reshape(-1)[ii], e

    def marginal(self, a: int, b: int) -> tuple[np.ndarray, ...]:
        """Change in the contest's total measures from adding each candidate, scenario rows a..b: (b - a, K) each."""
        G, E, gi, ei = self.G[a:b], self.E[a:b], self.gi[a:b], self.ei[a:b]
        Gn, En = G + gi, E + ei
        if self.live_below is None:
            new = list(self.kernel(Gn, En))
        else:  # a candidate ranked outside the paid places has every measure 0: evaluate the others only
            hit = np.flatnonzero(Gn < self.live_below)
            vals = self.kernel(Gn.reshape(-1)[hit], En.reshape(-1)[hit])
            new = []
            for i, v in enumerate(vals):
                twin = next((j for j in range(i) if vals[j] is v), None)
                if twin is None:
                    z = np.zeros(Gn.shape, v.dtype)
                    np.put(z, hit, v)
                    new.append(z)
                else:
                    new.append(new[twin])
        m = len(self.cols)
        if not m:
            return tuple(new)
        cj = self._cj
        Gj = G[:, cj].astype(np.int64) + gi[:, cj]
        live = self._live(Gj)
        keep = np.flatnonzero(live.any(axis=1))
        if not keep.size:
            return tuple(new)
        every = keep.size == Gj.shape[0]
        if not every:
            Gj, live = Gj[keep], live[keep]
        ii, rr, jj, row, g, e = self._pairs(a, b, None if every else keep, Gj, live)
        base, down, tie = self.kernel(g, e), self.kernel(g + 1, e), self.kernel(g, e + 1)
        R = keep.size
        flat = rr * (m + 1) + self.rank[a:b][row, jj].astype(np.intp) + 1  # slot of each entry in its scenario's prefix row
        gl, el = (gi, ei) if every else (gi[keep], ei[keep])
        below = np.arange(R, dtype=np.intp)[:, None] * (m + 1) + (m - gl - el)  # entries strictly below k: the first
        upto = below + el  # `m - gi - ei` in order; those tied with k follow, up to `m - gi`
        for i in self._distinct(new):  # d_down = effect of being passed once, d_tie = effect of gaining a sharer
            q, t = np.zeros((R, m + 1), base[i].dtype), np.zeros((R, m + 1), base[i].dtype)
            np.put(q, flat, down[i] - tie[i])  # below k: passed, not tied (the tied ones are added back through t)
            np.put(t, flat, tie[i] - base[i])
            np.cumsum(q, axis=1, out=q)
            np.cumsum(t, axis=1, out=t)
            corr = np.take(q.reshape(-1), below, mode="clip") + np.take(t.reshape(-1), upto, mode="clip")
            if every:
                new[i] += corr
            else:
                new[i][keep] += corr
        return tuple(new)

    def add(self, k: int) -> tuple[np.ndarray, ...]:
        """Place candidate k; returns the change in the contest's total measures, (S,) each (exact integer cents
        for the payout kernel), recounted from the updated ranks of every placed entry."""
        m = len(self.cols)
        if m >= self.rank.shape[1]:
            self.rank = np.concatenate([self.rank, np.zeros_like(self.rank)], axis=1)
        for a in range(0, self.S, self.chunk):
            b = min(self.S, a + self.chunk)
            pos = (m - self.gi[a:b, k] - self.ei[a:b, k]).astype(np.int16)  # k goes after the entries below it
            if m:
                placed = self.rank[a:b, :m]
                placed += placed >= pos[:, None]  # the entries at or after that slot move up one
            self.rank[a:b, m] = pos
            own = self.scores[a:b, [k]]
            self.gi[a:b] += own > self.scores[a:b]
            self.ei[a:b] += own == self.scores[a:b]
        self.cols.append(k)
        self._cj = np.asarray(self.cols, np.intp)
        return self._recount()

    def _recount(self) -> tuple[np.ndarray, ...]:
        sums = None
        cj = self._cj
        m = len(cj)
        for a in range(0, self.S, self.chunk):
            b = min(self.S, a + self.chunk)
            Gj = self.G[a:b, cj].astype(np.int64) + self.gi[a:b, cj]
            ii, rr, _jj, _row, g, e = self._pairs(a, b, None, Gj, self._live(Gj))
            vals = self.kernel(g, e)
            if sums is None:
                sums = [np.zeros(self.S, v.dtype) for v in vals]
            for s, v in zip(sums, vals):
                s[a:b] = np.bincount(rr, weights=v, minlength=b - a).astype(v.dtype) if v.size else 0
        old, self.total = self.total, tuple(sums)
        return self.total if old is None else tuple(n - o for n, o in zip(self.total, old))

    def totals(self) -> tuple[np.ndarray, ...]:
        """The placed entries' measures summed per scenario, recounted without the incremental counters
        (`own_pairwise` on the placed scores): what `total` must equal. For tests and checks, not the fill."""
        cj = self._cj
        pg, pe = own_pairwise(self.scores[:, cj])
        vals = self.kernel(self.G[:, cj].astype(np.int64) + pg, self.E[:, cj].astype(np.int64) + pe)
        return tuple(v.sum(axis=1) for v in vals)

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


def joint_by_contest(assignment: Mapping[str, Sequence[str]], contests: Mapping[str, ContestEval], scenarios: ScenarioSet,
                     *, pool, cfg: dict) -> dict[str, tuple[list[str], Metrics]]:
    """Joint metrics per contest; contests sharing one weighted field (same lineups) sort it once."""
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for cid, ce in contests.items():
        if any(e in assignment for e in ce.entry_ids):
            key = ("w", tuple(ce.field.keys)) if ce.field.mode == "weighted" else ("s", cid)
            groups[key].append(cid)
    out = {}
    S = scenarios.n
    for key, cids in groups.items():
        eids = {cid: [e for e in contests[cid].entry_ids if e in assignment] for cid in cids}
        cols = [e for cid in cids for e in eids[cid]]
        own = scenarios.scores([assignment[e] for e in cols], pool.mode).full()
        fs, _ = contests[cids[0]].field.scores(scenarios, pool.mode)
        wl = [contests[cid].field.weights if contests[cid].field.mode == "weighted"
              else np.ones(contests[cid].field.n_opponents, np.int64) for cid in cids]
        G = {cid: np.zeros((S, len(eids[cid])), np.int64) for cid in cids}
        E = {cid: np.zeros((S, len(eids[cid])), np.int64) for cid in cids}
        pos = {e: j for j, e in enumerate(cols)}
        idx = {cid: [pos[e] for e in eids[cid]] for cid in cids}
        step = _chunk_rows(S, fs.shape[1] * 64 + len(cols) * (96 + 16 * len(cids)), float(cfg["objectives"]["memory_cap_mb"]))
        for a in range(0, S, step):
            b = min(S, a + step)
            for cid, (g, e) in zip(cids, ranks_multi(own[a:b], np.asarray(fs[a:b]), wl)):
                G[cid][a:b] = g[:, idx[cid]]
                E[cid][a:b] = e[:, idx[cid]]
        for cid in cids:
            pg, pe = own_pairwise(own[:, idx[cid]])
            out[cid] = (eids[cid], metrics_from_ranks(G[cid] + pg, E[cid] + pe, contests[cid].contest, cfg))
    return out


def own_totals(joint: Mapping[str, tuple[list[str], "Metrics | None"]], only=None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-scenario (payout, utility, top-1% payout) cents summed over the user's entries in these contests' joint
    metrics: every entry, or only those named in `only`. A contest's own entries are one account (an entry's finish
    counts every other entry), so the change from one portfolio to another is the difference of these totals, never
    of the edited entries alone (C18, B56, R05)."""
    pay = util = top = None
    for eids, m in joint.values():
        if m is None:
            continue
        idx = [j for j, e in enumerate(eids) if only is None or e in only]
        if not idx:
            continue
        p, u, t = (a[:, idx].astype(np.int64).sum(axis=1) for a in (m.payout_cents, m.utility_cents, m.top_payout_cents))
        pay, util, top = (p, u, t) if pay is None else (pay + p, util + u, top + t)
    if pay is None:
        raise ValueError("no entries in these contests' joint metrics")
    return pay, util, top


def portfolio_metrics(assignment: Mapping[str, Sequence[str]], contests: Mapping[str, ContestEval], scenarios: ScenarioSet,
                      *, pool, fees_cents: Mapping[str, int], cfg: dict | None = None,
                      joint: Mapping[str, tuple[list[str], Metrics]] | None = None) -> PortfolioMetrics:
    """Every entry scored jointly with the user's other entries in its contest, against that contest's
    field, in one scenario set. Concentration by goalie, game (when the slate has more than one) and
    Captain, in fees; plus the shared failure scenario (build.exposure.concentration). joint: per
    contest (entry ids, Metrics) already computed from ranks (the selection frontier reuses its own)."""
    from nhl_dfs.build import exposure

    cfg = cfg if cfg is not None else load_risk_config()
    S = scenarios.n
    total_pay = np.zeros(S, np.int64)
    total_util = np.zeros(S, np.int64)
    per_entry: dict[str, dict] = {}
    pay_by_entry: dict[str, np.ndarray] = {}
    tickets = 0.0
    joint = joint if joint is not None else joint_by_contest(assignment, contests, scenarios, pool=pool, cfg=cfg)
    for cid, ce in contests.items():
        if cid not in joint:
            continue
        eids, m = joint[cid]
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

