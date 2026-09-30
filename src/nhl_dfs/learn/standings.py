"""DraftKings contest standings exports (card C11; plan section 6 "After each slate").

A `contest-standings-<contest id>.csv` (or a .zip holding one) has two unrelated lists that share row
positions: the entry block (Rank, EntryId, EntryName, TimeRemaining, Points, Lineup) and, after an empty
column, the ownership block (Player, Roster Position, %Drafted, FPTS). The contest id is in the file name only.

Ownership semantics (checked on the 2026-09-29 exports):
- Classic lists each player once per roster slot kind: his position row (C, W, D, G) and, when he was used
  there, a UTIL row; a player's ownership is the sum of his rows (the rows sum to about 900%). A UTIL row
  carries no position token.
- Showdown lists CPT and FLEX rows separately (each block sums to about 100% and 500%); a CPT row's FPTS is
  1.5 times the FLEX row's.
- Only drafted players are listed. Zero ownership is established by reconstructing the counts from the
  complete lineup rows (`join`), never assumed. Entries with a blank lineup stay in the %Drafted denominator
  and are excluded from lineup reconstruction and duplicate counts.

Lineups are parsed against the fixed slot sequence (Classic `C C D D G UTIL W W W`, Showdown `CPT` + 5 `FLEX`),
never by splitting on a slot word. Joins use the normalized name plus the roster-position token; when the pool
has more than one candidate for a name and token, the row is CONFLICTED and never guessed. Own entries are
joined by Entry ID exactly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from nhl_dfs.contracts.geometry import Mode, lineup_key
from nhl_dfs.contracts.ids import normalize_name

CLASSIC_SEQ = ("C", "C", "D", "D", "G", "UTIL", "W", "W", "W")
SHOWDOWN_SEQ = ("CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX")
_NAME_RE = re.compile(r"contest-standings-(\d+)")


@dataclass(frozen=True)
class EntryRow:
    rank: int
    entry_id: str
    entry_name: str
    points: Decimal
    lineup: tuple[tuple[str, str], ...]  # (slot, player name) in the export's order; empty when blank
    lineup_raw: str

    @property
    def blank(self) -> bool:
        return not self.lineup


@dataclass(frozen=True)
class OwnRow:
    name: str
    roster_position: str
    pct_drafted: float
    fpts: Decimal


@dataclass
class Standings:
    contest_id: int
    mode: Mode
    entries: list[EntryRow]
    ownership: list[OwnRow]
    raw_sha: str
    source: str
    notes: list[str] = field(default_factory=list)

    def by_entry(self) -> dict[str, EntryRow]:
        return {e.entry_id: e for e in self.entries}

    def tied(self, rank: int) -> int:
        return sum(1 for e in self.entries if e.rank == rank)


def _parse_lineup(text: str, mode: Mode) -> tuple[tuple[str, str], ...]:
    text = (text or "").strip()
    if not text:
        return ()
    seq = CLASSIC_SEQ if mode is Mode.CLASSIC else SHOWDOWN_SEQ
    pat = "^" + " ".join(f"{s} (.+?)" for s in seq[:-1]) + f" {seq[-1]} (.+)$"
    m = re.match(pat, text)
    if m is None:
        raise ValueError(f"lineup does not follow the {mode.value} slot sequence: {text[:80]!r}")
    return tuple(zip(seq, (g.strip() for g in m.groups())))


def _mode_of(rows: list[list[str]]) -> Mode:
    for r in rows:
        if len(r) > 8 and r[8].strip() in ("CPT", "FLEX"):
            return Mode.SHOWDOWN
        if len(r) > 5 and r[5].strip().startswith("CPT "):
            return Mode.SHOWDOWN
    return Mode.CLASSIC


def parse(data: bytes, source: str, contest_id: int | None = None) -> Standings:
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip() for c in rows[0][:6]] != ["Rank", "EntryId", "EntryName", "TimeRemaining", "Points", "Lineup"]:
        raise ValueError(f"{source}: not a DraftKings contest standings export (header {rows[0][:6] if rows else None})")
    hdr = [c.strip() for c in rows[0]]
    try:
        o = hdr.index("Player")
    except ValueError as exc:
        raise ValueError(f"{source}: no ownership block (Player column)") from exc
    if hdr[o:o + 4] != ["Player", "Roster Position", "%Drafted", "FPTS"]:
        raise ValueError(f"{source}: unexpected ownership columns {hdr[o:o + 4]}")
    if contest_id is None:
        m = _NAME_RE.search(Path(source).name)
        if m is None:
            raise ValueError(f"{source}: no contest id in the file name (contest-standings-<id>.csv)")
        contest_id = int(m.group(1))
    body = rows[1:]
    mode = _mode_of(body)
    entries, own, notes = [], [], []
    for r in body:
        r = r + [""] * (o + 4 - len(r))
        if r[0].strip():
            entries.append(EntryRow(int(r[0]), r[1].strip(), r[2].strip(), Decimal(r[4].strip() or "0"),
                                    _parse_lineup(r[5], mode), r[5].strip()))
        if r[o].strip():
            own.append(OwnRow(r[o].strip(), r[o + 1].strip(), float(r[o + 2].strip().rstrip("%") or 0),
                              Decimal(r[o + 3].strip() or "0")))
    blanks = sum(1 for e in entries if e.blank)
    if blanks:
        notes.append(f"{blanks} entr{'y' if blanks == 1 else 'ies'} with a blank lineup (kept in the %Drafted "
                     "denominator, excluded from lineup reconstruction and duplicate counts)")
    if mode is Mode.SHOWDOWN:
        by: dict[str, dict[str, Decimal]] = {}
        for x in own:
            by.setdefault(x.name, {})[x.roster_position] = x.fpts
        bad = [n for n, v in by.items() if "CPT" in v and "FLEX" in v and v["CPT"] != v["FLEX"] * Decimal("1.5")]
        if bad:
            notes.append("CPT FPTS is not 1.5 x FLEX for: " + ", ".join(bad[:5]))
    return Standings(contest_id, mode, entries, own, hashlib.sha256(data).hexdigest(), source, notes)


def read(path) -> Standings:
    """One standings export: a CSV or a .zip holding exactly one CSV."""
    p = Path(path)
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{p}: expected one CSV in the zip, found {len(names)}")
            m = _NAME_RE.search(names[0]) or _NAME_RE.search(p.name)
            return parse(z.read(names[0]), f"{p}!{names[0]}", int(m.group(1)) if m else None)
    return parse(p.read_bytes(), str(p))


def read_all(path) -> tuple[list[Standings], list[str]]:
    """A file, a zip, or a folder (searched recursively). Duplicates (same contest id and content) are kept
    once; two different exports of one contest keep the later-listed one and say so (a standings revision)."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.suffix.lower() in (".csv", ".zip")
                                           and x.name.lower().startswith("contest-standings"))
    out: dict[int, Standings] = {}
    notes: list[str] = []
    for f in files:
        try:
            s = read(f)
        except (ValueError, zipfile.BadZipFile) as exc:
            notes.append(f"{f.name}: skipped ({exc})")
            continue
        prev = out.get(s.contest_id)
        if prev is not None:
            if prev.raw_sha != s.raw_sha:
                notes.append(f"contest {s.contest_id}: two different exports ({Path(prev.source).name}, "
                             f"{Path(s.source).name}); the later one is used")
            else:
                continue
        out[s.contest_id] = s
    return [out[k] for k in sorted(out)], notes


