# C22 chalk core: does the ownership tie-break move picks off the slate's top two chalk pieces? (backlog B46)

Written and committed 2026-10-13 BEFORE the measuring code exists and before any number from it. The git history is the proof of order: this
file's commit precedes the commit that adds the instrument, `scripts/c22_chalk_core.py` and every result. If any line of the rule changes after
numbers exist, the change is named, with the number that prompted it, in the result section below, in the tracker log line and in the final
report to Ben. The result section is appended below its marker; the text above the marker is never edited. BEN flag 95 records the defaults.

## The question

The 2026-10-03 standings synthesis (`reviews/2026-10-03_standings_synthesis.md` section 4, B46; three slates, in-sample for anything chosen
from it) found that top-1% Classic lineups held the slate's top two chalk pieces at 90 to 100 percent (MacKinnon 47 percent owned, 100
percent of the top 1; McDavid and Draisaitl 32 and 25 percent, 90 and 82 percent) and differentiated with one to three low-owned correlated
teammates. The selection tie-break prefers the lower lineup ownership inside the materiality band, so it can trade a core piece for a
low-owned one. This file asks how often it does that on the real slates. The card (C22) builds the core exemption only if it does.

## The tie-break as the code has it (read 2026-10-13 on master 062ac8c)

- The scenario selection's one tie-break call is `portfolio._greedy` (`build/portfolio.py:427-430`): the candidates that pass the caps at the
  first cap level with any become `tiebreak.Item(key, score, se, own, dup)` and the pick is `tiebreak.rank(items, risk_cfg, policy)[0]`.
- `own` is the linear sum of the lineup's role ownership percent in that contest's field (`portfolio.py:293`). `dup` is the pre-fit proxy
  (sum of log ownership plus a salary-left term) or the sampled copy count once FIELD_CALIBRATION=FITTED (`tiebreak.dup_measure`).
- `rank` sorts by value, opens a band at the best remaining value (width `max(0.03 x |value|, 1.0 x its standard error)`, `config/risk.yaml`
  tiebreak) and orders the band's members by the family policy (`config/contest_families.yaml`): large_gpp `own_then_dup` (own, then dup,
  then value), small_field and wta `dup_first` (dup, then own, then value), cash and satellite `mean`, top_n `finishes`.
- Policy `mean` orders the band by value with the same key order that chose the anchor, so `rank(items, cfg, "mean")[0]` is exactly the pick
  with no ownership order: the band's anchor. **The tie-break moved a pick** when the real pick's key differs from the anchor's key.

## Definitions (decide)

- **Core.** Per contest: the two SKATERS with the largest selection-stream mean (tenths, `ScenarioSet.means_tenths`) times that contest's field
  ownership percent (`own_by[contest]`); ties by person key. Skaters only is a stated deviation from the card's word "players": B46's evidence
  is skaters, and B45 found the field's chalk goalies at 0 percent of the top 1 percent, so a goalie is never part of the core. Classic only:
  Showdown Captain choice is C23, and a Showdown run reports no core. The report prints, per run, whether the eligible contests' cores agree.
- **Core held.** The number of core persons in a lineup (0, 1 or 2).
- **Eligible pick.** A Classic entry whose contest family orders by ownership or duplicates inside the band: large_gpp, small_field, wta.
  Cash, satellite and top_n picks are printed and never counted.
- **Counts per run (the published knob's fill only).** N eligible picks. M picks the tie-break moved, split by the first component of the
  policy's in-band key on which the pick and the anchor differ: M_own (ownership sum) and M_dup (duplicate measure). D picks moved OFF the core:
  core held by the pick below core held by the anchor. U picks moved ONTO it: above. Core retention of the picks (both, one, none) beside the
  anchors'. This is a per-step counterfactual: after a moved pick the fill continues from the real pick, so the anchors' retention is not a
  portfolio the engine would have built (the `mean` arm below is that portfolio).

## The exemption, defined now and not wired

So that a WARRANTED verdict cannot choose the exemption's form after seeing numbers, the form is fixed here. Inside a band the policy key
becomes `(max(0, core_held(anchor) - core_held(item)), <the policy's in-band key>)`: members that hold at least as many core pieces as the anchor
come first, ordered as today; members that drop a core piece come after. With it the band's first item never holds fewer core pieces than the
anchor, which is the card's "never displaced". The plainer fix (leaving the core's ownership out of the lineup sum) does not guarantee that: a
lineup with no core piece and eight low-owned players can still have the lower sum. Per step this key changes the pick exactly at the D picks
(a pick that is not a D pick has deficit 0 and stays first among the deficit-0 members; a D pick has deficit above 0 and the anchor, deficit
0, precedes it), so D is also the count of picks the exemption would change.

This chunk adds the key to `tiebreak.rank` behind an argument no caller passes, with its tests. It adds no config key and does not reach
`_greedy`, so the shipping path is unchanged.

## Data

