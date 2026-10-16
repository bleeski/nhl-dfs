# C41 ownership band: does the field's ownership error move a pick? (backlog B74, B130)

Written and committed 2026-10-16 BEFORE any C41 code, before the calibration and before any replay. The git history is the proof of
order: this file's commit precedes the commits that add `models/ownership.error_band`, the scaled sampler, the report and the
instrument `scripts/c41_ownership_band.py`. If any line of the rule changes after numbers exist, the change is named, with the number
that prompted it, in the result section below, in the tracker log line and in the final report to Ben. The result section is appended
below its marker; the text above it is never edited. BEN flags 119 to 122 record the defaults; the plan was reviewed by the advisor
tool before Ben approved it.

## The question

Plan section 6 (cold start) asks for ownership as ranges and alternatives under low, base and high ownership. Today selection sees one
field, the base forecast. The 09-30 forecast missed per-player ownership by 3.9 to 4.3 points (MAE). C41 asks whether a field as far
from the forecast as the graded forecasts were changes which lineup an entry gets, beyond what resampling the same field changes.
Selection uses the answer only if it does, and even then a change waits on C51's reading of the Classic objective (B112).

## Seen before this file was written (planning, read-only, in memory)

Planning ran the settle code path (`grade_ownership.forecast_from_run`, `learn.standings.join`, `grade_ownership.grade`) on the local
runs and standings and computed one more figure, the slope of actual ownership on forecast ownership (OLS on deviations from each
contest's mean, every role in the forecast-plus-drafted set, zeros included). Nothing was written. These numbers were therefore known
when the rules below were set:

| Forecast run | Contest (family) | MAE, whole pool | MAE, actual top 20 | Slope |
|---|---|---:|---:|---:|
| 20260930-214909-classic (graded with 20260930-233424) | 196218433 (large_gpp) | 4.106 | 10.107 | 0.732 |
| | 196218434 (large_gpp) | 4.034 | 11.147 | 0.729 |
| | 196218438 (large_gpp) | 4.315 | 11.279 | 0.749 |
| | 196228907 (large_gpp) | 3.942 | 10.034 | 0.738 |
| 20260929-222125-classic | 195958173 (large_gpp) | 2.233 | 10.911 | 0.850 |
| | 195958188 (cash) | 2.909 | 19.870 | 0.852 |
| 20260930-013521-showdown | 196048725 (large_gpp) | 2.830 | 10.854 | 0.793 |
| | 196048727 (large_gpp) | 3.047 | 9.772 | 0.715 |
| | 196050215 (large_gpp) | 2.594 | 10.387 | 0.782 |

The MAE column equals each run's `settle/grades.json`. Every graded contest is flatter than its forecast: on 09-30 the players the
forecast put at 10% or more held 519 points of ownership against about 401 actual. The residual after the slope (sd about 5.5 points
per player on 09-30) is larger than the concentration part. C41 models the concentration part only; the residual is a backlog row.

## The band (flag 119)

`models/ownership.error_band(rows)` takes the graded table (per contest and role: forecast percent, actual percent) and returns per
contest the whole-pool MAE (the arithmetic of `grade_ownership.grade`, so it reproduces the grades), the MAE over the actual top 20,
the slope and the residual sd; and per forecast the pooled slope: sum over its contests of sum(dp x da) over sum over its contests of
sum(dp x dp), each contest's deviations taken from its own means. Only large_gpp contests enter the pooled slope (a cash contest's
field is another family's).

- beta_low = the smallest pooled slope among the graded Classic forecasts (09-30 and 09-29); beta_high = 1 / beta_low, the mirror in
  log space. The base forecast is the band's center and is not moved (the card's "Do not"); the evidence that every graded field was
  flatter than its forecast becomes a backlog row for C12.
- Classic only. Showdown has one graded forecast and a slower sampler; its report prints `SKIPPED (C41 calibrated Classic only)` and
  a backlog row holds its own band.
- The fixture `tests/fixtures/c41/graded_ownership.csv` holds the large_gpp contests of the three graded forecasts at full precision,
  every role of the graded set, zeros included, with role ids only (no names, no lineups, no entries).

## The low and high fields (flag 120)

One temperature t multiplies every behavior's Gumbel noise standard deviation, with every seed unchanged, so t = 1 gives today's field
byte for byte and t > 1 gives a flatter field. Both parts of the field are rebuilt at t: the provisional draws (the run seed) and the
growth draws (seed + 9001), on the run's own feature table, behaviors and own_cfg.

Calibration, fixed now: on each replay pool (the inputs of 20260929-222125-classic and 20260930-210607-classic replayed as below), the
realized slope is the slope of the large_gpp field's role ownership at t on the same field's ownership at t = 1, every role of the
pool. Bisection on log t over [0.25, 4], at most 12 steps, stops within 0.02 of the target (beta_low for the low field, beta_high for
the high field). A target that cannot be reached takes the bound; its realized slope is recorded and the decision is read at that
band. `config/ownership.yaml` `field.error_band` records the geometric mean of the two pools' t for each side as a PRIOR, with this
file as its source; every run reports its own realized slope and the MAE of its low and high fields against base.

Fallback, used only if noise alone cannot reach a target on a pool: t also divides the stack-team log-weights (exp(total / t)). It is
used only if the pool's `implied_total` feature is non-zero for some team; the saved runs priced games from the model, so it may be
zero everywhere, and then the fallback is recorded as unavailable.

## What is counted

At the base run's chosen knob, for every entry, under each alternative field (low, high, and the two controls below):