# -- join to the salary pool -------------------------------------------------------------------------------

@dataclass
class Joined:
    contest_id: int
    mode: Mode
    entries_n: int  # every entry, blank lineups included (the %Drafted denominator)
    own: dict[str, float]  # role_id -> actual % (Classic: the person's single row; Showdown: CPT and FLEX rows)
    points_tenths: dict[str, int]  # person_key -> actual DK points in tenths (FLEX scale in Showdown)
    lineups: dict[str, list[str | None]]  # entry_id -> role ids in slot order (None: not joined)
    dup_counts: dict[str, int]  # lineup key -> entries (complete, joined lineups only)
    complete: bool  # listed %Drafted reproduced from the complete lineups (zero ownership is then observed)
    conflicts: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _candidates(pool, mode: Mode) -> dict[tuple[str, str], list]:
    """(normalized name, token) -> pool rows whose DK roster positions accept the token: Classic C/W/D/G and
    UTIL (the pool's LW and RW rows list W), Showdown CPT/FLEX."""
    out: dict[tuple[str, str], list] = {}
    toks = CLASSIC_SEQ if mode is Mode.CLASSIC else SHOWDOWN_SEQ
    for r in pool.rows:
        n = normalize_name(r.name)
        for tok in set(toks):
            if tok in r.roster_positions:
                out.setdefault((n, tok), []).append(r)
    return out


