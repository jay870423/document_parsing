from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def js_block(js, start, end):
    begin = js.index(start)
    return js[begin:js.index(end, begin)]


def run_node(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.run(["node", str(script)], capture_output=True, text=True)


JS_DOM_HARNESS = r'''
class FakeNode {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
    this.references = [];
    this.classList = {toggle: () => {}};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }
  reset() {
    (this.resetFields || []).forEach((id) => {
      if (nodes[id]) nodes[id].value = "";
    });
  }
  querySelectorAll() { return this.references; }
  setAttribute() {}
  scrollIntoView() {}
  get valueAsNumber() { return this.value.trim() === "" ? NaN : Number(this.value); }
  get text() { return [this.textContent, ...this.children.map((node) => node.text || "")].join(" "); }
}
const nodes = {};
const document = {
  getElementById(id) { return nodes[id] ||= new FakeNode(id); },
  createElement(tag) { return new FakeNode(tag); },
  querySelectorAll() { return []; },
};
const window = {
  location: {assign() {}},
  matchMedia: () => ({matches: true}),
  scrollTo() {},
  confirm: () => true,
};
class FakeStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
globalThis.sessionStorage = new FakeStorage();
function jsonResponse(data, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: {get: () => "application/json"},
    json: async () => data,
  };
}
function trigger(id, type = "submit") {
  const listeners = document.getElementById(id).listeners[type] || [];
  return Promise.all(listeners.map((listener) => listener({preventDefault() {}})));
}
'''


JS_WORKSPACE_STUBS = r'''
let workspaceLoads = 0;
async function loadWorkspace() { workspaceLoads += 1; }
function renderEditor() {}
function renderBriefs() {}
function styleText(profile) { return profile ? "style" : ""; }
function switchView() {}
'''


FULL_APP_WORKSPACE_FIXTURE = r'''
state.userId = 1;
function workspaceFixture(knowledgeBaseId = 1, label = "A") {
  return {
    user_id: 1,
    knowledge_base_id: knowledgeBaseId,
    profile: {
      audience: `${label} 读者`, goal: `${label} 目标`, cta: "", forbidden: "",
      style_profile: null, style_draft: null,
    },
    own_articles: [], reference_articles: [], briefs: [], drafts: [], metrics: [],
    billing: {plan_name: `${label} 套餐`, remaining_calls: 8, ai_success_cost: 1},
    data_boundaries: {},
  };
}
function briefFixture(id = 2) {
  return {
    id, analysis_mode: "structure", created_at: "2026-11-25", focus: "已保存简报",
    content: {summary: "POST 已保存的简报摘要", content_observations: [], topics: [], data_limitations: []},
  };
}
function draftFixture(id = 8) {
  return {
    id, revision: 1, status: "draft", title: "已保存草稿标题",
    body_markdown: "POST 已保存的草稿正文", title_options: [], citation_map: [],
    unresolved_facts: [], risk_notes: [], sources: [],
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}
'''


def run_full_app_node(tmp_path, name, assertions):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    source = (
        JS_DOM_HARNESS
        + js[:js.rindex("initialise();")]
        + FULL_APP_WORKSPACE_FIXTURE
        + assertions
    )
    return run_node(tmp_path, name, source)


def test_growth_workbench_is_external_responsive_and_mobile_first():
    html = (STATIC / "growth.html").read_text(encoding="utf-8")
    css = (STATIC / "growth.css").read_text(encoding="utf-8")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"' in html
    assert '<script src="/static/growth.js" defer></script>' in html
    assert "onclick=" not in html
    assert "min-height: 48px" in css
    assert re.search(r"\.brand\s*\{[^}]*min-height:\s*44px", css)
    assert re.search(r"\.source-card a\s*\{[^}]*min-height:\s*44px", css)
    assert re.search(r"\.header-links a\s*\{[^}]*white-space:\s*nowrap", css)
    assert re.search(r"@media \(max-width: 760px\)[\s\S]*font-size: 16px", css)
    assert re.search(r"\.mobile-tabbar[\s\S]*?position: fixed", css)
    assert "env(safe-area-inset-bottom)" in css
    for view in ("setupView", "briefView", "writeView", "metricsView"):
        assert f'id="{view}"' in html
    assert "不自动发布" in html
    assert "描述性指标" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_growth_javascript_syntax_and_contract(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    script = tmp_path / "growth.js"
    script.write_text(js, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for endpoint in (
        "/api/growth/workspace", "/api/growth/profile", "/api/growth/own-articles",
        "/api/growth/style-profiles/generate", "/api/growth/style-profiles/activate",
        "/api/growth/briefs", "/api/growth/drafts", "/api/growth/metrics",
    ):
        assert endpoint in js
    assert "textContent" in js
    assert "noopener" in js
    assert "auto_publish" not in js.lower()
    assert 'behavior: "smooth"' not in js


def test_existing_navigation_exposes_growth_workbench():
    console = (STATIC / "console.html").read_text(encoding="utf-8")
    workbench = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<a[^>]+href="/growth"[^>]*>内容增长</a>', console)
    assert re.search(r'<a[^>]+href="/growth"[^>]*>内容增长</a>', workbench)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_evidence_renderer_surfaces_manual_edit_invalidation(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")

    def block(start, end):
        begin = js.index(start)
        return js[begin:js.index(end, begin)]

    harness = r'''
class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; this.className = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute() {}
  get text() { return [this.textContent, ...this.children.map((node) => node.text || "")].join(" "); }
}
const nodes = Object.fromEntries(["citationList", "unresolvedFacts", "riskNotes"].map((id) => [id, new FakeNode("div")]));
const document = { getElementById: (id) => nodes[id], createElement: (tag) => new FakeNode(tag) };
'''
    assertions = r'''
renderEvidence({
  evidence_invalidated: true, citation_map: [], unresolved_facts: [], risk_notes: [],
  sources: [{ref_id: "R1", title: "历史资料", url: ""}],
});
if (!nodes.citationList.text.includes("原 AI 引用映射与待核实内容已作废")) {
  throw new Error(`missing visible invalidation notice: ${nodes.citationList.text}`);
}
if (!nodes.unresolvedFacts.text.includes("旧待核实项已作废")) {
  throw new Error(`stale-fact invalidation is not visible: ${nodes.unresolvedFacts.text}`);
}
renderEvidence({
  evidence_invalidated: false,
  citation_map: [{claim: "有效结论", evidence_refs: ["R1"]}],
  unresolved_facts: ["仍需核实"], risk_notes: [], sources: [],
});
if (nodes.citationList.text.includes("已作废") || !nodes.citationList.text.includes("有效结论")) {
  throw new Error(`valid evidence was misreported: ${nodes.citationList.text}`);
}
console.log("evidence invalidation behaviour ok");
'''
    script = tmp_path / "evidence-renderer.js"
    script.write_text(
        harness
        + block("const $ =", "const state =")
        + block("function element", "function showNotice")
        + block("function refTags", "function renderBriefs")
        + block("function safeSourceLink", "function renderEditor")
        + assertions,
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "evidence invalidation behaviour ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_view_switch_respects_reduced_motion(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    switch_view = js[js.index("function switchView"):js.index("function localDate")]
    harness = r'''
const views = ["setupView", "briefView"].map((id) => ({id, hidden: false}));
const buttons = ["setupView", "briefView"].map((view) => ({
  dataset: {view},
  classList: {toggle() {}},
  setAttribute() {},
}));
const document = {querySelectorAll: (selector) => selector === ".view" ? views : buttons};
let reduceMotion = true;
const scrollCalls = [];
const window = {
  matchMedia: () => ({matches: reduceMotion}),
  scrollTo: (options) => scrollCalls.push(options),
};
'''
    assertions = r'''
switchView("briefView");
if (scrollCalls[0].behavior !== "auto") {
  throw new Error(`reduced motion was ignored: ${JSON.stringify(scrollCalls[0])}`);
}
reduceMotion = false;
switchView("setupView");
if (scrollCalls[1].behavior !== "smooth") {
  throw new Error(`normal motion preference was ignored: ${JSON.stringify(scrollCalls[1])}`);
}
console.log("view switch motion preferences ok");
'''
    script = tmp_path / "view-switch-motion.js"
    script.write_text(harness + switch_view + assertions, encoding="utf-8")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "view switch motion preferences ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_requests_use_idempotent_operations_and_suppress_double_submit(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listeners = "".join((
        js_block(js, '$("generateStyleBtn").addEventListener', '$("activateStyleBtn").addEventListener'),
        js_block(js, '$("briefForm").addEventListener', '$("draftForm").addEventListener'),
        js_block(js, '$("draftForm").addEventListener', '$("saveDraftBtn").addEventListener'),
    ))
    assertions = r'''
(async () => {
  const requests = [];
  const responders = [];
  globalThis.fetch = async (path, request) => {
    const body = request.body === undefined ? null : JSON.parse(request.body);
    requests.push({path, method: request.method, body});
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  let uuidCounter = 0;
  const nativeCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {subtle: nativeCrypto.subtle, randomUUID: () => `test-operation-${++uuidCounter}`},
  });
  state.userId = 1;
  $("briefFocusInput").value = "同一选题";
  state.knowledgeBaseId = 1;
  $("analysisModeSelect").value = "structure";
  $("referenceList").references = [{value: "4"}];
  $("draftTopicInput").value = "同一主题";
  $("draftLengthSelect").value = "1200";
  $("draftInstructionsInput").value = "保留来源";

  responders.push(() => jsonResponse({style_profile: {}}));
  await trigger("generateStyleBtn", "click");
  responders.push(() => jsonResponse({draft: {id: 8, revision: 1}}));
  await trigger("draftForm");
  for (const path of ["/api/growth/style-profiles/generate", "/api/growth/drafts"]) {
    const request = requests.find((item) => item.path === path);
    if (!request || !/^[A-Za-z0-9][A-Za-z0-9._:-]{7,79}$/.test(request.body.operation_id || "")) {
      throw new Error(`missing or invalid operation_id for ${path}: ${JSON.stringify(request)}`);
    }
  }

  responders.push(() => { throw new TypeError("offline"); });
  await trigger("briefForm");
  const failed = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{7,79}$/.test(failed.body.operation_id || "")) {
    throw new Error(`brief operation_id missing: ${JSON.stringify(failed)}`);
  }
  responders.push(() => jsonResponse({brief: {id: 2}}));
  await trigger("briefForm");
  const retried = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  if (retried.body.operation_id !== failed.body.operation_id) {
    throw new Error(`network retry changed operation_id: ${failed.body.operation_id} -> ${retried.body.operation_id}`);
  }
  responders.push(() => jsonResponse({brief: {id: 3}}));
  await trigger("briefForm");
  const nextGeneration = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  if (nextGeneration.body.operation_id === retried.body.operation_id) {
    throw new Error("a successful generation reused its completed operation_id");
  }

  let release;
  responders.push(() => new Promise((resolve) => {
    release = () => resolve(jsonResponse({brief: {id: 4}}));
  }));
  const beforeDoubleClick = requests.filter((item) => item.path === "/api/growth/briefs").length;
  const firstClick = trigger("briefForm");
  const secondClick = trigger("briefForm");
  // SHA-256 is asynchronous; wait for the first request to reach fetch rather
  // than assuming one setImmediate is enough for a real WebCrypto digest.
  for (let attempt = 0; attempt < 100 &&
      requests.filter((item) => item.path === "/api/growth/briefs").length === beforeDoubleClick;
       attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  const duringDoubleClick = requests.filter((item) => item.path === "/api/growth/briefs").length;
  if (duringDoubleClick !== beforeDoubleClick + 1) {
    throw new Error(`double submit sent ${duringDoubleClick - beforeDoubleClick} requests`);
  }
  release();
  await Promise.all([firstClick, secondClick]);

  $("briefFocusInput").value = "断网后的原请求";
  responders.push(() => { throw new TypeError("offline again"); });
  await trigger("briefForm");
  const original = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  $("briefFocusInput").value = "修改后的另一个请求";
  responders.push(() => jsonResponse({brief: {id: 5}}));
  await trigger("briefForm");
  const changed = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  if (changed.body.operation_id === original.body.operation_id) {
    throw new Error("different payloads shared an operation_id");
  }
  $("briefFocusInput").value = "断网后的原请求";
  responders.push(() => jsonResponse({brief: {id: 6}}));
  await trigger("briefForm");
  const originalRetry = requests.filter((item) => item.path === "/api/growth/briefs").at(-1);
  if (originalRetry.body.operation_id !== original.body.operation_id) {
    throw new Error("returning to an unresolved payload did not reuse its operation_id");
  }
  console.log("paid operation idempotency and duplicate suppression ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = JS_DOM_HARNESS + js[:js.index("function switchView")] + JS_WORKSPACE_STUBS + listeners + assertions
    result = run_node(tmp_path, "paid-operation-contract.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "paid operation idempotency and duplicate suppression ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_operation_id_prefers_uuid_and_has_cryptographic_fallback(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    assertions = r'''
Object.defineProperty(globalThis, "crypto", {
  configurable: true,
  value: {
    randomUUID: () => "preferred-uuid-0001",
    getRandomValues: () => { throw new Error("UUID should be preferred"); },
  },
});
if (createOperationId() !== "preferred-uuid-0001") throw new Error("randomUUID was not preferred");
let counter = 0;
Object.defineProperty(globalThis, "crypto", {
  configurable: true,
  value: {
    getRandomValues: (bytes) => {
      counter += 1;
      bytes.fill(counter);
      return bytes;
    },
  },
});
Math.random = () => { throw new Error("insecure random fallback used"); };
const first = createOperationId();
const second = createOperationId();
if (counter !== 2 || first === second) throw new Error("secure fallback did not draw fresh randomness");
for (const id of [first, second]) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{7,79}$/.test(id)) throw new Error(`invalid fallback id: ${id}`);
}
console.log("secure operation id generation ok");
'''
    result = run_node(
        tmp_path,
        "operation-id-randomness.js",
        js_block(js, "function createOperationId", "async function paidApi") + assertions,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "secure operation id generation ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_draft_save_uses_revision_and_conflict_never_overwrites(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listener = js_block(js, '$("saveDraftBtn").addEventListener', '$("metricForm").addEventListener')
    assertions = r'''
(async () => {
  const requests = [];
  const responders = [];
  globalThis.fetch = async (path, request) => {
    requests.push({path, body: JSON.parse(request.body)});
    return responders.shift()();
  };
  state.activeDraft = {id: 7, revision: 3, title: "服务端标题"};
  $("editorTitleInput").value = "本地标题";
  $("editorBodyInput").value = "本地正文";
  responders.push(() => jsonResponse({draft: {
    id: 7, revision: 4, title: "本地标题", body_markdown: "本地正文",
  }}));
  await trigger("saveDraftBtn", "click");
  if (requests[0].body.expected_revision !== 3) {
    throw new Error(`expected_revision missing or stale: ${JSON.stringify(requests[0])}`);
  }
  if (state.activeDraft.revision !== 4) throw new Error("successful save did not advance revision");

  $("editorTitleInput").value = "冲突时仍需保留的本地标题";
  responders.push(() => jsonResponse({detail: "草稿已在其他页面更新"}, 409));
  await trigger("saveDraftBtn", "click");
  if (requests[1].body.expected_revision !== 4) {
    throw new Error(`latest revision was not used: ${JSON.stringify(requests[1])}`);
  }
  const message = $("notice").textContent;
  if (!message.includes("未覆盖") || !message.includes("重新打开")) {
    throw new Error(`409 did not explain how to avoid overwriting: ${message}`);
  }
  if ($("editorTitleInput").value !== "冲突时仍需保留的本地标题" || state.activeDraft.revision !== 4) {
    throw new Error("409 discarded local edits or changed the active revision");
  }
  console.log("draft revision and conflict handling ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = JS_DOM_HARNESS + js[:js.index("function switchView")] + JS_WORKSPACE_STUBS + listener + assertions
    result = run_node(tmp_path, "draft-revision-contract.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "draft revision and conflict handling ok" in result.stdout


def test_metric_form_requires_provenance_and_leaves_counts_unknown_by_default():
    html = (STATIC / "growth.html").read_text(encoding="utf-8")
    for field_id in ("metricAccountInput", "metricSourceInput"):
        tag = re.search(rf'<input id="{field_id}"[^>]*>', html)
        assert tag is not None, field_id
        assert "required" in tag.group(0)
    assert 'id="metricCampaignInput"' in html
    for field_id in ("readsInput", "sharesInput", "followsInput", "clicksInput", "registrationsInput", "paymentsInput"):
        tag = re.search(rf'<input id="{field_id}"[^>]*>', html)
        assert tag is not None, field_id
        assert "required" not in tag.group(0)
        assert 'value="0"' not in tag.group(0)
    assert "留空" in html and "未知" in html and "明确为 0" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_metric_submission_distinguishes_null_zero_and_provenance(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listener = js_block(js, '$("metricForm").addEventListener', '  await runTask("initialise"')
    assertions = r'''
(async () => {
  let sent;
  globalThis.fetch = async (path, request) => {
    sent = {path, body: JSON.parse(request.body)};
    return jsonResponse({metric: {}});
  };
  state.knowledgeBaseId = 11;
  $("metricArticleSelect").value = "21";
  $("metricDateInput").value = "2026-11-25";
  $("metricWindowSelect").value = "7d";
  $("metricAccountInput").value = "  我的公众号  ";
  $("metricSourceInput").value = "  公众号后台 + 订单表  ";
  $("metricCampaignInput").value = "  campaign-2026-11  ";
  for (const [id, value] of Object.entries({
    readsInput: "", sharesInput: "0", followsInput: "7",
    clicksInput: "", registrationsInput: "0", paymentsInput: "12",
  })) $(id).value = value;
  await trigger("metricForm");
  const expected = {
    reads: null, shares: 0, new_follows: 7,
    landing_clicks: null, registrations: 0, payments: 12,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (sent.body[key] !== value) throw new Error(`${key}: expected ${value}, got ${sent.body[key]}`);
  }
  if (sent.body.account_label !== "我的公众号" || sent.body.source_name !== "公众号后台 + 订单表") {
    throw new Error(`metric provenance missing: ${JSON.stringify(sent.body)}`);
  }
  if (sent.body.campaign_id !== "campaign-2026-11") throw new Error("campaign_id was not preserved");
  console.log("nullable metrics and provenance ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = (
        JS_DOM_HARNESS
        + js[:js.index("function switchView")]
        + js_block(js, "function numberValue", "function percent")
        + JS_WORKSPACE_STUBS
        + listener
        + assertions
    )
    result = run_node(tmp_path, "metric-submission-contract.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "nullable metrics and provenance ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_metric_renderer_uses_current_rates_and_marks_unknown_values(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    assertions = r'''
state.workspace = {
  own_articles: [{id: 21, title: "测试文章"}],
  metrics: [{
    own_article_id: 21, metric_date: "2026-11-25", window: "7d",
    reads: null, shares: 0, new_follows: null, landing_clicks: 10,
    registrations: 0, payments: 2, account_label: "我的公众号", source_name: "后台导出",
    observed_rates: {
      share_rate: null, landing_click_rate: 0.125, registration_per_click: 0,
      payment_per_read: null, payment_per_registration: 0.5,
    },
    sample_note: "未知不计为 0",
  }],
};
renderMetrics();
const text = $("metricList").text;
if (text.includes("null") || text.includes("undefined")) throw new Error(`unknown rendered as a value: ${text}`);
if (!text.includes("落地页点击率 12.50%") || !text.includes("点击注册率 0.00%") ||
    !text.includes("注册付费率 50.00%")) throw new Error(`new observed_rates were not rendered: ${text}`);
if (text.includes("关注率")) throw new Error(`removed follow_rate is still rendered: ${text}`);
if (!text.includes("我的公众号") || !text.includes("后台导出")) throw new Error(`provenance is not visible: ${text}`);
console.log("metric rendering contract ok");
'''
    source = (
        JS_DOM_HARNESS
        + js_block(js, "const $ =", "const state =")
        + "const state = {workspace: null};\n"
        + js_block(js, "function element", "function showNotice")
        + js_block(js, "function numberValue", "function renderQuota")
        + js_block(js, "function renderMetrics", "function renderWorkspace")
        + assertions
    )
    result = run_node(tmp_path, "metric-rendering-contract.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "metric rendering contract ok" in result.stdout
    assert "follow_rate" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_operation_id_survives_network_409_and_500_until_success(tmp_path):
    assertions = r'''
(async () => {
  const requests = [];
  const responders = [];
  globalThis.fetch = async (path, request) => {
    requests.push({path, method: request.method, body: request.body ? JSON.parse(request.body) : null});
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  let uuidCounter = 0;
  const nativeCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {subtle: nativeCrypto.subtle, randomUUID: () => `retry-operation-${++uuidCounter}`},
  });
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();
  $("briefFocusInput").value = "同一付费负载";
  $("analysisModeSelect").value = "structure";

  responders.push(() => { throw new TypeError("offline"); });
  await trigger("briefForm");
  responders.push(() => jsonResponse({detail: "操作冲突"}, 409));
  await trigger("briefForm");
  responders.push(() => jsonResponse({detail: "服务端异常"}, 500));
  await trigger("briefForm");
  const failedIds = requests.filter((item) => item.path === "/api/growth/briefs").map((item) => item.body.operation_id);
  if (failedIds.length !== 3 || new Set(failedIds).size !== 1) {
    throw new Error(`network/409/500 did not retain one operation: ${JSON.stringify(failedIds)}`);
  }
  if (uuidCounter !== 1) throw new Error(`retry generated another operation id: ${uuidCounter}`);
  if ($("generateBriefBtn").disabled) throw new Error("failed paid request left its button disabled");

  responders.push(
    () => jsonResponse({brief: briefFixture(2)}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await trigger("briefForm");
  const allIds = requests.filter((item) => item.path === "/api/growth/briefs").map((item) => item.body.operation_id);
  if (allIds.length !== 4 || new Set(allIds).size !== 1) {
    throw new Error(`final success did not retry the unresolved operation: ${JSON.stringify(allIds)}`);
  }
  if ($("generateBriefBtn").disabled) throw new Error("successful retry left its button disabled");
  console.log("network, 409 and 500 all preserve the paid operation id");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-http-retry.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "network, 409 and 500 all preserve" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_all_paid_post_successes_survive_refresh_failure_and_retry_get_only(tmp_path):
    assertions = r'''
(async () => {
  const requests = [];
  const responders = [];
  globalThis.fetch = async (path, request) => {
    requests.push({path, method: request.method, body: request.body ? JSON.parse(request.body) : null});
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  const nativeCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {subtle: nativeCrypto.subtle, randomUUID: () => "paid-refresh-operation"},
  });
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();

  function assertRefreshFailure() {
    const message = $("notice").textContent;
    if (!message.includes("已生成") || !message.includes("已扣费") || !message.includes("仅列表刷新失败")) {
      throw new Error(`paid success was reported as a whole-request failure: ${message}`);
    }
    if ($("refreshWorkspaceBtn").hidden) throw new Error("GET-only retry was not offered");
  }
  async function refreshWithoutRepaid(path) {
    const postsBefore = requests.filter((item) => item.path === path && item.method === "POST").length;
    responders.push(() => jsonResponse(workspaceFixture(1, "fresh")));
    await trigger("refreshWorkspaceBtn", "click");
    const postsAfter = requests.filter((item) => item.path === path && item.method === "POST").length;
    if (postsAfter !== postsBefore) throw new Error(`refresh repeated paid POST ${path}`);
    if (!$("refreshWorkspaceBtn").hidden) throw new Error("successful GET did not dismiss refresh retry");
  }

  responders.push(
    () => jsonResponse({style_profile: {voice: "已保存的温和声音", tone: "克制"}, activated: false}),
    () => jsonResponse({detail: "refresh failed"}, 500),
  );
  await trigger("generateStyleBtn", "click");
  if (!$("draftStyleCard").textContent.includes("已保存的温和声音")) {
    throw new Error("style POST result was not rendered before the failed GET");
  }
  assertRefreshFailure();
  await refreshWithoutRepaid("/api/growth/style-profiles/generate");

  $("briefFocusInput").value = "付费成功后刷新失败";
  $("analysisModeSelect").value = "structure";
  responders.push(
    () => jsonResponse({brief: briefFixture(22)}),
    () => jsonResponse({detail: "refresh failed"}, 500),
  );
  await trigger("briefForm");
  if (!$("briefResults").text.includes("POST 已保存的简报摘要")) {
    throw new Error(`brief POST result is not visible: ${$("briefResults").text}`);
  }
  assertRefreshFailure();
  await refreshWithoutRepaid("/api/growth/briefs");

  $("draftTopicInput").value = "付费成稿后刷新失败";
  $("draftLengthSelect").value = "1200";
  responders.push(
    () => jsonResponse({draft: draftFixture(88)}),
    () => jsonResponse({detail: "refresh failed"}, 500),
  );
  await trigger("draftForm");
  if (state.activeDraft?.id !== 88 || $("draftEditor").hidden ||
      $("editorBodyInput").value !== "POST 已保存的草稿正文") {
    throw new Error("draft was not activated and rendered immediately after POST success");
  }
  assertRefreshFailure();
  await refreshWithoutRepaid("/api/growth/drafts");
  if (state.activeDraft?.id !== 88) throw new Error("GET-only refresh discarded the generated draft");
  console.log("all paid results are visible and refresh retries GET only");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-post-get-failure.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "all paid results are visible" in result.stdout
    html = (STATIC / "growth.html").read_text(encoding="utf-8")
    assert 'id="refreshWorkspaceBtn"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_kb_switch_is_atomic_and_clears_selected_brief_only_after_commit(tmp_path):
    assertions = r'''
(async () => {
  const requests = [];
  const responders = [];
  globalThis.fetch = async (path, request) => {
    requests.push({path, method: request.method, body: request.body ? JSON.parse(request.body) : null});
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}, {id: 2, name: "B"}, {id: 3, name: "C"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();
  state.selectedBriefId = 7;
  $("selectedBriefNote").hidden = false;
  $("selectedBriefNote").textContent = "已关联简报 #7";

  const styleResponse = deferred();
  responders.push(() => styleResponse.promise);
  const generating = trigger("generateStyleBtn", "click");
  await new Promise((resolve) => setImmediate(resolve));
  $("knowledgeBaseSelect").value = "2";
  await trigger("knowledgeBaseSelect", "change");
  if (state.knowledgeBaseId !== 1 || $("knowledgeBaseSelect").value !== "1" || state.workspace.profile.audience !== "A 读者") {
    throw new Error("pending task left a B selection with A state");
  }
  responders.push(() => jsonResponse(workspaceFixture(1, "A")));
  styleResponse.resolve(jsonResponse({style_profile: {voice: "A 风格"}}));
  await generating;

  const switchResponse = deferred();
  responders.push(() => switchResponse.promise);
  $("knowledgeBaseSelect").value = "2";
  const switching = trigger("knowledgeBaseSelect", "change");
  await new Promise((resolve) => setImmediate(resolve));
  if (state.knowledgeBaseId !== 1 || state.workspace.profile.audience !== "A 读者" ||
      $("knowledgeBaseSelect").value !== "1" || state.selectedBriefId !== 7) {
    throw new Error("KB state was committed before the target workspace loaded");
  }
  switchResponse.resolve(jsonResponse(workspaceFixture(2, "B")));
  await switching;
  if (state.knowledgeBaseId !== 2 || state.workspace.profile.audience !== "B 读者" ||
      $("knowledgeBaseSelect").value !== "2" || $("audienceInput").value !== "B 读者") {
    throw new Error("valid target workspace was not committed atomically");
  }
  if (state.selectedBriefId !== null || !$("selectedBriefNote").hidden || $("selectedBriefNote").textContent !== "") {
    throw new Error("KB switch left a stale associated-brief note");
  }
  $("draftTopicInput").value = "B 的主题";
  $("draftLengthSelect").value = "1200";
  responders.push(
    () => jsonResponse({draft: draftFixture(9)}),
    () => jsonResponse(workspaceFixture(2, "B")),
  );
  await trigger("draftForm");
  const draftRequest = requests.filter((item) => item.path === "/api/growth/drafts").at(-1);
  if (draftRequest.body?.brief_id) throw new Error("B draft still sent A brief_id");

  responders.push(() => jsonResponse({detail: "C unavailable"}, 500));
  $("knowledgeBaseSelect").value = "3";
  await trigger("knowledgeBaseSelect", "change");
  if (state.knowledgeBaseId !== 2 || $("knowledgeBaseSelect").value !== "2" || state.workspace.profile.audience !== "B 读者") {
    throw new Error("failed KB GET did not restore the previous committed context");
  }
  console.log("KB switching is atomic and clears stale brief association");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "atomic-kb-switch.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "KB switching is atomic" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_older_workspace_response_cannot_overwrite_newer_kb(tmp_path):
    assertions = r'''
(async () => {
  const responders = [];
  globalThis.fetch = async () => {
    if (!responders.length) throw new Error("unexpected request");
    return responders.shift()();
  };
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();
  const oldResponse = deferred();
  const newResponse = deferred();
  responders.push(() => oldResponse.promise, () => newResponse.promise);
  const oldLoad = loadWorkspace({knowledgeBaseId: 2, switchKnowledgeBase: true});
  const newLoad = loadWorkspace({knowledgeBaseId: 3, switchKnowledgeBase: true});
  newResponse.resolve(jsonResponse(workspaceFixture(3, "new")));
  await newLoad;
  oldResponse.resolve(jsonResponse(workspaceFixture(2, "old")));
  await oldLoad;
  if (state.knowledgeBaseId !== 3 || state.workspace.profile.audience !== "new 读者") {
    throw new Error("late old KB response overwrote the latest selection");
  }
  console.log("stale workspace responses are ignored");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "stale-kb-response.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "stale workspace responses are ignored" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_kb_switch_accepts_a_valid_workspace_with_null_profile(tmp_path):
    assertions = r'''
(async () => {
  const responders = [];
  globalThis.fetch = async () => {
    if (!responders.length) throw new Error("unexpected request");
    return responders.shift()();
  };
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}, {id: 2, name: "empty"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();
  const emptyWorkspace = workspaceFixture(2, "empty");
  emptyWorkspace.profile = null;
  responders.push(() => jsonResponse(emptyWorkspace));
  $("knowledgeBaseSelect").value = "2";
  await trigger("knowledgeBaseSelect", "change");
  if (state.knowledgeBaseId !== 2 || state.workspace.profile !== null ||
      $("audienceInput").value !== "" || $("knowledgeBaseSelect").value !== "2") {
    throw new Error("a valid new KB with null profile was rejected");
  }
  console.log("null profile is a valid empty workspace");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "null-profile-workspace.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "null profile is a valid empty workspace" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_editor_input_during_get_and_patch_refresh_is_never_overwritten(tmp_path):
    assertions = r'''
(async () => {
  const responders = [];
  globalThis.fetch = async (path, request) => {
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();
  state.activeDraft = {...draftFixture(7), revision: 3};
  renderEditor();
  $("editorTitleInput").value = "请求前快照标题";
  $("editorBodyInput").value = "请求前快照正文";

  const getResponse = deferred();
  responders.push(() => getResponse.promise);
  const loading = loadWorkspace({preserveEditor: true});
  $("editorTitleInput").value = "GET 期间输入的标题";
  $("editorBodyInput").value = "GET 期间输入的正文";
  await trigger("editorBodyInput", "input");
  getResponse.resolve(jsonResponse(workspaceFixture(1, "fresh")));
  await loading;
  if ($("editorTitleInput").value !== "GET 期间输入的标题" ||
      $("editorBodyInput").value !== "GET 期间输入的正文" || !state.editorDirty) {
    throw new Error("workspace GET restored an old pre-await editor snapshot");
  }

  state.activeDraft = {...draftFixture(7), revision: 3};
  $("editorTitleInput").value = "PATCH 提交标题";
  $("editorBodyInput").value = "PATCH 提交正文";
  await trigger("editorBodyInput", "input");
  const patchResponse = deferred();
  const refreshResponse = deferred();
  responders.push(() => patchResponse.promise, () => refreshResponse.promise);
  const saving = trigger("saveDraftBtn", "click");
  await new Promise((resolve) => setImmediate(resolve));
  $("editorTitleInput").value = "PATCH 期间新增标题";
  await trigger("editorTitleInput", "input");
  patchResponse.resolve(jsonResponse({draft: {
    ...draftFixture(7), revision: 4, title: "PATCH 提交标题", body_markdown: "PATCH 提交正文",
  }}));
  await new Promise((resolve) => setImmediate(resolve));
  $("editorBodyInput").value = "保存后 GET 期间新增正文";
  await trigger("editorBodyInput", "input");
  refreshResponse.resolve(jsonResponse(workspaceFixture(1, "fresh again")));
  await saving;
  if ($("editorTitleInput").value !== "PATCH 期间新增标题" ||
      $("editorBodyInput").value !== "保存后 GET 期间新增正文" || !state.editorDirty) {
    throw new Error("PATCH success or its GET erased newer editor input");
  }
  if (state.activeDraft.revision !== 4) throw new Error("successful PATCH did not retain the new revision");
  if ($("saveDraftBtn").disabled) throw new Error("save button did not recover in finally");
  console.log("editor version protection survives GET and PATCH refresh races");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "editor-version-races.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "editor version protection" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_profile_and_article_submit_preserve_input_entered_while_pending(tmp_path):
    assertions = r'''
(async () => {
  const responders = [];
  globalThis.fetch = async (path, request) => {
    if (!responders.length) throw new Error(`unexpected request: ${path}`);
    return responders.shift()();
  };
  $("ownArticleForm").resetFields = ["ownTitleInput", "ownBodyInput", "ownUrlInput", "ownPublishedInput"];
  responders.push(
    () => jsonResponse({knowledge_bases: [{id: 1, name: "A"}]}),
    () => jsonResponse(workspaceFixture(1, "A")),
  );
  await initialise();

  for (const [id, value] of Object.entries({
    audienceInput: "提交时读者", goalInput: "提交时目标", ctaInput: "提交时 CTA", forbiddenInput: "提交时红线",
  })) $(id).value = value;
  await trigger("audienceInput", "input");
  const putResponse = deferred();
  const profileGetResponse = deferred();
  responders.push(() => putResponse.promise, () => profileGetResponse.promise);
  const savingProfile = trigger("profileForm");
  await new Promise((resolve) => setImmediate(resolve));
  $("audienceInput").value = "PUT 期间新增读者";
  await trigger("audienceInput", "input");
  putResponse.resolve(jsonResponse({profile: workspaceFixture(1, "PUT response").profile}));
  profileGetResponse.resolve(jsonResponse(workspaceFixture(1, "GET response")));
  await savingProfile;
  if ($("audienceInput").value !== "PUT 期间新增读者" || !state.profileDirty) {
    throw new Error("old profile PUT/GET response cleared dirty or overwrote newer input");
  }

  $("ownTitleInput").value = "提交时文章";
  $("ownBodyInput").value = "提交时正文";
  $("ownUrlInput").value = "https://example.com/old";
  $("ownPublishedInput").value = "2026-11-25";
  await trigger("ownBodyInput", "input");
  const postResponse = deferred();
  const articleGetResponse = deferred();
  responders.push(() => postResponse.promise, () => articleGetResponse.promise);
  const savingArticle = trigger("ownArticleForm");
  await new Promise((resolve) => setImmediate(resolve));
  $("ownBodyInput").value = "POST 期间新增正文";
  $("ownTitleInput").value = "POST 期间新增标题";
  await trigger("ownBodyInput", "input");
  postResponse.resolve(jsonResponse({article: {id: 5, title: "提交时文章"}}));
  articleGetResponse.resolve(jsonResponse(workspaceFixture(1, "article GET")));
  await savingArticle;
  if ($("ownBodyInput").value !== "POST 期间新增正文" || $("ownTitleInput").value !== "POST 期间新增标题") {
    throw new Error("article form.reset erased input entered while POST was pending");
  }

  responders.push(
    () => jsonResponse({article: {id: 6, title: "POST 期间新增标题"}}),
    () => jsonResponse(workspaceFixture(1, "article GET 2")),
  );
  await trigger("ownArticleForm");
  if ($("ownBodyInput").value !== "" || $("ownTitleInput").value !== "") {
    throw new Error("unchanged successfully submitted article form was not reset");
  }
  console.log("profile and article input versions survive pending saves");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "profile-article-version-races.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "profile and article input versions" in result.stdout


def test_tablet_editor_uses_ios_safe_input_font_size():
    css = (STATIC / "growth.css").read_text(encoding="utf-8")
    tablet = re.search(r"@media \(max-width: 980px\) \{(.*?)\n\}", css, re.S)
    assert tablet is not None
    assert re.search(r"\.body-editor\s*\{[^}]*font-size:\s*16px", tablet.group(1), re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_operation_id_survives_refresh_until_result_is_applied(tmp_path):
    assertions = r'''
(async () => {
  const requests = [];
  let responses = 0;
  let applied = 0;
  globalThis.fetch = async (path, request) => {
    const body = JSON.parse(request.body);
    requests.push({path, body});
    responses += 1;
    if (responses === 1) throw new TypeError("response lost after server commit");
    return jsonResponse({brief: {id: 8, title: "恢复结果"}});
  };
  const payload = {knowledge_base_id: 11, focus: "刷新后恢复同一付费操作"};
  let lost = false;
  try {
    await paidApi("brief", "/api/growth/briefs", payload);
  } catch (error) {
    lost = error.isNetworkError === true;
  }
  if (!lost || requests.length !== 1) throw new Error("lost-response setup failed");

  // A real refresh destroys this Map but preserves the tab's sessionStorage.
  state.operationIds = new Map();
  let applyFailed = false;
  try {
    await paidApi("brief", "/api/growth/briefs", payload, () => {
      throw new Error("injected render failure");
    });
  } catch (error) {
    applyFailed = error.message === "injected render failure";
  }
  if (!applyFailed) throw new Error("successful POST was cleared before its result was applied");

  state.operationIds = new Map();
  await paidApi("brief", "/api/growth/briefs", payload, (result) => {
    if (result.brief.id !== 8) throw new Error("wrong result");
    applied += 1;
  });
  if (requests.length !== 3 || applied !== 1) throw new Error("operation did not recover correctly");
  const ids = requests.map((item) => item.body.operation_id);
  if (!ids[0] || ids.some((id) => id !== ids[0])) {
    throw new Error(`refresh generated a new paid operation id: ${JSON.stringify(ids)}`);
  }
  for (const value of sessionStorage.values.values()) {
    if (value.includes(ids[0])) throw new Error("applied operation remained in sessionStorage");
  }
  console.log("paid operation survives refresh and render failure");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-operation-refresh-recovery.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "paid operation survives refresh" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_load_draft_preserves_input_and_confirms_discarding_dirty_editor(tmp_path):
    assertions = r'''
(async () => {
  state.activeDraft = draftFixture(7);
  state.editorVersion = 3;
  state.editorDirty = false;
  $("editorTitleInput").value = "原草稿";
  $("editorBodyInput").value = "原正文";
  const pendingResponse = deferred();
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; return pendingResponse.promise; };
  const opening = loadDraft(8);
  await new Promise((resolve) => setImmediate(resolve));
  $("editorTitleInput").value = "等待期间的新标题";
  $("editorBodyInput").value = "等待期间的新正文";
  markEditorInput();
  pendingResponse.resolve(jsonResponse({draft: draftFixture(8)}));
  await opening;
  if (state.activeDraft.id !== 7 || !state.editorDirty ||
      $("editorTitleInput").value !== "等待期间的新标题" ||
      $("editorBodyInput").value !== "等待期间的新正文") {
    throw new Error("loadDraft overwrote input that arrived while GET was pending");
  }
  if (!$("notice").textContent.includes("未覆盖")) {
    throw new Error("race protection did not explain that the server draft was not rendered");
  }

  window.confirm = () => false;
  await loadDraft(9);
  if (requests !== 1) throw new Error("dirty editor was discarded without confirmation");
  console.log("loadDraft editor version and dirty confirmation ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "load-draft-editor-version.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "loadDraft editor version" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_operation_fails_closed_until_session_storage_is_durable(tmp_path):
    assertions = r'''
(async () => {
  const storage = globalThis.sessionStorage;
  const originalSet = storage.setItem.bind(storage);
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return jsonResponse({brief: {id: 8}});
  };
  const payload = {knowledge_base_id: 11, focus: "存储失败必须失败关闭"};
  storage.setItem = () => { throw new Error("injected storage denial"); };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let blocked = false;
    try { await paidApi("brief", "/api/growth/briefs", payload); }
    catch (error) { blocked = error.message.includes("尚未请求 AI"); }
    if (!blocked) throw new Error("storage denial did not block the paid POST");
  }
  if (requests !== 0 || state.operationIds.size !== 0) {
    throw new Error("failed persistence left an in-memory operation that bypassed the guard");
  }

  storage.setItem = () => {}; // A no-op storage must not be treated as durable.
  let noOpBlocked = false;
  try { await paidApi("brief", "/api/growth/briefs", payload); }
  catch (error) { noOpBlocked = error.message.includes("尚未请求 AI"); }
  if (!noOpBlocked || requests !== 0) throw new Error("no-op storage allowed a paid POST");

  storage.setItem = originalSet;
  await paidApi("brief", "/api/growth/briefs", payload, () => {});
  if (requests !== 1) throw new Error("durable storage recovery did not permit exactly one POST");
  console.log("paid operation storage fails closed");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-operation-storage-denial.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "paid operation storage fails closed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_operation_read_and_parse_failures_never_replace_existing_id(tmp_path):
    assertions = r'''
(async () => {
  const storage = globalThis.sessionStorage;
  const originalGet = storage.getItem.bind(storage);
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    throw new TypeError("response lost after server commit");
  };
  const payload = {knowledge_base_id: 11, focus: "旧编号绝不可被覆盖"};
  try { await paidApi("brief", "/api/growth/briefs", payload); } catch (_error) {}
  if (requests !== 1) throw new Error("failed to seed original paid operation");
  const originalRaw = storage.values.get(OPERATION_STORAGE_KEY);
  if (!originalRaw) throw new Error("original operation was not persisted");

  state.operationIds = new Map();
  storage.getItem = () => { throw new Error("injected read denial"); };
  try { await paidApi("brief", "/api/growth/briefs", payload); }
  catch (error) {
    if (!error.message.includes("尚未请求 AI")) throw error;
  }
  if (requests !== 1 || storage.values.get(OPERATION_STORAGE_KEY) !== originalRaw) {
    throw new Error("read failure issued a new POST or overwrote the original operation");
  }

  storage.getItem = originalGet;
  storage.setItem(OPERATION_STORAGE_KEY, "{broken-json");
  const corruptedRaw = storage.values.get(OPERATION_STORAGE_KEY);
  state.operationIds = new Map();
  try { await paidApi("brief", "/api/growth/briefs", payload); }
  catch (error) {
    if (!error.message.includes("尚未请求 AI")) throw error;
  }
  if (requests !== 1 || storage.values.get(OPERATION_STORAGE_KEY) !== corruptedRaw) {
    throw new Error("corrupt JSON was treated as an empty store and overwritten");
  }
  console.log("paid operation read and parse failures fail closed");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-operation-read-failures.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "read and parse failures fail closed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_pending_operation_is_user_scoped_and_contains_no_plaintext_payload(tmp_path):
    assertions = r'''
(async () => {
  state.userId = 101;
  let requests = 0;
  const ids = [];
  globalThis.fetch = async (_path, request) => {
    requests += 1;
    const id = JSON.parse(request.body).operation_id;
    ids.push(id);
    if (requests === 1) throw new TypeError("lost after commit");
    return jsonResponse({brief: {id: 8}});
  };
  const secret = "虚构敏感选题：私人家庭情况";
  const payload = {knowledge_base_id: 11, focus: secret};
  try { await paidApi("brief", "/api/growth/briefs", payload); } catch (_error) {}
  const rawForA = sessionStorage.values.get(OPERATION_STORAGE_KEY);
  if (!rawForA || rawForA.includes(secret) || rawForA.includes("knowledge_base_id")) {
    throw new Error("pending operation leaked plaintext payload into sessionStorage");
  }

  state.userId = 202;
  state.operationIds = new Map();
  await paidApi("brief", "/api/growth/briefs", payload, () => {});
  if (requests !== 2 || ids[1] === ids[0]) {
    throw new Error("another account reused the original account operation ID");
  }

  state.userId = 101;
  state.operationIds = new Map();
  await paidApi("brief", "/api/growth/briefs", payload, () => {});
  if (requests !== 3 || ids[2] !== ids[0]) {
    throw new Error("original account could not recover its own operation ID");
  }
  console.log("pending operation user scope and payload privacy ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "paid-operation-user-privacy.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "user scope and payload privacy" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_empty_operation_record_after_refresh_fails_closed_without_new_post(tmp_path):
    assertions = r'''
(async () => {
  const storage = globalThis.sessionStorage;
  const nativeCrypto = globalThis.crypto;
  let uuidCounter = 0;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {subtle: nativeCrypto.subtle, randomUUID: () => `empty-record-operation-${++uuidCounter}`},
  });
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new TypeError("response lost"); };
  const payload = {knowledge_base_id: 1, focus: "空字符串也必须保留原编号"};
  try { await paidApi("brief", "/api/growth/briefs", payload); } catch (_error) {}
  if (requests !== 1 || uuidCounter !== 1) throw new Error("original operation was not seeded");

  // Web Storage uses null for an absent key; an existing empty string is corrupt.
  storage.setItem(OPERATION_STORAGE_KEY, "");
  state.operationIds = new Map();
  let failedClosed = false;
  try { await paidApi("brief", "/api/growth/briefs", payload); }
  catch (error) { failedClosed = error.message.includes("尚未请求 AI"); }
  if (!failedClosed || requests !== 1 || uuidCounter !== 1 ||
      storage.values.get(OPERATION_STORAGE_KEY) !== "") {
    throw new Error("empty storage record issued a new operation or overwrote the evidence");
  }
  console.log("empty operation record fails closed after refresh");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = run_full_app_node(tmp_path, "empty-operation-record.js", assertions)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "empty operation record fails closed" in result.stdout


def test_paid_recovery_notice_and_button_share_sticky_stack():
    html = (STATIC / "growth.html").read_text(encoding="utf-8")
    css = (STATIC / "growth.css").read_text(encoding="utf-8")
    start = html.index('<div id="recoveryStack"')
    end = html.index('<div id="busyBar"', start)
    stack = html[start:end]
    assert 'id="notice"' in stack
    assert 'id="refreshWorkspaceBtn"' in stack
    assert stack.index('id="notice"') < stack.index('id="refreshWorkspaceBtn"')
    stack_match = re.search(r"\.notice-stack\s*\{([^}]+)\}", css)
    assert stack_match and "position: sticky" in stack_match.group(1)
    assert re.search(r"\.refresh-action\s*\{[^}]*width:\s*100%", css, re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_get_only_recovery_rejects_invalid_draft_id(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    refreshListener = js_block(js, '$("refreshWorkspaceBtn").addEventListener', '$("profileForm").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/drafts") return jsonResponse({draft: {title: "缺失编号的付费草稿"}});
    if (path.startsWith("/api/growth/workspace")) return jsonResponse(workspaceFixture(1));
    throw new Error(`unexpected request: ${path}`);
  };
  try {
    await paidApi("draft", "/api/growth/drafts", {knowledge_base_id: 1, topic: "无效编号"}, () => {
      throw new Error("injected draft render failure");
    });
  } catch (error) {
    if (!error.paidResultRecovery) throw error;
  }
  if (!state.refreshRecovery) throw new Error("invalid paid draft did not enter recovery");
  await trigger("refreshWorkspaceBtn", "click");
  if (!state.refreshRecovery || $("refreshWorkspaceBtn").hidden ||
      sessionStorage.values.size !== 1) {
    throw new Error("GET-only cleared a draft operation without a valid positive draft ID");
  }
  console.log("invalid paid draft ID cannot clear recovery");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = (JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE
              + refreshListener + assertions)
    result = run_node(tmp_path, "invalid-draft-recovery.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "invalid paid draft ID" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_get_only_brief_recovery_requires_original_kb_and_brief_id(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    refreshListener = js_block(js, '$("refreshWorkspaceBtn").addEventListener', '$("profileForm").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  let workspaceResponse = workspaceFixture(1);
  let posts = 0;
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/briefs") {
      posts += 1;
      return jsonResponse({brief: briefFixture(22)});
    }
    if (path.startsWith("/api/growth/workspace")) return jsonResponse(workspaceResponse);
    throw new Error(`unexpected request: ${path}`);
  };
  try {
    await paidApi("brief", "/api/growth/briefs", {knowledge_base_id: 1, focus: "原知识库"}, () => {
      throw new Error("injected brief render failure");
    });
  } catch (error) {
    if (!error.paidResultRecovery) throw error;
  }

  // Simulate a completed KB switch before the user clicks GET-only recovery.
  state.knowledgeBaseId = 2;
  state.workspace = workspaceFixture(2);
  workspaceResponse = workspaceFixture(2);
  await trigger("refreshWorkspaceBtn", "click");
  if (!state.refreshRecovery || sessionStorage.values.size !== 1 || posts !== 1) {
    throw new Error("another KB workspace cleared the original brief operation");
  }

  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  workspaceResponse = workspaceFixture(1);
  workspaceResponse.briefs = [briefFixture(22)];
  await trigger("refreshWorkspaceBtn", "click");
  if (state.refreshRecovery || sessionStorage.values.size !== 0 || posts !== 1 ||
      !$("refreshWorkspaceBtn").hidden) {
    throw new Error(`confirmed original brief did not finish GET-only recovery: ${JSON.stringify({recovery: state.refreshRecovery, storage: [...sessionStorage.values], posts, hidden: $("refreshWorkspaceBtn").hidden, notice: $("notice").textContent, kb: state.knowledgeBaseId})}`);
  }
  console.log("brief recovery is bound to original KB and artifact ID");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = (JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE
              + refreshListener + assertions)
    result = run_node(tmp_path, "brief-kb-recovery.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "bound to original KB" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_get_only_style_recovery_requires_matching_saved_style(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    refreshListener = js_block(js, '$("refreshWorkspaceBtn").addEventListener', '$("profileForm").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  const paidStyle = {voice: "本次已扣费风格", tone: "克制", do: ["真实场景"]};
  let workspaceResponse = workspaceFixture(1);
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/style-profiles/generate") return jsonResponse({style_profile: paidStyle});
    if (path.startsWith("/api/growth/workspace")) return jsonResponse(workspaceResponse);
    throw new Error(`unexpected request: ${path}`);
  };
  try {
    await paidApi("style", "/api/growth/style-profiles/generate", {knowledge_base_id: 1}, () => {
      throw new Error("injected style render failure");
    });
  } catch (error) {
    if (!error.paidResultRecovery) throw error;
  }

  workspaceResponse = workspaceFixture(1);
  workspaceResponse.profile.style_draft = {voice: "旧风格", tone: "旧"};
  await trigger("refreshWorkspaceBtn", "click");
  if (!state.refreshRecovery || sessionStorage.values.size !== 1) {
    throw new Error("an unrelated old style cleared the paid style operation");
  }

  workspaceResponse = workspaceFixture(1);
  workspaceResponse.profile.style_draft = paidStyle;
  await trigger("refreshWorkspaceBtn", "click");
  if (state.refreshRecovery || sessionStorage.values.size !== 0 || !$("refreshWorkspaceBtn").hidden) {
    throw new Error(`matching saved style did not finish GET-only recovery: ${JSON.stringify({recovery: state.refreshRecovery, storage: [...sessionStorage.values], hidden: $("refreshWorkspaceBtn").hidden, notice: $("notice").textContent, kb: state.knowledgeBaseId})}`);
  }
  console.log("style recovery requires the matching server style draft");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = (JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE
              + refreshListener + assertions)
    result = run_node(tmp_path, "style-result-recovery.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "matching server style" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_paid_draft_confirmation_failure_exposes_get_only_recovery(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    draftListener = js_block(js, '$("draftForm").addEventListener', '$("saveDraftBtn").addEventListener')
    refreshListener = js_block(js, '$("refreshWorkspaceBtn").addEventListener', '$("profileForm").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  state.activeDraft = draftFixture(7);
  state.editorVersion = 3;
  state.editorDirty = false;
  $("draftTopicInput").value = "已扣费但确认刷新失败";
  $("draftLengthSelect").value = "1200";
  $("draftInstructionsInput").value = "";
  let postRequests = 0;
  let getRequests = 0;
  let getShouldFail = true;
  const pendingPost = deferred();
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/drafts") {
      postRequests += 1;
      return pendingPost.promise;
    }
    if (path.startsWith("/api/growth/workspace")) {
      getRequests += 1;
      if (getShouldFail) throw new TypeError("confirmation GET failed");
      const workspace = workspaceFixture(1);
      workspace.drafts = [draftFixture(7), draftFixture(8)];
      return jsonResponse(workspace);
    }
    throw new Error(`unexpected request: ${path}`);
  };
  const generating = trigger("draftForm");
  await new Promise((resolve) => setImmediate(resolve));
  $("editorBodyInput").value = "等待期间的人工输入";
  markEditorInput();
  pendingPost.resolve(jsonResponse({draft: draftFixture(8)}));
  await generating;
  if (postRequests !== 1 || getRequests !== 1 || $("refreshWorkspaceBtn").hidden ||
      !$("notice").textContent.includes("已生成并扣费") ||
      !$("notice").textContent.includes("当前输入未覆盖") ||
      $("editorBodyInput").value !== "等待期间的人工输入") {
    throw new Error(`paid confirmation failure lacked explicit recovery: ${$("notice").textContent}`);
  }

  getShouldFail = false;
  await trigger("refreshWorkspaceBtn", "click");
  if (postRequests !== 1 || getRequests !== 2 || !$("refreshWorkspaceBtn").hidden ||
      $("editorBodyInput").value !== "等待期间的人工输入" || state.refreshRecovery) {
    throw new Error("GET-only recovery repeated a paid POST or failed to finish recovery");
  }
  if (sessionStorage.values.size !== 0) throw new Error("confirmed paid result left a stale pending operation");
  if (!$("notice").textContent.includes("恢复完成") || $("notice").textContent.includes("请点击")) {
    throw new Error(`successful recovery retained a stale failure instruction: ${$("notice").textContent}`);
  }
  console.log("paid draft confirmation failure has GET-only recovery");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = (JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE
              + refreshListener + draftListener + assertions)
    result = run_node(tmp_path, "paid-draft-get-only-recovery.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "GET-only recovery" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_draft_generation_preserves_editor_input_and_confirms_existing_dirty(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listener = js_block(js, '$("draftForm").addEventListener', '$("saveDraftBtn").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  state.activeDraft = draftFixture(7);
  state.editorVersion = 3;
  state.editorDirty = false;
  $("editorTitleInput").value = "草稿 A";
  $("editorBodyInput").value = "A 的旧正文";
  $("draftTopicInput").value = "生成草稿 B";
  $("draftLengthSelect").value = "1200";
  $("draftInstructionsInput").value = "";
  const pendingPost = deferred();
  let postRequests = 0;
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/drafts") {
      postRequests += 1;
      return pendingPost.promise;
    }
    if (path.startsWith("/api/growth/workspace")) {
      const workspace = workspaceFixture(1);
      workspace.drafts = [draftFixture(8)];
      return jsonResponse(workspace);
    }
    throw new Error(`unexpected request: ${path}`);
  };
  const generating = trigger("draftForm");
  await new Promise((resolve) => setImmediate(resolve));
  $("editorBodyInput").value = "等待期间的人工新输入";
  markEditorInput();
  pendingPost.resolve(jsonResponse({draft: draftFixture(8)}));
  await generating;
  if (state.activeDraft.id !== 7 || !state.editorDirty ||
      $("editorBodyInput").value !== "等待期间的人工新输入") {
    throw new Error("draft generation overwrote editor input that arrived while POST was pending");
  }
  if (!$("notice").textContent.includes("已生成并扣费") ||
      !$("notice").textContent.includes("未覆盖")) {
    throw new Error("paid draft preservation was not explained to the user");
  }

  state.editorDirty = true;
  window.confirm = () => false;
  await trigger("draftForm");
  if (postRequests !== 1) throw new Error("existing dirty editor was discarded without confirmation");
  console.log("draft generation editor protection ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE + listener + assertions
    result = run_node(tmp_path, "draft-generation-editor-protection.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "draft generation editor protection" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_kb_switch_confirms_dirty_and_aborts_when_editor_changes_in_flight(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listener = js_block(js, '$("knowledgeBaseSelect").addEventListener', '$("refreshWorkspaceBtn").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  state.activeDraft = draftFixture(7);
  state.editorVersion = 3;
  state.editorDirty = true;
  $("editorBodyInput").value = "未保存的 A 正文";
  $("knowledgeBaseSelect").value = "2";
  window.confirm = () => false;
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new Error("must not fetch"); };
  await trigger("knowledgeBaseSelect", "change");
  if (requests !== 0 || state.knowledgeBaseId !== 1 || $("knowledgeBaseSelect").value !== "1") {
    throw new Error("dirty KB switch did not fail closed before GET");
  }

  state.editorDirty = false;
  window.confirm = () => true;
  const pendingGet = deferred();
  globalThis.fetch = async () => { requests += 1; return pendingGet.promise; };
  $("knowledgeBaseSelect").value = "2";
  const switching = trigger("knowledgeBaseSelect", "change");
  await new Promise((resolve) => setImmediate(resolve));
  $("editorBodyInput").value = "切换期间的新输入";
  markEditorInput();
  pendingGet.resolve(jsonResponse(workspaceFixture(2)));
  await switching;
  if (state.knowledgeBaseId !== 1 || state.workspace.knowledge_base_id !== 1 ||
      state.activeDraft.id !== 7 || $("editorBodyInput").value !== "切换期间的新输入") {
    throw new Error("in-flight editor input did not abort KB switch");
  }
  console.log("KB switch dirty and in-flight editor protection ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE + listener + assertions
    result = run_node(tmp_path, "kb-switch-editor-protection.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "KB switch dirty and in-flight" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_save_response_does_not_replace_a_different_active_draft(tmp_path):
    js = (STATIC / "growth.js").read_text(encoding="utf-8")
    listener = js_block(js, '$("saveDraftBtn").addEventListener', '$("metricForm").addEventListener')
    assertions = r'''
(async () => {
  state.knowledgeBaseId = 1;
  state.workspace = workspaceFixture(1);
  state.activeDraft = draftFixture(7);
  state.editorVersion = 3;
  state.editorDirty = true;
  $("editorTitleInput").value = "保存 A";
  $("editorBodyInput").value = "A 的提交内容";
  const pendingPatch = deferred();
  globalThis.fetch = async (path) => {
    if (path === "/api/growth/drafts/7") return pendingPatch.promise;
    if (path.startsWith("/api/growth/workspace")) {
      const workspace = workspaceFixture(1);
      workspace.drafts = [draftFixture(7), draftFixture(8)];
      return jsonResponse(workspace);
    }
    throw new Error(`unexpected request: ${path}`);
  };
  const saving = trigger("saveDraftBtn", "click");
  await new Promise((resolve) => setImmediate(resolve));
  state.activeDraft = draftFixture(8);
  state.editorVersion += 1;
  state.editorDirty = true;
  $("editorTitleInput").value = "当前草稿 B";
  $("editorBodyInput").value = "B 的未保存内容";
  pendingPatch.resolve(jsonResponse({draft: {...draftFixture(7), revision: 2}}));
  await saving;
  if (state.activeDraft.id !== 8 || !state.editorDirty ||
      $("editorTitleInput").value !== "当前草稿 B" ||
      $("editorBodyInput").value !== "B 的未保存内容") {
    throw new Error("save response rebound the editor to the old draft");
  }
  if (!$("notice").textContent.includes("未覆盖")) {
    throw new Error("successful background save did not explain editor preservation");
  }
  console.log("save response active draft protection ok");
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    source = JS_DOM_HARNESS + js[:js.rindex("initialise();")] + FULL_APP_WORKSPACE_FIXTURE + listener + assertions
    result = run_node(tmp_path, "save-active-draft-protection.js", source)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "save response active draft protection" in result.stdout
