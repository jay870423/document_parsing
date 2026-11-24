from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC_DIR = Path(__file__).parents[1] / "app" / "static"


def _inline_scripts(html: str) -> str:
    return "\n".join(
        re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    )


def _media_block(html: str, query: str) -> str:
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    marker = f"@media ({query})"
    start = css.index(marker)
    brace = css.index("{", start + len(marker))
    depth = 0
    for index in range(brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[brace + 1 : index]
    raise AssertionError(f"Unclosed media query: {query}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for JS syntax checks")
@pytest.mark.parametrize("filename", ["index.html", "console.html"])
def test_inline_javascript_is_syntactically_valid(filename, tmp_path):
    script = tmp_path / f"{filename}.js"
    script.write_text(_inline_scripts((STATIC_DIR / filename).read_text()), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(script)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_workbench_mobile_prioritizes_task_and_has_touch_targets():
    html = (STATIC_DIR / "index.html").read_text()
    mobile = _media_block(html, "max-width: 900px")

    # The main task follows the compact tab bar; account/knowledge-base metadata
    # remains available below it instead of pushing upload below the first screen.
    assert re.search(r"\.workspace-side\s*\{[^}]*display:\s*contents", mobile, re.S)
    assert re.search(r"\.workspace-main\s*\{[^}]*order:\s*1", mobile, re.S)
    assert re.search(r"\.kb-strip\s*\{[^}]*order:\s*2", mobile, re.S)
    assert re.search(r"\.doc-panel\s*\{[^}]*order:\s*3", mobile, re.S)
    assert re.search(r"\.tabs\s*\{[^}]*repeat\(auto-fit,\s*minmax\(108px,\s*1fr\)\)", mobile, re.S)

    # 16px inputs prevent iOS focus zoom; primary targets are at least 44px.
    assert re.search(r"input\[type=\"text\"\][^{]*\{[^}]*font-size:\s*16px", mobile, re.S)
    assert re.search(r"input\[type=\"file\"\]\s*\{[^}]*min-height:\s*48px", mobile, re.S)
    assert re.search(r"\.btn\s*\{[^}]*min-height:\s*48px", mobile, re.S)
    assert re.search(r"\.nav-link\s*\{[^}]*min-height:\s*44px", mobile, re.S)
    assert re.search(r"\.check\s*\{[^}]*min-height:\s*44px", mobile, re.S)
    assert html.count('class="row mobile-action-row"') == 2
    assert ".mobile-action-row" in mobile
    assert '$("fileInput").click();' in html
    assert "scrollIntoView({" in html


def test_workbench_stacked_layout_keeps_task_first_and_controls_touchable():
    html = (STATIC_DIR / "index.html").read_text()
    stacked = _media_block(html, "max-width: 1160px")

    assert re.search(r"\.workspace-side\s*\{[^}]*display:\s*contents", stacked, re.S)
    assert re.search(r"\.workspace-main\s*\{[^}]*order:\s*1", stacked, re.S)
    assert re.search(r"\.kb-strip\s*\{[^}]*order:\s*2", stacked, re.S)
    assert re.search(r"\.doc-panel\s*\{[^}]*order:\s*3", stacked, re.S)
    assert re.search(r"\.tabs\s*\{[^}]*order:\s*0", stacked, re.S)
    assert re.search(r"\.btn[^{}]*\{[^}]*min-height:\s*44px", stacked, re.S)
    assert re.search(r"input\[type=\"file\"\]\s*\{[^}]*min-height:\s*44px", stacked, re.S)


def test_console_mobile_controls_and_modals_are_touch_friendly():
    html = (STATIC_DIR / "console.html").read_text()
    mobile = _media_block(html, "max-width: 900px")

    assert re.search(r"input,\s*select\s*\{[^}]*font-size:\s*16px", mobile, re.S)
    assert re.search(r"input,\s*select\s*\{[^}]*min-height:\s*48px", mobile, re.S)
    assert re.search(r"\.password-toggle\s*\{[^}]*width:\s*44px[^}]*height:\s*44px", mobile, re.S)
    assert re.search(r"\.text-link\s*\{[^}]*min-height:\s*44px", mobile, re.S)
    assert re.search(r"\.agreement-check\s*\{[^}]*min-height:\s*48px", mobile, re.S)
    assert re.search(r"\.captcha-close\s*\{[^}]*width:\s*44px[^}]*height:\s*44px", mobile, re.S)
    assert re.search(r"\.captcha-icon-btn\s*\{[^}]*width:\s*44px[^}]*height:\s*44px", mobile, re.S)
    assert re.search(r"\.tabs\s*\{[^}]*repeat\(3,\s*minmax\(0,\s*1fr\)\)", mobile, re.S)
    assert "body.modal-open" in html
    assert html.count("syncBodyModalLock();") >= 5


def test_console_tablet_controls_remain_touchable_without_ios_focus_zoom():
    html = (STATIC_DIR / "console.html").read_text()
    tablet = _media_block(html, "max-width: 1160px")

    assert re.search(r"button,\s*\.link-btn\s*\{[^}]*min-height:\s*44px", tablet, re.S)
    assert re.search(r"input,\s*select\s*\{[^}]*min-height:\s*44px", tablet, re.S)
    assert re.search(r"input,\s*select\s*\{[^}]*font-size:\s*16px", tablet, re.S)
    assert re.search(r"\.password-toggle\s*\{[^}]*width:\s*44px[^}]*height:\s*44px", tablet, re.S)
    assert re.search(r"\.text-link\s*\{[^}]*min-height:\s*44px", tablet, re.S)


def test_modal_grid_tracks_cannot_clip_fixed_dialogs_on_narrow_screens():
    html = (STATIC_DIR / "console.html").read_text()

    for selector in (".captcha-backdrop", ".agreement-backdrop"):
        assert re.search(
            rf"{re.escape(selector)}\s*\{{[^}}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
            html,
            re.S,
        )
    assert re.search(r"\.captcha-modal\s*\{[^}]*max-width:\s*100%", html, re.S)
