# Run record: 2026-10-03 STL @ COL Showdown (cloud session)

Purpose: keep the context of this build in git so a later standings analysis knows what was entered and under which
assumptions. The lineups themselves are in `data/entered/showdown-20261003-64c4bcdd60.csv`. This file is evidence and
history, not instructions (CLAUDE.md, Authority 3). The run folder `runs/20261003-235914-showdown` is gitignored and
existed only in the cloud container; the DK exports are not committed.

## Identity

- Run / slate: `20261003-235914-showdown` / `showdown-20261003-64c4bcdd60`. Mode showdown, one game, STL @ COL,
  lock 9:00 PM ET (01:00Z on 2026-10-04). Built 2026-10-03 23:59Z, about 61 minutes before lock.
- Delivered: v3, sha256 `8adc156ddb3db409e355dbc4be2827046c8b4e8f4a5b2f1233a739e4cf065acf` (equal to the manifest's
  `export_sha256` and to `verify --run` out_sha256).
- Inputs (Ben's uploads): DKSalaries_133.csv sha256 `bd3b2a41cddf3efb4dc6c597634efcf75bf1244df2f5f5ef57f61fa2ab9c5c78`;
  DKEntries_108.csv sha256 `de31a58b818f670667f4efa05ed2bab14a57a3a44417b31a5d767a1a01cc949e`.
- Upload to DraftKings is Ben's. This record does not show that the file was uploaded or that these lineups were entered.

## Entries in v3

| Entry | Contest (id) | Fee | CPT | FLEX | Salary | COL / STL |
|---|---|---|---|---|---:|---|
| 5280375251 | $100 Quarter Jukebox (196302350) | $0.25 | T.J. Hughes | MacKinnon, Blackwood, Makar, Landeskog, Jiricek | 50,000 | 5 / 1 |
| 5280383144 | $100 Quarter Jukebox (196302350) | $0.25 | Broberg | MacKinnon, Thomas, Holloway, Snuggerud, Jiricek | 49,600 | 1 / 5 |
| 5280375310 | $75 Daily Dollar, single entry (196302351) | $1 | Binnington | Thomas, Holloway, Snuggerud, Lehkonen, Dvorsky | 49,300 | 1 / 5 |
| 5280383369 | $1 Triple Up, Top 9 Win $3 (196302810) | $1 | Carlo | MacKinnon, Blackwood, Makar, McTavish, Binnington | 50,000 | 3 / 3 |

Independent check against the salary file (not the referee): each lineup has 6 unique players, salaries sum to at most
50,000 with CPT at its CPT salary, and both teams appear. Data/entered rows equal the delivered file's entry rows.

## Status as printed

FILE_VALID=TRUE, NEWS_STATE=NONE, MODEL_STATUS=PRIOR, SEARCH_STATUS=FEASIBLE, DELIVERY_STATUS=CHECKED,
PAYOUT_SOURCE=PRIOR, OUTCOME_CALIBRATION=UNVALIDATED, FIELD_CALIBRATION=PRIOR, MARKET_COVERAGE=0/1 (1 STALE),
RISK_BUDGET=OK, GOALIE_CAP=LINEUPS 2/4 (fee floor 0.50 above the 0.40 budget, relaxed from 1 because 2 goalies are
usable). Route milp, no relaxations. Portfolio p_lose80 0.1969 (model figure, uncalibrated).

## Goalies at build time

- Binnington (STL): CONFIRMED, Daily Faceoff 2026-10-03 23:54Z. In 2 entries (5280375310 as CPT, 5280383369).
- Blackwood (COL): EXPECTED only (DK Starting=P; Daily Faceoff "Likely"; no team or beat-reporter confirmation found).
  In 2 entries (5280375251, 5280383369). A scratch would make those two entries need Wedgewood.
- Wedgewood (COL) and Hofer (STL): backups, in no entry. The researcher read Hofer's stored CONFIRMED state as stale from Friday's start.
- DK status excluded 7 OUT/IR players (Toropchenko OUT; Mailloux, L'Heureux, Merkulov, Tucker, Jecho, O'Connor IR).
- The COL Daily Faceoff lines page was 58.6 h old, so no COL line or power-play data fed the model. T.J. Hughes has a DK
  average of 0 and an unverified role; he is CPT in 5280375251.

## Research and QA

- Researcher: 0 overrides accepted; Blackwood left unresolved (above).
- QA round 1 (adversary, 3 proposals, all rejected on paired draws; the controller labels each "inside the band" though
  every gain is negative): CPT Binnington to CPT Makar, gain -10.51 (SE 0.62), tail 11.90 to 1.38; Dvorsky to Roy, gain
  -3.01 (SE 0.43), tail 11.90 to 8.88; exclude Hughes, gain -0.32 (SE 0.07), tail 0.51 to 0.19.
- The adversary sent a second reply after the correction message below, with two other swaps (Thomas to Nelson in
  5280383144, Lehkonen to McMichael in 5280375310). They were never applied or simulated: round 1 was already recorded.
- Ben asked for one more round. The controller refused it (`round 1 did not permit another round (zero changes
  accepted: QA stops)`); rounds 2 and 3 open only after a round accepts a correctness repair. Nothing was bypassed.
- A packet value was mistyped when relayed to the adversary (the Triple Up sample's objective label and exp_payout_se)
  and corrected by message. Whether its first reply saw the correction is not known; neither reply cites those two
  values.

## Assumptions to remember when reading the results

- Field sizes were family priors, not DraftKings data: 5,000 for both large_gpp contests and 200 for the cash contest.
  The DK contest endpoints returned HTTP 403, so the sizes were never read. The contest names imply much smaller fields
  (about 400 for $100 at $0.25, under 100 for $75 at $1, about 27 for Top 9 Win $3). Nobody verified those either.
- Contest family came from the name: the Triple Up as cash, the other two as large_gpp (default). Payouts are PRIOR.
- Game intensity came from the model (market odds stale), so the prior-based mean and ownership figures are uncalibrated.

## For the standings analysis

- Join on entry id and contest id from `data/entered/showdown-20261003-64c4bcdd60.csv`.
- Worth checking after results: Hughes as CPT in a 5 COL / 1 STL build, the two STL-heavy entries against the one
  COL-heavy entry, and which goalie actually started.
- If a refresh or late swap changes the file, run `scripts/standings_checklist.py --save-entered
  outputs/<slate>/DKEntries.csv` again and add a line here.
