# Template fixtures (C16)

Real DraftKings bodies, untrimmed except where stated; nothing is edited or invented.

| File | Source | Bytes | Trimmed |
|---|---|---:|---|
| `dk_contest_195958176.json` | https://api.draftkings.com/contests/v1/contests/195958176?format=json (cached at data/raw/dk_contest/2026-09-28; the $5K mini-MAX, 11,890 max entries, 2,732 paid) | 5158 | none |
| `dk_lobby_rows.json` | https://www.draftkings.com/lobby/getcontests?sport=NHL (newest cached capture that lists each id) | 4431 | Contests cut to 6 rows: 196228907 (the 09-30 mini-MAX), 195958176 (the 09-28 mini-MAX), 196267162 (the 10-02 $1K Daily Dollar), 195958173 (the 09-29 $1K Daily Dollar), 196218438 (the 09-30 $750 Daily Dollar, 891 max: no cached table), 196218433 (the 09-30 $6K Hip Check, 7,134 max: no cached table) |

The 09-29 $1K Daily Dollar table (195958173) is `tests/fixtures/http/dk_contest_195958173.json`.
