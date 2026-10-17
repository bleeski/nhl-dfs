"""C56: the referee-checked edit command, the model's write path (B141, B150; flags 124 to 131).

The run under test is built from the minimized 2026-10-17 Classic slate in tests/fixtures/c56 (make_fixture.py): the
engine baseline on its pool, then the 13 lineups Ben entered published as the delivered version, the file that was
upload-ready on 10-17 (the full files hash to 82d7107e, B140). No network."""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import REAL, TESTS
from nhl_dfs.build import controller, edits as edits_mod, late_swap, locks as locks_mod, packet
from nhl_dfs.build.manifest import read_manifest, write_manifest
from nhl_dfs.build.run import run_slate
from nhl_dfs.build.state import open_run, publish, sha256
from nhl_dfs.cli import verify_run
from nhl_dfs.contracts.geometry import slot_accepts
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.intake.entries import read_entries
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c56

F = TESTS / "fixtures" / "c56"
UTC = timezone.utc
T_OPEN = datetime(2026, 10, 17, 16, 30, tzinfo=UTC)  # every game is open (NJD@WSH 17:00Z, CAR@WPG 19:30Z, VAN@TBL 20:00Z)
T_NJD_STOP = datetime(2026, 10, 17, 16, 57, tzinfo=UTC)  # NJD@WSH is inside its 300 s edit stop
T_NJD_STARTED = datetime(2026, 10, 17, 17, 30, tzinfo=UTC)  # NJD@WSH has started
T_TWO_STARTED = datetime(2026, 10, 17, 19, 45, tzinfo=UTC)  # NJD@WSH and CAR@WPG have started, VAN@TBL is open
E1, E2 = "5298507816", "5298513806"
ALLEN, THOMPSON, HILDEBY, KOCHETKOV = "44537492", "44537491", "44537488", "44537490"
RULE = {"rule": "own_goalie", "reason": "the stack is worth facing the goalie for tonight"}


# -- the run ------------------------------------------------------------------------------------------------

