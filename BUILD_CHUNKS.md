# NHL DFS engine: build chunks

**Companion to `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (revision 3) · final · 26 September 2026**

Seventeen chunks in objective order, each sized for one Claude Code session without compaction. Fifteen are unconditional; C12 and C13 are gated on data. `chunks.yaml` is the machine-readable dependency graph; `BUILD_STATUS.md` is the tracker; `tools/next_chunk.py` (built in C0a) enforces the order. This file is the brief a session reads instead of the architecture document.

---

## How a development session works

**Start.**

1. Open Claude Code in the repo root. `CLAUDE.md` loads automatically and is short.
2. Run `/nhl-dev-next` (after C10) or, until then, `python tools/next_chunk.py`. It reads `BUILD_STATUS.md` and `chunks.yaml`, runs the exit checks of every DONE predecessor, and prints the one eligible chunk. If a predecessor's checks fail, it refuses and names the failure; fix that first (as a repair inside the current session, logged in the tracker) before starting new work.
3. Read the chunk card, the files its "Read first" line names, and the modules the chunk imports (their public signatures, not their tests). Do not read the architecture document in full; cards cite section numbers for reference.
4. `python tools/next_chunk.py --start <id>` sets the tracker row to IN_PROGRESS with today's date. Stage and commit that change.

**Work.**

5. Build exactly the files the card lists, with the interfaces it names. Write tests alongside. Prefer small, typed functions with docstrings that state units (tenths, seconds, UTC).
6. Keep tool output small: `pytest -q`, `head`, `tail -n 20`. Never print a data file or a fixture larger than 50 lines into the conversation; fixtures are loaded by code.
7. If the session's context indicator passes roughly 60% before exit checks pass: stage and commit work in progress, write `HANDOFF: done=…; remaining=…; next=…` in the tracker row, leave it IN_PROGRESS, and end the session. The next session resumes the same chunk.

**Finish.**

8. Run the card's exit checks. All must pass. If one cannot pass in this session, the row becomes BLOCKED with the reason; never DONE. A chunk whose purpose is an experiment (C12, C13, C21 to C23) completes successfully with "challenger rejected"; an improvement is never required to finish.
9. `python tools/next_chunk.py --done <id> --commit <hash>` sets DONE, the finish date, and the commit. Append a session-log line to `BUILD_STATUS.md` (date, chunk, outcome, deviations from the card, new `[BEN: ...]` flags, backlog IDs).
10. Commit with explicit staging: review `git status`, `git add` the paths the card lists plus `BUILD_STATUS.md`, then `git commit -m "<id>: <title> (DONE)"`. Never `git commit -a`. Then either stop or, if context use is well under 60%, start the next eligible chunk in the same session (the rule exists to avoid compaction, not to cap chunks per session).

**Sizing rule.** A chunk is 600 to 1,500 lines of code and tests, at most eight new modules, and no open-ended research. Where a card says "discover" or "benchmark", the result is written to a file the next session can read.

**Repair rule.** A defect in an earlier chunk is fixed now only if it blocks the current chunk; otherwise write the failing test and a `BACKLOG.md` row.

**Git hygiene.** Full personal DK exports (`tests/fixtures/real/`, `data/standings/`, `data/raw/`, `runs/`, `outputs/`) are gitignored. Only minimized fixtures under `tests/fixtures/mini/`, `tests/fixtures/http/`, `tests/fixtures/history/`, `tests/fixtures/standings/` are committed. Tests that need a real file skip with a loud message when it is absent.

---

## Ben's prerequisites (outside any session)

- **Before C0a:** Python 3.11 or newer, Git for Windows, Claude Code working in `C:\Users\benja\Documents\Claude\nhl-dfs`. The two DK rule text files reachable at `C:\Users\benja\Downloads\NHL Classic.txt` and `NHL Showdown Captain Mode.txt` (C0a copies them into `docs/rules/`).
- **Before C0b:** one real Classic pair and one real Showdown pair of `DKSalaries.csv` and `DKEntries.csv` (reserve at least one entry, a free or minimum-fee contest is enough, so the entries template downloads). Save as `tests/fixtures/real/<YYYY-MM-DD>/classic/` and `.../showdown/`. C0b is BLOCKED without them; the template is never reconstructed from memory.
- **After C1:** run `tools/register_capture_task.ps1` once so daily snapshots start.
- **Before C11:** one Classic and one Showdown standings export (`Download Standings` from a finished contest) under `data/standings/inbox/`.
- **Any time:** answer the seven flags in plan section 15 by editing the named config or telling a session; the tracker lists them.

---

Added 2026-10-03 (the queue's deferred items name them): fill `winnings.csv` under `data/standings/inbox/2026-09-29` (two contests), `2026-09-30`, `2026-10-01` and `2026-10-02` from DraftKings My Contests and settle 09-30 again; save one real post-lock entries export as `tests/fixtures/real/<date>/<mode>/DKEntries.postlock.csv`; register the scheduled refresh (`tools/register_refresh_task.ps1` or the Claude scheduled task); overturn any of flags 11 to 20 in BUILD_STATUS.md you disagree with (each has a default in force and nothing waits on it).

## Dependency graph

```
C0a ─► C0b ─┬─► C1 ──┬──────────────► C4 ─► C5 ─┬─► C6 ─┐
            │        │                          │       ├─► C8 ─► C9 ─► C10
            │        │                          └─► C7 ─┘    │      │
            └─► C2a ─┴─► C2b ─► C2c ─► C3 ──────────────────┘      │
                                                                    ▼
                                            C11 (needs C8, C3, C0b) ─► C12, C13 (gated)
