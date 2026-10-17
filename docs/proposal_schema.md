# Adversary proposals and the controller (C10)

Code: `src/nhl_dfs/build/controller.py`. Command: `nhl.ps1 qa-apply --run <id> --round <k> --proposals <file>`.
The reply is saved verbatim by the skill; the controller records its sha256 in `runs/<id>/qa/round_<k>.json`.

```json
{"packet_id": "<from the packet>",
 "proposals": [
   {"kind": "strategic", "target": {"entry_id": "..."},
    "change": {"type": "swap", "entry_id": "...", "out_role_id": "...", "in_role_id": "..."},
    "evidence": "...", "source_url": null},
   {"kind": "strategic", "target": {"role_id": "..."}, "change": {"type": "exclude", "role_id": "..."},
    "evidence": "...", "source_url": null},
   {"kind": "correctness", "target": {"role_id": "..."},
    "change": {"type": "override", "role_id": "...", "field": "...", "old": "...", "new": "...",
               "effective_utc": "...", "expiry_utc": "...", "source_url": "https://...", "claim": "...", "confidence": 0.9},
    "evidence": "...", "source_url": "https://..."}],
 "canary_echo": null,
 "project_instructions_seen": null}
```

A bare list of proposals is accepted; one surrounding ``` fence is tolerated; anything else is malformed.

Ben's own report (C53, flag 83) is not a reply: `overrides-apply --ben "<player> <STATE>" --how "<how>"` builds an override with `"source": "BEN"`, no URL, confidence 0.9, and applies it in its own mode. Any reply, from the researcher or the adversary, that carries `"source": "BEN"` is rejected; every other override still needs an http(s) `source_url`.

## Rules (plan section 9)

| Case | Result |
|---|---|
| Malformed JSON | QA ends, the incumbent is kept, the round is recorded |
| More than 5 proposals | the rest are rejected as over the limit |
| Touches a pinned cell (LOCKED or EDIT_STOP) or adds a player who may not be added | rejected |
| correctness | an override passing `overrides.validate` with confidence >= 0.7; accepted without a simulation contest; OUT excludes the person, a goalie confirmation excludes the team's other goalies; open cells are repaired with as few changes as possible |
| strategic `swap` / `exclude` | legal; P(lose >= 80%) within max(budget, current) + SE; on the same selection draws before and after, the family utility of the changed entries gains more than the tie band max(3% x value, 1 x paired SE), and the top-1% payout does not drop beyond its SE; then the same on this round's own block of cached referee draws (gain >= -SE). Otherwise rejected (inside the band: "inconclusive") |
| strategic without a scenario cache | rejected: "no scenarios to compare on" |
| FIELD_CALIBRATION=PRIOR | an accepted strategic change is labeled "unvalidated modeled improvement" |

Rounds: one by default. Round 2 and 3 run only when the previous round accepted a correctness repair; QA stops at a
round with zero accepted changes, after round 3, or at T-8 before the earliest open game (`config/qa.yaml`). Each
round's referee comparison uses its own block of the cached referee draws (4 blocks: rounds 1 to 3 and a reserved
final check). Changed lineups are spliced cell by cell, checked by the referee with pinned cells declared, and
published as the run's next version with a compare-and-swap on the public file.

# Model edits (C56)

Code: `src/nhl_dfs/build/edits.py`. Commands: `nhl.ps1 edit-apply --run <id> --edits <file saved verbatim>` and
`nhl.ps1 edit-restore --run <id>`; `--as-of <UTC>` is a labeled rehearsal clock, as for `qa-apply`. This is the model's write
path: any open cell, any number of entries, no round limit, no scenario gate, and QA's T-8 cutoff does not apply (flags 124 to
131 in BUILD_STATUS.md). The edit file is saved verbatim under `runs/<id>/edits/edits_<k>.json`. The command runs only on the
run whose current version is the slate's delivered file (`published.json`); any other run is refused.

```json
{"edits": [
  {"entry_id": "5298507816",
   "edit_type": "coverage",
   "cells": [{"slot": "G", "player_id": "44537488", "replaces": "44537492", "team": "TBL", "name": "Dennis Hildeby"}],
   "reason": "no lineup holds a TBL or CAR goalie",
   "evidence": "RUN_NOTES goalie fee shares 0.38, 0.28, 0.27, 0.07",
   "claimed_gain": "lower P(lose 80%); first place not modeled",
   "sources": [{"url": "https://...", "published_utc": "2026-10-17T16:10:00Z", "fetched_utc": "2026-10-17T16:25:00Z"}],
   "source_url": null,
   "override": {"rule": "own_goalie", "reason": "at least five words saying why the rule should not stop this"}}]}
