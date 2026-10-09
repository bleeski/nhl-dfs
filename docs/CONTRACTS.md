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

- `PayoutSource`: `EXACT` | `TEMPLATE` | `PRIOR` (C16: TEMPLATE sits between; see Payout templates below)
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
- Joint own-entry accounting (C18, backlog B55 and B56, reviews R04 and R05): the user's entries in one contest are one account. `objectives.OwnContest` scores each candidate in the selection fill by the change to the whole contest's payout and utility (its own plus what it takes from entries already placed there, ties included), from prefix sums over the placed order; `pay_total` is the exact joint payout (integer cents, equal to a from-scratch `joint_payouts` recount). `select` records the largest gap as `Selection.accounting_gap_cents` and in the manifest's scenario section `accounting` (expected 0). A portfolio with one entry in every contest is unchanged; a contest with two or more entries is no longer scored entry by entry. `Choice.score` is the entry's contribution to its contest (a second entry's can be 0 or negative). `controller.evaluate` compares the paired totals of every touched contest (all owned entries, `objectives.own_totals`), anchors the 3% band on the edited entries' own utility before, uses the paired standard error of the aggregate change, and tests the top-1% tail as a paired delta (`Metrics.top_payout_cents`); the QA round figures `utility_gain`, `tail_before` and `tail_after` therefore describe the whole touched contests, not only the edited entries (nothing in `src` reads them but the round record). Not covered: `swap_objective.choose` (late-swap repairs) still scores an alternative against its siblings as opponents only (backlog B97).
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

## Publication lock safety (C14, `build/state.py`, `locks.py`, `run.py`, `controller.py`)

- Three `publish()` call sites exist (`run._export_and_publish`, `controller.apply_round`, `late_swap.run`). A boundary is a
  game start or the edit-stop buffer. `publish(precheck=...)` runs the last lock check with both publish locks held, so a
  wait for them (up to 60 s) is covered; a refusal raises `LockCrossed` (a `PublishRefused`) and writes nothing.
- Controller (B52): `now` is the round-start time (deadline, records); `clock` is the live callable and defaults to the wall
  clock whether or not `now` is given. Only a labeled rehearsal (`--as-of`) passes a fixed clock.
- Initial run (B53): `_export_and_publish` (phases A, B, P, S; `clock` and `runtime` are required) diffs the new file against
  the predecessor version, or the uploaded entries file for a first publish, with `locks.publish_crossings`. A changed cell
  whose old or new occupant is in a started or edit-stop game refuses the version: the incumbent stays current, RUN_NOTES
  and the manifest (`lock_stops`) say why. Unchanged cells of a started game are never a crossing, and a row with no known
  start time is not "unaddable" here (late swap's `not_addable` is). Flag 20: a first publish ships with a warning on an
  edit-stop change and publishes nothing on a started-game change.
- The optional passes (phase B, provisional, scenario) do not start once a game has started, is inside the edit stop, or the
  next open game is inside late swap's optional-work margin (`swap_objective.optional_work_ok`).

## Started slates (C15, `intake/salary.py`, `build/locks.py`, `run.py`, `late_swap.py`, `packet.py`)

- A game is in progress when DraftKings writes the In-Progress marker in Game Info (salary file "In-Progress", entries
  file's embedded list "In Progress"; the entries reader takes IDs only), or, for a run, when its Game Info start has
  passed at the run's first clock read (`salary.mark_started`). Those rows live in `SalaryPool.started_rows` and are never
  in `rows`, `by_role_id`, `persons` or `games` (flag 24). `salary.with_started_rows(pool)` is a copy that adds them back
  to resolve, write, score and pin the cells a lineup already holds; every caller keeps them in its exclusion set, so a
  started player is never a candidate or an addition. A started game is named by its teams ("BOS (in progress)") when only
  the marker is known: the marker carries no opponent, so no AWAY@HOME is written for it.
- Locks: a cell whose occupant is a started row is LOCKED "started" by the marker alone (no start time needed, the clock is
  not consulted), the entry stays readable (not "unreadable"), and the ID is in `started_role_ids` and `not_addable`.
  `publish_crossings` resolves such an ID through `started_rows`, so C14's guard (flag 20, unchanged) sees a changed
  started-game cell although its row left the pool.
- Late swap (B42 part 2): a fresh In-Progress salary file passed as the third file is a re-download of the same slate
  (`added_rows_diff` accepts a row that is now In-Progress when ID, name, team, positions and salary are unchanged; a changed
  salary is still refused). Pinned started cells go through `with_started_rows` for the re-solve, the scenario scorer
  (`swap_objective.resolve(score_pool=...)`) and the exposure counts, and stay byte for byte.
- Initial run (B42 part 3, flag 15): `run_slate` builds the open games. An entry that holds started-game players is solved
  on its own with those cells pinned in their slots (late swap's repair under the baseline objective; started and DK-OUT
  rows are not candidates); the other entries are filled from the bank with these counted as fixed. An entry no rebuild can
  complete keeps its current cells if they are a complete legal lineup (DELIVERY_STATUS=DEGRADED_REVIEW), else the run
  publishes nothing. A slate with no open game is refused (late swap). Leaving a started game out does not degrade
  DELIVERY_STATUS (flag 26). The manifest's `started_slate`, RUN_NOTES and the printed notes name the started teams, the
  excluded players, the open games built and the pinned cells. The written pinned cell is the writer's `Name (ID)` (ID and
  slot unchanged); late swap keeps the original bytes. The optional passes stay off once a game has started (C14), so a
  started-slate run is baseline-only (B89).
- Slate id: the hash is over the selectable IDs plus the started rows' IDs (not other excluded rows), so a file made after a
  game started has the id of the file made before it, and no existing slate's id changed.
- `RunView` (QA packet, research request, controller) carries the started rows for lookups and locks only; they are never a
  status, a research target or an alternative.

## Payout templates (C16, `models/payout_templates.py`, B62, B50, flag 14)

- A contest whose DraftKings page is unavailable (403, offline) is priced on the cached table of the same template and
  labeled `PAYOUT_SOURCE=TEMPLATE`; PRIOR stays for every contest with no match. One matcher serves the build
  (`models/contests.resolve`) and settle (`learn/ledger.prize_tables`); it reads local files only and never fetches.
- Template = name without the " (AAA @ BBB)" game suffix and stray spaces, plus max entries ("(Late)" stays: another
  template). The lobby capture (`<cache root>/dk_lobby`, newest capture that lists the contest) gives the contest's name,
  size, fee, prize pool, own `IsGuaranteed` and DraftKings' template id (`tmpl`); tables come from `<cache root>/dk_contest`,
  every run's `contests/` and `settle/prize_tables/` copies and browser-saved `dk_contest_*.json`. Roots are read at call
  time (tests point the cache root at an empty folder).
- A match needs: a lobby row; not a satellite (name pattern or any ticket tier); same (name key, max entries); lobby fee equal
  to the table's fee (and the entries file's); lobby prize pool equal to the table's `totalPayouts`; when both lobby rows
  carry `tmpl` they are equal (a veto and a note, never a wider match). A table that was resized (`wasResized`,
  `isResizable`), holds a ticket tier or whose tiers do not sum to `totalPayouts` is refused. Several tables of one key that
  differ: the newest start wins and the note says so. Off-switch: `config/contest_families.yaml payout_templates.enabled`.
- Build: a TEMPLATE contest takes its field size from the lobby row (`field_size_source=lobby`; the table's positions are in
  terms of max entries) and its family from the matched table. Overall `PAYOUT_SOURCE` is the weakest contest: EXACT, TEMPLATE
  (every contest EXACT or TEMPLATE, one TEMPLATE) or PRIOR; the same rule in `run` and late swap
  (`contests.combine_payout_sources`). One printed line per contest: `PAYOUT_SOURCE=TEMPLATE contest <id> (table of contest
  <id> ...)` or `PAYOUT_SOURCE=PRIOR contest <id> (<reason>)`. The manifest (`provisional.contests[*]`, `scenario.contests[*]`)
  and RUN_NOTES carry the template contest id, paid places, tiers sha256 and lobby capture.
- Settle: REPORTED > EXACT > TEMPLATE > UNKNOWN. A TEMPLATE table is final only by the contest's own facts (its lobby row's
  `IsGuaranteed`, or standings entries >= max entries); otherwise the entries stay UNKNOWN with the reason (a possibly
  resized contest). Not saved as `dk_contest_<settled id>.json` (it would read back as EXACT). TEMPLATE money counts as known
  in net, drawdown and `complete_payout`; `winnings.csv` rows are still written for TEMPLATE entries (Ben's reported amount
  is the only check that the template holds, REPORTED wins and a mismatch is noted); a settle notes when the table's tiers
  sha256 differs from the one the run priced on.
