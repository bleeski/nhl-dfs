# Slate review: 2026-09-29 (regular-season opening night)

Sources: Ben's five DraftKings standings exports (data/standings/inbox/2026-09-29/, gitignored), the files Ben was
given (scratch runs 20260929-222125-classic v3 and 20260930-013521-showdown v3), the scenario caches of those runs and
of a role-state rerun (b9real, as of 20:00Z). One slate is one draw: every model figure below is evidence to log, not
a reason to retune anything by itself (plan section 12).

## Results

All 7 entries were entered exactly as recommended. Fees $3.70.

| Contest | Fee | Field | Our lineup | Points | Rank | Result |
|---|---|---:|---|---:|---:|---|
| Classic $1K Daily Dollar (single entry, large GPP) | $1 | 1,189 | MTL stack (Suzuki, Danault, Caufield, Demidov, Hutson, Matheson), Barbashev, Bouchard, **G Adin Hill** | 95.6 | 532 | $0 (cash line rank 285, 121 pts) |
| Classic $1 Double Up (single entry, cash) | $1 | 229 | McDavid, Miller, Caufield, Donato, Anderson, Hutson, Zadorov, Bouchard, G Shesterkin | 110.0 | 108 | line at about 114 pts if the top 100 are paid: likely just missed |
| Showdown $0.25 Quarter Jukebox | $0.25 | 713 | CPT Eichel, Stone, Frondell, Barbashev, Hanifin, Dowd | 80.35 | 101 | $0.50 |
| Showdown $0.25 Quarter Jukebox | $0.25 | 713 | CPT Spencer Knight, Kane, Frondell, Kantserov, McNabb, Greene | 48.1 | 582 | $0 |
| Showdown $1 Daily Dollar (single entry) | $1 | 107 | CPT Carter Hart, Eichel, Andersson, Hertl, Barbashev, Kaiser | 77.6 | 18 | top 17%: prize table unavailable (likely a small cash) |
| Showdown $0.10 Dime Time | $0.10 | 1,164 | CPT Eichel, Marner, Andersson, Barbashev, Hanifin, Levshunov | 76.95 | 210 | top 18%: prize table unavailable |
| Showdown $0.10 Dime Time | $0.10 | 1,164 | CPT Rasmus Andersson, Eichel, Marner, Theodore, Barbashev, Kaiser | 68.85 | 448 | top 39%: likely $0 |

Known winnings $0.50; three prize tables are unknown because DraftKings answers HTTP 403 to contest pages from this
machine today (the recorded tables of 195958173 and 196048725 came from 2026-09-28). The DraftKings "My Contests"
history shows the settled amounts.

## What worked

- Showdown: Eichel as Captain (the top-1% lineups' dominant Captain) in 2 of 5 entries; 4 of 5 entries finished in
  the top 40% and 3 in the top 18%. Both confirmed goalies (Hart, Knight) were used; the Hart-Captain single entry
  finished 18 of 107.
- The role state (C7, wired into the run in C10): goalie projections from the C8 table without roles averaged 3.2
  points against 5.55 actual (correlation 0.45); with roles 5.49 against 5.55 (correlation 0.66). The rerun with
  roles picked Shesterkin and Dobes; Dobes was the goalie in 5 of the 11 top-1% lineups of the single-entry GPP.
- Evan Bouchard (60.4 points, 26% owned in the GPP) was in both Classic lineups.

## What did not work

1. **Adin Hill did not start** (0 points). Daily Faceoff showed Carter Hart confirmed at 18:22Z, five hours before
   lock, but the Classic file had been built by the C8 pipeline before the role state was wired into `run`, and
   the fast late swap does not treat a non-starting goalie as a repair (backlog B17). Ben was told to swap Hill for
   Hart by hand; the entry went in with Hill. With Hart the lineup scores 111.4 (about 350th): still no cash, but
   180 places better.
2. **The Classic GPP lineup was one bet.** Six Montreal skaters (23.7 points combined) plus a goalie from another
   game. C8's own shared-failure report flagged it (in Montreal's bottom-20% scenarios, 72% of fees cashed nothing). In a
   single-entry GPP a stack is normal; six of nine from one team is extreme, and the game went under.
3. **The cash lineup was built against a field that did not exist.** The ownership prior missed the news-driven chalk
   badly in the Double Up: Vasily Podkolzin 50.7% drafted (predicted 4.7%), Hampus Lindholm 43.7% (3.7%),
   Shesterkin 33.2% (5.0%). The field's consensus (McDavid, Draisaitl, Podkolzin, Lindholm, Bouchard) scored well,
   which raised the cash line; our Double Up entry held low-owned punts (Donato 0.4%, Anderson 1.3%, Zadorov 1.8%)
   and finished about 4 points under the 100th-place score. Ownership correlation, predicted vs actual: Classic 0.43 to 0.54 (mean
   error 4 to 6 points), Showdown 0.52 to 0.70.
4. **Contrarian Captains did not pay**: Spencer Knight (12.6 as Captain) and Rasmus Andersson (1.6% owned as Captain,
   6.45) were the two worst Showdown entries.
5. Projections, one night: forwards correlation 0.27 to 0.33, defense 0.48 to 0.50, mean bias under 1 point; single
   slates are noisy and this is not evidence against the model. The biggest misses were news-driven roles
   (Podkolzin on McDavid's line, 30.5 points, projected 7) and a goalie projected as a starter who was not.

## The two objectives

- **Contest objective** (top-1% payout in GPPs, clearing the line in cash): the Showdown GPP entries did their job
  (top 14% to 18% on the Eichel builds). The Classic GPP entry was a single high-variance thesis with a non-starting
  goalie. The cash entry optimized against a wrong field: `p_clear_line` is only as good as the field model.
- **Portfolio risk** (losing 80% or more of fees at most 60%; concentration): the modeled figure was 0.35 for Classic
  and 0.19 for Showdown. Realized: known payouts $0.50 of $3.70 so far. Both Classic entries shared Bouchard,
  Hutson and Caufield, so the "safety" entry and the tournament entry failed together on Montreal.

## Changes proposed (backlog rows B17, B20 to B24)

1. **Goalie gate (B17, now High):** the engine already sees confirmations (Daily Faceoff) and the researcher agent
   confirms every portfolio goalie by web search (C10). Close the remaining gap in code: late swap and refresh
   repair any open-cell goalie whose team has another CONFIRMED starter, and every report ends with a goalie table
   (entry, goalie, CONFIRMED / EXPECTED / NOT STARTING, source and time). No file is handed over with a goalie known
   not to start.
2. **Pre-lock refresh on a schedule (B23):** a Task Scheduler job runs `refresh` at T-60 and T-20 for the day's
   runs and sends Ben a notification when a file changes, so late news does not depend on anyone remembering.
3. **Ownership from news (B20):** feed Daily Faceoff line and PP changes and confirmed goalies into the ownership
   features, and fit the field model from standings as they accumulate (tonight's five exports are the first real
   labels; FIELD_CALIBRATION stays PRIOR until the C12 gate).
4. **Cash entries track the field (B21):** for cash contests, simulate the line from a field that includes the
   consensus value plays; do not share the tournament entry's stack (decorrelate the safety leg).
5. **Stack-size preference for single-entry GPPs (B22, hypothesis):** measure over many slates whether 5+ skaters
   from one team helps top-1% rate before changing anything.
6. **Record payouts (B24):** save each contest's prize table at build (Phase B) into the run so settlement never
   depends on a later fetch; build C11 now that standings exist.
