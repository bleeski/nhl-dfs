"""Contest families (C3): classify each entry's contest from exact DK detail or a declared prior.

A ContestDetail from the DK contest endpoint gives PAYOUT_SOURCE=EXACT, the real field size and
a family read off the payout table. Without it, the family comes from config/contest_families.yaml
name patterns (or its declared default) and the field size from that family's prior, all
labeled PRIOR. Missing is never passed off as exact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.sources.dk_public import ContestDetail

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTEST_FAMILIES_YAML = REPO_ROOT / "config" / "contest_families.yaml"

FAMILIES = ("cash", "satellite", "wta", "small_field", "large_gpp")
SELECTIONS = ("mean", "dup_first", "own_then_dup")


@dataclass(frozen=True)
class FamilyPrior:
    """What the declared priors say about a contest the endpoint did not describe."""

    contest_id: str
    name: str
    family: str
    family_source: str  # "name_pattern" | "default"
    field_size: int
    paid_fraction: float


@dataclass(frozen=True)
class ContestContext:
    contest_id: str
    name: str
    fee: str  # as written in DKEntries.csv
    family: str
    family_source: str  # "contest_detail" | "name_pattern" | "default"
    payout_source: PayoutSource
    field_size: int
    field_size_source: str  # "contest_detail" | "family_prior"
    detail: ContestDetail | FamilyPrior

    def record(self) -> dict:
        return {
            "contest_id": self.contest_id,
            "name": self.name,
            "fee": self.fee,
            "family": self.family,
            "family_source": self.family_source,
            "PAYOUT_SOURCE": self.payout_source.value,
            "field_size": self.field_size,
            "field_size_source": self.field_size_source,
        }


def load_contest_families(path: Path = CONTEST_FAMILIES_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_contest_families(cfg)
    return cfg


def validate_contest_families(cfg: dict) -> None:
    fams = cfg.get("families") or {}
    for fam in FAMILIES:
        spec = fams.get(fam)
        if not spec:
            raise ValueError(f"contest_families: family {fam!r} missing")
        if spec.get("selection") not in SELECTIONS:
            raise ValueError(f"contest_families: {fam}.selection must be one of {SELECTIONS}")
        if int(spec.get("field_size_prior", 0)) < 1:
            raise ValueError(f"contest_families: {fam}.field_size_prior must be >= 1")
    if cfg.get("default_family") not in FAMILIES:
        raise ValueError("contest_families: default_family must name a family")
    for p in cfg.get("name_patterns") or []:
        if p.get("family") not in FAMILIES:
            raise ValueError(f"contest_families: pattern for unknown family {p.get('family')!r}")
        re.compile(p["pattern"])
    sel = cfg.get("selection") or {}
    pct = sel.get("tie_band_pct")
    if not isinstance(pct, (int, float)) or not 0 <= pct < 1:
        raise ValueError("contest_families: selection.tie_band_pct must be in [0, 1)")
    q = sel.get("questionable_play_prob")
    if not isinstance(q, (int, float)) or not 0 < q <= 1:
        raise ValueError("contest_families: selection.questionable_play_prob must be in (0, 1]")
    if sorted(sel.get("family_order") or []) != sorted(FAMILIES):
        raise ValueError("contest_families: selection.family_order must list every family once")


def family_from_name(name: str, cfg: dict) -> tuple[str, str]:
    """(family, source) from the contest name alone."""
    for p in cfg.get("name_patterns") or []:
        if re.search(p["pattern"], name, flags=re.IGNORECASE):
            return p["family"], "name_pattern"
    return cfg["default_family"], "default"


def family_from_detail(detail: ContestDetail, cfg: dict) -> str:
    fam, src = family_from_name(detail.name, cfg)
    if fam == "satellite" and src == "name_pattern":
        return fam
    tiers = [t for t in detail.payout_table if t.cash > 0 or t.other]
    rules = cfg["exact_rules"]
    if tiers:
        paid = max(t.max_pos for t in tiers)
        if paid == 1:
            return "wta"
        cash_amounts = {t.cash for t in tiers}
        if len(cash_amounts) == 1 and detail.maximum_entries > 0 and \
                paid / detail.maximum_entries >= float(rules["cash_min_paid_fraction"]):
            return "cash"
    if 0 < detail.maximum_entries <= int(rules["small_field_max_entries"]):
        return "small_field"
    return "large_gpp"


def family_prior(contest_id: str, name: str, cfg: dict) -> FamilyPrior:
    fam, src = family_from_name(name, cfg)
    spec = cfg["families"][fam]
    return FamilyPrior(contest_id, name, fam, src, int(spec["field_size_prior"]), float(spec["paid_fraction_prior"]))


def resolve(entries, details: Mapping[str, ContestDetail] | None = None, cfg: dict | None = None) -> dict[str, ContestContext]:
    """ContestContext per distinct contest id in an EntriesFile (or EntryRows), file order."""
    cfg = cfg if cfg is not None else load_contest_families()
    details = details or {}
    out: dict[str, ContestContext] = {}
    for e in getattr(entries, "entries", entries):
        cid = str(e.contest_id)
        if cid in out:
            continue
        d = details.get(cid)
        if d is not None:
            size = int(d.maximum_entries)
            out[cid] = ContestContext(cid, d.name, e.fee, family_from_detail(d, cfg), "contest_detail",
                                      PayoutSource.EXACT, size, "contest_detail", d)
        else:
            p = family_prior(cid, e.contest_name.strip(), cfg)
            out[cid] = ContestContext(cid, p.name, e.fee, p.family, p.family_source, PayoutSource.PRIOR,
                                      p.field_size, "family_prior", p)
    return out


def overall_payout_source(contexts: Iterable[ContestContext]) -> PayoutSource:
    """EXACT only when every contest's payout came from the endpoint."""
    cs = list(contexts)
    return PayoutSource.EXACT if cs and all(c.payout_source is PayoutSource.EXACT for c in cs) else PayoutSource.PRIOR


def fee_value(fee: str) -> Decimal | None:
    try:
        return Decimal(fee.replace("$", "").replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
