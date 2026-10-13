"""Ben's roadmap page (tools/roadmap.py): the repo's plain-English notes stay current, and the page is built from the tracker."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import roadmap  # noqa: E402

CHUNKS = """version: 3
chunks:
  - id: C0a
    title: Scaffold
    depends: []
    marker: c0a
    checks: [pytest -m c0a -q]
  - id: C1
    title: First queued item
    depends: [C0a]
    marker: c1
    band: 1
    size: M
    effort: low
    breakpoint: one commit
    impact: Band 1, thin evidence.
    checks: [pytest -m c1 -q]
  - id: C2
    title: Second queued item
    depends: [C1]
    marker: c2
    band: 2
    size: S
    effort: low
    breakpoint: one commit
    impact: Band 2.
    checks: [pytest -m c2 -q]
"""

STATUS = """# Build status

## Chunks

| Chunk | Status | Depends on | Started | Finished | Commit | Exit checks | Notes |
|---|---|---|---|---|---|---|---|
| C0a | DONE |  | 2026-09-26 | 2026-09-26 | abc1234 | PASS | first |
| C1 | TODO | C0a | | | | | |
| C2 | TODO | C1 | | | | | |

## Open [BEN] flags
"""

PLAIN = """version: 1
goals:
  foundation: {name: Foundation, blurb: Built first.}
  win: {name: Win big prizes, blurb: Prizes.}
  washout: {name: Avoid a washed-out night, blurb: Washouts.}
asks:
  - id: one
    chunks: [C2]
    question: Decide something?
    why: Because.
updates:
  - date: 2026-10-13
    text: Something changed.
chunks:
  C0a:
    title_seen: Scaffold
    name: The skeleton
    goal: foundation
    what: Set up.
    why: Needed.
    matters: Everything sits on it.
    result: Done Sept 26.
  C1:
    title_seen: First queued item
    name: First item
    what: A thing.
    why: A reason </script> with a tag.
    matters: Wins.
  C2:
    title_seen: Second queued item
    name: Second item
    what: Another thing.
    why: Another reason.
    matters: Fewer washouts.
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "docs" / "roadmap").mkdir(parents=True)
    (tmp_path / "chunks.yaml").write_text(CHUNKS, encoding="utf-8")
    (tmp_path / "BUILD_STATUS.md").write_text(STATUS, encoding="utf-8")
    (tmp_path / "docs" / "roadmap" / "plain.yaml").write_text(PLAIN, encoding="utf-8")
    shutil.copy(ROOT / "docs" / "roadmap" / "page.html", tmp_path / "docs" / "roadmap" / "page.html")
    return tmp_path


def _set_plain(tree: Path, old: str, new: str) -> None:
    p = tree / "docs" / "roadmap" / "plain.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_repo_notes_are_current():
    """Catches a chunk added, retitled or marked DONE without its plain-English entry (the page would go stale)."""
    problems = roadmap.check(ROOT)
    assert problems == [], "update docs/roadmap/plain.yaml (see docs/roadmap/README.md):\n" + "\n".join(problems)


def test_fixture_tree_is_clean(tree):
    assert roadmap.check(tree) == []


def test_missing_entry_is_reported(tree):
    _set_plain(tree, "  C2:\n    title_seen: Second queued item\n", "  C9:\n    title_seen: Second queued item\n")
    problems = roadmap.check(tree)
    assert any(p.startswith("C2: no entry") for p in problems)
    assert any(p.startswith("C9: entry has no chunk") for p in problems)


def test_retitled_chunk_is_reported_and_stamp_clears_it(tree):
    (tree / "chunks.yaml").write_text(CHUNKS.replace("title: First queued item", "title: First item, renamed"), encoding="utf-8")
    assert any(p.startswith("C1: title changed") for p in roadmap.check(tree))
    assert roadmap.stamp(tree, ["C1"]) == ["C1"]
    assert roadmap.check(tree) == []
    text = (tree / "docs" / "roadmap" / "plain.yaml").read_text(encoding="utf-8")
    assert 'title_seen: "First item, renamed"' in text and text.count("title_seen:") == 3


def test_done_without_result_and_em_dash_are_reported(tree):
    _set_plain(tree, "    result: Done Sept 26.\n", "")
    _set_plain(tree, "matters: Wins.", "matters: Wins — big.")
    problems = roadmap.check(tree)
    assert any(p.startswith("C0a: DONE in the tracker but has no result") for p in problems)
    assert any("C1.matters: contains an em dash" in p for p in problems)


def test_unbanded_chunk_needs_a_goal(tree):
    _set_plain(tree, "    goal: foundation\n", "")
    assert any(p.startswith("C0a: no band") for p in roadmap.check(tree))


def test_page_data_states_rank_and_escaping(tree):
    page = roadmap.render(roadmap.gather(tree), tree)
    blob = re.search(r'id="roadmap-data" type="application/json">(.*?)</script>', page, re.S).group(1)
    data = json.loads(blob)
    by = {c["id"]: c for c in data["chunks"]}
    assert data["next"] == "C1"
    assert (by["C0a"]["state"], by["C1"]["state"], by["C2"]["state"]) == ("done", "next", "queued")
    assert (by["C0a"]["rank"], by["C1"]["rank"], by["C2"]["rank"]) == (None, 1, 2)
    assert (by["C1"]["goal"], by["C2"]["goal"], by["C0a"]["goal"]) == ("win", "washout", "foundation")
    assert by["C2"]["asked"] is True and by["C0a"]["finished"] == "2026-09-26"
    assert "A reason </script> with a tag." == by["C1"]["why"]
    assert roadmap.DATA_MARK not in page
