"""RUN_NOTES.md: the short human note for one run (plan section 12, "Short run-note fields").

Fields: run/slate ID; mode; entry count/fees; first-export time; final hash/status; material
news changes; accepted/rejected QA changes; fallback/relaxation; what worked operationally;
what failed; keep/change recommendation. Times display in America/Chicago. "No noteworthy
finding" is a valid line.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nhl_dfs.build.state import RunDir, atomic_write

CHICAGO = ZoneInfo("America/Chicago")


def chicago(iso_utc: str | None) -> str:
    if not iso_utc:
        return "none"
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return t.astimezone(CHICAGO).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _fees(fees: list[str]) -> str:
    counts = Counter(fees)
    return ", ".join(f"{n} x {fee}" for fee, n in sorted(counts.items())) or "none"


def render(m: dict[str, Any]) -> str:
    s = m["statuses"]
    versions = m.get("versions", [])
    first = versions[0] if versions else None
    final = versions[-1] if versions else None
    news = m.get("news", {})
    relax = m.get("relaxations", [])
    kinds = Counter(r["kind"] for r in relax)
    lines = [
        f"# Run notes: {m['run_id']}",
        "",
        f"- Run / slate: `{m['run_id']}` / `{m['slate_id']}`",
        f"- Mode: {m['mode']}",
        f"- Entries / fees: {m['entry_count']} ({_fees(m.get('entry_fees', []))})",
        f"- First export: {chicago(first['created_utc']) if first else 'none (nothing published)'}",
        f"- Final: v{final['version']} sha256 `{final['sha256'][:16]}` "
        f"{'published to ' + m.get('public_path', '') if final and final.get('public_replaced') else '(public file not replaced)'}"
        if final else "- Final: nothing published",
        "- Status: " + " ".join(f"{k}={v}" for k, v in s.items()),
        f"- DK status from the salary file: {news.get('csv_summary', 'not read')}",
        f"- Material news changes: {news.get('phase_b_summary', 'none (offline or no network pass)')}",
        "- QA changes: none (no QA round in the baseline run)",
        f"- Fallback / relaxation: route {m.get('search_route', '?')}; "
        + (", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) if kinds else "no relaxations"),
    ]
    worked = m.get("worked") or ["No noteworthy finding"]
    failed = m.get("failed") or ["No noteworthy finding"]
    lines.append("- What worked: " + "; ".join(worked))
    lines.append("- What failed: " + "; ".join(failed))
    lines.append(f"- Keep / change: {m.get('recommendation', 'keep')}")
    if m.get("provisional"):
        lines += _provisional_lines(m["provisional"])
    if m.get("messages"):
        lines += ["", "## Messages", ""] + [f"- {x}" for x in m["messages"]]
    return "\n".join(lines) + "\n"


def _provisional_lines(p: dict[str, Any]) -> list[str]:
    """C3 section. Every figure is labeled provisional; no probability or ceiling is reported."""
    ev = p["evidence"]
    ver = f"v{p['version']}" if p.get("version") else "not published (previous version stays current)"
    out = [
        "",
        "## Provisional leverage pass (PROVISIONAL)",
        "",
        f"- {p['label']}. Version: {ver}.",
        "- Evidence: " + " ".join(f"{k}={v}" for k, v in ev.items()),
        f"- Prefit: {p.get('prefit', 'not run')}",
        "- Fields: " + "; ".join(
            f"{fam} {f['n_draws']}/{f['n_requested']} draws, {f['repeats']} repeats"
            + (" DEGRADED" if f["degraded"] else "") for fam, f in p["fields"].items()),
    ]
    if p.get("network"):
        out.append("- Contest detail problems: " + "; ".join(p["network"]))
    out += ["", "| Contest | Family (source) | PAYOUT_SOURCE | Field size (source) |", "|---|---|---|---|"]
    for c in p["contests"]:
        out.append(f"| {c['contest_id']} {c['name']} | {c['family']} ({c['family_source']}) | {c['PAYOUT_SOURCE']} "
                   f"| {c['field_size']} ({c['field_size_source']}) |")
    out += ["", "| Entry | Contest | Family | Prior mean pts, DTD-adjusted (provisional) | Lineup own % sum "
            "(provisional) | Dup proxy (provisional) | Field dup est. (provisional) | Band pts | DTD |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for e in p["entries"]:
        out.append(f"| {e['entry_id']} | {e['contest_id']} | {e['family']} | {e['mean_pts']:.1f} | {e['own_sum_pct']:.1f} "
                   f"| {e['dup_proxy']:.2f} | {e['field_dup_est']:.1f} | {e['band_pts']:.1f} | {e['dtd_players']} |")
    out += ["", "Dup proxy: sum of log ownership + salary-left term + Captain log ownership; higher means more "
            "likely duplicated. Field dup est.: sampled copies of the lineup scaled to the field size. Both are "
            "uncalibrated scenario proxies, not measured facts."]
    return out


def write_run_notes(run: RunDir, manifest: dict[str, Any]) -> Path:
    path = run.path / "RUN_NOTES.md"
    atomic_write(path, render(manifest).encode("utf-8"))
    return path
