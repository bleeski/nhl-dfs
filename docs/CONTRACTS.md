# Contracts (condensed reference)

Read this instead of `NHL_DFS_PLAN_AND_ARCHITECTURE.md` in later sessions.
Source of truth for scoring and legality is `docs/rules/NHL_Classic.txt` and
`docs/rules/NHL_Showdown_Captain_Mode.txt`; both files carry identical
scoring tables. All arithmetic below is integer tenths of a DK point;
Captain scoring is integer twentieths (tenths x 1.5 expressed as x3).

## Scoring (tenths of a point)

| Event | Tenths | Notes |
|---|---:|---|
| Goal | 85 | |
| Assist | 50 | |
| Shot on goal | 15 | |
| Blocked shot | 13 | |
| Shorthanded point bonus | 20 | per qualifying SH goal or assist, stacks |
| Shootout goal | 15 | not a regular goal; does not count toward goal-based bonuses |
| Hat trick (goals >= 3) | 30 | once per player-game |
| 5+ shots on goal | 30 | once per player-game |
| 3+ blocked shots | 30 | once per player-game |
| 3+ points (goals + assists >= 3) | 30 | once per player-game |
| Goalie win | 60 | |
| Goalie save | 7 | |
| Goalie goal against | -35 | |
| Goalie shutout | 40 | sole goalie of record, full game, 0 GA in regulation+OT; caller sets the flag, scoring does not infer it |
| Goalie overtime loss | 20 | DK's rule text has only "Win" and "Overtime Loss" goalie decisions; a shootout loss is scored as OTL (confirmed by the C0a card's test case: OTL + shutout + 0 GA) |
| Goalie 35+ saves | 30 | |
| Captain | x1.5 | multiplies the entire base score including every bonus and penalty, exactly once, using the salary file's Captain salary/ID |

Goalies earn every stat in the top block too (goals, assists, SOG, blocks,
SH bonus, and the four threshold bonuses), per "Goalies WILL receive
points for all stats they accrue, including goals and assists." A goalie
never scores a shootout goal in real play, so `GoalieLine` has no
`shootout_goals` field. No plus/minus, hits, faceoff, or penalty-minute
scoring exists.

## Roster geometry

| Contract | Classic | Showdown |
|---|---|---|
| Roster | 2 C, 3 W, 2 D, 1 skater UTIL, 1 G (9 total) | 1 CPT, 5 FLEX (6 total) |
| Salary | <= $50,000, each row's own salary | <= $50,000, each row's own salary |
| Teams | Skaters (not the goalie) span >= 3 distinct teams | >= 2 distinct teams among the six |
| Identity | 9 distinct people, exact DK slot eligibility | 6 distinct people; a person cannot be both CPT and FLEX |
| Captain | n/a | one 1.5x scorer; goalie CPT allowed |
| Invented rules | none (no per-team max) | none (no one-goalie limit; two opposing goalies legal if the pool allows) |

`UTIL` accepts only skaters even if a row's data mistakenly lists it for a
goalie. `G` accepts only goalie rows. No other slot rule exists.

## Identity

- `normalize_name`: lowercase, NFKD-strip combining accents, transliterate
  the letters NFKD does not decompose (o/O-stroke, ae/AE ligature,
  l/L-stroke, d/D-stroke, sharp s), turn any remaining non-alphanumeric
  character into a word boundary, then collapse whitespace. "Aston-Reese"
  normalizes to "aston reese", not "astonreese".
- `position_group`: C/LW/RW/W -> "F"; D -> "D"; G -> "G"; anything else
  raises rather than guessing.
- `person_key`: `f"{normalize_name(name)}|{team}|{position_group(position)}"`.

## The four per-row states (plan, "Decisions that shape the design")

Kept apart, never conflated:

1. **Participation** - is the person expected to play (`Participation`;
   carries its own `UNKNOWN` member for unresolved news).
2. **DK eligibility** - is the row rosterable in the DK pool (`Eligibility`:
   `ROSTERABLE` | `DISABLED`).
3. **Actual lock** - has the real game started (part of `CellLock`).
4. **Engine's edit-stop buffer** - the engine's own pre-lock safety margin,
   independent of the real lock (the other part of `CellLock`).

