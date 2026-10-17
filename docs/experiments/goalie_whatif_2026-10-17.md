# Goalie what-if on run 20261017-161544-classic (read-only, 2026-10-17)

Written 2026-10-17 after the figures were measured. Nothing was published and nothing under `runs/` or `outputs/` was
written: the scripts below only read the run's scenario cache and call `controller.evaluate` and `ob.own_totals`
without applying anything. The run folder is gitignored and a cloud container is reclaimed, so the cache these figures
came from will not survive; the numbers and the method are kept here so a later session can redo them on a saved run.
Everything here is a scenario estimate under PAYOUT_SOURCE=PRIOR, FIELD_CALIBRATION=PRIOR and OUTCOME_CALIBRATION=UNVALIDATED.

## What was measured

Run 20261017-161544-classic, Classic, 13 entries, three games (CAR@WPG, NJD@WSH, VAN@TBL), 8,000 referee draws, read in
independent blocks of 2,000. The 7-entry contest is 196825463 (NHL $20 Dime Time, field about 204, 55 paid).

1. Probability that a goalie scores the most of all slate goalies (8,000 referee draws): Thompson 0.260, Allen 0.193,
   Kochetkov 0.138, Hildeby 0.116, Stevenson 0.095, Vasilevskiy 0.061, Bussi 0.053, Merilainen 0.039, Skinner 0.030,
   Lankinen 0.015. Per-goalie sd 9 to 10 points; the best goalie of the slate averages 24.0 points (median 23.5, q90 31.7),
   10.9 above the median goalie that played.
2. Goalies held by the 7 Dime Time entries: Allen (3) and Thompson (4). They cover 0.453 of the best-goalie worlds. The four
   goalies the whole portfolio held (Allen, Thompson, Stevenson, Merilainen) cover 0.587.
3. Paired what-if on the same draws (all owned entries of the touched contest, first-place dollars; goalie-only swaps, skaters
   unchanged). Base first-place utility 0.524. Selection draws, 8,000 scenarios: 5298507816 Allen to Hildeby -0.019 +/- 0.005;
   5298513806 Thompson to Kochetkov -0.010 +/- 0.004; both -0.028 +/- 0.007; both plus 5298513807 Allen to Bussi -0.056 +/- 0.008.
   Referee block of 2,000: -0.010 +/- 0.010, -0.008 +/- 0.010, -0.013 +/- 0.013, -0.044 +/- 0.016. P(at least one Dime Time entry
   takes first) 0.1626 to 0.1541 for the first two swaps together. `controller.evaluate` rejects all four ("inside the band").
