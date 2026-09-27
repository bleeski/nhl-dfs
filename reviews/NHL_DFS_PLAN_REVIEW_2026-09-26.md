# Design review: DraftKings NHL lineup generator plan

**Review of `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (25 September 2026) · reviewed 26 September 2026**

Basis: the plan, the two attached DK rule documents, and live checks of every load-bearing external claim (section 6). Nothing else in the nhl-dfs folder was opened. The independent checklist in section 1 was written and saved before the plan was read.

---

## 1. Independent checklist (written before reading the plan)

1. Legality is code, never the LLM. Classic: 9 slots (2 C, 3 W, 2 D, 1 UTIL, 1 G), $50,000 cap, skaters from at least 3 teams, UTIL excludes G. Showdown: 1 CPT + 5 FLEX, $50,000 cap, at least 1 player from each team, no person twice. Eligibility comes from the DK CSV's Roster Position column. Showdown CSVs list each person twice (CPT row at 1.5x salary, FLEX row) with different DK IDs, and the CPT slot must carry the CPT-row ID.
2. A no-network, no-LLM path must still produce a legal, uploadable portfolio from the salary CSV alone. Every enrichment (stats, lines, odds, news, ownership) degrades to that prior instead of aborting.
3. Output matches the DK bulk template exactly: DK IDs in each slot, entry IDs from the entries CSV, same column order, one row per entry, no locked or duplicate IDs.
4. Skater distributions fit the scoring: a low-rate high-value event layer (goal 8.5, assist 5, 3+ points bonus) on a high-rate low-value base (SOG 1.5, blocks 1.3, threshold bonuses at 5 SOG and 3 blocks). Right-skewed and lumpy; per-stat count models with simulation, not a point estimate times a multiplier.
5. Goalies are their own model: win +6, saves 0.7, GA -3.5, shutout +4, OTL +2, 35+ saves +3. Deeply negative floor, win tied to the moneyline, save volume tied to opponent shot rate. Confirmed starter is a gate.
6. Correlation is the ceiling lever: one goal pays up to 18.5 across a line or PP unit. Lineups are built from stack primitives using current line combinations; the goalie is anti-correlated with opposing skaters.
7. Ceiling is a simulated quantile of the correlated joint distribution, never a sum of individual ceilings.
8. Ownership is modeled, not sourced: salary, value, team total, PP1 and line status, recency. Validated against %Drafted from DK standings after each slate; validation error drives re-weighting, nothing else.
9. Vegas comes from a keyless free source with a fallback to team goal rates when down.
10. Every external source gets an existence check, a schema-drift detector, a last-good cache, and defined stale/down behavior.
11. Identity mapping across DK ID, NHL ID, and site names is explicit, with fuzzy fallback and a rule that an unmatched player still gets a prior rather than a null.
12. Leverage and duplication only exist relative to a field: expected duplicates and the payout curve. Without a field model, "avoid chalk" also fades correct chalk.
13. Portfolio allocation is explicit: exposure caps per player, stack, goalie, game; entries assigned by contest type; a stated rule for the tension between per-lineup tail and portfolio spread.
14. Code owns legality, projections, simulation, optimization, ownership, and file I/O. The LLM converts unstructured news into structured overrides and writes notes. It never assembles lineups.
15. Goalies and scratches confirm 30 to 90 minutes before puck drop, so the re-run is seconds, respects locked slots, and touches the LLM only for overrides.
16. QA is a separate context, bounded to 3 rounds with early stop, and emits structured edits that go back through the optimizer. Deterministic checks (captain diversity, stack coherence, exposure caps, both teams present) run in code before the LLM sees anything.
17. One slate is one sample. The learning loop calibrates projection bias and ownership error over many slates, never retunes per slate, and has minimum sample sizes.
18. Every Claude Code feature relied on (subagents, skills, slash commands, WebFetch) must exist and behave as described.
19. Phase 1 produces uploadable lineups on its own.

---

## 2. Verdict

**Ready to build after the fixes in section 7.** The plan is sound on legality, scoring, distribution shape, the never-fail ladder, the code/LLM split, and the learning-loop discipline. What it gets wrong is mostly about what it leaves on the table and what it builds in the wrong order: it treats DraftKings' own keyless contest and player-pool endpoints as unavailable, it scrapes an HTML odds board when the NHL API serves DraftKings odds as JSON, its token budgets are not real Claude Code numbers, and it builds a segment-level game simulator before the ownership and payout layers that the stated objectives depend on. None of that requires redesign. No finding would cause a failed generation or an illegal lineup.

---

## 3. Findings

### Blockers

None. The roster contracts (section 7, "Hard roster contracts") match both rule documents line for line, including the skater-only three-team test, UTIL as skater-only, Showdown both-teams, CPT as a role-specific ID, and no invented per-team or one-goalie limits. The fallback ladder (section 10) has no single point of failure between a valid salary CSV and a written file.

### Major

**M1. Contest metadata and player status are treated as unavailable when DraftKings serves both keylessly.**
*Plan says:* "Exact payout curve, field size, max entries, and contest type may be absent. Store missing metadata explicitly" (section 3); "When exact field/payout metadata is unavailable, use declared contest-family priors and label tail/coverage scores uncalibrated scenario proxies" (section 7); "Exact payout metadata is extra information beyond the two routine uploads" (section 15).
*Why it fails:* Both objectives are defined against payouts and field size (top-1% probability, tie-adjusted first-place share, probability of losing 80% of fees). Without them the plan's own text admits the metrics are proxies. The plan also routes scratch and injury detection through DailyFaceoff scraping and LLM research when the platform publishes a per-player status keyed by the exact DK ID.
*Evidence:* `https://api.draftkings.com/contests/v1/contests/{contestId}?format=json` returned, with no login or key, `payoutSummary` (every position range and cash value), `maximumEntries` 11764, `maximumEntriesPerUser` 150, `entryFee` 15, `entries` 1011, `draftGroupId`, `contestStartTime` for contest 195958011 (the contest ID is column C of the entries CSV). `https://api.draftkings.com/draftgroups/v1/draftgroups/{draftGroupId}/draftables?format=json` returned every player with `draftableId` (the salary CSV's ID), `salary`, `rosterSlotId`, `status` (observed values "OUT", "IR"), `isDisabled`, `isSwappable`, `newsStatus`, `draftAlerts`, and each competition's `startTime`. `https://www.draftkings.com/lobby/getcontests?sport=NHL` lists all contests with `dg` (draft group), `m` (field size), `mec` (max entries per user), `a` (fee), `po` (prize pool). Verified 2026-09-26.
*Fix:* Add a `dk_public` adapter in Phase 0 with three calls per slate: lobby (once), contest detail (once per distinct Contest ID in the entries CSV), draftables (once, then at T-60/T-15). Use payoutSummary and maximumEntries as the default field/payout inputs; keep contest-family priors only as the fallback when the call fails. Use draftables `status`/`isDisabled` as the first-class scratch signal (no name join needed) and treat DailyFaceoff injuries and LLM research as secondary. Same caveat the plan already applies to NHL endpoints: undocumented, read-only, low volume, cache the response, do not bypass any access control.