Exact edges are in chunks.yaml. C9 needs C8, C7, C2c. C10 needs C9, C7. C13 also needs C6.
```

**Order (from 2026-10-03):** `chunks.yaml` file order is the rank, set by Ben's priority rule (see Queue below): band 0 a legal file, band 1 win large prizes, band 2 prevent portfolio washouts, band 3 everything else; inside a band, impact against difficulty; a prerequisite moves up to just before its first dependent; two chunks that edit the same functions are ordered with `depends:`. NEXT (`python tools/next_chunk.py`) takes the first chunk in that order whose dependencies are DONE, skipping GATED chunks and chunks BLOCKED on a [BEN] flag. The DONE chunks ran in dependency order: C0a, C0b, C1 or C2a, C2b, C2c, C3, C4, C5, C6 or C7, C8, C9, C10, C11.

**Milestones.** After C2c: an uploadable legal file, offline, with lock-safe late swap. After C3: a provisional leverage-aware portfolio using ownership, duplication, and real payout metadata on priors. After C8: a scenario-based, objective-aware portfolio. After C11: the learning loop and financial ledger.

---

## Queue

Ben's priority rule (2026-10-03), the repo's first ranking rule. Band 0, ahead of everything: a defect that has lost, stranded or double-entered a delivered file, or that an ordinary slate is likely to reach, because a slate with no legal file wins nothing. Band 1: anything that helps win large prizes in the contests Ben enters (stack shape and size by slate size, goalie choice and share, ownership against the field and its stack mix, tail and ceiling terms reaching the solver, contests routed to the right shape, Showdown Captain choice, DTD priced as risk and leverage, controls that never reach the solver); an item that serves bands 1 and 2 ranks in band 1. Band 2: anything that prevents portfolio-level washouts (one goalie, one game, one core; caps that bind per lineup but not across the portfolio; blank, stranded or duplicate entries). Band 3: everything else by impact against difficulty. Dependencies override rank. Sizes: S under 100 changed lines, M 100 to 500, L 500 to 1,500; XL is split. Every band-1 figure below comes from one week of standings (17 contests, 52,490 Classic and 341 Showdown lineups) and is thin; a modeled figure is labeled modeled and its calibration is PRIOR or UNVALIDATED unless stated. The section between the markers is generated: edit `chunks.yaml` and run `python tools/next_chunk.py --render-queue`.

<!-- QUEUE:BEGIN -->
Generated 2026-10-07 by `python tools/next_chunk.py --render-queue` from chunks.yaml and BUILD_STATUS.md. Do not edit by hand; edit chunks.yaml and rerun.

DONE: C0a, C0b, C1, C2a, C2b, C2c, C3, C4, C5, C6, C7, C8, C9, C10, C11, C14, C15, C16, C38, C17, C39.

### Defaults taken (overturn any in one line)

| Flag | Blocks (rank) | Question | Default in force | Where it lands | Recommendation |
|---|---|---|---|---|---|
| 35 | C40 (#1) | Daily Faceoff stamp (B73, C40): the first look was inconclusive (the stamp missed 4 of 85 real lines changes, 4.7%, but the one-sided 95% upper bound is 10.08% against the 10% limit). Wait for ONE fixed second look, or close C40 as REJECTED now (the age gate stays as it is)? | Wait: C40 stays BLOCKED and off the queue (`needs: [35]` in chunks.yaml) until a session runs the single second look at cutoff 2026-10-21T13:30:00Z (same rule, all fetches up to it); that session then removes `needs: [35]` and sets C40 TODO (ADOPT, ADOPT WITH A LIMIT) or closes it (REJECT; a second BLOCKED counts as REJECT). Nothing about the design for a reopened C40 is in force until then | `docs/experiments/2026-10-07_c40_df_stamp_rule.md` (addendum), `docs/sources.md`, `scripts/c40_measure.py` (C40) |  |
| 2 | nothing (default proceeds) | Risk budget | P(lose ≥80% of slate fees) ≤ 0.60; ≤40% of fees on one goalie; ≤40% on one game only when the slate has more than one game (Ben 2026-10-02: 40% goalie and game fee share with the LINEUPS fallback; session log) | `config/risk.yaml` (C8) | Record Ben's 2026-10-02 answer in the flags table: 40% goalie and game fee share with the LINEUPS fallback (done in this plan); C27 fixes the feasibility test behind it. |
| 3 | nothing (default proceeds) | Typical entry mix (count, fees, contest types) | 20–150 entries across 150-max GPPs and Showdown; occasional WTA and single-entry | `config/contest_families.yaml` (C3) | Replace the placeholder mix with the observed one (5 to 10 entries per slate, 1 to 2 per contest, fees $0.10 to $1, 20-max and 150-max GPPs plus Showdown) so family priors and C21's sleeve sizes fit what Ben plays. |
| 8 | nothing (default proceeds) | Provisional leverage tie band and DTD haircut | Band = 3% of the lineup's prior mean (no Monte Carlo floor before C6, `band_floor_sims: null`); a QUESTIONABLE (DTD) person counts at 0.85 of their mean in the provisional ranking; the field fades DTD players by 2 points | `config/contest_families.yaml` `selection`, `config/ownership.yaml` `weights.questionable` (C3) | Keep the 0.85 DTD haircut and the 2-point field fade until C24 measures sitting DTD players; no change recommended now. |
| 9 | nothing (default proceeds) | DK tie settlement: are the tied places' prizes pooled, each share rounded down to the cent, and how are tied satellite tickets split? | Pooled over the tied places, floored to the cent; a tied seat is split as seats / tied count. The DK Terms of Use (checked 2026-09-29) verify only the even split | `docs/payouts.md`, `build/objectives.py` (C8) | Keep the pooled-and-floored tie rule; C16 and C18 both rely on it and no tie has involved a paid place yet. |
| 11 | nothing (default proceeds) | Dev dependency group and one lockfile (B34): move pytest and pytest-timeout to a dev group and keep uv.lock or requirements.lock? | Unchanged: both lockfiles kept, pytest stays a runtime dependency | `pyproject.toml`, `uv.lock`, `requirements.lock` (deferred item) | Choose uv.lock as the one lockfile and a dev group for pytest and pytest-timeout (B34); nothing in the queue waits on it. |
| 12 | nothing (default proceeds) | Showdown Captain policy (B49): a goalie as Captain only when the simulator's goalie ceiling ranks top-3 on the slate? | Yes, as the experiment default; C23 measures before any file changes | `config/ownership.yaml` `field.captain_rules`, `build/candidates.py` (C23) | Yes as the experiment default: a goalie Captain only when the simulator's goalie ceiling ranks top-3; C23 measures before any file changes. |
| 13 | nothing (default proceeds) | Skaters against the lineup's own goalie (B45): a hard rule for GPP families, cash exempt? | Hard rule for large_gpp, small_field and wta; cash and satellite exempt; the referee reports it | `build/candidates.py`, `build/exposure.py` (C20) | A hard rule for large_gpp, small_field and wta lineups, cash and satellite exempt, reported by the referee; C20. |
| 14 | nothing (default proceeds) | Payout tables by template (B62, B50): price a contest on DraftKings' cached table for the same template name and max entries when its page answers 403? | Yes, labeled PAYOUT_SOURCE=TEMPLATE; never for satellites or resized contests | `models/contests.py`, `learn/ledger.py` (C16) | Yes: price on the cached template table when the contest page is 403, labeled PAYOUT_SOURCE=TEMPLATE, never for satellites or resized contests; C16. |
| 15 | nothing (default proceeds) | Started-slate builds (B42 part 3): when a run starts after the first game, build the open games and report the started ones as excluded instead of refusing? | Yes: build the open games, exclude STARTED_GAME rows, pin started-game cells, name them in the report | `build/run.py`, `build/locks.py` (C15) | Yes: when a run starts after the first game, build the open games, exclude STARTED_GAME rows, pin started-game cells and name them in the report; C15. |
| 16 | nothing (default proceeds) | Cloud settle inputs (B68): what a cloud run persists so settle can work after the container is reclaimed | Persist only the small frozen record (contest ids and forecast fields, a few KB), never the 52 MB scenario directory; settle degrades to money and lineup match from data/entered and says so | `learn/settle.py`, `data/entered/` (C29) | Default taken: persist only the small frozen record for a cloud run (the contest ids and forecast fields, a few KB) and never the 52 MB scenario directory; settle degrades to money and lineup match from data/entered and says so (B68, C29). |
| 17 | nothing (default proceeds) | QA relay (B81): what happens when a relayed adversary packet value is wrong | Relaunch the adversary with the exact printed packet; one reply per round; qa-apply records the packet hash it evaluated against | `build/controller.py`, `.claude/skills/nhl-run` (C31) | Default taken: when a relayed packet value is wrong, relaunch the adversary with the exact printed packet; one reply per round; qa-apply records the packet hash it evaluated against (B81, C31). |
| 18 | nothing (default proceeds) | History store on cloud (B71): where it comes from | `tools/cloud_bootstrap.sh` starts the history backfill in the background when the store is absent; Phase A never waits for it | `tools/cloud_bootstrap.sh`, `build/run.py` (C39) | Default taken: tools/cloud_bootstrap.sh starts the history backfill in the background when the store is absent and Phase A never waits for it (B71, C39). |
| 19 | nothing (default proceeds) | Pre-lock refresh for cloud-built runs (B77) | The delivery message names the refresh command and the lock time; no cloud scheduled routine is created until Ben registers one | `.claude/skills/nhl-run` and `nhl-refresh` (C43) | Default taken: the delivery message names the refresh command and the lock time; no cloud scheduled routine is created until Ben registers one (B77, C43). |
| 20 | nothing (default proceeds) | First publish with a started game (B53) | A first publish with no predecessor is never blocked for EDIT_STOP cells (it warns) and never changes cells of a game that has started; with a predecessor, pinned cells are diffed against it | `build/run.py` `_export_and_publish`, `build/locks.py` (C14) | Default taken: a first publish with no predecessor is never blocked for EDIT_STOP cells (it warns) and never changes cells of a game that has started; with a predecessor, pinned cells are diffed against it (B53, C14). |

### Ranked chunks

| # | Chunk | Band | Title | Size | Effort | Depends | Needs | Backlog | Findings | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | C40 | 1 win | Daily Faceoff age gate keeps the last known lines | S | medium: config/roles.yaml max_line_age_h, models/roles.py:224 (the age gate), the Daily Faceoff adapter page stamp, tests with the CHI 28.7 h, FLA 36.9 h and COL 58.6 h cases | C7 | 35 | B73 | none | BLOCKED |
| 2 | C18 | 1 win | Joint own-entry accounting in the greedy fill and the QA evaluator | M | high: build/portfolio.py 625 (_greedy, _joint_from_states), build/objectives.py 722 (own_pairwise, metrics_from_ranks, joint_payouts), controller.evaluate (about 60 lines of build/controller.py) | C14 | none | B55, B56 | R04, R05 | IN_PROGRESS |
| 3 | C19 | 1 win | Field stack mix by slate size, team4 and double-stack behaviors, mixture weights, shape report | M | medium: models/field.py 354 and models/field_fast.py (stack rules), config/ownership.yaml mixtures, the stack family report in build/packet.py, scripts/standings_synthesis.py pooled shape table as the target | C17 | none | B43, B15 | none | TODO |
| 4 | C20 | 1 win | Goalie as the top-1% factor, unresolved goalies in research, no skater against the lineup's goalie, goalie choice with leverage | M | high: build/packet.py research request (B37), build/candidates.py 155 and build/milp.py (the constraint), build/portfolio.py and build/objectives.py (goalie term), build/exposure.py shared-failure report | C18 | none | B37, B45 | none | TODO |
| 5 | C21 | 1 win | Stack size by slate blow-up, discovery sleeves and the shape mix (experiment) | M | high: build/portfolio.py discovery families (risk.yaml discovery.*), build/candidates.py, the saved scenario caches of the 09-29 and 09-30 runs for the study, a docs/experiments/ preregistration | C19, C18 | none | B44, B22 | none | TODO |
| 6 | C22 | 1 win | Chalk core kept, leverage applied to the depth slots (experiment) | S | medium: build/tiebreak.py, build/provisional.py 230, the own_then_dup region of build/portfolio.py | C19 | none | B46 | none | TODO |
| 7 | C23 | 1 win | Showdown Captain choice, skater Captains, Captain leverage, team split sleeve (experiment) | M | medium: models/field.py captain rules, the Showdown branch of build/candidates.py and build/milp.py, build/exposure.py captain cap, config/ownership.yaml captain_rules | C20 | none | B49 | none | TODO |
| 8 | C24 | 1 win | DTD participation inside the simulator | M | high: sim/game.py 614 (dressing step), sim/outcomes.py, models/roles.py p_play, tests/test_frozen_record.py (frozen records move) | C6 | none | B14 | none | TODO |
| 9 | C25 | 1 win | Clean calibration constants, the co-ceiling cause, and the 3+ point tail preregistration | M | high: sim/validate.py, sim/game.py (a unit-level factor, read only), docs/calibration/2026-09-29.md and .json, the history store for actual linemates | C6 | none | B64, B6 | none | TODO |
| 10 | C41 | 1 win | Selection robust to field ownership error (low, base and high ownership) | M | high: models/ownership.py, models/field.py (sampled fields under scaled ownership), build/portfolio.py (screening and the knob loop), build/objectives.py (utility across field scenarios), config/ownership.yaml | C19 | none | B74 | none | TODO |
| 11 | C42 | 1 win | Improvement pass after the greedy fill (experiment) | M | high: build/portfolio.py _greedy (one pass, order family, fee, file; lines 360 to 430), the saved scenario caches of the 09-29 and 09-30 runs, a docs/experiments/ preregistration | C18 | none | B75 | none | TODO |
| 12 | C26 | 2 washout | Controller safety, known exclusions in repairs, caps after QA, transactional override batches, export hash | M | high: build/controller.py 559 (repair solves, apply_round publish), models/overrides.py 239, the caps API of build/exposure.py, cli.verify_run | C18 | none | B54, B41, B57, B60 | R03, R06, R09 | TODO |
| 13 | C27 | 2 washout | Portfolio caps, exact fee-share feasibility, line-core and PP-unit exposure, one goalie-share key | M | medium: build/exposure.py 282 (fee_floor, concentration_cap, concentration), the caps region of build/portfolio.py, config/risk.yaml and config/exposure.yaml | C18 | none | B58, B13 | R07 | TODO |
| 14 | C28 | 2 washout | Role state in Phase A and in simulate | M | medium: build/run.py Phase A (projection and goalie choice before the scenario pass), cli simulate, models/roles merge and apply_state | C15 | none | B9 | none | TODO |
| 15 | C43 | 2 washout | Cloud parity, the skills run under bash and cloud-built runs get a pre-lock refresh | M | medium: .claude/skills/nhl-run, nhl-late-swap and nhl-refresh (shell powershell, allowed-tools), tools/cloud_bootstrap.sh, build/scheduled.py (reads local runs only), flag 4 text, nhl.sh | C10 | none | B77 | none | TODO |
| 16 | C44 | 2 washout | Goalie and game caps in late swap and refresh | M | medium: build/late_swap.py:599 (assign.Caps.person_cap only), build/swap_objective.py, build/refresh.py, the caps API of build/exposure.py | C27 | none | B78 | none | TODO |
| 17 | C45 | 2 washout | Cross-mode fee-weighted risk (Classic and Showdown on one slate) | M | medium: build/exposure.py fee shares, config/risk.yaml, build/scenario_pass.py, the RUN_NOTES risk section, plan section 7 allocation paragraph | C27 | none | B79 | none | TODO |
| 18 | C29 | 3 other | Settle without a local run, stable slate groups | M | medium: learn/settle.py 339 (run, evidence_counts), learn/ledger.py, cli settle, scripts/standings_synthesis.py lobby_meta as the name and fee source | C11 | none | B47, B59, B68, B84 | R08 | TODO |
| 19 | C30 | 3 other | Standings tooling, the checklist sees filed standings, the synthesis runs after each pull | S | low: scripts/standings_checklist.py (status classification), .claude/skills/standings-checklist and nhl-settle SKILL.md, the scripts/standings_synthesis.py entry point | C11 | none | B48 | none | TODO |
| 20 | C37 | 3 other | Skills agree with CLAUDE.md, the status lists and the save-entered step | S | low: .claude/skills/nhl-run, nhl-late-swap and nhl-refresh SKILL.md (allowed-tools and the lines that forbid further commands), CLAUDE.md slate rules (lines 30 to 32), scripts/standings_checklist.py --save-entered usage; no src change | C43 | none | B76 | none | TODO |
| 21 | C31 | 3 other | QA throughput and provenance, independent same-entry proposals, packet team counts, round rule, metrics for the published version | M | high: the proposal loop of build/controller.py, build/packet.py 411, build/notes.py, the CLAUDE.md round rule, .claude/agents/nhl-adversary.md | C26, C37 | none | B38, B39, B80, B81 | none | TODO |
| 22 | C32 | 3 other | Hygiene, dead and v1-only config keys, docstrings, enum cleanup, apply-once, shootout shooters, raw eviction, page-age report | M | medium: config/*.yaml comments, sim/slate.py, models/roles.py page age, build/news.py NewsState, sim/game.py _shootout, data/history evict_raw; each item is small | C11 | none | B65, B29, B32, B10, B11, B83, B86 | none | TODO |
| 23 | C33 | 3 other | Referee raw-byte check of entry lines and pinned cells | M | medium: referee/check_file.py 170, the export writer's field spans (read for the contract, never imported), the real export fixtures | C0b | none | B61 | R10 | TODO |
| 24 | C34 | 3 other | Repair draws for large_gpp late-swap repairs | S | medium: the repair objective region of build/late_swap.py, build/swap_objective.py 630 (read for the draw count), config/runtime.yaml | C9 | none | B19 | none | TODO |
| 25 | C35 | 3 other | Fast field sampler agreement and field resampling in the standard error | M | high: models/field_fast.py (local search), models/field.py, the standard-error region of build/objectives.py, tests/bench_field.py | C19 | none | B12, B15 | none | TODO |
| 26 | C36 | 3 other | Odds history grading in settle | S | medium: the tools/capture.py index format, learn/grade_forecasts.py, learn/settle.py record fields, sim/market.py implied totals | C11 | none | B67 | none | TODO |

### Why each ranks where it does, and where to stop

- **#1 C40** (1 win): Band 1 (inputs that move lineups): the age gate drops a playing team lines and PP units whenever the page stamp is over 24 h (CHI 28.7 h and FLA 36.9 h on 10-01, COL 58.6 h on 10-03: 2 of the 2 cloud late runs on record), leaving the team at role priors, and a call-up with DK average 0 became a Showdown Captain on 10-03. Whether a stale stamp means stale lines is unchecked (judgment), hence measure first. Reach: any team whose lines do not change for a day, which is most teams. Evidence: three team-days. Breakpoint: first commit measures offline on the cached pages whether the Last updated stamp moves only on edits; the second applies the decision (keep the last known lines, label them stale with their age, unless newer contrary news exists)
- **#2 C18** (1 win): Band 1 and band 2 together, so band 1: the selector credits prizes that cannot be paid together (review R04: A+B chosen at every kappa, exact joint $67.50 against the available $90.00; the washout term p80 reads the same wrong totals) and the QA evaluator accepts a pure transfer between owned entries as a 90-point gain (R05). Deterministic. Reach: every contest where Ben holds two or more entries (his 20-max and 150-max contests; 1 to 2 entries today, so the magnitude is modest until he scales: judgment). Depends on C14 because both edit controller.py. Breakpoint: a joint-accounting primitive whose incremental totals equal joint_payouts on the review's fixtures, committed; then _greedy; then controller.evaluate
- **#3 C19** (1 win): Band 1: ownership level against the field's stack mix. Observed in 14 contests and 52,490 lineups (one week, three slate sizes: thin): 93.5% of large-GPP lineups hold a 3+ stack and 65.7% a 4+ stack, 4-3-1 modal at 31%, while the field model gives large_gpp a 25% stacker share with team3 only. A field with too few stacks understates the opponents a stacked lineup must beat. Depends on C17 (the build_fields signature). FIELD_CALIBRATION stays PRIOR until C12; this reshapes the prior, it is not a fit. Breakpoint: team4 and double_stack behaviors with tests, committed; then mixture weights per family and slate size; then the shape mix line in RUN_NOTES
- **#4 C20** (1 win): Band 1: the winning goalie sat in 91.8% of pooled top-1% lineups (field 54.1%); the top-1% goalie was a 9 to 15% owned starter on a favorite (Silovs, Daccord, Hofer) while the field's chalk goalies were 0% of the top 1%; top-1% lineups faced their own goalie 5.4% of the time against 11.7% for the field (14 contests, one week: thin). B37 is S and Ben approved it on 10-01. The constraint also removes a washout pattern (a lineup betting against itself). Depends on C18 (portfolio.py). Breakpoint: B37 (unresolved goalie pairs in the research request), committed; then the own-goalie constraint with its referee report (flag 13 default: hard in GPP families); then the goalie selection term as an experiment, DONE when rejected
- **#5 C21** (1 win): Band 1: stack shape and size by slate size. Top-1% lineups held a 5+ stack in 45 to 65% on the 3-game slate (field 20 to 23%), 8 to 21% on the 8-game slate (field 14 to 18%) and 4 to 36% on the 5-game slate; 4+ stacks were 83.5% of the pooled top 1% against 65.7% of the field; bring-back 39% against 34% (one week: thin, hence an experiment first). The engine has no notion of primary stack size today. Depends on C19 so the study runs against a field with the observed stack mix; B22's measurement is the study. Breakpoint: the preregistered sim study (top-1% equity by primary stack size per slate, on saved scenario caches) committed as docs/experiments/stack_size_<date>.md; then the sleeve only if the study supports it; DONE when the challenger is rejected
- **#6 C22** (1 win): Band 1: ownership level against the field. Top-1% lineups held the slate's top two chalk pieces at 90 to 100% (MacKinnon 47% owned; McDavid and Draisaitl 32% and 25%) and differentiated with 1 to 3 low-owned teammates; their own-sum exceeded the field's whenever the chalk hit (3 slates: thin). The read-only audit shows own and dup reorder lineups only inside the tie band, so today's penalty is small; this chunk measures before it changes anything. Breakpoint: a measurement of how often the band-only ownership tie-break moves a pick on the saved caches, committed; then the core exemption only if the tie-break moves picks away from the top-2 chalk; DONE when rejected
- **#7 C23** (1 win): Band 1: Showdown Captain choice, on the thinnest evidence in the queue (one game, ANA@VGK, 3 contests, 341 lineups): a goalie Captain was 20 to 33% of the field and 0% of the top 1%; top-1% Captains were 3 to 11% CPT-owned skaters; our three busted Captains were the field's chalk (Hart, Stone). Last among the band-1 construction items for that reason. Depends on C20 (the goalie constraint in Showdown). Breakpoint: the Captain-share and goalie-in-lineup report per cohort on the saved Showdown standings, committed; then the skater-Captain default (flag 12) and the leverage term as an experiment; DONE when rejected
- **#8 C24** (1 win): Band 1: DTD players priced as risk and leverage. Today a QUESTIONABLE player who sits scores 0 after the simulation, so his minutes and events are not redistributed to teammates (B14, design-time, unverified on data); the field already fades DTD by 2 points and the projection by p_play 0.85 (flag 8 default). Reach: every slate with a DTD skater (common: judgment). Low in band 1 because its evidence is design-time. Breakpoint: the participation draw inside the dressing step with conservation tests, committed; then the frozen-record and calibration updates
- **#9 C25** (1 win): Band 1: tail and ceiling terms reaching the solver, at the bottom of the band because the evidence is one season with a disclosed flaw (the league constants include the evaluation dates). The 3+ point tail ratio 1.40 and co-ceiling 0.55 against 1.59 are diagnostics until recomputed cleanly; this chunk is the prerequisite of B6 and of C13's gate. No tuning happens here. Breakpoint: the recomputed constants and a new docs/calibration/<date>.md with machine-readable deficiencies (B64), committed; then the co-ceiling cause test; then B6's preregistration (the Gamma factor itself belongs to C13 or a later experiment)
- **#10 C41** (1 win): Band 1: ownership level against the field. Plan section 6 asks for ownership as ranges and alternatives under low, base and high scenarios; nothing in src, config or the queue does this (C35 only resamples the field for the standard error). With per-player ownership error of 3.9 to 4.3 points and team stack shares off by 20 points on the 09-30 forecast, a selection optimal only at the base field can be badly placed when the field differs. Evidence: one graded forecast, so the measured spread comes first and selection use is conditional. Depends on C19 because that chunk changes the field model this one perturbs. Breakpoint: the ownership error band measured from the graded forecasts and committed as a function; then the field sampler draws from the low and high bands and the report prints each candidate tail spread; selection uses it only if the spread moves a pick
- **#11 C42** (1 win): Band 1 (tail terms reaching the solver): selection is a one-pass greedy fill with no improvement pass, where plan section 7 specifies bounded local search, and the order (family, fee, file) can decide which entries get the better candidates. Unmeasured (judgment). Depends on C18 because that chunk rewrites the same accounting. Experiment: DONE on rejected is valid. Breakpoint: the preregistration committed first; then the gap between the greedy portfolio and a bounded swap-improvement pass measured on the saved caches; DONE on rejected if the gain is inside the standard error
- **#12 C26** (2 washout): Band 2: entries sharing a failure point, and dead roster spots. A correctness repair can roster a player the salary file already marks OUT (review R03, reproduced in three entries), an accepted QA swap can breach GOALIE_CAP or GAME_CAP (B41), two contradictory goalie confirmations strip a team from every open cell (R06), and the manifest misnames the delivered bytes after QA (R09). All deterministic; R03 is reached on any slate where an override triggers a repair. Depends on C18, which edits controller.evaluate. Ben may pull this chunk ahead of the band-1 experiments: R03 is S on its own. Breakpoint: R03 (one exclusion mask for every repair path) with the review's goalie case and a skater case, committed; then R09 (export hash and verify); then B41 (caps after QA); then R06 (transactional batch)
- **#13 C27** (2 washout): Band 2: caps that bind per lineup but not across the portfolio. The dollar cap is dropped for a lineup-count cap whenever LPT misses a feasible packing (R07, exact counterexample: 0.407 against an achievable one third), and the plan's 30% line-core cap and the line and PP-unit fee exposure are neither enforced nor reported (B13, recorded at C8). Deterministic; reach: every multi-entry slate. Breakpoint: R07 (a lower bound plus an exact small-instance check; LPT certifies feasibility only) with the review's seven-fee case, committed; then B13's line-core and PP-unit fee shares and the 20+ entry count cap; then the one-key reconciliation of the goalie share
- **#14 C28** (2 washout): Band 2: one failure point in the baseline file. v1 (Phase A) and simulate still use the C5 table for goalies and dressing (B9 open part), so a run that stops after Phase A near lock can deliver a goalie who does not start; the 09-29 Adin Hill entry (0 points) was that pattern before C7 reached the later passes. Depends on C15 (run.py). Breakpoint: Phase A reads the merged role state (goalies and dressing) with a test that a CONFIRMED starter is used in v1, committed; then simulate
- **#15 C43** (2 washout): Band 2 (one failure point, no correction near lock): the 10-01 and 10-03 slates ran in cloud sessions, where the run, late-swap and refresh skills are PowerShell-only and the local scheduled refresh reads only local runs, so a cloud-built file never gets the T-60 refresh; the 10-03 file shipped with an unconfirmed goalie in 2 entries. Reach: every cloud slate. Evidence: two cloud runs. C37 follows it because both edit the same three skills. Breakpoint: the three skills run under bash with nhl.sh (checked headless on a cloud-like shell), committed; then the delivery message names the refresh command and the lock time (flag 19)
- **#16 C44** (2 washout): Band 2: caps that bind per lineup but not across the portfolio, after the first publish. Late swap and refresh apply only the per-person cap, so B17 goalie-gate repairs funnel every affected entry onto the confirmed starter (judgment: the same concentration the 10-01 report recorded at build time). Reach: any slate with a goalie change near lock. Evidence: code reading; not observed. Depends on C27 because it reuses the exact feasibility check. Breakpoint: late swap and refresh pass exposure.caps for the open cells and report GOALIE_CAP and GAME_CAP; a goalie-gate repair that would exceed the cap spreads across alternates; committed with a test on the B17 repair
- **#17 C45** (2 washout): Band 2: entries sharing one failure point across modes. Plan section 7 asks for fee-weighted risk across all modes on the slate; each run budgets only its own mode, so one goalie can be 40% of the Classic fees and the Showdown Captain at once (Ben played both modes on 10-02). Reach: slates with both modes. Evidence: a design requirement; no loss recorded. Breakpoint: the report states the slate combined goalie fee share across the two modes (report only), committed; then the cap uses it
- **#18 C29** (3 other): Band 3, first in it: measurement the other bands wait on. 20 of Ben's entries ($7.30 of fees, 13 contests of ownership labels) have standings but no local run and cannot be booked (B47), and the evidence counter double-counts a slate when the salary file changes (R08). Deterministic; the archive only grows through settle. Breakpoint: R08 (group key from slate date and games) with the review's two-record case, committed; then settle --no-run for money and labels; then the checklist shows the dates settled
- **#19 C30** (3 other): Band 3: measurement coverage. The checklist read filed 4 after 17 exports were filed because 13 contests had no entries file (B48), and no skill reruns the synthesis after a pull. Small and deterministic. Breakpoint: the checklist lists contests found only in filed standings, committed; then the synthesis step in the settle skill
- **#20 C37** (3 other): Band 3: the delivery rule already landed on master on 2026-10-03 (PR 5: the skills now present the file per CLAUDE.md and say a path alone is not delivery), so B69's stranded-file defect is addressed and only its observation on the next cloud run is open (deferred). What remains is narrower and was read, not run: nhl-run still says never to run another command while CLAUDE.md line 32 asks for scripts/standings_checklist.py --save-entered, a commit and a pull request, and no allowed-tools entry covers them; the report steps omit SEARCH_STATUS (run) and most of the statuses CLAUDE.md line 30 requires (late swap, refresh). Effect: the standings pull misses contests and reports are incomplete; no file or lineup changes. Follows C43 because both edit the same three skills. Breakpoint: one commit per skill: the status list from CLAUDE.md line 30 in each report step, the save-entered, commit and pull-request step allowed instead of forbidden; then tests/test_skills_delivery.py
- **#21 C31** (3 other): Band 3: QA proposals reach the file more often and the notes tell the truth after QA. On 10-01 the only goalie-diversifying proposal was rejected by an ordering artifact (B38) and RUN_NOTES kept v3 figures after v4 published (B39). Observed once each; S to M items. Depends on C26 (controller.py). Breakpoint: per-entry team counts in the packet and independent evaluation of same-entry proposals, committed; then the round-rule reconciliation; then B39's metric tables by version
- **#22 C32** (3 other): Band 3: no lineup moves. Small deterministic items (B10, B11, B29, B32, B65) that mislead config editors or leave an xfail in the suite; ordered smallest first. Breakpoint: one commit per item in this order: B29 (the xfail becomes a pass), B65 config keys and docstrings, B10, B11 apply-once, B32, the page-age line
- **#23 C33** (3 other): Band 3: a validator blind spot (review R10). The referee compares parsed values, so quote-equivalent byte changes pass; the writer was not observed making such changes and DraftKings' tolerance is unknown, so the washout path is hypothetical (judgment). Guards the C0b contract independently. Breakpoint: a raw-span tokenizer for entry lines with quoting and BOM cases, committed; then pinned-cell byte binding
- **#24 C34** (3 other): Band 3: determinism. On 4,000 draws the top-1% objective cannot separate minimal repairs, so the pick changes between runs and with the real clock (B19, measured once). Small. Breakpoint: one commit: more draws or the exp_payout proxy inside the band, with a determinism test across two clock values
- **#25 C35** (3 other): Band 3: measurement quality of the field. The fast sampler matches the MILP lineup in 51% of optimizer and stacker draws (B12, measured on the 09-29 pool) and a single field sample leaves field variance out of the SE (B15, second half). Depends on C19 (new behaviors change the search). Breakpoint: 2-swap moves or several price starts with the agreement re-measured, committed; then field resampling across scenario blocks in the SE
- **#26 C36** (3 other): Band 3: measurement. Team totals against market and goalie wins against implied cannot be graded today although the partner feed is captured hourly (B67). Value accrues one slate at a time. Breakpoint: implied totals and win probabilities from the capture nearest lock stored on the frozen record, committed; then the two calibration rows in settle

### Deferred

| Item | Trigger | Backlog | Findings |
|---|---|---|---|
| C12: Field-model fit by contest family | its gate: 30 distinct Classic slate groups with 15,000 labels (or 50 Showdown games with 5,000) plus the holdout; at 2026-10-03 the ledger holds 3 settled Classic slates, and C29 (settle without a run) and R08's group fix change the count | B15, B20 | none |
| C13: Segment simulator challenger | its gate, read against a clean calibration: C25 recomputes the constants without the evaluation dates and names the co-ceiling cause; then the preregistration in docs/experiments/ is written before code | B6 | none |
| B27 salary re-download near lock (READY) | a live slate on which Ben re-downloads DKSalaries.csv inside the last hour and runs refresh or late swap with --salary; the acceptance needs a real added-row file | B27 | none |
| Native post-lock export acceptance (two tests skip loudly) | Ben saves one real post-lock DraftKings entries export as tests/fixtures/real/<date>/<mode>/DKEntries.postlock.csv (tracker C2c note); C15 proceeds on synthetic fixtures until then | B42 | none |
| Scheduled refresh registration and a T-20 run (B23) | Ben registers tools/register_refresh_task.ps1 or the Claude scheduled task; the hourly cron gives one refresh near T-60 and none at T-20; flag 10 keeps phone push off | B23 | none |
| Dev dependency group and one lockfile (B34 remainder) | Ben answers flag 11; the default (both lockfiles, pytest at runtime) is safe and in force | B34 | none |
| 150-entry runtime and memory (B16) | Ben enters 20 or more entries on one slate (flag 3's default says 20 to 150; the four slates on record had 5 to 10) | B16 | none |
| Cash leg decorrelated from the tournament stack (B21) | Ben enters cash contests regularly (one Double Up on 09-29, none since) | B21 | none |
| Live score conditioning (B18) | a DraftKings live standings snapshot source and its format; LIVE_STATUS stays NO_SNAPSHOT | B18 | none |
| Covers odds fallback (B5) | the NHL partner feed goes stale again (it was fresh on 10-01 and 10-02); until then C32 corrects the stale docstrings and adds B5's status note | B5 | none |
| Prior payout curve uses the lobby prize pool (B94) | a settled slate where a contest priced on the prior (FIELD_SIZE_SOURCE=LOBBY, PAYOUT_SOURCE=PRIOR) paid out differently from the prior by more than the rake assumption, or the next chunk that edits build/objectives.prior_curve; C38 sizes the contest, the pool is the next fact on the same row | B94 | none |
| Late swap publish precheck under the publish lock (B87) | the next change to late swap's publish call, or a rehearsal that shows the window (the referee's runtime plus a publish-lock wait of up to 60 s); C14 closed the same window for the controller and the initial run | B87 | none |
| --as-of output separation for qa-apply and overrides-apply (B88) | Ben runs a qa-apply or overrides-apply rehearsal with --as-of and no --outputs-root, or the next change to the QA commands in cli.py | B88 | none |
| Objective for a started-slate build (B89) | a slate Ben opens after its first game and wants leverage-aware lineups for the open games (late registration); today a started-slate run is baseline-only | B89 | none |
| Settle's PRE_LOCK test on a started-slate run (B90) | the first started-slate run Ben enters and settles (its manifest carries started_slate); a strict xfail in tests/test_started_slate.py flips when fixed | B90 | none |
| Template payouts in cloud sessions (B91) | Ben answers flag 16 (B68, what a cloud run persists); a small committed store of DraftKings' tables and lobby facts would give cloud runs TEMPLATE instead of PRIOR | B91 | none |
| C16 exit-check wording and a run --as-of rehearsal flag (B92) | Ben saves the 10-02 DKSalaries_127 and a 10-02 DKEntries.csv under tests/fixtures/real (the same rehearsal should then print TEMPLATE for the 10-02 Daily Dollar); the flag waits for B88's rehearsal-output separation | B92 | none |
| B50 acceptance: TEMPLATE payouts against Ben's reported amounts | Ben types winnings_usd for entry 5276576946 (196228907, the 09-30 mini-MAX) into data/standings/inbox/2026-09-30/winnings.csv and settles again, and a slate holding the 10-02 Daily Dollar (196267162) is booked; settle then notes any mismatch between the TEMPLATE table and the reported amount | B50 | none |
| B63 acceptance: ownership error with the field inputs on, over five settled slates | five settled Classic slates built after C17 (the run manifest carries provisional.field_inputs); compare each contest's mae_all with the same slate re-run with field_inputs.enabled false (scripts/c17_replay.py shows the replay); the 09-30 replay worsened mae_all by 0.02 to 0.27 with 3 of 6 Daily Faceoff pages usable, so C40 (keep the last known lines) first improves the coverage; flip [BEN] flag 30 off if the error keeps rising | B63 | none |
| Score effects and OT shots (B7); per-strength SOG and block rates (B3) | C25's clean calibration names the SOG-to-goal correlation or the per-strength rates as a deficiency outside tolerance | B7, B3 | none |
| Measured replacements for the model placeholders (model.yaml role priors, roles.yaml p_play and weights, priors.yaml buckets, history_priors.yaml, sim.yaml q_en_shape, ot_slope, so_home_p, toi_floor, pp_opps_mean, so_goal_p, pull_frac) | C25 done and a month of 2026-27 games in the history store, so each constant is measured out of sample; flag 8's DTD haircut is one of them | none | none |
| Full history backfill after the season | after 2027-07-31 (docs/CONTRACTS.md raw-cache rule) | none | none |
| Evidence plumbing for the gates (holdout counts, prefit label file) (B82) | C29 done and 10 settled Classic slate groups: until then no gate could open anyway; C12 needs the holdout counts and the ownership_labels producer | B82 | none |
| Decay-window and shrinkage-strength challengers (decay_half_life_games 40, goalie start half-life 20) | C39 done and a month of 2026-27 games in the history store, so a walk-forward comparison is possible | none | none |
| Diagnose a pasted DraftKings upload rejection and repair only the affected contract (plan section 15) | the first DraftKings rejection of an engine file; B42 showed DraftKings rejects started-game cells | none | none |
| Measured usage table (docs/measured_usage.md), filled after each of the first ten real slates | each real slate session; /usage attribution is interactive only, so a human pastes it (optional, never blocks) | none | none |
| Shootout winner goal assumption (sim.yaml so_adds_goal) | C36 grades team totals against the partner feed; the assumption is then checked against the feed's treatment of the shootout winner | none | none |
| Playoff regime validation (flag 7: playoffs are validated separately) | April 2027, once playoff games exist; model.yaml regimes is regular only | none | none |
| Delivery observed on the next cloud run (B69: the file attached with its sha256 in the caption) | the next cloud-built slate: its run, late swap and refresh each end with the upload file attached; the rule already landed in PR 5 | B69 | none |
| Keep the history store current between runs (B95): a local scheduled `history --refresh` outside the lock windows | Ben registers a local scheduled task for it (a Windows scheduled task is his to create), or C39's first weeks show runs starting on a store a day or more behind | B95 | none |
| Cloud history backfill observed on the next cloud run (B71: the notes line names the bootstrap backfill, MODEL_STATUS is not PRIOR once it is done) | the next cloud-built slate: its RUN_NOTES History store line says the session's background backfill is running, done or failed, and after it is done the run prices players with history on HISTORY or MIXED | B71 | none |
| HttpCache body writes are not atomic (B96) | the next change to data/http.py, or a cached body that fails to parse after an interrupted run | B96 | none |
<!-- QUEUE:END -->
## Chunk cards

### C0a · Repo scaffold, contracts, exact scoring, tracker tooling

**Depends on:** nothing. **Read first:** this card; plan §5 "Definitions and scoring contract" table and §7 "Hard roster contracts" table; the two rule files after copying them.

**Goal.** A pinned Python package whose scoring and legality functions are exact against the DK rule documents, plus the tooling every later session uses to find its chunk and update the tracker.

**Create.**

- `pyproject.toml` (package `nhl_dfs` under `src/`, Python ≥3.11; dependencies: numpy, pandas, pyarrow, scipy ≥1.11, pyyaml, requests, pytest, pytest-timeout), `requirements.lock` (or `uv.lock` if `uv` is installed), `.gitignore` (`data/raw/`, `data/cache/`, `data/standings/`, `data/ledger/`, `tests/fixtures/real/`, `runs/`, `outputs/`, `.venv/`, `__pycache__/`), `git init` and first commit.
- `nhl.ps1` and `nhl.sh`: both run `python -m nhl_dfs.cli` from the repo root with the pinned environment (`.venv\Scripts\python.exe` on Windows).
- `src/nhl_dfs/cli.py`: `status` (prints tracker table and open [BEN] flags); `verify` and `run` as stubs that print "available after C0b/C2b" and exit 2.
- `src/nhl_dfs/contracts/scoring.py`, `geometry.py`, `ids.py`, `statuses.py`.
- `docs/CONTRACTS.md`: the scoring table with exact values, roster geometry, ID and person-key rules, the four per-row states, the evidence states, and the status vocabularies. Two pages; later sessions read this instead of the plan.
- `docs/rules/NHL_Classic.txt`, `docs/rules/NHL_Showdown_Captain_Mode.txt` (copied verbatim).
- `tools/next_chunk.py`, `pytest.ini` (markers `c0a` … `c13`; `timeout = 120`), `tests/conftest.py`, `tests/test_scoring.py`, `tests/test_geometry.py`, `tests/test_next_chunk.py`.

**Interfaces.**

```python
# contracts/scoring.py   (integer arithmetic; tenths of a DK point)
@dataclass(frozen=True)
class SkaterLine: goals:int=0; assists:int=0; sog:int=0; blocks:int=0; sh_points:int=0; shootout_goals:int=0
@dataclass(frozen=True)
class GoalieLine: decision:str="ND"   # "W" | "L" | "OTL" | "ND"
                  saves:int=0; goals_against:int=0; shutout:bool=False
                  goals:int=0; assists:int=0; sog:int=0; blocks:int=0     # offensive stats a goalie accrues
