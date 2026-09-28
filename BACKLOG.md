# Backlog

Row format from plan section 12 ("Backlog row"). Status: `NEW / SHADOW / READY / DONE / REJECTED`.
A deterministic defect is marked as such; a strategy idea is a hypothesis until measured.

| ID | Date / evidence | Problem or hypothesis | Affected metric | Proposed bounded change | Confidence / sample | Acceptance test | Priority | Status | Result / version |
|---|---|---|---|---|---|---|---|---|---|
| B1 | 2026-09-28, C2c design (unverified against DK) | Possible defect: late-swap and refresh accept a fresh `--salary` file only when its exact role-ID set matches the parent run's slate (`slate_id_for`). If DK adds a late player to a draft group, a re-downloaded salary file (Ben's only fresh status source while draftables returns 403) would be refused; the referee's embedded-list check would fail the same way. | Late-swap availability near lock | Accept a fresh salary file with the same mode and games when the ID sets differ only by added rows; report the ID-set diff; keep every original row's salary and ID; rerun the referee against the fresh file. | Low: not observed; DK behavior unverified | A fresh file with one added player is accepted, the diff is reported, and pinned cells and existing IDs are unchanged | Medium | NEW | |