**M2. The only market input is an HTML scrape of Covers when the NHL API serves DraftKings odds as JSON.**
*Plan says:* "Moneylines and totals | Covers odds | Yes, current moneylines and dated odds board visible; totals view linked | ... Scraping, region, market delays ... Full automated totals ingestion remains an implementation acceptance item" (section 3). ESPN is "Unverified for live use".
*Why it fails:* The plan makes odds optional (correct), but the adapter it chose is the most fragile in the chain and the plan is not even confident totals can be ingested. Vegas is one of the four strategy aims.
*Evidence:* Covers is server-rendered today (a full row with moneylines and totals was extractable), so the plan's "Yes" holds. But `https://api-web.nhle.com/v1/partner-game/US/now` returned JSON with `currentOddsDate`, `lastUpdatedUTC`, `bettingPartner.name` "DraftKings", and per game 2-way and 3-way moneylines, puck line, and total (O/U 5.5 observed). Same host as the schedule endpoint the plan already depends on, and the odds are the same book Ben plays on. ESPN's scoreboard API had no `odds` object in preseason (unverified for the season, consistent with the plan).
*Fix:* Make `partner-game` the primary odds adapter, Covers the fallback, and keep the team-strength model as the last rung. Respect the plan's own rule that `lastUpdatedUTC` must postdate any goalie confirmation before the market is treated as current.