`CellLock` collapses (3) and (4) into one three-valued vocabulary: `OPEN`
(outside the buffer, game not started), `EDIT_STOP` (inside the engine's
buffer, game not yet started), `LOCKED` (game has started).

## Evidence states

- `PayoutSource`: `EXACT` | `PRIOR`
- `OutcomeCalibration`: `UNVALIDATED` | `SHADOW` | `VALIDATED`
- `FieldCalibration`: `PRIOR` | `FITTED`

"Checked" means the stated checks passed; it never means guaranteed profit
or confirmed participation.

## Status vocabularies (`contracts/statuses.py`)

`ModelStatus` gained `HISTORY` and `MIXED` in C5 (per-person and run-level history coverage; `PARTIAL`/`FULL` stay for compatibility).

`FileStatus` (`TRUE`/`FALSE`, mirrors the plan's `FILE_VALID=TRUE` report
line as an enum rather than a bare bool), `NewsState`, `ModelStatus`,
`SearchStatus`, `DeliveryStatus`, `ObsStatus`, `GoalieState`,
`Participation`, `Eligibility`, `CellLock`, `PayoutSource`,
`OutcomeCalibration`, `FieldCalibration`, `FeasibleStatus`. A DK
participation status the engine does not recognize is `UNKNOWN`, never
coerced to a known value (CLAUDE.md); other vocabularies here that have no
`UNKNOWN` member raise on an unrecognized input instead (see
`contracts/ids.py:position_group`) rather than guess.

## Simulator (C6, `src/nhl_dfs/sim/`)

- One process for market and simulation: `sim/resolve.py` (regulation Poisson goals, equalizers and
  empty-net goals while a goalie is pulled, then overtime or a shootout). `market.fit_game` inverts it
  analytically (implied win probability and expected book total, shootout winner's goal counted when
  `so_adds_goal`), `game.simulate` samples it, so a fit that hits its target hits it in simulation.
- `sim/game.py`: per PERSON outcomes, `Outcomes` arrays `(n, P)`, person axis sorted by `person_key`.
  Empty-net goals are never charged to a goalie; shootout goals have their own column
  (`so_goals`, 1.5 points, never goals, SOG, GA or a bonus input); saves are exactly the opposing
  non-goal shots. Seeds: `SeedSequence([seed, purpose, chunk, game])`, purposes design / selection /
  referee (config/sim.yaml `purposes`). `chunk_size` is fixed in config: a memory-cap change never
  changes the draws.
- `sim/score.py`: `base_tenths` (int32, constants from `contracts.scoring`), `role_tenths` (a role row
  copies its person's column; CPT is not multiplied), `lineup_twentieths` (Captain applied once).
  `role_map_for(pool, person_keys)` builds the role to column map.
- `sim/cache.py`: `runs/<id>/sim/chunk_XXXX.npy` (int32 tenths) plus `meta.json` (seed, purpose,
  chunk size, spec hash). No candidates x scenarios matrix is ever stored.
- Odds: a snapshot older than `market.max_age_h` is not used; a game with no verified team-code match
  is `source=MODEL`, reported per game. A market that predates a goalie confirmation is STALE and
  blended toward the model.
- `sim/validate.py` (`cli calibrate`): graded conditional on who dressed and started; historical odds
  do not exist in the store, so market comparisons print UNAVAILABLE. Machine-readable
  `deficiencies` in `docs/calibration/<date>.json` are what C13's gate reads.

## Scenario portfolio (C8, `build/objectives.py`, `tiebreak.py`, `exposure.py`, `portfolio.py`, `scenario_pass.py`)

- `run` without `--baseline` publishes v1 baseline, v2 provisional (C3), v3 scenario. Seed streams: design (discovery),
  selection (the choice), referee (every reported figure). DTD (QUESTIONABLE) is priced once, as a seeded participation
  mask on the scenario scores (0.85 play probability); the C3 ranking haircut is not reapplied; minutes are not reallocated.
- Ties: the tied positions' prizes are pooled and split by the tied count, floored to the cent (integer cents; Decimal
  ROUND_DOWN reference `split_tie`). DK Terms verify only the even split (docs/payouts.md); [BEN] flag 9.
- Field: at most 5,000 distinct sampled lineups per contest. Weighted mode: integer multiplicities (largest remainder)
  summing to field size minus own entries. Sampled mode (fewer than 1,000 opponents): each scenario draws its actual
  opponents. GPP fields are grown to min(5,000, opponents) draws (Classic: `models/field_fast.py`; Showdown: threaded
  MILP); ownership is read off the grown field.
