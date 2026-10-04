# Standings synthesis: 2026-09-30 to 2026-10-02 (17 contests, 25 entries)

Evidence and history, not instructions (CLAUDE.md, Authority 3). Written 2026-10-03 from the 17 DraftKings exports Ben
dropped in `data/standings/inbox/` (now filed under `inbox/<slate date>/<contest id>/`, gitignored), the lobby captures
under `data/raw/dk_lobby/` (contest names, fees, sizes), NHL box scores and rosters (teams, decisions), and the frozen
09-30 run. Every table below comes from `scripts/standings_synthesis.py`; the full per-contest output is
`reviews/2026-10-03_standings_synthesis_detail.md`. Other entrants are never named.

Caveats. One week, three slate sizes, 52,490 Classic and 341 Showdown lineups. "Cash" uses DraftKings' template tables
where a cached table has the same name and size (two contests) and otherwise a proxy of 23.5% of max entries (every
cached GPP template pays 22.9% to 24.0%); the Triple Up pays 3 by name. Payouts are unknown until Ben fills the
`winnings.csv` files (section 6). Fewer than 0.4% of lineup slots had a name the rosters could not resolve
(injured-reserve players are not on the NHL roster endpoint); every lineup's listed points reproduced from its
players' FPTS, so the parse is sound. One slate is one draw: these are hypotheses to test, not reasons to retune.

## 1. What was filed

| Slate date | Draft group | Games | Contests (entries, fee) | Our entries | Fees | Run on this machine | Settled |
|---|---|---|---|---|---|---|---|
| 2026-09-30 | 154305 | 3 (PIT@PHI 7-0, NYI@TOR 1-2, LAK@COL 4-8) | $5K mini-MAX (10,750), $6K Hip Check (7,134), $1.5K Quarter Jukebox (6,854), $750 Daily Dollar SE (891) | 5 | $3.00 | 20260930-233424-classic (forecast from 20260930-214909) | yes, payouts UNKNOWN |
| 2026-10-01 main | 154311 | 8 | $2.5K mini-MAX (5,945), $200 Dime Time (2,317), $250 Quarter Jukebox (1,189) | 5 | $1.20 | none | cannot (B47) |
| 2026-10-01 late | 154316 | 4 (SEA@CGY 6-1, CHI@UTA 0-6, EDM@VAN 9-7, FLA@SJS 3-4) | $150 Quarter Jukebox (713), $20 Dime Time (163), $100 Daily Dollar SE (118) | 5 | $1.70 | cloud container only (run 20261002-002102-classic) | cannot (B47) |
| 2026-10-02 classic | 154323 | 5 (NYR@DET 2-0, WSH@CAR 5-2, BOS@WPG 4-3, STL@DAL 4-0, ANA@VGK 4-3) | $1.5K Quarter Jukebox (7,134), $2.5K mini-MAX (5,945), $200 Dime Time (2,378), $1K Daily Dollar SE (1,145) | 6 | $2.20 | none | cannot (B47) |
| 2026-10-02 showdown | 154330 | ANA@VGK 4-3 | $20 Dime Time (233), $100 Daily Dollar SE (103), $1 Triple Up (9) | 4 | $2.20 | none | cannot (B47) |

The checklist (`scripts/standings_checklist.py`) now reads awaiting 0, filed 4, settled 5. It cannot see the 13 contests
of 10-01 and 10-02 because no entries file for them exists here (B48).

## 2. Our results

Cohort: top1% = rank within 1% of the field; cash = within the paid places (proxy unless noted).

