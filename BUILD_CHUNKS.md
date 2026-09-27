# NHL DFS engine: build chunks

**Companion to `NHL_DFS_PLAN_AND_ARCHITECTURE.md` (revision 3) · final · 26 September 2026**

Seventeen chunks in objective order, each sized for one Claude Code session without compaction. Fifteen are unconditional; C12 and C13 are gated on data. `chunks.yaml` is the machine-readable dependency graph; `BUILD_STATUS.md` is the tracker; `tools/next_chunk.py` (built in C0a) enforces the order. This file is the brief a session reads instead of the architecture document.

---

## How a development session works

**Start.**

1. Open Claude Code in the repo root. `CLAUDE.md` loads automatically and is short.
2. Run `/nhl-dev-next` (after C10) or, until then, `python tools/next_chunk.py`. It reads `BUILD_STATUS.md` and `chunks.yaml`, runs the exit checks of every DONE predecessor, and prints the one eligible chunk. If a predecessor's checks fail, it refuses and names the failure; fix that first (as a repair inside the current session, logged in the tracker) before starting new work.
3. Read the chunk card, the files its "Read first" line names, and the modules the chunk imports (their public signatures, not their tests). Do not read the architecture document in full; cards cite section numbers for reference.
4. `python tools/next_chunk.py --start <id>` sets the tracker row to IN_PROGRESS with today's date. Stage and commit that change.

**Work.**

5. Build exactly the files the card lists, with the interfaces it names. Write tests alongside. Prefer small, typed functions with docstrings that state units (tenths, seconds, UTC).
6. Keep tool output small: `pytest -q`, `head`, `tail -n 20`. Never print a data file or a fixture larger than 50 lines into the conversation; fixtures are loaded by code.
7. If the session's context indicator passes roughly 60% before exit checks pass: stage and commit work in progress, write `HANDOFF: done=…; remaining=…; next=…` in the tracker row, leave it IN_PROGRESS, and end the session. The next session resumes the same chunk.

**Finish.**

8. Run the card's exit checks. All must pass. If one cannot pass in this session, the row becomes BLOCKED with the reason; never DONE. A chunk whose purpose is an experiment (C12, C13) completes successfully with "challenger rejected"; an improvement is never required to finish.
9. `python tools/next_chunk.py --done <id> --commit <hash>` sets DONE, the finish date, and the commit. Append a session-log line to `BUILD_STATUS.md` (date, chunk, outcome, deviations from the card, new `[BEN: ...]` flags, backlog IDs).
10. Commit with explicit staging: review `git status`, `git add` the paths the card lists plus `BUILD_STATUS.md`, then `git commit -m "<id>: <title> (DONE)"`. Never `git commit -a`. Stop. One chunk per session; if it finishes early, improve its tests or end the session.

**Sizing rule.** A chunk is 600 to 1,500 lines of code and tests, at most eight new modules, and no open-ended research. Where a card says "discover" or "benchmark", the result is written to a file the next session can read.

**Repair rule.** A defect in an earlier chunk is fixed now only if it blocks the current chunk; otherwise write the failing test and a `BACKLOG.md` row.

**Git hygiene.** Full personal DK exports (`tests/fixtures/real/`, `data/standings/`, `data/raw/`, `runs/`, `outputs/`) are gitignored. Only minimized fixtures under `tests/fixtures/mini/`, `tests/fixtures/http/`, `tests/fixtures/history/`, `tests/fixtures/standings/` are committed. Tests that need a real file skip with a loud message when it is absent.

---

## Ben's prerequisites (outside any session)

- **Before C0a:** Python 3.11 or newer, Git for Windows, Claude Code working in `C:\Users\benja\Documents\Claude\nhl-dfs`. The two DK rule text files reachable at `C:\Users\benja\Downloads\NHL Classic.txt` and `NHL Showdown Captain Mode.txt` (C0a copies them into `docs/rules/`).
- **Before C0b:** one real Classic pair and one real Showdown pair of `DKSalaries.csv` and `DKEntries.csv` (reserve at least one entry, a free or minimum-fee contest is enough, so the entries template downloads). Save as `tests/fixtures/real/<YYYY-MM-DD>/classic/` and `.../showdown/`. C0b is BLOCKED without them; the template is never reconstructed from memory.
- **After C1:** run `tools/register_capture_task.ps1` once so daily snapshots start.
- **Before C11:** one Classic and one Showdown standings export (`Download Standings` from a finished contest) under `data/standings/inbox/`.
- **Any time:** answer the seven flags in plan section 15 by editing the named config or telling a session; the tracker lists them.

---

## Dependency graph

```
C0a ─► C0b ─┬─► C1 ──┬──────────────► C4 ─► C5 ─┬─► C6 ─┐
            │        │                          │       ├─► C8 ─► C9 ─► C10
            │        │                          └─► C7 ─┘    │      │
            └─► C2a ─┴─► C2b ─► C2c ─► C3 ──────────────────┘      │
                                                                    ▼
                                            C11 (needs C8, C3, C0b) ─► C12, C13 (gated)
Exact edges are in chunks.yaml. C9 needs C8, C7, C2c. C10 needs C9, C7. C13 also needs C6.
```

**First-eligibility order:** C0a, C0b, C1 or C2a (either order), C2b, C2c, C3, C4, C5, C6 or C7 (either order), C8, C9, C10, C11, then C12 and C13 when their gates open.

**Milestones.** After C2c: an uploadable legal file, offline, with lock-safe late swap. After C3: a provisional leverage-aware portfolio using ownership, duplication, and real payout metadata on priors. After C8: a scenario-based, objective-aware portfolio. After C11: the learning loop and financial ledger.

---

## Chunk cards

### C0a · Repo scaffold, contracts, exact scoring, tracker tooling

**Depends on:** nothing. **Read first:** this card; plan §5 "Definitions and scoring contract" table and §7 "Hard roster contracts" table; the two rule files after copying them.

**Goal.** A pinned Python package whose scoring and legality functions are exact against the DK rule documents, plus the tooling every later session uses to find its chunk and update the tracker.

**Create.**

- `pyproject.toml` (package `nhl_dfs` under `src/`, Python ≥3.11; dependencies: numpy, pandas, pyarrow, scipy ≥1.11, pyyaml, requests, pytest, pytest-timeout), `requirements.lock` (or `uv.lock` if `uv` is installed), `.gitignore` (`data/raw/`, `data/cache/`, `data/standings/`, `data/ledger/`, `tests/fixtures/real/`, `runs/`, `outputs/`, `.venv/`, `__pycache__/`), `git init` and first commit.
- `nhl.ps1` and `nhl.sh`: both run `python -m nhl_dfs.cli` from the repo root with the pinned environment (`.venv\Scripts\python.exe` on Windows).
- `src/nhl_dfs/cli.py`: `status` (prints tracker table and open [BEN] flags); `verify` and `run` as stubs that print "available after C0b/C2b" and exit 2.
- `src/nhl_dfs/contracts/scoring.py`, `geometry.py`, `ids.py`, `statuses.py`.
- `docs/CONTRACTS.md`: the scoring table with exact values, roster geometry, ID and person-key rules, the four per-row states, the evidence states, and the status vocabularies. Two pages; later sessions read this instead of the plan.
- `docs/rules/NHL_Classic.txt`, `docs/rules/NHL_Showdown_Captain_Mode.txt` (copied verbatim).
- `tools/next_chunk.py`, `pytest.ini` (markers `c0a` … `c13`; `timeout = 120`), `tests/conftest.py`, `tests/test_scoring.py`, `tests/test_geometry.py`, `tests/test_next_chunk.py`.

**Interfaces.**

```python
# contracts/scoring.py   (integer arithmetic; tenths of a DK point)
@dataclass(frozen=True)
class SkaterLine: goals:int=0; assists:int=0; sog:int=0; blocks:int=0; sh_points:int=0; shootout_goals:int=0
@dataclass(frozen=True)
class GoalieLine: decision:str="ND"   # "W" | "L" | "OTL" | "ND"
                  saves:int=0; goals_against:int=0; shutout:bool=False
                  goals:int=0; assists:int=0; sog:int=0; blocks:int=0     # offensive stats a goalie accrues
def score_skater_tenths(s: SkaterLine) -> int
def score_goalie_tenths(g: GoalieLine) -> int
def captain_twentieths(base_tenths: int) -> int          # base_tenths * 3
def lineup_twentieths(base_tenths: Sequence[int], captain_index: int | None) -> int
def tenths_to_points(t: int) -> float
```

