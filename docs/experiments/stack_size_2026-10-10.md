# C21 stack size: which stack shape has the best modeled top-1% equity? (backlog B44, B22)

Written and committed 2026-10-10 BEFORE the study script exists and before any number from it. The git history is the proof of order: this
file's commit precedes the commit that adds `scripts/c21_stack_study.py` and every result. If any line of the rule changes after numbers
exist, the change is named, with the number that prompted it, in the result section below, in the tracker log line and in the final report
to Ben. The result section is appended below its marker; the text above the marker is never edited.

## The question

The 2026-10-03 standings synthesis (14 pooled Classic contests, one week; in-sample for anything chosen from it) found that on the 3-game
09-30 slate 45 to 65 percent of the top 1 percent held a 5+ stack against 20 to 23 percent of the field, and on the 8-game 10-01 slate 8 to 21
percent against 14 to 18. That is a realized outcome (COL scored 8 on 09-30), not a forecast, and it cannot be used as a parameter (C12). This
file asks the simulator instead: for each stack shape, what is the modeled probability that a lineup of that shape finishes in the top 1
percent, and does any shape beat the 4-3-1 core by enough, on both saved slates, to justify a sleeve (a small capped share of discovery
candidates of that shape)? A shape is the count of skaters per team with the goalie left out, so every shape below sums to 8.

## Shapes, and how a shape is forced