- Family objectives: large_gpp exp_payout_top1pct, small_field exp_payout, wta first_place_equity, cash p_clear_line,
  satellite p_seat (ticket face value kept apart from cash). Own entries in a contest are copies and opponents.
- Tie-break: anchored bands, band = max(3% x anchor, anchor Monte Carlo SE); ownership never past one band; cash and
  satellite ignore ownership; dup = sampled count when FIELD_CALIBRATION=FITTED else the pre-fit proxy.
- Caps: tournament entries only; plan defaults at >= 20 tournament entries with feasibility floors; below that no person
  or goalie cap, Showdown Captain cap 1 for 2 to 3 entries. Goalie and game budgets (B36): LPT floor <= budget is
  DOLLARS mode (fee share cap = budget, hard); floor > budget is LINEUPS mode (no dollar cap; at most
  max(1, floor(budget x entries)) of all entries per goalie / primary game, raised to ceil(entries / usable) only when
  too few exist). Both hold through EXPOSURE; CONCENTRATION relaxes them by the smallest step, then REPEAT.
  Captain budget compares against max(budget, LPT floor). Discovery adds `per_goalie` forced-goalie candidates per
  usable goalie (Classic). Stdout: GOALIE_CAP, GAME_CAP, RISK_BUDGET, MARKET_COVERAGE (B40).
- Frontier: five kappas (risk.yaml); dominated points dropped, identical portfolios share a row; choose the highest tail
  utility inside the budget, else the least-risk point (recorded). Budget is the [BEN] flag 2 placeholder.
- Frozen record (backlog B25, B26, B28): beside each kept selection and referee chunk, `flags_XXXX.npy` holds per-draw
  indicators `sim/score.FLAG_NAMES` (dressed, started, hat trick, 5+ SOG, 3+ blocks, 3+ points, any SH point, shutout,
  35+ saves), `(F, P, ceil(size/8))` uint8 packed along the draws (`np.packbits`, little bit order), same whole-chunk
  truncation as the points; `meta.json` names them (`flags`) and adds `participation`: per person `p_sim` (the
  simulator's dress or start probability), `mask`, `p_play`, per goalie `p_start` and its source (rotation, Daily
  Faceoff state, team page, override). Cache format stays 1: older caches load and settle as before. `Outcomes.started`
  marks a goalie who started in net (a relief goalie is dressed, not started); it consumes no draws. Real 2026-09-29
  Classic: +2.6 MB per stream (cache 21.2 to 26.4 MB), peak 657 to 675 MB, draws byte-identical.
- Contest details (B24): every DK contest detail that answers during `run` (Phase B, provisional pass) is saved to
  `runs/<id>/contests/dk_contest_<id>.json` with `sources.json`, before lock.

## Late swap and refresh objective (C9, `build/swap_objective.py`, `scenario_cache.py`, `live.py`)

- Lock semantics are C2c's, unchanged: pinned cells (LOCKED, EDIT_STOP) byte-identical, no started or edit-stop
  player added, only changed cells spliced, lock recheck before writing, referee on every file.
- Objective order from `--objective auto`: scenario, provisional, baseline. Every step down is recorded
  (manifest `objective.fallbacks`, RUN_NOTES `OBJECTIVE=`, CLI). A late swap with nothing to repair evaluates no
  objective (`not needed`) and returns the input bytes; refresh always resolves (eager).
- Scenario: the C8 pass keeps the first 8,000 UNMASKED selection and referee draws per person plus per-game hashes,
  contests (payout curves) and field lineups under `runs/<id>/scenario/`; late swap looks on the run, then its
  `parent_run_id` chain. Changed, unstarted games are re-simulated with their full-slate seed streams (games are
  independent draws, tested); started games keep cached draws. `--fast` scores on 4,000. Candidates: the MILP's best
  repair plus no-good-cut alternatives with the same (minimal) number of changed cells, chosen by the contest family's
  C8 objective jointly with the user's other entries, then `tiebreak.rank`. Figures: choosing draws (labeled
  optimistic) and referee draws, each with its Monte Carlo SE.