**M3. Token budgets are wrong by an order of magnitude and cannot be enforced from inside a run.**
*Plan says:* "Normal slate ... About 8k–16k input + 2k–4k output tokens total across orchestration, research, and one QA pass"; "Three-round maximum ... Hard planning ceiling about 30k input + 6k output tokens"; "Stop immediately when a round accepts zero changes, after round three, or when its deadline/token budget expires" (sections 8 and 9).
*Why it fails:* Claude Code re-sends the full conversation on every tool call (cached, but counted against plan limits), and a subagent invocation carries CLAUDE.md, the agent's own prompt, and the packet. A packet that "supplies actual IDs" for a 150-entry portfolio plus projections and ownership ranges for a 300-player pool is itself larger than the whole stated allowance. Nothing inside a skill or agent can read the session's token count to stop a loop, so "token budget expires" is not an implementable stop rule.
*Evidence:* Claude Code docs (`/docs/en/costs`): "Claude Code sends your full conversation with every request, and each time Claude uses tools it sends another request carrying that batch of tool results"; usage is visible only through `/usage` (session totals and subagent attribution) and cappable only session-wide via `--max-budget-usd`. No per-subagent or per-skill token cap exists. Non-fork subagents receive "CLAUDE.md files (user, project, local, managed policy)" in their initial context (`/docs/en/sub-agents`).
*Fix:* Replace token budgets with two enforceable bounds: a maximum number of LLM invocations per run (one research call, at most three adversary calls) and wall clock. Cap packets by construction: the adversary gets top-N exposures, stack and captain counts, flagged conflicts, and the candidate alternatives the code already ranked, not the full pool. Run the engine through the skill's `!`command`` preprocessing so only a summary of a few hundred tokens enters context. Measure real usage with `/usage` attribution after the first ten runs and write the observed numbers into the plan; drop the guesses.

**M4. The build order delivers the objectives last.**
*Plan says:* Phase 2 "Reliable hockey features" (4–6 days) and Phase 3 "Better joint outcomes" (segment-based game process, empty net, OT, shootouts, goalie pulls; 5–8 days) precede Phase 4 "Field and portfolio economics" (ownership, opponent sampler, duplicates, payouts, frontier; 4–7 days) (section 14).
*Why it fails:* Both stated objectives are field-relative. Leverage, duplication, fragile chalk, tie-adjusted first place, and loss-probability all need ownership and payouts. Under this order those arrive around week 4 or 5, while the two preceding weeks refine a simulator whose DFS-relevant correlations (line and PP co-scoring, goalie versus opposing offense, win/OTL) are already captured by the "simpler coherent aggregate model" the plan permits in Phase 1. The plan's own risk section agrees: "Ownership and field errors can dominate tiny simulated advantages."
*Evidence:* Section 5 ("phase one can use a simpler coherent aggregate model") and section 15 (quoted above), against the Phase table.
*Fix:* Reorder to 0 → 1 → 4-lite → 5-lite → 2 → 4-full → 3 → 6. "4-lite" is the ownership prior (section 6 inputs, hand-weighted), a duplicate-risk proxy, and payout-aware selection using M1's data; about two days. "5-lite" is DK status plus DailyFaceoff goalies, fast refresh, and late swap. Phase 3's segment simulator becomes a preregistered challenger under section 12's promotion rule rather than a prerequisite.

**M5. The QA loop cannot accept strategic changes until the field model is calibrated, so the leverage tie-break it is meant to perform never happens.**
*Plan says:* "For strategic changes, require ... no decrease in the predeclared tail metric ... and at least one gain larger than its Monte Carlo error/practical materiality threshold ... If evidence is inconclusive, keep the incumbent" (section 9); "Cold start: forecast ownership ranges, use only moderate leverage preference ... Never rank tiny projected differences as a known edge" (section 6).
*Why it fails:* The workflow's step 5 wants QA to prefer the much-lower-owned of two near-equal players. In the plan, that swap moves the simulated tail metric by less than Monte Carlo error (20,000 scenarios, about 200 draws past P99), and the ownership-driven gain only registers once the opponent field is fitted (30 or more slates by the plan's own floors). Until then every strategic proposal is "inconclusive," and the loop does only correctness repairs, which code already does. The three rounds cost time and tokens for no accepted change.
*Evidence:* Section 5's own Monte Carlo statement ("Twenty thousand scenarios give only about 200 observations beyond a nominal P99") combined with section 9's acceptance rule and section 12's ownership floors.
*Fix:* Put the tie-break in code: when two candidates (or two lineups for the same entry) are within the declared materiality band on the tail metric, prefer the lower projected ownership and lower duplicate-risk proxy, with a cap on how far ownership alone can move a choice. Restrict the LLM adversary to what code cannot check: conflicting news, thesis coherence, and overlooked hypotheses that change a model input. Default to one QA round; allow rounds two and three only when round one accepted a correctness repair.

