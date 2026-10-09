# C20 goalie leverage term: does it add top-1% equity on top of the own-goalie rule? (backlog B45, part 2)

Written and committed 2026-10-09 BEFORE any arm was run. The git history is the proof of order: this file's commit precedes the commit
that adds the term's code, the experiment script and every result. No arm has been run, no number has been looked at, and the term's code
does not exist yet. If any line of this rule changes after numbers exist, the change is named, with the number that prompted it, in the
result section below, in the tracker log line and in the final report to Ben. The result section is appended below its marker; the text
above the marker is never edited.

## The question

The 2026-10-03 standings synthesis (14 pooled Classic contests, one week; in-sample for anything chosen from it) found the goalie to be the
biggest difference between top-1% lineups and the rest: the winning goalie was in 91.8 percent of top-1% lineups, the field's chalk goalies
in none, and the winning goalies were 9 to 15 percent owned. The own-goalie rule (flag 13, flags 51 to 53) is built whatever this file finds.
This file asks only about the second idea: an explicit goalie term in the discovery objective with a leverage piece (ownership against the
field). Does it raise modeled top-1% equity on top of the rule that ships?

## The term (exact, hand-set, never fitted)

For each goalie role row g in a Classic pool, the discovery objective (points) gains
`ceiling_weight * (q90_g - mean_g) + leverage_weight * (top_share_g - own_g)`, where over the DESIGN scenarios `q90_g` is the 0.9 quantile
of g's points and `mean_g` its mean (both in points), `top_share_g` is the fraction of scenarios in which g scores the most of any goalie in
the pool (ties split equally), and `own_g` is g's ownership as a fraction in the sampled field of the pool's biggest contest. Weights:
`ceiling_weight` 0.25 (a quarter of the gap between a goalie's 90th percentile and his mean), `leverage_weight` 10.0 (one point for each 10
percentage points of top-goalie share above ownership). Both are set by this arithmetic, labeled PRIOR, and not fitted to standings (C12). Skater rows are never touched. The term is
applied to every discovery job (central, alternate, priors wrong, the per-goalie menu). It lives behind `discovery.goalie_term.enabled`,
shipped false.

## Arms

| Arm | `own_goalie.enabled` | `discovery.goalie_term.enabled` | Meaning |
|---|---|---|---|
| A0 | false | false | today's tree (the sha256 check against origin/master 3eec8d3 is the proof of equality) |
| A1 | true | false | the rule only: what ships |
| A2 | false | true | the term only |
| A3 | true | true | the rule and the term |

**The only decision is A3 against A1** (does the term help on top of what ships). A1 against A0 and A2 against A0 are reported and gate
nothing; flag 13 already decided the rule.

## Data, frozen

- No saved scenario folder is on the machine this chunk was built on (a Linux cloud container with no `runs/`). The scenario cache also
  holds only the selection and referee draws, not the design draws discovery uses, so each arm runs the whole offline scenario pass.
- Bed: a synthetic slate written by `scripts/c20_goalie_experiment.py`: 6 teams in 3 games, 2 goalies per team, DraftKings-format salary
  file with Game Info, one 40-entry large_gpp contest, `run_slate(scenario=True, offline=True)` in scratch roots. MODEL_STATUS is PRIOR,
  FIELD_CALIBRATION is PRIOR, PAYOUT_SOURCE is PRIOR. **This bed is synthetic: it says nothing about any real slate and cannot promote the
  term.**
- Real caches: the 2026-09-29 and 2026-09-30 runs on Ben's machine are the only real inputs. They sit INSIDE the data that suggested the rule
  (in-sample), so a gain there is not evidence for a new slate. That run is a separate step for Ben (backlog B102), with the same arms,
  seeds and rule below, and it is the only run that can turn this into ACCEPT-TO-SHADOW.

## Metric

Per arm and seed: the mean, over the large_gpp entries, of the modeled probability of a top-1% finish on the REFEREE draws (the stream
independent of selection), as recorded in the run's manifest. Also reported, deciding nothing except where the rule below says so:
RISK_BUDGET, the GOALIE_CAP line, the shared-failure worst teams, the own-goalie count and the goalie distribution of the 40 entries.

## Seeds and noise

- Seeds `20261009 + i`, i = 0 to 4. A seed is a different world (scenarios, field, discovery); every arm sees the same five worlds, so
  differences are paired by seed.
- Noise baseline FIRST: A0 twice at seed 20261009 on identical inputs (B98: time limits in the solver calls may make identical inputs differ),
  then the five-seed spread of A1. Runs are one at a time on an idle machine; any other python process seen at start is recorded.