Values: goal 85, assist 50, SOG 15, block 13, SH point bonus 20 per SH goal or assist, shootout goal 15 (not a goal), hat trick 30 at goals ≥3, 5+ SOG 30, 3+ blocks 30, 3+ points 30 where points = goals + assists; goalie win 60, save 7, GA −35, shutout 40, OTL 20, 35+ saves 30; goalies also earn skater stats. A shootout loss with zero regulation and OT goals against is shutout + OTL. `shutout` is a boolean the caller decides (sole goalie of record, full game); scoring does not infer it.

```python
# contracts/geometry.py
class Mode(Enum): CLASSIC = "classic"; SHOWDOWN = "showdown"
CLASSIC_SLOTS  = ("C","C","W","W","W","D","D","UTIL","G")
SHOWDOWN_SLOTS = ("CPT","FLEX","FLEX","FLEX","FLEX","FLEX")
SALARY_CAP = 50_000
@dataclass(frozen=True)
class PoolRow: role_id:str; person_key:str; name:str; team:str; position:str
               roster_positions:frozenset[str]; salary:int; game_info:str
               appg_raw:float|None; appg_flag:str          # appg_flag in {"VALUE","APPG_ZERO","MISSING"}
    @property def is_goalie(self) -> bool
def slot_accepts(slot: str, row: PoolRow, mode: Mode) -> bool
def check_lineup(rows: Sequence[PoolRow], mode: Mode) -> LegalityResult   # rows in slot order
# LegalityResult: ok, reasons: list[str], salary_total, skater_teams: set[str], teams: set[str]
def lineup_key(rows: Sequence[PoolRow], mode: Mode) -> str
# Classic key: sorted person keys. Showdown key: CPT person + "|" + sorted FLEX persons.
```

Checks: exact slot count; `slot_accepts` requires the slot label in `roster_positions` (DK gives UTIL only to skaters; G accepts only G); distinct `person_key`; salary ≤ cap using each row's own salary; Classic non-goalie rows span ≥3 teams; Showdown CPT row has roster positions `{"CPT"}` and FLEX rows `{"FLEX"}`; Showdown rows span ≥2 distinct teams (for a two-team pool this is exactly both teams). No other rule.

```python
# contracts/ids.py
def normalize_name(s: str) -> str                 # lowercase, strip accents and punctuation, collapse spaces
def position_group(pos: str) -> str               # C/LW/RW/W -> "F"; D -> "D"; G -> "G"
def person_key(name: str, team: str, position: str) -> str    # f"{normalize_name(name)}|{team}|{position_group(position)}"
# contracts/statuses.py (enums)
# FileStatus; NewsState(FULL, PARTIAL, NONE); ModelStatus(PRIOR, PARTIAL, FULL); SearchStatus(FEASIBLE, TIME_LIMIT_WITH_INCUMBENT, INFEASIBLE, ERROR)
# DeliveryStatus(CHECKED, DEGRADED_REVIEW, FAILED); ObsStatus(CURRENT, STALE, CONFLICTED, MISSING); GoalieState(EXPECTED, CONFIRMED, OUT, CONFLICTED)
# Participation(PLAYING, QUESTIONABLE, OUT, UNKNOWN); Eligibility(ROSTERABLE, DISABLED); CellLock(OPEN, EDIT_STOP, LOCKED)
# PayoutSource(EXACT, PRIOR); OutcomeCalibration(UNVALIDATED, SHADOW, VALIDATED); FieldCalibration(PRIOR, FITTED)
# FeasibleStatus(FOUND, TIMEOUT, INFEASIBLE_PROVEN)
```

```text
tools/next_chunk.py
  (no args)               eligible chunk(s): deps DONE, status TODO or IN_PROGRESS; runs DONE predecessors' checks first; honors requires_files and gated_on from chunks.yaml
  --check <id>            run that chunk's checks; exit 1 on failure
  --start <id>            set IN_PROGRESS with today's date
  --done <id> --commit H  set DONE, finish date, commit; refuses unless --check passes
  --block <id> --reason   set BLOCKED with reason
  --status                print the table and open [BEN] flags
Parses BUILD_STATUS.md by its literal column order and round-trips the file without touching other lines.
```

**Tests.** Every threshold boundary (4 vs 5 SOG, 2 vs 3 blocks, 2 vs 3 points, 2 vs 3 goals); bonuses stack (3 goals + 0 assists = 255 + 30 + 30); SH points add 20 each; shootout goal is 15 and not a goal; goalie 25 saves 3 GA loss = 175 − 105 = 70; goalie shootout loss with 0 GA = 20 + 40 + saves; goalie assist earns 50; captain 13 → 39 twentieths, never floating point; Classic legality: two-team eight-skater roster fails, goalie does not count toward three teams, G in UTIL fails, 50,001 fails, duplicate person fails; Showdown: FLEX row in CPT slot fails, one-team roster fails, two opposing goalies passes, a four-team pool with two teams represented passes; `lineup_key` ignores FLEX order and distinguishes captains; `next_chunk.py` on a fixture tracker prints the right chunk and refuses when a predecessor check fails.

**Exit checks.** `pytest -m c0a -q` green; `python tools/next_chunk.py` prints `C0b` (BLOCKED until real files exist, and says so); `.\nhl.ps1 status` prints the tracker; `git log` shows the initial commit.

**Do not.** Parse DK files (C0b). Add any dependency beyond the list. Write a solver.

---

### C0b · Intake, byte-splicing export, independent referee, real-file fixtures

**Depends on:** C0a. **Read first:** this card; `docs/CONTRACTS.md`; plan §10 "Intake and exact output" and "Pre-export checks". **Ben supplies:** the real fixture files. BLOCKED without them.

**Goal.** Read real DK files byte-faithfully, write the entries template back by splicing cells into the original bytes, and re-check the written bytes with a referee that shares no parser or rule code with the builder.

**Create.** `src/nhl_dfs/intake/salary.py`, `intake/entries.py`, `src/nhl_dfs/export/writer.py`, `src/nhl_dfs/referee/reader.py`, `referee/rules.py`, `referee/check_file.py`, `tests/fixtures/real/<date>/{classic,showdown}/` (gitignored), `tests/fixtures/mini/` (≤40-row synthetic pools and entries files for both modes, including an `Elias Pettersson` C and D on the same team and a `Sebastian Aho` C and D on different teams), `tests/test_intake.py`, `tests/test_export.py`, `tests/test_referee.py`, `tests/test_rules_agreement.py`; `cli verify` implemented.

**Interfaces.**

```python
# intake/salary.py
def read_salary(path) -> SalaryPool
# SalaryPool: mode (any Roster Position in {CPT, FLEX} -> SHOWDOWN, else CLASSIC), rows: list[PoolRow], by_role_id,
#   persons: dict[person_key, PersonRows(cpt, flex, classic)], teams, games: dict[str, GameInfo(home, away, start_et_text, start_utc)],
#   conflicts: list[Conflict], sha256, raw: bytes
# Required columns exactly: Position, Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame.
# APPG: blank -> (None, "MISSING"); 0 -> (0.0, "APPG_ZERO"); else (value, "VALUE"). Showdown pairing by (Name, TeamAbbrev, Position);
# CPT salary != 1.5x FLEX is a warning; CPT APPG != FLEX APPG is recorded in the conflicts report (never rewritten).
# Same Name+Team+Position with more rows than the mode allows -> Conflict; those rows are excluded from selection and reported.

# intake/entries.py
def read_entries(path) -> EntriesFile
# EntriesFile: raw: bytes, newline: str, bom: bool, header: list[str], roster_cols: list[int], entries: list[EntryRow(entry_id, contest_name, contest_id, fee, cells, line_no, line_bytes)],
#   tail_lines: list[bytes] (verbatim; never parsed as entries), sha256
# An entry row has a numeric Entry ID. The instructions/player-list block to the right and below is preserved as bytes.

# export/writer.py
def write_entries(entries: EntriesFile, assignment: dict[str, tuple[str, ...]], pool: SalaryPool, out_path) -> bytes
# Splices f"{row.name} ({row.role_id})" into each roster cell of the entry's original line bytes; every other byte unchanged; rows never re-serialized;
# every entry must be assigned (raises otherwise).

# referee/reader.py      (stdlib csv only; no imports from intake or contracts.geometry)
def read_salary_min(path) -> dict[str, RefRow]     # role_id -> (person_key, roster_positions, salary, team, is_goalie), plus sha256
def read_entries_min(path) -> RefEntries          # entry ids in order, cells, sha256
# referee/rules.py        (independent implementation of the roster rules; must not import contracts.geometry)
def legal(rows_in_slot_order, mode) -> tuple[bool, list[str]]
# referee/check_file.py
def check_file(out_path, salary_path, entries_path, *, parent_path=None, locked: dict[tuple[str,int], str]|None=None) -> RefereeReport
# Always binds out bytes to salary and entries hashes; entry set and order must equal the entries file (or the parent when given);
# each cell's ID exists with the right role; rules.legal per entry; locked cells unchanged; ok only if every entry passes.
```

