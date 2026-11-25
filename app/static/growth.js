"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  knowledgeBaseId: null,
  workspace: null,
  activeDraft: null,
  selectedBriefId: null,
  profileDirty: false,
  profileVersion: 0,
  articleVersion: 0,
  editorVersion: 0,
  editorDirty: false,
  workspaceRequestId: 0,
  refreshRetryMessage: "",
  refreshRecovery: null,
  userId: null,
  pending: new Set(),
  operationIds: new Map(),
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function showNotice(message, isError = false) {
  const notice = $("notice");
  notice.textContent = message || "";
  notice.classList.toggle("error", Boolean(isError));
}

function setBusy(key, message) {
  if (key) state.pending.add(key);
  const busy = state.pending.size > 0;
  $("busyBar").hidden = !busy;
  $("busyBar").textContent = busy ? (message || "正在处理，请保持页面打开…") : "";
  ["generateStyleBtn", "generateBriefBtn", "generateDraftBtn", "saveDraftBtn", "refreshWorkspaceBtn"].forEach((id) => {
    if ($(id)) $(id).disabled = busy;
  });
}

function clearBusy(key) {
  state.pending.delete(key);
  if (!state.pending.size) setBusy(null, "");
}

async function api(path, options = {}) {
  const request = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (options.body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, request);
  } catch (cause) {
    const error = new Error("网络连接失败，请检查网络后重试。");
    error.cause = cause;
    error.isNetworkError = true;
    throw error;
  }
  if (response.status === 401) {
    window.location.assign("/?next=/growth");
    throw new Error("登录已失效，正在返回登录页");
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    const detail = data.detail;
    const error = new Error(typeof detail === "string" ? detail : `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function runTask(key, message, task) {
  if (state.pending.size) {
    showNotice("已有 AI 任务正在执行，请等待当前结果返回。", true);
    return null;
  }
  setBusy(key, message);
  showNotice("");
  try {
    return await task();
  } catch (error) {
    showNotice(error.userMessage || error.message || "操作失败，请稍后重试", true);
    return null;
  } finally {
    clearBusy(key);
  }
}

function createOperationId() {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID();
  }
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  throw new Error("当前浏览器无法安全生成操作编号，请使用支持加密随机数的新版浏览器。");
}

const OPERATION_STORAGE_KEY = "content-growth.pending-operations.v2";

function operationStorageError(message) {
  return new Error(`${message}。为避免重复扣费，本次尚未请求 AI、未扣费；请勿反复尝试，可联系客服核查原付费任务。`);
}

function readPendingOperations() {
  let raw;
  try {
    const storage = globalThis.sessionStorage;
    if (!storage || typeof storage.getItem !== "function") throw new Error("storage unavailable");
    raw = storage.getItem(OPERATION_STORAGE_KEY);
  } catch (_error) {
    throw operationStorageError("无法读取浏览器中的付费恢复记录");
  }
  if (raw === null) return {};
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_error) {
    throw operationStorageError("浏览器中的付费恢复记录已损坏");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw operationStorageError("浏览器中的付费恢复记录格式异常");
  }
  // Unknown or malformed entries may hold the only durable ID of an already
  // committed paid request. Never reinterpret them as an empty store.
  for (const [cacheKey, record] of Object.entries(parsed)) {
    if (!/^\d+:[a-f0-9]{64}$/.test(cacheKey) || !record || typeof record !== "object" ||
        record.cacheKey !== cacheKey || !Number.isInteger(record.userId) || record.userId <= 0 ||
        !Number.isInteger(record.knowledgeBaseId) || record.knowledgeBaseId <= 0 ||
        typeof record.operationId !== "string" || !record.operationId ||
        typeof record.operation !== "string" || typeof record.path !== "string") {
      throw operationStorageError("浏览器中的付费恢复记录不完整");
    }
  }
  return parsed;
}

function writePendingOperations(records) {
  try {
    const storage = globalThis.sessionStorage;
    if (!storage) return false;
    if (Object.keys(records).length) {
      storage.setItem(OPERATION_STORAGE_KEY, JSON.stringify(records));
    } else {
      storage.removeItem(OPERATION_STORAGE_KEY);
    }
    return true;
  } catch (_error) {
    return false;
  }
}

function restorePendingOperations() {
  const records = readPendingOperations();
  const restored = new Map();
  for (const [cacheKey, record] of Object.entries(records)) {
    if (record.userId === state.userId) restored.set(cacheKey, record.operationId);
  }
  state.operationIds = restored;
}

function matchingPendingOperation(record, cacheKey, operation, path, payload, operationId, userId) {
  return Boolean(record && record.cacheKey === cacheKey && record.userId === userId &&
    record.knowledgeBaseId === Number(payload.knowledge_base_id) && record.operationId === operationId &&
    record.operation === operation && record.path === path);
}

function persistPendingOperation(cacheKey, operation, path, payload, operationId, userId) {
  const records = readPendingOperations();
  records[cacheKey] = {
    cacheKey, userId, knowledgeBaseId: Number(payload.knowledge_base_id),
    operation, path, operationId,
  };
  return writePendingOperations(records);
}

function clearPendingOperation(cacheKey) {
  const records = readPendingOperations();
  delete records[cacheKey];
  if (!writePendingOperations(records)) return false;
  // A restricted WebView can silently ignore removeItem. Keeping the old ID
  // is safe (the server replays it); deleting its only memory handle is not.
  if (readPendingOperations()[cacheKey]) return false;
  state.operationIds.delete(cacheKey);
  return true;
}

function currentOperationUserId() {
  const userId = Number(state.userId ?? state.workspace?.user_id);
  if (!Number.isInteger(userId) || userId <= 0) {
    throw new Error("账号身份尚未确认，本次尚未请求 AI、未扣费。请先刷新工作台后再试。");
  }
  return userId;
}

async function createOperationCacheKey(operation, path, payload, userId) {
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi?.subtle || typeof globalThis.TextEncoder !== "function") {
    throw new Error("当前浏览器无法安全生成请求摘要，本次尚未请求 AI、未扣费。请使用支持安全摘要的现代浏览器。");
  }
  const source = JSON.stringify({userId, operation, path, payload});
  const digest = await cryptoApi.subtle.digest("SHA-256", new TextEncoder().encode(source));
  const hash = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${userId}:${hash}`;
}

function paidResultRecovery(cacheKey, operation, result, error) {
  const artifact = result?.draft || result?.brief || null;
  const label = operation === "draft" ? "草稿" : operation === "brief" ? "选题简报" : "风格草稿";
  const idLabel = artifact?.id ? ` #${artifact.id}` : "";
  const message = error.paidResultMessage ||
    `${label}${idLabel} 已生成并扣费，但结果展示或本地恢复记录确认失败。结果仍保存在服务端，请点击“仅刷新工作台”，不要重复生成。`;
  state.refreshRecovery = {
    cacheKey,
    operation,
    result,
    userId: currentOperationUserId(),
    knowledgeBaseId: Number(state.workspace?.knowledge_base_id ?? state.knowledgeBaseId),
  };
  state.refreshRetryMessage = message;
  setWorkspaceRefreshRetry(true);
  error.paidResultRecovery = true;
  error.userMessage = message;
  return error;
}

async function paidApi(operation, path, payload, applyResult) {
  // Only a SHA-256 request digest and account-scoped operation metadata are
  // persisted; topics, instructions and other payload text stay out of storage.
  const userId = currentOperationUserId();
  const cacheKey = await createOperationCacheKey(operation, path, payload, userId);
  if (state.refreshRecovery && state.refreshRecovery.cacheKey !== cacheKey) {
    throw new Error("上一笔已扣费结果尚未完成列表确认。请先点击“仅刷新工作台”，不会重复生成或扣费。");
  }
  let operationId = state.operationIds.get(cacheKey);
  if (!operationId) {
    restorePendingOperations();
    operationId = state.operationIds.get(cacheKey);
  }
  let record = readPendingOperations()[cacheKey];
  if (!operationId) operationId = createOperationId();
  if (!matchingPendingOperation(record, cacheKey, operation, path, payload, operationId, userId)) {
    // Existing in-memory IDs must be re-persisted; ambiguous records fail closed.
    if (record || !persistPendingOperation(cacheKey, operation, path, payload, operationId, userId)) {
      throw new Error("浏览器无法确认防重复扣费任务编号，本次尚未请求 AI、未扣费。请允许会话存储后重试；不要清除尚未恢复的付费任务。");
    }
    record = readPendingOperations()[cacheKey];
  }
  if (!matchingPendingOperation(record, cacheKey, operation, path, payload, operationId, userId)) {
    throw new Error("浏览器未能持久化防重复扣费任务编号，本次尚未请求 AI、未扣费。请更换浏览器后重试。");
  }
  state.operationIds.set(cacheKey, operationId);
  const result = await api(path, {
    method: "POST",
    body: { ...payload, operation_id: operationId },
  });
  // Clear only after the paid result was applied. Render/confirmation errors
  // keep the ID and expose a GET-only recovery instead of inviting another POST.
  try {
    if (applyResult) await applyResult(result);
  } catch (error) {
    throw paidResultRecovery(cacheKey, operation, result, error);
  }
  try {
    if (!clearPendingOperation(cacheKey)) throw new Error("本地付费恢复记录尚未确认清理");
  } catch (error) {
    throw paidResultRecovery(cacheKey, operation, result, error);
  }
  if (state.refreshRecovery?.cacheKey === cacheKey) {
    state.refreshRecovery = null;
    state.refreshRetryMessage = "";
    setWorkspaceRefreshRetry(false);
  }
  return result;
}

function stableJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) =>
    `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
}

function assertPaidRecoveryConfirmed(recovery, loaded) {
  if (!loaded || recovery.userId !== state.userId) {
    throw new Error("账号或工作台响应已变化，尚未确认原付费结果");
  }
  const originalKb = Number(recovery.knowledgeBaseId);
  if (!Number.isInteger(originalKb) || originalKb <= 0 ||
      originalKb !== Number(state.knowledgeBaseId) ||
      originalKb !== Number(loaded.knowledge_base_id)) {
    throw new Error("当前不是发起付费时的知识库，原恢复记录仍保留；请切回原知识库后仅刷新。");
  }

  if (recovery.operation === "draft" || recovery.operation === "brief") {
    const isDraft = recovery.operation === "draft";
    const label = isDraft ? "草稿" : "简报";
    const artifact = isDraft ? recovery.result?.draft : recovery.result?.brief;
    const artifactId = Number(artifact?.id);
    if (!Number.isInteger(artifactId) || artifactId <= 0) {
      throw new Error(`付费${label}响应缺少有效正整数编号，恢复记录仍保留，请由客服核查该操作。`);
    }
    const items = isDraft ? loaded.drafts : loaded.briefs;
    if (!Array.isArray(items) || !items.some((item) => Number(item.id) === artifactId)) {
      throw new Error(`原知识库列表中尚未确认${label} #${artifactId}，付费恢复记录仍保留`);
    }
    return;
  }

  if (recovery.operation === "style") {
    const paidStyle = recovery.result?.style_profile;
    const savedStyle = loaded.profile?.style_draft;
    if (!paidStyle || typeof paidStyle !== "object" || Array.isArray(paidStyle) ||
        !Object.keys(paidStyle).length || !savedStyle || typeof savedStyle !== "object" ||
        stableJson(paidStyle) !== stableJson(savedStyle)) {
      throw new Error("服务端风格草稿尚未与本次付费结果匹配，恢复记录仍保留。");
    }
    return;
  }
  throw new Error("未知付费操作类型，恢复记录仍保留，请联系客服核查。");
}