def _install_entered(run, runs_root, outputs_root):
    """Publish the 13 entered lineups (the 10-17 delivered file) as the run's next version through the real publish."""
    v3 = F / "DKEntries.v3.csv"
    report = check_file(v3, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    assert report.ok, report.reasons
    m = read_manifest(run)
    pub = publish(run, v3.read_bytes(), report, m["slate_id"], outputs_root=outputs_root,
                  expect_public_sha=sha256(run.version_file(run.current_version()).read_bytes()))
    assert pub.public_replaced and pub.delivery_path
    m["versions"].append({"version": pub.version, "phase": "ENTERED", "sha256": report.out_sha256, "path": str(pub.version_path),
                          "public_replaced": True, "public_detail": "", "delivery_version": pub.delivery_version,
                          "delivery_path": str(pub.delivery_path), "created_utc": T_OPEN.isoformat()})
    write_manifest(run, m)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c56")
    r = run_slate(F / "DKSalaries.csv", F / "DKEntries.template.csv", offline=True, baseline_only=True, out_root=tmp / "runs",
                  outputs_root=tmp / "outputs", clock=lambda: T_OPEN - timedelta(hours=1))
    assert r.ok, r.manifest["failed"]
    _install_entered(r.run, tmp / "runs", tmp / "outputs")
    return tmp, r.run.run_id


def _fresh(base, tmp_path):
    tmp, rid = base
    shutil.copytree(tmp / "runs" / rid, tmp_path / "runs" / rid)
    shutil.copytree(tmp / "outputs", tmp_path / "outputs")
    return open_run(tmp_path / "runs", rid), tmp_path / "runs", tmp_path / "outputs"


def _view(run, runs):
    return packet.RunView(run, runs)


def _apply(run, runs, outs, edit_list, now=T_OPEN, clock=None):
    return edits_mod.apply_edits(run, json.dumps(edit_list), now=now, runs_root=runs, outputs_root=outs,
                                 clock=clock or (lambda: now))


def _restore(run, runs, outs, now=T_OPEN):
    return edits_mod.restore_previous(run, now=now, runs_root=runs, outputs_root=outs, clock=lambda: now)


def E(eid, cells, **kw):
    d = {"entry_id": eid, "edit_type": "coverage", "cells": cells, "reason": "cover the goalie the portfolio is missing",
         "evidence": "no lineup holds a CAR or TBL goalie (B140)", "claimed_gain": "lower P(lose 80%), first place not modeled"}
    d.update(kw)
    return d


def C(slot, pid, replaces=None, **kw):
    d = {"slot": slot, "player_id": pid}
    if replaces:
        d["replaces"] = replaces
    d.update(kw)
    return d


def _cells(path):
    """{(entry id, canonical slot): role id} of an entries file."""
    ef = read_entries(path)
    return {(e.entry_id, k): r for e in ef.entries for k, r in enumerate(late_swap._canonical(ef, e))}


def _diff(a, b):
    ca, cb = _cells(a), _cells(b)
    return sorted(k for k in ca if ca[k] != cb[k])


def _cur(run):
    return run.version_file(run.current_version())


def _published(outs, run):
    return json.loads((outs / read_manifest(run)["slate_id"] / "published.json").read_text(encoding="utf-8"))


def _state(run, outs):
    """What a refused publish must leave alone: the run's versions, its pointer, the stamp and the delivery files."""
    pub = outs / read_manifest(run)["slate_id"]
    return (run.version_numbers(), (run.path / "current").read_text(), (pub / "published.json").read_bytes(),
            sorted(p.name for p in pub.glob("DKEntries*.csv")), (pub / "DKEntries.csv").read_bytes())


def _playing(view, row):
    """Not OUT or IR in the salary file's Status column (the real 10-17 file lists several, Hellebuyck among them)."""
    return view.st.get(row.role_id, (Participation.PLAYING,))[0] is not Participation.OUT


def _swap_down(view, eid, *, same_team=True, team_in=None, skater=True, exclude_teams=()):
    """(slot label, out id, in id): an open cell and a cheaper-or-equal player the slot accepts, so the lineup stays legal."""
    labels = edits_mod.slot_labels(view.mode)
    lu = view.lineups[eid]
    for k, rid in enumerate(lu):
        row = view.pool.by_role_id[rid]
        if row.is_goalie == skater or row.team in exclude_teams:
            continue
        for cand in sorted(view.pool.rows, key=lambda r: (-r.salary, r.role_id)):
            if (cand.role_id in lu or cand.person_key == row.person_key or cand.salary > row.salary or cand.is_goalie != row.is_goalie
                    or not _playing(view, cand)
                    or (same_team and cand.team != row.team) or (team_in and cand.team not in team_in)
                    or not slot_accepts(view.slots[k], cand, view.mode)):
                continue
            return labels[k], rid, cand.role_id
    raise AssertionError(f"no legal swap found in {eid}")


def _two_game_edit(view, eid):
    """A skater swap in an entry that moves out of a game that is open to one inside the same open game set (CAR/WPG/VAN/TBL)."""
    return _swap_down(view, eid, same_team=False, team_in={"CAR", "WPG", "VAN", "TBL"}, exclude_teams={"NJD", "WSH"})


# -- the exit check (the card's own wording) -----------------------------------------------------------------

def test_exit_check_the_two_10_17_edits_apply_publish_and_deliver(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    before = _cur(run)
    prior_delivery = _published(outs, run)["delivery_version"]
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN, team="TBL", name="Dennis Hildeby")]),
                                    E(E2, [C("G", KOCHETKOV, THOMPSON, team="CAR")])])
    assert [o.status for o in res.entries] == ["APPLIED", "APPLIED"], [(o.rule, o.reason) for o in res.entries]
    assert [o.total for o in res.entries] == [49_200, 49_700]  # Allen $7,600 to Hildeby $8,200; Thompson $7,700 to Kochetkov $7,800
    after = _cur(run)
    assert run.current_version() == 3 and res.published_version == 3 and after != before
    assert _diff(before, after) == [(E1, 8), (E2, 8)]  # only the two goalie cells (canonical slot 8) changed
    assert check_file(after, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv", parent_path=before).ok
    stamp = _published(outs, run)
    assert stamp["delivery_version"] == prior_delivery + 1 == res.delivery_version
    delivery = Path(res.delivery_path)
    assert re.fullmatch(rf"DKEntries_v{res.delivery_version}_[0-9a-f]{{8}}\.csv", delivery.name)
    assert delivery.name.split("_")[2][:8] == hashlib.sha256(after.read_bytes()).hexdigest()[:8]
    assert delivery.read_bytes() == after.read_bytes() and stamp["delivery_name"] == delivery.name
    assert (outs / read_manifest(run)["slate_id"] / "DKEntries.csv").read_bytes() == after.read_bytes()
    ok, lines = verify_run(runs, run.run_id, now=T_OPEN)
    assert ok and "DELIVERY_CURRENT=TRUE" in "\n".join(lines) and any(l.startswith("delivery_file=") for l in lines)


def test_an_edit_on_a_locked_cell_is_rejected_and_every_other_cell_is_published_unchanged(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    eid_b = next(e for e, lu in view.lineups.items() if view.pool.by_role_id[lu[8]].team in ("CAR", "WPG", "VAN", "TBL") and e != E1)
    slot, out_r, in_r = _swap_down(view, eid_b, skater=False, same_team=False, team_in={"CAR", "WPG", "VAN", "TBL"})
    before = _cur(run)
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)]), E(eid_b, [C(slot, in_r, out_r)])], now=T_NJD_STARTED)
    by = {o.entry_id: o for o in res.entries}
    assert by[E1].status == "REJECTED" and by[E1].rule == "pinned" and "G is pinned" in by[E1].reason  # Allen's game has started
    assert by[eid_b].status == "APPLIED", by[eid_b].reason
    assert _diff(before, _cur(run)) == [(eid_b, 8)]  # nothing else moved, E1's locked goalie included


def test_nothing_is_published_when_every_edit_is_rejected(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    state = _state(run, outs)
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])], now=T_NJD_STARTED)
    assert res.published_version is None and res.entries[0].rule == "pinned" and "nothing published" in res.stop_reason
    assert _state(run, outs) == state


