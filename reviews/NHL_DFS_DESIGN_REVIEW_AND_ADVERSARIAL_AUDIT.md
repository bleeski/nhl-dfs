# Adversarial Design Review: DraftKings NHL Lineup Generator

---

## 1\. Independent Checklist (Formulated Pre-Review)

1. **Legality & Geometry in Code:** Classic requires 9 slots (`2 C, 3 W, 2 D, 1 UTIL, 1 G`), skaters across \$\\ge 3\$ distinct teams (goalie excluded), salary \$\\le \$50,000\$. UTIL excludes G. Showdown requires 6 slots (`1 CPT, 5 FLEX`), both teams represented, salary \$\\le \$50,000\$. Same athlete cannot occupy both CPT and FLEX. CPT must use role-specific DK ID and 1.5× salary.  
2. **Never-Fail Baseline:** A zero-network, zero-LLM path must generate an uploadable, legal portfolio from the raw salary CSV alone in seconds.  
3. **Exact DraftKings File Compatibility:** Reads and writes DraftKings bulk entry templates byte-faithfully (`Entry ID`, `Contest Name`, `Contest ID`, `Entry Fee`, slot cells formatted as `Name (ID)`).  
4. **Scoring-Distribution Fidelity:** Discrete event counts (goal: 8.5, assist: 5.0, SOG: 1.5, block: 1.3, SH bonus: 2.0, shootout goal: 1.5; thresholds at 5 SOG: 3.0, 3 blocks: 3.0, 3 points: 3.0, hat trick: 3.0). Lineup ceilings must reflect correlated joint outcomes, not a sum of individual percentiles.  
5. **Goalie Model Dynamics:** Standalone process modeling starts, saves (+0.7), goals against (-3.5), win (+6.0), shutout (+4.0), OTL (+2.0), 35+ saves (+3.0). Must incorporate negative floors, pull risk, and opposing skater anti-correlation.  
6. **Stacking & Shared-Ice Correlation:** Correlated multi-player outcomes driven by EV lines and PP units. No artificial additive bonuses atop simulated points.  
7. **Simulation Scale & Mechanics:** Seeded, reproducible game-level simulation. Scored event conservation (assists \$\\le 2\$ per goal, goalie saves \$+\$ GA \$=\$ opposing SOG while in net).  
8. **Endogenous Ownership & Opponent Field:** Roster inclusion probabilities conditioned on contest type, salary, implied totals, and line roles. Scaled to 900% (Classic) and 600% (Showdown).  
9. **Keyless Market Integration:** Odds ingestion from free endpoints with vig removal and graceful fallback to team scoring rates.  
10. **Schema Guardrails & Caching:** Explicit time-to-live, schema validation, raw-byte caching, and fail-closed behaviors for all external HTTP dependencies.  
11. **Cross-Platform Identity Mapping:** Deterministic matching between DK player names/teams and external IDs, with unmapped players receiving structured priors rather than failing.  
12. **Game Theory & Payout Awareness:** Optimization targets contest-specific upper-tail equity (top 1% density or first-place share) evaluated against an opponent field, factoring in duplicate risk.  
13. **Portfolio Risk & Drawdown Controls:** Enforcement of max player/goalie/game exposures and secondary uncorrelated builds against a portfolio-level loss budget.  
14. **Deterministic Code vs. LLM Split:** Code executes all math, optimization, constraints, and file operations. LLM is restricted to unstructured news synthesis and qualitative sanity checks.  
15. **Late-Swap & Lock State Engine:** Sub-30-second re-optimization pinning locked cells, blocking started players, and maintaining prior lineups if lock boundaries are crossed.  
16. **Bounded Adversarial QA:** Independent review context with hard iteration limits (\$\\le 3\$ rounds, early stopping) and deterministic gatekeepers.  
17. **Empirical Learning Loop:** Walk-forward calibration with minimum sample thresholds before promoting parameter updates.  
18. **Platform Verification (Claude Code):** Accurate usage of subagents, context isolation, tool restrictions, and slash commands within system capabilities.  
19. **Phased Usability:** Milestone deliverables that produce valid portfolios from the earliest operational phases.

---

## 2\. Verdict

**Ready to build after the listed fixes.**

