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
                          + " <<WRITE: something>> {{CHUNK_ID}} " + chr(0x2014))
    joined = " | ".join(bad)
    for needle in ("start with /plan", "PHASE 3", "/advisor", "unfilled or broken <<WRITE", "{{CHUNK_ID}}", "em dash"):
        assert needle in joined


def test_the_template_has_no_em_dash_and_every_required_section():
    text = pm.TEMPLATE.read_text(encoding="utf-8")
    assert chr(0x2014) not in text
    assert text.lstrip().startswith("/plan") and all(s in text for s in pm.REQUIRED_SECTIONS)


def test_the_live_tracker_renders_a_prompt_that_only_the_write_blocks_keep_open():
    g = pm.gather(REPO_ROOT)
    if not g["chunk"]:
        pytest.skip("no eligible chunk in the live tracker")
    text = pm.render_prompt(REPO_ROOT)
    assert g["chunk"] in text and f"git checkout -b dev/{g['chunk'].lower()}-" in text
    assert not re.findall(r"\{\{[A-Z_]+\}\}", text), "every mechanical field must be filled from the tracker"
    problems = pm.check_prompt(text)
    assert any("unfilled or broken <<WRITE" in p for p in problems) and len(problems) == 1  # an unfinished prompt cannot pass
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


# -- the reviewer's cases ----------------------------------------------------------------------------------------

def test_a_finished_branch_whose_remote_is_gone_is_deleted_even_from_a_stale_branch(world):
    """git branch -d refuses this exact case (merged into the base, not into HEAD or the deleted upstream)."""
    _, work, _ = world
    sh(work, "switch", "-c", "stale-dev")  # a stale checkout at the old master tip
    sh(work, "switch", "-c", "feat")
    commit(work, "feat.txt")
    sh(work, "push", "-u", "origin", "feat")
    sh(work, "switch", "master")
    sh(work, "merge", "--no-ff", "feat", "-m", "Merge feat")
    sh(work, "push", "origin", "master")
    sh(work, "push", "origin", "--delete", "feat")  # GitHub deleting the head branch after the merge
    sh(work, "switch", "stale-dev")
    plan = pm.cleanup_plan(work)  # the fetch --prune turns feat's upstream into [gone]
    assert actions(plan)["feat"] == "delete"
    done = pm.apply_plan(work, plan)
    assert any(d.startswith("deleted local branch feat (was ") and "restore with: git branch feat " in d for d in done), done
    assert "feat" not in sh(work, "branch", "--format=%(refname:short)").split()


def test_the_ancestor_check_is_rerun_just_before_deleting(world):
    _, work, _ = world
    make_branches(work)
    plan = pm.cleanup_plan(work)
    assert actions(plan)["feat"] == "delete"
    sh(work, "switch", "feat")  # new work lands on the branch after the plan was made
    commit(work, "late.txt")
    sh(work, "switch", "master")
    done = pm.apply_plan(work, plan)
    assert any("NOT deleted local branch feat" in d and "no longer fully merged" in d for d in done)
    assert "feat" in sh(work, "branch", "--format=%(refname:short)").split()


def test_master_is_never_deleted_even_when_the_base_is_another_branch(world):
    origin, work, _ = world
    sh(work, "push", "origin", "master:develop")
    sh(work, "fetch", "origin")
    plan = pm.cleanup_plan(work, base="origin/develop", fetch=False)
    assert actions(plan).get("master") == "keep"
    pm.apply_plan(work, plan)
    assert "master" in sh(work, "branch", "--format=%(refname:short)").split()


def test_remote_branches_honor_keep_and_skip_a_branch_pushed_with_no_commits(world):
    _, work, _ = world
    make_branches(work)  # feat is merged and still on the remote
    sh(work, "push", "origin", "master:fresh-pr")  # pushed with no commits of its own: sits at the base tip
    sh(work, "fetch", "origin")
    assert pm.cleanup_plan(work, fetch=False)["remote_merged"] == ["feat"]
    assert pm.cleanup_plan(work, keep=("feat",), fetch=False)["remote_merged"] == []
    assert "confirm no open pull request" in pm.render_cleanup(pm.cleanup_plan(work, fetch=False))


