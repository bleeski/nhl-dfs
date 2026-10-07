#!/usr/bin/env bash
# SessionStart hook for cloud sessions (B34, then C39 / B71 / flag 18). Cloud sessions (Claude Code on the web set
# CLAUDE_CODE_REMOTE=true) start with no .venv and no history store (data/features and data/raw are gitignored):
#   1. build the .venv when it is missing (B34: the first nhl.sh command failed on 2026-10-01 without it);
#   2. when the history store is absent, start `history --backfill 2` in the BACKGROUND and return at once. A run never waits for
#      it: Phase A reads local files only, so a run that starts first prices on priors and says so (RUN_NOTES, "History store").
# The background job writes data/cache/history_bootstrap.state (state=RUNNING with its pid, then DONE or FAILED with the exit
# code) and data/cache/history_bootstrap.log; data/cache/ is gitignored. Locally (Ben's Windows box) this exits at once: the
# local .venv uses C:\Python313 and must not be rebuilt by uv.
#
#   tools/cloud_bootstrap.sh --dry-run      say what each step would do and start nothing (also NHL_BOOTSTRAP_DRY_RUN=1)
#
# Always exits 0: a hook that fails would stop the session. Test overrides: NHL_HISTORY_STORE, NHL_BOOTSTRAP_STATE,
# NHL_BOOTSTRAP_LOG.
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
[ "${NHL_BOOTSTRAP_DRY_RUN:-}" = "1" ] && DRY=1
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "$(dirname "$0")/.." || exit 0

STORE="${NHL_HISTORY_STORE:-data/features/history}"
STATE="${NHL_BOOTSTRAP_STATE:-data/cache/history_bootstrap.state}"
LOG="${NHL_BOOTSTRAP_LOG:-data/cache/history_bootstrap.log}"
PY=".venv/bin/python"
say() { echo "cloud_bootstrap: $*" >&2; }

# 1. the .venv
if [ "$DRY" = 1 ]; then
    say "dry run: the .venv step is skipped"
elif [ ! -x "$PY" ]; then
    if command -v uv >/dev/null 2>&1; then
        uv sync --frozen >&2 || say "'uv sync --frozen' failed; nhl.sh will say so"
    else
        say "uv not found; run 'uv sync --frozen' by hand"
    fi
fi

# 2. the history store
if compgen -G "$STORE/skater_games/*.parquet" >/dev/null 2>&1; then
    say "history store present ($STORE): SKIP backfill"
elif [ "$DRY" = 1 ]; then
    say "dry run: history store absent ($STORE): WOULD START backfill (history --backfill 2) in the background"
elif [ ! -x "$PY" ]; then
    say "history store absent but there is no .venv to run it: backfill NOT started"
else
    mkdir -p "$(dirname "$STATE")" "$(dirname "$LOG")" 2>/dev/null
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    SETSID=""
    command -v setsid >/dev/null 2>&1 && SETSID="setsid"
    # A detached wrapper: it records its own pid (alive while the backfill runs), runs the backfill, then records the result.
    # Every descriptor is redirected, so the hook's pipes are not held open and the hook returns at once.
    $SETSID nohup bash -c '
        printf "state=RUNNING\npid=%s\nstarted=%s\n" "$$" "$3" >"$4"
        "$1" -m nhl_dfs.cli history --backfill 2 >"$2" 2>&1
        rc=$?
        state=DONE
        [ "$rc" = 0 ] || state=FAILED
        printf "state=%s\nstarted=%s\nexit=%s\nfinished=%s\n" "$state" "$3" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$4"
    ' bootstrap "$PY" "$LOG" "$started" "$STATE" >/dev/null 2>&1 </dev/null &
    disown 2>/dev/null
    say "history store absent ($STORE): STARTED backfill in the background (log $LOG, state $STATE)"
fi
exit 0
