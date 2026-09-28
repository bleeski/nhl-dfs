import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import REPO_ROOT, fixture_bytes

from nhl_dfs.data.http import HttpCache, SourceSchemaError, SourceUnavailable, load_sources_config
from nhl_dfs.data.observations import read_all
from nhl_dfs.data.sources.nhl import parse_partner_odds

pytestmark = pytest.mark.c1

ODDS_URL = "https://api-web.nhle.com/v1/partner-game/US/now"
T0 = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _get(cache, schema=parse_partner_odds, ttl_s=600):
    return cache.get_json(ODDS_URL, source="nhl_partner_odds", ttl_s=ttl_s, schema=schema)


def test_fresh_fetch_is_stored_indexed_and_observed(make_cache, tmp_path):
    clock = Clock(T0)
    cache, transport = make_cache([("partner-game", 200, fixture_bytes("nhl_partner_odds.json"))], clock=clock)
    f = _get(cache)
    assert not f.from_cache and f.status.value == "CURRENT"
    assert f.raw_path.parent == tmp_path / "raw" / "nhl_partner_odds" / "2026-09-28"
    assert f.raw_path.name == f"{f.raw_hash}.json" and f.raw_path.read_bytes() == fixture_bytes("nhl_partner_odds.json")
    index = json.loads((tmp_path / "raw" / "nhl_partner_odds" / "index.json").read_text())
    assert index[ODDS_URL]["raw_hash"] == f.raw_hash
    obs = read_all(tmp_path / "raw")
    assert [o["status"] for o in obs] == ["CURRENT"] and obs[0]["raw_hash"] == f.raw_hash
    assert len(transport.calls) == 1


def test_cache_hit_within_ttl_then_refetch_after(make_cache):
    clock = Clock(T0)
    cache, transport = make_cache([("partner-game", 200, fixture_bytes("nhl_partner_odds.json"))], clock=clock)
    _get(cache)
    clock.t = T0 + timedelta(seconds=300)
    assert _get(cache).from_cache and len(transport.calls) == 1
    clock.t = T0 + timedelta(seconds=601)
    assert not _get(cache).from_cache and len(transport.calls) == 2


def test_offline_serves_cache_as_stale_and_raises_when_absent(make_cache, tmp_path):
    clock = Clock(T0)
    online, _ = make_cache([("partner-game", 200, fixture_bytes("nhl_partner_odds.json"))], clock=clock)
    _get(online)
    clock.t = T0 + timedelta(days=2)
    offline = HttpCache(tmp_path / "raw", config=load_sources_config(), transport=None, offline=True, clock=clock)
    f = _get(offline)
    assert f.from_cache and f.status.value == "STALE"
    with pytest.raises(SourceUnavailable, match="offline"):
        offline.get_json("https://api-web.nhle.com/v1/schedule/2026-09-29", source="nhl_schedule", ttl_s=900,
                         schema=lambda d: None)
    assert read_all(tmp_path / "raw")[-1]["status"] == "MISSING"


def test_offline_env_var_turns_on_cache_only(monkeypatch, tmp_path):
    monkeypatch.setenv("NHL_DFS_OFFLINE", "1")
    cache = HttpCache(tmp_path, config=load_sources_config())
    assert cache.offline
    with pytest.raises(SourceUnavailable):
        _get(cache)


def test_403_fixture_is_source_unavailable_without_retry(make_cache, tmp_path):
    cache, transport = make_cache([("draftables", 403, fixture_bytes("dk_draftables_403.html"))])
    with pytest.raises(SourceUnavailable, match="403"):
        cache.get_json("https://api.draftkings.com/draftgroups/v1/draftgroups/153983/draftables?format=json",
                       source="dk_draftables", ttl_s=300, schema=lambda d: None)
    assert len(transport.calls) == 1 and transport.sleeps == []
    assert not (tmp_path / "raw" / "dk_draftables" / "index.json").exists()
    assert read_all(tmp_path / "raw")[-1]["detail"] == "HTTP 403"


def test_5xx_is_retried_once_after_one_second(make_cache):
    cache, transport = make_cache([("partner-game", 503, b"busy")])
    with pytest.raises(SourceUnavailable):
        _get(cache)
    assert len(transport.calls) == 2 and 1.0 in transport.sleeps


def test_timeout_then_success_returns_data(tmp_path):
    body = fixture_bytes("nhl_partner_odds.json")
    responses = [TimeoutError("read timed out"), (200, body, {})]

    def transport(url, headers, timeout_s):
        assert timeout_s == 6.0 and "Mozilla" in headers["User-Agent"]
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    cache = HttpCache(tmp_path, config=load_sources_config(), transport=transport, sleep=lambda s: None)
    assert _get(cache).data["bettingPartner"]["name"] == "DraftKings"
    assert cache.calls_made == 2


