# Build status

Tracker for `BUILD_CHUNKS.md` (final, 26 September 2026). Every development session reads this first and updates it last. `tools/next_chunk.py` (from C0a) edits the Status, Started, Finished, Commit, and Exit checks cells; sessions write Notes and the session log by hand. Status values: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`, `GATED`.

## Chunks

| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |
|---|---|---|---|---|---|---|---|
| C0a | TODO | | | | | | Copies the two DK rule files from Downloads into docs/rules/. |
| C0b | BLOCKED | C0a | | | | | Needs real Classic and Showdown DKSalaries.csv + DKEntries.csv under tests/fixtures/real/<date>/ (gitignored). Flip to TODO when in place. |
| C1 | TODO | C0b | | | | | Register the capture task after the session (tools/register_capture_task.ps1). |
| C2a | TODO | C0b | | | | | May be done before or after C1. |
| C2b | TODO | C2a, C1 | | | | | Local-first; network second pass bounded. |
| C2c | TODO | C2b | | | | | Milestone 1: uploadable file with lock-safe late swap. |
| C3 | TODO | C2c | | | | | Milestone 2: provisional leverage portfolio. Prefit runs only if gates allow (flag 1). |
| C4 | TODO | C1 | | | | | Run the backfill outside any slate clock. MoneyPuck enabled by default (flag 6). |
| C5 | TODO | C4 | | | | | Per-person params; implements Projection so C3 upgrades automatically. |
| C6 | TODO | C5, C1 | | | | | Record simulate wall clock here. |
| C7 | TODO | C5, C1 | | | | | Writes the Daily Faceoff goalie parse path to docs/sources.md. May be done before or after C6. |
| C8 | TODO | C6, C3 | | | | | Milestone 3: objective-aware portfolio. Uses the placeholder risk budget until flag 2 is answered. Record bench_field numbers. |
| C9 | TODO | C8, C7, C2c | | | | | |
| C10 | TODO | C9, C7 | | | | | Record /usage attribution and the rehearsal result in docs/measured_usage.md. |
| C11 | BLOCKED | C8, C3, C0b | | | | | Needs at least one Classic and one Showdown standings export in data/standings/inbox/. Flip to TODO when present. Milestone 4. |
| C12 | GATED | C11 | | | | | Gate: learn/gates.allows("field_fit") per mode. DONE on "rejected" is valid. |
| C13 | GATED | C11, C6 | | | | | Gate: C6 calibration deficiency plus a preregistration written first. DONE on "rejected" is valid. |

## Open [BEN] flags

Defaults are in force until answered. Answer by editing the named config or by telling a session, which records the answer here.

| # | Flag | Default in force | Where it lands |
|---|---|---|---|
| 1 | Prior-season NHL DK standings exports exist? | Assumed none | `data/standings/history/` (C3 prefit runs only when the evidence gate allows) |
| 2 | Risk budget | P(lose ≥80% of slate fees) ≤ 0.60; ≤40% of fees on one goalie; ≤40% on one game only when the slate has more than one game | `config/risk.yaml` (C8) |
| 3 | Typical entry mix (count, fees, contest types) | 20–150 entries across 150-max GPPs and Showdown; occasional WTA and single-entry | `config/contest_families.yaml` (C3) |
| 4 | Claude Code host and terminal willingness near lock | Local Windows, PowerShell; terminal fast path is the documented default | `config/runtime.yaml` (C2c, C9), skills (C10) |
| 5 | "System tunes itself" meaning | Measure, propose, promote in a dev session behind the evidence floors; nothing self-updates | `learn/gates.py` (C3, C11) |
| 6 | MoneyPuck non-commercial terms acceptable for personal DFS? | Yes, with attribution; `sources.moneypuck.enabled: true` | `config/sources.yaml` (C4) |
| 7 | Preseason a target? | No; regular season first, playoffs validated separately | `config/model.yaml` (C5) |

## Milestones

| Milestone | After | Reached |
|---|---|---|
| Uploadable legal file, offline, with lock-safe late swap | C2c | |
| Provisional leverage-aware portfolio on priors | C3 | |
| Scenario-based, objective-aware portfolio | C8 | |
| Learning loop and financial ledger live | C11 | |

## Session log

One line per session, newest last: `YYYY-MM-DD · <chunk> · <outcome: DONE / IN_PROGRESS handoff / BLOCKED> · deviations from the card · new flags · backlog IDs added`.

- 2026-09-26 · plan revision 2 · chunk plan, dependency graph, and tracker created; no code yet.
- 2026-09-26 · plan revision 3 · two critiques synthesized (reviews/NHL_DFS_CRITIQUE_SYNTHESIS_2026-09-26.md); chunks renumbered in objective order (17); tracker reset; no code yet.