**Tests.** Real Classic and Showdown files round-trip (assign each entry its existing lineup, or a legal one from the pool if blank), write, and compare bytes outside roster cells; BOM, newline style, and the tail block preserved; CPT ID in CPT slot passes and a FLEX ID in the CPT slot fails; wrong-mode file rejected; Pettersson/Aho mini fixtures produce Conflicts and are never merged; referee catches a hand-edited cap overrun, a duplicated person, and a swapped entry order; property test: `referee.rules.legal` agrees with `contracts.geometry.check_lineup` on 1,000 random slot assignments over the mini pools; `verify` exits 0 on a valid file and 1 with reasons otherwise; real-file tests skip loudly when the files are absent.

**Exit checks.** `pytest -m c0b -q` green with the real files present; `.\nhl.ps1 verify --salary <real classic> --entries <real classic> --out <real classic>` prints `FILE_VALID=TRUE` for the downloaded file (or FALSE with "empty roster" reasons if the template has blank lineups, and TRUE after a test assignment).

**Do not.** Fetch anything. Build a solver. Read the full fixture files into the conversation.

---

### C1 · Adapters, cache, observations, prospective capture

**Depends on:** C0b. **Read first:** this card; plan §3 rows for DK public, NHL (schedule, box score, per-game reports, partner odds), Covers; `docs/CONTRACTS.md`.

**Goal.** Every keyless source behind one cached, schema-checked HTTP layer with a bounded budget and an offline mode, plus a scheduler script that snapshots the sources daily from now on.

**Create.** `src/nhl_dfs/data/http.py`, `data/observations.py`, `data/sources/dk_public.py`, `data/sources/nhl.py`, `data/sources/covers.py`, `config/sources.yaml`, `config/capture.yaml`, `config/teams.yaml` (32 rows: DK abbrev, NHL abbrev, Daily Faceoff slug, full name), `tools/capture.py`, `tools/register_capture_task.ps1`, `tests/fixtures/http/` (one recorded response per source, trimmed to ≤40 KB), `tests/test_http.py`, `tests/test_dk_public.py`, `tests/test_nhl.py`, `tests/test_covers.py`; `cli probe`.

**Interfaces.**

```python
# data/http.py
class HttpCache:
    def get_json(self, url, *, source, ttl_s, schema: Callable[[Any], None]) -> Fetched
    def get_text(self, url, *, source, ttl_s, schema) -> Fetched
# Fetched: data, fetched_at_utc, from_cache, raw_path (data/raw/<source>/<YYYY-MM-DD>/<sha>.json|html), raw_hash
# Browser-like User-Agent; timeout 6 s; one retry after 1 s (worst case about 14 s per call, config); NHL_DFS_OFFLINE=1 -> cache only else SourceUnavailable;
# HTTP 403/404/5xx -> SourceUnavailable (never partial data); schema failure -> SourceSchemaError; every fetch appends an Observation.

# data/sources/dk_public.py
def lobby() -> list[ContestSummary]                      # id, name, fee, field_size, max_per_user, prize_pool, draft_group_id, game_type, start_utc
def contest_detail(contest_id: int) -> ContestDetail    # payout_table: list[PayoutTier(min_pos, max_pos, cash: Decimal)], maximum_entries, max_per_user, entry_fee, entries, draft_group_id, start_utc, raw
def draftables(draft_group_id: int) -> Draftables       # rows: Draftable(draftable_id, player_id, name, position, roster_slot_id, salary, status_raw, participation: Participation,
                                                        #   eligibility: Eligibility, is_swappable, news_status, team, competition_id, start_utc); competitions
STATUS_MAP = {"OUT": OUT, "IR": OUT, "O": OUT, "Q": QUESTIONABLE, "GTD": QUESTIONABLE, "D": QUESTIONABLE, "None": PLAYING, "": PLAYING}   # else UNKNOWN, raw preserved
def reconcile(pool: SalaryPool, d: Draftables) -> Reconciliation   # by draftable_id == role_id; salary/team mismatches -> CONFLICTED; participation/eligibility/start by role_id

# data/sources/nhl.py
def schedule(date) -> list[Game]
def roster(team_abbrev) -> list[NhlPlayer]
def boxscore(game_id) -> BoxScore
def game_log(nhl_id, season: int, game_type: int = 2) -> list[GameLogRow]
def skater_report(report: Literal["summary","timeonice","realtime"], date_from, date_to) -> list[dict]   # per-game rows, paginated (start/limit), cayenneExp on gameDate
def partner_odds() -> OddsSnapshot                       # as_of_utc, book, games: GameOdds(game_id, home_ml, away_ml, home_ml_3way, away_ml_3way, draw_ml, total_line, over_price, under_price, home_puck, away_puck)
# data/sources/covers.py
def odds() -> OddsSnapshot                               # fail closed on any unparsed row
# data/observations.py: Observation(source, source_player_id, game_id, observed_at, published_at, fetched_at, valid_from, raw_hash, definition_version, status) ; append()
```

```text
tools/capture.py --once     lobby(); contest_detail for up to N contests per draft group (config); draftables per group; schedule(today); partner_odds(); covers raw HTML;
                            Daily Faceoff starting-goalies page and each slate team's line page as raw HTML INCLUDING <script> tags (no parsing here)
                            -> data/raw/capture/<YYYY-MM-DD>/<HHMM>/... plus index.json with fetched_at and hashes.
tools/register_capture_task.ps1
                            $RepoRoot = Split-Path -Parent $PSScriptRoot; action = "$RepoRoot\.venv\Scripts\python.exe" "$RepoRoot\tools\capture.py" --once, WorkingDirectory $RepoRoot;
                            one task per time in config/capture.yaml (defaults, America/Chicago: 08:30 11:30 14:30 16:30 17:30 18:00 18:30 18:50 19:20 19:50 20:50 21:20); idempotent; prints how to unregister.
```

**Tests.** Each parser on its recorded fixture; schema failure raises and returns nothing partial; a 403 fixture becomes SourceUnavailable; offline mode returns cache and raises when absent; Showdown draftables pairing (slots 612 and 613, salary 1.5x, distinct IDs) reconciles to the Showdown fixture by `draftable_id`; STATUS_MAP maps `OUT`/`IR` to OUT, `Q` to QUESTIONABLE, and an unrecognized value to UNKNOWN with the raw value preserved; `isSwappable` never changes participation; per-game reports parse `evTimeOnIce`, `ppTimeOnIce`, `shTimeOnIce`, `blockedShots`; partner odds parse 2-way and 3-way; Covers parser fails closed on a mutated fixture; capture writes an index with hashes and the goalie page HTML contains its script tags.

**Exit checks.** `pytest -m c1 -q` green; `.\nhl.ps1 probe` hits each live source once and prints one status line per source (network failures allowed and recorded); `python tools/capture.py --once` writes a snapshot directory; the scheduler script runs without error (Ben registers it after the session).

**Do not.** Parse Daily Faceoff HTML (C7). Backfill history (C4). Store anything outside `data/raw/`.

---

### C2a · Priors, feasibility fallback with result states, HiGHS candidate builder

**Depends on:** C0b. **Read first:** this card; `docs/CONTRACTS.md`; plan §7 solver paragraph; §10 ladder steps 1, 4, and 5.

**Goal.** Generate many legal lineups quickly from any objective, with a solver-free fallback that distinguishes a timeout from proven infeasibility, and a sampling mode that allows repeats for field construction.

**Create.** `src/nhl_dfs/models/priors.py`, `config/priors.yaml`, `src/nhl_dfs/build/feasible.py`, `build/milp.py`, `build/candidates.py`, `tests/test_priors.py`, `tests/test_feasible.py`, `tests/test_milp.py`, `tests/test_candidates.py`.

**Interfaces.**

