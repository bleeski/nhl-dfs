"""Build the trimmed C11 standings fixtures from Ben's full exports (gitignored under data/standings/inbox/).

    .venv\\Scripts\\python.exe tests\\fixtures\\standings\\make_fixtures.py

At most 60 rows per file. The entry block and the ownership block are unrelated lists that share row positions,
so they are chosen separately and paired back up row by row:
- entry rows: the top ranks, our entries (EntryName starting with OURS), every entry tied with ours, a tie group,
  and blank lineups;
- ownership rows: every row of a player in a kept lineup (so kept lineups have their FPTS), then CPT/FLEX pairs and
  UTIL rows, up to the cap.
Other entrants' DraftKings usernames are personal data: every EntryName that is not ours is replaced by a
placeholder (the "(k/n)" multi-entry suffix is kept). Entry IDs, ranks, points and lineups are kept.
"""

import csv
import io
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INBOX = REPO / "data" / "standings" / "inbox" / "2026-09-29"
OUT = Path(__file__).resolve().parent
OURS = "bleeski"
CAP = 60
PICK = {"classic": "195958173", "showdown": "196048725"}


def build(contest: str) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO((INBOX / contest / f"contest-standings-{contest}.csv").read_bytes().decode("utf-8-sig"))))
    hdr, body = rows[0], rows[1:]
    o = hdr.index("Player")
    ents = [r[:o] for r in body if r[0].strip()]
    owns = [r[o:o + 4] for r in body if len(r) > o and r[o].strip()]
    ours = [e for e in ents if e[2].startswith(OURS)]
    our_ranks = {e[0] for e in ours}
    keep = ents[:12] + ours + [e for e in ents if e[0] in our_ranks]
    ranks = [e[0] for e in ents]
    tie = next((r for r in ranks[12:] if ranks.count(r) >= 3 and r not in our_ranks), None)
    keep += [e for e in ents if e[0] == tie][:4]
    keep += [e for e in ents if not e[5].strip()][:2]
    seen, kept = set(), []
    for e in keep:
        if e[1] not in seen:
            seen.add(e[1])
            kept.append(e)
    kept.sort(key=lambda e: (int(e[0]), e[1]))
    kept = kept[:CAP]
    names = {n.strip() for e in kept for n in re.split(r"\b(?:CPT|FLEX|UTIL|C|W|D|G) ", e[5]) if n.strip()}
    own_keep = [x for x in owns if x[0] in names]
    own_keep += [x for x in owns if x not in own_keep and x[1] in ("CPT", "UTIL")]
    own_keep = own_keep[:CAP]
    anon = 0
    out = [hdr]
    for i in range(max(len(kept), len(own_keep))):
        e = list(kept[i]) if i < len(kept) else [""] * o
        if e[0] and not e[2].startswith(OURS):
            anon += 1
            suffix = re.search(r"\s\(\d+/\d+\)$", e[2])
            e[2] = f"entrant{anon:04d}" + (suffix.group(0) if suffix else "")
        x = own_keep[i] if i < len(own_keep) else ["", "", "", ""]
        out.append(e + list(x))
    return out


def main() -> None:
    for mode, contest in PICK.items():
        rows = build(contest)
        d = OUT / mode
        d.mkdir(exist_ok=True)
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(rows)
        (d / f"contest-standings-{contest}.csv").write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
        print(f"{mode}: {len(rows) - 1} rows -> {d / f'contest-standings-{contest}.csv'}")


if __name__ == "__main__":
    main()