- Minimum detectable effect, stated before the verdict: `2.78 * sd(A1 metric over the 5 seeds) / sqrt(5)` (2.78 is the 95 percent
  two-sided t cutoff for 4 degrees of freedom). If it exceeds 10 percent of A1's mean metric the verdict is INCONCLUSIVE by construction.

## Verdict, mechanical, in this order

Let `d_i` be A3 minus A1 at seed i, `mean_d` their mean and `se_d = sd(d) / sqrt(5)`.
1. MDE above 10 percent of A1's mean: INCONCLUSIVE.
2. ACCEPT-TO-SHADOW only if ALL hold: `mean_d >= 2.78 * se_d`; `mean_d >= 0.03 * mean(A1)`; RISK_BUDGET is not BREACHED in any A3 seed where A1's
   was not; the GOALIE_CAP line mode (DOLLARS or LINEUPS) is the same in A3 and A1 for every seed; at least 4 of 5 seeds have `d_i > 0`.
3. REJECT if `mean_d <= 0` or `mean_d < 0.01 * mean(A1)`.
4. Anything else: INCONCLUSIVE.

Threshold numbers (2.78, 3 percent, 1 percent, 10 percent, 4 of 5) are judgment. No earlier measurement stands behind them.

## What ships, whatever the verdict

The term ships OFF (`discovery.goalie_term.enabled: false`). A verdict of ACCEPT-TO-SHADOW on the synthetic bed is reported as "accepted on
a synthetic bed, not promoted" and the term still ships off. Turning the term on needs Ben, after a real-cache run (B102) satisfies this
same rule. INCONCLUSIVE counts as not accepted. C20 is DONE on any verdict with the term off.

## Caveats written before the numbers

- The bed is synthetic, priors only, one slate shape, five worlds; it can only show that the mechanism runs and that arms are comparable.
- `own_g` comes from the PRIOR field model, not measured ownership.
- The per-goalie menu in discovery already forces every usable goalie into some candidates, and selection already ranks against the field,
  so the term may have nothing left to add. That is a possible finding, not a failure.
- Five seeds is a small sample; the rule above is stated for that sample.

<!-- RESULT MARKER: everything below was appended after the arms ran; nothing above this line is edited -->

## Result (appended 2026-10-09 after the arms ran; nothing above the marker was edited)

**Verdict, by the rule above: A3 against A1 is INCONCLUSIVE. The term ships OFF (it always did). Nothing here promotes it.**

What ran: `PYTHONHASHSEED=0 python scripts/c20_goalie_experiment.py --out <scratch> --scenario-n medium` on the code of commit `c385afc`
(the term and the script) plus `6f6b9c0` (the script's own checks, committed before any arm), in a Linux cloud container with no other python
process (checked at start), 22 runs in 577 seconds, interleaved by seed with the arm order rotated. Every run is the whole offline scenario
pass in a scratch folder; the per-run records are `docs/experiments/goalie_leverage_2026-10-09_results.json`. The write guard refused 132
writes, all appends of the line "dailyfaceoff offline: not cached" to `data/raw/observations/2026-10-09.jsonl` (the known gap: nothing
redirects that log), and no network attempt was made. No other file under `data/`, `runs/` or `outputs/` was touched.

Noise baseline (first, as written): A0 twice at seed 20261009 gave the identical ordered lineups (sha256 `d9e733baad71e1bc` both times, and the same as the
A0 seed-0 run of the main sweep) and a metric difference of 0.000000, so the B98 nondeterminism did not appear on this idle container. The
five-seed spread of A1 is sd 0.000342 (1.6 percent of its mean), so the minimum detectable effect is 2.78 x 0.000342 / sqrt(5) = 0.000425,
1.96 percent of A1's mean: well under the 10 percent line, so step 1 of the rule did not fire.

| Arm | Mean P(top 1%) per large_gpp entry, 5 seeds | Seeds (20261009 to 20261013) | Entries facing their own goalie (per seed) |
|---|---:|---|---|
| A0 today's tree | 0.019922 | 0.019822, 0.019187, 0.020630, 0.020260, 0.019710 | 18, 15, 23, 15, 14 |
| A1 rule only (ships) | 0.021648 | 0.021385, 0.021380, 0.022143, 0.021465, 0.021868 | 0 in every seed |
| A2 term only | 0.020732 | 0.021535, 0.020073, 0.021138, 0.020383, 0.020533 | 15, 15, 23, 14, 17 |
| A3 rule and term | 0.022341 | 0.022305, 0.021233, 0.023370, 0.021815, 0.022980 | 0 in every seed |

The decision, A3 minus A1 by seed: +0.000920, -0.000148, +0.001228, +0.000350, +0.001113. Mean 0.000692, standard error 0.000259.
- Step 2 needs all of: mean >= 2.78 x se (0.000719): **not met, 0.000692 is 2.68 se**; mean >= 3 percent of A1's mean: met (3.20 percent);
  RISK_BUDGET not BREACHED in any A3 seed where A1's was not: met (OK in all 22 runs); the GOALIE_CAP mode the same in A3 and A1:
  met (DOLLARS 0.40 in all runs); at least 4 of 5 seeds above zero: met (4 of 5).
- Step 3 (REJECT) needs a mean of 0 or less, or under 1 percent of A1's mean: not the case.
- Step 4: INCONCLUSIVE. It missed the t test by 0.09 standard errors. The threshold is not moved: it was written before the run.

Information only, deciding nothing: the rule against today's tree (A1 minus A0) is +0.001726 (+8.7 percent of A0's mean), and every one of
the five seeds is higher; the term alone (A2 minus A0) is +0.000810 (+4.1 percent). These are modeled figures on the synthetic bed.

