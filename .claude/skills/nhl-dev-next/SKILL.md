---
name: nhl-dev-next
description: Development session: find the one eligible build chunk (reruns every DONE chunk's checks) and work it per CLAUDE.md.
disable-model-invocation: true
shell: powershell
allowed-tools: PowerShell(.venv\Scripts\python.exe tools\next_chunk.py*)
---

## Next chunk (already computed; every DONE chunk's checks were rerun)

!`.venv\Scripts\python.exe tools\next_chunk.py`

## What to do

Follow CLAUDE.md "Development session protocol" for the chunk named above and nothing else: read its card in
BUILD_CHUNKS.md and only the files it names, `next_chunk.py --start <id>` and commit before the first file,
commit after each module with explicit `git add`, run the card's exit checks, `--done`, one session-log line in
BUILD_STATUS.md, then commit "<id>: <title> (DONE)". If a predecessor check failed above, stop and report it.
When every chunk is DONE, work one READY row of BACKLOG.md instead.
