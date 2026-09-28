"""Ownership prefit on historical labels (C3, plan section 6 "Cold start" and section 12).

Runs only when learn/gates.allows("prefit") passes for that mode on the historical counts;
historical data counts toward the floors and never bypasses them. Flag 1 (prior-season DK
standings exports) is answered "none", so on real data this returns "gated".

Input format (neutral, documented here; C11 must produce it from real DK standings exports,
whose raw schema has not been seen yet, so nothing here guesses at it):

    <standings_dir>/<YYYY-MM-DD>_<label>/ownership_labels.csv   header exactly
        contest_id,role_id,drafted_pct
    <standings_dir>/<YYYY-MM-DD>_<label>/DKSalaries.csv         the slate's salary file
                                                                 (or pass the pool in `pools`)

One directory is one slate group (Classic) or one game (Showdown); the mode comes from the
salary file. role_id is the DK ID of the exact row, so Showdown labels are role-separated
(CPT and FLEX). drafted_pct is a percentage, 0 to 100. A player missing from a contest's labels
is missing, not zero: only labeled rows enter that contest's likelihood.

Model (a surrogate for the field sampler, in the sampler's own units): within each contest and
position block (Classic F/D/G; Showdown CPT or FLEX by F/D/G), a row's share of the block's
observed mass is softmax(b0 * mean_points + sum_k b_k * feature_k). Then w_k = b_k / b0 are
points-unit weights, the same quantities as config/ownership.yaml `weights`. Chronological
holdout: the latest groups (the floor's holdout_groups) are never fitted.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.ids import position_group
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.intake.salary import SalaryPool, read_salary
from nhl_dfs.learn import gates
from nhl_dfs.models import ownership
from nhl_dfs.models.projection import PriorProjection

LABELS_FILE = "ownership_labels.csv"
LABELS_HEADER = ["contest_id", "role_id", "drafted_pct"]
_GROUP_DIR = re.compile(r"^(\d{4}-\d{2}-\d{2})_.+$")
FIT_FEATURES = ("appg", "value_z", "salary_rank", "implied_total", "goalie_start_win", "questionable")
RIDGE = 1e-6  # numerical guard only; the chronological holdout, not shrinkage, polices overfit


class LabelFormatError(ValueError):
    pass


@dataclass
class ModePrefit:
    mode: Mode
    status: str  # "fitted" | "gated" | "rejected"
    reason: str
    counts: gates.EvidenceCounts
    weights: dict[str, float] = field(default_factory=dict)  # fitted points-unit weights
    offsets: dict[str, float] = field(default_factory=dict)  # fitted minus prior
    holdout_mae_fitted: float | None = None  # percentage points, holdout groups only
    holdout_mae_prior: float | None = None
    train_groups: list[str] = field(default_factory=list)
    holdout_groups: list[str] = field(default_factory=list)

    @property
    def improves(self) -> bool:
        return (self.status == "fitted" and self.holdout_mae_fitted is not None
                and self.holdout_mae_prior is not None and self.holdout_mae_fitted < self.holdout_mae_prior)


@dataclass
class PrefitResult:
    by_mode: dict[Mode, ModePrefit]
    skipped: list[str]  # "<dir>: reason"

    def summary(self) -> dict[str, str]:
        return {m.value: f"{r.status}: {r.reason}" for m, r in self.by_mode.items()}


@dataclass
class _Group:
    name: str
    date: str
    pool: SalaryPool
    labels: dict[str, dict[str, float]]  # contest_id -> role_id -> pct
    invalid: int


def read_labels(path: Path, pool: SalaryPool) -> tuple[dict[str, dict[str, float]], int]:
    """contest_id -> role_id -> pct, and the count of rows rejected (unknown ID, bad pct, repeat)."""
    text = path.read_bytes().decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header != LABELS_HEADER:
        raise LabelFormatError(f"{path}: header must be exactly {','.join(LABELS_HEADER)}, got {header!r}")
    out: dict[str, dict[str, float]] = defaultdict(dict)
    bad = 0
    for rec in reader:
        if not rec or all(not x.strip() for x in rec):
            continue
        if len(rec) != 3:
            bad += 1
            continue
        cid, rid, pct = (x.strip() for x in rec)
        try:
            v = float(pct)
        except ValueError:
            bad += 1
            continue
        if rid not in pool.by_role_id or not 0.0 <= v <= 100.0 or rid in out[cid]:
            bad += 1
            continue
        out[cid][rid] = v
    return dict(out), bad


def load_groups(standings_dir, pools: Mapping[str, SalaryPool] | None = None) -> tuple[list[_Group], list[str]]:
    root = Path(standings_dir)
    pools = pools or {}
    groups, skipped = [], []
    if not root.is_dir():
        return groups, [f"{root}: no such directory"]
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        m = _GROUP_DIR.match(d.name)
        if not m:
            skipped.append(f"{d.name}: directory name is not <YYYY-MM-DD>_<label>")
            continue
        lab = d / LABELS_FILE
        if not lab.exists():
            skipped.append(f"{d.name}: no {LABELS_FILE}")
            continue
        pool = pools.get(d.name)
        if pool is None:
            sal = d / "DKSalaries.csv"
            if not sal.exists():
                skipped.append(f"{d.name}: no DKSalaries.csv and no pool supplied")
                continue
            pool = read_salary(sal)
        labels, bad = read_labels(lab, pool)  # a header mismatch raises: never guess a schema
        groups.append(_Group(d.name, m.group(1), pool, labels, bad))
    groups.sort(key=lambda g: (g.date, g.name))
    return groups, skipped


def _block(pool: SalaryPool, rid: str) -> tuple[str, str]:
    r = pool.by_role_id[rid]
    role = ("CPT" if "CPT" in r.roster_positions else "FLEX") if pool.mode is Mode.SHOWDOWN else ""
    return role, position_group(r.position)


def _statuses(pool: SalaryPool) -> dict[str, Participation]:
    from nhl_dfs.build.run import salary_statuses  # the run's own reading of the DK Status column

    return {rid: p for rid, (p, _) in salary_statuses(pool).items()}


def _design(g: _Group, cfg: dict) -> list[tuple[list[list[float]], list[float]]]:
    """Per (contest, block): (rows of [mean_pts, features...], observed pct)."""
    proj = PriorProjection(g.pool)
    feats = ownership.feature_table(g.pool, proj, cfg=cfg, statuses=_statuses(g.pool))
    out = []
    for cid in sorted(g.labels):
        blocks: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rid in sorted(g.labels[cid]):
            blocks[_block(g.pool, rid)].append(rid)
        for key in sorted(blocks):
            rids = blocks[key]
            if len(rids) < 2 or sum(g.labels[cid][r] for r in rids) <= 0:
                continue
            x = [[proj.mean_tenths(r) / 10.0] + [feats[r][k] for k in FIT_FEATURES] for r in rids]
            out.append((x, [g.labels[cid][r] for r in rids]))
    return out


def _predict(x: list[list[float]], mass: float, beta) -> list[float]:
    z = [sum(b * v for b, v in zip(beta, row)) for row in x]
    mx = max(z)
    e = [math.exp(v - mx) for v in z]
    s = sum(e)
    return [mass * v / s for v in e]


def _fit_beta(blocks, active: list[int]):
    import numpy as np
    from scipy.optimize import minimize

    data = [(np.asarray(x)[:, active], np.asarray(y) / sum(y)) for x, y in blocks]
    scale = sum(len(y) for _, y in data)

    def loss(beta):
        total, grad = 0.0, np.zeros_like(beta)
        for x, p in data:
            z = x @ beta
            z = z - z.max()
            e = np.exp(z)
            q = e / e.sum()
            total -= float(p @ np.log(q + 1e-300))
            grad -= x.T @ (p - q)
        total = total / scale + RIDGE * float(beta @ beta)
        return total, grad / scale + 2 * RIDGE * beta

    x0 = np.zeros(len(active))
    x0[0] = 0.2
    res = minimize(loss, x0, jac=True, method="L-BFGS-B")
    return res.x, bool(res.success)


def _mae(blocks, beta_full) -> float:
    err, n = 0.0, 0
    for x, y in blocks:
        pred = _predict(x, sum(y), beta_full)
        err += sum(abs(a - b) for a, b in zip(pred, y))
        n += len(y)
    return err / n if n else float("nan")


def counts_for(groups: list[_Group], mode: Mode, holdout: int) -> gates.EvidenceCounts:
    mine = [g for g in groups if g.pool.mode is mode]
    labels = sum(len(v) for g in mine for v in g.labels.values())
    dates = len({g.date for g in mine})
    return gates.EvidenceCounts(slate_dates=dates, slate_groups=len(mine), ownership_labels=labels,
                                holdout_groups=holdout)


def fit(standings_dir, pools: Mapping[str, SalaryPool] | None = None, *, cfg: dict | None = None,
        floors: dict | None = None) -> PrefitResult:
    """Per mode: fitted weights (behind the gate) or "gated" with the gate's reason."""
    cfg = cfg if cfg is not None else ownership.load_ownership_config()
    floors = floors if floors is not None else gates.load_floors()
    groups, skipped = load_groups(standings_dir, pools)
    by_mode: dict[Mode, ModePrefit] = {}
    for mode in Mode:
        tier = floors["actions"]["prefit"]
        hold = int(floors["tiers"][mode.value][tier].get("holdout_groups", 0))
        counts = counts_for(groups, mode, hold)
        ok, why = gates.allows("prefit", counts, mode, cfg=floors)
        if not ok:
            by_mode[mode] = ModePrefit(mode, "gated", why, counts)
            continue
        mine = [g for g in groups if g.pool.mode is mode]
        train, test = mine[:-hold] if hold else mine, mine[-hold:] if hold else []
        tr_blocks = [b for g in train for b in _design(g, cfg)]
        te_blocks = [b for g in test for b in _design(g, cfg)]
        # Fit the mean coefficient and every feature that varies in training; the rest keep the prior.
        active = [0] + [1 + i for i, _ in enumerate(FIT_FEATURES)
                        if len({round(row[1 + i], 9) for x, _ in tr_blocks for row in x}) > 1]
        beta, converged = _fit_beta(tr_blocks, active)
        b0 = float(beta[0])
        if not converged or b0 <= 0:
            by_mode[mode] = ModePrefit(mode, "rejected", f"fit did not converge to a positive mean coefficient (b0={b0:.4g})",
                                       counts, train_groups=[g.name for g in train], holdout_groups=[g.name for g in test])
            continue
        prior_w = {k: float(cfg["weights"][k]) for k in FIT_FEATURES}
        fitted = dict(prior_w)
        for j, col in enumerate(active[1:], start=1):
            fitted[FIT_FEATURES[col - 1]] = float(beta[j]) / b0
        full_fit = [b0] + [b0 * fitted[k] for k in FIT_FEATURES]
        full_prior = [b0] + [b0 * prior_w[k] for k in FIT_FEATURES]
        r = ModePrefit(mode, "fitted", why, counts, weights=fitted,
                       offsets={k: fitted[k] - prior_w[k] for k in FIT_FEATURES},
                       holdout_mae_fitted=_mae(te_blocks, full_fit), holdout_mae_prior=_mae(te_blocks, full_prior),
                       train_groups=[g.name for g in train], holdout_groups=[g.name for g in test])
        by_mode[mode] = r
    return PrefitResult(by_mode, skipped)