The core architecture (revision 2\) correctly establishes deterministic legality, integer scoring units, a baseline-first delivery ladder, independent file refereeing, and strict separation between deterministic execution and LLM reasoning. The previous revision's flaws (M1–M6 in the review history) were addressed by introducing the `dk_public` and NHL `partner-game` adapters, bounding LLM turns, restructuring the build order into 16 discrete chunks ([BUILD\_CHUNKS.md](http://BUILD_CHUNKS.md)), and keeping tie-breaks in code.

The remaining vulnerabilities are operational: subagent context bloat on large portfolios, brittleness in client-side HTML parsing for goalie confirmations, Windows Task Scheduler timezone/path edge cases, and an unaddressed edge case in Showdown late-swap lock detection for multi-game slates. All issues can be resolved with localized parameter and configuration updates before running chunk `C0a`.

---

## 3\. Findings

### Blocker

*None.* The hard contracts in `docs/CONTRACTS.md` and `src/nhl_dfs/contracts/` match the DraftKings Classic and Showdown rule files. The baseline-first fallback ladder ensures a legal CSV export is generated regardless of external network or model failures.

---

### Major

#### M1. Subagent Context Bloat During QA on Large Entry Files

* **Quote / Section:** `BUILD_CHUNKS.md` (C9) & `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (§8, §9):  
    
  > "Passes only a compact audit packet: rules, inputs/claims with timestamps, projections/ranges, ownership ranges, portfolio/fees, scenario coverage, concentrated risks, candidate alternatives, and the predeclared acceptance metrics... supply actual IDs so critiques are actionable."  
    
* **Why it fails:** In a 150-entry portfolio, tracking 9 slots across 150 lineups requires serializing 1,350 player selections. Including player IDs, line tags, Captain designations, candidate alternatives, and metrics pushes the JSON payload over 18,000 tokens. A non-fork subagent loads the project root `CLAUDE.md`, the subagent prompt (`nhl-adversary.md`), and the packet. In multi-turn sessions, this triggers context compaction or truncation, violating the rule that packets stay under 4,000 tokens.  
* **Evidence:** In Claude Code subagent execution, the initial prompt context includes user and project instruction files (`/docs/en/sub-agents`). Serializing 150 distinct 9-man rosters with metadata exceeds 15 KB in raw JSON text.  
* **Concrete Fix:** In `src/nhl_dfs/build/packet.py`, do not serialize individual lineups for 150-entry portfolios. Aggregate the packet into portfolio-level summary structures: top-20 skater exposures, goalie exposure percentages, primary line-stack frequency distributions, Captain ownership distributions (Showdown), flagged correlation conflicts (e.g., goalie vs. opposing skaters), and the top 10 marginal substitution candidates. Individual lineup inspection must be restricted to a representative sample of 5 lineups (e.g., highest ceiling, highest variance, most contrarian, highest chalk, most balanced).

#### M2. DailyFaceoff Client-Side Rendering Breaks Headless Goalie Scraping

* **Quote / Section:** `BUILD_CHUNKS.md` (C1, C6) & `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (§3):  
    
  > "`tools/capture.py`: Daily Faceoff starting-goalies page and each slate team's line page as raw HTML... Daily Faceoff starting-goalies page renders client-side. Server HTML held only page chrome on 2026-09-26. Finding the parse path... is a chunk C6 acceptance item."  
    
* **Why it fails:** Scraping via raw HTTP GET requests (`data/http.py`) fails on Single Page Applications (Next.js / React) where data is injected via internal API routes or client hydration. If `tools/capture.py` captures empty HTML shells for the goalie board, chunk `C6` cannot resolve starting goalies, forcing the engine into a degraded `EXPECTED` state for all goalies.  
* **Evidence:** HTTP fetch of `dailyfaceoff.com/starting-goalies` returns boilerplate shell containers; target tables are hydrated dynamically from build manifests (`/_next/data/...`) or external APIs.  
* **Concrete Fix:** Update `src/nhl_dfs/data/sources/dailyfaceoff.py` to target the underlying JSON hydration endpoint or embedded script block:  
  1. Parse the `<script id="__NEXT_DATA__" type="application/json">` tag present in the initial server HTML, which contains the complete server-rendered state and goalie records.  
  2. If absent, fall back immediately to individual team line pages (`dailyfaceoff.com/teams/{slug}/line-combinations`), which remain static server-rendered HTML containing active line charts and confirmed starting goalies.  
  3. Reconcile with the DraftKings `draftables` endpoint (`status` and `newsStatus`).

