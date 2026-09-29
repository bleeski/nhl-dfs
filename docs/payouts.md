# Payouts and ties (C8)

How the engine turns a finish into money, what DraftKings' own text says, and what is still an
assumption. Code: `src/nhl_dfs/build/objectives.py`.

## What the DraftKings Terms of Use say (checked 2026-09-29)

Source: DraftKings Daily Fantasy Terms of Use, https://www.draftkings.com/help/terms, page marked
"Last Updated 06/30/2026", read 2026-09-29 (the page answered HTTP 403 to a plain fetch and was read
with a browser user agent).

- The PRIZES section says that in the event of a tie, "prizes are divided evenly amongst the
  participants that have tied."
- The Terms do not say, in so many words, that the prizes of all the tied places are pooled first,
  do not state a rounding rule (for example down to the cent), and say nothing about leftover cents.
- Nothing found on ties for non-cash prizes (satellite tickets, seats): not in the Terms and not in
  the support articles "What is a contest ticket" (KB0010410) and "Fantasy sports ticket types
  overview" (KB0010347), both updated 2026-06-23. A 2014 DraftKings social post, seen only in search
  snippets, said tied players split the ticket's cash value; that is not current official text.
- No separate tie rule for head-to-head, 50/50, double-up or winner-take-all; the general sentence
  applies. Individual promotional contests may set their own tie-breakers.
- The rules pages https://www.draftkings.com/help/rules and /help/rules/nhl had no tie text in
  their HTML (some content may load by script).

## What the engine does

| Rule | Status |
|---|---|
| Tied entries share equally | VERIFIED (Terms of Use sentence above) |
| The prizes of the tied places (positions G+1 to G+T) are pooled, then divided by T | ASSUMPTION: the usual reading of "divided evenly" when a tie spans several paid places; plan section 7 |
| Each share is rounded down to the cent; leftover cents are not paid to anyone | ASSUMPTION (plan section 7); integer cents with floor division, equal to Decimal ROUND_DOWN, tested |
| The user's own entries count as copies in the tie and as opponents in the ranks | engine rule (plan section 7) |
| Tied satellite seats: each tied entry is credited seats / T of a seat (a fractional seat, the same as splitting the ticket's value) | ASSUMPTION, UNVERIFIED against DK text |
| Tier cash in the DK contest detail is per position | VERIFIED on the recorded fixtures dk_contest_195958173 (tiers sum to totalPayouts $1,000) and dk_contest_196048725 ($150) |

[BEN] flag 9 asks Ben to confirm the pooling, the rounding and the ticket rule against a real
settled contest when one is available (a tie in a standings export would settle it).

## Payout curves

- `PAYOUT_SOURCE=EXACT`: the contest detail's payout table (cash per position; a tier with non-cash
  text is a ticket; a ticket's face value is read from a dollar amount in that text, else missing).
- `PAYOUT_SOURCE=PRIOR`: the declared family prior in `config/contest_families.yaml`
  (`payout_priors`): rake, paid fraction, a flat cash curve, a WTA single prize, satellite seats
  (face value = pool / seats) or a top-heavy power-law GPP curve whose last paid place gets
  2 x fee. Every figure built on a PRIOR curve is an uncalibrated scenario proxy.
