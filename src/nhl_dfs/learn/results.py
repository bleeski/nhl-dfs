"""C49 (B110): the results report. Ben's NHL entries from DraftKings' contest entry history against a random entry in the same
contests, by format, contest kind, field size, month, own entries per contest and era.

In-sample description only: nothing reads this to tune, select or book money. A slice under 20 slates is THIN: it prints the
count of slates better, worse and even and no interval, and claims nothing. Definitions: docs/CONTRACTS.md, "Results report (C49)".
"""

from __future__ import annotations

import json
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from nhl_dfs.learn import entry_history as eh

REPO_ROOT = Path(__file__).resolve().parents[3]
HISTORY_DIR = REPO_ROOT / "data" / "standings" / "history"
DIMENSIONS = ("format", "kind", "field", "month", "entries", "era", "recorded")
SEED = 49
N_BOOT = 4000
N_SPREAD = 2000
MIN_OWN_FOR_SPREAD = 3
MIN_SLATES = 20
# Fixed indices seed each statistic's generator; a statistic never takes another's index.
STAT_INDEX = {"finish excess": 0, "in-the-money excess": 1, "top-10% excess": 2, "observed finish": 3}
STATISTICS = ("finish excess", "in-the-money excess", "top-10% excess", "observed finish")
EXCESSES = tuple(k for k in STATISTICS if k.endswith("excess"))
CHICAGO = ZoneInfo("America/Chicago")


@dataclass
class Context:
    own_per_contest: Counter
    records: eh.RecordSources | None = None
    join: str = "NONE"  # OK, NONE (no record files) or FAILED (files, but no engine-era entry joins)


def _recorded(r: eh.HistoryRow, c: Context) -> str:
    if c.join == "NONE":
        return "no records"
    if c.join == "FAILED":
        return "join failed"
    return "recorded" if r.entry_key in c.records.ids else "unrecorded"


LABELERS: dict[str, Callable[[eh.HistoryRow, Context], str]] = {
    "format": lambda r, c: r.format,
    "kind": lambda r, c: eh.kind(r.contest_name),
    "field": lambda r, c: eh.field_bucket(r.entrants),
    "month": lambda r, c: r.contest_date_et.strftime("%Y-%m"),
    "entries": lambda r, c: eh.entries_bucket(c.own_per_contest[r.contest_key]),
    "era": lambda r, c: eh.era(r),
    "recorded": _recorded,
}


@dataclass
class Slice:
    label: str
    rows: list[eh.HistoryRow]
    n: int = 0
    slates: int = 0
    dates: int = 0
    contests: int = 0
    fees_cents: int = 0
    net_cents: int = 0
    pool_share_cents: Fraction = Fraction(0)  # sum of pool / N: a random entry's expected winnings
    pct: Fraction = Fraction(0)  # mean observed finish, fraction of the field
    random_pct: Fraction = Fraction(0)
    in_money: int = 0
    exp_in_money: Fraction = Fraction(0)
    top1: int = 0
    exp_top1: Fraction = Fraction(0)
    top10: int = 0
    exp_top10: Fraction = Fraction(0)
    bottom10: int = 0
    exp_bottom10: Fraction = Fraction(0)
    blanks: int = 0
    better: int = 0
    worse: int = 0
    even: int = 0
    intervals: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # statistic -> (point, lo, hi)

    @property
    def thin(self) -> bool:
        return self.slates < MIN_SLATES

    @property
    def roi(self) -> float:
        return self.net_cents / self.fees_cents

    @property
    def rake_roi(self) -> float:
        return float(self.pool_share_cents / self.fees_cents) - 1


def by_slate(rows: Iterable[eh.HistoryRow]) -> dict[tuple[date, str], list[eh.HistoryRow]]:
    out: dict[tuple[date, str], list[eh.HistoryRow]] = defaultdict(list)
    for r in rows:
        out[eh.slate_key(r)].append(r)
    return dict(sorted(out.items()))


def slate_sign(rows: Sequence[eh.HistoryRow]) -> int:
    """-1 better than random (the summed finish is below the summed random finish; lower is better), +1 worse, 0 even."""
    d = sum((r.pct - r.random_pct for r in rows), Fraction(0))
    return (d > 0) - (d < 0)


