const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const consoleHtml = fs.readFileSync(path.join(path.dirname(process.argv[2]), "console.html"), "utf8");
const escapeFunction = consoleHtml.match(/function escapeHtml\(value\) \{[\s\S]*?\n    \}/);
if (!escapeFunction) throw new Error("real console escapeHtml function not found");
const guardedControls = [
  "weweLoginBtn",
  "weweShareLink",
  "weweTarget",
  "weweName",
  "weweType",
  "weweAddBtn",
  "weweArticleRefreshBtn",
  "weweTaskRefreshBtn",
  "weweArticleSource",
  "weweImageQuery",
  "weweImageSearchBtn",
  "weweSubscribeBtn",
];

function classList() {
  const values = new Set();
  return {
    add(value) { values.add(value); },
    remove(value) { values.delete(value); },
    contains(value) { return values.has(value); },
    toggle(value, force) {
      const enabled = force === undefined ? !values.has(value) : Boolean(force);
      if (enabled) values.add(value);
      else values.delete(value);
      return enabled;
    },
  };
}

function boot(statusResponder, taskResponder, operationResponder, exposeInternals = false) {
  const elements = new Map();
  const calls = [];
  const errors = [];
  const notices = [];
  const timers = [];
  const dynamicButtons = [];
  function $(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        disabled: false,
        textContent: "",
        innerHTML: "",
        className: "",
        value: "",
        href: "#",
        dataset: {},
        attributes: {},
        listeners: {},
        tabIndex: 0,
        options: [{ value: "" }],
        classList: classList(),
        addEventListener(type, handler) {
          (this.listeners[type] ||= []).push(handler);
        },
        dispatch(type, event) {
          for (const handler of this.listeners[type] || []) handler(event);
        },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        contains() { return true; },
        focus() {},
        scrollIntoView() {},
      });
    }
    return elements.get(id);
  }
  const window = {
    location: { origin: "https://doc.test" },
    addEventListener() {},
    clearTimeout() {},
    clearInterval() {},
    setTimeout(callback) { timers.push({ callback }); return timers.length; },
    setInterval() { return 1; },
    confirm() { return true; },
  };
  const context = {
    URL,
    AbortController,
    console,
    document: {
      body: { classList: classList(), contains() { return true; } },
      querySelectorAll(selector) {
        return selector === "#weweSourceRows [data-wewe-action]" ? dynamicButtons : [];
      },
    },
    window,
    $,
    api: async (path, options) => {
      calls.push(path);
      if (operationResponder) {
        const handled = operationResponder(path, options);
        if (handled !== undefined) return handled;
      }
      if (path === "/api/wewe/status") return statusResponder();
      if (path === "/api/wewe/accounts") return { accounts: [] };
      if (path === "/api/wewe/sources") return { sources: [] };
      if (path.startsWith("/api/wewe/articles")) return { articles: [] };
      if (path.startsWith("/api/wewe/tasks")) {
        return taskResponder ? taskResponder(path) : { tasks: [] };
      }
      throw new Error("unexpected request: " + path);
    },
    messageFrom(error) { return error && error.message ? error.message : String(error); },
    showError(error) { errors.push(error); },
    showNotice(message) { notices.push(String(message)); },
    escapeHtml(value) { return String(value); },
    formatCNDate(value) { return String(value); },
  };
  vm.createContext(context);
  vm.runInContext(escapeFunction[0], context, { filename: "console-escapeHtml.js" });
  const executable = exposeInternals
    ? source.replace(
      "window.loadWeweAll = loadWeweAll;",
      "window.__weweRefreshPlan = refreshWewePlan; window.__weweHandleAction = handleWeweAction; window.loadWeweAll = loadWeweAll;",
    )
    : source;
  vm.runInContext(executable, context, { filename: "wewe.js" });
  return { window, $, calls, errors, notices, timers, dynamicButtons };
}

const tests = [];
function test(name, run) { tests.push({ name, run }); }

function enabledStatus(overrides = {}) {
  return {
    enabled: true,
    upstream_configured: true,
    upstream_ok: true,
    upstream_feed_count: 1,
    plan_entitlement: {
      active: true,
      can_collect: true,
      plan_name: "个人轻量版",
      remaining_calls: 7,
    },
    ...overrides,
  };
}

test("controls fail closed before feature status is known", () => {
  const { $ } = boot(() => ({ enabled: false }));
  for (const id of guardedControls) assert.equal($(id).disabled, true, id);
});