4. Does a Dime Time first place need the best goalie, in the model? Three blocks of 2,000 pooled (n = 6,000): P(first | we hold
   the slate's best goalie) 0.180, P(first | we do not) 0.159, lift +0.021 +/- 0.010; 48.2 percent of our modeled wins held the
   best goalie against a 45.1 percent base. (One block alone read 0.168 against 0.163; the pooled figure is the one to use.)

## How to read it

- The swaps in item 3 moved entries to unconfirmed goalies with lower odds of being the best goalie (Hildeby mean 8.8 points,
  Bussi 3.4) and left the skaters unchanged, so they do not test coverage built by construction. They show the cost of a naive
  edit under the model's start probabilities.
- The same swap scored -0.019 +/- 0.005 on the selection draws and -0.010 +/- 0.010 on a referee block: repeated what-ifs on the
  draws that chose a portfolio will mine noise (B147, C62).
- Item 4 against the standings synthesis (B45: the winning goalie sat in 91.8 percent of pooled top-1% lineups, 14 contests, one
  week, in-sample) is the conflict B142 and C59 are written to explain. Candidate causes: the scenarios tie a goalie too loosely
  to his own skaters or skater tails run too fat; the simulated field is too weak (7 entries win 16.3 percent, 4.7 times their
  fair share); the standings overstate the link.
- Per-contest P(at least one entry first) was computed by hand here and is not printed by the engine (B152).

## Scripts (scratch copies, run with the repo's Python from /home/user/nhl-dfs)

`whatif.py`: goalie top-share, held-goalie coverage, the four what-ifs and the single-block conditional.

```python
"""Read-only what-if on run 20261017-161544-classic: goalie top-share and paired scoring of goalie edits.
Nothing is published or written under runs/ or outputs/."""
import sys
from argparse import Namespace
from datetime import datetime, timezone

import numpy as np

from nhl_dfs.build import controller, packet as packet_mod, swap_objective
from nhl_dfs.build import objectives as ob
from nhl_dfs.build.objectives import load_risk_config
from nhl_dfs.build.run import load_runtime_config
from nhl_dfs.cli import _qa_run

RUN = "20261017-161544-classic"
args = Namespace(run=RUN, runs_root="/home/user/nhl-dfs/runs", outputs_root="/home/user/nhl-dfs/outputs", as_of=None)
run, runs_root, outputs_root = _qa_run(args)
now = datetime.now(timezone.utc)
v = packet_mod.RunView(run, runs_root)
pool, mode = v.pool, v.mode
rt = load_runtime_config()
ls = v.locks(now)
c = v.cache
risk_cfg = load_risk_config()
blocks = 4
size = c.n("referee") // blocks
rows = (size, 2 * size)  # the block QA round 2 would have used; round 2 can never run on this run
so = swap_objective.ScenarioObjective(c, pool=pool, work=pool, rm=None, st=v.st, started_games=ls.started_games,
                                      n_use=int(rt.get("late_swap", {}).get("full_scenarios", 8000)), now=now,
                                      odds_snapshot=None, notes=[], referee_rows=rows)

by_name = {}
for r in pool.rows:
    by_name.setdefault(r.name, r)
rid = lambda name: by_name[name].role_id
cur = {e: list(lu) for e, lu in v.lineups.items()}
contest_of, fees = v.contest_of, v.fee
print("entries", len(cur), "| sample lineup", next(iter(cur.values()))[:2])

# ---- goalie top-share on the referee draws -------------------------------------------------------------
goalies = ["Jake Allen", "Nico Daws", "Remi Poirier", "Logan Thompson", "Charlie Lindgren", "Clay Stevenson",
           "Stuart Skinner", "Brandon Bussi", "Pyotr Kochetkov", "Leevi Merilainen", "Kevin Lankinen",
           "Andrei Vasilevskiy", "Dennis Hildeby"]
pk = c.person_keys
idx = {g: pk.index(by_name[g].person_key) for g in goalies if by_name[g].person_key in pk}
n = min(c.n("referee"), 20000)
base = c.base("referee", n)
G = np.stack([base[:, idx[g]] for g in idx], axis=1).astype(float)
names = list(idx)
top = G.argmax(axis=1)
share = {nm: float((top == i).mean()) for i, nm in enumerate(names)}
print(f"\nGOALIE TOP-SHARE (referee draws, n={n}): P(goalie scores the most of all slate goalies)")
for nm, s in sorted(share.items(), key=lambda kv: -kv[1]):
    if s > 0.001:
        print(f"  {nm:20s} {s:6.3f}   mean pts {G[:, names.index(nm)].mean()/10:5.2f}")
dime = [e for e in cur if contest_of[e] == "196825463"]
held = lambda es: {pool.by_role_id[next(r for r in cur[e] if pool.by_role_id[r].pos == 'G' or 'G' == getattr(pool.by_role_id[r], 'position', None))].name for e in es}
def goalie_of(lu):
    for r in lu:
        row = pool.by_role_id[r]
        if row.name in by_name and row.name in goalies:
            return row.name
dime_goalies = sorted({goalie_of(cur[e]) for e in dime})
print("\nDime Time (7 entries) goalies held:", dime_goalies)
cov = lambda gs: sum(share.get(g, 0.0) for g in gs)
print(f"  P(slate's best goalie is one we hold in Dime Time) = {cov(dime_goalies):.3f}")
all_held = sorted({goalie_of(l) for l in cur.values()})
print("Portfolio goalies held:", all_held, f"-> P(best goalie is held anywhere) = {cov(all_held):.3f}")


# ---- paired scoring of hypothetical goalie edits ---------------------------------------------------------
def swap(lus, eid, old, new):
    out = {e: list(l) for e, l in lus.items()}
    k = out[eid].index(rid(old))
    out[eid][k] = rid(new)
    return out


def figures(before, after, changed, purpose):
    touched = sorted({contest_of[e] for e in changed})
    res = {}
    for side, lus in (("before", before), ("after", after)):
        joint = controller._portfolio(so, lus, contest_of, purpose, touched)
        pay, util, topd = ob.own_totals(joint, tail="first_place")
        res[side] = (np.asarray(pay), np.asarray(util), np.asarray(topd))
    S = len(res["before"][1])
    d = (res["after"][1] - res["before"][1]) / 100.0
    fp_b, fp_a = res["before"][2], res["after"][2]
    fees_c = int(round(sum(fees.values()) * 100))
    return {"S": S, "util_gain": d.mean(), "se": d.std() / np.sqrt(S),
            "util_before": res["before"][1].mean() / 100.0,
            "first_before": fp_b.mean() / 100.0, "first_after": fp_a.mean() / 100.0,
            "p_any_first_before": float((fp_b > 0).mean()), "p_any_first_after": float((fp_a > 0).mean()),
            "touched": touched, "_fp_before": fp_b, "_fp_after": fp_a}


tests = {
    "H1  7816 Allen -> Hildeby (TBL goalie)": [("5298507816", "Jake Allen", "Dennis Hildeby")],
    "H2  13806 Thompson -> Kochetkov (CAR goalie)": [("5298513806", "Logan Thompson", "Pyotr Kochetkov")],
    "H3  H1 + H2": [("5298507816", "Jake Allen", "Dennis Hildeby"), ("5298513806", "Logan Thompson", "Pyotr Kochetkov")],
    "H4  H3 + 13807 Allen -> Bussi (both CAR goalies covered)": [("5298507816", "Jake Allen", "Dennis Hildeby"),
                                                              ("5298513806", "Logan Thompson", "Pyotr Kochetkov"),
                                                              ("5298513807", "Jake Allen", "Brandon Bussi")],
}
print("\nPAIRED WHAT-IF (all owned entries of each touched contest, same draws before and after; first-place dollars)")
for label, edits in tests.items():
    new = cur
    changed = set()
    for eid, old, newg in edits:
        new = swap(new, eid, old, newg)
        changed.add(eid)
    for purpose in ("selection", "referee"):
        f = figures(cur, new, changed, purpose)
        # conditional diagnostic: does a first-place finish need the slate's best goalie?
        print(f"{label} [{purpose}] S={f['S']}: utility gain {f['util_gain']:+.4f} +/- {f['se']:.4f} "
              f"(before {f['util_before']:.4f}); first-place $ {f['first_before']:.4f} -> {f['first_after']:.4f}; "
              f"P(we take first) {f['p_any_first_before']:.4f} -> {f['p_any_first_after']:.4f}")
    ok, figs = controller.evaluate(so, cur, new, changed, contest_of, fees, risk_cfg, mode)
    sel = figs.get("selection", {})
    print(f"   controller.evaluate: accept={ok}; band {sel.get('band')}; gain_ok={sel.get('gain_ok')}; "
          f"p_lose80 {sel.get('p_lose80_before')} -> {sel.get('p_lose80_after')}")

# ---- does a Dime Time first place need the best goalie? (model view) -------------------------------------
joint = controller._portfolio(so, cur, contest_of, "referee", ["196825463"])
pay, util, topd = ob.own_totals(joint, tail="first_place")
topd = np.asarray(topd)
m = len(topd)
rb = c.base("referee", c.n("referee"))[rows[0]:rows[1]] if False else base[rows[0]:rows[0] + m]
Gb = np.stack([rb[:, idx[g]] for g in idx], axis=1).astype(float)
besti = Gb.argmax(axis=1)
best_name = np.array([names[i] for i in besti])
holds = np.isin(best_name, dime_goalies)
won = topd > 0
print(f"\nDime Time under the model (n={m}): P(we win first)={won.mean():.4f}; "
      f"P(hold best goalie)={holds.mean():.3f}; P(first | hold best goalie)={won[holds].mean():.4f}; "
      f"P(first | not hold)={won[~holds].mean() if (~holds).any() else float('nan'):.4f}; "
      f"share of our wins that held the best goalie={holds[won].mean() if won.any() else float('nan'):.3f}")
```

`goalie_dist.py`: per-goalie and best-goalie score spread (it reuses the head of `whatif.py`).

```python
import numpy as np
exec(open("/tmp/claude-0/-home-user-nhl-dfs/779cb321-eb78-50b4-86ad-d3c55d028ffb/scratchpad/whatif.py").read().split("# ---- paired scoring")[0].replace('print(', 'pass  # print('))
X = G / 10.0
best = X.max(axis=1)
print("per-goalie points (referee draws): mean / sd / q90 / P(>=25)")
for i, nm in enumerate(names):
    if share[nm] > 0.01:
        print(f"  {nm:20s} {X[:,i].mean():5.1f} / {X[:,i].std():5.1f} / {np.quantile(X[:,i],0.9):5.1f} / {(X[:,i]>=25).mean():.3f}")
print(f"best goalie of the slate: mean {best.mean():.1f}, q10 {np.quantile(best,.1):.1f}, median {np.median(best):.1f}, q90 {np.quantile(best,.9):.1f}, P(>=25) {(best>=25).mean():.3f}, P(>=30) {(best>=30).mean():.3f}")
print(f"gap, best goalie minus the median goalie that played: mean {(best - np.median(np.where(X>0, X, np.nan), axis=1)).mean() if False else (best - np.nanmedian(np.where(X>0, X, np.nan), axis=1)).mean():.1f}")
```

`lift.py`: the pooled three-block best-goalie lift.

```python
import numpy as np
src = open("/tmp/claude-0/-home-user-nhl-dfs/779cb321-eb78-50b4-86ad-d3c55d028ffb/scratchpad/whatif.py").read()
head = src.split("so = swap_objective.ScenarioObjective")[0]
exec(head)
tail = src.split("# ---- goalie top-share")[0].split("by_name = {}")[1]
exec("by_name = {}" + tail)
goalies = ["Jake Allen","Nico Daws","Remi Poirier","Logan Thompson","Charlie Lindgren","Clay Stevenson","Stuart Skinner",
           "Brandon Bussi","Pyotr Kochetkov","Leevi Merilainen","Kevin Lankinen","Andrei Vasilevskiy","Dennis Hildeby"]
pk = c.person_keys
idx = {g: pk.index(by_name[g].person_key) for g in goalies if by_name[g].person_key in pk}
names = list(idx)
base = c.base("referee", c.n("referee"))
def goalie_of(lu):
    for r in lu:
        if pool.by_role_id[r].name in idx: return pool.by_role_id[r].name
H, W = [], []
for contest, label in (("196825463", "Dime Time (7 entries)"),):
    held = sorted({goalie_of(cur[e]) for e in cur if contest_of[e] == contest})
    for a in (0, 2000, 4000):
        so = swap_objective.ScenarioObjective(c, pool=pool, work=pool, rm=None, st=v.st, started_games=ls.started_games,
                 n_use=8000, now=now, odds_snapshot=None, notes=[], referee_rows=(a, a + 2000))
        joint = controller._portfolio(so, cur, contest_of, "referee", [contest])
        _, _, topd = ob.own_totals(joint, tail="first_place")
        won = np.asarray(topd) > 0
        Gb = np.stack([base[a:a+2000, idx[g]] for g in names], axis=1)
        best = np.array(names)[Gb.argmax(axis=1)]
        holds = np.isin(best, held)
        W.append(won); H.append(holds)
        print(f"  block {a}-{a+2000}: P(first)={won.mean():.4f} P(hold best)={holds.mean():.3f} P(first|hold)={won[holds].mean():.4f} P(first|not)={won[~holds].mean():.4f}")
    W, H = np.concatenate(W), np.concatenate(H)
    p1, p0 = W[H].mean(), W[~H].mean()
    se = np.sqrt(p1*(1-p1)/H.sum() + p0*(1-p0)/(~H).sum())
    print(f"{label} pooled n={len(W)}: P(first|hold best)={p1:.4f}  P(first|not)={p0:.4f}  lift={p1-p0:+.4f} +/- {se:.4f}  | our wins that held the best goalie {H[W].mean():.3f} vs base {H.mean():.3f}")
```
