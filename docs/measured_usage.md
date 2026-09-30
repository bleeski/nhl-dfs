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
| 2026-09-30 03:18 | headless `/nhl-run` (C10 exit check), synthetic tests/fixtures/late_swap/classic, scratch roots | 5 | 4 | 40.8 | 142 | yes (9 players, 0 overrides: no source for the synthetic teams) | 1 (4 proposals, 0 accepted) | pending Ben (`/usage` is interactive); headless report: $0.48 at list price | Claude Code 2.1.285; first attempts: a multi-statement preprocess block does not match `PowerShell(.\nhl.ps1 *)` and is refused silently, so preprocessing is one `nhl.ps1 slate $ARGUMENTS` call |
| 2026-09-30 03:05 | headless `/nhl-status` | n/a | 1 | n/a | n/a | no | 0 | n/a | proves `shell: powershell` preprocessing on this machine |
| 2026-09-30 03:11 | headless `/nhl-qa-rehearse` | 5 (synthetic) | n/a | n/a | 58 | no | rehearsal | $0.42 at list price | see the rehearsal table |
<!-- slates: new rows above this line -->

## Adversary isolation rehearsals (`/nhl-qa-rehearse`)

A PASS means the adversary echoed the packet's canary and its reply contains neither the token planted in the main
conversation nor any CLAUDE.md sentence (and it reported no project instruction file). `omitClaudeMd` needs Claude
Code 2.1.271 or later.

| Checked (UTC) | Claude Code | Verdict | Canary echoed | Planted token | CLAUDE.md | Packet |
|---|---|---|---|---|---|---|
| 2026-09-30 03:11Z | 2.1.285 (Claude Code) | PASS | yes | no | no | 14643007a2d3ab22 |
<!-- rehearsals: new rows above this line -->
