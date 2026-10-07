# C40 decision rule: does the Daily Faceoff "Last updated" stamp move when the lines change? (backlog B73)

Written and committed 2026-10-07 as `8cf1dbd` at 15:06:04Z (10:06 local) BEFORE any analysis of the stamp against the lines. The git
history is the proof of order: this file's commit precedes the measurement script's (`525ca1d`, 15:11:58Z) and the report's (generated
15:09Z, committed with it). Nothing below was tuned on a result. If any line of this rule changes after numbers exist, the change is named,
with the number that prompted it, in the report, in `docs/sources.md`, in the tracker log line and in the final report to Ben.
(Correction, made in the final commit: the first version of this paragraph gave guessed clock times, "about 15:10Z" here and "about 16:00Z"
in the addendum; both are replaced by the commit times from `git log --format='%h %cI'`. Nothing else in the rule changed.)

What had been looked at before this file was written, none of it a comparison of the stamp with the lines: counts of cached pages and of
fetches per team; for the three teams in the card, which skaters had a news item dated after the page stamp (this is how the CHI case below
was found) and where T.J. Hughes sits on the COL page; and the field names of a page.

## The question

`models/roles.py` drops a team's Daily Faceoff lines, power-play (PP) units and tags when the page's own `updatedAt` stamp is more
than `max_line_age_h` (24 h) old. It did so for CHI (28.7 h) and FLA (36.9 h) on 10-01 and COL (58.6 h) on 10-03, in both cloud late
runs on record. That is right only if an old stamp means old lines. It is wrong if the stamp moves only when an editor touches a
page whose lines did not change, because then an old stamp means "unchanged" and the last known lines are the best information.

## Data, frozen

- Material: Ben's local, gitignored raw cache `data/raw/dailyfaceoff/<date>/<sha>.html` (team-page bodies) and the fetch log
  `data/raw/observations/<date>.jsonl` (source `dailyfaceoff`: url, raw_hash, fetched_at). Read only through scratch copies, behind
  the write guard of `scripts/c39_measure.py` (refuses writes under `data/`, `runs/`, `outputs/`; blocks the network).
- **Cutoff:** only fetches with `fetched_at` at or before **2026-10-07T13:30:28Z** (the newest Daily Faceoff fetch on record when this
  rule was written; Ben's Windows capture tasks `nhl-dfs-capture-*` (`tools/capture.py --once`, times in `config/capture.yaml`, about 12 a day)
  keep adding fetches, and the numbers must be reproducible).
- Every fetch in the log counts, repeats of the same body included (the log holds more fetches than distinct bodies; a repeat is
  evidence that nothing changed). A fetch is parsed with the repo's own `parse_team_page`, so the measurement sees what the model sees.
  A body that does not parse as a team page (the goalie page) is counted and set aside.

## Definitions

- **Lines signature (EV):** for each of `f1, f2, f3, f4, d1, d2, d3` (and `d4` when a page has it) the SET of Daily Faceoff player ids
  in that group. Order inside a group does not matter; a player moving from one line to another does.
- **PP signature:** the sets of ids in `pp1` and `pp2`.
- **Lines changed** between two fetches = the EV signature differs (EV), or the PP signature differs (PP), or either (EV+PP).
  NOT counted: news items, injury tags, timestamps, penalty-kill groups, the `ir` group. The goalie depth order is reported
  separately and decides nothing.
- **Stamp moved** = `updatedAt` differs between the two fetches.
- **Fetch order:** per team, by `fetched_at`.
- **Stamps going backward:** a fetch whose stamp is older than the newest stamp already seen for that team is a stale cached copy.
  It is listed in the report and removed from that team's sequence before anything else is computed, so no pair or episode can touch
  it (otherwise it would fake a moved stamp or a changed line).
- **Pair:** two consecutive fetches of one team at least 20 minutes apart.
- **Episode:** one (team, stamp) pair, however many times it was fetched. The floors and the R test below count episodes, never
  fetches: one stamp can be fetched six times in a day, and 30 fetches could be five pages.