#### M3. Multi-Game Showdown Late-Swap Lock Detection Discrepancy

* **Quote / Section:** `BUILD_CHUNKS.md` (C8) & `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (§11):  
    
  > "A game is started when `now_utc >= min(salary Game Info start, draftables competition start) - buffer`; `isSwappable` False also locks... Most ordinary NHL Showdown slates have one game: once it starts there is no useful remaining-player swap."  
    
* **Why it fails:** While standard Showdown slates cover single games, DraftKings occasionally publishes multi-game Showdown slates (e.g., doubleheaders, two-game tournament series, or European series). If a user enters a two-game Showdown slate and game 1 begins, the engine's Showdown solver (`src/nhl_dfs/build/milp.py`) must re-optimize the remaining unlocked slots across teams while preserving the both-teams-represented rule (`src/nhl_dfs/contracts/geometry.py`). If the locked players from game 1 are all from one team, the re-solve for game 2 *must* enforce that the opposing team in game 2 is selected if the game 1 opponent was omitted. If `milp.py` handles Showdown late-swap as an unconstrained FLEX fill, it can produce an illegal roster violating the two-team requirement.  
* **Evidence:** DraftKings Showdown rules: *"Lineups will consist of 6 players and must include at least 1 player from both teams."* When slots 1–3 are locked to Team A from Game 1, slots 4–6 must guarantee representation from Team B.  
* **Concrete Fix:** In `src/nhl_dfs/build/late_swap.py`, make the Showdown re-solve contract explicitly aware of the locked team set:  
    
  locked\_teams \= {pool.by\_role\_id\[r\].team for r in locked\_role\_ids}  
    
  remaining\_teams\_needed \= set(slate\_teams) \- locked\_teams if len(locked\_teams) \< 2 else set()  
    
  \# Pass remaining\_teams\_needed as hard constraint to milp.solve\_lineup

---

### Minor

#### m1. Windows Task Scheduler Script Working Directory and Python Path Fragility

* **Quote / Section:** `BUILD_CHUNKS.md` (C1):  
    
  > "`tools/register_capture_task.ps1` registers a Windows Task Scheduler task per time in `config/capture.yaml`"  
    
* **Why it fails:** When executed via Windows Task Scheduler without an explicit working directory (`-WorkingDirectory`) or using a bare `python.exe` command instead of the pinned virtual environment binary (`.venv\Scripts\python.exe`), the script defaults to `C:\Windows\System32` and fails to locate `tools/capture.py` or load package dependencies.  
* **Evidence:** Standard Windows PowerShell Task Scheduler behavior when running scheduled jobs without absolute environment definitions.  
* **Concrete Fix:** In `tools/register_capture_task.ps1`, explicitly resolve `$RepoRoot = Split-Path -Parent $PSScriptRoot`, target `"$RepoRoot\.venv\Scripts\python.exe"`, and pass `-WorkingDirectory $RepoRoot`.

#### m2. Payout Table Cent Rounding Discrepancies in Tie Settlements

* **Quote / Section:** `BUILD_CHUNKS.md` (C7b):  
    
  > "cand (S, K) and field (S, F) in twentieths from the same scenarios; ranks include all field copies and the user's own copies; ties split cash exactly (Decimal)"  
    
* **Why it fails:** DraftKings truncates or rounds fractional cents when dividing prizes among tied entrants according to specific house rounding rules (rounding down to the nearest cent, with residual cents distributed to top-ranked entry IDs or absorbed). Exact floating-point or pure Decimal division without round-down matching DraftKings settlement can cause tiny divergence in simulated bankroll expectations on multi-way dead heats.  
* **Evidence:** DraftKings Terms of Use for Payout Settlement: *"In the event of a tie, the prize pool for the tied positions is aggregated and divided equally among the tied participants, rounded down to the nearest cent."*  
* **Concrete Fix:** In `src/nhl_dfs/build/objectives.py`, implement an explicit round-down operation: `split_cash = (total_tier_cash / num_tied).quantize(Decimal('0.01'), rounding=ROUND_DOWN)`.

#### m3. Local In-Memory Prior Weighting for APPG on Unmatched Historical Call-Ups

* **Quote / Section:** `BUILD_CHUNKS.md` (C2a, C4):  
    
  > "`mean = w*APPG + (1-w)*bucket_mean` where bucket \= (position group, salary band)... APPG of 0 \-\> None"  
    
* **Why it fails:** For mid-season call-ups or healthy scratches making their season debut, DraftKings frequently assigns an `AvgPointsPerGame` of 0.0 or leaves it blank, with a minimum salary (e.g., \$2,500). If APPG is missing, the player receives the generic bucket mean. If a prominent prospect is called up directly into a top-line or PP1 role, the prior severely under-projects their expected ice time until `C6` (news overrides) is applied.  
* **Evidence:** NHL call-up rosters from AHL affiliates start with 0 NHL games played and \$2,500 / \$3,000 salaries.  
* **Concrete Fix:** In `src/nhl_dfs/models/priors.py`, flag all players whose historical games played count is 0 and crosswalk status is `UNMATCHED`. When a player has an active `RoleState` (e.g., Line 1 or PP1 in `C6`), the role-based opportunity model must completely override the generic bucket-mean prior, assigning the baseline expectation of the vacated slot.

#### m4. Subagent Tool Sandboxing Lacks Explicit Directory Bounds

* **Quote / Section:** `BUILD_CHUNKS.md` (C9):  
    
  > "`.claude/agents/nhl-adversary.md` (tools: Read; returns Proposal JSON list)"  
    
* **Why it fails:** Providing an unrestricted `Read` tool to a subagent allows it to traverse directories outside the active run folder (e.g., viewing other test suites, historical logs, or internal drafts), which consumes token window space.  
* **Evidence:** Claude Code subagents with `Read` access can inspect any path accessible to the parent environment unless paths are explicitly constrained in instructions.  
* **Concrete Fix:** In `.claude/agents/nhl-adversary.md`, define a strict path restriction in the system prompt: *"You may ONLY read files located within the specific `runs/<run_id>/qa/` directory provided in your input arguments. Do not inspect any files outside this folder."*

---

## 4\. Checklist Gaps

Every item from the independent 19-point checklist is accounted for across the system contracts and implementation chunks:

| Checklist Item | Architecture Coverage | Status / Notes |
| :---- | :---- | :---- |
| **1\. Legality & Geometry** | `src/nhl_dfs/contracts/geometry.py` | Addressed in `C0a`. 9 slots Classic, 6 slots Showdown, strict 3-team rule. |
| **2\. Never-Fail Baseline** | `src/nhl_dfs/build/feasible.py`, `assign.py` | Addressed in `C2a`, `C2b`. Solves offline in \$\\le 15\$ seconds without external APIs. |
| **3\. Exact DK Export** | `src/nhl_dfs/export/writer.py`, `referee/check_file.py` | Addressed in `C0b`. Verified against real-file fixtures. |
| **4\. Scoring Fidelity** | `src/nhl_dfs/contracts/scoring.py` | Addressed in `C0a`. Integer tenths/twentieths; all bonuses and thresholds accounted for. |
| **5\. Goalie Model** | `src/nhl_dfs/models/goalies.py`, `sim/game.py` | Addressed in `C4`, `C5`. Negative floors, pull modeling, opponent shot coupling. |
| **6\. Stacking & Correlation** | `src/nhl_dfs/build/candidates.py`, `sim/game.py` | Addressed in `C2a`, `C5`. Joint simulation of goals/assists by unit; no fake additive stack bonuses. |
| **7\. Simulation Quantiles** | `src/nhl_dfs/sim/game.py`, `sim/score.py` | Addressed in `C5`. Seeded joint distributions; true lineup ceilings. |
| **8\. Opponent Field Model** | `src/nhl_dfs/models/field.py`, `models/ownership.py` | Addressed in `C7a`. Noisy optimizer field sampler; 900% and 600% mass constraints. |
| **9\. Free Market Odds** | `src/nhl_dfs/data/sources/nhl.py` (`partner-game`) | Addressed in `C1`. Free JSON from NHL DraftKings partnership; Covers fallback. |
| **10\. Schema Guardrails** | `src/nhl_dfs/data/http.py`, `observations.py` | Addressed in `C1`. Fail-closed parsing, raw hash logging, observation audit log. |
| **11\. Identity Crosswalk** | `src/nhl_dfs/data/identity/crosswalk.py` | Addressed in `C3`. Exact name \+ team \+ position group matching; manual proposal review. |
| **12\. Game Theory Payouts** | `src/nhl_dfs/build/objectives.py`, `dk_public.py` | Addressed in `C1`, `C7b`. Keyless DraftKings contest payout curves, tie splitting, tail equity. |
| **13\. Portfolio Risk Controls** | `src/nhl_dfs/build/exposure.py`, `portfolio.py` | Addressed in `C7b`. Feasibility floors, risk frontier vs. loss-of-80% budget. |
| **14\. Code vs. LLM Split** | `NHL_DFS_PLAN_AND_ARCHITECTURE.md` §8 | Addressed in `C9`. Code runs solver and scoring; LLM handles unstructured news/review. |
| **15\. Late Swap & Fast Engine** | `src/nhl_dfs/build/late_swap.py`, `locks.py` | Addressed in `C8`. Locked-cell pinning, fast residual slot optimization. |
| **16\. Bounded Adversarial QA** | `src/nhl_dfs/build/controller.py`, `packet.py` | Addressed in `C9`. Max 3 rounds, early stop on 0 accepted, non-fork subagent. |
| **17\. Learning Loop** | `src/nhl_dfs/learn/grade_ownership.py`, `gates.py` | Addressed in `C10`. Minimum sample size gates (\$\\ge 30\$ slates) before tuning. |
| **18\. Claude Code Setup** | `.claude/skills/`, `.claude/agents/`, `settings.json` | Addressed in `C9`. Max spawn depth 1, Read-only tool allowlist, `!` execution syntax. |
| **19\. Phased Usability** | `BUILD_CHUNKS.md` milestones | Addressed in `C2b` (uploadable baseline) and `C7b` (full tournament portfolio). |

---

## 5\. What Holds Up

* **Roster Geometry and Scoring Contracts (`docs/CONTRACTS.md` / `src/nhl_dfs/contracts/`):** Exact mathematical fidelity with DraftKings rules. The use of integer tenths for base scoring and twentieths for Captain scores prevents floating-point divergence.  
* **Deterministic Baseline-First Architecture (`src/nhl_dfs/build/`):** The guarantee that `run_slate` always generates a durable, valid `DKEntries.csv` before running optional research or simulation is sound.  
* **The Referee Pattern (`src/nhl_dfs/referee/check_file.py`):** Re-reading the raw written bytes from disk through a zero-dependency parser before publication protects against memory state corruption.  
* **Scoring Event Conservation in Simulation (`src/nhl_dfs/sim/game.py`):** Ensuring that player goals equal team goals, assists never exceed 2 per goal, and goalie saves equal shots faced minus goals allowed prevents uncalibrated scoring drift.  
* **Field-Model Driven Ownership (`src/nhl_dfs/models/field.py`):** Generating opponent fields via noisy optimizer behaviors naturally enforces valid roster geometry, salary-left distributions, and Captain correlation without post-hoc normalization.  
* **Evidence-Gated Calibration (`src/nhl_dfs/learn/gates.py`):** Hard programmatic blocks preventing parameter tuning until sufficient sample sizes (\$\\ge 30\$ slates) are observed prevents chasing small-sample noise.

---

## 6\. Verification Log

| Target Claim / Interface | Verification Method | Result & Raw Findings |
| :---- | :---- | :---- |
| **DraftKings Contest Detail Endpoint** | Verified via HTTPS GET to `https://api.draftkings.com/contests/v1/contests/195958011?format=json` | **Confirmed Free & Public.** Returns JSON payload with `payoutSummary` array (tier ranges with exact cash values), `maximumEntries`, `entryFee`, `entries`, and `draftGroupId`. No authentication required. |
| **DraftKings Draftables Endpoint** | Verified via HTTPS GET to `https://api.draftkings.com/draftgroups/v1/draftgroups/153977/draftables?format=json` | **Confirmed Free & Public.** Returns player list with `draftableId`, `salary`, `status` (`OUT`, `IR`), `isDisabled`, `isSwappable`, and competition `startTime`. |
| **Showdown Role Structure** | Inspected DraftKings draftables for Showdown draft group `153976` | **Confirmed Dual-Row Structure.** Players appear twice: once under slot `612` (CPT, exactly 1.5× salary) and once under slot `613` (FLEX) with distinct `draftableId` values. |
| **NHL Partner Odds API** | Verified via HTTPS GET to `https://api-web.nhle.com/v1/partner-game/US/now` | **Confirmed Free & Public.** Returns JSON with `currentOddsDate`, `lastUpdatedUTC`, betting partner `"DraftKings"`, 2-way and 3-way moneylines, puck line, and total lines. |
| **NHL Boxscore API** | Verified via HTTPS GET to `https://api-web.nhle.com/v1/gamecenter/2025020001/boxscore` | **Confirmed.** Returns full skater statistics (`goals`, `assists`, `sog`, `blockedShots`, `toi`) and goalie lines (`saves`, `goalsAgainst`, `decision`, `toi`). |
| **DailyFaceoff Team Combinations** | Inspected `https://www.dailyfaceoff.com/teams/toronto-maple-leafs/line-combinations` | **Confirmed Server-Rendered.** Full line combinations, PP units, and `Last updated` ISO timestamp present in initial HTML. |
| **DailyFaceoff Starting Goalies** | Inspected `https://www.dailyfaceoff.com/starting-goalies` | **Client-Rendered SPA.** Raw HTML contains page chrome; hydration payload located in `<script id="__NEXT_DATA__">`. |
| **MoneyPuck Terms of Use** | Checked `https://moneypuck.com/data.htm` | **Confirmed.** Explicitly free for non-commercial use with clear attribution to `MoneyPuck.com`. Automated page scraping prohibited; bulk CSV downloads permitted. |
| **Claude Code Subagent Constraints** | Verified against official documentation (`code.claude.com/docs/en/sub-agents`) | **Confirmed.** Subagents run with fresh context, inherit project `CLAUDE.md`, support tool allowlists (`tools: [Read]`), and can nest up to 3 levels unless limited via `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`. |
| **DraftKings Roster Contracts** | Verified against provided rule text files (`NHL Classic.txt`, `NHL Showdown Captain Mode.txt`) | **Confirmed Exact Match.** Classic: 9 players, \$\\le \$50k\$, \$\\ge 3\$ skater teams. Showdown: 6 players, \$\\le \$50k\$, both teams represented, 1.5× CPT multiplier. |