def summarize(label: str, rows: list[eh.HistoryRow]) -> Slice:
    s = Slice(label=label, rows=rows, n=len(rows))
    slates = by_slate(rows)
    s.slates, s.dates, s.contests = len(slates), len({k[0] for k in slates}), len({r.contest_key for r in rows})
    s.fees_cents = sum(r.fee_cents for r in rows)
    s.net_cents = sum(r.net_cents for r in rows)
    s.pool_share_cents = sum((Fraction(r.pool_cents, r.entrants) for r in rows), Fraction(0))
    s.pct = sum((r.pct for r in rows), Fraction(0)) / s.n
    s.random_pct = sum((r.random_pct for r in rows), Fraction(0)) / s.n
    s.in_money, s.exp_in_money = sum(r.in_money for r in rows), sum((r.exp_in_money for r in rows), Fraction(0))
    s.top1, s.exp_top1 = sum(r.top1 for r in rows), sum((r.exp_top1 for r in rows), Fraction(0))
    s.top10, s.exp_top10 = sum(r.top10 for r in rows), sum((r.exp_top10 for r in rows), Fraction(0))
    s.bottom10, s.exp_bottom10 = sum(r.bottom10 for r in rows), sum((r.exp_bottom10 for r in rows), Fraction(0))
    s.blanks = sum(r.blank for r in rows)
    signs = Counter(slate_sign(v) for v in slates.values())
    s.better, s.worse, s.even = signs[-1], signs[1], signs[0]
    if not s.thin:
        s.intervals = bootstrap(label, slates)
    return s


def _rng(label: str, stat: int) -> np.random.Generator:
    """A fresh generator per slice and statistic, so an interval never depends on --by, other slices or row order."""
    return np.random.default_rng(np.random.SeedSequence([SEED, zlib.crc32(label.encode("utf-8")), stat]))


def bootstrap(label: str, slates: dict[tuple[date, str], list[eh.HistoryRow]]) -> dict[str, tuple[float, float, float]]:
    """90% intervals from N_BOOT resamples of whole slates (sorted by key) of the entry-weighted statistic."""
    groups = list(slates.values())
    n = np.array([len(g) for g in groups], dtype=float)
    obs = np.array([float(sum(r.pct for r in g)) for g in groups])
    rnd = np.array([float(sum(r.random_pct for r in g)) for g in groups])
    itm = np.array([float(sum(r.in_money for r in g)) for g in groups])
    eitm = np.array([float(sum(r.exp_in_money for r in g)) for g in groups])
    t10 = np.array([float(sum(r.top10 for r in g)) for g in groups])
    e10 = np.array([float(sum(r.exp_top10 for r in g)) for g in groups])
    parts = {"finish excess": (obs - rnd, 100.0), "in-the-money excess": (itm - eitm, 100.0),
             "top-10% excess": (t10 - e10, 100.0), "observed finish": (obs, 100.0)}
    out = {}
    for name in STATISTICS:
        num, scale = parts[name]
        idx = _rng(label, STAT_INDEX[name]).integers(0, len(groups), size=(N_BOOT, len(groups)))
        stat = num[idx].sum(axis=1) / n[idx].sum(axis=1) * scale
        lo, hi = np.quantile(stat, [0.05, 0.95])
        out[name] = (float(num.sum() / n.sum() * scale), float(lo), float(hi))
    return out


def parse_by(text: str | None) -> list[tuple[str, ...]]:
    if not text:
        return [(d,) for d in DIMENSIONS]
    out = []
    for item in text.split(","):
        dims = tuple(d.strip() for d in item.split("+") if d.strip())
        bad = [d for d in dims if d not in LABELERS]
        if not dims or bad:
            raise ValueError(f"--by: unknown dimension {', '.join(bad) or repr(item)}; choose from {', '.join(LABELERS)}")
        out.append(dims)
    return out


def tables(rows: list[eh.HistoryRow], by: list[tuple[str, ...]], ctx: Context) -> list[tuple[str, list[Slice]]]:
    every = summarize("all", rows)
    out = []
    for dims in by:
        groups: dict[str, list[eh.HistoryRow]] = defaultdict(list)
        for r in rows:
            groups[" / ".join(LABELERS[d](r, ctx) for d in dims)].append(r)
        name = " + ".join(dims)
        out.append((name, [every] + [summarize(f"{name}={k}", groups[k]) for k in sorted(groups)]))
    return out