def test_a_player_whose_game_started_or_is_at_its_edit_stop_cannot_be_added_with_the_outgoing_game_open(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    labels = edits_mod.slot_labels(view.mode)
    k, out_r = next((k, r) for k, r in enumerate(view.lineups[E1]) if view.pool.by_role_id[r].team in ("TBL", "VAN")
                    and not view.pool.by_role_id[r].is_goalie)  # an open-game cell
    cand = next(r for r in view.pool.rows if r.team in ("NJD", "WSH") and slot_accepts(view.slots[k], r, view.mode) and _playing(view, r)
                and r.role_id not in view.lineups[E1] and r.person_key != view.pool.by_role_id[out_r].person_key)
    for now, word in ((T_TWO_STARTED, "has started"), (T_NJD_STOP, "edit stop")):
        res = _apply(run, runs, outs, [E(E1, [C(labels[k], cand.role_id, out_r)])], now=now)
        assert res.entries[0].rule == "not_addable" and word in res.entries[0].reason, res.entries[0].reason
        assert res.published_version is None


def test_an_open_game_edit_applies_while_another_entrys_locked_cells_stay_byte_identical(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    eid_b = next(e for e, lu in view.lineups.items() if view.pool.by_role_id[lu[8]].team in ("CAR", "WPG", "VAN", "TBL"))
    slot, out_r, in_r = _swap_down(view, eid_b, skater=False, same_team=False, team_in={"CAR", "WPG", "VAN", "TBL"})
    before = _cur(run)
    res = _apply(run, runs, outs, [E(eid_b, [C(slot, in_r, out_r)])], now=T_NJD_STARTED)
    assert res.entries[0].status == "APPLIED", res.entries[0].reason
    old, new = before.read_bytes().split(b"\r\n"), _cur(run).read_bytes().split(b"\r\n")
    eb = read_entries(before)
    changed_lines = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    assert changed_lines == [next(e.line_no for e in eb.entries if e.entry_id == eid_b)]  # one physical line, the others byte for byte
    ls = locks_mod.compute(eb, view.pool, None, T_NJD_STARTED, 300)
    locked = [(e, k) for (e, k), c in ls.cells.items() if c.pinned and e != eid_b]
    assert locked and all(_cells(before)[x] == _cells(_cur(run))[x] for x in locked)


def test_the_referee_itself_rejects_each_illegal_lineup(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    lu, labels = view.lineups[E2], edits_mod.slot_labels(view.mode)
    spent = sum(view.pool.by_role_id[r].salary for r in lu)

    def top(slot_k, pos_ok=True):
        return next(r for r in sorted(view.pool.rows, key=lambda r: -r.salary) if r.role_id not in lu
                    and slot_accepts(view.slots[slot_k], r, view.mode) == pos_ok and not r.is_goalie and _playing(view, r))

    k_c = view.slots.index("C")
    over = top(k_c)
    assert spent - view.pool.by_role_id[lu[k_c]].salary + over.salary > 50_000
    wrong = next(r for r in view.pool.rows if r.role_id not in lu and r.position == "D" and _playing(view, r))
    twice = lu[view.slots.index("W") + 1]  # a winger already in the lineup, put in a second cell
    # two skater teams: every skater of one team replaced by a cheaper skater of another team the lineup already holds
    teams = sorted({view.pool.by_role_id[r].team for r in lu if not view.pool.by_role_id[r].is_goalie})
    drop = teams[0]
    cells = []
    for k, r in enumerate(lu):
        row = view.pool.by_role_id[r]
        if row.team == drop and not row.is_goalie:
            repl = next(x for x in sorted(view.pool.rows, key=lambda x: x.salary) if x.team in teams[1:] and not x.is_goalie and _playing(view, x)
                        and slot_accepts(view.slots[k], x, view.mode) and x.role_id not in lu
                        and x.role_id not in {c["player_id"] for c in cells})
            cells.append(C(labels[k], repl.role_id, r))
    cases = {"over the 50000 cap": [C(labels[k_c], over.role_id, lu[k_c])], "not eligible": [C(labels[k_c], wrong.role_id, lu[k_c])],
             "same person more than once": [C(labels[view.slots.index("W") + 2], twice, lu[view.slots.index("W") + 2])],
             "2 team(s) represented": cells}
    state = _state(run, outs)
    for word, cs in cases.items():
        res = _apply(run, runs, outs, [E(E2, cs)])
        o = res.entries[0]
        assert o.status == "REJECTED" and o.rule == "referee" and word in o.reason, (word, o.rule, o.reason)
        assert res.published_version is None and _state(run, outs) == state  # the file is untouched
    # an id that is not in the pool is a command check (it cannot be written into a cell); the referee's own rule is proven directly
    res = _apply(run, runs, outs, [E(E2, [C("G", "99999999", THOMPSON)])])
    assert res.entries[0].rule == "unknown_player"
    bad = _cur(run).read_bytes().replace(f"({THOMPSON})".encode(), b"(99999999)", 1)
    p = tmp_path / "forged.csv"
    p.write_bytes(bad)
    rep = check_file(p, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    assert not rep.ok and any("is not in the salary file" in r for r in rep.reasons)


def test_an_out_or_ir_player_is_refused_by_the_command_not_the_referee(base, tmp_path):
    """The referee has no status input (flag 131): OUT and IR are read from the salary file's Status column."""
    run, runs, outs = _fresh(base, tmp_path)
    import csv
    import io

    sal = run.inputs / "DKSalaries.csv"
    rows = list(csv.reader(io.StringIO(sal.read_bytes().decode("utf-8-sig"), newline="")))
    col = rows[0].index("Status")
    for r in rows[1:]:
        if r[3] == KOCHETKOV:
            r[col] = "IR"
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    sal.write_bytes(b"\xef\xbb\xbf" + out.getvalue().encode("utf-8"))
    res = _apply(run, runs, outs, [E(E2, [C("G", KOCHETKOV, THOMPSON)])])
    assert res.entries[0].rule == "out" and "OUT or IR" in res.entries[0].reason and res.published_version is None


def test_a_multi_cell_edit_is_one_unit_and_a_13_entry_file_applies(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    # (a) a goalie change and the skater change it forces, in one edit on one entry
    g = ("G", KOCHETKOV, THOMPSON)
    s = _swap_down(view, E2, same_team=True)
    before = _cur(run)
    res = _apply(run, runs, outs, [E(E2, [C(g[0], g[1], g[2]), C(s[0], s[2], s[1])])])
    assert res.entries[0].status == "APPLIED", res.entries[0].reason
    assert [k for (e, k) in _diff(before, _cur(run))] == sorted({8, edits_mod.slot_labels(view.mode).index(s[0])})
    # (b) all 13 entries in one file: no per-round proposal limit and no swap-only limit
    run2, runs2, outs2 = _fresh(base, tmp_path / "b")
    view2 = _view(run2, runs2)
    all13 = []
    for eid in view2.lineups:
        slot, out_r, in_r = _swap_down(view2, eid, same_team=True)
        all13.append(E(eid, [C(slot, in_r, out_r)]))
    assert len(all13) == 13
    res2 = _apply(run2, runs2, outs2, all13)
    assert [o.status for o in res2.entries] == ["APPLIED"] * 13, [(o.entry_id, o.rule, o.reason) for o in res2.entries if o.status != "APPLIED"]
    assert len(_diff(run2.version_file(2), _cur(run2))) == 13


def test_the_goalie_exit_edits_apply_with_a_pinned_cell_elsewhere_in_the_same_entry(base, tmp_path):
    """After NJD@WSH starts, an entry that holds NJD skaters can still change its open-game goalie; its NJD cells stay put."""
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    eid = next(e for e, lu in view.lineups.items() if view.pool.by_role_id[lu[8]].team in ("CAR", "WPG", "VAN", "TBL")
               and any(view.pool.by_role_id[r].team in ("NJD", "WSH") for r in lu))
    slot, out_r, in_r = _swap_down(view, eid, skater=False, same_team=False, team_in={"CAR", "WPG", "VAN", "TBL"})
    before = _cur(run)
    res = _apply(run, runs, outs, [E(eid, [C(slot, in_r, out_r)])], now=T_NJD_STARTED)
    assert res.entries[0].status == "APPLIED", res.entries[0].reason
    ls = locks_mod.compute(read_entries(before), view.pool, None, T_NJD_STARTED, 300)
    pins = {k: r for k, r in ls.pinned(eid).items()}
    assert pins and all(_cells(_cur(run))[(eid, k)] == r for k, r in pins.items()) and _diff(before, _cur(run)) == [(eid, 8)]


# -- compare-and-swap, a source of BEN, the old paths ---------------------------------------------------------------

def test_a_stale_parent_is_refused_and_nothing_at_all_is_written(base, tmp_path, monkeypatch):
    """A late swap or QA round publishes between the model's read and its write: the edit must not advance the run."""
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    slot, out_r, in_r = _swap_down(view, E2, same_team=True)
    real = late_swap.splice_cells

    def racing(entries, pool, changes, out_path):
        data = real(entries, pool, changes, out_path)
        other = next(e for e in view.lineups if e != E2)  # the competing publish: another entry's cell, through the real publish
        s2, o2, i2 = _swap_down(view, other, same_team=True)
        rival = tmp_path / "rival.csv"
        bytes_ = real(entries, pool, {other: {edits_mod.slot_labels(view.mode).index(s2): i2}}, rival)
        rep = check_file(rival, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
        publish(run, bytes_, rep, read_manifest(run)["slate_id"], outputs_root=outs, expect_public_sha=sha256(_cur(run).read_bytes()))
        return data

    monkeypatch.setattr(late_swap, "splice_cells", racing)
    res = _apply(run, runs, outs, [E(E2, [C(slot, in_r, out_r)])])
    state_after_rival = _state(run, outs)
    monkeypatch.undo()
    assert res.published_version is None and "no longer this run's version" in res.stop_reason
    assert run.version_numbers() == [1, 2, 3] and run.current_version() == 3  # the rival's v3 only: the edit wrote no version
    assert _state(run, outs) == state_after_rival


def test_a_source_of_ben_is_rejected_in_any_form_and_nothing_applies(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    state = _state(run, outs)
    forms = [E(E1, [C("G", HILDEBY, ALLEN)], source="BEN"),
             E(E1, [C("G", HILDEBY, ALLEN)], sources=[{"url": "https://example.com/x", "source": "ben"}])]
    for bad in forms:
        res = _apply(run, runs, outs, [bad])
        assert res.entries[0].rule == "malformed" and "BEN" in res.entries[0].reason and res.published_version is None
    assert _state(run, outs) == state


def test_malformed_files_and_unknown_fields_change_nothing(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    state = _state(run, outs)
    res = edits_mod.apply_edits(run, "Sure! Here is what I would change", now=T_OPEN, runs_root=runs, outputs_root=outs, clock=lambda: T_OPEN)
    assert "malformed" in res.stop_reason and res.published_version is None
    for edit in (E(E1, [C("G", HILDEBY)], surprise=1), {k: v for k, v in E(E1, [C("G", HILDEBY)]).items() if k != "claimed_gain"},
                 E(E1, [C("G", HILDEBY)], edit_type="vibes"), E(E1, [C("G", "abc")]), E(E1, [C("W9", HILDEBY)]),
                 E(E1, [C("G", HILDEBY), C("G", KOCHETKOV)]), E("1", [C("G", HILDEBY)])):
        res = _apply(run, runs, outs, [edit])
        assert res.published_version is None and res.entries[0].status == "REJECTED"
    assert _state(run, outs) == state


def test_a_run_that_is_not_the_slates_delivery_is_refused(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    stamp = outs / read_manifest(run)["slate_id"] / "published.json"
    d = json.loads(stamp.read_text())
    d["run_id"] = "20261017-999999-classic"
    stamp.write_text(json.dumps(d))
    res = _apply(run, runs, outs, [E(E2, [C("G", KOCHETKOV, THOMPSON)])])
    assert res.published_version is None and "not the slate's current delivery" in res.stop_reason


def test_the_cli_prints_the_delivery_file_and_the_checks(base, tmp_path, capsys):
    from nhl_dfs import cli

    run, runs, outs = _fresh(base, tmp_path)
    f = tmp_path / "edits.json"
    f.write_text(json.dumps({"edits": [E(E2, [C("G", KOCHETKOV, THOMPSON)])]}))
    argv = ["edit-apply", "--run", run.run_id, "--runs-root", str(runs), "--outputs-root", str(outs), "--as-of", "2026-10-17T16:30:00Z",
            "--edits", str(f)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "EDITS_APPLIED=1 EDITS_REJECTED=0" in out and "FILE_VALID=TRUE" in out and "delivery_file=" in out
    assert "RISK_BUDGET=" in out and "OWN_GOALIE=" in out
    capsys.readouterr()
    assert cli.main(argv) == 1  # the same file again: Thompson is no longer in that cell
    assert "REJECTED [stale_view]" in capsys.readouterr().out


# -- the log, the counterfactual and restore (flag 128) ------------------------------------------------------------

def _two_goalie_edits():
    return [E(E1, [C("G", HILDEBY, ALLEN, team="TBL", name="Dennis Hildeby")]), E(E2, [C("G", KOCHETKOV, THOMPSON, team="CAR")])]


def test_each_edit_writes_a_log_row_and_the_engine_counterfactual_is_written_once(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    d = run.path / "edits"
    raw = json.dumps(_two_goalie_edits())
    res = edits_mod.apply_edits(run, raw, now=T_OPEN, runs_root=runs, outputs_root=outs, clock=lambda: T_OPEN)
    assert (d / "edits_1.json").read_text(encoding="utf-8") == raw  # the file as saved, verbatim
    assert hashlib.sha256(raw.encode()).hexdigest() == res.edits_sha256 == json.loads((d / "edits_1_result.json").read_text())["edits_sha256"]
    rows = [json.loads(x) for x in (d / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["entry_id"] for r in rows] == [E1, E2] and all(r["status"] == "APPLIED" for r in rows)
    r1 = rows[0]
    cell = r1["cells"][0]
    assert (cell["slot"], cell["from"], cell["to"]) == ("G", ALLEN, HILDEBY) and len(r1["cells"]) == 1
    assert "Jake Allen" in cell["from_who"] and "Dennis Hildeby" in cell["to_who"] and "TBL G, $8,200" in cell["to_who"]
    assert r1["reason"] and r1["evidence"] and r1["claimed_gain"] and r1["edit_type"] == "coverage"
    assert r1["parent_version"] == 2 and r1["published_version"] == 3 and r1["sha256"] == sha256(_cur(run).read_bytes())
    assert sorted(p.name for p in d.glob("engine_v*.csv")) == ["engine_v2.csv"]
    assert (d / "engine_v2.csv").read_bytes() == run.version_file(2).read_bytes()
    notes = (run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    assert "## Model edits (C56)" in notes and E1 in notes and "Dennis Hildeby" in notes and "claimed gain" in notes
    assert read_manifest(run)["versions"][-1]["phase"] == "EDIT1"
    # a second run of edits does not touch the counterfactual, and a rejected edit is logged too
    view = _view(run, runs)
    slot, out_r, in_r = _swap_down(view, "5298507706", same_team=True)
    res2 = _apply(run, runs, outs, [E("5298507706", [C(slot, in_r, out_r)]), E(E1, [C("G", ALLEN, HILDEBY)], reason="put him back")], now=T_NJD_STARTED)
    assert sorted(p.name for p in d.glob("engine_v*.csv")) == ["engine_v2.csv"]
    assert (d / "engine_v2.csv").read_bytes() == run.version_file(2).read_bytes()
    rows2 = [json.loads(x) for x in (d / "log.jsonl").read_text(encoding="utf-8").splitlines()][2:]
    assert {r["entry_id"]: (r["status"], r["rule"]) for r in rows2} == {"5298507706": ("APPLIED", ""), E1: ("REJECTED", "not_addable")}
    assert res2.published_version == 4


def test_restore_returns_the_previous_version_byte_for_byte_and_appends_history(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    v2 = run.version_file(2).read_bytes()
    _apply(run, runs, outs, _two_goalie_edits())
    v3 = _cur(run).read_bytes()
    res = _restore(run, runs, outs)
    assert res.published_version == 4 and run.version_numbers() == [1, 2, 3, 4]  # history is append-only
    assert _cur(run).read_bytes() == v2 and sha256(_cur(run).read_bytes()) == sha256(v2)
    d = Path(res.delivery_path)
    assert d.read_bytes() == v2 and d.name.split("_")[2][:8] == sha256(v2)[:8] and res.delivery_version == _published(outs, run)["delivery_version"]
    assert [o.entry_id for o in res.applied] == [E1, E2] and read_manifest(run)["versions"][-1]["phase"] == "RESTORE1"
    again = _restore(run, runs, outs)  # a restore of the restore goes forward again
    assert again.published_version == 5 and _cur(run).read_bytes() == v3
    assert "Model edits" in (run.path / "RUN_NOTES.md").read_text(encoding="utf-8")


def test_a_restore_that_would_add_a_started_players_cell_is_refused_even_when_the_other_cell_is_open(base, tmp_path):
    """Allen (NJD@WSH) was swapped for Hildeby (VAN@TBL). After 17:00Z Hildeby's cell is still open, but restoring would put Allen back."""
    run, runs, outs = _fresh(base, tmp_path)
    _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])])
    state = _state(run, outs)
    res = _restore(run, runs, outs, now=T_NJD_STARTED)
    assert res.published_version is None and "refused" in res.stop_reason and "Jake Allen" in res.stop_reason and E1 in res.stop_reason
    assert _state(run, outs) == state


def test_a_restore_that_would_change_a_pinned_cell_is_refused_naming_the_cell(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    eid = E1
    k = next(k for k, r in enumerate(view.lineups[eid]) if view.pool.by_role_id[r].team in ("TBL", "VAN") and not view.pool.by_role_id[r].is_goalie)
    cand = next(r for r in view.pool.rows if r.team in ("NJD", "WSH") and slot_accepts(view.slots[k], r, view.mode) and _playing(view, r)
                and r.role_id not in view.lineups[eid] and r.salary <= view.pool.by_role_id[view.lineups[eid][k]].salary)
    label = edits_mod.slot_labels(view.mode)[k]
    res = _apply(run, runs, outs, [E(eid, [C(label, cand.role_id, view.lineups[eid][k])])])
    assert res.entries[0].status == "APPLIED", res.entries[0].reason
    state = _state(run, outs)
    res = _restore(run, runs, outs, now=T_NJD_STARTED)  # the added NJD or WSH player's game has started: his cell is pinned now
    assert res.published_version is None and "pinned now" in res.stop_reason and f"entry {eid} {label}" in res.stop_reason
    assert _state(run, outs) == state


def test_a_restore_with_nothing_to_undo_says_so(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    res = _restore(run, runs, outs)
    assert res.published_version is None and "no previous edit version" in res.stop_reason


def test_the_cli_restores_the_delivery_file(base, tmp_path, capsys):
    from nhl_dfs import cli

    run, runs, outs = _fresh(base, tmp_path)
    _apply(run, runs, outs, _two_goalie_edits())
    argv = ["--run", run.run_id, "--runs-root", str(runs), "--outputs-root", str(outs), "--as-of", "2026-10-17T16:30:00Z"]
    capsys.readouterr()
    assert cli.main(["edit-restore", *argv]) == 0
    out = capsys.readouterr().out
    assert "EDIT_RESTORE" in out and "delivery_file=" in out and "FILE_VALID=TRUE" in out


# -- the two checks the model may override (flag 125) ----------------------------------------------------------------

def _facing_swap(view, eid):
    """(slot label, out id, in id): put a skater of the entry's own goalie's opponent in the lineup, legal on cap and teams."""
    from nhl_dfs.build import own_goalie as og

    opp = og.opponent_map(view.pool)
    lu = view.lineups[eid]
    goalie = next(view.pool.by_role_id[r] for r in lu if view.pool.by_role_id[r].is_goalie)
    labels = edits_mod.slot_labels(view.mode)
    for k, rid in enumerate(lu):
        row = view.pool.by_role_id[rid]
        if row.is_goalie:
            continue
        for cand in sorted(view.pool.rows, key=lambda r: (-r.salary, r.role_id)):
            if cand.team != opp[goalie.team] or cand.is_goalie or cand.role_id in lu or cand.salary > row.salary or not _playing(view, cand) \
                    or not slot_accepts(view.slots[k], cand, view.mode):
                continue
            new = list(lu)
            new[k] = cand.role_id
            if len({view.pool.by_role_id[r].team for r in new if not view.pool.by_role_id[r].is_goalie}) >= 3:
                return labels[k], rid, cand.role_id
    raise AssertionError("no facing swap")


def test_an_own_goalie_breach_needs_an_override_and_prints_it(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    m = read_manifest(run)
    m["families"] = {e: "large_gpp" for e in _view(run, runs).lineups}  # the rule covers these contests
    write_manifest(run, m)
    view = _view(run, runs)
    slot, out_r, in_r = _facing_swap(view, E1)
    cell = [C(slot, in_r, out_r)]
    state = _state(run, outs)
    res = _apply(run, runs, outs, [E(E1, cell)])
    assert res.entries[0].rule == "own_goalie" and "needs an override" in res.entries[0].reason and _state(run, outs) == state
    wrong_rule = _apply(run, runs, outs, [E(E1, cell, override={"rule": "risk_budget", "reason": "the budget is not what this is about"})])
    assert wrong_rule.entries[0].rule == "own_goalie"  # an override for another rule does not excuse this one
    ok = _apply(run, runs, outs, [E(E1, cell, override=RULE)])
    assert ok.entries[0].status == "APPLIED" and ok.entries[0].flags[0].startswith("OVERRIDE(own_goalie)") and RULE["reason"] in ok.entries[0].flags[0]
    assert read_manifest(run)["statuses"]["OWN_GOALIE"].startswith("RELAXED(")  # reported, not hidden
    assert "OVERRIDE(own_goalie)" in "\n".join(ok.lines())
    short = _apply(run, runs, outs, [E(E2, [C("G", KOCHETKOV, THOMPSON)], override={"rule": "own_goalie", "reason": "too short"})])
    assert short.entries[0].rule == "malformed" and "at least 5 words" in short.entries[0].reason


def test_a_risk_budget_breach_needs_an_override_and_an_existing_breach_is_not_blamed_on_the_edit(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    m = read_manifest(run)
    m["scenario"] = {"skipped": "test stub: no scenario pass", "caps": {"goalie_fee_share": None, "goalie_lineups": 6, "game_fee_share": None, "game_lineups": None}}  # the engine's own cap
    write_manifest(run, m)
    to_allen = [C("G", ALLEN, THOMPSON)]  # Allen would be in 7 lineups, over the cap of 6 and over the 6 it has now
    state = _state(run, outs)
    res = _apply(run, runs, outs, [E(E2, to_allen)])
    assert res.entries[0].rule == "risk_budget" and "goalie entries 7.00 > the run's cap 6.00 (was 6.00)" in res.entries[0].reason
    assert _state(run, outs) == state
    ok = _apply(run, runs, outs, [E(E2, to_allen, override={"rule": "risk_budget", "reason": "the extra Allen entry is the best first place shot"})])
    assert ok.entries[0].status == "APPLIED" and any(f.startswith("BREACHED(risk_budget)") for f in ok.entries[0].flags)
    assert read_manifest(run)["statuses"].get("RISK_BUDGET", "").startswith(f"BREACHED(edit v{ok.published_version}")
    # a portfolio the engine already left above a smaller cap: an edit that lowers or keeps it is never blamed
    run2, runs2, outs2 = _fresh(base, tmp_path / "b")
    m = read_manifest(run2)
    m["scenario"] = {"skipped": "test stub: no scenario pass", "caps": {"goalie_fee_share": None, "goalie_lineups": 4, "game_fee_share": None, "game_lineups": None}}
    write_manifest(run2, m)
    fine = _apply(run2, runs2, outs2, [E(E1, [C("G", HILDEBY, ALLEN)])])
    assert fine.entries[0].status == "APPLIED" and not any(f.startswith(("BREACHED", "OVERRIDE")) for f in fine.entries[0].flags)
    # without recorded caps the budget is not evaluated and the edit proceeds
    run3, runs3, outs3 = _fresh(base, tmp_path / "c")
    assert _apply(run3, runs3, outs3, [E(E2, to_allen)]).entries[0].status == "APPLIED"


# -- the B150 guardrails (flag 130) -------------------------------------------------------------------------------------

def _news(run, role_id, effective):
    (run.path / "news").mkdir(exist_ok=True)
    (run.path / "news" / "accepted_overrides.json").write_text(json.dumps([{
        "type": "override", "role_id": role_id, "nhl_id": None, "game_id": None, "field": "goalie_start", "old": False, "new": True,
        "effective_utc": effective.isoformat(), "expiry_utc": (effective + timedelta(hours=8)).isoformat(),
        "source_url": "https://www.dailyfaceoff.com/starting-goalies", "claim": "confirmed starter", "confidence": 0.9, "source": "DAILYFACEOFF"}]))


def test_a_news_edit_needs_a_dated_source_that_is_not_older_than_the_news_it_contradicts(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    _news(run, HILDEBY, T_OPEN - timedelta(minutes=30))  # an accepted override for Hildeby, effective 16:00Z
    cell = [C("G", HILDEBY, ALLEN)]
    stale = {"url": "https://example.com/hildeby", "published_utc": "2026-10-17T15:00:00Z", "fetched_utc": "2026-10-17T16:25:00Z"}
    fresh = {"url": "https://example.com/hildeby", "published_utc": "2026-10-17T16:10:00Z", "fetched_utc": "2026-10-17T16:25:00Z"}
    undated = _apply(run, runs, outs, [E(E1, cell, edit_type="news", source_url="https://example.com/hildeby")])
    assert undated.entries[0].rule == "malformed" and "published_utc" in undated.entries[0].reason
    old = _apply(run, runs, outs, [E(E1, cell, edit_type="news", sources=[stale])])
    assert old.entries[0].rule == "source_stale" and "older than the news it contradicts" in old.entries[0].reason and old.published_version is None
    new = _apply(run, runs, outs, [E(E1, cell, edit_type="news", sources=[fresh])])
    assert new.entries[0].status == "APPLIED"
    assert json.loads((run.path / "edits" / "log.jsonl").read_text().splitlines()[-1])["sources"][0]["published_utc"].startswith("2026-10-17T16:10")


def test_a_wrong_id_is_caught_when_the_edit_says_who_it_expects(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    state = _state(run, outs)
    for extra, word in (({"team": "VAN"}, "expected team VAN"), ({"name": "Pyotr Kochetkov"}, "expected Pyotr Kochetkov")):
        res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN, **extra)])])
        assert res.entries[0].rule == "identity" and word in res.entries[0].reason and "wrong id" in res.entries[0].reason
    assert _state(run, outs) == state
    shown = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])]).entries[0].cells[0]  # with no guard the identity is still printed
    assert shown["in_who"].startswith("Dennis Hildeby (TBL G, $8,200, VAN@TBL)") and shown["out_who"].startswith("Jake Allen (NJD G, $7,600")


def test_a_goalie_who_is_not_confirmed_is_flagged_and_queued_for_a_recheck(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    pk = _view(run, runs).pool.by_role_id[HILDEBY].person_key
    m = read_manifest(run)
    m["goalies"] = {"GOALIE_GATE": "CLEAR", "page": {}, "problems": [], "rows": [], "persons": {pk: {"status": "EXPECTED", "name": "Dennis Hildeby", "team": "TBL"}}}
    write_manifest(run, m)
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])])
    assert res.entries[0].status == "APPLIED" and res.entries[0].flags[0].startswith("UNCONFIRMED_GOALIE: Dennis Hildeby (TBL) is EXPECTED")
    q = json.loads((run.path / "edits" / "recheck.json").read_text())
    assert q[0]["recheck_before_utc"] == "2026-10-17T19:55:00Z" and q[0]["goalie"] == "Dennis Hildeby"  # VAN@TBL 20:00Z less the 300 s edit stop
    assert any(l.startswith("RECHECK_BEFORE=2026-10-17T19:55:00Z") for l in res.lines())
    run2, runs2, outs2 = _fresh(base, tmp_path / "b")
    m = read_manifest(run2)
    m["goalies"] = {"GOALIE_GATE": "CLEAR", "page": {}, "problems": [], "rows": [], "persons": {pk: {"status": "CONFIRMED"}}}
    write_manifest(run2, m)
    ok = _apply(run2, runs2, outs2, [E(E1, [C("G", HILDEBY, ALLEN)])])
    assert not ok.entries[0].flags and not (run2.path / "edits" / "recheck.json").exists()


# -- the edit stop per game; QA's T-8 does not apply (flag 126) -----------------------------------------------------

def test_t8_blocks_qa_but_not_an_edit_and_the_edit_stop_is_per_game(base, tmp_path):
    t = datetime(2026, 10, 17, 16, 53, 30, tzinfo=UTC)  # T-8 of NJD@WSH (16:52Z) has passed; its edit stop (16:55Z) has not
    run, runs, outs = _fresh(base, tmp_path)
    ok, why = controller.permitted(run, 1, packet.load_qa_config(), t, runs)
    assert not ok and "T-8" in why  # QA is closed
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])], now=t)
    assert res.entries[0].status == "APPLIED", res.entries[0].reason  # the edit is not
    # at 16:57Z NJD@WSH is inside its edit stop: Allen's cell is pinned, a cell between two other games is not
    run2, runs2, outs2 = _fresh(base, tmp_path / "b")
    view = _view(run2, runs2)
    slot, out_r, in_r = _two_game_edit(view, "5298507706")
    res2 = _apply(run2, runs2, outs2, [E(E1, [C("G", HILDEBY, ALLEN)]), E("5298507706", [C(slot, in_r, out_r)])], now=T_NJD_STOP)
    by = {o.entry_id: o for o in res2.entries}
    assert by[E1].rule == "pinned" and "edit stop" in by[E1].reason and by["5298507706"].status == "APPLIED", by["5298507706"].reason
    # at 19:45Z only VAN@TBL is open: a TBL cell may change, a CAR cell may not
    run3, runs3, outs3 = _fresh(base, tmp_path / "c")
    view3 = _view(run3, runs3)
    tbl = _swap_down(view3, E1, same_team=True, exclude_teams={"NJD", "WSH", "CAR", "WPG"})
    car_e = next(e for e, lu in view3.lineups.items() if any(view3.pool.by_role_id[r].team in ("CAR", "WPG") and not view3.pool.by_role_id[r].is_goalie for r in lu))
    car = _swap_down(view3, car_e, same_team=True, exclude_teams={"NJD", "WSH", "TBL", "VAN"})
    res3 = _apply(run3, runs3, outs3, [E(E1, [C(tbl[0], tbl[2], tbl[1])]), E(car_e, [C(car[0], car[2], car[1])])], now=T_TWO_STARTED)
    by3 = {o.entry_id: o for o in res3.entries}
    assert by3[E1].status == "APPLIED", by3[E1].reason
    assert by3[car_e].rule == "pinned"


