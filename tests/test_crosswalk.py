import csv
import json
from datetime import datetime, timezone

import pytest

from conftest import mini_pair
from nhl_dfs.contracts.ids import normalize_name
from nhl_dfs.data.identity import crosswalk as cw
from nhl_dfs.data.identity.crosswalk import NhlPerson
from nhl_dfs.intake.salary import read_salary
from pool_builder import make_pool, row

pytestmark = pytest.mark.c4

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
PETTERSSON_C, PETTERSSON_D = 8480012, 8483678
CALL_UP = "Drew O'Connor"  # absent from the directory below


def directory(pool):
    """NHL people for the mini pool: made-up ids except the two real Petterssons."""
    out, n = [], 9_000_000
    pos = {"C": "C", "LW": "L", "RW": "R", "D": "D", "G": "G"}
    for pr in pool.persons.values():
        r = pr.classic
        if r.name == CALL_UP:
            continue
        if r.name == "Elias Pettersson":
            out.append(NhlPerson(PETTERSSON_C if r.position == "C" else PETTERSSON_D, r.name, "VAN", r.position, "roster"))
            continue
        n += 1
        name = "Kasperi Kapannen" if r.name == "Kasperi Kapanen" else r.name  # NHL spelling differs
        out.append(NhlPerson(n, name, r.team, pos[r.position], "roster"))
    return out


@pytest.fixture
def pool():
    base = read_salary(mini_pair("classic")[0])
    fabricated = row("99999999", "EDM", "LW", 3000)
    fabricated = type(fabricated)(**{**fabricated.__dict__, "name": "Elias Pettersson",
                                     "person_key": "elias pettersson|EDM|F"})
    return make_pool(base.mode, list(base.rows) + [fabricated]), base


def seed(tmp_path, p, d):
    return cw.seed(p, directory=d, accepted_path=tmp_path / "accepted.csv",
                   proposals_path=tmp_path / "proposals.json", clock=lambda: T0)


def test_petterssons_accepted_by_group_fabricated_third_proposed(tmp_path, pool):
    p, base = pool
    res = seed(tmp_path, p, directory(base))
    got = {k: v for k, v in res.accepted.items() if k.startswith("elias pettersson")}
    assert got == {"elias pettersson|VAN|F": PETTERSSON_C, "elias pettersson|VAN|D": PETTERSSON_D}
    third = [x for x in res.proposals if x.dk_team == "EDM" and normalize_name(x.dk_name) == "elias pettersson"]
    assert {x.nhl_id for x in third} == {PETTERSSON_C, PETTERSSON_D}
    assert all("team differs" in x.reason for x in third)
    assert "elias pettersson|EDM|F" not in res.accepted


def test_call_up_stays_unmatched_and_flagged(tmp_path, pool):
    p, base = pool
    res = seed(tmp_path, p, directory(base))
    un = [u for u in res.unmatched if u["name"] == CALL_UP]
    assert len(un) == 1 and "no NHL identity" in un[0]["reason"]
    assert not any(k.startswith("drew o connor") for k in res.accepted)


def test_unverified_dk_team_never_auto_accepts(tmp_path, pool):
    p, base = pool
    assert "NYI" not in cw.verified_team_map()
    res = seed(tmp_path, p, directory(base))
    assert not any(k.endswith("|NYI|F") or k.endswith("|NYI|D") or k.endswith("|NYI|G") for k in res.accepted)
    nyi = [x for x in res.proposals if x.dk_team == "NYI"]
    assert nyi and all("no verified NHL mapping" in x.reason for x in nyi)


def test_fuzzy_name_is_a_proposal(tmp_path, pool):
    p, base = pool
    res = seed(tmp_path, p, directory(base))
    k = [x for x in res.proposals if x.dk_name == "Kasperi Kapanen"]
    assert len(k) == 1 and "similar name" in k[0].reason and k[0].nhl_name == "Kasperi Kapannen"


