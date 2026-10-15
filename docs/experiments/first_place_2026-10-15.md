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

### Part 1: the origin/master replays (2026-10-15, before any C55 objective code was committed)

Runs: master-1 and master-2 at seed 20261015 and master-b at seed 20261016, on a clean export of origin/master 3ea1c4a (the
script prints where nhl_dfs was imported from; every run named the export), PYTHONHASHSEED=0, the script as committed in 2ebe002.
Another project's test suite (mlb-dfs, `tools/audit.py --run-tests`) ran throughout: 2 to 4 other python commands at each start,
recorded per run in `first_place_2026-10-15_results.json`. Writes refused 0 and network attempts blocked 0 in every run; every run
published version 3 and also listed "phase B: DK draftables unavailable" and "history refresh: FAILED" (network steps the pinned
replay does not serve). The C55 switch code was written in the working tree while these ran; it could not reach them (they import
the 3ea1c4a export) and it was committed after this section.

**Correction to the description above (written before any number).** Today's code does not price these contests at 5,000: the
replays read the contest facts rows (C50) and priced every contest at its history field size (FIELD_SIZE_SOURCE=HISTORY) with the
PRIOR power curve on the given pool and paid places. Those rows come from standings of the same contest series (2 or 3 contests
between 2026-09-30 and 2026-10-09 each), the replayed night included, so the sizes are in-sample. The bar's wording ("against that
contest's selection field") applies unchanged.

| Contest | Field size used (real entrants) | Opponents / distinct sampled lineups | First prize / pool | Top-1% places / share of pool |
|---|---:|---|---:|---:|
| 09-29 $1K Daily Dollar | 1,189 (1,189) | 1,160 / 1,160, weighted | $102.33 / $1,000 (10.2%) | 11 / 29.4% |
| 09-30 $6K Hip Check | 7,134 (7,134) | 7,108 / 4,991, weighted | $450.52 / $6,000 (7.5%) | 71 / 32.1% |
| 09-30 $1.5K Quarter Jukebox | 7,134 (6,854) | 7,132 / 4,991, weighted | $129.37 / $1,500 (8.6%) | 71 / 35.4% |
| 09-30 $750 Daily Dollar | 891 (891) | 890 drawn per scenario from 4,991 | $85.70 / $750 (11.4%) | 8 / 29.8% |
| 09-30 $5K mini-MAX | 11,890 (10,750) | 11,889 / 4,991, weighted | $419.40 / $5,000 (8.4%) | 118 / 38.0% |

Hip Check, Jukebox and mini-MAX share one sampled field, so their exact first-place figures are identical: five contests, three
distinct fields. Cloning to 30 entries put 29 in the 09-29 Daily Dollar (the 30th is the Double Up, cash) and 26 in the Hip Check.

**Part A (flag 108), master-1, exact estimate:** the median SE / value of the 20 candidates with the highest first-place figure
(372 to 386 candidates per slate), at the first 20,000 / 8,000 / 4,000 selection scenarios, with first-place finishes among those
20 (minimum / median / maximum) and the same median for the smoothed estimate and for today's figure (the top-1% payout).

| Contest | Exact 20,000 / 8,000 / 4,000 | Finishes at 20,000 (at 4,000) | Smoothed | Today's top-1% figure |
|---|---|---|---|---|
| 09-29 Daily Dollar | **0.128** / 0.193 / 0.262 | 55 / 61 / 103 (12 / 15 / 23) | 0.090 / 0.137 / 0.195 | 0.061 / 0.097 / 0.136 |
| 09-30 Hip Check | **0.242** / 0.353 / 0.447 | 15 / 17 / 30 (4 / 5 / 9) | 0.190 / 0.283 / 0.385 | 0.101 / 0.161 / 0.246 |
| 09-30 Quarter Jukebox | **0.242** / 0.353 / 0.447 | 15 / 17 / 30 (4 / 5 / 9) | 0.190 / 0.286 / 0.389 | 0.104 / 0.162 / 0.254 |
| 09-30 Daily Dollar | **0.110** / 0.166 / 0.242 | 73 / 83 / 123 (15 / 17 / 26) | 0.081 / 0.126 / 0.181 | 0.059 / 0.092 / 0.128 |
| 09-30 mini-MAX | **0.242** / 0.353 / 0.447 | 15 / 17 / 30 (4 / 5 / 9) | 0.225 / 0.333 / 0.429 | 0.130 / 0.201 / 0.303 |

- **Part A passes at 20,000 in every contest**, narrowly: the largest median is 0.2424 against the bar of 0.25. master-2 gave the
  same figures to the fourth decimal (same candidates, same draws). The seed control (master-b, information only) gave 0.127,
  0.219, 0.219, 0.104 and 0.219, also under the bar; the pass sits about 0.01 to 0.03 under it.