- **Stamp age** of a fetch = `fetched_at` minus its stamp.

## What is counted

1. **The 2x2 over pairs**, once each for EV, PP and EV+PP:

   | | lines changed | lines unchanged |
   |---|---|---|
   | stamp moved | A | B |
   | stamp not moved | **C (the dangerous cell)** | D |

   Headline **M = C / (A + C)**: the share of real lines changes the stamp failed to move. C is the dangerous cell because it is
   where we would present changed lines as unchanged. U = the one-sided 95% Wilson upper bound of M (z = 1.645:
   (p + z^2/2n + z*sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n), with p = C/n, n = A + C). Also reported: A/(A+B) and C/(C+D), per team,
   and for the first and second half of the window (split at the median `fetched_at` of the pairs).
2. **R, the stale-still-right rate, over episodes**, once per band of stamp age: 12 to 24 h (the reference: pages the gate already
   trusts), 24 to 48 h, 48 to 72 h, 72 to 96 h. For each episode with a fetch whose stamp age is in the band, take its first such
   fetch (time t) and compare its EV signature with the last fetch of that team inside [t + 6 h, t + 24 h]. No fetch in that window:
   the episode has no follow-up and is left out of R (a window shorter than 6 h cannot show a change; it would inflate R). R = share
   of episodes with a follow-up whose lines are equal. A band with fewer than 10 episodes with a follow-up is "not measured".
   R for the PP signature is reported too.

## Thresholds (judgment; no earlier measurement stands behind any of them; each has its reason)

- **BLOCKED (the data cannot answer)** if A + C (EV) is under 30 real lines changes, or the stale episodes (stamp age 24 h or more)
  with a follow-up number under 30, or the reference band is "not measured".
  Reason: under about 30 events a proportion near 5% cannot be told from 15%.
