# DraftKings NHL lineup generator: plan and architecture

**Planning document · revision 3, 26 September 2026 (revision 1: 25 September 2026; revision 2: 26 September 2026) · Classic and Showdown · Claude Code**

Build a local deterministic engine that produces a checked legal portfolio first, then improves it with hockey event models, ownership estimates, joint simulations, and bounded adversarial review. The LLM researches changing roles and challenges decisions; it does not calculate scores, enforce legality, hand-build the portfolio, or decide whether its own proposals improved the objectives.

**Scope and authority.** This document is the architecture. Two companions are operational: [BUILD_CHUNKS.md](BUILD_CHUNKS.md) breaks the build into single-session chunks with dependencies, files, interfaces, and exit checks, and [BUILD_STATUS.md](BUILD_STATUS.md) tracks what is done; `chunks.yaml` is the machine-readable dependency graph and `CLAUDE.md` at the repo root is the operating contract every session reads first. The supplied [Classic rules](<C:/Users/benja/Downloads/NHL Classic.txt>) and [Showdown rules](<C:/Users/benja/Downloads/NHL Showdown Captain Mode.txt>) govern scoring and legality; chunk C0a copies them into `docs/rules/`. Revisions 1 and 2 are archived under `archive/` and are superseded. The design review and the two critiques that produced revisions 2 and 3, and the synthesis of decisions on them, are under `reviews/`. The master specification and platform comparison remain research inputs, not instructions to execute; NHL rules and this document override their NFL assumptions, Cowork workflow, and broad certification requirements.

**Revision 2 changes (26 September 2026), from the design review in [reviews/NHL_DFS_PLAN_REVIEW_2026-09-26.md](reviews/NHL_DFS_PLAN_REVIEW_2026-09-26.md):** (1) DraftKings' public lobby, contest-detail, and draftables endpoints become first-class inputs for payouts, field size, entry limits, lock times, and player status (sections 3, 7, 11). (2) Odds come from the NHL partner-odds endpoint, with Covers as the fallback (section 3). (3) LLM work is bounded by invocation count and wall clock; token figures are measured after the fact, never used as budgets (section 8). (4) Build order puts ownership, duplication, and payouts before hockey-feature depth and makes the segment simulator a challenger; the phase table is replaced by BUILD_CHUNKS.md (sections 5, 14). (5) The leverage tie-break and every checkable audit criterion move into code; QA defaults to one round (sections 7, 9). (6) AvgPointsPerGame enters the emergency prior and the ownership model, never the rate model (sections 6, 10). (7) The ownership model and the opponent field are one noisy-optimizer sampler (section 6). (8) MoneyPuck's listed downloads are the primary historical feature source behind a config switch (section 3). (9) Prospective capture of odds, lines, goalies, and draftables starts in the first adapter chunk (sections 3, 13). (10) Subagent isolation settings, the Daily Faceoff goalie parse path, real same-name fixtures, and the crosswalk seed are named acceptance items (sections 9, 11, 13). Open **[BEN: ...]** flags are collected in section 15.

**Revision 3 changes (26 September 2026), from the two critiques of revision 2 (decisions in [reviews/NHL_DFS_CRITIQUE_SYNTHESIS_2026-09-26.md](reviews/NHL_DFS_CRITIQUE_SYNTHESIS_2026-09-26.md)):** (1) The build is renumbered in objective order: locks and late swap directly after the baseline (C2c), ownership, field sampler, duplicate proxy, evidence gates, and provisional leverage selection on priors (C3) before any hockey-feature chunk; seventeen chunks (section 14). (2) The baseline is local-only through publish; network enhancement is a bounded second pass (section 10). (3) Publication replaces the public file atomically under a slate-level lock, and the referee has its own parser and rules and always binds output bytes to the input files (section 10). (4) Participation, DK eligibility, actual lock, and the engine's edit-stop buffer are four separate states with a recognized status map (sections 10, 11). (5) Simulation is per person; role rows map to the person's outcome; a shared team pace factor drives SOG and blocks (section 5). (6) The opponent field is sampled with replacement and keeps multiplicities; every fit, historical or live, passes one evidence-gate definition (sections 6, 12). (7) Mode-aware concentration limits, three separate evidence states (payout source, outcome calibration, field calibration), explicit cash and satellite objectives, cent-rounded tie payouts, and WTA allocation by first-place equity (section 7). (8) The NHL fallback for history is the stats REST per-game reports (time on ice by strength, realtime, summary) plus box scores, with labeled priors where MoneyPuck fields have no NHL equivalent (sections 3, 4). (9) QA may accept a large modeled strategic gain under the prior field, recorded as unvalidated; the adversary launches with `omitClaudeMd`, receives the packet inline, and reads nothing (section 9). (10) Financial settlement (fees, gross, net, drawdown by Entry ID) is a measured output of the learning loop (section 12).

**Decisions that shape the design:**

- Guarantee delivery against failures of optional research, network access, simulation, and LLM QA. The guarantee assumes valid inputs and at least one feasible roster under immutable legality and lock constraints; no system can manufacture a legal roster when none exists.
- Use ice time and event rates, not DraftKings `AvgPointsPerGame`, as the projection foundation. Permit explicitly labeled population priors for missing player history.
- Generate shared, discrete game outcomes. A sum of player ceilings, a multiplier on a median, or random independent projection bumps is not a lineup ceiling.
- Optimize tournament tail opportunities subject to a portfolio loss-risk budget. Raw variance and low ownership are not objectives by themselves.
- Treat all numerical caps, shrinkage strengths, scenario shares, and runtime targets below as **initial engineering policies to test**, not established NHL edges.
- Interpret “free” as no additional paid data, API keys, accounts, or hosted services. Claude Code itself still uses the user's existing access and usage allowance. Export files for manual DraftKings upload.
- Use DraftKings' own public, keyless lobby, contest-detail, and draftables endpoints as first-class inputs for payouts, field size, entry limits, lock times, and player status. They are undocumented and read-only; cache every response with the run and keep contest-family priors as the fallback.
- Build in objective order: legal baseline, then ownership, duplication, and payouts, then hockey-feature depth, then the field-model fit, then the segment simulator as a challenger. The aggregate game model is production until a preregistered test says otherwise.
- Treat DraftKings `AvgPointsPerGame` as a shrinkage component of the emergency prior and as an ownership feature, never as a rate-model input.
- Put every tie-break and every checkable audit criterion in code. The LLM adversary reviews only what code cannot check.
- Bound LLM work by invocation count and wall clock. Token counts are measured afterward with `/usage`; they are never a stop rule, because nothing inside a run can read them.
- Keep four states apart for every pool row: participation (playing or not), DK eligibility (rosterable or disabled), actual lock (the game has started), and the engine's edit-stop buffer. A status the engine does not recognize stays unknown.
- Simulate once per person. Role rows (CPT and FLEX, or a Classic row) map to that person's outcome; Captain scoring is applied once at lineup scoring.
- One evidence-gate definition governs every fit, whether the data is historical or live. Historical data does not bypass the floors; it counts toward them.

## 1. Repo review: reuse, avoid, and hockey transfer

**Review scope:** read-only inspection of the three local checkouts, their configured GitHub remotes, documentation, and selected implementation paths. Public web retrieval of all three GitHub URLs failed; local copies were available. Remote HEAD equivalence was not established. No suites were run, and this review does not establish live profitability or current end-to-end certification. The soccer checkout also contains an untracked `Claude outputs/` directory, which was not used or changed.

