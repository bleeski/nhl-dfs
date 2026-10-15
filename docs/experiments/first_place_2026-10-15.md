# C55 first place: can the engine tell lineups apart on first place, and does aiming at it pass the bar? (backlog B129)

Written and committed 2026-10-15 BEFORE the measuring code exists, before any objective code and before any number. The git history is the
proof of order: this file's commit precedes the commit that adds the instrument, `scripts/c55_first_place.py`, and every result. If any line
of the rule changes after numbers exist, the change is named, with the number that prompted it, in the result section below, in the tracker
log line and in the final report to Ben. The result section is appended below its marker; the text above the marker is never edited. BEN
flags 107 (which contests switch, in what unit) and 108 to 112 (this file's rules) record the defaults.

## The question

Ben's first goal changed on 2026-10-14 (B129): each large_gpp and small_field entry is to be chosen for its share of its own contest's first
prize, not for its payout from top-1% finishes (large_gpp) or under the whole curve (small_field). His second goal, P(lose >= 80% of slate
fees) through kappa, the 0.60 budget and the goalie and game caps, is unchanged. First place is a rare event: a lineup with a fair share of a
5,000-entry field finishes first in about 4 of the 20,000 selection scenarios. This file asks, before any switch exists, whether the engine's
first-place figure separates the candidates it chooses among, and fixes the bar the switch must pass to ship ON.

## The code as it stands (read 2026-10-15 on origin/master 3ea1c4a)

- `build/objectives.py` `FAMILY_OBJECTIVE` (line 52) maps large_gpp to `exp_payout_top1pct`, small_field to `exp_payout`, wta to
  `first_place_equity`. `_evaluate` (634 to 690) and `PayCurve.pay_util` (718 to 733) set the per-scenario utility in cents: large_gpp the
  payout from positions inside the top 1% pooled over ties; wta floor(share x first prize) through a float; everything else its payout.
- `first_place_equity` per scenario is (G == 0) / (E + 1): G the field weight (plus own entries) strictly above, E the weight tied.
- Selection (`build/portfolio.py` `_greedy`) scores every candidate per entry as E[utility $] - kappa x fees x P(lose >= 80%) and picks
  `tiebreak.rank(items, cfg, policy)[0]`; the first band holds every candidate within max(0.03 x |best|, 1.0 x its SE) of the best score.
- Late swap scores repairs on the first 8,000 cached selection scenarios, and on the first 4,000 with `--fast` (`config/runtime.yaml`
  `late_swap`). The QA controller accepts a strategic change only if the family utility gains beyond the band and the top-1% payout of the
  touched contests does not fall beyond its SE (`build/controller.py` `evaluate`).
- Both saved runs price every GPP at the 5,000 PRIOR field (`runs/<id>/scenario/contests.json`): 20260929-222125-classic holds the $1K
  Daily Dollar (large_gpp) and a $1 Double Up (cash); 20260930-210607-classic holds four large_gpp (Hip Check, Quarter Jukebox, $750 Daily
  Dollar, $5K mini-MAX). Cloning to 30 entries copies the first entry's contest: 29 entries in the 09-29 Daily Dollar, 26 in the 09-30 Hip
  Check. No saved run holds a small_field contest.

## Estimators (flag 109)

For candidate k in contest c and selection scenario s, with G and E that contest's field weight strictly above and tied with k (the field
alone, no own entries) and N the opponents (the field weight sum; the drawn count for a sampled field):

- **Exact**, today's wta formula: x = 1[G = 0] / (E + 1). A first-place finish is a scenario with G = 0.
- **Smoothed**, the chance of beating N independent draws from the weighted field with ties shared: with b = (N - G - E) / N and
  t = E / N, y = ((b + t)^(N + 1) - b^(N + 1)) / ((N + 1) t), and y = b^N when t = 0.

The figure is the mean over scenarios; its standard error is the population standard deviation over sqrt(S), as `objectives._se` computes
it. Both are reported side by side everywhere. The smoothed estimate is field only: C18's joint-accounting kernel receives field and own ranks
already added together, so a smoothed field with own entries counted exactly would need a change to that accounting which this file does not
preregister. Therefore if the exact estimate fails the bar and the smoothed one passes, the switch ships OFF, C55 is BLOCKED with the numbers
and a backlog row is filed for the kernel; the smoothed estimate never ships in C55.

