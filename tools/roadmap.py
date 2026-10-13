"""Roadmap page for Ben: chunks.yaml + BUILD_STATUS.md + docs/roadmap/plain.yaml -> one self-contained HTML page.

    python tools/roadmap.py check            ROADMAP=OK, or ROADMAP=STALE with the entries to write (exit 1)
    python tools/roadmap.py build [--out P]  check, then write the page (default outputs/roadmap/roadmap.html)
    python tools/roadmap.py stamp <id> ...   after rereading a chunk's card, record its current title as reviewed

Rank, title, band, size and dependencies come from chunks.yaml; status and dates from the BUILD_STATUS.md tracker; the
next chunk from next_chunk.find_next(verify=False), the same preview rule as `--peek`. Only the plain-English text is
written by hand, in docs/roadmap/plain.yaml. The page is published as a claude.ai artifact (docs/roadmap/README.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_chunk as nc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAIN = Path("docs/roadmap/plain.yaml")
TEMPLATE = Path("docs/roadmap/page.html")
DEFAULT_OUT = Path("outputs/roadmap/roadmap.html")
DATA_MARK = "__ROADMAP_DATA__"

BAND_GOAL = {0: "legal", 1: "win", 2: "washout", 3: "other"}
GOALS = ("foundation", "legal", "win", "washout", "other")
TEXT_FIELDS = ("name", "what", "why", "matters", "result", "caveat", "waiting")
REQUIRED = ("name", "what", "why", "matters")
SIZE_WORDS = {"S": "Small job", "M": "Medium job", "L": "Large job"}


def load(root: Path) -> tuple[dict, dict, dict]:
    data = nc.load_data(root / "chunks.yaml")
    rows = nc.parse_rows(nc.load_tracker_lines(root / "BUILD_STATUS.md"))
    plain = yaml.safe_load((root / PLAIN).read_text(encoding="utf-8")) or {}
    return data, rows, plain


def _status(rows: dict, cid: str) -> str:
    row = rows.get(cid)
    return row.status if row else "NO_ROW"


def _texts(plain: dict):
    """Every hand-written string in plain.yaml, with a label for error messages."""
    for key in ("name", "blurb"):
        for gid, goal in (plain.get("goals") or {}).items():
            if goal.get(key):
                yield f"goals.{gid}.{key}", str(goal[key])
    for i, ask in enumerate(plain.get("asks") or []):
        for key in ("question", "why", "recommendation"):
            if ask.get(key):
                yield f"asks[{i}].{key}", str(ask[key])
    for i, up in enumerate(plain.get("updates") or []):
        yield f"updates[{i}].text", str(up.get("text", ""))
    for cid, entry in (plain.get("chunks") or {}).items():
        for key in TEXT_FIELDS:
            if entry and entry.get(key):
                yield f"{cid}.{key}", str(entry[key])


def check(root: Path = ROOT) -> list[str]:
    """Problems that make the page stale or wrong; empty means ROADMAP=OK."""
    data, rows, plain = load(root)
    entries = plain.get("chunks") or {}
    ids = [c["id"] for c in data["chunks"]]
    problems: list[str] = []
    for c in data["chunks"]:
        cid = c["id"]
        entry = entries.get(cid)
        if not entry:
            problems.append(f"{cid}: no entry in {PLAIN.as_posix()} (write name, what, why, matters from its card)")
            continue
        missing = [k for k in REQUIRED if not str(entry.get(k) or "").strip()]
        if missing:
            problems.append(f"{cid}: missing {', '.join(missing)}")
        if entry.get("title_seen") != c["title"]:
            problems.append(f"{cid}: title changed to {c['title']!r}; reread the card, update the entry, then "
                            f"`python tools/roadmap.py stamp {cid}`")
        if "band" not in c and entry.get("goal") not in GOALS:
            problems.append(f"{cid}: no band in chunks.yaml, so the entry needs goal: one of {', '.join(GOALS)}")
        if _status(rows, cid) == "DONE" and not str(entry.get("result") or "").strip():
            problems.append(f"{cid}: DONE in the tracker but has no result (say what happened in plain words)")
    for cid in entries:
        if cid not in ids:
            problems.append(f"{cid}: entry has no chunk in chunks.yaml (remove it or fix the id)")
    for i, ask in enumerate(plain.get("asks") or []):
        for cid in ask.get("chunks") or []:
            if cid not in ids:
                problems.append(f"asks[{i}] ({ask.get('id')}): unknown chunk {cid}")
    for label, text in _texts(plain):
        if "—" in text:
            problems.append(f"{label}: contains an em dash (style rule)")
    return problems


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _next_id(graph: dict, rows: dict, root: Path) -> str | None:
    first = nc.find_next(graph, rows, root, verify=False).splitlines()[0].strip()
    return first if first in graph else None


def gather(root: Path = ROOT) -> dict:
    """The page's data: everything the page shows, in one JSON-ready dict."""
    data, rows, plain = load(root)
    graph = {c["id"]: c for c in data["chunks"]}
    entries = plain.get("chunks") or {}
    nxt = _next_id(graph, rows, root)
    queued = [c["id"] for c in data["chunks"] if "band" in c and _status(rows, c["id"]) not in ("DONE", "GATED")]
    asked = {cid for ask in plain.get("asks") or [] for cid in ask.get("chunks") or []}
    chunks = []
    for order, c in enumerate(data["chunks"], 1):
        cid = c["id"]
        row = rows.get(cid)
        cells = [x.strip() for x in row.cells] if row else [""] * 8
        status = _status(rows, cid)
        entry = entries.get(cid) or {}
        if status == "DONE":
            state = "done"
        elif status == "IN_PROGRESS":
            state = "in_progress"
        elif cid == nxt:
            state = "next"
        elif status == "BLOCKED":
            state = "on_hold"
        elif status == "GATED":
            state = "gated"
        elif nc.deps_done(cid, graph, rows):
            state = "ready"
        else:
            state = "queued"
        chunks.append({
            "id": cid,
            "order": order,
            "rank": queued.index(cid) + 1 if cid in queued else None,
            "title": c["title"],
            "band": c.get("band"),
            "goal": BAND_GOAL.get(c.get("band"), entry.get("goal", "other")),
            "size": SIZE_WORDS.get(c.get("size", ""), ""),
            "experiment": "experiment" in c["title"].lower() or "measurement" in c["title"].lower(),
            "status": status,
            "state": state,
            "started": cells[3],
            "finished": cells[4],
            "depends": list(c.get("depends") or []),
            "asked": cid in asked,
            **{k: str(entry.get(k) or "").strip() for k in TEXT_FIELDS},
        })
    milestones = [{"name": str(c["milestone"]).split(":", 1)[-1].strip(), "after": c["id"],
                   "reached": next((ch["finished"] for ch in chunks if ch["id"] == c["id"] and ch["state"] == "done"), "")}
                  for c in data["chunks"] if c.get("milestone")]
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "commit": _git(root, "rev-parse", "--short", "HEAD"),
        "commit_date": _git(root, "log", "-1", "--format=%cs"),
        "next": nxt,
        "goals": plain.get("goals") or {},
        "asks": plain.get("asks") or [],
        "updates": plain.get("updates") or [],
        "milestones": milestones,
        "chunks": chunks,
    }