**M6. The guaranteed baseline discards the best per-player prior in the file.**
*Plan says:* "Use ice time and event rates, not DraftKings `AvgPointsPerGame`, as the projection foundation" (decisions); "If no history exists, use documented position/salary/role population priors, with broad uncertainty; APPG remains unused" (section 10, ladder step 1).
*Why it fails:* The main-model decision is right. Extending it to the emergency baseline is not: on the night the NHL cache is empty or stale, a salary-bucket prior is strictly worse than the player's own realized DK average shrunk toward that bucket. APPG is also the number most of the field looks at, so excluding it from the ownership model removes a real popularity signal.
*Evidence:* The DK salary CSV carries `AvgPointsPerGame` per row (column set verified: Position, Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame). It is always present, offline, and keyed by the exact DK ID.
*Fix:* In ladder step 1, use APPG shrunk toward the position/salary prior (treat 0 as missing, since the CSV has no games-played column). Add APPG as an ownership-model feature. Keep it out of the rate model.

### Minor

**m1. The DailyFaceoff starting-goalies page renders client-side; the parse path is unverified.** Plan: "Yes, public page; full daily record coverage unverified" (section 3). A fetch of `/starting-goalies` returned the page chrome and the meta line "Today's starting goalies updated for September 26, 2026" but no goalie table; the team line-combination page did return full lines, PP units, goalies, injuries, and "Last updated: 2026-09-24T18:05:32.940Z". Phase 0's source probe must find the goalie data path (Next.js data route or embedded JSON) or fall back to per-team pages plus the DK draftables status.

**m2. Claude Code isolation needs two settings the plan does not name.** Plan: `nhl-adversary` has "no nested delegation" and the rehearsal "must prove that the QA invocation is non-fork" (section 13). Docs: "By default, a subagent can spawn subagents of its own, up to three layers below the main conversation"; nesting is removed by omitting `Agent` from the agent's `tools` allowlist or setting `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` to 1. Separately, a skill with `context: fork` runs in an isolated subagent that "doesn't see your conversation history," while an Agent-tool fork and `/subtask` inherit the parent. The rehearsal should assert both: the adversary's tool list is `Read` only, and the packet is the only slate content in its transcript.

**m3. Wall-clock targets ignore Claude Code's per-turn overhead.** Plan: "Fast refresh / late swap ... 10–30 seconds target"; "Stop optional work at T-5 minutes" (section 8). An engine that finishes in 20 seconds still takes one to two minutes end to end when invoked through a Claude Code turn (model reads the command, runs it, reads output, replies), plus the manual DK upload. Make `nhl.ps1 late-swap` in a terminal the documented default near lock, and set the T-5 stop at T-8 for anything routed through the LLM.

**m4. Same-name fixtures should be real NHL cases, and the standings join is name-only.** Plan: "Name ambiguity is never solved by selecting a convenient same-name athlete" (section 10). Recent seasons had two Elias Petterssons on Vancouver (C and D) and two Sebastian Ahos (CAR C, NYI D). The standings export's `Lineup` string and `Player` column carry names without IDs, so the settle step must disambiguate by the position token in the lineup string and by team, and must refuse when both candidates share position and team.

**m5. Initial DK-to-NHL crosswalk construction is unspecified.** Plan: "Unknown mappings use DK-only priors until resolved" (section 3). Specify the seed: for each team in the salary CSV, pull `api-web.nhle.com/v1/roster/{TEAM}/current`, match on normalized name plus position group, and require a human-accepted proposal for anything below an exact match, reusing the soccer crosswalk pattern the plan already cites.

