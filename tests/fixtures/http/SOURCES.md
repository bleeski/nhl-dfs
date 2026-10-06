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
| `dk_draftables_153983.json` | https://api.draftkings.com/draftgroups/v1/draftgroups/153983/draftables?format=json (saved by Ben from his browser 2026-09-28; this machine's script gets HTTP 403) | 29688 | draftables cut from 158 rows to 24 (12 people, CPT and FLEX each: Bedard OUT, Ellis IR, Lavoie DTD, Uchacz OUT, Pietrangelo IR, Eichel, Hart, Knight, Kane, Theodore, Frondell, Greene); other top-level keys whole |
| `dailyfaceoff_team_vancouver-canucks.html` | https://www.dailyfaceoff.com/teams/vancouver-canucks/line-combinations (raw capture 2026-09-29 13:30Z; built by `make_df_fixtures.py`) | 15286 | `__NEXT_DATA__` kept (`props.pageProps.combinations`: updatedAt, source, 5 lines/pairs/PP/PK groups, IR group); dropped capSummary, sortedTeams, toolkitPosts, stubData, and per player cap, season, last5, last10, rating, images; news text cut to 100 chars; 3 other script tags kept. VAN carries every injury tag seen (out, ir, dtd) and a gameTimeDecision forward |
| `dailyfaceoff_goalies.html` | https://www.dailyfaceoff.com/starting-goalies (raw capture 2026-09-29 13:30Z) | 4556 | `__NEXT_DATA__` kept with the 5 games of the day (EDM's Jarry `Confirmed`, source and time; the other four unconfirmed); dropped headshots, stats, cap data, fantasy prose, toolkit posts; 4 other script tags kept. The price fields are puck-line prices, not a moneyline and total pair |
| `dk_lobby_gamesets_2026-09-29.json` | https://www.draftkings.com/lobby/getcontests?sport=NHL (cached at data/raw/dk_lobby/2026-09-29, written 2026-09-29 23:00Z) | 10060 | only the `GameSets` key kept (whole); Contests, DraftGroups and the rest removed (684053 bytes). B35: the 2026-09-29 codes (MTL among them) |
| `dk_lobby_gamesets_2026-09-30.json` | same URL (cached 2026-09-30 23:30Z) | 6660 | only `GameSets` kept (417451 bytes). B35: COL, LAK, NYI, PHI, PIT, TOR |
| `dk_lobby_gamesets_2026-10-01.json` | same URL (cached 2026-10-01 16:30Z) | 28595 | only `GameSets` kept (856943 bytes). B35: the 25 codes of 2026-10-01, SJS, UTA, CGY, SEA among them |
| `nhl_partner_odds_2026-10-01_slate.json` | NHL partner odds (DraftKings), the 2026-10-01 run's cached payload, lastUpdatedUTC 2026-10-02T00:00:38Z | 6402 | `games` cut (9106 bytes) from 8 to the slate's 4 (SEA@CGY, CHI@UTA, EDM@VAN, FLA@SJS); `bettingPartner` cut to partnerId, country, name; re-serialized with indent |
| `dk_lobby_gamesets_2026-10-03.json` | same URL (cached data/raw/dk_lobby/2026-10-03/ec22f8aa, fetched 2026-10-03T19:30:01Z) | 21165 | only `GameSets` kept (1094422 bytes). B85 (C17): OTT @ TOR ("Ottawa Senators @ Toronto Maple Leafs"), the first DK file to show Ottawa |
| `nhl_partner_odds_2026-10-03_ott.json` | NHL partner odds (DraftKings), the 2026-10-03 capture of lastUpdatedUTC 2026-10-03T23:00:40Z (data/raw/nhl_partner_odds/2026-10-03/...006ea33ed.json, 13786 bytes) | 2210 | `games` cut from 13 to the one OTT @ TOR game (B85 acceptance: it prices on MARKET once OTT is verified) |