function setWorkspaceRefreshRetry(visible) {
  $("refreshWorkspaceBtn").hidden = !visible;
}

async function refreshAfterPaidSuccess(successMessage) {
  try {
    await loadWorkspace({ preserveEditor: true });
    state.refreshRetryMessage = "";
    setWorkspaceRefreshRetry(false);
    showNotice(successMessage);
  } catch (error) {
    state.refreshRetryMessage = successMessage;
    setWorkspaceRefreshRetry(true);
    showNotice(`${successMessage} 已生成、已扣费；仅列表刷新失败（${error.message || "未知错误"}）。结果已保存在本页，请点击“仅刷新工作台”，不要重复生成。`, true);
  }
}

function applyStyleGeneration(result) {
  const style = result.style_profile;
  const profile = { ...(state.workspace?.profile || {}), style_draft: style };
  state.workspace = { ...(state.workspace || {}), profile };
  $("draftStyleCard").textContent = styleText(style) || "尚未生成";
  $("activateStyleBtn").hidden = !style;
}

function applyBriefGeneration(result) {
  const brief = result.brief;
  const briefs = [brief, ...(state.workspace?.briefs || []).filter((item) => Number(item.id) !== Number(brief.id))];
  state.workspace = { ...(state.workspace || {}), briefs };
  renderBriefs();
}