def test_schema_failure_returns_nothing_and_is_not_cached(make_cache, tmp_path):
    broken = json.loads(fixture_bytes("nhl_partner_odds.json"))
    del broken["lastUpdatedUTC"]
    cache, _ = make_cache([("partner-game", 200, json.dumps(broken).encode())])
    with pytest.raises(SourceSchemaError) as info:
        _get(cache)
    assert info.value.raw_path is not None and info.value.raw_path.exists()
    assert not (tmp_path / "raw" / "nhl_partner_odds" / "index.json").exists()
    offline = HttpCache(tmp_path / "raw", config=load_sources_config(), offline=True)
    with pytest.raises(SourceUnavailable):
        _get(offline)


def test_non_json_200_is_a_schema_error(make_cache):
    cache, _ = make_cache([("partner-game", 200, fixture_bytes("dk_draftables_403.html"))])
    with pytest.raises(SourceSchemaError):
        _get(cache)


def test_call_budget_is_enforced(make_cache):
    cfg = copy.deepcopy(load_sources_config())
    cfg["http"]["max_calls_per_run"] = 2
    cache, transport = make_cache([("", 200, fixture_bytes("nhl_partner_odds.json"))], config=cfg)
    for n in range(2):
        cache.get_json(f"{ODDS_URL}?n={n}", source="nhl_partner_odds", ttl_s=0, schema=lambda d: None)
    with pytest.raises(SourceUnavailable, match="budget"):
        cache.get_json(f"{ODDS_URL}?n=2", source="nhl_partner_odds", ttl_s=0, schema=lambda d: None)
    assert len(transport.calls) == 2


def test_force_refresh_ignores_the_cache(make_cache):
    cache, transport = make_cache([("partner-game", 200, fixture_bytes("nhl_partner_odds.json"))])
    _get(cache)
    cache.force_refresh = True
    assert not _get(cache).from_cache and len(transport.calls) == 2


def test_tests_cannot_reach_the_real_network(tmp_path):
    cache = HttpCache(tmp_path, config=load_sources_config(), sleep=lambda s: None)
    with pytest.raises(SourceUnavailable, match="real network call"):
        _get(cache)


# --- capture ---------------------------------------------------------------------------------


def _capture_routes():
    return [
        ("lobby/getcontests", 200, fixture_bytes("dk_lobby.json")),
        ("contests/v1/contests/", 200, lambda url: fixture_bytes(f"dk_contest_{url.split('/')[-1].split('?')[0]}.json")),
        ("draftables", 403, fixture_bytes("dk_draftables_403.html")),
        ("v1/schedule/2026-09-29", 200, fixture_bytes("nhl_schedule_2026-09-29.json")),
        ("partner-game", 200, fixture_bytes("nhl_partner_odds.json")),
        ("covers.com", 200, fixture_bytes("covers_odds.html")),
        ("starting-goalies", 200, fixture_bytes("df_starting_goalies.html")),
        ("line-combinations", 200, fixture_bytes("df_lines_vegas-golden-knights.html")),
    ]


def _load_capture():
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import capture

    return capture


def test_capture_writes_index_with_hashes_and_goalie_scripts(make_cache, tmp_path):
    capture = _load_capture()
    now = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)  # 08:30 America/Chicago
    cache, transport = make_cache(_capture_routes(), clock=Clock(now))
    cache.force_refresh = True
    snap = capture.run_once(cache, now=now)
    assert snap == tmp_path / "raw" / "capture" / "2026-09-29" / "1330"
    index = json.loads((snap / "index.json").read_text(encoding="utf-8"))
    by_name = {i["name"]: i for i in index["items"]}
    assert {"dk_lobby", "dk_contest_196048725", "dk_contest_195958173", "nhl_partner_odds", "covers_odds",
            "df_starting_goalies", "nhl_schedule_2026-09-29"} <= set(by_name)
    for item in index["items"]:
        if item.get("ok"):
            body = (snap / item["file"]).read_bytes()
            assert len(item["raw_hash"]) == 64
            assert __import__("hashlib").sha256(body).hexdigest() == item["raw_hash"]
    drafts = [i for i in index["items"] if i["name"].startswith("dk_draftables_")]
    assert drafts and all(not i["ok"] and "403" in i["error"] for i in drafts)
    goalies = (snap / by_name["df_starting_goalies"]["file"]).read_text(encoding="utf-8")
    assert "<script" in goalies and 'id="__NEXT_DATA__"' in goalies
    lines = [i for i in index["items"] if i["name"].startswith("df_lines_")]
    assert len(lines) == 10  # the 2026-09-29 fixture day has 5 games
    assert index["calls_made"] == len(transport.calls)


def test_register_capture_task_dry_run_changes_nothing():
    shell = shutil.which("powershell")
    if shell is None:
        pytest.skip("powershell not available on this machine")
    script = REPO_ROOT / "tools" / "register_capture_task.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-DryRun"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert "Dry run: nothing registered. 12 task(s)" in result.stdout
    assert "Unregister" in result.stdout