- **M test (the stamp's blind spot):** passes when M(EV) is 5% or less AND U(EV) is 10% or less. Reason: kept lines act at FULL
  strength, not damped. The page weight (at most 0.35) only moves a player's chance to dress; his line, his PP unit and, for a call-up,
  role ice time are applied whole (`apply_state`), which is how a page 23 h old is treated today. A miss therefore puts a player on the
  wrong line or PP unit for that game at full strength. One real change in twenty is the most I will let the stamp hide, and the upper
  bound stops a small sample from passing by luck. A point estimate of 5% or less with U above 10% is BLOCKED (inconclusive).
- **R test (is an old page still right as often as a page we already trust):** a band passes when it is measured and
  R(band) is at least R(12-24 h) minus 0.10. Reason: the gate already accepts pages up to 24 h old, so a kept page should be no
  materially worse than that, and 10 points is a visible gap on episode counts of this size.
- **PP test:** needs at least 15 PP-change events (A + C for PP) and M(PP) of 5% or less with U(PP) of 10% or less. Fewer events means PP is
  "not measured" and PP units are not kept.

## Verdict, mechanical, in this order

1. A BLOCKED condition above holds: **BLOCKED** (written with its reason; gate unchanged; tracker row BLOCKED).
2. M(EV) above 5%: **REJECT**. Point estimate of 5% or less with U(EV) above 10%: **BLOCKED**.
3. Band 24-48 h not measured: **BLOCKED**. Band 24-48 h measured and failing the R test: **REJECT**.
4. Otherwise the cap `stale_lines.max_age_h` is the upper edge of the highest consecutive passing band among 24-48, 48-72, 72-96 h
   (a band that is not measured or fails stops the run of bands). Cap 48 or 72 or 96 hours: nothing older is ever kept, so there is no
   uncapped adopt.
   - **ADOPT:** the cap is 72 or more AND the PP test passes (cap used: the measured cap, at most 96).
   - **ADOPT WITH A LIMIT:** the cap is 48, or the PP test fails or is not measured (`keep_pp: false`: lines kept, PP units not).
   COL at 58.6 h sits in the 48-72 h band: if that band is thin or fails, COL stays dropped, and that is an honest result.
3a. **REJECT** leaves the gate as it is, writes the finding to `docs/sources.md` and a backlog row, and the chunk is DONE
   (the tests then pin the unchanged gate and the written finding).

## What counts as "newer contrary news" (flag 35; fixed here, NOT loosened by any number)

A team whose page is older than the gate but within the cap loses its lines anyway (falls back to role priors, as today) when any of:

- (a) a lineup skater (f1-f4, d1-d3, pp1, pp2) has DK status OUT (IR maps to OUT);
- (b) a Daily Faceoff injury tag `out` or `ir` sits on a lineup skater, or an `ir`-group skater is not OUT in DK (DF says injured, DK
  says healthy: he was probably activated);
- (c) ANY skater on the page (lineup, power play, penalty kill and the `ir` group; goalies excluded because goalie news runs on its own
  path) has a Daily Faceoff news item dated after the page's stamp.

The measurement also reports, as information only, whether (c) predicts a lines change. It cannot relax (c): the card and Ben's
instruction both forbid relaxing the gate for teams with news newer than the page.

## Reported but deciding nothing

1. How often the lines listed on the last page fetched before a game matched the skaters who dressed (box scores in
   `data/raw/nhl_boxscore`; 25 games are cached, 09-26 to 10-03, so this is a sanity check, not a test), split by fresh stamp (24 h or less)
   and stale stamp.
2. For the first fetch of every episode: a skater news item after the stamp (the (c) rule) against a lines change at the follow-up.
3. How long a team's lines typically stay unchanged (runs of equal EV signatures), in hours.
4. Whether tags and news items move without the stamp moving (are they live fields joined onto the page?).
5. The goalie depth order against the stamp (same 2x2).

## Caveats written before the numbers

- The window (fetches from 2026-09-28 to 2026-10-07) is preseason into opening week, when lines churn more than in midseason
  (roster cuts, call-ups). That makes real changes more frequent, which makes this window HARDER on an ADOPT, not easier. It also means a
  REJECT that rests on churn alone should be read as "not shown for this window" rather than "never".
- 25 box scores are all there is to check a page against what actually dressed.
- A fetch is a snapshot of the page as Daily Faceoff served it (a CDN may serve a copy a few minutes old); a few minutes of lag cannot
  turn a day-scale pattern.

## Addendum written AFTER the first look (2026-10-07, committed in `c496816` at 15:17:07Z): the procedure for a second look

This addendum changes no threshold and no definition above. The first look (cutoff 2026-10-07T13:30:28Z) gave BLOCKED: 4 misses in 85 real
lines changes (4.7%), upper bound 10.08% against the 10% limit (`2026-10-07_c40_df_stamp_measurement.md`). So that an inconclusive result
cannot pass by repetition, the follow-up is fixed now:

1. There is exactly ONE second look, at the fixed cutoff **2026-10-21T13:30:00Z**, by the same script and the same rule, over all fetches
   up to that cutoff (the first-look fetches included). Nobody re-runs it earlier or at another cutoff.
2. It is recorded as the second look in its report and in `docs/sources.md`.
3. Its mechanical verdict is final for this rule: ADOPT, ADOPT WITH A LIMIT or REJECT as above. If it is BLOCKED again (a floor not met or
   the bound still above 10%), it counts as REJECT: the gate stays, the finding is written, the chunk closes.
   Before running it, the session checks that fetches AFTER 2026-10-07T13:30:28Z exist in the log. They come from the Windows tasks
   `nhl-dfs-capture-*` (read-only check on 2026-10-07: 12 tasks, state Ready, next run 11:30 local that day); if those tasks were removed
   and no fetch arrived, the second look would only repeat the first table, and the session reports that and asks Ben (flag 35) instead of
   closing C40 on a table that holds no new evidence. The cutoff stays fixed either way.
4. Any different rule (for example a variant that keeps only the top lines, after noticing that all four EV misses were f4 or d3) must be
   committed as its own rule file before it is run, and may be run only on fetches after 2026-10-07T13:30:28Z, so the data that suggested it
   cannot be used to test it.