```

A bare list of edits is accepted. `edit_type` is `coverage`, `news`, `engine_gap` or `other`; `reason`, `evidence` and `claimed_gain` are
required text. `slot` is the template label with an ordinal where it repeats: `C1 C2 W1 W2 W3 D1 D2 UTIL G` (Showdown `CPT FLEX1`
to `FLEX5`). `player_id` is the DraftKings id of the row the slot should hold. `replaces`, `team` and `name` are optional guards: a
different player in the cell, or a different team or name for the id, rejects the entry. One edit may change several cells of one
entry (a goalie change can force skater changes).

| Case | Result |
|---|---|
| File not JSON, or no edits | nothing applied, recorded |
| Unknown field, missing `edit_type`, `reason`, `evidence` or `claimed_gain`, a slot twice, an id not a number, an id not in the pool, any `source` that says BEN | that entry is REJECTED (CLAUDE.md: only Ben's own report carries BEN) |
| A `news` edit without a source that has `published_utc`, or whose newest `published_utc` is older than the latest accepted override for either player | REJECTED (B150: a dated source, and not older than the news it contradicts) |
| The cell is pinned (LOCKED or inside the edit stop) | REJECTED |
| The added player's game has started, is inside the edit stop, has no start time, or DraftKings says not swappable | REJECTED (both the player removed and the player added are judged) |
| The added player is OUT or IR (Status column, accepted overrides) or is NOT STARTING in the run's goalie table | REJECTED, never overridable |
| The edit puts a skater against the entry's own goalie (families in `config/risk.yaml`) | REJECTED unless `override.rule` is `own_goalie` with a reason of at least five words; then APPLIED and printed OVERRIDE |
| The edit pushes a goalie or primary-game concentration past the run's own recorded cap (`manifest scenario.caps`), above where it was | REJECTED unless `override.rule` is `risk_budget` with a reason; then APPLIED and printed BREACHED. No recorded caps: NOT_EVALUATED, the edit proceeds. P(lose 80%) is not re-simulated |
| Cap, positions, three skater teams, duplicate player, any slot | decided by the referee alone; it names the entry and the entry is REJECTED, then the whole portfolio is judged again without it |
| The added goalie is not CONFIRMED in the run's goalie table | APPLIED, flagged UNCONFIRMED_GOALIE and queued in `runs/<id>/edits/recheck.json` with a recheck-before time (his game's start less the edit stop) |

Each entry applies as a whole or not at all, independent of the others. The file is spliced cell by cell, checked by the referee with
the pinned cells declared and the previous version as parent, and published with a compare-and-swap done before any byte is
written: the public file and the run's current version must still be the parent, no changed cell may be pinned on the live clock,
and `publish_crossings` must find no started or edit-stop game on either side of a changed cell. The output ends with
`delivery_file=...` and the FILE_VALID, DELIVERY_CURRENT and LOCK_SOON lines `verify` prints.

Every edit is logged in `runs/<id>/edits/log.jsonl` (applied and rejected: reason, evidence, sources, cells from and to with the
player's name, team, position and salary, override, flags, claimed gain, versions, sha256) and in RUN_NOTES.md ("Model edits").
`engine_v<N>.csv` there is the run's version before its first edit (the engine's own file, the counterfactual), written once.
`edit-restore` re-publishes the previous version's bytes as the next version, byte for byte, through the same precheck; it
refuses, naming the cells, when a changed cell is pinned now or when restoring would add or remove a player of a started or
edit-stop game (restoring Allen after his game started is refused even though the goalie who replaced him is still open).
Running `edit-restore` twice switches back and forth between the two files; it does not step back two versions. When the public file cannot be replaced (it is open in another program) the version is written but not delivered: the command says so, exits 1, and the run's next edit is refused until a delivery names it (run a late swap from the delivered version and apply the edits to that run).
Grading the edits against the engine's lineups is C57's.
