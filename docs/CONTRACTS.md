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
  or goalie cap, Showdown Captain cap 1 for 2 to 3 entries. Fee-share budgets compare against max(budget, LPT floor).
- Frontier: five kappas (risk.yaml); dominated points dropped, identical portfolios share a row; choose the highest tail
  utility inside the budget, else the least-risk point (recorded). Budget is the [BEN] flag 2 placeholder.