```python
# models/priors.py
def prior_table(pool: SalaryPool, cfg) -> dict[str, Prior]     # Prior(mean_tenths, sd_tenths, source: "APPG_SHRUNK" | "BUCKET", appg_flag)
# mean = w*APPG + (1-w)*bucket_mean; w from config (default 0.6, labeled a challenger setting); APPG_ZERO -> w = 0 and flag kept; MISSING -> bucket.
# config/priors.yaml: bucket table by (position group, salary band) with a header comment "engineering placeholders; replace from history in C5".

# build/feasible.py     (no scipy import)
@dataclass class FeasibleResult: status: FeasibleStatus; lineup: list[str] | None; nodes: int; elapsed_s: float
def find_one(pool, mode, *, exclude=frozenset(), locked: dict[int, str] | None = None, budget_s: float = 2.0) -> FeasibleResult
# INFEASIBLE_PROVEN only when the search space is exhausted; TIMEOUT otherwise. Classic: goalie first, then slots cheapest-first with backtracking and a team-count repair;
# Showdown: enumerate CPT rows by salary, then FLEX completion. Respects arbitrary roster_positions, duplicate persons, and locked slots.

# build/milp.py
@dataclass class GroupConstraint: role_ids: frozenset[str]; min_count: int = 0; max_count: int | None = None
def solve_lineup(pool, mode, objective: dict[str, float], *, exclude=frozenset(), locked=None, groups=(), max_overlap_with=(), time_limit_s=5.0) -> SolveResult
# SolveResult(status: SearchStatus, lineup, objective_value, elapsed_s, solver_status_code). Classic x[row, slot]; Showdown x[role_row]; team indicators for the 3-team and
# 2-team rules over ALL slots including locked ones; scipy status 2 -> INFEASIBLE; 1 -> TIME_LIMIT_WITH_INCUMBENT when feasible; returned lineups re-validated with check_lineup.
def solver_available() -> bool       # import probe; callers route to feasible.find_one when False

# build/candidates.py
def generate(pool, mode, objective, n, *, seed, perturb_sd, groups_menu=(), time_limit_total_s=20.0, distinct=True, min_pairwise_diff=2) -> list[Candidate]
# Candidate(role_ids, key, objective_value, family). distinct=True excludes repeats and enforces min_pairwise_diff (portfolio use);
# distinct=False draws with replacement and returns repeats (field use). Perturbation is Gumbel noise on the objective.
```

**Tests.** MILP solutions pass `check_lineup`; an all-one-team pool returns INFEASIBLE; a tiny time limit returns TIME_LIMIT_WITH_INCUMBENT or FEASIBLE, never an invalid lineup; property test over 200 random pools: whenever MILP finds a lineup, `find_one` returns FOUND; a pool with two rich teams forces the third team; a Showdown pool with locked rows all from one team forces the other team into a free slot; `find_one` with `budget_s=0.001` on a large pool returns TIMEOUT, never INFEASIBLE_PROVEN; a provably infeasible tiny pool returns INFEASIBLE_PROVEN; `generate(distinct=True)` returns distinct keys with pairwise difference ≥2; `generate(distinct=False)` returns repeats when the objective is sharp; `solver_available()` is False when the import is monkeypatched to fail.

**Exit checks.** `pytest -m c2a -q` green; benchmark prints ≥150 distinct Classic and ≥150 distinct Showdown candidates from the real fixtures in ≤10 s each (record in the tracker).

**Do not.** Simulate. Assign entries. Add exposure logic.

---

### C2b · Local-first baseline run, atomic publish under a slate lock, manifest, CLI

**Depends on:** C2a, C1. **Read first:** this card; plan §10 ladder and status list; §12 "Short run-note fields".

**Goal.** `nhl.ps1 run --baseline` turns the two DK files into a checked, published `DKEntries.csv` in seconds from local inputs only, then runs a bounded network pass that can only add a newer version.

**Create.** `src/nhl_dfs/build/assign.py`, `build/run.py`, `build/state.py`, `build/manifest.py`, `build/notes.py`, `config/runtime.yaml` (`network_pass_budget_s: 25`, clock buffer), `config/exposure.yaml`, `tests/test_assign.py`, `tests/test_state.py`, `tests/test_run_baseline.py`, `tests/test_fallbacks.py`; `cli run --baseline [--offline]`, `cli verify --run <id>`, `cli status` extended with last run.

**Interfaces.**

```python
# build/assign.py
def assign(candidates, entries, pool, mode, caps: Caps, *, seed, later_start_utc: dict[str, datetime] | None = None) -> Assignment
# Assignment(by_entry, exposures, person_exposures, captain_exposures, overlap_max, relaxations). Distinct candidates by objective; person and captain caps with the feasibility floor;
# Classic overlap ≤7; UTIL receives the later-starting eligible skater when the objective is within the tie band; repetition of the best legal lineup is the last resort, recorded.

# build/state.py
def new_run(root="runs") -> RunDir                # runs/<YYYYMMDD-HHMMSS>-<mode>/ with inputs/, versions/, sim/, qa/, news/
def publish(run: RunDir, export_bytes: bytes, report: RefereeReport, slate_id: str) -> Path
# versions/v<N>/DKEntries.csv via temp + fsync + rename; refuses when report.ok is False; run/current -> v<N>;
# public outputs/<slate_id>/DKEntries.csv replaced by writing a temp file IN THAT DIRECTORY and os.replace(), under outputs/<slate_id>/.lock (slate lock) plus the run lock.

# build/run.py
def run_slate(salary_path, entries_path, *, offline: bool, baseline_only: bool = True, out_root="runs") -> RunResult
# Phase A (local only, no network): intake -> priors -> candidates (milp if solver_available else feasible) -> assign -> write -> referee -> publish v1 -> manifest -> RUN_NOTES.md
# Phase B (skipped when offline; bounded by network_pass_budget_s): draftables reconcile -> exclude participation OUT and eligibility DISABLED -> re-solve affected entries -> referee -> publish v2 if changed
# Empty candidate bank -> feasible.find_one per entry; INFEASIBLE_PROVEN -> report SEARCH_STATUS=INFEASIBLE with scope and stop; TIMEOUT -> retry once with larger budget then report.
# manifest.json: run_id, created_utc, mode, slate_id, salary_sha256, entries_sha256, export_sha256, statuses (FILE_VALID, NEWS_STATE, MODEL_STATUS, SEARCH_STATUS, DELIVERY_STATUS,
#   PAYOUT_SOURCE, OUTCOME_CALIBRATION, FIELD_CALIBRATION), entry_count, exposures_top20, relaxations, phase_timings, versions
```

**Tests.** Offline end-to-end on both real fixtures yields FILE_VALID=TRUE with no network call (socket monkeypatched to fail loudly); Phase A completes and publishes before any Phase B call is attempted (order asserted with a spy); a crash between temp write and `os.replace` leaves the public file unchanged; two concurrent runs on one slate serialize on the slate lock; a failing referee report blocks publish; manifest hashes match bytes; solver import failure routes to `find_one` and still publishes; an empty candidate bank routes to `find_one`; a Phase B source outage leaves v1 as current with NEWS_STATE=NONE; 20 entries ≤15 s and 150 entries ≤30 s for Phase A on the fixture.

**Exit checks.** `pytest -m c2b -q` green; `.\nhl.ps1 run --salary <real> --entries <real> --baseline --offline` writes `DKEntries.csv`, `manifest.json`, `RUN_NOTES.md`; `.\nhl.ps1 verify --run <id>` passes.

**Do not.** Use projections beyond priors. Call the LLM. Add ownership.

---

### C2c · Four-state lock model, late swap, refresh (baseline objective)

**Depends on:** C2b. **Read first:** this card; plan §11 "Fast mode" and the four-state paragraph in §10/§11.

**Goal.** Re-run in seconds from a current DK entries export, pinning locked cells, adding no started player, refusing edits inside the edit-stop buffer without claiming a game started, and re-solving only what is open.

**Create.** `src/nhl_dfs/build/locks.py`, `build/late_swap.py`, `build/refresh.py`, `tests/fixtures/late_swap/` (synthetic partially locked entries files, Classic and Showdown, including a two-game Showdown pool), `tests/test_locks.py`, `tests/test_late_swap.py`, `tests/test_refresh.py`; `cli late-swap --run <id> --entries <current> [--fast] [--offline]`, `cli refresh --run <id> [--offline]`.

**Interfaces.**

```python
# build/locks.py
@dataclass class CellState: lock: CellLock; reason: str        # LOCKED: game started or isSwappable false; EDIT_STOP: inside buffer; OPEN otherwise
def compute(entries_current: EntriesFile, pool, draftables: Draftables | None, now_utc, buffer_s: int) -> LockState
# LockState: cells: dict[(entry_id, slot_idx), CellState], started_role_ids, started_games, edit_stop_games. Start time = draftables competition start, else salary Game Info.
# "Started" is strictly now_utc >= start. Participation and eligibility are NOT lock inputs; they are exclusion inputs handled by the re-solve.

# build/late_swap.py
def run(run_id, entries_current_path, *, offline: bool, fast: bool = True) -> RunResult
# Parent = the current export, never the last generated file; diff vs last delivered version recorded; LOCKED and EDIT_STOP cells pinned; started players never added;
# re-solve is the full-lineup MILP with pinned variables fixed (team rules span pinned and free slots); residual exposure optimized; predecessor preserved if a lock boundary is crossed mid-compute.
# build/refresh.py
def run(run_id, *, offline: bool) -> RunResult      # re-fetch draftables (participation/eligibility/start) and odds; uses the SAME LockState rules as late swap; re-solves only affected entries
```