- Provisional: the role-applied ParamTable mean times the play probability. Baseline: the C2 prior objective.
- Roles: ParamTable, then `roles.merge`, then `roles.apply_state` exactly once per run; `confirmed_at()` goes to
  `build_slate`. Participation is priced once per person: RoleState `p_play` replaces C8's 0.85 unless roles already
  lowered that person's dressing or start probability. NEWS_STATE stays DK status coverage; ROLES_NEWS_STATE is C7's.
- Optional work stops at T-5 (engine) / T-8 (LLM) before the earliest start among games with open cells
  (`config/runtime.yaml late_swap`); inside it the objective is baseline. Separate from `edit_stop_buffer_s`.
- Goalie gate (`build/goalies.py`, B17, fix after C10): before targets are chosen, the Daily Faceoff
  starting-goalies page (one request, `late_swap.goalie_gate_budget_s`, else the stored copy labeled with its age;
  team pages stored only; `roles.merge` without a ParamTable, never `apply_state`) plus accepted overrides give each
  goalie CONFIRMED / NOT STARTING / CONFLICTED / EXPECTED / NOT EXPECTED / UNKNOWN. Only NOT STARTING (another goalie
  CONFIRMED, or OUT) joins the free-cell exclusions, so the fast repair replaces it minimally; a pinned one is
  reported and the file is DEGRADED_REVIEW. An override and Daily Faceoff confirming different goalies is CONFLICTED
  (no repair). Reports created after `now` are ignored. `GOALIE_GATE=` CLEAR / NO_NEWS / CONFLICTED / NOT_STARTING
  and a goalie table end every run, late swap, refresh, overrides-apply and qa-apply output and RUN_NOTES.
- Fresh salary file (B1, B27): `--salary` (late-swap: optional third positional; refresh: `<run-id> [salary]`) is checked
  against the PARENT's own copy (`intake/salary.added_rows_diff`): accepted when it only adds rows (Status, Starting,
  APPG may change); a removed or changed original row (ID, name, team, positions, salary, game info) or a new game is
  refused with the field named. The child keeps the parent's slate id, stores the fresh file and cumulative
  `salary_added_ids`; `SALARY_DIFF:` lists added players. The referee (`check_file(..., added_ids=)`, used by late
  swap, QA and verify) accepts an older export's embedded list only when it lacks exactly declared added IDs. An export
  listing IDs the salary file lacks is refused up front (re-download DKSalaries.csv). Refresh's child cache carries
  the indicators and participation forward (re-simulated games from new outcomes, OUT persons cleared).
- Scheduled refresh (`build/scheduled.py`, B23): one task (Ben registers it) runs the dispatcher every 5 minutes;
  the newest delivered, non-rehearsal run of each slate is refreshed once at T-60 and T-20 before its first lock
  (never inside T-5); a toast and `runs/_scheduler/notifications.log` on a changed file, a goalie alert, no goalie
  news at the last window, or a failure.
- `live.condition(scenarios, snapshot)`: without a reliable snapshot (declared source, at most 15 min old, covering
  every entry) a no-op with `LIVE_STATUS=NO_SNAPSHOT` or `UNRELIABLE`, and no chase pivot. With one: observed points
  plus the draw scaled by time left; TRAILING entries use `dup_first` inside the band, AHEAD ones `mean`.

## Settlement and learning loop (C11, `learn/`)

- `settle --run <id> --standings <csv|zip|folder>` (one call; the `/nhl-settle` skill preprocesses exactly it).
  Writes only `runs/<id>/settle/` (grades.json, settlement.md) and the Settlement section of RUN_NOTES.md (kept last
  by every later `write_run_notes`); FREEZE_CHECK hashes manifest, field.json, inputs, versions and scenario before
  and after (plus `contests/`). Ledger `data/ledger/ledger.parquet` (NHL_DFS_LEDGER_ROOT), backlog `BACKLOG.md`
  (NHL_DFS_BACKLOG).
- Forecast: PRE_LOCK only if the manifest's `created_utc` AND the forecast files' write times are before the
  slate's first game; otherwise POST_LOCK, graded as a plumbing check and never counted as evidence. The forecast run
  (B30) is the nearest run on the `parent_run_id` chain, the settled run first, with a PRE_LOCK scenario cache (a
  late-swap child grades against its pre-lock ancestor, labeled 'graded from run X'); none on the chain: the run
  itself. Money, standings and the entered-lineup check stay on the settled run; the freeze check hashes both. Only frozen
  files are graded: `scenario/fields.json` `own_by`/`dup_by`, `scenario/selection` draws. Nothing is rebuilt.