- Cloud sessions: `data/raw/` and `runs/` are gitignored, so a cloud container has no lobby capture and no tables and stays
  PRIOR (reported per contest). Persisting them is B68's question (flag 16), not C16's.

## Field size from the lobby row (C38, `models/contests.py`, B72, flag 29)

- A contest whose page did not describe it and that no template matched is sized from its lobby row, the same local capture and
  reader C16 uses (`payout_templates.lobby_rows`; `build/run._lobby_rows` reads it once, or takes the template store's rows).
  Missing is missing: no row, no usable row, or a switch off keeps the family prior and says why.
- A row sizes a contest only when: the fee equals the entries file's; the name (game suffix and stray spaces stripped) equals the
  entries file's; max entries is at least 1; and our own entries in that contest are fewer than the row's max (at least one
  opponent exists). Otherwise the contest stays `field_size_source=family_prior` with the reason in `field_size_note`.
- Family: a family the name gives (cash, satellite, wta, small_field patterns) is kept and only the size moves. A contest whose
  family is only the declared default is `small_field` (family source `lobby`) when max entries is at most
  `exact_rules.small_field_max_entries` (500), else the default; the cut is the exact path's own (`family_from_detail`), so a contest
  is classed the same whether its page answered or not. It reads max entries only; max per user is recorded in the note, so a
  multi-entry contest under the cut (475 max, 14 per user) is small_field too.
- Labels: `FIELD_SIZE_SOURCE` is `EXACT` (the contest page), `LOBBY` (the lobby row's max entries; also a TEMPLATE contest's) or
  `PRIOR` (the family's declared size). The stored `field_size_source` keeps its older values (`contest_detail`, `lobby`,
  `family_prior`). One printed line per contest: `FIELD_SIZE_SOURCE=LOBBY contest <id> (<max entries>, <per user>; lobby capture
  <snapshot>)` or `FIELD_SIZE_SOURCE=PRIOR contest <id> (<reason>)`; the manifest (`provisional.contests[*]` and
  `scenario.contests[*]`: `FIELD_SIZE_SOURCE`, `field_size_note`), RUN_NOTES (both contest tables and Messages) and the CLI `note:`
  lines carry it.
- `PAYOUT_SOURCE` is untouched: a LOBBY-sized contest is still PRIOR unless C16 matched a template. Only the size and the family
  move, never the payout curve. Late swap and refresh read the contests frozen at build time.
- Off-switch: `config/contest_families.yaml lobby_field_size.enabled` (independent of `payout_templates.enabled`); false restores
  the family priors exactly.
- Cloud sessions: no run phase fetches the lobby and a container has no `data/raw`, so a cloud contest prints
  `FIELD_SIZE_SOURCE=PRIOR` (no lobby capture lists this contest). B91 carries the fix with C16's.

## Inputs reach the field features and the simulator (C17, `models/field_inputs.py`, `build/run.py`, `scenario_pass.py`, `sim/market.py`; B63, B20, B51, B66, B85, B93; flags 30 to 32)

- What the field is told: `field_inputs.collect(work, snapshot, role_state, now, own_cfg)` returns `FieldInputs`. Odds: the run's one
  snapshot matched with `market.match_odds` (only verified DK codes match, Covers codes translate to the pool's) and not older than
  `market.max_age_h`. Roles: `pp1` = Daily Faceoff PP unit 1, `line` = forward line or defense pair, usable pages only. News ages: hours
  since the player's latest Daily Faceoff news item or a goalie confirmation (a time ahead of the clock is 0). Goalie starts: only a
  CONFIRMED team (starter 1, the team's other goalies 0); EXPECTED and CONFLICTED keep the 0.8/0.2 prior. A skater whose team has no usable
  page reads as the covered average of his position group on `pp1`, `line1` and `news_recent` (`imputed`), never as a non-member; with no
  covered team, or all covered, nothing is imputed.
- Where: `run._run_odds` fetches the odds once (cache first, bounded by `network_pass_budget_s`, none offline) before the provisional
  pass; `_provisional_pass` builds the inputs and calls `build_fields(inputs=)`; `FieldBuild.feats` is the one feature table and
  `_grow_fields` reuses it; the scenario pass takes the same odds (`odds=`), and its fallback after a failed provisional pass builds the
  same inputs and looks up C16 template tables (B93). `cli field` of a baseline-only run stays an offline family-prior diagnostic. Late
  swap and refresh never sample a field (they read the frozen `scenario/fields.json`).
- Report: one line `FIELD_INPUTS MARKET=a/b ROLES=c/d (...)` in the messages, the manifest (`provisional.field_inputs`;
  `scenario.field_inputs` on the fallback) and RUN_NOTES; `FIELD_INPUTS=OFF` when switched off. Off-switch
  `config/ownership.yaml field_inputs.enabled` (flag 30): false restores the pre-C17 field exactly.
- Team rate (B51): `ParamTable.team_goals` is DK team to (mean skater goals per game over its last 82 regular-season games before the
  slate, games used) from the as-of history frame (`config/model.yaml team.team_rate`). `sim/market.model_strength` uses it instead of the
  league rate for a team whose listed skaters cannot make up a lineup (fewer than 10 F or 5 D expected to dress) when it has at least
  `min_games` (20) games, else the league rate; the note says which.
- Clip (B66, flag 31): `TeamStrength.history_home` and `history_away` are the share of a side's modeled goals that rest on history (HISTORY
  persons 1, MIXED by exposure ratio, PRIOR 0; a side on its own recent rate 1, on the league rate 0). The limit is `max_discrepancy` +
  (`max_discrepancy_prior` - `max_discrepancy`) x (1 - share), 0.35 and 1.0 today, so a fully history-backed side keeps 0.35 and a prior side
  is not clipped for a plausible market. A CAPPED note prints the market value, the clipped value, the model value, the limit and the share.
  A caller that gives no share keeps 0.35.
- Evidence: `docs/experiments/2026-10-06_c17_replay.md` (time-pinned replay of the 09-30 slate, nothing fetched). All inputs against off on
  today's code worsen `mae_all` by 0.02 to 0.27 on that slate (one slate, 3 of 6 Daily Faceoff pages usable, placeholder weights); against
  the recorded 09-30 run all four contests are lower, but that run had 112 of 133 persons on PRIOR (B35), and the pass needs the covered-average treatment adopted after seeing replay numbers (without it contest 196218438 is 4.543 against 4.315). B63's five-slate check is a
  deferred trigger.
- OTT (B85): `config/teams.yaml` OTT dk OTT verified from the 2026-10-03 DK lobby GameSets (OTT @ TOR).

## Classic stack mix in the field (C19, `models/field.py`, `models/field_fast.py`, `config/ownership.yaml`; B43, B15 first half, B99, B100; flags 40 to 47)

- Rules: a behavior's `stack_rule` is none, team3 (3 or more skaters of one team), team4 (4 or more), double_stack (4 of one team and 3 of another; the 3-skater-team rule makes it 4-3-1) or, from C46, team5 (5 or more; the other 3 skaters come from at least 2 other teams, so 5-2-1, 5-1-1-1 or 6-1-1 and never 5-3). team4, double_stack and team5 are Classic only and raise in Showdown. `field.stack_jobs` is the one allocation both samplers use: teams in proportion to exp(implied total), double-stack pairs in proportion to exp(sum of the two totals), draws split by largest remainder; a rule no team or pair can satisfy draws unstacked and says so in `Field.detail`.
- Vectorized sampler: the new rules force skaters one at a time, the one that displaces least from the best completion so far (a forced set must still reach 8 skaters with 2 C, 3 W and 2 D), alternating the two teams of a double stack, and block both teams from the non-forced fill so the eighth skater is from a third team. none and team3 keep their original forcing and per-team batches; their lineups are identical to before C19 (flag 44). A draw that fails a check is solved by the MILP with the same group constraints, and a draw with no legal lineup is counted in `detail` as dropped.
- Mixtures: `field.classic_mixtures` (family, then a game-count bucket, only `default` filled) replaces `field.mixtures` for Classic large_gpp and small_field when `enabled` is true. It shipped `enabled: false`, and was switched on on 2026-10-08 after the 2026-09-30 gate (flag 46) passed; with it off every field has the same lineups as before C19 (the only difference is a `detail` line when a draw is dropped, which used to vanish silently). `field.behaviors_for_pool(family, pool, cfg)` is what the field builders call. Showdown and the other families never use it. The weights are a PRIOR solved by one grid search against the pooled 14-contest table's shares (`scripts/c19_weights.py`, flag 40), not a model fitted from standings; FIELD_CALIBRATION stays PRIOR.
- Team5 table (C46, flags 47 and 49): `field.classic_mixtures_team5` is a second table of the same shape (family, game-count bucket, only `default` filled; behaviors may include `stacker5`) with its own `enabled`. For a Classic family it lists it replaces the C19 table, and only while `classic_mixtures.enabled` is also true; a family it does not list keeps the C19 table, and every other family and Showdown are unchanged. It shipped `enabled: false` and was switched on on 2026-10-08 after the real-pool gate (flag 49) passed: 5,000 legal draws on the 2026-09-30 pool with the 3+, 4+ and 5+ shares within 10 points of 93.5, 65.7 and 21.3. The weights are a PRIOR from one grid search on the synthetic stand-in pool (`scripts/c46_weights.py`), not a fit. Set `enabled: false` to get the C19 table back. The RUN_NOTES shape line names the table in force (`Classic mixture ON, team5 table`).
- RUN_NOTES: one line per Classic family in the scenario section, `Field shapes, <family> (<mixture in force>; <games>; <draws>; FIELD_CALIBRATION=PRIOR ...)`, the sampled 3+, 4+, 5+ and two 3+ shares and the top shapes beside the table (`field.classic_stack_table`). It prints with the switch off too, as a before-picture. The data is `scenario.field_growth[<family>].shape_mix` in the manifest.
- Known limits: the weights were solved on a synthetic 6-team pool and the table includes the 09-30 contests (flag 43); with the C19 table alone no behavior produces 5+ stacks (0.6 percent against the table's 21.3), which the team5 table fixes in total but not in composition: 6-1-1 is 0 percent against 6.3 and 5-1-1-1 about 10 percent (B101); the vectorized lineups of the new rules sit 0.2 to 1.3 percent below the MILP optimum on average, and double_stack was above the 1 percent rule in 2 of 12 seed and pool cases (flag 45; the single-swap search is B12 and C35's); a double stack's pair composition is by implied totals only (B99).

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
  C8 objective against the field and the user's other entries in the contest as opponents (their own loss is not yet counted: backlog B97),
  then `tiebreak.rank`. Figures: choosing draws (labeled
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

## History store currency (C39, `data/history/status.py`, `refresh.py`, `build/run.py`, `tools/cloud_bootstrap.sh`; B70, B71; flags 18, 33, 34)

- Measured every run, local files only, never raises: `status.measure(as_of)` reads three columns of the newest stored seasons and returns
  a state, judged on the regimes the model reads (`model.yaml regimes`, regular only; preseason and playoff rows never make a store look
  current; the newest game of any type is reported next to it). `ABSENT` (no `skater_games` file), `PRIOR_SEASON_ONLY` (no regular game of
  the as-of date's own season stored), `STALE`, `CURRENT` (newest regular game at most `history.fresh_days` = 1 day before the as-of date,
  so the store holds the last finished day and the back-to-back flag can see last night), `SEASON_COMPLETE` (regular season over,
  `regular_season_complete`), `UNREADABLE`. Phase A puts it in `manifest["history"]` and the `- History store:` line of RUN_NOTES (with the
  model's HISTORY / MIXED / PRIOR counts); `history --status [--as-of]` prints the same line. It changes no lineup (same v1 sha256 with
  and without it, tested). A league break can read STALE falsely; the cost is one small fetch that finds nothing.
- Refreshed in Phase B, after v1 is published and before the optional passes (`run._history_refresh`; `refresh.refresh_incremental`): only
  when the state is `PRIOR_SEASON_ONLY` or `STALE`, never offline, never when `stopped("history refresh")` (a started game, the edit stop,
  or too little time), never while the cloud bootstrap backfill is `RUNNING`. It fetches the NHL per-game reports for the season in
  progress from two days before the newest stored regular game (the season start when none) through the last finished Eastern day
  (`status.completed_through`: a day completes 3 hours after midnight ET), NHL reports only (flag 33: no MoneyPuck, no box-score cross-check,
  so the new rows are Tier B), on its own `HttpCache` call cap (`history.refresh_max_calls` 40) and a wall-clock budget
  (`history_refresh_budget_s` 20). It is staged: the season's files are copied to a temp folder, the fetch and the combine write only
  there (`backfill(upsert=True)` merges by key and never deletes), and the four changed tables are promoted into the store from the calling
  thread only when the worker finished inside the budget and every stored (nhl_id, game_id) key is still present; a timeout, a failure or
  a shrinking result leaves the store byte for byte as it was. A file swap that fails part way (Windows refuses to replace a file another process has open) is
  reported as FAILED naming the files swapped and not swapped; the store stays consistent (the per-source tables are newer than the
  combined ones) and the next refresh recombines it. When games were added the projection the role, provisional and scenario
  passes use is rebuilt from the refreshed store (flag 34; v1 keeps the earlier one); if the rebuild fails the earlier one stays. The
  outcome (`REFRESHED`, `NO_NEW_GAMES`, `SKIPPED` with the reason, `FAILED`, `TIMED_OUT`) is in `manifest["history"]["refresh"]` and the
  notes line; a failure is a message and a `failed` entry, never a stopped run. `history --refresh` runs the same function by hand.
- Cloud bootstrap (flag 18, `tools/cloud_bootstrap.sh`): after the .venv step, when `data/features/history/skater_games/*.parquet` is
  absent, `history --backfill 2` starts detached (all descriptors redirected, `setsid` where present) and the hook returns at once; the
  wrapper writes `data/cache/history_bootstrap.state` (`state=RUNNING` with its pid, then `DONE` or `FAILED` with the exit code) and
  `data/cache/history_bootstrap.log`. Phase A never waits: a run that starts first prices on priors and its ABSENT line says the backfill
  is running (`status.read_bootstrap_state`; a RUNNING record whose process is gone, or older than `history.bootstrap_stale_min` 30,
  reads ABANDONED and never blocks a refresh). `--dry-run` (or `NHL_BOOTSTRAP_DRY_RUN=1`) says what it would do and starts nothing.
  The script always exits 0.
- The game day the back-to-back flag compares is the Eastern date (`params.build`, as `slate_as_of`): a start after 8 PM ET is the next UTC
  day, and the UTC date made the flag read 0 for those games.
- Not done here: the refresh is not part of `scheduled-refresh` (B95); MoneyPuck stays a manual `history --backfill`; eviction of the raw
  report bodies stays with the `history` command (B2 section above).
