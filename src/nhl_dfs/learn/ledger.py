"""Financial settlement (card C11; plan section 12, "Financial settlement is a measured output").

Own entries are joined to their final rank by Entry ID; the payout comes from, in this order:
1. REPORTED: the amount Ben reads in DraftKings "My Contests" and types into `winnings.csv` (what DK paid);
2. EXACT: the contest's payout table with the DK tie rule (the tied places' prizes pooled, split evenly,
   each share rounded down to the cent: build.objectives.split_tie). A table is used only when it is final:
   the contest filled (standings entries = maximum entries) or its prize pool is guaranteed;
3. UNKNOWN: nothing is invented. The entry's fee is counted, its payout is null, and the slate is incomplete.
When both a reported amount and a table exist, the reported amount is booked and the table is a cross-check
(a mismatch is flagged). Prize-table sources, each labeled: a file Ben saved from the browser
(`dk_contest_<id>.json` beside the standings), an explicit `--prize-table` path, or the raw DK cache
(`data/raw/dk_contest`, a pre-lock fetch). Tickets (non-cash tiers) are not converted to cash.

The ledger is `<ledger root>/ledger.parquet` (default data/ledger/, gitignored; NHL_DFS_LEDGER_ROOT redirects
it), one row per own entry, keyed by (run_id, contest_id, entry_id): a re-settle replaces that run's rows. Net
and rolling drawdown are over known payouts; a slate with any UNKNOWN contest marks the drawdown incomplete.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nhl_dfs.build.objectives import exact_curve, split_tie, to_cents
from nhl_dfs.data.sources.dk_public import ContestDetail, parse_contest_detail

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTED, EXACT, UNKNOWN = "REPORTED", "EXACT", "UNKNOWN"
WINNINGS_FIELDS = ["contest_id", "contest_name", "entry_id", "rank", "fee", "winnings_usd", "source", "noted_utc"]
ET = ZoneInfo("America/New_York")


def default_root() -> Path:
    return Path(os.environ.get("NHL_DFS_LEDGER_ROOT") or REPO_ROOT / "data" / "ledger")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def cents(fee: str) -> int:
    return to_cents(str(fee).replace("$", "").replace(",", "").strip() or "0")


# -- prize tables -----------------------------------------------------------------------------------------------

@dataclass
class PrizeTable:
    contest_id: int
    detail: ContestDetail
    source: str
    final: bool
    final_note: str


def _load_json(path: Path) -> ContestDetail:
    return parse_contest_detail(json.loads(Path(path).read_bytes().decode("utf-8-sig")))


def _cached(contest_id: int, cache_root: Path) -> tuple[ContestDetail, str] | None:
    idx = cache_root / "dk_contest" / "index.json"
    if not idx.exists():
        return None
    for url, e in json.loads(idx.read_text(encoding="utf-8")).items():
        if f"/{contest_id}?" in url or url.rstrip("/").endswith(f"/{contest_id}"):
            p = cache_root / e["raw_path"]
            if p.exists():
                return _load_json(p), f"raw DK cache, fetched {e['fetched_at']}"
    return None


def prize_tables(contest_ids, *, entries_n: dict[int, int], search_dirs=(), paths=(), cache_root: Path | None = None
                 ) -> tuple[dict[int, PrizeTable], list[str]]:
    """contest id -> the final prize table from the first source that has one (see the module docstring)."""
    cache_root = cache_root if cache_root is not None else REPO_ROOT / "data" / "raw"
    found: dict[int, tuple[ContestDetail, str]] = {}
    notes: list[str] = []
    for p in paths:
        try:
            d = _load_json(Path(p))
            found.setdefault(d.contest_id, (d, f"prize table file {Path(p).name}"))
        except Exception as exc:
            notes.append(f"prize table {p}: unreadable ({type(exc).__name__})")
    for d_ in search_dirs:
        for p in sorted(Path(d_).rglob("dk_contest_*.json")) if Path(d_).is_dir() else []:
            try:
                d = _load_json(p)
                found.setdefault(d.contest_id, (d, f"saved from the browser: {p.name}"))
            except Exception as exc:
                notes.append(f"{p.name}: unreadable ({type(exc).__name__})")
    out: dict[int, PrizeTable] = {}
    for cid in contest_ids:
        got = found.get(int(cid)) or _cached(int(cid), cache_root)
        if got is None:
            continue
        d, src = got
        n = entries_n.get(int(cid))
        guaranteed = bool((d.raw.get("contestDetail") or {}).get("isGuaranteed"))
        filled = n is not None and n >= d.maximum_entries
        final = guaranteed or filled
        note = ("guaranteed prize pool" if guaranteed else "") + ("; " if guaranteed and filled else "") + \
               (f"filled ({n} of {d.maximum_entries})" if filled else "")
        if not filled and n is not None:
            note += f"; {n} of {d.maximum_entries} entries"
        if not final:
            note = f"not guaranteed and not filled ({n} of {d.maximum_entries}): the table may not be final"
        out[int(cid)] = PrizeTable(int(cid), d, src, final, note.strip("; "))
    return out, notes


def payout_from_table(t: PrizeTable, rank: int, tied: int) -> tuple[int | None, str]:
    """Cents for one of `tied` entries at `rank` (DK tie rule), or None for a ticket tier."""
    prizes, seats, _face, _ = exact_curve(t.detail)
    a, b = rank - 1, rank - 1 + tied
    if seats[a:b].any():
        return None, "a ticket (non-cash) tier: not converted to cash"
    got = [Decimal(int(x)) / 100 for x in prizes[a:b]] + [Decimal(0)] * max(0, b - max(a, len(prizes)))
    share = split_tie(got, tied)
    return int(share * 100), (f"rank {rank}, tied with {tied - 1} other(s): pooled and split" if tied > 1 else f"rank {rank}")


# -- reported winnings -------------------------------------------------------------------------------------------

def read_winnings(path) -> dict[tuple[str, str], tuple[int | None, str]]:
    """(contest_id, entry_id) -> (cents or None when blank, source label). entry_id may be blank for a contest with
    one own entry."""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    for r in csv.DictReader(io.StringIO(p.read_bytes().decode("utf-8-sig"))):
        cid = (r.get("contest_id") or "").strip()
        if not cid:
            continue
        amt = (r.get("winnings_usd") or "").strip().replace("$", "").replace(",", "")
        src = (r.get("source") or "DraftKings My Contests").strip()
        when = (r.get("noted_utc") or "").strip()
        out[(cid, (r.get("entry_id") or "").strip())] = (to_cents(amt) if amt else None,
                                                         f"reported by Ben ({src}{', ' + when if when else ''})")
    return out


def write_winnings_template(path: Path, rows: list[dict]) -> int:
    """A pre-filled CSV for Ben to complete from DraftKings My Contests. An existing file is never rewritten: rows
    for entries it does not list yet are appended (Ben's typed lines stay as they are). Returns rows added."""
    have = set()
    if path.exists():
        for r in csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"))):
            have.add(((r.get("contest_id") or "").strip(), (r.get("entry_id") or "").strip()))
    todo = [r for r in rows if (str(r.get("contest_id", "")), str(r.get("entry_id", ""))) not in have]
    if not todo:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=WINNINGS_FIELDS, lineterminator="\r\n")
    if not path.exists():
        w.writeheader()
    for r in todo:
        w.writerow({k: r.get(k, "") for k in WINNINGS_FIELDS})
    raw = path.read_bytes() if path.exists() else b""
    if raw and not raw.endswith(b"\n"):
        raw += b"\r\n"
    path.write_bytes(raw + buf.getvalue().encode("utf-8"))
    return len(todo)


