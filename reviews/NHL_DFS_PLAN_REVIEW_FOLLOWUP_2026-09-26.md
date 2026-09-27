# NHL DFS plan: follow-up review

**26 September 2026 · Review of architecture revision 2 and supporting build documents**

## Assessment

I reviewed the updated architecture, review comments, build chunks, dependency graph, tracker, `CLAUDE.md`, and archived plan. **The revisions improve the design, but several contradictions should be corrected before implementing the build cards.** This is a document review, not runtime validation. The reviewed files were not changed.

I support the public contest-data and odds adapters, prospective data capture, simpler initial simulator, deterministic strategy checks, bounded LLM calls, and one-round default QA. My recheck confirmed that the [NHL odds endpoint](https://api-web.nhle.com/v1/partner-game/US/now) and [DK contest-detail endpoint](https://api.draftkings.com/contests/v1/contests/195958011?format=json) responded. The cited draftables endpoint returned **403**, reinforcing the need for optional adapters and reliable fallbacks. That response does not establish that the endpoint is unavailable to every client.

## Corrections before implementation

### 1. The opponent-field sampler suppresses the duplicates it needs to predict

C7a reuses `build/candidates.generate`, but C2a requires distinct lineups and minimum differences between candidates. Those are portfolio-construction preferences, not properties of an opponent field. Popular lineups must be able to appear hundreds of times; otherwise duplication penalties and tie-adjusted payouts will be distorted.

Reuse the legal-lineup solver, but sample opponent entries **with replacement**, retaining multiplicities. Apply diversification constraints only when modeling a particular entrant's portfolio.

References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C7a lines 489–492 and C2a lines 279–284.

### 2. The baseline instructions contradict “generate before networking”

C2b places draftables reconciliation before priors, construction, and publication. One HTTP request can consume roughly 22 seconds with the specified retry policy, exceeding the 15-second baseline target. The orchestration also does not explicitly invoke the solver-free fallback when candidate generation fails. Its two-second feasibility function returns `None` without distinguishing timeout from proven infeasibility.

Require a checked baseline using local inputs first, then network enhancement. Explicitly wire and test solver-import failure, empty candidate banks, timeouts, and unavailable sources. A search timeout must never become “no legal lineup exists.”

References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C2b lines 315–322, C2a lines 265–268, and C1 line 210.

### 3. Publication and independent validation lose safeguards during translation into chunks

C2b atomically writes a version, then *copies* it to the public output path. A crash during that copy can leave a partial public file. Its lock is per run, although different runs can write the same slate output.

Separately, the referee receives the already-parsed pool, reuses `check_lineup`, and checks entry identity/order only when an optional parent is supplied. That can miss a shared parser or constraint defect.

Make the public replacement atomic, lock the shared output destination, and require the referee to bind the final bytes to immutable salary/entry inputs using separate legality checks.

References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C2b lines 309–313 and C0b lines 180–183.

### 4. Player availability, roster eligibility, and editability are conflated

The architecture calls any non-`None` DK status, `isDisabled`, and `isSwappable` scratch signals. An unfamiliar status is not necessarily “out,” and a player being unswappable does not mean they will not play. The lock card also labels a game “started” before its start time because it subtracts the safety buffer.

Keep separate states for participation, DK eligibility, actual lock, and the engine's conservative stop-editing buffer. Define recognized status mappings; preserve unknown values as unknown. Make `refresh` use the same locked-slot protections as late swap.

References: [NHL_DFS_PLAN_AND_ARCHITECTURE.md](NHL_DFS_PLAN_AND_ARCHITECTURE.md), section 11 line 383; [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C8 lines 553–561.

### 5. Showdown's shared-person outcome needs an explicit implementation invariant

The architecture correctly requires CPT and FLEX to share one underlying outcome. However, C4 produces parameters per role row, and C5 produces simulated stats per role row. Its tests verify Captain arithmetic, but not that the two rows share identical underlying stats.

Simulate once per person, then map to role IDs. Test that CPT/FLEX base outcomes are identical in every scenario and that Captain scoring applies exactly once. Also test that a skater sampled as not dressing receives zero minutes and events; C5 currently specifies starter draws without explicitly consuming skater dressing probabilities.

References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C4 lines 366–386 and C5 lines 414–429.

### 6. Some objective tests encode the wrong strategy

C7b requires allocation to send the “highest-variance builds to WTA entries.” The objective is **tie-adjusted first-place equity**, not variance. A worse lineup can have more variance. It also requires the frontier to be monotone in exposure/stress settings, which is not mathematically guaranteed.

Test that WTA assignment favors first-place equity and that the reported frontier excludes dominated alternatives. Explicitly implement cash and satellite objectives too: the current `tail_utility` contract specifies only GPP and WTA.

Reference: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C7b lines 518–533.

### 7. The risk defaults and calibration labels need qualification

“At most 40% of fees dependent on one game” is impossible for a portfolio consisting entirely of one Showdown slate. That constraint would always invoke the infeasible-budget fallback. Use mode-aware concentration measures: Showdown can diversify Captains and game outcomes, but cannot diversify away its game.

Also, exact payout metadata does not make modeled payout probabilities calibrated. C7b only explicitly marks metrics uncalibrated when using a contest-family prior. Require separate evidence states for payout completeness, outcome calibration, and field calibration.

References: [NHL_DFS_PLAN_AND_ARCHITECTURE.md](NHL_DFS_PLAN_AND_ARCHITECTURE.md), section 7 line 203; [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C7b line 520.

### 8. The advertised build-order change has not actually been implemented

Revision 2 says ownership and payouts precede hockey-feature depth. Its dependency graph still requires history, opportunity/rate models, and the simulator before ownership. Fast refresh and late swap arrive later still.

Introduce ownership and payout metadata against the initial priors earlier, keep financial estimates explicitly provisional, and move basic lock-safe repair into the first usable slate milestone. Richer hockey features can then improve an already functioning workflow.

References: [NHL_DFS_PLAN_AND_ARCHITECTURE.md](NHL_DFS_PLAN_AND_ARCHITECTURE.md), section 14 lines 497–516; [chunks.yaml](chunks.yaml), C7a/C7b/C8 dependencies.

### 9. The NHL-only historical fallback is not specified sufficiently to replace MoneyPuck

C3 promises the same feature columns from box scores. But the listed box-score contract lacks primary/secondary assists, strength-specific minutes, shot quality, and shared-ice information required by C4.

Either add the necessary event/shift ingestion or explicitly populate unavailable features with labeled priors and missingness indicators. Identical column names do not establish equivalent data coverage. This matters because MoneyPuck is now the primary source and its fallback must be real.

Reference: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C1 line 222, C3 lines 345–354, and C4 lines 374–386.

### 10. The learning chunks bypass the architecture's overfitting safeguards

C7a permits fitting whenever *any* historical standings exist. C11 permits fitting after 30 Classic groups **or** 50 Showdown games, uses a generic ten-group holdout, and includes mixture-weight fitting. The architecture requires separate mode/label counts, fifteen held-out Showdown games, and stronger evidence before changing field-mixture weights.

Historical data should satisfy the same evidence requirements, not bypass them. Encode those requirements in one shared gate definition. Also let an experiment finish successfully with “challenger rejected”; requiring improved results to complete a development chunk encourages repeated holdout searching.

References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C7a line 495 and C11 lines 645–653; [NHL_DFS_PLAN_AND_ARCHITECTURE.md](NHL_DFS_PLAN_AND_ARCHITECTURE.md), section 12 lines 431–439.

## Two review conclusions I would qualify

### APPG is reasonable emergency information, but “strictly better” is unproven

Its sample period, role changes, and Showdown scaling need verification. Zero can be a real observed average; retain the raw value and distinguish uncertain history from confirmed missingness. Treat the proposed 60% weight as a challenger setting, not established superiority. APPG remains a sensible ownership hypothesis.

References: [NHL_DFS_PLAN_REVIEW_2026-09-26.md](NHL_DFS_PLAN_REVIEW_2026-09-26.md), M6 lines 77–81; [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C2a line 262.

### QA can accept some strategic changes before ownership calibration

The claim that it cannot do so confuses model uncertainty with Monte Carlo uncertainty. A prior field can reveal substantial modeled improvements, although they remain unvalidated predictions. I support deterministic tie-breaking and one-round QA for efficiency; the claim that strategic QA cannot work beforehand is too strong.

Reference: [NHL_DFS_PLAN_REVIEW_2026-09-26.md](NHL_DFS_PLAN_REVIEW_2026-09-26.md), M5 lines 71–75.

## Additional omissions

- **Actual financial settlement.** C10 grades ownership and forecasts, but lacks an explicit contract for matching actual entries to final ranks, applying final payouts/ties, and computing fees, net return, and drawdown. Those measurements are necessary to evaluate the second objective. Reference: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C10 lines 612–641.
- **Field-simulation scale.** Specify weighted duplicate counts, chunked scoring, and memory limits. A 20,000-scenario × 100,000-entry score matrix alone occupies about 8 GB at four bytes per score. Benchmark field construction and payout evaluation, not just player simulation.
- **Controlled reviewer access.** A `Read`-only agent cannot edit files, but can still read builder notes outside its packet. Restrict readable paths or supply the packet directly without file-reading tools. Non-fork isolation is the right choice; it is not a filesystem boundary. Claude's current documentation also describes `omitClaudeMd`, which may help control inherited briefing material. References: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), C9 line 579; [official Claude Code subagent documentation](https://code.claude.com/docs/en/sub-agents).
- **Safer development handoffs.** Replace `git commit -am` with explicit reviewed staging: it omits new files and includes unrelated tracked changes. Exclude full personal entry/standings exports from Git, retaining minimized fixtures. Allow targeted reading of relevant implementation dependencies instead of “nothing else,” which would make cross-chunk integration unnecessarily brittle. Reference: [BUILD_CHUNKS.md](BUILD_CHUNKS.md), session protocol lines 14–28 and C0a line 74.

The architecture remains a sound foundation. The immediate work is to reconcile its guarantees with the narrower contracts and tests that future implementation sessions will actually follow.

## Review scope and provenance

Reviewed files:

- `NHL_DFS_PLAN_AND_ARCHITECTURE.md`
- `NHL_DFS_PLAN_REVIEW_2026-09-26.md`
- `BUILD_CHUNKS.md`
- `BUILD_STATUS.md`
- `chunks.yaml`
- `CLAUDE.md`
- `archive/NHL_DFS_PLAN_AND_ARCHITECTURE_v1_2026-09-25.md`

Line references identify the versions reviewed on 26 September 2026 and may move after edits. Relative links work when this document is kept alongside those files. Prior DFS design notes informed checks of role identity, field duplication, and portfolio objectives; the findings above are grounded in the current documents. No implementation, repository scaffolding, or runtime tests were performed for this review.