# -- render ---------------------------------------------------------------------------------------------------------------

def _usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def _pc(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _row(s: Slice) -> str:
    label = s.label.split("=", 1)[-1]
    signs = f"{s.better} / {s.worse} / {s.even}" + (" THIN" if s.thin else "")
    return (f"| {label} | {s.n} | {s.slates} ({s.dates}) | {s.contests} | {_usd(s.fees_cents)} | {_usd(s.net_cents)} | "
            f"{_pc(s.roi)} | {_pc(s.rake_roi)} | {float(s.pct) * 100:.1f} ({float(s.random_pct) * 100:.1f}) | "
            f"{s.in_money} ({float(s.exp_in_money):.1f}) | {s.top1} ({float(s.exp_top1):.1f}) | {s.top10} ({float(s.exp_top10):.1f}) | "
            f"{s.bottom10} ({float(s.exp_bottom10):.1f}) | {s.blanks} | {signs} |")


TABLE_HEAD = ("| slice | entries | slates (dates) | contests | fees | net | ROI | rake ROI | finish (random) | "
              "in the money (exp) | top 1% (exp) | top 10% (exp) | bottom 10% (exp) | blanks | "
              "slates better / worse / even |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def _interval_lines(slices: list[Slice]) -> list[str]:
    out = []
    for s in slices:
        if s.thin:
            continue
        iv = s.intervals
        f, m, t, o = iv["finish excess"], iv["in-the-money excess"], iv["top-10% excess"], iv["observed finish"]
        out.append(f"- {s.label}: finish {o[0]:.1f} (90% {o[1]:.1f} to {o[2]:.1f}); excess over random {f[0]:+.1f} points "
                   f"(90% {f[1]:+.1f} to {f[2]:+.1f}); in the money {m[0]:+.1f} per 100 entries (90% {m[1]:+.1f} to "
                   f"{m[2]:+.1f}); top 10% {t[0]:+.1f} per 100 entries (90% {t[1]:+.1f} to {t[2]:+.1f})")
    return out


# -- the own-entry spread, the definition checks and the gate line --------------------------------------------------------

@dataclass(frozen=True)
class Spread:
    contest_date_et: date
    format: str
    contest_name: str
    contest_key: str
    entrants: int
    own: int
    sd: float  # population sd of own place / N
    random_median_sd: float
    share_at_or_below: float  # share of random draws whose sd is at or below the observed (near 0 clustered)


def _pop_sd(x: np.ndarray) -> np.ndarray:
    return x.std(axis=-1)  # numpy's default divides by k: the population sd


def spread(rows: list[eh.HistoryRow]) -> list[Spread]:
    """For each contest with MIN_OWN_FOR_SPREAD or more own entries: their finish spread against N_SPREAD random draws of as
    many distinct places from 1..N (the review's within-contest check)."""
    per: dict[str, list[eh.HistoryRow]] = defaultdict(list)
    for r in rows:
        per[r.contest_key].append(r)
    out = []
    for key, rs in per.items():
        k, n = len(rs), rs[0].entrants
        if k < MIN_OWN_FOR_SPREAD or k > n:
            continue
        sd = float(_pop_sd(np.array([r.place / n for r in rs])))
        rng = np.random.default_rng(np.random.SeedSequence([SEED, int(key)]))
        draws = np.array([rng.choice(n, size=k, replace=False) for _ in range(N_SPREAD)], dtype=float)
        ref = _pop_sd((draws + 1) / n)
        out.append(Spread(rs[0].contest_date_et, rs[0].format, rs[0].contest_name, key, n, k, sd, float(np.median(ref)),
                          float(np.mean(ref <= sd + 1e-12))))
    return sorted(out, key=lambda s: (s.contest_date_et, s.format, int(s.contest_key)))


def definition_checks(s: Slice) -> str:
    rows = s.rows
    entry_mean = sum((Fraction(r.pool_cents, r.entrants * r.fee_cents) for r in rows), Fraction(0)) / len(rows) - 1
    cash_net = sum(r.cash_cents - r.fee_cents for r in rows)
    card_itm = sum((r.exp_in_money_card for r in rows), Fraction(0))
    above = sum(r.places_paid > r.entrants for r in rows)
    disagree = sum(r.in_money != (r.cash_cents + r.ticket_cents > 0) for r in rows)
    return (f"- {s.label}: rake ROI fee-weighted {_pc(s.rake_roi)} (used), entry mean {_pc(float(entry_mean))}; net with "
            f"tickets {_usd(s.net_cents)} (used), cash only {_usd(cash_net)}; expected in the money min(P, N) / N "
            f"{float(s.exp_in_money):.2f} (used), P / N {float(card_itm):.2f}, {above} entries with places paid above "
            f"entrants; {disagree} entries where place <= places paid disagrees with winnings above $0; finish "
            f"{float(s.pct) * 100:.2f} against {float(s.random_pct) * 100:.2f}; top 10% expected {float(s.exp_top10):.2f}.")


def gate_line(ledger_root: Path) -> str:
    """The ownership gate's progress toward C12 (field_fit) from the settled evidence index; read only."""
    p = Path(ledger_root) / "graded.json"
    if not p.exists():
        return f"Ownership gate (C12 field_fit): nothing settled on this machine ({p} absent)."
    try:
        from nhl_dfs.contracts.geometry import Mode
        from nhl_dfs.learn import gates, settle

        idx = json.loads(p.read_text(encoding="utf-8"))
        cfg = gates.load_floors()
        tier = cfg["actions"]["field_fit"]
        parts = []
        for mode in Mode:
            counts = settle.evidence_counts(idx, mode.value)
            bits = [f"{name} not counted yet (B82) of {need:,}" if name == "holdout_groups" else
                    f"{name} {counts.value(name):,} of {need:,}" for name, need in cfg["tiers"][mode.value][tier].items()]
            ok, _ = gates.allows("field_fit", counts, mode, cfg=cfg)
            parts.append(f"{mode.value} " + ", ".join(bits) + (" (met)" if ok else ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"Ownership gate (C12 field_fit): {p} unreadable ({type(exc).__name__}: {exc})."
    return f"Ownership gate (C12 field_fit, tier {tier}, {p}): " + "; ".join(parts) + "."


def render(hf: eh.HistoryFile, rows: list[eh.HistoryRow], since: date | None, built: list[tuple[str, list[Slice]]],
           ctx: Context, spreads: list[Spread], gate: str) -> str:
    paid = [r for r in hf.rows if r.paid]
    shown = {s.label for _, sl in built for s in sl if not s.thin}  # the all row is one slice, however many tables
    n_iv = len(shown) * len(EXCESSES)
    first = min((r.contest_date_et for r in rows), default=None)
    last = max((r.contest_date_et for r in rows), default=None)
    engine = [r for r in rows if eh.era(r) == "engine"]
    rec = ctx.records
    if ctx.join == "NONE":
        rec_line = "- Recorded: no entry records on this machine (data/entered, runs/*/inputs/DKEntries.csv, " \
                   "outputs/*/DKEntries.csv); the recorded slice reads 'no records'."
    else:
        joined = sum(r.entry_key in rec.ids for r in engine)
        rec_line = (f"- Recorded: {len(rec.ids):,} entry ids from {len(rec.files)} record files, {len(rec.unreadable)} "
                    f"unreadable; {joined} of {len(engine)} engine-era entries in the window join"
                    + ("; RECORDED_JOIN=FAILED: record files exist but no engine-era entry joins, so the recorded slice "
                       "reads 'join failed'" if ctx.join == "FAILED" else "") + ".")
    lines = [
        "# NHL results against a random entry (C49)",
        "",
        f"- Export: {hf.path.name} (sha256 {hf.sha256[:12]}): {len(hf.rows)} NHL rows with a result, {len(paid)} paid, "
        f"{len(hf.rows) - len(paid)} free left out; {hf.unsettled_rows} NHL rows unsettled (blank place or points: the "
        f"contest was open) left out; {hf.other_sport_rows} rows of other sports counted, not read.",
        f"- Window: Eastern contest dates {first} to {last}" + (f" (since {since})" if since else "") +
        f": {len(rows)} paid entries.",
        "- Finish = place / entrants x 100, lower is better. Random = each entry's own expectation in its contest: finish "
        "(N + 1) / 2N; in the money min(P, N) / N; top 1% floor(N / 100) / N; top 10% floor(N / 10) / N; bottom 10% "
        "(N - floor(9N / 10)) / N.",
        "- Place is used as DraftKings gives it: tied entries share the best place, which flatters observed finishes "
        "slightly, most in Showdown.",
        "- A slate is the Eastern date of Contest_Date_EST as written plus the format. A slate is better when its "
        "entries' summed finish is below their summed random finish. Under 20 slates a slice is THIN: slate counts only, "
        "no interval, no finding.",
        f"- Intervals: 90%, {N_BOOT:,} resamples of whole slates, seed {SEED}; {n_iv} printed. About 1 in 10 misses zero "
        "by chance, so none is a finding alone.",
        f"- Kind by contest name ({eh.KIND_SOURCE}). Era: engine from {eh.ENGINE_ERA_START}. Net counts ticket winnings; "
        "rake ROI = pool / entrants summed over the entries, over fees, minus 1.",
        rec_line,
        "- In-sample description: nothing here tunes, selects or books money.",
    ]
    for name, slices in built:
        lines += ["", f"## By {name}", "", TABLE_HEAD] + [_row(s) for s in slices]
        iv = _interval_lines(slices)
        if iv:
            lines += [""] + iv
    lines += ["", f"## Definition checks (by {built[0][0]})", ""] + [definition_checks(s) for s in built[0][1]]
    lines += ["", f"## Own-entry spread (contests with {MIN_OWN_FOR_SPREAD} or more own entries)", "",
              f"Population sd of own place / N against {N_SPREAD:,} random draws of as many distinct places; a share near 0 "
              "means the entries finished closer together than random, near 1 further apart.", ""]
    if spreads:
        lines += ["| date | format | contest | entrants | own | sd | random median sd | share of random at or below |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {x.contest_date_et} | {x.format} | {x.contest_name} | {x.entrants:,} | {x.own} | {x.sd:.3f} | "
                  f"{x.random_median_sd:.3f} | {x.share_at_or_below:.2f} |" for x in spreads]
    else:
        lines.append("- none in the window")
    lines += ["", gate]
    return "\n".join(lines) + "\n"


# -- the command ----------------------------------------------------------------------------------------------------------

def newest_history(folder: Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else HISTORY_DIR
    files = sorted(folder.glob("*.csv"), key=lambda p: (p.stat().st_mtime, p.name)) if folder.is_dir() else []
    return files[-1] if files else None


def select(hf: eh.HistoryFile, since: date | None) -> list[eh.HistoryRow]:
    return [r for r in hf.rows if r.paid and (since is None or r.contest_date_et >= since)]


def context(rows: list[eh.HistoryRow], records: eh.RecordSources | None) -> Context:
    ctx = Context(own_per_contest=Counter(r.contest_key for r in rows), records=records)
    if records is None or not records.files:
        ctx.join = "NONE"
    elif any(eh.era(r) == "engine" for r in rows) and not any(
            r.entry_key in records.ids for r in rows if eh.era(r) == "engine"):
        ctx.join = "FAILED"
    else:
        ctx.join = "OK"
    return ctx


def build(history: Path, *, since: date | None = None, by: str | None = None, records: eh.RecordSources | None = None,
          ledger_root: Path | None = None) -> tuple[str, list[tuple[str, list[Slice]]]]:
    hf = eh.read_history(history)
    rows = select(hf, since)
    if not rows:
        raise ValueError(f"{Path(history).name}: no paid NHL entries" + (f" since {since}" if since else ""))
    ctx = context(rows, records)
    built = tables(rows, parse_by(by), ctx)
    if ledger_root is None:
        from nhl_dfs.learn import ledger

        ledger_root = ledger.default_root()
    return render(hf, rows, since, built, ctx, spread(rows), gate_line(Path(ledger_root))), built


def write(text: str, outputs_root: Path, now: datetime | None = None) -> Path:
    day = (now or datetime.now(CHICAGO)).astimezone(CHICAGO).date().isoformat()
    out = Path(outputs_root) / "results" / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