# -- settlement ----------------------------------------------------------------------------------------------------

@dataclass
class EntryResult:
    contest_id: str
    contest_name: str
    entry_id: str
    fee_cents: int
    rank: int | None
    tied: int | None
    points: str | None
    payout_cents: int | None
    payout_source: str
    source_detail: str
    table_cents: int | None = None  # the table's figure when a reported amount is booked (cross-check)
    entered_matches: bool | None = None  # the standings lineup equals the run's final version
    entered_detail: str = ""


@dataclass
class SlateLedger:
    run_id: str
    slate_id: str
    mode: str
    slate_date: str
    settled_utc: str
    entries: list[EntryResult]
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(e.payout_cents is not None for e in self.entries)

    def totals(self) -> dict:
        fees = sum(e.fee_cents for e in self.entries)
        known = [e for e in self.entries if e.payout_cents is not None]
        gross = sum(e.payout_cents for e in known)
        fees_known = sum(e.fee_cents for e in known)
        return {"fees_cents": fees, "fees_known_cents": fees_known, "fees_unknown_cents": fees - fees_known,
                "gross_known_cents": gross, "net_known_cents": gross - fees_known, "complete": self.complete,
                "net_cents": (gross - fees) if self.complete else None}

    def by_contest(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in self.entries:
            c = out.setdefault(e.contest_id, {"contest_name": e.contest_name, "entries": 0, "fees_cents": 0,
                                              "gross_cents": 0, "known": True, "sources": set()})
            c["entries"] += 1
            c["fees_cents"] += e.fee_cents
            c["sources"].add(e.payout_source)
            if e.payout_cents is None:
                c["known"] = False
            else:
                c["gross_cents"] += e.payout_cents
        for c in out.values():
            c["net_cents"] = c["gross_cents"] - c["fees_cents"] if c["known"] else None
            c["gross_cents"] = c["gross_cents"] if c["known"] else None
            c["sources"] = sorted(c["sources"])
        return out

    def record(self) -> dict:
        return {"run_id": self.run_id, "slate_id": self.slate_id, "mode": self.mode, "slate_date": self.slate_date,
                "settled_utc": self.settled_utc, "totals": self.totals(), "by_contest": self.by_contest(),
                "entries": [asdict(e) for e in self.entries], "notes": self.notes}


def slate_date(pool) -> str:
    """The NHL slate date: the first game's date in Eastern time."""
    return str(min(g.start_utc for g in pool.games.values()).astimezone(ET).date())


def _entered(own_lineup, s_entry, pool, mode) -> tuple[bool | None, str]:
    """Compare the standings lineup with the run's final version (names by slot kind; order-free)."""
    from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode

    if own_lineup is None or s_entry is None or s_entry.blank:
        return None, "not compared"
    slots = CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    from nhl_dfs.contracts.ids import normalize_name

    ours = sorted((str(slots[k]), normalize_name(pool.by_role_id[r].name)) for k, r in enumerate(own_lineup) if r)
    theirs = sorted((slot, normalize_name(n)) for slot, n in s_entry.lineup)
    if ours == theirs:
        return True, "entered as delivered"
    missing = sorted({n for _, n in ours} - {n for _, n in theirs})
    extra = sorted({n for _, n in theirs} - {n for _, n in ours})
    return False, f"entered lineup differs from the run's final version (not entered: {', '.join(missing) or '-'}; " \
                  f"entered instead: {', '.join(extra) or '-'})"


def settle(run, standings: list, contests_final: dict[int, PrizeTable], *, reported: dict | None = None, pool=None,
           final_lineups: dict[str, list] | None = None, now: datetime | None = None) -> SlateLedger:
    """Own entries (the run's DKEntries input) -> final rank -> payout. `final_lineups`: entry_id -> role ids of the
    run's final version (for the entered-lineup check)."""
    from nhl_dfs.build.manifest import read_manifest
    from nhl_dfs.intake.entries import read_entries
    from nhl_dfs.intake.salary import read_salary

    now = now or datetime.now(timezone.utc)
    m = read_manifest(run)
    pool = pool or read_salary(run.inputs / "DKSalaries.csv")
    ef = read_entries(run.inputs / "DKEntries.csv")
    by_cid = {str(s.contest_id): s for s in standings}
    reported = reported or {}
    rows, notes = [], []
    per_contest_own = {}
    for e in ef.entries:
        per_contest_own.setdefault(str(e.contest_id), []).append(e.entry_id)
    for e in ef.entries:
        cid = str(e.contest_id)
        s = by_cid.get(cid)
        se = s.by_entry().get(e.entry_id) if s is not None else None
        rank = se.rank if se else None
        tied = s.tied(se.rank) if se else None
        table = contests_final.get(int(cid))
        t_cents, t_detail = (None, "")
        if table is not None and se is not None:
            if table.final:
                t_cents, t_detail = payout_from_table(table, rank, tied)
                t_detail = f"{t_detail}; {table.source}; {table.final_note}"
            else:
                t_detail = f"{table.source}; {table.final_note}"
        rep = reported.get((cid, e.entry_id))
        if rep is None and len(per_contest_own.get(cid, [])) == 1:
            rep = reported.get((cid, ""))
        if rep is not None and rep[0] is not None:
            payout, src, detail = rep[0], REPORTED, rep[1]
            if t_cents is not None and t_cents != rep[0]:
                notes.append(f"contest {cid} entry {e.entry_id}: reported ${rep[0] / 100:.2f} but the table gives "
                             f"${t_cents / 100:.2f} ({t_detail}): the reported amount is booked; check it")
        elif se is None:
            payout, src, detail = None, UNKNOWN, "no standings row for this entry (contest not in the standings supplied)"
        elif t_cents is not None:
            payout, src, detail = t_cents, EXACT, t_detail
        else:
            payout, src, detail = None, UNKNOWN, (t_detail or "no prize table: DraftKings contest pages answer HTTP 403 "
                                                  "here; see the winnings template")
        ok, why = _entered((final_lineups or {}).get(e.entry_id), se, pool, pool.mode)
        rows.append(EntryResult(cid, e.contest_name, e.entry_id, cents(e.fee), rank, tied, str(se.points) if se else None,
                                payout, src, detail, t_cents if src == REPORTED else None, ok, why))
        if ok is False:
            notes.append(f"entry {e.entry_id}: {why}")
    for cid, s in by_cid.items():
        if cid not in per_contest_own:
            notes.append(f"contest {cid}: none of this run's entries is in it (skipped)")
    return SlateLedger(run.run_id, m["slate_id"], m["mode"], slate_date(pool), _iso(now), rows, notes)


# -- the ledger file ------------------------------------------------------------------------------------------------

def _path(root: Path) -> Path:
    return Path(root) / "ledger.parquet"


def append(sl: SlateLedger, root: Path | None = None) -> Path:
    """Write the slate's rows, replacing any earlier settle of the same run."""
    import pandas as pd

    root = Path(root) if root is not None else default_root()
    root.mkdir(parents=True, exist_ok=True)
    p = _path(root)
    new = pd.DataFrame([{"run_id": sl.run_id, "slate_id": sl.slate_id, "mode": sl.mode, "slate_date": sl.slate_date,
                         "settled_utc": sl.settled_utc, **{k: v for k, v in asdict(e).items()}} for e in sl.entries])
    if p.exists():
        old = pd.read_parquet(p)
        old = old[old["run_id"] != sl.run_id]
        new = pd.concat([old, new], ignore_index=True)
    for col in ("payout_cents", "table_cents", "rank", "tied"):
        new[col] = new[col].astype("Int64")
    new["entered_matches"] = new["entered_matches"].astype("boolean")
    tmp = p.with_suffix(".tmp")
    new.to_parquet(tmp, index=False)
    os.replace(tmp, p)
    return p


def read(root: Path | None = None):
    import pandas as pd

    p = _path(Path(root) if root is not None else default_root())
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def drawdown(root: Path | None = None) -> dict:
    """Rolling drawdown over settled slates in date order, on known payouts. `complete` is False once any slate
    so far has an UNKNOWN payout (its net is then a lower bound on nothing: the figure is partial)."""
    df = read(root)
    if df.empty:
        return {"runs": 0, "slate_dates": 0, "cum_net_known_cents": 0, "peak_cents": 0, "drawdown_cents": 0, "max_drawdown_cents": 0,
                "complete": True, "series": []}
    rows = []
    for (date, run_id), g in df.sort_values(["slate_date", "run_id"]).groupby(["slate_date", "run_id"], sort=True):
        known = g[g["payout_cents"].notna()]
        rows.append({"slate_date": date, "run_id": run_id,
                     "net_known_cents": int(known["payout_cents"].sum() - known["fee_cents"].sum()),
                     "complete": bool(g["payout_cents"].notna().all())})
    cum = peak = mdd = 0
    complete = True
    for r in rows:
        cum += r["net_known_cents"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        complete = complete and r["complete"]
        r.update({"cum_net_known_cents": cum, "drawdown_cents": peak - cum, "complete_so_far": complete})
    return {"runs": len(rows), "slate_dates": len({r["slate_date"] for r in rows}), "cum_net_known_cents": cum, "peak_cents": peak, "drawdown_cents": peak - cum,
            "max_drawdown_cents": mdd, "complete": complete, "series": rows}