- **Real slates (decide):** Ben's runs 20260929-222125-classic and 20260930-214909-classic, replayed on his machine from each run's saved inputs
  (`inputs/DKSalaries.csv`, `inputs/DKEntries.csv`, clock = the manifest's `created_utc`) through today's tree, offline, in scratch roots, with
  `run_slate(scenario=True, offline=True)`. The saved scenario caches hold no candidates and no design draws, so a replay is the only way to
  reach the fill. The replay's inputs are not frozen (the history store has grown since; offline takes no odds, so every game takes model
  intensities; no contest page is fetched, so field sizes and payouts may fall to lobby rows, templates or priors). Each replay prints every
  contest's family, PAYOUT_SOURCE and FIELD_SIZE_SOURCE, because the policy follows the family and a replay can route a contest differently
  from the original run. Both slates sit inside the standings that suggested the question: any result is IN-SAMPLE and says so.
- **Synthetic bed (decides nothing):** the committed `tests/fixtures/late_swap/classic` fixture (6 teams in 3 games, 2 goalies per team) with its
  entries cloned to 40 in its one contest ("NHL Synthetic Classic", the config default family large_gpp, field at the family prior), priors only.
  The cloud container that builds this chunk has no `runs/` folder, so the bed is the only slate it can run.

## Arms, seeds and settings

- **Deciding arm (`cloned30`):** the inputs with the entries file cloned to 30 entries (`tests/pool_builder.clone_entries`; the clones join the
  first entry's contest), the shipping tree, seeds 20261013 to 20261017. Thirty because the real runs hold 5 to 10 entries (the 09-30 run held
  five; the four slates on record before 10-03 held 5 to 10), too few to count anything, while Ben's 2026-10-11 Classic slate held 28; the overlap and repeat caps make the band matter more at that size.
- **Information only, deciding nothing:** `as_entered` (the inputs as saved, seed 20261013); `mean` (the cloned30 inputs with large_gpp,
  small_field and wta set to policy `mean` in memory, seed 20261013: the core retention of the portfolio the fill builds with no ownership
  order; the patch also reaches the provisional pass, which reads the same file).
- **Scenario counts:** real replays run the shipping counts of `config/risk.yaml` (design 5,000, selection 20,000, referee 20,000, field
  growth target 5,000), because the band's width follows the standard error and smaller counts would widen it and inflate M. The bed runs
  "medium" (design 1,000, selection 3,000, referee 3,000, field target 1,000), so bed and real rates are not comparable.
- **Noise baseline first (B98):** the cloned30 arm at seed 20261013 twice on identical inputs. Reported: whether the published lineups are
  identical (sha256) and whether N, M, D and U match. Any other python process at start and `PYTHONHASHSEED` are recorded. Runs are one at a time.

## Verdict, mechanical, in this order

Per real slate, on the cloned30 arm pooled over its five seeds:
1. The slate failed to run, or has no eligible pick: NO VERDICT.
2. WARRANTED if D / N >= 0.10 AND D >= 1 in at least 4 of the 5 seeds.
3. REJECT if D / N < 0.03.
4. Anything else: INCONCLUSIVE.

Study verdict:
- Any synthetic slate in the run: NOT MEASURED on real caches. The per-slate statuses are printed labeled synthetic and decide nothing.
- Fewer than two real slates: NOT MEASURED.
- WARRANTED on both real slates: WARRANTED. REJECT on both: REJECT. Anything else: INCONCLUSIVE.

The thresholds (0.10, 4 of 5, 0.03) are judgment; no earlier measurement stands behind them. At 30 entries and five seeds N is at most 150 per
slate, so 0.10 is about 15 picks moved off the core and 0.03 about 4.

## What ships, whatever the verdict

No pick changes in this chunk. WARRANTED opens a follow-up Ben starts that passes the key above from `_greedy` behind a config key, OFF until
C51 reads the Classic objective (B112). REJECT closes B46's exemption question for these slates. NOT MEASURED, NO VERDICT and INCONCLUSIVE keep
the real run open (B126) with this rule unchanged. A NOT MEASURED close is never reported as a REJECT.

## Out of scope, named

- `provisional.rank` (`build/provisional.py:86-111`): its own band and policy key; its file is superseded by the scenario pass on a full run.
- Late swap's `swap_objective.choose` (`build/swap_objective.py:474-478`): the same `rank` call on the near-lock path; not measured here (B126
  names it as the next place to look).
- B46's second and third clauses (the leverage and duplicate penalties moved to the depth slots, the priors-wrong sleeve sized by the chalk
  team's P(at most 1 goal)): not this card.

## Caveats written before the numbers

- D counts how often the rule fires against the core at one step, not whether the portfolio ends up without the core; the `mean` arm's
  retention is the portfolio-level comparison and decides nothing.
- The core comes from the model's own projection and ownership forecast. On 09-30 the forecast missed the team concentration (Toronto 42
  percent forecast against 21 to 24 actual, Colorado 19 against 38 to 51), so the model's core can differ from the realized chalk.
- The band is narrow (3 percent of the knob score or one standard error), so M is expected to be small; the read-only audit cited in C22's queue
  entry found own and dup reorder lineups only inside it.
- Two slates, both in-sample. A WARRANTED result is a reason to build the switch, not evidence that keeping the core wins.

## Result

<!-- RESULT MARKER: everything below is appended after the runs; nothing above this line is edited -->
