import random
from datetime import datetime, timedelta, timezone

import pytest

from nhl_dfs.build import provisional
from nhl_dfs.build.assign import Caps
from nhl_dfs.build.candidates import generate
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.intake.entries import EntryRow
from nhl_dfs.models import field, ownership
from nhl_dfs.models.contests import load_contest_families, resolve
from nhl_dfs.models.priors import prior_objective, prior_table
from nhl_dfs.models.projection import PriorProjection
from pool_builder import varied_pool

pytestmark = pytest.mark.c3

LOOSE = Caps(person_max_share=1.0, captain_max_share=1.0, classic_max_overlap=9, util_tie_band_points=0.25)


@pytest.fixture(scope="module")
def fam_cfg():
    return load_contest_families()


@pytest.fixture(scope="module")
def own_cfg():
    return ownership.load_ownership_config()


def bank(mode, n=60, seed=0):
    pool = varied_pool(mode, seed=seed)
    proj = PriorProjection(pool)
    obj = prior_objective(pool, prior_table(pool))
    cands = generate(pool, mode, obj, n, seed=seed, perturb_sd=3.0, min_pairwise_diff=2)
    return pool, proj, cands


def fake_marginals(pool, seed=1, family="large_gpp"):
    rng = random.Random(seed)
    own = {r.role_id: rng.choice([0.0, 0.0, rng.uniform(0.5, 60.0)]) for r in pool.rows}
    return field.Marginals(family, 100, 1000, own, {}, {}, {}, {}, {}, 0, False)


def entries(spec):
    """[(contest name, contest id, count)] -> EntryRows."""
    out, n = [], 0
    for name, cid, k in spec:
        for _ in range(k):
            n += 1
            out.append(EntryRow(str(8_000_000 + n), name, cid, "$1", (), n, b""))
    return out


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("policy", ["own_then_dup", "dup_first", "mean"])
def test_never_prefers_a_candidate_more_than_one_band_worse(mode, policy, fam_cfg, own_cfg):
    pool, proj, cands = bank(mode)
    ranked = provisional.rank(cands, pool, proj, fake_marginals(pool), policy, fam_cfg, own_cfg=own_cfg)
    assert len(ranked) == len(cands)
    for i, a in enumerate(ranked):
        for b in ranked[i + 1:]:
            assert b.mean_tenths - a.mean_tenths <= a.band + 1e-9
    if policy == "mean":
        means = [s.mean_tenths for s in ranked]
        assert means == sorted(means, reverse=True)


def test_leverage_moves_inside_band_only(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    ranked = provisional.rank(cands, pool, proj, fake_marginals(pool), "own_then_dup", fam_cfg, own_cfg=own_cfg)
    first_band = [s for s in ranked if s.band_index == 0]
    assert first_band[0].own_pct == min(s.own_pct for s in first_band)
    assert ranked[0].mean_tenths >= max(s.mean_tenths for s in ranked) - ranked[0].band


def test_band_floor_can_exceed_the_percentage(fam_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    c = cands[0]
    mean = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {}, 1.0)
    from nhl_dfs.models.projection import lineup_sd_tenths

    se = lineup_sd_tenths(pool, proj, c.role_ids) / 10.0  # Monte Carlo standard error at 100 sims
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.0, 100) == pytest.approx(se)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.001, 100) == pytest.approx(se)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.5, 100) == pytest.approx(0.5 * mean)


def test_no_simulation_means_the_percentage_governs(fam_cfg, own_cfg):
    assert fam_cfg["selection"]["band_floor_sims"] is None  # priors: analytic mean, standard error 0
    pool, proj, cands = bank(Mode.SHOWDOWN)
    c = cands[0]
    mean = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {}, 1.0)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.03) == pytest.approx(0.03 * mean)
    ranked = provisional.rank(cands, pool, proj, fake_marginals(pool), "own_then_dup", fam_cfg, own_cfg=own_cfg)
    assert ranked[0].band == pytest.approx(0.03 * ranked[0].anchor_tenths)
    assert ranked[0].mean_tenths >= 0.97 * max(s.mean_tenths for s in ranked) - 1e-9