def score_skater_tenths(s: SkaterLine) -> int
def score_goalie_tenths(g: GoalieLine) -> int
def captain_twentieths(base_tenths: int) -> int          # base_tenths * 3
def lineup_twentieths(base_tenths: Sequence[int], captain_index: int | None) -> int
def tenths_to_points(t: int) -> float
```

Values: goal 85, assist 50, SOG 15, block 13, SH point bonus 20 per SH goal or assist, shootout goal 15 (not a goal), hat trick 30 at goals ≥3, 5+ SOG 30, 3+ blocks 30, 3+ points 30 where points = goals + assists; goalie win 60, save 7, GA −35, shutout 40, OTL 20, 35+ saves 30; goalies also earn skater stats. A shootout loss with zero regulation and OT goals against is shutout + OTL. `shutout` is a boolean the caller decides (sole goalie of record, full game); scoring does not infer it.

```python
# contracts/geometry.py
class Mode(Enum): CLASSIC = "classic"; SHOWDOWN = "showdown"
CLASSIC_SLOTS  = ("C","C","W","W","W","D","D","UTIL","G")
SHOWDOWN_SLOTS = ("CPT","FLEX","FLEX","FLEX","FLEX","FLEX")
SALARY_CAP = 50_000
@dataclass(frozen=True)
class PoolRow: role_id:str; person_key:str; name:str; team:str; position:str
               roster_positions:frozenset[str]; salary:int; game_info:str
               appg_raw:float|None; appg_flag:str          # appg_flag in {"VALUE","APPG_ZERO","MISSING"}
    @property def is_goalie(self) -> bool
def slot_accepts(slot: str, row: PoolRow, mode: Mode) -> bool
def check_lineup(rows: Sequence[PoolRow], mode: Mode) -> LegalityResult   # rows in slot order
# LegalityResult: ok, reasons: list[str], salary_total, skater_teams: set[str], teams: set[str]
def lineup_key(rows: Sequence[PoolRow], mode: Mode) -> str
# Classic key: sorted person keys. Showdown key: CPT person + "|" + sorted FLEX persons.
```

Checks: exact slot count; `slot_accepts` requires the slot label in `roster_positions` (DK gives UTIL only to skaters; G accepts only G); distinct `person_key`; salary ≤ cap using each row's own salary; Classic non-goalie rows span ≥3 teams; Showdown CPT row has roster positions `{"CPT"}` and FLEX rows `{"FLEX"}`; Showdown rows span ≥2 distinct teams (for a two-team pool this is exactly both teams). No other rule.

```python
# contracts/ids.py
def normalize_name(s: str) -> str                 # lowercase, strip accents and punctuation, collapse spaces
def position_group(pos: str) -> str               # C/LW/RW/W -> "F"; D -> "D"; G -> "G"
def person_key(name: str, team: str, position: str) -> str    # f"{normalize_name(name)}|{team}|{position_group(position)}"
# contracts/statuses.py (enums)
# FileStatus; NewsState(FULL, PARTIAL, NONE); ModelStatus(PRIOR, PARTIAL, FULL); SearchStatus(FEASIBLE, TIME_LIMIT_WITH_INCUMBENT, INFEASIBLE, ERROR)
# DeliveryStatus(CHECKED, DEGRADED_REVIEW, FAILED); ObsStatus(CURRENT, STALE, CONFLICTED, MISSING); GoalieState(EXPECTED, CONFIRMED, OUT, CONFLICTED)
# Participation(PLAYING, QUESTIONABLE, OUT, UNKNOWN); Eligibility(ROSTERABLE, DISABLED); CellLock(OPEN, EDIT_STOP, LOCKED)
# PayoutSource(EXACT, PRIOR); OutcomeCalibration(UNVALIDATED, SHADOW, VALIDATED); FieldCalibration(PRIOR, FITTED)
# FeasibleStatus(FOUND, TIMEOUT, INFEASIBLE_PROVEN)
```

```text
tools/next_chunk.py
  (no args)               eligible chunk(s): deps DONE, status TODO or IN_PROGRESS; runs DONE predecessors' checks first; honors requires_files and gated_on from chunks.yaml
  --check <id>            run that chunk's checks; exit 1 on failure
  --start <id>            set IN_PROGRESS with today's date
  --done <id> --commit H  set DONE, finish date, commit; refuses unless --check passes
  --block <id> --reason   set BLOCKED with reason
  --status                print the table and open [BEN] flags