**Tests.** All-locked file returns unchanged bytes; partially locked changes only OPEN cells and reports pinned exposures over cap; EDIT_STOP cells are pinned and the report says "edit stop", not "started"; a started player is never added; Showdown with all pinned rows from one team forces the other team in a free slot; a two-game Showdown fixture with game 1 locked re-solves game 2 legally; an expensive goalie replacement forces a two-player repair within cap; lock boundary crossing mid-compute keeps the predecessor; refresh applies the same pins as late swap; DST and UTC conversions on fixture dates.

**Exit checks.** `pytest -m c2c -q` green; `.\nhl.ps1 late-swap --run <id> --entries <fixture current> --fast --offline` completes in ≤30 s with locked cells unchanged. **Milestone 1: usable slate workflow.**

**Do not.** Call the LLM. Assume the last generated file was uploaded. Read roles or news (C7).

---

### C3 · Ownership prior, field sampler, duplicate proxy, evidence gates, provisional leverage selection

**Depends on:** C2c. **Read first:** this card; plan §6 in full; §7 "Deterministic tie-break" and the evidence-state paragraph; §12 evidence-floor table.

**Goal.** On priors alone: a hand-weighted ownership prior, an opponent field sampled with replacement, a duplicate proxy, the single evidence-gate definition, and a provisional leverage-aware selection that uses real payout metadata and labels every figure as provisional.

**Create.** `src/nhl_dfs/models/projection.py` (the `Projection` interface priors and later params both satisfy), `models/ownership.py`, `models/field.py`, `models/prefit.py`, `learn/gates.py`, `config/evidence_floors.yaml` (the plan §12 table, per mode), `config/ownership.yaml`, `config/contest_families.yaml` (name-pattern inference and priors when the contest endpoint fails), `build/provisional.py`, `tests/test_ownership.py`, `tests/test_field.py`, `tests/test_gates.py`, `tests/test_prefit.py`, `tests/test_provisional.py`; `cli field --run <id>`; `cli run` gains the provisional pass after Phase B.

**Interfaces.**

```python
# models/projection.py
class Projection(Protocol): def mean_tenths(self, role_id) -> int; def sd_tenths(self, role_id) -> int; def source(self) -> ModelStatus
# models/ownership.py
def utilities(pool, proj: Projection, contests: list[ContestDetail | FamilyPrior], odds: OddsSnapshot | None, roles=None, cfg=...) -> dict[str, dict[str, float]]   # by contest family
# Features: salary rank within position, value (mean/salary), APPG (raw), team implied total when odds exist, PP1 and line flags when roles exist, goalie start*win, news recency.
# models/field.py
@dataclass class Behavior: name; weight; noise_sd; stack_rule; captain_rule; salary_left_pref
def sample(pool, mode, util, behaviors, n, seed, contest_family) -> Field       # draws WITH replacement via candidates.generate(distinct=False); Field(lineups (n, slots), multiplicity, behavior_id)
def marginals(field: Field, pool, field_size: int) -> Marginals                  # own (Classic ≈900%, Showdown CPT 100% + FLEX 500%), cpt_share, dup_counts weighted to field_size, stack_freq, salary_left_hist
def dup_proxy(role_ids, own, mode) -> float                                     # sum of log own + salary-left bucket + captain own (Showdown)
# learn/gates.py
def tier(counts: EvidenceCounts, mode: Mode) -> GateReport     # reads config/evidence_floors.yaml; per-mode counts; the ONLY gate any fit or promotion may consult
def allows(action: Literal["prefit","field_fit","mixture_fit","rate_correction","strategy_change"], counts, mode) -> tuple[bool, str]
# models/prefit.py
def fit(standings_dir, pools) -> PrefitResult     # runs only if gates.allows("prefit") for that mode using the historical counts; chronological holdout (Classic ≥10 groups, Showdown ≥15 games); returns offsets or "gated"
# build/provisional.py
def select(candidates, proj, marginals_by_contest, contests, entries, caps, cfg, *, seed) -> Assignment
# Rank by projected mean; within the tie band (config, default 3% of mean, never below prior sd/√n) prefer lower ownership then lower dup proxy, bounded by one band;
# contest families: cash -> highest mean, no leverage; WTA and small-field -> lowest dup within band; large GPP -> band + dup. Labels: MODEL_STATUS=PRIOR, PAYOUT_SOURCE per contest,
# OUTCOME_CALIBRATION=UNVALIDATED, FIELD_CALIBRATION=PRIOR. No ceiling or probability figures are reported in this mode.
```

**Tests.** Mass sums within tolerance for both modes; every sampled lineup passes `check_lineup`; sampled field contains repeats and `dup_counts` scale to field size; marginals reproducible under seed; dup proxy monotone in ownership; `gates.allows("prefit")` is False on a synthetic history below the floor and True above it, per mode; prefit on a synthetic standings set above the floor recovers planted offsets and refuses below it; provisional selection never prefers a candidate more than one band worse; cash entries get the highest-mean candidates; the run manifest carries the three evidence states.

**Exit checks.** `pytest -m c3 -q` green; `.\nhl.ps1 run --salary <real> --entries <real> --offline` publishes v1 (baseline) and a provisional version with ownership and duplicate columns in `RUN_NOTES.md`, all figures labeled provisional. **Milestone 2: provisional leverage portfolio.**

**Do not.** Simulate. Fit to live standings (C12). Report any probability or ceiling.

---

### C4 · History cache (MoneyPuck or NHL per-game reports), identity crosswalk, as-of features

**Depends on:** C1. **Read first:** this card; plan §3 MoneyPuck and NHL per-game report rows; §4 regime paragraph.

**Goal.** Two seasons of as-of-safe player history from listed downloads (Tier A) or NHL per-game reports plus box scores (Tier B), stored as Parquet, with an explicit DK-to-NHL identity map and honest missingness where Tier B has no equivalent.

**Create.** `src/nhl_dfs/data/history/moneypuck.py`, `data/history/nhl_reports.py`, `data/history/store.py` (Parquet partitions by season and kind), `data/identity/crosswalk.py`, `data/features/asof.py`, `data/identity/accepted.csv` (empty with header), `docs/features.md` (every feature column: Tier A source, Tier B source or "league prior + `<col>_missing` indicator"), `config/sources.yaml` extended (`moneypuck.enabled: true`, listed URLs, attribution string), `tests/fixtures/history/` (≤50-row samples per kind, two box scores, per-game report samples), `tests/test_moneypuck.py`, `tests/test_nhl_reports.py`, `tests/test_store.py`, `tests/test_crosswalk.py`, `tests/test_asof.py`; `cli history --backfill <seasons>`, `cli identity --seed --salary <path>`, `cli identity --accept <proposal_id>`.

**Interfaces.**

```python
# data/history/moneypuck.py    (listed downloads only; attribution printed by cli history)
def download(kind: Literal["skaters","goalies","lines","teams","shots"], season: int, level: Literal["season","game"]) -> Path
def load(kind, season, level) -> pd.DataFrame          # normalized columns per docs/features.md; situation in {all, 5on5, 5on4, 4on5, other}
# data/history/nhl_reports.py   (Tier B)
def backfill(seasons, *, since=None) -> BackfillStats  # skater_report summary/timeonice/realtime by date window + boxscores for goalie decisions and blocks cross-check; incremental
# Provides: goals, assists (total), SOG, blocks, PP points, TOI by strength, shifts per game. Does NOT provide A1/A2 split, shot quality, or shared ice: those columns are
# filled with league priors by position and role and carry <col>_missing = 1.
# data/history/store.py
def write(kind, season, df) / def read(kind, seasons) -> pd.DataFrame     # Parquet; load of two seasons under one second
# data/identity/crosswalk.py
def seed(pool: SalaryPool) -> CrosswalkResult          # exact normalized name + team + position group -> accepted; else proposal; accepted.csv rows carry sha256(name|team|pos)
def accepted() -> dict[str, int]; def accept(proposal_id, nhl_id) -> None
# data/features/asof.py
def frame(as_of: date, nhl_ids, *, seasons=2) -> FeatureFrame   # games dated < as_of only; regime column; missingness indicators present in both tiers
```