- **It fails at 8,000 and at 4,000** on the three large 09-30 fields (0.353 and 0.447). Under flag 110 both late-swap paths keep the
  top-1% goal (`late_swap.full_goal: top1pct`, `fast_goal: top1pct`) if the switch ships ON.
- The smoothed estimate passes and fails exactly where the exact one does, so it decides nothing here (flag 109); its top 20 share
  17 to 19 candidates with the exact top 20.
- Known biases, in the direction that flatters first place: the top 20 are partly lucky draws (stated above), and on the three
  large 09-30 fields the engine samples 4,991 distinct lineups for 7,108 to 11,889 opponents, so a lineup has to beat only the best
  of 4,991 to finish first. The smoothed figure uses N = opponents, so part of its gap to the exact one there is this cap. Sampling
  a full-size field is C35's.

**Noise pair and seed control.** master-1 and master-2 chose the same 30 lineups on both slates with byte-identical selection
draws, despite the other project's load; master-b changed 30 of 30 on both (its draws differ by design).

**Part B reference.** Under today's goal, at the chosen knob, 24 of 29 large_gpp picks on 09-29 and 23 of 30 on 09-30 were decided
inside the tie band (47 of 59, 0.80; the seed control 52 of 59). Part B fails only when first place's share is more than double
today's, which cannot happen above 0.50: **on these slates Part B cannot fail.** The rule is left as committed (Ben's default reads
that way); the formal Part B comparison is made on the C55 tree in part 2. Finding: most large_gpp picks are already settled by the
ownership order inside the band, not by the goal's value (backlog row filed with this chunk).

**Winning scores (flag 112, information only, never a fit).** The best field lineup's score per selection scenario against the
real winning score:

| Contest | Simulated median (10th to 90th percentile) | Real winning score |
|---|---|---:|
| 09-29 Daily Dollar | 145.8 (120.5 to 177.4) | 189.5 |
| 09-30 Hip Check, Quarter Jukebox, mini-MAX (one field) | 157.4 (132.1 to 189.4) | 173.7, 172.0, 179.4 |
| 09-30 Daily Dollar | 147.4 (123.2 to 178.3) | 167.1 |

The real winner scored above the simulated median in all five contests and above the 90th percentile once (09-29). Each is one
real night, so this is consistent with B15's weak-at-the-top field and does not measure it.

