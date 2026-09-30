"""Deterministic QA controller (C10; plan section 9, "Acceptance rule").

The adversary and the researcher only propose. This module validates, applies, re-solves, compares and
accepts or rejects, and it alone decides whether another round may run:

- Malformed JSON ends QA and keeps the incumbent. At most `max_proposals` per round; the rest are rejected.
- A proposal touching a pinned cell (LOCKED or EDIT_STOP now) or adding a player who may not be added is
  rejected.
- correctness: an override (models.overrides.Override) that passes overrides.validate on the run's role
  state, with confidence >= correctness_min_confidence and an http(s) source. Accepted WITHOUT a simulation
  contest. Effects: participation OUT excludes the person; goalie_start (a confirmation) makes the team's
  other goalies non-starters, so every open cell holding one is repaired (backlog B17 on this path); other
  fields are recorded for the next refresh. Repairs change as few cells as possible (late swap's fast repair).
- strategic: `swap` (one entry, one role for another) or `exclude` (a person out of every open cell, each
  entry repaired). Requires legality, the risk budget, and on paired draws (the same selection draws before
  and after, then this round's own referee block) a family-utility gain beyond the tie band with no loss in
  the tail (top-1% payout) or safety (portfolio P(lose >= 80%)) metric beyond its standard error. No
  scenario cache: rejected ("no scenarios to compare on"). Under FIELD_CALIBRATION=PRIOR an accepted
  change is recorded as an unvalidated modeled improvement.
- Rounds: 1 by default; 2 and 3 only after the previous round accepted a correctness repair; never inside
  T-8 of the earliest open game. Every round is recorded in runs/<id>/qa/round_<k>.json with the sha256 of
  the proposals file as saved.
Changed lineups are spliced cell by cell, checked by the referee with the pinned cells declared, and
published as the run's next version (compare-and-swap on the public file). Nothing here edits a CSV by hand.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np

from nhl_dfs.build import packet as packet_mod
from nhl_dfs.contracts.geometry import Mode, check_lineup, slot_accepts
from nhl_dfs.contracts.statuses import Participation


class MalformedProposals(ValueError):
    pass


@dataclass
class Proposal:
    kind: Literal["correctness", "strategic"]
    target: dict
    change: dict
    evidence: str
    source_url: str | None


@dataclass
class RoundResult:
    run_id: str
    round: int
    packet_id: str | None
    decisions: list[dict] = field(default_factory=list)
    accepted_correctness: int = 0
    accepted_strategic: int = 0
    published_version: int | None = None
    another_round: bool = False
    stop_reason: str = ""
    record_path: str | None = None
    proposals_sha256: str | None = None

    def lines(self) -> list[str]:
        out = [f"QA round {self.round}: {self.accepted_correctness} correctness and {self.accepted_strategic} strategic "
               f"change(s) accepted, {sum(d['status'] == 'rejected' for d in self.decisions)} rejected"]
        for d in self.decisions:
            out.append(f"  #{d['index']} {d['kind']} {d.get('type', '?')}: {d['status'].upper()}: {d['reason']}")
        out.append(f"published: v{self.published_version}" if self.published_version else "published: nothing (incumbent kept)")
        out.append(f"ANOTHER_ROUND={'YES' if self.another_round else 'NO'}: {self.stop_reason}")
        return out


# -- parsing ------------------------------------------------------------------------------------------------

def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def parse(text: str) -> tuple[list[Proposal], str | None]:
    """Strict: a JSON object {"packet_id": ..., "proposals": [...]} (a bare list is accepted too). A single
    ``` fence around it is tolerated; anything else is malformed."""
    try:
        doc = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedProposals(f"not JSON ({exc})") from None
    pid = None
    if isinstance(doc, dict):
        pid = doc.get("packet_id")
        doc = doc.get("proposals")
    if not isinstance(doc, list):
        raise MalformedProposals("expected a list of proposals")
    out = []
    for i, p in enumerate(doc):
        if not isinstance(p, dict):
            raise MalformedProposals(f"proposal {i} is not an object")
        kind = p.get("kind")
        if kind not in ("correctness", "strategic"):
            raise MalformedProposals(f"proposal {i}: kind must be correctness or strategic")
        if not isinstance(p.get("change"), dict) or not isinstance(p.get("target", {}), dict):
            raise MalformedProposals(f"proposal {i}: change and target must be objects")
        out.append(Proposal(kind, dict(p.get("target") or {}), dict(p["change"]), str(p.get("evidence") or ""),
                            p.get("source_url") if isinstance(p.get("source_url"), str) else None))
    return out, pid


def _dt(x) -> datetime:
    if isinstance(x, datetime):
        return x.astimezone(timezone.utc)
    t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def override_from(d: dict):
    """An models.overrides.Override from a proposal's change (or a researcher's override object)."""
    from nhl_dfs.models.overrides import Override

    return Override(str(d["role_id"]), int(d["nhl_id"]) if d.get("nhl_id") not in (None, "") else None,
                    d.get("game_id"), str(d["field"]), d.get("old"), d.get("new"), _dt(d["effective_utc"]),
                    str(d.get("source_url") or ""), str(d.get("claim") or ""), float(d.get("confidence", -1)),
                    _dt(d["expiry_utc"]))


# -- round bookkeeping --------------------------------------------------------------------------------------

def qa_dir(run) -> Path:
    d = run.path / "qa"
    d.mkdir(exist_ok=True)
    return d


def permitted(run, round_no: int, cfg: dict, now: datetime, runs_root=None) -> tuple[bool, str]:
    """May round `round_no` run now? Round 1 once; round k > 1 only after round k-1 accepted a correctness
    repair; never past max_rounds or inside T-8 of the earliest open game."""
    c = cfg["controller"]
    if round_no < 1 or round_no > int(c["max_rounds"]):
        return False, f"round {round_no} is outside 1..{c['max_rounds']}"
    rec = qa_dir(run) / f"round_{round_no}.json"
    if rec.exists():
        return False, f"round {round_no} was already applied ({rec.name})"
    if round_no > 1:
        prev = qa_dir(run) / f"round_{round_no - 1}.json"
        if not prev.exists():
            return False, f"round {round_no - 1} has not been applied"
        p = json.loads(prev.read_text(encoding="utf-8"))
        if not p.get("another_round"):
            return False, f"round {round_no - 1} did not permit another round ({p.get('stop_reason', '')})"
    v = packet_mod.RunView(run, runs_root)
    dl = v.qa_deadline(now, float(c["llm_stop_min"]))
    if dl is None:
        return False, "every game has started or is inside the edit stop: nothing open to review"
    if now >= dl:
        return False, f"T-{c['llm_stop_min']} deadline passed ({dl:%H:%MZ}): QA skipped, the checked file stands"
    return True, f"permitted until {dl:%H:%MZ} (T-{c['llm_stop_min']})"


# -- evaluation on paired draws -------------------------------------------------------------------------------

def _portfolio(so, lineups: dict, contest_of: dict, purpose: str, contests: list[str]):
    """Per contest: (entry ids, joint Metrics) for the listed contests."""
    out = {}
    for cid in contests:
        eids = [e for e in lineups if contest_of[e] == cid and all(lineups[e])]
        out[cid] = (eids, so.joint(cid, [lineups[e] for e in eids], purpose)) if eids else ([], None)
    return out


def evaluate(so, before: dict, after: dict, changed: set[str], contest_of: dict, fees: dict, risk_cfg: dict,
             mode: Mode) -> tuple[bool, dict]:
    """Paired comparison of two portfolios differing in `changed` entries: the same selection draws, then this
    round's referee block. Returns (accept, figures)."""
    from nhl_dfs.build import tiebreak

    touched = sorted({contest_of[e] for e in changed})
    others = sorted({contest_of[e] for e in before} - set(touched))
    fees_c = int(round(sum(fees.values()) * 100))
    limit = float(risk_cfg["budget"][mode.value]["p_lose80_max"])
    figs: dict[str, Any] = {}
    ok = True
    for purpose in ("selection", "referee"):
        if not all(c in so.cache.contests for c in touched):
            return False, {"reason": "a changed entry's contest is not in the scenario cache"}
        base_pay = 0
        for cid, (eids, m) in _portfolio(so, before, contest_of, purpose, [c for c in others if c in so.cache.contests]).items():
            if m is not None:
                base_pay = base_pay + m.payout_cents.astype(np.int64).sum(axis=1)
        tot, util, tail, tail_se = {}, {}, {}, {}
        for side, lus in (("before", before), ("after", after)):
            pay = base_pay
            u = 0
            t = t_se2 = 0.0
            for cid, (eids, m) in _portfolio(so, lus, contest_of, purpose, touched).items():
                pay = pay + m.payout_cents.astype(np.int64).sum(axis=1)
                idx = [j for j, e in enumerate(eids) if e in changed]
                u = u + m.utility_cents[:, idx].astype(np.int64).sum(axis=1)
                t += float(m.exp_payout_top1pct[idx].sum())
                t_se2 += float((m.exp_payout_top1pct_se[idx] ** 2).sum())
            tot[side], util[side], tail[side], tail_se[side] = np.asarray(pay), np.asarray(u), t, math.sqrt(t_se2)
        S = len(util["before"])
        d = (util["after"] - util["before"]) / 100.0
        gain, se = float(d.mean()), float(d.std() / math.sqrt(max(1, S)))
        anchor = float(util["before"].mean() / 100.0)
        band = tiebreak.band_width(anchor, se, risk_cfg)
        p = {s: float((tot[s] <= 0.2 * fees_c).mean()) for s in tot}
        p_se = math.sqrt(max(p["before"] * (1 - p["before"]), 1e-12) / max(1, S))
        tail_ok = tail["after"] >= tail["before"] - math.hypot(tail_se["before"], tail_se["after"])
        safe_ok = p["after"] <= max(limit, p["before"]) + p_se
        gain_ok = gain > band if purpose == "selection" else gain >= -se
        figs[purpose] = {"scenarios": S, "utility_gain": round(gain, 4), "gain_se": round(se, 4), "band": round(band, 4),
                         "tail_before": round(tail["before"], 4), "tail_after": round(tail["after"], 4),
                         "p_lose80_before": round(p["before"], 4), "p_lose80_after": round(p["after"], 4),
                         "p_lose80_limit": limit, "gain_ok": gain_ok, "tail_ok": tail_ok, "safety_ok": bool(safe_ok)}
        ok = ok and gain_ok and tail_ok and bool(safe_ok)
        if not ok:
            break
    return ok, figs


# -- the round ----------------------------------------------------------------------------------------------

def apply_round(run, round_no: int, proposals, cfg: dict | None = None, *, now: datetime | None = None,
                runs_root=None, outputs_root=None, apply_state: Callable | None = None,
                clock: Callable[[], datetime] | None = None) -> RoundResult:
    """proposals: a path to the reply saved verbatim, or its text."""
    from nhl_dfs.build import late_swap, swap_objective
    from nhl_dfs.build.manifest import read_manifest, write_manifest
    from nhl_dfs.build.notes import write_run_notes
    from nhl_dfs.build.objectives import load_risk_config
    from nhl_dfs.build.run import load_runtime_config, pool_without
    from nhl_dfs.build.state import LockTimeout, PublishRefused, publish, sha256
    from nhl_dfs.contracts.statuses import FieldCalibration
    from nhl_dfs.models import overrides as overrides_mod
    from nhl_dfs.referee.check_file import check_file

    cfg = cfg or packet_mod.load_qa_config()
    cc = cfg["controller"]
    clock = clock or (lambda: now or datetime.now(timezone.utc))
    now = now or clock()
    runs_root = Path(runs_root) if runs_root is not None else run.path.parent
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    if isinstance(proposals, Path) or (isinstance(proposals, str) and len(proposals) < 400 and Path(proposals).exists()):
        path = Path(proposals)
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        path, text = None, str(proposals)
        raw = text.encode("utf-8")
    res = RoundResult(run.run_id, int(round_no), None, proposals_sha256=hashlib.sha256(raw).hexdigest())
    t0 = time.perf_counter()

    def record() -> RoundResult:
        rec = {**asdict(res), "proposals_path": str(path) if path else None, "created_utc": now.isoformat(),
               "elapsed_s": round(time.perf_counter() - t0, 3)}
        p = qa_dir(run) / f"round_{round_no}.json"
        p.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        res.record_path = str(p)
        return res

    ok, why = permitted(run, round_no, cfg, now, runs_root)
    if not ok:
        res.stop_reason = why
        if "already applied" in why:
            return res  # never overwrite an earlier round's record
        return record()
    try:
        props, res.packet_id = parse(text)
    except MalformedProposals as exc:
        res.stop_reason = f"malformed proposals ({exc}): QA ends, the incumbent is kept"
        return record()

    v = packet_mod.RunView(run, runs_root)
    pool, mode = v.pool, v.mode
    rt = load_runtime_config()
    ls = v.locks(now)
    pins = {eid: ls.pinned(eid) for eid in v.lineups}
    risk_cfg = load_risk_config()
    cur = {e: list(lu) for e, lu in v.lineups.items()}
    excluded_people: dict[str, str] = {}  # person_key -> reason (correctness)
    so = None
    so_note = ""
    c = v.cache
    if c is not None:
        blocks = max(1, int(cc["referee_blocks"]))
        size = c.n("referee") // blocks
        rows = ((round_no - 1) * size, round_no * size) if size else None
        try:
            so = swap_objective.ScenarioObjective(c, pool=pool, work=pool, rm=None, st=v.st, started_games=ls.started_games,
                                                  n_use=int(rt.get("late_swap", {}).get("full_scenarios", 8000)), now=now,
                                                  odds_snapshot=None, notes=[], referee_rows=rows)
        except Exception as exc:
            so_note = f"scenario cache unusable ({type(exc).__name__}: {str(exc)[:100]})"
    else:
        so_note = "no scenario cache on the run or its parents"
    rs_box: dict[str, Any] = {}

    def role_state():
        if "rs" not in rs_box:
            work = pool_without(pool, {r for r, (p, _) in v.st.items() if p is Participation.OUT})
            rm = swap_objective.build_role_model(pool, work, v.st, dk_rec=None, now=now, clock=lambda: now, offline=True,
                                                 cache=None, budget_s=float(rt["network_pass_budget_s"]), apply_state=apply_state)
            rs_box["rs"] = rm.roles
        return rs_box["rs"]

    def pinned_rows(eid):
        return set(pins[eid].values())

    def reject(i, p, typ, reason):
        res.decisions.append({"index": i, "kind": p.kind, "type": typ, "status": "rejected", "reason": reason,
                              "target": p.target, "change": p.change})

    def accept(i, p, typ, reason, **extra):
        res.decisions.append({"index": i, "kind": p.kind, "type": typ, "status": "accepted", "reason": reason,
                              "target": p.target, "change": p.change, **extra})

    accepted_overrides = []
    for i, p in enumerate(props):
        typ = str(p.change.get("type", ""))
        if i >= int(cc["max_proposals"]):
            reject(i, p, typ, f"over the per-round limit of {cc['max_proposals']} proposals")
            continue
        try:
            if p.kind == "correctness":
                if typ != "override":
                    reject(i, p, typ, "a correctness change must be an override (docs/proposal_schema.md)")
                    continue
                try:
                    o = override_from({**p.change, "source_url": p.change.get("source_url") or p.source_url or ""})
                except (KeyError, TypeError, ValueError) as exc:
                    reject(i, p, typ, f"override fields missing or malformed ({type(exc).__name__}: {str(exc)[:80]})")
                    continue
                if o.role_id not in pool.by_role_id:
                    reject(i, p, typ, f"unknown role_id {o.role_id}")
                    continue
                errs = overrides_mod.validate(o, role_state(), now)
                if errs:
                    reject(i, p, typ, "; ".join(errs))
                    continue
                if o.confidence < float(cc["correctness_min_confidence"]):
                    reject(i, p, typ, f"confidence {o.confidence:g} below {cc['correctness_min_confidence']:g}")
                    continue
                row = pool.by_role_id[o.role_id]
                effect = "recorded for the next refresh (model input only)"
                if o.field == "participation" and o.new == "OUT":
                    excluded_people[row.person_key] = f"override OUT ({o.claim[:60]})"
                    effect = f"{row.name} excluded; open cells holding him are repaired"
                elif o.field == "goalie_start" and o.new is True:
                    for r in pool.rows:
                        if r.is_goalie and r.team == row.team and r.person_key != row.person_key:
                            excluded_people[r.person_key] = f"not the confirmed {row.team} starter ({row.name} confirmed)"
                    effect = f"{row.name} confirmed; open cells holding another {row.team} goalie are repaired"
                accepted_overrides.append({**p.change, "role_id": o.role_id, "source_url": o.source_url})
                accept(i, p, typ, f"verified override: {effect}")
                res.accepted_correctness += 1
                continue
            # strategic
            if so is None:
                reject(i, p, typ, f"no scenarios to compare on ({so_note})")
                continue
            new = {e: list(lu) for e, lu in cur.items()}
            changed: set[str] = set()
            if typ == "swap":
                eid, out_r, in_r = str(p.change.get("entry_id") or p.target.get("entry_id")), str(p.change.get("out_role_id")), \
                    str(p.change.get("in_role_id"))
                if eid not in cur or out_r not in cur[eid]:
                    reject(i, p, typ, f"entry {eid} does not hold {out_r}")
                    continue
                if out_r in pinned_rows(eid):
                    reject(i, p, typ, f"{out_r} is in a pinned cell (locked or inside the edit stop)")
                    continue
                if in_r not in pool.by_role_id or not ls.addable(in_r):
                    reject(i, p, typ, f"{in_r} is not in the pool or may not be added (started, edit stop or unswappable)")
                    continue
                if v.st.get(in_r, (Participation.PLAYING,))[0] is Participation.OUT or pool.by_role_id[in_r].person_key in excluded_people:
                    reject(i, p, typ, f"{in_r} is OUT")
                    continue
                k = cur[eid].index(out_r)
                lu = list(cur[eid])
                lu[k] = in_r
                if not slot_accepts(v.slots[k], pool.by_role_id[in_r], mode):
                    reject(i, p, typ, f"{in_r} cannot fill slot {v.slots[k]}")
                    continue
                chk = check_lineup([pool.by_role_id[r] for r in lu], mode)
                if not chk.ok:
                    reject(i, p, typ, "illegal lineup: " + "; ".join(chk.reasons[:3]))
                    continue
                new[eid] = lu
                changed.add(eid)
            elif typ == "exclude":
                rid = str(p.change.get("role_id") or p.target.get("role_id"))
                if rid not in pool.by_role_id:
                    reject(i, p, typ, f"unknown role_id {rid}")
                    continue
                pk = pool.by_role_id[rid].person_key
                rows = {r.role_id for r in pool.rows if r.person_key == pk}
                holders = [e for e, lu in cur.items() if rows & set(lu)]
                if any(rows & pinned_rows(e) for e in holders):
                    reject(i, p, typ, f"{pool.by_role_id[rid].name} is in a pinned cell (locked or inside the edit stop)")
                    continue
                failed = None
                for e in holders:
                    lu, route, _, detail = late_swap._solve_entry(
                        pool, mode, so.linear, cur[e], pins[e], fast=True, exclude_rows=frozenset(rows) | ls.not_addable,
                        capped_rows=frozenset(), overlaps=[], time_limit_s=float(rt.get("late_swap", {}).get("per_entry_time_limit_s", 2.0)))
                    if lu is None:
                        failed = f"entry {e}: {detail}"
                        break
                    new[e] = late_swap._place(cur[e], pins[e], list(lu), pool, mode)
                    changed.add(e)
                if failed or not changed:
                    reject(i, p, typ, failed or "the person is in no entry")
                    continue
            else:
                reject(i, p, typ, "unsupported strategic change type (swap or exclude)")
                continue
            good, figs = evaluate(so, cur, new, changed, v.contest_of, v.fee, risk_cfg, mode)
            if not good:
                sel = figs.get("selection", {})
                why_not = figs.get("reason") or ("inconclusive: the gain is inside the band" if sel and not sel.get("gain_ok")
                                                 else "a tail, safety or referee check failed")
                reject(i, p, typ, why_not)
                res.decisions[-1]["figures"] = figs
                continue
            label = ("unvalidated modeled improvement (FIELD_CALIBRATION=PRIOR)"
                     if so.field_cal is FieldCalibration.PRIOR else f"modeled improvement (FIELD_CALIBRATION={so.field_cal.value})")
            cur = new
            accept(i, p, typ, label, figures=figs, entries=sorted(changed))
            res.accepted_strategic += 1
        except Exception as exc:  # one bad proposal never ends the round or strands the incumbent
            reject(i, p, typ, f"could not be evaluated ({type(exc).__name__}: {str(exc)[:120]})")

    # correctness repairs: every open cell holding an excluded person
    repair_notes = []
    if excluded_people:
        ex_rows = frozenset(r.role_id for r in pool.rows if r.person_key in excluded_people)
        linear = so.linear if so is not None else swap_objective.baseline_linear(pool)
        for e, lu in cur.items():
            open_bad = [r for k, r in enumerate(lu) if r in ex_rows and k not in pins[e]]
            pinned_bad = [r for k, r in enumerate(lu) if r in ex_rows and k in pins[e]]
            for r in pinned_bad:
                repair_notes.append(f"entry {e}: {pool.by_role_id[r].name} is pinned and cannot be removed")
            if not open_bad:
                continue
            got, route, _, detail = late_swap._solve_entry(
                pool, mode, linear, lu, pins[e], fast=True, exclude_rows=ex_rows | ls.not_addable, capped_rows=frozenset(),
                overlaps=[], time_limit_s=float(rt.get("late_swap", {}).get("per_entry_time_limit_s", 2.0)))
            if got is None:
                repair_notes.append(f"entry {e}: no legal repair ({detail}); cells kept")
                continue
            cur[e] = late_swap._place(lu, pins[e], list(got), pool, mode)
    if accepted_overrides:
        store = run.path / "news" / "accepted_overrides.json"
        store.parent.mkdir(exist_ok=True)
        prior = json.loads(store.read_text(encoding="utf-8")) if store.exists() else []
        store.write_text(json.dumps(prior + accepted_overrides, indent=1, default=str), encoding="utf-8")

    # publish the changed cells
    changes = {e: {k: r for k, r in enumerate(lu) if r != v.lineups[e][k]} for e, lu in cur.items()}
    changes = {e: ch for e, ch in changes.items() if ch}
    if changes:
        ls2 = v.locks(clock())
        crossed = [(e, k) for e, ch in changes.items() for k, r in ch.items() if ls2.cells[(e, k)].pinned or not ls2.addable(r)]
        if crossed:
            res.stop_reason = "a lock boundary was crossed during QA: nothing published, the checked file stands"
            return record()
        staging = run.path / "staging" / f"qa_round_{round_no}.csv"
        staging.parent.mkdir(exist_ok=True)
        data = late_swap.splice_cells(v.entries, pool, changes, staging)
        from nhl_dfs.intake.entries import template_permutation

        perm = template_permutation(v.entries.roster_labels, mode)
        col_of = {k: col for col, k in enumerate(perm)}
        locked_text = {(e.entry_id, col_of[k]): e.cells[col_of[k]] for e in v.entries.entries for k in pins[e.entry_id]}
        report = check_file(staging, run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv", locked=locked_text)
        if not report.ok:
            res.stop_reason = "referee rejected the QA file (" + "; ".join(report.reasons[:3]) + "): the checked file stands"
            return record()
        m = read_manifest(run)
        try:
            pub = publish(run, data, report, m["slate_id"], outputs_root=outputs_root,
                          expect_public_sha=sha256(v.version_path.read_bytes()))
        except (PublishRefused, LockTimeout) as exc:
            res.stop_reason = f"publish failed ({exc}): the checked file stands"
            return record()
        res.published_version = pub.version
        m["versions"].append({"version": pub.version, "phase": f"QA{round_no}", "sha256": report.out_sha256,
                              "created_utc": datetime.now(timezone.utc).isoformat(), "path": str(pub.version_path),
                              "public_replaced": pub.public_replaced, "public_detail": pub.public_detail})
        m.setdefault("qa", []).append({"round": round_no, "accepted_correctness": res.accepted_correctness,
                                       "accepted_strategic": res.accepted_strategic, "version": pub.version,
                                       "changed_entries": sorted(changes), "notes": repair_notes})
        m["messages"] = list(m.get("messages", [])) + [f"QA round {round_no} published v{pub.version}"] + repair_notes
        write_manifest(run, m)
        write_run_notes(run, m)
    more = res.accepted_correctness > 0 and round_no < int(cc["max_rounds"])  # the T-8 check runs when that round starts
    res.another_round = bool(more)
    res.stop_reason = ("round accepted a correctness repair: another round is permitted (before T-8)" if more else
                       ("zero changes accepted: QA stops" if not (res.accepted_correctness or res.accepted_strategic) else
                        (f"round {round_no} was the last permitted" if round_no >= int(cc["max_rounds"]) else
                         "no correctness repair accepted: QA stops after this round")))
    if repair_notes:
        res.stop_reason += "; " + "; ".join(repair_notes[:3])
    return record()
