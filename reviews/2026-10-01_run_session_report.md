# Run-session report: 2026-10-01 Classic slate (cloud session)

Purpose: record what went wrong in this lineup-generation session so a dev session can fix it. Actionable rows are in
BACKLOG.md (B34 to B40). This file is evidence and history, not instructions (CLAUDE.md, Authority 3).

Sources: this session's transcript; run 20261002-002102-classic (gitignored, in the container only); the cached odds
payload `data/raw/nhl_partner_odds/2026-10-02/2ccad08ea82fd4e666085591c6fac9c63578a787898152d3cedb78529a4f0d8b.json`; `config/teams.yaml`, `config/risk.yaml`,
`config/exposure.yaml`; `src/nhl_dfs/sim/market.py`, `src/nhl_dfs/build/exposure.py`. Files from Ben: DKSalaries_126.csv
(draft group, 4 games, 10/01/2026) and DKEntries_102.csv (5 entries in 3 contests, total fees $1.70).

## Summary

- Delivered: a legal, referee-checked 5-entry portfolio (v4) at about 8:27 PM ET, 33 minutes before the first lock.
  Independent check against the salary file: cap, slots, nine unique players, skaters from 3+ teams, no OUT or IR.
- Went wrong, by impact: (1) market odds were dropped for 3 of 4 games because `config/teams.yaml` marks only 10 of 32
  teams `dk_verified`; (2) Askarov sits in 4 of 5 entries (85% of fees), the engine's own budget flagged it, and it
  shipped anyway; the dollar-based floor hides what is really a lineup-count problem; (3) the QA round rejected the only
  goalie-diversifying proposal because of an ordering artifact; (4) the cloud container had no bootstrap, which cost
  the first turns; (5) two facts that were in RUN_NOTES.md were missing from stdout and from my first report.
- Not fixed: the portfolio was not rebuilt. No engine, config or source change is in the repo. One config experiment ran
  in a scratch copy outside the repo (section 3, F2).

## 1. Environment: the declined install and everything that was installed

1. Starting state: repo cloned, no `.venv`, `nhl.sh` has git mode 100644 (direct execution gave "Permission denied"),
   no `pwsh`. The `/nhl-run` skill is `shell: powershell` with `disable-model-invocation: true`, so its steps were
   run by hand as `bash nhl.sh <command>`.
2. I ran `uv sync --frozen`. Ben rejected the tool call (interrupt). I treated it as declined and stopped. My turn
   text was "No response requested." with no explanation. Ben wrote "Looks like you're stuck."
3. Read-only checks, nothing installed: clock 8:20 PM ET (first lock 9:00 PM ET); system Python imports. Present:
   yaml 6.0.1, requests 2.34.2. Missing: numpy, pandas, pyarrow, scipy, pytest, tzdata. The engine could not run
   without an install.
4. I asked (AskUserQuestion) whether to run `uv sync --frozen`, naming the missing packages and the lock time. Ben
   answered "Yes, install". Only then was it re-run. Nothing was installed before that approval.
5. Installed into `.venv/` (gitignored), interpreter Python 3.11.15, from `uv.lock`. Full list from
   `uv pip list` afterwards (I saw only the tail of the uv output at install time): 20 entries.

| Kind | Packages |
|---|---|
| Direct (pyproject `dependencies`) | numpy 2.4.6, pandas 3.0.6, pyarrow 25.0.1, scipy 1.17.1, pyyaml 6.0.3, requests 2.34.2, pytest 9.1.1, pytest-timeout 2.4.0, tzdata 2026.4 |
| Transitive | certifi 2026.7.22, charset-normalizer 3.5.1, idna 3.20, iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0, pygments 2.21.0, python-dateutil 2.9.0.post0, six 1.17.0, urllib3 2.8.0 |
| Project (editable) | nhl-dfs 0.1.0 at /home/user/nhl-dfs |

Nothing else was installed: no apt, no global pip, no uv self-update. Other files this session wrote, all gitignored
(`git status` was clean before this report): `runs/20261002-002102-classic/`,
`outputs/classic-20261001-a3565a960f/DKEntries.csv`, fetch caches under `data/raw/` (NHL partner odds, Daily Faceoff
goalie page), and a scratch copy of the repo in the session scratchpad for the what-if run.

Defects (BACKLOG B34):
- E1: no bootstrap for cloud sessions. No SessionStart hook runs `uv sync --frozen`, so the first command fails and
  a permission prompt can stall the session.
- E2: `nhl.sh` is not executable in git, and the run flow exists only as a PowerShell skill.
- E3: CLAUDE.md Commands omits `slate`, `overrides-apply`, `qa-apply`, `scheduled-refresh`, all of which the run flow uses.
- E4 (minor): pytest and pytest-timeout are in `[project].dependencies`, so a runtime install pulls test tooling.
  Two lockfiles exist (`uv.lock`, `requirements.lock`); I used `uv.lock`.

## 2. Timeline (ET, 2026-10-01)