Four forced shapes: 4-3-1, 5-2-1, 6-1-1, 3-3-2 (skaters counted from role IDs, never from the field sampler's own stack figures). A shape is
forced in the solver with group rows (`milp.GroupConstraint`, the mechanism of the per-goalie menu in `portfolio.discover`): for a team tuple,
"exactly k skaters of team T", "exactly k2 of team U" where the shape has a second stack, "exactly k3 of team W" for 3-3-2, and "at most 1 of
every other team". With the existing rule of at least 3 teams this gives exactly that shape and no other. No solver, candidate-builder,
portfolio, config or field-sampler file is edited by this chunk; shape rows exist only inside the study script and never reach the field
sampler.

Equal depth: one fixed set of eligible primary teams for every shape, the top 6 teams by mean modeled skater points over the design draws (fewer
if the pool has fewer), and exactly one tuple per eligible primary team per shape. The primary team is the largest stack (for 3-3-2, one of
the two 3-stacks). The companion teams are chosen by the best unperturbed objective (sum of per-role mean points over the design draws) among
all other teams, ties broken by team code. Each tuple is ranked by calling `LineupModel.solve` directly with a 30 second limit and the status
recorded; a primary team with no feasible tuple for a shape is dropped for that shape and the shape's tuple count is reported.

## Data, frozen

Two stages, so the study itself is a pure function of frozen caches.

- Saved cache S (the slate's scenario cache, read through scratch copies): the selection and referee draws with the play-time mask applied once over
  the full stream, then sliced; the contests (family, field size, payout curve) exactly as the run priced them; the pool from the run's own inputs.
- Replay R (stage 1): the slate's saved inputs (`DKSalaries.csv`, `DKEntries.csv`, clock = the run's `created_utc`) run offline through today's tree in
  scratch roots with `run_slate(scenario=True, offline=True)` at design 300, selection 800, referee 800 and field target 5,000, seed 20261010.
  Only R's sampled large_gpp field LINEUPS are used. R's own draws are not (an offline replay takes no odds, so every game would take model
  intensities and the blow-up structure would be lost, and no contest details, so field size and payouts would fall to priors).
- Why a replay field: the saved caches of runs 20260929-222125-classic and 20260930-214909-classic predate the 2026-10-08 switch-on of the C19 and C46
  stack mixtures (flag 47: the older sampled field held about 0.6 percent 5+ stacks against the observed 21.3). A 5+ stack scored against a field
  with almost none is flattered. The card says to run against a field with the observed stack mix.
- Decides: saved draws and contests with the replay field. Reported beside it, deciding nothing: the same candidates against S's own saved field,
  labeled "built before C19 and C46" with its measured 3+, 4+, 5+ and 6-1-1 shares.
- Fidelity gate: the replay field's 3+, 4+ and 5+ shares, counted from role IDs over all its draws, must each lie within 10 points of the pooled
  table (93.5, 65.7, 21.3, `config/ownership.yaml` classic_stack_table; the C46 gate's tolerance). A slate that fails has NO VERDICT. The 6-1-1 share is
  reported; the replay field has none (B101: 0.0 percent against 6.3), so every 6-1-1 result is labeled "against a field that has none".
- The contest scored: the large_gpp contest of S with the most opponents, in weighted field mode (more than `objectives.sampled_max_opponents` = 1,000
  opponents); ties by contest id. None: NO VERDICT for that slate. Field spec: `objectives.field_spec` on R's lineups with S's opponent count and S's seed.
- Slates: the 09-29 and 09-30 runs on Ben's machine (backlog B107). They sit inside the standings that suggested the question, so any gain is
  IN-SAMPLE and says so wherever it appears. The replay's inputs are not frozen (the local history store has grown since the original runs, and
  its field's team weights follow model intensities, not the market).
- Synthetic bed: the committed `tests/fixtures/late_swap/classic` fixture (6 teams in 3 games, 2 goalies per team) with its entries cloned to 40, one
  large_gpp contest at the family prior, run offline at design 1,000, selection 3,000, referee 3,000 and field target 1,000 (the bed's S and R are
  the same run). It is synthetic, priors only, and **says nothing about any real slate and cannot promote anything**. The cloud container that builds
  this chunk has no `runs/` folder, so this is the only slate it can run.
- Probe disclosure: before this file was written one plumbing probe ran on the bed. It printed the cache's sizes (800 draws per stream at the small
  setting, 400 field lineups, one 5,000-opponent large_gpp contest), the contest and field descriptors, and the shapes of forced candidates (30 of 30 in
  the asked shape for each of the four shapes, own-goalie rule on and off, and a free search landing in 3-2-1-1-1, 3-2-2-1 and 2-2-2-1-1). It computed no
  probability of any kind.

## Streams and candidates

- Design stream: the first half of S's cached selection draws. It gives the objective (mean points per role) and the team ranking.
- Choosing stream: the second half of the selection draws. Used only for the best-10 condition below.
- Reporting stream: all of S's referee draws.
- Arms, per slate and per seed: the four forced shapes and one free arm (the unconstrained central search: shows which shapes today's search lands in
  by itself). 60 candidates per arm per seed through `candidates.generate` with the runtime settings (`perturb_sd_points` 2.0, `min_pairwise_diff` 2),
  `distinct=True` for every arm, a 600 second budget, the menu entries cycled in primary-team rank order. The own-goalie rule is on in every arm (the
  shipping default for large_gpp; flag 60). An arm that returns fewer than 60 reports its count; each comparison between two arms uses the first m
  candidates of each, m the smaller count, per seed.

## Metric (decides)

Per candidate, the modeled probability of a top-1 percent finish: the engine's own rule from `objectives._evaluate`, `top = clip(top_k - G, 0, T) / T`
with `top_k = floor(0.01 x field size)`, `T = ties + 1`, ranked against the contest's field on the reporting stream. A test proves its mean equals
`objectives.contest_metrics(...).p_top1pct` on the same candidates and that, on a 1-entry bed, it equals the manifest's own `p_top1pct`. The metric of an
arm is the mean over its candidates, then over seeds. Also required to pass (best-10): the mean over seeds of each arm's best 10 candidates, chosen by
the choosing stream and scored on the reporting stream. Reported and deciding nothing: each arm's shape mix and tuple counts, the saved-field condition,
the published portfolio's shape mix, and the ordering of shapes per slate beside the observed ordering of the standings synthesis (09-30: 6-1-1, 4-3-1,
5-2-1; 10-01 main: 4-3-1, 4-2-1-1, 5-2-1).

## Baseline and challengers

Baseline: 4-3-1 (the card's 4-3-1 core, and the field's most common shape at 31.4 percent). Challengers: 5-2-1 and 3-3-2. 6-1-1 is reported with the label
above and is never a challenger (B101: the field has none, so a 6-1-1 win could only be an artifact of the field). The published portfolio's shape mix is
information only.

## Seeds, error and the noise baseline

- Candidate-generation seeds 20261010 to 20261014; a seed is the same noise stream for every arm. The cache is one fixed set of simulated worlds, so
  seeds alone would understate the noise; two more error parts are added.
