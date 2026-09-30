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
<!-- slates: new rows above this line -->

## Adversary isolation rehearsals (`/nhl-qa-rehearse`)

A PASS means the adversary echoed the packet's canary and its reply contains neither the token planted in the main
conversation nor any CLAUDE.md sentence (and it reported no project instruction file). `omitClaudeMd` needs Claude
Code 2.1.271 or later.

| Checked (UTC) | Claude Code | Verdict | Canary echoed | Planted token | CLAUDE.md | Packet |
|---|---|---|---|---|---|---|
<!-- rehearsals: new rows above this line -->
