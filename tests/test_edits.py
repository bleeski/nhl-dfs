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
