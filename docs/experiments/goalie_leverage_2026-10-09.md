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
