# Measured usage (C10)

Token counts are measured, not budgeted (plan section 8). The enforceable bounds are invocation counts and wall
clock, held by the controller. Fill the slate table after each of the first ten slates.

## How to measure

1. Run the slate through the skill in Claude Code: `/nhl-run "<DKSalaries.csv>" "<DKEntries.csv>"`.
2. Count model turns: headless, `claude -p "/nhl-run ..." --output-format json` reports `num_turns`; interactive, count the
   assistant replies.
3. In the same Claude Code window, type `/usage` and copy the Attribution lines (skills and subagents, as a share of
   recent usage) into the table. `/usage` is interactive only.

## Slate runs

| Date (UTC) | Command | Entries | Model turns | Engine wall (s) | End to end (s) | Researcher called | Adversary rounds | /usage attribution | Notes |
|---|---|---:|---:|---:|---:|---|---:|---|---|
| 2026-09-30 03:18 | headless `/nhl-run`, superseded: the skill's frontmatter was invalid YAML (argument-hint began with a quoted string), so preprocessing did not run and the model ran `slate` itself | 5 | 4 | 40.8 | 142 | yes | 1 | n/a | not a valid exit measurement; kept for the record |
| 2026-09-30 03:05 | headless `/nhl-status` | n/a | 1 | n/a | n/a | no | 0 | n/a | proves `shell: powershell` preprocessing on this machine |
| 2026-09-30 03:11 | headless `/nhl-qa-rehearse` | 5 (synthetic) | n/a | n/a | 58 | no | rehearsal | $0.42 at list price | see the rehearsal table |
| 2026-09-30 09:14 | headless `/nhl-run` (C10 exit check), synthetic tests/fixtures/late_swap/classic, scratch roots runs/_c10exit | 5 | 4 | 42.6 | 137 | yes (9 players, 0 overrides) | 1 (2 proposals: 1 strategic swap accepted as an unvalidated modeled improvement and published as v4, 1 rejected) | derived, not /usage: $0.46 at list price; models claude-sonnet-5-5 (main) and claude-haiku-4-5 | preprocessing verified: exactly one run folder per invocation, so the engine ran before the model; no permission denials |
| 2026-09-30 09:05 | headless `/nhl-late-swap latest "<path with a space and (1)>"` | 5 | 1 | about 5 | 9.6 | no | 0 | n/a | status lines from preprocessing; `latest` resolved to the run of the export's mode and entries; OBJECTIVE not needed |
| 2026-09-30 09:06 | headless `/nhl-refresh <run id>` | 5 | 1 | about 10 | 14.7 | no | 0 | n/a | OBJECTIVE=provisional (no scenario cache on a baseline-only run), 0 cells changed |
| 2026-09-30 09:17 | headless `/nhl-qa-rehearse` | 5 (synthetic) | 4 | n/a | 61 | no | rehearsal | $0.29 at list price | PASS; setup preprocessed (one rehearsal run per invocation) |
<!-- slates: new rows above this line -->

## /usage after the C10 exit runs (2026-09-30)

Ben ran `/usage` in the Claude desktop app's Code window after the headless exit runs. It showed plan usage only, with
no Attribution section, so no per-skill or per-subagent split is available from this client:

- Current session: 21% used (resets Sep 30, 12:10am America/Chicago)
- Current week (all models): 54% used (resets Oct 2, 7pm America/Chicago)
- Current week (Fable): 19% used (resets Oct 2, 6:59pm America/Chicago)

Ben chose not to rerun `/usage` in the command-line tool. Per-run figures therefore come from the headless JSON report and are
labeled "derived, not /usage" (list-price cost; `usage` covers the main conversation, `modelUsage` adds the subagents):
`/nhl-run` $0.46, `/nhl-qa-rehearse` $0.29 (slate table above). The headless sessions ran on claude-sonnet-5-5 with
claude-haiku-4-5 for part of the work, so turn counts and costs are for those models. If a later client shows Attribution,
add it here after a slate run.

## Adversary isolation rehearsals (`/nhl-qa-rehearse`)

A PASS means the adversary echoed the packet's canary and its reply contains neither the token planted in the main
conversation nor any CLAUDE.md sentence (and it reported no project instruction file). `omitClaudeMd` needs Claude
Code 2.1.271 or later.

| Checked (UTC) | Claude Code | Verdict | Canary echoed | Planted token | CLAUDE.md | Packet |
|---|---|---|---|---|---|---|
| 2026-09-30 03:11Z | 2.1.285 (Claude Code) | PASS | yes | no | no | 14643007a2d3ab22 |
| 2026-09-30 09:17Z | 2.1.285 (Claude Code) | PASS | yes | no | no | 6c5361149bf6c9f6 |
<!-- rehearsals: new rows above this line -->
