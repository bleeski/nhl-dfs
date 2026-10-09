"""Skaters against the lineup's own goalie (C20, backlog B45, flags 13 and 51 to 53).

A lineup that rosters a goalie and a skater of that goalie's opponent bets that one team scores and that the
other does not. In the 14 pooled Classic contests behind B45 the top-1% lineups did it 5.4% of the time against
11.7% for the field (in-sample, one week). The rule: in the contest families named in config/risk.yaml
own_goalie.families (large_gpp, small_field, wta) a lineup rosters no skater who plays against its goalie. Cash and
satellite are exempt. Showdown is out (flag 53). The rule is OFF in every builder unless a caller asks for it, so
the field sampler (models/field.py, models/field_fast.py), which shares milp.LineupModel, never sees it.

This module is pure: who plays whom (`pool.games`), which skaters face which goalie, which families the rule
covers, and the `OWN_GOALIE=` text the run reports. The solver row is in build/milp.py, the selection filter in
build/portfolio.py and build/provisional.py, the re-solve flag in build/late_swap.py.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping, Sequence

from nhl_dfs.contracts.geometry import Mode

DEFAULT_FAMILIES = ("large_gpp", "small_field", "wta")


def settings(risk_cfg: Mapping | None) -> tuple[bool, frozenset[str]]:
    """(enabled, families) from config/risk.yaml own_goalie. A config without the block means off."""
    block = (risk_cfg or {}).get("own_goalie") or {}
    return bool(block.get("enabled", False)), frozenset(block.get("families") or DEFAULT_FAMILIES)


def rule_families(risk_cfg: Mapping | None, mode: Mode) -> frozenset[str]:
    """The contest families the rule covers: empty when it is switched off or the pool is Showdown (flag 53)."""
    on, fams = settings(risk_cfg)
    return fams if on and mode is Mode.CLASSIC else frozenset()


def opponent_map(pool) -> dict[str, str]:
    """team -> the team it plays: the pool's games, then the Game Info of any row whose team they do not cover (a game
    that has started leaves `pool.games`, but its pinned rows still carry it). Empty when the pool has no game
    information at all."""
    out: dict[str, str] = {}
    for g in getattr(pool, "games", {}).values():
        out[g.home] = g.away
        out[g.away] = g.home
    from nhl_dfs.intake.salary import parse_game_info

    for r in (*pool.rows, *getattr(pool, "started_rows", ())):
        if r.team in out or not r.game_info:
            continue
        try:
            info = parse_game_info(r.game_info)[1]
        except ValueError:
            continue
        out.setdefault(info.home, info.away)
        out.setdefault(info.away, info.home)
    return out


def missing_opponents(pool, opp: Mapping[str, str] | None = None) -> set[str]:
    """Teams with a goalie row and no known opponent: the rule cannot be evaluated for them."""
    opp = opponent_map(pool) if opp is None else opp
    return {r.team for r in pool.rows if r.is_goalie and r.team not in opp}


def conflict_rows(role_ids: Iterable[str | None], pool, opp: Mapping[str, str] | None = None) -> list[str]:
    """Role IDs of the skaters in the lineup who play against the lineup's goalie (empty: none)."""
    opp = opponent_map(pool) if opp is None else opp
    rows = [pool.by_role_id[r] for r in role_ids if r and r in pool.by_role_id]
    facing = {opp[r.team] for r in rows if r.is_goalie and r.team in opp}
    return [r.role_id for r in rows if not r.is_goalie and r.team in facing]


def faces_own_goalie(role_ids: Iterable[str | None], pool, opp: Mapping[str, str] | None = None) -> bool:
    return bool(conflict_rows(role_ids, pool, opp))