| Repository and inspected local HEAD | What is useful and why | What to avoid or replace | NHL transfer |
|---|---|---|---|
| [mlb-dfs](https://github.com/bleeski/mlb-dfs), `786e73d`, 2026-09-22 | The stage-0 `production/workflow.py` completes a baseline before enhancement, preserves it when enhancement fails, and uses independent export inspection. `state.py` supplies atomic publication/recovery. Its settlement routine explicitly handles ties. These are strong delivery patterns. | Current `CLAUDE.md` identifies `pipeline/execution_pipeline.run_slate` as the actual build entry and `production/` as an offline-verified alternative: do not confuse the two. Its normal/lognormal factor simulation is labeled uncalibrated and should not become the NHL outcome model. Avoid accumulated multi-host, approval, and certification machinery on the live critical path. | Baseline-first delivery, a durable incumbent, exact settlement, explicit search scope, and separate data/model/legality status. Replace hitter/pitcher correlations with lines, special teams, shared scoring events, and goalie opposition. |
| [nfl-dfs](https://github.com/bleeski/nfl-dfs), `04d7d83`, 2026-09-22 | Exact salary/template identity, byte-aware slot editing, locked-cell audits, bounded candidate generation, and one-lineup-per-Entry-ID allocation. Strong distinction between a candidate-bank optimum and a full-slate optimum. | `classic_portfolio.py` explicitly uses a prior-only central estimate and excludes field, ownership, payouts, and simulation: its allocator is infrastructure, not the requested tournament model. Its README still describes Classic review/Excel acceptance limitations. Do not import workbook acceptance, NFL weather, or complete-evidence requirements as conditions for generating NHL CSVs. | Share intake and roster-contract ideas across modes; preserve the exact uploaded entry template during late swap; report feasible/time-limited results honestly. Replace QB/receiver rules with actual shared-ice and PP relationships. |
| [soccer-dfs](https://github.com/bleeski/soccer-dfs), `5c03697`, 2026-09-04 | Minutes-first projections and source-to-current-DK crosswalks. `player_crosswalk.py` separates proposals, acceptance, and hash-bound validation; fuzzy names do not silently become identities. README distinguishes historical data from current starting-lineup evidence. | Requiring a current statistical row for every salary player and fully confirmed XI before certification would make NHL fragile, especially for call-ups and late goalies. Do not carry over soccer cross-scoring ambiguity or a workbook-centered operating flow. | Project EV/PP/SH ice time; attach uncertainty to projected starts; keep historical player identity separate from current slate/role IDs. Missing history gets an explicit hockey prior, not fabricated data or a failed build. |

**Concrete measurement defect worth preventing:** MLB's main standings summary uses a Captain-aware duplicate key, but `summarize_own_entries` and registry logic still group by sorted player names alone. The same people with different Captains therefore risk being treated as one lineup in those consumers. NHL needs one canonical, role-aware identity function used everywhere, backed by fixtures for different Captains and interchangeable FLEX order.

Source anchors inspected: [MLB workflow](<C:/Users/benja/Documents/Claude/mlb-dfs/mlb_engine/production/workflow.py:233>), [MLB simulation](<C:/Users/benja/Documents/Claude/mlb-dfs/mlb_engine/production/simulation.py:1>), [MLB duplicate summary](<C:/Users/benja/Documents/Claude/mlb-dfs/mlb_engine/field/field_miner.py:1007>), [MLB own-entry grouping](<C:/Users/benja/Documents/Claude/mlb-dfs/mlb_engine/field/field_miner.py:1521>), [NFL candidate portfolio](<C:/Users/benja/Documents/Claude/nfl-dfs/src/nfl_dfs/classic_portfolio.py:1>), [NFL late swap](<C:/Users/benja/Documents/Claude/nfl-dfs/src/nfl_dfs/late_swap.py:92>), and [soccer identity proposals](<C:/Users/benja/Documents/Claude/soccer-dfs/src/soccer_dfs/player_crosswalk.py:350>).

“Worked” here means useful architecture or a behavior visible in source, not proven investment performance. Reimplement the small contracts NHL needs, borrowing tested ideas and suitable fixtures; do not copy whole engines or their historical policies.

## 2. Commercial platforms: borrow mechanisms, skip product imitation

The comparison document is a useful capability map. Its claims of “true ROI,” “exact correlation,” and proprietary solver internals are not independent evidence. Public vendor pages verify advertised capabilities, not their implementation quality. These products also overlap more than the five-category comparison implies.

| Platform | Borrow | Skip / qualification | Priority |
|---|---|---|---|
| Stokastic | Contest-conditioned opponent fields; model popular combinations, salary left, Captain popularity, and duplicate-adjusted payouts. Compare a good lineup with what opponents actually enter. | No reverse engineering of private projections, paid feeds, or “ROI” labels before calibration. Do not assume every advertised sport has the same simulator. Its public strategy material describes field simulation; the recommendation is architectural. [Source](https://www.stokastic.com/articles/dfs-strategy) | After a useful outcome and ownership model. |
| SaberSim | Build candidates from shared simulated games so correlation and bonuses emerge together. Its NHL product explicitly advertises play-by-play simulation. | Do not start with a full puck-possession simulator, rink physics, or a cloned UI. A validated event/segment model can capture the DFS-relevant dependencies much sooner. More simulation detail is not automatically more predictive. [Source](https://www.sabersim.com/nhl/optimizer) | Core distribution design, implemented incrementally. |
| Establish The Run | Precise opportunity inputs, bounded news adjustments, salary-relative value, and ownership conditioned on contest type. | No paid analyst feed or assumption that an NFL projection workflow supplies NHL expertise. Its published ownership benchmarks are contest-specific; carry over that discipline. [Source](https://establishtherun.com/saturday-2-game-dfs-projections-2/) | First version: role accuracy beats elaborate optimization on bad inputs. |
| RotoGrinders / LineupHQ | Transparent stack groups, exposures, goalie opposition controls, and late-swap flexibility. Its NHL offering explicitly covers EV/PP lines and these construction controls. | Do not build a general rule scripting language or let overlapping arbitrary rules make the feasible set disappear. Product feature descriptions do not establish the exact proprietary optimization algorithm. [Source](https://rotogrinders.com/lineuphq) | Small, understandable preset policies from the start. |
| FantasyLabs | A feature registry and reproducible experiments comparing hypotheses against simple baselines. Its NHL tools advertise custom models and factor selection. | No manually chosen “30% form + 25% Vegas” score presented as a probability; no search through thousands of trends followed by selective reporting. Skip dashboards until the CLI workflow works. [Source](https://www.fantasylabs.com/daily-fantasy-sports/nhl/) | Lightweight experiment ledger early; learned models later. |

## 3. Data sources: verified access, freshness, fragility, and fallbacks

**Verification date: 2026-09-25.** “Yes” means a public no-key response/page or explicitly offered free download was observed. It does not mean an open license, a supported API, complete historical coverage, or permission for unrestricted scraping. Refresh times below are proposed engine schedules unless identified as a publisher schedule. Verify schema, attribution, and permitted access during implementation; do not bypass access restrictions.

Direct read-only requests returned NHL schedule JSON, 361 play-by-play events and box-score data for sample game `2025020001`, a season-summary response, and shift-chart data. This establishes current access and sample fields, not season-wide completeness. Cache these requests and test representative regular-season, playoff, overtime, and shootout games before relying on derived statistics.

| Metric / information | Source | Verified free? | Refresh / freshness policy | Fragility and fallback |
|---|---|---|---|---|
| Eligible players, teams, positions, salaries, role IDs | User's DK salary CSV | User-supplied; no new data subscription | Every slate; re-import a changed file as a new version | No actual salary CSV accompanied this request. No external source may rewrite these fields. |
| Entry IDs, contest names, fees, roster columns | User's DK entries CSV | User-supplied | Initial build and fresh export for late swap | The entries CSV carries Entry ID, Contest ID, contest name, and fee only. Payout curve, field size, and entry limits come from the DK contest-detail endpoint below, keyed by Contest ID; contest-family priors are the fallback and are labeled. |
| Contest list, draft groups, field sizes, entry limits, fees, prize pools | [DK lobby](https://www.draftkings.com/lobby/getcontests?sport=NHL) | **Yes**, keyless JSON verified 2026-09-26 | Once per slate; capture snapshots daily | Undocumented; terse fields (`id`, `n`, `a` fee, `m` field size, `mec` max entries per user, `po` prize pool, `dg` draft group, `gameType`). Cache; schema check; fallback is contest-family priors. |
| Payout table, `maximumEntries`, `maximumEntriesPerUser`, `entryFee`, `entries`, `draftGroupId`, `contestStartTime` | [DK contest detail](https://api.draftkings.com/contests/v1/contests/195958011?format=json), one call per distinct Contest ID in the entries CSV | **Yes**, keyless JSON verified 2026-09-26 (`payoutSummary` lists every position range with cash value) | Once per slate at build; again at settle for the final entry count | Undocumented. Store the raw response in the run directory. Fallback: priors by contest family, labeled `UNCALIBRATED`. |
| Player pool with `draftableId` (the salary CSV `ID`), `salary`, `rosterSlotId`, `status` (`OUT`, `IR` observed), `isDisabled`, `isSwappable`, `newsStatus`, `draftAlerts`, competition `startTime` | [DK draftables](https://api.draftkings.com/draftgroups/v1/draftgroups/153977/draftables?format=json) | **Yes**, keyless JSON verified 2026-09-26; Showdown groups list each person twice (slot 612 CPT at exactly 1.5x salary, 613 FLEX, distinct IDs) | At build; T-60 and T-15 per game; before any late swap | The salary CSV still governs eligibility and salary. Draftables is the scratch and lock signal keyed by exact DK ID. A disagreement between the CSV and draftables is a `CONFLICTED` status and a report, never a rewrite of the CSV. One reviewer's client received HTTP 403 from this endpoint on 2026-09-26; the adapter treats 403 like any outage (`MISSING`, fall back to the salary file and Daily Faceoff) and never blocks. `status` values map through a recognized table (`OUT`, `IR`, `O` → out; `Q`, `GTD`, `D` → questionable; anything else → unknown, preserved); `isSwappable` is editability, not participation. |
| Schedule, game IDs, start times, game state | [NHL schedule endpoint](https://api-web.nhle.com/v1/schedule/now) | **Yes**, JSON request succeeded | Daily; every 15 minutes during a slate; event-driven near starts | Undocumented public interface. Cached schedule plus uploaded DK game times; DK lock state governs editability. |
| Goals, assists, SOG, blocks, TOI, goalie results | [NHL box score](https://api-web.nhle.com/v1/gamecenter/2025020001/boxscore) and [season summaries](https://api.nhle.com/stats/rest/en/skater/summary?isAggregate=false&isGame=false&start=0&limit=1&cayenneExp=seasonId=20252026) | **Yes**, sampled both | Update completed games nightly; recheck corrections at +24 and +72 hours | Box-score totals and event feeds may temporarily disagree. Reconcile before training; keep last validated historical snapshot. |
| Per-game skater time on ice by strength (`evTimeOnIce`, `ppTimeOnIce`, `shTimeOnIce`, `shifts`), per-game blocks (`blockedShots`, `emptyNetGoals`), per-game summary (goals, assists, shots, PP points) | NHL stats REST per-game reports: [timeonice](https://api.nhle.com/stats/rest/en/skater/timeonice?isAggregate=false&isGame=true&start=0&limit=2&cayenneExp=seasonId=20252026), [realtime](https://api.nhle.com/stats/rest/en/skater/realtime?isAggregate=false&isGame=true&start=0&limit=1&cayenneExp=seasonId=20252026), `skater/summary` with `isGame=true` | **Yes**, keyless JSON verified 2026-09-26 (fields sampled) | Nightly, paginated by date range, not per game | This is the NHL-only fallback for strength-split minutes, blocks, and scoring events when MoneyPuck is off. Assist order (A1/A2) and shot quality are not in these reports; the fallback uses labeled league priors with missingness indicators for them. |
| Individual attempts, shot coordinates/type, goals/assists, strength state, penalties | [NHL play-by-play](https://api-web.nhle.com/v1/gamecenter/2025020001/play-by-play) | **Yes**, sampled JSON | Nightly; incremental fetch for game-state updates | Schema changes, rink bias, event corrections, incomplete preseason events. Fall back to box-score SOG/blocks and population shot-quality priors. |
| EV/PP/SH TOI, shared ice, actual line combinations | [NHL shift charts](https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId=2025020001&limit=1), joined to PBP | **Yes**, sampled JSON | Nightly, with coverage/reconciliation checks | Overlapping/missing shifts and strength transitions require careful intervals. Fall back to recent TOI splits and projected lines; observed historical co-ice is not tonight's confirmation. |
| Stable statistical player identity | [NHL player data](https://api-web.nhle.com/v1/player/8478402/landing), NHL rosters/PBP identities | **Yes**, player response sampled | Daily and after trades/call-ups | NHL IDs are statistical keys; DK IDs are slate/role keys. Unknown mappings use DK-only priors until resolved. |
| ixG, rebound probabilities, shot quality, goalie GSAx | [MoneyPuck download page](https://moneypuck.com/data.htm) | **Yes, with published use limits** | Listed shot files update nightly; engine refresh daily | Terms verified 2026-09-26: "free to use for non-commercial purposes", credit MoneyPuck.com wherever its data is an input, and "non-approved scraping ... will be blocked." Use only the listed downloads, never page scraping. Revision 2 makes MoneyPuck the primary historical feature source (season and game-level skater, goalie, line, and team files; per-shot files with xG) behind `sources.moneypuck.enabled`, with the NHL-derived path as a complete fallback. **[BEN: confirm personal DFS use is acceptable under these terms; if not, set the flag false.]** |
| Historical skater/goalie/team/line summaries | [MoneyPuck downloads](https://moneypuck.com/data.htm) | **Yes, same qualification** | Daily when updated | Definitions differ from other sites. Shot files omit blocked shots; obtain blocks elsewhere. Maintain provider/version-specific features. |
| iCF/60, iSF/60, A1/60, HD chances, team CF/CA by strength | [Natural Stat Trick](https://www.naturalstattrick.com/) | **Unverified today** | If usable, daily historical refresh | Research access was blocked by robots. Do not promise a dependable free scraper. Reconstruct attempts/primary assists from NHL events; use a named local danger definition instead of relabeling it NST HD. |
| Projected EV lines, defense pairs, PP/PK units, injury tags | [Daily Faceoff team page](https://www.dailyfaceoff.com/teams/toronto-maple-leafs/line-combinations) | **Yes**, timestamp and unit tables visible | Morning; after practice; T-90/T-30/T-10 for each game, respecting site limits | HTML changes and stale team pages. Sample displayed a September 24 update. Use event/source timestamps, not page-fetch time; fallback to recent actual co-ice and official reports. |
| Expected/confirmed starting goalies | [Daily Faceoff goalies](https://www.dailyfaceoff.com/starting-goalies) | **Page exists; the goalie table renders client-side.** Server HTML held only page chrome on 2026-09-26. The adversarial audit reports the hydration payload in `<script id="__NEXT_DATA__">`; the capture in chunk C1 stores raw HTML including scripts so chunk C7 can confirm and parse it, falling back to per-team pages and then DK status. | Morning, T-90/T-30/T-10; one final check for affected goalies | Publication lag; status wording varies. Prefer an explicit named reporter/team confirmation over a ranking or depth-chart order. Backstops: per-team line pages (server-rendered, timestamped), DK draftables `status`, and start probabilities from rotation and rest. |
| Official roster/news corrections and coach comments | NHL team sites; [example team lineup report](https://www.nhl.com/islanders/news/topic/training-camp/preseason-game-preview-islanders-at-devils-sept-20-2026) | **Yes**, sample page accessible | News-triggered for slate teams | Inconsistent publication and embedded images/social posts. Store exact claim, game, time, and URL; user-pasted text is a fallback. |
| Left Wing Lock starting goalies, combinations, site tools | [Access page](https://leftwinglock.com/accessCheck.php) | **No** | Excluded | Current page requires premium access. Do not rely on old recommendations describing these tools as free. |
| Moneylines (2-way and 3-way), puck lines, totals | [NHL partner odds](https://api-web.nhle.com/v1/partner-game/US/now) (DraftKings is the listed partner; `lastUpdatedUTC` per response) | **Yes**, keyless JSON verified 2026-09-26 | T-180/T-60/T-15, plus goalie changes; retain `lastUpdatedUTC` and book | Same undocumented host as the schedule. A response whose `lastUpdatedUTC` predates a goalie confirmation is stale. Fallback 1: Covers board. Fallback 2: team-strength model. Odds never block generation. |
| Backup displayed odds | [Covers odds](https://www.covers.com/sport/hockey/nhl/odds) | **Yes**, server-rendered board with moneylines and totals verified 2026-09-26 | Only when partner odds are missing or stale; same-game timestamp required | HTML scraping; layout changes break it silently, so the parser fails closed on a schema check and reports `STALE`. ESPN's scoreboard API carried no odds in preseason and is not used. |
| Free team totals, SOG/goal/save props | Public sportsbook/aggregator pages | **Unverified for dependable complete coverage** | Optional, same-game timestamp required | No dependable no-key complete prop feed verified. Infer team scoring rates from totals and moneylines; omit props when unavailable. No Odds API account requirement. |
| Entries, exits, primary shot assists, slot/cross-seam passes | [All Three Zones](https://www.allthreezones.com/) and other manual tracking projects | **No verified complete free feed** | Excluded from production v1 | Public samples exist; full project promotes patron access. Use proxies below, not invented tracking values or a scraping project. |
| Zone time / shot-location summaries | [NHL EDGE](https://www.nhl.com/news/nhl-edge-advanced-stats-section-brings-fans-closer-to-game) | **Yes**, public summaries | Optional daily research | Bulk historical extraction unverified; omit from the critical path. |
| Actual ownership, lineups, scores, final ranks | User-uploaded DK standings | User-supplied; schema not yet inspected | After final results, with revisions retained | Ownership block coverage and CPT/FLEX semantics need validation. Payouts may be missing; parse them separately when supplied. |

**Preferred dependency chain:** DK salary and entries files → DK public endpoints (contest detail, draftables) for payouts, field size, status, and lock times → NHL schedule, rosters, box scores, and partner odds → MoneyPuck listed downloads for history (config-switched) → Daily Faceoff for projected roles → LLM research only for conflicts. NST and tracking projects are not required. The NHL per-game reports are the fallback for history when MoneyPuck is off. Prospective capture of odds, lines, goalies, lobby, and draftables starts in chunk C1, before any model exists, because none of it can be backfilled; historical betting lines and pre-lock news were not verified as complete free archives.

Each observation carries `source`, `source_player_id`, `game_id`, `observed_at`, `published_at`, `fetched_at`, `valid_from`, `raw_hash`, `definition_version`, and status `CURRENT / STALE / CONFLICTED / MISSING`. A refreshed scrape cannot refresh the age of an old claim. Freeze an as-of view per build; never select a date directory called “latest” without checking its scope.

## 4. Feature engineering: what survives, changes, or gets cut

This is an interpretable **opportunity → events → DK points** model. Calling it causal mediation does not establish causality: observational line changes, injuries, and matchups are confounded. Use role interventions operationally, then test whether their predicted changes calibrate. Every feature definition records its numerator, denominator, strength state, window, source, missingness, and availability time.

| Hypothesis | Decision and reasoning | DK connection / implementation |
|---|---|---|
| TOI, PP TOI share, EV line, PP unit for every skater | **Core.** Model probability of playing and distributions of EV/PP/SH time separately. Role matters more than a rigid C/W label. | Scales every scored event and shared-event opportunity. PP share denominator is team PP clock time, not summed skater PP minutes. |
| Winger iSF/60 and iCF/60 | **Keep for all skaters.** Attempts lead to SOG but contain blocked/missed attempts. Avoid counting both as independent full-strength boosts. | Model attempts and the on-target fraction jointly; +1.5/SOG and nonlinear +3 at five. |
| ixG/60 and high-danger chances | **Keep with shrinkage.** Use continuous shot quality rather than overlapping independent boosts from ixG, HD chances, and shooting percentage. | +8.5/goals and joint assist/point bonuses. Underfinishing is uncertainty about conversion, not a debt the player is due to repay. |
| Rush shots, rebound creation | **Keep rebound features where defined; defer true rush tracking.** Fast PBP transitions are imperfect rush proxies, not controlled-entry observations. | Goals, assists, and clustered chances. Name proxy fields honestly and test incremental value after TOI/shot volume. |
| PP1 half-wall/bumper finishing advantage | **Keep PP1; qualify sub-role assertion.** PP1 membership alone does not identify a specific tactical role or guarantee higher shooting percentage. | Use documented sub-roles only if current; otherwise learn strength-specific shot/assist shares. |
| A1/60 | **Keep, shrunk and joint with linemates.** Primary and secondary assists both score five DK points, but need different forecasting treatment. | PBP assist order provides A1/A2 where complete. Include both when scoring. |
| Primary shot assists | **Defer true metric.** A1 is not a shot assist. | Proxy: on-ice teammate shot/xG rate, shared-ice time, PP time, and historical primary-assist share. Do not label this passing tracking. |
| Controlled entries and dangerous passing | **Cut from required inputs.** No complete free current feed verified. | Proxy: on-ice shot generation, transition-like event sequences, PP role, and linemate opportunity. Drop proxy if it adds no held-out value. |
| Linemate quality | **Core.** Model the center and wingers through shared ice and scorer/assist allocations, including finishing-skill uncertainty. | Generates assist-goal correlation; recognizes that strong passers need teammates who get and convert chances. |
| Defense: block anchors | **Keep as a continuous archetype, not a filter.** EV/SH minutes × opponent attempts × block involvement. A block-prone defense can suppress goalie saves. | +1.3/block, +3 at three. Twenty minutes is informative, not a universal cutoff. PK points are rare upside, not the reason to select a defensive specialist. |
| Defense: PP quarterback | **Keep continuous PP share.** Test the proposed ~60% threshold as a spline/knot, not a hard gate. Two-D PP units need explicit treatment. | Assists, SOG, goals, and bonuses. A point shot can score via a tip/rebound; do not assign two shooters credit for the same shot. |
| Goalies: volume versus danger | **Keep; correct the direction.** For the opponent's offense use **CF/SF/xGF/HDCF**, not its CA/HDCA, which describe what that opponent allows. Combine with the goalie's own team's defensive allowances. | Forecast shots faced and conversion risk together: +0.7/save versus −3.5/GA. Blocks/misses do not generate saves. |
| Goalie GSAx/60 | **Keep with strong multi-season shrinkage.** Per-shot residual save skill, adjusted for quality, is preferable to a noisy short-run per-60 ranking. | Influences GA, shutout probability, pull risk, and indirectly the game result. Avoid counting market-implied goalie quality twice. |
| Moneyline, totals, free props | **Keep as optional calibrated inputs.** Markets inform game environment; they do not directly give each player's distribution. | Goals/assists, saves, win/OTL and shutout probabilities. Fit coherent game rates; do not independently bolt a win probability onto contradictory simulated scores. |
| Last-10 SOG ≥3 or blocks ≥1.5 | **Keep as soft signals only.** Normalize for minutes, strength state, opponent, and position/role. | Prefer probabilities of reaching the exact five-shot/three-block bonus to a coarse threshold. A low-volume cheap PP replacement must remain eligible. |
| Goals below ixG over 15 days | **Replace the fixed window.** Fifteen days often means too few shots. Use minutes/shots and exponential decay with season/multi-season priors. | Compare 5/10/20-game and longer-window challengers in chronological validation. Do not use realized future finishing to select windows. |
| Opponent bottom-quartile CA/HDCA at 5v5 | **Keep as a continuous, opponent-adjusted context feature.** Rank cutoffs lose information; schedule and score state distort raw rates. | Skater opportunity and quality. Model PP versus PK separately rather than applying 5v5 weakness everywhere. |
| Faceoff percentage | **Drop initially.** No direct DK points, and much of its useful role information is already in TOI/deployment. | Add only if it improves held-out event forecasts beyond those features. |

**Additions with higher priority than exotic tracking:** probability of dressing; rookie/call-up and trade regime; EV/PP/SH role uncertainty; team penalties drawn/taken and PP opportunities; score-state effects on pace and ice time; home/away and rest/back-to-back effects with conservative shrinkage; empty-net and goalie-pull states; overtime/shootout rules by game type; opponent lineup strength; team finishing/save uncertainty; historical scorer/assist partner shares; player/line salary efficiency relative to an attainable replacement; and ownership uncertainty.

Maintain season/playoff/preseason distinctions. Do not train a full-game goalie-minute assumption on exhibitions with planned goalie splits. Don't infer a trade, injury, or new line from an external page that conflicts with the authoritative salary team; resolve the statistical join while leaving DK eligibility unchanged.

## 5. Projection model: player distributions and shared game outcomes

### Definitions and scoring contract

Display **floor = P10**, **median = P50**, **ceiling = P90**, and **extreme ceiling = P99**, plus mean, probability of playing/starting, and uncertainty flags. These are outcome quantiles, not confidence bounds on the estimate. An unconditional goalie floor can be zero or negative. Show starter-conditional numbers alongside the unconditional mixture to make the source of risk visible.

The two attached rule files specify the same base scoring:

| Event | DK points | Exact application |
|---|---:|---|
| Goal; assist; shot on goal; blocked shot | 8.5; 5; 1.5; 1.3 | Preserve integer event counts. Goals normally also count as SOG in official statistics; shootout events are separate. |
| Shorthanded point; shootout goal | 2; 1.5 | Shorthanded bonus per qualifying goal/assist; do not turn a shootout tally into a normal goal. |
| Hat trick; 5+ SOG; 3+ blocks; 3+ points | 3 each | Each threshold bonus once per qualifying player-game; bonuses can accumulate together. |
| Goalie win; save; goal against | 6; 0.7; −3.5 | Award official goalie decisions; do not charge empty-net goals to an absent goalie. |
| Goalie shutout; overtime loss; 35+ saves | 4; 2; 3 | Shutout requires the sole goalie of record completing the full game with no regulation/OT GA. Shootout goals do not break it. |
| Captain | ×1.5 | Multiply the entire base fantasy score, including bonuses and penalties, exactly once. Use the salary file's Captain salary. |

Goalies receive points for offensive statistics they actually accrue, including goals and assists. No plus/minus, hits, faceoff wins, or penalty-minute fantasy scoring is added.

### Opportunity and rate estimation

1. **Participation:** estimate a dressing probability for skaters and mutually exclusive starter probabilities within each team. Known scratches are excluded from new selections when a feasible replacement exists. Unresolved news is uncertainty, not a made-up confirmation.
2. **Minutes:** fit role-conditioned EV/PP/SH TOI models, starting with shrunk weighted historical means and bounded distributions. Reallocate an absent player's opportunities across credible replacements; do not grant full vacated minutes to several players. Team manpower budgets must reconcile; summed skater minutes can exceed game-clock time by the number of skaters on ice.
3. **Events per minute:** begin with hierarchical count/binomial models by strength state. Regularize player rates toward position/role/team priors; test negative-binomial dispersion for attempts/blocks. Start with roughly 300 EV minutes and 100 special-teams minutes of prior weight as candidate settings, then choose them offline. Goalie conversion skill needs substantially more history; test priors equivalent to 1,000–2,000 shots faced.
4. **Shot quality:** use a consistent provider's xG or a compact calibrated local shot model using distance, angle, shot type, strength, rebound/previous-event information, and rink effects. Local uncalibrated geometry starts as a “danger proxy,” not xG. Missing coordinates receive a quality prior and a missing-data indicator, not zero danger.
5. **Context:** incorporate team/opponent pace, defense, penalties, goalie scenarios, and markets once. Fit interactions conservatively. A usage change adjusts minutes/event shares, not an arbitrary +20% fantasy multiplier.

Centers and wingers share a forward model with individual shot/assist tendencies; defensemen add block and PP-share structure; goalies have their own shots-faced, goal-conversion, decision, and playing-time process. Player archetypes can overlap. A scoring center should not be forced into a playmaker prior.

### Joint simulation

Use a reproducible seeded simulator: given frozen inputs and a seed, outputs repeat. Randomness represents outcomes, not LLM judgment.

Simulate once per **person**, never per role row. A Showdown person's CPT and FLEX rows, and any Classic row, map to that person's realized stat line; the Captain multiplier is applied once at lineup scoring. A skater drawn as not dressing has zero minutes and zero events in that scenario; a goalie drawn as not starting has no decision, saves, or goals against unless the relief process puts him in net. Within a game, a shared pace factor per team per scenario (Gamma-distributed, calibrated to the dispersion of team shot totals) scales every skater's SOG and the opponents' block opportunities, so high-volume games cluster the way real ones do.

- Draw a lineup/goalie-role scenario and uncertain rate parameters, then shared game pace, penalties, and scoring environment. The production model through the field-model chunks is the **aggregate game model**: team goal counts by strength state drawn from market-fit intensities and a PP-opportunity process; each goal allocated to an on-ice unit, a scorer, and zero to two assisters by shared-ice and assist shares; SOG and blocks drawn per player scaled by projected TOI; goalie saves derived from opposing SOG minus GA; empty-net goals from a simple trailing-team process; regulation ties resolved to OT or shootout. The segment-level process (period segments, penalties, pulls) is a preregistered challenger (chunk C13), promoted only if it improves held-out line-pair tails or bonus calibration.
- Generate attacking attempts and classify blocks, misses, and on-target outcomes. Allocate defending blocks and attacking shots to players conditional on active units and ice time.
- Generate goals as outcomes of on-target opportunities; choose the credited scorer and zero, one, or two distinct eligible assistants using shared-ice and assist-share models. A scorer cannot assist their own goal. Model deflections/rebounds explicitly enough to avoid duplicate shot credit.
- Derive goalie saves from on-target shots while that goalie is in net minus goals allowed. Draw pulls/replacements when appropriate. Opposing scorer success and goalie failure must be the same event, not separately sampled variables.
- Simulate game state, empty-net opportunities, overtime, and shootouts according to the game type. Separate the shootout's game-deciding bookkeeping from ordinary individual goals/GA. Reconcile goalie-of-record wins/OT losses against official examples, including shootout losses, before accepting the scorer.
- Apply the complete DK scoring function to each realized player stat line. Thresholds create the desired lumpy, right-skewed distributions naturally.

This captures positive goal/assist and PP correlations, shared volume, competition for the same goal/ice time, and goalie opposition. A scalar pairwise “stack bonus” must not be added again to simulated points.

### Vegas integration

Convert paired prices for the same book/market/time to implied probabilities and remove vig. For American odds: `100/(a+100)` for positive `a`, and `abs(a)/(abs(a)+100)` for negative `a`; normalize the two outcome probabilities. Aggregate matched-book estimates robustly, rather than combining the best price for each side from unrelated books.

Fit home/away scoring intensities so the simulated total-goals distribution and full-game win probability approximately match the market, with regularization toward the hockey model. **Total × win probability is not an implied team total.** Totals are betting thresholds, not necessarily expected goals. Respect regulation-only versus OT-inclusive markets and the market's shootout settlement convention. Fit direct team-total markets if available; otherwise label the result inferred. Player props are soft checks on shot/save/goal distributions and must not overwrite sparse-player uncertainty or become a mandatory dependency.

If markets predate a goalie change, reprice through the model and mark the market stale until refreshed. If unavailable, use the team-strength model. Log discrepancies and cap influence; neither stale odds nor failed prop research can prevent generation.

### Simulation scale and calibration

Starting budgets: 5,000–10,000 design scenarios for candidate discovery; 20,000 separate selection scenarios; 20,000 independent referee scenarios for finalists, increased only when runtime permits and uncertainty matters. Cache player outcomes and score candidate lineups in chunks. Avoid storing a full candidates × simulations matrix unnecessarily.

Twenty thousand scenarios give only about 200 observations beyond a nominal P99 and very few events for a 1-in-100,000 win chance. Report Monte Carlo uncertainty; never imply precise massive-field win rates from this budget. Keep the final referee draws out of candidate generation. More simulation reduces numerical error, not model misspecification. Validate marginal distributions, bonuses, line-pair tails, goalie/skater joint behavior, and team totals against held-out games.

## 6. Ownership model and standings calibration

**Start with a transparent prior; learn a model when enough distinct slates exist.** Inputs: salary and position-relative salary rank, mean/ceiling/value, expected TOI, EV/PP role, line partners, team total, goalie start/win expectations, recent visible fantasy production, DraftKings `AvgPointsPerGame` (the number most of the field reads), star/reputation proxy from prior ownership, news timing, slate size, start time, and contest family/fee/entry limit. Past fantasy outcomes can help predict popularity even when they are excluded as a projection foundation. No paid ownership source is required.

Ownership is roster inclusion probability, not probability of being the best play. Revision 2 makes the ownership model and the opponent field **one object**: a noisy-optimizer field sampler that builds legal lineups from projection plus noise under a mixture of behaviors (projection optimizer, line/PP stack builder, stars-and-value, casual, contrarian), each with its own noise scale, stacking rule, Captain heuristic, and salary-left preference. Opponent entries are sampled **with replacement**: the same lineup can and should appear many times, and the sampler records multiplicities. Distinctness and minimum-difference constraints belong to one entrant's portfolio, never to the field. Marginal ownership, Captain shares, duplicate counts (weighted by multiplicity), stack frequencies, and salary-left distributions are all read off the sampled field, so they are coherent with position, salary, and team constraints by construction. Calibration adjusts per-player utility offsets and the mixture weights until sampled marginals match observed %Drafted and sampled duplicate counts match observed duplicates. A regression producing unrelated percentages that do not fit legal rosters is insufficient.

For Classic, total player inclusion mass should be approximately **900%**, including **100% goalie mass** and **800% skater mass**; UTIL means C/W/D marginal budgets are not simply 200/300/200%. For Showdown, track **100% CPT**, **500% FLEX**, and **600% combined player inclusion**, with CPT and FLEX mutually exclusive for the same person in a lineup. Do not double fantasy points when extracting role ownership. Roundoff and incomplete exports receive explicit tolerances/statuses.

The opponent field is a mixture of believable behavior: projection optimizers, line/PP stack builders, stars-and-value constructions, casual selections, and contrarian builders. The mixture weights are learned by contest family when data permits (chunk C12, at the higher evidence tier for mixture weights); otherwise use broad priors and sensitivity ranges. Preserve same-line popularity, combinations, salary left, goalie choices, and exact duplicates. Multiplying nine player ownership percentages does not estimate a Classic lineup's probability.

**Cold start:** the sampler runs with hand-set utilities (salary rank, value, APPG, team implied total, PP1 and line status, goalie start and win expectation, news timing) scaled so Classic mass is 900% and Showdown mass is 600%; forecast ownership as ranges, use only moderate leverage preference, and generate alternatives under low/base/high ownership scenarios. If prior-season DK standings exports exist, chunk C3 fits the utilities to them before slate one, subject to the same section 12 evidence floors applied to that historical data (per-mode counts, chronological holdout); historical data counts toward the floors, it does not bypass them. **[BEN: do prior-season NHL standings exports exist?]** Never rank tiny projected differences as a known edge. Fragile chalk means high popularity paired with uncertain opportunity, fragile conversion assumptions, or highly duplicated construction, not simply a popular excellent player.

After each slate:

- Join standings to the exact salary file and frozen pre-lock ownership forecast. Deduplicate contest exports and revisions by Contest ID/content; separate player ownership summaries from entry rows.
- Verify whether the export contains every eligible player, zero-owned players, role-separated percentages, and complete lineups. Missing is not zero. For Showdown, reconstruct CPT/FLEX rates from complete lineup rows when the summary is combined or ambiguous. Do not assume the user's description determines the raw schema.
- Grade whole-pool and active-pool MAE in percentage points, weighted error on popular players, calibration by ownership band, top-chalk recall, zero-observed mass, position/team/line totals, Captain shares, and duplicate-count error. Rank correlation alone is not calibration.
- Fit with chronological slate/day splits. Hundreds of contests from one slate do not supply hundreds of independent observations. Actual future ownership is never a pre-lock input in a historical test.

## 7. Lineup construction and the two objectives

### Define the objectives correctly

Hockey scores are discrete: “99th-percentile outcome density” should become **probability mass in contest-relevant upper-tail outcomes**, supported by P99/exceedance diagnostics. Maximizing variance alone can select bad plays with large downside; adding player P99s overstates attainable lineup outcomes. Each contest family has an explicit objective function: large GPP, expected payout from top-1% finishes; WTA, tie-adjusted first-place equity; cash, H2H, and double-up, probability of clearing the payout line; satellite, probability of a seat with the ticket's face value tracked separately from cash. Allocation to WTA entries favors first-place equity, not variance.

For candidate lineup `l`, contest `c`, and shared slate scenario `s`, compute score `S(l,s)` against an independently generated opponent field. Define the upper-tail threshold from the **same scenario's** opponent scores. For large GPPs, track top-1% finish probability, expected top-1% entry count, probability any portfolio entry reaches that region, and expected payout under the actual curve. For WTA, use tie-adjusted first-place prize share. Tied prize money is pooled across the tied positions and divided equally, rounded down to the cent, as DraftKings settles it (the exact Terms of Use wording is recorded in chunk C8); the user's own copies count as copies. The field is represented as at most 5,000 distinct sampled lineups with multiplicity weights summing to the contest's field size; ranks are weighted; scoring is chunked by scenario under a configured memory cap, and field construction and payout evaluation are benchmarked, not only player simulation. Top-1% is not a substitute for first place in enormous fields.

Portfolio scenario return is `R(s) = sum(entry payouts in s) − total entry fees`. Report separately: probability of **zero gross payout**, probability of **net loss**, probability of **losing at least 80% of slate fees**, and expected shortfall. These answer different questions. With highly skewed GPP portfolios, worst-5% expected shortfall can equal the full buy-in almost everywhere; do not pretend that flat metric distinguishes portfolios. Add the loss-threshold probabilities and recovery distribution.

Choose a small frontier of tournament-tail utility versus portfolio loss risk. Select the strongest tail portfolio that fits the configured risk budget. Placeholder budget in `config/risk.yaml` until Ben sets one: probability of losing at least 80% of slate fees at most 0.60; at most 40% of fees dependent on any one goalie; and, only when the slate has more than one game, at most 40% of fees dependent on one game. A single-game Showdown slate cannot diversify its game, so its concentration is measured by Captain share and by shared failure scenario instead. The frontier report excludes dominated points; monotonicity in the knob is not assumed. Every payout-based figure carries three evidence states reported separately: `PAYOUT_SOURCE` (`EXACT` from the contest endpoint or `PRIOR`), `OUTCOME_CALIBRATION` (`UNVALIDATED`, `SHADOW`, `VALIDATED`), and `FIELD_CALIBRATION` (`PRIOR`, `FITTED`). Exact payouts do not make modeled probabilities calibrated. **[BEN: set the risk budget.]** When no portfolio meets a requested budget, show the least-risk feasible alternative and the achievable tradeoff; still produce the legal file. When exact field/payout metadata is unavailable, use declared contest-family priors and label tail/coverage scores **uncalibrated scenario proxies**. Do not report payout-based ruin, ROI, or EV as measured facts.

**Deterministic tie-break.** When two candidates for the same entry, or two lineups competing for a portfolio slot, are within the declared materiality band on the tail metric (the band is never smaller than that metric's Monte Carlo standard error), code prefers the lower projected ownership and the lower duplicate-risk proxy. Ownership alone may not move a choice past one band: a candidate whose tail metric is more than one band below the alternative is not preferred on ownership. Before the field model is fitted, the duplicate-risk proxy is the sum of log ownership across the roster plus a salary-left bucket and, in Showdown, the Captain's ownership; after the fit it is the sampled duplicate count. This is where "two players project nearly the same but one is much lower owned" is resolved. It is not an LLM decision.

The entries CSV fixes committed capital. Allocation means choosing which lineup goes into which purchased entry, with fee-weighted risk across all modes on the slate. It cannot retroactively move a $20 entry into a $1 contest. Contest purchasing and future bankroll allocation remain user decisions. A meaningful multi-slate ruin calculation needs bankroll and a spending policy; slate diversification alone cannot guarantee capital preservation.

### Hard roster contracts

| Contract | Classic | Showdown |
|---|---|---|
| Roster | 2 C, 3 W, 2 D, 1 skater UTIL, 1 G | 1 CPT, 5 FLEX; any supplied eligible position including G |
| Salary | At most $50,000 | At most $50,000 using each selected role row's salary |
| Teams | Skaters from at least **three** distinct teams; the goalie does not satisfy this test | At least two distinct teams among the six (for a two-team pool this is exactly both teams; the constraint spans locked and free slots during late swap) |
| Identity | Nine different people; exact DK slot eligibility | Six different people; cannot use the same person as CPT and FLEX |
| Captain | Not applicable | One 1.5× scorer; role-specific DK ID; goalie CPT allowed |
| Other team/goalie restrictions | Add no invented DK maximum-per-team rule | Add no invented one-goalie limit; two opposing goalies are legal if the supplied pool permits them |

### Classic strategy

Generate several candidate families instead of imposing one universal stack:

- EV line stacks of two or three forwards, especially with shared PP time; PP groups of two to four skaters; PP quarterback plus connected forwards.
- A secondary line/PP pair from a different team or game. Three-team skater coverage is always checked independently of stack naming. A literal two-team eight-skater roster fails the supplied Classic rule.
- Smaller correlations plus strong standalone volume/blocks for cash and smaller fields; selective lower-popularity lines whose opportunity supports a tournament ceiling.
- No automatic opposing “bring-back”: NHL has no NFL-style requirement that the opponent sustain a quarterback's volume. Let the shared game model evaluate it.
- Default to no opposing skater against one's goalie in normal Classic builds. Treat this as a soft strategic preference, not legality: high-volume shot-only outcomes can support exceptions, especially in limited pools. Do not force same-team goalie stacking either.

Reject a cheap punt only for inadequate role/value relative to alternatives, not for failing an arbitrary shot threshold. Value is salary-adjusted gain against feasible replacements and the lineup's tail performance, not points-per-dollar alone, which can overrate low-upside minimum salaries.

### Showdown strategy and coherent theses

Evaluate every eligible person at CPT using the actual CPT salary/ID. Reuse the same underlying outcome draw for CPT and FLEX. Candidate generation must cover both teams, plausible score regimes, and role archetypes:

| Thesis | Typical coherent construction | What the evaluator must challenge |
|---|---|---|
| Favorite offense succeeds | Scoring/creating CPT, connected EV/PP teammates, opposing volume or value | Excessive ownership, correlated goal allocation, and whether the opponent piece has a route to points. |
| Underdog upsets / chalk goalie fails | Underdog scorer or creator CPT with connected pieces | Whether the lower ownership compensates for the team's lower scoring expectation. |
| Goalie wins a volume game | Goalie CPT with a plausible win/save path and selected skaters | Several opposing goal-dependent skaters conflict with a dominant-goalie thesis; opposing SOG specialists may be tolerable. |
| Low-event, saves/peripherals dominate | One or two goalies, shot/block anchors, affordable skaters | Both goalies cannot win; benefits must come from saves/low GA/OTL. Shared shutout/OT scenarios need correct scoring. |
| Special teams or concentrated line wins | PP-linked CPT and teammates | PP opportunities, shared time, and whether too many points are assigned to mutually competing roles. |

A thesis is derived from the scenario subset in which a lineup excels and recorded with supporting stats. The LLM may explain it; a persuasive story cannot rescue poor simulated support. Permit 5–1, 4–2, and 3–3 team splits; none is intrinsically optimal. Search salary-left choices rather than requiring a spend floor. Count exact duplicates with `(Captain person, sorted FLEX people)`; count total-person overlap separately.

### Portfolio policies and secondary builds

Starting defaults for **20 or more tournament entries**: Classic player exposure 45%, goalie 35%, primary line core 30%; Showdown person exposure 60% across both roles and Captain exposure 25%. Start Classic overlap at at most seven common people between lineups; in Showdown prefer at least one person difference as well as avoiding exact Captain-aware duplicates. These are adjustable search preferences, not DK rules or proven optimal caps. Cash need not use these tournament diversity rules.

Before applying a cap, solve a feasibility check and calculate its unavoidable minimum. Examples: fewer than three usable goalies can make a 35% cap impossible; a six-player Showdown roster imposes a minimum average inclusion burden. For count caps use `floor(cap × entry_count)`, then explicitly raise a binding advisory cap only to a feasible documented value. Small portfolios use deliberate lineup choices, not mechanically rounded percentages; one entry cannot diversify Captains. With two or three Showdown entries, prefer different viable Captains; record any concentration needed to retain competitive lineups.

Start candidate discovery with approximately **65% central-case**, **25% alternate viable cores**, and **10% “priors wrong”** families. This is a search allocation, not a claim about scenario probabilities or an unconditional mandate to spend 10% on weak plays. Select from that coverage using the objectives; keep a small stress-coverage sleeve only where its tail loss is within a declared tolerance. Examples: PP2 inherits real minutes, the popular line is suppressed, an underdog scores first, a high-total game disappoints, or the confirmed but popular goalie allows several goals. Do not invent unsupported injury states to create contrarianism.

Track exposure by player, Captain, goalie, team, EV line, PP unit, game, and failure scenario; report entry counts **and dollars**. Compare simulated return dependence, not just roster overlap. Different players from the same PP unit can still make two lineups fail together. Secondary builds should add outcomes the primary core does not already cover. Never enforce negative correlation so strongly that both sides lose tournament viability.

### Contest treatment and optimization

| Contest | Selection emphasis |
|---|---|
| Large-field GPP | Upper-tail probability, prize-curve value, duplication, correlated upside, diversified failure scenarios. |
| WTA | Tie-adjusted first-place equity/prize share; acceptable volatility with portfolio controls applied across entries. |
| Satellite/qualifier | Probability and tie rules for the actual number of seats. A multi-seat satellite is **not** WTA. Track ticket face value separately from cash realization. |
| Cash / H2H / double-up | Mean, downside/participation risk, and probability of clearing the relevant payout/opponent threshold. No automatic chalk fade or forced high-variance stack. |
| Small-field / single-entry GPP | Higher emphasis on projection quality; less ownership penalty than a massive field. Single-entry describes entry limits, not a payout curve. |
| Unknown | Infer a provisional family from name only when plausible, label confidence, preserve a balanced default, and collect exact metadata later. |

Use SciPy/HiGHS for legal candidate construction and joint assignment; its public API supports time limits and explicit solve statuses. [Solver documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html) Generate candidates from mean, coherent scenario clusters, stack families, and uncertainty variants. Re-score them nonlinearly against outcomes/fields, then choose one per entry using marginal coverage and a bounded assignment/local-search improvement. Do not claim that a sum-of-ceilings MILP directly solves the stochastic objective.

Keep the entire legal pool available, including low-owned values; limit search effort instead of permanently trimming player eligibility. Preserve an incumbent and stop on time. Report `FEASIBLE`, `TIME_LIMIT_WITH_INCUMBENT`, `INFEASIBLE` with scope, or `ERROR`; never mistake a timeout or small candidate bank for proof of roster infeasibility. SciPy status 2 indicates infeasibility of the submitted problem; statuses 1, 3, and 4 do not. Independently validate any returned incumbent. “Optimal” is permissible only for the exact solved objective and searched domain. [Status definitions](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html)

## 8. Code versus LLM, with time and token budgets

```text
DK files → exact intake → immediate legal baseline → durable checked export
                              ↓
Public snapshots → identity → feature/role state → outcome + ownership models
                              ↓
                shared scenarios → candidate bank → entry allocation
                              ↓
           bounded independent critique → deterministic accept/reject
                              ↓
                   final-byte check → newer export
                              ↓
             standings + frozen forecasts → evaluation → backlog
```

| Work | Owner | Why |
|---|---|---|
| Parse, identity, statistics, scoring, simulation, salary/slot constraints | Deterministic code | Reproducible and fast; numeric constraints must not depend on model attention. |
| Candidate generation, entry allocation, exposures, exact duplicates | Deterministic code | Scales to 150 entries without hundreds of narrative decisions. |
| Routine source collection and freshness checks | Deterministic adapters | Fetch once; cache; reject schema/date mismatches; no repeated LLM browsing. |
| Ambiguous injury/role news and conflicting reports | Bounded LLM research | Extract source-backed structured claims and alternatives from messy prose. |
| Strategy critique and overlooked player/line hypotheses | Independent LLM subagent | Challenge assumptions from a controlled evidence packet. |
| Accept proposals, repair/export, stop rules | Deterministic controller | The critic cannot grade its own suggestion or prolong the loop. |
| Run notes/backlog proposal | Code summary plus optional short LLM synthesis | Preserve measurements without manufacturing lessons after every result. |

**Target budgets, to benchmark on the user's machine:**

| Mode | Deterministic work | LLM invocations (hard bound) | Engine wall clock | End to end through Claude Code |
|---|---|---|---|---|
| Initial baseline | Cached priors + roster feasibility + independent format check | 0 | ≤15 seconds for 20 entries; ≤30 seconds for 150, excluding file upload | One model turn, about a minute |
| Normal slate | Cached history, current role deltas, simulations, bounded bank and selection | ≤1 research call (conflicts only) + ≤1 adversary round by default | 2–5 minutes for 20–150 entries | About three model turns |
| Extended QA | Rounds two and three only after round one accepted a correctness repair | ≤3 adversary rounds total | ≤8 minutes | Must finish before T-8 |
| Fast refresh / late swap | Reuse history, update affected games, repair remaining slots | 0; the terminal command is the documented default near lock | 10–30 seconds | Terminal: engine time only. Slash command: add one to two minutes |
| Network/LLM/solver failure | Reuse checked baseline or dependency-light feasibility fallback | 0 | ≤10 seconds to return an existing checked artifact | n/a |

These are acceptance targets, not measured benchmarks, and token counts are not budgets. Claude Code sends the full conversation on every request, and no skill or agent can read the session's token count, so the enforceable bounds are invocation counts and wall clock, held by the controller. Token usage is measured after runs with `/usage` (subagent attribution) and recorded in `docs/measured_usage.md` after the first ten slates. The slate skill runs the engine through skill preprocessing (`!`nhl.ps1 run ...``) so only the manifest summary enters the model's context; packets to the adversary are capped by construction (top exposures, stack and Captain counts, flagged conflicts, ranked alternatives, never the full pool). Stream/chunk calculations for an ordinary laptop; no GPU or paid cloud service. Download/backfill historical data outside the slate clock. Research only slate teams and unresolved claims with material exposure impact. One retry per source, then its fallback; one error diagnosis, then degraded mode. Stop optional work at T-8 minutes for anything routed through the LLM and T-5 for engine-only work, or earlier if the measured runtime would consume the upload buffer.

## 9. Adversarial QA: controlled independence and at most three rounds

**Your understanding is correct, with one important qualification:** use a **named non-fork subagent**. Claude Code also supports conversation forks, which inherit the parent's history and defeat the intended input isolation. A non-fork subagent starts with its own definition, the prompt supplied to it, and the project's CLAUDE.md files. Two settings make the isolation real: the agent's `tools` allowlist is `Read` only, which also removes the `Agent` tool (subagents can otherwise spawn subagents up to three levels deep by default), and `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` is set in project settings as a second lock. Note the naming collision: a skill with `context: fork` runs isolated, while an Agent-tool fork or `/subtask` inherits the parent conversation. The adversary launches with `omitClaudeMd: true` (Claude Code v2.1.271 or later) so no project instruction file rides along; it keeps `tools: Read` only because a subagent with no resolvable tool refuses to launch, and its prompt instructs it to read nothing, because the packet is passed inline in the delegation prompt. Non-fork isolation is a context boundary, not a filesystem boundary. The setup rehearsal asserts that the adversary transcript contains the packet and nothing else from the slate. [Claude Code subagent documentation](https://code.claude.com/docs/en/sub-agents)

`/advisor` enables a consultation tool whose timing the main model chooses; the advisor receives the full conversation. It fits development architecture questions or diagnosis of a recurring problem. It is not the controlled independent reviewer. Leave it off during bounded slate runs, because it introduces another variable in usage/timing; enable it selectively during development when available on the existing plan. [Advisor documentation](https://code.claude.com/docs/en/advisor)

**Mechanism:** the deterministic run state records round number, current artifact hashes, remaining time, and proposal history. The main skill requests one fresh `nhl-adversary` invocation per round and passes only a compact audit packet, capped at about 4,000 tokens by construction: portfolio aggregates (top-20 person exposures by entries and dollars, goalie and Captain shares, stack-family frequencies, game and failure-scenario concentration), flagged conflicts with IDs, the top-10 ranked substitution alternatives with IDs, scenario coverage, the predeclared acceptance metrics, and a five-lineup representative sample (highest tail, highest duplicate risk, most contrarian, most concentrated, most balanced). Individual lineups beyond the sample are never serialized. Do not include the builder's conversational advocacy. Supply actual IDs so critiques are actionable. The agent returns structured proposals; it has read-only access to the packet, no export editing, shell, or nested-agent tools. Optional additional research comes through the research path, not unrestricted recursive criticism.

**Before any adversary call, code asserts:** Captain and person exposure counts against caps; both teams present (Showdown) and three skater teams (Classic); pairwise overlap limits; goalie-versus-opposing-skater conflicts; shared-failure-event concentration (fee-weighted dependence on any one goalie, line, PP unit, or game); locked-slot integrity; and the section 7 tie-break. Anything these assertions catch is repaired or reported before the LLM sees a packet. The adversary's scope is what code cannot check: conflicting news, thesis coherence, and overlooked hypotheses that change an interpretable model input.

**Audit criteria:**

1. Does the selection increase contest tail opportunities without creating unpriced portfolio concentration? Examine fee-weighted goalie/line/PP/game dependence and failure scenarios.
2. Are there dominated or near-equal alternatives after salary, role, uncertainty, stacks, and lineup opportunity costs are considered? Lower ownership helps only when the resulting lineup remains competitive.
3. Is a projection based on the wrong role, stale minutes, unconfirmed starter, misjoined identity, or duplicated market adjustment? Proposed overrides must cite a specific observation and change an interpretable model input.
4. Are diamonds excluded by a hard recent-volume threshold, candidate pruning, or weak missing-history treatment? Are low-owned punts mistaken for value?
5. For Showdown, does each lineup have a supported game thesis; are player/CPT exposure counts correct; are alternative viable Captains represented; and do goalie/skater combinations fit their successful scenarios?
6. Is apparent diversification only a different set of names sharing the same failure event? Are ownership uncertainty and alternative field constructions tested?

**Acceptance rule:** classify proposals as correctness repairs or strategic changes. A verified wrong player identity, scoring error, lock violation, or new scratch is repaired for correctness, without demanding that the old erroneous model lose a simulated contest. Record the before/after facts and regenerate affected metrics. Strategic proposals become bounded input/constraint changes and re-solves, never direct CSV edits.

For strategic changes, require legality, the agreed risk budget, no decrease in the predeclared tail metric or safety metric on paired selection/referee comparisons, and at least one gain larger than its Monte Carlo error/practical materiality threshold. Use the same scenarios for the before/after comparison, fresh referee draws across accepted rounds, and a reserved final check. If evidence is inconclusive, keep the incumbent. Before the field model is fitted, the comparison runs under the prior field: a modeled gain well beyond Monte Carlo error can be accepted, and it is recorded as an unvalidated modeled improvement with the `FIELD_CALIBRATION` state attached to the decision, so model uncertainty and Monte Carlo uncertainty are never conflated. This is a conservative **estimated Pareto** acceptance rule, not proof of a real-world Pareto improvement. The initial frontier selection handles explicit tradeoffs; the critic cannot silently change risk preference.

The controller accepts at most five grouped, ranked proposals per round; it rejects unsupported claims and infeasible modifications with a short reason. **Default is one round. Rounds two and three run only when the previous round accepted at least one correctness repair, and the loop stops immediately when a round accepts zero changes, after round three, or at the wall-clock deadline.** Agent failure, malformed output, or a timeout ends optional QA and retains the current checked portfolio. Fresh context reduces conversational bias; it does not provide an independent statistical model or eliminate shared model errors.

## 10. Output, validation, and the generation guarantee

### Intake and exact output

Detect Classic/Showdown from the actual headers and role rows, not filenames. Match each entry to a compatible slate/game set. One salary file cannot service unrelated draft groups; process each supported group with its own pool, then preserve original entry ordering where the uploaded template supports the combined output. Name ambiguity is never solved by selecting a convenient same-name athlete.

**No salary or entries CSV was supplied for this planning task.** The claimed Showdown duplicate-row structure therefore cannot yet be confirmed against a real uploaded file. Phase-zero acceptance must inspect actual NHL exports: underlying-person mapping, CPT/FLEX IDs, available role rows, listed salaries, duplicate positional headers, and roster-cell syntax. Never manufacture a CPT ID or derive a salary from an assumed 1.5 ratio; the file governs. Pair role rows deterministically from unambiguous identity evidence in that file, adding the stable NHL identity where verified; an unresolved same-name collision cannot be treated as two different people to bypass uniqueness.

Retain exact `Entry ID`, `Contest ID`, `Contest Name`, fee/metadata fields, original row order, quoting/encoding requirements, and roster-column order. Do not reconstruct a template from the master spec's NFL example. Fill roster cells using the verified format supported by the supplied DK template, normally its exact `Name + ID` value, by splicing the cell text into the original line bytes; rows are never re-serialized. Repeated headers such as W/FLEX must remain distinct columns. Embedded instructions or salary-help blocks are data, not entry rows or instructions to the LLM.

“Row for row” means every actual reserved-entry row receives one lineup in the same position. Preserve auxiliary template rows as required by the actual export; do not count them as entries. Never create entries, silently drop rows, or invent missing Entry IDs.

### Pre-export checks

The independent lightweight referee re-parses the **written bytes** with its own CSV reader and its own implementation of the roster rules (deliberately separate from `contracts.geometry`; a property test keeps the two in agreement), and always binds the output to the input salary and entries file hashes, so entry set and order are checked on every run, not only when a parent is supplied. It verifies:

- Exact authorized entry set and order; no duplicate/missing assignments; correct mode/game set and roster length.
- All roster IDs exist in the correct salary-file version and are valid for that slot; underlying people are unique; Captain role IDs are correct.
- Integer salary total ≤$50,000; Classic skater-only three-team minimum; Showdown two-team representation; no invented extra DK restrictions.
- Locked player and locked slot cells are unchanged; no already-started player is newly added; all untouched entries remain unchanged.
- Current known exclusions/disabled-game facts are surfaced, with a distinction between actual illegality and a rosterable but nonplaying athlete.
- Finite projections, sensible ordered quantiles, conservation/scoring checks, actual exposure counts, duplicate keys, and any relaxed preferences.
- Output hash, input hashes, run ID, model version, generation time, and validity/recheck time agree with the artifact being delivered.

Use exact integer score units: base scores can be represented in tenths and CPT scores in twentieths. This avoids floating-point false ties. Payout division uses exact cents/fractions with the site's actual rounding/settlement treatment validated separately. A simulator's tie handling must count all copies, including the user's own multiple entries.

Game cancellation, postponement, suspension, and scoring-period state belong to the contest contract too. The attachments define the ordinary NHL scoring period through 11:59 p.m. Eastern on the day after the last scheduled game, subject to DK adjustments. Use DK's actual disabled-game/refund decision, retain suspended-game statistics only as permitted, and do not infer that a generic NHL reschedule changes the entry's lock or scoring period. The Showdown attachment includes multi-game cancellation boilerplate; reconcile any unusual game set against its actual DK status rather than inventing a blanket cancellation rule.

Future run outputs: `DKEntries.csv`, a short `RUN_NOTES.md`, and a machine-readable `manifest.json`; model/QA detail stays in the run directory. No Excel workbook or document-rendering acceptance is required to deliver a lineup CSV.

Report orthogonal statuses: `FILE_VALID`, `NEWS_STATE`, `MODEL_STATUS`, `SEARCH_STATUS`, and `DELIVERY_STATUS`. A legal fallback can be `FILE_VALID=TRUE`, `NEWS_STATE=PARTIAL`, `MODEL_STATUS=PRIOR`, `DELIVERY_STATUS=DEGRADED_REVIEW`. “Checked” means the stated checks passed; it never means guaranteed profit or confirmed future participation. Optional missing market/model evidence must not withhold the file.

### Baseline-first fallback ladder

1. **Immediately build:** immutable intake plus last validated player/role data. If no history exists, use the salary file's `AvgPointsPerGame` shrunk toward documented position/salary/role population priors, with broad uncertainty. The raw APPG value is retained; an APPG of 0 is flagged `APPG_ZERO` and given zero weight rather than treated as missing, because the file has no games-played column and zero can be a real average. The shrinkage weight is a challenger setting, not an established edge. APPG never enters the rate model. The baseline uses local inputs only: no network call precedes the first checked export. Solve, validate, and publish the initial full entry assignment; only then run the bounded network enhancement (draftables status, odds, roles) as a second pass that publishes a new version if it changes anything.
2. **Publish atomically:** retain immutable export bytes and their successful check. The public file is replaced by writing a temp file in the same directory and an atomic rename, under a slate-level lock keyed by draft group (not only a per-run lock), so two runs on one slate cannot interleave. Enhancement writes another version; a failed step cannot overwrite the incumbent or leave a partial CSV at its public path.
3. **Degrade inputs:** stale optional source → cached observation with age → role/population prior. Missing odds → hockey model. Missing ownership → broad prior with weak leverage. LLM failure → skip the critique.
4. **Degrade search:** retain a valid solver incumbent on time limit. If the normal solver is unavailable, use a small dependency-light roster feasibility routine and deterministic ranking. Classic can search represented-team choices and minimum-cost eligible slot assignments; Showdown can enumerate CPT choices and legal FLEX completions. The fallback must handle duplicate people and arbitrary supplied eligibility, not greedily choose cheap players into a dead end, and it reports `FOUND`, `TIMEOUT`, or `INFEASIBLE_PROVEN` (search space exhausted) as distinct results; a timeout is never reported as infeasibility. Solver import failure, an empty candidate bank, and a source outage each route explicitly to this rung and are tested.
5. **Relax only preferences:** drop salary-spend floors, stack shapes, pairwise uniqueness, then advisory exposure/thesis quotas as required. Preserve actual salary, identities, slots, teams, and locks. Record each relaxation. Repeating a legal lineup across reserved entries is a last-resort deliverable if necessary, with concentration explicitly reported.
6. **Retain prior evidence:** if optional QA or enhanced validation crashes, return the previously checked unchanged export. A separate tiny legality checker can assess repairs. Never stamp an unchecked new candidate valid just because the main validator failed.

**Limits that must be explicit:** corrupt/ambiguous authoritative IDs, contradictory entry geometry, no salary-feasible roster, or impossible locked slots cannot be repaired by relaxing strategy. If new news leaves no feasible replacement, retain the existing legal file and name any unavoidable nonplaying slot; do not claim it became safe or current. If no checked artifact has ever existed and all legality checking is broken, report that exact failure rather than manufacture a guarantee. These are boundary conditions, not permission for a network request or optional audit to strand an otherwise feasible portfolio.

## 11. News, goalie confirmation, and fast late swap

### Evidence and role updates

Rank reports by specificity, provenance, and time: explicit official/team or direct coach confirmation for the named game; attributed credentialed reporter observation; dated aggregator report; recent historical role. Multiple sites copying one report are one source. A later projected depth chart does not override an earlier explicit starter confirmation unless it contains new evidence.

The first scratch signal is deterministic and keyed by the exact DK ID: draftables `status` mapped through the recognized table (`OUT`, `IR`, `O` → participation OUT; `Q`, `GTD`, `D` → QUESTIONABLE with a start-probability haircut; unrecognized → UNKNOWN, preserved and reported, never treated as out), `isDisabled` (DK eligibility: not rosterable), and `isSwappable` (editability only; it says nothing about whether the player plays), refreshed at build, T-60, and T-15. Daily Faceoff injury tags and line pages are the second signal. LLM research is invoked only for a conflict between signals or an unresolved status on a player with material exposure.

Initial age policies: historical rates refreshed after the latest completed game; projected lines acceptable for modeling within 24 hours if no subsequent contrary news; a same-day practice/PP change triggers a refresh regardless of age. Inside 60 minutes to a team's start, stale/unattributed roles reduce confidence and prioritize that team for a check. Goalies remain `EXPECTED`, `CONFIRMED`, `OUT`, or `CONFLICTED`; fetching an old confirmation never changes its game/date.

Conflicts generate a role mixture and exposure warning until resolved. A player moving to PP1 changes projected PP time, teammates' shares, score distributions, and ownership, not simply their point estimate. Every LLM proposal contains the player/game IDs, old/new role, effective time, source URL/claim, confidence, and expiry; deterministic checks enforce team time/role constraints before acceptance.

### Goalies before confirmation

Estimate team starter probabilities from rotation, recent workloads, rest/back-to-backs, and explicit reports. Do not default every two-goalie team to certainty or infer certainty from the first depth-chart name. Sample one starter per team, with a separate small relief/pull process; conditional distributions and start-probability mixtures are distinct outputs.

Prefer high start-probability goalies with legal replacement paths. For every selected uncertain goalie, store affordable same-game/later-game alternatives and the smallest collateral skater changes needed. Confirmation collapses the starter mixture, updates opposing scoring rates and market consistency, and re-solves affected portfolio exposures. A late expensive replacement may require a two-player repair; keeping only a one-for-one list is insufficient.

### Fast mode

At initial construction, optimize slot placement for later flexibility: put a suitable later-starting skater in UTIL when equivalent eligibility allows, without moving any locked player later. Track times in UTC and display America/Chicago with the actual date; handle DST, schedule changes, and actual early starts.

For late swap, use the user's current DK entries export as the operational record of what is entered, compare it with the last delivered version, and record manual changes. Derive locked slots from actual lock (the game's scheduled start has passed, using draftables competition start or the salary file's Game Info) and from DK editability (`isSwappable` false); apply the engine's edit-stop buffer as a separate, earlier stop that refuses to change a cell without claiming the game has started. Refresh uses the same lock state as late swap. Never assume the last file generated was uploaded. If a refreshed entry export is unavailable, clearly state the assumed parent and preserve its locked cells; do not claim it reflects the live account.

Only fetch changed teams/news; reuse unaffected features and simulations. Pin locked cells exactly, prohibit adding started players, update remaining-game scenarios, and jointly reallocate the unlocked pieces. Locked exposures may already exceed caps; optimize residual exposure without pretending locked players can be removed. Recheck locks immediately before writing and preserve the predecessor if time crosses a lock boundary during computation.

When a current score/standing snapshot is supplied, condition remaining upside and payout thresholds on observed scores. A trailing entry may benefit from lower duplication and more variance; an ahead entry may favor reliable remaining opportunity. Do not make a “chase” pivot merely because a player has started slowly. Without reliable field scores, perform news/legality repair and use the original objective rather than inventing live standings.

Most ordinary NHL Showdown slates have one game: once it starts there is no useful remaining-player swap. The attached rules still lock each player by their game; pre-lock repair matters, and any unusual multi-game template must follow its actual game set. An unconfirmed goalie whose game has already begun cannot be rescued by late swap. Timely initial delivery and the final pregame check remain essential.

## 12. Learning loop: results, tuning gates, and backlog

Store before lock: raw-file hashes, as-of source claims, model/version, all-player forecasts and quantiles, ownership by contest/role, candidate/selected lineups, exposures, uncertainty scenarios, and build time. Store after lock: actual entry state where supplied, outcomes, standings revisions, payout evidence, and settle time. Never grade a prediction reconstructed using final news as if it were saved pre-lock.

Use one game outcome once when training player projections; multiple slates/contests reusing it are not new sporting outcomes. For ownership/field research, deduplicate exports and weight distinct slate/game-set groups, clustering uncertainty by date and overlapping games. Maintain per-mode and contest-family reporting; regular season, playoffs, and preseason are separate regimes. Financial settlement is a measured output: own entries are joined to final ranks by Entry ID, the contest's final payout table is applied with DraftKings tie rounding, and fees, gross, net, and rolling drawdown are written to a ledger per slate. That ledger is the measurement for the second objective.

| Layer | What to measure | What the data cannot establish alone |
|---|---|---|
| Opportunity | Dressing/start calibration, TOI MAE, PP-share error, role-change response | A missed goalie start is not an event-rate problem. |
| Skater/goalie forecasts | Event deviance, MAE, distribution score such as CRPS, quantile coverage; calibration of five-shot/three-block/35-save and point bonuses | P99 quality cannot be judged from one player's one big game. |
| Joint model | Line/PP teammate co-ceilings, opponent-goalie dependence, team scoring/shot distributions | Accurate player means do not establish accurate lineup tails. |
| Ownership/field | Section 6 metrics, stack/team-split/CPT/leftover-salary frequencies, exact duplication | Product of marginal ownership is not a duplicate estimate. |
| Construction | Top-1% appearances, rank percentiles, score regret, overlap, failure-scenario concentration | Best possible hindsight lineup is a diagnostic upper bound, not achievable pre-lock performance. |
| Portfolio economics | Exact payouts, net return, zero-payout/large-loss frequency, rolling drawdown; ticket outcomes separately | Ranks alone do not reveal cashes, seat awards, or profit without paid-place/payout evidence. |
| Operations | Time to first legal file, deadline misses, stale sources, fallback causes, useful QA proposals, latency/tokens | More proposals or longer notes do not mean better decisions. |

### Minimum evidence before model changes

These are conservative **eligibility floors**, not statistical guarantees. Historical backfills can meet projection floors only with as-of-safe features; they generally cannot supply missing historical news or ownership forecasts. Use at least two recent seasons of regular-season event history where available for rate/shot models; keep the final time block untouched.

| Data accumulated | Allowed adaptation | Frozen / held back |
|---|---|---|
| Every run, starting day one | Update observed history and current roles using the existing formula; correct parser/scoring/identity bugs with fixtures | No weight/exposure change because yesterday won or lost. News updates are not parameter tuning. |
| ≥20 independent slate dates, ≥5,000 skater-games and ≥300 goalie starts in evaluation history | Diagnose systematic TOI/rate bias; test small global intercept/scale corrections in shadow mode | No player-specific overrides learned from a tiny recent sample; no strategy promotion. |
| ≥30 distinct Classic slate groups, ≥15,000 valid player-contest ownership labels | Fit regularized ownership intercepts and broad salary/position/role effects; reserve the latest ≥10 groups for validation | Fine contest/fee submodels pooled into the parent unless each has enough groups. Thousands of labels from one slate do not qualify. |
| ≥50 distinct Showdown games, ≥5,000 valid person-role ownership labels | Fit CPT/FLEX behavior and basic team-split/goalie popularity with partial pooling; latest ≥15 games held out | No unshrunk Captain-archetype or exact-score-thesis tuning on a handful of winners. |
| ≥60 slate dates, ≥20,000 skater-games and ≥1,000 goalie starts, with a latest ≥20-date holdout | Compare decay windows, shrinkage strengths, context features, dispersion, and joint-event assumptions | Release only a limited challenger; do not jointly sweep every feature and exposure cap. |
| ≥100 independent slate groups per mode, ≥30 per proposed contest family, plus ≥30 untouched/prospective groups | Consider small changes to stack priors, exposure defaults, field-mixture weights, or stress sleeve | Require improved relevant out-of-sample metrics and uncertainty reporting; no bankroll-risk relaxation driven by a short profit streak. |
| ≥200 independent slate dates with complete fees/payouts and prospective forecasts | Evaluate whether payout/risk estimates calibrate; consider economic objective weights | Rare first-place ROI still has large uncertainty. Do not announce precise profitability or long-run ruin from this threshold alone. |

Count floors separately for modes and label availability. The floors live in one place, `config/evidence_floors.yaml`, read by `learn/gates.py`; every fit, whether on historical exports or live standings, and every promotion decision passes through that one gate. A development chunk completes successfully when its challenger is rejected; a chunk never requires an improvement to finish, because that invites repeated holdout searching. If NHL volume makes a threshold slow, pool sensible coefficients and remain in prior/shadow mode; do not quietly lower the requirement. A single season may be insufficient for niche WTA or Showdown goalie-CPT claims.

**Promotion process:** preregister the hypothesis, changed parameters, comparison metric, and practical effect threshold; run chronological walk-forward evaluation with data known at each build time; compare against simple role/rate and salary-aware baselines; cluster/bootstrap by slate/date; adjust for multiple tested hypotheses; examine adverse slices; then shadow the challenger prospectively for at least 20 new slate groups. Promote a versioned change only if the relevant forecast/field metric improves without an unacceptable deterioration in portfolio risk or operational reliability. Retain rollback. Strategy changes remain constrained by the stronger evidence floors above. Prospective 20-slate shadowing is an additional minimum, not a substitute for them.

Do not tune to winner anecdotes or profit alone. Withhold multiple-choice window/model searches from the final holdout; repeated peeking consumes that holdout. Use the same saved scenarios to compare policies for lower-noise experiments, and separate research evaluation from final untouched evidence. Online self-improvement means automatic measurement and proposals; code/weight promotion happens in a development session with tests, not during a live slate.

**Short run-note fields:** run/slate ID; mode; entry count/fees; first-export time; final hash/status; material news changes; accepted/rejected QA changes; fallback/relaxation; what worked operationally; what failed; keep/change recommendation. “No noteworthy finding” is valid.

**Backlog row:** ID; date/run evidence; problem or hypothesis; affected metric; proposed bounded change; confidence/sample size; acceptance test; priority; status (`NEW / SHADOW / READY / DONE / REJECTED`); result/version. Distinguish a deterministic defect from an unproven strategy hypothesis. Deduplicate repeated observations and keep one dependency-satisfied READY implementation chunk.

## 13. Planned repository and Claude Code setup

**This is a proposed layout, not created files.** Favor one Python package, a pinned environment, a small local database/index, immutable raw snapshots, and command wrappers. No web app, distributed service, credentials manager, or background agent swarm is needed.

| Planned path | Responsibility |
|---|---|
| `CLAUDE.md` | Brief operating contract: authority, role boundaries, deadline/fallback, prohibited assumptions, approved commands, where to find details. |
| `src/nhl_dfs/contracts/` | Scoring, Classic/Showdown geometry, typed player/person/role IDs, entries, evidence states. |
| `src/nhl_dfs/data/` | Public adapters, caching, NHL identities, as-of feature construction, role/news observations. |
| `src/nhl_dfs/models/` | Opportunity, event rates, shot quality, goalies, ownership, field construction, uncertainty. |
| `src/nhl_dfs/sim/` | Shared game events, exact DK scoring, outcome arrays, contest/tie settlement. |
| `src/nhl_dfs/build/` | Baseline, candidate families, portfolio assignment, fast repair, dependency-light fallback. |
| `src/nhl_dfs/referee/` | Separate final-byte legality and comparison checks; small dependency surface. |
| `src/nhl_dfs/learn/` | Standings intake, forecast evaluation, experiments, version promotion evidence. |
| `data/raw/`, `data/cache/`, `data/features/` | Immutable captures, replaceable caches, reproducible feature versions; private data ignored by Git. |
| `runs/<run_id>/` | Inputs, manifests, models/seeds, baseline, proposals, evaluations, final artifacts, predecessor relationship. |
| `outputs/<slate>/` | Simple user-facing export and notes; publication points to an immutable run artifact. |
| `config/` | Mode/contest defaults, source/freshness policies, runtime budgets, model versions; no secrets. |
| `tests/fixtures/` | Synthetic and safely minimized real-schema fixtures: roles, locks, duplicate headers, scoring edge cases, outages. |
| `docs/` | Architecture, scoring/source definitions, runbook, backlog, experiment log and implementation status. |
| `.claude/skills/` | Thin skills for setup, run-slate, late-swap, refresh, settle, and dev-next; procedures call deterministic commands. `nhl-run` uses `!` preprocessing so the engine runs before the model reads anything and only the manifest summary enters context. |
| `.claude/agents/` | `nhl-researcher` (tools: WebFetch, Read) and `nhl-adversary` (tools: Read, `omitClaudeMd: true`, packet inline, instructed to read nothing); explicit compact inputs and JSON output schemas; `.claude/settings.json` sets `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` to 1. |
| `nhl.ps1`, `nhl.sh`, environment lockfile | Equivalent one-command launch paths for Windows and Linux; checked during setup. |
| `BUILD_CHUNKS.md`, `chunks.yaml`, `BUILD_STATUS.md` | The build broken into seventeen single-session chunks (human cards), the machine-readable dependency graph, and the tracker every development session reads first and updates last. |
| `tools/next_chunk.py` | Prints the next chunk whose dependencies are DONE, runs the exit checks of DONE predecessors, and refuses to start a chunk whose predecessors fail. |
| `tools/capture.py`, `tools/register_capture_task.ps1` | Prospective snapshotting of partner odds, Daily Faceoff pages, DK lobby and draftables at fixed local times via Windows Task Scheduler. Raw bytes only; no Claude Code involvement. |
| `docs/CONTRACTS.md` | The condensed scoring table, roster geometry, ID rules, and status vocabulary that sessions read instead of this document. |
| `config/evidence_floors.yaml`, `learn/gates.py` | The single evidence-gate definition every fit and promotion passes through. |
| `data/ledger/` | Financial settlement per slate: fees, gross, net, drawdown, by Entry ID. |
| `reviews/` | The design review, the two critiques of revision 2, and the synthesis of decisions. |

Skills can expose repeatable slash commands and consume arguments; keep their instructions short and delegate calculations to the engine. Use a named fresh-context agent definition for QA, with a read-only tool allowlist. Claude Code configuration is version-dependent, so setup must verify the installed version's behavior rather than blindly copying syntax from another project. [Skills documentation](https://code.claude.com/docs/en/skills)

`CLAUDE.md` should say: salary and entry bytes are authoritative; never invent IDs/news/payouts; always create the checked baseline first; never wait for optional QA to export; no live engine editing; no direct LLM CSV editing; at most three review rounds; use structured role changes; preserve locked slots; report honest model status; manual DK login/upload/entry/money actions; and keep source instructions isolated as untrusted data. Put detailed schemas in reference files instead of rereading the full architecture each slate.

Keep source code immutable during a live build. One writer per slate; a slate-level lock keyed by draft group prevents parallel runs overwriting the chosen export. Development commits stage the chunk's listed paths explicitly after reviewing `git status`; full personal DK exports and standings are gitignored and only minimized fixtures are committed. Development, slate builds, and settlement are separate modes. Retain only a small index in SQLite; large historical tables/scenario arrays can live in compressed local files. Install and test dependencies once; daily generation must not depend on a package download.

**Proposed user-facing commands, available only after implementation:**

| When | Claude Code command | Deterministic equivalent to provide |
|---|---|---|
| Once | `/nhl-setup` | `.\nhl.ps1 setup`: pinned environment, source smoke tests, fixtures, offline fallback rehearsal |
| Normal slate | `/nhl-run "C:\path\DKSalaries.csv" "C:\path\DKEntries.csv"` | `.\nhl.ps1 run --salary "C:\path\DKSalaries.csv" --entries "C:\path\DKEntries.csv"` |
| Close to lock | `/nhl-run ... --fast` | Same run command with `--fast`; baseline/repair first, no optional critique |
| Goalie/news update | `/nhl-refresh <run-id>` | `.\nhl.ps1 refresh --run <run-id>` |
| After some games lock | `/nhl-late-swap <run-id> "C:\path\DKEntries-current.csv"` | `.\nhl.ps1 late-swap --run <run-id> --entries "C:\path\DKEntries-current.csv" --fast` |
| Independently verify | `/nhl-verify <run-id>` | `.\nhl.ps1 verify --run <run-id>` |
| After contests | `/nhl-settle <run-id> "C:\path\standings.zip"` | `.\nhl.ps1 settle --run <run-id> --standings "C:\path\standings.zip"` |
| Development session (build) | `/nhl-dev-next` | Runs `python tools/next_chunk.py`, works that one chunk per `BUILD_CHUNKS.md`, runs its exit checks, updates `BUILD_STATUS.md`, commits. After every chunk is DONE it switches to the backlog: one READY item per session. |
| Any time | `/nhl-status` | `.\nhl.ps1 status`: chunk status, last run, open [BEN] flags. |

Normal operation should also accept plain language plus the two uploaded files. Slash commands make behavior repeatable; they should not become additional manual configuration work. The setup rehearsal must prove that the QA invocation is non-fork, sees only its intended briefing/shared project instructions, and cannot write exports.

## 14. Build order (revision 2) and chunking

*Superseded for ordering (2026-10-04): the order of work is the Queue section of `BUILD_CHUNKS.md`, generated from `chunks.yaml`; this table records how C0a to C13 were sequenced.*

The build is sequenced by objective, not by model sophistication: a legal file first, then lock-safe late swap, then the things the two objectives are defined against on priors (ownership, duplication, payout metadata, a sampled field, a provisional leverage-aware selection), then hockey features that sharpen projections, then the simulator and the scenario-based objectives, then roles and news, then the Claude Code layer and settlement, then the field-model fit, then the segment simulator as a challenger. The operational breakdown is [BUILD_CHUNKS.md](BUILD_CHUNKS.md): seventeen chunks (fifteen unconditional, two gated on data), each sized for one Claude Code session without compaction, with explicit dependencies, files, interfaces, and exit checks. [BUILD_STATUS.md](BUILD_STATUS.md) tracks them and `tools/next_chunk.py` enforces the order.

| Order | Chunk | Adds | Revision 1 phase |
|---|---|---|---|
| 1 | C0a | Repo, contracts, exact scoring, tracker tooling | 0 |
| 2 | C0b | Intake, byte-splicing export, independent referee, real-file fixtures | 0 |
| 3 | C1 | DK public, NHL (schedule, rosters, box scores, per-game reports, partner odds), Covers adapters; cache and observations; prospective capture | 0 and 2 (sources) |
| 4 | C2a | Priors with APPG shrinkage, feasibility fallback with result states, HiGHS candidates | 1 |
| 5 | C2b | Local-first baseline run, atomic publish under a slate lock, manifest, CLI, bounded network second pass | 1 |
| 6 | C2c | Four-state lock model, late swap, refresh on the baseline objective. **Milestone 1: usable slate workflow** | 5 (lock-safe repair) |
| 7 | C3 | Ownership prior, field sampler with replacement, duplicate proxy, evidence gates, provisional leverage-aware selection with payout metadata. **Milestone 2: provisional leverage portfolio** | 4 (lite) |
| 8 | C4 | History cache (MoneyPuck listed downloads or NHL per-game reports), identity crosswalk, as-of features, Parquet storage | 2 |
| 9 | C5 | Opportunity, event-rate, and goalie models, per person | 2 |
| 10 | C6 | Aggregate joint simulator with pace factor, market fit, scoring arrays, calibration harness | 1 and 3 (aggregate only) |
| 11 | C7 | Roles and news: Daily Faceoff hydration payload, status map, role state machine, overrides | 5 (news) |
| 12 | C8 | Scenario objectives by contest family, evidence states, frontier, allocation, full run. **Milestone 3: objective-aware portfolio** | 4 (full objectives) |
| 13 | C9 | Objective-aware refresh and late swap using C7 roles and C8 objectives on the C2c lock model | 5 |
| 14 | C10 | Claude Code layer: skills, agents, controller, rehearsal, measured usage | 5 |
| 15 | C11 | Settle: financial ledger, ownership and forecast grading, run notes, backlog, gate reports. **Milestone 4: learning loop live** | 6 |
| 16 | C12 | Field-model fit by contest family (gated on the evidence floors) | 4 (fit) |
| 17 | C13 | Segment simulator challenger (gated on C6 calibration evidence and a preregistration) | 3 |

**Expected scale:** a checked legal file with lock-safe late swap after C2c (six sessions); a provisional leverage-aware portfolio after C3 (seven); a scenario-based, objective-aware portfolio after C8 (twelve); news, late swap, and QA through Claude Code after C10 (fourteen); the learning loop and financial ledger after C11 (fifteen). Statistical validation still takes much of a season; C12 and C13 wait on data, not effort. C1 and C2a are independent once C0b is done and may be taken in either order.

High-value tests include scoring threshold boundaries and stacking of bonuses; goalie shutout with shootout versus shared-goalie game; negative goalie scores; CPT multiplier once; same person with two role IDs; Classic skater-team count excluding goalie; ambiguous names; mixed entry/help rows; all locked late-swap; expensive goalie replacement requiring collateral change; exact ties/duplicates; stale source cache; interrupted writes; dead network; solver timeout/import failure; malformed QA; and an optional step consuming the entire remaining deadline. Test actual behaviors and invariants, not every low-impact helper implementation.

Do not add a dashboard, native Excel requirement, complete historical paid odds archive, sophisticated passing tracker, deep neural model, multiple solver backends, or multi-agent development bureaucracy before these exit criteria pass.

## 15. Risks, unresolved details, and assumptions in force

**Material risks and decisions:**

- **Free does not imply stable or unrestricted.** NHL endpoints are public but undocumented; scraping layouts change. MoneyPuck states specific use limits. Make every optional adapter replaceable, cache data, credit providers, and keep the core viable from NHL events. Current accessibility was checked; season-wide completeness and terms for the eventual usage remain implementation checks.
- **Actual NHL CSV geometry is still unverified.** No salary, entries, or standings sample was attached. All specific role-ID and ownership-block behavior must be confirmed from those files before live acceptance.
- **No free projection oracle exists in this design.** Accurate roles, strong shrinkage, honest uncertainty, and prospective evaluation matter more than branding the feature layer “ML.” A poor mean/role model cannot be repaired with a larger simulator.
- **Tail and financial risk are hard to estimate.** Ownership and field errors can dominate tiny simulated advantages. Exact payout metadata is extra information beyond the two routine uploads. Missing bankroll prevents meaningful long-run ruin claims. Never infer cashing from an arbitrary top-rank percentage.
- **The two objectives can conflict.** Exposure reduction can sacrifice first-place equity; chasing maximum upside can increase simultaneous failure. Report the frontier and choose a risk policy explicitly. Diversification is not free and cannot ensure profit.
- **Late swap is not universal insurance.** Some information arrives after the affected game locks; ordinary single-game Showdown has no post-start recourse. Default to a pre-lock upload buffer and practical replacement plans.
- **Preseason and playoffs need separate regimes.** Planned goalie splits, sparse prospects, unusual rosters, and overtime formats can invalidate regular-season priors. Support legal fallback for any valid supplied pool; mark modeling quality limited until that regime is tested. Default model development focuses on regular-season NHL.
- **Feature identification errors are costly.** Use opponent attacking metrics for goalie workload; distinguish on-ice from individual rates, primary assists from shot assists, observed roles from projections, and provider-specific high-danger definitions. Do not equate expensive player salaries with known skill when history is absent; salary priors are labeled emergency information.
- **Independent QA has bounded value.** It can reveal unsupported assumptions; it cannot independently validate a model using only that model's outputs. Deterministic checks and genuinely held-out sporting outcomes remain necessary.
- **Account state is external.** A generated file does not establish that it was uploaded or accepted. Final upload and any DK rejection are manual; the engine should diagnose a pasted rejection and repair only the affected contract.
- **Maintenance must remain small.** One CLI, one state owner, one canonical identity/duplicate function, small adapters, and concise run notes. No evidence ceremony should make a legal portfolio miss lock.

**Assumptions in force until Ben overrides them (none blocks the build; all are also listed in BUILD_STATUS.md):**

1. **[BEN: prior-season NHL standings exports?]** Assumed none. The ownership sampler starts from hand-set utilities; if exports exist, C3 fits them first.
2. **[BEN: risk budget]** Placeholder in `config/risk.yaml`: probability of losing at least 80% of slate fees at most 0.60; at most 40% of fees on one goalie; at most 40% of fees on one game only when the slate has more than one game (a single-game Showdown slate is measured by Captain share and shared failure scenario instead).
3. **[BEN: typical entry mix]** Assumed 20–150 entries per slate across 150-max GPPs and Showdown, with occasional WTA and single-entry. Field-model calibration order follows what the first ten slates' contest endpoints report.
4. **[BEN: Claude Code host]** Assumed local Windows with PowerShell; `nhl.ps1` is primary and `nhl.sh` is kept equivalent. Near lock, the terminal command is the documented default and the slash command is the alternative.
5. **[BEN: "tunes itself"]** Interpreted as measure, propose, and promote in a development session behind the section 12 floors. No parameter updates itself between slates.
6. **[BEN: MoneyPuck terms]** Assumed acceptable for personal non-commercial use with attribution; `sources.moneypuck.enabled` defaults to true; the NHL-derived path is complete without it.
7. Preseason is not a target; regular season first, playoffs validated separately. Exhibitions keep a labeled legal fallback.