def test_the_powershell_block_stops_on_changes_and_on_a_failed_switch(world):
    _, work, _ = world
    block = pm.powershell_block(pm.cleanup_plan(work))
    assert "if (git status --porcelain)" in block and "STOP: the files listed above" in block
    assert "$LASTEXITCODE -eq 0" in block and "STOP: could not switch to master" in block
    assert block.index("git switch master") < block.index("git pull --ff-only origin master")
    assert "{{" not in block and "}}" not in block  # the doubled braces were format escapes


def test_the_branch_name_avoids_a_name_that_already_exists(world):
    _, work, _ = world
    assert pm.branch_name(work, "C19", "Field stack mix by slate size and more") == "dev/c19-field-stack-mix-by-slate"
    sh(work, "branch", "dev/c19-field-stack-mix-by-slate")
    assert pm.branch_name(work, "C19", "Field stack mix by slate size and more") == "dev/c19-field-stack-mix-by-slate-2"
    sh(work, "push", "origin", "master:dev/c19-field-stack-mix-by-slate-2")  # a remote-only name counts too
    sh(work, "fetch", "origin")
    assert pm.branch_name(work, "C19", "Field stack mix by slate size and more") == "dev/c19-field-stack-mix-by-slate-3"


@pytest.mark.parametrize("junk", ["<<WRITE >>", "<<WRITE", "{{ NEXT_FLAG }}", "{{NEXT_FLAG2}}", "see >> here"])
def test_check_prompt_catches_malformed_markers_too(junk):
    assert pm.check_prompt(GOOD + junk)


# -- dev merge or lineup-run merge: only a dev merge gets the cleanup and the next prompt -------------------------------

def commit_path(cwd: Path, rel: str) -> None:
    p = cwd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rel, encoding="utf-8")
    sh(cwd, "add", rel)
    sh(cwd, "commit", "-m", rel)


def merge_pr(work: Path, branch: str, number: int, paths: list[str]) -> str:
    """Land `paths` on master through a GitHub-style merge commit; returns the merge commit's sha."""
    sh(work, "switch", "-c", branch)
    for rel in paths:
        commit_path(work, rel)
    sh(work, "push", "-u", "origin", branch)
    sh(work, "switch", "master")
    sh(work, "merge", "--no-ff", branch, "-m", f"Merge pull request #{number} from bleeski/{branch}")
    return sh(work, "rev-parse", "HEAD")


@pytest.mark.parametrize("branch,files,verdict", [
    ("dev/c19-x", ["data/entered/a.csv"], "DEV"),  # a dev/ branch is dev work whatever it changed
    ("claude/kind-einstein", ["CLAUDE.md"], "DEV"),  # a cloud dev session: the harness named the branch, the files give it away
    ("claude/kind-einstein", ["src/nhl_dfs/x.py", "data/entered/a.csv"], "DEV"),  # one file outside data/entered is enough
    ("run/classic-20261008-entered", ["data/entered/a.csv"], "RUN"),
    ("claude/kind-einstein", ["data/entered/a.csv", "data/entered/b.csv"], "RUN"),  # a cloud lineup run on a harness branch
    ("run/classic-20261008-entered", ["data/entered/a.csv", "tools/post_merge.py"], "DEV"),  # run/ plus a code change
    ("run/classic-20261008-entered", None, "RUN"),  # no file list: the branch name alone
    ("run/classic-20261008-entered", [], "RUN"),
    ("claude/kind-einstein", None, "DEV"),  # cannot tell: the full routine, as before
    (None, None, "DEV"),
    (None, [r"data\entered\a.csv"], "RUN"),  # Windows separators
    (None, ["data/entered_old/a.csv"], "DEV"),  # the folder, not a name that starts the same
    (None, ["data/identity/accepted.csv"], "DEV"),  # tracked, but a dev chunk's file
])
def test_classify_tells_a_dev_merge_from_a_lineup_run_merge(branch, files, verdict):
    got, reason = pm.classify(branch, files)
    assert got == verdict and reason


