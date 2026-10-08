"""tools/post_merge.py: branch cleanup after a merge, and the guard on the next dev-session prompt."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.c0a

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
import post_merge as pm  # noqa: E402

ENV = ["-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=master"]


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *ENV, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def commit(cwd: Path, name: str) -> None:
    (cwd / name).write_text(name, encoding="utf-8")
    sh(cwd, "add", name)
    sh(cwd, "commit", "-m", name)


@pytest.fixture()
def world(tmp_path: Path):
    """A bare origin, the working clone `work`, and a second clone `other` that can move origin's master."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "--bare", str(origin))
    work, other = tmp_path / "work", tmp_path / "other"
    sh(tmp_path, "clone", str(origin), str(work))
    commit(work, "base.txt")
    sh(work, "push", "-u", "origin", "master")
    sh(tmp_path, "clone", str(origin), str(other))
    return origin, work, other


def make_branches(work: Path) -> None:
    # feat: merged into master with a merge commit and pushed (a finished pull request)
    sh(work, "switch", "-c", "feat")
    commit(work, "feat.txt")
    sh(work, "push", "-u", "origin", "feat")
    sh(work, "switch", "master")
    sh(work, "merge", "--no-ff", "feat", "-m", "Merge feat")
    sh(work, "push", "origin", "master")
    # wip: local-only commits, never pushed
    sh(work, "switch", "-c", "wip")
    commit(work, "wip.txt")
    # gone: pushed, then its remote branch deleted, with a commit the base lacks (squash-merged look-alike)
    sh(work, "switch", "master")
    sh(work, "switch", "-c", "gone")
    commit(work, "gone.txt")
    sh(work, "push", "-u", "origin", "gone")
    sh(work, "push", "origin", "--delete", "gone")
    sh(work, "switch", "master")


def actions(plan: dict) -> dict:
    return {b["name"]: b["action"] for b in plan["branches"]}


def test_the_report_sorts_branches_into_delete_keep_and_review(world):
    _, work, _ = world
    make_branches(work)
    plan = pm.cleanup_plan(work)
    assert actions(plan) == {"feat": "delete", "wip": "keep", "gone": "review"}
    assert plan["master"] == "in sync" and not plan["dirty"]
    text = pm.render_cleanup(plan)
    assert "DELETE  feat" in text and "REVIEW  gone" in text and "squash-merged" in text
    assert "git push origin --delete feat" in text  # merged and still on the remote: listed, never run


def test_apply_deletes_only_the_fully_merged_local_branch_and_never_a_remote(world):
    origin, work, _ = world
    make_branches(work)
    plan = pm.cleanup_plan(work)
    done = pm.apply_plan(work, plan)
    assert any("deleted local branch feat" in d for d in done)
    branches = sh(work, "branch", "--format=%(refname:short)").split()
    assert "feat" not in branches and "wip" in branches and "gone" in branches  # unmerged work is untouched
    assert "feat" in sh(work, "ls-remote", "--heads", "origin", "feat")  # the remote branch is still there


def test_a_brand_new_dev_branch_is_never_taken_for_a_finished_one(world):
    _, work, _ = world
    sh(work, "switch", "-c", "dev/next", "origin/master")  # exactly how a dev session starts: upstream is origin/master
    sh(work, "switch", "-c", "never-pushed")  # merged-looking, no upstream at all
    sh(work, "switch", "master")
    plan = pm.cleanup_plan(work)
    assert actions(plan) == {"dev/next": "keep", "never-pushed": "review"}
    assert pm.apply_plan(work, plan) == ["nothing to apply"]
    assert {"dev/next", "never-pushed"} <= set(sh(work, "branch", "--format=%(refname:short)").split())


def test_keep_protects_a_branch_and_the_current_branch_is_never_deleted(world):
    _, work, _ = world
    make_branches(work)
    assert actions(pm.cleanup_plan(work, keep=("feat",)))["feat"] == "keep"
    sh(work, "switch", "feat")
    plan = pm.cleanup_plan(work)
    assert actions(plan)["feat"] == "keep" and plan["current"] == "feat"
    pm.apply_plan(work, plan)
    assert "feat" in sh(work, "branch", "--format=%(refname:short)").split()


def test_a_master_that_is_behind_is_fast_forwarded_from_another_branch(world):
    _, work, other = world
    commit(other, "elsewhere.txt")
    sh(other, "push", "origin", "master")
    sh(work, "switch", "-c", "side")
    plan = pm.cleanup_plan(work)
    assert plan["master"] == "behind"
    done = pm.apply_plan(work, plan)
    assert any("fast-forwarded local master" in d for d in done)
    assert sh(work, "rev-parse", "master") == sh(work, "rev-parse", "origin/master")
    assert pm.cleanup_plan(work, fetch=False)["master"] == "in sync"