---

## 4. Checklist gaps

| Checklist item | Plan status | Gap |
|---|---|---|
| 2. No-network fallback quality | Ladder exists | Uses salary-bucket priors when APPG is in the file (M6) |
| 9. Keyless Vegas with fallback | Covers scrape + team model | JSON source on a host already used exists (M2) |
| 12. Field model needs payouts and field size | Labeled "may be absent" | Available per Contest ID without a key (M1) |
| 15. Timing | Engine targets only | Claude Code turn overhead and terminal fast path not stated (m3) |
| 16. Deterministic checks before the LLM | Six audit criteria all posed to the LLM | Captain and exposure counts, both-teams, overlap, and scenario concentration are code checks; tie-break belongs in code (M5) |
| 18. Claude Code features | Correct on subagents, advisor, skills | Nesting default and fork naming (m2); token budgets not measurable in-loop (M3) |
| 11. Identity mapping | Principles stated | Seed procedure and real-collision fixtures missing (m4, m5) |
| 19. Phase 1 usable alone | Yes | Objectives wait until Phase 4 (M4) |

Items 1, 3, 4, 5, 6, 7, 8, 10, 13, 14, and 17 are handled as well as or better than the checklist asked.

---

## 5. What holds up

- **Decisions block:** every principle (shared discrete outcomes, no sum-of-ceilings, tail subject to a risk budget, caps as policies to test, free means no new accounts) is correct and should not be reopened.
- **Scoring contract (section 5 table):** matches both rule documents exactly, including goals counting as SOG, empty-net goals not charged, shootout goals not breaking shutouts, and CPT applied once to the full base score.
- **Hard roster contracts (section 7):** correct against both documents; no invented rules.
- **Participation, minutes, and rate estimation (section 5):** dressing and starter mixtures, manpower reconciliation, hierarchical shrinkage with stated prior weights, "usage change adjusts minutes and shares, not a fantasy multiplier."
- **Joint simulation principles:** seeded reproducibility, scorer and goalie failure as one event, no scalar stack bonus on top of simulated points, separate design/selection/referee draws, honest Monte Carlo error.
- **Vegas math:** paired-price vig removal, "total × win probability is not an implied team total," market staleness after goalie changes, capped influence.
- **Ownership accounting (section 6):** 900%/600% mass, CPT and FLEX mutually exclusive, MAE and calibration-by-band metrics, "missing is not zero," chronological splits, "hundreds of contests from one slate are not hundreds of observations."
- **Objective definitions (section 7):** tail mass against the same scenario's field, tie-adjusted first place for WTA, the four loss metrics, frontier plus declared risk budget, "still produce the legal file."
- **Portfolio policies:** feasibility check before any cap, `floor(cap × entries)`, small-portfolio captain handling, fee-weighted exposure, "compare simulated return dependence, not just roster overlap."
- **Contest treatment table:** satellites are not WTA, single-entry is an entry limit not a payout curve, unknown contests get labeled priors.
- **Solver handling:** HiGHS via SciPy `milp`, time limits, incumbent retention, and the status-code reading (2 is infeasible; 1, 3, 4 are not) is exactly what the SciPy docs say.
- **Code versus LLM table (section 8):** the split is right; the LLM never scores, enforces legality, or edits the CSV.
- **QA mechanics (section 9):** named fresh-context agent, read-only packet, structured proposals, controller accepts and re-solves, three stop rules, "fresh context reduces conversational bias; it does not provide an independent statistical model."
- **Intake and referee (section 10):** mode detection from headers, "the file governs" for CPT IDs and salaries, byte-level re-parse, integer score units in tenths and twentieths, orthogonal status fields, atomic publication.
- **Fallback ladder and explicit limits:** complete; relaxations are preferences only; "repeating a legal lineup across reserved entries is a last-resort deliverable."
- **News and goalies (section 11):** evidence ranking, one source copied many times is one source, four goalie states, replacement paths stored per uncertain goalie, "a late expensive replacement may require a two-player repair."
- **Late swap:** current DK export as the operational record, locked cells pinned, UTIL slot placement for later starts, no chase pivots without live standings.
- **Learning loop (section 12):** evidence floors, one game outcome counted once, preregistration and walk-forward, shadow before promotion, "promotion happens in a development session with tests, not during a live slate." This correctly reinterprets "the system tunes itself" as measure-propose-promote; see question 5.
- **Repo layout and CLAUDE.md contract (section 13):** one package, one writer per slate, no live engine edits, deterministic equivalents for every slash command.
- **Feature table (section 4):** the directional corrections (opponent CF/xGF for goalie workload, blocks as a continuous archetype, PP share as a spline, faceoffs dropped, no 15-day finishing window) are right and cheap.
- **Risk section (section 15):** accurate, including "late swap is not universal insurance" for single-game Showdown.

