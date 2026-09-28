"""Prospective capture: one raw snapshot of every live source.

    python tools/capture.py --once

Writes data/raw/capture/<YYYY-MM-DD>/<HHMM>/ (UTC) with one file per fetched
body (hard-linked to the HttpCache copy, copied if linking fails) and
index.json listing each item's URL, fetch time, hash, and status. Nothing is
parsed beyond what locates the next URL; Daily Faceoff HTML is stored whole,
<script> tags included, for C7. A failing source is recorded and skipped; the
exit code is nonzero only for a local error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    import yaml
except ModuleNotFoundError:  # run under the repo venv when started with a bare system python
    import subprocess

    for _venv_py in (REPO_ROOT / ".venv" / "Scripts" / "python.exe", REPO_ROOT / ".venv" / "bin" / "python"):
        if _venv_py.exists():
            sys.exit(subprocess.run([str(_venv_py), *sys.argv]).returncode)
    sys.stderr.write("pyyaml is not importable and no .venv was found; run `uv sync` first.\n")
    sys.exit(1)
sys.path.insert(0, str(REPO_ROOT / "src"))

from nhl_dfs.data.http import HttpCache, SourceSchemaError, SourceUnavailable, load_sources_config  # noqa: E402
from nhl_dfs.data.sources.dk_public import parse_contest_detail, parse_draftables, parse_lobby  # noqa: E402
from nhl_dfs.data.sources.nhl import parse_partner_odds, parse_schedule  # noqa: E402


def _nonempty_html(page) -> None:
    if not isinstance(page, str) or "<html" not in page.lower():
        raise SourceSchemaError("not an HTML page")


def _link(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def run_once(cache: HttpCache, *, now: datetime | None = None, root: Path | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    root = Path(root) if root is not None else cache.root
    capture_cfg = yaml.safe_load((REPO_ROOT / "config" / "capture.yaml").read_text(encoding="utf-8"))
    teams = yaml.safe_load((REPO_ROOT / "config" / "teams.yaml").read_text(encoding="utf-8"))["teams"]
    slug_by_nhl = {t["nhl"]: t["df_slug"] for t in teams}
    cfg = load_sources_config()
    urls = cfg["urls"]
    snap = root / "capture" / now.strftime("%Y-%m-%d") / now.strftime("%H%M")
    snap.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []

    def grab(name: str, url: str, source: str, kind: str, schema):
        entry = {"name": name, "url": url, "source": source}
        try:
            getter = cache.get_json if kind == "json" else cache.get_text
            f = getter(url, source=source, ttl_s=0, schema=schema)
            entry.update(ok=True, fetched_at=f.fetched_at_utc.isoformat(), raw_hash=f.raw_hash,
                         file=f"{name}.{kind}", stored=_link(f.raw_path, snap / f"{name}.{kind}"))
            items.append(entry)
            return f.data
        except SourceSchemaError as exc:
            entry.update(ok=False, error=f"SourceSchemaError: {exc}")
            if exc.raw_path is not None:
                entry.update(file=f"{name}.{kind}", stored=_link(exc.raw_path, snap / f"{name}.{kind}"))
        except SourceUnavailable as exc:
            entry.update(ok=False, error=f"SourceUnavailable: {exc}")
        items.append(entry)
        return None

    lobby = grab("dk_lobby", urls["dk_lobby"], "dk_lobby", "json", parse_lobby)
    if lobby is not None:
        per_group: dict[int, list] = {}
        for c in parse_lobby(lobby):
            per_group.setdefault(c.draft_group_id, []).append(c)
        n = int(capture_cfg.get("contest_details_per_draft_group", 2))
        for dg, contests in sorted(per_group.items()):
            for c in sorted(contests, key=lambda c: (-c.field_size, c.id))[:n]:
                grab(f"dk_contest_{c.id}", urls["dk_contest"].format(contest_id=c.id), "dk_contest", "json",
                     parse_contest_detail)
            grab(f"dk_draftables_{dg}", urls["dk_draftables"].format(draft_group_id=dg), "dk_draftables", "json",
                 parse_draftables)

    today = now.astimezone(ZoneInfo(capture_cfg.get("timezone", "America/Chicago"))).date().isoformat()
    sched = grab(f"nhl_schedule_{today}", urls["nhl_schedule"].format(date=today), "nhl_schedule", "json",
                 parse_schedule)
    grab("nhl_partner_odds", urls["nhl_partner_odds"], "nhl_partner_odds", "json", parse_partner_odds)
    grab("covers_odds", urls["covers"], "covers", "html", _nonempty_html)
    grab("df_starting_goalies", urls["df_goalies"], "dailyfaceoff", "html", _nonempty_html)
    if sched is not None:
        slate = sorted({t for g in parse_schedule(sched, on_date=today) for t in (g.away, g.home)})
        for team in slate:
            slug = slug_by_nhl.get(team)
            if slug is None:
                items.append({"name": f"df_lines_{team}", "ok": False, "error": "team not in config/teams.yaml"})
                continue
            grab(f"df_lines_{slug}", urls["df_lines"].format(slug=slug), "dailyfaceoff", "html", _nonempty_html)

    index = {
        "captured_at_utc": now.isoformat(),
        "directory_time_basis": "UTC",
        "calls_made": cache.calls_made,
        "items": items,
    }
    (snap / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    return snap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args(argv)
    assert args.once
    snap = run_once(HttpCache(force_refresh=True))
    index = json.loads((snap / "index.json").read_text(encoding="utf-8"))
    ok = sum(1 for i in index["items"] if i.get("ok"))
    print(f"snapshot {snap.relative_to(REPO_ROOT)}: {ok}/{len(index['items'])} items ok, {index['calls_made']} calls")
    for item in index["items"]:
        if not item.get("ok"):
            print(f"  failed: {item['name']}: {item.get('error', '')[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