**Timings** (another project's tests running): whole replays 126.9 and 130.8 s (master-1), 103.7 and 131.8 s (master-2), 107.1
and 123.1 s (master-b), against C24's 114 and 154 s on record; master-1 simulation seconds design / selection / referee 7.8 / 9.5
/ 12.6 (09-29) and 4.0 / 5.2 / 4.3 (09-30), against 7.4 / 12.1 / 11.4 and 3.1 / 4.6 / 5.0 on record. The script's own figures took
7.6 and 73.2 s after each replay.

### Part 2: the C55 tree, and the decision (2026-10-15)

Runs on clean exports of 8b85bc8 (the switch code, committed after part 1): c55-top-a with `objectives.goal: top1pct` at seed
20261015, then c55-fp-a and c55-fp-b with `goal: first_place` at seeds 20261015 and 20261016. One other project's python command was
running at each start. Writes refused 0, network blocked 0, every run published.

**Switch-off equality gate (flag 111, step 3): passed.** c55-top-a chose master-1's 30 lineups on both slates (0 of 30 changed) with
byte-identical selection draws. The mini slate through the whole scenario pass published byte-identical files from the 3ea1c4a export
and from the 8b85bc8 export with goal top1pct (sha256 bae60c54a54d2d99c0935d28c2895934782f12ea4d6852ddf6b4f2e18f6b56f2 both, and again
on a second 3ea1c4a run). A first comparison against the repo's own working tree differed (3078fe16...); that tree reads the repo's
untracked data folders, which a clean export does not have, so export was compared with export.

**Part B (flag 108): passed.** Large_gpp picks at the chosen knob decided inside the tie band: top1pct 47 of 59 (0.797), first_place
51 of 59 (0.864); the bar fails only above 1.593. As stated in part 1, it could not fail on these slates.

**Decision, by the rule committed before any number: Part A (exact) and Part B pass, so the switch ships ON** (`config/risk.yaml`
`objectives.goal: first_place`). Part A failed at 8,000 and at 4,000, so both late-swap paths keep the top-1% goal
(`config/runtime.yaml` `late_swap.full_goal: top1pct`, `fast_goal: top1pct`) and print it. The smoothed estimate decided nothing.

**Before and after (flag 112, information): the portfolio on the referee draws at the chosen knob.** Same seed, so the same referee
draws for both goals; the standard error of each difference was not computed (the independent SEs are about 0.002 for the Daily
Dollar's 7.9% and 0.001 for the Hip Check's 2.0%, so these differences are about 1 to 1.5 independent SEs).

| Slate, seed | Goal | Knob | P(lose >= 80%) | E[payout] | Utility / fees | Top-1% utility / fees | Big contest: P(cash) sum, first-place equity sum |
|---|---|---:|---:|---:|---:|---:|---|
| 09-29, 20261015 | top1pct | 0 | 0.0028 | $46.96 | 0.711 (top-1%) | 0.674 | Daily Dollar (29): 10.08, 7.89% |
| 09-29, 20261015 | first_place | 0 | 0.0012 | $45.73 | 0.305 (first place) | 0.661 | 9.78, 7.78% |
| 09-29, 20261016 | top1pct (master-b) | 0.25 | 0.0031 | $46.42 | 0.707 | n/a | 9.96, 8.09% |
| 09-29, 20261016 | first_place | 0.25 | 0.0018 | $43.50 | 0.305 | 0.624 | 9.31, 7.70% |
| 09-30, 20261015 | top1pct | 0.25 | 0.0120 | $47.56 | 0.971 | 0.971 | Hip Check (26): 8.28, 2.01% |
| 09-30, 20261015 | first_place | 1.0 | 0.0077 | $45.70 | 0.343 | 0.918 | 8.23, 1.86% |
| 09-30, 20261016 | top1pct (master-b) | 0.5 | 0.0126 | $48.33 | 1.033 | n/a | 7.98, 2.21% |
| 09-30, 20261016 | first_place | 0 | 0.0158 | $46.99 | 0.390 | 0.994 | 7.88, 2.20% |

Fees $30.00 (09-29) and $28.00 (09-30). The single-entry contests on 09-30 moved both ways: Quarter Jukebox (2 entries) first-place
equity 0.075% to 0.170% and 0.110% to 0.170%; 09-30 Daily Dollar 0.684% to 0.515% and 0.477% to 0.690%; mini-MAX unchanged at seed
20261015 (same lineup) and 0.100% to 0.045% at 20261016.

- **Headline caveat: the first-place goal did not raise first-place equity on fresh draws.** In the two big contests (29 and 26 of
  the 30 entries) the first-place portfolios' first-place equity on the referee draws was equal or slightly lower at both seeds, and
  E[payout] and the P(cash) sums dipped, as B112 predicted. The pattern points to overfitting to the selection draws, the risk B129
  named: on the selection draws the first-place portfolios' utility was 3 to 6% above what the same portfolios scored on the referee
  draws (ratios 0.97 and 0.94 at seed 20261015, 0.95 and 0.94 at 20261016), while the top-1% portfolios held (0.99, 1.01, 1.00,
  1.02). A candidate with 15 to 30 first-place finishes is picked partly for lucky wins that do not repeat. Two slates, in-sample
  field sizes, a PRIOR field: this is a caveat, not a measured loss. Backlog B132 holds the bounded follow-up (a held-out selection
  split or shrinkage, judged on C51's real results); BEN flag 113 records that the switch is ON by the preregistered rule and how
  to turn it off.
- **The knob moved under first place**, as the card warned (a smaller utility makes the same kappa weigh the risk term more): 09-30
  went from 0.25 to 1.0 at seed 20261015 and from 0.5 to 0 at seed 20261016; 09-29 kept 0 and 0.25. P(lose >= 80%) stayed at most
  0.0158 against the budget's 0.60 in every run. The move confounds the comparison above; it is reported, not retuned.
- **Lineups changed:** 27 of 30 on both slates between the goals at seed 20261015 (24 and 28 at 20261016), against 30 of 30 for the
  seed control alone: read against that control, the count says nothing about the goal (flag 111).
- **The QA controller** scores on 8,000 scenarios (`late_swap.full_scenarios`) with the run's goal, so with the switch ON it judges
  first place where Part A failed (0.353 on 09-30). Flag 110 covers late swap only and was not extended after the fact; QA's band is
  SE-based, so it will accept fewer changes rather than riskier ones. Backlog B131.

**Timings** (one other project's python running): c55-top-a 85.0 and 113.9 s, c55-fp-a 97.8 and 116.7 s, c55-fp-b 102.1 and
125.3 s; selection 27.1 to 53.9 s, the same range as origin/master's 30.6 to 57.4 s, so the first-place kernel adds no visible cost.