function applyDraftGeneration(result) {
  state.activeDraft = result.draft;
  state.editorDirty = false;
  state.editorVersion += 1;
  renderEditor();
}

async function applyDraftGenerationGuarded(result, versionAtRequest, activeDraftIdAtRequest) {
  if (state.editorVersion === versionAtRequest &&
      (state.activeDraft?.id ?? null) === activeDraftIdAtRequest) {
    applyDraftGeneration(result);
    return null;
  }
  const draftId = Number(result.draft?.id);
  if (!Number.isInteger(draftId) || draftId <= 0) {
    throw new Error("付费结果缺少草稿编号；操作恢复记录已保留，请重试以读取原结果。");
  }
  // The paid draft is already durable. Put it in the visible draft list while
  // preserving the editor, then let paidApi clear the recovered operation.
  try {
    const workspace = await loadWorkspace({ preserveEditor: true });
    if (!workspace?.drafts?.some((item) => Number(item.id) === draftId)) {
      throw new Error("列表中尚未出现新草稿");
    }
  } catch (cause) {
    const error = new Error(`草稿 #${draftId} 已生成并扣费；当前输入未覆盖，但列表确认刷新失败（${cause.message || "未知错误"}）。请点击“仅刷新工作台”，不要重复生成。`);
    error.paidResultMessage = error.message;
    throw error;
  }
  return draftId;
}

function switchView(viewId) {
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== viewId; });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === viewId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

function localDate() {
  const date = new Date();
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 10);
}

function numberValue(id) {
  const input = $(id);
  if (input.value.trim() === "") return null;
  const value = input.valueAsNumber;
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : null;
}

function percent(value) {
  return value === null || value === undefined ? "—" : `${(Number(value) * 100).toFixed(2)}%`;
}

function renderQuota() {
  if (!state.workspace) return;
  const billing = state.workspace.billing;
  const remaining = billing.remaining_calls === null ? "不限量" : `余 ${billing.remaining_calls} 次`;
  $("quotaBadge").textContent = `${billing.plan_name} · ${remaining}`;
  $("quotaBadge").title = "风格分析、选题简报、证据化成稿每次成功扣 1 次；失败退回";
}

function styleText(profile) {
  if (!profile) return "";
  const lines = [
    `表达声音：${profile.voice || "未说明"}`,
    `语气：${profile.tone || "未说明"}`,
    `句式：${profile.sentence_style || "未说明"}`,
    `结构偏好：${(profile.structure_preferences || []).join("；") || "未说明"}`,
    `建议保留：${(profile.do || []).join("；") || "未说明"}`,
    `建议避免：${(profile.avoid || []).join("；") || "未说明"}`,
    `样本数：${profile.sample_count || 0} · 置信度：${profile.confidence || "未说明"}`,
  ];
  if (profile.evidence) lines.push(`分析依据：${profile.evidence}`);
  return lines.join("\n");
}

