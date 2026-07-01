from __future__ import annotations

import argparse
import re
from html import escape
from pathlib import Path


SITE_URL = "https://ai.rtoc.cc/"
IMAGE_URL = "https://ai.rtoc.cc/logo.png"
TITLE = "旋转智能 | Radius to Core"
DESCRIPTION = (
    "AI 聚合平台，提供 OpenAI 兼容的 LLM、生图、视频生成等多类 API 服务。"
    "OpenAI-compatible AI API platform for LLMs, image, video, and more."
)

_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"\n?[ \t]*<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(r"\n?[ \t]*<meta\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""\b(name|property)\s*=\s*(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_PRIMARY_META_MARKER_RE = re.compile(r"<!--\s*Primary Meta Tags\s*-->", re.IGNORECASE)


def _meta(name: str, content: str, *, attr: str = "name") -> str:
    return f'    <meta {attr}="{escape(name)}" content="{escape(content)}" />'


def _share_metadata_block() -> str:
    return "\n".join(
        [
            f"    <title>{escape(TITLE)}</title>",
            _meta("title", TITLE),
            _meta("description", DESCRIPTION),
            _meta("og:type", "website", attr="property"),
            _meta("og:url", SITE_URL, attr="property"),
            _meta("og:title", TITLE, attr="property"),
            _meta("og:description", DESCRIPTION, attr="property"),
            _meta("og:image", IMAGE_URL, attr="property"),
            _meta("twitter:card", "summary"),
            _meta("twitter:url", SITE_URL),
            _meta("twitter:title", TITLE),
            _meta("twitter:description", DESCRIPTION),
            _meta("twitter:image", IMAGE_URL),
        ]
    )


def _is_share_meta(tag: str) -> bool:
    match = _ATTR_RE.search(tag)
    if not match:
        return False

    value = " ".join(match.group(3).lower().split())
    return value in {"title", "description"} or value.startswith(("og:", "twitter:"))


def _strip_share_metadata(head_content: str) -> str:
    head_content = _TITLE_RE.sub("\n", head_content)
    return _META_RE.sub(lambda match: "\n" if _is_share_meta(match.group(0)) else match.group(0), head_content)


def patch_share_metadata(html: str) -> str:
    head_open = _HEAD_OPEN_RE.search(html)
    if not head_open:
        raise ValueError("HTML does not contain a <head> tag")

    head_close = _HEAD_CLOSE_RE.search(html, head_open.end())
    if not head_close:
        raise ValueError("HTML does not contain a closing </head> tag")

    before_head_content = html[: head_open.end()]
    head_content = html[head_open.end() : head_close.start()]
    after_head_content = html[head_close.start() :]

    remaining_head = _strip_share_metadata(head_content)
    marker = _PRIMARY_META_MARKER_RE.search(remaining_head)
    if marker:
        patched_head = (
            f"{remaining_head[: marker.end()].rstrip()}\n"
            f"{_share_metadata_block()}\n"
            f"{remaining_head[marker.end() :].lstrip('\n')}"
        )
        return f"{before_head_content}{patched_head}{after_head_content}"

    remaining_head = remaining_head.lstrip()
    if remaining_head:
        return f"{before_head_content}\n{_share_metadata_block()}\n{remaining_head}{after_head_content}"
    return f"{before_head_content}\n{_share_metadata_block()}\n{after_head_content}"


def patch_file(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    path.write_text(patch_share_metadata(html), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch ai.rtoc.cc share metadata in a built index.html file.")
    parser.add_argument("index_html", type=Path, help="Path to the local built index.html")
    args = parser.parse_args(argv)
    patch_file(args.index_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
