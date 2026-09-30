"""Grade the frozen pre-lock ownership forecast against the standings (card C11; plan section 6 metrics).

The forecast is what the run saved before lock: `runs/<id>/scenario/fields.json` (`own_by`: contest -> role id ->
% of the sampled field; `dup_by`: contest -> lineup key -> expected entries at the forecast's field size;
`n_opponents`). Nothing is recomputed. The actual is learn.standings.join (the lineup reconstruction; Classic per
person over his position and UTIL rows, Showdown per CPT and FLEX role).

Metrics, in percentage points: whole-pool and active MAE, popular-player error (the actual top 20, and an
actual-weighted MAE), calibration by predicted band, top-10 chalk recall, Captain-share error (Showdown), team
totals, zero-observed mass (forecast mass on roles nobody drafted, only when the zero is observed), Pearson and
Spearman correlation, and duplicate counts (the forecast's expected counts rescaled from its field size to the
actual entry count; labeled). Rank correlation alone is not calibration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from nhl_dfs.contracts.geometry import Mode

BANDS = [(0.0, 1.0), (1.0, 3.0), (3.0, 10.0), (10.0, 20.0), (20.0, 40.0), (40.0, 101.0)]


def forecast_from_run(run, contest_id: str | int, pool):
    """The frozen field forecast of one contest as a models.field.Marginals, or None when the run saved none."""
    from nhl_dfs.models.field import Marginals

    p = run.path / "scenario" / "fields.json"
    if not p.exists():
        return None
    f = json.loads(p.read_text(encoding="utf-8"))
    cid = str(contest_id)
    own = (f.get("own_by") or {}).get(cid)
    if own is None:
        return None
    n_opp = int((f.get("n_opponents") or {}).get(cid, 0))
    person_own: dict[str, float] = {}
    cpt: dict[str, float] = {}
    for rid, v in own.items():
        r = pool.by_role_id.get(rid)
        if r is None:
            continue
        person_own[r.person_key] = person_own.get(r.person_key, 0.0) + float(v)
        if "CPT" in r.roster_positions:
            cpt[r.person_key] = float(v)
    return Marginals((f.get("contest_family") or {}).get(cid, "?"), n_opp, n_opp + 1, {k: float(v) for k, v in own.items()},
                     person_own, cpt, {k: float(v) for k, v in ((f.get("dup_by") or {}).get(cid) or {}).items()},
                     {}, {}, 0, False)


@dataclass
class OwnershipGrade:
    contest_id: str
    family: str
    mode: str
    n_roles: int
    complete: bool
    mae_all: float
    mae_active: float
    mae_popular: float  # over the actual top 20
    weighted_mae: float  # actual-weighted
    band_calibration: list[dict]
    top_chalk_recall: float
    cpt_share_err: float | None
    team_total_mae: float
    zero_observed_mass: float | None
    pearson: float
    spearman: float
    dup_count_err: dict
    mass_outside_pool: float
    notes: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return asdict(self)


def _corr(a, b) -> tuple[float, float]:
    from scipy.stats import pearsonr, spearmanr

    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan"), float("nan")
    return float(pearsonr(a, b)[0]), float(spearmanr(a, b)[0])


def grade(forecast, actual, family: str | None = None, *, pool) -> OwnershipGrade:
    """forecast: models.field.Marginals (forecast_from_run); actual: learn.standings.Joined."""
    notes = []
    roles = sorted(set(forecast.own) | set(actual.own))
    if not actual.complete:
        roles = sorted(set(actual.own))
        notes.append("zero ownership not observed (CONFLICTED names): only drafted roles are graded")
    p = np.array([forecast.own.get(r, 0.0) for r in roles])
    a = np.array([actual.own.get(r, 0.0) for r in roles])
    err = np.abs(p - a)
    active = (a > 0) | (p >= 0.5)
    top = np.argsort(-a)[:20]
    bands = []
    for lo, hi in BANDS:
        m = (p >= lo) & (p < hi)
        if m.any():
            bands.append({"band": f"{lo:g}-{hi:g}%", "n": int(m.sum()), "mean_pred": round(float(p[m].mean()), 2),
                          "mean_actual": round(float(a[m].mean()), 2)})
    top_p = set(np.array(roles)[np.argsort(-p)[:10]])
    top_a = set(np.array(roles)[np.argsort(-a)[:10]])
    cpt_err = None
    if actual.mode is Mode.SHOWDOWN:
        cpt = [r for r in roles if r in pool.by_role_id and "CPT" in pool.by_role_id[r].roster_positions]
        cpt_err = float(np.mean([abs(forecast.own.get(r, 0.0) - actual.own.get(r, 0.0)) for r in cpt])) if cpt else None
    teams: dict[str, list[float]] = {}
    for r, x, y in zip(roles, p, a):
        if r in pool.by_role_id:
            t = teams.setdefault(pool.by_role_id[r].team, [0.0, 0.0])
            t[0] += x
            t[1] += y
    team_mae = float(np.mean([abs(x - y) for x, y in teams.values()])) if teams else float("nan")
    zero_mass = float(p[a == 0].sum()) if actual.complete else None
    pr, sp = _corr(p, a)
    # slot mass drafted on players the run's salary file does not list (DK added them after the download)
    slots = 9 if actual.mode is Mode.CLASSIC else 6
    lineup_mass = 100.0 * slots * len(actual.lineups) / actual.entries_n if actual.entries_n else 0.0
    outside = round(lineup_mass - float(sum(actual.own.values())), 2)
    # duplicates: the forecast's expected counts are at its own field size; rescale to the actual entry count
    scale = actual.entries_n / forecast.field_size if forecast.field_size else float("nan")
    pred_top = sorted(forecast.dup_counts.items(), key=lambda kv: -kv[1])[:5]
    act_top = sorted(actual.dup_counts.items(), key=lambda kv: -kv[1])[:5]
    dup = {"forecast_field_size": forecast.field_size, "actual_entries": actual.entries_n, "scale": round(scale, 4),
           "top_predicted": [{"lineup": k, "pred_scaled": round(v * scale, 2), "actual": actual.dup_counts.get(k, 0)}
                             for k, v in pred_top],
           "top_actual": [{"lineup": k, "actual": v, "pred_scaled": round(forecast.dup_counts.get(k, 0.0) * scale, 2)}
                          for k, v in act_top],
           "mae_top_predicted": round(float(np.mean([abs(v * scale - actual.dup_counts.get(k, 0)) for k, v in pred_top])), 3)
           if pred_top else None,
           "note": "forecast counts are expected entries at the forecast's field size, rescaled linearly; a lineup "
                   "absent from the forecast's top list counts as 0"}
    if forecast.field_size and abs(forecast.field_size - actual.entries_n) > 0.2 * actual.entries_n:
        notes.append(f"the forecast assumed {forecast.field_size} entries, the contest had {actual.entries_n}")
    return OwnershipGrade(str(actual.contest_id), family or forecast.family, actual.mode.value, len(roles), actual.complete,
                          round(float(err.mean()), 3), round(float(err[active].mean()), 3) if active.any() else float("nan"),
                          round(float(err[top].mean()), 3), round(float((a * err).sum() / a.sum()), 3) if a.sum() else float("nan"),
                          bands, len(top_p & top_a) / 10.0, round(cpt_err, 3) if cpt_err is not None else None,
                          round(team_mae, 3), round(zero_mass, 2) if zero_mass is not None else None,
                          round(pr, 3), round(sp, 3), dup, outside, notes)
