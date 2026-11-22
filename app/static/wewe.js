/* WeWe RSS onboarding, subscriptions and collection task console. */
(function () {
  "use strict";

  const state = {
    status: null,
    statusError: "",
    accounts: null,
    accountsError: "",
    sources: [],
    articles: [],
    tasks: [],
    activeTasks: new Set(),
    taskTimer: null,
    taskPollBusy: false,
    loadAllPromise: null,
    generation: 0,
    qr: {
      id: null,
      generation: 0,
      controller: null,
      countdownTimer: null,
      retryTimer: null,
      deadline: 0,
      polling: false,
      failures: 0,
    },
  };

  const taskLabels = {
    running: "采集中",
    success: "已完成",
    succeeded: "已完成",
    completed: "已完成",
    partial: "部分完成",
    failed: "失败",
  };
  const articleLabels = {
    pending: "待处理",
    fetched: "已抓取",
    processing: "处理中",
    indexed: "已入库",
    partial: "部分入库",
    failed: "处理失败",
  };

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || ""), window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_err) {
      return "";
    }
  }

  function displayTime(value) {
    return value ? formatCNDate(value) : "尚未同步";
  }

  function sourceName(id) {
    const source = state.sources.find((item) => String(item.id) === String(id));
    return source ? source.name : "未命名来源";
  }

  function isTaskRunning(task) {
    return task && task.status === "running";
  }

  function hasRunningTaskForSource(sourceId) {
    return state.tasks.some((task) => String(task.source_id) === String(sourceId) && isTaskRunning(task));
  }

  function setSubscribeMessage(message, level) {
    const box = $("weweSubscribeState");
    box.textContent = message;
    box.className = "wewe-inline-state" + (level ? " " + level : "");
  }

  function updateSteps() {
    const hasAccount = Array.isArray(state.accounts) && state.accounts.some((item) => item.available);
    const hasSource = state.sources.length > 0;
    $("weweStepAccount").classList.toggle("active", !hasAccount);
    $("weweStepSubscribe").classList.toggle("active", hasAccount && !hasSource);
    $("weweStepCollect").classList.toggle("active", hasSource);
  }

  function weweDisabled() {
    return !state.status || state.status.enabled !== true;
  }

  function updateFeatureAvailability() {
    const disabled = weweDisabled();
    [
      "weweLoginBtn", "weweShareLink", "weweTarget", "weweName", "weweType", "weweAddBtn",
      "weweArticleRefreshBtn", "weweTaskRefreshBtn", "weweArticleSource", "weweImageQuery", "weweImageSearchBtn",
    ].forEach((id) => { $(id).disabled = disabled; });
    const exportLink = $("weweExportOpml");
    exportLink.classList.toggle("disabled", disabled);
    exportLink.setAttribute("aria-disabled", disabled ? "true" : "false");
    exportLink.tabIndex = disabled ? -1 : 0;
    updateSubscribeAvailability();
  }

  function updateSubscribeAvailability() {
    const button = $("weweSubscribeBtn");
    if (weweDisabled()) {
      button.disabled = true;
      if (state.statusError) {
        button.textContent = "状态检查失败";
        setSubscribeMessage("采集状态检查失败，账号绑定、订阅与数据操作已暂停。请点击「刷新状态」重试。", "err");
      } else if (!state.status) {
        button.textContent = "正在检查采集状态";
        setSubscribeMessage("正在确认采集功能是否可用，请稍候。", "");
      } else {
        button.textContent = "采集功能未启用";
        setSubscribeMessage("当前环境未启用内容采集，账号绑定与订阅入口已停用。", "");
      }
      return;
    }
    if (button.dataset.busy === "1") return;
    button.disabled = false;
    button.textContent = "订阅并开始采集";
    if (state.status && state.status.enabled && !state.status.upstream_configured) {
      button.disabled = true;
      button.textContent = "公众号上游未配置";
      setSubscribeMessage("目前仅可使用下方高级入口添加网站来源。", "err");
      return;
    }
    if (Array.isArray(state.accounts) && !state.accounts.some((item) => item.available)) {
      button.disabled = true;
      button.textContent = "请先绑定可用读书账号";
      setSubscribeMessage("先完成上方扫码绑定；若账号显示失效或小黑屋，请重新扫码或更换账号。", "");
    }
  }

  function renderAccounts() {
    const box = $("weweAccountRows");
    if (weweDisabled()) {
      const message = state.statusError
        ? "采集状态检查失败，账号信息未加载。请点击右上角「刷新状态」重试。"
        : (!state.status ? "正在确认采集功能状态，账号信息暂未加载。" : "当前环境未启用内容采集，账号绑定与订阅入口已停用。");
      box.innerHTML = '<div class="empty-state">' + escapeHtml(message) + '</div>';
      updateSteps();
      updateSubscribeAvailability();
      return;
    }
    if (state.accountsError) {
      box.innerHTML = '<div class="empty-state">' + escapeHtml(state.accountsError) +
        ' <button class="secondary" type="button" data-wewe-action="retry-accounts">重新检查</button></div>';
      updateSteps();
      updateSubscribeAvailability();
      return;
    }
    if (!Array.isArray(state.accounts)) {
      box.innerHTML = '<div class="empty-state">正在检查读书账号…</div>';
      return;
    }
    if (!state.accounts.length) {
      box.innerHTML = '<div class="empty-state">尚未绑定读书账号。<br>点击右上角「微信扫码绑定」，完成后再订阅公众号。</div>';
      updateSteps();
      updateSubscribeAvailability();
      return;
    }
    box.innerHTML = state.accounts.map((account) => {
      const available = Boolean(account.available);
      const statusClass = available ? "" : (account.blocked ? " warn" : " err");
      return '<div class="wewe-account-item' + (available ? "" : " unavailable") + '">' +
        '<span class="wewe-account-icon" aria-hidden="true">读</span>' +
        '<div class="wewe-account-main">' +
          '<div class="wewe-item-title">' + escapeHtml(account.name || "微信读书账号") + '</div>' +
          '<div class="wewe-item-meta">账号 #' + escapeHtml(account.id) + ' · 更新于 ' + escapeHtml(displayTime(account.updated_at)) + '</div>' +
        '</div>' +
        '<span class="wewe-status' + statusClass + '">' + escapeHtml(account.status_label || "未知") + '</span>' +
      '</div>';
    }).join("");
    updateSteps();
    updateSubscribeAvailability();
  }

  async function loadWeweStatus() {
    const badge = $("weweState");
    try {
      const status = await api("/api/wewe/status");
      state.status = status;
      state.statusError = "";
      let label = "未启用";
      if (status.enabled) {
        if (status.upstream_ok) label = "服务正常 · 上游 " + status.upstream_feed_count + " 个订阅源";
        else if (status.upstream_configured) label = "公众号服务暂时不可用";
        else label = "仅网站来源可用";
      }
      badge.textContent = label;
      badge.className = "badge" + (status.upstream_ok || (status.enabled && !status.upstream_configured) ? " ok" : "");
    } catch (err) {
      state.status = null;
      state.statusError = messageFrom(err);
      badge.textContent = "状态检查失败";
      badge.className = "badge";
      state.accountsError = state.statusError;
    }
    updateFeatureAvailability();
  }

  async function loadWeweAccounts() {
    try {
      const data = await api("/api/wewe/accounts");
      state.accounts = data.accounts || [];
      state.accountsError = "";
    } catch (err) {
      state.accounts = null;
      state.accountsError = messageFrom(err);
    }
    renderAccounts();
  }

  function renderSources() {
    const box = $("weweSourceRows");
    $("weweSourceSummary").textContent = state.sources.length
      ? "共 " + state.sources.length + " 个来源 · 自动订阅已开启的来源"
      : "按公众号自动更新，也可单独采集或导出。";
    if (!state.sources.length) {
      box.innerHTML = '<div class="empty-state">暂无订阅，先粘贴一篇公众号文章链接。</div>';
    } else {
      box.innerHTML = state.sources.map((item) => {
        const kind = item.source_type === "web" ? "网站" : "公众号";
        const subscribed = Boolean(item.subscribed);
        const identity = item.source_type === "web" ? item.target_url : item.upstream_feed_id;
        const running = hasRunningTaskForSource(item.id);
        const name = item.name || identity || "未命名来源";
        const avatar = Array.from(name)[0] || "订";
        return '<article class="wewe-source-item">' +
          '<span class="wewe-avatar" aria-hidden="true">' + escapeHtml(avatar) + '</span>' +
          '<div class="wewe-source-main">' +
            '<div class="wewe-item-title">' + escapeHtml(name) + '</div>' +
            '<div class="wewe-item-meta">' + kind + ' · ' + (subscribed ? "自动订阅中" : "已暂停自动订阅") + ' · 上次同步 ' + escapeHtml(displayTime(item.last_sync_at)) + '</div>' +
            '<div class="wewe-item-meta wewe-source-identity" tabindex="0" title="' + escapeHtml(identity || "") + '">' + escapeHtml(identity || "") + '</div>' +
          '</div>' +
          '<span class="wewe-status' + (subscribed ? "" : " warn") + '">' + (running ? "采集中" : (subscribed ? "已订阅" : "已暂停")) + '</span>' +
          '<div class="wewe-source-actions">' +
            '<button class="teal" type="button" data-wewe-action="collect" data-id="' + Number(item.id) + '"' + (running ? " disabled" : "") + '>' + (running ? "正在采集…" : "立即采集") + '</button>' +
            '<button class="secondary" type="button" data-wewe-action="toggle" data-id="' + Number(item.id) + '" data-subscribed="' + (subscribed ? "1" : "0") + '">' + (subscribed ? "暂停订阅" : "恢复订阅") + '</button>' +
            '<button class="secondary" type="button" data-wewe-action="delete-source" data-id="' + Number(item.id) + '">删除</button>' +
            '<span class="wewe-feed-links" aria-label="导出订阅">' +
              '<a class="secondary" href="/api/wewe/sources/' + Number(item.id) + '/feed.rss" target="_blank" rel="noopener">RSS</a>' +
              '<a class="secondary" href="/api/wewe/sources/' + Number(item.id) + '/feed.atom" target="_blank" rel="noopener">Atom</a>' +
              '<a class="secondary" href="/api/wewe/sources/' + Number(item.id) + '/feed.json" target="_blank" rel="noopener">JSON</a>' +
            '</span>' +
          '</div>' +
        '</article>';
      }).join("");
    }
    const select = $("weweArticleSource");
    const current = select.value;
    select.innerHTML = '<option value="">全部来源</option>' + state.sources.map((item) =>
      '<option value="' + Number(item.id) + '">' + escapeHtml(item.name) + '</option>'
    ).join("");
    if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
    updateSteps();
  }

  async function loadWeweSources() {
    const generation = state.generation;
    const data = await api("/api/wewe/sources");
    if (generation !== state.generation || weweDisabled()) return;
    state.sources = data.sources || [];
    renderSources();
  }

  function renderArticles() {
    const box = $("weweArticleRows");
    if (!state.articles.length) {
      box.innerHTML = '<div class="empty-state">暂无文章。订阅后采集需要一些时间，可稍后在任务列表查看进度。</div>';
      return;
    }
    box.innerHTML = state.articles.map((item) => {
      const status = articleLabels[item.status] || item.status || "未知";
      const statusClass = item.status === "failed" ? " err" : (item.status === "indexed" ? "" : " warn");
      const originalUrl = safeHttpUrl(item.url);
      const title = item.title || item.upstream_article_id || "未命名文章";
      return '<article class="wewe-article-item">' +
        '<div class="wewe-article-main">' +
          '<div class="wewe-item-title">' + escapeHtml(title) + '</div>' +
          '<div class="wewe-item-meta">' + escapeHtml(sourceName(item.source_id)) + ' · ' + escapeHtml(displayTime(item.published_at)) + '</div>' +
          (item.error ? '<div class="wewe-task-error">' + escapeHtml(item.error) + '</div>' : "") +
        '</div>' +
        '<span class="wewe-status' + statusClass + '">' + escapeHtml(status) + '</span>' +
        '<div class="wewe-article-actions">' +
          (originalUrl ? '<a href="' + escapeHtml(originalUrl) + '" target="_blank" rel="noopener noreferrer">查看原文</a>' : "") +
          '<button class="secondary" type="button" data-wewe-action="delete-article" data-id="' + Number(item.id) + '">删除</button>' +
        '</div>' +
      '</article>';
    }).join("");
  }

  async function loadWeweArticles() {
    const generation = state.generation;
    const sourceId = $("weweArticleSource").value;
    const url = "/api/wewe/articles?limit=50" + (sourceId ? "&source_id=" + encodeURIComponent(sourceId) : "");
    const data = await api(url);
    if (generation !== state.generation || weweDisabled()) return;
    state.articles = data.articles || [];
    renderArticles();
  }

  function renderTasks() {
    const box = $("weweTaskRows");
    const runningCount = state.tasks.filter(isTaskRunning).length;
    $("weweTaskSummary").textContent = runningCount
      ? runningCount + " 个任务正在后台采集，页面会自动更新。"
      : "运行中的任务会自动刷新，无需停留等待。";
    if (!state.tasks.length) {
      box.innerHTML = '<div class="empty-state">暂无任务</div>';
      return;
    }
    box.innerHTML = state.tasks.map((item) => {
      const status = taskLabels[item.status] || item.status || "未知";
      const statusClass = item.status === "failed" ? " err" : (isTaskRunning(item) || item.status === "partial" ? " warn" : "");
      const trigger = item.kind === "scheduled" ? "定时" : (item.kind === "subscribe" ? "订阅" : "手动");
      return '<div class="wewe-task-item">' +
        '<div class="wewe-task-main">' +
          '<div class="wewe-item-title">#' + Number(item.id) + ' · ' + escapeHtml(item.source_id ? sourceName(item.source_id) : "全量订阅") + '</div>' +
          '<div class="wewe-item-meta">' + trigger + '采集 · 开始于 ' + escapeHtml(displayTime(item.started_at || item.created_at)) + '</div>' +
          (item.error ? '<div class="wewe-task-error">' + escapeHtml(item.error) + '</div>' : "") +
        '</div>' +
        '<span class="wewe-status' + statusClass + '">' + escapeHtml(status) + '</span>' +
        '<div class="wewe-task-metrics">抓取 ' + Number(item.article_count || 0) + ' 篇<br>入库 ' + Number(item.indexed_count || 0) + ' 篇 · 图片 ' + Number(item.image_count || 0) + ' 张</div>' +
      '</div>';
    }).join("");
  }

  function scheduleTaskPoll(delay, generation = state.generation) {
    if (generation !== state.generation || weweDisabled()) return;
    window.clearTimeout(state.taskTimer);
    state.taskTimer = null;
    if (!state.activeTasks.size) return;
    state.taskTimer = window.setTimeout(() => {
      if (generation !== state.generation || weweDisabled()) return;
      return pollWeweTasks(generation);
    }, delay || 1800);
  }

  function watchWeweTask(taskId) {
    const id = Number(taskId);
    if (!id || weweDisabled()) return;
    state.activeTasks.add(id);
    $("weweTaskSummary").textContent = "任务 #" + id + " 已进入后台队列，正在等待执行结果…";
    scheduleTaskPoll(500);
  }

  async function pollWeweTasks(generation = state.generation) {
    if (generation !== state.generation || weweDisabled() || state.taskPollBusy || !state.activeTasks.size) return;
    state.taskPollBusy = true;
    let retryDelay = 1800;
    try {
      const data = await api("/api/wewe/tasks?limit=100");
      if (generation !== state.generation || weweDisabled()) return;
      const previousActive = new Set(state.activeTasks);
      state.tasks = data.tasks || [];
      const runningIds = new Set(state.tasks.filter(isTaskRunning).map((item) => Number(item.id)));
      const finished = state.tasks.filter((item) => previousActive.has(Number(item.id)) && !isTaskRunning(item));
      state.activeTasks.clear();
      runningIds.forEach((id) => state.activeTasks.add(id));
      renderTasks();
      renderSources();
      if (finished.length) {
        await Promise.all([loadWeweSources(), loadWeweArticles()]);
        if (generation !== state.generation || weweDisabled()) return;
        const failed = finished.filter((item) => item.status === "failed" || item.status === "partial");
        const indexed = finished.reduce((sum, item) => sum + Number(item.indexed_count || 0), 0);
        if (failed.length) showNotice(failed.length + " 个采集任务失败或部分完成，请查看任务详情", "err");
        else showNotice("采集已完成，新增/更新入库 " + indexed + " 篇文章");
      }
    } catch (err) {
      if (generation !== state.generation || weweDisabled()) return;
      retryDelay = 4000;
      $("weweTaskSummary").textContent = "任务状态暂时获取失败，将自动重试：" + messageFrom(err);
    } finally {
      state.taskPollBusy = false;
      scheduleTaskPoll(retryDelay, generation);
    }
  }

  async function loadWeweTasks(generation = state.generation) {
    if (generation !== state.generation || weweDisabled()) return;
    const data = await api("/api/wewe/tasks?limit=100");
    if (generation !== state.generation || weweDisabled()) return;
    state.tasks = data.tasks || [];
    state.tasks.filter(isTaskRunning).forEach((item) => state.activeTasks.add(Number(item.id)));
    renderTasks();
    renderSources();
    scheduleTaskPoll(800, generation);
  }

  function loadWeweAll() {
    if (state.loadAllPromise) return state.loadAllPromise;
    const generation = ++state.generation;
    state.loadAllPromise = (async function () {
      await loadWeweStatus();
      if (weweDisabled()) {
        window.clearTimeout(state.taskTimer);
        state.taskTimer = null;
        state.accounts = [];
        state.accountsError = "";
        state.sources = [];
        state.articles = [];
        state.tasks = [];
        state.activeTasks.clear();
        renderAccounts();
        renderSources();
        renderArticles();
        renderTasks();
        const disabledMessage = state.statusError
          ? '<div class="empty-state">采集状态检查失败，数据加载与操作入口已暂停。请点击右上角「刷新状态」重试。</div>'
          : '<div class="empty-state">内容采集未启用，数据加载与操作入口已暂停。</div>';
        const disabledSummary = state.statusError ? "状态检查失败，已暂停数据加载" : "内容采集未启用";
        $("weweSourceRows").innerHTML = disabledMessage;
        $("weweArticleRows").innerHTML = disabledMessage;
        $("weweTaskRows").innerHTML = disabledMessage;
        $("weweSourceSummary").textContent = disabledSummary;
        $("weweTaskSummary").textContent = disabledSummary;
        $("weweImageResults").innerHTML = "";
        updateFeatureAvailability();
        return;
      }
      await Promise.all([loadWeweAccounts(), loadWeweSources()]);
      await Promise.all([loadWeweArticles(), loadWeweTasks()]);
    })().finally(() => {
      state.loadAllPromise = null;
    });
    return state.loadAllPromise;
  }

  async function startCollection(sourceId, generation = state.generation) {
    if (generation !== state.generation || weweDisabled()) return;
    const result = await api("/api/wewe/sources/" + Number(sourceId) + "/collect", {
      method: "POST",
      body: JSON.stringify({ refresh: true }),
    });
    if (generation !== state.generation || weweDisabled()) return;
    watchWeweTask(result.task_id);
    await loadWeweTasks(generation);
    if (generation !== state.generation || weweDisabled()) return;
    showNotice("采集任务 #" + result.task_id + " 已开始，可继续使用其他功能");
  }

  async function handleWeweAction(event) {
    const button = event.target.closest("[data-wewe-action]");
    if (!button || !$("weweView").contains(button) || button.disabled) return;
    const action = button.dataset.weweAction;
    const id = Number(button.dataset.id || 0);
    if (action === "open-login") {
      await startWeweLogin();
      return;
    }
    if (action === "retry-accounts") {
      await loadWeweAccounts();
      return;
    }
    if (action === "delete-source" && !window.confirm("确认删除该来源在 doc 中的文章和向量？\n此操作不会删除 WeWe 上游的公众号订阅，且无法撤销。")) return;
    if (action === "delete-article" && !window.confirm("确认删除该文章及其向量？此操作无法撤销。")) return;
    button.disabled = true;
    try {
      if (action === "collect") {
        await startCollection(id);
      } else if (action === "toggle") {
        const subscribed = button.dataset.subscribed === "1";
        await api("/api/wewe/sources/" + id, {
          method: "PATCH",
          body: JSON.stringify({ subscribed: !subscribed }),
        });
        showNotice(subscribed ? "已暂停自动订阅，历史文章仍保留" : "已恢复自动订阅");
        await loadWeweSources();
      } else if (action === "delete-source") {
        await api("/api/wewe/sources/" + id, { method: "DELETE" });
        showNotice("本地来源、文章和向量已删除");
        await Promise.all([loadWeweSources(), loadWeweArticles(), loadWeweTasks()]);
      } else if (action === "delete-article") {
        await api("/api/wewe/articles/" + id, { method: "DELETE" });
        showNotice("文章及其向量已删除");
        await loadWeweArticles();
      }
    } catch (err) {
      showError(err);
    } finally {
      if (document.body.contains(button)) button.disabled = false;
    }
  }

  function clearQrTimers() {
    window.clearInterval(state.qr.countdownTimer);
    window.clearTimeout(state.qr.retryTimer);
    state.qr.countdownTimer = null;
    state.qr.retryTimer = null;
  }

  function setQrExpired() {
    if (state.qr.controller) state.qr.controller.abort();
    $("weweQrStatus").textContent = "二维码已过期，请重新生成后再扫码。";
    $("weweQrCountdown").textContent = "已过期";
  }

  function updateQrCountdown() {
    const seconds = Math.max(0, Math.ceil((state.qr.deadline - Date.now()) / 1000));
    if (!seconds) {
      clearQrTimers();
      setQrExpired();
      return;
    }
    $("weweQrCountdown").textContent = "二维码有效期剩余 " + seconds + " 秒";
  }

  function closeWeweQr() {
    state.qr.generation += 1;
    clearQrTimers();
    if (state.qr.controller) state.qr.controller.abort();
    state.qr.controller = null;
    state.qr.polling = false;
    const oldId = state.qr.id;
    state.qr.id = null;
    $("weweQrBackdrop").classList.remove("active");
    $("weweQrBackdrop").setAttribute("aria-hidden", "true");
    document.body.classList.remove("wewe-modal-open");
    if (oldId) api("/api/wewe/accounts/login-sessions/" + encodeURIComponent(oldId), { method: "DELETE" }).catch(() => {});
  }

  async function pollWeweLogin(loginId, generation) {
    if (state.qr.polling || generation !== state.qr.generation) return;
    state.qr.polling = true;
    try {
      const result = await api("/api/wewe/accounts/login-sessions/" + encodeURIComponent(loginId), {
        signal: state.qr.controller.signal,
      });
      if (generation !== state.qr.generation) return;
      state.qr.failures = 0;
      if (result.status === "waiting") {
        $("weweQrStatus").textContent = result.message || "等待微信扫码确认；扫码后请在手机中确认登录。";
        state.qr.retryTimer = window.setTimeout(() => pollWeweLogin(loginId, generation), 1200);
        return;
      }
      if (result.status === "success") {
        clearQrTimers();
        state.qr.id = null;
        $("weweQrStatus").textContent = "绑定成功：" + (result.account?.name || "微信读书账号");
        $("weweQrCountdown").textContent = "现在可以关闭窗口并粘贴公众号文章链接";
        showNotice("读书账号绑定成功");
        await loadWeweAccounts();
        $("weweShareLink").focus();
        return;
      }
      clearQrTimers();
      $("weweQrStatus").textContent = result.message || "扫码未完成，请重新生成二维码。";
      $("weweQrCountdown").textContent = "本次扫码已结束";
    } catch (err) {
      if (err.name === "AbortError" || generation !== state.qr.generation) return;
      state.qr.failures += 1;
      if (Date.now() >= state.qr.deadline) {
        setQrExpired();
      } else if (state.qr.failures >= 2) {
        $("weweQrStatus").textContent = messageFrom(err) + "。请重新生成二维码后重试。";
        $("weweQrCountdown").textContent = "扫码状态查询已暂停";
      } else {
        $("weweQrStatus").textContent = "状态查询暂时失败，正在自动重试…";
        state.qr.retryTimer = window.setTimeout(() => pollWeweLogin(loginId, generation), 2500);
      }
    } finally {
      state.qr.polling = false;
    }
  }

  async function startWeweLogin() {
    if (weweDisabled()) return;
    closeWeweQr();
    const generation = ++state.qr.generation;
    const backdrop = $("weweQrBackdrop");
    backdrop.classList.add("active");
    backdrop.setAttribute("aria-hidden", "false");
    document.body.classList.add("wewe-modal-open");
    $("weweQrCode").innerHTML = '<div class="wewe-qr-loading">正在生成安全二维码…</div>';
    $("weweQrStatus").textContent = "正在连接微信读书，请稍候…";
    $("weweQrCountdown").textContent = "";
    $("weweQrLink").href = "#";
    $("weweQrRefreshBtn").disabled = true;
    try {
      const session = await api("/api/wewe/accounts/login-sessions", {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (generation !== state.qr.generation) return;
      state.qr.id = session.login_id;
      state.qr.deadline = Date.now() + Number(session.expires_in || 120) * 1000;
      state.qr.failures = 0;
      state.qr.controller = new AbortController();
      $("weweQrCode").innerHTML = session.qr_svg;
      $("weweQrStatus").textContent = "请使用微信扫一扫，并在微信读书中确认登录。";
      const loginUrl = safeHttpUrl(session.scan_url);
      $("weweQrLink").href = loginUrl || "#";
      $("weweQrLink").classList.toggle("hidden", !loginUrl);
      updateQrCountdown();
      state.qr.countdownTimer = window.setInterval(updateQrCountdown, 1000);
      pollWeweLogin(session.login_id, generation);
    } catch (err) {
      if (generation !== state.qr.generation) return;
      $("weweQrCode").innerHTML = '<div class="wewe-qr-loading">二维码暂时不可用</div>';
      $("weweQrStatus").textContent = messageFrom(err);
      $("weweQrCountdown").textContent = "请点击下方按钮重试";
    } finally {
      if (generation === state.qr.generation) $("weweQrRefreshBtn").disabled = false;
    }
  }

  async function subscribeMp(event) {
    event.preventDefault();
    const button = $("weweSubscribeBtn");
    if (button.dataset.busy === "1") return;
    const input = $("weweShareLink");
    const shareLink = input.value.trim();
    if (!shareLink.startsWith("https://mp.weixin.qq.com/s/")) {
      setSubscribeMessage("请粘贴以 https://mp.weixin.qq.com/s/ 开头的公众号文章分享链接。", "err");
      input.focus();
      return;
    }
    button.dataset.busy = "1";
    button.disabled = true;
    button.textContent = "正在识别公众号…";
    setSubscribeMessage("正在识别公众号并创建订阅，请勿重复提交。", "");
    try {
      const result = await api("/api/wewe/subscriptions", {
        method: "POST",
        body: JSON.stringify({ share_link: shareLink, limit: 30 }),
      });
      const sourceNameValue = result.source?.name || "公众号";
      const taskId = result.collection?.task_id;
      input.value = "";
      setSubscribeMessage("已订阅「" + sourceNameValue + "」，采集任务 #" + taskId + " 正在后台运行。", "ok");
      showNotice(result.message || "公众号已订阅，文章正在后台采集");
      if (taskId) watchWeweTask(taskId);
      await Promise.all([loadWeweSources(), loadWeweTasks()]);
      $("weweStepCollect").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      setSubscribeMessage(messageFrom(err), "err");
      showError(err);
    } finally {
      button.dataset.busy = "0";
      button.disabled = false;
      updateSubscribeAvailability();
    }
  }

  async function addAdvancedSource() {
    const generation = state.generation;
    if (weweDisabled()) return;
    const button = $("weweAddBtn");
    const sourceType = $("weweType").value;
    const target = $("weweTarget").value.trim();
    const name = $("weweName").value.trim();
    if (!target) throw new Error(sourceType === "web" ? "请填写网站 URL" : "请填写已有公众号 feed id");
    if (sourceType === "web" && !/^https?:\/\//i.test(target)) throw new Error("请输入以 http:// 或 https:// 开头的网站 URL");
    const payload = { source_type: sourceType, name: name, subscribed: true };
    if (sourceType === "web") payload.target_url = target;
    else payload.upstream_feed_id = target;
    button.disabled = true;
    try {
      const created = await api("/api/wewe/sources", { method: "POST", body: JSON.stringify(payload) });
      if (generation !== state.generation || weweDisabled()) return;
      $("weweTarget").value = "";
      $("weweName").value = "";
      await loadWeweSources();
      if (generation !== state.generation || weweDisabled()) return;
      await startCollection(created.source_id, generation);
    } finally {
      updateFeatureAvailability();
    }
  }

  async function searchWeweImages() {
    const generation = state.generation;
    if (weweDisabled()) return;
    const button = $("weweImageSearchBtn");
    const query = $("weweImageQuery").value.trim();
    if (!query) throw new Error("请输入图片描述");
    button.disabled = true;
    button.textContent = "搜索中…";
    try {
      const data = await api("/api/wewe/images/search", {
        method: "POST",
        body: JSON.stringify({ query: query, top_k: 8 }),
      });
      if (generation !== state.generation || weweDisabled()) return;
      const box = $("weweImageResults");
      if (!data.hits || !data.hits.length) {
        box.innerHTML = '<div class="empty-state">没有命中的图片，请换一组描述词。</div>';
        return;
      }
      box.innerHTML = data.hits.map((hit) => {
        const imageUrl = safeHttpUrl(hit.image_file_url);
        return '<figure class="wewe-image-card">' +
          (imageUrl ? '<img src="' + escapeHtml(imageUrl) + '" alt="' + escapeHtml(hit.description || hit.article_title || "文章图片") + '" loading="lazy">' : "") +
          '<figcaption><div class="wewe-item-title">' + escapeHtml(hit.article_title || "文章图片") + '</div>' +
          '<div>' + escapeHtml(hit.description || "暂无图片说明") + '</div></figcaption>' +
        '</figure>';
      }).join("");
    } finally {
      updateFeatureAvailability();
      button.textContent = "搜索图片";
    }
  }

  $("weweView").addEventListener("click", (event) => {
    handleWeweAction(event).catch(showError);
  });
  $("weweRefreshBtn").onclick = () => loadWeweAll().catch(showError);
  $("weweExportOpml").addEventListener("click", (event) => {
    if (weweDisabled()) event.preventDefault();
  });
  $("weweArticleRefreshBtn").onclick = () => loadWeweArticles().catch(showError);
  $("weweTaskRefreshBtn").onclick = () => loadWeweTasks().catch(showError);
  $("weweArticleSource").onchange = () => loadWeweArticles().catch(showError);
  $("weweSubscribeForm").addEventListener("submit", (event) => subscribeMp(event).catch(showError));
  $("weweAddBtn").onclick = () => addAdvancedSource().catch(showError);
  $("weweImageSearchBtn").onclick = () => searchWeweImages().catch(showError);
  $("weweImageQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchWeweImages().catch(showError);
  });
  $("weweLoginBtn").onclick = () => startWeweLogin().catch(showError);
  $("weweQrRefreshBtn").onclick = () => startWeweLogin().catch(showError);
  $("weweQrCloseBtn").onclick = closeWeweQr;
  $("weweQrBackdrop").addEventListener("click", (event) => {
    if (event.target === $("weweQrBackdrop")) closeWeweQr();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("weweQrBackdrop").classList.contains("active")) closeWeweQr();
  });

  updateFeatureAvailability();
  window.loadWeweAll = loadWeweAll;
  window.startWeweLogin = startWeweLogin;
  window.__weweConsoleState = state;
})();
