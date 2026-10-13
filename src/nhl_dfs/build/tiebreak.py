"""Deterministic tie-break on a scenario metric (C8, plan section 7 "Deterministic tie-break").

Two candidates whose metric values are within the materiality band are ordered by lower
projected ownership, then by lower duplicate risk (large GPP), or duplicate risk first (WTA and
small field, config/contest_families.yaml `selection`), or not by ownership at all (cash and
satellite, policy "mean"), or by more expected top-N finishes (`alt`) in a "Top N Win $X"
contest (policy "finishes", C47 flag 70; a caller without that figure leaves alt 0 and gets
"mean"). The band is never smaller than the metric's Monte Carlo standard error:
    band = max(band_pct x |anchor value|, se_mult x anchor standard error).
Ownership never moves a choice past one band: `rank` anchors each band at the best remaining
candidate, so no candidate is ever placed ahead of one more than one band better.

Duplicate risk: the sampled duplicate count once FIELD_CALIBRATION=FITTED, else the pre-fit proxy
(models.field.dup_proxy: sum of log ownership + salary-left term + Captain log ownership).
This is code, not an LLM decision.

Chalk core (C22, flag 95, docs/experiments/chalk_core_2026-10-13.md): `chalk_core` names a contest's two skaters with the
largest projected mean x field ownership and `core_held` counts them in a lineup. `rank(..., keep_core=True)` is the
preregistered exemption: inside a band, members holding fewer core pieces than the band's anchor go last. No caller passes
it; the shipping path is unchanged until a measured verdict and Ben open the follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import FieldCalibration

POLICIES = ("mean", "dup_first", "own_then_dup", "finishes")


def band_width(anchor_value: float, anchor_se: float, cfg: dict) -> float:
    tb = cfg["tiebreak"]
    return max(float(tb["band_pct"]) * abs(float(anchor_value)), float(tb["se_mult"]) * float(anchor_se))


def _inside_key(policy: str, value: float, own: float, dup: float, alt: float = 0.0) -> tuple:
    if policy == "mean":
        return (-value,)
    if policy == "finishes":
        return (-alt, -value)
    if policy == "dup_first":
        return (dup, own, -value)
    if policy == "own_then_dup":
        return (own, dup, -value)
    raise ValueError(f"unknown tie-break policy {policy!r}")


def prefer(a: float, b: float, own_a: float, own_b: float, dup_a: float, dup_b: float, band: float,
           policy: str = "own_then_dup") -> Literal["a", "b"]:
    """a, b: metric values (higher is better). Outside the band the better metric wins; inside it the
    policy's ownership / duplicate order decides, then the metric, then "a"."""
    if a > b + band:
        return "a"
    if b > a + band:
        return "b"
    ka, kb = _inside_key(policy, a, own_a, dup_a), _inside_key(policy, b, own_b, dup_b)
    return "b" if kb < ka else "a"


@dataclass(frozen=True)
class Item:
    key: str
    value: float
    se: float
    own: float
    dup: float
    ref: object = None  # caller's payload (candidate index, lineup, ...)
    alt: float = 0.0  # policy "finishes": expected top-N finishes the item adds (higher first inside the band)
    core: int = 0  # C22: chalk core pieces the lineup holds; read only by rank(..., keep_core=True)


@dataclass(frozen=True)
class Ranked:
    item: Item
    band: float
    band_index: int
    anchor: float


def rank(items: Sequence[Item], cfg: dict, policy: str, keep_core: bool = False) -> list[Ranked]:
    """Anchored bands: the best remaining value opens a band; every remaining item within it joins and
    is ordered by the policy; the next band opens at the best item left. keep_core (C22, flag 95, never passed by
    the fill): inside a band, members holding fewer core pieces than the anchor come after the rest, so the band's
    first item never holds fewer than the anchor; the policy order applies within each group."""
    remaining = sorted(items, key=lambda it: (-it.value, it.key))
    out: list[Ranked] = []
    k = 0
    while remaining:
        anchor = remaining[0]
        width = band_width(anchor.value, anchor.se, cfg)  # policy "mean" orders by value inside it anyway
        members = [it for it in remaining if it.value >= anchor.value - width]
        if keep_core:
            members.sort(key=lambda it: (max(0, anchor.core - it.core), _inside_key(policy, it.value, it.own, it.dup, it.alt),
                                         it.key))
        else:
            members.sort(key=lambda it: (_inside_key(policy, it.value, it.own, it.dup, it.alt), it.key))
        out += [Ranked(it, width, k, anchor.value) for it in members]
        taken = {it.key for it in members}
        remaining = [it for it in remaining if it.key not in taken]
        k += 1
    return out


def dup_measure(role_ids: Sequence[str], key: str, own_pct: dict, sampled_counts: dict, field_calibration: FieldCalibration,
                pool, own_cfg: dict | None = None) -> tuple[float, str]:
    """(duplicate risk, which measure): sampled copies at the field size when the field model is FITTED,
    else the pre-fit proxy."""
    from nhl_dfs.models.field import dup_proxy

    if field_calibration is FieldCalibration.FITTED:
        return float(sampled_counts.get(key, 0.0)), "sampled"
    return float(dup_proxy(role_ids, own_pct, pool.mode, pool=pool, cfg=own_cfg)), "proxy"


def chalk_core(role_mean: Mapping[str, float], own: Mapping[str, float], pool, n: int = 2) -> tuple[str, ...]:
    """C22 (flag 95): a contest's chalk core, the n skaters with the largest projected mean (role_mean, tenths) x field
    ownership percent (own, by role id), as person keys; ties by person key. Goalies are never in it (B45) and a person
    with no mean or no ownership never is. Classic only: a Showdown pool has no core (empty tuple)."""
    if pool.mode is not Mode.CLASSIC:
        return ()
    best: dict[str, float] = {}
    for r in pool.rows:
        if r.is_goalie:
            continue
        v = float(role_mean.get(r.role_id, 0.0)) * float(own.get(r.role_id, 0.0))
        if v > best.get(r.person_key, 0.0):
            best[r.person_key] = v
    return tuple(sorted(best, key=lambda p: (-best[p], p))[:n])


def core_held(role_ids: Sequence[str], core: Sequence[str], pool) -> int:
    """C22: how many of the core persons the lineup holds (0 to len(core))."""
    return len({pool.by_role_id[r].person_key for r in role_ids} & set(core))
