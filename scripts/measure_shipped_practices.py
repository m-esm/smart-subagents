#!/usr/bin/env python3
"""Print count of shipped, sourced practice rows. One number. Exit 0. Stdlib only."""
import pathlib
import re
import sys

ROOT = pathlib.Path("design/roadmap")
URL = re.compile(r"https?://[^\s)\]>'\"`]+")
SKIP = {"post.md"}


def sourced(path: pathlib.Path, body: str) -> bool:
    if URL.search(body):
        return True
    m = re.search(r"^evidence:\s*\n((?:[ \t]+- .+\n)+)", body, re.M)
    if not m:
        return False
    for line in m.group(1).splitlines():
        rel = line.strip().lstrip("- ").strip()
        ev = pathlib.Path(rel)
        if ev.is_file() and URL.search(ev.read_text(errors="replace")):
            return True
    return False


def main() -> None:
    n = 0
    if ROOT.is_dir():
        for p in sorted(ROOT.glob("*.md")):
            if p.name in SKIP:
                continue
            text = p.read_text(errors="replace")
            fm = text.split("---", 2)
            if len(fm) < 3:
                continue
            if not re.search(r"^state:\s*shipped\s*$", fm[1], re.M):
                continue
            if sourced(p, text):
                n += 1
    print(n)
    sys.exit(0)


if __name__ == "__main__":
    main()
