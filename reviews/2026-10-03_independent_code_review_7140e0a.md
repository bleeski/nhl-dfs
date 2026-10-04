# Independent code review of nhl-dfs

Review date: 2026-10-03. Requested revision: `7140e0a8ef2248c6e8326057396c4578295e0fa8`.

## Assessment and scope

**The lock and controller paths need correction before their safety guarantees can be relied on.** I reproduced two ways to publish changes after a game starts, an override repair that reintroduces a salary-file OUT player, and a portfolio-selection calculation that credits one owned entry for taking another owned entry's prize. These are deterministic defects, independent of whether the outcome model or field prior is calibrated.

This report contains **10 findings: 4 P1 and 6 P2**. P1 means address before relying on the affected path in live operation. P2 means a concrete correctness or measurement defect with a narrower trigger. Every finding below was reproduced against the engine code, rather than inferred from a losing slate. No finding establishes a profitable alternative strategy.

The review concentrated on initial generation and publication, per-cell locks, override/QA application, joint contest selection, concentration constraints, independent file validation, and settlement evidence counts. I read the requested operating contracts, rules, plan sections 7, 8, 10, 11, 12 and 15, relevant chunk cards, status flags, backlog, and supplied graded reports. This is a targeted code review with regression testing, not a claim that every source line or external adapter was exhaustively audited. Archived plans were not used as authority. Repository instructions addressed to the maintaining agent were treated as context.

The checkout started at the requested revision. During review, another change advanced HEAD to `14beeff03cb0a7d86ffd18731ace714b09513f9b`, adding standings synthesis, its tests, and backlog/status updates. A comparison against `7140e0a` showed **no change under `src/` or `config/`**, and no edits to the existing tests used by these probes. All engine locations below therefore also apply to that later HEAD. The new synthesis is outside this review's scope; its B43-B51 rows were checked for duplication, not independently validated.

No engine, configuration, tracked test, ledger, or existing documentation file was edited by this review. Synthetic runs and probe scripts were isolated under a temporary directory; there were no DraftKings account actions or live network research calls. This Markdown file is the deliverable.

## Validation evidence