def audit(by_entry: Mapping[str, Sequence[str | None]], family_of: Mapping[str, str | None], pool,
          risk_cfg: Mapping | None, *, pins: Mapping[str, Mapping[int, str]] | None = None) -> str:
    """The value of the OWN_GOALIE= report line for the lineups as they stand (flag 52). It reads the final lineups,
    not how they were built, so a relaxation, a feasibility fallback or a pinned cell cannot hide.

    OK                    no entry of a rule family faces its own goalie
    FORCED_BY_PINS(k)     k entries do, and every such conflict is between two pinned cells (never changed)
    RELAXED(k)            k entries do for another reason (no legal compliant lineup, or an old lineup kept)
    OFF / NOT_APPLICABLE / NOT_EVALUATED(reason)   see the flags; missing information is reported, never passed
    Exempt families (cash, satellite) are counted and never judged. family_of maps entry id -> family or None."""
    if pool.mode is not Mode.CLASSIC:
        return "NOT_APPLICABLE(Showdown)"
    on, fams = settings(risk_cfg)
    if not on:
        return "OFF(config own_goalie.enabled is false)"
    opp = opponent_map(pool)
    if not opp:
        return "NOT_EVALUATED(the pool has no game information)"
    rule = [e for e in by_entry if family_of.get(e) in fams]
    exempt = [e for e in by_entry if family_of.get(e) is not None and family_of.get(e) not in fams]
    unknown = [e for e in by_entry if family_of.get(e) is None]
    if not rule:
        if unknown:
            return f"NOT_EVALUATED(contest family unknown for {len(unknown)} entries; the rule was not applied to them)"
        return f"NOT_APPLICABLE(no {'/'.join(sorted(fams))} entries; {len(exempt)} exempt)"
    pins = pins or {}
    bad: dict[str, list[str]] = {}
    for e in rule:
        rows = conflict_rows(by_entry[e], pool, opp)
        if rows:
            bad[e] = rows
    forced = set()
    for e, rows in bad.items():
        pinned = set((pins.get(e) or {}).values())
        goalies = [r for r in by_entry[e] if r and r in pool.by_role_id and pool.by_role_id[r].is_goalie]
        if goalies and all(g in pinned for g in goalies) and all(r in pinned for r in rows):
            forced.add(e)
    shown = lambda ids: ", ".join(sorted(ids)[:5]) + (" ..." if len(ids) > 5 else "")  # noqa: E731
    if not bad:
        status = "OK"
    elif len(forced) == len(bad):
        status = f"FORCED_BY_PINS({len(bad)}: {shown(bad)})"
    else:
        loose = [e for e in bad if e not in forced]
        status = f"RELAXED({len(bad)}: {shown(loose)}" + (f"; {len(forced)} forced by pins)" if forced else ")")
    by_family = Counter(family_of[e] for e in rule)
    parts = [f"rule on for {len(rule)} of {len(by_entry)} entries ({', '.join(f'{f} {n}' for f, n in sorted(by_family.items()))})",
             f"{len(bad)} face their own goalie"]
    if exempt:
        parts.append(f"exempt {len(exempt)}, {sum(1 for e in exempt if faces_own_goalie(by_entry[e], pool, opp))} face theirs (allowed)")
    if unknown:
        parts.append(f"family unknown for {len(unknown)} (rule not applied)")
    miss = missing_opponents(pool, opp)
    if miss:
        parts.append(f"no game information for {', '.join(sorted(miss))}")
    return f"{status} ({'; '.join(parts)})"


def baseline_line(by_entry: Mapping[str, Sequence[str | None]], pool, risk_cfg: Mapping | None) -> str:
    """The OWN_GOALIE value for the Phase A baseline (v1): contest families are not known yet, so the rule is not applied
    and nothing is judged; the count of lineups that face their own goalie is information for the reader."""
    if pool.mode is not Mode.CLASSIC:
        return "NOT_APPLICABLE(Showdown)"
    on, _ = settings(risk_cfg)
    if not on:
        return "OFF(config own_goalie.enabled is false)"
    opp = opponent_map(pool)
    if not opp:
        return "NOT_EVALUATED(the pool has no game information)"
    n = sum(1 for lu in by_entry.values() if faces_own_goalie(lu, pool, opp))
    return (f"NOT_EVALUATED(Phase A baseline: contest families are not known until the provisional pass, so the rule was not "
            f"applied; {n} of {len(by_entry)} lineups face their own goalie)")


def families_from_manifest(m: Mapping) -> dict[str, str]:
    """entry id -> contest family from a run manifest: the scenario pass's entry rows, else the provisional pass's."""
    out: dict[str, str] = {}
    for block in ("provisional", "scenario"):  # the later pass wins
        for row in ((m.get(block) or {}).get("entries") or []):
            if row.get("entry_id") is not None and row.get("family"):
                out[str(row["entry_id"])] = str(row["family"])
    return out


def families_by_name(entries, fam_cfg: Mapping) -> dict[str, str | None]:
    """entry id -> family, from the contest NAME only and only when a name pattern matched. The config default
    (large_gpp) is not a match: an unnamed cash contest must not be treated as a tournament (flag 52)."""
    from nhl_dfs.models.contests import family_from_name

    out: dict[str, str | None] = {}
    for e in getattr(entries, "entries", entries):
        fam, source = family_from_name(e.contest_name, fam_cfg)
        out[e.entry_id] = fam if source == "name_pattern" else None
    return out


def entry_families(entries, *, cache_family: Mapping[str, str] | None = None, manifest: Mapping | None = None,
                   fam_cfg: Mapping | None = None) -> dict[str, str | None]:
    """entry id -> contest family for a re-solve (flag 52): the scenario cache by contest id, then the run manifest's
    rows (scenario pass, else provisional pass), then a contest-name pattern that really matched; None when none of
    them knows (the rule is then not applied and the report line says so)."""
    from_manifest = families_from_manifest(manifest or {})
    by_name = families_by_name(entries, fam_cfg) if fam_cfg is not None else {}
    out: dict[str, str | None] = {}
    for e in getattr(entries, "entries", entries):
        out[e.entry_id] = ((cache_family or {}).get(str(e.contest_id)) or from_manifest.get(e.entry_id)
                           or by_name.get(e.entry_id))
    return out
