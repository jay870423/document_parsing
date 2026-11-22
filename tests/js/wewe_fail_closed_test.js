const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
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

function boot(statusResponder, taskResponder, operationResponder) {
  const elements = new Map();
  const calls = [];
  const errors = [];
  const notices = [];
  const timers = [];
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
    document: { body: { classList: classList(), contains() { return true; } } },
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
  vm.runInContext(source, context, { filename: "wewe.js" });
  return { window, $, calls, errors, notices, timers };
}

const tests = [];
function test(name, run) { tests.push({ name, run }); }

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
  const { window, $, calls } = boot(() => ({
    enabled: true,
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

test("disabled generation rejects an already in-flight task poll", async () => {
  let enabled = true;
  let resolvePoll;
  let taskCalls = 0;
  const running = { tasks: [{ id: 7, source_id: 1, status: "running" }] };
  const { window, $, timers } = boot(
    () => ({ enabled, upstream_configured: false, upstream_ok: false, upstream_feed_count: 0 }),
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
      () => ({ enabled, upstream_configured: false, upstream_ok: false, upstream_feed_count: 0 }),
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