Parses BUILD_STATUS.md by its literal column order and round-trips the file without touching other lines.
```

**Tests.** Every threshold boundary (4 vs 5 SOG, 2 vs 3 blocks, 2 vs 3 points, 2 vs 3 goals); bonuses stack (3 goals + 0 assists = 255 + 30 + 30); SH points add 20 each; shootout goal is 15 and not a goal; goalie 25 saves 3 GA loss = 175 − 105 = 70; goalie shootout loss with 0 GA = 20 + 40 + saves; goalie assist earns 50; captain 13 → 39 twentieths, never floating point; Classic legality: two-team eight-skater roster fails, goalie does not count toward three teams, G in UTIL fails, 50,001 fails, duplicate person fails; Showdown: FLEX row in CPT slot fails, one-team roster fails, two opposing goalies passes, a four-team pool with two teams represented passes; `lineup_key` ignores FLEX order and distinguishes captains; `next_chunk.py` on a fixture tracker prints the right chunk and refuses when a predecessor check fails.

**Exit checks.** `pytest -m c0a -q` green; `python tools/next_chunk.py` prints `C0b` (BLOCKED until real files exist, and says so); `.\nhl.ps1 status` prints the tracker; `git log` shows the initial commit.

**Do not.** Parse DK files (C0b). Add any dependency beyond the list. Write a solver.

---

### C0b · Intake, byte-splicing export, independent referee, real-file fixtures

**Depends on:** C0a. **Read first:** this card; `docs/CONTRACTS.md`; plan §10 "Intake and exact output" and "Pre-export checks". **Ben supplies:** the real fixture files. BLOCKED without them.

**Goal.** Read real DK files byte-faithfully, write the entries template back by splicing cells into the original bytes, and re-check the written bytes with a referee that shares no parser or rule code with the builder.

**Create.** `src/nhl_dfs/intake/salary.py`, `intake/entries.py`, `src/nhl_dfs/export/writer.py`, `src/nhl_dfs/referee/reader.py`, `referee/rules.py`, `referee/check_file.py`, `tests/fixtures/real/<date>/{classic,showdown}/` (gitignored), `tests/fixtures/mini/` (≤40-row synthetic pools and entries files for both modes, including an `Elias Pettersson` C and D on the same team and a `Sebastian Aho` C and D on different teams), `tests/test_intake.py`, `tests/test_export.py`, `tests/test_referee.py`, `tests/test_rules_agreement.py`; `cli verify` implemented.

**Interfaces.**

```python
# intake/salary.py
def read_salary(path) -> SalaryPool
# SalaryPool: mode (any Roster Position in {CPT, FLEX} -> SHOWDOWN, else CLASSIC), rows: list[PoolRow], by_role_id,
#   persons: dict[person_key, PersonRows(cpt, flex, classic)], teams, games: dict[str, GameInfo(home, away, start_et_text, start_utc)],
#   conflicts: list[Conflict], sha256, raw: bytes
# Required columns exactly: Position, Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame.
# APPG: blank -> (None, "MISSING"); 0 -> (0.0, "APPG_ZERO"); else (value, "VALUE"). Showdown pairing by (Name, TeamAbbrev, Position);
# CPT salary != 1.5x FLEX is a warning; CPT APPG != FLEX APPG is recorded in the conflicts report (never rewritten).
# Same Name+Team+Position with more rows than the mode allows -> Conflict; those rows are excluded from selection and reported.

# intake/entries.py
def read_entries(path) -> EntriesFile
# EntriesFile: raw: bytes, newline: str, bom: bool, header: list[str], roster_cols: list[int], entries: list[EntryRow(entry_id, contest_name, contest_id, fee, cells, line_no, line_bytes)],
#   tail_lines: list[bytes] (verbatim; never parsed as entries), sha256
# An entry row has a numeric Entry ID. The instructions/player-list block to the right and below is preserved as bytes.

# export/writer.py
def write_entries(entries: EntriesFile, assignment: dict[str, tuple[str, ...]], pool: SalaryPool, out_path) -> bytes
# Splices f"{row.name} ({row.role_id})" into each roster cell of the entry's original line bytes; every other byte unchanged; rows never re-serialized;
# every entry must be assigned (raises otherwise).

# referee/reader.py      (stdlib csv only; no imports from intake or contracts.geometry)
def read_salary_min(path) -> dict[str, RefRow]     # role_id -> (person_key, roster_positions, salary, team, is_goalie), plus sha256
def read_entries_min(path) -> RefEntries          # entry ids in order, cells, sha256
# referee/rules.py        (independent implementation of the roster rules; must not import contracts.geometry)
def legal(rows_in_slot_order, mode) -> tuple[bool, list[str]]
# referee/check_file.py
def check_file(out_path, salary_path, entries_path, *, parent_path=None, locked: dict[tuple[str,int], str]|None=None) -> RefereeReport
# Always binds out bytes to salary and entries hashes; entry set and order must equal the entries file (or the parent when given);
# each cell's ID exists with the right role; rules.legal per entry; locked cells unchanged; ok only if every entry passes.
```

**Tests.** Real Classic and Showdown files round-trip (assign each entry its existing lineup, or a legal one from the pool if blank), write, and compare bytes outside roster cells; BOM, newline style, and the tail block preserved; CPT ID in CPT slot passes and a FLEX ID in the CPT slot fails; wrong-mode file rejected; Pettersson/Aho mini fixtures produce Conflicts and are never merged; referee catches a hand-edited cap overrun, a duplicated person, and a swapped entry order; property test: `referee.rules.legal` agrees with `contracts.geometry.check_lineup` on 1,000 random slot assignments over the mini pools; `verify` exits 0 on a valid file and 1 with reasons otherwise; real-file tests skip loudly when the files are absent.

**Exit checks.** `pytest -m c0b -q` green with the real files present; `.\nhl.ps1 verify --salary <real classic> --entries <real classic> --out <real classic>` prints `FILE_VALID=TRUE` for the downloaded file (or FALSE with "empty roster" reasons if the template has blank lineups, and TRUE after a test assignment).

**Do not.** Fetch anything. Build a solver. Read the full fixture files into the conversation.

---

### C1 · Adapters, cache, observations, prospective capture

**Depends on:** C0b. **Read first:** this card; plan §3 rows for DK public, NHL (schedule, box score, per-game reports, partner odds), Covers; `docs/CONTRACTS.md`.

**Goal.** Every keyless source behind one cached, schema-checked HTTP layer with a bounded budget and an offline mode, plus a scheduler script that snapshots the sources daily from now on.

**Create.** `src/nhl_dfs/data/http.py`, `data/observations.py`, `data/sources/dk_public.py`, `data/sources/nhl.py`, `data/sources/covers.py`, `config/sources.yaml`, `config/capture.yaml`, `config/teams.yaml` (32 rows: DK abbrev, NHL abbrev, Daily Faceoff slug, full name), `tools/capture.py`, `tools/register_capture_task.ps1`, `tests/fixtures/http/` (one recorded response per source, trimmed to ≤40 KB), `tests/test_http.py`, `tests/test_dk_public.py`, `tests/test_nhl.py`, `tests/test_covers.py`; `cli probe`.

**Interfaces.**

```python
# data/http.py
class HttpCache:
    def get_json(self, url, *, source, ttl_s, schema: Callable[[Any], None]) -> Fetched
    def get_text(self, url, *, source, ttl_s, schema) -> Fetched
# Fetched: data, fetched_at_utc, from_cache, raw_path (data/raw/<source>/<YYYY-MM-DD>/<sha>.json|html), raw_hash
# Browser-like User-Agent; timeout 6 s; one retry after 1 s (worst case about 14 s per call, config); NHL_DFS_OFFLINE=1 -> cache only else SourceUnavailable;
# HTTP 403/404/5xx -> SourceUnavailable (never partial data); schema failure -> SourceSchemaError; every fetch appends an Observation.

# data/sources/dk_public.py
def lobby() -> list[ContestSummary]                      # id, name, fee, field_size, max_per_user, prize_pool, draft_group_id, game_type, start_utc
def contest_detail(contest_id: int) -> ContestDetail    # payout_table: list[PayoutTier(min_pos, max_pos, cash: Decimal)], maximum_entries, max_per_user, entry_fee, entries, draft_group_id, start_utc, raw
def draftables(draft_group_id: int) -> Draftables       # rows: Draftable(draftable_id, player_id, name, position, roster_slot_id, salary, status_raw, participation: Participation,
                                                        #   eligibility: Eligibility, is_swappable, news_status, team, competition_id, start_utc); competitions
STATUS_MAP = {"OUT": OUT, "IR": OUT, "O": OUT, "Q": QUESTIONABLE, "GTD": QUESTIONABLE, "D": QUESTIONABLE, "DTD": QUESTIONABLE, "None": PLAYING, "": PLAYING}   # else UNKNOWN, raw preserved. DTD (day-to-day) added by Ben 2026-09-28.
def reconcile(pool: SalaryPool, d: Draftables) -> Reconciliation   # by draftable_id == role_id; salary/team mismatches -> CONFLICTED; participation/eligibility/start by role_id

# data/sources/nhl.py
def schedule(date) -> list[Game]
def roster(team_abbrev) -> list[NhlPlayer]
def boxscore(game_id) -> BoxScore
def game_log(nhl_id, season: int, game_type: int = 2) -> list[GameLogRow]
def skater_report(report: Literal["summary","timeonice","realtime"], date_from, date_to) -> list[dict]   # per-game rows, paginated (start/limit), cayenneExp on gameDate
def partner_odds() -> OddsSnapshot                       # as_of_utc, book, games: GameOdds(game_id, home_ml, away_ml, home_ml_3way, away_ml_3way, draw_ml, total_line, over_price, under_price, home_puck, away_puck)
# data/sources/covers.py
def odds() -> OddsSnapshot                               # fail closed on any unparsed row
# data/observations.py: Observation(source, source_player_id, game_id, observed_at, published_at, fetched_at, valid_from, raw_hash, definition_version, status) ; append()
```

```text
tools/capture.py --once     lobby(); contest_detail for up to N contests per draft group (config); draftables per group; schedule(today); partner_odds(); covers raw HTML;
                            Daily Faceoff starting-goalies page and each slate team's line page as raw HTML INCLUDING <script> tags (no parsing here)
                            -> data/raw/capture/<YYYY-MM-DD>/<HHMM>/... plus index.json with fetched_at and hashes.
tools/register_capture_task.ps1
                            $RepoRoot = Split-Path -Parent $PSScriptRoot; action = "$RepoRoot\.venv\Scripts\python.exe" "$RepoRoot\tools\capture.py" --once, WorkingDirectory $RepoRoot;
                            one task per time in config/capture.yaml (defaults, America/Chicago: 08:30 11:30 14:30 16:30 17:30 18:00 18:30 18:50 19:20 19:50 20:50 21:20); idempotent; prints how to unregister.
```

**Tests.** Each parser on its recorded fixture; schema failure raises and returns nothing partial; a 403 fixture becomes SourceUnavailable; offline mode returns cache and raises when absent; Showdown draftables pairing (slots 612 and 613, salary 1.5x, distinct IDs) reconciles to the Showdown fixture by `draftable_id`; STATUS_MAP maps `OUT`/`IR` to OUT, `Q` to QUESTIONABLE, and an unrecognized value to UNKNOWN with the raw value preserved; `isSwappable` never changes participation; per-game reports parse `evTimeOnIce`, `ppTimeOnIce`, `shTimeOnIce`, `blockedShots`; partner odds parse 2-way and 3-way; Covers parser fails closed on a mutated fixture; capture writes an index with hashes and the goalie page HTML contains its script tags.

**Exit checks.** `pytest -m c1 -q` green; `.\nhl.ps1 probe` hits each live source once and prints one status line per source (network failures allowed and recorded); `python tools/capture.py --once` writes a snapshot directory; the scheduler script runs without error (Ben registers it after the session).

**Do not.** Parse Daily Faceoff HTML (C7). Backfill history (C4). Store anything outside `data/raw/`.

---

### C2a · Priors, feasibility fallback with result states, HiGHS candidate builder

**Depends on:** C0b. **Read first:** this card; `docs/CONTRACTS.md`; plan §7 solver paragraph; §10 ladder steps 1, 4, and 5.

**Goal.** Generate many legal lineups quickly from any objective, with a solver-free fallback that distinguishes a timeout from proven infeasibility, and a sampling mode that allows repeats for field construction.

**Create.** `src/nhl_dfs/models/priors.py`, `config/priors.yaml`, `src/nhl_dfs/build/feasible.py`, `build/milp.py`, `build/candidates.py`, `tests/test_priors.py`, `tests/test_feasible.py`, `tests/test_milp.py`, `tests/test_candidates.py`.

**Interfaces.**

```python
# models/priors.py
def prior_table(pool: SalaryPool, cfg) -> dict[str, Prior]     # Prior(mean_tenths, sd_tenths, source: "APPG_SHRUNK" | "BUCKET", appg_flag)
# mean = w*APPG + (1-w)*bucket_mean; w from config (default 0.6, labeled a challenger setting); APPG_ZERO -> w = 0 and flag kept; MISSING -> bucket.
# config/priors.yaml: bucket table by (position group, salary band) with a header comment "engineering placeholders; replace from history in C5".

# build/feasible.py     (no scipy import)
@dataclass class FeasibleResult: status: FeasibleStatus; lineup: list[str] | None; nodes: int; elapsed_s: float
def find_one(pool, mode, *, exclude=frozenset(), locked: dict[int, str] | None = None, budget_s: float = 2.0) -> FeasibleResult
# INFEASIBLE_PROVEN only when the search space is exhausted; TIMEOUT otherwise. Classic: goalie first, then slots cheapest-first with backtracking and a team-count repair;
# Showdown: enumerate CPT rows by salary, then FLEX completion. Respects arbitrary roster_positions, duplicate persons, and locked slots.

# build/milp.py
@dataclass class GroupConstraint: role_ids: frozenset[str]; min_count: int = 0; max_count: int | None = None
def solve_lineup(pool, mode, objective: dict[str, float], *, exclude=frozenset(), locked=None, groups=(), max_overlap_with=(), time_limit_s=5.0) -> SolveResult
# SolveResult(status: SearchStatus, lineup, objective_value, elapsed_s, solver_status_code). Classic x[row, slot]; Showdown x[role_row]; team indicators for the 3-team and
# 2-team rules over ALL slots including locked ones; scipy status 2 -> INFEASIBLE; 1 -> TIME_LIMIT_WITH_INCUMBENT when feasible; returned lineups re-validated with check_lineup.
def solver_available() -> bool       # import probe; callers route to feasible.find_one when False

# build/candidates.py
def generate(pool, mode, objective, n, *, seed, perturb_sd, groups_menu=(), time_limit_total_s=20.0, distinct=True, min_pairwise_diff=2) -> list[Candidate]
# Candidate(role_ids, key, objective_value, family). distinct=True excludes repeats and enforces min_pairwise_diff (portfolio use);
# distinct=False draws with replacement and returns repeats (field use). Perturbation is Gumbel noise on the objective.
```

**Tests.** MILP solutions pass `check_lineup`; an all-one-team pool returns INFEASIBLE; a tiny time limit returns TIME_LIMIT_WITH_INCUMBENT or FEASIBLE, never an invalid lineup; property test over 200 random pools: whenever MILP finds a lineup, `find_one` returns FOUND; a pool with two rich teams forces the third team; a Showdown pool with locked rows all from one team forces the other team into a free slot; `find_one` with `budget_s=0.001` on a large pool returns TIMEOUT, never INFEASIBLE_PROVEN; a provably infeasible tiny pool returns INFEASIBLE_PROVEN; `generate(distinct=True)` returns distinct keys with pairwise difference ≥2; `generate(distinct=False)` returns repeats when the objective is sharp; `solver_available()` is False when the import is monkeypatched to fail.

**Exit checks.** `pytest -m c2a -q` green; benchmark prints ≥150 distinct Classic and ≥150 distinct Showdown candidates from the real fixtures in ≤10 s each (record in the tracker).

**Do not.** Simulate. Assign entries. Add exposure logic.

---

### C2b · Local-first baseline run, atomic publish under a slate lock, manifest, CLI

**Depends on:** C2a, C1. **Read first:** this card; plan §10 ladder and status list; §12 "Short run-note fields".

**Goal.** `nhl.ps1 run --baseline` turns the two DK files into a checked, published `DKEntries.csv` in seconds from local inputs only, then runs a bounded network pass that can only add a newer version.

**Create.** `src/nhl_dfs/build/assign.py`, `build/run.py`, `build/state.py`, `build/manifest.py`, `build/notes.py`, `config/runtime.yaml` (`network_pass_budget_s: 25`, clock buffer), `config/exposure.yaml`, `tests/test_assign.py`, `tests/test_state.py`, `tests/test_run_baseline.py`, `tests/test_fallbacks.py`; `cli run --baseline [--offline]`, `cli verify --run <id>`, `cli status` extended with last run.

**Interfaces.**

```python
# build/assign.py
def assign(candidates, entries, pool, mode, caps: Caps, *, seed, later_start_utc: dict[str, datetime] | None = None) -> Assignment
# Assignment(by_entry, exposures, person_exposures, captain_exposures, overlap_max, relaxations). Distinct candidates by objective; person and captain caps with the feasibility floor;
# Classic overlap ≤7; UTIL receives the later-starting eligible skater when the objective is within the tie band; repetition of the best legal lineup is the last resort, recorded.

# build/state.py
def new_run(root="runs") -> RunDir                # runs/<YYYYMMDD-HHMMSS>-<mode>/ with inputs/, versions/, sim/, qa/, news/
def publish(run: RunDir, export_bytes: bytes, report: RefereeReport, slate_id: str) -> Path
# versions/v<N>/DKEntries.csv via temp + fsync + rename; refuses when report.ok is False; run/current -> v<N>;
# public outputs/<slate_id>/DKEntries.csv replaced by writing a temp file IN THAT DIRECTORY and os.replace(), under outputs/<slate_id>/.lock (slate lock) plus the run lock.

