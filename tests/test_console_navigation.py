from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_open_platform_nav_exposes_authenticated_online_debugger():
    html = (ROOT / "app" / "static" / "console.html").read_text(encoding="utf-8")
    nav = html.split('<nav class="app-nav" aria-label="主导航">', 1)[1].split("</nav>", 1)[0]
    assert re.search(
        r'<a\b[^>]*href="/api/debug"[^>]*>\s*在线调试\s*</a>',
        nav,
    ), "开放平台主导航必须提供 /api/debug 在线调试入口"