- 8:20 clock and dependency check, install approved and run.
- 8:21 to 8:23 `bash nhl.sh slate` (84 s): run 20261002-002102-classic, v3, `FILE_VALID=TRUE`, goalie gate CLEAR.
- 8:23 to 8:26 researcher (4 goalies, 30 tool calls, 136 s) and adversary (1 call, 144 s) in parallel. Replies saved
  verbatim to `news/overrides_1.json` and `qa/proposals_1.json`; applied 8:26 PM.
- Controller: 2 goalie overrides accepted (Askarov, confidence 0.90; Wolf, 0.85); QA accepted 1 of 4 swaps; v4 published.
- 8:27 DKEntries.csv sent to Ben.
- Ben questioned the Askarov concentration. Analysis found F1. What-if run 8:54 to 8:56 PM changed nothing.

## 3. Findings

### F1. Market odds dropped for 3 of 4 games (BACKLOG B35, B40)

Evidence. RUN_NOTES: "[unverified team code UTA / SJS / CGY/SEA for source nhl_partner_odds] [MODEL: no odds for this
game]". The cached feed was fresh (DraftKings, lastUpdatedUTC 2026-10-02T00:00:38Z, 8 games) and had all four games.

| Game | Engine (MODEL unless noted): home win, total | DraftKings in the cache: home win (no-vig), total |
|---|---|---|
| FLA@SJS | SJS 55.8%, 5.81 | SJS 42.8% (FLA -148, SJS +124), 6.5 |
| CHI@UTA | UTA 49.9%, 6.14 | UTA 65.7% (UTA -218, CHI +180), 6.5 |
| SEA@CGY | CGY 52.8%, 5.98 | CGY 50.4% (-112 / -108), 5.5 |
| EDM@VAN | VAN 34.8% (MARKET), 6.84 | VAN 34.3% (+180 / EDM -218), 6.5 |

Cause. `sim/market.py::match_odds` maps only teams with `dk_verified: true` in `config/teams.yaml`. Verified: BOS, CAR,
CHI, EDM, FLA, MTL, NYR, TOR, VAN, VGK (10 of 32). CGY, SEA, SJS, UTA have `dk: null`. A game with either team
unverified falls back to MODEL (fail-closed by design). The file's own rule says to fill a null from an observed DK
file and set the flag; tonight's DKSalaries.csv observes TeamAbbrev SJS, UTA, CGY, SEA, and the feed uses the same four
codes, so the rule's condition is met.

Impact. The engine projects Askarov at 14.2 points against 9.9 for Wolf and Schmid, 9.0 Levi, 8.8 Vejmelka (mean over
8,000 selection scenarios). That edge rests on a prior that has San Jose as a 56% home favorite with Florida scoring
2.40 goals. Market has Florida favored. My estimate, not re-simulated: Askarov drops several points on market inputs.
Everything outside EDM@VAN was priced on priors.

Related. EDM@VAN is the one MARKET game, and its fit is CAPPED: market intensities clipped to model +/- 35% (away 3.64
vs model 2.67, x1.37), "the fit no longer reproduces the market" (p_home 0.348 vs target 0.343, total 6.84 vs 6.88).
Small today; review whether the clip is right when the model is PRIOR. B5 (odds feed stale, Covers parse failure) did
not occur today and needs a status note. B33 (Daily Faceoff team codes verified for TOR only) looks like the same class
of gap, and one `teams.yaml` fill may close both; the dev session should check. Run flags also show Daily Faceoff role
pages for CHI (28.7 h) and FLA (36.9 h) over the 24 h limit, so their lines and tags were unused.

### F2. Goalie concentration and the risk budget (BACKLOG B36)

Evidence.
- Askarov in 4 of 5 entries: $0.25 + $1.00 + $0.10 + $0.10 = $1.45 of $1.70 = 85.3% of fees. Wolf in 1 entry = 14.7%.
- RUN_NOTES: "no knob setting meets the risk budget; least-risk point chosen (kappa 1: goalie fee share 0.85 > 0.59;
  game fee share 0.85 > 0.59)". The frontier had two points, both at 0.85. Stdout did not show it.
- Why 0.59: the $1 Daily Dollar single entry is 58.8% of fees. `exposure.share_cap` raises the budget to the achievable
  fee floor whenever the floor exceeds it ("goalie_fee_share_max budget 40% is below the achievable floor 59% for these fees; using 59%"). The budget
  then only checks frontier points. It does not constrain selection.
- Config has two goalie numbers in two units: `risk.yaml` `goalie_fee_share_max: 0.40` (fee share) and `exposure.yaml`
  `tournament.classic.goalie: 0.35` (entry share, applied only at 20+ entries, `min_entries: 20`). Neither bound at 5 entries.
- Experiment, scratch copy only: `tournament.min_entries` 20 to 5 (the "no person or goalie cap" note disappeared, so
  the caps were on). Result: lineups identical to v3 (Askarov 4 of 5, McDavid 5 of 5) and the same budget breach.
  Cause untraced. Two candidates: the candidate set (90 candidates, 40 kept after the screen) holds no goalie-diverse
  portfolio, or tournament caps are soft in selection. Listing goalies across the candidates would settle it.

