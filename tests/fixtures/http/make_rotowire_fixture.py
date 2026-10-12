"""Trim a recorded RotoWire hockey news page into a test fixture (C54, flags 91 and 93).

    python tests/fixtures/http/make_rotowire_fixture.py <raw body> <out file> <news id,news id,...>

The raw body is the engine's own recording (data/sources/rotowire.fetch_news into a scratch cache root; the body
HttpCache stores before the schema check). The fixture is the raw bytes with whole byte ranges deleted, nothing
else: the kept ranges are the text before the <head> (doctype and the <html> start tag), the <main class="page
news"> start tag, the whole filter-links-group element (the page's own team filter, the evidence for the team
codes), each kept news item as a whole <div class="news-update"> element, the </main> end tag and the </body> and
</html> end tags. Nothing is re-serialized, re-encoded or re-indented, so line endings and bytes are RotoWire's.
The script checks that every kept range is a slice of the raw body, in order and without overlap, and prints the
ranges, the kept items and the byte counts for tests/fixtures/http/SOURCES.md. The raw body is never committed.
Keep the fewest items the tests need: the repository is public (flag 93).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_DIV = re.compile(rb"<div\b|</div\s*>")
_MAIN = re.compile(rb"<main\b|</main\s*>")


def _element(raw: bytes, start: int, tags: re.Pattern) -> int:
    """The end offset of the balanced element whose start tag begins at `start`."""
    depth = 0
    for m in tags.finditer(raw, start):
        depth += -1 if m.group(0).startswith(b"</") else 1
        if depth == 0:
            return m.end()
    raise SystemExit(f"unbalanced element at byte {start}")


def _start_tag(raw: bytes, needle: bytes, frm: int = 0) -> int:
    i = raw.find(needle, frm)
    if i < 0:
        raise SystemExit(f"not found: {needle!r}")
    return raw.rfind(b"<", 0, i)


def ranges(raw: bytes, keep_ids: list[int]) -> tuple[list[tuple[int, int]], list[int]]:
    head = raw.find(b"<head>")
    out = [(0, head)]
    m = _start_tag(raw, b'class="page news"')
    out.append((m, raw.index(b">", m) + 1))
    f = _start_tag(raw, b'class="filter-links-group"', m)
    out.append((f, _element(raw, f, _DIV)))
    kept = []
    for it in re.finditer(rb'<div class="news-update(?: [^"_]*)?">', raw):  # injury items add "is-injured"
        end = _element(raw, it.start(), _DIV)
        nid = re.search(rb'news-update__headline" href="[^"]*-(\d+)"', raw[it.start():end])
        if nid and int(nid.group(1)) in keep_ids:
            out.append((it.start(), end))
            kept.append(int(nid.group(1)))
    main_end = _element(raw, m, _MAIN)
    out.append((main_end - len(b"</main>"), main_end))
    body = raw.rfind(b"</body>")
    out.append((body, len(raw)))
    return out, kept


def main(argv: list[str]) -> int:
    raw = Path(argv[1]).read_bytes()
    keep_ids = [int(x) for x in argv[3].split(",") if x]
    rs, kept = ranges(raw, keep_ids)
    missing = sorted(set(keep_ids) - set(kept))
    if missing:
        raise SystemExit(f"news ids not on the page: {missing}")
    last = 0
    for a, b in rs:
        assert last <= a < b <= len(raw), (last, a, b)
        last = b
    body = b"".join(raw[a:b] for a, b in rs)
    Path(argv[2]).write_bytes(body)
    print(f"raw {len(raw)} bytes; kept {len(body)} bytes in {len(rs)} ranges; items {len(kept)}: {kept}")
    print("ranges", rs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