test("status failure stops every data request and keeps controls disabled", async () => {
  const { window, $, calls } = boot(() => { throw new Error("status unavailable"); });
  await window.loadWeweAll();
  assert.deepEqual(calls, ["/api/wewe/status"]);
  for (const id of guardedControls) assert.equal($(id).disabled, true, id);
  assert.match($("weweSubscribeState").textContent, /状态检查失败/);
  assert.match($("weweSubscribeState").textContent, /「刷新状态」/);
  assert.match($("weweSourceRows").innerHTML, /状态检查失败/);
  assert.match($("weweSourceRows").innerHTML, /「刷新状态」/);
});

test("explicitly disabled feature stops every data request and shows disabled copy", async () => {
  const { window, $, calls } = boot(() => ({ enabled: false }));
  await window.loadWeweAll();
  assert.deepEqual(calls, ["/api/wewe/status"]);
  for (const id of guardedControls) assert.equal($(id).disabled, true, id);
  assert.match($("weweAccountRows").innerHTML, /账号绑定与订阅入口已停用/);
  assert.equal($("weweSubscribeBtn").textContent, "采集功能未启用");
  const exportLink = $("weweExportOpml");
  assert.equal(exportLink.classList.contains("disabled"), true);
  assert.equal(exportLink.attributes["aria-disabled"], "true");
  assert.equal(exportLink.tabIndex, -1);
  let prevented = false;
  exportLink.dispatch("click", { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
});

test("public login entry cannot bypass disabled feature state", async () => {
  const { window, calls } = boot(() => ({ enabled: false }));
  await window.loadWeweAll();
  await window.startWeweLogin();
  assert.deepEqual(calls, ["/api/wewe/status"]);
});

test("confirmed enabled feature follows normal loading path and unlocks controls", async () => {
  const { window, $, calls } = boot(() => enabledStatus({
    upstream_configured: false,
    upstream_ok: false,
    upstream_feed_count: 0,
  }));
  await window.loadWeweAll();
  assert.deepEqual(calls, [
    "/api/wewe/status",
    "/api/wewe/accounts",
    "/api/wewe/sources",
    "/api/wewe/articles?limit=50",
    "/api/wewe/tasks?limit=100",
  ]);
  assert.equal($("weweLoginBtn").disabled, false);
  assert.equal($("weweAddBtn").disabled, false);
  assert.equal($("weweArticleRefreshBtn").disabled, false);
  const exportLink = $("weweExportOpml");
  assert.equal(exportLink.classList.contains("disabled"), false);
  assert.equal(exportLink.attributes["aria-disabled"], "false");
  assert.equal(exportLink.tabIndex, 0);
  let prevented = false;
  exportLink.dispatch("click", { preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
});

test("active plan with zero remaining quota keeps read-only controls available", async () => {
  const { window, $ } = boot(
    () => ({
      enabled: true,
      upstream_configured: true,
      upstream_ok: true,
      upstream_feed_count: 1,
      plan_entitlement: {
        active: true,
        can_collect: false,
        plan_name: "个人轻量版",
        remaining_calls: 0,
        total_quota: 100,
        used_calls: 100,
      },
    }),
    null,
    (path, options) => {
      if (path === "/api/wewe/sources" && !options) {
        return { sources: [{ id: 8, name: "历史来源", source_type: "web", target_url: "https://example.com/feed", subscribed: true }] };
      }
      return undefined;
    },
  );
  await window.loadWeweAll();
  assert.equal($("weweLoginBtn").disabled, false, "binding is free with an active plan");
  assert.equal($("weweArticleRefreshBtn").disabled, false);
  assert.equal($("weweTaskRefreshBtn").disabled, false);
  assert.equal($("weweArticleSource").disabled, false);
  assert.equal($("weweExportOpml").classList.contains("disabled"), false);
  for (const id of ["weweShareLink", "weweSubscribeBtn", "weweAddBtn", "weweImageQuery", "weweImageSearchBtn"]) {
    assert.equal($(id).disabled, true, id);
  }
  assert.match($("wewePlanNotice").textContent, /剩余额度不足/);
  assert.match($("wewePlanNotice").textContent, /历史文章.*导出.*不扣费/);
  assert.match($("weweSubscribeState").textContent, /剩余额度不足/);
  assert.match($("weweSourceRows").innerHTML, /data-wewe-action="collect"[^>]*disabled/);
});

test("missing active plan blocks new paid operations but preserves history", async () => {
  const { window, $ } = boot(() => ({
    enabled: true,
    upstream_configured: true,
    upstream_ok: true,
    upstream_feed_count: 1,
    plan_entitlement: {
      active: false,
      can_collect: false,
      plan_name: null,
      remaining_calls: 0,
      total_quota: 0,
      used_calls: 0,
    },
  }));
  await window.loadWeweAll();
  assert.equal($("weweLoginBtn").disabled, true);
  assert.equal($("weweSubscribeBtn").disabled, true);
  assert.equal($("weweArticleRefreshBtn").disabled, false);
  assert.equal($("weweTaskRefreshBtn").disabled, false);
  assert.match($("wewePlanNotice").textContent, /暂无有效套餐/);
  assert.match($("weweSubscribeState").textContent, /开通套餐/);
});

test("active plan with remaining quota unlocks collection and shows shared allowance", async () => {
  const { window, $ } = boot(() => ({
    enabled: true,
    upstream_configured: true,
    upstream_ok: true,
    upstream_feed_count: 1,
    plan_entitlement: {
      active: true,
      can_collect: true,
      plan_name: "个人轻量版",
      remaining_calls: 7,
      total_quota: 100,
      used_calls: 93,
    },
  }));
  await window.loadWeweAll();
  assert.equal($("weweLoginBtn").disabled, false);
  assert.equal($("weweAddBtn").disabled, false);
  assert.equal($("weweImageSearchBtn").disabled, false);
  assert.match($("wewePlanNotice").textContent, /个人轻量版/);
  assert.match($("wewePlanNotice").textContent, /剩余 7 次/);
  assert.match($("wewePlanNotice").textContent, /共用额度/);
});

test("missing entitlement payload fails closed without disabling history or exports", async () => {
  const { window, $ } = boot(() => enabledStatus({ plan_entitlement: null }));
  await window.loadWeweAll();
  for (const id of ["weweLoginBtn", "weweShareLink", "weweAddBtn", "weweImageSearchBtn", "weweSubscribeBtn"]) {
    assert.equal($(id).disabled, true, id);
  }
  assert.equal($("weweArticleRefreshBtn").disabled, false);
  assert.equal($("weweTaskRefreshBtn").disabled, false);
  assert.equal($("weweExportOpml").classList.contains("disabled"), false);
  assert.match($("wewePlanNotice").textContent, /套餐信息暂未返回.*已暂停/);
});

function fakeSourceButton(action, subscribed) {
  return {
    disabled: false,
    dataset: { weweAction: action, id: "8", subscribed: subscribed ? "1" : "0" },
  };
}

function actionEvent(button) {
  return { target: { closest() { return button; } } };
}

test("malformed entitlement payloads never display an unlimited or numeric success state", async () => {
  const malformed = [
    [], "invalid", {}, { active: true }, { active: true, can_collect: true },
    { active: "yes", can_collect: true, remaining_calls: 7 },
    { active: true, can_collect: "yes", remaining_calls: 7 },
    { active: true, can_collect: true, remaining_calls: "unlimited" },
    { active: true, can_collect: true, remaining_calls: -1 },
  ];
  for (const plan of malformed) {
    const { window, $ } = boot(() => enabledStatus({ plan_entitlement: plan }));
    await window.loadWeweAll();
    assert.equal($("weweAddBtn").disabled, true, JSON.stringify(plan));
    assert.equal($("weweArticleRefreshBtn").disabled, false);
    assert.match($("wewePlanNotice").textContent, /套餐信息.*暂停/);
    assert.doesNotMatch($("wewePlanNotice").textContent, /不限量|剩余 NaN|剩余 undefined/);
    assert.match($("wewePlanNotice").className, /warn/);
  }
});

test("stale resume button cannot PATCH after quota loss or status failure", async () => {
  for (const mode of ["quota", "status-error"]) {
    let currentStatus = enabledStatus();
    const { window, calls, dynamicButtons } = boot(
      () => {
        if (currentStatus instanceof Error) throw currentStatus;
        return currentStatus;
      },
      null,
      (path, options) => {
        if (path === "/api/wewe/sources" && !options) {
          return { sources: [{ id: 8, name: "历史来源", source_type: "web", subscribed: false }] };
        }
        if (path === "/api/wewe/sources/8" && options && options.method === "PATCH") return {};
        return undefined;
      },
      true,
    );
    await window.loadWeweAll();
    const resume = fakeSourceButton("toggle", false);
    dynamicButtons.push(resume);
    currentStatus = mode === "quota"
      ? enabledStatus({ plan_entitlement: { active: true, can_collect: false, plan_name: "个人轻量版", remaining_calls: 0 } })
      : new Error("status unavailable");
    await window.__weweRefreshPlan(window.__weweConsoleState.generation);
    resume.disabled = false; // Simulate a real stale DOM event even if the old UI missed the redraw.
    await window.__weweHandleAction(actionEvent(resume));
    assert.equal(calls.filter((path) => path === "/api/wewe/sources/8").length, 0, mode);
    assert.equal(resume.disabled, true, mode);
  }
});

test("pause remains clickable without collection quota while resume is blocked", async () => {
  let currentStatus = enabledStatus();
  const { window, calls, dynamicButtons } = boot(
    () => currentStatus,
    null,
    (path, options) => {
      if (path === "/api/wewe/sources" && !options) {
        return { sources: [{ id: 8, name: "历史来源", source_type: "web", subscribed: true }] };
      }
      if (path === "/api/wewe/sources/8" && options && options.method === "PATCH") return {};
      return undefined;
    },
    true,
  );
  await window.loadWeweAll();
  const pause = fakeSourceButton("toggle", true);
  dynamicButtons.push(pause);
  currentStatus = enabledStatus({
    plan_entitlement: { active: true, can_collect: false, plan_name: "个人轻量版", remaining_calls: 0 },
  });
  await window.__weweRefreshPlan(window.__weweConsoleState.generation);
  assert.equal(pause.disabled, false);
  await window.__weweHandleAction(actionEvent(pause));
  assert.equal(calls.filter((path) => path === "/api/wewe/sources/8").length, 1);
  assert.equal(pause.disabled, false);
});

test("real console escaping is applied to dynamic source markup and feed exports stay readable", async () => {
  const { window, $ } = boot(
    () => enabledStatus({ plan_entitlement: null }),
    null,
    (path, options) => {
      if (path === "/api/wewe/sources" && !options) {
        return { sources: [{ id: 8, name: "<img src=x onerror=alert(1)>", source_type: "web", subscribed: false }] };
      }
      return undefined;
    },
  );
  await window.loadWeweAll();
  assert.doesNotMatch($("weweSourceRows").innerHTML, /<img src=x/);
  assert.match($("weweSourceRows").innerHTML, /&lt;img src=x onerror=alert\(1\)&gt;/);
  for (const suffix of ["feed.rss", "feed.atom", "feed.json"]) {
    assert.match($("weweSourceRows").innerHTML, new RegExp("/api/wewe/sources/8/" + suffix.replace(".", "\\.")));
  }
});

test("older generation status cannot re-enable controls after disable", async () => {
  let statusCalls = 0;
  let taskCalls = 0;
  let resolveStale;
  const { window, $, timers } = boot(
    () => {
      statusCalls += 1;
      if (statusCalls === 2) return new Promise((resolve) => { resolveStale = resolve; });
      if (statusCalls >= 3) return { enabled: false };
      return enabledStatus();
    },
    () => {
      taskCalls += 1;
      return { tasks: [{ id: 7, source_id: 1, status: taskCalls === 1 ? "running" : "success" }] };
    },
  );
  await window.loadWeweAll();
  const pendingPoll = timers[0].callback();
  for (let index = 0; index < 20 && statusCalls < 2; index += 1) await Promise.resolve();
  assert.equal(statusCalls, 2, "finished task should start a plan refresh");
  await window.loadWeweAll();
  assert.equal($("weweAddBtn").disabled, true);
  resolveStale(enabledStatus());
  await pendingPoll;
  assert.equal($("weweAddBtn").disabled, true, "stale enabled response must not be committed");
  assert.equal($("weweImageSearchBtn").disabled, true);
});

test("older same-generation quota response cannot overwrite a newer exhausted response", async () => {
  let statusCalls = 0;
  const resolvers = [];
  const { window, $ } = boot(() => {
    statusCalls += 1;
    if (statusCalls === 1) return enabledStatus();
    return new Promise((resolve) => { resolvers.push(resolve); });
  }, null, null, true);
  await window.loadWeweAll();
  const generation = window.__weweConsoleState.generation;
  const older = window.__weweRefreshPlan(generation);
  const newer = window.__weweRefreshPlan(generation);
  await Promise.resolve();
  assert.equal(resolvers.length, 2);
  resolvers[1](enabledStatus({
    plan_entitlement: { active: true, can_collect: false, plan_name: "个人轻量版", remaining_calls: 0 },
  }));
  await newer;
  assert.equal($("weweAddBtn").disabled, true);
  resolvers[0](enabledStatus());
  await older;
  assert.equal($("weweAddBtn").disabled, true, "older quota snapshot must be ignored");
  assert.equal($("weweImageSearchBtn").disabled, true);
  assert.match($("wewePlanNotice").textContent, /剩余额度不足/);
});

test("disabled generation rejects an already in-flight task poll", async () => {
  let enabled = true;
  let resolvePoll;
  let taskCalls = 0;
  const running = { tasks: [{ id: 7, source_id: 1, status: "running" }] };
  const { window, $, timers } = boot(
    () => enabledStatus({ enabled, upstream_configured: false, upstream_ok: false, upstream_feed_count: 0 }),
    () => {
      taskCalls += 1;
      if (taskCalls === 1) return running;
      return new Promise((resolve) => { resolvePoll = resolve; });
    },
  );
  await window.loadWeweAll();
  assert.equal(timers.length, 1);
  const pendingPoll = timers[0].callback();
  await Promise.resolve();
  assert.equal(taskCalls, 2);

  enabled = false;
  await window.loadWeweAll();
  const disabledHtml = $("weweTaskRows").innerHTML;
  const timerCount = timers.length;
  assert.match(disabledHtml, /已暂停/);
  resolvePoll(running);
  await pendingPoll;

  assert.equal($("weweTaskRows").innerHTML, disabledHtml);
  assert.equal(timers.length, timerCount, "stale poll must not schedule another request");
});

test("disabled generation rejects in-flight image search, source creation, and collection", async () => {
  for (const operation of ["search", "add", "collect"]) {
    let enabled = true;
    let resolveOperation;
    const pending = new Promise((resolve) => { resolveOperation = resolve; });
    const { window, $, calls, notices } = boot(
      () => enabledStatus({ enabled, upstream_configured: false, upstream_ok: false, upstream_feed_count: 0 }),
      null,
      (path, options) => {
        if (operation === "search" && path === "/api/wewe/images/search") return pending;
        if (path === "/api/wewe/sources" && options && options.method === "POST") {
          return operation === "add" ? pending : { source_id: 8 };
        }
        if (path === "/api/wewe/sources/8/collect") {
          return operation === "collect" ? pending : { task_id: 9 };
        }
        return undefined;
      },
    );
    await window.loadWeweAll();
    let pendingAction;
    if (operation === "search") {
      $("weweImageQuery").value = "曲线";
      pendingAction = $("weweImageSearchBtn").onclick();
    } else {
      $("weweType").value = "web";
      $("weweTarget").value = "https://example.com/feed.xml";
      $("weweName").value = "不得由旧请求清空";
      pendingAction = $("weweAddBtn").onclick();
    }
    for (let index = 0; index < 20; index += 1) {
      if (operation !== "collect" || calls.includes("/api/wewe/sources/8/collect")) break;
      await Promise.resolve();
    }
    if (operation === "collect") assert.equal(calls.includes("/api/wewe/sources/8/collect"), true);
    enabled = false;
    await window.loadWeweAll();
    const disabledResults = $("weweImageResults").innerHTML;
    const taskRequestCount = calls.filter((path) => path === "/api/wewe/tasks?limit=100").length;
    resolveOperation(operation === "search" ? {
      hits: [{ image_file_url: "/api/wewe/images/1/file", description: "旧结果" }],
    } : operation === "add" ? { source_id: 8 } : { task_id: 9 });
    await pendingAction;

    assert.equal($("weweImageResults").innerHTML, disabledResults, operation);
    assert.equal($("weweImageSearchBtn").disabled, true, operation);
    assert.equal($("weweAddBtn").disabled, true, operation);
    assert.equal(calls.filter((path) => path === "/api/wewe/tasks?limit=100").length, taskRequestCount, operation);
    assert.equal(notices.some((message) => message.includes("采集任务 #9 已开始")), false, operation);
    if (operation === "add") assert.equal($("weweName").value, "不得由旧请求清空");
  }
});

(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try {
      await run();
      console.log("PASS", name);
    } catch (error) {
      failures += 1;
      console.error("FAIL", name);
      console.error(error.stack || error);
    }
  }
  if (failures) process.exitCode = 1;
})();
