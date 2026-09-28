# Recorded HTTP fixtures

Each file is one real response recorded on 2026-09-28 from this machine, then trimmed as stated.
Trimming only removes whole records or markup; nothing is edited or invented.

| File | URL | Bytes | Trimmed |
|---|---|---:|---|
| `dk_lobby.json` | https://www.draftkings.com/lobby/getcontests?sport=NHL | 15107 | Contests cut from 787 to 3 (Ben's 196048725, 195958173, plus 196049683); DraftGroups to those 3 groups |
| `dk_contest_196048725.json` | https://api.draftkings.com/contests/v1/contests/196048725?format=json | 3469 | none |
| `dk_contest_195958173.json` | https://api.draftkings.com/contests/v1/contests/195958173?format=json | 4975 | none |
| `dk_contest_196049683.json` | https://api.draftkings.com/contests/v1/contests/196049683?format=json | 1614 | none |
| `dk_draftables_403.html` | https://api.draftkings.com/draftgroups/v1/draftgroups/153983/draftables?format=json | 435 | none (HTTP 403 Akamai Access Denied body, 2026-09-28) |
| `nhl_schedule_2026-09-29.json` | https://api-web.nhle.com/v1/schedule/2026-09-29 | 12346 | gameWeek cut from 7 days to the first (2026-09-29, 5 games) |
| `nhl_schedule_2026-09-26.json` | https://api-web.nhle.com/v1/schedule/2026-09-26 | 5857 | gameWeek cut to 2 days, 2 games each |
| `nhl_roster_VGK.json` | https://api-web.nhle.com/v1/roster/VGK/current | 11497 | none |
| `nhl_boxscore_2026010053.json` | https://api-web.nhle.com/v1/gamecenter/2026010053/boxscore | 12533 | none |
| `nhl_gamelog_8478403_20252026_2.json` | https://api-web.nhle.com/v1/player/8478403/game-log/20252026/2 | 29990 | none |
| `nhl_report_timeonice.json` | https://api.nhle.com/stats/rest/en/skater/timeonice?isAggregate=false&isGame=true&start=0&limit=3&cayenneExp=gameDate>="2026-04-01" and gameDate<="2026-04-01" | 1622 | none (requested with limit=3; total field reports 108) |
| `nhl_report_realtime.json` | https://api.nhle.com/stats/rest/en/skater/realtime?isAggregate=false&isGame=true&start=0&limit=3&cayenneExp=gameDate>="2026-04-01" and gameDate<="2026-04-01" | 2130 | none (requested with limit=3; total field reports 108) |
| `nhl_report_summary.json` | https://api.nhle.com/stats/rest/en/skater/summary?isAggregate=false&isGame=true&start=0&limit=3&cayenneExp=gameDate>="2026-04-01" and gameDate<="2026-04-01" | 1563 | none (requested with limit=3; total field reports 108) |
| `nhl_partner_odds.json` | https://api-web.nhle.com/v1/partner-game/US/now | 5794 | none |
| `covers_odds.html` | https://www.covers.com/sport/hockey/nhl/odds | 30752 | 3 market tables (moneyline, spread, total) kept; 2 games each (FLA@CAR, MON@TOR); only the team cell and the DraftKings cell kept, plus FanDuel in the moneyline table; whitespace runs between tags collapsed to one space |
| `df_starting_goalies.html` | https://www.dailyfaceoff.com/starting-goalies | 23334 | markup outside <script> removed; all 26 script tags kept, __NEXT_DATA__ whole, other inline bodies over 800 bytes emptied |
| `df_lines_vegas-golden-knights.html` | https://www.dailyfaceoff.com/teams/vegas-golden-knights/line-combinations | 1899 | markup removed; all 23 script tags kept with bodies emptied (page was 230238 B) |
