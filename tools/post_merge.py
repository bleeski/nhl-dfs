"""After a pull request merges: branch cleanup, and the facts and guard for the next dev-session prompt.

    python tools/post_merge.py cleanup [--apply] [--no-fetch] [--keep BRANCH]... [--base origin/master]
    python tools/post_merge.py facts
    python tools/post_merge.py prompt [--out FILE]
    python tools/post_merge.py check-prompt FILE

cleanup   Report by default (it only runs `git fetch --prune`). With --apply it deletes LOCAL branches that are fully
          merged into the base and were pushed (the ancestor check is re-run just before each delete, then `git branch -D`:
          `-d` compares with HEAD or a deleted upstream and so refuses exactly the finished branches; the old commit id is
          printed so a branch can be restored) and fast-forwards a local master that is strictly behind the base. It never touches the current branch, a --keep branch, a branch with commits the base
          lacks (including one whose upstream is gone and which may have been squash-merged: those are listed for review),
          a branch that tracks the base or was never pushed (a new dev branch looks merged: it is listed for review),
          a dirty working tree, or a diverged master. It never deletes a remote branch: merged remote branches are listed
          with the command for Ben to run. The report ends with a copy-and-paste PowerShell block for Ben's machine.
facts     What the next prompt needs, read from the tracker: the next chunk (the selector's own rule with `--peek`, so
          the DONE chunks' checks are NOT rerun), its metadata and card, the backlog rows, flags and deferred items that
          name it, what follows it in the queue, and the next free flag and backlog numbers.
prompt    docs/templates/next_session_prompt.md with every mechanical {{FIELD}} filled from the tracker. The <<WRITE: ...>>
          blocks stay: the session fills them from the repo's real state.
check-prompt  Exit 1 unless the prompt starts with /plan, has every required section, has no unfilled marker or
          placeholder, and has no em dash.

Standard library only. Nothing here writes to a tracked file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
TEMPLATE = ROOT / "docs" / "templates" / "next_session_prompt.md"
REQUIRED_SECTIONS = ("STATE OF THE REPO", "1. WHAT TO DO", "2. HOW MUCH EFFORT", "3. HOW TO VERIFY",
                     "ADVISOR", "PHASE 1: PLAN", "PHASE 2: BUILD", "PHASE 3: REPORT AND ARCHIVE")
EM_DASH = chr(0x2014)  # spelled out so that no source file contains one
PROTECTED = ("master", "main", "develop", "trunk")


# --- git ---------------------------------------------------------------------------------------

def git(cwd: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout if r.returncode == 0 else r.stdout + r.stderr).strip()


def is_ancestor(cwd: Path, a: str, b: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", a, b)[0] == 0


def default_base(cwd: Path) -> str:
    rc, out = git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return out if rc == 0 and out else "origin/master"


def cleanup_plan(cwd: Path, base: str | None = None, keep: tuple[str, ...] = (), fetch: bool = True) -> dict:
    """Everything the report and --apply need; reads git only (plus `fetch --prune` unless fetch is False)."""
    notes: list[str] = []
    if fetch:
        rc, out = git(cwd, "fetch", "--prune", "origin")
        if rc != 0:
            notes.append(f"git fetch --prune origin failed ({out.splitlines()[-1] if out else 'no output'}): "
                         "the remote view below may be stale")
    base = base or default_base(cwd)
    if git(cwd, "rev-parse", "--verify", "--quiet", base)[0] != 0:
        return {"error": f"base {base} does not exist here; run `git fetch origin` first", "notes": notes}
    base_branch = base.split("/", 1)[-1]
    _, current = git(cwd, "branch", "--show-current")
    dirty = bool(git(cwd, "status", "--porcelain")[1])
    plan: dict = {"base": base, "current": current, "dirty": dirty, "notes": notes, "branches": [], "remote_merged": [],
                  "master": None}

    fmt = "%(refname:short)|%(upstream:short)|%(upstream:track)"
    for line in git(cwd, "for-each-ref", f"--format={fmt}", "refs/heads")[1].splitlines():
        name, upstream, track = (line.split("|") + ["", ""])[:3]
        if name == base_branch:
            continue
        merged = is_ancestor(cwd, name, base)
        if name in PROTECTED or upstream.endswith("/HEAD"):
            action, why = "keep", "a long-lived branch: never deleted by this tool"
        elif name == current:
            action, why = "keep", "checked out"
        elif name in keep:
            action, why = "keep", "named with --keep (an open pull request or work in progress)"
        elif upstream == base:
            # a dev session starts with `git checkout -b dev/x origin/master`: it tracks the base, has no commits yet,
            # and so looks "merged". It is new work, not a finished branch.
            action, why = "keep", "tracks " + base + " (a new branch with nothing pushed yet)"
        elif merged and upstream:
            action, why = "delete", "fully merged into " + base + " (it had been pushed)"
        elif merged:
            action, why = "review", "merged but never pushed: new work or abandoned? if abandoned, `git branch -d " + name + "`"
        elif "gone" in track:
            action, why = "review", "its upstream is gone but it has commits " + base + " lacks (squash-merged?): `git log " + base + ".." + name + "`"
        elif "ahead" in track or not upstream:
            action, why = "keep", "has commits that exist only here" if not upstream else "has commits not pushed yet"
        else:
            action, why = "keep", "not merged into " + base
        plan["branches"].append({"name": name, "upstream": upstream, "track": track, "merged": merged,
                                 "action": action, "why": why})

    if git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{base_branch}")[0] == 0:
        if is_ancestor(cwd, base_branch, base) and git(cwd, "rev-parse", base_branch)[1] == git(cwd, "rev-parse", base)[1]:
            plan["master"] = "in sync"
        elif is_ancestor(cwd, base_branch, base):
            plan["master"] = "behind"
        elif is_ancestor(cwd, base, base_branch):
            plan["master"] = "ahead"
        else:
            plan["master"] = "diverged"
    else:
        plan["master"] = "absent"
    plan["master_name"] = base_branch

    _, remote = git(cwd, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    base_sha = git(cwd, "rev-parse", base)[1]
    for r in remote.splitlines():
        short = r.split("/", 1)[1] if "/" in r else r
        if r in (base, "origin/HEAD", "origin") or short in PROTECTED or short == base_branch or short in keep:
            continue
        # a branch pushed with no commits of its own sits AT the base tip: a pull request about to open, not a merged one
        if is_ancestor(cwd, r, base) and git(cwd, "rev-parse", r)[1] != base_sha:
            plan["remote_merged"].append(short)
    return plan


def apply_plan(cwd: Path, plan: dict) -> list[str]:
    """Run the safe parts of the plan; returns what was done or skipped, one line each."""
    done: list[str] = []
    if "error" in plan:
        return [plan["error"]]
    for b in plan["branches"]:
        if b["action"] != "delete":
            continue
        name = b["name"]
        if not is_ancestor(cwd, name, plan["base"]):  # the real guard, re-run now: the branch may have moved since the plan
            done.append(f"NOT deleted local branch {name}: it is no longer fully merged into {plan['base']}")
            continue
        sha = git(cwd, "rev-parse", name)[1]
        rc, out = git(cwd, "branch", "-D", name)  # safe only because of the check above; -d would refuse a finished branch
        done.append(f"deleted local branch {name} (was {sha[:10]}; restore with: git branch {name} {sha[:10]})" if rc == 0
                    else f"NOT deleted local branch {name} ({out})")
    name = plan["master_name"]
    if plan["master"] == "behind":
        if plan["dirty"] and plan["current"] == name:
            done.append(f"local {name} is behind but the working tree has changes: not fast-forwarded")
        elif plan["current"] == name:
            rc, out = git(cwd, "merge", "--ff-only", plan["base"])
            done.append(f"{'fast-forwarded' if rc == 0 else 'NOT fast-forwarded'} local {name} to {plan['base']}" + ("" if rc == 0 else f" ({out})"))
        else:
            rc, out = git(cwd, "fetch", "origin", f"{name}:{name}")  # refuses a non-fast-forward
            done.append(f"{'fast-forwarded' if rc == 0 else 'NOT fast-forwarded'} local {name} to {plan['base']}" + ("" if rc == 0 else f" ({out})"))
    return done or ["nothing to apply"]


POWERSHELL = """git fetch --prune origin
if (git status --porcelain) {{
  git status --short
  Write-Host "STOP: the files listed above have uncommitted changes. Do not go on; send this output to Claude."
}} else {{
  git switch {master}
  if ($LASTEXITCODE -eq 0) {{
    git pull --ff-only origin {master}
    .venv\\Scripts\\python.exe tools\\post_merge.py cleanup
  }} else {{
    Write-Host "STOP: could not switch to {master}. Send this output to Claude."
  }}
}}
"""


def render_cleanup(plan: dict) -> str:
    if "error" in plan:
        return "CLEANUP: " + plan["error"]
    out = [f"CLEANUP against {plan['base']} (current branch: {plan['current'] or 'detached'}; working tree "
           f"{'HAS CHANGES' if plan['dirty'] else 'clean'})"]
    out += [f"note: {n}" for n in plan["notes"]]
    m = plan["master"]
    out.append(f"local {plan['master_name']}: " + {
        "in sync": "in sync with the base", "behind": "behind the base and fast-forwardable (safe)",
        "ahead": "AHEAD of the base: it holds commits GitHub lacks; left alone",
        "diverged": "DIVERGED from the base: left alone, decide by hand", "absent": "no local branch"}[m])
    for b in plan["branches"]:
        out.append(f"  {b['action'].upper():7s} {b['name']}: {b['why']}")
    if not plan["branches"]:
        out.append("  no other local branches")
    if plan["remote_merged"]:
        out.append("merged remote branches still on GitHub (GitHub normally deletes these itself; this tool never does). "
                   "Before running any line, confirm no open pull request uses that branch (deleting it would close the pull request):")
        out += [f"  git push origin --delete {r}" for r in plan["remote_merged"]]
    deletes = [b["name"] for b in plan["branches"] if b["action"] == "delete"]
    todo = deletes or m == "behind" or plan["remote_merged"]
    out.append("IN SYNC: nothing to do." if not todo and not any(b["action"] == "review" for b in plan["branches"])
               else "STATUS: cleanup available; run with --apply for the local parts.")
    return "\n".join(out)


def powershell_block(plan: dict) -> str:
    name = plan.get("master_name", "master")
    return ("COPY AND PASTE INTO POWERSHELL on your machine, in the nhl-dfs folder (it only fetches, fast-forwards master "
            "and prints a report; the last line deletes nothing):\n```powershell\n" + POWERSHELL.format(master=name) + "```\n"
            "Expected: no STOP line, the pull says Fast-forward or Already up to date, and the report names what it would "
            "delete. If a STOP line appears, nothing was changed: send the output to Claude. If the last line says it "
            "cannot find post_merge.py, the pull did not work: send the output to Claude. To delete the merged local "
            "branches the report lists, run its last line again with `--apply` added.")


# --- the next prompt -----------------------------------------------------------------------------

def _next_chunk():
    sys.path.insert(0, str(TOOLS))
    import next_chunk  # noqa: PLC0415

    return next_chunk


def card_text(root: Path, chunk_id: str) -> str:
    text = (root / "BUILD_CHUNKS.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(rf"^### {re.escape(chunk_id)} [·-].*?(?=^### |\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else f"(no card for {chunk_id} in BUILD_CHUNKS.md)"


def gather(root: Path) -> dict:
    """The tracker facts the next prompt needs. The chunk is the selector's own pick with --peek."""
    nc = _next_chunk()
    lines = nc.load_tracker_lines(root / "BUILD_STATUS.md")
    rows = nc.parse_rows(lines)
    flags = nc.parse_flags(lines)
    data = nc.load_data(root / "chunks.yaml")
    graph = {c["id"]: c for c in data["chunks"]}
    said = nc.find_next(graph, rows, root, verify=False)
    first = said.splitlines()[0].strip() if said else ""
    chunk_id = first if first in graph else None
    out: dict = {"selector": said, "chunk": chunk_id, "flags": flags,
                 "next_flag": max(flags, default=0) + 1, "data": data}
    backlog = (root / "BACKLOG.md").read_text(encoding="utf-8")
    ids = [int(x) for x in re.findall(r"^\| B(\d+) ", backlog, re.M)]
    out["next_backlog"] = max(ids, default=0) + 1
    if not chunk_id:
        return out
    c = graph[chunk_id]
    # the Queue table's own rank: chunks.yaml order among chunks that carry a band and are not DONE or GATED
    queue = [x["id"] for x in data["chunks"] if "band" in x and rows.get(x["id"]) and rows[x["id"]].status not in ("DONE", "GATED")]
    rank = queue.index(chunk_id) + 1 if chunk_id in queue else None
    out |= {"meta": c, "status": rows[chunk_id].status, "notes": rows[chunk_id].cells[7].strip(),
            "card": card_text(root, chunk_id), "rank": rank,
            "after": [f"{x} ({rows[x].status})" for x in (queue[rank:] if rank else [])][:3],
            "backlog_rows": {}, "named_flags": {}, "deferred": []}
    for b in c.get("backlog") or []:
        m = re.search(rf"^\| {b} \|(.*)$", backlog, re.M)
        out["backlog_rows"][b] = (m.group(1).split("|")[1].strip() if m else "(no row)")[:260]
    for n, f in flags.items():
        blob = " ".join(str(v) for v in f.values())
        if re.search(rf"\b{chunk_id}\b", blob):
            out["named_flags"][n] = blob[:200]
    for d in data.get("deferred") or []:
        if d.get("chunk") == chunk_id or set(d.get("backlog") or []) & set(c.get("backlog") or []):
            out["deferred"].append(d.get("item") or d.get("chunk"))
    return out