def join(s: Standings, pool) -> Joined:
    """Standings -> the pool's role ids. Collisions are CONFLICTED (never guessed); unmatched names are reported."""
    if s.mode is not pool.mode:
        raise ValueError(f"contest {s.contest_id} is {s.mode.value} but the pool is {pool.mode.value}")
    cand = _candidates(pool, s.mode)
    conflicts, unmatched = set(), set()

    def resolve(name: str, token: str):
        rows = cand.get((normalize_name(name), token), [])
        people = {r.person_key for r in rows}
        if not rows:
            unmatched.add(f"{name} ({token})")
            return None
        if len(people) > 1:
            conflicts.add(f"{name} ({token}): {len(people)} pool candidates")
            return None
        return rows[0]

    own: dict[str, float] = {}
    pts: dict[str, int] = {}
    for x in s.ownership:
        r = resolve(x.name, x.roster_position)
        if r is None:
            continue
        own[r.role_id] = own.get(r.role_id, 0.0) + x.pct_drafted
        tenths = x.fpts * 10 if (s.mode is Mode.CLASSIC or x.roster_position == "FLEX") else x.fpts * 10 / Decimal("1.5")
        if tenths != tenths.to_integral_value():
            raise ValueError(f"contest {s.contest_id}: {x.name} FPTS {x.fpts} is not a whole number of tenths")
        pts.setdefault(r.person_key, int(tenths))
    lineups: dict[str, list[str | None]] = {}
    counts: Counter[str] = Counter()
    for e in s.entries:
        if e.blank:
            continue
        rids = []
        for slot, name in e.lineup:
            r = resolve(name, slot)
            rids.append(r.role_id if r is not None else None)
        lineups[e.entry_id] = rids
        counts.update(r for r in rids if r)
    n = len(s.entries)
    recon = {rid: 100.0 * c / n for rid, c in counts.items()} if n else {}
    # The actual ownership is the reconstruction from the complete lineup rows (every non-blank lineup parses
    # against the slot sequence, or the file is refused): DK's listed block omitted position rows on 2026-09-29
    # (Drew O'Connor's W row). The listed %Drafted is the cross-check. Zero ownership of a pool player is observed
    # when no name is CONFLICTED (a name outside the pool is reported and hides no pool player).
    listed = own
    mismatch = sorted(f"{pool.by_role_id[r].name} listed {listed.get(r, 0.0):.2f}% vs lineups {recon.get(r, 0.0):.2f}%"
                      for r in set(recon) | set(listed) if abs(recon.get(r, 0.0) - listed.get(r, 0.0)) > 0.011)
    complete = bool(n) and not conflicts
    own = recon
    notes = []
    if mismatch:
        notes.append(f"DraftKings' listed ownership differs from the lineup rows for {len(mismatch)} role(s) (the lineup "
                     "reconstruction is used): " + "; ".join(mismatch[:4]))
    if not complete:
        notes.append("CONFLICTED names: those persons' ownership is not observed, and zero ownership is not graded")
    if unmatched:
        notes.append(f"{len(unmatched)} standings name(s) not in the run's salary file (added by DraftKings after the "
                     "download, or an identity gap): " + ", ".join(sorted(unmatched)[:6]))
    dup: Counter[str] = Counter()
    for rids in lineups.values():
        if all(rids):
            dup[lineup_key([pool.by_role_id[r] for r in rids], s.mode)] += 1
    return Joined(s.contest_id, s.mode, n, own, pts, lineups, dict(dup), complete, sorted(conflicts), sorted(unmatched),
                  notes + list(s.notes))