---

## 6. Verification log

| Claim | How checked | Result |
|---|---|---|
| NHL schedule endpoint is keyless JSON | Fetched `api-web.nhle.com/v1/schedule/now` | JSON; `gameWeek[].games[]` with `id`, `startTimeUTC`, `gameState`, teams; top-level `oddsPartners` key present |
| NHL box score is keyless JSON with the needed stats | Fetched `/v1/gamecenter/2025020001/boxscore` | Skater fields include `goals, assists, sog, blockedShots, toi, powerPlayGoals, shifts`; goalie fields include `saveShotsAgainst, goalsAgainst, toi, starter, decision, saves` |
| NHL shift charts are keyless JSON | Fetched `api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId=2025020001&limit=1` | JSON; `playerId, period, startTime, endTime, duration, teamAbbrev` |
| NHL API carries partner odds (not in plan) | Fetched `/v1/partner-game/US/now` | JSON; DraftKings moneyline (2- and 3-way), puck line, total per game; `lastUpdatedUTC` present |
| Covers odds page is scrapeable | Fetched `covers.com/sport/hockey/nhl/odds` | Server-rendered; a full PIT/BUF row with moneylines and O/U 6.0 was extractable |
| ESPN odds usable | Fetched `site.api.espn.com/.../hockey/nhl/scoreboard` | JSON but no `odds` object in preseason; unverified for the season (matches plan) |
| MoneyPuck free with limits | Fetched `moneypuck.com/data.htm` | "free to use for non-commercial purposes ... clearly credit MoneyPuck.com ... Non-approved scraping ... will be blocked"; season and game-level skater, goalie, line, team files and per-shot files with xG; nightly updates |
| Natural Stat Trick blocked by robots | Fetched robots.txt | Fetch refused: "URL is disallowed by robots.txt rules" (matches plan) |
| DailyFaceoff team lines page usable with timestamp | Fetched Toronto line-combinations page | Four lines, three pairs, PP1/PP2, PK, goalies, injuries, "Last updated: 2026-09-24T18:05:32.940Z" |
| DailyFaceoff starting-goalies page usable | Fetched `/starting-goalies` | No goalie table in server HTML; client-rendered (m1) |
| Left Wing Lock requires premium | Fetched `accessCheck.php` | "Access to Left Wing Lock site tools requires a premium account" |
| DK lobby endpoint keyless (not in plan) | Fetched `draftkings.com/lobby/getcontests?sport=NHL` | JSON; contests with `id, n, a, m, mec, po, dg, gameType`; NHL Classic and Showdown contests listed for 2026-09-29 |
| DK contest detail keyless (not in plan) | Fetched `api.draftkings.com/contests/v1/contests/195958011?format=json` | `payoutSummary` with per-position cash, `maximumEntries`, `maximumEntriesPerUser`, `entryFee`, `entries`, `draftGroupId`, `contestStartTime` |
| DK draftables keyless, includes status (not in plan) | Fetched `.../draftgroups/153977/draftables?format=json` | `draftableId, playerId, position, rosterSlotId, salary, status ("OUT","IR" observed), isDisabled, isSwappable, newsStatus, draftAlerts`, competition `startTime` |
| Showdown lists each person twice with a 1.5x CPT salary and separate IDs | Fetched draftables for Showdown group 153976 | Sam Reinhart: draftableId 44250352 slot 612 $15,300 and 44250267 slot 613 $10,200; exactly 1.5x; only slot IDs 612 and 613 in the group |
| DK salary CSV columns | dfs-with-r `coach` reader source | `Position, Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame` |
| DKEntries template format | runthesims bulk-entry doc | Columns `Entry ID, Contest Name, Contest ID, Entry Fee`, then roster slots; cells take `Name (ID)`; instructions and player list to the right; downloaded from Lineups → Edit Entries per slate |
| Standings export reports CPT and FLEX ownership separately | `dkownership` README | README states each player can appear twice, once as CPT and once as FLEX, each with its own %Drafted; columns Player, Roster Position, %Drafted |
| Custom subagents start with fresh context; tools allowlist; no nesting | Claude Code sub-agents doc | Fresh context confirmed; `tools`/`disallowedTools` confirmed; nesting allowed by default up to depth 3 (m2); CLAUDE.md is included in a subagent's context |
| Forks inherit the parent conversation | Same doc, plus skills doc | Agent-tool fork and `/subtask` inherit; a skill's `context: fork` is isolated (naming collision, m2) |
| `/advisor` exists and sees the full conversation | Claude Code advisor doc | Confirmed: experimental, Anthropic API only, "The advisor receives the full conversation"; the plan's characterization is accurate |
| Skills are slash commands with arguments | Claude Code skills doc | `/name args`, `$ARGUMENTS`, `disable-model-invocation`, and `!`cmd`` preprocessing confirmed |
| Token budgets measurable or cappable in-loop | Claude Code costs doc | Only `/usage` (session-wide) and `--max-budget-usd`; full conversation sent per request; no per-agent cap (M3) |
| SciPy `milp` status codes | SciPy docs | "0 Optimal, 1 Iteration or time limit, 2 Infeasible, 3 Unbounded, 4 Other"; `time_limit`, `mip_rel_gap`, `node_limit` options exist |
| Scoring and roster rules | Attached DK documents | All figures in the plan's section 5 table and section 7 contracts match |

