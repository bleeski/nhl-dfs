"""Contest families (C3): classify each entry's contest from exact DK detail or a declared prior.

A ContestDetail from the DK contest endpoint gives PAYOUT_SOURCE=EXACT, the real field size and
a family read off the payout table. Without it (C16, flag 14), DraftKings' cached table of the same
template (models/payout_templates: same name without the game suffix, same max entries, fee and prize pool)
gives PAYOUT_SOURCE=TEMPLATE, the lobby's max entries as the field size and a family read off that table; a
satellite never matches. Otherwise the family comes from config/contest_families.yaml name patterns (or its
declared default) and the field size from that family's prior, all labeled PRIOR. Missing is never passed off
as exact.

The field size has its own label (C38, flag 29): FIELD_SIZE_SOURCE=EXACT (the contest page), LOBBY (the lobby row's
max entries, read from the same local capture C16 uses; also what a TEMPLATE contest carries) or PRIOR (the family's
declared size). A contest the page did not describe and no template matched takes its size from its lobby row when the
row agrees with the entries file (fee, name, and room for at least one opponent), and a contest whose family came only
from the declared default is small_field at or under the exact path's own cut. PAYOUT_SOURCE stays PRIOR there: only the
size and family move, never the payout curve.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.sources.dk_public import ContestDetail
from nhl_dfs.models.payout_templates import LobbyRow, TemplateMatch, TemplateStore, template_key

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTEST_FAMILIES_YAML = REPO_ROOT / "config" / "contest_families.yaml"

FAMILIES = ("cash", "satellite", "wta", "small_field", "large_gpp")
SELECTIONS = ("mean", "dup_first", "own_then_dup")
# ContestContext.field_size_source (stored, kept for old manifests) -> the printed FIELD_SIZE_SOURCE (C38)
FIELD_SIZE_LABELS = {"contest_detail": "EXACT", "lobby": "LOBBY", "family_prior": "PRIOR"}


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
    field_size_source: str  # "contest_detail" | "lobby" (the lobby row's max entries) | "family_prior"
    detail: ContestDetail | FamilyPrior  # a TEMPLATE contest carries the matched table's ContestDetail
    template: TemplateMatch | None = None  # set exactly when payout_source is TEMPLATE
    payout_note: str = ""  # why a contest stayed PRIOR although templates were looked up
    size_note: str = ""  # the lobby row behind a LOBBY size, or why the size stayed a family prior (C38)

    @property
    def field_size_label(self) -> str:
        """The printed FIELD_SIZE_SOURCE: EXACT (contest page), LOBBY (lobby row) or PRIOR (family prior)."""
        return FIELD_SIZE_LABELS[self.field_size_source]

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
            "FIELD_SIZE_SOURCE": self.field_size_label,
        }
        if self.template is not None:
            out["payout_template"] = self.template.record()
        if self.payout_note:
            out["payout_note"] = self.payout_note
        if self.size_note:
            out["field_size_note"] = self.size_note
        return out

    def size_line(self) -> str:
        """The one printed line per contest: FIELD_SIZE_SOURCE=LOBBY contest 196302810 (31 max entries, 1 per user; ...)."""
        why = self.size_note or {"EXACT": "max entries from the contest page", "LOBBY": "lobby row",
                                 "PRIOR": f"family prior for {self.family}"}[self.field_size_label]
        return f"FIELD_SIZE_SOURCE={self.field_size_label} contest {self.contest_id} ({why})"

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
    lf = cfg.get("lobby_field_size")
    if lf is not None and not isinstance((lf or {}).get("enabled"), bool):
        raise ValueError("contest_families: lobby_field_size.enabled must be true or false")
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


def lobby_enabled(cfg: dict) -> bool:
    """[BEN] flag 29: take the field size (and, for a default-family contest, the small-field cut) from the lobby row
    when the contest page is unavailable (default yes). Independent of flag 14 (payout templates)."""
    return bool((cfg.get("lobby_field_size") or {}).get("enabled", True))


def family_from_lobby(name: str, row: LobbyRow, cfg: dict) -> tuple[str, str]:
    """(family, source) for a contest the page did not describe, given its lobby row. A family the NAME names (cash,
    satellite, wta, small_field) is kept: the name is the stronger signal and only the size comes from the row. A
    contest whose family is only the declared default is cut by the exact path's own rule (family_from_detail:
    max entries <= exact_rules.small_field_max_entries is small_field), so it is classed the same whether its page
    answered or not. Max entries only: max per user is recorded in the note, not a threshold."""
    fam, src = family_from_name(name, cfg)
    if src == "default" and 0 < row.max_entries <= int(cfg["exact_rules"]["small_field_max_entries"]):
        return "small_field", "lobby"
    return fam, src


def lobby_note(row: LobbyRow) -> str:
    snap = f"; lobby capture {row.snapshot}" if row.snapshot else ""
    return f"{row.max_entries:,} max entries, {row.max_per_user} per user{snap}"


def usable_lobby_row(contest_id: str, name: str, fee: str, own_entries: int, lobby: Mapping[int, LobbyRow] | None,
                     cfg: dict) -> LobbyRow | str:
    """The contest's lobby row when it can size the contest, else the reason it cannot (the contest then keeps its
    family prior and says why). The row must agree with the entries file: same fee, same name (game suffix and stray
    spaces stripped), a usable max, and room for at least one opponent beside our own entries."""
    if not lobby_enabled(cfg):
        return "lobby field size is switched off (contest_families.yaml lobby_field_size)"
    if lobby is None:
        return "no lobby capture was read here"
    if not str(contest_id).isdigit():
        return "the contest id is not numeric, so no lobby row can be looked up"
    row = lobby.get(int(contest_id))
    if row is None:
        return "no lobby capture lists this contest"
    entry_fee = fee_value(fee)
    if entry_fee is not None and entry_fee != row.fee:
        return f"the entries file fee {entry_fee} differs from the lobby fee {row.fee}"
    if template_key(name, 0)[0] != template_key(row.name, 0)[0]:
        return f"the entries file name {name.strip()!r} differs from the lobby name {row.name!r}"
    if row.max_entries < 1:
        return f"the lobby row has no usable max entries ({row.max_entries})"
    if own_entries >= row.max_entries:
        return (f"the entries file holds {own_entries} of this contest's {row.max_entries} max entries, "
                "so the lobby size leaves no opponent")
    return row


def resolve(entries, details: Mapping[str, ContestDetail] | None = None, cfg: dict | None = None,
            templates: TemplateStore | None = None, lobby: Mapping[int, LobbyRow] | None = None) -> dict[str, ContestContext]:
    """ContestContext per distinct contest id in an EntriesFile (or EntryRows), file order. A contest the endpoint did
    not describe asks `templates` (when given and enabled in the config) for its template's cached table before it
    falls to its family prior; a contest with no table is then sized from its lobby row (C38: `lobby`, else the
    store's own lobby rows) when the row agrees with the entries file, and only otherwise from the family prior."""
    cfg = cfg if cfg is not None else load_contest_families()
    details = details or {}
    use_templates = templates is not None and templates_enabled(cfg)
    lobby_map = lobby if lobby is not None else (templates.lobby if templates is not None else None)
    rows = list(getattr(entries, "entries", entries))
    own_n = Counter(str(e.contest_id) for e in rows)
    out: dict[str, ContestContext] = {}
    for e in rows:
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
                                          PayoutSource.TEMPLATE, int(m.lobby.max_entries), "lobby", td, m, "",
                                          lobby_note(m.lobby))
                continue
            note = m
        sized = usable_lobby_row(cid, e.contest_name, e.fee, own_n[cid], lobby_map, cfg)
        if isinstance(sized, LobbyRow):
            fam, src = family_from_lobby(p.name, sized, cfg)
            p = FamilyPrior(cid, p.name, fam, src, int(sized.max_entries), float(cfg["families"][fam]["paid_fraction_prior"]))
            out[cid] = ContestContext(cid, p.name, e.fee, p.family, p.family_source, PayoutSource.PRIOR,
                                      p.field_size, "lobby", p, None, note, lobby_note(sized))
            continue
        out[cid] = ContestContext(cid, p.name, e.fee, p.family, p.family_source, PayoutSource.PRIOR,
                                  p.field_size, "family_prior", p, None, note, sized)
    return out


def field_size_lines(contexts: Iterable[ContestContext]) -> list[str]:
    """One report line per contest (the CLI prints each as `note:`; the manifest keeps the label)."""
    return [c.size_line() for c in contexts]


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
