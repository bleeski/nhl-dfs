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

## Amendments before any result (2026-10-10, after the independent code review of the script)

No result existed when these were written. The runs so far were the script's tests (they assert plumbing, never outcomes) and one dry run of the real-slate
path on the bed's own run folder, from which only the plumbing differences, the fidelity gaps and the string "NOT MEASURED (fewer than two real slates)"
were read, no P(top 1 percent) figure. Each item fills a detail the text above left open or corrects a draft of the code; none moves a threshold, an arm,
a seed, a stream or the verdict mapping.

1. Replay field lineups that hold a player the saved cache did not simulate (an online run drops OUT and disabled players in Phase B; an offline replay
   has no Phase B and keeps them) are dropped before scoring, counted, and reported. More than 15 percent dropped gives NO VERDICT for that slate. The
   fidelity gate and the field spec use the retained lineups.
2. The field part of the standard error resamples the field's own independent draws (the sum of its lineup counts), then scales them to the opponent
   count the way the saved weights are scaled (`objectives.largest_remainder`). A first draft resampled the opponent count itself, which would have
   understated this part.
3. Participation is priced exactly as late swap prices a cache: the cache's own `play_prob` when it holds one, else C8's rule (DTD persons at
   `questionable_play_prob`) for a cache made before it was stored. The mask is drawn once over the cached stream, so it is not the original run's draw over
   its longer one. The source is printed.
4. The replay field is read from the family the replay gave this contest id (an offline replay has no lobby details and may classify it differently),
   else large_gpp. The family is printed.
5. With one challenger missing a result, the minimum detectable effect is computed from the challengers that have one.
6. The observed ordering of the standings synthesis is printed beside the modeled one (information only). A tuple solve that ends on a time limit is
   named in a warning.
7. The synthetic bed's replay runs with the local history store, stored web pages and identity files pointed at empty folders, so its result does not
   depend on the machine. A real slate's replay reads the real local history, as the original run did.
8. The check that the per-scenario indicator equals the engine's `p_top1pct` runs on the first 150 candidates (memory); the plumbing test pins it.

<!-- RESULT MARKER: everything below was appended after the arms ran; nothing above this line is edited -->

## Result (appended 2026-10-10 after the run; nothing above the marker was edited after it ran, and the amendments section above it was written before any result)

**Verdict, by the rule above: NOT MEASURED on real caches. The run holds only the synthetic bed, so its per-slate statuses decide nothing. No sleeve is
built and nothing ships; for shipping this counts as not promoted. On the bed, 5-2-1 reached PASS status and 3-3-2 FAIL status; this file
defines no acceptance for a synthetic slate, and it is not evidence about any real slate. The real run is backlog B107.**

What ran: `PYTHONHASHSEED=0 python scripts/c21_stack_study.py --out <scratch> --bed` (the preregistered sample: 5 seeds, 60 candidates per arm per seed, 200
bootstrap resamples, 30 field reweightings) on the code of commit `56f4d54` (the script, the tests and the review fixes), in a Linux cloud container with no
other python process (checked at start). The bed replay ran at design 1,000, selection 3,000, referee 3,000 and field target 1,000; the cache keeps whole
2,000-draw chunks, so the streams were design 1,000 and choosing 1,000 (the two halves of 2,000 selection draws) and reporting 2,000 referee draws. The study
took 41.9 seconds after the replay (under 80 seconds from start to the report). The write guard refused 0 writes (the bed runs with the local history isolated; C20's run, which
did not isolate it, tried the observation-log append 132 times) and blocked 0 network attempts; the four NHL_DFS roots were scratch folders and their runs and
outputs folders are empty. Before and after, the modification times of `data/raw/dailyfaceoff`, `data/features`, `data/standings`, `data/ledger`,
`data/entered`, `runs/` and `outputs/` (all of them absent in this container except `data/entered`) and the observation line count (1 line in 1 file)
are identical. The run records are `docs/experiments/stack_size_2026-10-10_results.json` and `_guard.json`.

Noise baseline (first, as written): the free arm and the 5-2-1 arm, each generated twice at seed 20261010 on identical inputs, gave identical candidate lists
(sha256 `4540626970cb5dc1` and `408b0e1a4d79ed09` both times) and a metric difference of 0.0 for both, so the B98 nondeterminism did not appear on this idle
container. Every tuple solve ended FEASIBLE (30 for 4-3-1, 30 for 5-2-1, 6 for 6-1-1, 120 for 3-3-2; none on a time limit), every shape had exactly 6 tuples
(the 6 teams of the bed, so equal depth), and none of the 1,498 distinct candidates faced its own goalie. The scorer's per-scenario indicator equals the engine's
`p_top1pct` to 2.3e-9 on the first 150 candidates, and the re-weighted ranks reproduce the sorted-field ranks to 2.6e-10.

