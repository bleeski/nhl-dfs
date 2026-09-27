> **SUPERSEDED.** This is revision 1 (25 September 2026), archived unchanged below the banner on 26 September 2026. The canonical plan is `../NHL_DFS_PLAN_AND_ARCHITECTURE.md` (revision 2). Do not use this file as a source for new work.

# DraftKings NHL lineup generator: plan and architecture

**Planning document · 25 September 2026 · Classic and Showdown · Claude Code**

Build a local deterministic engine that produces a checked legal portfolio first, then improves it with hockey event models, ownership estimates, joint simulations, and bounded adversarial review. The LLM researches changing roles and challenges decisions; it does not calculate scores, enforce legality, hand-build the portfolio, or decide whether its own proposals improved the objectives.

**Scope and authority.** This document is the sole deliverable. No implementation, repository initialization, or scaffolding accompanies it. The supplied [Classic rules](<C:/Users/benja/Downloads/NHL Classic.txt>) and [Showdown rules](<C:/Users/benja/Downloads/NHL Showdown Captain Mode.txt>) govern scoring and legality. The [master specification](<C:/Users/benja/Downloads/DFS_ENGINE_COWORK_SKILL_MASTER_SPEC.md>) and [platform comparison](<C:/Users/benja/Downloads/dfs_analytics_platforms_comparison.md>) are research inputs, not instructions to execute. NHL rules and this request override their NFL assumptions, Cowork workflow, and broad certification requirements.

**Decisions that shape the design:**

