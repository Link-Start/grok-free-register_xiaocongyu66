from deploy.ai_share_metadata import (
    DESCRIPTION,
    IMAGE_URL,
    SITE_URL,
    TITLE,
    patch_share_metadata,
)


EXPECTED_DESCRIPTION = (
    "AI 聚合平台，提供 OpenAI 兼容的 LLM、生图、视频生成等多类 API 服务。"
    "OpenAI-compatible AI API platform for LLMs, image, video, and more."
)


NEW_API_INDEX = """<!doctype html>
<html lang="zh">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/png" href="/logo.png" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />

    <!-- Primary Meta Tags -->
    <title>New API</title>
    <meta name="title" content="New API" />
    <meta
      name="description"
      content="Unified AI API gateway and admin dashboard."
    />

    <meta name="theme-color" content="#fff" />
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>
"""


def test_patch_replaces_new_api_share_metadata():
    patched = patch_share_metadata(NEW_API_INDEX)

    assert DESCRIPTION == EXPECTED_DESCRIPTION
    assert "New API" not in patched
    assert "Unified AI API gateway and admin dashboard." not in patched
    assert f"<title>{TITLE}</title>" in patched
    assert f'<meta name="title" content="{TITLE}" />' in patched
    assert f'<meta name="description" content="{DESCRIPTION}" />' in patched
    assert f'<meta property="og:url" content="{SITE_URL}" />' in patched
    assert f'<meta property="og:title" content="{TITLE}" />' in patched
    assert f'<meta property="og:description" content="{DESCRIPTION}" />' in patched
    assert f'<meta property="og:image" content="{IMAGE_URL}" />' in patched
    assert '<meta name="twitter:card" content="summary" />' in patched
    assert f'<meta name="twitter:url" content="{SITE_URL}" />' in patched


def test_patch_share_metadata_is_idempotent():
    once = patch_share_metadata(NEW_API_INDEX)
    twice = patch_share_metadata(once)

    assert twice == once


def test_patch_preserves_basic_head_tags_before_share_metadata():
    patched = patch_share_metadata(NEW_API_INDEX)

    assert patched.index('<meta charset="UTF-8" />') < patched.index(f"<title>{TITLE}</title>")
    assert patched.index('name="viewport"') < patched.index(f"<title>{TITLE}</title>")
