"""Test-only builders for synthetic SalaryPools (no CSV round trip)."""

from __future__ import annotations

import random

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, PoolRow, slot_accepts
from nhl_dfs.intake.salary import PersonRows, SalaryPool

_CLASSIC_ELIG = {"C": {"C", "UTIL"}, "LW": {"W", "UTIL"}, "RW": {"W", "UTIL"}, "D": {"D", "UTIL"}, "G": {"G"}}


def row(role_id, team, position, salary, *, roster=None, person=None, appg=5.0, flag="VALUE", mode=Mode.CLASSIC):
    """One PoolRow. `person` defaults to a unique key per role_id; pass it to share a person."""
    if roster is None:
        roster = _CLASSIC_ELIG[position] if mode is Mode.CLASSIC else {"FLEX"}
    group = "G" if position == "G" else ("D" if position == "D" else "F")
    return PoolRow(
        role_id=str(role_id),
        person_key=person or f"p{role_id}|{team}|{group}",
        name=f"Player {person or role_id}",
        team=team,
        position=position,
        roster_positions=frozenset(roster),
        salary=int(salary),
        game_info="",
        appg_raw=None if flag == "MISSING" else (0.0 if flag == "APPG_ZERO" else appg),
        appg_flag=flag,
    )


def sd_person(pid, team, position, flex_salary, *, appg=5.0, flag="VALUE"):
    """A Showdown person as (CPT row, FLEX row); CPT salary is 1.5x FLEX."""
    group = "G" if position == "G" else ("D" if position == "D" else "F")
    key = f"s{pid}|{team}|{group}"
    cpt = row(f"{pid}c", team, position, round(flex_salary * 1.5), roster={"CPT"}, person=key,
              appg=appg, flag=flag, mode=Mode.SHOWDOWN)
    flex = row(f"{pid}f", team, position, flex_salary, roster={"FLEX"}, person=key,
               appg=appg, flag=flag, mode=Mode.SHOWDOWN)
    return cpt, flex


def make_pool(mode: Mode, rows: list[PoolRow]) -> SalaryPool:
    persons: dict[str, PersonRows] = {}
    for r in rows:
        cur = persons.get(r.person_key, PersonRows())
        if mode is Mode.CLASSIC:
            persons[r.person_key] = PersonRows(classic=r)
        elif "CPT" in r.roster_positions:
            persons[r.person_key] = PersonRows(cpt=r, flex=cur.flex)
        else:
            persons[r.person_key] = PersonRows(cpt=cur.cpt, flex=r)
    return SalaryPool(
        mode=mode,
        rows=list(rows),
        by_role_id={r.role_id: r for r in rows},
        persons=persons,
        teams=frozenset(r.team for r in rows),
        games={},
        conflicts=[],
        sha256="synthetic",
        raw=b"",
    )


def classic_pool(teams=("AAA", "BBB", "CCC"), per_team=None, salary=4000):
    """A roomy legal Classic pool: per team 3 C, 4 W, 3 D, 1 G at a flat salary."""
    per_team = per_team or {"C": 3, "LW": 2, "RW": 2, "D": 3, "G": 1}
    rows, n = [], 0
    for t in teams:
        for pos, k in per_team.items():
            for _ in range(k):
                n += 1
                rows.append(row(n, t, pos, salary))
    return make_pool(Mode.CLASSIC, rows)


def showdown_pool(teams=("AAA", "BBB"), per_team=6, salary=6000):
    rows, n = [], 0
    for t in teams:
        for i in range(per_team):
            n += 1
            rows.extend(sd_person(n, t, "G" if i == 0 else "C", salary))
    return make_pool(Mode.SHOWDOWN, rows)