| Date | Contest | Fee | Rank / field | Pts | Build | Outcome |
|---|---|---|---|---|---|---|
| 09-30 | $5K mini-MAX | $0.50 | 8,403 / 10,750 | 54.1 | 4-2-1-1, Stolarz | no cash (table: $0) |
| 09-30 | $6K Hip Check | $1 | 5,635 / 7,134 | 53.4 | 5-2-1 LAK, Kuemper (-13.3) | no cash |
| 09-30 | $1.5K Quarter Jukebox | $0.25 | 4,795 and 6,776 / 6,854 | 65.4, 10.0 | 6-1-1 TOR; 3-2-1-1-1, Kuemper | no cash |
| 09-30 | $750 Daily Dollar SE | $1 | 835 / 891 | 34.0 | 3-2-1-1-1, Kuemper | no cash |
| 10-01 main | $2.5K mini-MAX | $0.50 | 3,100 / 5,945 | 107.8 | 3-2-1-1-1, Woll | no cash (line 138.5) |
| 10-01 main | $200 Dime Time | $0.10 | 664 and 2,298 / 2,317 | 135.2, 33.3 | 2-1-1-1-1-1-1 (no stack), Shesterkin; same shape, Lankinen (-18.9) | no cash (line 141.1) |
| 10-01 main | $250 Quarter Jukebox | $0.25 | 660 and 1,078 / 1,189 | 111.3, 67.2 | 4-1-1-1-1 PHI, Woll; 3-2-1-1-1 | no cash |
| 10-01 late | $150 Quarter Jukebox | $0.25 | 34 and 622 / 713 | 179.0, 80.4 | 5-1-1-1 EDM, Askarov; 3-2-1-1-1 CHI, Wolf | top 4.8%: cash (about $0.75 by the same-size template); no cash |
| 10-01 late | $20 Dime Time | $0.10 | 65 and 111 / 163 | 133.8, 114.1 | 4-2-1-1 EDM; 3-2-2-1 | no cash (line 139.0) |
| 10-01 late | $100 Daily Dollar SE | $1 | 32 / 118 | 142.0 | 6-1-1 EDM, Askarov | no cash (line 147.8) |
| 10-02 | $1.5K Quarter Jukebox | $0.25 | 3,369 and 4,370 / 7,134 | 85.3, 76.2 | 4-3-1 CAR-ANA, Dostal; 4-2-1-1 DAL, Oettinger | no cash |
| 10-02 | $2.5K mini-MAX | $0.50 | 3,276 / 5,945 | 79.2 | 5-2-1 CAR, Bussi (-2.1) | no cash |
| 10-02 | $200 Dime Time | $0.10 | 134 and 1,193 / 2,378 | 122.8, 83.3 | 5-2-1 STL, Thompson; 3-2-1-1-1 | top 5.6%: cash (about $0.20 to $0.30); no cash |
| 10-02 | $1K Daily Dollar SE | $1 | 691 / 1,145 | 73.3 | 6-1-1 CAR, Bussi | no cash (table: $0, 285 paid) |
| 10-02 SD | $20 Dime Time | $0.10 | 2 and 198 / 233 | 89.7, 32.1 | CPT Gauthier, 5 ANA + Eichel; CPT Hart (G) | 2nd place (top 1%); no cash |
| 10-02 SD | $100 Daily Dollar SE | $1 | 47 / 103 | 60.6 | CPT Stone (1.3 pts), 5 VGK + Gauthier | no cash (line 68.9) |
| 10-02 SD | $1 Triple Up (top 3 paid) | $1 | 4 / 9 | 61.2 | CPT Luneau, Hart + Stone in FLEX | 4th, one place short (line 71.6) |

Three cashes in 25 entries. Known money: none yet; the 09-30 settle books fees $3.00 and payouts UNKNOWN; the other
20 entries ($7.30) cannot be booked until B47. The 09-29 slate (7 entries, $3.70, $0.50 known) is unchanged.

## 3. Question 1: what stack shapes should we target

### Classic

Pooled over 14 contests (shape = skaters per team, goalie excluded; every lineup counted):

| Cohort | Lineups | 3+ stack | 4+ stack | 5+ stack | Teams | Own sum % | Chalk (>=25%) | Punts (<=5%) | Dup % | G with stack | G vs own skaters | G won | Bring-back | D at UTIL | Two 3+ stacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Top 1% | 553 | 98.5 | 83.5 | 36.7 | 3.4 | 143.6 | 1.7 | 1.7 | 11.2 | 32.7 | 5.4 | 91.8 | 39.2 | 24.4 | 38.9 |
| Cash | 12,740 | 95.1 | 67.8 | 23.7 | 3.6 | 140.9 | 1.6 | 1.6 | 13.1 | 39.1 | 11.2 | 81.5 | 35.7 | 24.7 | 43.2 |
| Field | 52,490 | 93.5 | 65.7 | 21.3 | 3.6 | 132.7 | 1.2 | 1.5 | 11.9 | 44.0 | 11.7 | 54.1 | 34.1 | 21.3 | 44.8 |

| Shape | Top 1% | Cash | Field |
|---|---|---|---|
| 4-3-1 | 28.9 | 30.6 | 31.4 |
| 6-1-1 | 19.9 | 8.1 | 6.3 |
| 5-2-1 | 11.9 | 12.5 | 12.2 |
| 4-2-1-1 | 11.8 | 8.8 | 8.0 |
| 3-3-1-1 | 6.9 | 8.0 | 8.3 |
| 3-3-2 | 3.1 | 4.6 | 5.1 |
| 3-2-2-1 | 2.7 | 6.8 | 6.9 |

The pooled table hides the slate effect, which is the real finding:

