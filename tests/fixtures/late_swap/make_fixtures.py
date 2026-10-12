"""Generate the synthetic late-swap fixtures (C2c). Run from the repo root:

    .venv\\Scripts\\python.exe tests\\fixtures\\late_swap\\make_fixtures.py

Writes, with CRLF line endings and a UTF-8 BOM like DK exports:
  classic/DKSalaries.csv            3 games on 10/15/2026 (EDT): AAA@BBB 7:00PM, CCC@DDD 8:00PM, EEE@FFF 10:00PM ET
  classic/DKEntries.template.csv    5 blank reservations (the parent baseline run's input)
  classic/DKEntries.current.csv     the "current DK export": 4 filled entries (mixed Name (ID) and bare-ID cells), 1 blank
  showdown2/DKSalaries.csv          2 games on 10/15/2026: AAA@BBB 7:00PM, CCC@DDD 10:00PM ET
  showdown2/DKEntries.template.csv  3 blank reservations
  showdown2/DKEntries.current.csv   2 filled entries, 1 blank
  postlock/DKSalaries.csv           the classic rows after game 1 (AAA@BBB) started: its Game Info reads In-Progress
  postlock/DKEntries.template.csv   8 blank reservations (the parent run's input)
  postlock/DKEntries.postlock.csv   DraftKings' export after game 1 started, in the 2026-10-11 format (C53, B118): every
                                    game-1 cell reads Name (ID) (LOCKED), the other cells Name (ID); entry 7300000001 holds
                                    Echo C1 (OUT) in an open cell; the embedded player list starts beside entry 7, its
                                    Name + ID carries the marker and its Game Info reads In Progress for game 1
Synthetic names and IDs; no real DK data. Every filled lineup is checked legal here.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nhl_dfs.contracts.geometry import Mode, check_lineup  # noqa: E402
from nhl_dfs.intake.salary import read_salary  # noqa: E402

CRLF = b"\r\n"
BOM = b"\xef\xbb\xbf"
SAL_HEADER = "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame,Status,Starting"
DATE = "10/15/2026"
CLASSIC_GAMES = [("AAA", "BBB", "07:00PM"), ("CCC", "DDD", "08:00PM"), ("EEE", "FFF", "10:00PM")]
SHOWDOWN_GAMES = [("AAA", "BBB", "07:00PM"), ("CCC", "DDD", "10:00PM")]
TEAM_WORD = {"AAA": "Alpha", "BBB": "Bravo", "CCC": "Charlie", "DDD": "Delta", "EEE": "Echo", "FFF": "Foxtrot"}


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BOM + CRLF.join(x.encode("utf-8") for x in lines) + CRLF)


def _game(team: str, games) -> str:
    for away, home, t in games:
        if team in (away, home):
            return f"{away}@{home} {DATE} {t} ET"
    raise KeyError(team)


# -- Classic ---------------------------------------------------------------------------------

CLASSIC_SHAPE = [("C", 7400), ("C", 5200), ("C", 3500), ("LW", 6000), ("LW", 3000), ("RW", 5600), ("RW", 3100),
                 ("D", 6200), ("D", 4200), ("D", 2800), ("G", 8200), ("G", 7400)]
CLASSIC_STATUS = {("EEE", "C", 1): "OUT", ("CCC", "LW", 1): "DTD"}


def classic_rows():
    rows, rid = [], 81000000
    for ti, (away, home, _) in enumerate(CLASSIC_GAMES):
        for team in (away, home):
            seen: dict[str, int] = {}
            for pos, sal in CLASSIC_SHAPE:
                seen[pos] = seen.get(pos, 0) + 1
                rid += 1
                n = seen[pos]
                name = f"{TEAM_WORD[team]} {pos}{n}"
                roster = {"C": "C/UTIL", "LW": "W/UTIL", "RW": "W/UTIL", "D": "D/UTIL", "G": "G"}[pos]
                price = sal + 100 * (ti % 2) - (100 if team == home else 0)  # DK salary: a synthetic player price
                appg = round(price / 650, 1)
                status = CLASSIC_STATUS.get((team, pos, n), "")
                rows.append((pos, name, str(rid), roster, price, _game(team, CLASSIC_GAMES), team, appg, status))
    return rows


def _pool_lines(rows):
    return [SAL_HEADER] + [f"{p},{n} ({i}),{n},{i},{rp},{s},{g},{t},{a},{st}," for p, n, i, rp, s, g, t, a, st in rows]


def _ids(rows):
    return {r[1]: r[2] for r in rows}


CLASSIC_LABELS = ["C", "C", "W", "W", "W", "D", "D", "G", "UTIL"]
# Canonical order C,C,W,W,W,D,D,UTIL,G; written to the template order C,C,W,W,W,D,D,G,UTIL.
CLASSIC_LINEUPS = {
    # every filled entry holds Alpha C1 (game 1) -> 4 of 5 entries, over the 3-entry cap
    "7100000001": ["Alpha C1", "Echo C1", "Alpha LW2", "Delta RW2", "Charlie LW2", "Bravo D3", "Foxtrot D3", "Echo C3", "Charlie G2"],
    "7100000002": ["Alpha C1", "Delta C2", "Bravo LW2", "Charlie RW2", "Foxtrot LW2", "Alpha D2", "Echo D3", "Foxtrot C3", "Delta G2"],
    "7100000003": ["Alpha C1", "Foxtrot C2", "Charlie LW1", "Echo RW2", "Bravo RW2", "Delta D2", "Charlie D3", "Echo C3", "Bravo G2"],
    "7100000004": ["Alpha C1", "Echo C2", "Delta LW2", "Foxtrot RW2", "Bravo LW1", "Charlie D1", "Foxtrot D1", "Delta C3", "Echo G1"],
}
BARE_ID_ENTRIES = {"7100000002"}  # DK accepts bare IDs; this export mixes both cell formats
CLASSIC_ENTRY_IDS = ["7100000001", "7100000002", "7100000003", "7100000004", "7100000005"]


def _entries_lines(labels, entry_ids, contest, contest_id, fee, cells_by_entry):
    lead = "Entry ID,Contest Name,Contest ID,Entry Fee"
    lines = [f"{lead},{','.join(labels)},,Instructions"]
    notes = ["1. Column A lists all of your contest entries for this draftgroup",
             "2. Your current lineup is listed next to each entry (blank for reservations)"]
    for k, eid in enumerate(entry_ids):
        cells = cells_by_entry.get(eid, [""] * len(labels))
        note = notes[k] if k < len(notes) else ""
        lines.append(f"{eid},{contest},{contest_id},{fee},{','.join(cells)},,{note}")
    return lines


def _to_template(canonical, mode):
    if mode is Mode.CLASSIC:  # canonical C,C,W,W,W,D,D,UTIL,G -> template ...,G,UTIL
        return canonical[:7] + [canonical[8], canonical[7]]
    return list(canonical)


def _check(pool, names_by_entry, ids):
    for eid, names in names_by_entry.items():
        rows = [pool.by_role_id[ids[n]] for n in names]
        res = check_lineup(rows, pool.mode)
        assert res.ok, (eid, res.reasons)


def make_classic():
    rows = classic_rows()
    out = HERE / "classic"
    _write(out / "DKSalaries.csv", _pool_lines(rows))
    ids = _ids(rows)
    pool = read_salary(out / "DKSalaries.csv")
    _check(pool, CLASSIC_LINEUPS, ids)
    cells = {}
    for eid, names in CLASSIC_LINEUPS.items():
        fmt = (lambda n: ids[n]) if eid in BARE_ID_ENTRIES else (lambda n: f"{n} ({ids[n]})")
        cells[eid] = _to_template([fmt(n) for n in names], Mode.CLASSIC)
    args = (CLASSIC_LABELS, CLASSIC_ENTRY_IDS, "NHL Synthetic Classic", "297000001", "$1")
    _write(out / "DKEntries.template.csv", _entries_lines(*args, {}))
    _write(out / "DKEntries.current.csv", _entries_lines(*args, cells))


# -- Showdown, two games -------------------------------------------------------------------

SHOWDOWN_SHAPE = [("G", 7800), ("C", 10200), ("C", 6400), ("LW", 5000), ("D", 3600)]
SHOWDOWN_STATUS = {("DDD", "C", 1): "OUT"}


def showdown_rows():
    rows, rid = [], 82000000
    for away, home, _ in SHOWDOWN_GAMES:
        for team in (away, home):
            seen: dict[str, int] = {}
            for pos, sal in SHOWDOWN_SHAPE:
                seen[pos] = seen.get(pos, 0) + 1
                n = seen[pos]
                name = f"{TEAM_WORD[team]} {pos}{n}"
                price = sal - (200 if team == home else 0)  # DK salary: a synthetic player price
                appg = round(price / 700, 1)
                status = SHOWDOWN_STATUS.get((team, pos, n), "")
                game = _game(team, SHOWDOWN_GAMES)
                rid += 1
                rows.append((pos, name, str(rid), "CPT", int(price * 1.5), game, team, appg, status))
                rid += 1
                rows.append((pos, name, str(rid), "FLEX", price, game, team, appg, status))
    return rows


SHOWDOWN_LABELS = ["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"]
# (name, "CPT"|"FLEX") per slot. 7200000001: CPT and three FLEX all AAA (game 1), two open FLEX from game 2.
SHOWDOWN_LINEUPS = {
    "7200000001": [("Alpha C1", "CPT"), ("Alpha C2", "FLEX"), ("Alpha LW1", "FLEX"), ("Alpha D1", "FLEX"),
                   ("Charlie C2", "FLEX"), ("Delta LW1", "FLEX")],
    "7200000002": [("Charlie C1", "CPT"), ("Delta C1", "FLEX"), ("Bravo C2", "FLEX"), ("Alpha G1", "FLEX"),
                   ("Delta D1", "FLEX"), ("Charlie D1", "FLEX")],
}
SHOWDOWN_ENTRY_IDS = ["7200000001", "7200000002", "7200000003"]


def make_showdown():
    rows = showdown_rows()
    out = HERE / "showdown2"
    _write(out / "DKSalaries.csv", _pool_lines(rows))
    ids = {(r[1], r[3]): r[2] for r in rows}
    pool = read_salary(out / "DKSalaries.csv")
    cells = {}
    for eid, slots in SHOWDOWN_LINEUPS.items():
        role_ids = [ids[s] for s in slots]
        res = check_lineup([pool.by_role_id[r] for r in role_ids], Mode.SHOWDOWN)
        assert res.ok, (eid, res.reasons)
        cells[eid] = [f"{name} ({ids[(name, slot)]})" for name, slot in slots]
    args = (SHOWDOWN_LABELS, SHOWDOWN_ENTRY_IDS, "NHL Synthetic Showdown (2 games)", "297000002", "$0.25")
    _write(out / "DKEntries.template.csv", _entries_lines(*args, {}))
    _write(out / "DKEntries.current.csv", _entries_lines(*args, cells))


# -- Classic after game 1 started: DraftKings' post-lock export (C53) -------------------------

POSTLOCK_LINEUPS = {
    # canonical C,C,W,W,W,D,D,UTIL,G; game-1 (AAA, BBB) players are the cells DraftKings marks LOCKED
    "7300000001": CLASSIC_LINEUPS["7100000001"],  # Echo C1 (OUT, game 3) in an open cell: the one repair
    "7300000002": CLASSIC_LINEUPS["7100000002"],
    "7300000003": CLASSIC_LINEUPS["7100000003"],
    "7300000004": CLASSIC_LINEUPS["7100000004"],
    "7300000005": ["Charlie C1", "Delta C2", "Echo LW1", "Foxtrot RW1", "Charlie RW2", "Delta D1", "Echo D2", "Foxtrot C3", "Delta G1"],
    "7300000006": ["Bravo C1", "Alpha C2", "Alpha LW1", "Bravo RW1", "Delta LW1", "Alpha D3", "Bravo D2", "Charlie C3", "Alpha G1"],
    "7300000007": ["Delta C1", "Echo C2", "Foxtrot LW1", "Echo RW1", "Alpha RW2", "Foxtrot D2", "Charlie D2", "Bravo C3", "Foxtrot G1"],
    "7300000008": ["Foxtrot C1", "Charlie C2", "Echo LW2", "Delta RW1", "Bravo LW2", "Echo D1", "Delta D3", "Alpha C3", "Echo G2"],
}
POSTLOCK_ENTRY_IDS = list(POSTLOCK_LINEUPS)
POSTLOCK_STARTED = {"AAA", "BBB"}
LOCKED = " (LOCKED)"  # DraftKings' suffix on a started cell, after "Name (ID)" (B118: "J.T. Miller (44471322) (LOCKED)")
EMBEDDED_HEADER = ["Position", "Name + ID", "Name", "ID", "Roster Position", "Salary", "Game Info", "TeamAbbrev",
                   "AvgPointsPerGame"]
EMBEDDED_FROM_ENTRY = 7  # the list's header sits beside the 7th entry, so entry lines carry list fields too


def make_postlock():
    rows = classic_rows()
    started = [r for r in rows if r[6] in POSTLOCK_STARTED]
    pool_rows = [r if r[6] not in POSTLOCK_STARTED else r[:5] + ("In-Progress",) + r[6:] for r in rows]
    out = HERE / "postlock"
    _write(out / "DKSalaries.csv", _pool_lines(pool_rows))
    ids = _ids(rows)
    _check(read_salary(HERE / "classic" / "DKSalaries.csv"), POSTLOCK_LINEUPS, ids)
    team_of = {r[1]: r[6] for r in rows}
    cells = {eid: _to_template([f"{n} ({ids[n]})" + (LOCKED if team_of[n] in POSTLOCK_STARTED else "") for n in names],
                               Mode.CLASSIC)
             for eid, names in POSTLOCK_LINEUPS.items()}
    args = (CLASSIC_LABELS, POSTLOCK_ENTRY_IDS, "NHL Synthetic Classic", "297000003", "$1")
    _write(out / "DKEntries.template.csv", _entries_lines(*args, {}))
    lines = _entries_lines(*args, cells)
    embedded = [",".join(EMBEDDED_HEADER)]
    for p, n, i, rp, s, g, t, a, _st in rows:
        live = t in POSTLOCK_STARTED
        embedded.append(f"{p},{n} ({i}){LOCKED if live else ''},{n},{i},{rp},{s},{'In Progress' if live else g},{t},{a}")
    first = EMBEDDED_FROM_ENTRY  # lines[0] is the header; lines[k] is entry k
    base = CLASSIC_LABELS.__len__() + 4 + 1  # the Instructions column
    for j, text in enumerate(embedded):
        k = first + j
        if k < len(lines):
            lines[k] = lines[k].rsplit(",", 1)[0] + "," + text  # replace the (blank) Instructions note
        else:
            lines.append("," * base + text)
    assert len(started) == 24
    _write(out / "DKEntries.postlock.csv", lines)


if __name__ == "__main__":
    make_classic()
    make_showdown()
    make_postlock()
    print("late_swap fixtures written")