def test_a_dirty_master_checkout_is_not_touched(world):
    _, work, other = world
    commit(other, "elsewhere.txt")
    sh(other, "push", "origin", "master")
    (work / "base.txt").write_text("edited", encoding="utf-8")
    plan = pm.cleanup_plan(work)
    assert plan["dirty"] and plan["master"] == "behind"
    done = pm.apply_plan(work, plan)
    assert any("not fast-forwarded" in d for d in done)
    assert sh(work, "rev-parse", "master") != sh(work, "rev-parse", "origin/master")


def test_a_diverged_or_ahead_master_is_left_alone(world):
    _, work, other = world
    commit(other, "elsewhere.txt")
    sh(other, "push", "origin", "master")
    commit(work, "local-only.txt")
    plan = pm.cleanup_plan(work)
    assert plan["master"] == "diverged"
    assert "DIVERGED" in pm.render_cleanup(plan)
    before = sh(work, "rev-parse", "master")
    pm.apply_plan(work, plan)
    assert sh(work, "rev-parse", "master") == before


def test_the_report_ends_with_a_copy_and_paste_powershell_block(world):
    _, work, _ = world
    block = pm.powershell_block(pm.cleanup_plan(work))
    assert "```powershell" in block and "git pull --ff-only origin master" in block
    assert "tools\\post_merge.py cleanup" in block and "Expected:" in block


def test_a_missing_base_is_an_error_not_a_crash(world):
    _, work, _ = world
    plan = pm.cleanup_plan(work, base="origin/nope", fetch=False)
    assert "error" in plan and "CLEANUP:" in pm.render_cleanup(plan)
    assert pm.apply_plan(work, plan)


# -- the next prompt ----------------------------------------------------------------------------------------------

GOOD = """/plan
x
STATE OF THE REPO 1. WHAT TO DO 2. HOW MUCH EFFORT 3. HOW TO VERIFY ADVISOR /advisor
PHASE 1: PLAN PHASE 2: BUILD PHASE 3: REPORT AND ARCHIVE
"""


def test_check_prompt_accepts_a_complete_prompt_and_names_every_defect():
    assert pm.check_prompt(GOOD) == []
    bad = pm.check_prompt(GOOD.replace("/plan", "plan", 1).replace("/advisor", "").replace("PHASE 3: REPORT AND ARCHIVE", "")
                          + " <<WRITE: something>> {{CHUNK_ID}} —")
    joined = " | ".join(bad)
    for needle in ("start with /plan", "PHASE 3", "/advisor", "unfilled <<WRITE", "{{CHUNK_ID}}", "em dash"):
        assert needle in joined


def test_the_template_has_no_em_dash_and_every_required_section():
    text = pm.TEMPLATE.read_text(encoding="utf-8")
    assert "—" not in text
    assert text.lstrip().startswith("/plan") and all(s in text for s in pm.REQUIRED_SECTIONS)


def test_the_live_tracker_renders_a_prompt_that_only_the_write_blocks_keep_open():
    g = pm.gather(REPO_ROOT)
    if not g["chunk"]:
        pytest.skip("no eligible chunk in the live tracker")
    text = pm.render_prompt(REPO_ROOT)
    assert g["chunk"] in text and f"git checkout -b dev/{g['chunk'].lower()}-" in text
    assert not re.findall(r"\{\{[A-Z_]+\}\}", text), "every mechanical field must be filled from the tracker"
    problems = pm.check_prompt(text)
    assert any("unfilled <<WRITE" in p for p in problems) and len(problems) == 1  # an unfinished prompt cannot pass
    filled = re.sub(r"<<WRITE:[^>]*>>", "filled", text)
    assert pm.check_prompt(filled) == []


def test_facts_and_peek_agree_on_the_next_chunk():
    peek = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "next_chunk.py"), "--peek"], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(REPO_ROOT))
    assert peek.returncode == 0
    first = peek.stdout.splitlines()[0].strip()
    facts = pm.render_facts(REPO_ROOT)
    assert facts.startswith("NEXT CHUNK (peek")
    assert first in facts


def test_the_cli_runs_check_prompt_with_an_exit_code(tmp_path: Path):
    f = tmp_path / "p.md"
    f.write_text(GOOD, encoding="utf-8")
    ok = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "post_merge.py"), "check-prompt", str(f)], capture_output=True, text=True)
    assert ok.returncode == 0 and "PROMPT=OK" in ok.stdout
    f.write_text(GOOD + "<<WRITE: todo>>", encoding="utf-8")
    bad = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "post_merge.py"), "check-prompt", str(f)], capture_output=True, text=True)
    assert bad.returncode == 1 and "PROMPT=FAIL" in bad.stdout