- **Anchored scoring.** The fill is sequential: one early change moves the caps, the used candidates and the contest book for every
  later entry. So each entry is scored under the alternative field with the state advanced by the base fill's own earlier picks
  (`portfolio._greedy(..., forced=base picks)`): what the alternative field would pick there, and the band's anchor there (the
  candidate `tiebreak.rank` puts first under policy "mean", value only), before the base pick is placed. A **pick flip** is a
  different pick; an **anchor flip** a different anchor.
- **Candidates** under an alternative field are the union of the base screen and that field's own screen (`portfolio.screen`); the
  number it adds is reported.
- **Spread.** Every field is scored on the same selection scenarios. Per candidate and contest with entries, the goal's figure (the
  family utility the fill maximizes: first_place_equity for large_gpp under the shipped goal) against the field alone; d_low and
  d_high, each the mean per-scenario difference from base, with its paired SE; spread = max(0, d_low, d_high) minus
  min(0, d_low, d_high), in dollars, and its size against noise as the larger of |d_low| / SE_low and |d_high| / SE_high (the
  paired z). The base value, its SE and the same figures for the top-1% payout are printed beside it.

## The decision (flag 121)

Replays: saved runs 20260929-222125-classic and 20260930-210607-classic, the C55 protocol (flag 111): a clean export of the C41 tree,
PYTHONHASHSEED=0, seed 20261015, entries cloned to 30, each run's created_utc clock, web inputs time-pinned as `scripts/c17_replay.py`
pins them, the write guard, the network blocked, every NHL_DFS root in scratch, the goal as shipped (first_place), other python
processes recorded. Low and high are each pool's own calibrated t. Two controls at t = 1 with the field seed + 1 and + 2: each control
reseeds the provisional draws and the growth draws (so + 9002 and + 9003), because the base field is their join; the selection
scenarios are unchanged. Only the large_gpp picks count (29 on 09-29, 30 on 09-30); the cash entry is reported.

- **The spread moves a pick** if and only if, on both slates, the larger of the low and high anchor-flip counts is greater than the
  larger of the two controls' anchor-flip counts.
- **Tie criterion (B130).** A pick flip whose anchor did not move was settled by the ownership order inside the band. It is counted and
  reported and never counts toward the rule, so the rule can fail even when the ownership order reshuffles many picks. Per base pick,
  the instrument records whether the 3% floor or one SE set the band's width.
- **Known bias, stated now.** The low and high fields reuse the base field's uniforms (scaled); the controls are fresh draws. Sampling
  noise therefore moves the controls more than it moves low and high, and the rule leans toward "no move".
- **If the spread moves a pick:** `config/risk.yaml` `selection.ownership_band` (false), under which the fill keeps one contest book
  per field and scores each candidate by the mean of its base, low and high scores, is built and measured on the same replays (picks
  changed, the referee figures under each field); it ships OFF, a BEN flag records it, and adoption waits on C51. **If not:** no
  selection code is written; the verdict is recorded and C41 is DONE.

Reported, never used to decide: the spread distributions, the realized slopes and MAEs, pick flips, a free-running refill under each
alternative field (the fill from scratch at the chosen knob), a full `select()` under each alternative field and whether its chosen
knob moves, the candidates each alternative screen adds, timings and memory.

## The report in every run (flag 122)

After the scenario pass publishes, read-only: the low and high fields at the config's t, built and freed one at a time, the spread of
every candidate in `runs/<id>/scenario/ownership_bands.json`, a summary in the manifest, and one RUN_NOTES line naming every anchored
flip. It never changes selection or the published bytes, has its own time budget, and a failure is a report line. It ships ON only if
the replays show at most 60 s of added wall time and at most 1 GB of added peak memory at 30 entries on both slates; otherwise
`field.error_band.report` ships false and flag 122 says so.

## Result

(appended below this line after the runs; the text above is not edited)

### Part 1: the band and the calibration (2026-10-16, before any replay that counts a flip)

**The band (flag 119),** `models/ownership.error_band` on `tests/fixtures/c41` (1,281 roles, the 8 large_gpp contests of the three
graded forecasts): it reproduces every settle grade's MAE to 3 decimals (09-30: 4.106, 4.034, 4.315, 3.942). Pooled slopes: 09-30
Classic 0.7369, 09-29 Classic 0.8503, 09-29 Showdown 0.7635. So beta_low = 0.7369 (09-30) and beta_high = 1 / 0.7369 = 1.357.

**The calibration (flag 120),** `scripts/c41_ownership_band.py calibrate` on a clean export of a219485, PYTHONHASHSEED=0, seed
20261015, both saved runs replayed with the report off (`ownership_band_2026-10-16_calibration.json`). Writes refused 0, network
blocked 0. One other python command (besides the instrument's own pair) was running during the 09-30 pool, none during 09-29.

| Pool | implied_total | t = 1 | Low: t, realized slope, MAE vs base, most-owned player | High: t, realized slope, MAE vs base, most-owned player |
|---|---|---|---|---|
| 09-29 (1,160 draws) | 0 for every team | keys equal the base field's | 1.6818, 0.7432, 0.789, 44.2% to 31.3% | 0.3856, 1.3635, 1.311, 44.2% to 68.0% |
| 09-30 (5,000 draws) | 0 for every team | keys equal the base field's | 1.6818, 0.7551, 1.336, 64.8% to 47.2% | 0.3536, 1.3737, 2.985, 64.8% to 90.3% |

- Noise alone reached every target inside 0.02, so the stack-team fallback was not needed; it was also unavailable, because the
  replayed pools carry no implied team totals (the saved runs priced every game from the model).
- `config/ownership.yaml` `field.error_band.classic`: t_low 1.6818, t_high 0.3693 (the geometric means), the report switched on for
  the replays; flag 122's cost bar decides whether it stays on.
- The decision replays use each pool's own t (09-29: 1.6818 and 0.3856; 09-30: 1.6818 and 0.3536), as flag 120 says.