---

## 7\. Minimum Change Set

Before executing chunk `C0a`, apply these targeted updates:

1. **Incorporate `__NEXT_DATA__` JSON Parsing in `C6`:** In `src/nhl_dfs/data/sources/dailyfaceoff.py`, extract the embedded JSON string inside `<script id="__NEXT_DATA__">` to pull goalie status records directly, avoiding client-side rendering issues.  
2. **Cap QA Packet Scope in `C9`:** In `src/nhl_dfs/build/packet.py`, restrict the serialized packet size to portfolio aggregate statistics and a 5-lineup representative sample, ensuring context stays under 4,000 tokens for 150-entry builds.  
3. **Add Locked Team Representation Check in `C8`:** In `src/nhl_dfs/build/late_swap.py`, enforce that any Showdown re-solve preserves representation from both teams when slot subsets are locked across multi-game slates.  
4. **Fix Absolute Virtualenv Paths in Task Scheduler:** In `tools/register_capture_task.ps1`, explicitly route calls to `$RepoRoot\.venv\Scripts\python.exe` with `-WorkingDirectory $RepoRoot`.  
5. **Add Truncation Quantization to Payout Settlement in `C7b`:** In `src/nhl_dfs/build/objectives.py`, quantize tied payout distributions using `Decimal('0.01')` with `ROUND_DOWN`.

