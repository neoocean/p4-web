#!/usr/bin/env python3
"""Flag plain-form (해라체) sentences on the Korean site pages.

Every page under docs/ko/ addresses the reader in polite 합쇼체
("…합니다"). New copy is where that slips: a paragraph appended to an
existing section, or one card in a grid, ends up in plain form and
nobody notices because the surrounding page reads fine.

Matching the plain endings directly rather than "anything that is not
polite" keeps the false positives down: headings and captions here are
noun phrases ("쓰기 작업", "읽음/안 읽음"), and a sentence wrapped across
source lines leaves fragments that no ending test can classify. An
ending only counts when whitespace, punctuation, or a tag follows it.

Run from the repo root; exits non-zero if anything is flagged.
"""
import html
import pathlib
import re
import sys

PAGES = ["docs/ko/index.html", "docs/ko/changes.html", "docs/ko/guide.html"]

PLAIN_ENDINGS = (
    "는다|한다|된다|이다|았다|었다|온다|간다|린다|긴다|뜬다|난다|준다|본다|없다|있다"
)
# A trailing space, sentence punctuation, quote, dash, or tag boundary —
# without this, "다음" and "읽음" match as often as real sentences do.
PLAIN = re.compile("(" + PLAIN_ENDINGS + ")(?=[\\s.,)”\"<—]|$)")


def audit(path):
    src = pathlib.Path(path).read_text()
    src = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", src)
    hits = []
    for lineno, line in enumerate(src.split("\n"), 1):
        text = html.unescape(re.sub(r"<[^>]+>", " ", line))
        for m in PLAIN.finditer(text):
            hits.append((lineno, text[max(0, m.start() - 45):m.end() + 3].strip()))
    return hits


def main():
    total = 0
    for page in PAGES:
        hits = audit(page)
        total += len(hits)
        print(f"{page}: {len(hits)} plain-form hit(s)")
        for lineno, context in hits:
            print(f"   line {lineno}: …{context}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
