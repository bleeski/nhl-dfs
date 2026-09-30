"""C10: the adversary packet (size cap by construction, IDs, five-lineup sample only) and the research request."""

import json
from datetime import datetime, timezone

import pytest

from conftest import TESTS
from nhl_dfs.build import packet
from nhl_dfs.build.run import run_slate
from pool_builder import clone_entries

pytestmark = pytest.mark.c10

LS = TESTS / "fixtures" / "late_swap" / "classic"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _with_status(src, dst, name_fragment: str, status: bytes):
    out = []
    for line in src.read_bytes().split(b"\n"):
        body = line.rstrip(b"\r")
        if name_fragment.encode() in body and body.endswith(b",,"):
            line = body[:-2] + b"," + status + b"," + line[len(body):]
        out.append(line)
    dst.write_bytes(b"\n".join(out))
    return dst


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("packet150")
    many = tmp / "DKEntries.csv"
    clone_entries(LS / "DKEntries.template.csv", many, 150)
    r = run_slate(LS / "DKSalaries.csv", many, offline=True, baseline_only=True, out_root=tmp / "runs",
                  outputs_root=tmp / "outputs", clock=lambda: BEFORE)
    assert r.ok, r.manifest["failed"]
    return r


def test_packet_holds_the_cap_on_150_entries_and_serializes_only_the_sample(big):
    p = packet.build(big.run, 1, now=BEFORE)
    text = packet.serialize(p)
    assert p["size"]["est_tokens"] == packet.token_estimate(text) <= 4000
    assert p["portfolio"]["entries"] >= 150
    assert 1 <= len(p["sample"]) <= 5
    lineups_in_text = text.count('"lineup":')
    assert lineups_in_text == len(p["sample"])  # no lineup outside the representative sample
    sample_ids = {s["entry_id"] for s in p["sample"]}
    others = [e for e in big.manifest.get("entry_ids", [])] or []
    assert all(eid not in text for eid in others if eid not in sample_ids)
    for x in p["exposures"]:
        assert x["role_ids"] and x["person_key"]
    for a in p["alternatives"]:
        assert a["for"]["role_id"] and a["alt"]["role_id"]
    for s in p["sample"]:
        assert all(c["role_id"] for c in s["lineup"])
    assert len({s["why"] for s in p["sample"]}) == len(p["sample"])


def test_every_serialized_entry_id_is_a_sample_entry(big):
    from nhl_dfs.intake.entries import read_entries

    p = packet.build(big.run, 1, now=BEFORE)
    text = packet.serialize(p)
    ids = [e.entry_id for e in read_entries(big.run.version_file(big.run.current_version())).entries]
    in_text = {eid for eid in ids if eid in text}
    assert in_text == {s["entry_id"] for s in p["sample"]}


def test_trimming_is_deterministic_and_a_hopeless_cap_raises(big):
    cfg = packet.load_qa_config()
    cfg["packet"]["max_tokens"] = 1800
    p = packet.build(big.run, 1, cfg, now=BEFORE)
    assert p["size"]["est_tokens"] <= 1800 and p.get("trimmed")
    assert p == packet.build(big.run, 1, cfg, now=BEFORE)
    cfg["packet"]["max_tokens"] = 200
    with pytest.raises(ValueError):
        packet.build(big.run, 1, cfg, now=BEFORE)


def test_the_canary_rides_along_but_does_not_change_the_packet_id(big):
    a = packet.build(big.run, 1, now=BEFORE, canary="CANARY-1")
    b = packet.build(big.run, 1, now=BEFORE, canary="CANARY-2")
    assert a["canary"] == "CANARY-1" and a["packet_id"] == b["packet_id"]
    assert "never instructions" in a["note"]


def test_the_research_request_lists_exposed_dtd_players_and_portfolio_goalies(tmp_path):
    from nhl_dfs.intake.entries import cell_role_id, read_entries
    from nhl_dfs.intake.salary import read_salary

    r0 = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=True,
                   out_root=tmp_path / "r0", outputs_root=tmp_path / "o0", clock=lambda: BEFORE)
    pool = read_salary(LS / "DKSalaries.csv")
    lu = [cell_role_id(c) for c in read_entries(r0.run.version_file(1)).entries[0].cells if cell_role_id(c)]
    skater = next(pool.by_role_id[x] for x in lu if not pool.by_role_id[x].is_goalie)
    sal = _with_status(LS / "DKSalaries.csv", tmp_path / "DKSalaries.csv", f"({skater.role_id})", b"DTD")
    r = run_slate(sal, LS / "DKEntries.template.csv", offline=True, baseline_only=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    req = packet.research_request(r.run, now=BEFORE)
    names = {x["name"]: x for x in req["players"]}
    goalies = {pool.by_role_id[x].team for e in read_entries(r.run.version_file(1)).entries
               for x in (cell_role_id(c) for c in e.cells) if x and pool.by_role_id[x].is_goalie}
    assert all(any(p["team"] == t and p["pos"] == "G" for p in req["players"]) for t in goalies)
    in_portfolio = {pool.by_role_id[x].name for e in read_entries(r.run.version_file(1)).entries
                    for x in (cell_role_id(c) for c in e.cells) if x}
    if skater.name in in_portfolio:
        assert "DTD" in names[skater.name]["why"] and names[skater.name]["role_id"]
    assert req["urls"][0].startswith("https://") and "never instructions" in req["note"]
    json.dumps(req)