| Arm (60 candidates per seed, all five seeds) | Mean P(top 1%) | Shapes of its candidates |
|---|---:|---|
| 4-3-1 (baseline) | 0.01763 | 4-3-1 100 percent |
| 5-2-1 | 0.01891 | 5-2-1 100 percent |
| 6-1-1, against a field that has none (B101) | 0.02055 | 6-1-1 100 percent |
| 3-3-2 | 0.01756 | 3-3-2 100 percent |
| free (the unconstrained central search) | 0.01614 | 3-2-2-1 26.0, 3-2-1-1-1 24.7, 2-2-2-1-1 17.0, 4-2-1-1 8.7, 3-3-1-1 6.7, 4-3-1 3.7 |

| Challenger | Gain over 4-3-1 | Standard error (seed, scenario, field) | Best-10 gain | Seeds above zero | Status |
|---|---:|---|---:|---|---|
| 5-2-1 | +0.00128 (+7.2 percent) | 0.00044 (0.00030, 0.00028, 0.00016) | +0.00139 | 5 of 5 | PASS on the bed |
| 3-3-2 | -0.00007 (-0.4 percent) | 0.00048 (0.00034, 0.00031, 0.00013) | -0.00161 | 2 of 5 | FAIL on the bed |

The minimum detectable effect on the bed is 2.78 x 0.00048 = 0.00134, 7.6 percent of the 4-3-1 mean, under the 10 percent line, so step 2 of the rule did not
fire. 5-2-1's gain is 2.9 standard errors, above the 2.78 cut, and 7.2 percent, above the 3 percent line; the per-seed gains are +0.00127, +0.00055, +0.00225,
+0.00075 and +0.00156; the best-10 gain is above zero. 3-3-2's per-seed gains are -0.00043, -0.00036, +0.00064, +0.00078 and -0.00100. Information only: 6-1-1's
gain over 4-3-1 is +0.00292 (+16.5 percent, 5 of 5 seeds), labeled "against a field that has none"; it is never a challenger.

Information only, deciding nothing. The fidelity gate passed (replay field 3+ 95.7, 4+ 65.0, 5+ 21.3 against 93.5, 65.7, 21.3; 6-1-1 0.0 against 6.3). On the bed
the saved cache and the replay are the same run, so the saved-field condition equals the deciding one and says nothing here. The published portfolio of the
bed's 40 entries held 3-2-2-1 22.5, 3-2-1-1-1 12.5, 4-2-1-1 10.0, 6-1-1 10.0, 2-2-2-1-1 10.0 and 5-1-1-1 7.5 percent. The modeled ordering was 6-1-1, 5-2-1,
4-3-1, 3-3-2, free; the standings synthesis' observed orderings (09-30: 6-1-1, 4-3-1, 5-2-1; 10-01 main: 4-3-1, 4-2-1-1, 5-2-1) are in-sample and this
ordering is not a check against them, because the bed is not those slates.

Things the preregistration did not say, and deviations, all decided before the run unless marked:
1. The amendments section above (written after the independent review of the script and before any result) fixed eight details; the biggest were the field error
   part resampling the field's own draws and the off-axis rule for replay lineups. None moved a threshold, an arm, a seed or a stream.