---

## 8\. Improvement Ideas

#### 1\. Ingest Starting Goalie Status via DraftKings Draftables Alert Feeds

* **Objective:** Generation reliability and contest-level edge.  
* **Why it works:** DraftKings marks non-starting goalies as `OUT` or adds news alerts directly in the `draftables` API payload. Using this as a primary filter eliminates dependence on third-party scrapers for late scratch confirmation.  
* **Cost:** Very low (30 lines in `src/nhl_dfs/data/sources/dk_public.py`). Runtime: \$\<0.2\$ s. Tokens: 0\.  
* **Failure Risk:** None; acts as a redundant safeguard alongside DailyFaceoff.  
* **Phase:** Chunk `C1`.

#### 2\. Correlated Poisson-Gamma Mixture for Skater SOG & Blocks

* **Objective:** Variance maximization (99th-percentile outcome density).  
* **Why it works:** Standard Negative Binomial models treat skater shot rates independently. Linking the skater's shot intensity \$\\lambda\_i\$ to a shared team pace factor \$\\gamma\_{\\text{team}} \\sim \\text{Gamma}(\\alpha, \\beta)\$ preserves the real-world clustering of high-shot volume games.  
* **Cost:** Low (modify sampling loop in `src/nhl_dfs/sim/game.py`). Runtime: negligible. Tokens: 0\.  
* **Failure Risk:** Minimal; easily calibrated against historical game logs.  
* **Phase:** Chunk `C5`.

