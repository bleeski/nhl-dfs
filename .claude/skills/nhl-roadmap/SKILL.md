---
name: nhl-roadmap
description: "Rebuild and republish Ben's NHL DFS roadmap page, the plain-English view of every chunk (done, up next, on hold, waiting on Ben). Use after a dev merge (nhl-post-merge calls it), after a change to the queue in chunks.yaml, or when Ben asks to see, refresh or fix the roadmap."
---

## What it is

One HTML page built by `tools/roadmap.py` from chunks.yaml (rank, band, size, dependencies), the BUILD_STATUS.md
tracker (status, dates) and docs/roadmap/plain.yaml (the plain-English notes, the only hand-written part). It is a
claude.ai artifact; the link and the rules for writing the notes are in docs/roadmap/README.md.

## Steps

1. `python tools/roadmap.py check` (PowerShell on Ben's machine: `.venv\Scripts\python.exe tools\roadmap.py check`).
   ROADMAP=STALE lists what to write. In a dev session or a queue change, write it in docs/roadmap/plain.yaml following
   the README's rules, `python tools/roadmap.py stamp <id>` for an entry you reread after a retitle, and commit it with
   that work. After a merge (nhl-post-merge), do not edit tracked files: report the stale entries instead.
2. `python tools/roadmap.py build` writes outputs/roadmap/roadmap.html (gitignored) and prints PAGE_SHA256.
3. Publish with the Artifact tool to the link in docs/roadmap/README.md: first `action: "read"` with that `url` (once
   per conversation; a publish to an artifact the conversation has not read is refused), then publish with
   `file_path` = outputs/roadmap/roadmap.html and `url` = the link, no `icon`, and a short `label` such as
   "C24 done" or "queue change 2026-10-14". Never publish without `url`: that creates a second page.
4. No Artifact tool in this session (some cloud sessions): say `ROADMAP_PAGE=NOT_PUBLISHED (no Artifact tool)`. The
   notes in the repo are what matter; the next session with the tool republishes. Catch-up, in any session with the
   tool: the page footer names the commit it was built from; when it differs from `git rev-parse --short
   origin/master`, build from an up-to-date master and republish.
5. Tell Ben in one line what changed on the page (the newest `updates` line) and give the link.

## Do not

Hand-edit the built HTML, put numbers in the notes that the chunk's card, impact text or session log does not contain,
or mark a result as a success when it was measured only on made-up data (that is a `caveat`).
