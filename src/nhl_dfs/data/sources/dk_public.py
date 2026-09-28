"""DraftKings public, keyless endpoints: lobby, contest detail, draftables.

Undocumented interfaces (plan section 3). The salary CSV governs eligibility
and salary; draftables is the scratch and lock signal keyed by exact DK ID, and
a disagreement is reported as CONFLICTED, never used to rewrite the CSV.
Money is Decimal dollars parsed from DK's strings; times are UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from nhl_dfs.contracts.statuses import Eligibility, ObsStatus, Participation
from nhl_dfs.data.http import HttpCache, SourceSchemaError, default_cache

STATUS_MAP = {
    "OUT": Participation.OUT,
    "IR": Participation.OUT,
    "O": Participation.OUT,
    "Q": Participation.QUESTIONABLE,
    "GTD": Participation.QUESTIONABLE,
    "D": Participation.QUESTIONABLE,
    "None": Participation.PLAYING,
    "": Participation.PLAYING,
}
SHOWDOWN_CPT_SLOT = 612
SHOWDOWN_FLEX_SLOT = 613

_MS_DATE = re.compile(r"^/Date\((\d+)\)/$")
_ISO_FRACTION = re.compile(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def map_status(raw: Any) -> tuple[Participation, str]:
    """(participation, raw text). JSON null is the string "None"; unknown -> UNKNOWN."""
    text = "None" if raw is None else str(raw)
    return STATUS_MAP.get(text, Participation.UNKNOWN), text


def parse_ms_date(text: str) -> datetime:
    m = _MS_DATE.match(text or "")
    if not m:
        raise SourceSchemaError(f"unrecognized /Date(ms)/ value {text!r}")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)


def parse_iso_utc(text: str) -> datetime:
    m = _ISO_FRACTION.match(text or "")
    if not m:
        raise SourceSchemaError(f"unrecognized timestamp {text!r}")
    base, frac, tz = m.group(1), (m.group(2) or "")[:6], m.group(3) or "Z"
    stamp = base + (f".{frac}" if frac else "") + ("+00:00" if tz == "Z" else tz)
    dt = datetime.fromisoformat(stamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_money(text: str) -> Decimal:
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise SourceSchemaError(f"unparseable money {text!r}") from exc


def _require(obj: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SourceSchemaError(f"{where}: missing {missing}")


# --- lobby ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContestSummary:
    id: int
    name: str
    fee: Decimal
    field_size: int
    max_per_user: int
    prize_pool: Decimal
    draft_group_id: int
    game_type: str
    start_utc: datetime


def parse_lobby(data: Any) -> list[ContestSummary]:
    if not isinstance(data, dict) or not isinstance(data.get("Contests"), list):
        raise SourceSchemaError("lobby: no Contests list")
    out = []
    for c in data["Contests"]:
        _require(c, ("id", "n", "a", "m", "mec", "po", "dg", "gameType", "sd"), "lobby contest")
        out.append(
            ContestSummary(
                id=int(c["id"]),
                name=str(c["n"]),
                fee=Decimal(str(c["a"])),
                field_size=int(c["m"]),
                max_per_user=int(c["mec"]),
                prize_pool=Decimal(str(c["po"])),
                draft_group_id=int(c["dg"]),
                game_type=str(c["gameType"]),
                start_utc=parse_ms_date(c["sd"]),
            )
        )
    return out


def lobby(*, cache: HttpCache | None = None) -> list[ContestSummary]:
    cache = cache or default_cache()
    cfg = cache.config
    f = cache.get_json(cfg["urls"]["dk_lobby"], source="dk_lobby", ttl_s=cfg["ttl_s"]["dk_lobby"], schema=parse_lobby)
    return parse_lobby(f.data)


# --- contest detail ------------------------------------------------------------------


@dataclass(frozen=True)
class PayoutTier:
    min_pos: int
    max_pos: int
    cash: Decimal  # Decimal("0") for a tier with no cash component
    other: str | None  # non-cash prize text (tickets), verbatim


@dataclass(frozen=True)
class ContestDetail:
    contest_id: int
    name: str
    payout_table: list[PayoutTier]
    maximum_entries: int
    max_per_user: int
    entry_fee: Decimal
    entries: int
    draft_group_id: int
    start_utc: datetime
    raw: dict = field(repr=False)


def parse_contest_detail(data: Any) -> ContestDetail:
    if not isinstance(data, dict) or not isinstance(data.get("contestDetail"), dict):
        raise SourceSchemaError("contest detail: no contestDetail object")
    d = data["contestDetail"]
    _require(
        d,
        ("contestKey", "name", "payoutSummary", "maximumEntries", "maximumEntriesPerUser",
         "entryFee", "entries", "draftGroupId", "contestStartTime"),
        "contest detail",
    )
    tiers = []
    for t in d["payoutSummary"]:
        _require(t, ("minPosition", "maxPosition", "tierPayoutDescriptions"), "payout tier")
        desc = t["tierPayoutDescriptions"] or {}
        cash = parse_money(desc["Cash"]) if "Cash" in desc else Decimal("0")
        other = {k: v for k, v in desc.items() if k != "Cash"}
        tiers.append(PayoutTier(int(t["minPosition"]), int(t["maxPosition"]), cash, str(other) if other else None))
    return ContestDetail(
        contest_id=int(d["contestKey"]),
        name=str(d["name"]),
        payout_table=tiers,
        maximum_entries=int(d["maximumEntries"]),
        max_per_user=int(d["maximumEntriesPerUser"]),
        entry_fee=Decimal(str(d["entryFee"])),
        entries=int(d["entries"]),
        draft_group_id=int(d["draftGroupId"]),
        start_utc=parse_iso_utc(d["contestStartTime"]),
        raw=data,
    )


def contest_detail(contest_id: int, *, cache: HttpCache | None = None) -> ContestDetail:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["dk_contest"].format(contest_id=int(contest_id))
    f = cache.get_json(url, source="dk_contest", ttl_s=cfg["ttl_s"]["dk_contest"], schema=parse_contest_detail)
    return parse_contest_detail(f.data)


# --- draftables ------------------------------------------------------------------------


@dataclass(frozen=True)
class Draftable:
    draftable_id: str
    player_id: str
    name: str
    position: str
    roster_slot_id: int
    salary: int
    status_raw: str
    participation: Participation
    eligibility: Eligibility
    is_swappable: bool | None
    news_status: str | None
    team: str
    competition_id: str | None
    start_utc: datetime | None


@dataclass(frozen=True)
class Competition:
    competition_id: str
    name: str
    start_utc: datetime | None


@dataclass
class Draftables:
    draft_group_id: int | None
    rows: list[Draftable]
    competitions: dict[str, Competition]

    @property
    def by_id(self) -> dict[str, Draftable]:
        return {r.draftable_id: r for r in self.rows}


def _competition(obj: dict | None) -> Competition | None:
    if not obj:
        return None
    start = obj.get("startTime")
    return Competition(str(obj.get("competitionId")), str(obj.get("name", "")), parse_iso_utc(start) if start else None)


def parse_draftables(data: Any, draft_group_id: int | None = None) -> Draftables:
    if not isinstance(data, dict) or not isinstance(data.get("draftables"), list):
        raise SourceSchemaError("draftables: no draftables list")
    comps: dict[str, Competition] = {}
    for c in data.get("competitions") or []:
        comp = _competition(c)
        if comp:
            comps[comp.competition_id] = comp
    rows = []
    for d in data["draftables"]:
        _require(d, ("draftableId", "playerId", "displayName", "position", "rosterSlotId", "salary", "status"), "draftable")
        participation, raw = map_status(d["status"])
        comp = _competition(d.get("competition"))
        if comp and comp.competition_id not in comps:
            comps[comp.competition_id] = comp
        rows.append(
            Draftable(
                draftable_id=str(d["draftableId"]),
                player_id=str(d["playerId"]),
                name=str(d["displayName"]),
                position=str(d["position"]),
                roster_slot_id=int(d["rosterSlotId"]),
                salary=int(d["salary"]),
                status_raw=raw,
                participation=participation,  # isSwappable never feeds this
                eligibility=Eligibility.DISABLED if d.get("isDisabled") else Eligibility.ROSTERABLE,
                is_swappable=d.get("isSwappable"),
                news_status=d.get("newsStatus"),
                team=str(d.get("teamAbbreviation", "")),
                competition_id=comp.competition_id if comp else None,
                start_utc=comp.start_utc if comp else None,
            )
        )
    return Draftables(draft_group_id, rows, comps)


def draftables(draft_group_id: int, *, cache: HttpCache | None = None) -> Draftables:
    cache = cache or default_cache()
    cfg = cache.config
    url = cfg["urls"]["dk_draftables"].format(draft_group_id=int(draft_group_id))
    f = cache.get_json(url, source="dk_draftables", ttl_s=cfg["ttl_s"]["dk_draftables"], schema=parse_draftables)
    return parse_draftables(f.data, int(draft_group_id))


# --- reconciliation ----------------------------------------------------------------------


@dataclass(frozen=True)
class RowState:
    participation: Participation
    eligibility: Eligibility | None  # None when draftables has no row for this ID
    start_utc: datetime | None
    status_raw: str | None
    obs_status: ObsStatus  # CURRENT, CONFLICTED (salary/team disagree), MISSING (no draftable)
    notes: tuple[str, ...] = ()


@dataclass
class Reconciliation:
    rows: dict[str, RowState]  # by salary-file role_id
    unmatched_draftable_ids: list[str]

    @property
    def conflicted(self) -> list[str]:
        return [rid for rid, s in self.rows.items() if s.obs_status is ObsStatus.CONFLICTED]


def reconcile(pool, d: Draftables) -> Reconciliation:
    """Match salary-file rows to draftables by draftable_id == role_id. The pool
    is never modified: disagreements become CONFLICTED rows with notes."""
    by_id = d.by_id
    rows: dict[str, RowState] = {}
    for r in pool.rows:
        dr = by_id.get(r.role_id)
        if dr is None:
            rows[r.role_id] = RowState(Participation.UNKNOWN, None, None, None, ObsStatus.MISSING, ("no draftable",))
            continue
        notes = []
        if dr.salary != r.salary:
            notes.append(f"salary csv={r.salary} draftables={dr.salary}")
        if dr.team and dr.team != r.team:
            notes.append(f"team csv={r.team} draftables={dr.team}")
        rows[r.role_id] = RowState(
            participation=dr.participation,
            eligibility=dr.eligibility,
            start_utc=dr.start_utc,
            status_raw=dr.status_raw,
            obs_status=ObsStatus.CONFLICTED if notes else ObsStatus.CURRENT,
            notes=tuple(notes),
        )
    pool_ids = {r.role_id for r in pool.rows}
    return Reconciliation(rows, sorted(i for i in by_id if i not in pool_ids))