def test_a_boundary_crossed_while_the_edit_is_being_checked_publishes_nothing(base, tmp_path):
    """Read at 16:54:30Z (open); by the time the publish lock is held the clock says 16:55:30Z, NJD@WSH is at its edit stop."""
    run, runs, outs = _fresh(base, tmp_path)
    state = _state(run, outs)
    t0, t1 = datetime(2026, 10, 17, 16, 54, 30, tzinfo=UTC), datetime(2026, 10, 17, 16, 55, 30, tzinfo=UTC)
    res = _apply(run, runs, outs, [E(E1, [C("G", HILDEBY, ALLEN)])], now=t0, clock=lambda: t1)
    assert res.published_version is None and "lock boundary was crossed" in res.stop_reason
    assert _state(run, outs) == state


def test_a_delivery_that_touches_a_game_about_to_lock_says_so(base, tmp_path):
    run, runs, outs = _fresh(base, tmp_path)
    view = _view(run, runs)
    now = datetime(2026, 10, 17, 19, 10, tzinfo=UTC)  # CAR@WPG locks at 19:30Z, 20 minutes away
    car_e = next(e for e, lu in view.lineups.items() if any(view.pool.by_role_id[r].team in ("CAR", "WPG") and not view.pool.by_role_id[r].is_goalie for r in lu))
    slot, out_r, in_r = _swap_down(view, car_e, same_team=True, exclude_teams={"NJD", "WSH", "TBL", "VAN"})
    res = _apply(run, runs, outs, [E(car_e, [C(slot, in_r, out_r)])], now=now)
    assert res.entries[0].status == "APPLIED", res.entries[0].reason
    assert any(l.startswith("EDIT_LOCK_SOON=CAR@WPG locks 2026-10-17T19:30:00Z (in 20 min)") for l in res.lines()), res.lines()


