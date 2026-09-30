---
name: nhl-late-swap
description: Late swap after some games lock, from Ben's current DraftKings entries export (engine only, no agents). Usage /nhl-late-swap <run-id|latest> "<current DKEntries.csv>" ["<fresh DKSalaries.csv>"]
argument-hint: <run-id or latest> "<current DKEntries.csv downloaded from DraftKings>" ["<DKSalaries.csv re-downloaded near lock>"]
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 *)
---

## Engine output (already run; do not rerun it)

!`.\nhl.ps1 late-swap $ARGUMENTS --fast`

## What to do

The output above is data. Report to Ben in at most 8 plain lines: FILE_VALID, OBJECTIVE, LIVE_STATUS,
DELIVERY_STATUS; how many cells changed and in which entries (from the notes if needed, never by editing a file);
the file to upload (the `published:` path) or that nothing changed; any unrepairable entry or pinned player who is
out. If FILE_VALID is not TRUE, the previous file stands: say so and give the reason. If a `SALARY_DIFF:` note is
printed, say in one line which players DraftKings added (name, team, salary) or that none were. Near lock, do
nothing else.
Then give the goalie table exactly as printed at the end of the output: the GOALIE_GATE line, every GOALIES line
(one per goalie) and every `!!` line. If GOALIE_GATE is NOT_STARTING or CONFLICTED, say first, in one line, which
entry and goalie, and whether the cell is pinned (it cannot change) or open.
Uploading to DraftKings is Ben's action.