def slug(title: str) -> str:
    """The first five words of the title, for a branch name: dev/c19-field-stack-mix-by.."""
    return "-".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split()[:5])


def branch_name(root: Path, chunk_id: str, title: str) -> str:
    """dev/<id>-<five words of the title>, with -2, -3 ... when that name already exists here or on origin (a resume)."""
    base = f"dev/{chunk_id.lower()}-{slug(title)}"
    name, n = base, 1
    while (git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0
           or git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}")[0] == 0):
        n += 1
        name = f"{base}-{n}"
    return name


def render_facts(root: Path) -> str:
    g = gather(root)
    rc, head = git(root, "log", "origin/master", "--oneline", "-3")
    out = [f"NEXT CHUNK (peek: the DONE chunks' checks were NOT rerun; the session still runs the real selector):",
           g["selector"], ""]
    if not g["chunk"]:
        return "\n".join(out + [f"next free flag {g['next_flag']}, next free backlog B{g['next_backlog']}"])
    c = g["meta"]
    out += [f"{g['chunk']} · {c['title']}", f"status {g['status']}; band {c.get('band')}; size {c.get('size')}; effort: {c.get('effort')}",
            f"depends on {c.get('depends')}; marker {c.get('marker')}; needs flags {c.get('needs') or 'none'}",
            f"breakpoint: {c.get('breakpoint')}", "exit checks: " + "; ".join(c.get("checks") or []),
            f"branch name for the new session: {branch_name(root, g['chunk'], c['title'])}",
            f"next in the queue after it: {', '.join(g['after']) or 'nothing'}",
            f"next free flag {g['next_flag']}, next free backlog B{g['next_backlog']}",
            f"tracker notes (HANDOFF if any): {g['notes'] or 'none'}",
            "backlog rows it carries: " + ("; ".join(f"{k}: {v}" for k, v in g["backlog_rows"].items()) or "none"),
            "flags that name it: " + ("; ".join(f"{k}: {v}" for k, v in g["named_flags"].items()) or "none"),
            "deferred items that name it: " + ("; ".join(g["deferred"]) or "none"),
            "origin/master head: " + (head.replace("\n", " / ") if rc == 0 else "(not fetched)"), "",
            "THE CARD (BUILD_CHUNKS.md):", g["card"]]
    return "\n".join(out)