- Standings: entry block and ownership block share rows but are unrelated. Lineups parse against the slot sequence
  (Classic `C C D D G UTIL W W W`, Showdown `CPT` + 5 `FLEX`). Actual ownership is reconstructed from the lineup
  rows (DK's listed block omitted position rows on 2026-09-29; the listing is the cross-check); blank lineups stay
  in the denominator. Classic ownership per person (position + UTIL rows); Showdown per CPT and FLEX role (CPT FPTS
  = 1.5 x FLEX). Joins: normalized name + roster token; two candidates = CONFLICTED, never guessed; own entries by
  Entry ID. Names outside the salary file are reported (DK added them after the download).
- Money per own entry: REPORTED (Ben's `winnings.csv` from DK My Contests) > EXACT (a final prize table: filled or
  guaranteed; DK tie rule `objectives.split_tie`) > UNKNOWN (null, never $0). Tables, first found: `--prize-table`,
  the run's pre-lock `contests/` copy (then its parent chain's), browser-saved `dk_contest_<id>.json` beside the
  standings, an earlier settle's copy, the raw DK cache; each labeled. An UNKNOWN entry
  gets a row in a pre-filled `winnings.csv` (appended, Ben's lines kept). Net and drawdown over known payouts;
  incomplete when any payout is unknown. A re-settle replaces the run's ledger rows, and any other run's rows for the
  same (slate date, entry id) (B31: a run and its child book each entry once; the note names the replaced run).
- Grades: ownership (MAE all/active/top-20, weighted, bands, top-10 recall, CPT share, team totals, zero-observed
  mass, Pearson/Spearman, duplicate counts rescaled from the forecast's field size); forecasts (FPTS for points; NHL
  box scores through the accepted crosswalk for participation and bonus outcomes; MAE, bias, CRPS, p10-p90 coverage).
  With frozen indicators: skaters on dressed draws, goalies on started draws (relief on relief draws); bonus-rate
  calibration per bonus for every pool person in the box scores (hat trick, 5+ SOG, 3+ blocks, 3+ points; starters'
  35+ saves and shutout; SH point not graded: no shorthanded points in box scores); goalie decisions and Brier from
  the saved p_start, checked against the started share in Monte Carlo SE; play-probability Brier from saved p_play.
  Older caches: skaters unconditional, goalies on nonzero draws, decisions and play probability from the nonzero
  share, bonus rates NOT_AVAILABLE (each labeled).
- Evidence: `data/ledger/graded.json`; `gates.tier` per mode from PRE_LOCK runs only (Showdown contests of one game
  = one group; a (date, game, person) outcome counts once across modes; a contest's ownership labels count once per
  (slate date, contest id), newest settle, B31). Reported, never acted on. `complete_payout` per date is still an AND
  over the settled runs (a stale incomplete parent keeps its date incomplete: errs low).
- Backlog: `learn/backlog.add` appends keyed rows only for deterministic defects and missing frozen artifacts;
  hand-written rows are mapped (B17, B20, B21, B22, B24). Strategy hypotheses stay in the grades and notes.

## History raw cache (B2, `data/history/nhl_reports.evict_raw`)

- After a clean `history` backfill (no exception, zero box-score cross-check mismatches; `--keep-raw` skips it), the
  report sources (`nhl_report`, `nhl_goalie_report`) drop index entries past their TTL that are not a completed
  season's canonical window (the full-season windows and every split of them) and whose season's Parquet store was
  written after the fetch, then delete bodies no index entry references (unreferenced diagnosis bodies after
  `history.keep_unindexed_days`) and MoneyPuck `.tmp` files over a day old. Completed seasons rebuild offline row for
  row from the kept bodies. No other source is touched (DK contest bodies, capture/, observations/).
- Guard: a completed season whose cached windows match none of today's canonical URLs (window_days or the URL
  template changed) keeps all of them, with a WARNING line. A season that completes while only incremental windows
  are cached has no canonical bodies until a full backfill runs after July 31. Not evicted (mismatches, `--keep-raw`,
  `history.evict_raw: false`): a dry run prints the sizes.