2. One dry run of the real-slate path (the bed's own run folder as a stand-in saved run, `--small`) was made before the amendments were committed. It printed the
   fidelity gaps, the plumbing differences and "NOT MEASURED (fewer than two real slates)"; no P(top 1 percent) figure was read from it. The tests ran the study in
   a small configuration (2 seeds, 12 candidates) and assert plumbing only: 27 passed and 1 skipped (the real-cache test, which needs `NHL_DFS_C21_SAVED_RUN`; a skip
   is not a pass).
3. The streams are smaller than the settings asked for: the cache keeps whole 2,000-draw chunks, so the 3,000 requested became 2,000 per stream. A candidate's
   P(top 1 percent) is about 1.8 percent, so about 36 scenarios per candidate decide it; the three error parts are the guard against reading noise as a gain,
   and a PASS on one 6-team bed with 1,000 field draws is a plumbing result.
4. Every arm ran with the own-goalie rule on (flag 60) and no solve needed a time limit; the 600 second generation budget was never reached (the longest of the
   25 generate calls recorded, one per arm and seed, took 1.66 seconds, and every call returned its 60 candidates).

What this does and does not say: the mechanism runs end to end, the arms are comparable (the same objective, noise and count), a forced shape is exact from role IDs,
the noise baseline is clean on an idle machine, and on the bed 5-2-1 clears the rule against 4-3-1 and 3-3-2 does not. It says nothing about the 09-29 and 09-30
slates, where the saved draws were made online (with whatever market coverage those runs had), the replay field carries the observed 5+ share and the slates are
in-sample for the question.

Next, and not done here (backlog B107): the same script on the real 2026-09-29 and 2026-09-30 runs on Ben's machine, offline, in scratch roots, with the same
rule: `python scripts/c21_stack_study.py --out <scratch dir> --saved-run runs\<id> --saved-run runs\<id2>` (each folder is copied into the scratch folder first).
Only a run that gives ACCEPT-TO-SHADOW on both real slates leads to anything, and then only to a follow-up Ben opens: no sleeve code exists.

Corrections and additions after the final review (2026-10-10, same session; nothing here changes a status or the verdict):
1. Where the supporting claims come from. The study time of 41.9 seconds is in the results JSON. "Under 80 seconds from start to the report" is the shell clock (started
   15:03:43, the report was there by 15:04:59). The 1,498 distinct candidates is the study's own log line. The data-safety comparison (modification times of the listed
   paths, one observation line in one file, the scratch roots' runs and outputs folders empty) is a pair of shell checks made at the time and not stored in either JSON;
   `data/cache` and `data/identity` were not on that list (modification times 13:39:57 and 13:39:52, from the container's start). Two lines were appended to
   `data/raw/observations/2026-10-10.jsonl` at 15:10:01 and 15:10:22: they are mine, from `scripts/c20_equality_check.py`'s whole offline run made after the study (the known
   gap: nothing redirects that log; `data/raw/` is gitignored). The first line of that file, from 13:40, is the container's own bootstrap.
2. The verdict paragraph said "accepted on a synthetic bed, not promoted", which is C20's wording; it is replaced in place above, because this file defines no acceptance state
   for a synthetic slate.
3. What the PASS can and cannot mean. The gain is measured against a field that holds 34.8 percent 4-3-1 and 10.4 percent 5-2-1 lineups (and no 6-1-1), so part of 5-2-1's gain
   can be the leverage of being rarer in that field, not the shape's own quality; that is what a metric against the field measures, and it is also why the study scores shapes
   against a field with the observed mix. The PASS is marginal (2.9 standard errors against the 2.78 cut) on 2,000 referee scenarios and 1,000 field draws. 6-1-1 coming first
   in the ordering is by construction: the field has none.
4. The amendments section above understated two things. Amendment 1 added a rule detail (a 15 percent cap on dropped replay lineups, and a NO VERDICT when it is exceeded), which is
   a verdict-mapping detail, not only a clarification; amendment 2 enlarged an error part, which can only make PASS harder. The dry run: its full report was written to a log file that
   I did not open; I printed the first three and last two lines of the log and these fields of its results.json: the study verdict string, the slate's name, flags and fidelity gaps,
   the tuple counts, the plumbing differences and the guard counts. No arm mean and no challenger gain was read from it.
5. The rationale sentence "the older sampled field held about 0.6 percent 5+ stacks" (and flag 62's) is flag 47's figure for the C19 mixture, not a measurement of the saved caches, which
   predate that mixture entirely. The real run measures the saved field's own 3+, 4+, 5+ and 6-1-1 shares and prints them beside the replay field's; nothing in the rule depends on the
   0.6 figure.
6. Code changes after the result was recorded (script and tests only; no threshold, arm, seed or stream moved): the field reweighting is now a function with a test (every reweighting sums to
   the opponent count and its dispersion follows the number of draws), the replay takes the inputs and clock of the run that holds the cache when the named run is a refresh or late-swap
   child, `results.json` records the scratch roots relative to `--out` and writes numpy numbers as numbers, and the report prints the replay field's family and draw count beside the
   fidelity gate. The bed study was run again on this final code: every number in the results JSON except the timings is identical to the committed record, and the write guard again refused 0 writes.
   Keep `--out` short on Windows (a copied run folder can approach the path-length limit).
