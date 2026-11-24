from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "app" / "static" / "console.html"


def test_wewe_console_has_one_controller_and_external_assets():
    html = CONSOLE.read_text(encoding="utf-8")
    assert 'id="weweSubscribeForm"' in html
    assert 'id="weweQrBackdrop"' in html
    assert 'src="/static/wewe.js?' in html
    assert 'href="/static/wewe.css?' in html
    # The old inline table controller must not remain and double-bind actions.
    assert "const weweState =" not in html
    js = (ROOT / "app" / "static" / "wewe.js").read_text(encoding="utf-8")
    assert "window.loadWeweAll = loadWeweAll" in js
    assert '"/api/wewe/subscriptions"' in js
    assert '"/api/wewe/accounts/login-sessions"' in js


def test_console_error_feedback_is_visible_and_reads_response_body_once():
    html = CONSOLE.read_text(encoding="utf-8")
    api_function = html.split("async function api(path, options = {}) {", 1)[1].split(
        "function showNotice", 1
    )[0]
    assert "const responseText = await resp.text();" in api_function
    assert "JSON.parse(responseText)" in api_function
    assert "await resp.json()" not in api_function

    active_notice_css = html.split(".notice.ok, .notice.err {", 1)[1].split(
        ".layout", 1
    )[0]
    assert "position: fixed" in active_notice_css
    assert "env(safe-area-inset-top, 0px)" in active_notice_css
    assert 'box.setAttribute("role", level === "err" ? "alert" : "status")' in html

    mobile_css = html.split("@media (max-width: 900px) {", 1)[1].split("</style>", 1)[0]
    assert "button, .link-btn" in mobile_css
    assert "input, select" in mobile_css
    assert ".password-toggle" in mobile_css
    assert mobile_css.count("min-height: 44px") >= 2
    assert "font-size: 16px" in mobile_css


def test_wewe_disabled_state_short_circuits_data_requests_and_disables_controls():
    js = (ROOT / "app" / "static" / "wewe.js").read_text(encoding="utf-8")
    assert "function weweDisabled()" in js
    load_all = js.split("function loadWeweAll() {", 1)[1].split(
        "async function startCollection", 1
    )[0]
    assert load_all.index("await loadWeweStatus(generation);") < load_all.index("if (weweDisabled())")
    assert "if (!applied || generation !== state.generation) return;" in load_all
    assert 'state.accountsError = "";' in load_all
    assert "renderAccounts();" in load_all
    disabled_accounts = js.split("function renderAccounts() {", 1)[1].split(
        "async function loadWeweStatus", 1
    )[0]
    assert "if (weweDisabled())" in disabled_accounts
    assert "账号绑定与订阅入口已停用" in disabled_accounts
    availability = js.split("function updateSubscribeAvailability() {", 1)[1].split(
        "function renderAccounts", 1
    )[0]
    assert "采集功能未启用" in availability
    for control in (
        "weweLoginBtn",
        "weweShareLink",
        "weweTarget",
        "weweName",
        "weweType",
        "weweAddBtn",
    ):
        assert f'"{control}"' in js


def test_plan_catalog_ui_exposes_collection_capabilities_and_billing_rules():
    html = CONSOLE.read_text(encoding="utf-8")
    for element_id in ("planCapabilities", "planBillingRules", "wewePlanNotice"):
        assert f'id="{element_id}"' in html
    plans_view = html.split('<section id="plansView"', 1)[1].split('<section id="usageView"', 1)[0]
    assert "文档、知识库问答与采集源共用套餐额度" in plans_view
    assert "成功或部分成功的采集任务扣 1 次" in plans_view
    assert "失败自动退回" in plans_view
    assert "查看已采集来源、文章、历史任务和导出订阅不扣费" in plans_view
    assert "p.capabilities" in html
    assert "p.billing_rules" in html

    js = (ROOT / "app" / "static" / "wewe.js").read_text(encoding="utf-8")
    assert "function weweCollectionBlocked()" in js
    assert "plan_entitlement" in js
    assert "剩余额度不足" in js
    assert "renderPlanNotice" in js


def test_wewe_responsive_layout_and_touch_contract():
    css = (ROOT / "app" / "static" / "wewe.css").read_text(encoding="utf-8")
    base_targets = css.split("#weweView button, #weweView a", 1)[1].split("}", 1)[0]
    assert "min-height: 44px" in base_targets
    base_inputs = css.split("#weweView input, #weweView select", 1)[1].split("}", 1)[0]
    assert "min-height: 46px" in base_inputs
    assert "font-size: 16px" in base_inputs

    tablet = css.split("@media (max-width: 720px) {", 1)[1].split(
        "@media (max-width: 480px) {", 1
    )[0]
    assert ".wewe-subscribe-row, .wewe-image-search { grid-template-columns: 1fr; }" in tablet
    assert ".wewe-source-actions { padding-left: 0; }" in tablet
    assert ".wewe-feed-links { display: flex; width: 100%; }" in tablet

    phone = css.split("@media (max-width: 480px) {", 1)[1]
    assert ".wewe-article-item, .wewe-task-item { flex-wrap: wrap; }" in phone
    assert ".wewe-filter-actions { max-width: none; width: 100%; }" in phone
    assert ".wewe-article-actions { width: 100%; }" in phone
    assert ".wewe-task-metrics { width: 100%; text-align: left; }" in phone
    assert ".wewe-qr-actions { grid-template-columns: 1fr; }" in phone

    js = (ROOT / "app" / "static" / "wewe.js").read_text(encoding="utf-8")
    assert "wewe-source-identity" in js
    identity = css.split(".wewe-source-identity {", 1)[1].split("}", 1)[0]
    assert "-webkit-line-clamp: 2" in identity
    assert "overflow: hidden" in identity
    assert ".wewe-source-identity:focus" in css