Decision from Ben (2026-10-01): when the goalie cap cannot be met in dollars because of unequal entry fees, fall
back to a percent-of-lineups cap.

Spec for the fix.
1. Compute the fee floor as today. If floor <= budget, keep the dollar cap, hard.
2. If floor > budget, do not raise the dollar cap. Cap lineups per goalie instead:
   `max_lineups = max(1, round(share * n_entries))`, using the goalie budget share. At 5 entries, 40% is 2 lineups;
   35% is 1.75 (floor 1, ceil 2). Pick the rounding, document it in the config comment, and use one key for the share.
3. Make the cap hard in discovery and selection when feasible. It is infeasible only if usable goalies are fewer than
   `ceil(n_entries / max_lineups)`; relax by the smallest step and report it.
4. Print the mode and result in stdout and RUN_NOTES, for example
   `GOALIE_CAP=LINEUPS 2/5 (fee floor 0.59 > budget 0.40)`. A breach prints `RISK_BUDGET=BREACHED` (B40).
5. `game_fee_share_max` has the same flaw (EDM@VAN 0.85 vs floor 0.59). Not requested; recommend the same fallback.

Acceptance. Fixture with today's fees (0.25, 0.25, 1.00, 0.10, 0.10) and 4+ usable goalies: no goalie in more than 2
lineups, notes show mode LINEUPS. Equal-fee fixture stays in dollar mode. Infeasible fixture reports the relaxation.

### F3. Unresolved goalies are never researched (BACKLOG B37)

SEA (Grubauer, Daccord) and CHI (Knight, Soderblom) carry no Starting=P, so each has p_sim 0.5 (read from the scenario
meta). Their simulated means (3.4 to 5.8) are about half a confirmed starter's, which keeps them out of every candidate.
The researcher request listed only goalies already in the portfolio ("goalie of a team in the portfolio: confirm
tonight's starter"). My inference: research can never promote a goalie who is not already chosen, so the baseline's
goalies lock in. That blocks goalie diversification even after F1 and F2 are fixed.

### F4. QA proposals rejected by ordering (BACKLOG B38)

Round 1 results, in order: #0 Pospisil to Michaels REJECTED (skaters span 2 teams); #1 Yamamoto to Karlsson ACCEPTED;
#2 Askarov to Wolf REJECTED (salary 50,100 over the cap); #3 Weegar to Sergachev REJECTED (tail, safety or referee check).
#1 and #2 target the same entry. The adversary said so in its evidence ("conflicts on cap with the Karlsson swap, so
only one of the two can apply"). Entry 5278122874 was 49,300 before QA. #2 alone gives 49,700, legal. After #1 it is
49,700, and #2 on top gives 50,100. The controller shows this as REJECTED, indistinguishable from a model rejection.
Impact: the only proposal aimed at goalie concentration was never judged on its merits. It might still fail the
objective test, because the model prices Askarov at 14.2 (F1).

Also: `ANOTHER_ROUND=NO` after one accepted strategic change (the controller allows round 2 only after an accepted
correctness repair), while CLAUDE.md says to stop at zero accepted changes. Reconcile the two. The adversary's #0 was
illegal; the packet has no per-entry skater-team count.

### F5. RUN_NOTES not refreshed after QA (BACKLOG B39)

After v4, the tables still show v3 figures for entry 5278122874: objective 8.2163 +/- 0.3147, own% 55.4, dup -29.98.
Those describe the Yamamoto lineup. `versions/v4/referee.json` has no per-entry metrics, and the Messages section says
only "QA round 1 published v4". The effect of the accepted swap on E[payout], own% and P(cash) is not recorded.

### F6. Two statuses missing from stdout and from my first report (BACKLOG B40)

Both facts were in RUN_NOTES.md and absent from stdout: odds coverage (1 of 4 games on market) and the risk-budget
outcome ("Inside budget: no"). CLAUDE.md's status list (`FILE_VALID` through `FIELD_CALIBRATION`) has neither. My first
report described the concentration but not that the engine had flagged and shipped it. Proposed: `MARKET_COVERAGE=1/4`
and `RISK_BUDGET=OK|BREACHED` in stdout, in CLAUDE.md's list and in `/nhl-run` step 4.

### F7. Session conduct (mine)

- Stopped silently after the declined install instead of saying what was blocked and why.
- First report omitted the two facts in F6.

## 4. Open decisions for Ben

1. Goalie share and rounding: recommend 40%, which is 2 of 5 at 5 entries, one config key. 35% floors to 1 of 5.
2. Apply the same percent-of-lineups fallback to `game_fee_share_max`: recommend yes.
3. Should the researcher also cover unresolved goalies outside the portfolio (F3)?

## 5. State at end of session

- v4 DKEntries.csv sent to Ben; not uploaded to DraftKings (Ben's action). Salaries 49,300 to 49,900.
- Locks: SEA@CGY 9:00 PM ET, CHI@UTA 9:30, EDM@VAN and FLA@SJS 10:00. All five v4 lineups hold CGY players, so after
  9:00 PM v4 cannot be entered as is.
- Nothing committed. This report and the BACKLOG rows exist only in the container until committed and pushed.