Deviations from, and things the preregistration did not say, all decided before any arm was run unless marked:
1. The bed is the committed `tests/fixtures/late_swap/classic` fixture (6 teams in 3 games, 2 goalies per team) with its entries file cloned
   to 40 entries by the script, not a slate the script writes. It carries one OUT and one DTD skater. The contest is the fixture's
   "NHL Synthetic Classic", which has no name pattern, so its family is the config default large_gpp; its field is the family prior (5,000
   entries, FIELD_SIZE_SOURCE=PRIOR) of which 40 entries are ours, not a 40-entry field.
2. Scenario counts are the script's "medium": design 1,000, selection 3,000, referee 3,000, field_target 1,000, against `risk.yaml`'s 5,000,
   20,000 and 20,000. The preregistration did not state them. One plumbing run (A1, seed 20261009, medium) was made before the script's
   checks were added; its metric, 0.021385, was seen, and it equals the later A1 seed-0 run exactly. It was not used to choose anything.
3. The two goalies of a team have no history in this offline bed, so the sim gives each the same start probability and skill; the term
   still moved them apart (the goalie points it added at seed 0 range from 2.41 to 4.50, higher for the second goalie of each team), but
   the bed cannot say whether that is the right direction on a real slate.
4. GOALIE_CAP was in DOLLARS mode in every run and does not depend on the arm, so the GOALIE_CAP condition of step 2 is vacuous here; RISK_BUDGET was OK
   everywhere, so its condition is also uninformative here.
5. The rule-on arms hold 474 candidates against 324: the provisional pass adds a second bank built with the rule (flag 52). A whole offline
   run took 28.6 to 31.0 s with the rule on against 21.5 to 23.7 s with it off; `candidates.generate` alone took 1.4 s against 1.0 to 1.4 s for 150
   candidates and 3.1 s against 2.0 s for 300 (one measurement, the 72-row fixture).
6. The script sets the four NHL_DFS roots to the scratch folder and refuses writes under `data/`, `runs/` and `outputs/`, but `cache=None`
   makes the offline run read the repo's own `data/raw` and history, which are nearly empty in this container (so the field and roles carry
   no news and no odds: MODEL_STATUS, FIELD_CALIBRATION and PAYOUT_SOURCE are all PRIOR in every run, which the script asserts).

What this does and does not say: on a synthetic, priors-only bed, the term's gain over the rule is about 3 percent of the metric with
4 of 5 seeds positive and a t statistic just under the preregistered cut. That is a reason to run the real caches, not a finding about any
real slate. The rule's own gain on the bed (8.7 percent) is a modeled figure from a simulator whose goalie and opponent-skater
correlation is the mechanism the rule leans on; it is not a measured ROI.

Next, and not done here (backlog B102): the same script on the real 2026-09-29 and 2026-09-30 inputs on Ben's machine
(`--salary <runs folder>\<id>\inputs\DKSalaries.csv --entries <same folder>\DKEntries.csv --clock <the run's UTC start>`), which are in-sample for
the rule and cannot support a claim about a new slate. Only a run that satisfies this same rule on real inputs can turn the term on, and
turning it on is Ben's call.