## The bar (flag 108)

Part A, resolution, on the origin/master replay at seed 20261015 (the candidate bank comes from discovery on the design scenarios and does not
depend on the selection goal, so it is measured before any objective code exists). For every large_gpp contest of both saved runs: every
candidate `portfolio.select` receives, scored against that contest's selection field alone over the selection scenarios; the 20 with the
highest figure (ties by candidate key); the median of their SE / value, a candidate with value 0 counting as infinite. **Part A passes when
that median is at most 0.25 at 20,000 scenarios in every large_gpp contest of both runs.** The same figure at the first 8,000 and first 4,000
scenarios decides the late-swap paths (flag 110). Reported beside it: first-place finishes per candidate (min, median, max among the 20), the
same for the smoothed estimate, the same for today's top-1% figure, and the master-2 and seed-20261016 replays as information.

Part B, ties, on the C55 tree once the switch exists (this rule is committed now; the number comes after the switch is built). A pick is
decided inside the band when the first band of its `tiebreak.rank` call under the entry's policy holds two or more candidates (the second
call, under policy "mean", which only names the band's anchor, is not counted). The share of large_gpp picks at the chosen knob decided
inside the band, goal first_place against goal top1pct, same tree, seed 20261015. **Part B fails only if first_place's share is more than
double top1pct's.**

**The switch ships ON when Part A (exact) and Part B both pass.** Otherwise it ships OFF and C55 is BLOCKED with the numbers. small_field has
no real contest to measure: it follows the large_gpp outcome (a fair-share lineup in a field of at most 500 finishes first ten times as often
as in 5,000), and that is stated as unmeasured.

Known flattering bias, stated now: the top 20 by figure are partly lucky draws, so their SE / value reads better than their true value would.

## Late swap (flag 110)

Each late-swap path aims at first place only where Part A passes at the scenario count it scores on: the full path at 8,000
(`late_swap.full_scenarios`), `--fast` at 4,000. A path where it fails keeps the top-1% goal for its repairs and prints which goal it used
(`config/runtime.yaml` `late_swap.full_goal` / `fast_goal`). Ben's default named only 4,000; the same rule at 8,000 is the session's
decision, because the full path scores on 8,000, not 20,000.

## Protocol (flag 111)

Saved inputs of runs 20260929-222125-classic and 20260930-210607-classic, offline, at each run's created_utc clock, web inputs time-pinned
as `scripts/c17_replay.py` pins them, PYTHONHASHSEED=0, shipping scenario counts (design 5,000, selection 20,000, referee 20,000), entries
cloned to 30, behind the c17 write guard with the network blocked and NHL_DFS_RUNS_ROOT, NHL_DFS_OUTPUTS_ROOT, NHL_DFS_LEDGER_ROOT and
NHL_DFS_BACKLOG in scratch. Other python processes and the capture schedule are recorded for every run. Order:

1. origin/master at seed 20261015, twice (master-1, master-2: the noise baseline, B98);
2. origin/master at seed 20261016 (the seed control);
3. the C55 tree with goal top1pct at seed 20261015: it must give master-1's 30 lineups with identical selection draws, else the chunk stops;
4. the C55 tree with goal first_place at seeds 20261015 and 20261016.

Lineup changes between goals are read only against the seed control (master-1 against the seed-20261016 run). If master-1 and master-2
disagree, the load is recorded and the pair is rerun once with no other python process running; if it still disagrees, the equality gate in
step 3 becomes the switch-off byte test on the mini fixture plus "C55-top1pct differs from master-1 in no more entries than master-2 does,
with identical selection draws".

## Reported, never used to decide (flag 112)

Per contest: the first prize and the top-1% payouts as shares of the prize pool; the simulated winning score (the best field lineup's score
per selection scenario, DK points: median, 10th and 90th percentile) against the real winning score and the real entrant count in the
matching standings export (`data/standings/inbox/2026-09-29` and `2026-09-30`, read through scratch copies), beside the 5,000 the run assumed.
Per run and goal: lineups changed, P(lose >= 80%), the chosen knob, E[payout], P(cash), first-place equity and the top-1% figures. None of it
is fitted or used as a threshold.

## Result

(appended below this line after the runs; the text above is not edited)
