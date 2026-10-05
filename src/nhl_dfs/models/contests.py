"""Contest families (C3): classify each entry's contest from exact DK detail or a declared prior.

A ContestDetail from the DK contest endpoint gives PAYOUT_SOURCE=EXACT, the real field size and
a family read off the payout table. Without it (C16, flag 14), DraftKings' cached table of the same
template (models/payout_templates: same name without the game suffix, same max entries, fee and prize pool)
gives PAYOUT_SOURCE=TEMPLATE, the lobby's max entries as the field size and a family read off that table; a
satellite never matches. Otherwise the family comes from config/contest_families.yaml name patterns (or its
declared default) and the field size from that family's prior, all labeled PRIOR. Missing is never passed off
as exact.
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
from nhl_dfs.models.payout_templates import TemplateMatch, TemplateStore

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
    family_source: str  # "contest_detail" | "template" | "name_pattern" | "default"
    payout_source: PayoutSource
    field_size: int
    field_size_source: str  # "contest_detail" | "lobby" (a TEMPLATE contest's max entries) | "family_prior"
    detail: ContestDetail | FamilyPrior  # a TEMPLATE contest carries the matched table's ContestDetail
    template: TemplateMatch | None = None  # set exactly when payout_source is TEMPLATE
    payout_note: str = ""  # why a contest stayed PRIOR although templates were looked up

    def record(self) -> dict:
        out = {
            "contest_id": self.contest_id,
            "name": self.name,
            "fee": self.fee,
            "family": self.family,
            "family_source": self.family_source,
            "PAYOUT_SOURCE": self.payout_source.value,
            "field_size": self.field_size,
            "field_size_source": self.field_size_source,
        }
        if self.template is not None:
            out["payout_template"] = self.template.record()
        if self.payout_note:
            out["payout_note"] = self.payout_note
        return out

    def payout_line(self) -> str:
        """The one printed line per contest: PAYOUT_SOURCE=TEMPLATE contest 196228907 (table of contest ...)."""
        if self.template is not None:
            note = f"; {self.template.note}" if self.template.note else ""
            return f"PAYOUT_SOURCE=TEMPLATE contest {self.contest_id} ({self.template.label()}{note})"
        why = f" ({self.payout_note})" if self.payout_note else ""
        return f"PAYOUT_SOURCE={self.payout_source.value} contest {self.contest_id}{why}"


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
    pt = cfg.get("payout_templates")
    if pt is not None and not isinstance((pt or {}).get("enabled"), bool):
        raise ValueError("contest_families: payout_templates.enabled must be true or false")
    sel = cfg.get("selection") or {}
    pct = sel.get("tie_band_pct")
    if not isinstance(pct, (int, float)) or not 0 <= pct < 1:
        raise ValueError("contest_families: selection.tie_band_pct must be in [0, 1)")
    sims = sel.get("band_floor_sims")
    if sims is not None and (not isinstance(sims, int) or sims < 1):
        raise ValueError("contest_families: selection.band_floor_sims must be null or a positive integer")
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


def templates_enabled(cfg: dict) -> bool:
    """[BEN] flag 14: price on DraftKings' cached template table when the contest page is unavailable (default yes)."""
    return bool((cfg.get("payout_templates") or {}).get("enabled", True))


def is_satellite_name(name: str, cfg: dict) -> bool:
    fam, src = family_from_name(name, cfg)
    return fam == "satellite" and src == "name_pattern"


def family_prior(contest_id: str, name: str, cfg: dict) -> FamilyPrior:
    fam, src = family_from_name(name, cfg)
    spec = cfg["families"][fam]
    return FamilyPrior(contest_id, name, fam, src, int(spec["field_size_prior"]), float(spec["paid_fraction_prior"]))


def resolve(entries, details: Mapping[str, ContestDetail] | None = None, cfg: dict | None = None,
            templates: TemplateStore | None = None) -> dict[str, ContestContext]:
    """ContestContext per distinct contest id in an EntriesFile (or EntryRows), file order. A contest the endpoint did
    not describe asks `templates` (when given and enabled in the config) for its template's cached table before it
    falls to its family prior."""
    cfg = cfg if cfg is not None else load_contest_families()
    details = details or {}
    use_templates = templates is not None and templates_enabled(cfg)
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
            continue
        p = family_prior(cid, e.contest_name.strip(), cfg)
        note = ""
        if use_templates and str(cid).isdigit():
            row = templates.lobby.get(int(cid))
            sat = is_satellite_name(e.contest_name, cfg) or (row is not None and is_satellite_name(row.name, cfg))
            m = templates.match(int(cid), satellite=sat, fee=fee_value(e.fee))
            if not isinstance(m, str):
                td = m.table.detail
                out[cid] = ContestContext(cid, p.name, e.fee, family_from_detail(td, cfg), "template",
                                          PayoutSource.TEMPLATE, int(m.lobby.max_entries), "lobby", td, m)
                continue
            note = m
        out[cid] = ContestContext(cid, p.name, e.fee, p.family, p.family_source, PayoutSource.PRIOR,
                                  p.field_size, "family_prior", p, None, note)
    return out


def combine_payout_sources(sources: Iterable[PayoutSource]) -> PayoutSource:
    """The weakest link: EXACT only when every contest's payout came from the endpoint; TEMPLATE when every contest is
    EXACT or TEMPLATE and at least one is TEMPLATE; PRIOR otherwise (and for no contest at all)."""
    ss = list(sources)
    if not ss or any(x is PayoutSource.PRIOR for x in ss):
        return PayoutSource.PRIOR
    return PayoutSource.TEMPLATE if any(x is PayoutSource.TEMPLATE for x in ss) else PayoutSource.EXACT


def overall_payout_source(contexts: Iterable[ContestContext]) -> PayoutSource:
    """The run's PAYOUT_SOURCE: the weakest of its contests' (see combine_payout_sources)."""
    return combine_payout_sources(c.payout_source for c in contexts)


def payout_lines(contexts: Iterable[ContestContext]) -> list[str]:
    """One report line per contest (the CLI prints each as `note:`; the manifest keeps them)."""
    return [c.payout_line() for c in contexts]


def fee_value(fee: str) -> Decimal | None:
    try:
        return Decimal(fee.replace("$", "").replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