def render_prompt(root: Path, template: Path = TEMPLATE) -> str:
    g = gather(root)
    if not g["chunk"]:
        raise SystemExit("no eligible chunk to write a prompt for:\n" + g["selector"])
    c = g["meta"]
    fields = {
        "CHUNK_ID": g["chunk"], "CHUNK_TITLE": c["title"], "BAND": str(c.get("band")), "SIZE": str(c.get("size")),
        "EFFORT_CARD": str(c.get("effort")), "DEPENDS": ", ".join(c.get("depends") or []) or "nothing",
        "BACKLOG": ", ".join(c.get("backlog") or []) or "none", "MARKER": str(c.get("marker")),
        "BREAKPOINT": str(c.get("breakpoint")), "STATUS": g["status"], "RANK": f"Queue #{g['rank']}" if g["rank"] else "not ranked",
        "EXIT_CHECKS": "; ".join(f"`{x}`" for x in c.get("checks") or []),
        "BRANCH": branch_name(root, g["chunk"], c["title"]), "NEXT_FLAG": str(g["next_flag"]),
        "NEXT_BACKLOG": f"B{g['next_backlog']}", "AFTER": ", ".join(g["after"]) or "nothing",
    }
    text = template.read_text(encoding="utf-8")
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: fields.get(m.group(1), m.group(0)), text)