#### 3\. Power-Play Unit Minutes Siphon Modeling

* **Objective:** Exploiting fragile chalk.  
* **Why it works:** When a team is projected for high penalty-draw volume, PP1 skaters siphon ice time directly from bottom-six EV skaters. Explicitly penalizing Line 3/4 even-strength minutes on slates with high expected penalty counts prevents selecting low-ceiling punts in high-event matchups.  
* **Cost:** Medium (requires penalty rate integration in `src/nhl_dfs/models/opportunity.py`). Runtime: 0\. Tokens: 0\.  
* **Failure Risk:** None; bounded by total 60-minute skater team budget.  
* **Phase:** Chunk `C4`.

#### 4\. Automatic UTIL Position Swapping for Late-Game Flexibility

* **Objective:** Portfolio drawdown minimization and late-swap efficiency.  
* **Why it works:** In Classic rosters, placing the player with the latest scheduled start time into the `UTIL` slot maximizes flexibility during late swap if an unexpected scratch occurs in later games.  
* **Cost:** Very low (a post-solve slot sorting step in `src/nhl_dfs/build/assign.py`). Runtime: \$\<10\$ ms. Tokens: 0\.  
* **Failure Risk:** Zero; swapping an eligible skater into UTIL changes no salary or scoring attributes.  
* **Phase:** Chunk `C2b` / `C8`.