Not checked: the three local repo reviews in section 1 and the vendor pages in section 2, because neither is load-bearing for the build and the repos were out of scope for this review.

---

## 7. Minimum change set

1. Add the `dk_public` adapter (lobby, contest detail, draftables) in Phase 0; make payoutSummary, maximumEntries, and draftables status first-class inputs with the existing priors as fallback (M1).
2. Swap the primary odds adapter to `api-web.nhle.com/v1/partner-game/US/now`; demote Covers to fallback (M2).
3. Replace token budgets with invocation counts and wall clock; cap packet contents by construction; route the engine through skill preprocessing; record measured `/usage` numbers after ten runs (M3).
4. Reorder phases to 0 → 1 → 4-lite → 5-lite → 2 → 4-full → 3 → 6, with the segment simulator as a preregistered challenger (M4).
5. Move the leverage tie-break and the six audit criteria that code can check into deterministic code; default to one QA round (M5).
6. Use shrunk APPG in ladder step 1 and as an ownership feature (M6).
7. Add to the Phase 0 exit criteria: a working DailyFaceoff goalie parse path or a documented fallback (m1); adversary agent `tools: Read` with a nesting assertion (m2); Pettersson/Aho fixtures for the settle join (m4); a roster-endpoint crosswalk seed (m5); terminal fast path documented as the near-lock default (m3).

---

## 8. Improvement ideas (ranked by impact relative to cost)

**1. Use DraftKings' own keyless endpoints for payouts, field size, entry limits, lock times, and player status.**
Serves: both objectives (exact payout curve and field size make the tail and loss metrics real) and reliability (scratch signal keyed by DK ID, no name join, no LLM). Why it works: verified above; three small JSON calls per slate. Cost: half a day; runtime negligible; zero tokens. Failure risk: endpoint changes; the plan's contest-family priors remain as fallback, so generation is unaffected. Constraints: free, no key, runs in Python. Phase 0.

**2. Take odds from the NHL partner-game endpoint.**
Serves: "use Vegas where they help." Why: verified JSON with timestamp, same book as the contests, same host already in the dependency chain. Cost: two hours. Failure risk: low; Covers and the team model remain below it. Phase 1.

**3. Build 4-lite before the segment simulator.**
Serves: objective 1 directly (leverage and duplication are field-relative) and cost. Content: hand-weighted ownership prior from section 6's inputs plus APPG, scaled to 900%/600% mass; duplicate-risk proxy (sum of log ownership, salary-left bucket, captain popularity); payout-aware selection using idea 1. Why: the plan's own risk section says field errors dominate simulator refinements. Cost: about two days moved earlier, no new work. Failure risk: none added. Phase order change.

