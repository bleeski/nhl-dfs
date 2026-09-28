import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from conftest import HTTP, fixture_bytes, real_pair

from nhl_dfs.contracts.statuses import Eligibility, ObsStatus, Participation
from nhl_dfs.data.http import SourceSchemaError, SourceUnavailable
from nhl_dfs.data.sources import dk_public
from nhl_dfs.intake.salary import read_salary

pytestmark = pytest.mark.c1

DRAFTABLES_FIXTURE = "dk_draftables_153983.json"


def _json(name):
    return json.loads(fixture_bytes(name))


def _draftables_payload() -> dict:
    path = HTTP / DRAFTABLES_FIXTURE
    assert path.exists(), (
        f"recorded draftables response missing: tests/fixtures/http/{DRAFTABLES_FIXTURE}. "
        "The endpoint returns HTTP 403 from this machine's script; Ben saves it from a browser (see BUILD_STATUS C1 notes)."
    )
    return json.loads(path.read_bytes())


# --- lobby and contest detail -------------------------------------------------------------------


def test_lobby_parses_contests():
    contests = {c.id: c for c in dk_public.parse_lobby(_json("dk_lobby.json"))}
    sd = contests[196048725]
    assert sd.name == "NHL Showdown $150 Quarter Jukebox (CHI @ VGK)"
    assert sd.draft_group_id == 153983 and sd.game_type == "Showdown"
    assert isinstance(sd.fee, Decimal) and isinstance(sd.prize_pool, Decimal)
    assert sd.start_utc == datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)
    assert contests[195958173].game_type == "Classic"


def test_lobby_through_cache_and_schema_failure(make_cache):
    cache, _ = make_cache([("lobby/getcontests", 200, fixture_bytes("dk_lobby.json"))])
    assert len(dk_public.lobby(cache=cache)) == 3
    broken = _json("dk_lobby.json")
    del broken["Contests"][1]["dg"]
    with pytest.raises(SourceSchemaError):
        dk_public.parse_lobby(broken)


def test_contest_detail_payouts_are_exact_decimals():
    d = dk_public.parse_contest_detail(_json("dk_contest_196048725.json"))
    assert (d.contest_id, d.draft_group_id, d.max_per_user, d.maximum_entries) == (196048725, 153983, 20, 713)
    assert d.entry_fee == Decimal("0.25")
    assert d.payout_table[0] == dk_public.PayoutTier(1, 1, Decimal("15.00"), None)
    assert d.start_utc == datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)
    positions = [(t.min_pos, t.max_pos) for t in d.payout_table]
    assert positions == sorted(positions) and all(t.cash > 0 for t in d.payout_table)
    classic = dk_public.parse_contest_detail(_json("dk_contest_195958173.json"))
    assert classic.draft_group_id != d.draft_group_id


def test_money_and_ticket_tiers():
    assert dk_public.parse_money("$1,000.00") == Decimal("1000.00")
    with pytest.raises(SourceSchemaError):
        dk_public.parse_money("$1.00 + ticket")
    data = _json("dk_contest_196048725.json")
    data["contestDetail"]["payoutSummary"][0]["tierPayoutDescriptions"] = {"Ticket": "$5 NHL Ticket"}
    tier = dk_public.parse_contest_detail(data).payout_table[0]
    assert tier.cash == Decimal("0") and "Ticket" in tier.other


def test_draftables_403_becomes_source_unavailable(make_cache):
    cache, transport = make_cache([("draftables", 403, fixture_bytes("dk_draftables_403.html"))])
    with pytest.raises(SourceUnavailable, match="403"):
        dk_public.draftables(153983, cache=cache)
    assert len(transport.calls) == 1


# --- status map --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("OUT", Participation.OUT), ("IR", Participation.OUT), ("O", Participation.OUT),
     ("Q", Participation.QUESTIONABLE), ("GTD", Participation.QUESTIONABLE), ("D", Participation.QUESTIONABLE),
     ("DTD", Participation.QUESTIONABLE),
     (None, Participation.PLAYING), ("None", Participation.PLAYING), ("", Participation.PLAYING)],
)
def test_status_map(raw, expected):
    assert dk_public.map_status(raw)[0] is expected


def test_unrecognized_status_is_unknown_with_raw_kept():
    participation, raw = dk_public.map_status("SUSP")
    assert participation is Participation.UNKNOWN and raw == "SUSP"


# --- draftables payload (recorded 200 response) -------------------------------------------------


def test_draftables_parse_showdown_pairing():
    d = dk_public.parse_draftables(_draftables_payload(), 153983)
    by_player: dict[str, list] = {}
    for r in d.rows:
        by_player.setdefault(r.player_id, []).append(r)
    pairs = [rows for rows in by_player.values() if {r.roster_slot_id for r in rows} == {612, 613}]
    assert pairs, "no CPT/FLEX pairs found"
    for rows in pairs:
        cpt = next(r for r in rows if r.roster_slot_id == 612)
        flex = next(r for r in rows if r.roster_slot_id == 613)
        assert cpt.draftable_id != flex.draftable_id
        assert cpt.salary * 2 == flex.salary * 3
        assert cpt.participation is flex.participation
    assert all(r.start_utc is None or r.start_utc.tzinfo is not None for r in d.rows)


def test_is_swappable_never_changes_participation():
    payload = _draftables_payload()
    base = dk_public.parse_draftables(payload)
    flipped = json.loads(json.dumps(payload))
    for row in flipped["draftables"]:
        row["isSwappable"] = not row.get("isSwappable", False)
    after = dk_public.parse_draftables(flipped)
    assert [r.participation for r in base.rows] == [r.participation for r in after.rows]
    assert [r.is_swappable for r in base.rows] != [r.is_swappable for r in after.rows]


def test_draftables_schema_failure_returns_nothing():
    payload = _draftables_payload()
    del payload["draftables"][0]["rosterSlotId"]
    with pytest.raises(SourceSchemaError):
        dk_public.parse_draftables(payload)


def test_reconcile_to_real_showdown_csv_by_draftable_id():
    sal, _ = real_pair("showdown", "2026-09-29")  # the draftables fixture is draft group 153983, this slate
    pool = read_salary(sal)
    payload = _draftables_payload()
    rec = dk_public.reconcile(pool, dk_public.parse_draftables(payload, 153983))
    drafted_ids = {str(r["draftableId"]) for r in payload["draftables"]}
    for rid, state in rec.rows.items():
        if rid in drafted_ids:
            assert state.obs_status is ObsStatus.CURRENT, (rid, state.notes)
            assert state.eligibility in (Eligibility.ROSTERABLE, Eligibility.DISABLED)
        else:
            assert state.obs_status is ObsStatus.MISSING and state.participation is Participation.UNKNOWN
    assert rec.unmatched_draftable_ids == [] and not rec.conflicted
    assert drafted_ids <= set(rec.rows)

    changed = json.loads(json.dumps(payload))
    changed["draftables"][0]["salary"] += 100
    rec2 = dk_public.reconcile(pool, dk_public.parse_draftables(changed, 153983))
    first = str(changed["draftables"][0]["draftableId"])
    assert rec2.rows[first].obs_status is ObsStatus.CONFLICTED
    assert pool.by_role_id[first].salary == read_salary(sal).by_role_id[first].salary  # the CSV is never rewritten