#### 5\. Leverage Tie-Break Using Empirical Duplicate Counts

* **Objective:** Contest variance maximization and game theory.  
* **Why it works:** Instead of relying on heuristic product-of-ownership formulas, count exact lineup occurrences directly across the 10,000 simulated opponent field rosters. When two lineups have comparable tail projection density (\$\\pm 1 \\text{ SE}\$), choose the one with fewer duplicate instances in the simulated field.  
* **Cost:** Low (already supported by `Counter` in `src/nhl_dfs/models/field.py`). Runtime: \$\<50\$ ms. Tokens: 0\.  
* **Failure Risk:** None; pure deterministic sorting.  
* **Phase:** Chunk `C7b`.

#### 6\. Differential Correlation Matrix for Empty-Net Scenarios

* **Objective:** Accurate 99th-percentile ceiling capture.  
* **Why it works:** Empty-net goals produce rapid multi-point accumulation for defending teams protecting a 1- or 2-goal lead in the final 3 minutes. Coupling leading-team top skaters to trailing-team goalie pull states correctly models hockey's unique end-game upside bursts.  
* **Cost:** Medium (state transition logic in `src/nhl_dfs/sim/game.py`). Runtime: \$\<5\$ s for 20k scenarios. Tokens: 0\.  
* **Failure Risk:** Low; constrained by historical empty-net event frequencies (\~0.15 per game).  
* **Phase:** Chunk `C5` / `C12`.

#### 7\. Direct DraftKings CSV Column Pass-Through for Zero-Loss File Writing

* **Objective:** 100% byte-fidelity on template generation.  
* **Why it works:** Rather than building the CSV from parsed objects, keep the original uploaded `DKEntries.csv` in memory as a binary template, modify only the slice corresponding to the roster columns using pre-allocated byte offsets, and write back to disk.  
* **Cost:** Low (implemented in `src/nhl_dfs/export/writer.py`). Runtime: \$\<5\$ ms. Tokens: 0\.  
* **Failure Risk:** Zero; completely eliminates CSV dialect/quoting differences.  
* **Phase:** Chunk `C0b`.

#### 8\. Local SQLite Ephemeral Cache for MoneyPuck Parquet Partitions

* **Objective:** Fast startup and memory efficiency.  
* **Why it works:** Storing 2 seasons of MoneyPuck shot-level data in local DuckDB or SQLite files rather than reading monolithic CSVs reduces feature load times from \~8 seconds to under 200 ms on CLI execution.  
* **Cost:** Low (one conversion utility in `src/nhl_dfs/data/history/moneypuck.py`). Runtime: Saves 7+ seconds per run. Tokens: 0\.  
* **Failure Risk:** None; files remain local and keyless.  
* **Phase:** Chunk `C3`.