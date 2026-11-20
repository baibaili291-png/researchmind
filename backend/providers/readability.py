"""Readability-style main-content extraction using only the standard library.

Two-stage approach:
1. Paragraph scoring — drop nav/script/footer chrome, keep blocks that carry
   prose density (length + Chinese punctuation).
2. Density window — select the longest run of consecutive substantive
   paragraphs, which discards "related articles" and comment tails that
   otherwise leak into the body.

This matters because a failed extraction silently produces junk evidence
downstream: on a test against a NetEase article the naive extractor yielded
740 chars of sports headlines, and a fact extractor happily promoted those
into "facts". Quality gating is therefore part of extraction, not optional.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterable

SKIP_TAGS = {
    "script", "style", "noscript", "svg", "nav", "header", "footer",
    "form", "button", "select", "textarea", "aside", "iframe", "audio",
    "video", "template",
}
BLOCK_TAGS = {"p", "div", "section", "article", "td", "blockquote", "h2", "h3", "h4"}

# Chrome/boilerplate markers. A block dominated by these is navigation or legal
# footer text, not article body.
_CHROME_RE = re.compile(
    r"(首页|登录|注册|下载APP|下载客户端|客服|版权所有|备案|ICP|"
    r"关于我们|联系我们|免责声明|特别声明|举报|扫码关注|返回顶部|"
    r"Notice:|Copyright|All Rights Reserved|Cookie|Privacy Policy)"
)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
# Related-article tails: short headline-like lines with no sentence ending.
_HEADLINE_RE = re.compile(r"[^。！？；]{6,60}$")


class _BlockExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.title: str = ""
        self._skip_depth = 0
        self._buf: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            t = data.strip()
            if t:
                self.title = t
        if self._skip_depth == 0 and data.strip():
            self._buf.append(data)

    def _flush(self) -> None:
        text = _WS_RE.sub(" ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            self.blocks.append(text)


def _score(text: str) -> int:
    """Prose density score: length plus Chinese punctuation weight."""
    punct = sum(text.count(c) for c in "，。；、：！？")
    return len(text) + punct * 8


def _is_chrome(text: str) -> bool:
    if _CHROME_RE.search(text) and len(text) < 160:
        return True
    # Long link-farm / menu block: many short segments separated by delimiters
    if len(text) < 120 and text.count(" ") > len(text) // 6:
        return True
    return False


def _density_window(blocks: list[str]) -> tuple[int, int]:
    """Return (start, end) of the longest run of substantive blocks."""
    best = (0, -1)
    cur_start, cur_end = 0, -1
    for i, b in enumerate(blocks):
        substantive = len(b) >= 50 and not _is_chrome(b)
        if substantive:
            if cur_end < 0:
                cur_start = i
            cur_end = i
            if cur_end - cur_start > best[1] - best[0]:
                best = (cur_start, cur_end)
        else:
            cur_start, cur_end = 0, -1
    return best


def extract_main(html: str, min_chars: int = 120) -> tuple[str, str]:
    """Return (title, body_text). Body is '' when quality gating rejects it."""
    parser = _BlockExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return "", ""

    blocks = [_TAG_RE.sub(" ", b).strip() for b in parser.blocks]
    blocks = [b for b in blocks if b]
    if not blocks:
        return parser.title, ""

    start, end = _density_window(blocks)
    selected: Iterable[str] = blocks[start : end + 1] if end >= start else blocks[:12]
    body_parts: list[str] = []
    for b in selected:
        if len(b) < 30 or _is_chrome(b):
            continue
        body_parts.append(b)

    body = "\n\n".join(body_parts)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) < min_chars:
        return parser.title, ""
    return parser.title, body


def looks_relevant(body: str, keywords: list[str], min_hits: int = 2) -> bool:
    """Reject bodies that do not actually discuss the queried topic.

    Guard against the observed failure mode where a paywalled/JS page yields a
    body made of unrelated headlines that still passes the length gate.
    """
    if not body or not keywords:
        return False
    hits = sum(1 for k in keywords if k and k.lower() in body.lower())
    return hits >= min_hits