**4. Make MoneyPuck the primary historical feature source and the NHL API the recency and identity source.**
Serves: cost. MoneyPuck's season and game-level skater, goalie, line, and team files already carry situation-split TOI, shots, xG, on-ice rates, and line combinations; its per-shot files replace thousands of play-by-play and shift-chart requests. Phase 2 shrinks from 4–6 days to about 2, and the shot-quality model becomes a consumer of a published xG rather than a local build. Cost: one day for the adapter and definitions. Failure risk: MoneyPuck's terms are non-commercial with attribution and "non-approved scraping ... will be blocked," so use only the listed downloads and keep the NHL-derived path as the plan already specifies. Ben decides whether personal DFS use fits the terms (question 6). Phase 2.

**5. Deterministic leverage tie-break and code-first QA.**
Serves: objective 1 in cold start; token cost. Why: M5. Within the materiality band on the tail metric, prefer lower projected ownership and lower duplicate risk, bounded; run captain counts, exposure caps, both-teams, overlap, and shared-failure-event concentration as assertions before the adversary is invoked; restrict the adversary to conflicts, theses, and model-input hypotheses. Cost: one day. Failure risk: none to generation. Phase 1 (tie-break) and Phase 5 (QA scope).

**6. Unify the ownership model and the opponent field sampler.**
Serves: objective 1 and simplicity. Instead of fitting player utilities inside a legal lineup sampler and separately maintaining a behavior-mixture field, generate the field with one noisy optimizer (projection plus noise, a mixture of stack rules, captain heuristics, and salary-left preferences) and read marginal ownership, duplicate counts, and construction frequencies off the sampled field. Calibrate the noise scale and mixture weights to observed %Drafted and observed duplicates. One object, one calibration target. Cost: replaces Phase 4 work rather than adding to it. Failure risk: none to generation; the field is an evaluation input only. Phase 4.

**7. APPG as a shrinkage component and popularity feature.**
Serves: never-fail baseline quality and ownership accuracy. Cost: an hour. Phase 1.

**8. Start prospective capture now, before the engine exists.**
Serves: the learning loop, which the plan says "may require much of a season." A 50-line script on Windows Task Scheduler snapshots DailyFaceoff lines and goalies, partner odds, the DK lobby, and draftables for that day's draft groups at fixed times (morning, T-60, T-15 per game) into `data/raw/` with fetch timestamps. By the time Phase 4 exists there are weeks of as-of odds, lines, and player-status history that cannot be backfilled. Cost: half a day. Failure risk: none; it writes files. Not an agent, no Claude Code involvement. Phase 0.

**9. Run the engine through skill preprocessing and keep the LLM out of the fast path.**
Serves: token and time constraints. The `run-slate` skill uses `!`nhl.ps1 run ...`` so the engine runs before the model sees anything and only the manifest summary enters context; the model's job is to read the summary, decide whether the one research call is needed, and report. Late swap and refresh default to the terminal command. Cost: design choice, no extra work. Phase 5.

---

## 9. Questions only Ben can answer

1. Do you have DK NHL standings exports from any prior season? If yes, the ownership prior and duplicate model can be fitted before slate one instead of after 30 slates.
2. What is the risk budget the frontier should select against: an acceptable probability of losing 80% or more of a slate's fees, a maximum share of fees riding on one goalie or one game, or something else?
3. Typical slate: how many entries, the fee range, and the mix of 150-max GPPs, WTA, single-entry, and Showdown. This decides which contest families the field model is calibrated for first.
4. Claude Code runs locally on the Windows laptop (the plan assumes so)? And are you willing to run `nhl.ps1 late-swap` from a terminal near lock, or must every action go through a slash command?
5. Workflow step 7 says "the system tunes itself." The plan reinterprets that as measure, propose, and promote in a development session behind evidence floors. Is that acceptable, or do you want any parameter (for example the ownership prior weights) to update automatically between slates?
6. MoneyPuck's terms are non-commercial with attribution. Are you comfortable using its listed downloads for personal DFS, or should the NHL-derived path be the only historical source?