- Standard error of a challenger's gain over 4-3-1 = sqrt(seed part squared + scenario part squared + field part squared). Seed part: the spread of
  the five paired seed differences divided by sqrt(5). Scenario part: the spread, over 200 bootstrap resamples of the reporting-stream scenarios (seed
  20261010, the same rows for both arms), of the seed-pooled difference. Field part: the spread, over 30 multinomial reweightings of the field's lineup
  counts (`objectives.ranks_multi`, seed 20261010), of the seed-pooled difference.
- Minimum detectable effect, stated before the verdict, per slate: 2.78 x the larger of the two challengers' standard errors. If it exceeds 10 percent of
  the 4-3-1 mean on that slate, the slate is INCONCLUSIVE by construction.
- Noise baseline FIRST (B98): the free arm and the 5-2-1 arm each generated twice at seed 20261010 on identical inputs. Reported: whether the two candidate
  lists are identical (sha256) and the metric difference. Any other python process at start and `PYTHONHASHSEED` are recorded. Runs are one at a time.

## Verdict, mechanical, in this order

Per slate and challenger C, with `d` the mean gain of C over 4-3-1 (seed-pooled), `se` its standard error and `b` the 4-3-1 mean:
1. The slate failed to run, has no eligible contest, or its replay field failed the fidelity gate: NO VERDICT.
2. MDE above 10 percent of `b`: INCONCLUSIVE.
3. PASS if `d >= 2.78 x se` AND `d >= 0.03 x b` AND the best-10 gain is above zero.
4. FAIL if `d <= 0` or `d < 0.01 x b`.
5. Anything else: INCONCLUSIVE.

Study verdict:
- Any synthetic slate in the run: NOT MEASURED on real caches. The per-slate statuses are printed labeled synthetic and decide nothing.
- Fewer than two real slates: NOT MEASURED.
- Any real slate with NO VERDICT for every challenger: NO VERDICT.
- ACCEPT-TO-SHADOW(C) only if the same challenger C is PASS on both real slates. If both challengers pass both, the one with the larger smaller-slate gain
  as a share of the 4-3-1 mean; a tie goes to 5-2-1.
- REJECT if every challenger is FAIL on at least one real slate.
- Anything else: INCONCLUSIVE.

The thresholds (2.78, 3 percent, 1 percent, 10 percent) are judgment. No earlier measurement stands behind them. 2.78 is the 95 percent two-sided t
cutoff for 4 degrees of freedom; with the scenario and field parts dominating the error it is conservative, and the result section says so.

## What ships, whatever the verdict

Nothing in the shipping path changes: no sleeve code is added in this chunk. INCONCLUSIVE, NOT MEASURED and NO VERDICT all count as not promoted for
shipping. Only ACCEPT-TO-SHADOW on both real slates leads anywhere, and then only to a follow-up Ben opens. If a sleeve is ever built it is a fixed
share of discovery candidates capped like `discovery.stress_sleeve_max` (0.10), tagged by shape and reported in RUN_NOTES, not sized by game count (two
caches are two points, so no rule by slate size can be fitted, C12) and never a rule. The tracker reading of the outcomes: ACCEPT-TO-SHADOW opens that
follow-up; REJECT closes B44's stack-size question for these slates; INCONCLUSIVE, NOT MEASURED and NO VERDICT keep B107 open for the real run or
more slates, with the rule above unchanged.

## Caveats written before the numbers

- A forced-shape candidate is best by modeled points under a perturbed objective, not leverage-aware. The study measures how good each shape's best-by-points
  lineups are against the field, not how the full selection would use them.
- The top-1 percent is a rare event; P(top 1 percent) of one candidate is a few percent, so every figure has wide Monte Carlo error. The error parts
  above are the guard; a challenger that looks better on one slate and not the other is not promoted.
- Three comparisons per slate (two challengers, plus the label-only 6-1-1) and two slates; the requirement that the same challenger passes both
  slates is the guard, not a multiple-comparison correction.
- The replay field's team weights follow model intensities rather than the market, and its 6-1-1 share is zero (B101).
- The bed is synthetic and priors only; it can show that the mechanism runs and that the arms are comparable. Nothing else.
- Both real slates are in-sample for the question. An accepted result would be a reason to shadow-test on slates not yet played, not a validated edge.

<!-- RESULT MARKER: everything below was appended after the arms ran; nothing above this line is edited -->
