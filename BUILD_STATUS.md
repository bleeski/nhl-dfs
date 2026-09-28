# Build status

Tracker for `BUILD_CHUNKS.md` (final, 26 September 2026). Every development session reads this first and updates it last. `tools/next_chunk.py` (from C0a) edits the Status, Started, Finished, Commit, and Exit checks cells; sessions write Notes and the session log by hand. Status values: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`, `GATED`.

## Chunks

| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |
|---|---|---|---|---|---|---|---|
| C0a | DONE |  | 2026-09-26 | 2026-09-26 | ed988d5 | PASS | Copies the two DK rule files from Downloads into docs/rules/. |
| C0b | DONE | C0a | 2026-09-28 | 2026-09-28 | fc1bb5f | PASS | Real fixtures: tests/fixtures/real/2026-09-29/ (gitignored), 2 Classic + 5 Showdown reserved entries, all blank. Real salary files carry DK Status (OUT/IR/DTD) and Starting columns, kept only in raw bytes; C2b and C7 need them. |
| C1 | IN_PROGRESS | C0b | 2026-09-28 |  |  |  | HANDOFF: done=all Create-list files at 6b3455a; 148 tests pass; probe, capture --once, register_capture_task.ps1 -DryRun all run; remaining=DK draftables returns HTTP 403 (Akamai) from this machine's script, so the recorded 200 payload tests/fixtures/http/dk_draftables_153983.json is missing and 4 draftables tests fail by design; next=Ben saves https://api.draftkings.com/draftgroups/v1/draftgroups/153983/draftables?format=json from his browser to C:/Users/benja/Downloads/dk_draftables_153983.json; the session trims it to about 12 people (CPT+FLEX rows; include Bedard OUT, Ellis IR, Lavoie DTD), commits it, confirms the parser field names against it, runs pytest -m c1, then --done C1. If Ben's browser also gets 403, stop for Ben's decision. Register the capture task after the session (tools/register_capture_task.ps1). |
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
- 2026-09-26 · C0a · DONE · deviations: (1) FileStatus enum members were not specified by the card; grepped the plan for FILE_VALID/per-row and implemented FileStatus(TRUE, FALSE) to mirror the plan's `FILE_VALID=TRUE` report line as an enum, matching the style of every other status vocabulary; (2) `next_chunk.py` bare mode reports the next chunk by dependency-satisfaction alone (not restricted to TODO/IN_PROGRESS status), since the literal how-a-session-works rule would print nothing once C0b is BLOCKED, contradicting this chunk's own exit check; (3) `--block` writes its `--reason` into the Notes cell (prepending to any existing note) since no other tracker column fits, though the header note assigns Notes to sessions by hand; (4) `--root` added to `next_chunk.py` for hermetic testing (tests/test_next_chunk.py never touches the real repo's tracker or checks); (5) `requirements.lock` generated via `uv pip freeze` rather than `pip freeze`, since pip is not installed inside a uv-managed venv; uv.lock is the primary pinned lock file; (6) `GoalieLine` gained an `sh_points` field the card's interface omitted, because docs/rules/ ("Goalies WILL receive points for all stats they accrue") and the SH bonus living under "Players" require it; the rule files override the card here; no `shootout_goals` field was added for goalies since a goalie is never a shootout shooter in real play; (7) added `.gitattributes` (`*.sh eol=lf`) so `nhl.sh` survives a checkout under `core.autocrlf=true`. Open question for Ben: docs/rules/ lists only "Win" and "Overtime Loss" goalie decisions with no separate shootout-loss category; this build scores a shootout loss as OTL on the strength of the card's own test case (OTL + shutout + 0 GA), not on explicit rule text; please confirm this matches DK's actual settlement. No new [BEN] flags in BUILD_STATUS.md. No backlog rows (no earlier-chunk defects encountered; there is no earlier chunk).
- 2026-09-28 · C0b · DONE · real fixtures filed from Ben's 2026-09-29 exports (byte-identical copies, gitignored). Deviations: (1) extra Conflict kinds beyond the card's triple-overflow rule: PERSON_KEY_COLLISION (different raw triples, one person_key; excluded), SAME_NAME (informational; how the Pettersson/Aho fixtures 'produce Conflicts' while staying distinct people), UNPAIRED (Showdown CPT or FLEX missing; informational); (2) `read_salary` requires the nine columns by exact name but tolerates extras (real files add Status and Starting); (3) `write_entries` takes role IDs in canonical slot order and maps them to the template's column order (the real Classic template is C,C,W,W,W,D,D,G,UTIL, G before UTIL); (4) referee avoids all of `nhl_dfs.contracts`, not only geometry (stricter than the card; enforced by an AST test); its identity is the raw (Name, TeamAbbrev, Position) triple; `rules.legal` takes optional `slots` so `check_file` checks each cell against its own template column label; (5) referee extra checks: every non-roster byte of the output bound to the entries file, Name-vs-ID agreement in 'Name (ID)' cells, overflowed identities rejected, and a draft-group check (the entries file's embedded player list, which starts at the Instructions column, must list exactly the salary file's IDs); (6) `verify` gains `--parent`; (7) tzdata added as a direct dependency (tracker note: needed by zoneinfo on Windows for ET to UTC; already installed transitively via pandas); (8) tests/lineup_helpers.py: test-only cheapest-legal picker (not a solver) plus an independent regex field tokenizer; (9) .gitattributes gains `tests/fixtures/**/*.csv -text` so git never rewrites fixture line endings (C0a path, edited because it blocked fixture integrity); (10) public helpers beyond the card: `physical_lines`, `template_permutation`/`canonical_to_template`/`template_to_canonical`, `cell_role_id`, `existing_lineup`, `parse_game_info`, `field_spans`, `format_cell`. Also fixed an em dash in the C0a log line. Verified: real Name + ID cells equal the written 'Name (ID)' format. Open question for Ben: the salary file gives AvgPointsPerGame to 1 decimal (13.8) while the entries file's embedded list gives 2 (13.83); intake uses the salary file's value. runs/c0b-demo/ holds cheapest-legal placeholder files for the exit check; they may include OUT/IR players and are not for upload. No new [BEN] flags. No backlog rows.
- 2026-09-28 · C1 · IN_PROGRESS handoff · blocked mid-chunk on DK draftables: HTTP 403 (Akamai Access Denied) for every draft group from this machine, while the DK lobby and contest-detail endpoints answer 200; the plan makes draftables the scratch and lock signal for C2c and C9, so this matters beyond C1. Deviations so far: GameOdds gains home_abbrev/away_abbrev (Covers has its own game ids); OddsSnapshot gains as_of_basis (partner odds use lastUpdatedUTC, observed 2026-08-28 and kept as received; Covers has no timestamp so it uses fetch time); teams.yaml adds a covers column (Covers writes MON, NAS, VEG, TB, LA) and leaves 22 unobserved DK abbreviations null with dk_verified false; HttpCache adds a per-run call budget (max_calls_per_run 200), per-host minimum intervals, force_refresh, and SourceSchemaError.raw_path; observations live under data/raw/observations/; capture hard-links bodies into the snapshot (copy fallback) and exits 0 when sources fail; register_capture_task.ps1 adds -DryRun and -Unregister; capture.py re-launches under .venv like next_chunk.py; Covers fails closed on unparseable structure or prices, treats an empty book cell as missing (counted) and falls back to the next configured book, and maps EV to +100; nhl.week_schedule added for probe; tests/fixtures/http/SOURCES.md records provenance and trimming; .gitattributes marks tests/fixtures/http/** -text; conftest blocks real network calls in every test. Open question for Ben: DK's CSV Status column carries DTD, which the card's STATUS_MAP does not list (it maps to UNKNOWN with the raw value kept); the draftables status vocabulary may differ and needs checking against the saved payload. No new [BEN] flags. No backlog rows.