**Tests.** As-of frame excludes same-day and future games (planted future row); MoneyPuck parser on fixtures per kind; Tier B builds the same columns with `<col>_missing` set for A1/A2 share, shot quality, and shared ice, and equal values for TOI by strength, SOG, blocks on an overlapping fixture game; Parquet round trip; crosswalk on the mini pool gives two accepted IDs for the Petterssons by position group and a proposal for a fabricated third; an unmatched call-up stays unmatched and flagged.

**Exit checks.** `pytest -m c4 -q` green; `.\nhl.ps1 history --backfill 2` completes online outside any slate clock (record elapsed and row counts) or the offline fixture path passes; `.\nhl.ps1 identity --seed --salary <real classic>` reports accepted/proposal/unmatched counts.

**Do not.** Model anything. Scrape MoneyPuck pages. Hand-edit `accepted.csv`.

---

### C5 · Opportunity, event-rate, and goalie models, per person

**Depends on:** C4. **Read first:** this card; plan §5 "Opportunity and rate estimation"; §4 rows for TOI, iSF/iCF, A1, blocks, goalies; `docs/features.md`.

**Goal.** One parameter row per person: dressing and start probabilities, TOI by strength, per-60 event rates with shrinkage and dispersion, goalie parameters, and a role-row map, satisfying the `Projection` interface so C3's selection upgrades automatically.

**Create.** `src/nhl_dfs/models/opportunity.py`, `models/rates.py`, `models/goalies.py`, `models/params.py`, `config/model.yaml` (prior weights 300 EV min and 100 ST min, goalie 1,500 shots, decay half-life, position priors, penalty-rate coupling), `tests/test_opportunity.py`, `tests/test_rates.py`, `tests/test_goalies.py`, `tests/test_params.py`; `cli params --salary <path> [--as-of <date>]`.

**Interfaces.**

```python
# models/opportunity.py
def estimate(features, roles: RoleState | None, cfg) -> dict[int, Opportunity]
# Opportunity(p_dress, toi_ev_s, toi_pp_s, toi_sh_s, sd_toi_s, pp_share, unit_ev, unit_pp). PP share denominator is team PP clock time. EV minutes for depth lines scale down with the
# team's expected PP and PK time from penalty rates (manpower budget reconciled). Without RoleState, units come from historical co-membership (Tier A) or are None (Tier B).
# When a RoleState exists and a person has no history, the role's vacated-slot expectation overrides the bucket prior (call-ups).
# models/rates.py
def estimate(features, cfg) -> dict[int, Rates]     # g60/a1/a2/sog60/blk60 by strength, dispersions, shot_quality (prior + indicator when missing), n_ev_min, n_st_min
# models/goalies.py
def estimate(features, schedule, cfg) -> dict[int, GoalieParams]     # p_start (team goalies sum to 1), save_skill (1,500-shot prior), workload_adj from OPPONENT shot-for rate, pull_prob_per_ga
# models/params.py
@dataclass class PersonParams: person_key; nhl_id | None; opportunity; rates | goalie; source: ModelStatus
def build(pool, crosswalk, as_of, cfg) -> ParamTable       # one PersonParams per person; role_map: dict[role_id, person_key]; implements Projection (mean/sd per role via the person)
```

**Tests.** Zero history returns the prior exactly; long history converges to the sample mean; decay weights; PP share denominator; back-to-back lowers p_start and the pair sums to 1; goalie workload uses opponent shot-for rate; leakage guard; every person gets a row and every role row maps to exactly one person (CPT and FLEX rows to the same person); a call-up with no history and a RoleState PP1 assignment gets the role expectation; Tier B params carry the missingness indicators.

**Exit checks.** `pytest -m c5 -q` green; `.\nhl.ps1 params --salary <real classic> --as-of <fixture date>` writes `params.parquet` with no nulls and prints PRIOR/HISTORY/MIXED counts; `.\nhl.ps1 run --offline` now reports MODEL_STATUS=HISTORY or MIXED where the crosswalk resolves.

**Do not.** Simulate. Fetch news. Tune weights to any single slate.

---

### C6 · Aggregate joint simulator, market fit, scoring arrays, calibration harness

**Depends on:** C5, C1. **Read first:** this card; plan §5 "Joint simulation" (including the per-person and pace-factor paragraph), "Vegas integration", "Simulation scale and calibration".

**Goal.** Seeded, chunked scenarios that produce realized stat lines for every person with the right joint structure, mapped to role rows and scored exactly, plus a harness that grades the simulator against held-out games.

**Create.** `src/nhl_dfs/sim/market.py`, `sim/game.py`, `sim/score.py`, `sim/cache.py`, `sim/validate.py`, `config/sim.yaml` (budgets 5,000 design / 20,000 selection / 20,000 referee; pace-factor dispersion; EN probabilities; OT and shootout rules by game type; memory cap), `tests/test_market.py`, `tests/test_game.py`, `tests/test_score_arrays.py`, `tests/test_validate.py`; `cli simulate --run <id> --n <N>`, `cli calibrate --seasons <n>`.

**Interfaces.**

```python
# sim/market.py
def implied(ml_a: int, ml_b: int) -> tuple[float, float]
def fit_game(odds: GameOdds | None, strength: TeamStrength, asof_utc, goalie_confirmed_at) -> GameRates     # lambda_home, lambda_away, p_home_reg_win, p_ot, source MARKET|MODEL, stale
# sim/game.py
def simulate(slate: SlateSpec, params: ParamTable, n: int, seed: int) -> Outcomes
# Per scenario, per game: draw starters and dressing; draw a team pace factor (Gamma) per team; PP opportunities; EV/PP/SH team goals; each goal -> unit, scorer, 0–2 assisters (scorer excluded);
# SOG per PERSON ~ NB(mean = sog60 * TOI * pace), scorer SOG >= goals; blocks scaled by opponent pace; goalie shots = opponent SOG, saves = shots − GA (EN not charged);
# regulation tie -> OT goal or shootout; decision; shutout flag; EN goals from a trailing-team process. Not-dressed persons: all zeros. Outcomes are per PERSON (n, P) int16 arrays.
# sim/score.py
def base_tenths(outcomes: Outcomes, params: ParamTable) -> np.ndarray        # (n, P) per person
def role_tenths(base_person: np.ndarray, role_map) -> np.ndarray             # (n, R): each role row copies its person's column; CPT rows are NOT multiplied here
def lineup_twentieths(role_base: np.ndarray, lineups: np.ndarray, captain_col: int | None) -> np.ndarray   # (n, L); captain ×1.5 applied exactly once here
# sim/cache.py: chunked arrays under runs/<id>/sim/ with seed and hashes, respecting the memory cap
# sim/validate.py
def report(as_of_dates, cfg) -> CalibrationReport    # PIT histograms per stat; observed vs simulated rates of 5+ SOG, 3+ blocks, 3+ points, hat tricks, 35+ saves; line-pair co-ceiling; team totals vs market; goalie win vs implied -> docs/calibration/<date>.md
```

**Tests.** Conservation (team goals = Σ person goals; assists ≤2 per goal and never the scorer; saves + GA = opposing SOG while in net; EN goals not charged); a Showdown person's CPT and FLEX role columns are identical in every scenario and the captain multiplier changes the lineup score exactly once; a person drawn as not dressed has zero minutes and events; same seed reproduces bit-for-bit; scoring arrays equal `contracts.scoring` on 1,000 random stat lines; team SOG dispersion with the pace factor exceeds independent NB dispersion and matches a fixture target; market fit reproduces implied win probability within 0.01 and total within 0.1; stale rule flips when a goalie confirmation postdates `as_of`; validate writes a report on fixture history.

**Exit checks.** `pytest -m c6 -q` green; `.\nhl.ps1 simulate --run <run> --n 20000` completes for the real Classic fixture; record wall clock (target ≤90 s; a miss is recorded, not a failure); `.\nhl.ps1 calibrate --seasons 1` writes a calibration report.

**Do not.** Build the segment simulator (C13). Add a stack bonus. Store a candidates × scenarios matrix for all candidates.

---

### C7 · Roles and news, deterministic

**Depends on:** C5, C1. **Read first:** this card; plan §11 "Evidence and role updates" (including the status map) and "Goalies before confirmation"; §3 Daily Faceoff rows.

**Goal.** Tonight's lines, PP units, goalie states, and participation as a deterministic role state that adjusts the parameter table, with a validated override schema for the LLM path built in C10.

**Create.** `src/nhl_dfs/data/sources/dailyfaceoff.py`, `models/roles.py`, `models/overrides.py`, `build/news.py`, `docs/sources.md` (the confirmed goalie parse path and its fallbacks), an update to `tools/capture.py` to save the confirmed goalie data path, `tests/fixtures/http/dailyfaceoff_*.html` (one team page; one goalie page with its script tags), `tests/test_dailyfaceoff.py`, `tests/test_roles.py`, `tests/test_overrides.py`; `cli roles --salary <path> [--offline]`.

