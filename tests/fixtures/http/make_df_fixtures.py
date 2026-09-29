"""Build the C7 Daily Faceoff fixtures from the 2026-09-29 raw capture (gitignored, data/raw/dailyfaceoff).

    python tests/fixtures/http/make_df_fixtures.py

Keeps each page's `__NEXT_DATA__` payload (the parse path) with the bulky and irrelevant fields dropped,
plus a few of the page's other script tags so the "script tags kept" shape is real. Recorded in SOURCES.md.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
OUT = Path(__file__).resolve().parent
NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
TEAM_URL = "https://www.dailyfaceoff.com/teams/vancouver-canucks/line-combinations"
GOALIE_URL = "https://www.dailyfaceoff.com/starting-goalies"


def raw_page(url: str) -> str:
    idx = json.loads((RAW / "dailyfaceoff" / "index.json").read_text(encoding="utf-8"))
    return (RAW / idx[url]["raw_path"]).read_text(encoding="utf-8")


def page(title: str, payload: dict, head_scripts: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return (f"<!DOCTYPE html><html><head><title>{title}</title></head><body>{head_scripts}"
            f'<script id="__NEXT_DATA__" type="application/json">{body}</script></body></html>\n')


SCRIPTS = ('<script id="didomi-cmp-1" data-nscript="beforeInteractive"></script>'
           '<script src="/_next/static/chunks/main-f9e8946682bd40bc.js" defer=""></script>'
           '<script src="/_next/static/chunks/webpack-ed141a2e0cf701d4.js" defer=""></script>')


def team_fixture() -> None:
    d = json.loads(NEXT.search(raw_page(TEAM_URL)).group(1))
    c = d["props"]["pageProps"]["combinations"]
    keep = ("playerId", "name", "positionIdentifier", "jerseyNumber", "groupIdentifier", "categoryIdentifier", "injuryStatus",
            "gameTimeDecision")
    players = []
    for p in c["players"]:
        q = {k: p[k] for k in keep}
        news = p.get("latestNews") or {}
        q["latestNews"] = {"createdAt": news.get("createdAt"), "details": (news.get("details") or "")[:100]}
        players.append(q)
    combos = {k: c[k] for k in ("teamId", "teamName", "teamAbbreviation", "teamSlug", "sourceName", "source", "updatedAt")}
    combos["players"] = players
    combos["lines"] = [{k: ln[k] for k in ("groupIdentifier", "groupName", "categoryIdentifier")} for ln in c["lines"]]
    payload = {"props": {"pageProps": {"slug": d["props"]["pageProps"]["slug"], "combinations": combos}}, "page": d["page"],
               "query": d["query"], "buildId": d["buildId"]}
    (OUT / "dailyfaceoff_team_vancouver-canucks.html").write_text(page("Lines (trimmed)", payload, SCRIPTS), encoding="utf-8", newline="\n")


def goalie_fixture() -> None:
    d = json.loads(NEXT.search(raw_page(GOALIE_URL)).group(1))
    pp = d["props"]["pageProps"]
    keep = ("TeamId", "TeamName", "TeamSlug", "GoalieId", "GoalieName", "NewsStrengthId", "NewsStrengthName", "NewsDetails",
            "NewsSourceName", "NewsSourceUrl", "NewsCreatedAt")
    rows = []
    for r in pp["data"]:
        q = {}
        for side in ("home", "away"):
            for k in keep:
                q[side + k] = r.get(side + k)
        for k in ("date", "time", "dateGmt", "pointSpread", "homeTeamMoneylinePointSpread", "awayTeamMoneylinePointSpread"):
            q[k] = r.get(k)
        rows.append(q)
    payload = {"props": {"pageProps": {"data": rows, "date": pp["date"], "specificDate": pp["specificDate"]}}, "page": d["page"],
               "query": d["query"], "buildId": d["buildId"]}
    ld = ('<script type="application/ld+json" data-next-head="">{"@type":"NewsArticle","headline":"NHL Starting Goalies - '
          'September 29, 2026"}</script>')
    (OUT / "dailyfaceoff_goalies.html").write_text(page("Starting Goalies (trimmed)", payload, ld + SCRIPTS), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    team_fixture()
    goalie_fixture()
    for f in sorted(OUT.glob("dailyfaceoff_*.html")):
        print(f.name, f.stat().st_size)
