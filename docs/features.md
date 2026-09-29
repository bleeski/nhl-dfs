# History and feature columns (C4)

Every column the history store and the as-of frame carry, with its Tier A and Tier B source.
Tier A = NHL per-game reports plus MoneyPuck listed downloads (`sources.moneypuck.enabled: true`).
Tier B = NHL per-game reports only. The NHL reports are the backbone of both tiers, so any
column they provide has one definition everywhere. A column Tier B cannot observe is filled
from `config/history_priors.yaml` (league prior by position group; roles arrive in C7) and
carries `<col>_missing = 1`; in Tier A the same indicator is 0 when MoneyPuck had the value.

Credit: data from MoneyPuck.com (https://moneypuck.com/data.htm), free for non-commercial use.
Only files listed on that page are downloaded (`config/sources.yaml` `moneypuck.listed`).

Season key: NHL eight-digit id (20252026). MoneyPuck names it by start year (2025).
Regime: from the game id type digits (YYYY TT NNNN): 01 preseason, 02 regular, 03 playoffs.
Availability: a game's row exists the day after the game (backfill fetches completed days only).

## skater_games (one row per player-game)

| Column | Meaning | Tier A source | Tier B source |
|---|---|---|---|
| nhl_id, name, game_id, game_date, team, opponent, home, position | keys | NHL skater/timeonice | same |
| season, game_type, regime | from game_id | derived | derived |
| toi_s | total time on ice, seconds | NHL skater/timeonice `timeOnIce` | same |
| toi_ev_s, toi_pp_s, toi_sh_s | NHL even-strength, power-play, shorthanded TOI | NHL skater/timeonice | same |
| toi_strength_source | "nhl", or "moneypuck_situations" when only MoneyPuck had the game (EV = all - 5on4 - 4on5) | derived | always "nhl" |
| shifts | shifts | NHL skater/timeonice | same |
| goals, assists, sog, pp_points, sh_points | scoring | NHL skater/summary | same |
| blocks | blocked shots | NHL skater/realtime `blockedShots` | same |
| pp_points_missing, sh_points_missing | 1 when the summary report lacked the row | derived | derived |
| a1, a2 | primary and secondary assists | MoneyPuck skaters game `I_F_primaryAssists`, `I_F_secondaryAssists` | league prior (assists x a1_share) + `a1_missing`, `a2_missing` |
| attempts | individual shot attempts | MoneyPuck `I_F_shotAttempts` | league prior (sog x attempts_per_sog) + `attempts_missing` |
| ixg | individual expected goals (shot quality) | MoneyPuck `I_F_xGoals` | league prior (sog x ixg_per_sog) + `ixg_missing` |
| hd_xg | high-danger expected goals | MoneyPuck `I_F_highDangerxGoals` | league prior + `hd_xg_missing` |
| onice_xgf, onice_xga | on-ice expected goals for / against, all situations | MoneyPuck `OnIce_F_xGoals`, `OnIce_A_xGoals` | league prior per 60 x TOI + `_missing` |
| onice_xgf_5on5, onice_xga_5on5 | the same at 5on5 | MoneyPuck, 5on5 situation | league prior per 60 x EV TOI + `_missing` |
| shared_ice | share of 5on5 unit time spent in the player's most-used line or pairing | MoneyPuck lines game file (`lineId` = concatenated player ids) | league prior + `shared_ice_missing` |
| mp_toi_5on5_s, mp_toi_5on4_s, mp_toi_4on5_s, mp_toi_other_s | MoneyPuck situation times (not the NHL PP/SH definition) | MoneyPuck `icetime` by situation | missing: NaN, no prior, `_missing = 1` |
| tier | "A" when MoneyPuck had the row, else "B" | derived | derived |

## goalie_games (one row per goalie-game)

| Column | Meaning | Tier A source | Tier B source |
|---|---|---|---|
| nhl_id, name, game_id, game_date, team, opponent, home, season, game_type, regime | keys | NHL goalie/summary | same |
| started, toi_s | started the game; time on ice | NHL goalie/summary `gamesStarted`, `timeOnIce` | same |
| decision | W, L, O (overtime or shootout loss), or none | NHL goalie/summary wins/losses/otLosses | same |
| saves, shots_against, goals_against, shutout | results | NHL goalie/summary | same |
| xga | expected goals against | MoneyPuck goalies game `xGoals` | league prior (shots against x xga_per_shot_against) + `xga_missing` |
| gsax | goals saved above expected (xga - goals_against) | derived | 0 (league average) + `gsax_missing` |
| tier | as above | derived | derived |

## line_games (Tier A only)

line_id, player_ids (pipe-separated NHL ids), unit (line or pairing), team, game_id, game_date,
season, situation, toi_s, xgf, xga: MoneyPuck lines game file. Tier B has no equivalent.

## Other loaders (not in the backfill)

`moneypuck.load` also reads the listed team game file (all_teams.csv: team, game_id, situation,
toi_s, xgf, xga, sogf, soga, gf, ga), the per-shot files (game_id, shooter_id, xg, on_goal, goal,
situation from the skaters on ice) and the season summaries (skaters, goalies: the skater and
goalie columns above without game fields, plus games_played). They load on demand; C5 decides
whether to backfill them.

## As-of frame (data/features/asof.py)

`frame(as_of, nhl_ids, seasons=2)` reads the two most recent seasons whose regular season ended
before as_of, plus the season in progress, and keeps game rows dated strictly before as_of.
Season summaries are used only for completed seasons. Added per game row: games_before,
team_stint (increments on a team change), days_rest, and the regime recomputed from game_id.
`players` aggregates regular-season games only: games, last_game_date, last_team, team_stint,
mean TOI by strength, per-60 rates (goals, assists, a1, a2, sog, attempts, blocks, ixg, hd_xg,
pp_points), a1_share, `<col>_missing_share` for every enrichment column, and tier_a_share.
