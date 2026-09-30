"""Scheduled pre-lock refresh dispatcher (backlog B23). The Task Scheduler job registered by
tools/register_refresh_task.ps1 runs `scheduled_refresh.py --once` every few minutes; the logic is in
src/nhl_dfs/build/scheduled.py. `--dry-run` lists what is due and changes nothing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nhl_dfs.build.scheduled import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