def test_merge_facts_reads_the_branch_and_the_files_of_one_merge_not_the_tip(world):
    _, work, _ = world
    run_sha = merge_pr(work, "claude/kind-x", 7, ["data/entered/slate.csv"])  # a cloud lineup run on a harness branch
    dev_sha = merge_pr(work, "claude/other", 8, ["tools/a.py", "BUILD_STATUS.md"])  # a later merge: the tip
    assert pm.merge_facts(work, run_sha) == ("claude/kind-x", ["data/entered/slate.csv"], None)
    branch, files, problem = pm.merge_facts(work, dev_sha)
    assert (branch, sorted(files), problem) == ("claude/other", ["BUILD_STATUS.md", "tools/a.py"], None)
    assert pm.classify(*pm.merge_facts(work, run_sha)[:2])[0] == "RUN"  # still RUN with a newer merge on top of it
    assert pm.classify(branch, files)[0] == "DEV"


def test_a_squash_commit_has_no_branch_name_and_the_files_decide(world):
    _, work, _ = world
    (work / "data" / "entered").mkdir(parents=True)
    (work / "data" / "entered" / "s.csv").write_text("x", encoding="utf-8")
    sh(work, "add", "data/entered/s.csv")
    sh(work, "commit", "-m", "Save entries (#9)")
    branch, files, problem = pm.merge_facts(work, "HEAD")
    assert (branch, files, problem) == (None, ["data/entered/s.csv"], None)
    assert pm.classify(branch, files)[0] == "RUN"


def test_the_classify_cli_prints_the_verdict_and_exits_zero(world):
    _, work, _ = world
    run_sha = merge_pr(work, "claude/kind-x", 7, ["data/entered/slate.csv"])
    dev_sha = merge_pr(work, "run/not-really", 8, ["src/x.py"])  # a run/ name cannot hide a code change
    cli = [sys.executable, str(REPO_ROOT / "tools" / "post_merge.py"), "--root", str(work), "classify"]
    run = subprocess.run([*cli, "--merge", run_sha], capture_output=True, text=True)
    dev = subprocess.run([*cli, "--merge", dev_sha], capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.startswith("POST_MERGE=RUN"), run.stdout + run.stderr
    assert dev.returncode == 0 and dev.stdout.startswith("POST_MERGE=DEV") and "src/x.py" in dev.stdout
    by_hand = subprocess.run([*cli, "--branch", "claude/z", "--file", "data/entered/a.csv"], capture_output=True, text=True)
    assert by_hand.stdout.startswith("POST_MERGE=RUN")


def test_an_unreadable_merge_falls_back_to_dev_and_says_why(world):
    _, work, _ = world
    cli = [sys.executable, str(REPO_ROOT / "tools" / "post_merge.py"), "--root", str(work), "classify", "--merge", "deadbeef"]
    r = subprocess.run(cli, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("POST_MERGE=DEV") and "could not read commit deadbeef" in r.stdout


def test_the_skill_and_claude_md_name_the_classify_check_and_the_skill_stays_model_invoked():
    skill = " ".join((REPO_ROOT / ".claude" / "skills" / "nhl-post-merge" / "SKILL.md").read_text(encoding="utf-8").split())
    claude = " ".join((REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    assert "post_merge.py classify --merge" in skill and "POST_MERGE=RUN" in skill and "DEV-session" in skill
    assert "Only after a dev session" in claude and "post_merge.py classify --merge" in claude and "run/<slate>-entered" in claude
    assert "disable-model-invocation" not in skill.split("---")[1]  # a merge event must still be able to start it
    assert "—" not in skill and "—" not in claude
