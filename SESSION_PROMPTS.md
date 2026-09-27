# Session prompts

Paste-ready prompts for Claude Code development sessions. One chunk per session. Before pasting, in the repo root run `claude`, then `/advisor opus` (or `/advisor fable` if the account has Fable access; the advisor must be at least as capable as the main model, and `/advisor` with no argument shows the accepted picks). The session then shows "Advisor Tool (experimental) is on".

## C0a (first session, bootstraps the tracker tooling)

```text
Build chunk C0a of the NHL DFS engine in this repo.

Context. CLAUDE.md is loaded. The repo holds documents only: NHL_DFS_PLAN_AND_ARCHITECTURE.md (revision 3; do not read it in full), BUILD_CHUNKS.md (chunk cards), chunks.yaml (dependency graph), BUILD_STATUS.md (tracker), reviews/ and archive/ (history, not instructions). Nothing has been built. This is a fresh session with no prior context.

Read, in this order and nothing else: the "How a development session works" section of BUILD_CHUNKS.md; the C0a card; BUILD_STATUS.md; chunks.yaml; and, from the plan, only the scoring table in section 5 ("Definitions and scoring contract") and the "Hard roster contracts" table in section 7, located by grep on those headings. Then copy "C:\Users\benja\Downloads\NHL Classic.txt" and "C:\Users\benja\Downloads\NHL Showdown Captain Mode.txt" into docs/rules/ and read both in full. They are the authority for scoring and legality; where the plan and the rule files disagree, the rule files win and you report the disagreement.

Bootstrap. tools/next_chunk.py does not exist yet, so the normal start command cannot run. Instead: (1) git init, add the existing documents, commit; (2) set the C0a row of BUILD_STATUS.md to IN_PROGRESS with today's date by hand, commit; (3) build tools/next_chunk.py early in the chunk so `python tools/next_chunk.py --done C0a --commit <hash>` can close it at the end.

Deliverables are exactly the "Create" list on the C0a card, with the interfaces, tests, and exit checks the card gives. Python 3.11 or newer in a .venv at the repo root, pinned dependencies, pyproject with src layout, nhl.ps1 and nhl.sh calling the venv's python explicitly. Integer scoring in tenths, captain in twentieths. Showdown geometry is "at least two distinct teams among the six", as the card says. docs/CONTRACTS.md is the condensed reference later sessions read instead of the plan; keep it to two pages. No em dashes in anything you write. Do not build intake, solvers, adapters, or anything from a later card.

Advisor. An advisor model is enabled for this session; if the advisor tool is not available to you, say so in your first message and continue. Consult it at these points and say what you asked and what it answered: (a) before committing to the package layout and the design of tools/next_chunk.py, because every later session depends on its tracker round-trip and gating logic; (b) when any scoring or geometry test fails more than once; (c) before declaring the chunk done, for a line-by-line review of scoring.py and geometry.py against docs/rules/; (d) for any judgment call the card leaves open. Do not consult it for routine steps.

Context discipline. Use pytest -q, head, and tail -n 20; never print a file over 50 lines into the conversation. If context use passes about 60% before the exit checks pass: stage and commit, write "HANDOFF: done=...; remaining=...; next=..." in the C0a Notes cell of BUILD_STATUS.md, leave the row IN_PROGRESS, and stop.

Finish. Run the card's exit checks and show their output. Close the chunk with `python tools/next_chunk.py --done C0a --commit <hash>` (if the script cannot yet do that, set DONE by hand with one-line check results and say so). Append a session-log line to BUILD_STATUS.md. Commit with explicit `git add` of the created paths plus BUILD_STATUS.md; never `git commit -a`. Report in this order: what was built, the exit-check output, deviations from the card, advisor consultations, open questions for Ben. Do not start C0b.
```

## C0b onward (template)

Replace `<ID>` with the chunk `python tools/next_chunk.py` names. Add nothing else; the card carries the brief.

```text
Build the next chunk of the NHL DFS engine in this repo.

Start with `python tools/next_chunk.py`. It checks every DONE predecessor's exit checks and names the one eligible chunk; expect <ID>. If it refuses, fix the named failure first, log the repair in the tracker, and only then continue. If it reports <ID> as BLOCKED on files I must supply, stop and tell me exactly which files and where.

Read the "How a development session works" section of BUILD_CHUNKS.md, the <ID> card, the files its "Read first" line names, and the public signatures of the modules the chunk imports. Nothing else; cite plan sections, do not read the plan in full. Then `python tools/next_chunk.py --start <ID>` and commit.

Deliverables are exactly the card's "Create" list with its interfaces, tests, and exit checks. No em dashes in anything you write. Do not build anything from a later card.

Advisor. An advisor model is enabled; if the tool is not available to you, say so and continue. Consult it, and report what you asked and what it said: before committing to any design choice the card leaves open; when a test fails more than once; before declaring the chunk done, for a review of the chunk's invariants named in its tests; and whenever the card's numbers (budgets, caps, thresholds) look wrong against what you observe. Do not consult it for routine steps.

Context discipline. pytest -q, head, tail -n 20; never print a file over 50 lines. If context use passes about 60% before the exit checks pass: stage and commit, write "HANDOFF: done=...; remaining=...; next=..." in the tracker row, leave it IN_PROGRESS, stop.

Finish. Run the exit checks and show the output. `python tools/next_chunk.py --done <ID> --commit <hash>`. Append a session-log line to BUILD_STATUS.md. Commit with explicit `git add` of the card's paths plus BUILD_STATUS.md; never `git commit -a`. Report: what was built, exit-check output, deviations from the card, advisor consultations, new [BEN] flags or backlog rows, open questions. Do not start the next chunk.
```