| Slate | Games | Highest team score | Top-1% with 5+ stack | Field with 5+ stack | Top-1% modal shapes |
|---|---|---|---|---|---|
| 09-30 | 3 | COL 8, PIT 7 | 45 to 65% | 20 to 23% | 6-1-1 (39 to 47%), 4-3-1, 5-2-1 |
| 10-01 main | 8 | EDM 9 | 8 to 21% | 14 to 18% | 4-3-1 (25 to 38%), 4-2-1-1, 5-2-1 |
| 10-01 late | 4 | EDM 9, UTA 6, SEA 6 | 14 to 100% (tiny fields) | 5 to 26% | 4-3-1, 6-1-1, 5-1-1-1 |
| 10-02 | 5 | WSH 5 | 4 to 36% | 14 to 25% | 4-3-1 (34 to 48%), 3-3-1-1, 3-3-2, 2-2-2-1-1 |

Reading: the field already stacks. Almost every lineup has a 3-stack and two thirds have a 4-stack, so "stack 3" is
not an edge; it is the baseline. What separates the top 1% is the size of the primary stack relative to the slate's
blow-up. On the 3-game slate, where Colorado scored 8, six-man Colorado stacks were a fifth of the field's 6-1-1s but
40 to 47% of the top 1%. On the 5-game slate, where no team scored more than 5, the top 1% had fewer 5+ stacks than
the field and the winners were 4-3-1, 3-3-2 and 2-2-2-1-1 built across the two one-goal games (BOS@WPG, ANA@VGK).
Two 3+ stacks were slightly less common in the top 1% than the field; a bring-back (one skater from the primary
stack's opponent) slightly more (39% vs 34%). A defenseman at UTIL was a touch more common in the top 1% (24% vs 21%).

Target, as a hypothesis (B44): a 4-3-1 core with a bring-back, a 6-1-1 / 5-2-1 sleeve sized to the slate's blow-up
probability (fewer games and a lopsided total: bigger sleeve), and 3-3-2 game stacks when no team is a clear
blow-up candidate. The engine's current knob is a team3 stack rule in the field model and a lineup-ownership
tie-break; it has no notion of primary stack size.

### Showdown (one game, ANA@VGK 4-3)

| Cohort | Lineups | Split 5-1 | 4-2 | 3-3 | Any goalie | Goalie as CPT | Own sum % | Dup % |
|---|---|---|---|---|---|---|---|---|
| Top 1% | 4 | 75 | 0 | 25 | 25 | 0 | 157 | 0 |
| Cash | 88 | 42 | 33 | 21 | 31 | 14 | 162 | 24 |
| Field | 341 | 51 | 28 | 19 | 51 | 23 | 170 | 22 |

5-1 was already the field's modal split; the top 1% was 5-1 of the right team. Anaheim 4+ stacks were 15 to 17% of
the field (Vegas 61 to 78%) and 100% of the Dime Time's top 1%. The question in Showdown is which team, not which
split. Half the field carried a goalie and a fifth to a third made him Captain; no top-1% lineup had a goalie Captain
and only one in four had a goalie at all. Top-1% Captains were 3 to 11% CPT-owned skaters (Gauthier, Luneau,
Olofsson) plus Eichel; the field's chalk Captains Hart (-2.8) and Stone (1.3) busted. (B49)

## 4. Question 2: do we need to change ownership assumptions

Yes, in three places.

1. The field is all stackers. `config/ownership.yaml` gives large_gpp a 25% stacker share, and the only stack rule is
   team3. Observed: 93.5% of lineups with a 3+ stack, 65.7% with 4+, 4-3-1 the modal shape. The frozen 09-30 forecast
   put Toronto's stack frequency at 42% (actual 21 to 24%) and Colorado's at 19% (actual 38 to 51%). Per-player grades
   were acceptable (MAE 3.9 to 4.3, Pearson 0.71 to 0.75, top-10 recall 0.4 to 0.6 across the four contests), so the
   miss is in team concentration, not in the player ranking. (B43)
2. The chalk is where the goals are expected, and the top 1% keeps it. The field's most-owned players were the
   highest-total game's stars (MacKinnon 47 to 60%, McDavid 32 to 55%) and the obvious favorite's stack (Dallas 30%
   on 10-02). Top-1% lineups held the slate's top two chalk pieces at 90 to 100% and differentiated with one to three
   low-owned correlated teammates: Lehkonen 13% owned, 57 to 67% of the top 1%; Nick Robertson 12 to 15%, 56 to 73%;
   Carcone 2 to 5%, 43 to 58%; Kapanen 4 to 6%, 43 to 100%. The top 1% was chalkier than the field whenever the
   chalk hit (own sum 167 vs 154 on 09-30, 111 vs 80 on 10-01 main) and less chalky only when the chalk team was
   shut out (Dallas, 10-02). Fading the top chalk is not what wins; being right about the second-tier pieces is. (B46)
3. Goalie ownership is flat and the winner is mid-owned. Field goalie ownership topped out at 15 to 24% and was spread
   across five or six names. The goalie in the top 1% was a 9 to 15% option on a favorite that won or posted a
   shutout: Silovs 14% owned, 51 to 78% of the top 1%; Daccord 10 to 22%, 47 to 100%; Hofer 9 to 11%, 55 to 67%.
   The field's chalk goalies (Kuemper, Hart, Luukkonen) were 0% of the top 1%. (B45)

Also observed: blank lineups are 0.3 to 0.8% of entries; duplicate lineups are 7 to 16% of large fields, with one user
entering 150 identical lineups in the 09-30 mini-MAX (the largest duplicate groups were 150, 45 and 29); the top 1% is
less duplicated than the field (11% vs 12%), the single-entry contests least of all.

## 5. Question 3: which lineups finish in the top 1%, which cash

Cash (top 23%) looks like the field plus two things: the winning goalie (81.5% vs 54.1%) and the slate's top chalk
(1.6 chalk players vs 1.2). Its shape mix is the field's shape mix. In the single-entry Daily Dollars the cash
cohort was even chalkier (own sum 189 vs 168 on 09-30). Cashing is being right about the goalie and holding the
consensus core.

Top 1% adds three things to that: the winning goalie almost always (91.8%), a bigger primary stack of the team that
blew up (4+ in 83.5%, 5+ in 36.7%), and the low-owned correlated depth pieces listed above. Its goalie is less often
paired with its own stack (32.7% vs 44.0%) and almost never faces its own skaters (5.4% vs 11.7%). The Daily Dollar
winners on all three dates were 6-1-1 or 5-1-1-1 of the blow-up team with a goalie from another game.

## 6. Our lineups against that picture

- 09-30 (engine, runs 20260930-*): zero Colorado skaters in five entries, Matthews in four (2.8 points), Kuemper in
  three (-13.3). RUN_NOTES: all three games priced MODEL (team codes COL, LAK, NYI, PHI, PIT unverified for the odds
  feed), "COL: listed skaters cover 9.3 F and 5.9 D, league goal rate used", LAK@COL total 5.99, and the game cap
  split fees 0.33 per game. Colorado stacks were 42% of the field and 95% of the top 1%. This is the B35 class of
  defect (fixed 2026-10-02) plus a league-average team rate for Colorado; a scratch rerun should confirm (B51).
- 10-01 late (engine, cloud): Edmonton stacks were right (EDM scored 9) and one entry reached the top 4.8%. The
  single entry's 6-1-1 missed cash by 6 points because its depth slots were Jones (1.5), Ekholm (0.0) and Karlsson
  (2.8), while the top-1% six-stacks used Kapanen (18.3), Murphy (12.3) and Frederic (12.8). Askarov in four of five
  (11.6) against Daccord (24.3) is the B36 concentration, since fixed.
- 10-01 main: five entries built outside any run recorded here; three were seven-team lineups with no stack, the
  opposite of what the field and the winners do. Best finish top 28.7% (135.2, cash line 141.1).
- 10-02 classic: Carolina in four of six, Bussi twice (-2.1), Robertson (DAL, 3.0) twice; the one St. Louis 5-stack
  with Thompson cashed (top 5.6%).
- 10-02 showdown: second of 233 with Captain Gauthier and five Anaheim pieces plus Eichel. The other three Captains
  were Hart (goalie, -2.8), Stone (1.3) and Luneau with Hart and Stone at FLEX; the Triple Up finished one place
  short with Hart and Stone contributing -1.5 points together.

## 7. Bookkeeping done this session

- Filed: 17 exports into `data/standings/inbox/2026-09-30`, `2026-10-01`, `2026-10-02` (zip beside an extracted
  CSV per contest, the 09-29 convention; 196311455 came as a bare CSV). No file was modified.
- Settled 09-30: `settle --run 20260930-233424-classic --standings data/standings/inbox/2026-09-30`: SETTLE=OK,
  FORECAST=PRE_LOCK (graded from 20260930-214909-classic), FREEZE_CHECK=OK, PAYOUT_SOURCE=UNKNOWN, ledger 3 runs on
  2 dates, cumulative known net -$1.00, INCOMPLETE; gate tier every_run, nothing tuned.
- Winnings templates for Ben to fill from DraftKings My Contests: `data/standings/inbox/2026-09-30/winnings.csv`
  (5 rows, written by settle; settle again after filling), `2026-10-01/winnings.csv` (10 rows) and
  `2026-10-02/winnings.csv` (10 rows) (written here; consumed once B47 exists).
- New: `scripts/standings_synthesis.py` with `tests/test_standings_synthesis.py`; rerun after each pull with
  `--dates <slate dates> --offline`.

## 8. Backlog rows added

B43 field-model stack share; B44 primary stack size by slate; B45 goalie as the top-1% factor; B46 chalk kept,
differentiate in depth slots; B47 settle without a local run; B48 checklist blind to contests without an entries
file; B49 Showdown Captain and goalie; B50 template payout tables as a payout source; B51 the 09-30 Colorado miss.
