# Ben's roadmap page

Link: https://claude.ai/artifact/Hr4LNXmMRs55Zp4YjEWW8w (private to Ben's claude.ai account; sharing is his, from the
page's Share menu).

A plain-English view of the build: what each chunk is, why we are doing it, why it matters for winning money, what is
done, what is next, what is on hold, and what waits on Ben. It is generated, never typed by hand:

| Part | Source |
|---|---|
| Rank, title, band, size, dependencies | `chunks.yaml` (file order is the rank) |
| Status, start and finish dates | the `BUILD_STATUS.md` tracker |
| Next chunk | `next_chunk.find_next(verify=False)`, the same rule as `--peek` |
| The plain-English words, Ben's open decisions, the changelog | `docs/roadmap/plain.yaml` |
| Layout and behavior | `docs/roadmap/page.html` (the data is embedded at build time) |

`python tools/roadmap.py check` fails (ROADMAP=STALE) when a chunk has no entry, a DONE chunk has no `result`, a chunk
was retitled since its entry was written, or any note contains an em dash. `tests/test_roadmap.py` runs the same check,
so the suite fails until the notes are current.

## When the notes change

- A dev session that finishes a chunk writes its `result`, adds a `caveat` when the result was not measured on real
  data, and adds one `updates` line (CLAUDE.md, Finish).
- A session that adds, splits or retitles a chunk writes or rereads that entry and runs `python tools/roadmap.py stamp <id>`.
- A queue change (reorder, a new band, Ben's answer that unblocks a chunk) adds one `updates` line, and edits or removes
  the matching `asks` entry. The check fails while an ask names a chunk that is no longer DONE, BLOCKED or GATED.
- Backlog work that settles a `caveat` or an `ask` (for example the real-slate runs B102, B107, B126 and B127 behind
  the C20 to C23 caveats) rewrites or removes them and adds one `updates` line. The check cannot see this one, so it is
  on the session.

## When the page falls behind

A session without the Artifact tool cannot publish (`ROADMAP_PAGE=NOT_PUBLISHED`). Any later session that has the tool
catches up: `read` the link, compare the commit in the page footer with `git rev-parse --short origin/master`, and when
they differ, build from an up-to-date master and republish.

## When the page is republished

After every dev merge (the `nhl-post-merge` skill, section 3) and after a queue change, through the `nhl-roadmap`
skill: build, then publish to the link above with the Artifact tool (`read` the link first in a new conversation).

## How to write a note

Ben plays DraftKings NHL and is not an engineer. DFS words are fine (stack, chalk, ownership, leverage, Captain, late
swap, GPP); engineering words are not (solver, MILP, manifest, config key, sha256). Write `why` from the chunk's
impact text and session log, keep their numbers, and never add one they do not contain. Say plainly when something did
not work or was only tested on made-up data. Short sentences, active voice, no em dashes, dates like "Oct 13".
