"""NEWS_STATE from a role state (C7). Only the second-signal sources count (Daily Faceoff pages inside the
age policy, matched goalie reports); DK's own status column is intake data and does not by itself make
news, so a run with no Daily Faceoff information stays NONE (as C2b established)."""

from __future__ import annotations

from nhl_dfs.contracts.statuses import NewsState


def summary(roles, pool) -> dict:
    """Per-team coverage behind the state, for the run notes and `roles`."""
    teams = sorted(pool.teams)
    out = {}
    for t in teams:
        page = roles.team_pages.get(t)
        g = roles.goalies.get(t)
        out[t] = {"lines": bool(page and page["usable"]), "goalie_report": bool(g and g.report is not None),
                  "goalie_state": g.state.value if g else None, "low_confidence": bool(page and page["low_confidence"])}
    return out


def state(roles, pool) -> NewsState:
    """FULL: every slate team has usable lines and a matched goalie report. NONE: neither anywhere.
    PARTIAL: anything in between. FULL never means the news is right, only that it was seen and is current."""
    if roles is None:
        return NewsState.NONE
    cov = summary(roles, pool)
    if not cov:
        return NewsState.NONE
    lines = sum(c["lines"] for c in cov.values())
    goalies = sum(c["goalie_report"] for c in cov.values())
    if lines == 0 and goalies == 0:
        return NewsState.NONE
    if lines == len(cov) and goalies == len(cov):
        return NewsState.FULL
    return NewsState.PARTIAL