def test_accepted_csv_rows_carry_sha_and_accept_flow(tmp_path, pool):
    p, base = pool
    res = seed(tmp_path, p, directory(base))
    rows = list(csv.DictReader((tmp_path / "accepted.csv").open(encoding="utf-8")))
    assert list(rows[0].keys()) == cw.ACCEPTED_HEADER
    assert len(rows) == res.new_exact == len(res.accepted)
    r = next(x for x in rows if x["nhl_id"] == str(PETTERSSON_D))
    assert r["key_sha256"] == cw.key_sha("Elias Pettersson", "VAN", "D") and r["method"] == "exact"
    # proposal ids are stable across reruns, so --accept works after a reseed
    again = seed(tmp_path, p, directory(base))
    assert {x.proposal_id for x in again.proposals} == {x.proposal_id for x in res.proposals}
    assert again.new_exact == 0  # exact matches are read back, not duplicated
    kap = next(x for x in again.proposals if x.dk_name == "Kasperi Kapanen")
    with pytest.raises(ValueError):
        cw.accept(kap.proposal_id, 1, accepted_path=tmp_path / "accepted.csv", proposals_path=tmp_path / "proposals.json")
    cw.accept(kap.proposal_id, kap.nhl_id, accepted_path=tmp_path / "accepted.csv",
              proposals_path=tmp_path / "proposals.json", clock=lambda: T0)
    assert cw.accepted(tmp_path / "accepted.csv")[kap.key_sha256] == kap.nhl_id
    with pytest.raises(ValueError):
        cw.accept(kap.proposal_id, accepted_path=tmp_path / "accepted.csv", proposals_path=tmp_path / "proposals.json")
    third = seed(tmp_path, p, directory(base))
    assert third.accepted["kasperi kapanen|EDM|F"] == kap.nhl_id
    with pytest.raises(KeyError):
        cw.accept("nope", accepted_path=tmp_path / "accepted.csv", proposals_path=tmp_path / "proposals.json")


def test_nhl_positions_l_r_map_to_forward():
    assert NhlPerson(1, "a", "VAN", "L", "roster").group == "F"
    assert NhlPerson(1, "a", "VAN", "R", "roster").group == "F"
    assert NhlPerson(1, "a", "VAN", "D", "roster").group == "D"


def test_committed_accepted_csv_is_header_only_or_generated():
    from nhl_dfs.data.identity.crosswalk import ACCEPTED_CSV

    head = ACCEPTED_CSV.read_bytes().decode("utf-8").splitlines()[0]
    assert head.split(",") == cw.ACCEPTED_HEADER


def test_roster_directory_from_fixture(make_cache):
    from conftest import fixture_bytes

    cache, _ = make_cache([("roster/VGK/current", 200, fixture_bytes("nhl_roster_VGK.json"))])
    people, problems = cw.directory_from_rosters(["VGK"], cache=cache)
    assert people and not problems and {p.team for p in people} == {"VGK"}
    assert json.dumps([p.position for p in people])


def test_cli_identity_seed_and_accept(tmp_path, capsys):
    from nhl_dfs import cli
    from test_nhl_reports import SEASON, run

    run(tmp_path, moneypuck=False)  # store with the 8 fixture skaters (DAL / WPG) and 2 goalies
    salary = mini_pair("classic")[0]
    args = ["identity", "--seed", "--salary", str(salary), "--offline", "--store-root", str(tmp_path / "a" / "store"),
            "--accepted-path", str(tmp_path / "acc.csv"), "--proposals-path", str(tmp_path / "prop.json")]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "identity seed: accepted=0 proposals=0 unmatched=" in out and "directory 10 NHL people" in out
    assert cli.main(["identity", "--accept", "nope", "--accepted-path", str(tmp_path / "acc.csv"),
                     "--proposals-path", str(tmp_path / "prop.json")]) == 1
    assert cli.main(["identity"]) == 2


def test_cli_history_prints_counts_and_credit(monkeypatch, capsys):
    from nhl_dfs import cli
    from nhl_dfs.data.history import nhl_reports

    seen = {}

    def fake_backfill(seasons, **kw):
        seen["seasons"], seen["moneypuck"] = seasons, kw["moneypuck"]
        s = nhl_reports.BackfillStats(seasons=list(seasons), windows=3, calls=12)
        s.rows = {"skater_games/20252026": 8}
        s.tiers = {"20252026": {"A": 8, "B": 0}}
        s.crosscheck = {"2025020017": {"checked": 10, "mismatch": 0}}
        return s

    monkeypatch.setattr(nhl_reports, "backfill", fake_backfill)
    assert cli.main(["history", "--backfill", "2", "--store-root", "none-here"]) == 0
    out = capsys.readouterr().out
    assert "credit: " in out and "MoneyPuck.com" in out and "rows skater_games/20252026: 8" in out
    assert len(seen["seasons"]) >= 2 and seen["moneypuck"] is True
    assert cli.main(["history"]) == 2


def test_current_roster_beats_history_team(tmp_path):
    p = make_pool(read_salary(mini_pair("classic")[0]).mode, [row("1", "EDM", "G", 7000)])
    r = p.rows[0]
    moved = [NhlPerson(8475883, r.name, "EDM", "G", "roster"), NhlPerson(8475883, r.name, "CAR", "G", "history")]
    for order in (moved, moved[::-1]):
        res = cw.seed(p, directory=order, accepted_path=tmp_path / f"a{len(order)}{order[0].source}.csv",
                      proposals_path=tmp_path / "p.json", clock=lambda: T0)
        assert res.accepted == {r.person_key: 8475883} and not res.proposals
