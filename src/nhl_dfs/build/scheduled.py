"""Scheduled pre-lock refresh (backlog B23): the dispatcher a Task Scheduler job runs every few minutes.

For each slate with a delivered, non-rehearsal run whose first lock (earliest game start) is ahead and within
`horizon_h`, the newest such run is refreshed once per window (T-60 and T-20 by default; config/
scheduled_refresh.yaml). A machine that wakes late runs the latest due window once and marks the earlier ones
done. Inside T-`late_cutoff_min` nothing runs: near lock the terminal late swap is the path.

Ben is notified (a Windows toast, and always a line in runs/_scheduler/notifications.log) when: the file changed,
the goalie gate still sees a goalie known not to start or a conflict, goalie news is unavailable at the last
window, or the refresh failed. "Nothing changed" goes to the log only. Refresh uses the LAST DELIVERED file as its
base (an assumed parent): if Ben edited entries on DraftKings, the late swap with his current export is the right
command, and the notification says so. Upload stays Ben's. The dispatcher never registers anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "scheduled_refresh.yaml"
NOTIFY_PS1 = REPO_ROOT / "tools" / "notify.ps1"


def load_config(path: Path = CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Candidate:
    slate_id: str
    run_id: str
    mode: str
    first_start_utc: datetime
    created_utc: str


def candidates(runs_root: Path, now: datetime, horizon_h: float) -> list[Candidate]:
    """The newest delivered, non-rehearsal run of each slate whose first lock is ahead and within the horizon."""
    from nhl_dfs.intake.salary import read_salary

    best: dict[str, Candidate] = {}
    starts: dict[str, datetime] = {}
    for d in sorted(runs_root.iterdir()) if runs_root.is_dir() else []:
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("rehearsal_clock") or not any(v.get("public_replaced") for v in m.get("versions") or []):
            continue
        slate = m.get("slate_id")
        if slate not in starts:
            try:
                pool = read_salary(d / "inputs" / "DKSalaries.csv")
                starts[slate] = min(g.start_utc for g in pool.games.values())
            except Exception:
                continue
        first = starts[slate]
        if not (now < first <= now + timedelta(hours=horizon_h)):
            continue
        c = Candidate(slate, d.name, m.get("mode", "?"), first, m.get("created_utc") or "")
        if slate not in best or c.created_utc > best[slate].created_utc:
            best[slate] = c
    return sorted(best.values(), key=lambda c: (c.first_start_utc, c.slate_id))


def due_window(c: Candidate, now: datetime, cfg: dict, done: set[str]) -> tuple[int | None, list[int]]:
    """(the window to run now or None, the windows it also closes). Only the latest due window runs."""
    left_min = (c.first_start_utc - now).total_seconds() / 60.0
    if left_min <= float(cfg.get("late_cutoff_min", 5)):
        return None, []
    due = [int(w) for w in sorted(cfg["windows_min"], reverse=True) if left_min <= float(w)]
    todo = [w for w in due if f"{c.slate_id}|T-{w}" not in done]
    if not todo:
        return None, []
    return min(todo), todo  # the smallest due window (nearest lock) runs; larger ones it passed are closed with it


def toast(title: str, message: str) -> str:
    """A Windows toast through tools/notify.ps1 (WinRT; no module, no network). Returns 'sent' (the notifier
    accepted it; Focus Assist or notification settings can still hide it) or the error."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(NOTIFY_PS1),
                            "-Title", title, "-Message", message], capture_output=True, text=True, timeout=30,
                           creationflags=flags)
        return "sent" if r.returncode == 0 else f"toast failed: {(r.stderr or r.stdout).strip()[:200]}"
    except Exception as exc:
        return f"toast failed: {type(exc).__name__}"


def _message(c: Candidate, window: int, ev: dict) -> tuple[str, str] | None:
    """(title, body) when Ben must be told, else None."""
    head = f"{c.mode} {c.slate_id}, T-{window}"
    tail = (" Refresh used the last delivered file as its base: if you edited entries on DraftKings, run the late "
            "swap with your current export instead. Uploading is yours.")
    if ev["result"] == "failed":
        return f"NHL DFS: refresh FAILED ({c.mode})", f"{head}: {ev['detail'][:160]}. The previous file stands." + tail
    parts = []
    if ev.get("changed_cells"):
        parts.append(f"{ev['changed_cells']} cell(s) changed in {ev['changed_entries']} entr"
                     f"{'y' if ev['changed_entries'] == 1 else 'ies'}; upload {ev.get('published') or 'the new file'}")
    gate = ev.get("goalie_gate")
    if gate in ("NOT_STARTING", "CONFLICTED"):
        parts.append(f"GOALIE_GATE={gate}: " + "; ".join(ev.get("goalie_alerts", [])[:3]))
    if gate == "NO_NEWS" and ev.get("last_window"):
        parts.append("goalie news unavailable at the last scheduled refresh: confirm the goalies by hand")
    if not parts:
        return None
    title = "NHL DFS: file changed" if ev.get("changed_cells") else "NHL DFS: goalie check needed"
    return f"{title} ({c.mode})", f"{head}: " + "; ".join(parts) + "." + tail


