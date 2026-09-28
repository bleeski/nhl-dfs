import random

import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.models import ownership, prefit
from nhl_dfs.models.projection import PriorProjection
from pool_builder import varied_pool

pytestmark = pytest.mark.c3

PLANTED = {"appg": 0.9, "value_z": 0.4, "salary_rank": 3.5}  # offsets are these minus the config prior
B0 = 0.35


def write_history(root, mode: Mode, n_groups: int, contests: int, cfg, *, seed=0, planted=PLANTED):
    """Synthetic history whose labels come from the prefit surrogate with planted weights."""
    rng = random.Random(seed)
    pools = {}
    w = {k: float(cfg["weights"][k]) for k in prefit.FIT_FEATURES}
    w.update(planted)
    for g in range(n_groups):
        name = f"2025-{10 + g // 28:02d}-{1 + g % 28:02d}_{mode.value}{g}"
        pool = varied_pool(mode, seed=1000 * seed + g)
        pools[name] = pool
        proj = PriorProjection(pool)
        feats = ownership.feature_table(pool, proj, cfg=cfg)
        blocks = {}
        for r in pool.rows:
            blocks.setdefault(prefit._block(pool, r.role_id), []).append(r.role_id)
        lines = [",".join(prefit.LABELS_HEADER)]
        for c in range(contests):
            for key, rids in sorted(blocks.items()):
                mass = 100.0  # any positive block mass; only shares within a block are modeled
                x = [[proj.mean_tenths(r) / 10.0] + [feats[r][k] for k in prefit.FIT_FEATURES] for r in rids]
                beta = [B0] + [B0 * w[k] for k in prefit.FIT_FEATURES]
                pred = prefit._predict(x, mass, beta)
                for rid, p in zip(rids, pred):
                    noisy = p * rng.uniform(0.97, 1.03)
                    lines.append(f"{9000 + c},{rid},{noisy:.2f}")
        d = root / name
        d.mkdir(parents=True)
        (d / prefit.LABELS_FILE).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return pools


@pytest.fixture(scope="module")
def cfg():
    return ownership.load_ownership_config()


def test_refuses_below_floor_per_mode(tmp_path, cfg):
    pools = write_history(tmp_path, Mode.CLASSIC, 12, 2, cfg)
    res = prefit.fit(tmp_path, pools, cfg=cfg)
    c = res.by_mode[Mode.CLASSIC]
    assert c.status == "gated" and "slate_groups 12 < 30" in c.reason and not c.weights
    assert res.by_mode[Mode.SHOWDOWN].status == "gated"


def test_classic_above_floor_recovers_planted_weights(tmp_path, cfg):
    # 34 groups x 10 contests x 48 rows = 16,320 labels: clears 30 groups and 15,000 labels.
    pools = write_history(tmp_path, Mode.CLASSIC, 34, 10, cfg, seed=1)
    res = prefit.fit(tmp_path, pools, cfg=cfg)
    r = res.by_mode[Mode.CLASSIC]
    assert r.status == "fitted", r.reason
    assert r.counts.ownership_labels >= 15000 and r.counts.slate_groups == 34
    assert len(r.holdout_groups) == 10 and r.train_groups[-1] < r.holdout_groups[0]  # chronological
    for k, v in PLANTED.items():
        assert r.weights[k] == pytest.approx(v, abs=0.1), (k, r.weights[k])
        assert r.offsets[k] == pytest.approx(v - cfg["weights"][k], abs=0.1)
    assert r.improves and r.holdout_mae_fitted < r.holdout_mae_prior
    assert res.by_mode[Mode.SHOWDOWN].status == "gated"  # no Showdown history: per-mode floors


def test_showdown_above_floor_recovers_planted_weights(tmp_path, cfg):
    # 52 games x 4 contests x 32 role rows = 6,656 labels: clears 50 games and 5,000 labels.
    pools = write_history(tmp_path, Mode.SHOWDOWN, 52, 4, cfg, seed=2)
    r = prefit.fit(tmp_path, pools, cfg=cfg).by_mode[Mode.SHOWDOWN]
    assert r.status == "fitted", r.reason
    assert len(r.holdout_groups) == 15
    for k, v in PLANTED.items():
        assert r.weights[k] == pytest.approx(v, abs=0.1), (k, r.weights[k])


def test_many_contests_on_few_slates_stay_gated(tmp_path, cfg):
    pools = write_history(tmp_path, Mode.CLASSIC, 6, 60, cfg)  # 17,280 labels but only 6 groups
    r = prefit.fit(tmp_path, pools, cfg=cfg).by_mode[Mode.CLASSIC]
    assert r.status == "gated" and r.counts.ownership_labels >= 15000


def test_unverified_header_fails_loudly(tmp_path, cfg):
    pools = write_history(tmp_path, Mode.CLASSIC, 1, 1, cfg)
    (d,) = [p for p in tmp_path.iterdir()]
    (d / prefit.LABELS_FILE).write_bytes(b"Rank,EntryId,EntryName,Player,Roster Position,%Drafted\n")
    with pytest.raises(prefit.LabelFormatError):
        prefit.fit(tmp_path, pools, cfg=cfg)


def test_missing_dir_and_bad_rows(tmp_path, cfg):
    res = prefit.fit(tmp_path / "absent", cfg=cfg)
    assert all(r.status == "gated" for r in res.by_mode.values()) and res.skipped
    pools = write_history(tmp_path, Mode.CLASSIC, 1, 1, cfg)
    (d,) = [p for p in tmp_path.iterdir()]
    pool = pools[d.name]
    rid = pool.rows[0].role_id
    (d / prefit.LABELS_FILE).write_bytes(
        f"contest_id,role_id,drafted_pct\n1,{rid},12.5\n1,{rid},3\n1,999999,4\n1,{pool.rows[1].role_id},140\n".encode())
    labels, bad = prefit.read_labels(d / prefit.LABELS_FILE, pool)
    assert labels == {"1": {rid: 12.5}} and bad == 3
    (tmp_path / "not-a-group").mkdir()
    assert any("not-a-group" in s for s in prefit.fit(tmp_path, pools, cfg=cfg).skipped)