**Interfaces.**

```python
# data/sources/dailyfaceoff.py
def team_lines(slug) -> TeamLines                         # updated_utc from "Last updated"; f_lines, d_pairs, pp1, pp2, pk1, pk2, goalies, injuries [(name, status_text)]
def starting_goalies(date) -> list[GoalieReport]          # 1) parse <script id="__NEXT_DATA__"> JSON if present; 2) else per-team pages' goalie section; 3) else [] (DK status and rotation carry it). Path used is logged.
# models/roles.py
def merge(dk: Reconciliation | None, df_lines, df_goalies, rotation, now_utc, cfg) -> RoleState
# Participation precedence: DK OUT (mapped) -> OUT; DK QUESTIONABLE -> haircut; DK UNKNOWN -> no change, reported; explicit named goalie confirmation -> CONFIRMED; depth order -> EXPECTED; contradictions -> CONFLICTED mixture.
# Eligibility DISABLED excludes from selection regardless of participation. Age policy per plan §11.
# models/overrides.py
@dataclass class Override: role_id; nhl_id; game_id; field; old; new; effective_utc; source_url; claim; confidence; expiry_utc
def validate(o, roles) -> list[str]; def apply(params, roles, overrides) -> ParamTable      # conserves team minutes; call-up role expectation applied here for unmatched persons
# build/news.py
def state(roles, pool) -> NewsState
```

**Tests.** Team-page parser on the fixture returns lines, pairs, PP units, goalies, injuries, timestamp; goalie parser takes the `__NEXT_DATA__` path on the fixture and the per-team path when the tag is removed, and returns [] when both fail; a fetched old confirmation never changes its game date; conflicting goalie reports produce a mixture with a warning; DK OUT overrides a Daily Faceoff line listing; DK UNKNOWN changes nothing and is reported; `isSwappable` never affects participation; `apply` conserves team EV minutes; an override that overfills a unit is rejected.

**Exit checks.** `pytest -m c7 -q` green; `.\nhl.ps1 roles --salary <real classic> --offline` prints per-team role state from captured HTML; `docs/sources.md` states the goalie path in use.

**Do not.** Call the LLM. Change ownership. Invent injury states.

---

### C8 · Scenario objectives by contest family, evidence states, frontier, allocation, full run

**Depends on:** C6, C3. **Read first:** this card; plan §7 in full; §10 ladder steps 2 and 5.

**Goal.** `nhl.ps1 run` publishes the baseline, then the provisional pass, then a scenario-based portfolio: per-family objectives against a weighted sampled field in shared scenarios, exact payouts with DK tie rounding, the tie-break, mode-aware exposure and concentration, a frontier against the risk budget, and fee-weighted allocation.

**Create.** `src/nhl_dfs/build/objectives.py`, `build/tiebreak.py`, `build/exposure.py`, `build/portfolio.py`, `config/risk.yaml` (placeholder budget, mode-aware, `[BEN]` comment), `docs/payouts.md` (the DK Terms of Use tie wording as verified, with date), `tests/test_objectives.py`, `tests/test_tiebreak.py`, `tests/test_exposure.py`, `tests/test_portfolio.py`, `tests/test_run_full.py`, `tests/bench_field.py`; `cli run` full path.

**Interfaces.**

```python
# build/objectives.py
def contest_metrics(cand: np.ndarray, field: np.ndarray, weights: np.ndarray, contest, own_copies) -> Metrics
# cand (S, K), field (S, F_distinct ≤ 5,000) in twentieths from the same scenarios; weights are multiplicities summing to field size; weighted ranks include own copies;
# ties: pooled tier cash / tied count, quantized ROUND_DOWN to the cent (Decimal). Per family: gpp -> exp_payout and p_top1pct; wta -> tie-adjusted first-place equity;
# cash/h2h/double_up -> p_clear_line; satellite -> p_seat with ticket face value separate. Scoring chunked by scenario within config memory cap.
def portfolio_metrics(assignment, contests, scenarios) -> PortfolioMetrics    # R_s, p_zero_payout, p_net_loss, p_lose80, es5, tail_utility (family-specific), concentration by goalie/game/captain
# build/tiebreak.py
def prefer(a, b, own_a, own_b, dup_a, dup_b, band) -> "a" | "b"        # band ≥ Monte Carlo SE; ownership never moves a choice past one band; dup from sampled counts when FIELD_CALIBRATION=FITTED else proxy
# build/exposure.py
def caps(cfg, n_entries, pool, mode, n_games) -> Caps          # feasibility floor; per-game cap only when n_games > 1; Showdown concentration by captain and failure scenario
# build/portfolio.py
def select(candidates, role_base, fields, contests, entries, caps, risk: RiskBudget, families=(0.65, 0.25, 0.10), *, seed) -> Selection
# frontier over five knob settings; report excludes dominated points; choose the highest tail_utility with p_lose80 ≤ budget, else least-risk point (recorded); WTA entries allocated by first-place equity;
# every figure carries PAYOUT_SOURCE, OUTCOME_CALIBRATION, FIELD_CALIBRATION.
```

**Tests.** Three-way tie for first splits pooled cash rounded down to the cent; own copies count as copies; weighted field ranks equal explicit expansion on a small case; tie-break never prefers a candidate more than one band worse; WTA allocation picks higher first-place equity over higher variance on a constructed pair; cash objective ignores ownership; satellite objective counts seats; caps with two goalies raise to the floor; a single-game Showdown slate never triggers the per-game cap; frontier report has no dominated points; infeasible budget yields least-risk point and still a file; full run completes offline within 5 minutes.

**Exit checks.** `pytest -m c8 -q` green; `python tests/bench_field.py` reports field construction (5,000 lineups) plus payout evaluation for 150 candidates × 20,000 scenarios within 60 s and peak memory under the cap (record numbers); `.\nhl.ps1 run --salary <real> --entries <real> --offline` publishes baseline, provisional, and scenario versions with objective metrics and the three evidence states in `RUN_NOTES.md`. **Milestone 3: objective-aware portfolio.**

**Do not.** Call the LLM. Edit a CSV outside `write_entries` and the referee.

---

### C9 · Objective-aware refresh and late swap

**Depends on:** C8, C7, C2c. **Read first:** this card; plan §11 "Fast mode" and "Goalies before confirmation".

**Goal.** Late swap and refresh use the C2c lock model unchanged, but re-solve open slots with C8's scenario objective and C7's role state, and condition on live standings when supplied.

**Create.** Extensions to `build/late_swap.py` and `build/refresh.py` (objective selection: scenario when available, provisional otherwise, baseline as last resort), `build/live.py` (optional current-score conditioning), `config/runtime.yaml` extended (T-8 for LLM paths, T-5 for engine-only), `tests/test_late_swap_objective.py`, `tests/test_live.py`.

**Interfaces.** `late_swap.run(..., objective: Literal["scenario","provisional","baseline"]="auto")`; `refresh.run(...)` re-fetches roles (C7) and odds and re-simulates only affected games; `live.condition(scenarios, standings_snapshot)` restricts remaining upside to unlocked games and current scores; without a reliable snapshot it is a no-op with a status.

**Tests.** Pinned cells identical to C2c behavior; objective falls back in the documented order when the simulator cache is absent; a trailing entry prefers lower duplication only when a snapshot is present; no chase pivot without a snapshot; runtime ≤30 s on the fixture.

**Exit checks.** `pytest -m c9 -q` green; `.\nhl.ps1 late-swap --run <id> --entries <fixture> --fast --offline` ≤30 s.

**Do not.** Call the LLM. Change lock semantics.

---

### C10 · Claude Code layer: skills, agents, controller, rehearsal, measured usage

**Depends on:** C9, C7. **Read first:** this card; plan §8 table and paragraph; §9 in full; §13 skills and agents rows.

**Goal.** Slash commands that wrap the CLI with the engine running before the model reads anything; two fresh-context agents with JSON contracts, launched without project instruction files and fed inline; a controller that validates and applies their output deterministically with one-round default and wall-clock bounds.