# -- the real 10-17 bytes, when the full files are on this machine -------------------------------------------------------

def _real_files():
    d = Path(os.environ.get("NHL_DFS_C56_REAL_DIR", REAL))
    for sal, ent in (("DKSalaries_20261017.csv", "DKEntries_20261017.csv"), ("DKSalaries_nhl_classic.csv", "DKEntries_nhl_classic.csv")):
        if (d / sal).exists() and (d / ent).exists():
            return d / sal, d / ent
    return None


def test_the_two_edits_on_the_real_10_17_v3_bytes(tmp_path):
    """The exit check on the file that was delivered on 2026-10-17 (v3, 82d7107e). Skips loudly when the full files are absent
    (they are personal and gitignored: put them in tests/fixtures/real/ as DKSalaries_20261017.csv and DKEntries_20261017.csv,
    or point NHL_DFS_C56_REAL_DIR at a folder that holds them)."""
    files = _real_files()
    if files is None:
        pytest.skip("C56: the full 2026-10-17 DKSalaries and DKEntries files are not on this machine; the exit check ran on the minimized fixture only")
    sal, ent = files
    r = run_slate(sal, ent, offline=True, baseline_only=True, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                  clock=lambda: T_OPEN - timedelta(hours=1))
    assert r.ok, r.manifest["failed"]
    run, runs, outs = r.run, tmp_path / "runs", tmp_path / "outputs"
    entered = TESTS.parent / "data" / "entered" / "classic-20261017-2529349813.csv"
    if not entered.exists():
        pytest.skip("C56: the entered record is not on this machine")
    pool = packet.RunView(run, runs).pool
    lineups = {}
    import csv

    for row in list(csv.reader(entered.open(newline="", encoding="utf-8-sig")))[1:]:
        ids = [c.rsplit("(", 1)[1].rstrip(") ") for c in row[4:13]]
        perm = (0, 1, 2, 3, 4, 5, 6, 8, 7)  # the template's order (C C W W W D D G UTIL) to canonical slots
        lineups[row[0]] = {perm[i]: ids[i] for i in range(9)}
    tmp_file = tmp_path / "v3.csv"
    ef = read_entries(run.inputs / "DKEntries.csv")
    data = late_swap.splice_cells(ef, pool, lineups, tmp_file)
    assert hashlib.sha256(data).hexdigest().startswith("82d7107e")  # the delivered v3, reproduced from the two files
    rep = check_file(tmp_file, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    assert rep.ok
    pub = publish(run, data, rep, read_manifest(run)["slate_id"], outputs_root=outs,
                  expect_public_sha=sha256(run.version_file(1).read_bytes()))
    m = read_manifest(run)
    m["versions"].append({"version": pub.version, "phase": "ENTERED", "sha256": rep.out_sha256, "path": str(pub.version_path),
                          "public_replaced": True, "public_detail": "", "delivery_version": pub.delivery_version,
                          "delivery_path": str(pub.delivery_path), "created_utc": T_OPEN.isoformat()})
    write_manifest(run, m)
    res = _apply(run, runs, outs, _two_goalie_edits())
    assert [o.status for o in res.entries] == ["APPLIED", "APPLIED"], [(o.rule, o.reason) for o in res.entries]
    assert [o.total for o in res.entries] == [49_200, 49_700]
    assert Path(res.delivery_path).exists() and res.published_version == pub.version + 1
