"""Standings pull checklist: which contests Ben entered still need their results export pulled.

Reads only local files and never touches the network. It finds every contest in the DKEntries files the
repo keeps, classifies each one by what is on disk, and writes a markdown list and a clickable HTML
checklist under data/standings/. The export URL is only ever written as a string built from a contest id
in Ben's own entry file; the click happens in Ben's own browser, while logged in.

Entry-file locations scanned (see LOCATIONS in the report):
- runs/<run id>/inputs/*.csv    snapshot when its sha256 equals the manifest's entries_sha256
- outputs/<slate>/*.csv         snapshot when its sha256 equals published.json's sha256
- runs/<other folder>/*.csv     loose (demo copies); runs/_* folders are scratch and are not scanned
- <repo root>/*.csv             loose
- tests/fixtures/real/**/*.csv  loose (personal exports, gitignored)
- --also <dir>                  loose, recursive (for example a Downloads folder)
runs/ and outputs/ are gitignored: a slate run in a cloud session exists only in that container until its
entry file is placed in one of these folders on this machine. The report names slates that tracked review
notes mention but that have no local folder.

Contest ids found in tests/fixtures (outside real/) are synthetic and reported apart, never as owed.
State per contest, strongest first: settled (data/ledger/graded.json) > filed (any file in the standings
inbox whose name holds the contest id) > unrecoverable/placeholder (dispositions.json, set by Ben) >
fixture > awaiting. The repo has no normalize stage, so there is no "normalized" state.

Dates are a proxy for DraftKings' contest date: the slate date in the run's manifest or the slate folder
name, else the run id (UTC, converted to America/Chicago), else the file's modification time.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nhl_dfs.intake.entries import read_entries  # noqa: E402  (the repo's own entry-file parser)

RUNS_DIR = REPO_ROOT / "runs"
OUTPUTS_DIR = REPO_ROOT / "outputs"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
REAL_FIXTURES_DIR = FIXTURES_DIR / "real"
STANDINGS_DIR = REPO_ROOT / "data" / "standings"
REVIEWS_DIR = REPO_ROOT / "reviews"

# Assumption: DraftKings' per-contest standings export. It cannot be checked from here (no network, and the
# contest pages answer 403 to this machine). Override with --url-template if a click does not download a file.
EXPORT_URL_TEMPLATE = "https://www.draftkings.com/contest/exportfullstandingscsv/{contest_id}"

MD_NAME = "CONTESTS_AWAITING_STANDINGS.md"
HTML_STABLE = "CONTESTS_AWAITING_STANDINGS.html"
HTML_DATED = "standings_pulls_{day}.html"
DATE_NOTE = ("Dates are a proxy, not DraftKings' contest date: the slate date in the run manifest or slate "
             "folder name, else the run id (UTC, shown in America/Chicago), else the file's modification time.")
INBOX_NOTE = ("The standings inbox is read-only here. It is not flat (a dated folder with extracted copies), "
              "so every file under it counts, matched by contest id in the file name.")

_RUN_ID = re.compile(r"^(\d{8})-(\d{6})-")
_SLATE_DATE = re.compile(r"-(\d{8})-")
_SLATE_ID = re.compile(r"\b(?:classic|showdown)-\d{8}-[0-9a-f]{10}\b")
_ID_TOKEN = re.compile(r"(?<!\d)(\d+)(?!\d)")
_STATUS_ORDER = ("awaiting", "filed", "settled", "unrecoverable", "placeholder", "fixture")


# ---------------------------------------------------------------------------- helpers

def _chicago_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Chicago")
    except Exception:  # no tz database: fall back to UTC, the note still says what was done
        return timezone.utc


def _local_date(dt_utc: datetime) -> date:
    return dt_utc.astimezone(_chicago_zone()).date()


def _ymd(text: str) -> date | None:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime_date(path: Path) -> date | None:
    try:
        return _local_date(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return None


def _ledger_dir() -> Path:
    return Path(os.environ.get("NHL_DFS_LEDGER_ROOT") or REPO_ROOT / "data" / "ledger")


def _is_run_dir(name: str) -> bool:
    return _RUN_ID.match(name) is not None


# ---------------------------------------------------------------------------- discovery

def _candidates(also: list[Path]) -> tuple[list[dict], int]:
    """Entry-file candidates as dicts (path, location, kind, evidence, basis, note); and the count of scratch
    run folders (runs/_*) that are not scanned."""
    out: list[dict] = []
    scratch = 0

    def add(path, location, kind, evidence, basis, note=""):
        out.append({"path": path, "location": location, "kind": kind, "evidence": evidence, "basis": basis,
                    "note": note})

    if RUNS_DIR.is_dir():
        for d in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()):
            if d.name.startswith("_"):
                scratch += 1
            elif _is_run_dir(d.name):
                manifest = _json(d / "manifest.json") or {}
                slate = _SLATE_DATE.search(str(manifest.get("slate_id", "")))
                m = _RUN_ID.match(d.name)
                if slate and _ymd(slate.group(1)):
                    ev, basis = _ymd(slate.group(1)), "slate id in the run manifest"
                else:
                    ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    ev, basis = _local_date(ts), "run id (UTC to America/Chicago)"
                for f in sorted((d / "inputs").glob("*.csv")) if (d / "inputs").is_dir() else []:
                    bound = manifest.get("entries_sha256") == _sha(f)
                    add(f, "runs/*/inputs", "snapshot" if bound else "loose", ev, basis,
                        "" if bound else "hash does not match the run manifest")
            else:
                for f in sorted(d.glob("*.csv")):
                    add(f, "runs/<other>", "loose", _mtime_date(f), "file mtime")
    if OUTPUTS_DIR.is_dir():
        for d in sorted(p for p in OUTPUTS_DIR.iterdir() if p.is_dir()):
            m = _SLATE_DATE.search(d.name)
            ev, basis = (_ymd(m.group(1)), "slate folder name") if m and _ymd(m.group(1)) else (None, "")
            pub = _json(d / "published.json") or {}
            for f in sorted(d.glob("*.csv")):
                bound = pub.get("sha256") == _sha(f)
                add(f, "outputs/*", "snapshot" if bound else "loose", ev or _mtime_date(f),
                    basis or "file mtime", "" if bound else "hash does not match published.json")
    for f in sorted(REPO_ROOT.glob("*.csv")):
        add(f, "repo root", "loose", _mtime_date(f), "file mtime")
    if REAL_FIXTURES_DIR.is_dir():
        for f in sorted(REAL_FIXTURES_DIR.rglob("*.csv")):
            add(f, "tests/fixtures/real", "loose", _mtime_date(f), "file mtime")
    for extra in also:
        if extra.is_dir():
            for f in sorted(extra.rglob("*.csv")):
                add(f, f"--also {extra.name}", "loose", _mtime_date(f), "file mtime")
    return out, scratch


def _fixture_ids() -> dict[str, str]:
    """Contest ids in committed test fixtures (not tests/fixtures/real): synthetic, never owed."""
    found: dict[str, str] = {}
    if not FIXTURES_DIR.is_dir():
        return found
    real = REAL_FIXTURES_DIR.resolve()
    for f in sorted(FIXTURES_DIR.rglob("*.csv")):
        if "entries" not in f.name.lower() or real in f.resolve().parents:
            continue
        try:
            for row in read_entries(f).entries:
                found.setdefault(str(row.contest_id), _rel(f))
        except (ValueError, OSError, UnicodeDecodeError):
            continue
    return found


def _settled_ids() -> set[str]:
    idx = _json(_ledger_dir() / "graded.json")
    ids: set[str] = set()
    if isinstance(idx, dict):
        for rec in idx.values():
            if isinstance(rec, dict) and isinstance(rec.get("contests"), dict):
                ids.update(str(k) for k in rec["contests"])
    return ids


def _inbox_files() -> list[Path]:
    inbox = STANDINGS_DIR / "inbox"
    return sorted(p for p in inbox.rglob("*") if p.is_file()) if inbox.is_dir() else []


def _blank_winnings() -> list[dict]:
    rows: list[dict] = []
    for f in _inbox_files():
        if f.suffix.lower() != ".csv" or not f.name.lower().startswith("winnings"):
            continue
        try:
            with f.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("contest_id") and not (r.get("winnings_usd") or "").strip():
                        rows.append({"contest_id": r["contest_id"].strip(), "entry_id": (r.get("entry_id") or "").strip(),
                                     "file": _rel(f)})
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return rows


def _unseen_slates() -> list[dict]:
    """Slate ids that tracked review notes mention but that have no outputs/ folder and no run on this machine."""
    local = {p.name for p in OUTPUTS_DIR.iterdir() if p.is_dir()} if OUTPUTS_DIR.is_dir() else set()
    if RUNS_DIR.is_dir():
        for d in RUNS_DIR.iterdir():
            slate = (_json(d / "manifest.json") or {}).get("slate_id") if d.is_dir() else None
            if slate:
                local.add(str(slate))
    cited: dict[str, list[str]] = {}
    if REVIEWS_DIR.is_dir():
        for f in sorted(REVIEWS_DIR.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for sid in sorted(set(_SLATE_ID.findall(text))):
                cited.setdefault(sid, []).append(_rel(f))
    return [{"slate_id": sid, "cited_in": files} for sid, files in sorted(cited.items()) if sid not in local]


# ---------------------------------------------------------------------------- dispositions

def _disp_path() -> Path:
    return STANDINGS_DIR / "dispositions.json"


def load_dispositions() -> dict[str, dict]:
    data = _json(_disp_path())
    return data if isinstance(data, dict) else {}


def mark(contest_id: str, kind: str, reason: str, now: datetime | None = None) -> dict:
    """Record Ben's call that a contest's export cannot be pulled (unrecoverable) or is a stand-in id (placeholder).
    Refuses a duplicate and never guesses."""
    if not contest_id.isdigit():
        raise ValueError(f"contest id must be digits, got {contest_id!r}")
    if kind not in ("unrecoverable", "placeholder"):
        raise ValueError(f"unknown disposition {kind!r}")
    if not reason.strip():
        raise ValueError("a reason is required")
    disp = load_dispositions()
    if contest_id in disp:
        raise ValueError(f"contest {contest_id} already has a disposition ({disp[contest_id]['kind']}: "
                         f"{disp[contest_id]['reason']}); edit {_rel(_disp_path())} by hand to change it")
    disp[contest_id] = {"kind": kind, "reason": reason.strip(),
                        "marked_utc": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    STANDINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _disp_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(disp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(_disp_path())
    return disp[contest_id]


# ---------------------------------------------------------------------------- report

def export_url(template: str, contest_id: str) -> str | None:
    if not contest_id.isdigit() or "{contest_id}" not in template:
        return None
    return template.replace("{contest_id}", contest_id)


def build_report(also: list[Path] | None = None, template: str = EXPORT_URL_TEMPLATE) -> dict:
    cands, scratch = _candidates(list(also or []))
    scan: dict[str, dict] = {}
    skipped: list[dict] = []
    contests: dict[str, dict] = {}
    for c in cands:
        loc = scan.setdefault(c["location"], {"files": 0, "parsed": 0, "skipped": 0})
        loc["files"] += 1
        try:
            ef = read_entries(c["path"])
            rows = list(ef.entries)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            loc["skipped"] += 1
            skipped.append({"file": _rel(c["path"]), "location": c["location"], "reason": str(exc).replace(str(c["path"]), _rel(c["path"]))[:120]})
            continue
        if not rows:
            loc["skipped"] += 1
            skipped.append({"file": _rel(c["path"]), "location": c["location"], "reason": "no entry rows"})
            continue
        loc["parsed"] += 1
        mode = str(getattr(ef.mode, "value", ef.mode))
        for r in rows:
            cid = str(r.contest_id).strip()
            k = contests.setdefault(cid, {"id": cid, "name": "", "fee": "", "mode": mode, "entry_ids": set(),
                                          "kinds": set(), "sources": [], "_best": None})
            k["entry_ids"].add(str(r.entry_id))
            k["kinds"].add(c["kind"])
            rel = _rel(c["path"])
            if rel not in k["sources"]:
                k["sources"].append(rel)
            rank = c["evidence"].toordinal() if c["evidence"] else -1
            if k["_best"] is None or rank > k["_best"]:  # name, fee and mode come from the newest evidence
                k["_best"] = rank
                k.update(name=r.contest_name, fee=r.fee, mode=mode,
                         evidence=c["evidence"], basis=c["basis"] if c["evidence"] else "no date")
    fixtures = _fixture_ids()
    settled = _settled_ids()
    disp = load_dispositions()
    files_by_id: dict[str, list[str]] = {}
    unmatched: list[str] = []
    known = set(contests) | set(fixtures) | set(disp)
    for f in _inbox_files():
        tokens = set(_ID_TOKEN.findall(f.name))
        hit = tokens & known  # a known contest id as a whole digit run in the file name
        for t in hit:
            files_by_id.setdefault(t, []).append(_rel(f))
        if not hit and any(len(t) >= 6 for t in tokens):  # a long number that is no entered contest
            unmatched.append(_rel(f))
    out_contests = []
    for cid, k in contests.items():
        if cid in settled:
            status = "settled"
        elif cid in files_by_id:
            status = "filed"
        elif cid in disp:
            status = disp[cid]["kind"]
        elif cid in fixtures:
            status = "fixture"
        else:
            status = "awaiting"
        ev = k.get("evidence")
        out_contests.append({
            "id": cid, "name": k["name"], "fee": k["fee"], "mode": k["mode"], "entries": len(k["entry_ids"]),
            "status": status, "loose_only": k["kinds"] == {"loose"}, "sources": sorted(k["sources"]),
            "evidence_date": ev.isoformat() if ev else None, "evidence_basis": k.get("basis", ""),
            "raw_on_disk": cid in files_by_id, "settled": cid in settled,
            "inbox_files": sorted(files_by_id.get(cid, [])), "disposition": disp.get(cid),
            "fixture_evidence": fixtures.get(cid), "export_url": export_url(template, cid),
        })
    out_contests.sort(key=lambda c: (c["evidence_date"] is None, c["evidence_date"] or "", int(c["id"]) if c["id"].isdigit() else 0))
    counts = {s: sum(1 for c in out_contests if c["status"] == s) for s in _STATUS_ORDER}
    counts["dispositioned"] = counts["unrecoverable"] + counts["placeholder"]
    counts["total"] = len(out_contests) - counts["fixture"]
    return {
        "counts": counts, "contests": out_contests, "scan": scan, "skipped_files": skipped,
        "scratch_run_folders_not_scanned": scratch, "unmatched_inbox_files": sorted(unmatched),
        "loose_only": [c["id"] for c in out_contests if c["loose_only"] and c["status"] == "awaiting"],
        "unseen_slates": _unseen_slates(), "winnings_blank": _blank_winnings(),
        "disposition_ids_not_in_any_entry_file": sorted(set(disp) - set(contests)),
        "export_url_template": template, "date_note": DATE_NOTE, "inbox_note": INBOX_NOTE,
    }


def awaiting_groups(report: dict) -> list[tuple[str | None, list[dict]]]:
    """Awaiting contests grouped by evidence date, oldest first, unknown date last."""
    groups: dict[str | None, list[dict]] = {}
    for c in report["contests"]:
        if c["status"] == "awaiting":
            groups.setdefault(c["evidence_date"], []).append(c)
    return sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))


# ---------------------------------------------------------------------------- markdown

def _fee(c: dict) -> str:
    return c["fee"] or "fee ?"


def render_markdown(report: dict, today: date) -> str:
    n = report["counts"]
    lines = [f"# Contests awaiting standings ({today.isoformat()})", "",
             "Click each Open export link while logged in to DraftKings, confirm the file is not 0 bytes, and "
             "drop it unmodified in `data/standings/inbox/`. Written by `scripts/standings_checklist.py`, "
             "which never contacts DraftKings.", "",
             f"awaiting {n['awaiting']} | filed {n['filed']} | settled {n['settled']} | "
             f"dispositioned {n['dispositioned']} | total {n['total']} "
             f"(plus {n['fixture']} synthetic test contest id(s) not counted)", ""]
    groups = awaiting_groups(report)
    if not groups:
        lines += ["## Awaiting", "", "Nothing awaiting among the contests this scan can see "
                  "(see the notes below for what it cannot see).", ""]
    for day, items in groups:
        lines += [f"## {day or 'Date unknown'} ({len(items)} contest{'s' if len(items) != 1 else ''})", ""]
        for c in items:
            tags = f"{c['mode']}, {_fee(c)}, {c['entries']} entr{'y' if c['entries'] == 1 else 'ies'}"
            if c["loose_only"]:
                tags += ", loose"
            link = f" - [Open export]({c['export_url']})" if c["export_url"] else ""
            lines.append(f"- [ ] {c['name'] or '(no name)'} ({tags}) `{c['id']}`{link}")
        lines.append("")
    rest = [c for c in report["contests"] if c["status"] != "awaiting"]
    if rest:
        lines += ["## Not awaiting", "", "| Contest | Name | Status | Export on disk | Settled | Note |",
                  "|---|---|---|---|---|---|"]
        for c in rest:
            note = (c["disposition"] or {}).get("reason") or (f"fixture: {c['fixture_evidence']}" if c["status"] == "fixture" else "")
            lines.append(f"| `{c['id']}` | {c['name']} | {c['status']} | {'yes' if c['raw_on_disk'] else 'no'} | "
                         f"{'yes' if c['settled'] else 'no'} | {note.replace('|', '/')} |")
        lines.append("")
    lines += ["## Notes", ""]
    if report["loose_only"]:
        lines.append("- Found only in loose (hand-editable) files: " + ", ".join(f"`{i}`" for i in report["loose_only"]))
    if report["unmatched_inbox_files"]:
        lines.append("- Inbox files whose id matches no entered contest: " +
                     ", ".join(f"`{p}`" for p in report["unmatched_inbox_files"]))
    for s in report["unseen_slates"]:
        lines.append(f"- Slate `{s['slate_id']}` is cited in {', '.join(s['cited_in'])} but has no entry file on "
                     "this machine, so its contests cannot be listed here. If you entered contests on it, copy its "
                     "DKEntries file into `outputs/<slate>/` or the repo root and run this again.")
    if report["winnings_blank"]:
        lines.append(f"- {len(report['winnings_blank'])} winnings amount(s) are blank in the inbox winnings.csv "
                     "(owed to settle, not a standings pull): " +
                     ", ".join(f"`{w['contest_id']}`/`{w['entry_id']}`" for w in report["winnings_blank"]))
    if report["skipped_files"]:
        lines.append(f"- {len(report['skipped_files'])} file(s) the entry parser rejected were skipped "
                     "(salary files and the like); see `--json` for each reason.")
    lines += [f"- {report['date_note']}", f"- {report['inbox_note']}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- html

_CSS = """
:root{color-scheme:dark;--bg:#12151c;--card:#1b2030;--line:#2b3247;--fg:#e6e9f2;--mut:#9aa3b8;--ok:#1f4d36;--okfg:#7be0a8;--amb:#e8b04a;--blue:#3b82f6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.3rem;margin:.2rem 0 .6rem}
h2{font-size:1.05rem;margin:1.4rem 0 .5rem;color:var(--mut);font-weight:600}
.counts{display:flex;flex-wrap:wrap;gap:6px;margin:.4rem 0 .8rem}
.counts span{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 10px;color:var(--mut)}
.counts b{color:var(--fg)}
.how{color:var(--mut);margin:.2rem 0 .8rem}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line)}
.track{height:10px;background:var(--card);border:1px solid var(--line);border-radius:999px;overflow:hidden;margin-top:6px}
.fill{height:100%;width:0;background:var(--okfg);transition:width .15s}
ul.list{list-style:none;margin:0;padding:0}
li.row{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:6px 0}
li.row.done{background:var(--ok);border-color:#2f7a55}
li.row.done .name{text-decoration:line-through;color:var(--okfg)}
li.row input{width:22px;height:22px;flex:none}
.main{flex:1;min-width:0}
.name{overflow-wrap:anywhere}
.tag{display:inline-block;font-size:.75rem;color:var(--mut);border:1px solid var(--line);border-radius:6px;padding:0 6px;margin-left:6px;white-space:nowrap}
.tag.loose{color:var(--amb);border-color:var(--amb)}
.cid{font:12px ui-monospace,Consolas,monospace;color:var(--mut)}
a.btn{flex:none;background:var(--blue);color:#fff;text-decoration:none;border-radius:8px;padding:8px 12px;font-weight:600;white-space:nowrap}
a.btn.none{background:var(--line);color:var(--mut)}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
.note{color:var(--mut);font-size:.85rem;margin:.3rem 0}
code{font:12px ui-monospace,Consolas,monospace;white-space:nowrap}
.tw{overflow-x:auto}
@media(max-width:520px){li.row{flex-wrap:wrap}a.btn{width:100%;text-align:center}}
"""

_JS = """
(function(){
  var KEY='standings_pulls_ticks_v1', rows=[].slice.call(document.querySelectorAll('li.row[data-id]'));
  function load(){try{return JSON.parse(localStorage.getItem(KEY)||'{}')||{};}catch(e){return {};}}
  function save(m){try{localStorage.setItem(KEY,JSON.stringify(m));}catch(e){}}
  var ticks=load();
  function paint(){
    var done=0;
    rows.forEach(function(r){var on=r.querySelector('input').checked;r.classList.toggle('done',on);if(on)done++;});
    document.getElementById('pulled').textContent=done+' / '+rows.length+' pulled';
    document.getElementById('fill').style.width=(rows.length?100*done/rows.length:0)+'%';
  }
  function setTick(r,on){
    r.querySelector('input').checked=on;
    var id=r.getAttribute('data-id'); if(on){ticks[id]=1;}else{delete ticks[id];}
    save(ticks); paint();
  }
  rows.forEach(function(r){
    r.querySelector('input').checked=!!ticks[r.getAttribute('data-id')];
    r.querySelector('input').addEventListener('change',function(e){setTick(r,e.target.checked);});
    var a=r.querySelector('a.btn[href]'); if(a){a.addEventListener('click',function(){setTick(r,true);});}
  });
  paint();
})();
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def render_html(report: dict, today: date) -> str:
    n = report["counts"]
    groups = awaiting_groups(report)
    total_rows = sum(len(items) for _, items in groups)
    body: list[str] = []
    for day, items in groups:
        body.append(f"<h2>{_e(day or 'Date unknown')} ({len(items)} contest{'s' if len(items) != 1 else ''})</h2>")
        body.append('<ul class="list">')
        for c in items:
            tags = [c["mode"], _fee(c), f"{c['entries']} entr{'y' if c['entries'] == 1 else 'ies'}"]
            tag_html = "".join(f'<span class="tag">{_e(t)}</span>' for t in tags)
            if c["loose_only"]:
                tag_html += '<span class="tag loose">loose</span>'
            btn = (f'<a class="btn" href="{_e(c["export_url"])}" target="_blank" rel="noopener">Open export &#8599;</a>'
                   if c["export_url"] else '<a class="btn none">no link</a>')
            body.append(
                f'<li class="row" data-id="{_e(c["id"])}"><input type="checkbox" aria-label="pulled {_e(c["id"])}">'
                f'<div class="main"><span class="name">{_e(c["name"] or "(no name)")}</span>{tag_html}'
                f'<div class="cid">{_e(c["id"])}</div></div>{btn}</li>')
        body.append("</ul>")
    if not groups:
        body.append('<p class="note">Nothing awaiting among the contests this scan can see. See the notes below '
                    "for what it cannot see.</p>")
    rest = [c for c in report["contests"] if c["status"] != "awaiting"]
    if rest:
        body.append("<h2>Not awaiting</h2><div class=\"tw\"><table><tr><th>Contest</th><th>Name</th><th>Status</th><th>Export on disk</th>"
                    "<th>Settled</th><th>Note</th></tr>")
        for c in rest:
            note = (c["disposition"] or {}).get("reason") or (f"fixture: {c['fixture_evidence']}" if c["status"] == "fixture" else "")
            body.append(f"<tr><td><code>{_e(c['id'])}</code></td><td>{_e(c['name'])}</td><td>{_e(c['status'])}</td>"
                        f"<td>{'yes' if c['raw_on_disk'] else 'no'}</td><td>{'yes' if c['settled'] else 'no'}</td>"
                        f"<td>{_e(note)}</td></tr>")
        body.append("</table></div>")
    notes: list[str] = []
    if report["loose_only"]:
        notes.append("Found only in loose (hand-editable) files: " + ", ".join(f"<code>{_e(i)}</code>" for i in report["loose_only"]))
    if report["unmatched_inbox_files"]:
        notes.append("Inbox files whose id matches no entered contest: " +
                     ", ".join(f"<code>{_e(p)}</code>" for p in report["unmatched_inbox_files"]))
    for s in report["unseen_slates"]:
        notes.append(f"Slate <code>{_e(s['slate_id'])}</code> is cited in {_e(', '.join(s['cited_in']))} but has no entry "
                     "file on this machine, so its contests cannot be listed here. If you entered contests on it, copy "
                     "its DKEntries file into <code>outputs/&lt;slate&gt;/</code> or the repo root and run this again.")
    if report["winnings_blank"]:
        notes.append(f"{len(report['winnings_blank'])} winnings amount(s) are blank in the inbox winnings.csv (owed to "
                     "settle, not a standings pull): " +
                     ", ".join(f"<code>{_e(w['contest_id'])}/{_e(w['entry_id'])}</code>" for w in report["winnings_blank"]))
    if report["skipped_files"]:
        notes.append(f"{len(report['skipped_files'])} file(s) the entry parser rejected were skipped "
                     "(salary files and the like).")
    notes += [_e(report["date_note"]), _e(report["inbox_note"])]
    body.append("<h2>Notes</h2>" + "".join(f'<p class="note">{x}</p>' for x in notes))
    counts = "".join(f"<span><b>{v}</b> {k}</span>" for k, v in
                     (("awaiting", n["awaiting"]), ("filed", n["filed"]), ("settled", n["settled"]),
                      ("dispositioned", n["dispositioned"]), ("total", n["total"])))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Standings pulls {_e(today.isoformat())}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>Standings to pull ({_e(today.isoformat())})</h1>"
        f'<div class="counts">{counts}</div>'
        '<p class="how">Click <b>Open export</b> while logged in to DraftKings, confirm the file is not 0 bytes, '
        "and drop it unmodified in <code>data/standings/inbox/</code>. Clicking also ticks the row.</p>"
        f'<div class="bar"><span id="pulled">0 / {total_rows} pulled</span>'
        '<div class="track"><div class="fill" id="fill"></div></div></div>'
        + "".join(body) + f"<script>{_JS}</script></main></body></html>")


# ---------------------------------------------------------------------------- output

def write_outputs(report: dict, today: date) -> list[Path]:
    STANDINGS_DIR.mkdir(parents=True, exist_ok=True)
    md = STANDINGS_DIR / MD_NAME
    dated = STANDINGS_DIR / HTML_DATED.format(day=today.isoformat())
    stable = STANDINGS_DIR / HTML_STABLE
    md.write_text(render_markdown(report, today), encoding="utf-8")
    dated.write_text(render_html(report, today), encoding="utf-8")
    newest = max(STANDINGS_DIR.glob("standings_pulls_*.html"), key=lambda p: p.name)
    shutil.copyfile(newest, stable)
    return [md, dated, stable]


def _today() -> date:
    return _local_date(datetime.now(timezone.utc))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List the contests whose results export has not been pulled yet.")
    ap.add_argument("--json", action="store_true", help="print the machine-readable report; write nothing")
    ap.add_argument("--also", action="append", default=[], metavar="DIR",
                    help="also scan this folder (recursive) for entry files, tagged loose; repeatable")
    ap.add_argument("--url-template", default=EXPORT_URL_TEMPLATE, help="export link, with {contest_id}")
    ap.add_argument("--mark-unrecoverable", nargs=2, metavar=("ID", "REASON"))
    ap.add_argument("--mark-placeholder", nargs=2, metavar=("ID", "REASON"))
    args = ap.parse_args(argv)
    marks = [(k, v) for k, v in (("unrecoverable", args.mark_unrecoverable), ("placeholder", args.mark_placeholder)) if v]
    if args.json and marks:
        ap.error("--json writes nothing, so it cannot be combined with --mark-*")
    if len(marks) > 1:
        ap.error("mark one contest at a time")
    for kind, (cid, reason) in marks:
        try:
            rec = mark(cid, kind, reason)
        except ValueError as exc:
            print(f"refused: {exc}")
            return 2
        print(f"marked {cid} {rec['kind']}: {rec['reason']}")
    report = build_report([Path(p) for p in args.also], args.url_template)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    paths = write_outputs(report, _today())
    n = report["counts"]
    print(f"awaiting {n['awaiting']} | filed {n['filed']} | settled {n['settled']} | dispositioned "
          f"{n['dispositioned']} | total {n['total']} | synthetic ids excluded {n['fixture']}")
    for loc, s in sorted(report["scan"].items()):
        print(f"  scanned {loc}: {s['files']} file(s), {s['parsed']} parsed, {s['skipped']} skipped")
    for s in report["unseen_slates"]:
        print(f"  UNSEEN slate {s['slate_id']} (cited in {', '.join(s['cited_in'])}): no entry file on this machine")
    for p in paths:
        print(f"wrote {_rel(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
