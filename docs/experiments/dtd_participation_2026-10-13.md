# C24: DTD participation inside the simulator, before and after (information only)

Protocol: flag 106 (written before any code or number; amended before any C24 replay to replay 20260930-210607-classic, the
run that built the 09-30 slate, because 20260930-214909-classic is its refresh child). No threshold, no adoption decision.
Script: `scripts/c24_dtd_replay.py` (run half committed unchanged in 3e7fdba, blob f0bd827e, before the C24 replays; the
compare half was rewritten afterwards, see "Changed after seeing numbers").

## What ran

- Inputs: the saved inputs of runs 20260929-222125-classic (5 DTD skaters) and 20260930-210607-classic (1), entries cloned
  to 30, each run's own created_utc clock, seed 20261013, shipping scenario counts, PYTHONHASHSEED=0. Web inputs time-pinned
  from data/raw/observations into an empty scratch cache; the history store read only; every NHL_DFS root in scratch; the
  write guard of scripts/c17_replay.py: 0 writes refused, 0 network attempts, in every run.
- Trees: origin/master (clean export of 54c6640, whose src equals 1005933) twice, then C24 (clean export of 9816763) twice.
- Both trees saw the same inputs. The original runs fetched their odds and some pages after their created_utc, so those are
  unpinned here: every game is priced on the hockey model (MARKET_COVERAGE 0/4 and 0/3), and no DK contest detail answers.
- The covered set came from each replay's own role state: 09-29 Dakota Joshua (TOR), Ivan Demidov (MTL), Nick Paul (TOR),
  Raphael Lavoie (VGK), Vincent Iorio (NYR); 09-30 Corey Perry (LAK); all at 0.85, all skaters, so the C24 runs drew all of
  them inside the game and masked nobody. TOR has two covered forwards in one group.

## Results

Means are DK points in tenths, priced means on the 8,000 kept selection draws, Monte Carlo standard errors in brackets.

1. Reproducibility. Every draw column of a game with no DTD skater is byte-identical between the trees (09-29: 67 of 67,
   09-30: 87 of 87). On the mini fixture, C24 with every priced person forced back to the mask reproduces origin/master's
   cached draws and lineups byte for byte.
2. The DTD skaters themselves are priced as before (his mean was p x dressed mean under the mask, and is now); all but
   Demidov (z 2.4, one of six) within 2 SE. Each covered skater and his three most-changed teammates (by the size of the
   cross-tree change; every one of them within noise, as expected for a lift of about one Monte Carlo SE):

   | Slate | Skater | Before | After | Change (z) | Most-changed teammates: before to after (z, label) |
   |---|---|---|---|---|---|
   | 20260929 | Dakota Joshua (TOR) | 20.5 [0.4] | 21.1 [0.5] | +0.5 (0.84) | Matthew Knies (TOR) 76.8 to 78.8 (1.59, within noise); Auston Matthews (TOR) 82.9 to 84.1 (0.78, within noise); Jack Roslovic (TOR) 52.1 to 53.2 (1.04, within noise) |
   | 20260929 | Ivan Demidov (MTL) | 59.0 [0.6] | 61.4 [0.8] | +2.4 (2.43) | Lane Hutson (MTL) 93.1 to 95.2 (1.9, within noise); Cole Caufield (MTL) 119.2 to 121.1 (1.26, within noise); Nick Suzuki (MTL) 116.5 to 118.2 (1.18, within noise) |
   | 20260929 | Nick Paul (TOR) | 28.0 [0.5] | 27.6 [0.5] | -0.4 (-0.58) | Matthew Knies (TOR) 76.8 to 78.8 (1.59, within noise); Auston Matthews (TOR) 82.9 to 84.1 (0.78, within noise); Jack Roslovic (TOR) 52.1 to 53.2 (1.04, within noise) |
   | 20260929 | Raphael Lavoie (VGK) | 3.9 [0.2] | 4.1 [0.2] | +0.2 (0.54) | Parker Wotherspoon (VGK) 52.2 to 51.7 (-0.69, within noise); Brayden Mcnabb (VGK) 64.1 to 63.6 (-0.55, within noise); Victor Olofsson (VGK) 49.3 to 49.7 (0.47, within noise) |
   | 20260929 | Vincent Iorio (NYR) | 5.5 [0.2] | 5.7 [0.2] | +0.2 (0.5) | Eeli Tolvanen (NYR) 67.2 to 66.5 (-0.73, within noise); Pavel Dorofeyev (NYR) 96.7 to 97.4 (0.53, within noise); Vladislav Gavrikov (NYR) 66.7 to 67.2 (0.65, within noise) |
   | 20260930 | Corey Perry (LAK) | 36.8 [0.5] | 37.0 [0.6] | +0.2 (0.26) | Cody Ceci (LAK) 50.5 to 51.8 (1.77, within noise); Quinton Byfield (LAK) 83.8 to 85.0 (1.02, within noise); Drew Doughty (LAK) 55.9 to 56.8 (1.11, within noise) |