def render(page_data: dict, root: Path = ROOT) -> str:
    template = (root / TEMPLATE).read_text(encoding="utf-8")
    if DATA_MARK not in template:
        raise SystemExit(f"{TEMPLATE.as_posix()} has no {DATA_MARK} placeholder")
    blob = json.dumps(page_data, ensure_ascii=False, default=str).replace("</", "<\\/").replace("<!--", "<\\!--")
    return template.replace(DATA_MARK, blob)


def stamp(root: Path, ids: list[str]) -> list[str]:
    """Set title_seen to the current chunks.yaml title for each id, editing plain.yaml in place (comments kept)."""
    data, _, _ = load(root)
    titles = {c["id"]: c["title"] for c in data["chunks"]}
    path = root / PLAIN
    text = path.read_text(encoding="utf-8")
    done = []
    for cid in ids:
        if cid not in titles:
            raise SystemExit(f"unknown chunk {cid}")
        head = re.search(rf"^  {re.escape(cid)}:\s*$", text, flags=re.M)
        if not head:
            raise SystemExit(f"{cid}: no entry in {PLAIN.as_posix()}; write it first")
        line = f"    title_seen: {json.dumps(titles[cid], ensure_ascii=False)}"
        nxt = re.search(r"^  \S", text[head.end():], flags=re.M)
        end = head.end() + (nxt.start() if nxt else len(text) - head.end())
        block = text[head.end():end]
        if re.search(r"^    title_seen:.*$", block, flags=re.M):
            block = re.sub(r"^    title_seen:.*$", lambda _: line, block, count=1, flags=re.M)
        else:
            block = "\n" + line + block
        text = text[:head.end()] + block + text[end:]
        done.append(cid)
    path.write_text(text, encoding="utf-8")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=DEFAULT_OUT)
    s = sub.add_parser("stamp")
    s.add_argument("ids", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "stamp":
        print("STAMPED: " + ", ".join(stamp(ROOT, args.ids)))
        return 0
    problems = check(ROOT)
    if problems:
        print("ROADMAP=STALE")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    if args.cmd == "check":
        print("ROADMAP=OK")
        return 0
    page = render(gather(ROOT))
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(page.encode("utf-8"))
    print(f"ROADMAP=OK\nPAGE={out}\nPAGE_SHA256={hashlib.sha256(page.encode('utf-8')).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