# build/run.py
def run_slate(salary_path, entries_path, *, offline: bool, baseline_only: bool = True, out_root="runs") -> RunResult
# Phase A (local only, no network): intake -> priors -> candidates (milp if solver_available else feasible) -> assign -> write -> referee -> publish v1 -> manifest -> RUN_NOTES.md
# Phase B (skipped when offline; bounded by network_pass_budget_s): draftables reconcile -> exclude participation OUT and eligibility DISABLED -> re-solve affected entries -> referee -> publish v2 if changed
# Empty candidate bank -> feasible.find_one per entry; INFEASIBLE_PROVEN -> report SEARCH_STATUS=INFEASIBLE with scope and stop; TIMEOUT -> retry once with larger budget then report.
# manifest.json: run_id, created_utc, mode, slate_id, salary_sha256, entries_sha256, export_sha256, statuses (FILE_VALID, NEWS_STATE, MODEL_STATUS, SEARCH_STATUS, DELIVERY_STATUS,
#   PAYOUT_SOURCE, OUTCOME_CALIBRATION, FIELD_CALIBRATION), entry_count, exposures_top20, relaxations, phase_timings, versions
```

**Tests.** Offline end-to-end on both real fixtures yields FILE_VALID=TRUE with no network call (socket monkeypatched to fail loudly); Phase A completes and publishes before any Phase B call is attempted (order asserted with a spy); a crash between temp write and `os.replace` leaves the public file unchanged; two concurrent runs on one slate serialize on the slate lock; a failing referee report blocks publish; manifest hashes match bytes; solver import failure routes to `find_one` and still publishes; an empty candidate bank routes to `find_one`; a Phase B source outage leaves v1 as current with NEWS_STATE=NONE; 20 entries ≤15 s and 150 entries ≤30 s for Phase A on the fixture.

**Exit checks.** `pytest -m c2b -q` green; `.\nhl.ps1 run --salary <real> --entries <real> --baseline --offline` writes `DKEntries.csv`, `manifest.json`, `RUN_NOTES.md`; `.\nhl.ps1 verify --run <id>` passes.

**Do not.** Use projections beyond priors. Call the LLM. Add ownership.

---

### C2c · Four-state lock model, late swap, refresh (baseline objective)

**Depends on:** C2b. **Read first:** this card; plan §11 "Fast mode" and the four-state paragraph in §10/§11.

**Goal.** Re-run in seconds from a current DK entries export, pinning locked cells, adding no started player, refusing edits inside the edit-stop buffer without claiming a game started, and re-solving only what is open.

**Create.** `src/nhl_dfs/build/locks.py`, `build/late_swap.py`, `build/refresh.py`, `tests/fixtures/late_swap/` (synthetic partially locked entries files, Classic and Showdown, including a two-game Showdown pool), `tests/test_locks.py`, `tests/test_late_swap.py`, `tests/test_refresh.py`; `cli late-swap --run <id> --entries <current> [--fast] [--offline]`, `cli refresh --run <id> [--offline]`.

**Interfaces.**

```python
# build/locks.py
@dataclass class CellState: lock: CellLock; reason: str        # LOCKED: game started or isSwappable false; EDIT_STOP: inside buffer; OPEN otherwise
def compute(entries_current: EntriesFile, pool, draftables: Draftables | None, now_utc, buffer_s: int) -> LockState
# LockState: cells: dict[(entry_id, slot_idx), CellState], started_role_ids, started_games, edit_stop_games. Start time = draftables competition start, else salary Game Info.
# "Started" is strictly now_utc >= start. Participation and eligibility are NOT lock inputs; they are exclusion inputs handled by the re-solve.

# build/late_swap.py
def run(run_id, entries_current_path, *, offline: bool, fast: bool = True) -> RunResult
# Parent = the current export, never the last generated file; diff vs last delivered version recorded; LOCKED and EDIT_STOP cells pinned; started players never added;
# re-solve is the full-lineup MILP with pinned variables fixed (team rules span pinned and free slots); residual exposure optimized; predecessor preserved if a lock boundary is crossed mid-compute.
# build/refresh.py
def run(run_id, *, offline: bool) -> RunResult      # re-fetch draftables (participation/eligibility/start) and odds; uses the SAME LockState rules as late swap; re-solves only affected entries
```

**Tests.** All-locked file returns unchanged bytes; partially locked changes only OPEN cells and reports pinned exposures over cap; EDIT_STOP cells are pinned and the report says "edit stop", not "started"; a started player is never added; Showdown with all pinned rows from one team forces the other team in a free slot; a two-game Showdown fixture with game 1 locked re-solves game 2 legally; an expensive goalie replacement forces a two-player repair within cap; lock boundary crossing mid-compute keeps the predecessor; refresh applies the same pins as late swap; DST and UTC conversions on fixture dates.

**Exit checks.** `pytest -m c2c -q` green; `.\nhl.ps1 late-swap --run <id> --entries <fixture current> --fast --offline` completes in ≤30 s with locked cells unchanged. **Milestone 1: usable slate workflow.**

**Do not.** Call the LLM. Assume the last generated file was uploaded. Read roles or news (C7).

---

### C3 · Ownership prior, field sampler, duplicate proxy, evidence gates, provisional leverage selection

**Depends on:** C2c. **Read first:** this card; plan §6 in full; §7 "Deterministic tie-break" and the evidence-state paragraph; §12 evidence-floor table.

**Goal.** On priors alone: a hand-weighted ownership prior, an opponent field sampled with replacement, a duplicate proxy, the single evidence-gate definition, and a provisional leverage-aware selection that uses real payout metadata and labels every figure as provisional.

**Create.** `src/nhl_dfs/models/projection.py` (the `Projection` interface priors and later params both satisfy), `models/ownership.py`, `models/field.py`, `models/prefit.py`, `learn/gates.py`, `config/evidence_floors.yaml` (the plan §12 table, per mode), `config/ownership.yaml`, `config/contest_families.yaml` (name-pattern inference and priors when the contest endpoint fails), `build/provisional.py`, `tests/test_ownership.py`, `tests/test_field.py`, `tests/test_gates.py`, `tests/test_prefit.py`, `tests/test_provisional.py`; `cli field --run <id>`; `cli run` gains the provisional pass after Phase B.

**Interfaces.**

```python
# models/projection.py
class Projection(Protocol): def mean_tenths(self, role_id) -> int; def sd_tenths(self, role_id) -> int; def source(self) -> ModelStatus
# models/ownership.py
def utilities(pool, proj: Projection, contests: list[ContestDetail | FamilyPrior], odds: OddsSnapshot | None, roles=None, cfg=...) -> dict[str, dict[str, float]]   # by contest family
# Features: salary rank within position, value (mean/salary), APPG (raw), team implied total when odds exist, PP1 and line flags when roles exist, goalie start*win, news recency.
# models/field.py
@dataclass class Behavior: name; weight; noise_sd; stack_rule; captain_rule; salary_left_pref
def sample(pool, mode, util, behaviors, n, seed, contest_family) -> Field       # draws WITH replacement via candidates.generate(distinct=False); Field(lineups (n, slots), multiplicity, behavior_id)
def marginals(field: Field, pool, field_size: int) -> Marginals                  # own (Classic ≈900%, Showdown CPT 100% + FLEX 500%), cpt_share, dup_counts weighted to field_size, stack_freq, salary_left_hist
def dup_proxy(role_ids, own, mode) -> float                                     # sum of log own + salary-left bucket + captain own (Showdown)
# learn/gates.py
def tier(counts: EvidenceCounts, mode: Mode) -> GateReport     # reads config/evidence_floors.yaml; per-mode counts; the ONLY gate any fit or promotion may consult
def allows(action: Literal["prefit","field_fit","mixture_fit","rate_correction","strategy_change"], counts, mode) -> tuple[bool, str]
# models/prefit.py
def fit(standings_dir, pools) -> PrefitResult     # runs only if gates.allows("prefit") for that mode using the historical counts; chronological holdout (Classic ≥10 groups, Showdown ≥15 games); returns offsets or "gated"
# build/provisional.py
def select(candidates, proj, marginals_by_contest, contests, entries, caps, cfg, *, seed) -> Assignment
# Rank by projected mean; within the tie band (config, default 3% of mean, never below prior sd/√n) prefer lower ownership then lower dup proxy, bounded by one band;
# contest families: cash -> highest mean, no leverage; WTA and small-field -> lowest dup within band; large GPP -> band + dup. Labels: MODEL_STATUS=PRIOR, PAYOUT_SOURCE per contest,
# OUTCOME_CALIBRATION=UNVALIDATED, FIELD_CALIBRATION=PRIOR. No ceiling or probability figures are reported in this mode.
```

**Tests.** Mass sums within tolerance for both modes; every sampled lineup passes `check_lineup`; sampled field contains repeats and `dup_counts` scale to field size; marginals reproducible under seed; dup proxy monotone in ownership; `gates.allows("prefit")` is False on a synthetic history below the floor and True above it, per mode; prefit on a synthetic standings set above the floor recovers planted offsets and refuses below it; provisional selection never prefers a candidate more than one band worse; cash entries get the highest-mean candidates; the run manifest carries the three evidence states.

**Exit checks.** `pytest -m c3 -q` green; `.\nhl.ps1 run --salary <real> --entries <real> --offline` publishes v1 (baseline) and a provisional version with ownership and duplicate columns in `RUN_NOTES.md`, all figures labeled provisional. **Milestone 2: provisional leverage portfolio.**

**Do not.** Simulate. Fit to live standings (C12). Report any probability or ceiling.

---

### C4 · History cache (MoneyPuck or NHL per-game reports), identity crosswalk, as-of features

**Depends on:** C1. **Read first:** this card; plan §3 MoneyPuck and NHL per-game report rows; §4 regime paragraph.

**Goal.** Two seasons of as-of-safe player history from listed downloads (Tier A) or NHL per-game reports plus box scores (Tier B), stored as Parquet, with an explicit DK-to-NHL identity map and honest missingness where Tier B has no equivalent.

**Create.** `src/nhl_dfs/data/history/moneypuck.py`, `data/history/nhl_reports.py`, `data/history/store.py` (Parquet partitions by season and kind), `data/identity/crosswalk.py`, `data/features/asof.py`, `data/identity/accepted.csv` (empty with header), `docs/features.md` (every feature column: Tier A source, Tier B source or "league prior + `<col>_missing` indicator"), `config/sources.yaml` extended (`moneypuck.enabled: true`, listed URLs, attribution string), `tests/fixtures/history/` (≤50-row samples per kind, two box scores, per-game report samples), `tests/test_moneypuck.py`, `tests/test_nhl_reports.py`, `tests/test_store.py`, `tests/test_crosswalk.py`, `tests/test_asof.py`; `cli history --backfill <seasons>`, `cli identity --seed --salary <path>`, `cli identity --accept <proposal_id>`.

**Interfaces.**

```python
# data/history/moneypuck.py    (listed downloads only; attribution printed by cli history)
def download(kind: Literal["skaters","goalies","lines","teams","shots"], season: int, level: Literal["season","game"]) -> Path
def load(kind, season, level) -> pd.DataFrame          # normalized columns per docs/features.md; situation in {all, 5on5, 5on4, 4on5, other}
# data/history/nhl_reports.py   (Tier B)
def backfill(seasons, *, since=None) -> BackfillStats  # skater_report summary/timeonice/realtime by date window + boxscores for goalie decisions and blocks cross-check; incremental
# Provides: goals, assists (total), SOG, blocks, PP points, TOI by strength, shifts per game. Does NOT provide A1/A2 split, shot quality, or shared ice: those columns are
# filled with league priors by position and role and carry <col>_missing = 1.
# data/history/store.py
def write(kind, season, df) / def read(kind, seasons) -> pd.DataFrame     # Parquet; load of two seasons under one second
# data/identity/crosswalk.py
def seed(pool: SalaryPool) -> CrosswalkResult          # exact normalized name + team + position group -> accepted; else proposal; accepted.csv rows carry sha256(name|team|pos)
def accepted() -> dict[str, int]; def accept(proposal_id, nhl_id) -> None
# data/features/asof.py
def frame(as_of: date, nhl_ids, *, seasons=2) -> FeatureFrame   # games dated < as_of only; regime column; missingness indicators present in both tiers
```

**Tests.** As-of frame excludes same-day and future games (planted future row); MoneyPuck parser on fixtures per kind; Tier B builds the same columns with `<col>_missing` set for A1/A2 share, shot quality, and shared ice, and equal values for TOI by strength, SOG, blocks on an overlapping fixture game; Parquet round trip; crosswalk on the mini pool gives two accepted IDs for the Petterssons by position group and a proposal for a fabricated third; an unmatched call-up stays unmatched and flagged.

**Exit checks.** `pytest -m c4 -q` green; `.\nhl.ps1 history --backfill 2` completes online outside any slate clock (record elapsed and row counts) or the offline fixture path passes; `.\nhl.ps1 identity --seed --salary <real classic>` reports accepted/proposal/unmatched counts.

**Do not.** Model anything. Scrape MoneyPuck pages. Hand-edit `accepted.csv`.

---

### C5 · Opportunity, event-rate, and goalie models, per person

**Depends on:** C4. **Read first:** this card; plan §5 "Opportunity and rate estimation"; §4 rows for TOI, iSF/iCF, A1, blocks, goalies; `docs/features.md`.

**Goal.** One parameter row per person: dressing and start probabilities, TOI by strength, per-60 event rates with shrinkage and dispersion, goalie parameters, and a role-row map, satisfying the `Projection` interface so C3's selection upgrades automatically.

**Create.** `src/nhl_dfs/models/opportunity.py`, `models/rates.py`, `models/goalies.py`, `models/params.py`, `config/model.yaml` (prior weights 300 EV min and 100 ST min, goalie 1,500 shots, decay half-life, position priors, penalty-rate coupling), `tests/test_opportunity.py`, `tests/test_rates.py`, `tests/test_goalies.py`, `tests/test_params.py`; `cli params --salary <path> [--as-of <date>]`.

**Interfaces.**

```python
# models/opportunity.py
def estimate(features, roles: RoleState | None, cfg) -> dict[int, Opportunity]
# Opportunity(p_dress, toi_ev_s, toi_pp_s, toi_sh_s, sd_toi_s, pp_share, unit_ev, unit_pp). PP share denominator is team PP clock time. EV minutes for depth lines scale down with the
# team's expected PP and PK time from penalty rates (manpower budget reconciled). Without RoleState, units come from historical co-membership (Tier A) or are None (Tier B).
# When a RoleState exists and a person has no history, the role's vacated-slot expectation overrides the bucket prior (call-ups).
# models/rates.py
def estimate(features, cfg) -> dict[int, Rates]     # g60/a1/a2/sog60/blk60 by strength, dispersions, shot_quality (prior + indicator when missing), n_ev_min, n_st_min
# models/goalies.py
def estimate(features, schedule, cfg) -> dict[int, GoalieParams]     # p_start (team goalies sum to 1), save_skill (1,500-shot prior), workload_adj from OPPONENT shot-for rate, pull_prob_per_ga
# models/params.py
@dataclass class PersonParams: person_key; nhl_id | None; opportunity; rates | goalie; source: ModelStatus
def build(pool, crosswalk, as_of, cfg) -> ParamTable       # one PersonParams per person; role_map: dict[role_id, person_key]; implements Projection (mean/sd per role via the person)
```

**Tests.** Zero history returns the prior exactly; long history converges to the sample mean; decay weights; PP share denominator; back-to-back lowers p_start and the pair sums to 1; goalie workload uses opponent shot-for rate; leakage guard; every person gets a row and every role row maps to exactly one person (CPT and FLEX rows to the same person); a call-up with no history and a RoleState PP1 assignment gets the role expectation; Tier B params carry the missingness indicators.

**Exit checks.** `pytest -m c5 -q` green; `.\nhl.ps1 params --salary <real classic> --as-of <fixture date>` writes `params.parquet` with no nulls and prints PRIOR/HISTORY/MIXED counts; `.\nhl.ps1 run --offline` now reports MODEL_STATUS=HISTORY or MIXED where the crosswalk resolves.

**Do not.** Simulate. Fetch news. Tune weights to any single slate.

---

### C6 · Aggregate joint simulator, market fit, scoring arrays, calibration harness

**Depends on:** C5, C1. **Read first:** this card; plan §5 "Joint simulation" (including the per-person and pace-factor paragraph), "Vegas integration", "Simulation scale and calibration".

**Goal.** Seeded, chunked scenarios that produce realized stat lines for every person with the right joint structure, mapped to role rows and scored exactly, plus a harness that grades the simulator against held-out games.

**Create.** `src/nhl_dfs/sim/market.py`, `sim/game.py`, `sim/score.py`, `sim/cache.py`, `sim/validate.py`, `config/sim.yaml` (budgets 5,000 design / 20,000 selection / 20,000 referee; pace-factor dispersion; EN probabilities; OT and shootout rules by game type; memory cap), `tests/test_market.py`, `tests/test_game.py`, `tests/test_score_arrays.py`, `tests/test_validate.py`; `cli simulate --run <id> --n <N>`, `cli calibrate --seasons <n>`.

**Interfaces.**

```python
# sim/market.py
def implied(ml_a: int, ml_b: int) -> tuple[float, float]
def fit_game(odds: GameOdds | None, strength: TeamStrength, asof_utc, goalie_confirmed_at) -> GameRates     # lambda_home, lambda_away, p_home_reg_win, p_ot, source MARKET|MODEL, stale
# sim/game.py
def simulate(slate: SlateSpec, params: ParamTable, n: int, seed: int) -> Outcomes
# Per scenario, per game: draw starters and dressing; draw a team pace factor (Gamma) per team; PP opportunities; EV/PP/SH team goals; each goal -> unit, scorer, 0–2 assisters (scorer excluded);
# SOG per PERSON ~ NB(mean = sog60 * TOI * pace), scorer SOG >= goals; blocks scaled by opponent pace; goalie shots = opponent SOG, saves = shots − GA (EN not charged);
# regulation tie -> OT goal or shootout; decision; shutout flag; EN goals from a trailing-team process. Not-dressed persons: all zeros. Outcomes are per PERSON (n, P) int16 arrays.
# sim/score.py
def base_tenths(outcomes: Outcomes, params: ParamTable) -> np.ndarray        # (n, P) per person
def role_tenths(base_person: np.ndarray, role_map) -> np.ndarray             # (n, R): each role row copies its person's column; CPT rows are NOT multiplied here
def lineup_twentieths(role_base: np.ndarray, lineups: np.ndarray, captain_col: int | None) -> np.ndarray   # (n, L); captain ×1.5 applied exactly once here
# sim/cache.py: chunked arrays under runs/<id>/sim/ with seed and hashes, respecting the memory cap
# sim/validate.py
def report(as_of_dates, cfg) -> CalibrationReport    # PIT histograms per stat; observed vs simulated rates of 5+ SOG, 3+ blocks, 3+ points, hat tricks, 35+ saves; line-pair co-ceiling; team totals vs market; goalie win vs implied -> docs/calibration/<date>.md
```

**Tests.** Conservation (team goals = Σ person goals; assists ≤2 per goal and never the scorer; saves + GA = opposing SOG while in net; EN goals not charged); a Showdown person's CPT and FLEX role columns are identical in every scenario and the captain multiplier changes the lineup score exactly once; a person drawn as not dressed has zero minutes and events; same seed reproduces bit-for-bit; scoring arrays equal `contracts.scoring` on 1,000 random stat lines; team SOG dispersion with the pace factor exceeds independent NB dispersion and matches a fixture target; market fit reproduces implied win probability within 0.01 and total within 0.1; stale rule flips when a goalie confirmation postdates `as_of`; validate writes a report on fixture history.

**Exit checks.** `pytest -m c6 -q` green; `.\nhl.ps1 simulate --run <run> --n 20000` completes for the real Classic fixture; record wall clock (target ≤90 s; a miss is recorded, not a failure); `.\nhl.ps1 calibrate --seasons 1` writes a calibration report.

**Do not.** Build the segment simulator (C13). Add a stack bonus. Store a candidates × scenarios matrix for all candidates.

---

### C7 · Roles and news, deterministic

**Depends on:** C5, C1. **Read first:** this card; plan §11 "Evidence and role updates" (including the status map) and "Goalies before confirmation"; §3 Daily Faceoff rows.

**Goal.** Tonight's lines, PP units, goalie states, and participation as a deterministic role state that adjusts the parameter table, with a validated override schema for the LLM path built in C10.

**Create.** `src/nhl_dfs/data/sources/dailyfaceoff.py`, `models/roles.py`, `models/overrides.py`, `build/news.py`, `docs/sources.md` (the confirmed goalie parse path and its fallbacks), an update to `tools/capture.py` to save the confirmed goalie data path, `tests/fixtures/http/dailyfaceoff_*.html` (one team page; one goalie page with its script tags), `tests/test_dailyfaceoff.py`, `tests/test_roles.py`, `tests/test_overrides.py`; `cli roles --salary <path> [--offline]`.

**Interfaces.**

```python
# data/sources/dailyfaceoff.py
def team_lines(slug) -> TeamLines                         # updated_utc from "Last updated"; f_lines, d_pairs, pp1, pp2, pk1, pk2, goalies, injuries [(name, status_text)]
def starting_goalies(date) -> list[GoalieReport]          # 1) parse <script id="__NEXT_DATA__"> JSON if present; 2) else per-team pages' goalie section; 3) else [] (DK status and rotation carry it). Path used is logged.
# models/roles.py
def merge(dk: Reconciliation | None, df_lines, df_goalies, rotation, now_utc, cfg) -> RoleState
# Participation precedence: DK OUT (mapped) -> OUT; DK QUESTIONABLE -> haircut; DK UNKNOWN -> no change, reported; explicit named goalie confirmation -> CONFIRMED; depth order -> EXPECTED; contradictions -> CONFLICTED mixture.
# Eligibility DISABLED excludes from selection regardless of participation. Age policy per plan §11.
# models/overrides.py
@dataclass class Override: role_id; nhl_id; game_id; field; old; new; effective_utc; source_url; claim; confidence; expiry_utc
def validate(o, roles) -> list[str]; def apply(params, roles, overrides) -> ParamTable      # conserves team minutes; call-up role expectation applied here for unmatched persons
# build/news.py
def state(roles, pool) -> NewsState
```

**Tests.** Team-page parser on the fixture returns lines, pairs, PP units, goalies, injuries, timestamp; goalie parser takes the `__NEXT_DATA__` path on the fixture and the per-team path when the tag is removed, and returns [] when both fail; a fetched old confirmation never changes its game date; conflicting goalie reports produce a mixture with a warning; DK OUT overrides a Daily Faceoff line listing; DK UNKNOWN changes nothing and is reported; `isSwappable` never affects participation; `apply` conserves team EV minutes; an override that overfills a unit is rejected.

**Exit checks.** `pytest -m c7 -q` green; `.\nhl.ps1 roles --salary <real classic> --offline` prints per-team role state from captured HTML; `docs/sources.md` states the goalie path in use.

**Do not.** Call the LLM. Change ownership. Invent injury states.

---

### C8 · Scenario objectives by contest family, evidence states, frontier, allocation, full run

**Depends on:** C6, C3. **Read first:** this card; plan §7 in full; §10 ladder steps 2 and 5.

**Goal.** `nhl.ps1 run` publishes the baseline, then the provisional pass, then a scenario-based portfolio: per-family objectives against a weighted sampled field in shared scenarios, exact payouts with DK tie rounding, the tie-break, mode-aware exposure and concentration, a frontier against the risk budget, and fee-weighted allocation.

**Create.** `src/nhl_dfs/build/objectives.py`, `build/tiebreak.py`, `build/exposure.py`, `build/portfolio.py`, `config/risk.yaml` (placeholder budget, mode-aware, `[BEN]` comment), `docs/payouts.md` (the DK Terms of Use tie wording as verified, with date), `tests/test_objectives.py`, `tests/test_tiebreak.py`, `tests/test_exposure.py`, `tests/test_portfolio.py`, `tests/test_run_full.py`, `tests/bench_field.py`; `cli run` full path.

**Interfaces.**

```python
# build/objectives.py
def contest_metrics(cand: np.ndarray, field: np.ndarray, weights: np.ndarray, contest, own_copies) -> Metrics
# cand (S, K), field (S, F_distinct ≤ 5,000) in twentieths from the same scenarios; weights are multiplicities summing to field size; weighted ranks include own copies;
# ties: pooled tier cash / tied count, quantized ROUND_DOWN to the cent (Decimal). Per family: gpp -> exp_payout and p_top1pct; wta -> tie-adjusted first-place equity;
# cash/h2h/double_up -> p_clear_line; satellite -> p_seat with ticket face value separate. Scoring chunked by scenario within config memory cap.
def portfolio_metrics(assignment, contests, scenarios) -> PortfolioMetrics    # R_s, p_zero_payout, p_net_loss, p_lose80, es5, tail_utility (family-specific), concentration by goalie/game/captain
# build/tiebreak.py
def prefer(a, b, own_a, own_b, dup_a, dup_b, band) -> "a" | "b"        # band ≥ Monte Carlo SE; ownership never moves a choice past one band; dup from sampled counts when FIELD_CALIBRATION=FITTED else proxy
# build/exposure.py
def caps(cfg, n_entries, pool, mode, n_games) -> Caps          # feasibility floor; per-game cap only when n_games > 1; Showdown concentration by captain and failure scenario
# build/portfolio.py
def select(candidates, role_base, fields, contests, entries, caps, risk: RiskBudget, families=(0.65, 0.25, 0.10), *, seed) -> Selection
# frontier over five knob settings; report excludes dominated points; choose the highest tail_utility with p_lose80 ≤ budget, else least-risk point (recorded); WTA entries allocated by first-place equity;
# every figure carries PAYOUT_SOURCE, OUTCOME_CALIBRATION, FIELD_CALIBRATION.
```

**Tests.** Three-way tie for first splits pooled cash rounded down to the cent; own copies count as copies; weighted field ranks equal explicit expansion on a small case; tie-break never prefers a candidate more than one band worse; WTA allocation picks higher first-place equity over higher variance on a constructed pair; cash objective ignores ownership; satellite objective counts seats; caps with two goalies raise to the floor; a single-game Showdown slate never triggers the per-game cap; frontier report has no dominated points; infeasible budget yields least-risk point and still a file; full run completes offline within 5 minutes.

**Exit checks.** `pytest -m c8 -q` green; `python tests/bench_field.py` reports field construction (5,000 lineups) plus payout evaluation for 150 candidates × 20,000 scenarios within 60 s and peak memory under the cap (record numbers); `.\nhl.ps1 run --salary <real> --entries <real> --offline` publishes baseline, provisional, and scenario versions with objective metrics and the three evidence states in `RUN_NOTES.md`. **Milestone 3: objective-aware portfolio.**

**Do not.** Call the LLM. Edit a CSV outside `write_entries` and the referee.

---

### C9 · Objective-aware refresh and late swap

**Depends on:** C8, C7, C2c. **Read first:** this card; plan §11 "Fast mode" and "Goalies before confirmation".

**Goal.** Late swap and refresh use the C2c lock model unchanged, but re-solve open slots with C8's scenario objective and C7's role state, and condition on live standings when supplied.

**Create.** Extensions to `build/late_swap.py` and `build/refresh.py` (objective selection: scenario when available, provisional otherwise, baseline as last resort), `build/live.py` (optional current-score conditioning), `config/runtime.yaml` extended (T-8 for LLM paths, T-5 for engine-only), `tests/test_late_swap_objective.py`, `tests/test_live.py`.

**Interfaces.** `late_swap.run(..., objective: Literal["scenario","provisional","baseline"]="auto")`; `refresh.run(...)` re-fetches roles (C7) and odds and re-simulates only affected games; `live.condition(scenarios, standings_snapshot)` restricts remaining upside to unlocked games and current scores; without a reliable snapshot it is a no-op with a status.

**Tests.** Pinned cells identical to C2c behavior; objective falls back in the documented order when the simulator cache is absent; a trailing entry prefers lower duplication only when a snapshot is present; no chase pivot without a snapshot; runtime ≤30 s on the fixture.

**Exit checks.** `pytest -m c9 -q` green; `.\nhl.ps1 late-swap --run <id> --entries <fixture> --fast --offline` ≤30 s.

**Do not.** Call the LLM. Change lock semantics.

---

### C10 · Claude Code layer: skills, agents, controller, rehearsal, measured usage

**Depends on:** C9, C7. **Read first:** this card; plan §8 table and paragraph; §9 in full; §13 skills and agents rows.

**Goal.** Slash commands that wrap the CLI with the engine running before the model reads anything; two fresh-context agents with JSON contracts, launched without project instruction files and fed inline; a controller that validates and applies their output deterministically with one-round default and wall-clock bounds.

**Create.** `.claude/skills/nhl-run/SKILL.md`, `nhl-refresh/`, `nhl-late-swap/`, `nhl-settle/` (stub until C11), `nhl-dev-next/`, `nhl-status/`, `nhl-qa-rehearse/`; `.claude/agents/nhl-researcher.md` (`tools: WebFetch, Read`; `omitClaudeMd: true`; returns Override JSON), `.claude/agents/nhl-adversary.md` (`tools: Read`; `omitClaudeMd: true`; prompt says the packet is inline and no file may be read; returns Proposal JSON); `.claude/settings.json` with `"env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1"}`; `src/nhl_dfs/build/packet.py`, `build/controller.py`, `docs/packet_schema.md`, `docs/proposal_schema.md`, `docs/measured_usage.md` (template), `tests/test_packet.py`, `tests/test_controller.py`; `cli qa-packet --run <id> --round k`, `cli qa-apply --run <id> --round k --proposals <path>`, `cli overrides-apply --run <id> --file <path>`.

**Interfaces.**

```text
Protocol (the model relays bytes; it never edits lineups):
  1. /nhl-run <salary> <entries>: SKILL.md preprocesses `!nhl.ps1 run --salary $0 --entries $1` and injects only the manifest summary (≤60 lines).
  2. If NEWS_STATE != FULL and unresolved players have material exposure: invoke nhl-researcher with the request JSON inline; save its reply verbatim to runs/<id>/news/overrides_k.json; run `nhl.ps1 overrides-apply`.
  3. `nhl.ps1 qa-packet --round 1` writes the packet; the skill passes its CONTENT inline to nhl-adversary (≤4,000 tokens: aggregates, flagged conflicts with IDs, top-10 alternatives with IDs, coverage, predeclared metrics, five representative lineups).
  4. Save the reply verbatim to proposals.json; `nhl.ps1 qa-apply` classifies (correctness | strategic), validates, re-solves, compares on the same scenarios, accepts or rejects with reasons, publishes if changed, prints whether another round is permitted.
  5. Rounds 2–3 only when round 1 accepted a correctness repair; stop on zero accepted, round 3, or deadline (T-8).
