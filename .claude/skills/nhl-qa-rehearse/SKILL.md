---
name: nhl-qa-rehearse
description: Isolation rehearsal for the nhl-adversary agent. Builds a throwaway run from the synthetic fixture, launches the adversary with a canary packet, and checks the reply contains the canary and nothing from this conversation or CLAUDE.md.
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.\nhl.ps1 *) Agent(nhl-adversary) Write
---

## Rehearsal setup (already run; do not rerun it)

!`.\nhl.ps1 qa-rehearse --prepare`

## What to do, in three turns

1. Launch `nhl-adversary` with ONLY the PACKET JSON above pasted inline, exactly as printed. Do not add the PLANTED
   token, this conversation, or anything else to its prompt.
2. Save its reply verbatim with Write to the path on the SAVE THE REPLY TO line.
3. Run `.\nhl.ps1 qa-rehearse --check --reply "<that path>"` and report the REHEARSAL verdict and the
   four checks in plain words (canary echoed; planted token leaked; CLAUDE.md leaked). The result is recorded in
   docs/measured_usage.md by the command.
