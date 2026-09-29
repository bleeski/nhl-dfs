"""DK-to-NHL identity crosswalk (C4).

A DK person (normalized name, DK team, position group) is accepted automatically only on an
exact match: same normalized name, the NHL team that config/teams.yaml maps the DK code to with
dk_verified: true, and the same position group, with exactly one such NHL person. Everything
else is a proposal (reviewed and accepted with `identity --accept <proposal_id>`) or unmatched.
An unverified DK team code never auto-accepts. Nobody edits accepted.csv by hand.

accepted.csv rows are keyed by sha256("<normalized name>|<DK team>|<group>"). Proposal ids are
deterministic (key + NHL id), so a proposal id stays valid across reruns of `identity --seed`.
NHL positions L and R are LW and RW before position_group is applied.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from nhl_dfs.contracts.geometry import PoolRow
from nhl_dfs.contracts.ids import normalize_name, position_group
from nhl_dfs.intake.salary import SalaryPool

REPO_ROOT = Path(__file__).resolve().parents[4]
ACCEPTED_CSV = REPO_ROOT / "data" / "identity" / "accepted.csv"
PROPOSALS_JSON = REPO_ROOT / "data" / "identity" / "proposals.json"
TEAMS_YAML = REPO_ROOT / "config" / "teams.yaml"
ACCEPTED_HEADER = ["key_sha256", "dk_name", "dk_team", "position_group", "nhl_id", "method", "accepted_utc"]
FUZZY_MIN = 0.85
_NHL_POS = {"L": "LW", "R": "RW"}


@dataclass(frozen=True)
class NhlPerson:
    nhl_id: int
    name: str
    team: str  # NHL team code
    position: str  # NHL code: C, L, R, D, G (or LW, RW)
    source: str  # "roster" | "history"

    @property
    def group(self) -> str:
        p = self.position.upper()
        return position_group(_NHL_POS.get(p, p))


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    key_sha256: str
    dk_name: str
    dk_team: str
    position_group: str
    nhl_id: int
    nhl_name: str
    nhl_team: str
    nhl_position: str
    reason: str


@dataclass
class CrosswalkResult:
    accepted: dict[str, int] = field(default_factory=dict)  # person_key -> nhl_id
    proposals: list[Proposal] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)  # {"person_key", "name", "team", "group", "reason"}
    new_exact: int = 0

    def counts(self) -> dict[str, int]:
        return {"accepted": len(self.accepted), "proposals": len({p.key_sha256 for p in self.proposals}),
                "unmatched": len(self.unmatched)}


def key_sha(name: str, dk_team: str, group: str) -> str:
    return hashlib.sha256(f"{normalize_name(name)}|{dk_team}|{group}".encode("utf-8")).hexdigest()


def proposal_id(key: str, nhl_id: int) -> str:
    return hashlib.sha256(f"{key}|{int(nhl_id)}".encode("utf-8")).hexdigest()[:12]


def verified_team_map(path: Path = TEAMS_YAML) -> dict[str, str]:
    """DK code -> NHL code, only where dk_verified is true."""
    with open(path, encoding="utf-8") as f:
        teams = yaml.safe_load(f)["teams"]
    return {t["dk"]: t["nhl"] for t in teams if t.get("dk") and t.get("dk_verified")}


def nhl_codes(path: Path = TEAMS_YAML) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [t["nhl"] for t in yaml.safe_load(f)["teams"]]


def accepted(path: Path | None = None) -> dict[str, int]:
    """key_sha256 -> nhl_id from accepted.csv."""
    p = Path(path) if path is not None else ACCEPTED_CSV
    if not p.exists():
        return {}
    rows = list(csv.DictReader(io.StringIO(p.read_bytes().decode("utf-8-sig"))))
    return {r["key_sha256"]: int(r["nhl_id"]) for r in rows if r.get("key_sha256")}


def _append_accepted(path: Path, rows: list[list[str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    if not existing.strip():
        existing = (",".join(ACCEPTED_HEADER) + "\n").encode("utf-8")
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(existing + buf.getvalue().encode("utf-8"))
    os.replace(tmp, path)


def _dk_people(pool: SalaryPool) -> list[PoolRow]:
    """One row per DK person (Classic row, else FLEX, else CPT)."""
    out = []
    for pr in pool.persons.values():
        r = pr.classic or pr.flex or pr.cpt
        if r is not None:
            out.append(r)
    return sorted(out, key=lambda r: r.person_key)


def seed(pool: SalaryPool, *, directory: Iterable[NhlPerson], accepted_path: Path | None = None,
         proposals_path: Path | None = None, team_map: dict[str, str] | None = None, write: bool = True,
         clock=None) -> CrosswalkResult:
    accepted_path = Path(accepted_path) if accepted_path is not None else ACCEPTED_CSV
    proposals_path = Path(proposals_path) if proposals_path is not None else PROPOSALS_JSON
    team_map = team_map if team_map is not None else verified_team_map()
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    people = list({p.nhl_id: p for p in directory}.values())
    by_name: dict[str, list[NhlPerson]] = {}
    for p in people:
        by_name.setdefault(normalize_name(p.name), []).append(p)
    known = accepted(accepted_path)
    res = CrosswalkResult()
    new_rows: list[list[str]] = []
    for r in _dk_people(pool):
        group = position_group(r.position)
        key = key_sha(r.name, r.team, group)
        if key in known:
            res.accepted[r.person_key] = known[key]
            continue
        nhl_team = team_map.get(r.team)
        cands = by_name.get(normalize_name(r.name), [])
        exact = [p for p in cands if p.group == group and nhl_team is not None and p.team == nhl_team]
        if len(exact) == 1:
            res.accepted[r.person_key] = exact[0].nhl_id
            known[key] = exact[0].nhl_id
            new_rows.append([key, r.name, r.team, group, str(exact[0].nhl_id), "exact", now.isoformat()])
            continue
        props: list[tuple[NhlPerson, str]] = []
        if len(exact) > 1:
            props = [(p, "several NHL people share this name, team and position group") for p in exact]
        elif cands:
            for p in cands:
                why = []
                if nhl_team is None:
                    why.append(f"DK team {r.team} has no verified NHL mapping")
                elif p.team != nhl_team:
                    why.append(f"team differs (NHL {p.team})")
                if p.group != group:
                    why.append(f"position group differs (NHL {p.position})")
                props.append((p, "; ".join(why) or "name match"))
        else:
            target = nhl_team or r.team
            nn = normalize_name(r.name)
            for p in people:
                if p.team == target and p.group == group:
                    ratio = difflib.SequenceMatcher(None, nn, normalize_name(p.name)).ratio()
                    if ratio >= FUZZY_MIN:
                        props.append((p, f"similar name ({ratio:.2f}) on the same team and position group"))
        if props:
            for p, why in props:
                res.proposals.append(Proposal(proposal_id(key, p.nhl_id), key, r.name, r.team, group, p.nhl_id,
                                              p.name, p.team, p.position, why))
        else:
            res.unmatched.append({"person_key": r.person_key, "name": r.name, "team": r.team, "group": group,
                                  "reason": "no NHL identity found (call-up, new signing, or a name DK spells "
                                            "differently); left unmatched"})
    res.new_exact = len(new_rows)
    if write:
        _append_accepted(accepted_path, new_rows)
        proposals_path.parent.mkdir(parents=True, exist_ok=True)
        proposals_path.write_text(json.dumps({p.proposal_id: asdict(p) for p in res.proposals}, indent=1),
                                  encoding="utf-8")
    return res


def accept(proposal_id: str, nhl_id: int | None = None, *, accepted_path: Path | None = None,
           proposals_path: Path | None = None, clock=None) -> Proposal:
    """Record a reviewed proposal in accepted.csv. nhl_id, when given, must match the proposal."""
    accepted_path = Path(accepted_path) if accepted_path is not None else ACCEPTED_CSV
    proposals_path = Path(proposals_path) if proposals_path is not None else PROPOSALS_JSON
    if not proposals_path.exists():
        raise KeyError("no proposals on file; run `identity --seed` first")
    props = json.loads(proposals_path.read_text(encoding="utf-8"))
    if proposal_id not in props:
        raise KeyError(f"unknown proposal {proposal_id!r}")
    p = Proposal(**props[proposal_id])
    if nhl_id is not None and int(nhl_id) != p.nhl_id:
        raise ValueError(f"proposal {proposal_id} names NHL id {p.nhl_id}, not {nhl_id}")
    if p.key_sha256 in accepted(accepted_path):
        raise ValueError(f"{p.dk_name} ({p.dk_team}) is already accepted")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    _append_accepted(accepted_path, [[p.key_sha256, p.dk_name, p.dk_team, p.position_group, str(p.nhl_id),
                                      f"accepted:{proposal_id}", now.isoformat()]])
    return p


# -- directories -----------------------------------------------------------------------------

def directory_from_rosters(teams: Iterable[str], *, cache=None) -> tuple[list[NhlPerson], list[str]]:
    """Current NHL rosters for the given NHL team codes. Returns (people, problems)."""
    from nhl_dfs.data.sources import nhl

    out, problems = [], []
    for t in teams:
        try:
            for p in nhl.roster(t, cache=cache):
                out.append(NhlPerson(p.nhl_id, f"{p.first_name} {p.last_name}", t, p.position, "roster"))
        except Exception as exc:  # one team's outage is reported, the rest still seed
            problems.append(f"roster {t}: {type(exc).__name__}: {str(exc)[:80]}")
    return out, problems


def directory_from_history(seasons: Iterable[int], *, store_root=None) -> list[NhlPerson]:
    """Each player's latest team and position in the stored skater and goalie games."""
    from nhl_dfs.data.history import store

    out = []
    for kind in ("skater_games", "goalie_games"):
        df = store.read(kind, seasons, root=store_root)
        if df.empty:
            continue
        pos = df["position"] if "position" in df else "G"
        df = df.assign(position=pos).sort_values(["game_date", "game_id"]).groupby("nhl_id").tail(1)
        out += [NhlPerson(int(r.nhl_id), str(r.name), str(r.team), str(r.position), "history")
                for r in df.itertuples(index=False)]
    return out
