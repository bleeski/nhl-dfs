"""Build a SlateSpec from a salary pool, a ParamTable and (optionally) an odds snapshot (C6).

Every slate game gets a `GameRates`: MARKET when a paired moneyline and total matched a verified
team-code pair, MODEL otherwise, with the reason recorded per game. Nothing here invents a price.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nhl_dfs.sim import market
from nhl_dfs.sim.game import GameSpec, SlateSpec


def fetch_odds(cache=None, *, now: datetime | None = None, max_age_h: float | None = None) -> tuple[object | None, list[str]]:
    """Partner odds first, Covers second; (snapshot or None, messages). A snapshot older than
    max_age_h (default market.max_age_h) is passed over for the next source. Never raises."""
    from nhl_dfs.data.sources import covers, nhl

    now = now or datetime.now(timezone.utc)
    limit = float(max_age_h if max_age_h is not None else market.load_sim_config()["market"]["max_age_h"])
    msgs: list[str] = []
    for name, fn in (("nhl_partner_odds", nhl.partner_odds), ("covers", covers.odds)):
        try:
            snap = fn(cache=cache)
            age_h = (now - snap.as_of_utc).total_seconds() / 3600.0
            if snap.games and age_h > limit:
                msgs.append(f"odds: {name} snapshot is {age_h / 24:.1f} days old ({snap.as_of_utc:%Y-%m-%d %H:%MZ}, {snap.as_of_basis}); passed over")
                continue
            if snap.games:
                msgs.append(f"odds: {name} {len(snap.games)} games, as of {snap.as_of_utc:%Y-%m-%d %H:%MZ} ({snap.as_of_basis})")
                return snap, msgs
            msgs.append(f"odds: {name} returned no games")
        except Exception as exc:  # a failed source is reported, then the next is tried
            msgs.append(f"odds: {name} unavailable ({type(exc).__name__}: {str(exc)[:100]})")
    return None, msgs


def build_slate(pool, params, snapshot=None, *, cfg: dict | None = None, model_cfg: dict | None = None,
                confirmed_at: dict[str, datetime] | None = None, now: datetime | None = None) -> tuple[SlateSpec, list[str]]:
    """pool.games is keyed "AWAY@HOME" in DK codes. confirmed_at: game key -> latest goalie
    confirmation time (None until C7 supplies it). A snapshot older than market.max_age_h at `now`
    is not used: the NHL partner feed keeps an old lastUpdatedUTC when it is not refreshed (seen
    2026-08-28 on a 2026-09-29 slate), and a month-old line is not a price."""
    from nhl_dfs.models.rates import load_model_config

    cfg = cfg or market.load_sim_config()
    model_cfg = model_cfg or load_model_config()
    pairs = {k: (g.home, g.away) for k, g in pool.games.items()}
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []
    if snapshot is not None:
        age_h = (now - snapshot.as_of_utc).total_seconds() / 3600.0
        limit = float(cfg["market"]["max_age_h"])
        if age_h > limit:
            notes.append(f"odds snapshot is {age_h / 24:.1f} days old (limit {limit:g} h): not used, every game takes the model")
            snapshot = None
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
                     + (f" [{reason}]" if reason and gr.source == "MODEL" and reason != "no odds snapshot" else "") + (f" [{detail}]" if detail else ""))
    return SlateSpec(specs, pool.mode.value, cfg, model_cfg), notes + lines