def check_prompt(text: str) -> list[str]:
    problems: list[str] = []
    if not text.lstrip().startswith("/plan"):
        problems.append("the prompt must start with /plan")
    for s in REQUIRED_SECTIONS:
        if s not in text:
            problems.append(f"missing section: {s}")
    if "/advisor" not in text:
        problems.append("the prompt must tell the session to use /advisor")
    left = re.findall(r"<<[^\n]{0,80}", text)
    if left:
        problems.append(f"{len(left)} unfilled or broken <<WRITE: ...>> block(s), first: {left[0]}")
    ph = re.findall(r"\{\{[^\n]{0,40}", text)
    if ph:
        problems.append(f"unfilled or broken placeholder(s), first: {ph[0]}")
    if ">>" in text and not left:
        problems.append("a stray >> (reword it)")
    if EM_DASH in text:
        problems.append("the prompt contains an em dash (the project style forbids them)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--apply", action="store_true")
    c.add_argument("--no-fetch", action="store_true")
    c.add_argument("--keep", action="append", default=[])
    c.add_argument("--base", default=None)
    sub.add_parser("facts")
    p = sub.add_parser("prompt")
    p.add_argument("--out", type=Path, default=None)
    k = sub.add_parser("check-prompt")
    k.add_argument("file", type=Path)
    a = ap.parse_args(argv)
    root = a.root.resolve()

    if a.cmd == "cleanup":
        plan = cleanup_plan(root, a.base, tuple(a.keep), fetch=not a.no_fetch)
        print(render_cleanup(plan))
        if a.apply and "error" not in plan:
            print("\nAPPLIED:")
            print("\n".join("  " + x for x in apply_plan(root, plan)))
            print("\n" + render_cleanup(cleanup_plan(root, a.base, tuple(a.keep), fetch=False)))
        print("\n" + powershell_block(plan))
        return 0 if "error" not in plan else 1
    if a.cmd == "facts":
        print(render_facts(root))
        return 0
    if a.cmd == "prompt":
        text = render_prompt(root)
        if a.out:
            a.out.write_text(text, encoding="utf-8")
            print(f"wrote {a.out}; fill every <<WRITE: ...>> block, then run check-prompt on it")
        else:
            print(text)
        return 0
    problems = check_prompt(a.file.read_text(encoding="utf-8"))
    for pr in problems:
        print(f"PROMPT: {pr}")
    print("PROMPT=OK" if not problems else f"PROMPT=FAIL ({len(problems)} problem(s))")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
