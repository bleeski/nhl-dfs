"""Deterministic tie-break on a scenario metric (C8, plan section 7 "Deterministic tie-break").

Two candidates whose metric values are within the materiality band are ordered by lower
projected ownership, then by lower duplicate risk (large GPP), or duplicate risk first (WTA and
small field, config/contest_families.yaml `selection`), or not by ownership at all (cash and
satellite, policy "mean"). The band is never smaller than the metric's Monte Carlo standard error:
    band = max(band_pct x |anchor value|, se_mult x anchor standard error).
Ownership never moves a choice past one band: `rank` anchors each band at the best remaining
candidate, so no candidate is ever placed ahead of one more than one band better.

Duplicate risk: the sampled duplicate count once FIELD_CALIBRATION=FITTED, else the pre-fit proxy
(models.field.dup_proxy: sum of log ownership + salary-left term + Captain log ownership).
This is code, not an LLM decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from nhl_dfs.contracts.statuses import FieldCalibration

POLICIES = ("mean", "dup_first", "own_then_dup")


def band_width(anchor_value: float, anchor_se: float, cfg: dict) -> float:
    tb = cfg["tiebreak"]
    return max(float(tb["band_pct"]) * abs(float(anchor_value)), float(tb["se_mult"]) * float(anchor_se))


def _inside_key(policy: str, value: float, own: float, dup: float) -> tuple:
    if policy == "mean":
        return (-value,)
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


@dataclass(frozen=True)
class Ranked:
    item: Item
    band: float
    band_index: int
    anchor: float


def rank(items: Sequence[Item], cfg: dict, policy: str) -> list[Ranked]:
    """Anchored bands: the best remaining value opens a band; every remaining item within it joins and
    is ordered by the policy; the next band opens at the best item left."""
    remaining = sorted(items, key=lambda it: (-it.value, it.key))
    out: list[Ranked] = []
    k = 0
    while remaining:
        anchor = remaining[0]
        width = band_width(anchor.value, anchor.se, cfg)  # policy "mean" orders by value inside it anyway
        members = [it for it in remaining if it.value >= anchor.value - width]
        members.sort(key=lambda it: (_inside_key(policy, it.value, it.own, it.dup), it.key))
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