def test_dtd_questionable_mean_is_haircut(fam_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    c = cands[0]
    q = fam_cfg["selection"]["questionable_play_prob"]
    rid = c.role_ids[0]
    base = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {}, q)
    hit = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {rid: Participation.QUESTIONABLE}, q)
    assert hit == pytest.approx(base - (1 - q) * proj.mean_tenths(rid))


def later_starts(pool, seed=3):
    rng = random.Random(seed)
    t0 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
    by_team = {t: t0 + timedelta(hours=rng.randint(0, 3)) for t in sorted(pool.teams)}
    return {r.role_id: by_team[r.team] for r in pool.rows}


@pytest.mark.parametrize("mode", list(Mode))
def test_each_pick_within_one_band_of_best_available(mode, fam_cfg, own_cfg):
    pool, proj, cands = bank(mode)
    ents = entries([("NHL $5K Sniper, 150 Max", "1", 6), ("NHL $3 Winner Take All", "2", 3),
                    ("NHL Single Entry $1 Double Up", "3", 3)])
    ctx = resolve(ents, cfg=fam_cfg)
    margs = {cid: fake_marginals(pool, seed=int(cid)) for cid in ctx}
    starts = later_starts(pool)
    a, scored = provisional.select(cands, proj, margs, ctx, ents, LOOSE, fam_cfg, seed=1, pool=pool,
                                   later_start_utc=starts, own_cfg=own_cfg)
    assert set(a.by_entry) == {e.entry_id for e in ents} and not a.relaxations
    used: set[str] = set()
    order = [e for f in fam_cfg["selection"]["family_order"] for e in ents if ctx[e.contest_id].family == f]
    for e in order:
        policy = fam_cfg["families"][ctx[e.contest_id].family]["selection"]
        ranked = provisional.rank(cands, pool, proj, margs[e.contest_id], policy, fam_cfg, own_cfg=own_cfg)
        avail = [s for s in ranked if s.cand.key not in used]
        pick = scored[e.entry_id]
        assert pick.cand.key == avail[0].cand.key  # no UTIL swap undid the order
        assert max(s.mean_tenths for s in avail) - pick.mean_tenths <= pick.band + 1e-9
        used.add(pick.cand.key)


def test_cash_entries_get_the_highest_mean_candidates(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    ents = entries([("NHL $5K Sniper, 150 Max", "1", 4), ("NHL Single Entry $1 Double Up", "3", 3)])
    ctx = resolve(ents, cfg=fam_cfg)
    statuses = {pool.rows[0].role_id: Participation.QUESTIONABLE}
    margs = {cid: fake_marginals(pool) for cid in ctx}
    a, scored = provisional.select(cands, proj, margs, ctx, ents, LOOSE, fam_cfg, seed=1, pool=pool,
                                   statuses=statuses, own_cfg=own_cfg)
    q = fam_cfg["selection"]["questionable_play_prob"]
    means = sorted((provisional.adjusted_mean_tenths(pool, proj, c.role_ids, statuses, q) for c in cands), reverse=True)
    cash = sorted((scored[e.entry_id].mean_tenths for e in ents if e.contest_id == "3"), reverse=True)
    assert cash == pytest.approx(means[:3])


def test_caps_span_the_whole_portfolio(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC, n=80)
    ents = entries([("NHL Single Entry $1 Double Up", "3", 5), ("NHL $5K Sniper, 150 Max", "1", 5)])
    ctx = resolve(ents, cfg=fam_cfg)
    caps = Caps(person_max_share=0.5, captain_max_share=0.4, classic_max_overlap=7)
    a, _ = provisional.select(cands, proj, {}, ctx, ents, caps, fam_cfg, seed=1, pool=pool, own_cfg=own_cfg)
    if not a.relaxations:
        assert max(a.person_exposures.values()) <= caps.person_cap(10)


# -- run level ----------------------------------------------------------------------------------

import json  # noqa: E402

from conftest import fixture_bytes, mini_pair, real_pair  # noqa: E402
from nhl_dfs import cli  # noqa: E402
from nhl_dfs.build import run as run_mod  # noqa: E402
from nhl_dfs.build.run import run_slate  # noqa: E402
from nhl_dfs.contracts.statuses import Eligibility, ObsStatus  # noqa: E402
from nhl_dfs.data.sources import dk_public  # noqa: E402
from nhl_dfs.intake.entries import cell_role_id, read_entries  # noqa: E402

BEFORE = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)  # pinned before the 2026-09-29 slates