**Create.** `.claude/skills/nhl-run/SKILL.md`, `nhl-refresh/`, `nhl-late-swap/`, `nhl-settle/` (stub until C11), `nhl-dev-next/`, `nhl-status/`, `nhl-qa-rehearse/`; `.claude/agents/nhl-researcher.md` (`tools: WebFetch, Read`; `omitClaudeMd: true`; returns Override JSON), `.claude/agents/nhl-adversary.md` (`tools: Read`; `omitClaudeMd: true`; prompt says the packet is inline and no file may be read; returns Proposal JSON); `.claude/settings.json` with `"env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1"}`; `src/nhl_dfs/build/packet.py`, `build/controller.py`, `docs/packet_schema.md`, `docs/proposal_schema.md`, `docs/measured_usage.md` (template), `tests/test_packet.py`, `tests/test_controller.py`; `cli qa-packet --run <id> --round k`, `cli qa-apply --run <id> --round k --proposals <path>`, `cli overrides-apply --run <id> --file <path>`.

**Interfaces.**

```text
Protocol (the model relays bytes; it never edits lineups):
  1. /nhl-run <salary> <entries>: SKILL.md preprocesses `!nhl.ps1 run --salary $0 --entries $1` and injects only the manifest summary (≤60 lines).
  2. If NEWS_STATE != FULL and unresolved players have material exposure: invoke nhl-researcher with the request JSON inline; save its reply verbatim to runs/<id>/news/overrides_k.json; run `nhl.ps1 overrides-apply`.
  3. `nhl.ps1 qa-packet --round 1` writes the packet; the skill passes its CONTENT inline to nhl-adversary (≤4,000 tokens: aggregates, flagged conflicts with IDs, top-10 alternatives with IDs, coverage, predeclared metrics, five representative lineups).
  4. Save the reply verbatim to proposals.json; `nhl.ps1 qa-apply` classifies (correctness | strategic), validates, re-solves, compares on the same scenarios, accepts or rejects with reasons, publishes if changed, prints whether another round is permitted.
  5. Rounds 2–3 only when round 1 accepted a correctness repair; stop on zero accepted, round 3, or deadline (T-8).
```

```python
# build/packet.py
def build(run, round_no, cfg) -> dict         # enforces the size cap by construction; never serializes lineups beyond the five-sample; includes IDs for every referenced person and alternative
# build/controller.py
@dataclass class Proposal: kind: Literal["correctness","strategic"]; target: dict; change: dict; evidence: str; source_url: str | None
def apply_round(run, round_no, proposals, cfg) -> RoundResult
# correctness (verified identity/scoring/lock/scratch fact) accepted without a simulation contest; strategic requires legality, risk budget, no decrease in tail or safety metric, and a gain beyond the band on paired scenarios;
# under FIELD_CALIBRATION=PRIOR an accepted strategic change is recorded as "unvalidated modeled improvement" with the state attached; malformed JSON ends QA and keeps the incumbent.
```

**Tests.** Controller: malformed proposals end QA and keep the incumbent; a correctness repair is accepted without a simulation contest; a strategic proposal within the band is rejected as inconclusive; a strategic proposal beyond the band and safe is accepted and labeled by field-calibration state; packet size cap enforced on a 150-entry fixture; a proposal touching a pinned cell is rejected.

**Exit checks.** `pytest -m c10 -q` green; in a fresh Claude Code session `/nhl-run` on the fixture completes with at most four model turns and publishes; `/nhl-qa-rehearse` launches the adversary with a canary packet inline, confirms the reply references the canary and nothing planted in the main conversation or in CLAUDE.md, and records the result in `docs/measured_usage.md`; `/usage` attribution after the run recorded there too.

**Do not.** Give either agent Write, Edit, Bash, or Agent tools. Let the model edit `DKEntries.csv`.

---

### C11 · Settle: financial ledger, grading, run notes, backlog, gate reports

**Depends on:** C8, C3, C0b. **Read first:** this card; plan §6 "After each slate"; §12 tables, financial-settlement paragraph, "Short run-note fields", "Backlog row". **Ben supplies:** standings exports in `data/standings/inbox/`.

**Goal.** Grade frozen pre-lock forecasts against actual ownership and results, settle the money by Entry ID, write run notes and backlog rows, and report which evidence tier is unlocked, changing no parameter.

**Create.** `src/nhl_dfs/learn/standings.py`, `learn/ledger.py`, `learn/grade_ownership.py`, `learn/grade_forecasts.py`, `learn/backlog.py`, `learn/notes.py`, `BACKLOG.md` (created with header), `data/ledger/` (gitignored), `tests/fixtures/standings/` (≤60-row trimmed Classic and Showdown exports), `tests/test_standings.py`, `tests/test_ledger.py`, `tests/test_grades.py`; `cli settle --run <id> --standings <path or zip>`; `nhl-settle` skill body.

**Interfaces.**

```python
# learn/standings.py
def read(path) -> Standings     # contest_id, entries [EntryRow(rank, entry_id, entry_name, points, lineup [(slot, name)])], ownership [OwnRow(name, roster_position, pct_drafted, fpts)], raw_sha
def join(s, pool) -> Joined     # normalized name + roster position token (+ team when the pool has one candidate); collisions -> CONFLICTED, never guessed; own entries joined by Entry ID exactly
# learn/ledger.py
def settle(run, standings: list[Standings], contests_final: dict[int, ContestDetail]) -> SlateLedger   # own entries -> final rank -> payout via final payout table with DK tie rounding; fees, gross, net, by contest; appends to data/ledger/ledger.parquet; rolling drawdown across slates
# learn/grade_ownership.py: grade(forecast: Marginals, actual: Joined, family) -> OwnershipGrade (mae_all, mae_active, band_calibration, top_chalk_recall, cpt_share_err, dup_count_err, zero_observed_mass)
# learn/grade_forecasts.py: grade(run, boxscores) -> ForecastGrade (mae, crps, p10_p90_coverage, bonus_rate_calibration, goalie decision accuracy; one game outcome counted once)
# learn/gates.py (from C3) is consulted and its tier report written to the run; nothing is tuned
# learn/backlog.py: add(row) with ID, date/run, problem or hypothesis, metric, bounded change, confidence/sample, acceptance test, priority, status, result
```

**Tests.** Fixture parse for both modes; CPT and FLEX ownership separate; Pettersson resolved by position token and a same-team-same-position collision flagged; own entries settle by Entry ID with a tie case rounded down; ledger net equals gross minus fees and drawdown updates across two synthetic slates; grades on synthetic forecasts reproduce known errors; gate report shows the correct tier for planted counts; settle appends backlog rows without duplicates.

**Exit checks.** `pytest -m c11 -q` green; `.\nhl.ps1 settle --run <id> --standings <fixture>` writes `grades.json`, the ledger row, updates `RUN_NOTES.md`, appends to `BACKLOG.md`. **Milestone 4: learning loop live.**

**Do not.** Change any weight or cap. Grade a forecast reconstructed after the fact.

---

### C12 · Field-model fit by contest family (gated)

**Depends on:** C11, and `learn/gates.allows("field_fit")` True for the mode in question (plan §12: ≥30 distinct Classic slate groups with ≥15,000 labels, or ≥50 Showdown games with ≥5,000 labels; latest ≥10 Classic groups or ≥15 Showdown games held out). Mixture-weight changes require `allows("mixture_fit")`, the higher tier. `tools/next_chunk.py` checks the counts per mode.

**Create.** `src/nhl_dfs/models/field_fit.py`, `docs/experiments/field_fit_<date>.md` (preregistration first), `tests/test_field_fit.py`; `cli fit-field --mode <classic|showdown>`.

**Interface.** `fit(standings, pools, mode, cfg) -> FitResult(utility_offsets, mixture_weights | None, holdout_mae_before, holdout_mae_after, decision: "promote_shadow" | "rejected")`; shadow mode by default.

**Exit checks.** `pytest -m c12 -q` green; the experiment file records the result. The chunk is DONE whether the challenger is promoted to shadow or rejected. Promotion to live follows plan §12 in a separate session.

---

### C13 · Segment simulator challenger (gated)

**Depends on:** C11, C6, a calibration report in `docs/calibration/` showing line-pair co-ceiling or bonus-rate error outside tolerance, and a preregistration in `docs/experiments/` written before code.

**Create.** `src/nhl_dfs/sim/segment.py` behind `sim.engine: aggregate | segment`, `docs/experiments/segment_<date>.md`, `tests/test_segment.py`.

**Exit checks.** Same conservation and per-person tests as C6; walk-forward comparison on saved scenarios; DONE whether promoted to shadow or rejected.

---

## Tracker row format (BUILD_STATUS.md)

`| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |` with Status in `TODO | IN_PROGRESS | DONE | BLOCKED | GATED`. `tools/next_chunk.py` edits only Status, Started, Finished, Commit, and Exit checks; Notes and the session log are written by the session. A handoff note in Notes has the form `HANDOFF: done=<...>; remaining=<...>; next=<command>`.
