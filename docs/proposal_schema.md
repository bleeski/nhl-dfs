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
