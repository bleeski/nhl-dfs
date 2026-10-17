"""The model's write path (C56, backlog B141 and B150; flags 124 to 131 in BUILD_STATUS.md).

`apply_edits` takes the model's edit list, applies it to the run's current delivered version, has the independent
referee check the result and publishes the next version with a compare-and-swap. `restore_previous` puts the previous
version's bytes back. Nothing here edits a CSV by hand and nothing here simulates: no scenario gate decides an edit.

What this module checks (the referee, not this module, judges cap, positions, three skater teams, duplicates, every
slot and the pinned cells; flag 131):
  - the cell is open now and both the player removed and the player added are in games that are neither started nor
    inside the edit stop (build/locks.py; QA's T-8 does not apply, flag 126);
  - the added player is in the pool and is not OUT or IR (the salary Status column and accepted overrides);
  - the printed checks the model may override with a stated reason: the own-goalie rule and the risk budget (flag 125);
  - the B150 guardrails: a dated source for a news edit, the identity of every player printed, an unconfirmed goalie
    flagged and queued for a recheck, every edit logged with its type and claimed gain (flag 130).
Each entry is all-or-nothing and independent of the others (flag 127). The publish refuses, before any byte is written,
when the public file or the run's current version is no longer the parent the edits were built on, when a changed cell
is pinned now, or when a started or edit-stop game is touched on either side of any changed cell (flag 126).
Every edit is logged under runs/<id>/edits/ (flag 128); `engine_v<N>.csv` there is the version before the first edit.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode
from nhl_dfs.contracts.statuses import Participation

EDIT_TYPES = ("coverage", "news", "engine_gap", "other")
OVERRIDE_RULES = ("own_goalie", "risk_budget")
OVERRIDE_MIN_WORDS = 5
EDIT_KEYS = frozenset({"entry_id", "edit_type", "cells", "reason", "evidence", "claimed_gain", "sources", "source_url", "override"})
CELL_KEYS = frozenset({"slot", "player_id", "replaces", "team", "name"})
SOURCE_KEYS = frozenset({"url", "published_utc", "fetched_utc"})
_EPS = 1e-9


class MalformedEdits(ValueError):
    pass


# -- the edit file ---------------------------------------------------------------------------------------------

def slot_labels(mode: Mode) -> list[str]:
    """Canonical slot index -> the label an edit uses: the template label with an ordinal where it repeats."""
    slots = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    total = {s: slots.count(s) for s in slots}
    seen: dict[str, int] = defaultdict(int)
    out = []
    for s in slots:
        seen[s] += 1
        out.append(s if total[s] == 1 else f"{s}{seen[s]}")
    return out


@dataclass
class CellEdit:
    label: str
    index: int  # canonical slot index
    player_id: str
    replaces: str | None = None
    team: str | None = None
    name: str | None = None


@dataclass
class Edit:
    n: int  # position in the file, 0-based
    entry_id: str
    edit_type: str
    cells: list[CellEdit]
    reason: str
    evidence: str
    claimed_gain: str
    sources: list[dict]
    override: dict | None


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _claims_ben(obj: Any) -> bool:
    """True when any field named like a source says BEN (CLAUDE.md: only Ben's own report may carry BEN)."""
    if isinstance(obj, dict):
        return any((str(k).lower().startswith("source") and isinstance(v, str) and v.strip().upper() == "BEN") or _claims_ben(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(_claims_ben(x) for x in obj)
    return False


def _utc(text: Any) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("needs a UTC offset")
    return t.astimezone(timezone.utc)


def parse_edits(text: str, mode: Mode) -> tuple[list[Edit], dict[int, tuple[str, str]]]:
    """(edits that parsed, {position: (entry id, why)} for the ones that did not). The whole file being unreadable
    raises MalformedEdits (nothing is applied). A bare list and {"edits": [...]} are both accepted."""
    try:
        doc = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedEdits(f"not JSON ({exc})") from None
    if isinstance(doc, dict):
        doc = doc.get("edits")
    if not isinstance(doc, list) or not doc:
        raise MalformedEdits("expected a non-empty list of edits")
    labels = {lab: i for i, lab in enumerate(slot_labels(mode))}
    edits: list[Edit] = []
    bad: dict[int, tuple[str, str]] = {}
    for n, raw in enumerate(doc):
        eid = str(raw.get("entry_id")) if isinstance(raw, dict) and raw.get("entry_id") not in (None, "") else None
        try:
            if not isinstance(raw, dict):
                raise ValueError("edit is not an object")
            if _claims_ben(raw):
                raise ValueError("source BEN is accepted only from overrides-apply --ben (Ben's own report in session)")
            unknown = sorted(set(raw) - EDIT_KEYS)
            if unknown:
                raise ValueError(f"unknown field(s) {unknown}")
            if eid is None:
                raise ValueError("entry_id is missing")
            et = raw.get("edit_type")
            if et not in EDIT_TYPES:
                raise ValueError(f"edit_type must be one of {list(EDIT_TYPES)}")
            for key in ("reason", "evidence", "claimed_gain"):
                if not isinstance(raw.get(key), str) or not raw[key].strip():
                    raise ValueError(f"{key} is missing (every edit states its reason, evidence and claimed gain)")
            cells_raw = raw.get("cells")
            if not isinstance(cells_raw, list) or not cells_raw:
                raise ValueError("cells must be a non-empty list")
            cells: list[CellEdit] = []
            for c in cells_raw:
                if not isinstance(c, dict) or set(c) - CELL_KEYS:
                    raise ValueError(f"a cell must be an object with only {sorted(CELL_KEYS)}")
                if c.get("slot") not in labels:
                    raise ValueError(f"slot {c.get('slot')!r} is not one of {list(labels)}")
                pid = str(c.get("player_id") or "")
                if not pid.isdigit():
                    raise ValueError(f"player_id {c.get('player_id')!r} is not a DraftKings id")
                cells.append(CellEdit(c["slot"], labels[c["slot"]], pid, str(c["replaces"]) if c.get("replaces") else None,
                                      str(c["team"]) if c.get("team") else None, str(c["name"]) if c.get("name") else None))
            if len({c.index for c in cells}) != len(cells):
                raise ValueError("a slot appears twice in one edit")
            sources: list[dict] = []
            for s in raw.get("sources") or []:
                if not isinstance(s, dict) or set(s) - SOURCE_KEYS or not str(s.get("url") or "").startswith(("http://", "https://")):
                    raise ValueError(f"a source needs an http(s) url and only {sorted(SOURCE_KEYS)}")
                sources.append({"url": s["url"], "published_utc": _utc(s["published_utc"]).isoformat() if s.get("published_utc") else None,
                                "fetched_utc": _utc(s["fetched_utc"]).isoformat() if s.get("fetched_utc") else None})
            if raw.get("source_url"):
                if not str(raw["source_url"]).startswith(("http://", "https://")):
                    raise ValueError("source_url must be http(s)")
                sources.append({"url": raw["source_url"], "published_utc": None, "fetched_utc": None})
            if et == "news" and not any(s["published_utc"] for s in sources):
                raise ValueError("a news edit needs a source with a published_utc (a dated, fetchable page)")
            ov = raw.get("override")
            if ov is not None:
                if (not isinstance(ov, dict) or set(ov) - {"rule", "reason"} or ov.get("rule") not in OVERRIDE_RULES
                        or len(str(ov.get("reason") or "").split()) < OVERRIDE_MIN_WORDS):
                    raise ValueError(f"override needs rule {list(OVERRIDE_RULES)} and a reason of at least {OVERRIDE_MIN_WORDS} words")
            edits.append(Edit(n, eid, et, cells, raw["reason"].strip(), raw["evidence"].strip(), raw["claimed_gain"].strip(),
                              sources, ov))
        except (ValueError, TypeError) as exc:
            bad[n] = (eid or f"edit {n + 1}", str(exc))  # an edit with no entry id is reported under its position
    return edits, bad


# -- one entry --------------------------------------------------------------------------------------------------

@dataclass
class EntryOutcome:
    entry_id: str
    status: str  # APPLIED | REJECTED
    rule: str = ""  # the rule that stopped a rejected entry
    reason: str = ""
    cells: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    total: int | None = None
    new_lineup: list[str] | None = None


def _who(row, game: str = "") -> str:
    return f"{row.name} ({row.team} {row.position}, ${row.salary:,}{', ' + game if game else ''}) [{row.role_id}]"


def _game_key(row) -> str:
    from nhl_dfs.intake.salary import parse_game_info

    try:
        return parse_game_info(row.game_info)[0]
    except ValueError:
        return ""


def _concentration(lineups: dict[str, list[str]], v, means: dict[str, float]) -> dict[str, dict[str, tuple[float, int]]]:
    """Per goalie and per primary game: (fee share, entries). Fees and primary game as the engine's caps count them."""
    from nhl_dfs.build import exposure

    games = exposure.game_of(v.pool)
    total = sum(v.fee[e] for e in lineups) or 1.0
    out: dict[str, dict[str, list]] = {"goalie": defaultdict(lambda: [0.0, 0]), "game": defaultdict(lambda: [0.0, 0])}
    for e, lu in lineups.items():
        for pk in exposure.goalies_in(lu, v.pool):
            out["goalie"][pk][0] += v.fee[e] / total
            out["goalie"][pk][1] += 1
        pg = exposure.primary_game(lu, means, games, v.pool)
        if pg:
            out["game"][pg][0] += v.fee[e] / total
            out["game"][pg][1] += 1
    return {k: {a: (b[0], b[1]) for a, b in d.items()} for k, d in out.items()}


def _budget_breaches(before: dict, after: dict, caps: dict) -> list[str]:
    """Breaches the edit causes against the run's own recorded caps (flag 125): a goalie or primary-game concentration that is
    past its cap after the edit and higher than before it. DOLLARS mode caps the fee share, LINEUPS mode the entry count."""
    why = []
    for kind, share_key, count_key, label in (("goalie", "goalie_fee_share", "goalie_lineups", "goalie"),
                                              ("game", "game_fee_share", "game_lineups", "primary game")):
        share_cap, count_cap = caps.get(share_key), caps.get(count_key)
        if share_cap is None and count_cap is None:
            continue
        idx = 0 if share_cap is not None else 1
        cap = float(share_cap) if share_cap is not None else float(count_cap)
        worst_a = max((x[idx] for x in after.get(kind, {}).values()), default=0.0)
        worst_b = max((x[idx] for x in before.get(kind, {}).values()), default=0.0)
        if worst_a > cap + _EPS and worst_a > worst_b + _EPS:
            unit = "fee share" if idx == 0 else "entries"
            why.append(f"{label} {unit} {worst_a:.2f} > the run's cap {cap:.2f} (was {worst_b:.2f})")
    return why


# -- the result -------------------------------------------------------------------------------------------------

@dataclass
class EditResult:
    run_id: str
    k: int
    kind: str = "edit"  # edit | restore
    parent_version: int | None = None
    parent_sha256: str | None = None
    entries: list[EntryOutcome] = field(default_factory=list)
    published_version: int | None = None
    delivery_version: int | None = None
    delivery_path: str | None = None
    sha256: str | None = None
    stop_reason: str = ""
    public_detail: str = ""  # why the public file was not replaced, when it was not
    record_path: str | None = None
    edits_sha256: str | None = None
    recheck: list[dict] = field(default_factory=list)
    lock_soon: list[str] = field(default_factory=list)
    engine_copy: str | None = None

    @property
    def applied(self) -> list[EntryOutcome]:
        return [e for e in self.entries if e.status == "APPLIED"]

    @property
    def rejected(self) -> list[EntryOutcome]:
        return [e for e in self.entries if e.status == "REJECTED"]

    def lines(self) -> list[str]:
        head = "EDIT_RESTORE" if self.kind == "restore" else "EDIT_APPLY"
        out = [f"{head} run={self.run_id} file {self.k}: parent v{self.parent_version}"
               + (f" sha256 {self.parent_sha256[:8]}" if self.parent_sha256 else "")]
        for e in self.entries:
            if e.status == "APPLIED":
                out.append(f"  entry {e.entry_id}: APPLIED (lineup total ${e.total:,})" if e.total is not None
                           else f"  entry {e.entry_id}: APPLIED")
            else:
                out.append(f"  entry {e.entry_id}: REJECTED [{e.rule}]: {e.reason}")
            for c in e.cells:
                out.append(f"    {c['slot']}: {c['out_who']} -> {c['in_who']}")
            for ed in e.edits:
                out.append(f"    {ed['edit_type']}: {ed['reason']} | claimed gain: {ed['claimed_gain']}")
            for f in e.flags:
                out.append(f"    {f}")
        out.append(f"EDITS_APPLIED={len(self.applied)} EDITS_REJECTED={len(self.rejected)}")
        if self.delivery_path:
            out.append(f"published: v{self.published_version} (run version); delivery: {self.delivery_path} (v{self.delivery_version})")
        else:
            out.append(f"published: v{self.published_version} (the public file was not replaced)" if self.published_version
                       else "published: nothing (the checked file stands)")
            if self.published_version:
                out.append(f"DELIVERY=NONE: {self.public_detail or 'the public file was not replaced'}. Nothing new was delivered and DKEntries.csv still holds the previous "
                           "version. Close the file if it is open in another program, then run a late swap from the delivered version and apply the edits to that run "
                           "(this run's next edit is refused until a delivery names it).")
        for r in self.recheck:
            out.append(f"RECHECK_BEFORE={r['recheck_before_utc']} {r['goalie']} ({r['team']}, {r['status']}): {r['why']}")
        out += [f"EDIT_LOCK_SOON={x}" for x in self.lock_soon]
        if self.stop_reason:
            out.append(f"NOTE: {self.stop_reason}")
        return out


def edits_dir(run) -> Path:
    d = run.path / "edits"
    d.mkdir(exist_ok=True)
    return d


def _next_k(d: Path, stem: str) -> int:
    ns = [int(m.group(1)) for p in d.glob(f"{stem}_*.json") if (m := re.fullmatch(rf"{stem}_(\d+)(?:_result)?\.json", p.name))]
    return 1 + max(ns, default=0)


def _delivered_guard(run, outputs_root: Path, m: dict) -> str | None:
    """Why this run may not publish (the same rule as overrides-apply --ben): its current version is not the slate's delivered
    file, or the public file no longer holds those bytes. None when it may."""
    from nhl_dfs.build.state import PUBLIC_NAME, sha256

    public_dir = outputs_root / m["slate_id"]
    try:
        stamp = json.loads((public_dir / "published.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = {}
    if stamp.get("run_id") != run.run_id or stamp.get("version") != run.current_version():
        holder = f"run {stamp.get('run_id')} v{stamp.get('version')}" if stamp else "no run (nothing delivered)"
        return (f"run {run.run_id} v{run.current_version()} is not the slate's current delivery; {public_dir / 'published.json'} names "
                f"{holder}. Use --run <that run>.")
    public = public_dir / PUBLIC_NAME
    cur = run.version_file(run.current_version())
    if not public.exists() or sha256(public.read_bytes()) != sha256(cur.read_bytes()):
        return f"{public} no longer holds this run's v{run.current_version()}; a newer publish owns it"
    return None


# -- apply ------------------------------------------------------------------------------------------------------

def apply_edits(run, edits_input, *, now: datetime | None = None, runs_root=None, outputs_root=None,
                clock: Callable[[], datetime] | None = None) -> EditResult:
    """edits_input: a path to the file saved verbatim, or its text. Returns the result; every case, applied or not, is recorded
    under runs/<id>/edits/."""
    from nhl_dfs.build import controller, goalies as goalies_mod, late_swap, own_goalie as og_mod, packet as packet_mod
    from nhl_dfs.build import locks as locks_mod
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.objectives import load_risk_config
    from nhl_dfs.build.state import sha256
    from nhl_dfs.models.contests import load_contest_families
    from nhl_dfs.referee.check_file import check_file

    clock = clock or (lambda: controller._wall_now())
    now = now or clock()
    runs_root = Path(runs_root) if runs_root is not None else run.path.parent
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    d = edits_dir(run)
    k = _next_k(d, "edits")
    if controller._is_path_arg(edits_input):
        raw = Path(edits_input).read_bytes()
    else:
        raw = str(edits_input).encode("utf-8")
    text = raw.decode("utf-8-sig", errors="replace")
    (d / f"edits_{k}.json").write_bytes(raw)  # the file as saved, before anything reads it
    res = EditResult(run.run_id, k, edits_sha256=hashlib.sha256(raw).hexdigest())

    m = read_manifest(run)
    v = packet_mod.RunView(run, runs_root)
    res.parent_version = v.version
    parent_bytes = v.version_path.read_bytes()
    res.parent_sha256 = sha256(parent_bytes)
    why = _delivered_guard(run, outputs_root, m)
    if why:
        res.stop_reason = f"refused: {why}"
        return _record(run, res, m, now, d)
    try:
        edits, bad = parse_edits(text, v.mode)
    except MalformedEdits as exc:
        res.stop_reason = f"malformed edit file ({exc}): nothing applied, the delivered file stands"
        return _record(run, res, m, now, d)

    pool, mode = v.pool, v.mode
    ls = v.locks(now)
    pins = {eid: ls.pinned(eid) for eid in v.lineups}
    risk_cfg = load_risk_config()
    og_error, rule_fams, fam_of = None, frozenset(), {}
    try:  # a failure here is a report line and the rule is not applied, as in the controller
        rule_fams = og_mod.rule_families(risk_cfg, mode)
        fam_of = (og_mod.entry_families(v.entries, cache_family=getattr(v.cache, "contest_family", None), manifest=v.m,
                                        fam_cfg=load_contest_families()) if mode is Mode.CLASSIC else {})
    except Exception as exc:
        og_error, rule_fams, fam_of = og_mod.error_line(exc), frozenset(), {}
    objs, excluded = late_swap.accepted_overrides(run, pool, now)  # excluded: person_key -> why (OUT, or not the confirmed starter)
    news_at: dict[str, datetime] = {}
    for o in objs:
        if o.role_id in pool.by_role_id:
            pk = pool.by_role_id[o.role_id].person_key
            news_at[pk] = max(news_at.get(pk, o.effective_utc), o.effective_utc)
    goalie_table = (m.get("goalies") or {}).get("persons") or {}
    caps = (m.get("scenario") or {}).get("caps") or {}
    buffer_s = int(ls.buffer_s)

    by_entry: dict[str, list[Edit]] = defaultdict(list)
    for ed in edits:
        by_entry[ed.entry_id].append(ed)
    order: list[str] = []
    for n in range(len(edits) + len(bad)):  # the file's order of first appearance, bad edits included
        e = next((x.entry_id for x in edits if x.n == n), None) or (bad[n][0] if n in bad else None)
        if e and e not in order:
            order.append(e)
    means_box: dict[str, dict[str, float]] = {}

    def evaluate(skip: dict[str, str]):
        """Every entry judged on the parent lineups plus the entries applied before it (flag 127). `skip` holds the entries the
        referee dropped: they are judged for their cells and never applied, and the entries after them are judged without them."""
        cur = {e: list(lu) for e, lu in v.lineups.items()}
        outcomes: dict[str, EntryOutcome] = {}
        overridden: dict[str, list[str]] = {}
        rechecks: dict[str, list[dict]] = {}
        for eid in order:
            bad_here = [f"edit {n + 1}: {why}" for n, (e, why) in sorted(bad.items()) if e == eid]
            out = EntryOutcome(eid, "REJECTED")
            outcomes[eid] = out
            mine = by_entry.get(eid, [])
            out.edits = [{"n": ed.n, "edit_type": ed.edit_type, "reason": ed.reason, "evidence": ed.evidence,
                          "claimed_gain": ed.claimed_gain, "sources": ed.sources, "override": ed.override} for ed in mine]
            if bad_here:
                out.rule, out.reason = "malformed", "; ".join(bad_here)
                continue
            if eid not in cur:
                out.rule, out.reason = "unknown_entry", f"entry {eid} is not in this run's file"
                continue
            labels_used = [c.label for ed in mine for c in ed.cells]
            dup = next((x for x in labels_used if labels_used.count(x) > 1), None)
            if dup:
                out.rule, out.reason = "duplicate_cell", f"slot {dup} is edited twice for this entry"
                continue
            lu = list(cur[eid])
            stop = None
            for ed in mine:
                for c in ed.cells:
                    k_, old, new = c.index, lu[c.index], c.player_id
                    old_row = pool.by_role_id.get(old) if old else None
                    cell = {"slot": c.label, "index": k_, "out": old, "in": new,
                            "out_who": _who(old_row, _game_key(old_row)) if old_row else "(empty)", "in_who": f"[{new}]"}
                    out.cells.append(cell)
                    if c.replaces and c.replaces != old:
                        stop = ("stale_view", f"{c.label} holds {old}, the edit expected {c.replaces}: the model's view is out of date")
                    elif new not in pool.by_role_id:
                        stop = ("unknown_player", f"{new} is not in the salary pool")
                    else:
                        row = pool.by_role_id[new]
                        cell["in_who"] = _who(row, _game_key(row))
                        cs = ls.cells[(eid, k_)]
                        if cs.pinned:
                            stop = ("pinned", f"{c.label} is pinned ({cs.lock.value}): {cs.reason}")
                        elif not ls.addable(new):
                            if new in ls.started_role_ids:
                                ws = f"{_game_key(row) or row.team} has started"
                            elif new in ls.unswappable:
                                ws = "DraftKings reports him not swappable"
                            elif new not in ls.start_utc:
                                ws = "his game has no known start time"
                            else:
                                ws = f"{_game_key(row)} starts {ls.start_utc[new].isoformat()}, inside the {buffer_s}s edit stop"
                            stop = ("not_addable", f"{row.name} may not be added: {ws}")
                        elif v.st.get(new, (Participation.PLAYING,))[0] is Participation.OUT or row.person_key in excluded:
                            stop = ("out", f"{row.name} is OUT or IR" + (f" ({excluded[row.person_key]})" if row.person_key in excluded else ""))
                        elif row.is_goalie and (goalie_table.get(row.person_key) or {}).get("status") == goalies_mod.NOT_STARTING:
                            stop = ("not_starting", f"{row.name} ({row.team}) is NOT STARTING in the run's goalie table: his team's other goalie "
                                                    "is confirmed or DraftKings names him")
                        elif c.team and c.team.upper() != row.team.upper():
                            stop = ("identity", f"{new} is {row.name} of {row.team}, the edit expected team {c.team}: wrong id?")
                        elif c.name and c.name.strip().lower() != row.name.strip().lower():
                            stop = ("identity", f"{new} is {row.name}, the edit expected {c.name}: wrong id?")
                        elif ed.edit_type == "news":
                            newest = max((news_at[pk] for pk in {row.person_key, old_row.person_key if old_row else None}
                                          if pk in news_at), default=None)
                            pub = max((_utc(s["published_utc"]) for s in ed.sources if s["published_utc"]), default=None)
                            if newest is not None and pub is not None and pub < newest:
                                stop = ("source_stale", f"the cited page ({pub:%Y-%m-%dT%H:%MZ}) is older than the news it contradicts "
                                                        f"(accepted override effective {newest:%Y-%m-%dT%H:%MZ})")
                        if stop is None:
                            lu[k_] = new
                            if row.is_goalie:
                                g = goalie_table.get(row.person_key) or {}
                                status = g.get("status") or "UNKNOWN"
                                if status != "CONFIRMED":
                                    start = ls.start_utc.get(new)
                                    out.flags.append(f"UNCONFIRMED_GOALIE: {row.name} ({row.team}) is {status} in the run's goalie table; "
                                                     "queued for a recheck before his game")
                                    rechecks.setdefault(eid, []).append({"entry_id": eid, "goalie": row.name, "team": row.team, "role_id": new, "status": status,
                                                        "game": _game_key(row), "start_utc": start.isoformat() if start else None,
                                                        "recheck_before_utc": (start - timedelta(seconds=buffer_s)).isoformat().replace("+00:00", "Z")
                                                        if start else "unknown",
                                                        "why": "the edit seats a goalie whose start is not confirmed"})
                    if stop:
                        break
                if stop:
                    break
            if stop:
                out.rule, out.reason = stop
                continue
            if eid in skip:  # the referee dropped this entry on an earlier pass: it is judged again for its cells, never applied
                out.rule, out.reason = "referee", skip[eid]
                continue
            # the printed checks the model may override (flag 125)
            need: list[tuple[str, str]] = []
            if og_error is None and fam_of.get(eid) in rule_fams and set(og_mod.conflict_rows(lu, pool)) - set(og_mod.conflict_rows(cur[eid], pool)):
                need.append(("own_goalie", "the edit puts a skater against the entry's own goalie (own-goalie rule)"))
            if caps:
                if "m" not in means_box:
                    means_box["m"] = v.role_means()
                trial = {**cur, eid: lu}
                breaches = _budget_breaches(_concentration(cur, v, means_box["m"]), _concentration(trial, v, means_box["m"]), caps)
                if breaches:
                    need.append(("risk_budget", "; ".join(breaches)))
            have = {ed.override["rule"]: ed.override["reason"] for ed in mine if ed.override}
            missing = [(r, w) for r, w in need if r not in have]
            if missing:
                out.rule, out.reason = missing[0][0], f"{missing[0][1]}; needs an override naming the rule with a stated reason"
                continue
            for r, w in need:
                tag = "OVERRIDE" if r == "own_goalie" else "BREACHED"
                out.flags.append(f"{tag}({r}): {w}; reason given: {have[r]}")
                overridden.setdefault(r, []).append(f"entry {eid}: {w}")
            out.status, out.new_lineup = "APPLIED", lu
            out.total = sum(pool.by_role_id[r].salary for r in lu)
            cur[eid] = lu  # later entries are tested on the portfolio plus this one (flag 127)
        return outcomes, cur, overridden, rechecks

    # splice, then the referee decides legality; entries it names are dropped and the file is rebuilt (flag 127)
    from nhl_dfs.intake.entries import read_entries, template_permutation

    perm = template_permutation(v.entries.roster_labels, mode)
    col_of = {k_: col for col, k_ in enumerate(perm)}
    locked_text = {(e.entry_id, col_of[k_]): e.cells[col_of[k_]] for e in v.entries.entries for k_ in pins[e.entry_id]}
    staging = run.path / "staging" / f"edits_{k}.csv"
    staging.parent.mkdir(exist_ok=True)
    changes: dict[str, dict[int, str]] = {}
    data = parent_bytes
    report = None
    skip: dict[str, str] = {}
    for _ in range(len(order) + 1):  # each pass drops at least one entry the referee named, so this ends
        outcomes, cur, overridden, rechecks = evaluate(skip)
        changes = {o.entry_id: {k_: r for k_, r in enumerate(o.new_lineup) if r != v.lineups[o.entry_id][k_]}
                   for o in outcomes.values() if o.status == "APPLIED"}
        changes = {e: ch for e, ch in changes.items() if ch}
        if not changes:
            break
        data = late_swap.splice_cells(v.entries, pool, changes, staging)
        report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv", parent_path=v.version_path,
                            locked=locked_text, added_ids=frozenset(m.get("salary_added_ids") or ()))
        if report.ok:
            break
        named = {mm.group(1) for r in report.reasons if (mm := re.match(r"entry (\d+):", r))}
        hit = [e for e in changes if e in named]
        if not hit:
            res.stop_reason = "referee rejected the edited file (" + "; ".join(report.reasons[:3]) + "): nothing published, the checked file stands"
            for o in outcomes.values():
                if o.status == "APPLIED":
                    o.status, o.rule, o.reason = "REJECTED", "referee", res.stop_reason
            changes, report = {}, None
            break
        for e in hit:  # the whole evaluation runs again without them: an entry that passed the budget only because of them is judged anew
            skip[e] = "; ".join(r for r in report.reasons if r.startswith(f"entry {e}:"))[:300]
        report = None
    res.entries = [outcomes[e] for e in order]
    if not changes or report is None:
        changes = {}
        if not res.stop_reason:
            res.stop_reason = "no entry was applied: nothing published, the delivered file stands"
        return _record(run, res, m, now, d)

    pub, stop = _publish_checked(run, v, data, report, changes, staging, clock, outputs_root, m, phase=f"EDIT{k}", res=res)
    if pub is None:
        for o in res.entries:
            if o.status == "APPLIED":
                o.status, o.rule, o.reason = "REJECTED", "publish", stop
        res.stop_reason = stop
        return _record(run, res, m, now, d)
    _write_engine_copy(d, v, parent_bytes, res)
    res.recheck = [r for o in res.applied for r in rechecks.get(o.entry_id, [])]
    m = read_manifest(run)  # _publish_checked appended the version; the statuses below describe it
    try:
        m["statuses"]["OWN_GOALIE"] = og_error or og_mod.audit(cur, fam_of, pool, risk_cfg, pins=pins)
    except Exception as exc:
        m["statuses"]["OWN_GOALIE"] = og_mod.error_line(exc)
    if fam_of:
        m["families"] = {e: f for e, f in fam_of.items() if f}
    if overridden.get("risk_budget"):
        m["statuses"]["RISK_BUDGET"] = f"BREACHED(edit v{pub.version}: " + "; ".join(overridden["risk_budget"]) + ")"
    elif "RISK_BUDGET" in m["statuses"]:
        m["statuses"]["RISK_BUDGET"] = (f"NOT_EVALUATED (v{pub.version} changed {len(changes)} entr"
                                        f"{'y' if len(changes) == 1 else 'ies'} after the scenario pass)")
    try:
        soon = locks_mod.locking_soon(v.entries, read_entries(staging), pool, now)
        res.lock_soon = [f"{g} locks {s.isoformat().replace('+00:00', 'Z')} (in {max(0, int((s - now).total_seconds() // 60))} min); changed cells touch it"
                         for g, s in soon]
    except (OSError, ValueError):
        res.lock_soon = []
    return _record(run, res, m, now, d)


def _write_engine_copy(d: Path, v, parent_bytes: bytes, res: EditResult) -> None:
    """The engine's own version, kept as the counterfactual: written once, never overwritten (flag 128)."""
    from nhl_dfs.build.state import atomic_write

    if any(d.glob("engine_v*.csv")):
        return
    p = d / f"engine_v{v.version}.csv"
    atomic_write(p, parent_bytes)
    res.engine_copy = str(p)


def _publish_checked(run, v, data: bytes, report, changes: dict[str, dict[int, str]], staging: Path, clock, outputs_root: Path,
                     m: dict, *, phase: str, res: EditResult):
    """Publish with the compare-and-swap done before any byte is written (flag 126). Returns (PublishResult | None, why not)."""
    from nhl_dfs.build import locks as locks_mod
    from nhl_dfs.build.manifest import write_manifest
    from nhl_dfs.build.state import (PUBLIC_NAME, LockCrossed, LockTimeout, PublishRefused, publish, sha256)
    from nhl_dfs.intake.entries import read_entries

    parent_sha = sha256(v.version_path.read_bytes())
    public = outputs_root / m["slate_id"] / PUBLIC_NAME

    def precheck() -> str | None:
        have = sha256(public.read_bytes()) if public.exists() else ""
        if have != parent_sha:
            return "the public file is no longer this run's version (a newer publish owns it): nothing written"
        if run.current_version() != v.version:
            return f"the run's current version moved from v{v.version} to v{run.current_version()}: nothing written"
        t = clock()
        ls2 = v.locks(t)
        crossed = [(e, k_) for e, ch in changes.items() for k_, r in ch.items() if ls2.cells[(e, k_)].pinned or not ls2.addable(r)]
        if crossed:
            return "a lock boundary was crossed: nothing published, the checked file stands"
        cr = locks_mod.publish_crossings(v.entries, read_entries(staging), v.pool, t, int(ls2.buffer_s))
        if cr:
            return "a started or edit-stop game is touched on one side of a changed cell: nothing published (" + "; ".join(
                (*cr.started, *cr.edit_stop))[:300] + ")"
        return None

    try:
        pub = publish(run, data, report, m["slate_id"], outputs_root=outputs_root, expect_public_sha=parent_sha, precheck=precheck)
    except LockCrossed as exc:
        return None, str(exc)
    except (PublishRefused, LockTimeout) as exc:
        return None, f"publish failed ({exc}): the checked file stands"
    res.published_version, res.delivery_version = pub.version, pub.delivery_version
    res.public_detail = pub.public_detail or ""
    res.delivery_path = str(pub.delivery_path) if pub.delivery_path else None
    res.sha256 = report.out_sha256
    m2 = _read(run)
    m2["versions"].append({"version": pub.version, "phase": phase, "sha256": report.out_sha256,
                           "created_utc": datetime.now(timezone.utc).isoformat(), "path": str(pub.version_path),
                           "public_replaced": pub.public_replaced, "public_detail": pub.public_detail,
                           "delivery_version": pub.delivery_version,
                           "delivery_path": str(pub.delivery_path) if pub.delivery_path else None})
    write_manifest(run, m2)
    return pub, ""


def _read(run) -> dict:
    from nhl_dfs.build.manifest import read_manifest

    return read_manifest(run)


def _record(run, res: EditResult, m: dict, now: datetime, d: Path) -> EditResult:
    """Write the result record, one log row per edit, the recheck queue, and (when something was published) the manifest and notes."""
    from nhl_dfs.build.manifest import write_manifest
    from nhl_dfs.build.notes import write_run_notes

    stem = "restore" if res.kind == "restore" else "edits"
    rec = {**asdict(res), "created_utc": now.isoformat()}
    path = d / f"{stem}_{res.k}_result.json"
    path.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    res.record_path = str(path)
    rows = []
    for o in res.entries:
        for ed in (o.edits or [{}]):
            rows.append({"ts": now.isoformat(), "run_id": res.run_id, "file": res.k, "kind": res.kind, "edit": ed.get("n"),
                         "entry_id": o.entry_id, "status": o.status, "rule": o.rule, "reason_stopped": o.reason,
                         "edit_type": ed.get("edit_type"), "reason": ed.get("reason"), "evidence": ed.get("evidence"),
                         "claimed_gain": ed.get("claimed_gain"), "sources": ed.get("sources"), "override": ed.get("override"),
                         "flags": o.flags, "cells": [{"slot": c["slot"], "from": c["out"], "to": c["in"], "from_who": c["out_who"],
                                                      "to_who": c["in_who"]} for c in o.cells],
                         "parent_version": res.parent_version, "parent_sha256": res.parent_sha256,
                         "published_version": res.published_version if o.status == "APPLIED" else None,
                         "delivery_version": res.delivery_version if o.status == "APPLIED" else None,
                         "sha256": res.sha256 if o.status == "APPLIED" else None, "edits_sha256": res.edits_sha256})
    if rows:
        with open(d / "log.jsonl", "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
    if res.recheck and res.published_version:
        q = d / "recheck.json"
        prior = json.loads(q.read_text(encoding="utf-8")) if q.exists() else []
        q.write_text(json.dumps(prior + res.recheck, indent=1), encoding="utf-8")
    if res.published_version:
        m.setdefault("edits", []).append({
            "file": res.k, "kind": res.kind, "version": res.published_version, "delivery_version": res.delivery_version,
            "applied": [o.entry_id for o in res.applied], "rejected": [{"entry_id": o.entry_id, "rule": o.rule} for o in res.rejected],
            "edits": [{"entry_id": o.entry_id, "flags": o.flags, **ed, "cells": [f"{c['slot']}: {c['out_who']} -> {c['in_who']}"
                                                                              for c in o.cells]} for o in res.applied for ed in (o.edits or [{}])]})
        m["messages"] = list(m.get("messages", [])) + [
            f"{'Restore' if res.kind == 'restore' else 'Model edit file'} {res.k} published v{res.published_version} "
            f"({len(res.applied)} entr{'y' if len(res.applied) == 1 else 'ies'}; log in runs/{res.run_id}/edits/)"]
        write_manifest(run, m)
        try:  # the file is already published and delivered: a notes error is a message, never a crash before the delivery lines print
            write_run_notes(run, m)
        except Exception as exc:
            res.stop_reason = (res.stop_reason + "; " if res.stop_reason else "") + f"RUN_NOTES.md could not be written ({type(exc).__name__}: {str(exc)[:100]})"
    return res


# -- restore ----------------------------------------------------------------------------------------------------

def restore_previous(run, *, now: datetime | None = None, runs_root=None, outputs_root=None,
                     clock: Callable[[], datetime] | None = None) -> EditResult:
    """Re-publish the previous version's bytes (the parent the last edit or restore recorded) as the next run version (flag 128).
    Refuses, naming the cells, when a changed cell is pinned now or a started or edit-stop game is touched on either side."""
    from nhl_dfs.build import controller, late_swap, packet as packet_mod
    from nhl_dfs.build import locks as locks_mod
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.build.state import sha256
    from nhl_dfs.intake.entries import read_entries, template_permutation
    from nhl_dfs.referee.check_file import check_file

    clock = clock or (lambda: controller._wall_now())
    now = now or clock()
    runs_root = Path(runs_root) if runs_root is not None else run.path.parent
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    d = edits_dir(run)
    res = EditResult(run.run_id, _next_k(d, "restore"), kind="restore")
    m = read_manifest(run)
    v = packet_mod.RunView(run, runs_root)
    res.parent_version = v.version
    res.parent_sha256 = sha256(v.version_path.read_bytes())
    why = _delivered_guard(run, outputs_root, m)
    if why:
        res.stop_reason = f"refused: {why}"
        return _record(run, res, m, now, d)
    last = None
    for p in sorted(d.glob("*_result.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        if r.get("published_version") == v.version:
            last = r
    if last is None or last.get("parent_version") is None:
        res.stop_reason = f"refused: v{v.version} was not published by an edit or a restore, so there is no previous edit version to restore"
        return _record(run, res, m, now, d)
    target = run.version_file(int(last["parent_version"]))
    if not target.exists() or sha256(target.read_bytes()) != last["parent_sha256"]:
        res.stop_reason = f"refused: v{last['parent_version']} is missing or its bytes differ from the recorded hash"
        return _record(run, res, m, now, d)
    tgt_bytes = target.read_bytes()
    staging = run.path / "staging" / f"restore_{res.k}.csv"
    staging.parent.mkdir(exist_ok=True)
    staging.write_bytes(tgt_bytes)
    tgt = read_entries(staging)
    ls = v.locks(now)
    pins = {eid: ls.pinned(eid) for eid in v.lineups}
    cur_lineups = v.lineups
    new_lineups = {e.entry_id: late_swap._canonical(tgt, e) for e in tgt.entries}
    changes: dict[str, dict[int, str]] = {}
    pinned_hit: list[str] = []
    labels = slot_labels(v.mode)
    for eid, lu in new_lineups.items():
        for k_, r in enumerate(lu):
            if r != cur_lineups.get(eid, [None] * len(lu))[k_] and r is not None:
                changes.setdefault(eid, {})[k_] = r
                if k_ in pins.get(eid, {}):
                    pinned_hit.append(f"entry {eid} {labels[k_]}")
    cr = locks_mod.publish_crossings(v.entries, tgt, v.pool, now, int(ls.buffer_s))
    if pinned_hit or cr:
        res.stop_reason = ("refused: restoring would change a cell that is pinned now (" + "; ".join(pinned_hit[:6]) + ")" if pinned_hit
                           else "refused: restoring would touch a started or edit-stop game (" + "; ".join((*cr.started, *cr.edit_stop))[:300] + ")")
        return _record(run, res, m, now, d)
    perm = template_permutation(v.entries.roster_labels, v.mode)
    col_of = {k_: col for col, k_ in enumerate(perm)}
    locked_text = {(e.entry_id, col_of[k_]): e.cells[col_of[k_]] for e in v.entries.entries for k_ in pins[e.entry_id]}
    report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv", parent_path=v.version_path,
                        locked=locked_text, added_ids=frozenset(m.get("salary_added_ids") or ()))
    if not report.ok:
        res.stop_reason = "refused: the referee rejected the restored file (" + "; ".join(report.reasons[:3]) + ")"
        return _record(run, res, m, now, d)
    res.entries = [EntryOutcome(e, "APPLIED", cells=[{"slot": labels[k_], "out": cur_lineups[e][k_], "in": r,
                                                      "out_who": _who(v.pool.by_role_id[cur_lineups[e][k_]]) if cur_lineups[e][k_] else "(empty)",
                                                      "in_who": _who(v.pool.by_role_id[r])} for k_, r in sorted(ch.items())])
                   for e, ch in changes.items()]
    if not changes:
        res.stop_reason = "the previous version has the same cells as the current one: nothing to restore"
        return _record(run, res, m, now, d)
    pub, stop = _publish_checked(run, v, tgt_bytes, report, changes, staging, clock, outputs_root, m, phase=f"RESTORE{res.k}", res=res)
    if pub is None:
        for o in res.entries:
            o.status, o.rule, o.reason = "REJECTED", "publish", stop
        res.stop_reason = stop
        return _record(run, res, m, now, d)
    m = read_manifest(run)
    return _record(run, res, m, now, d)
