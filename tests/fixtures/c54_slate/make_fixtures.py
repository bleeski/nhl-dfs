"""C54 exit-check slate (synthetic except where said): two games of 2026-10-12, for the recorded RotoWire page.

    python tests/fixtures/c54_slate/make_fixtures.py

Real: the teams, dates and start times of FLA @ BUF (17:00Z, 1:00 PM ET) and VGK @ MIN (00:00Z on 10-13, 8:00 PM ET
on 10-12) from the NHL schedule of 2026-10-12, and the name Jacob Markstrom, the Florida goalie the recorded page
(tests/fixtures/http/rotowire_hockey_news.html, 15:05Z) says will defend the road net against the Sabres on Monday.
VGK and MIN have no item on that page. Synthetic: every other name ("FLA G2", "BUF C1", ...), every ID and price, the
contest and the entries. The Starting column is blank, so no DraftKings flag names a goalie.

  DKSalaries.csv            48 rows, 12 per team (3 C, 2 LW, 2 RW, 3 D, 2 G)
  DKEntries.template.csv    3 entries, blank (the run builds them)
  DKEntries.current.csv     the same 3 entries filled: 7200000001 holds Markstrom (G), 7200000002 holds his teammate
                            FLA G2 (G), 7200000003 holds MIN G1 (a control no source names)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("late_swap_fixtures", HERE.parent / "late_swap" / "make_fixtures.py")
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)

from nhl_dfs.contracts.geometry import Mode  # noqa: E402
from nhl_dfs.intake.salary import read_salary  # noqa: E402

DATE = "10/12/2026"
GAMES = [("FLA", "BUF", "01:00PM"), ("VGK", "MIN", "08:00PM")]
REAL_NAMES = {("FLA", "G", 1): "Jacob Markstrom"}
LINEUPS = {  # canonical order C, C, W, W, W, D, D, UTIL, G
    "7200000001": ["FLA C1", "BUF C2", "MIN LW1", "VGK RW1", "FLA LW2", "BUF D1", "MIN D2", "VGK C3", "Jacob Markstrom"],
    "7200000002": ["FLA C2", "BUF C1", "MIN LW2", "VGK RW2", "BUF LW1", "FLA D1", "MIN D1", "VGK C2", "FLA G2"],
    "7200000003": ["VGK C1", "MIN C2", "FLA RW1", "BUF RW2", "VGK LW1", "MIN D3", "BUF D3", "FLA C3", "MIN G1"],
}
ENTRY_IDS = list(LINEUPS)


def _game(team: str) -> str:
    for away, home, t in GAMES:
        if team in (away, home):
            return f"{away}@{home} {DATE} {t} ET"
    raise KeyError(team)


def rows():
    out, rid = [], 82000000
    for away, home, _ in GAMES:
        for team in (away, home):
            seen: dict[str, int] = {}
            for pos, base in ls.CLASSIC_SHAPE:
                seen[pos] = seen.get(pos, 0) + 1
                rid += 1
                n = seen[pos]
                name = REAL_NAMES.get((team, pos, n), f"{team} {pos}{n}")
                roster = {"C": "C/UTIL", "LW": "W/UTIL", "RW": "W/UTIL", "D": "D/UTIL", "G": "G"}[pos]
                price = base - (100 if team == home else 0)  # DK salary column: a synthetic player price
                out.append((pos, name, str(rid), roster, price, _game(team), team, round(price / 650, 1), ""))
    return out


def main() -> None:
    rs = rows()
    ls._write(HERE / "DKSalaries.csv", ls._pool_lines(rs))
    ids = ls._ids(rs)
    pool = read_salary(HERE / "DKSalaries.csv")
    ls._check(pool, LINEUPS, ids)
    cells = {eid: ls._to_template([f"{n} ({ids[n]})" for n in names], Mode.CLASSIC) for eid, names in LINEUPS.items()}
    args = (ls.CLASSIC_LABELS, ENTRY_IDS, "NHL Synthetic C54 Classic", "297000054", "$1")
    ls._write(HERE / "DKEntries.template.csv", ls._entries_lines(*args, {}))
    ls._write(HERE / "DKEntries.current.csv", ls._entries_lines(*args, cells))
    print("c54_slate fixtures written")


if __name__ == "__main__":
    main()