def _run(tmp_path, salary, entries, **kw):
    kw.setdefault("offline", True)
    kw.setdefault("clock", lambda: BEFORE)
    kw.setdefault("baseline_only", False)
    return run_slate(salary, entries, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs", **kw)


def _rostered(path) -> set[str]:
    ef = read_entries(path)
    return {cell_role_id(c) for e in ef.entries for c in e.cells if cell_role_id(c)}


@pytest.fixture(scope="module")
def offline_classic(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prov")
    return tmp, _run(tmp, *mini_pair("classic"))


def test_offline_run_publishes_baseline_then_provisional(offline_classic):
    tmp, r = offline_classic
    assert r.ok and [v["phase"] for v in r.manifest["versions"]] == ["A", "P"]
    assert all(v["public_replaced"] for v in r.manifest["versions"])
    p = r.manifest["provisional"]
    assert p["version"] == 2
    assert p["evidence"] == {"MODEL_STATUS": "PRIOR", "PAYOUT_SOURCE": "PRIOR",
                             "OUTCOME_CALIBRATION": "UNVALIDATED", "FIELD_CALIBRATION": "PRIOR"}
    for k, v in p["evidence"].items():
        assert r.statuses[k] == v  # the run manifest carries the evidence states
    assert {c["PAYOUT_SOURCE"] for c in p["contests"]} == {"PRIOR"}
    assert {c["field_size_source"] for c in p["contests"]} == {"family_prior"}
    assert p["prefit"].startswith("gated:") and "slate_groups 0 < 30" in p["prefit"]
    assert all(not f["degraded"] for f in p["fields"].values())
    assert {e["entry_id"] for e in p["entries"]} == {e.entry_id for e in read_entries(mini_pair("classic")[1]).entries}
    ok, lines = cli.verify_run(tmp / "runs", r.run.run_id)
    assert ok, lines


def test_run_notes_label_every_provisional_figure(offline_classic):
    _, r = offline_classic
    notes = r.notes_path.read_text(encoding="utf-8")
    sec = notes[notes.index("## Provisional leverage pass (PROVISIONAL)"):]
    assert "Lineup own % sum (provisional)" in sec and "Dup proxy (provisional)" in sec
    assert "Field dup est. (provisional)" in sec
    assert "OUTCOME_CALIBRATION=UNVALIDATED" in sec and "FIELD_CALIBRATION=PRIOR" in sec
    lowered = sec.lower()
    for banned in ("probability", "ceiling", "roi", "win%"):
        assert banned not in lowered.replace("no probability", "")
    assert "cash (name_pattern)" in sec and "large_gpp (default)" in sec


def test_cash_entry_gets_the_top_mean(offline_classic):
    _, r = offline_classic
    rows = r.manifest["provisional"]["entries"]
    cash = [e for e in rows if e["family"] == "cash"]
    assert cash and cash[0]["mean_pts"] >= max(e["mean_pts"] for e in rows)


def test_field_command_reads_the_saved_field(offline_classic, capsys):
    tmp, r = offline_classic
    assert (r.run.path / "field.json").exists()
    assert cli.main(["field", "--run", r.run.run_id, "--runs-root", str(tmp / "runs")]) == 0
    out = capsys.readouterr().out
    assert "PROVISIONAL" in out and "mass total 900%, goalie 100%, skater 800%" in out


def test_field_command_samples_for_a_baseline_only_run(tmp_path, capsys):
    r = _run(tmp_path, *mini_pair("showdown"), baseline_only=True)
    assert [v["phase"] for v in r.manifest["versions"]] == ["A"] and "provisional" not in r.manifest
    assert cli.main(["field", "--run", r.run.run_id, "--runs-root", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "sampled now" in out and "cpt 100%, flex 500%" in out


def test_phase_b_exclusion_never_reappears_in_the_provisional_file(tmp_path, monkeypatch):
    salary, entries = mini_pair("classic")
    base = _run(tmp_path / "base", salary, entries, baseline_only=True)
    victim = sorted(_rostered(base.manifest["versions"][0]["path"]))[0]

    def fake_fetch(ents, cache, pool, box=None):
        rows = {rid: dk_public.RowState(Participation.PLAYING, Eligibility.ROSTERABLE, None, "", ObsStatus.CURRENT)
                for rid in pool.by_role_id}
        rows[victim] = dk_public.RowState(Participation.OUT, Eligibility.ROSTERABLE, None, "OUT", ObsStatus.CURRENT)
        return 1, dk_public.Reconciliation(rows, [])

    monkeypatch.setattr(run_mod, "_fetch_draftables", fake_fetch)
    monkeypatch.setattr(run_mod, "_fetch_contest_details", lambda ids, cache, budget: ({}, ["stub: offline"]))
    r = _run(tmp_path / "net", salary, entries, offline=False)
    phases = [v["phase"] for v in r.manifest["versions"]]
    assert phases == ["A", "B", "P"], phases
    assert victim not in _rostered(r.manifest["versions"][-1]["path"])
    assert r.manifest["provisional"]["network"] == ["stub: offline"]


def test_exact_contest_detail_sets_payout_source_per_contest(tmp_path, monkeypatch, make_cache):
    salary, entries = mini_pair("classic")
    ids = [e.contest_id for e in read_entries(entries).entries]
    body = fixture_bytes("dk_contest_195958173.json")
    detail = dk_public.parse_contest_detail(json.loads(body))
    from nhl_dfs.data.http import SourceUnavailable

    def draftables_403(*a, **k):
        raise SourceUnavailable("stub 403")

    monkeypatch.setattr(run_mod, "_fetch_draftables", draftables_403)
    monkeypatch.setattr(run_mod, "_fetch_contest_details",
                        lambda wanted, cache, budget: ({ids[0]: detail}, []))
    r = _run(tmp_path, salary, entries, offline=False)
    by_id = {c["contest_id"]: c for c in r.manifest["provisional"]["contests"]}
    assert by_id[ids[0]]["PAYOUT_SOURCE"] == "EXACT" and by_id[ids[0]]["field_size"] == detail.maximum_entries
    assert by_id[ids[0]]["family_source"] == "contest_detail"
    assert by_id[ids[1]]["PAYOUT_SOURCE"] == "PRIOR"
    assert r.statuses["PAYOUT_SOURCE"] == "PRIOR"  # EXACT only when every contest is exact


def test_provisional_failure_keeps_the_previous_version(tmp_path, monkeypatch):
    from nhl_dfs.build import provisional as prov

    def boom(*a, **k):
        raise RuntimeError("stub failure")

    monkeypatch.setattr(prov, "select", boom)
    r = _run(tmp_path, *mini_pair("showdown"))
    assert r.ok and [v["phase"] for v in r.manifest["versions"]] == ["A"]
    assert any("provisional pass" in f for f in r.manifest["failed"])
    assert r.statuses["PAYOUT_SOURCE"] == "PRIOR" and r.statuses["FIELD_CALIBRATION"] == "PRIOR"


def test_real_showdown_offline_provisional(tmp_path):
    salary, entries = real_pair("showdown", "2026-09-29")
    r = _run(tmp_path, salary, entries)
    assert r.ok and [v["phase"] for v in r.manifest["versions"]] == ["A", "P"]
    p = r.manifest["provisional"]
    assert {c["family"] for c in p["contests"]} == {"large_gpp"}
    assert all(f["n_draws"] == f["n_requested"] for f in p["fields"].values())
    ok, lines = cli.verify_run(tmp_path / "runs", r.run.run_id)
    assert ok, lines