def random_pool(rng: random.Random, mode: Mode) -> SalaryPool:
    """Small random pool with tight caps, 2 to 4 teams, odd eligibilities and shared persons."""
    teams = [f"T{i}" for i in range(rng.randint(2, 4))]
    rows: list[PoolRow] = []
    if mode is Mode.CLASSIC:
        n = rng.randint(12, 22)
        for i in range(n):
            pos = rng.choice(["C", "LW", "RW", "D", "D", "G", "C", "LW"])
            roster = set(_CLASSIC_ELIG[pos])
            if rng.random() < 0.15 and pos != "G":
                roster = {rng.choice(["C", "W", "D"])} | ({"UTIL"} if rng.random() < 0.5 else set())
            rows.append(row(i, rng.choice(teams), pos, rng.randrange(2500, 9001, 100), roster=roster))
        # a shared person: a second row for someone already in the pool
        if rng.random() < 0.4:
            src = rng.choice(rows)
            if not src.is_goalie:
                rows.append(row(f"{src.role_id}x", src.team, src.position, rng.randrange(2500, 9001, 100),
                                roster={"UTIL", rng.choice(["C", "W", "D"])}, person=src.person_key))
    else:
        n = rng.randint(6, 11)
        for i in range(n):
            pos = rng.choice(["C", "LW", "D", "G"])
            cpt, flex = sd_person(i, rng.choice(teams[:2]) if rng.random() < 0.8 else rng.choice(teams),
                                  pos, rng.randrange(2000, 13001, 100))
            if rng.random() < 0.1:
                rows.append(flex)  # unpaired FLEX-only person
            else:
                rows.extend([cpt, flex])
    return make_pool(mode, rows)


def random_locks(rng: random.Random, pool: SalaryPool) -> dict[int, str]:
    """Zero to two locks, each a row the slot accepts (a lock may still make the pool infeasible)."""
    slots = CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    locks: dict[int, str] = {}
    for _ in range(rng.choice([0, 0, 1, 2])):
        i = rng.randrange(len(slots))
        ok = [r for r in pool.rows if slot_accepts(slots[i], r, pool.mode)]
        if ok:
            locks[i] = rng.choice(ok).role_id
    return locks


def clone_entries(src, dst, n: int, *, first_id: int = 9_100_000_000) -> None:
    """Write a DKEntries.csv with n entry rows, cloned from src's first entry.

    Each clone keeps the first entry's lead fields (contest, fee) and field count with a new
    Entry ID and blank roster and instruction cells; it is inserted after the real entries, so
    the header, instruction lines, and embedded player list stay byte-identical.
    """
    from pathlib import Path

    from nhl_dfs.intake.entries import physical_lines, read_entries

    ef = read_entries(src)
    bom, lines = physical_lines(ef.raw)
    first = ef.entries[0]
    body = first.line_bytes.rstrip(b"\r\n")
    ending = first.line_bytes[len(body):]
    lead = body.split(b",")[:4]  # Entry ID, Contest Name, Contest ID, Entry Fee (no quoted commas in DK lead fields)
    n_fields = len(ef.header)
    clones = []
    for k in range(n - len(ef.entries)):
        fields = [str(first_id + k).encode()] + lead[1:] + [b""] * (n_fields - 4)
        clones.append(b",".join(fields) + ending)
    last = ef.entries[-1].line_no
    out = lines[: last + 1] + clones + lines[last + 1:]
    Path(dst).write_bytes((b"\xef\xbb\xbf" if bom else b"") + b"".join(out))


def varied_pool(mode: Mode, *, seed: int = 0, teams=("AAA", "BBB", "CCC", "DDD")) -> SalaryPool:
    """A legal pool with spread salaries and APPG (C3 ownership and field tests).

    Classic: per team 3 C, 2 LW, 2 RW, 3 D, 2 G. Showdown: two teams, 8 people each (one G).
    """
    rng = random.Random(seed)
    rows: list[PoolRow] = []
    n = 0
    if mode is Mode.CLASSIC:
        for t in teams:
            for pos, k in (("C", 3), ("LW", 2), ("RW", 2), ("D", 3), ("G", 2)):
                for _ in range(k):
                    n += 1
                    sal = rng.randrange(2500, 9001, 100) if pos != "G" else rng.randrange(7000, 8601, 100)
                    rows.append(row(n, t, pos, sal, appg=round(sal / 1000 * rng.uniform(0.6, 1.3), 1)))
        return make_pool(Mode.CLASSIC, rows)
    for t in teams[:2]:
        for i in range(8):
            n += 1
            pos = "G" if i == 0 else ("D" if i < 3 else "C")
            sal = rng.randrange(2000, 12001, 200)
            rows.extend(sd_person(n, t, pos, sal, appg=round(sal / 1000 * rng.uniform(0.6, 1.3), 1)))
    return make_pool(Mode.SHOWDOWN, rows)