function renderSetup() {
  const { profile, own_articles: ownArticles = [] } = state.workspace;
  if (!state.profileDirty) {
    $("audienceInput").value = profile?.audience || "";
    $("goalInput").value = profile?.goal || "";
    $("ctaInput").value = profile?.cta || "";
    $("forbiddenInput").value = profile?.forbidden || "";
  }
  $("activeStyleCard").textContent = styleText(profile?.style_profile) || "尚未激活";
  $("draftStyleCard").textContent = styleText(profile?.style_draft) || "尚未生成";
  $("activateStyleBtn").hidden = !profile?.style_draft;

  const list = clear($("ownArticleList"));
  if (!ownArticles.length) {
    list.append(element("div", "empty-state", "尚未录入本人文章。录入后可用于风格分析与效果数据关联。"));
    return;
  }
  ownArticles.forEach((article) => {
    const item = element("article", "compact-item");
    item.append(element("strong", "", article.title));
    const count = article.features?.char_count || 0;
    item.append(element("small", "", `${count} 字 · ${article.published_at || "未填发布日期"}`));
    list.append(item);
  });
}

function renderReferences() {
  const previous = new Set(
    [...$("referenceList").querySelectorAll("input:checked")].map((input) => Number(input.value)),
  );
  const list = clear($("referenceList"));
  const references = state.workspace.reference_articles || [];
  if (!references.length) {
    list.append(element("div", "empty-state", "当前知识库还没有参考文章。请先到“文档处理 → 公众号 / RSS 采集”完成采集。"));
    return;
  }
  references.forEach((article) => {
    const label = element("label", "reference-item");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.value = String(article.id);
    checkbox.checked = previous.has(Number(article.id));
    checkbox.setAttribute("aria-label", `选择参考文章：${article.title || "无标题"}`);
    const text = element("span", "", `${article.title || "无标题"}\n${article.author || "未知作者"} · ${article.published_at || article.created_at || "未知日期"}`);
    label.append(checkbox, text);
    list.append(label);
  });
}

function refTags(refs) {
  const wrap = element("div", "ref-tags");
  (refs || []).forEach((ref) => wrap.append(element("span", "ref-tag", ref)));
  return wrap;
}

function renderBriefs() {
  const results = clear($("briefResults"));
  const briefs = state.workspace.briefs || [];
  if (!briefs.length) {
    results.append(element("div", "empty-state", "还没有选题简报。生成后，每个观察与选题都会保留来源编号。"));
    return;
  }
  briefs.forEach((brief) => {
    const card = element("article", "card brief-card");
    card.append(element("span", "eyebrow", `${brief.analysis_mode === "promotion" ? "推广要素观察" : "内容结构分析"} · ${brief.created_at || ""}`));
    card.append(element("h2", "", brief.focus || "近期内容选题"));
    card.append(element("p", "", brief.content.summary || ""));
    (brief.content.content_observations || []).forEach((observation) => {
      const block = element("div", "observation");
      block.append(element("p", "", observation.observation), refTags(observation.evidence_refs));
      card.append(block);
    });
    (brief.content.topics || []).forEach((topic) => {
      const block = element("section", "topic-card");
      block.append(element("h3", "", topic.title));
      block.append(element("p", "", `角度：${topic.angle || "未说明"}`));
      block.append(element("p", "", `读者价值：${topic.audience_value || "未说明"}`));
      if (topic.why_now) block.append(element("p", "", `为什么现在写：${topic.why_now}`));
      if (topic.risk) block.append(element("p", "hint", `风险：${topic.risk}`));
      block.append(refTags(topic.evidence_refs));
      const useButton = element("button", "secondary", "用这个选题写草稿");
      useButton.type = "button";
      useButton.addEventListener("click", () => {
        state.selectedBriefId = Number(brief.id);
        $("draftTopicInput").value = topic.title;
        $("selectedBriefNote").hidden = false;
        $("selectedBriefNote").textContent = `已关联简报 #${brief.id}；成稿会沿用该简报的来源快照。`;
        switchView("writeView");
      });
      block.append(useButton);
      card.append(block);
    });
    (brief.content.data_limitations || []).forEach((limitation) => {
      card.append(element("p", "hint", `数据边界：${limitation}`));
    });
    results.append(card);
  });
}

function renderDraftList() {
  const list = clear($("draftList"));
  const drafts = state.workspace.drafts || [];
  if (!drafts.length) {
    list.append(element("div", "empty-state", "还没有文章草稿。"));
    return;
  }
  drafts.forEach((draft) => {
    const item = element("article", "compact-item");
    item.append(element("strong", "", draft.title || "无标题草稿"));
    item.append(element("small", "", `${draft.status} · ${draft.updated_at || draft.created_at || ""}`));
    const open = element("button", "secondary", "打开并编辑");
    open.type = "button";
    open.addEventListener("click", () => loadDraft(draft.id));
    item.append(open);
    list.append(item);
  });
}