- Existing suite, launched before the checkout advanced: `732 passed, 2 skipped, 1 xfailed`, in `648.81 s`.
- Command: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\Users\benja\AppData\Local\Temp\nhlr1003a`.
- Runtime actually used: Python `3.13.7`, NumPy `2.5.3`, SciPy `1.18.1`, pytest `9.1.1`. This describes the installed environment, not a fresh lockfile installation.
- Both skips were `tests/test_late_swap.py::test_real_post_lock_export_cells_are_readable`, one per mode, because the real `DKEntries.postlock.csv` fixtures were missing. Native acceptance on those files remains unverified.
- The expected failure is the known undressed shootout-scorer defect, B29. It is not presented again as a new finding.
- Ten additional findings were demonstrated by isolated probes. The reproduction recipes and observed results are included below. They are synthetic counterexamples, not backtests or estimates of live winnings.
- The full performance benchmark and live adapters were not rerun. The regression result does not establish the 30-second late-swap target or an end-to-end lock-time guarantee.

For the end-to-end probes, the starting fixture was `tests/fixtures/late_swap/classic/`, whose first game starts at `2026-10-15T23:00:00Z`. History, identity files, and HTTP cache paths were redirected to empty temporary locations. The initial baseline's projection was replaced with `PriorProjection` to isolate construction/controller behavior from optional history. The actual solver, splicer, referee, and publisher ran. Goalie reporting was suppressed only where it was unrelated to the assertion. Model-free objective probes used the production ranking, payout, selector, and controller-evaluation functions.

## Triage index

Triaged 2026-10-03 and re-verified at HEAD on 2026-10-04: all ten accepted (one modified in scope, one at Low priority); each finding below carries its Decision line.

| ID | Priority | Finding | Backlog relationship | Decision / reason / implementation |
|---|---|---|---|---|
| R01 | P1 | QA's default clock freezes, defeating its final lock check | New; C10 lock guard | ACCEPT (2026-10-03): confirmed in controller.py (the default clock closes over the mutable `now`); B52; chunk C14 (band 0). Re-verified 2026-10-04 (see the Decision line). |
| R02 | P1 | Initial run publishes after crossing a start boundary | Related to B42, but a separate pre-start-to-post-start failure | ACCEPT: confirmed (`_export_and_publish` takes no clock or lock state); B53; chunk C14 (band 0); B42 part 3 rides in C15. Re-verified 2026-10-04 (see the Decision line). |
| R03 | P1 | Controller repairs can reintroduce already-OUT players | New; extends the safety scope around B17 | ACCEPT: confirmed (repair `exclude_rows` carry the batch and locks, never the CSV OUT statuses); B54; chunk C26 (band 2 by Ben's rule: a dead roster spot, not an illegal file). Re-verified 2026-10-04 (see the Decision line). |
| R04 | P1 | Greedy selection credits displaced own-entry prizes | New; C8 joint portfolio objective | ACCEPT: confirmed (`pay_total += pay_k` without recomputing placed entries); B55; chunk C18 (band 1: the selector's own arithmetic, serving band 2 too). Re-verified 2026-10-04 (see the Decision line). |
| R05 | P2 | QA counts prize transfers between owned entries as improvement | New; different from B38 proposal ordering | ACCEPT: confirmed (`idx` restricted to `changed`); B56; chunk C18. Re-verified 2026-10-04 (see the Decision line). |
| R06 | P2 | Contradictory overrides pass batch validation | New; C7/C10 state validation | ACCEPT, MODIFIED: contradictory claims become CONFLICTED under the existing evidence policy, no new certainty rule; B57; chunk C26. Re-verified 2026-10-04 (see the Decision line). |
| R07 | P2 | LPT packing is mistaken for an infeasibility proof | Reopen/extend B36 | ACCEPT: feasibility accounting only, the 40% budget and B36's decision stand; B58; chunk C27 (band 2). Re-verified 2026-10-04 (see the Decision line). |
| R08 | P2 | Salary ID-set changes inflate independent slate groups | Extend B31 bookkeeping | ACCEPT: a measurement defect; group key from slate date plus games; B59; chunk C29 (band 3). Re-verified 2026-10-04 (see the Decision line). |
| R09 | P2 | QA leaves the manifest's current export hash stale | Related to B39, with a distinct provenance failure | ACCEPT: confirmed (controller publish never sets export_sha256; verify never checks it); B60; chunk C26. Re-verified 2026-10-04 (see the Decision line). |
| R10 | P2 | Referee accepts byte changes outside editable cells | New; C0b exact-template checking | ACCEPT at Low priority, scope MODIFIED to the validator (no claim about the writer or DraftKings' tolerance); B61; chunk C33 (band 3). Re-verified 2026-10-04 by code trace only. |

## Findings

### R01 [P1] Keep a live clock for the controller's final lock check

**Location:** `src/nhl_dfs/build/controller.py:280-281`, with the ineffective recheck at `513-517`; ordinary CLI caller at `src/nhl_dfs/cli.py:629-630`.

**Contract:** plan sections 10 and 11; C10 must preserve pinned cells and recheck the actual clock before publishing.

**Cause and impact:** `clock = clock or (lambda: now or datetime.now(timezone.utc))` captures the mutable local variable `now`. The next statement assigns `now` a datetime. Consequently every later call to the default `clock()` returns that same datetime, including the final lock recheck. This also happens when the CLI supplies its initial `now` explicitly, as it normally does. A custom advancing `clock` in a test would bypass the bug, which is why testing only that injection path is insufficient.

**Reproduction and observed result:**

1. Build a checked synthetic baseline before first lock, with no scenario cache required.
2. Apply a valid participation-OUT override to a selected player in `AAA@BBB`, using the controller's default clock, not a supplied `clock=`.
3. Replace the controller module's wall-clock provider with a controllable datetime provider. Begin at `22:50:00Z`; advance it to `23:00:01Z` during the real repair solve.
4. Capture arguments passed to `RunView.locks`.

All three lock computations received `22:50:00Z`. The controller published v2 removing `81000002`, whose game had started. The real simulated wall clock at publication was `23:00:01Z`.

**Bounded correction:** distinguish the immutable round-start timestamp from a callable live clock. Production calls must use the live clock for deadline and final lock checks. A deliberately fixed rehearsal clock should be explicit and follow the existing rehearsal/public-output separation. Do not derive the production clock from the `now` variable.

**Acceptance:** exercise both ordinary CLI-style `now=` calls and calls omitting `now`, without injecting a clock that hides the default-path bug. Advance time through EDIT_STOP and through LOCKED during a solve. In both cases the proposed changed cells must not be published; the incumbent hash and public bytes must remain unchanged. A before-boundary control must still publish.

**Confidence:** high; end-to-end reproduction. No occurrence on a live slate is asserted.

**Decision (2026-10-04):** ACCEPT (band 0; B52; chunk C14). Re-verified 2026-10-04 by a second reader at HEAD (src and config unchanged since 7140e0a): reproduced; the wall clock advanced to 23:00:01Z inside the repair solve and v2 still published, while a control with an injected clock published nothing. Corrections: the CLI freezes the clock too (cli.py:629 and :682), the 15 `_apply` test call sites dated 2026-10-15 need migrating, and the final recheck belongs under the publish lock.

### R02 [P1] Recheck lock time at every initial-run publish

**Location:** `src/nhl_dfs/build/run.py:440-448`, `493`, `569-583`; later passes use the same publication helper, including `src/nhl_dfs/build/scenario_pass.py:325`.

**Contract:** plan sections 8, 10 and 11: baseline first, optional work stops before lock, and computation crossing a lock boundary must preserve the predecessor.

**Cause and impact:** `run_slate` checks whether the first game has started once, before projections and construction. `_export_and_publish` receives neither a clock nor lock state and performs no time check. Phase A, network repairs, provisional selection, and scenario selection can therefore publish after a game starts. The initial-run path also does not consult the optional-work cutoff used by late swap. Its docstring's statement that a run refuses to publish once any game has started is not enforced at publication.

**Reproduction and observed result:** start `run_slate(..., offline=True)` at T-10 on the Classic late-swap fixture. Wrap the real `build_bank` so that, after it finishes, the supplied clock advances to one second after first start. Leave writing, checking and publication intact. The run returned `FILE_VALID=TRUE` and published a portfolio containing **16 cells from the now-started game** across its five entries.

This uses ordinary timestamped salary rows. It is distinct from B42's `In-Progress` parsing and already-started intake problem.

**Bounded correction:** give the initial-run publisher the same time/lock enforcement as the repair paths. Check before optional work and again before committing a changed export, accounting for any publication-lock wait. Once the relevant boundary is reached, retain a checked predecessor when available and report why enhancement stopped. For an initial build with no safe predecessor, use the lock-aware path or report the exact limitation; do not stamp an unsafe new assignment as the completed delivery.

**Acceptance:** advancing-clock tests for Phase A and for each later publication route. If v1 exists, a late enhancement must leave v1 and the public hash intact. Cover crossing EDIT_STOP as well as actual start, and a multi-game file whose early game is locked while later games remain open. Keep the test independent of `In-Progress` markers.

**Confidence:** high; end-to-end reproduction.

**Decision (2026-10-04):** ACCEPT (band 0; B53; chunk C14). Re-verified 2026-10-04: reproduced; v1 published at 23:00:01Z with 12 started-game cells (the review says 16, immaterial), FILE_VALID=TRUE and DELIVERY_STATUS=CHECKED. One guard in `_export_and_publish` covers phases A, B, P and S (three publish call sites exist); it diffs against the predecessor's pinned cells, and flag 20 (default taken) decides the no-predecessor case: ship and warn.

### R03 [P1] Carry all known exclusions into controller repair solves

**Location:** `src/nhl_dfs/build/controller.py:452-456`, `485-498`; full-pool loading at `src/nhl_dfs/build/packet.py:70-79`.

**Contract:** plan sections 10 and 11; deterministic exclusion of known OUT/DISABLED players while preserving pinned cells. Unknown status must remain distinct from OUT.

**Cause and impact:** `RunView` loads the full salary pool. Direct strategic swaps check the incoming row's CSV OUT status, but the solver-backed paths do not use that same exclusion set. A strategic `exclude` supplies only its target rows and `ls.not_addable`; correctness repair supplies only exclusions introduced by the current proposal batch and `ls.not_addable`. `not_addable` represents locks, not participation. Thus repairing one scratch can select a different player already marked OUT in the authoritative input. Geometry-only referee checks do not catch this participation regression.

**Reproduction and observed result:** in a temporary copy of the fixture salary file, set goalie `81000011` to `Status=OUT`, salary `1000`, APPG `100`. These intentionally extreme synthetic values make the failure deterministic. Initial construction correctly excludes him. Apply one valid OUT override to a different rostered goalie. With the normal baseline repair objective and solver, the controller accepted the override, published v2, and placed `81000011` in **three entries**.

The high synthetic projection is not the defect; it demonstrates that the hard exclusion is absent. A player does not become eligible to add because a repair objective favors him.

**Bounded correction:** construct one authoritative exclusion mask for all controller add/repair paths, combining CSV statuses, applicable known eligibility state, still-valid accepted exclusions, and the current batch. Map exclusions through person identity to all role IDs. Apply it consistently to direct swaps, strategic exclusions, and correctness repairs. Preserve already-pinned occupants and report them, as late swap does.

**Acceptance:** reproduce the case above and assert the already-OUT goalie is never introduced. Add an analogous skater case, a strategic-exclude case, and Showdown CPT/FLEX exclusion coverage. Include UNKNOWN controls that remain available and pinned-OUT controls that stay unchanged with the correct warning.

**Confidence:** high; end-to-end reproduction. This is not a proposal to exclude uncertain players.

**Decision (2026-10-04):** ACCEPT (band 2 by Ben's rule: a dead roster spot, not an illegal file; B54; chunk C26). Re-verified 2026-10-04: reproduced in 1 to 3 entries. Broader than reported: DK Starting=P backup goalies are re-introduced too, and rounds 2 and 3 forget round 1's accepted OUT override; reuse late_swap's out_people construction as the mask.

### R04 [P1] Score each candidate's change to total owned-contest utility

**Location:** `src/nhl_dfs/build/portfolio.py:362-375`, `419-426`; final joint recomputation at `429-443`.

**Contract:** plan section 7: entries in the same contest are copies and opponents, and allocation uses marginal portfolio coverage and joint returns.

**Cause and impact:** `_greedy` prices only the newly considered entry's utility against opponents plus already-placed own entries. It does not subtract prize/utility displaced from those earlier entries. It then adds the new payout to `pay_total` without recomputing the earlier payouts. Once a later entry beats or ties an earlier entry, `pay_total` can contain prizes that cannot be paid together. Both the utility increment and subsequent washout scoring are consequently wrong.

The final frontier recomputation is joint and does correct the displayed completed-portfolio totals. That does not repair the choices already made or create a better portfolio that none of the five greedy fills explored.

**Reproduction:** use two owned entries in a 100-entry WTA, $1 each, with a $90 first prize. The 98 opponent copies score 5. Repeat the following four equally likely synthetic score rows 1,000 times to keep Monte Carlo tie bands small; use three distinct legal candidate lineups and nonbinding concentration caps.

| Scenario type | A | B | C | Each opponent |
|---|---:|---:|---:|---:|
| 1 | 10 | 11 | 0 | 5 |
| 2 | 10 | 11 | 0 | 5 |
| 3 | 10 | 0 | 0 | 5 |
| 4 | 0 | 0 | 10 | 5 |

**Observed:** the production `_greedy` selected A then B for **all five configured kappas**. Exact joint evaluation gives A+B mean gross **$67.50**, with 25% zero-return scenarios; the available A+C gives **$90.00**, with no zero-return scenarios. During the A+B fill, the accumulated payout reaches **$180** in a scenario despite the contest having only a $90 prize. These amounts describe the synthetic fixture only.

**Bounded correction:** when evaluating an insertion, compute the change to the entire affected own-contest payout and utility vector, then update the slate total by replacing that contest's old contribution. Efficient rank updates are fine; crediting only the new entry is not. Use the same accounting in screening/local improvement where it is intended to represent portfolio marginal value. No global-optimality claim is required to fix this arithmetic error.

**Acceptance:** the counterexample must select A+C, and incremental payout/utility totals after every insertion must equal an independent `joint_payouts` recomputation. Include strict overtakes, ties across tiers, repeat lineups, multiple contests, and a cash contest. Total own cash from any contest/scenario must not exceed its prize pool.

**Confidence:** high; production selector and payout functions reproduced the counterexample. It says nothing about which live lineup has the best true winning probability.

**Decision (2026-10-04):** ACCEPT (band 1, serving band 2; B55; chunk C18). Re-verified 2026-10-04: reproduced (A+B chosen at all five kappas; exact joint $67.50 against $90.00 for A+C). The fix needs per-entry rank vectors or a sparse update to stay cheap at 150-max, and swap_objective.choose and late_swap.py:619 to 625 share the omission. Judgment: modest where entries sit in different large contests, large in WTA and small fields.

### R05 [P2] Evaluate QA gains across all affected owned entries

**Location:** `src/nhl_dfs/build/controller.py:233-239`, especially the restriction to indices in `changed`.

**Contract:** plan sections 7 and 8; C10's requirement for a material strategic improvement on paired scenarios, including portfolio effects.

**Cause and impact:** the QA evaluator correctly obtains joint metrics for each touched contest, and uses all entries for total payout risk. It then discards unchanged entries when computing utility gain and the tail comparison. An unchanged lineup's payout can change when the proposed lineup passes it. The controller can label a pure transfer between owned entries as a material improvement.

**Reproduction and observed result:** use the same 100-entry, $90 WTA described in R04, but make all 4,000 scenarios identical. Before: owned A scores 10, owned B scores 0, opponents score 5. Proposed change: B scores 11. A wins before; B wins after. Total owned payout and first-place equity are identical before and after. Feed the real `joint_payouts` results to `controller.evaluate`, with only B in `changed`.

The controller accepted the change and reported **`utility_gain=90.0` on both selection and referee draws**, `tail_before=0.0`, and `tail_after=90.0`. Actual aggregate improvement is **zero**.

**Bounded correction:** calculate the paired utility and tail deltas across every owned entry in each touched contest. Use `changed` to identify touched contests, not to exclude their other entries from economic effects. Unaffected contests can retain their cached contribution.

**Acceptance:** reject the zero-gain transfer above as inconclusive. Also reject a change that helps the edited entry but reduces aggregate utility, and accept a true beyond-band aggregate improvement. Keep before/after metrics on the same scenario rows. This is separate from B38's sequential proposal-order conflict.

**Confidence:** high; direct reproduction through the production evaluator.

**Decision (2026-10-04):** ACCEPT (band 1 as a ride-along of C18, which shares the joint-accounting primitive; B56). Re-verified 2026-10-04: reproduced (a pure transfer scores +90.0; in a 70/30 mix a +36.0 gain coincides with all-entry utility falling from $90 to $63). Caveat: aggregating over the touched contests moves the 3% band anchor, so choose it or a paired aggregate SE deliberately.

### R06 [P2] Validate overrides as a consistent batch, not against an unchanged original state

**Location:** `src/nhl_dfs/build/controller.py:387-404`; `src/nhl_dfs/models/overrides.py:156-157`, `220-237`.

**Contract:** plan section 11 and C7: conflicting reports are represented as conflicted evidence; one confirmed starter per team and team role capacities are enforced deterministically.

**Cause and impact:** every correctness proposal is validated against the same cached `role_state()`. Accepted proposals do not update that validation state. The later model-application helpers likewise validate each item against the original `roles`, even while mutating a copy. Individually acceptable claims can therefore be mutually impossible. A batch can confirm both goalies of an unresolved team; the controller excludes each as the other's backup, while subsequent model application quietly makes the last proposal's goalie the confirmed starter.

**Reproduction and observed result:** on a fresh synthetic baseline with neither BBB goalie confirmed, submit two same-time `goalie_start: false -> true` overrides for `81000023` and `81000024`. Both have valid structural fields and confidence 0.95. The controller reported **two accepted correctness changes** and removed both goalies from the repaired portfolio. Applying the accepted batch to the role model reported only `bravo g2|BBB|G` as confirmed, because it was last.

**Bounded correction:** validate the batch transactionally against its resulting state, including cross-proposal conflicts and capacities. Reject or mark contradictory same-game goalie claims as CONFLICTED under the existing evidence policy; do not manufacture certainty from input order. Keep the controller's accepted record, repair exclusions, and downstream role/model state consistent.

**Acceptance:** the two-confirmation batch must not be accepted as two independent verified facts or silently become last-wins certainty. Repeat with reversed order. Add two simultaneous additions that would overfill a PP unit and two updates whose `old` values become inconsistent. A compatible multi-player batch must still apply.

**Confidence:** high; controller and subsequent role-model application reproduced the inconsistency. Source truth itself was not verified by these synthetic claims.

**Decision (2026-10-04):** ACCEPT, MODIFIED (band 2; B57; chunk C26): contradictory claims become CONFLICTED under the existing evidence policy, no new certainty rule. Re-verified 2026-10-04: reproduced (both goalies of a team leave every entry); also validate against previously accepted overrides, which role_state() never reads, and make every consumer honor CONFLICTED.

### R07 [P2] Do not treat an LPT packing result as the minimum achievable fee share

**Location:** `src/nhl_dfs/build/exposure.py:99-108`, `168-186`.

**Contract:** plan section 7 and Ben's B36 decision: switch to a lineup-count cap when the dollar cap is infeasible, not merely when a particular packing heuristic misses it.

**Cause and impact:** `fee_floor` returns the maximum bin load from longest-processing-time greedy packing. That is the cost of one feasible packing, hence an **upper bound** on the optimal maximum load. It is not an infeasibility certificate or a lower bound. `concentration_cap` treats it as proof that the dollar budget cannot be met and removes the dollar cap in favor of LINEUPS mode.

**Reproduction and observed result:** fees in cents `[500, 500, 400, 400, 300, 300, 300]`, three usable goalies, default 40% budget. LPT produces bins totaling 1100/800/800, so `fee_floor` is **0.407407**. The engine reports `GOALIE_CAP=LINEUPS 3/7`, relaxed from 2, with no dollar cap. Yet the packing `[500,400]`, `[500,400]`, `[300,300,300]` is 900/900/900, or **one third each**, comfortably inside the requested dollar budget. The game-cap path has the same result.

**Bounded correction:** use a valid lower bound to prove obvious impossibility, and a bounded integer allocation/feasibility check where LPT cannot resolve the question. LPT may certify feasibility when its result is inside budget. A timed-out proof must be labeled unresolved, not infeasible. Preserve the existing legal-file fallback and disclose any actual relaxation.

**Acceptance:** the fees above remain in DOLLARS mode at 0.40 for both goalie and game caps. A single entry genuinely exceeding 40% of total fees still triggers the documented fallback. Check heterogeneous-fee examples against exhaustive small-instance partitions. This changes feasibility accounting, not Ben's budget.

**Confidence:** high; exact counterexample and production cap-function reproduction.

**Decision (2026-10-04):** ACCEPT (band 2; B58; chunk C27): feasibility accounting only, the 40% budget and B36's decision stand. Re-verified 2026-10-04: reproduced (fee_floor 0.4074 against a brute-force optimum 0.3333; LINEUPS 3 of 7 can put 52% of fees on one goalie). Judged to fire on mixed-fee slates only; an exact check over fee classes is cheap.

### R08 [P2] Count sporting slate groups independently of salary-file revisions

**Location:** `src/nhl_dfs/learn/settle.py:82-95`; mutable run identity is built by `src/nhl_dfs/build/run.py:170-179`.

**Contract:** plan section 12: repeated exports and overlapping observations are not new independent sporting evidence. B31 already deduplicates ownership labels by contest/date.

**Cause and impact:** Classic `slate_groups` and `groups_by_family` use the run's `slate_id`. That ID hashes the salary role-ID set. A fresh run after DraftKings adds a player receives a different slate ID even when the sporting games and entered contest are unchanged. Labels are deduplicated, but the independent-group counts are not. This overstates progress toward the ownership and strategy evidence floors.

**Reproduction and observed result:** pass `evidence_counts` two frozen Classic records with the same slate date, games, contest ID, family, and 300 ownership labels, but different salary-derived slate IDs. Result: **1 slate date, 300 labels, 2 slate groups, and 2 large-GPP groups**. These should describe one group. This represents two fresh runs, not a refresh child that already inherits its parent's slate ID.

**Bounded correction:** persist/use a stable sporting-group key, based on verified draft-group/game identities and date rather than the selectable salary ID set. Preserve distinct contest labels while counting their shared game set once. Ensure older records can be grouped from their saved game/date data and that groups without qualifying observations do not create evidence merely by existing.

**Acceptance:** duplicate export, initial-plus-fresh-salary run, and parent/child settlements each count one group for the same games. Distinct game sets remain distinguishable and their dependence can be clustered as the plan requires. Assert both total and per-family counts, not just label totals.

**Limit:** this reproduction does not show an actual fit or promotion being improperly allowed. Other gate requirements, including holdout counts, remain separate and can still block it. The demonstrated defect is the reported evidence count.

**Confidence:** high; direct evidence-counter reproduction.

**Decision (2026-10-04):** ACCEPT (band 3, a measurement defect; B59; chunk C29). Re-verified 2026-10-04: reproduced (two slate groups for one slate); group key {slate date}|{sorted games}; models/prefit.py counts_for has the same flaw.

### R09 [P2] Update and verify the top-level export hash after QA publication

**Location:** `src/nhl_dfs/build/controller.py:536-551`; verification omission at `src/nhl_dfs/cli.py:73-82`.

**Contract:** plan sections 10 and 12; C2b's manifest must identify the exact current export bytes. Related to, but narrower and more fundamental than, B39's stale metric tables.

**Cause and impact:** controller publication appends a version record and advances `run/current`, but never updates `m['export_sha256']`. After any successful QA/override change, that manifest field still identifies the previous version. `verify_run` checks the version-specific hash and input hashes, but not the top-level export hash, so it reports success while the manifest's current-artifact identity is inconsistent.

**Observed after a synthetic v1-to-v2 repair:**

```text
manifest export_sha256: e00e48c95af2447c4e4bc70d025c708aecff43facac4651a5978ad249e3eebb3
actual current v2 hash: bc73970f85da85dbb4964bc89a958d01ce0ab0cb78f6366b1f30d279fb1ba082
verify_run result:     True
```

The exact hash values are fixture-specific; the inequality is the assertion. The initial-run publisher already updates both version history and the top-level hash, so the intended invariant is visible in `build/run.py:599-600`.

**Bounded correction:** update the current export hash as part of successful controller publication, preserving the distinction between a retained run version and a public file that was not replaced. Extend verification to check the current pointer, matching version record, actual bytes, and top-level hash together. Keep before/after metric versioning under B39 rather than silently treating old tables as current.

**Acceptance:** accepted strategic and correctness changes keep all current-version hashes consistent; rejected/no-change rounds preserve them. Tampering only with the top-level hash must make verification fail. Cover compare-and-swap/public-write refusal so the report does not imply that a different public file was replaced.

**Confidence:** high; publication and verification reproduced together.

**Decision (2026-10-04):** ACCEPT (band 3, provenance only; B60; chunk C26). Re-verified 2026-10-04: reproduced (verify still passes after zeroing export_sha256); nothing reads the field today, so a pre-fix tolerance is needed rather than urgency.

### R10 [P2] Check raw byte preservation in the independent referee

**Location:** `src/nhl_dfs/referee/check_file.py:63-79`, `128-131`.

**Contract:** plan section 10 and C0b: all bytes outside editable roster cells, including quoting and metadata, remain unchanged; pinned cells are byte-identical.

**Cause and impact:** entry-line binding checks compare parsed field strings for non-roster columns. Pinned-cell checks also compare parsed text. CSV encodings with different raw bytes can therefore pass as identical. The implementation detects value changes, but does not independently verify the stronger byte-splicing contract that the writer and delivery claim rely on.

**Reproduction and observed result:** take a valid synthetic generated file. In one copy, wrap the numeric Entry ID in CSV quotes, leaving its parsed value unchanged. In a separate copy, wrap a pinned roster cell in CSV quotes, passing its original decoded text through `locked=`. Both files differ in bytes that the contract requires preserved. Both returned **`referee.ok=True` with no reasons**.

**Bounded correction:** independently tokenize raw entry-line field spans or use another independent raw-byte comparison that masks only explicitly editable cells. Compare all remaining bytes to the authoritative template/current parent. Pinned cells require a raw representation or direct parent-byte binding in addition to their decoded value. Do not import the writer's field-span implementation into the referee, which would weaken its independence.

**Acceptance:** reject semantic-equivalent changes to metadata quoting, escaping, and pinned-cell quoting. Continue accepting correctly spliced open-cell changes. Cover BOM, CRLF/LF, embedded instructions/player lists, quoted names, reordered position headers, and untouched pinned bytes.

**Limit:** the probe demonstrates a validator blind spot. I did not observe the current splicing writer itself making these byte changes, nor establish that DraftKings would reject quote-equivalent CSV. The defect is failure to enforce the repository's exact-byte contract independently.

**Confidence:** high; direct final-byte referee reproduction.

**Decision (2026-10-04):** ACCEPT at Low priority, scope MODIFIED to the validator (band 3; B61; chunk C33). Re-verified 2026-10-04 by code trace only, not by execution: the engine's splice path copies unchanged bytes verbatim, so only a hand-edited or re-saved file reaches it.

## Existing issues and interpretation boundaries

The supplied evidence already identifies weak ownership/field calibration, co-ceiling deficiencies, incomplete role/news coverage, and missing native post-lock exports. I have not turned the September 29 shared-core loss or October 1 goalie concentration into new strategy rules. Their sample sizes do not support doing so.

- B29's undressed shootout scorer remains a known defect and expected failure.
- B37 unresolved-goalie research, B38 proposal ordering, B39 stale QA metrics, B41 missing post-QA concentration enforcement, and the open portions of B42 remain relevant. They were not counted again as standalone new findings.
- B35's observed-code mapping expansion and B40's added status output are present. That does not validate market calibration or the modeled washout probabilities.
- B36's cap changes are present; R07 is a new counterexample to the claimed dollar-infeasibility test, not a request to alter the 40% setting.
- B31's label and financial deduplication does not solve R08's group identity problem.
- The conditional calibration report explicitly discloses that its league constants include evaluation dates. Its reported ratios remain diagnostics with that limitation, not untouched prospective validation. No new tuning recommendation follows here.
- The risk budget and unverified tie/ticket details are decisions already recorded in the [BEN] flags. This review does not silently replace them.

The first implementation priority is R01-R03, with unchanged-incumbent assertions at both EDIT_STOP and actual lock. R04-R05 should share a consistent joint-contest accounting primitive, while retaining independent comparison tests. The remaining findings strengthen input consistency, feasibility claims, provenance, and the measurement needed to judge future changes.

## Adjudication (2026-10-03, planning session)

All ten findings were accepted after reading the cited code; R06 and R10 were modified as the table says. Rows B52 to B61 carry them in BACKLOG.md and the Queue section of BUILD_CHUNKS.md places them: R01 and R02 in C14 at the head of the queue (band 0, a legal file), R04 and R05 in C18 (band 1), R03, R06 and R09 in C26 and R07 in C27 (band 2), R08 in C29 and R10 in C33 (band 3). The review's own order (R01 to R03 first) was tested against Ben's exception sentence: R01 and R02 can leave a file DraftKings rejects, so they go first; R03 leaves a legal file with a known-OUT player, a washout, so it ranks at the top of band 2 and its fix is small enough for Ben to pull forward.
