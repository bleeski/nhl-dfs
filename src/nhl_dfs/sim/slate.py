"""Build a SlateSpec from a salary pool, a ParamTable and (optionally) an odds snapshot (C6).

Every slate game gets a `GameRates`: MARKET when a paired moneyline and total matched a verified
team-code pair, MODEL otherwise, with the reason recorded per game. Nothing here invents a price.
"""

from __future__ import annotations

from datetime import datetime

from nhl_dfs.sim import market
from nhl_dfs.sim.game import GameSpec, SlateSpec


def fetch_odds(cache=None) -> tuple[object | None, list[str]]:
    """Partner odds first, Covers second; (snapshot or None, messages). Never raises."""
    from nhl_dfs.data.sources import covers, nhl

    msgs: list[str] = []
    for name, fn in (("nhl_partner_odds", nhl.partner_odds), ("covers", covers.odds)):
        try:
            snap = fn(cache=cache)
            if snap.games:
                msgs.append(f"odds: {name} {len(snap.games)} games, as of {snap.as_of_utc:%Y-%m-%d %H:%MZ} ({snap.as_of_basis})")
                return snap, msgs
            msgs.append(f"odds: {name} returned no games")
        except Exception as exc:  # a failed source is reported, then the next is tried
            msgs.append(f"odds: {name} unavailable ({type(exc).__name__}: {str(exc)[:100]})")
    return None, msgs


def build_slate(pool, params, snapshot=None, *, cfg: dict | None = None, model_cfg: dict | None = None,
                confirmed_at: dict[str, datetime] | None = None) -> tuple[SlateSpec, list[str]]:
    """pool.games is keyed "AWAY@HOME" in DK codes. confirmed_at: game key -> latest goalie
    confirmation time (None until C7 supplies it)."""
    from nhl_dfs.models.rates import load_model_config

    cfg = cfg or market.load_sim_config()
    model_cfg = model_cfg or load_model_config()
    pairs = {k: (g.home, g.away) for k, g in pool.games.items()}
    matched, why = market.match_odds(snapshot, pairs)
    specs, lines = [], []
    confirmed_at = confirmed_at or {}
    for key in sorted(pairs):
        home, away = pairs[key]
        strength = market.model_strength(params, home, away, cfg, model_cfg)
        gr = market.fit_game(matched.get(key), strength, snapshot.as_of_utc if snapshot is not None else None,
                             confirmed_at.get(key), cfg=cfg)
        specs.append(GameSpec(key, home, away, gr))
        detail = "; ".join(n for n in gr.notes if not n.startswith("market:"))
        reason = why.get(key)
        lines.append(f"{key}: {gr.source}{' STALE' if gr.stale else ''} lambda {gr.lambda_home:.2f}/{gr.lambda_away:.2f}, "
                     f"P(home win) {gr.p_home_win:.3f}, total {gr.e_total:.2f}, P(regulation tie) {gr.p_ot:.3f}"
                     + (f" [{reason}]" if reason and gr.source == "MODEL" else "") + (f" [{detail}]" if detail else ""))
    return SlateSpec(specs, pool.mode.value, cfg, model_cfg), lines
