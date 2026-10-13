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

## Result (appended 2026-10-13 after the bed ran; nothing above the marker was edited)

**Verdict, by the rule above: NOT MEASURED on real caches.** Only the synthetic bed ran (this container has no `runs/` folder). Its per-slate
status by the same rule is INCONCLUSIVE (D/N 0.31, D in 3 of 5 seeds); it is synthetic and decides nothing. No pick changes, the exemption
key stays unwired, and the real run is B126. This is not a REJECT.

What ran: `PYTHONHASHSEED=0 python scripts/c22_chalk_core.py --out <scratch>` on commit `5cdc4f1`, in a Linux cloud container with no other
python process at start, 8 runs one at a time (240 seconds of run time). Every run is the whole offline scenario pass at "medium" (design 1,000,
selection 3,000, referee 3,000, field target 1,000). The bed's one contest, 297000001 "NHL Synthetic Classic", is large_gpp (policy
`own_then_dup`), field 5,000, PAYOUT_SOURCE=PRIOR, FIELD_SIZE_SOURCE=PRIOR; MODEL_STATUS and FIELD_CALIBRATION are PRIOR; RISK_BUDGET OK in
every run. The write guard refused 80 writes, all of them the known observation-log append (40 `os.mkdir` and 40 `open` of
`data/raw/observations/2026-10-13.jsonl`); no network attempt was made. The per-run records are `chalk_core_2026-10-13_results.json` and the
guard's list is `chalk_core_2026-10-13_guard.json`.

Noise baseline (first, as written): the cloned arm at seed 20261013 twice gave identical lineups and identical N, M, D and U.

| Seed | N | M (all own) | D | U | Core (selection mean x field ownership) |
|---|---:|---:|---:|---:|---|
| 20261013 | 40 | 37 | 18 | 2 | Delta C1 10.56 pts x 18.9%; Bravo RW1 7.90 x 25.2% |
| 20261014 | 40 | 38 | 0 | 17 | Charlie RW1 7.98 x 25.0%; Foxtrot C1 10.44 x 18.3% |
| 20261015 | 40 | 37 | 0 | 10 | Charlie RW1 7.86 x 24.4%; Alpha C1 10.31 x 18.3% |
| 20261016 | 40 | 38 | 24 | 3 | Echo LW1 8.54 x 22.1%; Alpha C1 10.35 x 18.2% |
| 20261017 | 40 | 36 | 20 | 2 | Bravo C1 10.28 x 18.1%; Bravo RW1 7.89 x 23.4% |
| Pooled | 200 | 186 | 62 | 34 | D/N 0.31; D in 3 of 5 seeds; M_dup 0 |

Information only, deciding nothing:
- As entered (the fixture's 5 entries, seed 20261013): N 5, M 4, D 1, U 1.
- The portfolio without the ownership order (policy `mean`, 40 entries, seed 20261013) held the core in 1 lineup twice, 11 once and 28 not
  at all; the shipping fill at the same seed held it 0, 12 and 28. Per step the tie-break moved 18 of those 40 picks off the core, yet the
  portfolio built without it holds the core no more often, so on this bed the per-step count overstates the portfolio effect. The person cap
  is not the reason (it allows 18 of the 40 entries per player, `config/exposure.yaml` 0.45). Why is not established here; one candidate
  (judgment) is the joint accounting of a contest's own entries (C18): once entries are placed, repeating the same pieces adds less to the
  contest, so the value-best pick at later steps moves off the core by itself.

What this does and does not say:
- The instrument runs end to end, the counts are reproducible on one seed, and the mean patch reaches the run (the core table shows policy
  `mean` for the contest).
- On the bed the tie-break moves 93 percent of picks (186 of 200), all on the ownership sum. At "medium" the selection standard error is wide,
  so nearly every candidate falls in the first band and ownership orders them. A real slate at the shipping 20,000 selection draws has
  narrower bands; the bed rate says nothing about it.
- The core changes in four of the five seeds because the bed's leading skaters are near ties (products 184.6 to 199.6 across the five cores),
  which is why D and U swing from seed to seed. A real slate's chalk (MacKinnon at 47 percent) is not a near tie; this instability is a bed
  artifact (judgment).

Disclosures, decided before this run unless marked:
1. A plumbing run preceded it (small counts, one seed): N 40, M 37, D 21, U 1. It showed the mean arm's core retention empty, because the
   instrument counted retention only for picks under an ownership policy and the mean arm has none. `core_report` then gained
   `retention_by_family` (every pick with a core, whatever its policy), which is the figure the preregistration names for that arm. The rule
   did not change.
2. The noise baseline's first run is also the cloned arm's seed-20261013 run; the preregistration did not say whether it is reused.
3. The bed's as-entered arm is the fixture's template, 5 entries in the one contest.

Next, and not done here (B126): the same script on the real 2026-09-29 and 2026-09-30 runs on Ben's machine, offline, in scratch roots, with
this rule. One block runs all three pending local replays (C22 B126, C21 B107, C20 B102); expect well over an hour, most of it the C22 step's
16 full runs at the shipping counts.

```powershell
# 1. In the repo root on master (after this pull request merges), with nothing else running
git pull
$env:PYTHONHASHSEED = "0"
$py = ".venv\Scripts\python.exe"
$runs = "runs\20260929-222125-classic", "runs\20260930-214909-classic"

# 2. C22 chalk core (B126): both slates in one call
& $py scripts\c22_chalk_core.py --out "$env:TEMP\c22_real" --saved-run $runs[0] --saved-run $runs[1]

# 3. C21 stack size (B107): both slates in one call
& $py scripts\c21_stack_study.py --out "$env:TEMP\c21_real" --saved-run $runs[0] --saved-run $runs[1]

# 4. C20 goalie term (B102): one call per slate, each run's own UTC clock read from its manifest as text
foreach ($r in $runs) {
  $clock = [regex]::Match((Get-Content "$r\manifest.json" -Raw), '"created_utc":\s*"([^"]+)"').Groups[1].Value
  & $py scripts\c20_goalie_experiment.py --out "$env:TEMP\c20_real_$(Split-Path $r -Leaf)" --salary "$r\inputs\DKSalaries.csv" --entries "$r\inputs\DKEntries.csv" --clock $clock
}

# 5. Hand a session these files: c22_real\results.json, report.md, guard.json; c21_real\results.json, report.md, guard.json;
#    each c20_real_*\summary.json, results.json, guard.json (all under $env:TEMP)
```

Corrections after the final review (2026-10-13, same session, by the independent advisor agent; nothing here changes a count or the verdict):
1. `saved_inputs` now refuses a late-swap or refresh child run (its manifest carries `parent_run_id`), which keeps its own inputs and a
   near-lock clock and would otherwise have been replayed silently; it names the parent to give instead. The two runs B126 names are parents.
2. `cores_agree` compares the two cores as sets (the same two skaters in another order agree) and reads None when no contest is eligible.
3. Each run's stdout line now prints every contest's family, PAYOUT_SOURCE and FIELD_SIZE_SOURCE, as the preregistration says, and the
   summary records whether the routing was the same in every run (`routing_same_in_every_run`); the IN-SAMPLE label prints only for the
   09-29 and 09-30 runs; `--seeds` outside 1 to 5 is refused instead of capped. The bed's committed results come from `5cdc4f1`, before these
   presentation changes, so they lack the routing field; the bed has one contest, routed large_gpp, PRIOR and PRIOR in all 8 runs.
