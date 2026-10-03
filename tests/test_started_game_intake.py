"""Backlog B42: DraftKings replaces Game Info with "In-Progress" once a game has started. read_salary must accept the
file (it used to raise "unrecognized Game Info") and keep every started row out of the selectable pool."""
import csv
import io

import pytest

from conftest import TESTS
from nhl_dfs.intake.salary import read_salary

LS = TESTS / "fixtures" / "late_swap" / "classic" / "DKSalaries.csv"
STARTED_GAME = "AAA@BBB"


def _with_started_game(tmp_path, marker):
    records = list(csv.reader(io.StringIO(LS.read_bytes().decode("utf-8-sig"), newline="")))
    gi = records[0].index("Game Info")
    n = 0
    for rec in records[1:]:
        if rec and rec[gi].startswith(STARTED_GAME):
            rec[gi] = marker
            n += 1
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(records)
    path = tmp_path / "DKSalaries.csv"
    path.write_bytes(out.getvalue().encode("utf-8"))
    return path, n


@pytest.mark.parametrize("marker", ["In-Progress", "In Progress"])
def test_started_game_rows_leave_the_pool(tmp_path, marker):
    path, n = _with_started_game(tmp_path, marker)
    assert n > 0
    before = read_salary(LS)
    pool = read_salary(path)
    started_ids = {r.role_id for r in before.rows if r.game_info.startswith(STARTED_GAME)}
    assert started_ids and not (started_ids & set(pool.by_role_id))
    assert not any(r.role_id in started_ids for r in pool.rows)
    assert STARTED_GAME not in pool.games and set(pool.games) < set(before.games)
    (c,) = [c for c in pool.conflicts if c.kind == "STARTED_GAME"]
    assert c.excluded and set(c.role_ids) == started_ids
    assert pool.sha256 != before.sha256 and pool.raw == path.read_bytes()