- Guarantee delivery against failures of optional research, network access, simulation, and LLM QA. The guarantee assumes valid inputs and at least one feasible roster under immutable legality and lock constraints; no system can manufacture a legal roster when none exists.
- Use ice time and event rates, not DraftKings `AvgPointsPerGame`, as the projection foundation. Permit explicitly labeled population priors for missing player history.
- Generate shared, discrete game outcomes. A sum of player ceilings, a multiplier on a median, or random independent projection bumps is not a lineup ceiling.
- Optimize tournament tail opportunities subject to a portfolio loss-risk budget. Raw variance and low ownership are not objectives by themselves.
- Treat all numerical caps, shrinkage strengths, scenario shares, and runtime targets below as **initial engineering policies to test**, not established NHL edges.
- Interpret “free” as no additional paid data, API keys, accounts, or hosted services. Claude Code itself still uses the user's existing access and usage allowance. Export files for manual DraftKings upload.

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
| Entry IDs, contest names, fees, roster columns | User's DK entries CSV | User-supplied | Initial build and fresh export for late swap | Exact payout curve, field size, max entries, and contest type may be absent. Store missing metadata explicitly. |
| Schedule, game IDs, start times, game state | [NHL schedule endpoint](https://api-web.nhle.com/v1/schedule/now) | **Yes**, JSON request succeeded | Daily; every 15 minutes during a slate; event-driven near starts | Undocumented public interface. Cached schedule plus uploaded DK game times; DK lock state governs editability. |
| Goals, assists, SOG, blocks, TOI, goalie results | [NHL box score](https://api-web.nhle.com/v1/gamecenter/2025020001/boxscore) and [season summaries](https://api.nhle.com/stats/rest/en/skater/summary?isAggregate=false&isGame=false&start=0&limit=1&cayenneExp=seasonId=20252026) | **Yes**, sampled both | Update completed games nightly; recheck corrections at +24 and +72 hours | Box-score totals and event feeds may temporarily disagree. Reconcile before training; keep last validated historical snapshot. |
| Individual attempts, shot coordinates/type, goals/assists, strength state, penalties | [NHL play-by-play](https://api-web.nhle.com/v1/gamecenter/2025020001/play-by-play) | **Yes**, sampled JSON | Nightly; incremental fetch for game-state updates | Schema changes, rink bias, event corrections, incomplete preseason events. Fall back to box-score SOG/blocks and population shot-quality priors. |
| EV/PP/SH TOI, shared ice, actual line combinations | [NHL shift charts](https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId=2025020001&limit=1), joined to PBP | **Yes**, sampled JSON | Nightly, with coverage/reconciliation checks | Overlapping/missing shifts and strength transitions require careful intervals. Fall back to recent TOI splits and projected lines; observed historical co-ice is not tonight's confirmation. |
| Stable statistical player identity | [NHL player data](https://api-web.nhle.com/v1/player/8478402/landing), NHL rosters/PBP identities | **Yes**, player response sampled | Daily and after trades/call-ups | NHL IDs are statistical keys; DK IDs are slate/role keys. Unknown mappings use DK-only priors until resolved. |
| ixG, rebound probabilities, shot quality, goalie GSAx | [MoneyPuck download page](https://moneypuck.com/data.htm) | **Yes, with published use limits** | Listed shot files update nightly; engine refresh daily | Free noncommercial use with attribution; other uses require inquiry. Personal DFS applicability is not resolved here. Use listed downloads only; optional adapter. Fallback: NHL-derived calibrated shot model. |
| Historical skater/goalie/team/line summaries | [MoneyPuck downloads](https://moneypuck.com/data.htm) | **Yes, same qualification** | Daily when updated | Definitions differ from other sites. Shot files omit blocked shots; obtain blocks elsewhere. Maintain provider/version-specific features. |
| iCF/60, iSF/60, A1/60, HD chances, team CF/CA by strength | [Natural Stat Trick](https://www.naturalstattrick.com/) | **Unverified today** | If usable, daily historical refresh | Research access was blocked by robots. Do not promise a dependable free scraper. Reconstruct attempts/primary assists from NHL events; use a named local danger definition instead of relabeling it NST HD. |
| Projected EV lines, defense pairs, PP/PK units, injury tags | [Daily Faceoff team page](https://www.dailyfaceoff.com/teams/toronto-maple-leafs/line-combinations) | **Yes**, timestamp and unit tables visible | Morning; after practice; T-90/T-30/T-10 for each game, respecting site limits | HTML changes and stale team pages. Sample displayed a September 24 update. Use event/source timestamps, not page-fetch time; fallback to recent actual co-ice and official reports. |
| Expected/confirmed starting goalies | [Daily Faceoff goalies](https://www.dailyfaceoff.com/starting-goalies) | **Yes**, public page; full daily record coverage unverified | Morning, T-90/T-30/T-10; one final check for affected goalies | Publication lag; status wording varies. Prefer an explicit named reporter/team confirmation over a ranking or depth-chart order. Fallback: start probabilities and swap alternatives. |
| Official roster/news corrections and coach comments | NHL team sites; [example team lineup report](https://www.nhl.com/islanders/news/topic/training-camp/preseason-game-preview-islanders-at-devils-sept-20-2026) | **Yes**, sample page accessible | News-triggered for slate teams | Inconsistent publication and embedded images/social posts. Store exact claim, game, time, and URL; user-pasted text is a fallback. |
| Left Wing Lock starting goalies, combinations, site tools | [Access page](https://leftwinglock.com/accessCheck.php) | **No** | Excluded | Current page requires premium access. Do not rely on old recommendations describing these tools as free. |
| Moneylines and totals | [Covers odds](https://www.covers.com/sport/hockey/nhl/odds) | **Yes**, current moneylines and dated odds board visible; totals view linked | T-180/T-60/T-15, plus goalie changes; retain book/time | Scraping, region, market delays. Cache then use a team-strength model if stale; manual paste is optional. Full automated totals ingestion remains an implementation acceptance item. |
| Backup displayed odds | [ESPN NHL odds](https://www.espn.com/nhl/odds) | **Unverified for live use** | Only when record date matches game | Research responses were inconsistent/stale. Never treat a fetched page as fresh odds without a timestamp and matching game. |
| Free team totals, SOG/goal/save props | Public sportsbook/aggregator pages | **Unverified for dependable complete coverage** | Optional, same-game timestamp required | No dependable no-key complete prop feed verified. Infer team scoring rates from totals and moneylines; omit props when unavailable. No Odds API account requirement. |
| Entries, exits, primary shot assists, slot/cross-seam passes | [All Three Zones](https://www.allthreezones.com/) and other manual tracking projects | **No verified complete free feed** | Excluded from production v1 | Public samples exist; full project promotes patron access. Use proxies below, not invented tracking values or a scraping project. |
| Zone time / shot-location summaries | [NHL EDGE](https://www.nhl.com/news/nhl-edge-advanced-stats-section-brings-fans-closer-to-game) | **Yes**, public summaries | Optional daily research | Bulk historical extraction unverified; omit from the critical path. |
| Actual ownership, lineups, scores, final ranks | User-uploaded DK standings | User-supplied; schema not yet inspected | After final results, with revisions retained | Ownership block coverage and CPT/FLEX semantics need validation. Payouts may be missing; parse them separately when supplied. |

**Preferred dependency chain:** NHL raw data → local features; Daily Faceoff + official team reports → role observations; Covers → optional market constraints. MoneyPuck is a valuable optional accelerator subject to its published terms. NST and tracking projects are not required. Historical betting lines and historical pre-lock news were not verified as complete free archives; begin prospective collection immediately rather than backfilling guessed information.

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

- Draw a lineup/goalie-role scenario and uncertain rate parameters, then shared game pace, penalties, and scoring environment. Use regulation segments and EV/PP/SH states; phase one can use a simpler coherent aggregate model.
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

**Start with a transparent prior; learn a model when enough distinct slates exist.** Inputs: salary and position-relative salary rank, mean/ceiling/value, expected TOI, EV/PP role, line partners, team total, goalie start/win expectations, recent visible fantasy production, star/reputation proxy from prior ownership, news timing, slate size, start time, and contest family/fee/entry limit. Past fantasy outcomes can help predict popularity even when they are excluded as a projection foundation. No paid ownership source is required.

Ownership is roster inclusion probability, not probability of being the best play. Use regularized player utilities inside a **legal lineup sampler**, then adjust utilities to fit observed marginal ownership. The sampler makes position/salary/team constraints, stack popularity, and Captain choice coherent. A regression producing unrelated percentages that do not fit legal rosters is insufficient.

For Classic, total player inclusion mass should be approximately **900%**, including **100% goalie mass** and **800% skater mass**; UTIL means C/W/D marginal budgets are not simply 200/300/200%. For Showdown, track **100% CPT**, **500% FLEX**, and **600% combined player inclusion**, with CPT and FLEX mutually exclusive for the same person in a lineup. Do not double fantasy points when extracting role ownership. Roundoff and incomplete exports receive explicit tolerances/statuses.

The opponent field is a mixture of believable behavior: projection optimizers, line/PP stack builders, stars-and-value constructions, casual selections, and contrarian builders. Learn their proportions by contest family when data permits; otherwise use broad priors and sensitivity ranges. Preserve same-line popularity, combinations, salary left, goalie choices, and exact duplicates. Multiplying nine player ownership percentages does not estimate a Classic lineup's probability.

**Cold start:** forecast ownership ranges, use only moderate leverage preference, and generate alternatives under low/base/high ownership scenarios. Never rank tiny projected differences as a known edge. Fragile chalk means high popularity paired with uncertain opportunity, fragile conversion assumptions, or highly duplicated construction—not simply a popular excellent player.

After each slate:

- Join standings to the exact salary file and frozen pre-lock ownership forecast. Deduplicate contest exports and revisions by Contest ID/content; separate player ownership summaries from entry rows.
- Verify whether the export contains every eligible player, zero-owned players, role-separated percentages, and complete lineups. Missing is not zero. For Showdown, reconstruct CPT/FLEX rates from complete lineup rows when the summary is combined or ambiguous. Do not assume the user's description determines the raw schema.
- Grade whole-pool and active-pool MAE in percentage points, weighted error on popular players, calibration by ownership band, top-chalk recall, zero-observed mass, position/team/line totals, Captain shares, and duplicate-count error. Rank correlation alone is not calibration.
- Fit with chronological slate/day splits. Hundreds of contests from one slate do not supply hundreds of independent observations. Actual future ownership is never a pre-lock input in a historical test.

## 7. Lineup construction and the two objectives

### Define the objectives correctly

Hockey scores are discrete: “99th-percentile outcome density” should become **probability mass in contest-relevant upper-tail outcomes**, supported by P99/exceedance diagnostics. Maximizing variance alone can select bad plays with large downside; adding player P99s overstates attainable lineup outcomes.

For candidate lineup `l`, contest `c`, and shared slate scenario `s`, compute score `S(l,s)` against an independently generated opponent field. Define the upper-tail threshold from the **same scenario's** opponent scores. For large GPPs, track top-1% finish probability, expected top-1% entry count, probability any portfolio entry reaches that region, and expected payout under the actual curve. For WTA, use tie-adjusted first-place prize share. Top-1% is not a substitute for first place in enormous fields.

Portfolio scenario return is `R(s) = sum(entry payouts in s) − total entry fees`. Report separately: probability of **zero gross payout**, probability of **net loss**, probability of **losing at least 80% of slate fees**, and expected shortfall. These answer different questions. With highly skewed GPP portfolios, worst-5% expected shortfall can equal the full buy-in almost everywhere; do not pretend that flat metric distinguishes portfolios. Add the loss-threshold probabilities and recovery distribution.

Choose a small frontier of tournament-tail utility versus portfolio loss risk. Select the strongest tail portfolio that fits the configured risk budget. When no portfolio meets a requested budget, show the least-risk feasible alternative and the achievable tradeoff; still produce the legal file. When exact field/payout metadata is unavailable, use declared contest-family priors and label tail/coverage scores **uncalibrated scenario proxies**. Do not report payout-based ruin, ROI, or EV as measured facts.

The entries CSV fixes committed capital. Allocation means choosing which lineup goes into which purchased entry, with fee-weighted risk across all modes on the slate. It cannot retroactively move a $20 entry into a $1 contest. Contest purchasing and future bankroll allocation remain user decisions. A meaningful multi-slate ruin calculation needs bankroll and a spending policy; slate diversification alone cannot guarantee capital preservation.

### Hard roster contracts

| Contract | Classic | Showdown |
|---|---|---|
| Roster | 2 C, 3 W, 2 D, 1 skater UTIL, 1 G | 1 CPT, 5 FLEX; any supplied eligible position including G |
| Salary | At most $50,000 | At most $50,000 using each selected role row's salary |
| Teams | Skaters from at least **three** distinct teams; the goalie does not satisfy this test | At least one player from each team |
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

Reject a cheap punt only for inadequate role/value relative to alternatives, not for failing an arbitrary shot threshold. Value is salary-adjusted gain against feasible replacements and the lineup's tail performance—not points-per-dollar alone, which can overrate low-upside minimum salaries.

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

| Mode | Deterministic work | LLM allowance | Wall-clock target |
|---|---|---|---|
| Initial baseline | Cached priors + roster feasibility + independent format check | Zero | ≤15 seconds for 20 entries; ≤30 seconds for 150, excluding file upload |
| Normal slate | Cached history, current role deltas, simulations, bounded bank and selection | About 8k–16k input + 2k–4k output tokens total across orchestration, research, and one QA pass | 2–5 minutes for 20–150 entries |
| Three-round maximum | At most three QA responses and bounded re-solves | Hard planning ceiling about 30k input + 6k output tokens; measure actual usage | ≤8 minutes and always below remaining lock budget |
| Fast refresh / late swap | Reuse history, update affected games, repair remaining slots | Zero by default; optional ≤1k input + 300 output for one ambiguous claim | 10–30 seconds target |
| Network/LLM/solver failure | Reuse checked baseline or dependency-light feasibility fallback | Zero | ≤10 seconds to return an existing checked artifact |

These are acceptance targets, not measured benchmarks. Stream/chunk calculations for an ordinary laptop; no GPU or paid cloud service. Download/backfill historical data outside the slate clock. Research only slate teams and unresolved claims with material exposure impact. One retry per source, then its fallback; one error diagnosis, then degraded mode. Stop optional work at T-5 minutes or earlier if the measured runtime would consume the upload buffer.

## 9. Adversarial QA: controlled independence and at most three rounds

**Your understanding is correct, with one important qualification:** use a **named non-fork subagent**. Claude Code also supports conversation forks, which inherit the parent's history and defeat the intended input isolation. A non-fork subagent starts with its own definition and the prompt supplied to it. [Claude Code subagent documentation](https://code.claude.com/docs/en/sub-agents)

`/advisor` enables a consultation tool whose timing the main model chooses; the advisor receives the full conversation. It fits development architecture questions or diagnosis of a recurring problem. It is not the controlled independent reviewer. Leave it off during bounded slate runs, because it introduces another variable in usage/timing; enable it selectively during development when available on the existing plan. [Advisor documentation](https://code.claude.com/docs/en/advisor)

**Mechanism:** the deterministic run state records round number, current artifact hashes, remaining time, and proposal history. The main skill requests one fresh `nhl-adversary` invocation per round and passes only a compact audit packet: rules, inputs/claims with timestamps, projections/ranges, ownership ranges, portfolio/fees, scenario coverage, concentrated risks, candidate alternatives, and the predeclared acceptance metrics. Do not include the builder's conversational advocacy. Supply actual IDs so critiques are actionable. The agent returns structured proposals; it has read-only access to the packet, no export editing, shell, or nested-agent tools. Optional additional research comes through the research path, not unrestricted recursive criticism.

**Audit criteria:**

1. Does the selection increase contest tail opportunities without creating unpriced portfolio concentration? Examine fee-weighted goalie/line/PP/game dependence and failure scenarios.
2. Are there dominated or near-equal alternatives after salary, role, uncertainty, stacks, and lineup opportunity costs are considered? Lower ownership helps only when the resulting lineup remains competitive.
3. Is a projection based on the wrong role, stale minutes, unconfirmed starter, misjoined identity, or duplicated market adjustment? Proposed overrides must cite a specific observation and change an interpretable model input.
4. Are diamonds excluded by a hard recent-volume threshold, candidate pruning, or weak missing-history treatment? Are low-owned punts mistaken for value?
5. For Showdown, does each lineup have a supported game thesis; are player/CPT exposure counts correct; are alternative viable Captains represented; and do goalie/skater combinations fit their successful scenarios?
6. Is apparent diversification only a different set of names sharing the same failure event? Are ownership uncertainty and alternative field constructions tested?

**Acceptance rule:** classify proposals as correctness repairs or strategic changes. A verified wrong player identity, scoring error, lock violation, or new scratch is repaired for correctness, without demanding that the old erroneous model lose a simulated contest. Record the before/after facts and regenerate affected metrics. Strategic proposals become bounded input/constraint changes and re-solves, never direct CSV edits.

For strategic changes, require legality, the agreed risk budget, no decrease in the predeclared tail metric or safety metric on paired selection/referee comparisons, and at least one gain larger than its Monte Carlo error/practical materiality threshold. Use the same scenarios for the before/after comparison, fresh referee draws across accepted rounds, and a reserved final check. If evidence is inconclusive, keep the incumbent. This is a conservative **estimated Pareto** acceptance rule, not proof of a real-world Pareto improvement. The initial frontier selection handles explicit tradeoffs; the critic cannot silently change risk preference.

The controller accepts at most five grouped, ranked proposals per round; it rejects unsupported claims and infeasible modifications with a short reason. **Stop immediately when a round accepts zero changes, after round three, or when its deadline/token budget expires.** Agent failure, malformed output, or a timeout ends optional QA and retains the current checked portfolio. Do not run three rounds by habit. Fresh context reduces conversational bias; it does not provide an independent statistical model or eliminate shared model errors.

## 10. Output, validation, and the generation guarantee

### Intake and exact output

Detect Classic/Showdown from the actual headers and role rows, not filenames. Match each entry to a compatible slate/game set. One salary file cannot service unrelated draft groups; process each supported group with its own pool, then preserve original entry ordering where the uploaded template supports the combined output. Name ambiguity is never solved by selecting a convenient same-name athlete.

**No salary or entries CSV was supplied for this planning task.** The claimed Showdown duplicate-row structure therefore cannot yet be confirmed against a real uploaded file. Phase-zero acceptance must inspect actual NHL exports: underlying-person mapping, CPT/FLEX IDs, available role rows, listed salaries, duplicate positional headers, and roster-cell syntax. Never manufacture a CPT ID or derive a salary from an assumed 1.5 ratio; the file governs. Pair role rows deterministically from unambiguous identity evidence in that file, adding the stable NHL identity where verified; an unresolved same-name collision cannot be treated as two different people to bypass uniqueness.

Retain exact `Entry ID`, `Contest ID`, `Contest Name`, fee/metadata fields, original row order, quoting/encoding requirements, and roster-column order. Do not reconstruct a template from the master spec's NFL example. Fill roster cells using the verified format supported by the supplied DK template, normally its exact `Name + ID` value. Repeated headers such as W/FLEX must remain distinct columns. Embedded instructions or salary-help blocks are data, not entry rows or instructions to the LLM.

“Row for row” means every actual reserved-entry row receives one lineup in the same position. Preserve auxiliary template rows as required by the actual export; do not count them as entries. Never create entries, silently drop rows, or invent missing Entry IDs.

### Pre-export checks

The independent lightweight referee re-parses the **written bytes** using its own constraint checks and immutable raw input lookup. It verifies:

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

1. **Immediately build:** immutable intake plus last validated player/role data. If no history exists, use documented position/salary/role population priors, with broad uncertainty; APPG remains unused. Solve and validate an initial full entry assignment before browsing or advanced simulation.
2. **Publish atomically:** retain immutable export bytes and their successful check. Enhancement writes another version; a failed step cannot overwrite the incumbent or leave a partial CSV at its public path.
3. **Degrade inputs:** stale optional source → cached observation with age → role/population prior. Missing odds → hockey model. Missing ownership → broad prior with weak leverage. LLM failure → skip the critique.
4. **Degrade search:** retain a valid solver incumbent on time limit. If the normal solver is unavailable, use a small dependency-light roster feasibility routine and deterministic ranking. Classic can search represented-team choices and minimum-cost eligible slot assignments; Showdown can enumerate CPT choices and legal FLEX completions. The fallback must handle duplicate people and arbitrary supplied eligibility, not greedily choose cheap players into a dead end.
5. **Relax only preferences:** drop salary-spend floors, stack shapes, pairwise uniqueness, then advisory exposure/thesis quotas as required. Preserve actual salary, identities, slots, teams, and locks. Record each relaxation. Repeating a legal lineup across reserved entries is a last-resort deliverable if necessary, with concentration explicitly reported.
6. **Retain prior evidence:** if optional QA or enhanced validation crashes, return the previously checked unchanged export. A separate tiny legality checker can assess repairs. Never stamp an unchecked new candidate valid just because the main validator failed.

**Limits that must be explicit:** corrupt/ambiguous authoritative IDs, contradictory entry geometry, no salary-feasible roster, or impossible locked slots cannot be repaired by relaxing strategy. If new news leaves no feasible replacement, retain the existing legal file and name any unavoidable nonplaying slot; do not claim it became safe or current. If no checked artifact has ever existed and all legality checking is broken, report that exact failure rather than manufacture a guarantee. These are boundary conditions, not permission for a network request or optional audit to strand an otherwise feasible portfolio.

## 11. News, goalie confirmation, and fast late swap

### Evidence and role updates

Rank reports by specificity, provenance, and time: explicit official/team or direct coach confirmation for the named game; attributed credentialed reporter observation; dated aggregator report; recent historical role. Multiple sites copying one report are one source. A later projected depth chart does not override an earlier explicit starter confirmation unless it contains new evidence.

Initial age policies: historical rates refreshed after the latest completed game; projected lines acceptable for modeling within 24 hours if no subsequent contrary news; a same-day practice/PP change triggers a refresh regardless of age. Inside 60 minutes to a team's start, stale/unattributed roles reduce confidence and prioritize that team for a check. Goalies remain `EXPECTED`, `CONFIRMED`, `OUT`, or `CONFLICTED`; fetching an old confirmation never changes its game/date.

Conflicts generate a role mixture and exposure warning until resolved. A player moving to PP1 changes projected PP time, teammates' shares, score distributions, and ownership—not simply their point estimate. Every LLM proposal contains the player/game IDs, old/new role, effective time, source URL/claim, confidence, and expiry; deterministic checks enforce team time/role constraints before acceptance.

### Goalies before confirmation

Estimate team starter probabilities from rotation, recent workloads, rest/back-to-backs, and explicit reports. Do not default every two-goalie team to certainty or infer certainty from the first depth-chart name. Sample one starter per team, with a separate small relief/pull process; conditional distributions and start-probability mixtures are distinct outputs.

Prefer high start-probability goalies with legal replacement paths. For every selected uncertain goalie, store affordable same-game/later-game alternatives and the smallest collateral skater changes needed. Confirmation collapses the starter mixture, updates opposing scoring rates and market consistency, and re-solves affected portfolio exposures. A late expensive replacement may require a two-player repair; keeping only a one-for-one list is insufficient.

### Fast mode

At initial construction, optimize slot placement for later flexibility: put a suitable later-starting skater in UTIL when equivalent eligibility allows, without moving any locked player later. Track times in UTC and display America/Chicago with the actual date; handle DST, schedule changes, and actual early starts.

For late swap, use the user's current DK entries export as the operational record of what is entered, compare it with the last delivered version, and record manual changes. Derive immutable locked slots from current DK information and conservative start-time checks. Never assume the last file generated was uploaded. If a refreshed entry export is unavailable, clearly state the assumed parent and preserve its locked cells; do not claim it reflects the live account.

Only fetch changed teams/news; reuse unaffected features and simulations. Pin locked cells exactly, prohibit adding started players, update remaining-game scenarios, and jointly reallocate the unlocked pieces. Locked exposures may already exceed caps; optimize residual exposure without pretending locked players can be removed. Recheck locks immediately before writing and preserve the predecessor if time crosses a lock boundary during computation.

When a current score/standing snapshot is supplied, condition remaining upside and payout thresholds on observed scores. A trailing entry may benefit from lower duplication and more variance; an ahead entry may favor reliable remaining opportunity. Do not make a “chase” pivot merely because a player has started slowly. Without reliable field scores, perform news/legality repair and use the original objective rather than inventing live standings.

Most ordinary NHL Showdown slates have one game: once it starts there is no useful remaining-player swap. The attached rules still lock each player by their game; pre-lock repair matters, and any unusual multi-game template must follow its actual game set. An unconfirmed goalie whose game has already begun cannot be rescued by late swap. Timely initial delivery and the final pregame check remain essential.

## 12. Learning loop: results, tuning gates, and backlog

Store before lock: raw-file hashes, as-of source claims, model/version, all-player forecasts and quantiles, ownership by contest/role, candidate/selected lineups, exposures, uncertainty scenarios, and build time. Store after lock: actual entry state where supplied, outcomes, standings revisions, payout evidence, and settle time. Never grade a prediction reconstructed using final news as if it were saved pre-lock.

Use one game outcome once when training player projections; multiple slates/contests reusing it are not new sporting outcomes. For ownership/field research, deduplicate exports and weight distinct slate/game-set groups, clustering uncertainty by date and overlapping games. Maintain per-mode and contest-family reporting; regular season, playoffs, and preseason are separate regimes.

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

Count floors separately for modes and label availability. If NHL volume makes a threshold slow, pool sensible coefficients and remain in prior/shadow mode; do not quietly lower the requirement. A single season may be insufficient for niche WTA or Showdown goalie-CPT claims.

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
| `.claude/skills/` | Thin skills for setup, run-slate, late-swap, settle, and development; procedures call deterministic commands. |
| `.claude/agents/` | `nhl-researcher` and `nhl-adversary`; explicit compact inputs, tools, output formats, no nested delegation. |
| `nhl.ps1`, `nhl.sh`, environment lockfile | Equivalent one-command launch paths for Windows and Linux; checked during setup. |

Skills can expose repeatable slash commands and consume arguments; keep their instructions short and delegate calculations to the engine. Use a named fresh-context agent definition for QA, with a read-only tool allowlist. Claude Code configuration is version-dependent, so setup must verify the installed version's behavior rather than blindly copying syntax from another project. [Skills documentation](https://code.claude.com/docs/en/skills)

`CLAUDE.md` should say: salary and entry bytes are authoritative; never invent IDs/news/payouts; always create the checked baseline first; never wait for optional QA to export; no live engine editing; no direct LLM CSV editing; at most three review rounds; use structured role changes; preserve locked slots; report honest model status; manual DK login/upload/entry/money actions; and keep source instructions isolated as untrusted data. Put detailed schemas in reference files instead of rereading the full architecture each slate.

Keep source code immutable during a live build. One writer per slate; local transaction/lock control prevents parallel runs overwriting the chosen export. Development, slate builds, and settlement are separate modes. Retain only a small index in SQLite; large historical tables/scenario arrays can live in compressed local files. Install and test dependencies once; daily generation must not depend on a package download.

**Proposed user-facing commands, available only after implementation:**

| When | Claude Code command | Deterministic equivalent to provide |
|---|---|---|
| Once | `/nhl-setup` | `.\nhl.ps1 setup` — pinned environment, source smoke tests, fixtures, offline fallback rehearsal |
| Normal slate | `/nhl-run "C:\path\DKSalaries.csv" "C:\path\DKEntries.csv"` | `.\nhl.ps1 run --salary "C:\path\DKSalaries.csv" --entries "C:\path\DKEntries.csv"` |
| Close to lock | `/nhl-run ... --fast` | Same run command with `--fast`; baseline/repair first, no optional critique |
| Goalie/news update | `/nhl-refresh <run-id>` | `.\nhl.ps1 refresh --run <run-id>` |
| After some games lock | `/nhl-late-swap <run-id> "C:\path\DKEntries-current.csv"` | `.\nhl.ps1 late-swap --run <run-id> --entries "C:\path\DKEntries-current.csv" --fast` |
| Independently verify | `/nhl-verify <run-id>` | `.\nhl.ps1 verify --run <run-id>` |
| After contests | `/nhl-settle <run-id> "C:\path\standings.zip"` | `.\nhl.ps1 settle --run <run-id> --standings "C:\path\standings.zip"` |
| Development session | `/nhl-dev-next` | Reads the one READY backlog item, implements/tests it separately, and updates its evidence/status. |

Normal operation should also accept plain language plus the two uploaded files. Slash commands make behavior repeatable; they should not become additional manual configuration work. The setup rehearsal must prove that the QA invocation is non-fork, sees only its intended briefing/shared project instructions, and cannot write exports.

## 14. Build phases, durations, and exit criteria

Estimates assume one focused developer using Claude Code, an ordinary Windows laptop, and accessible historical data. They include implementation and useful tests, not elapsed time waiting to accumulate live slates. Begin collecting prospective forecasts/results as soon as phase one exists.

| Phase | Adds | Exit criteria | Rough effort |
|---|---|---|---|
| 0 — Contracts and feasibility | Real Classic/Showdown salary/entry schema inspection; exact scoring; identity model; minimum referee; no-key source probes | Real-file round trips; correct CPT/FLEX IDs; all scoring thresholds and goalie edge cases; impossible inputs explained; no code borrowed without NHL tests | 1–2 working days |
| 1 — Smallest useful generator | Both modes; shrunk recent/season TOI and event-rate baseline; projected lines/goalies or explicit priors; simple coherent event scenarios; basic correlated candidates; full row assignment; CSV; offline fallback; locked-slot repair | Produces a sensible legal 20-entry portfolio with network/LLM unavailable; 150-entry benchmark; known scratches repaired when feasible; independent final-byte checks; first-file deadline met | 3–5 days |
| 2 — Reliable hockey features | NHL event/shift cache, situation TOI, attempts/quality, block/PP models, role changes, source conflicts, starter mixtures; daily historical updates | Reconciles historical games; no future leakage; compare with simple baseline on chronological holdout; missing history never drops eligible players | 4–6 days |
| 3 — Better joint outcomes | Segment-based game process, goal/assist allocation, saves/GA consistency, PP/SH, empty net/OT/shootouts, goalie pulls; market fit; uncertainty | Bonus and quantile calibration; feasible stat conservation; same-event correlations; separate development/selection/referee draws; runtime benchmark | 5–8 days |
| 4 — Field and portfolio economics | Ownership priors/learning, legal opponent sampler, exact duplicates/payouts/ties, fee-weighted frontier, alternate-core coverage, contest types | Field reproduces held-out construction; unknown payout data stays labeled; no false global-optimum claims; cash/WTA/satellite behavior differs correctly | 4–7 days |
| 5 — Controlled agents and operational hardening | Structured news extraction, independent adversary, ≤3-round controller, fast incremental updates, crash recovery, output race protection | Same baseline survives every optional failure; no accepted changes ends loop; fresh-context QA verified; late swap preserves current locked bytes; measured token/deadline budgets | 3–5 days |
| 6 — Measurement and guarded improvement | Standings/ownership grading, run-note/backlog automation, walk-forward experiments, promotion/rollback | Frozen pre-lock forecasts are graded; exact payouts required for cash/ROI labels; sample-size gates enforced; challenger shadow reports reproducible | 3–5 days plus ongoing data accumulation |

**Expected scale:** useful legal baseline in roughly one week; dependable role/news/late-swap workflow in roughly two weeks; fuller simulation/field/learning architecture in about five to eight working weeks, with some data work overlapping. Statistical validation takes longer than coding and may require much of a season. Phase one is usable without waiting for the final architecture.

High-value tests include scoring threshold boundaries and stacking of bonuses; goalie shutout with shootout versus shared-goalie game; negative goalie scores; CPT multiplier once; same person with two role IDs; Classic skater-team count excluding goalie; ambiguous names; mixed entry/help rows; all locked late-swap; expensive goalie replacement requiring collateral change; exact ties/duplicates; stale source cache; interrupted writes; dead network; solver timeout/import failure; malformed QA; and an optional step consuming the entire remaining deadline. Test actual behaviors and invariants, not every low-impact helper implementation.

Do not add a dashboard, native Excel requirement, complete historical paid odds archive, sophisticated passing tracker, deep neural model, multiple solver backends, or multi-agent development bureaucracy before these exit criteria pass.

## 15. Risks, unresolved details, and facts only the user can supply

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

**Facts only you can supply later; none blocks this plan:**

1. One representative Classic and Showdown salary/entries pair, plus a standings export for each, to verify real schemas and role-specific IDs.
2. Typical entry counts, fee range, contest mix, and whether exact field sizes/payouts or ticket rules can be saved alongside the entries.
3. An optional NHL bankroll/slate-spend limit and acceptable loss-risk preference; absent these, the engine treats purchased entries as fixed and reports tradeoffs without claiming bankroll optimization.
4. Your usual Claude Code host (Windows local versus cloud), approximate memory, and whether you can refresh/upload between staggered starts; these set measured runtime and swap-buffer policies.
5. Whether preseason is a regular target. Default: prioritize regular season, then validate playoffs; exhibitions retain a clearly labeled fallback until modeled separately.
