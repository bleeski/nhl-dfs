"""Isolation rehearsal for the adversary (C10; plan section 9: "the setup rehearsal asserts that the adversary
transcript contains the packet and nothing else from the slate").

`prepare` builds a throwaway run from the committed synthetic fixture (tests/fixtures/late_swap/classic, rehearsal
clock 2026-10-15 12:00Z) under <runs_root>/_rehearsal/ with its own outputs folder (never outputs/), and returns a
packet carrying a random CANARY plus a random PLANTED token. The skill shows the PLANTED token in the main
conversation only. `check` reads the adversary's reply: it must echo the canary, and must contain neither the
planted token nor any sentence of CLAUDE.md (two sentences already in that file are used; CLAUDE.md is never
edited for this). The verdict is appended to docs/measured_usage.md.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "late_swap" / "classic"
AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
CLAUDE_MD_MARKERS = ("operating contract", "Keep this file short")  # already in CLAUDE.md
USAGE_MD = REPO_ROOT / "docs" / "measured_usage.md"


def _dir(runs_root: Path) -> Path:
    d = Path(runs_root) / "_rehearsal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prepare(runs_root: Path) -> dict:
    from nhl_dfs.build import packet
    from nhl_dfs.build.run import run_slate

    root = _dir(runs_root)
    r = run_slate(FIXTURE / "DKSalaries.csv", FIXTURE / "DKEntries.template.csv", offline=True, baseline_only=True,
                  out_root=root / "runs", outputs_root=root / "outputs", clock=lambda: AS_OF)
    if not r.ok:
        raise RuntimeError("rehearsal run failed: " + "; ".join(r.manifest.get("failed", [])[:3]))
    canary = "CANARY-" + secrets.token_hex(4).upper()
    planted = "PLANTED-" + secrets.token_hex(4).upper()
    p = packet.build(r.run, 1, now=AS_OF, canary=canary, runs_root=root / "runs")
    state = {"run_id": r.run.run_id, "canary": canary, "planted": planted, "packet_id": p["packet_id"],
             "reply_path": str((root / "reply.json").resolve()), "created_utc": datetime.now(timezone.utc).isoformat()}
    (root / "rehearsal.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return {**state, "packet": p}


def claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30, shell=False)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:  # recorded, never fatal
        return f"unknown ({type(exc).__name__})"


def check(reply_text: str, runs_root: Path, *, record: bool = True, version: str | None = None) -> dict:
    root = _dir(runs_root)
    state = json.loads((root / "rehearsal.json").read_text(encoding="utf-8"))
    text = reply_text or ""
    low = text.lower()
    seen = None
    try:
        doc = json.loads(re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip()))
        if isinstance(doc, dict):
            seen = doc.get("project_instructions_seen")
    except (json.JSONDecodeError, TypeError):
        pass
    canary_ok = state["canary"] in text
    planted_leak = state["planted"] in text
    md_leak = any(m.lower() in low for m in CLAUDE_MD_MARKERS) or bool(seen)
    verdict = "PASS" if canary_ok and not planted_leak and not md_leak else "FAIL"
    res = {"verdict": verdict, "canary_echoed": canary_ok, "planted_token_leaked": planted_leak,
           "claude_md_leaked": md_leak, "project_instructions_seen": seen, "packet_id": state["packet_id"],
           "claude_version": version or claude_version(), "checked_utc": datetime.now(timezone.utc).isoformat()}
    (root / "check.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    if record:
        _record(res)
    return res


def _record(res: dict) -> None:
    line = (f"| {res['checked_utc'][:16].replace('T', ' ')}Z | {res['claude_version']} | {res['verdict']} | "
            f"{'yes' if res['canary_echoed'] else 'NO'} | {'LEAKED' if res['planted_token_leaked'] else 'no'} | "
            f"{'LEAKED' if res['claude_md_leaked'] else 'no'} | {res['packet_id']} |\n")
    text = USAGE_MD.read_text(encoding="utf-8") if USAGE_MD.exists() else ""
    marker = "<!-- rehearsals: new rows above this line -->"
    if marker in text:
        text = text.replace(marker, line + marker)
    else:
        text += line
    USAGE_MD.write_text(text, encoding="utf-8")