```

```python
# build/packet.py
def build(run, round_no, cfg) -> dict         # enforces the size cap by construction; never serializes lineups beyond the five-sample; includes IDs for every referenced person and alternative
# build/controller.py
@dataclass class Proposal: kind: Literal["correctness","strategic"]; target: dict; change: dict; evidence: str; source_url: str | None
def apply_round(run, round_no, proposals, cfg) -> RoundResult
# correctness (verified identity/scoring/lock/scratch fact) accepted without a simulation contest; strategic requires legality, risk budget, no decrease in tail or safety metric, and a gain beyond the band on paired scenarios;
# under FIELD_CALIBRATION=PRIOR an accepted strategic change is recorded as "unvalidated modeled improvement" with the state attached; malformed JSON ends QA and keeps the incumbent.
```

**Tests.** Controller: malformed proposals end QA and keep the incumbent; a correctness repair is accepted without a simulation contest; a strategic proposal within the band is rejected as inconclusive; a strategic proposal beyond the band and safe is accepted and labeled by field-calibration state; packet size cap enforced on a 150-entry fixture; a proposal touching a pinned cell is rejected.

**Exit checks.** `pytest -m c10 -q` green; in a fresh Claude Code session `/nhl-run` on the fixture completes with at most four model turns and publishes; `/nhl-qa-rehearse` launches the adversary with a canary packet inline, confirms the reply references the canary and nothing planted in the main conversation or in CLAUDE.md, and records the result in `docs/measured_usage.md`; `/usage` attribution after the run recorded there too.

**Do not.** Give either agent Write, Edit, Bash, or Agent tools. Let the model edit `DKEntries.csv`.

---

### C11 · Settle: financial ledger, grading, run notes, backlog, gate reports

**Depends on:** C8, C3, C0b. **Read first:** this card; plan §6 "After each slate"; §12 tables, financial-settlement paragraph, "Short run-note fields", "Backlog row". **Ben supplies:** standings exports in `data/standings/inbox/`.

**Goal.** Grade frozen pre-lock forecasts against actual ownership and results, settle the money by Entry ID, write run notes and backlog rows, and report which evidence tier is unlocked, changing no parameter.

**Create.** `src/nhl_dfs/learn/standings.py`, `learn/ledger.py`, `learn/grade_ownership.py`, `learn/grade_forecasts.py`, `learn/backlog.py`, `learn/notes.py`, `BACKLOG.md` (created with header), `data/ledger/` (gitignored), `tests/fixtures/standings/` (≤60-row trimmed Classic and Showdown exports), `tests/test_standings.py`, `tests/test_ledger.py`, `tests/test_grades.py`; `cli settle --run <id> --standings <path or zip>`; `nhl-settle` skill body.

**Interfaces.**

```python
# learn/standings.py
def read(path) -> Standings     # contest_id, entries [EntryRow(rank, entry_id, entry_name, points, lineup [(slot, name)])], ownership [OwnRow(name, roster_position, pct_drafted, fpts)], raw_sha
def join(s, pool) -> Joined     # normalized name + roster position token (+ team when the pool has one candidate); collisions -> CONFLICTED, never guessed; own entries joined by Entry ID exactly
# learn/ledger.py
def settle(run, standings: list[Standings], contests_final: dict[int, ContestDetail]) -> SlateLedger   # own entries -> final rank -> payout via final payout table with DK tie rounding; fees, gross, net, by contest; appends to data/ledger/ledger.parquet; rolling drawdown across slates
# learn/grade_ownership.py: grade(forecast: Marginals, actual: Joined, family) -> OwnershipGrade (mae_all, mae_active, band_calibration, top_chalk_recall, cpt_share_err, dup_count_err, zero_observed_mass)
# learn/grade_forecasts.py: grade(run, boxscores) -> ForecastGrade (mae, crps, p10_p90_coverage, bonus_rate_calibration, goalie decision accuracy; one game outcome counted once)
# learn/gates.py (from C3) is consulted and its tier report written to the run; nothing is tuned
# learn/backlog.py: add(row) with ID, date/run, problem or hypothesis, metric, bounded change, confidence/sample, acceptance test, priority, status, result
```

**Tests.** Fixture parse for both modes; CPT and FLEX ownership separate; Pettersson resolved by position token and a same-team-same-position collision flagged; own entries settle by Entry ID with a tie case rounded down; ledger net equals gross minus fees and drawdown updates across two synthetic slates; grades on synthetic forecasts reproduce known errors; gate report shows the correct tier for planted counts; settle appends backlog rows without duplicates.

**Exit checks.** `pytest -m c11 -q` green; `.\nhl.ps1 settle --run <id> --standings <fixture>` writes `grades.json`, the ledger row, updates `RUN_NOTES.md`, appends to `BACKLOG.md`. **Milestone 4: learning loop live.**

**Do not.** Change any weight or cap. Grade a forecast reconstructed after the fact.

---

### C12 · Field-model fit by contest family (gated)

**Depends on:** C11, and `learn/gates.allows("field_fit")` True for the mode in question (plan §12: ≥30 distinct Classic slate groups with ≥15,000 labels, or ≥50 Showdown games with ≥5,000 labels; latest ≥10 Classic groups or ≥15 Showdown games held out). Mixture-weight changes require `allows("mixture_fit")`, the higher tier. `tools/next_chunk.py` checks the counts per mode.

**Create.** `src/nhl_dfs/models/field_fit.py`, `docs/experiments/field_fit_<date>.md` (preregistration first), `tests/test_field_fit.py`; `cli fit-field --mode <classic|showdown>`.

**Interface.** `fit(standings, pools, mode, cfg) -> FitResult(utility_offsets, mixture_weights | None, holdout_mae_before, holdout_mae_after, decision: "promote_shadow" | "rejected")`; shadow mode by default.

**Exit checks.** `pytest -m c12 -q` green; the experiment file records the result. The chunk is DONE whether the challenger is promoted to shadow or rejected. Promotion to live follows plan §12 in a separate session.

---

### C13 · Segment simulator challenger (gated)

**Depends on:** C11, C6, a calibration report in `docs/calibration/` showing line-pair co-ceiling or bonus-rate error outside tolerance, and a preregistration in `docs/experiments/` written before code.

**Create.** `src/nhl_dfs/sim/segment.py` behind `sim.engine: aggregate | segment`, `docs/experiments/segment_<date>.md`, `tests/test_segment.py`.

**Exit checks.** Same conservation and per-person tests as C6; walk-forward comparison on saved scenarios; DONE whether promoted to shadow or rejected.

---

### C14 · Lock-safe publishing (band 0)

**Depends on:** C10. **Read first:** this card; BACKLOG B52, B53; review R01 and R02 (`reviews/2026-10-03_independent_code_review_7140e0a.md`); plan §10 and §11; `build/controller.py` apply_round (clock lines near 280 and the publish block near 510); `build/run.py` run_slate and `_export_and_publish`; the lock API in `build/locks.py`.

**Goal.** No publication path can commit cells from a game that has started or crossed EDIT_STOP: the QA and override rounds use a live clock for their final recheck, and the initial run rechecks lock state before every publish, keeping the checked predecessor when the boundary is crossed.

**Create.** In `build/controller.py`: a round-start timestamp distinct from a callable live clock (an explicit fixed clock only for rehearsal). In `build/run.py` and `build/scenario_pass.py`: a lock check in `_export_and_publish` (or its caller) before optional work and again before committing a changed export, honoring the publication-lock wait; with v1 present a crossed boundary retains v1 and reports why. `tests/test_lock_safe_publish.py`.

**Tests.** R01's acceptance on both call shapes (CLI-style `now=` and no `now`) with no injected clock; time advanced through EDIT_STOP and through LOCKED during the solve publishes nothing and leaves the incumbent hash and public bytes unchanged; a before-boundary control still publishes. R02's acceptance for Phase A and each later route, with a multi-game file whose early game is locked; independent of In-Progress markers.

**Re-verified 2026-10-04 (R01 and R02 reproduced by a second reader).** R01: the CLI freezes the clock too (`cli.py:629` and `:682` pass `now=datetime.now()`; pass `clock=lambda: as_of` only for `--as-of`, as `late_swap.run` does); the 15 `_apply` call sites in `tests/test_controller.py` (fixtures dated 2026-10-15) move to `clock=lambda: now` or they turn wall-clock dependent after that date; evaluate the final recheck under the publish lock (`state.py:227` waits up to 60 s). R02: one guard in `_export_and_publish` covers phases A, B, P and S (only three `publish()` call sites exist: `run.py:581`, `controller.py:533`, `late_swap.py:700`); it diffs against the predecessor's pinned cells as `late_swap.py:677` does and never bans started-game cells outright; with no predecessor the first publish ships (flag 20, default taken).

**Exit checks.** `pytest -m c14 -q` green; the full suite green; a rehearsal run on the late-swap fixture with the clock advanced mid-build reports the stop and keeps v1.

**Do not.** Change lock semantics (C2c). Touch B42's started-slate build (C15).

### C15 · Started slates (band 0)

**Depends on:** C14. **Read first:** this card; BACKLOG B42 (parts 2 and 3); flag 15; `intake/salary.py` STARTED_GAME rows; `build/locks.py`; `build/late_swap.py` pin handling; `build/run.py` run_slate start check and `slate_id_for`.

**Goal.** A slate that has already started still yields a legal file: late swap pins every occupant of a started game, run_slate builds the open games instead of refusing, and the slate id stays stable when started rows leave the pool.

**Create.** `build/locks.py`: a started-game occupant is pinned even when its row carries no start time. `build/run.py`: when `now` is past the first start and flag 15's default holds, exclude STARTED_GAME rows, pin their cells, build the rest, and name the started games in RUN_NOTES and stdout. `slate_id_for` from the full ID set. `tests/test_started_slate.py`.

**Tests.** A post-lock export with lineups in the started game: late swap keeps those cells and fills the rest; a blank-entries export builds with no started-game player; the slate id of a started-game file equals the pre-start id; the real post-lock fixture test keeps skipping loudly until Ben saves one.

**Exit checks.** `pytest -m c15 -q` green; the full suite green; the 10-02 salary file (DKSalaries_127, In-Progress rows) runs `slate` to a checked file with the started game excluded.

**Do not.** Add a player whose game has started. Change the referee.

### C16 · Payout curves from DraftKings template tables (band 1)

**Depends on:** C11. **Read first:** this card; BACKLOG B62, B50; flag 14; `models/contests.py` (classification, the 403 path); `build/objectives.py` prior_curve; `learn/ledger.py` prize_tables; `scripts/standings_synthesis.py` template_tables (the matcher prototype); `data/raw/dk_contest/` format.

**Goal.** A contest whose DraftKings page is unavailable is priced on the cached table of the same template name and max entries, labeled PAYOUT_SOURCE=TEMPLATE, at build time and in settle; PRIOR remains for unmatched contests.

**Create.** One matcher module (name without the game suffix plus max entries, from the lobby capture) used by `models/contests.py` and `learn/ledger.py`; `build/objectives.py` builds the curve from the matched tiers; manifest and RUN_NOTES carry the source and the table's contest id; `tests/test_template_payouts.py`.

**Tests.** The 09-30 mini-MAX (11,890) matches the 09-28 table with 2,732 paid places and the 10-02 Daily Dollar (1,189) matches 195958173's saved table; a satellite or resized contest never matches; an unmatched contest reports PRIOR; settle labels the matched payouts TEMPLATE below EXACT and above UNKNOWN, with REPORTED still winning.

**Exit checks.** `pytest -m c16 -q` green; `.\nhl.ps1 run --offline` on the 09-30 inputs prints PAYOUT_SOURCE=TEMPLATE for the two matched contests.

**Do not.** Fetch DraftKings. Invent a table for an unmatched name.

### C17 · Inputs that move lineups reach the models (band 1)

**Depends on:** C10. **Read first:** this card; BACKLOG B63, B20, B51, B66; `models/ownership.py` feature_table, team_odds and utilities; the build_fields callers (`build/run.py`, `build/scenario_pass.py`, `build/provisional.py`); `sim/market.py` max_discrepancy; RUN_NOTES of run 20260930-210607-classic (the COL league-rate line).

**Goal.** The ownership prior sees the slate's odds, role state and news ages on every path; the team-rate fallback that gave Colorado a league-average rate is traced and replaced by a team source; the market clip depends on the model side's status.

**Create.** Wiring in the three callers with a MARKET and ROLES coverage line for the field features; a test per weight (implied_total, goalie_start_win, pp1, line1, news_recent) showing the forecast moves; a scratch replay of the 09-30 inputs recorded in `docs/experiments/` or the chunk's notes (B51); the clip rule in `sim/market.py` (B66). `tests/test_field_features.py`.

**Tests.** Each weight changes ownership when its input changes and leaves it unchanged when absent; the 09-30 replay moves COL's stack frequency toward 38 to 51% and TOR's toward 21 to 24% (recorded, not asserted as a threshold); the clip is wide when the model side is PRIOR and 0.35 when it is HISTORY.

**Also carried (B85).** Set the OTT DraftKings team code in `config/teams.yaml` from the lobby GameSets capture (OTT @ TOR on 10-02 and 10-03) and mark it verified, so OTT games price on the market feed; one S commit with a lobby fixture test.

**Exit checks.** `pytest -m c17 -q` green; the full suite green; settle's ownership MAE on the 09-30 record does not rise when re-graded against the replay.

**Do not.** Fit anything from standings (C12). Change the field behaviors (C19).

### C18 · Joint own-entry accounting (band 1)

**Depends on:** C14. **Read first:** this card; BACKLOG B55, B56; review R04 and R05 with their fixtures; plan §7; `build/portfolio.py` `_greedy`, `_joint_from_states`; `build/objectives.py` `own_pairwise`, `metrics_from_ranks`, `joint_payouts`; `build/controller.py` evaluate.

**Goal.** Every insertion and every QA comparison scores the change to the whole owned-contest payout and utility vector; running totals never hold prizes that cannot be paid together.

**Create.** A joint-accounting primitive in `build/objectives.py` (incremental rank updates allowed) used by `_greedy`, screening or local improvement where it stands for marginal value, and `controller.evaluate`; `tests/test_joint_accounting.py` with the review's two fixtures.

**Tests.** The R04 counterexample selects A+C; after every insertion the incremental totals equal an independent `joint_payouts` recomputation; strict overtakes, tier ties, repeat lineups, several contests and a cash contest; own cash in a contest and scenario never exceeds its prize pool; the R05 zero-gain transfer is inconclusive, a transfer that lowers aggregate utility is rejected, a true beyond-band gain is accepted.

**Re-verified 2026-10-04 (R04 and R05 reproduced).** The accounting fix costs O(placed entries) per candidate per step unless it keeps per-entry rank vectors or a sparse paid-region update: design that first, since 150-max contests times five knobs is the budget. The same omission is in `swap_objective.choose` (`swap_objective.py:447` to `481`) and `late_swap.py:619` to `625`, where a repair candidate is scored against its siblings as opponents only: include them or file the row. R05: aggregating over all entries of the touched contests moves the 3% band anchor to whole-contest utility, so choose the anchor or a paired aggregate SE deliberately, and add a per-scenario top-1% array to Metrics for a paired tail delta.

**Exit checks.** `pytest -m c18 -q` green; `python tests/bench_field.py` and the C8 wall-clock measurement recorded (a miss is recorded, not a failure).

**Do not.** Claim global optimality. Change the frontier's kappas or the risk budget.

### C19 · Field stack mix by slate size (band 1)

**Depends on:** C17. **Read first:** this card; BACKLOG B43, B15; `reviews/2026-10-03_standings_synthesis.md` §3 and §4 (the shape tables); `models/field.py` behaviors and `_stack_min`; `models/field_fast.py` `_forced`; `config/ownership.yaml` field.behaviors and mixtures; `build/packet.py` `_stack_family`.

**Goal.** The sampled field carries the observed stack mix: a team4 behavior and a double-stack (4-3) behavior, mixture weights per family and slate size, and a shape-mix line in RUN_NOTES that can be compared with the synthesis table after each slate.

**Create.** The two behaviors in `models/field.py` and `models/field_fast.py`; mixture tables keyed by family and game count in `config/ownership.yaml` (values from the pooled table, labeled PRIOR); the RUN_NOTES line; `tests/test_field_mix.py`.

**Tests.** On the 09-30 pool the sampled field's 3+ and 4+ stack shares land within 10 points of 93.5% and 65.7%; team3 draws still exist; the fast and MILP samplers agree on the new rules within the C8 tolerance; FIELD_CALIBRATION stays PRIOR.

**Exit checks.** `pytest -m c19 -q` green; `python tests/bench_field.py` within budget; the shape line prints on a rehearsal run.

**Do not.** Fit mixture weights from standings (that is C12's gate). Touch selection.

### C20 · Goalie as the top-1% factor (band 1)

**Depends on:** C18. **Read first:** this card; BACKLOG B37, B45; flag 13; the goalie tables in `reviews/2026-10-03_standings_synthesis.md`; `build/packet.py` research request; `build/candidates.py` and `build/milp.py`; `build/exposure.py` shared failure; `build/portfolio.py` discovery per_goalie.

**Goal.** Unresolved goalie pairs on slate teams enter the research request; no GPP lineup rosters a skater against its own goalie (flag 13 default); the goalie choice carries an explicit leverage term, tested as an experiment.

**Create.** B37 in `build/packet.py` bounded by qa.yaml max_players; the own-goalie constraint in the candidate builder with a referee report line; a goalie term in the discovery objective behind a config key, with a preregistered sim comparison recorded in `docs/experiments/goalie_leverage_<date>.md`; `tests/test_goalie_factor.py`.

**Tests.** The research request lists both goalies of an unresolved team; no candidate holds a skater facing its goalie in large_gpp, small_field or wta; cash and satellite lineups may; the experiment's acceptance (modeled top-1% equity with and without the term on saved caches) is recorded whichever way it goes.

**Exit checks.** `pytest -m c20 -q` green; GOALIE_CAP and the shared-failure line still print; the experiment file exists. DONE when the leverage term is rejected is valid.

**Do not.** Exclude uncertain goalies. Change GOALIE_CAP's budget.

### C21 · Stack size by slate blow-up (band 1, experiment)

**Depends on:** C19, C18. **Read first:** this card; BACKLOG B44, B22; the by-slate table in `reviews/2026-10-03_standings_synthesis.md` §3; `config/risk.yaml` discovery; `build/portfolio.py` discovery families; the scenario caches of runs 20260929-222125-classic and 20260930-214909-classic.

**Goal.** Measure, on saved scenario caches, the modeled top-1% equity of 4-3-1, 5-2-1, 6-1-1 and 3-3-2 shapes per slate size; add a stack-size sleeve only if the study supports it.

**Create.** `docs/experiments/stack_size_<date>.md` (preregistration first, result after); if supported, a shape-aware discovery family in `build/portfolio.py` sized by game count and the sim's P(team scores 6+), and a shape-mix line in RUN_NOTES; `tests/test_stack_size.py`.

**Tests.** The study reproduces its numbers from the caches with a fixed seed; the sleeve, if built, never breaks the caps and reports its share; the shape mix line matches the portfolio.

**Exit checks.** `pytest -m c21 -q` green; the experiment file records promote-to-shadow or rejected. DONE on rejected is valid.

**Do not.** Tune from one week of standings; the sleeve stays a sized sleeve, not a rule.

### C22 · Chalk core kept, leverage in the depth slots (band 1, experiment)

**Depends on:** C19. **Read first:** this card; BACKLOG B46; the leverage tables in `reviews/2026-10-03_standings_synthesis.md`; `build/tiebreak.py`; `build/provisional.py` ranking; `build/portfolio.py` own_then_dup.

**Goal.** Measure how often the band-only ownership tie-break moves a pick away from the slate's top two chalk pieces; exempt the core from the penalty only if it does.

**Create.** A measurement script or test over the saved caches; if warranted, the exemption in `build/tiebreak.py` behind a config key with the result in `docs/experiments/chalk_core_<date>.md`; `tests/test_chalk_core.py`.

**Tests.** The measurement is reproducible; with the exemption on, the top two players by projection x ownership are never displaced by the tie-break; without it, behavior is unchanged.

**Exit checks.** `pytest -m c22 -q` green; the experiment file exists. DONE on rejected is valid.

**Do not.** Add a new objective term; this is a tie-break change at most.

### C23 · Showdown Captain choice (band 1, experiment)

**Depends on:** C20. **Read first:** this card; BACKLOG B49; flag 12; the Showdown section of `reviews/2026-10-03_standings_synthesis.md`; `models/field.py` captain rules; the Showdown branch of `build/candidates.py`; `build/exposure.py` captain cap.

**Goal.** Measure Captain share and goalie-in-lineup share per cohort on the saved Showdown standings, then test skater-Captain by default (flag 12) and a Captain leverage term as an experiment.

**Create.** The per-cohort report in `scripts/standings_synthesis.py` (already prints Captains; add the goalie-Captain and split sleeve lines if missing); the default and the term behind config keys; `docs/experiments/captain_<date>.md`; `tests/test_captain_choice.py`.

**Tests.** A goalie is Captain only when the sim's goalie ceiling ranks top-3; the captain cap still binds; the team split sleeve sizes follow the sim's goal share and report themselves.

**Exit checks.** `pytest -m c23 -q` green; the experiment file exists. DONE on rejected is valid.

**Do not.** Build from the one ANA@VGK game alone; the acceptance waits on the next five Showdown slates.

### C24 · DTD participation inside the simulator (band 1)

**Depends on:** C6. **Read first:** this card; BACKLOG B14; flag 8; `sim/game.py` dressing step; `sim/outcomes.py`; `models/roles.py` p_play; `tests/test_frozen_record.py`.

**Goal.** A QUESTIONABLE player's participation is drawn inside the dressing step, once, from p_play, so a sitting player's minutes and events go to teammates instead of vanishing.

**Create.** The draw in `sim/game.py` with conservation tests; frozen-record and calibration updates; `tests/test_dtd_dressing.py`.

**Tests.** Team minutes and shots are conserved when a DTD player sits; the mean of the DTD player equals p_play times his dressed mean; frozen records that change are regenerated and named.

**Exit checks.** `pytest -m c24 -q` green; `calibrate` rerun recorded.

**Do not.** Change p_play_questionable (flag 8).

### C25 · Clean calibration and the tail preregistration (band 1)

**Depends on:** C6. **Read first:** this card; BACKLOG B64, B6; `docs/calibration/2026-09-29.md` and `.json`; `sim/validate.py`; the history store's linemate data.

**Goal.** A calibration report whose constants exclude the evaluation dates, with the co-ceiling cause tested and the 3+ point tail preregistered for B6, so C13's gate reads clean numbers.

**Create.** The constant recomputation in `sim/validate.py`; `docs/calibration/<date>.md` and `.json` with machine-readable deficiencies; the co-ceiling cause test (actual linemates against rank-built lines); `docs/experiments/tail_<date>.md` preregistration; `tests/test_calibration_clean.py`.

**Tests.** The constants used exclude the held-out dates; the report lists the deficiencies in the json; the co-ceiling result names a cause or rejects both.

**Exit checks.** `pytest -m c25 -q` green; the report exists.

**Do not.** Tune any parameter here.

### C26 · Controller safety (band 2)

**Depends on:** C18. **Read first:** this card; BACKLOG B54, B41, B57, B60; review R03, R06, R09; `build/controller.py` repair solves and apply_round publish; `models/overrides.py` validate, apply_to_roles; `build/exposure.py` caps; `cli.verify_run`.

**Goal.** Repairs never roster a known OUT player, accepted QA changes respect the caps, contradictory override batches become CONFLICTED, and the manifest names the delivered bytes after QA.

**Create.** One exclusion mask for every controller add or repair path; cap checks with REJECTED_CAP or a re-measured budget for the published version; transactional batch validation; export_sha256 set on controller publication and verified; `tests/test_controller_safety.py`.

**Tests.** The review's OUT goalie is never introduced, plus a skater, a strategic-exclude and Showdown cases; UNKNOWN controls stay available; a swap breaching GOALIE_CAP is REJECTED_CAP; the two-confirmation batch is CONFLICTED in both orders; tampering with the top-level hash fails verification.

**Re-verified 2026-10-04 (R03, R06 and R09 reproduced).** R03: the repair also re-introduces DK Starting=P backup goalies (`run.dk_backup_goalies`); reuse `late_swap`'s `out_people` construction (`late_swap.py:470` to `497`) as the mask, and carry accepted OUT overrides across rounds (`apply_round` starts `excluded_people` empty each round, `controller.py:331`). R06: also validate against previously accepted overrides (`role_state()` at `controller.py:349` to `355` never reads `news/accepted_overrides.json`), and every consumer must honor CONFLICTED (`late_swap.accepted_overrides:200` to `203`, `goalies.build:177` to `180`, `swap_objective.py:178` to `179`). R09: mirror `late_swap.py:708` and `run.py:600`; the new verify check needs a tolerance for pre-fix manifests (the 10-01 v4 run); nothing reads `manifest['export_sha256']` today, so judge it provenance only.

**Exit checks.** `pytest -m c26 -q` green; a rehearsal QA round on the fixture prints RISK_BUDGET for the published version.

**Do not.** Exclude uncertain players. Change the round rule (C31).

### C27 · Portfolio caps (band 2)

**Depends on:** C18. **Read first:** this card; BACKLOG B58, B13; review R07; flag 2; `build/exposure.py` fee_floor, concentration_cap, concentration; `build/portfolio.py` caps region.

**Goal.** The dollar cap is dropped only when infeasibility is proven; the plan's 30% line-core cap and the line and PP-unit fee shares are enforced and reported; the goalie share lives under one key.

**Create.** A lower bound and an exact small-instance check (or HiGHS) in `fee_floor`; line-core and PP-unit shares in `concentration` with a count cap at 20+ entries; the key reconciliation in `config/risk.yaml` and `config/exposure.yaml` with comments; `tests/test_caps_feasibility.py`.

**Tests.** The seven-fee case stays DOLLARS 0.40 for goalie and game; a single entry above 40% still takes the fallback; heterogeneous cases match exhaustive partitions; the line-core share prints and binds at 20 entries.

**Re-verified 2026-10-04 (R07 reproduced: fee_floor 0.4074 against a brute-force optimum 0.3333; LINEUPS 3 of 7 can put 52% of fees on one goalie).** An exact check over fee classes (a DP or a small ILP) with a timeout labeled unresolved is cheap because fees take few distinct values. Other sites: `exposure.py:154` share_cap (the Showdown Captain budget uses max(budget, LPT floor)), the 'fee floor' text in `cap_status` and notes, `docs/CONTRACTS.md:142` to `146`. Judged to fire on mixed-fee slates only.

**Exit checks.** `pytest -m c27 -q` green; RUN_NOTES shows the new shares on the fixture.

**Do not.** Change the 40% budget (Ben's, flag 2).

### C28 · Role state in Phase A and in simulate (band 2)

**Depends on:** C15. **Read first:** this card; BACKLOG B9; `build/run.py` Phase A; `cli` simulate; `models/roles.py` merge and apply_state.

**Goal.** v1 and simulate use the merged role state for goalies and dressing, so the baseline file never carries a goalie the role state already rules out.

**Create.** The merge call before Phase A's projection and in simulate; a ROLES line in Phase A's notes; `tests/test_phase_a_roles.py`.

**Tests.** A CONFIRMED starter is the v1 goalie; a ruled-out goalie never appears in v1; simulate's output matches the run's role state.

**Exit checks.** `pytest -m c28 -q` green; the full suite green.

**Do not.** Change the late passes (C9, C10).

### C29 · Settle without a local run, stable slate groups (band 3)

**Depends on:** C11. **Read first:** this card; BACKLOG B47, B59; review R08; `learn/settle.py` run and evidence_counts; `learn/ledger.py`; `scripts/standings_synthesis.py` lobby_meta; the winnings.csv templates under `data/standings/inbox/2026-10-01` and `2026-10-02`.

**Goal.** Entries that exist only in standings are booked (money from standings, the lobby capture and winnings.csv; labels with forecast NONE; rows tagged NO_RUN), and independent slate groups count by date and games.

**Create.** `settle --no-run --standings <folder> [--winnings <csv>]`; the group key in evidence_counts; the checklist status for settled-without-run; `tests/test_settle_no_run.py`.

**Tests.** The review's two-record case counts one group; a duplicate export, a fresh-salary run and a parent and child each count once; the 10-01 and 10-02 folders settle to 20 entries with known fees and UNKNOWN or REPORTED payouts.

**Re-verified 2026-10-04 (R08 reproduced: slate_groups=2 for one slate).** The group key `{slate date}|{sorted games}` needs no migration (entries already store both); B47 no-run records need a fallback from the lobby capture; consider clustering by shared games; `settle.py:88` adds a slate group before the contest loop; `models/prefit.py` `counts_for` is the same class with a separate counter. Also carried: B68 (default taken, flag 16: a cloud run persists only the small frozen record, and settle degrades to money and lineup match from `data/entered/` and says so) and B84 (complete_payout per date from the union of settled contests).

**Exit checks.** `pytest -m c29 -q` green; `python scripts/standings_checklist.py` shows the two dates settled.

**Do not.** Grade a forecast that does not exist.

### C30 · Standings tooling (band 3)

**Depends on:** C11. **Read first:** this card; BACKLOG B48; `scripts/standings_checklist.py` status classification; `.claude/skills/standings-checklist/SKILL.md`; `.claude/skills/nhl-settle/SKILL.md`.

**Goal.** The checklist sees contests that exist only in filed standings, and the synthesis runs after each pull.

**Create.** The filed-standings scan and `--contest <id> [<date>]` in the checklist; a synthesis step in the settle skill; `tests/test_standings_checklist.py` extensions.

**Tests.** The 13 contests of 10-01 and 10-02 are listed and counted; a hand-added contest is awaited.

**Exit checks.** `pytest -m c30 -q` green.

**Do not.** Fetch DraftKings.

### C31 · QA throughput and provenance (band 3)

**Depends on:** C26. **Read first:** this card; BACKLOG B38, B39; `build/controller.py` proposal loop; `build/packet.py`; `build/notes.py`; CLAUDE.md round rule; `.claude/agents/nhl-adversary.md`.

**Goal.** Same-entry proposals are judged independently against the incumbent, the packet carries per-entry team counts, the round rule reads the same in CLAUDE.md and the controller, and RUN_NOTES carries metrics for the published version with before and after for accepted swaps.

**Create.** The independent evaluation or DEFERRED label; the packet field; the rule reconciliation; metric tables by version in `build/notes.py` and `versions/vN/referee.json`; `tests/test_qa_round.py` extensions.

**Tests.** Two same-entry proposals are both evaluated; a conflict is DEFERRED; the packet shows team counts; after a QA swap RUN_NOTES shows v4 figures.

**Also carried (B80, B81).** B80: print the signed gain and say 'worse than the incumbent' when the gain is below minus the band (`controller.py:249` and `:471`). B81 (default taken, flag 17): relaunch the adversary with the exact printed packet when a relayed value is wrong, one reply per round, and `qa-apply` records the packet hash it evaluated against.

**Exit checks.** `pytest -m c31 -q` green.

**Do not.** Call the LLM in tests.

### C32 · Hygiene (band 3)

**Depends on:** C11. **Read first:** this card; BACKLOG B65, B29, B32, B10, B11; the read-only audit table in the Queue rationale; `sim/game.py` `_shootout`; `data/history` evict_raw.

**Goal.** Every config key has a reader or a comment naming the pass that reads it; docstrings describe the current odds path; the shootout never credits an undressed skater; raw bodies are evicted; NewsState has no unreachable members; apply_state runs once.

**Create.** One commit per item in the breakpoint order; `tests/test_hygiene.py` where a test is missing (the B29 xfail becomes a pass).

**Tests.** As each item states; the xfail-strict test passes.

**Also carried (B83, B65 rider).** B83: RUN_NOTES prints the families the run used, not the `select()` defaults. B65 rider: remove the dead helpers `objectives._stack`, `swap_objective._avail`, `RoleState.participation_probs` and `dailyfaceoff.slug_for_nhl`, and correct the stale docstrings at `live.py:3`, `sim/slate.py:42` and `prefit.py:8`.

**Also carried (B86).** `tools/next_chunk.py` reruns every DONE chunk's checks at each session start (819 s measured on 2026-10-04 under contention): skip a chunk whose files' git tree hash is unchanged since its last passing run, keep `--all` for the full rerun and print the skipped chunks; a bare run on an unchanged tree finishes in under 60 s.

**Exit checks.** `pytest -m c32 -q` green; the full suite green with no xfail.

**Do not.** Move any lineup.

### C33 · Referee raw-byte check (band 3)

**Depends on:** C0b. **Read first:** this card; BACKLOG B61; review R10; `referee/check_file.py`; the export writer's field spans (read, never imported).

**Goal.** The referee rejects byte changes outside editable cells even when the parsed value is unchanged, and binds pinned cells to parent bytes.

**Create.** A raw-span tokenizer for entry lines; the pinned-cell byte binding; `tests/test_referee_bytes.py`.

**Tests.** Quote-equivalent changes to metadata and pinned cells are rejected; correctly spliced open-cell changes pass; BOM, CRLF and LF, embedded lists, quoted names and reordered headers covered.

**Re-verified 2026-10-04 (R10 by code trace only).** Judged hand-edit only: the splice path (`late_swap.splice_cells`) copies unchanged bytes verbatim, pinned cells included, and only `export.writer.write_entries` re-formats roster cells (editable there); `controller.py:526` calls `check_file` without `parent_path`. Scope: the validator.

**Exit checks.** `pytest -m c33 -q` green; the referee still verifies every real fixture.

**Do not.** Import the writer's implementation.

### C34 · Repair draws for large_gpp repairs (band 3)

**Depends on:** C9. **Read first:** this card; BACKLOG B19; `build/late_swap.py` repair objective; `config/runtime.yaml`.

**Goal.** A minimal repair's pick is stable between runs and clock values.

**Create.** More repair draws or the exp_payout proxy inside the band; `tests/test_repair_determinism.py`.

**Tests.** The same repair picks the same player at two clock values and two runs.

**Exit checks.** `pytest -m c34 -q` green; the late-swap fast path stays within 30 s on the fixture.

**Do not.** Change the objective family.

### C35 · Fast field sampler agreement and SE resampling (band 3)

**Depends on:** C19. **Read first:** this card; BACKLOG B12, B15; `models/field_fast.py`; `build/objectives.py` standard error; `tests/bench_field.py`.

**Goal.** The fast sampler agrees with the MILP lineup more often, and the standard error includes field variance.

**Create.** 2-swap moves or several price starts; field resampling across scenario blocks; `tests/test_field_fast.py` extensions.

**Tests.** Agreement re-measured and recorded; the SE grows when the field is resampled and shrinks with draws.

**Exit checks.** `pytest -m c35 -q` green; `python tests/bench_field.py` within budget.

**Do not.** Change the behaviors (C19).

### C36 · Odds history grading (band 3)

**Depends on:** C11. **Read first:** this card; BACKLOG B67; `tools/capture.py`; `learn/grade_forecasts.py`; `learn/settle.py`.

**Goal.** Settle grades team totals against the market total and goalie wins against the implied probability, from the capture nearest lock.

**Create.** The implied figures on the frozen record; two calibration rows in settle; `tests/test_odds_grading.py`.

**Tests.** The 09-30 record carries the figures from the nearest capture; settle prints the rows; a slate with no capture prints NOT_AVAILABLE.

**Exit checks.** `pytest -m c36 -q` green.

**Do not.** Fetch odds in settle.

## Tracker row format (BUILD_STATUS.md)

`| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |` with Status in `TODO | IN_PROGRESS | DONE | BLOCKED | GATED`. `tools/next_chunk.py` edits only Status, Started, Finished, Commit, and Exit checks; Notes and the session log are written by the session. A handoff note in Notes has the form `HANDOFF: done=<...>; remaining=<...>; next=<command>`.

### C37 · Skills agree with CLAUDE.md, the status lists and the save-entered step (band 3)

**Depends on:** C43. **Read first:** this card; BACKLOG B76 and B69; `CLAUDE.md` "Slate rules" (lines 30 to 32); `.claude/skills/nhl-run/SKILL.md`, `nhl-late-swap/SKILL.md`, `nhl-refresh/SKILL.md`; `scripts/standings_checklist.py` `--save-entered`.

**Goal.** The skills' own instructions and `allowed-tools` match `CLAUDE.md`: every report step lists the statuses `CLAUDE.md` requires (SEARCH_STATUS included), and the save-entered, commit and pull-request step is allowed instead of forbidden by "never run another command". PR 5 already added the present-the-file text; this chunk does not redo it, and B69's observation waits for the next cloud run (deferred).

**Create.** Edit the three `SKILL.md` files; `tests/test_skills_delivery.py`.

**Tests.** Parse each skill's frontmatter and body: every command the body names is covered by `allowed-tools`; the status list from `CLAUDE.md` line 30 is present in each report step; no line forbids a command another step needs.

**Exit checks.** `pytest -m c37 -q`.

**Do not.** Change engine code or lock semantics. Create a cloud scheduled routine (C43, flag 19).

### C38 · Field size and family from the lobby row (band 1)

**Depends on:** C16. **Read first:** this card; BACKLOG B72; `models/contests.py` classify and family_prior (lines 130 to 153); `data/sources/dk_public.py` parse_lobby; `config/contest_families.yaml` field_size_prior; `reviews/2026-10-03_showdown_stl_col_run_record.md` lines 63 to 67.

**Goal.** When the contest page answers 403 and no template matches, the field size and family come from the lobby row (max entries, max per user), labeled FIELD_SIZE_SOURCE=LOBBY, and small single-entry contests leave large_gpp; an unseen contest keeps the prior and says PRIOR.

**Exit checks.** `pytest -m c38 -q`: a 27-entry Showdown contest classifies small_field with size 27 and the LOBBY label; the full suite stays green.

**Do not.** Touch the payout-table lookup (C16).

### C39 · History store kept current in-season and on every host (band 1)

**Depends on:** C17. **Read first:** this card; BACKLOG B70, B71 and B2; flag 18; `cli.py` cmd_history (`--since`); `data/history/nhl_reports.py`; `tools/cloud_bootstrap.sh`; `models/goalies.py:71` to `83`.

**Goal.** A run says how stale the history store is; an incremental `history --since` runs in Phase B after the first publish, never blocking it; a cloud session starts the backfill in the background when the store is absent.

**Exit checks.** `pytest -m c39 -q`: staleness line in RUN_NOTES on a fixture store; the back-to-back goalie flag fires on consecutive starts once the store holds the first game; the bootstrap script's dry run starts the backfill only when the store is absent. First commit is measurement only.

**Do not.** Wait on the network in Phase A.

### C40 · Daily Faceoff age gate keeps the last known lines (band 1)

**Depends on:** C7. **Read first:** this card; BACKLOG B73; `models/roles.py:224`; `config/roles.yaml` max_line_age_h; the cached Daily Faceoff pages under `tests/fixtures/http/`.

**Goal.** Measure offline whether the page's Last updated stamp moves only on edits; if it does, keep a playing team's last known lines and PP units with a stale label and age unless newer contrary news exists.

**Exit checks.** `pytest -m c40 -q`: CHI at 28.7 h keeps its lines labeled stale; a team with newer contrary news loses them; the measurement result is written to `docs/sources.md`.

**Do not.** Relax the gate for teams with news newer than the page.

### C41 · Selection robust to field ownership error (band 1)

**Depends on:** C19. **Read first:** this card; BACKLOG B74; plan section 6 (cold-start rule); `models/ownership.py`; `models/field.py`; the 09-30 forecast-versus-actual tables in `reviews/2026-10-03_standings_synthesis.md`.

**Goal.** Measure the ownership error band from graded forecasts, sample the field under low and high bands, print each candidate's tail spread, and let selection use the spread only if it moves a pick.

**Exit checks.** `pytest -m c41 -q`: the error-band function reproduces the 09-30 MAE on the fixture table; the report prints the spread; a pick that flips between base and low or high is named.

**Do not.** Retune the base ownership prior (C19).

### C42 · Improvement pass after the greedy fill (band 1, experiment)

**Depends on:** C18. **Read first:** this card; BACKLOG B75; `build/portfolio.py` `_greedy` (lines 360 to 430); the saved scenario caches of the 09-29 and 09-30 runs; plan section 7.

**Goal.** Write the preregistration first, then measure a bounded swap-improvement pass against the greedy portfolio on the saved caches; DONE on rejected when the gain is inside the standard error.

**Exit checks.** `pytest -m c42 -q`; `docs/experiments/` holds the preregistration committed before the code, and the result.

**Do not.** Change the default path unless the gain clears the standard error on both caches.

### C43 · Cloud parity, skills under bash and a pre-lock refresh for cloud-built runs (band 2)

**Depends on:** C10 (C37 follows it). **Read first:** this card; BACKLOG B77 and B34; flags 4 and 19; the three skills; `nhl.sh`; `tools/cloud_bootstrap.sh`; `build/scheduled.py`.

**Goal.** The run, late-swap and refresh skills run under bash with `nhl.sh`; the delivery message names the refresh command and the lock time; flag 4's text matches reality.

**Exit checks.** `pytest -m c43 -q`: a headless bash invocation of each skill's command on a fixture; the delivery message carries the refresh command and lock time.

**Do not.** Create a cloud scheduled routine (flag 19).

### C44 · Goalie and game caps in late swap and refresh (band 2)

**Depends on:** C27. **Read first:** this card; BACKLOG B78 and B17; `build/late_swap.py:599`; `build/swap_objective.py`; `build/refresh.py`; the caps API of `build/exposure.py`.

**Goal.** Late swap and refresh pass the exposure caps for the open cells and print GOALIE_CAP and GAME_CAP; a goalie-gate repair that would exceed the cap spreads across alternates.

**Exit checks.** `pytest -m c44 -q`: a 5-entry fixture whose confirmed starter changes holds at most the capped number of entries on him and prints the cap; the C9 timing check still passes.

**Do not.** Change the 40% budget (flag 2).

### C45 · Cross-mode fee-weighted risk (band 2)

**Depends on:** C27. **Read first:** this card; BACKLOG B79; plan section 7 (allocation paragraph); `build/exposure.py`; `config/risk.yaml`; `build/scenario_pass.py`.

**Goal.** Report each slate's combined goalie and game fee share across Classic and Showdown; then apply the cap to the combination.

**Exit checks.** `pytest -m c45 -q`: a fixture with a Classic and a Showdown run on one slate prints the combined share; the report-only commit lands first.

**Do not.** Merge the two modes' candidate pools.