3. Team totals (the over-absorption check; per team, expected gain = sum of (1 - p) x his dressed mean, observed = summed
   change of every other skater on the team, TOR counted once):

   | Team | Expected gain | Observed gain | SE | z against expected |
   |---|---|---|---|---|
   | MTL | 10.4 | 10.7 | 4.4 | 0.06 |
   | TOR | 8.6 | 10.1 | 4.5 | 0.34 |
   | LAK | 6.5 | 7.3 | 4.0 | 0.21 |
   | NYR | 1.0 | 1.6 | 4.3 | 0.14 |
   | VGK | 0.7 | 0.8 | 4.4 | 0.02 |

   The teammates gain what the mask used to throw away, no more. Single teammates' cross-tree changes are within 2 SE
   (the expected lift per linemate is about one Monte Carlo SE).
4. Where the gain lands, C24 run only, paired (each teammate's mean in the draws where the recomputed participation draw
   said sit against play, about 1,150 of 8,000 sat): Demidov sits, Cole Caufield +8.4 (z 2.7) and Nick Suzuki +7.7 (z 2.7);
   Paul sits, Auston Matthews +7.8 (z 2.5); Perry sits, Quinton Byfield +5.9 (z 2.45). Joshua, Lavoie and Iorio dress
   rarely or play few minutes, so little moves (Iorio's Eeli Tolvanen -2.9, z -1.5, is noise among many comparisons).
5. Lineups (30 entries): origin/master against itself 0 of 30 on both slates. 09-30: C24 against itself 0 of 30, origin/master
   against C24 26 of 30; but origin/master at another seed (20261014, a control added after seeing the numbers) changes 30 of
   30, so the 26 cannot be told apart from re-drawing one game's scenarios. 09-29: the two C24 runs disagree on 24 of 30
   with byte-identical draws (they ran while another project's tests loaded the machine; B98), so 09-29's lineup count
   (29 of 30) is not interpretable.
6. Timings, contended on the C24 side (another project's test suite ran through the C24 replays and both calibrate runs):
   simulation seconds design / selection / referee, 09-29 origin/master 7.4 / 12.1 / 11.4 against C24 15.4 / 15.9 / 14.0;
   09-30 3.1 / 4.6 / 5.0 against 4.5 / 5.7 / 5.7. Whole replays 114 and 154 s against 183 and 161 s. The draw's own cost was
   not measured separately (it adds one small random generator per DTD skater per chunk); these timings were taken on a
   busy machine and do not isolate it.

## Calibration (the card's exit check)

`.\nhl.ps1 calibrate --seasons 1 --out-dir <scratch>` on the C24 tree (as typed, plus --out-dir so nothing was written
before comparing) and the same command on the origin/master export with `--store-root` at the real store: every graded
section (coverage, PIT, rates, co-ceiling, team, deficiencies, notes) identical. Expected: calibrate grades the stat model
conditional on who dressed and never sees a DTD skater. 10 held-out 2025-26 dates (store last written 2026-09-28), 1,500
scenarios each; 139 s and 159 s, both contended (98 s on record, 2026-09-29). Against the 2026-09-29 record some sections
moved through the chunks since then, not C24; the deficiencies are unchanged in kind (points>=3 rate ratio 1.401, co-ceiling
0.547). The C24 report is kept as docs/calibration/2026-10-13.md and .json.

## Changed after seeing numbers (all information only, said here and in the session log)

- The compare step now reports each change with the SE of the difference and its z, labels |z| < 2 as within noise, adds
  the per-team expected-against-observed table and the paired split on the recomputed participation draw (the first
  version split on "not dressed", which mixes in ordinary dressing misses). Advisor review, before this write-up.
- The 09-30 different-seed control run.

## Not done

Line-specific promotion and the replacement's dressing (B128); goalies keep the mask (flag 102); no slate other than these
two was saved on this machine; nothing here was fit or tuned.