def dispatch(*, now: datetime, runs_root: Path, outputs_root: Path | None = None, cfg: dict | None = None,
             refresh_fn: Callable | None = None, notify_fn: Callable[[str, str], str] | None = None,
             state_dir: Path | None = None, dry_run: bool = False) -> list[dict]:
    """Run every due window once; returns the events (also appended to runs/_scheduler/refresh_log.jsonl)."""
    cfg = cfg or load_config()
    state_dir = state_dir or runs_root / "_scheduler"
    state_path = state_dir / "state.json"
    done = set(json.loads(state_path.read_text(encoding="utf-8"))) if state_path.exists() else set()
    if refresh_fn is None:
        from nhl_dfs.build import refresh

        def refresh_fn(run_id):
            return refresh.run(run_id, offline=False, runs_root=runs_root, outputs_root=outputs_root)
    if notify_fn is None:
        notify_fn = toast if (cfg.get("notify") or {}).get("toast", True) else (lambda t, m: "toast disabled")
    events = []
    for c in candidates(runs_root, now, float(cfg.get("horizon_h", 18))):
        window, closes = due_window(c, now, cfg, done)
        if window is None:
            continue
        ev = {"at_utc": _iso(now), "slate_id": c.slate_id, "run_id": c.run_id, "mode": c.mode, "window": f"T-{window}",
              "first_lock_utc": _iso(c.first_start_utc), "closes": [f"T-{w}" for w in closes],
              "last_window": window == min(int(w) for w in cfg["windows_min"])}
        if dry_run:
            ev["result"] = "dry run: would refresh"
            events.append(ev)
            continue
        try:
            r = refresh_fn(c.run_id)
            m = r.manifest
            g = m.get("goalies") or {}
            ev.update({"result": "ok" if r.statuses.get("FILE_VALID") == "TRUE" else "failed",
                       "child_run": r.run.run_id, "statuses": dict(r.statuses),
                       "changed_cells": len(m.get("changed_cells") or []),
                       "changed_entries": len({x["entry_id"] for x in m.get("changed_cells") or []}),
                       "published": str(r.public_path) if r.public_path else None,
                       "goalie_gate": g.get("GOALIE_GATE"),
                       "goalie_alerts": [f"entry {x['entry_id']} {x['goalie']} {x['status']}"
                                         + (" (pinned)" if x.get("pinned") else "")
                                         for x in g.get("rows", []) if x["status"] in ("NOT STARTING", "CONFLICTED")],
                       "detail": "; ".join((m.get("failed") or [])[:2]) or "FILE_VALID not TRUE"})
        except Exception as exc:
            ev.update({"result": "failed", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
        msg = _message(c, window, ev)
        if msg is not None:
            ev["message"] = f"{msg[0]}: {msg[1]}"
            ev["notified"] = notify_fn(*msg)
            _append(state_dir / "notifications.log", f"{_iso(now)} {msg[0]}: {msg[1]}")
        elif (cfg.get("notify") or {}).get("on_no_change"):
            ev["notified"] = notify_fn(f"NHL DFS: no change ({c.mode})", f"{c.slate_id} T-{window}: nothing changed")
        done |= {f"{c.slate_id}|T-{w}" for w in closes}
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(done), indent=1), encoding="utf-8")
        _append(state_dir / "refresh_log.jsonl", json.dumps(ev, default=str))
        events.append(ev)
    return events


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Scheduled pre-lock refresh dispatcher (B23)")
    ap.add_argument("--once", action="store_true", help="check the windows once and exit (what the task runs)")
    ap.add_argument("--dry-run", action="store_true", help="list what is due; refresh nothing, notify nothing")
    ap.add_argument("--runs-root", default=os.environ.get("NHL_DFS_RUNS_ROOT") or str(REPO_ROOT / "runs"))
    ap.add_argument("--outputs-root", default=os.environ.get("NHL_DFS_OUTPUTS_ROOT") or None)
    ap.add_argument("--as-of", default=None, help="a labeled rehearsal clock (UTC); with it nothing is refreshed")
    ap.add_argument("--no-toast", action="store_true", help="no Windows toast (a Claude scheduled task relays NOTIFY)")
    args = ap.parse_args(argv)
    now = _dt(args.as_of) if args.as_of else datetime.now(timezone.utc)
    runs_root = Path(args.runs_root)
    outputs_root = Path(args.outputs_root) if args.outputs_root else runs_root.parent / "outputs"
    cfg = load_config()
    if args.no_toast:
        cfg.setdefault("notify", {})["toast"] = False
    events = dispatch(now=now, runs_root=runs_root, outputs_root=outputs_root, cfg=cfg,
                      dry_run=args.dry_run or bool(args.as_of))
    # Plain lines first (what a Claude scheduled task relays), then the full events.
    if not events:
        print(f"SCHEDULED_REFRESH=NOTHING_DUE at {_iso(now)}")
    for ev in events:
        print(f"SCHEDULED_REFRESH={ev.get('result', '?').upper().replace(' ', '_')} {ev['mode']} {ev['slate_id']} "
              f"{ev['window']} (first lock {ev['first_lock_utc']}): {ev.get('changed_cells', 0)} cell(s) changed; "
              f"GOALIE_GATE={ev.get('goalie_gate')}")
        if ev.get("message"):
            print(f"NOTIFY: {ev['message']}")
    for ev in events:
        print(json.dumps(ev, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
