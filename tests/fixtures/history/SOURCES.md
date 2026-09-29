# History fixtures (C4)

Real samples cut to 50 rows or fewer per kind on 2026-09-28. All player samples are the same
game, 2025020017 (DAL at WPG, 2025-10-09, regular season): 8 skaters (4 per team, chosen by TOI,
including a defenseman and a PP skater) and both goalies. Bytes were written with `write_bytes`.

MoneyPuck (credit: data from MoneyPuck.com, https://moneypuck.com/data.htm; free for
non-commercial use; only files listed on that page):

| File | Source (listed download) | Rows |
|---|---|---|
| mp_skaters_game_2025.zip | seasonPlayersSummary/skaters/2025.zip | 40 (8 players x 5 situations) |
| mp_goalies_game_2025.zip | seasonPlayersSummary/goalies/2025.zip | 10 |
| mp_lines_game_2025.zip | seasonPlayersSummary/lines/2025.zip | 34 (5on5 and all units holding a sampled player) |
| mp_all_teams_game.csv | careers/gameByGame/all_teams.csv | 10 |
| mp_shots_2025.zip | shots_2025.zip | 40 (first 40 shots of the game) |
| mp_skaters_season_2025.csv | seasonSummary/2025/regular/skaters.csv | 40 |
| mp_goalies_season_2025.csv | seasonSummary/2025/regular/goalies.csv | 10 |

NHL (keyless public APIs):

| File | Source | Rows |
|---|---|---|
| nhl_skater_{timeonice,realtime,summary}_20251009.json | api.nhle.com/stats/rest/en/skater/<report>?isGame=true, gameDate 2025-10-09, filtered to the 8 skaters; `total` rewritten to the kept row count | 8 each |
| nhl_goalie_summary_20251009.json | api.nhle.com/stats/rest/en/goalie/summary?isGame=true, same game | 2 |
| nhl_boxscore_2025020017.json, nhl_boxscore_2025020018.json | api-web.nhle.com/v1/gamecenter/<id>/boxscore (regular season, 2025-10-09) | full box scores |

Measured on the full 2025-10-09 data (504 skaters): goals, assists, SOG, blocks and total TOI
agree exactly between MoneyPuck and the NHL reports; NHL PP and SH time equal MoneyPuck 5on4 and
4on5 time for 88% of skaters and NHL EV time equals MoneyPuck all - 5on4 - 4on5 for 80%, so
strength-split TOI always uses the NHL definition.