function safeSourceLink(source) {
  if (!source.url) return null;
  try {
    const url = new URL(source.url);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    const link = element("a", "", "查看原文");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (_error) {
    return null;
  }
}

function renderEvidence(draft) {
  const citations = clear($("citationList"));
  const citationMap = draft.citation_map || [];
  const invalidated = draft.evidence_invalidated === true;
  if (invalidated) {
    citations.append(element("p", "hint evidence-invalidated", "正文已人工修改，原 AI 引用映射与待核实内容已作废；下方资料仅用于追溯，请按当前正文重新核对来源。"));
  } else if (!citationMap.length) {
    citations.append(element("p", "hint", "当前草稿没有引用映射，请人工确认是否均为观点或经验性表达。"));
  }
  citationMap.forEach((citation) => {
    const item = element("div", "citation-item");
    item.append(element("p", "", citation.claim), refTags(citation.evidence_refs));
    citations.append(item);
  });
  (draft.sources || []).forEach((source) => {
    const item = element("div", "source-card");
    item.append(element("strong", "", `[${source.ref_id}] ${source.title || "无标题来源"}`));
    if (source.excerpt) item.append(element("p", "", source.excerpt.slice(0, 220)));
    const link = safeSourceLink(source);
    if (link) item.append(link);
    citations.append(item);
  });
  const unresolved = clear($("unresolvedFacts"));
  const facts = draft.unresolved_facts || [];
  if (invalidated) {
    unresolved.append(element("p", "hint evidence-invalidated", "正文已人工修改，旧待核实项已作废；请按当前正文重新检查事实、数字和引语。"));
  } else {
    unresolved.append(element("p", "", facts.length ? facts.map((fact, index) => `${index + 1}. ${fact}`).join("\n") : "模型未列出待核实事实；发布前仍需人工检查。"));
  }
  const risks = clear($("riskNotes"));
  const notes = draft.risk_notes || [];
  risks.append(element("p", "", notes.length ? notes.map((note, index) => `${index + 1}. ${note}`).join("\n") : "未列出额外风险说明。"));
}

function markEditorInput() {
  state.editorVersion += 1;
  state.editorDirty = true;
}

function renderEditor() {
  const draft = state.activeDraft;
  if (!draft) {
    $("draftEditor").hidden = true;
    return;
  }
  $("draftEditor").hidden = false;
  $("editorTitleInput").value = draft.title || "";
  $("editorBodyInput").value = draft.body_markdown || "";
  $("draftStatus").textContent = draft.status || "draft";
  const options = clear($("titleOptions"));
  (draft.title_options || []).forEach((title) => {
    const button = element("button", "title-option", title);
    button.type = "button";
    button.addEventListener("click", () => {
      $("editorTitleInput").value = title;
      markEditorInput();
    });
    options.append(button);
  });
  ["Markdown", "Txt", "Html"].forEach((name) => {
    const format = name.toLowerCase();
    $(`export${name}Link`).href = `/api/growth/drafts/${draft.id}/export?format=${format}`;
  });
  renderEvidence(draft);
}

function renderMetrics() {
  const ownArticles = state.workspace.own_articles || [];
  const select = clear($("metricArticleSelect"));
  const selectedBefore = state.workspace._selectedMetricArticle;
  ownArticles.forEach((article) => {
    const option = element("option", "", article.title);
    option.value = String(article.id);
    option.selected = Number(selectedBefore) === Number(article.id);
    select.append(option);
  });
  if (!ownArticles.length) {
    const option = element("option", "", "请先录入本人文章");
    option.value = "";
    select.append(option);
  }
  const list = clear($("metricList"));
  const metrics = state.workspace.metrics || [];
  if (!metrics.length) {
    list.append(element("div", "empty-state", "还没有真实效果数据。录入后，这里会展示描述性比率与样本提示。"));
    return;
  }
  const articleNames = new Map(ownArticles.map((article) => [Number(article.id), article.title]));
  metrics.forEach((metric) => {
    const card = element("article", "metric-card");
    const head = element("div", "metric-card-head");
    head.append(element("h2", "", articleNames.get(Number(metric.own_article_id)) || `文章 #${metric.own_article_id}`));
    head.append(element("span", "status-pill", `${metric.metric_date} · ${metric.window}`));
    card.append(head);
    const provenance = [metric.account_label || "未记录账号", metric.source_name || "未记录来源"];
    if (metric.campaign_id) provenance.push(`活动 ${metric.campaign_id}`);
    card.append(element("p", "hint", provenance.join(" · ")));
    const values = element("div", "metric-values");
    const pairs = [
      ["阅读", metric.reads], ["分享", metric.shares], ["关注", metric.new_follows],
      ["点击", metric.landing_clicks], ["注册", metric.registrations], ["付费", metric.payments],
    ];
    pairs.forEach(([label, value]) => {
      const block = element("div", "metric-value");
      block.append(element("strong", "", value === null || value === undefined ? "—" : value), element("span", "", label));
      values.append(block);
    });
    card.append(values);
    const rates = metric.observed_rates || {};
    card.append(element("div", "rate-row", `分享率 ${percent(rates.share_rate)} · 落地页点击率 ${percent(rates.landing_click_rate)} · 点击注册率 ${percent(rates.registration_per_click)} · 阅读付费率 ${percent(rates.payment_per_read)} · 注册付费率 ${percent(rates.payment_per_registration)}`));
    card.append(element("p", "sample-note", metric.sample_note));
    if (metric.rate_note) card.append(element("p", "sample-note", metric.rate_note));
    list.append(card);
  });
}

function renderWorkspace() {
  renderQuota();
  renderSetup();
  renderReferences();
  renderBriefs();
  renderDraftList();
  renderMetrics();
}

function clearBriefAssociation() {
  state.selectedBriefId = null;
  $("selectedBriefNote").hidden = true;
  $("selectedBriefNote").textContent = "";
}

function validateWorkspace(data, knowledgeBaseId) {
  if (!data || typeof data !== "object" || !("profile" in data) || !data.billing) {
    throw new Error("知识库工作台响应不完整，未切换当前数据。");
  }
  if (data.knowledge_base_id !== undefined && Number(data.knowledge_base_id) !== knowledgeBaseId) {
    throw new Error("知识库响应与选择不一致，已保留原工作台。");
  }
  for (const key of ["own_articles", "reference_articles", "briefs", "drafts", "metrics"]) {
    if (!Array.isArray(data[key])) throw new Error(`知识库工作台缺少 ${key} 列表，未切换当前数据。`);
  }
}

async function loadWorkspace({
  preserveEditor = false,
  knowledgeBaseId = state.knowledgeBaseId,
  switchKnowledgeBase = false,
} = {}) {
  const targetId = Number(knowledgeBaseId);
  if (!Number.isInteger(targetId) || targetId <= 0) return null;
  const requestId = ++state.workspaceRequestId;
  const editorVersionAtRequest = state.editorVersion;
  const editorDraftIdAtRequest = state.activeDraft?.id ?? null;
  const data = await api(`/api/growth/workspace?knowledge_base_id=${encodeURIComponent(targetId)}`);

  // A slower response must never commit over a newer KB/workspace request.
  if (requestId !== state.workspaceRequestId) return null;
  if (!switchKnowledgeBase && state.knowledgeBaseId !== targetId) return null;
  validateWorkspace(data, targetId);
  const responseUserId = Number(data.user_id);
  if (!Number.isInteger(responseUserId) || responseUserId <= 0) {
    throw new Error("工作台响应缺少账号作用域，已保留原状态且不会发起付费请求。");
  }
  if (switchKnowledgeBase && (
    state.editorVersion !== editorVersionAtRequest
    || (state.activeDraft?.id ?? null) !== editorDraftIdAtRequest
  )) {
    throw new Error("切换期间编辑器已有新内容，知识库尚未切换，当前输入已保留；请先保存或复制后再切换。");
  }
  if (state.userId !== null && state.userId !== responseUserId) {
    // Commit a new account scope only after every response/editor guard passed.
    state.operationIds = new Map();
    state.refreshRecovery = null;
    setWorkspaceRefreshRetry(false);
  }
  state.userId = responseUserId;

  // Take the editor snapshot after the await. If input arrived while the GET
  // was pending, skip editor rendering entirely instead of restoring old text.
  const editorChanged = preserveEditor && (
    state.editorVersion !== editorVersionAtRequest
    || (state.activeDraft?.id ?? null) !== editorDraftIdAtRequest
  );
  const editorSnapshot = preserveEditor && !editorChanged && state.activeDraft ? {
    draft: state.activeDraft,
    title: $("editorTitleInput").value,
    body: $("editorBodyInput").value,
    dirty: state.editorDirty,
  } : null;

  if (switchKnowledgeBase) {
    state.knowledgeBaseId = targetId;
    state.profileDirty = false;
    clearBriefAssociation();
    clear($("referenceList"));
    $("knowledgeBaseSelect").value = String(targetId);
  }
  state.workspace = data;
  state.workspace._selectedMetricArticle = switchKnowledgeBase ? "" : $("metricArticleSelect").value;
  if (!preserveEditor) {
    state.activeDraft = null;
    state.editorDirty = false;
  }
  renderWorkspace();

  if (editorChanged) return data;
  if (editorSnapshot) {
    state.activeDraft = { ...editorSnapshot.draft, title: editorSnapshot.title, body_markdown: editorSnapshot.body };
    state.editorDirty = editorSnapshot.dirty;
  }
  renderEditor();
  return data;
}

async function loadDraft(draftId) {
  if (state.editorDirty && !window.confirm("当前草稿有未保存修改。打开另一草稿会放弃这些修改，是否继续？")) return;
  await runTask("load-draft", "正在读取草稿…", async () => {
    const versionAtRequest = state.editorVersion;
    const activeDraftIdAtRequest = state.activeDraft?.id ?? null;
    const data = await api(`/api/growth/drafts/${draftId}`);
    if (state.editorVersion !== versionAtRequest ||
        (state.activeDraft?.id ?? null) !== activeDraftIdAtRequest) {
      showNotice(`草稿 #${draftId} 已读取，但等待期间编辑器已有新内容；为避免丢失，本次未覆盖当前输入。请先保存或复制当前内容，再重新打开。`);
      return;
    }
    state.activeDraft = data.draft;
    state.editorDirty = false;
    state.editorVersion += 1;
    renderEditor();
    switchView("writeView");
    showNotice(`已打开草稿 #${draftId}`);
  });
}

async function initialise() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  ["audienceInput", "goalInput", "ctaInput", "forbiddenInput"].forEach((id) => {
    $(id).addEventListener("input", () => {
      state.profileDirty = true;
      state.profileVersion += 1;
    });
  });
  ["ownTitleInput", "ownBodyInput", "ownUrlInput", "ownPublishedInput"].forEach((id) => {
    $(id).addEventListener("input", () => { state.articleVersion += 1; });
  });
  ["editorTitleInput", "editorBodyInput"].forEach((id) => {
    $(id).addEventListener("input", markEditorInput);
  });
  $("metricDateInput").value = localDate();
  $("knowledgeBaseSelect").addEventListener("change", async () => {
    const select = $("knowledgeBaseSelect");
    const targetId = Number(select.value);
    const previousId = state.knowledgeBaseId;
    if (state.pending.size) {
      select.value = String(previousId || "");
      showNotice("已有任务正在执行，知识库尚未切换；请等待当前任务结束。", true);
      return;
    }
    if (!Number.isInteger(targetId) || targetId <= 0 || targetId === previousId) {
      select.value = String(previousId || "");
      return;
    }
    if (state.editorDirty && !window.confirm("当前草稿有未保存修改，切换知识库会丢弃编辑状态；建议先取消并保存。是否继续？")) {
      select.value = String(previousId || "");
      showNotice("已取消知识库切换，当前未保存内容仍保留。", true);
      return;
    }
    // Keep the committed KB and its visible selection together while loading.
    select.value = String(previousId || "");
    await runTask("change-kb", "正在校验并切换知识库…", async () => {
      const loaded = await loadWorkspace({ knowledgeBaseId: targetId, switchKnowledgeBase: true });
      if (!loaded) throw new Error("知识库切换请求已过期，请重新选择。");
      showNotice("知识库已切换；上一知识库的关联简报选择已清空。");
    });
    if (state.knowledgeBaseId !== targetId) select.value = String(state.knowledgeBaseId || "");
  });

  $("refreshWorkspaceBtn").addEventListener("click", async () => {
    await runTask("refresh-workspace", "正在仅刷新工作台列表（不会重复扣费）…", async () => {
      try {
        const loaded = await loadWorkspace({ preserveEditor: true });
        const recovery = state.refreshRecovery;
        if (recovery) {
          assertPaidRecoveryConfirmed(recovery, loaded);
          if (!clearPendingOperation(recovery.cacheKey)) {
            throw new Error("本地付费恢复记录尚未确认清理");
          }
          state.refreshRecovery = null;
        }
        let successMessage = state.refreshRetryMessage;
        if (recovery) {
          const label = recovery.operation === "draft" ? "草稿" : recovery.operation === "brief" ? "简报" : "风格草稿";
          const artifact = recovery.result?.draft || recovery.result?.brief;
          const idLabel = artifact?.id ? ` #${artifact.id}` : "";
          successMessage = `${label}${idLabel} 已在原知识库确认，恢复完成；人工编辑内容保持不变。`;
        }
        state.refreshRetryMessage = "";
        setWorkspaceRefreshRetry(false);
        showNotice(`${successMessage || "工作台列表已刷新。"} 本次仅执行列表查询，未重复生成或扣费。`);
      } catch (error) {
        setWorkspaceRefreshRetry(true);
        showNotice(`已生成结果仍保留且不会重复扣费；仅列表刷新失败（${error.message || "未知错误"}），请再次点击“仅刷新工作台”。`, true);
      }
    });
  });

  $("profileForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await runTask("profile", "正在保存账号定位…", async () => {
      const versionAtSubmit = state.profileVersion;
      await api("/api/growth/profile", { method: "PUT", body: {
        knowledge_base_id: state.knowledgeBaseId,
        audience: $("audienceInput").value,
        goal: $("goalInput").value,
        cta: $("ctaInput").value,
        forbidden: $("forbiddenInput").value,
      } });
      if (state.profileVersion === versionAtSubmit) state.profileDirty = false;
      await loadWorkspace({ preserveEditor: true });
      showNotice(state.profileDirty
        ? "提交时的账号定位已保存；之后输入的新内容尚未保存。"
        : "账号定位已保存到服务端。");
    });
  });

  $("ownArticleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await runTask("own-article", "正在保存本人文章…", async () => {
      const versionAtSubmit = state.articleVersion;
      await api("/api/growth/own-articles", { method: "POST", body: {
        knowledge_base_id: state.knowledgeBaseId,
        title: $("ownTitleInput").value,
        body: $("ownBodyInput").value,
        url: $("ownUrlInput").value,
        published_at: $("ownPublishedInput").value,
      } });
      if (state.articleVersion === versionAtSubmit) $("ownArticleForm").reset();
      await loadWorkspace({ preserveEditor: true });
      showNotice(state.articleVersion === versionAtSubmit
        ? "本人文章已保存，可用于风格分析和效果数据关联。"
        : "提交时的本人文章已保存；表单里之后输入的新内容尚未保存。");
    });
  });

  $("generateStyleBtn").addEventListener("click", async () => {
    await runTask("style", "AI 正在分析本人文章，成功才扣 1 次…", async () => {
      const result = await paidApi("style", "/api/growth/style-profiles/generate", {
        knowledge_base_id: state.knowledgeBaseId,
      }, applyStyleGeneration);
      const successMessage = "风格草稿已生成并扣费，请阅读后人工确认激活。";
      showNotice(`${successMessage} 正在仅刷新列表…`);
      await refreshAfterPaidSuccess(successMessage);
    });
  });

  $("activateStyleBtn").addEventListener("click", async () => {
    const styleDraft = state.workspace?.profile?.style_draft;
    if (!styleDraft) return;
    await runTask("activate-style", "正在激活已确认风格…", async () => {
      await api("/api/growth/style-profiles/activate", { method: "POST", body: {
        knowledge_base_id: state.knowledgeBaseId, style_profile: styleDraft,
      } });
      await loadWorkspace({ preserveEditor: true });
      showNotice("写作风格已激活，后续成稿会使用此版本。");
    });
  });

  $("briefForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const referenceIds = [...$("referenceList").querySelectorAll("input:checked")].map((input) => Number(input.value));
    await runTask("brief", "AI 正在生成有来源的选题简报，成功才扣 1 次…", async () => {
      const result = await paidApi("brief", "/api/growth/briefs", {
        knowledge_base_id: state.knowledgeBaseId,
        focus: $("briefFocusInput").value,
        analysis_mode: $("analysisModeSelect").value,
        reference_article_ids: referenceIds,
      }, applyBriefGeneration);
      switchView("briefView");
      const successMessage = "选题简报已生成并扣费；请核对来源与数据边界后再用于成稿。";
      showNotice(`${successMessage} 正在仅刷新列表…`);
      await refreshAfterPaidSuccess(successMessage);
    });
  });

  $("draftForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!$("draftTopicInput").value.trim()) {
      showNotice("请先填写写作主题。", true);
      return;
    }
    if (state.editorDirty && !window.confirm("当前草稿有未保存修改。继续生成可能替换编辑器内容；建议先取消并保存。是否继续？")) return;
    await runTask("draft", "AI 正在生成草稿与引用映射，成功才扣 1 次…", async () => {
      const body = {
        knowledge_base_id: state.knowledgeBaseId,
        topic: $("draftTopicInput").value,
        target_length: Number($("draftLengthSelect").value),
        additional_instructions: $("draftInstructionsInput").value,
      };
      if (state.selectedBriefId) body.brief_id = state.selectedBriefId;
      const versionAtRequest = state.editorVersion;
      const activeDraftIdAtRequest = state.activeDraft?.id ?? null;
      let preservedDraftId = null;
      await paidApi("draft", "/api/growth/drafts", body, async (result) => {
        preservedDraftId = await applyDraftGenerationGuarded(result, versionAtRequest, activeDraftIdAtRequest);
      });
      switchView("writeView");
      if (preservedDraftId) {
        showNotice(`草稿 #${preservedDraftId} 已生成并扣费，已加入草稿列表；检测到等待期间编辑器发生变化，当前输入未覆盖。请先保存或复制当前内容，再从列表打开新草稿。`);
        return;
      }
      $("draftEditor").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      const successMessage = "草稿已生成并扣费。请先核对事实、引用和待核实项，再导出或发布。";
      showNotice(`${successMessage} 正在仅刷新列表…`);
      await refreshAfterPaidSuccess(successMessage);
    });
  });

  $("saveDraftBtn").addEventListener("click", async () => {
    if (!state.activeDraft) return;
    await runTask("save-draft", "正在保存人工修改…", async () => {
      const draftAtSave = state.activeDraft;
      const versionAtSave = state.editorVersion;
      let result;
      try {
        result = await api(`/api/growth/drafts/${draftAtSave.id}`, { method: "PATCH", body: {
          title: $("editorTitleInput").value,
          body_markdown: $("editorBodyInput").value,
          expected_revision: draftAtSave.revision,
        } });
      } catch (error) {
        if (error.status === 409) {
          showNotice("保存冲突：草稿已在其他页面更新。本次修改未保存、未覆盖最新版本；请先复制保留当前内容，再重新打开草稿核对后合并。", true);
          return;
        }
        throw error;
      }
      const editorRemainedOnSavedDraft = (state.activeDraft?.id ?? null) === draftAtSave.id;
      if (editorRemainedOnSavedDraft) {
        state.activeDraft = result.draft;
        if (state.editorVersion === versionAtSave) state.editorDirty = false;
      }
      await loadWorkspace({ preserveEditor: true });
      if (!editorRemainedOnSavedDraft) {
        showNotice(`草稿 #${draftAtSave.id} 的人工修改已保存；检测到当前编辑器已切换到另一草稿，本次未覆盖当前输入。`);
        return;
      }
      showNotice(state.editorDirty
        ? "提交时的人工修改已保存；之后输入的新修改尚未保存。"
        : "人工修改已保存到服务端。");
    });
  });

  $("metricForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const ownArticleId = Number($("metricArticleSelect").value);
    if (!ownArticleId) {
      showNotice("请先在“账号风格”中录入本人文章。", true);
      return;
    }
    const accountLabel = $("metricAccountInput").value.trim();
    const sourceName = $("metricSourceInput").value.trim();
    if (!accountLabel || !sourceName) {
      showNotice("请填写公众号账号标识与数据来源。", true);
      return;
    }
    await runTask("metric", "正在保存真实效果数据…", async () => {
      await api("/api/growth/metrics", { method: "POST", body: {
        knowledge_base_id: state.knowledgeBaseId,
        own_article_id: ownArticleId,
        metric_date: $("metricDateInput").value,
        window: $("metricWindowSelect").value,
        account_label: accountLabel,
        source_name: sourceName,
        campaign_id: $("metricCampaignInput").value.trim(),
        reads: numberValue("readsInput"),
        shares: numberValue("sharesInput"),
        new_follows: numberValue("followsInput"),
        landing_clicks: numberValue("clicksInput"),
        registrations: numberValue("registrationsInput"),
        payments: numberValue("paymentsInput"),
      } });
      await loadWorkspace({ preserveEditor: true });
      $("metricArticleSelect").value = String(ownArticleId);
      showNotice("效果数据已保存。页面只展示描述性指标，不作因果推断。");
    });
  });

  await runTask("initialise", "正在加载知识库与工作台…", async () => {
    const data = await api("/api/knowledge-bases");
    const select = clear($("knowledgeBaseSelect"));
    (data.knowledge_bases || []).forEach((knowledgeBase) => {
      const option = element("option", "", knowledgeBase.name || `知识库 #${knowledgeBase.id}`);
      option.value = String(knowledgeBase.id);
      select.append(option);
    });
    if (!data.knowledge_bases?.length) throw new Error("当前账号没有可用知识库");
    const initialId = Number(data.knowledge_bases[0].id);
    select.value = String(initialId);
    await loadWorkspace({ knowledgeBaseId: initialId, switchKnowledgeBase: true });
    showNotice("工作台已就绪。建议先完善账号定位并录入本人文章。");
  });
}

initialise();
