#!/usr/bin/env bash
# B34: SessionStart hook. Cloud sessions (Claude Code on the web set CLAUDE_CODE_REMOTE=true) start with
# no .venv, so the first nhl.sh command failed on 2026-10-01. Locally (Ben's Windows box) this exits at once:
# the local .venv uses C:\Python313 and must not be rebuilt by uv.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "$(dirname "$0")/.." || exit 0
[ -x .venv/bin/python ] && exit 0
command -v uv >/dev/null 2>&1 || { echo "cloud_bootstrap: uv not found; run 'uv sync --frozen' by hand" >&2; exit 0; }
uv sync --frozen >&2 || echo "cloud_bootstrap: 'uv sync --frozen' failed; nhl.sh will say so" >&2
exit 0
