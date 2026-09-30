"""Backlog B2: after a clean backfill the raw report cache drops bodies whose rows are in the Parquet store, keeps the
completed seasons' canonical windows (so they rebuild offline) and anything within its TTL, and touches no other
source."""
import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FakeTransport
from nhl_dfs.data.history import nhl_reports, store
from nhl_dfs.data.http import HttpCache, SourceUnavailable, load_sources_config
from test_nhl_reports import H, _window

pytestmark = pytest.mark.c4

DONE, CUR = 20242025, 20252026
T0 = datetime(2025, 10, 20, 6, 0, tzinfo=timezone.utc)


def _route(name):
    body = json.loads((H / name).read_bytes())

    def respond(url):
        d0, d1 = _window(url)
        if any(d0 <= d <= d1 for d in (date(2025, 3, 10), date(2025, 10, 9), date(2025, 10, 12))):
            return json.dumps(body).encode()
        return b'{"data": [], "total": 0}'

    return respond


ROUTES = [("skater/timeonice", 200, _route("nhl_skater_timeonice_20251009.json")),
          ("skater/realtime", 200, _route("nhl_skater_realtime_20251009.json")),
          ("skater/summary", 200, _route("nhl_skater_summary_20251009.json")),
          ("goalie/summary", 200, _route("nhl_goalie_summary_20251009.json"))]


def _cfg():
    cfg = load_sources_config()
    return {**cfg, "http": {**cfg["http"], "max_calls_per_run": 100000}}


def backfill(tmp, *, at, today, since=None, seasons=(DONE, CUR), offline=False, store_sub="store"):
    cfg = _cfg()
    cache = HttpCache(tmp / "raw", config=cfg, transport=FakeTransport(ROUTES), sleep=lambda s: None,
                      clock=lambda: at, offline=offline)
    return nhl_reports.backfill(list(seasons), since=since, cache=cache, store_root=tmp / store_sub, cfg=cfg, today=today,
                                crosscheck_games=0, moneypuck=False, raw_root=tmp / "mp")


def evict(tmp, *, now, today, **kw):
    return nhl_reports.evict_raw(tmp / "raw", cfg=_cfg(), store_root=tmp / "store", today=today, now=now, **kw)


def _bytes(tmp):
    return sum(f.stat().st_size for s in ("nhl_report", "nhl_goalie_report") for f in (tmp / "raw" / s).rglob("*")
               if f.is_file() and f.name != "index.json")


def _new_bytes(tmp, at):
    """Bytes of the bodies fetched at `at` (the new windows of that run)."""
    total, seen = 0, set()
    for s in ("nhl_report", "nhl_goalie_report"):
        for e in json.loads((tmp / "raw" / s / "index.json").read_text(encoding="utf-8")).values():
            if e["fetched_at"].startswith(at.isoformat()[:19]) and e["raw_path"] not in seen:
                seen.add(e["raw_path"])
                total += (tmp / "raw" / e["raw_path"]).stat().st_size
    return total


@pytest.fixture
def aged(tmp_path):
    """A full backfill, then two nightly incremental runs, each followed by the eviction pass."""
    backfill(tmp_path, at=T0, today=date(2025, 10, 20))
    first = evict(tmp_path, now=T0, today=date(2025, 10, 20))
    assert all(v["entries_dropped"] == 0 for v in first.sources.values())  # everything is within its TTL
    s0 = _bytes(tmp_path)
    sizes = [s0]
    for k, since in ((1, date(2025, 10, 10)), (2, date(2025, 10, 11))):
        at = T0 + timedelta(days=k)
        backfill(tmp_path, at=at, today=date(2025, 10, 20) + timedelta(days=k), since=since)
        new = _new_bytes(tmp_path, at)
        evict(tmp_path, now=at, today=date(2025, 10, 20) + timedelta(days=k))
        sizes.append((_bytes(tmp_path), new))
    return tmp_path, sizes


def test_after_two_incremental_runs_raw_grows_by_no_more_than_the_new_windows(aged):
    tmp, sizes = aged
    s0 = sizes[0]
    (s1, new1), (s2, new2) = sizes[1], sizes[2]
    assert s1 - s0 <= new1 and s2 - s1 <= new2
    canon = nhl_reports.canonical_urls(DONE, _cfg())
    for src in ("nhl_report", "nhl_goalie_report"):
        idx = json.loads((tmp / "raw" / src / "index.json").read_text(encoding="utf-8"))
        done_urls = {u for u in idx if u in canon[src]}
        assert done_urls and all((tmp / "raw" / e["raw_path"]).exists() for e in idx.values())  # no dangling entry
        stale = [u for u, e in idx.items() if u not in done_urls and not e["fetched_at"].startswith("2025-10-22")]
        assert stale == []  # the full run's and the first incremental's current-season windows were evicted
        refs = {e["raw_path"] for e in idx.values()}
        on_disk = {f.relative_to(tmp / "raw").as_posix() for f in (tmp / "raw" / src).rglob("*")
                   if f.is_file() and f.name != "index.json"}
        assert on_disk == refs  # every body left is referenced (a shared body stays while any entry uses it)


def test_the_completed_season_rebuilds_offline_row_for_row_from_the_kept_files(aged):
    tmp, _ = aged
    backfill(tmp, at=T0 + timedelta(days=3), today=date(2025, 10, 23), seasons=(DONE,), offline=True, store_sub="rebuilt")
    for kind in ("nhl_skater_games", "nhl_goalie_games"):
        a = store.read(kind, [DONE], root=tmp / "store")
        b = store.read(kind, [DONE], root=tmp / "rebuilt")
        assert len(a) > 0
        pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


def test_an_offline_rebuild_that_needs_an_evicted_window_raises_and_leaves_the_store_untouched(aged):
    tmp, _ = aged
    p = store.path_for("nhl_skater_games", CUR, root=tmp / "store")
    before = p.read_bytes()
    with pytest.raises(SourceUnavailable):
        backfill(tmp, at=T0 + timedelta(days=3), today=date(2025, 10, 23), seasons=(CUR,), offline=True)
    assert p.read_bytes() == before


def test_bodies_not_yet_in_the_store_other_sources_and_dry_runs_are_kept(tmp_path):
    import os

    backfill(tmp_path, at=T0, today=date(2025, 10, 20))
    dk = tmp_path / "raw" / "dk_contest" / "2025-10-01" / "x.json"
    dk.parent.mkdir(parents=True)
    dk.write_text("{}", encoding="utf-8")
    later = T0 + timedelta(days=1)
    before = _bytes(tmp_path)
    idx_before = (tmp_path / "raw" / "nhl_report" / "index.json").read_bytes()
    dry = evict(tmp_path, now=later, today=date(2025, 10, 21), dry_run=True)
    assert dry.dry_run and dry.sources["nhl_report"]["entries_dropped"] > 0 and "dry run" in dry.lines()[0]
    assert _bytes(tmp_path) == before and (tmp_path / "raw" / "nhl_report" / "index.json").read_bytes() == idx_before
    old = (T0 - timedelta(days=30)).timestamp()  # the store predates the bodies: their rows may not be in it
    for kind in ("nhl_skater_games", "nhl_goalie_games"):
        for season in (DONE, CUR):
            sp = store.path_for(kind, season, root=tmp_path / "store")
            if sp.exists():
                os.utime(sp, (old, old))
    ev = evict(tmp_path, now=later, today=date(2025, 10, 21))
    assert all(v["entries_dropped"] == 0 and v["kept_unstored"] > 0 for v in ev.sources.values())
    assert _bytes(tmp_path) == before and dk.exists()  # nothing deleted; DK contest bodies are never touched
