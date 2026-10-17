"""C56: the minimized 2026-10-17 Classic slate (B140, B141), cut from the real DraftKings files.

Run once by hand: python tests/fixtures/c56/make_fixture.py [<DKSalaries csv> <DKEntries csv>]
Reads (never committed; default tests/fixtures/real/DKSalaries_20261017.csv and DKEntries_20261017.csv, which are gitignored)
the two real files and the lineups Ben entered, data/entered/classic-20261017-2529349813.csv. Writes beside this file:
  DKSalaries.csv         the pool cut to the players below (BOM and CRLF as DraftKings writes them)
  DKEntries.template.csv the blank entries template with its embedded player list cut to exactly the same players
  DKEntries.v3.csv       the 13 entered lineups in that template: the engine's v3 file on the cut pool
  SOURCES.txt            the sha256 of every input and the sizes of every output
Kept: every player in the 13 entered lineups, every goalie, and the top few skaters per team and position so the
engine baseline can solve. No salary, team, position or id is invented: every row is a row of the real file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
ENTERED = ROOT / "data" / "entered" / "classic-20261017-2529349813.csv"
DEPTH = {"C": 2, "W": 3, "D": 2}  # extra skaters kept per team and position beyond the entered lineups


def _rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def _emit(rows: list[list[str]], bom: bool) -> bytes:
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + out.getvalue().encode("utf-8")


def main(argv: list[str]) -> int:
    sal_p = Path(argv[0]) if len(argv) > 0 else REAL / "DKSalaries_20261017.csv"
    ent_p = Path(argv[1]) if len(argv) > 1 else REAL / "DKEntries_20261017.csv"
    for p in (sal_p, ent_p, ENTERED):
        if not p.exists():
            print(f"missing {p}")
            return 1
    sal = _rows(sal_p)
    hdr, body = sal[0], sal[1:]
    col = {name: i for i, name in enumerate(hdr)}
    entered = _rows(ENTERED)
    ent_hdr, ent_rows = entered[0], entered[1:]

    def pid(cell: str) -> str:
        return cell.rsplit("(", 1)[1].rstrip(") ")

    lineup_ids = {pid(c) for r in ent_rows for c in r[4:13]}
    keep = set(lineup_ids)
    by_team: dict[tuple[str, str], list[list[str]]] = {}
    for r in body:
        pos = r[col["Roster Position"]].split("/")[0]  # C, W, D or G
        if pos == "G":
            keep.add(r[col["ID"]])
            continue
        by_team.setdefault((r[col["TeamAbbrev"]], pos), []).append(r)
    for (team, pos), rows in by_team.items():
        extra = 0
        for r in sorted(rows, key=lambda r: -int(r[col["Salary"]])):
            if r[col["ID"]] in keep:
                continue
            if extra < DEPTH[pos]:
                keep.add(r[col["ID"]])
                extra += 1
    missing = lineup_ids - {r[col["ID"]] for r in body}
    assert not missing, f"entered ids not in the salary file: {sorted(missing)}"

    kept_sal = [r for r in body if r[col["ID"]] in keep]
    (HERE / "DKSalaries.csv").write_bytes(_emit([hdr] + kept_sal, bom=True))

    ent = _rows(ent_p)
    emb_hdr_at = next(i for i, r in enumerate(ent) if len(r) >= 23 and r[14] == "Position")
    emb_id_col = 14 + 3  # Position, Name + ID, Name, ID
    embedded = [r[14:23] for r in ent[emb_hdr_at + 1:] if len(r) >= 23 and r[emb_id_col] in keep]
    assert {r[3] for r in embedded} == {r[col["ID"]] for r in kept_sal}, "embedded list and salary rows differ"
    left = [r[:14] for r in ent[:14]]  # header and the 13 entry rows, cols A to N
    right = [[r[14]] if len(r) > 14 and i < emb_hdr_at else [] for i, r in enumerate(ent[:emb_hdr_at])]
    right.append(ent[emb_hdr_at][14:23])
    right += embedded
    n = max(len(left), len(right))
    left += [[""] * 14 for _ in range(n - len(left))]
    right += [[] for _ in range(n - len(right))]
    template = [l + r for l, r in zip(left, right)]
    # an entry row keeps its own trailing fields only when it has a right-hand part; DraftKings pads blank rows to width 23
    (HERE / "DKEntries.template.csv").write_bytes(_emit(template, bom=False))

    filled = [list(r) for r in template]
    by_entry = {r[0]: r[4:13] for r in ent_rows}
    for row in filled[1:14]:
        row[4:13] = by_entry[row[0]]
    (HERE / "DKEntries.v3.csv").write_bytes(_emit(filled, bom=False))

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    lines = [
        "C56 minimized 2026-10-17 fixture (made by make_fixture.py; inputs are not committed)",
        f"input {sal_p.name} sha256 {sha(sal_p)} ({len(body)} players)",
        f"input {ent_p.name} sha256 {sha(ent_p)} ({len(ent_rows)} entries)",
        f"input data/entered/classic-20261017-2529349813.csv sha256 {sha(ENTERED)}",
        f"kept {len(kept_sal)} players: {len(lineup_ids)} in the entered lineups, every goalie, up to {DEPTH} more skaters per team",
    ]
    for name in ("DKSalaries.csv", "DKEntries.template.csv", "DKEntries.v3.csv"):
        lines.append(f"output {name} {(HERE / name).stat().st_size} bytes sha256 {sha(HERE / name)[:16]}")
    (HERE / "SOURCES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
