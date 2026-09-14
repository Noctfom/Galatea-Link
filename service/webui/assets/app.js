// Galatea Link 控制台交互脚本，负责接口鉴权、运行控制、事件流与数据展示

"use strict";

const appState = {
  token: "",
  authenticated: false,
  serviceOnline: false,
  session: null,
  controls: null,
  capabilities: null,
  configuration: null,
  deckCatalog: null,
  editingDeck: null,
  modelCatalog: null,
  assetStatus: null,
  observation: null,
  socket: null,
  socketReconnectTimer: null,
  pollingTimer: null,
  refreshTimer: null,
  events: [],
  eventsPaused: false,
  currentPage: "overview",
  busy: new Set(),
};

const pageTitles = {
  overview: "运行概览",
  configuration: "配置中心",
  decks: "卡组仓库",
  strategy: "决策策略",
  models: "模型仓库",
  assets: "运行资产",
  chat: "游戏聊天",
  observation: "LLM 观察",
  events: "事件与诊断",
};

// 按选择器读取单个页面元素
function query(selector) {
  return document.querySelector(selector);
}

// 按选择器读取全部页面元素
function queryAll(selector) {
  return Array.from(document.querySelectorAll(selector));
}

// 安全写入指定元素的文本内容
function setText(selector, value) {
  const target = query(selector);
  if (target) {
    target.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  }
}

// 将未知值转换为适合界面的短文本
function displayValue(value) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

// 格式化结构化数据供诊断面板查看
function formatJson(value) {
  if (value === null || value === undefined) {
    return "暂无数据";
  }
  return JSON.stringify(value, null, 2);
}

// 格式化 Unix 时间为本地时间
function formatTime(value) {
  const numeric = Number(value);
  const date = Number.isFinite(numeric) && numeric > 0 ? new Date(numeric * 1000) : new Date();
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

// 格式化介入模式为易读中文
function formatMode(mode) {
  const names = {
    core_only: "仅 Core",
    hybrid: "置信度混合",
    llm_review: "LLM 全程复核",
    llm_only: "仅 LLM",
  };
  return names[mode] || displayValue(mode);
}

// 构造带当前访问令牌的请求头
function buildHeaders(hasBody) {
  const headers = {};
  if (appState.token) {
    headers.Authorization = `Bearer ${appState.token}`;
  }
  if (hasBody) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

// 更新最近一次接口请求耗时
function updateLatency(startedAt) {
  const elapsed = Math.max(0, performance.now() - startedAt);
  setText("#latencyValue", `${Math.round(elapsed)} ms`);
}

// 请求 Link API 并展开统一响应格式
async function api(path, options) {
  const requestOptions = options ? { ...options } : {};
  const startedAt = performance.now();
  const hasJsonBody = Boolean(requestOptions.body) && !(requestOptions.body instanceof FormData);
  requestOptions.headers = {
    ...buildHeaders(hasJsonBody),
    ...(requestOptions.headers || {}),
  };
  let response;
  try {
    response = await fetch(`/api/v1${path}`, requestOptions);
  } catch (networkError) {
    updateLatency(startedAt);
    const error = new Error("无法连接 Link 服务");
    error.code = "network_error";
    error.cause = networkError;
    throw error;
  }
  updateLatency(startedAt);
  let payload = null;
  try {
    payload = await response.json();
  } catch (parseError) {
    const error = new Error(`服务返回了无法解析的响应 HTTP ${response.status}`);
    error.code = "invalid_response";
    error.cause = parseError;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(payload.error?.message || `请求失败 HTTP ${response.status}`);
    error.code = payload.error?.code || "http_error";
    error.status = response.status;
    throw error;
  }
  if (payload.api_version) {
    setText("#apiVersionValue", payload.api_version);
  }
  return payload.data;
}

// 从提示区移除已经显示完毕的消息
function removeToast(toast) {
  if (toast && toast.parentNode) {
    toast.parentNode.removeChild(toast);
  }
}

// 显示短暂且不会阻塞操作的界面消息
function notify(message, kind) {
  const region = query("#toastRegion");
  const toast = document.createElement("div");
  toast.className = kind === "error" ? "toast is-error" : "toast";
  toast.textContent = message;
  region.appendChild(toast);
  while (region.children.length > 4) {
    region.removeChild(region.firstElementChild);
  }
  window.setTimeout(removeToast.bind(null, toast), 4200);
}

// 统一执行异步界面动作并展示异常
async function runAction(action, options) {
  const settings = options || {};
  try {
    return await action();
  } catch (error) {
    if (!settings.silent) {
      notify(error.message || String(error), "error");
    }
    if (settings.rethrow) {
      throw error;
    }
    return null;
  }
}

// 切换按钮忙碌状态并临时替换文案
function setButtonBusy(selector, busy, busyText) {
  const button = query(selector);
  if (!button) {
    return;
  }
  if (busy) {
    if (!button.dataset.idleText) {
      button.dataset.idleText = button.textContent;
    }
    button.textContent = busyText || "处理中";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.idleText || button.textContent;
    button.disabled = false;
  }
}

// 从本地偏好恢复主题且默认使用浅色
function restoreTheme() {
  let savedTheme = "light";
  try {
    savedTheme = localStorage.getItem("galatea-link-theme") || "light";
  } catch (error) {
    savedTheme = "light";
  }
  applyTheme(savedTheme === "dark" ? "dark" : "light");
}

// 应用主题并同步切换按钮文案
function applyTheme(theme) {
  document.body.dataset.theme = theme;
  const dark = theme === "dark";
  setText("#themeIcon", dark ? "☀" : "☾");
  setText("#themeLabel", dark ? "浅色外观" : "深色外观");
  try {
    localStorage.setItem("galatea-link-theme", theme);
  } catch (error) {
    return;
  }
}

// 在浅色和深色主题之间切换
function toggleTheme() {
  applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
}

// 切换当前显示的工作区页面
function showPage(pageName) {
  const nextPage = pageTitles[pageName] ? pageName : "overview";
  appState.currentPage = nextPage;
  const panels = queryAll("[data-page-panel]");
  for (const panel of panels) {
    const active = panel.dataset.pagePanel === nextPage;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  }
  const navItems = queryAll("[data-page]");
  for (const item of navItems) {
    item.classList.toggle("is-active", item.dataset.page === nextPage);
  }
  setText("#pageTitle", pageTitles[nextPage]);
  if (location.hash !== `#${nextPage}`) {
    history.replaceState(null, "", `#${nextPage}`);
  }
  document.body.classList.remove("nav-open");
  if (nextPage === "chat" && appState.session?.running) {
    runAction(refreshChatHistory, { silent: true });
  }
  if (nextPage === "configuration" && appState.authenticated) {
    runAction(refreshConfiguration, { silent: true });
  }
  if (nextPage === "decks" && appState.authenticated) {
    runAction(refreshDecks, { silent: true });
  }
  if (nextPage === "models" && appState.authenticated) {
    runAction(refreshModels, { silent: true });
  }
  if (nextPage === "observation" && appState.session?.running) {
    runAction(refreshObservation, { silent: true });
  }
  if (nextPage === "events") {
    renderEvents();
  }
}

// 根据地址栏片段切换可直接访问的控制台页面
function handleHashChange() {
  showPage(location.hash.slice(1));
}

// 处理侧栏导航按钮点击
function handleNavigationClick(event) {
  showPage(event.currentTarget.dataset.page);
}

// 处理跨页面快捷链接点击
function handlePageLinkClick(event) {
  showPage(event.currentTarget.dataset.goPage);
}

// 切换移动端侧栏显示状态
function toggleMobileNavigation() {
  document.body.classList.toggle("nav-open");
}

// 打开参数详解弹窗并阻止嵌套标签触发开关
function openHelpDialog(event) {
  event.preventDefault();
  event.stopPropagation();
  const source = event.currentTarget;
  setText("#helpDialogTitle", source.dataset.helpTitle || "参数说明");
  setText("#helpDialogBody", source.dataset.helpBody || "暂无详细说明");
  const dialog = query("#helpDialog");
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

// 关闭当前参数详解弹窗
function closeHelpDialog() {
  const dialog = query("#helpDialog");
  if (typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

// 点击弹窗遮罩区域时关闭参数详解
function handleHelpDialogBackdrop(event) {
  if (event.target === event.currentTarget) {
    closeHelpDialog();
  }
}

// 更新公开服务连通状态
function renderServiceHealth(health) {
  appState.serviceOnline = Boolean(health && health.status === "ok");
  const dot = query("#serviceDot");
  dot.className = appState.serviceOnline ? "state-dot is-online" : "state-dot is-error";
  setText("#serviceLabel", appState.serviceOnline ? "Link 服务在线" : "Link 服务不可用");
  if (health?.version) {
    setText("#linkVersionLabel", `对局控制台 · v${health.version}`);
  }
  if (health?.session_state && !appState.authenticated) {
    setText("#sessionMetricNote", `服务状态 ${health.session_state}`);
  }
}

// 更新服务能力和推理依赖诊断
function renderCapabilities(capabilities) {
  appState.capabilities = capabilities;
  query("#capabilitiesData").textContent = formatJson(capabilities);
  const backends = capabilities?.inference_backends || {};
  const labels = [];
  if (backends.onnxruntime === "available") {
    labels.push("ONNX");
  }
  if (backends.pytorch === "available") {
    labels.push("PyTorch");
  }
  setText("#backendValue", labels.length ? labels.join(" + ") : "依赖未就绪");
  setText("#pytorchDependencyValue", backends.pytorch === "available" ? "可用" : "缺少依赖");
  setText("#onnxDependencyValue", backends.onnxruntime === "available" ? "可用" : "缺少依赖");
  const versions = capabilities?.external_api_versions || [];
  if (capabilities?.link_version) {
    setText("#linkVersionLabel", `对局控制台 · v${capabilities.link_version}`);
  }
  if (versions.length) {
    setText("#apiVersionValue", versions[0]);
  }
}

// 检查无需鉴权的服务状态和能力声明
async function probePublicService() {
  try {
    const results = await Promise.all([api("/health"), api("/capabilities")]);
    renderServiceHealth(results[0]);
    renderCapabilities(results[1]);
    return true;
  } catch (error) {
    appState.serviceOnline = false;
    const dot = query("#serviceDot");
    dot.className = "state-dot is-error";
    setText("#serviceLabel", "Link 服务不可用");
    setText("#authHint", error.message || "无法连接服务");
    return false;
  }
}

// 更新状态标签的语义颜色
function setPill(selector, text, kind) {
  const pill = query(selector);
  if (!pill) {
    return;
  }
  pill.textContent = text;
  pill.className = `status-pill is-${kind || "neutral"}`;
}

// 将模型元数据渲染为简洁键值列表
function renderModelMetadata(coreModel, configuredModel) {
  const container = query("#modelDetails");
  container.replaceChildren();
  const metadata = coreModel?.metadata;
  const rows = [["状态", coreModel ? (coreModel.available ? "可用" : "不可用") : "等待加载"]];
  if (metadata && typeof metadata === "object") {
    const preferredKeys = ["backend", "protocol_version", "model_protocol_version", "checkpoint", "model_name", "adapter", "format"];
    const used = new Set();
    for (const key of preferredKeys) {
      if (Object.prototype.hasOwnProperty.call(metadata, key)) {
        rows.push([key, metadata[key]]);
        used.add(key);
      }
    }
    for (const key of Object.keys(metadata)) {
      if (rows.length >= 7) {
        break;
      }
      if (!used.has(key)) {
        rows.push([key, metadata[key]]);
      }
    }
  } else if (configuredModel) {
    rows.push(
      ["文件", selectedModelFilename({ selection: configuredModel })],
      ["推理后端", configuredModel.inference_backend],
      ["模型协议", configuredModel.protocol],
      ["模型 UUID", configuredModel.expected_model_id],
    );
  }
  for (const row of rows) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = row[0];
    detail.textContent = displayValue(row[1]);
    detail.title = detail.textContent;
    wrapper.append(term, detail);
    container.appendChild(wrapper);
  }
}

// 根据会话状态启用或锁定运行时操作
function updateRuntimeAvailability() {
  const authenticated = appState.authenticated;
  const running = Boolean(appState.session?.running);
  const state = appState.session?.state;
  const transitioning = state === "starting" || state === "stopping";
  query("#startButton").disabled = !authenticated || running || transitioning || appState.busy.has("session");
  query("#stopButton").disabled = !authenticated || !running || transitioning || appState.busy.has("session");
  query("#interventionFieldset").disabled = !authenticated;
  query("#autonomyFieldset").disabled = !authenticated;
  query("#configurationFieldset").disabled = !authenticated;
  query("#chatFieldset").disabled = !authenticated;
  query("#saveStrategyButton").disabled = !authenticated || appState.busy.has("controls");
  query("#saveChatSettingsButton").disabled = !authenticated || appState.busy.has("controls");
  query("#saveConfigurationButton").disabled = !authenticated || appState.busy.has("configuration");
  query("#testServerButton").disabled = !authenticated || appState.busy.has("server-test");
  query("#testLlmButton").disabled = !authenticated || appState.busy.has("llm-test");
  query("#saveLlmApiKeyButton").disabled = !authenticated || appState.busy.has("llm-secret");
  query("#clearLlmApiKeyButton").disabled = !authenticated || appState.busy.has("llm-secret");
  query("#chatInput").disabled = !running;
  query("#sendChatButton").disabled = !running || appState.busy.has("chat");
  query("#refreshModelsButton").disabled = !authenticated;
  query("#refreshDecksButton").disabled = !authenticated;
  query("#ydkFileInput").disabled = !authenticated;
  query("#overwriteDeckInput").disabled = !authenticated;
  query("#importDeckButton").disabled = !authenticated || appState.busy.has("deck-import");
  query("#applyDeckEditButton").disabled = !authenticated || running || appState.busy.has("deck-edit");
  query("#gkgFileInput").disabled = !authenticated;
  query("#activateImportedModel").disabled = !authenticated;
  query("#importGkgButton").disabled = !authenticated;
  query("#localPackageInput").disabled = !authenticated;
  query("#importLocalPackageButton").disabled = (
    !authenticated || !(appState.modelCatalog?.packages || []).length
  );
  query("#refreshAssetsButton").disabled = !authenticated;
  query("#syncCardDataButton").disabled = !authenticated || running || appState.busy.has("card-assets");
  query("#syncSemanticsButton").disabled = !authenticated || running || appState.busy.has("semantic-assets");
  query("#rebuildSemanticsButton").disabled = !authenticated || running || appState.busy.has("semantic-assets");
  query("#updateMetaStaplesButton").disabled = !authenticated || appState.busy.has("meta-staples");
  setText(
    "#strategySaveHint",
    authenticated
      ? running
        ? "立即应用到当前会话并持久化"
        : "保存后将在下次 Link 会话生效"
      : "连接控制台后可编辑",
  );
}

// 将完整服务状态同步到概览和诊断面板
function renderStatus(status) {
  appState.session = status;
  query("#rawStatusData").textContent = formatJson(status);
  const runtime = status?.runtime;
  const astrbot = status?.integrations?.astrbot || {};
  const activeAstrbot = (astrbot.clients || []).find((item) => item.connected);
  setText("#astrbotMetric", astrbot.connected ? "已连接" : "未连接");
  setText(
    "#astrbotMetricNote",
    activeAstrbot
      ? `${activeAstrbot.instance_name} · ${activeAstrbot.age_seconds} 秒前`
      : "等待插件心跳",
  );
  setText(
    "#astrbotIntegrationValue",
    astrbot.connected
      ? `${astrbot.active_count} 个插件实例已连接`
      : "未收到插件心跳",
  );
  const sessionStateNames = {
    stopped: "已停止",
    starting: "正在启动",
    running: "运行中",
    stopping: "正在停止",
    failed: "启动失败",
  };
  setText("#sessionMetric", sessionStateNames[status?.state] || displayValue(status?.state));
  setText("#sessionMetricNote", status?.running ? `会话代次 ${status.generation}` : "服务在线，模型未占用");
  setText("#generationBadge", `第 ${status?.generation || 0} 代`);
  const errorBox = query("#sessionError");
  errorBox.hidden = !status?.last_error;
  errorBox.textContent = status?.last_error || "";

  if (!runtime) {
    const configuredModel = status?.configured_model;
    const configuredFilename = selectedModelFilename({ selection: configuredModel });
    setText("#connectionMetric", "未启动");
    setText("#connectionMetricNote", "尚无游戏运行时");
    setText("#decisionMetric", "—");
    setText("#decisionMetricNote", "等待 Link 启动");
    setText("#modelMetric", configuredFilename ? "已配置" : "未选择");
    setText("#modelMetricNote", configuredFilename || "请前往模型仓库选择");
    setPill("#duelBadge", "未进入对局", "neutral");
    setText("#playerIdValue", "—");
    setText("#corePlayerIdValue", "—");
    setText("#timePlayerValue", "—");
    setText("#lastSourceValue", "—");
    setText("#lastChoiceValue", "—");
    setText("#chatCountValue", "0");
    renderModelMetadata(null, configuredModel);
    updateRuntimeAvailability();
    return;
  }

  setText("#connectionMetric", runtime.connected ? "已连接" : "等待连接");
  const networkProtocol = runtime.network_protocol || {};
  const activeProtocol = Number(networkProtocol.active_version);
  const protocolNote = Number.isFinite(activeProtocol)
    ? ` · 协议 0x${activeProtocol.toString(16).toUpperCase()}`
    : "";
  setText(
    "#connectionMetricNote",
    `${runtime.duel_active ? "对局正在进行" : "尚未进入对局"}${protocolNote}`,
  );
  setText("#decisionMetric", formatMode(runtime.decision?.mode));
  setText("#decisionMetricNote", `Core 阈值 ${displayValue(runtime.decision?.core_confidence_threshold)}`);
  const coreCircuitReason = runtime.core_model?.circuit_breaker_reason;
  setText(
    "#modelMetric",
    coreCircuitReason
      ? "本局已熔断"
      : runtime.core_model?.available
        ? "可用"
        : "不可用",
  );
  const modelBackend = runtime.core_model?.metadata?.backend || runtime.core_model?.metadata?.format;
  setText(
    "#modelMetricNote",
    coreCircuitReason || (modelBackend ? String(modelBackend) : "查看模型信息"),
  );
  setPill("#duelBadge", runtime.duel_active ? "对局进行中" : "未进入对局", runtime.duel_active ? "success" : "neutral");
  setText("#playerIdValue", runtime.player_id);
  setText("#corePlayerIdValue", runtime.core_player_id);
  setText("#timePlayerValue", runtime.time?.core_player_id);
  setText("#lastSourceValue", runtime.decision?.last_source);
  setText("#lastChoiceValue", runtime.decision?.last_choice_id);
  setText("#chatCountValue", runtime.game_chat?.history_size || 0);
  renderModelMetadata(runtime.core_model, status?.configured_model);
  updateRuntimeAvailability();
}

// 获取最新服务状态并更新页面
async function refreshStatus() {
  if (!appState.authenticated) {
    return null;
  }
  const status = await api("/status");
  renderStatus(status);
  return status;
}

// 将游戏协议整数格式化为便于复制的十六进制文本
function formatProtocolVersion(value) {
  const numeric = Number(value);
  if (!Number.isInteger(numeric) || numeric < 0) {
    return displayValue(value);
  }
  return `0x${numeric.toString(16).toUpperCase()}`;
}

// 渲染可选择卡组并保留当前失效配置用于诊断
function renderDeckOptions(deckCatalog, selectedDeck) {
  const select = query("#agentDeckInput");
  const records = Array.isArray(deckCatalog) ? [...deckCatalog] : [];
  if (selectedDeck && !records.some((item) => item.deck_ref === selectedDeck)) {
    records.unshift({ deck_ref: selectedDeck, display_name: `${selectedDeck}（不可用）` });
  }
  select.replaceChildren();
  for (const record of records) {
    const option = document.createElement("option");
    option.value = record.deck_ref;
    const counts = record.counts
      ? ` · M${record.counts.main}/E${record.counts.extra}`
      : "";
    option.textContent = `${record.display_name || record.deck_ref}${counts}`;
    select.appendChild(option);
  }
  select.value = selectedDeck || records[0]?.deck_ref || "";
}

// 创建本地卡组的分区明细并保持卡名文本安全
function createDeckSection(title, cards) {
  const section = document.createElement("div");
  section.className = "deck-section";
  const heading = document.createElement("strong");
  const content = document.createElement("p");
  const records = Array.isArray(cards) ? cards : [];
  heading.textContent = `${title} · ${records.reduce((total, item) => total + Number(item.count || 0), 0)} 张`;
  content.textContent = records.length
    ? records.map((item) => `${item.name || `Code ${item.code}`} ×${item.count}`).join("、")
    : "空";
  section.append(heading, content);
  return section;
}

// 创建一副 Link 本地卡组的查看选择和删除卡片
function createDeckRecord(record, selectedDeck) {
  const selected = record.deck_ref === selectedDeck;
  const card = document.createElement("article");
  card.className = selected ? "deck-record is-selected" : "deck-record";
  const header = document.createElement("header");
  const identity = document.createElement("div");
  const name = document.createElement("h3");
  const meta = document.createElement("p");
  const badge = document.createElement("span");
  name.textContent = record.display_name || record.deck_ref;
  meta.textContent = `${record.source?.filename || `${record.deck_ref}.ydk`} · ${new Date(Number(record.updated_at || 0) * 1000).toLocaleString("zh-CN")}`;
  badge.className = selected ? "status-pill is-success" : "tag";
  badge.textContent = selected ? "当前使用" : "本地";
  identity.append(name, meta);
  header.append(identity, badge);

  const counts = document.createElement("div");
  counts.className = "model-meta-row";
  for (const [label, value] of [
    ["主卡", record.counts?.main],
    ["额外", record.counts?.extra],
    ["副卡", record.counts?.side],
  ]) {
    const item = document.createElement("span");
    item.className = "tag";
    item.textContent = `${label} ${value ?? 0}`;
    counts.appendChild(item);
  }

  const details = document.createElement("details");
  details.className = "deck-details";
  const summary = document.createElement("summary");
  summary.textContent = "查看卡片明细";
  details.append(
    summary,
    createDeckSection("主卡组", record.cards?.main),
    createDeckSection("额外卡组", record.cards?.extra),
    createDeckSection("副卡组", record.cards?.side),
  );

  const footer = document.createElement("footer");
  const selectButton = document.createElement("button");
  const editButton = document.createElement("button");
  const deleteButton = document.createElement("button");
  selectButton.type = "button";
  selectButton.className = "secondary-button";
  selectButton.textContent = selected ? "已设为使用" : "设为使用卡组";
  selectButton.disabled = selected;
  selectButton.addEventListener("click", () => runAction(selectLocalDeck.bind(null, record.deck_ref), {}));
  editButton.type = "button";
  editButton.className = "secondary-button";
  editButton.textContent = "编辑单卡";
  editButton.disabled = Boolean(appState.session?.running);
  editButton.title = editButton.disabled ? "对局运行中不能修改卡组" : "增添、移除或移动单卡";
  editButton.addEventListener("click", openDeckEditor.bind(null, record));
  deleteButton.type = "button";
  deleteButton.className = "danger-button";
  deleteButton.textContent = "删除";
  deleteButton.disabled = selected;
  deleteButton.title = selected ? "请先选择其他卡组再删除" : "删除 Link 本地 YDK 文件";
  deleteButton.addEventListener("click", () => runAction(deleteLocalDeck.bind(null, record), {}));
  footer.append(selectButton, editButton, deleteButton);
  card.append(header, counts, details, footer);
  return card;
}

// 渲染 Link 根目录中的本地卡组仓库
function renderDeckCatalog(catalog) {
  appState.deckCatalog = catalog || { decks: [] };
  const records = Array.isArray(catalog?.decks) ? catalog.decks : [];
  const selectedDeck = appState.configuration?.agent?.deck || "";
  setText("#deckCountBadge", `${records.length} 副`);
  const selectedRecord = records.find((item) => item.deck_ref === selectedDeck);
  const selectedSummary = query("#selectedDeckSummary");
  selectedSummary.replaceChildren();
  selectedSummary.classList.toggle("empty-state", !selectedRecord);
  if (selectedRecord) {
    const card = document.createElement("div");
    card.className = "selected-model-card";
    const name = document.createElement("h3");
    const counts = document.createElement("p");
    name.textContent = selectedRecord.display_name || selectedRecord.deck_ref;
    counts.textContent = `主卡 ${selectedRecord.counts?.main ?? 0} · 额外 ${selectedRecord.counts?.extra ?? 0} · 副卡 ${selectedRecord.counts?.side ?? 0}`;
    card.append(name, counts);
    selectedSummary.appendChild(card);
  } else {
    selectedSummary.textContent = selectedDeck
      ? `当前配置的卡组不可用: ${selectedDeck}`
      : "尚未选择本地卡组";
  }
  const container = query("#localDeckCatalog");
  container.replaceChildren();
  container.classList.toggle("empty-state", !records.length);
  if (!records.length) {
    container.textContent = "decks 目录中还没有本地卡组";
    return;
  }
  for (const record of records) {
    container.appendChild(createDeckRecord(record, selectedDeck));
  }
}

// 扫描 Link 根目录并读取最新本地卡组目录
async function refreshDecks() {
  if (!appState.authenticated) {
    return null;
  }
  const catalog = await api("/decks");
  renderDeckCatalog(catalog);
  return catalog;
}

// 将指定本地卡组保存为下一次 Link 会话选择
async function selectLocalDeck(deckRef) {
  const configuration = await api("/configuration", {
    method: "PATCH",
    body: JSON.stringify({
      expected_revision: appState.configuration?.revision,
      patch: { agent: { deck: deckRef } },
    }),
  });
  renderConfiguration(configuration);
  renderDeckCatalog({
    schema_version: "galatea.link.deck_catalog.v1",
    scope: "link_local",
    decks: configuration.deck_catalog || [],
  });
  notify(configuration.applies_after_restart ? "卡组已选择，重启 Link 后生效" : "卡组已选择");
}

// 删除用户确认且未被当前配置选择的 Link 本地卡组
async function deleteLocalDeck(record) {
  const confirmed = window.confirm(`确定删除本地卡组“${record.display_name || record.deck_ref}”吗？此操作不能撤销`);
  if (!confirmed) {
    return;
  }
  await api("/decks/local", {
    method: "DELETE",
    body: JSON.stringify({ deck_ref: record.deck_ref }),
  });
  await Promise.all([refreshConfiguration(), refreshDecks()]);
  notify("本地卡组已删除");
}

// 更新 YDK 文件选择提示
function updateYdkFileLabel() {
  const file = query("#ydkFileInput").files[0];
  setText("#ydkFileLabel", file ? `${file.name} · ${formatBytes(file.size)}` : "选择或拖入 .ydk 卡组");
}

// 阻止 YDK 拖放触发浏览器默认打开行为
function preventYdkFileDrag(event) {
  event.preventDefault();
  query("#ydkDropzone").classList.add("is-dragging");
}

// 清除 YDK 文件拖放区域的悬停状态
function leaveYdkFileDrag(event) {
  event.preventDefault();
  query("#ydkDropzone").classList.remove("is-dragging");
}

// 接收拖入的单个 YDK 文件
function acceptDroppedYdk(event) {
  event.preventDefault();
  query("#ydkDropzone").classList.remove("is-dragging");
  const files = event.dataTransfer?.files;
  if (!files || files.length !== 1) {
    notify("每次只能拖入一个 YDK 文件", "error");
    return;
  }
  if (!files[0].name.toLocaleLowerCase().endsWith(".ydk")) {
    notify("请选择 .ydk 卡组文件", "error");
    return;
  }
  query("#ydkFileInput").files = files;
  updateYdkFileLabel();
}

// 验证并导入浏览器选择的本地 YDK 卡组
async function importLocalDeck(event) {
  event.preventDefault();
  const input = query("#ydkFileInput");
  const file = input.files[0];
  if (!file || !file.name.toLocaleLowerCase().endsWith(".ydk")) {
    throw new Error("请先选择 .ydk 卡组文件");
  }
  if (file.size > 512 * 1024) {
    throw new Error("YDK 文件超过 512 KiB 限制");
  }
  appState.busy.add("deck-import");
  setButtonBusy("#importDeckButton", true, "正在验证并导入");
  updateRuntimeAvailability();
  try {
    const record = await api("/decks/local", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        ydk_text: await file.text(),
        overwrite: query("#overwriteDeckInput").checked,
      }),
    });
    input.value = "";
    query("#overwriteDeckInput").checked = false;
    updateYdkFileLabel();
    await Promise.all([refreshConfiguration(), refreshDecks()]);
    notify(`本地卡组“${record.display_name}”已导入`);
  } finally {
    appState.busy.delete("deck-import");
    setButtonBusy("#importDeckButton", false);
    updateRuntimeAvailability();
  }
}

// 处理本地 YDK 导入表单并统一显示异常
function handleLocalDeckImport(event) {
  runAction(importLocalDeck.bind(null, event));
}

// 渲染单卡编辑器中的当前卡组内容
function renderDeckEditorContents(record) {
  const container = query("#deckEditorContents");
  container.replaceChildren();
  for (const [section, title] of [["main", "主卡组"], ["extra", "额外卡组"], ["side", "副卡组"]]) {
    const block = document.createElement("section");
    const heading = document.createElement("h3");
    const list = document.createElement("div");
    block.className = "deck-editor-section";
    list.className = "deck-editor-card-list";
    const cards = Array.isArray(record.cards?.[section]) ? record.cards[section] : [];
    heading.textContent = `${title} · ${record.counts?.[section] ?? 0} 张`;
    if (!cards.length) {
      list.classList.add("empty-state");
      list.textContent = "空";
    } else {
      for (const item of cards) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "deck-card-token";
        button.textContent = `${item.name || `Code ${item.code}`} ×${item.count}`;
        button.title = `选择 ${item.code} 进行移除或移动`;
        button.addEventListener("click", () => {
          query("#deckOperationInput").value = "remove";
          query("#deckCardCodeInput").value = String(item.code);
          query("#deckSectionInput").value = section;
          query("#deckCardCountInput").value = "1";
          updateDeckOperationFields();
        });
        list.appendChild(button);
      }
    }
    block.append(heading, list);
    container.appendChild(block);
  }
}

// 打开指定本地卡组的单卡编辑器
function openDeckEditor(record) {
  appState.editingDeck = record;
  setText("#deckEditorTitle", `编辑 · ${record.display_name || record.deck_ref}`);
  renderDeckEditorContents(record);
  query("#deckOperationInput").value = "add";
  query("#deckCardCodeInput").value = "";
  query("#deckCardCountInput").value = "1";
  updateDeckOperationFields();
  const dialog = query("#deckEditorDialog");
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

// 关闭本地卡组单卡编辑器
function closeDeckEditor() {
  const dialog = query("#deckEditorDialog");
  if (typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
  appState.editingDeck = null;
}

// 根据单卡操作切换目标区域输入状态
function updateDeckOperationFields() {
  const moving = query("#deckOperationInput").value === "move";
  query("#deckTargetSectionField").hidden = !moving;
  query("#deckTargetSectionInput").disabled = !moving;
}

// 提交一条本地卡组单卡增删或移动操作
async function applyLocalDeckEdit(event) {
  event.preventDefault();
  const record = appState.editingDeck;
  if (!record) {
    throw new Error("没有正在编辑的本地卡组");
  }
  const codeText = query("#deckCardCodeInput").value.trim();
  if (!/^\d+$/.test(codeText)) {
    throw new Error("卡密必须是有效数字");
  }
  const operation = query("#deckOperationInput").value;
  const action = {
    operation,
    code: Number(codeText),
    section: query("#deckSectionInput").value,
    count: Number(query("#deckCardCountInput").value),
  };
  if (operation === "move") {
    action.to_section = query("#deckTargetSectionInput").value;
  }
  appState.busy.add("deck-edit");
  setButtonBusy("#applyDeckEditButton", true, "正在保存");
  updateRuntimeAvailability();
  try {
    const updated = await api("/decks/local", {
      method: "PATCH",
      body: JSON.stringify({ deck_ref: record.deck_ref, operations: [action] }),
    });
    appState.editingDeck = updated;
    renderDeckEditorContents(updated);
    await Promise.all([refreshConfiguration(), refreshDecks()]);
    notify("本地卡组单卡修改已保存");
  } finally {
    appState.busy.delete("deck-edit");
    setButtonBusy("#applyDeckEditButton", false);
    updateRuntimeAvailability();
  }
}

// 处理单卡编辑表单并统一显示异常
function handleDeckEditorSubmit(event) {
  runAction(applyLocalDeckEdit.bind(null, event));
}

// 点击单卡编辑器遮罩时关闭窗口
function handleDeckEditorBackdrop(event) {
  if (event.target === event.currentTarget) {
    closeDeckEditor();
  }
}

// 将脱敏配置快照同步到配置中心表单
function renderConfiguration(configuration) {
  appState.configuration = configuration;
  const server = configuration?.server || {};
  const agent = configuration?.agent || {};
  const llm = configuration?.llm || {};
  const service = configuration?.service || {};
  query("#serverProfileInput").value = server.profile || "ygopro";
  query("#serverHostInput").value = server.host || "";
  query("#serverPortInput").value = server.port ?? 7911;
  query("#serverProtocolInput").value = formatProtocolVersion(server.protocol_version ?? 0x1361);
  query("#autoNegotiateVersionInput").checked = Boolean(server.auto_negotiate_version ?? true);
  query("#serverVersionRetriesInput").value = server.max_version_retries ?? 1;
  query("#serverGameIdInput").value = server.game_id ?? 0;
  query("#serverTimeoutInput").value = server.connect_timeout ?? 10;
  query("#tracePacketsInput").checked = Boolean(server.trace_packets);
  setPill(
    "#serverPasswordBadge",
    server.password_configured ? "房间密码已配置" : "房间密码未配置",
    server.password_configured ? "success" : "neutral",
  );

  query("#agentNameInput").value = agent.name || "";
  renderDeckOptions(configuration?.deck_catalog || [], agent.deck || "");
  if (query("#localDeckCatalog")) {
    renderDeckCatalog({
      schema_version: "galatea.link.deck_catalog.v1",
      scope: "link_local",
      decks: configuration?.deck_catalog || [],
    });
  }
  query("#preferSecondInput").checked = Boolean(agent.prefer_second);

  query("#llmEnabledInput").checked = Boolean(llm.enabled);
  query("#llmProviderInput").value = llm.provider || "openai_compatible";
  query("#llmBaseUrlInput").value = llm.base_url || "";
  query("#llmApiKeyInput").value = "";
  query("#llmApiKeyEnvInput").value = llm.api_key_env || "";
  query("#llmModelInput").value = llm.model || "";
  query("#llmTimeoutInput").value = llm.timeout ?? 30;
  query("#llmMaxTokensInput").value = llm.max_tokens ?? 512;
  query("#llmTemperatureInput").value = llm.temperature ?? 0.1;
  query("#llmResponseFormatInput").value = llm.response_format || "json_object";
  query("#llmThinkingInput").value = llm.thinking_mode || "auto";
  query("#llmTraceInput").checked = Boolean(llm.trace_requests);
  query("#llmCacheStaticInput").checked = Boolean(llm.cache_static_context);
  query("#llmCacheDeckTextInput").checked = Boolean(llm.cache_deck_text);
  query("#llmCompactInput").checked = Boolean(llm.compact_dynamic_observation);
  const keySourceNames = {
    environment: "环境变量",
    local_file: "Link 本地密钥文件",
    config: "config.yaml",
  };
  setPill(
    "#llmKeyBadge",
    llm.api_key_configured
      ? `密钥来自${keySourceNames[llm.api_key_source] || "服务端"}`
      : "密钥未配置",
    llm.api_key_configured ? "success" : "warning",
  );

  setText("#configurationRevisionBadge", `revision ${configuration?.revision ?? "—"}`);
  setText("#serviceListenValue", `${service.host || "—"}:${service.port ?? "—"}`);
  setText("#serviceTokenValue", service.api_token_configured ? "已配置" : "未配置");
  setText("#serviceQueueValue", service.event_queue_size);
  const astrbot = appState.session?.integrations?.astrbot || {};
  setText(
    "#astrbotIntegrationValue",
    astrbot.connected
      ? `${astrbot.active_count} 个插件实例已连接`
      : "未收到插件心跳",
  );
  setText(
    "#configurationApplyValue",
    configuration?.applies_after_restart ? "重启 Link 后" : "下次启动 Link",
  );
  query("#configurationRestartNotice").hidden = !configuration?.applies_after_restart;
  renderCapabilities(appState.capabilities || {});
  updateRuntimeAvailability();
}

// 获取最新脱敏配置快照
async function refreshConfiguration() {
  if (!appState.authenticated) {
    return null;
  }
  const configuration = await api("/configuration");
  renderConfiguration(configuration);
  return configuration;
}

// 从 YGOPro 表单读取服务器配置草稿
function readServerConfigurationPatch() {
  return {
    profile: query("#serverProfileInput").value,
    host: query("#serverHostInput").value.trim(),
    port: Number(query("#serverPortInput").value),
    protocol_version: query("#serverProtocolInput").value.trim(),
    auto_negotiate_version: query("#autoNegotiateVersionInput").checked,
    max_version_retries: Number(query("#serverVersionRetriesInput").value),
    game_id: Number(query("#serverGameIdInput").value),
    connect_timeout: Number(query("#serverTimeoutInput").value),
    trace_packets: query("#tracePacketsInput").checked,
  };
}

// 从 Agent 表单读取身份和卡组配置草稿
function readAgentConfigurationPatch() {
  return {
    name: query("#agentNameInput").value.trim(),
    deck: query("#agentDeckInput").value,
    prefer_second: query("#preferSecondInput").checked,
  };
}

// 从 LLM 表单读取不包含密钥值的配置草稿
function readLlmConfigurationPatch() {
  return {
    enabled: query("#llmEnabledInput").checked,
    provider: query("#llmProviderInput").value,
    base_url: query("#llmBaseUrlInput").value.trim(),
    api_key_env: query("#llmApiKeyEnvInput").value.trim(),
    model: query("#llmModelInput").value.trim(),
    timeout: Number(query("#llmTimeoutInput").value),
    max_tokens: Number(query("#llmMaxTokensInput").value),
    temperature: Number(query("#llmTemperatureInput").value),
    response_format: query("#llmResponseFormatInput").value,
    thinking_mode: query("#llmThinkingInput").value,
    trace_requests: query("#llmTraceInput").checked,
    cache_static_context: query("#llmCacheStaticInput").checked,
    cache_deck_text: query("#llmCacheDeckTextInput").checked,
    compact_dynamic_observation: query("#llmCompactInput").checked,
  };
}

// 保存 Link 本机 LLM API Key 并立即清空输入框
async function saveLlmApiKey() {
  const apiKey = query("#llmApiKeyInput").value.trim();
  if (!apiKey) {
    throw new Error("请输入要保存的 LLM API Key");
  }
  appState.busy.add("llm-secret");
  updateRuntimeAvailability();
  try {
    const configuration = await api("/configuration/llm-api-key", {
      method: "PUT",
      body: JSON.stringify({ action: "set", api_key: apiKey }),
    });
    query("#llmApiKeyInput").value = "";
    renderConfiguration(configuration);
    notify(configuration.applies_after_restart ? "API Key 已保存，重启 Link 后生效" : "API Key 已保存到 Link 本机");
  } finally {
    appState.busy.delete("llm-secret");
    updateRuntimeAvailability();
  }
}

// 清除 Link 本地密钥文件中的 LLM API Key
async function clearLlmApiKey() {
  appState.busy.add("llm-secret");
  updateRuntimeAvailability();
  try {
    const configuration = await api("/configuration/llm-api-key", {
      method: "PUT",
      body: JSON.stringify({ action: "clear" }),
    });
    query("#llmApiKeyInput").value = "";
    renderConfiguration(configuration);
    notify("Link 本地 API Key 已清除");
  } finally {
    appState.busy.delete("llm-secret");
    updateRuntimeAvailability();
  }
}

// 构建配置中心的完整非敏感补丁
function readConfigurationPatch() {
  return {
    server: readServerConfigurationPatch(),
    agent: readAgentConfigurationPatch(),
    llm: readLlmConfigurationPatch(),
  };
}

// 保存配置中心表单并执行乐观版本检查
async function saveConfiguration(event) {
  event.preventDefault();
  if (!query("#configurationForm").reportValidity()) {
    return;
  }
  appState.busy.add("configuration");
  updateRuntimeAvailability();
  try {
    const configuration = await api("/configuration", {
      method: "PATCH",
      body: JSON.stringify({
        patch: readConfigurationPatch(),
        expected_revision: appState.configuration?.revision,
      }),
    });
    renderConfiguration(configuration);
    notify(configuration.applies_after_restart ? "配置已保存，重启 Link 后生效" : "配置已保存");
  } catch (error) {
    if (error.code === "runtime_error" && /revision|版本|冲突/i.test(error.message)) {
      await runAction(refreshConfiguration, { silent: true });
      notify("配置已被其他客户端更新，表单已刷新，请确认后重试", "error");
      return;
    }
    throw error;
  } finally {
    appState.busy.delete("configuration");
    updateRuntimeAvailability();
  }
}

// 处理配置中心表单提交并统一捕获异常
function handleConfigurationSubmit(event) {
  runAction(saveConfiguration.bind(null, event));
}

// 显示服务器或 LLM 测试的成功和失败结果
function renderConfigurationTestResult(selector, message, success) {
  const target = query(selector);
  target.hidden = false;
  target.className = success
    ? "inline-alert test-result is-success"
    : "inline-alert test-result is-danger";
  target.textContent = message;
}

// 使用当前服务器草稿执行纯 TCP 连通测试
async function testServerConfiguration() {
  appState.busy.add("server-test");
  setButtonBusy("#testServerButton", true, "连接中");
  updateRuntimeAvailability();
  try {
    const result = await api("/configuration/server/test", {
      method: "POST",
      body: JSON.stringify({ patch: { server: readServerConfigurationPatch() } }),
    });
    renderConfigurationTestResult(
      "#serverTestResult",
      `TCP 连接成功 · ${result.host}:${result.port} · ${result.elapsed_seconds} 秒`,
      true,
    );
  } catch (error) {
    renderConfigurationTestResult("#serverTestResult", error.message || String(error), false);
    throw error;
  } finally {
    appState.busy.delete("server-test");
    setButtonBusy("#testServerButton", false);
    updateRuntimeAvailability();
  }
}

// 使用当前 LLM 草稿执行一次最小结构化输出测试
async function testLlmConfiguration() {
  appState.busy.add("llm-test");
  setButtonBusy("#testLlmButton", true, "请求中");
  updateRuntimeAvailability();
  try {
    const result = await api("/configuration/llm/test", {
      method: "POST",
      body: JSON.stringify({ patch: { llm: readLlmConfigurationPatch() } }),
    });
    renderConfigurationTestResult(
      "#llmTestResult",
      `结构化输出成功 · ${result.elapsed_seconds} 秒 · choice ${result.choice_id} · ${result.reason || "未提供理由"}`,
      true,
    );
  } catch (error) {
    renderConfigurationTestResult("#llmTestResult", error.message || String(error), false);
    throw error;
  } finally {
    appState.busy.delete("llm-test");
    setButtonBusy("#testLlmButton", false);
    updateRuntimeAvailability();
  }
}

// 将逗号或空格分隔的消息编号解析为数组
function parseIntegerList(value) {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return [];
  }
  const parts = normalized.split(/[\s,，]+/);
  const values = [];
  for (const part of parts) {
    const number = Number(part);
    if (!Number.isInteger(number) || number < 0) {
      throw new Error(`无效消息类型 ${part}`);
    }
    if (!values.includes(number)) {
      values.push(number);
    }
  }
  return values;
}

// 将运行时控制快照同步到全部表单
function renderControls(controls) {
  appState.controls = controls;
  const intervention = controls?.baseline_intervention || controls?.intervention || {};
  const autonomy = controls?.autonomy || {};
  const chat = controls?.game_chat || {};
  query("#modeInput").value = intervention.mode || "core_only";
  query("#agentBackendInput").value = intervention.agent_backend || "local";
  query("#corePolicyInput").value = intervention.core_policy_mode || "greedy";
  query("#coreTemperatureInput").value = intervention.core_temperature ?? 0.8;
  query("#thresholdInput").value = intervention.core_confidence_threshold ?? 0.65;
  setText("#thresholdOutput", intervention.core_confidence_threshold ?? 0.65);
  query("#forceTypesInput").value = (intervention.force_llm_message_types || []).join(", ");
  query("#includeCoreInput").checked = Boolean(intervention.include_core_suggestion);
  query("#coreTimeBudgetInput").value = intervention.core_time_budget ?? 5;
  query("#timeBudgetInput").value = intervention.llm_time_budget ?? 12;
  query("#autonomyEnabledInput").checked = Boolean(autonomy.enabled);
  const allowedModes = new Set(autonomy.allowed_modes || []);
  const modeInputs = queryAll("input[name='autonomyMode']");
  for (const input of modeInputs) {
    input.checked = allowedModes.has(input.value);
  }
  query("#autonomyMinInput").value = autonomy.core_confidence_min ?? 0;
  query("#autonomyMaxInput").value = autonomy.core_confidence_max ?? 1;
  query("#autonomyTtlInput").value = autonomy.max_ttl_decisions ?? 3;
  query("#autonomyForceLimitInput").value = autonomy.max_force_message_types ?? 8;
  query("#activeOverrideValue").textContent = autonomy.active_override ? formatJson(autonomy.active_override) : "无";
  query("#chatEnabledInput").checked = Boolean(chat.enabled);
  query("#captureIncomingInput").checked = Boolean(chat.capture_incoming);
  query("#sendEnabledInput").checked = Boolean(chat.send_enabled);
  query("#includeChatContextInput").checked = Boolean(chat.include_in_llm_context);
  query("#chatSuggestionsInput").checked = Boolean(chat.llm_suggestions_enabled);
  query("#autoSendChatInput").checked = Boolean(chat.auto_send_llm_chat);
  query("#contextMessagesInput").value = chat.max_context_messages ?? 12;
  query("#contextCharsInput").value = chat.max_context_chars ?? 3000;
  query("#outboundLimitInput").value = chat.max_outbound_utf16_units ?? 120;
  query("#autoSendIntervalInput").value = chat.min_auto_send_interval ?? 15;
  setText("#revisionBadge", `revision ${controls?.revision ?? "—"}`);
  updateChatLength();
}

// 获取最新运行时控制快照
async function refreshControls() {
  if (!appState.authenticated) {
    return null;
  }
  const controls = await api("/controls");
  renderControls(controls);
  return controls;
}

// 读取自主控制允许的模式列表
function readAllowedModes() {
  const values = [];
  const inputs = queryAll("input[name='autonomyMode']");
  for (const input of inputs) {
    if (input.checked) {
      values.push(input.value);
    }
  }
  if (!values.length) {
    throw new Error("自主调整至少需要允许一种模式");
  }
  return values;
}

// 提交带 revision 检查的控制面补丁
async function submitControlsPatch(patch, successMessage) {
  if (!appState.controls) {
    await refreshControls();
  }
  appState.busy.add("controls");
  updateRuntimeAvailability();
  try {
    const controls = await api("/controls", {
      method: "PATCH",
      body: JSON.stringify({
        patch,
        expected_revision: appState.controls?.revision,
      }),
    });
    renderControls(controls);
    notify(successMessage);
    return controls;
  } catch (error) {
    if (error.code === "runtime_error" && /revision|版本|冲突/i.test(error.message)) {
      await runAction(refreshControls, { silent: true });
      notify("设置已被其他客户端更新，表单已刷新，请确认后重试", "error");
      return null;
    }
    throw error;
  } finally {
    appState.busy.delete("controls");
    updateRuntimeAvailability();
  }
}

// 从决策策略表单构建并提交完整补丁
async function saveStrategy(event) {
  event.preventDefault();
  const form = query("#strategyForm");
  if (!form.reportValidity()) {
    return;
  }
  const minConfidence = Number(query("#autonomyMinInput").value);
  const maxConfidence = Number(query("#autonomyMaxInput").value);
  if (minConfidence > maxConfidence) {
    throw new Error("自主置信度下限不能高于上限");
  }
  const patch = {
    intervention: {
      mode: query("#modeInput").value,
      agent_backend: query("#agentBackendInput").value,
      core_policy_mode: query("#corePolicyInput").value,
      core_temperature: Number(query("#coreTemperatureInput").value),
      core_confidence_threshold: Number(query("#thresholdInput").value),
      force_llm_message_types: parseIntegerList(query("#forceTypesInput").value),
      include_core_suggestion: query("#includeCoreInput").checked,
      core_time_budget: Number(query("#coreTimeBudgetInput").value),
      llm_time_budget: Number(query("#timeBudgetInput").value),
    },
    autonomy: {
      enabled: query("#autonomyEnabledInput").checked,
      allowed_modes: readAllowedModes(),
      core_confidence_min: minConfidence,
      core_confidence_max: maxConfidence,
      max_ttl_decisions: Number(query("#autonomyTtlInput").value),
      max_force_message_types: Number(query("#autonomyForceLimitInput").value),
    },
  };
  await submitControlsPatch(patch, "决策策略已更新");
}

// 处理策略表单提交并统一捕获异常
function handleStrategySubmit(event) {
  runAction(saveStrategy.bind(null, event));
}

// 从聊天设置表单构建并提交完整补丁
async function saveChatSettings(event) {
  event.preventDefault();
  const form = query("#chatSettingsForm");
  if (!form.reportValidity()) {
    return;
  }
  const patch = {
    game_chat: {
      enabled: query("#chatEnabledInput").checked,
      capture_incoming: query("#captureIncomingInput").checked,
      send_enabled: query("#sendEnabledInput").checked,
      include_in_llm_context: query("#includeChatContextInput").checked,
      llm_suggestions_enabled: query("#chatSuggestionsInput").checked,
      auto_send_llm_chat: query("#autoSendChatInput").checked,
      max_context_messages: Number(query("#contextMessagesInput").value),
      max_context_chars: Number(query("#contextCharsInput").value),
      max_outbound_utf16_units: Number(query("#outboundLimitInput").value),
      min_auto_send_interval: Number(query("#autoSendIntervalInput").value),
    },
  };
  await submitControlsPatch(patch, "聊天设置已更新");
}

// 处理聊天设置表单提交并统一捕获异常
function handleChatSettingsSubmit(event) {
  runAction(saveChatSettings.bind(null, event));
}

// 根据滑块输入实时更新置信度显示
function updateThresholdOutput(event) {
  setText("#thresholdOutput", event.currentTarget.value);
}

// 判断聊天记录是否属于己方出站消息
function isOutboundChat(item) {
  return item?.direction === "outbound" || item?.role === "agent";
}

// 格式化聊天角色名称
function formatChatRole(item) {
  const roles = {
    agent: "Galatea",
    opponent: "对手",
    observer: "观战者",
    system: "系统",
  };
  return roles[item?.role] || item?.source || "未知来源";
}

// 将聊天历史渲染为安全的消息气泡
function renderChatHistory(history) {
  const container = query("#chatHistory");
  const items = Array.isArray(history) ? history : [];
  container.replaceChildren();
  container.classList.toggle("empty-state", !items.length);
  setText("#chatHistoryCount", `${items.length} 条`);
  if (!items.length) {
    container.textContent = "当前对局还没有聊天记录";
    return;
  }
  for (const item of items) {
    const wrapper = document.createElement("article");
    wrapper.className = isOutboundChat(item) ? "chat-message is-outbound" : "chat-message";
    const metadata = document.createElement("div");
    metadata.className = "chat-meta";
    const role = document.createElement("span");
    const time = document.createElement("time");
    role.textContent = formatChatRole(item);
    time.textContent = formatTime(item.created_at);
    metadata.append(role, time);
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = item?.text || "";
    wrapper.append(metadata, bubble);
    container.appendChild(wrapper);
  }
  container.scrollTop = container.scrollHeight;
}

// 读取最近的游戏聊天历史
async function refreshChatHistory() {
  if (!appState.session?.running) {
    return [];
  }
  const history = await api("/chat/history?limit=100");
  renderChatHistory(history);
  return history;
}

// 更新游戏聊天输入的 UTF-16 长度提示
function updateChatLength() {
  const input = query("#chatInput");
  const limit = Number(appState.controls?.game_chat?.max_outbound_utf16_units || 120);
  const length = input.value.length;
  setText("#chatLength", `${length} / ${limit} UTF-16`);
  query("#sendChatButton").disabled = !appState.session?.running || !input.value.trim() || length > limit || appState.busy.has("chat");
}

// 发送一条游戏内聊天消息
async function sendChat(event) {
  event.preventDefault();
  const input = query("#chatInput");
  const text = input.value.trim();
  if (!text) {
    return;
  }
  const limit = Number(appState.controls?.game_chat?.max_outbound_utf16_units || 120);
  if (text.length > limit) {
    throw new Error(`聊天内容不能超过 ${limit} 个 UTF-16 编码单元`);
  }
  appState.busy.add("chat");
  updateChatLength();
  try {
    await api("/chat", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    input.value = "";
    updateChatLength();
    await refreshChatHistory();
    notify("消息已提交给游戏聊天接口");
  } finally {
    appState.busy.delete("chat");
    updateChatLength();
  }
}

// 处理聊天发送表单提交并统一捕获异常
function handleChatSubmit(event) {
  runAction(sendChat.bind(null, event));
}

// 处理聊天输入框的快捷发送按键
function handleChatKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    runAction(sendChat.bind(null, event));
  }
}

// 将最新观察渲染到摘要和原始数据区
function renderObservation(observation) {
  appState.observation = observation;
  query("#observationData").textContent = formatJson(observation);
  const summary = query("#observationSummary");
  summary.replaceChildren();
  if (!observation) {
    summary.textContent = "当前尚未生成 LLM 观察";
    return;
  }
  const values = [
    ["schema", observation.schema_version],
    ["观察编号", observation.observation_id],
    ["消息类型", observation.message_type ?? observation.request?.message_type],
    ["动作数", Array.isArray(observation.legal_actions) ? observation.legal_actions.length : observation.actions?.length],
  ];
  for (const item of values) {
    if (item[1] === null || item[1] === undefined) {
      continue;
    }
    const chip = document.createElement("span");
    chip.className = "summary-chip";
    chip.textContent = `${item[0]} ${displayValue(item[1])}`;
    summary.appendChild(chip);
  }
  if (!summary.children.length) {
    summary.textContent = "已读取最新观察";
  }
}

// 读取最近一次实际交给 LLM 的观察
async function refreshObservation() {
  if (!appState.session?.running) {
    return null;
  }
  const observation = await api("/observation");
  renderObservation(observation);
  return observation;
}

// 将文本复制到系统剪贴板并提供旧浏览器回退
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

// 复制当前观察的格式化 JSON
async function copyObservation() {
  if (!appState.observation) {
    throw new Error("当前没有可复制的观察");
  }
  await copyText(formatJson(appState.observation));
  notify("观察 JSON 已复制");
}

// 触发浏览器下载内存中的文本文件
function downloadText(filename, text) {
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(URL.revokeObjectURL.bind(URL, url), 1000);
}

// 导出当前 LLM 观察为 JSON 文件
function downloadObservation() {
  if (!appState.observation) {
    throw new Error("当前没有可导出的观察");
  }
  const suffix = appState.observation.observation_id || new Date().toISOString().replace(/[:.]/g, "-");
  downloadText(`galatea-observation-${suffix}.json`, formatJson(appState.observation));
}

// 将字节数格式化为适合模型列表的容量文本
function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "未知大小";
  }
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

// 创建模型身份摘要标签
function createModelTag(text) {
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = text;
  return tag;
}

// 返回当前模型选择对应的主文件名
function selectedModelFilename(catalog) {
  const weightsPath = catalog?.selection?.weights_path || "";
  return String(weightsPath).replace(/\\/g, "/").split("/").pop() || "";
}

// 渲染当前选择模型的独立摘要卡片
function renderSelectedModel(catalog) {
  const container = query("#selectedModelSummary");
  container.replaceChildren();
  container.classList.remove("empty-state");
  const filename = selectedModelFilename(catalog);
  let record = null;
  for (const item of catalog?.models || []) {
    if (item.primary === filename) {
      record = item;
      break;
    }
  }
  if (!record) {
    container.classList.add("empty-state");
    container.textContent = filename ? `当前配置的模型未通过仓库检查: ${filename}` : "尚未选择模型";
    return;
  }
  const card = document.createElement("div");
  card.className = "selected-model-card";
  const header = document.createElement("header");
  const titleBox = document.createElement("div");
  const title = document.createElement("h3");
  const identity = document.createElement("p");
  title.textContent = record.primary;
  identity.textContent = record.model_id;
  titleBox.append(title, identity);
  header.append(titleBox, createModelTag(record.format === "onnx" ? "ONNX Runtime" : "PyTorch"));
  const metadata = document.createElement("div");
  metadata.className = "model-meta-row";
  metadata.append(
    createModelTag(`协议 V${record.model_protocol_version}`),
    createModelTag(`轮次 ${record.iteration}`),
    createModelTag(record.assets_available ? "语义资产就绪" : "缺少语义资产"),
    createModelTag(formatBytes(record.size_bytes)),
  );
  card.append(header, metadata);
  container.appendChild(card);
}

// 处理模型卡片上的选择操作
function handleModelSelect(event) {
  runAction(selectModel.bind(null, event.currentTarget.dataset.filename));
}

// 创建单个已安装模型的选择卡片
function createModelRecord(record, selectedFilename) {
  const card = document.createElement("article");
  const selected = record.primary === selectedFilename;
  card.className = selected ? "model-record is-selected" : "model-record";
  const header = document.createElement("header");
  const titleBox = document.createElement("div");
  const title = document.createElement("h3");
  const identity = document.createElement("p");
  title.textContent = record.primary;
  title.title = record.primary;
  identity.textContent = `${record.model_prefix} · ${record.model_id}`;
  titleBox.append(title, identity);
  header.append(titleBox, createModelTag(record.format === "onnx" ? "ONNX" : "PTH"));
  const metadata = document.createElement("div");
  metadata.className = "model-meta-row";
  metadata.append(
    createModelTag(`协议 V${record.model_protocol_version}`),
    createModelTag(`轮次 ${record.iteration}`),
    createModelTag(formatBytes(record.size_bytes)),
  );
  const footer = document.createElement("footer");
  const assetState = document.createElement("span");
  const button = document.createElement("button");
  assetState.textContent = record.assets_available ? "配套语义资产可用" : "缺少模型协议 V3 语义资产";
  button.type = "button";
  button.className = selected ? "secondary-button" : "primary-button";
  button.textContent = selected ? "当前选择" : "选择模型";
  button.dataset.filename = record.primary;
  button.disabled = selected || !record.assets_available;
  button.addEventListener("click", handleModelSelect);
  footer.append(assetState, button);
  card.append(header, metadata, footer);
  return card;
}

// 渲染无法识别或协议不兼容的模型制品
function renderInvalidModels(invalidModels) {
  const container = query("#invalidModelList");
  const items = Array.isArray(invalidModels) ? invalidModels : [];
  container.replaceChildren();
  container.classList.toggle("empty-state", !items.length);
  setText("#invalidModelCount", items.length);
  if (!items.length) {
    container.textContent = "没有发现无效制品";
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "invalid-record";
    const name = document.createElement("strong");
    const error = document.createElement("span");
    name.textContent = item.file || "未知文件";
    error.textContent = item.error || "无法识别";
    row.append(name, error);
    container.appendChild(row);
  }
}

// 渲染部署目录中可直接导入的 GKG 包
function renderLocalPackages(packages) {
  const select = query("#localPackageInput");
  const items = Array.isArray(packages) ? packages : [];
  select.replaceChildren();
  if (!items.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "没有可用部署包";
    select.appendChild(option);
  } else {
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.filename;
      option.textContent = `${item.filename} · ${formatBytes(item.size_bytes)}`;
      select.appendChild(option);
    }
  }
  query("#importLocalPackageButton").disabled = !appState.authenticated || !items.length;
}

// 将模型仓库响应同步到选择、导入和异常面板
function renderModelCatalog(catalog) {
  appState.modelCatalog = catalog;
  const models = Array.isArray(catalog?.models) ? catalog.models : [];
  const selectedFilename = selectedModelFilename(catalog);
  setText("#modelSelectionRevision", `revision ${catalog?.selection?.revision ?? "—"}`);
  setText("#modelCountBadge", `${models.length} 个`);
  query("#modelRestartNotice").hidden = !catalog?.selection_applies_after_restart;
  renderSelectedModel(catalog);
  const container = query("#modelCatalog");
  container.replaceChildren();
  container.classList.toggle("empty-state", !models.length);
  if (!models.length) {
    container.textContent = "models 目录中没有可选择的模型";
  } else {
    for (const model of models) {
      container.appendChild(createModelRecord(model, selectedFilename));
    }
  }
  renderInvalidModels(catalog?.invalid);
  renderLocalPackages(catalog?.packages);
}

// 扫描并读取最新模型仓库
async function refreshModels() {
  if (!appState.authenticated) {
    return null;
  }
  const catalog = await api("/models");
  renderModelCatalog(catalog);
  return catalog;
}

// 渲染当前模型的卡库语义脚本和 142 兜底池状态
function renderAssetStatus(status) {
  appState.assetStatus = status;
  setText("#assetPathValue", status?.active_asset_path);
  setText("#assetCardCountValue", status?.card_database?.card_count ?? status?.card_database?.error ?? "不可用");
  setText("#assetScriptCountValue", status?.script_count ?? 0);
  setText("#assetSemanticValue", status?.semantic_complete ? "完整" : "缺少文件");
  setText("#assetBuilderValue", status?.local_semantic_builder?.available ? "已安装" : "未安装（仅影响本机构建）");
  setText("#assetStapleCountValue", status?.meta_staples?.length ?? 0);
  const badge = query("#semanticStateBadge");
  badge.textContent = status?.semantic_complete ? "语义资产完整" : "语义资产不完整";
  badge.className = status?.semantic_complete ? "status-pill is-success" : "status-pill is-warning";
  if (!query("#assetCdbUrlInput").value) {
    query("#assetCdbUrlInput").value = status?.defaults?.cdb_url || "";
  }
  if (!query("#assetScriptRepoInput").value) {
    query("#assetScriptRepoInput").value = status?.defaults?.script_repository || "";
  }
  if (!query("#assetSemanticUrlInput").value) {
    query("#assetSemanticUrlInput").value = status?.defaults?.semantic_url || "";
  }
  const list = query("#metaStaplesList");
  const staples = Array.isArray(status?.meta_staples) ? status.meta_staples : [];
  list.replaceChildren();
  list.classList.toggle("empty-state", !staples.length);
  if (!staples.length) {
    list.textContent = "当前没有可用兜底卡";
  } else {
    for (const item of staples) {
      const row = document.createElement("div");
      row.className = "invalid-record";
      const name = document.createElement("strong");
      const code = document.createElement("span");
      name.textContent = item.name || `Code ${item.code}`;
      code.textContent = String(item.code);
      row.append(name, code);
      list.appendChild(row);
    }
  }
  updateRuntimeAvailability();
}

// 读取当前模型协议 V3 配套运行资产
async function refreshAssets() {
  if (!appState.authenticated) {
    return null;
  }
  const status = await api("/assets");
  renderAssetStatus(status);
  return status;
}

// 同步并校验 Link 的 CDB 和官方 Lua 脚本
async function syncCardData() {
  appState.busy.add("card-assets");
  setButtonBusy("#syncCardDataButton", true, "正在同步");
  updateRuntimeAvailability();
  try {
    const result = await api("/assets/card-data/sync", {
      method: "POST",
      body: JSON.stringify({
        cdb_url: query("#assetCdbUrlInput").value.trim(),
        script_repository: query("#assetScriptRepoInput").value.trim(),
        force_scripts: query("#assetForceScriptsInput").checked,
      }),
    });
    await refreshAssets();
    notify(`卡库与脚本同步完成，共 ${result.script_count} 个 Lua 文件`);
  } finally {
    appState.busy.delete("card-assets");
    setButtonBusy("#syncCardDataButton", false);
    updateRuntimeAvailability();
  }
}

// 同步并交叉校验完整远程语义资产
async function syncSemanticAssets() {
  appState.busy.add("semantic-assets");
  setButtonBusy("#syncSemanticsButton", true, "正在同步");
  updateRuntimeAvailability();
  try {
    const result = await api("/assets/semantics/sync", {
      method: "POST",
      body: JSON.stringify({
        remote_url: query("#assetSemanticUrlInput").value.trim(),
      }),
    });
    await refreshAssets();
    notify(`语义资产已更新，共 ${result.effect_slot_count} 个效果槽`);
  } finally {
    appState.busy.delete("semantic-assets");
    setButtonBusy("#syncSemanticsButton", false);
    updateRuntimeAvailability();
  }
}

// 在完整环境中按所选模式本机生成模型协议 V3 语义资产
async function rebuildSemanticAssets() {
  const mode = query("#semanticBuildModeInput").value;
  const confirmed = window.confirm(
    mode === "full_local"
      ? "完全重建会忽略当前语义资产并重新编码全部效果槽，可能耗时较长，是否继续？"
      : "本机语义化可能下载嵌入模型并运行较长时间，是否继续？",
  );
  if (!confirmed) {
    return;
  }
  appState.busy.add("semantic-assets");
  setButtonBusy("#rebuildSemanticsButton", true, "正在构建");
  updateRuntimeAvailability();
  try {
    const result = await api("/assets/semantics/rebuild", {
      method: "POST",
      body: JSON.stringify({
        remote_url: mode === "remote_incremental" ? query("#assetSemanticUrlInput").value.trim() : null,
        clear_existing: mode === "full_local",
      }),
    });
    await refreshAssets();
    const added = result?.parse?.added_card_count ?? 0;
    const vectors = result?.embedding?.added_effect_slot_count ?? 0;
    notify(`本机语义化完成，新增 ${added} 张卡、编码 ${vectors} 个效果槽`);
  } finally {
    appState.busy.delete("semantic-assets");
    setButtonBusy("#rebuildSemanticsButton", false);
    updateRuntimeAvailability();
  }
}

// 解析页面输入的一个或多个卡密
function parseMetaStapleCodes() {
  const values = query("#metaStaplesCodesInput").value.split(/[\s,，;；]+/).filter(Boolean);
  if (!values.length || values.some((value) => !/^\d+$/.test(value))) {
    throw new Error("请输入一个或多个有效数字卡密");
  }
  return values.map((value) => Number(value));
}

// 应用当前模型路径的 142 宣言兜底池调整
async function updateMetaStaples() {
  appState.busy.add("meta-staples");
  setButtonBusy("#updateMetaStaplesButton", true, "正在应用");
  updateRuntimeAvailability();
  try {
    const result = await api("/assets/meta-staples", {
      method: "PATCH",
      body: JSON.stringify({
        operation: query("#metaStaplesOperationInput").value,
        card_codes: parseMetaStapleCodes(),
      }),
    });
    query("#metaStaplesCodesInput").value = "";
    await refreshAssets();
    notify(result.applies_after_restart ? "兜底池已保存，重启 Link 会话后生效" : "兜底池已保存");
  } finally {
    appState.busy.delete("meta-staples");
    setButtonBusy("#updateMetaStaplesButton", false);
    updateRuntimeAvailability();
  }
}

// 保存下次 Link 会话使用的模型
async function selectModel(filename) {
  const revision = appState.modelCatalog?.selection?.revision;
  const catalog = await api("/models/selection", {
    method: "PATCH",
    body: JSON.stringify({ filename, expected_revision: revision }),
  });
  renderModelCatalog(catalog);
  notify(catalog.selection_applies_after_restart ? "模型已选择，重启 Link 后生效" : "模型已选择");
}

// 更新 GKG 文件选择提示
function updateGkgFileLabel() {
  const file = query("#gkgFileInput").files[0];
  setText("#gkgFileLabel", file ? `${file.name} · ${formatBytes(file.size)}` : "选择 .gkg 部署包");
}

// 阻止文件拖放触发浏览器默认打开行为
function preventFileDrag(event) {
  event.preventDefault();
  query("#gkgDropzone").classList.add("is-dragging");
}

// 清除文件拖放区域的悬停状态
function leaveFileDrag(event) {
  event.preventDefault();
  query("#gkgDropzone").classList.remove("is-dragging");
}

// 接收拖入的单个 GKG 文件
function acceptDroppedGkg(event) {
  event.preventDefault();
  query("#gkgDropzone").classList.remove("is-dragging");
  const files = event.dataTransfer?.files;
  if (!files || files.length !== 1) {
    notify("每次只能拖入一个 GKG 包", "error");
    return;
  }
  if (!files[0].name.toLocaleLowerCase().endsWith(".gkg")) {
    notify("请选择 .gkg 部署包", "error");
    return;
  }
  query("#gkgFileInput").files = files;
  updateGkgFileLabel();
}

// 上传并导入浏览器选择的 GKG 包
async function importGkg(event) {
  event.preventDefault();
  const input = query("#gkgFileInput");
  const file = input.files[0];
  if (!file) {
    throw new Error("请先选择 GKG 部署包");
  }
  const formData = new FormData();
  formData.append("activate", query("#activateImportedModel").checked ? "true" : "false");
  formData.append("package", file, file.name);
  setButtonBusy("#importGkgButton", true, "正在验证并导入");
  try {
    const result = await api("/models/import", {
      method: "POST",
      body: formData,
    });
    renderModelCatalog(result.catalog);
    input.value = "";
    updateGkgFileLabel();
    notify(`GKG 导入成功，共安装 ${result.imported.installed_files.length} 个新文件`);
  } finally {
    setButtonBusy("#importGkgButton", false);
  }
}

// 处理 GKG 导入表单提交并统一捕获异常
function handleGkgImport(event) {
  runAction(importGkg.bind(null, event));
}

// 导入 deploy_packages 目录中选定的 GKG 包
async function importLocalPackage() {
  const filename = query("#localPackageInput").value;
  if (!filename) {
    throw new Error("当前没有可导入的本地 GKG 包");
  }
  setButtonBusy("#importLocalPackageButton", true, "正在导入");
  try {
    const result = await api("/models/import-local", {
      method: "POST",
      body: JSON.stringify({ filename, activate: true }),
    });
    renderModelCatalog(result.catalog);
    notify(`本地 GKG 包 ${filename} 已导入`);
  } finally {
    setButtonBusy("#importLocalPackageButton", false);
    renderLocalPackages(appState.modelCatalog?.packages);
  }
}

// 按事件前缀推断筛选分类
function eventCategory(eventType) {
  const type = String(eventType || "");
  if (type.startsWith("service.")) {
    return "service";
  }
  if (type.startsWith("connection.") || type.startsWith("lobby.")) {
    return "connection";
  }
  if (type.startsWith("duel.") || type.startsWith("game.")) {
    return "duel";
  }
  if (type.startsWith("decision.") || type.startsWith("llm.") || type.startsWith("core.")) {
    return "decision";
  }
  if (type.startsWith("chat.") || type.startsWith("game_chat.")) {
    return "chat";
  }
  if (type.startsWith("runtime.")) {
    return "runtime";
  }
  return "other";
}

// 判断事件是否匹配当前搜索条件
function eventMatchesFilters(event) {
  const category = query("#eventCategoryInput").value;
  const search = query("#eventSearchInput").value.trim().toLocaleLowerCase();
  if (category !== "all" && eventCategory(event.event_type) !== category) {
    return false;
  }
  if (!search) {
    return true;
  }
  return `${event.event_type} ${formatJson(event.payload)}`.toLocaleLowerCase().includes(search);
}

// 创建一条可展开查看载荷的事件元素
function createEventElement(event) {
  const details = document.createElement("details");
  details.className = "event-item";
  const summary = document.createElement("summary");
  const sequence = document.createElement("span");
  const type = document.createElement("span");
  const time = document.createElement("time");
  sequence.className = "event-sequence";
  type.className = "event-type";
  time.className = "event-time";
  sequence.textContent = `#${event.sequence ?? "—"}`;
  type.textContent = event.event_type || "unknown";
  time.textContent = formatTime(event.created_at);
  summary.append(sequence, type, time);
  const payload = document.createElement("pre");
  payload.className = "event-payload";
  payload.textContent = formatJson(event.payload || {});
  details.append(summary, payload);
  return details;
}

// 根据筛选器重绘事件列表
function renderEvents() {
  if (appState.eventsPaused) {
    return;
  }
  const list = query("#eventList");
  const matching = [];
  for (const event of appState.events) {
    if (eventMatchesFilters(event)) {
      matching.push(event);
    }
  }
  list.replaceChildren();
  list.classList.toggle("empty-state", !matching.length);
  if (!matching.length) {
    list.textContent = appState.events.length ? "没有符合筛选条件的事件" : "连接控制台后会在这里显示实时事件";
  } else {
    const fragment = document.createDocumentFragment();
    for (const event of matching) {
      fragment.appendChild(createEventElement(event));
    }
    list.appendChild(fragment);
  }
  renderRecentEvents();
}

// 更新概览页最近活动列表
function renderRecentEvents() {
  const container = query("#recentEvents");
  container.replaceChildren();
  const recent = appState.events.slice(0, 5);
  container.classList.toggle("empty-state", !recent.length);
  if (!recent.length) {
    container.textContent = "尚未收到事件";
    return;
  }
  for (const event of recent) {
    const row = document.createElement("div");
    row.className = "recent-event";
    const name = document.createElement("strong");
    const time = document.createElement("time");
    name.textContent = event.event_type || "unknown";
    time.textContent = formatTime(event.created_at);
    row.append(name, time);
    container.appendChild(row);
  }
}

// 将新事件加入有界内存列表
function recordEvent(event) {
  const normalized = {
    sequence: event?.sequence ?? 0,
    event_type: event?.event_type || "unknown",
    created_at: event?.created_at || Date.now() / 1000,
    payload: event?.payload || {},
  };
  appState.events.unshift(normalized);
  if (appState.events.length > 300) {
    appState.events.length = 300;
  }
  if (!appState.eventsPaused) {
    renderEvents();
  }
}

// 清空当前浏览器保存的事件记录
function clearEvents() {
  appState.events = [];
  renderEvents();
  notify("本地事件列表已清空");
}

// 暂停或恢复事件列表重绘
function toggleEventsPaused() {
  appState.eventsPaused = !appState.eventsPaused;
  setText("#pauseEventsButton", appState.eventsPaused ? "恢复滚动" : "暂停滚动");
  if (!appState.eventsPaused) {
    renderEvents();
  }
}

// 导出当前浏览器保存的事件记录
function exportEvents() {
  if (!appState.events.length) {
    throw new Error("当前没有可导出的事件");
  }
  const suffix = new Date().toISOString().replace(/[:.]/g, "-");
  downloadText(`galatea-events-${suffix}.json`, formatJson(appState.events));
}

// 延迟合并高频事件触发的状态刷新
function scheduleRuntimeRefresh() {
  if (appState.refreshTimer) {
    return;
  }
  appState.refreshTimer = window.setTimeout(runScheduledRefresh, 250);
}

// 执行由事件流合并产生的状态刷新
function runScheduledRefresh() {
  appState.refreshTimer = null;
  runAction(refreshStatus, { silent: true });
}

// 根据事件类型刷新关联面板数据
function reactToEvent(event) {
  const type = String(event?.event_type || "");
  if (
    type === "runtime.controls.updated"
    || type === "service.controls.updated"
    || type.startsWith("runtime.autonomy.")
  ) {
    runAction(refreshControls, { silent: true });
  }
  if (type === "service.model.selected" || type === "service.model.imported") {
    runAction(refreshModels, { silent: true });
  }
  if (type.startsWith("service.deck.")) {
    runAction(refreshConfiguration, { silent: true });
    runAction(refreshDecks, { silent: true });
  }
  if (type === "service.configuration.updated") {
    runAction(refreshConfiguration, { silent: true });
  }
  if (type === "service.llm_secret.updated") {
    runAction(refreshConfiguration, { silent: true });
  }
  if (type.startsWith("game_chat.") || type.startsWith("chat.")) {
    runAction(refreshChatHistory, { silent: true });
  }
  if (type === "observation.updated" && query("#observationAutoRefresh").checked) {
    runAction(refreshObservation, { silent: true });
  }
  if (/^(service|connection|duel|decision|runtime|game_chat|link)\./.test(type)) {
    scheduleRuntimeRefresh();
  }
}

// 更新 WebSocket 连接状态显示
function renderSocketState(state) {
  const states = {
    connecting: ["事件流连接中", "warning"],
    connected: ["事件流已连接", "success"],
    disconnected: ["事件流已断开", "danger"],
    idle: ["事件流未连接", "neutral"],
  };
  const value = states[state] || states.idle;
  setPill("#socketBadge", value[0], value[1]);
  setText("#websocketValue", value[0].replace("事件流", ""));
}

// 处理 WebSocket 建立事件
function handleSocketOpen() {
  renderSocketState("connected");
}

// 处理 WebSocket 中收到的结构化事件
function handleSocketMessage(message) {
  try {
    const envelope = JSON.parse(message.data);
    if (envelope.api_version) {
      setText("#apiVersionValue", envelope.api_version);
    }
    if (envelope.event) {
      recordEvent(envelope.event);
      reactToEvent(envelope.event);
    }
  } catch (error) {
    recordEvent({
      event_type: "webui.event.parse_failed",
      payload: { message: error.message, raw: String(message.data).slice(0, 1000) },
    });
  }
}

// 在意外断开后安排事件流重连
function scheduleSocketReconnect() {
  if (!appState.authenticated || appState.socketReconnectTimer) {
    return;
  }
  appState.socketReconnectTimer = window.setTimeout(reconnectEventSocket, 2500);
}

// 执行延迟后的事件流重连
function reconnectEventSocket() {
  appState.socketReconnectTimer = null;
  openEventSocket();
}

// 处理 WebSocket 断开事件
function handleSocketClose(event) {
  if (event.currentTarget !== appState.socket) {
    return;
  }
  renderSocketState("disconnected");
  scheduleSocketReconnect();
}

// 处理 WebSocket 传输错误
function handleSocketError() {
  renderSocketState("disconnected");
}

// 创建使用鉴权 Cookie 的同源事件连接
function openEventSocket() {
  if (!appState.authenticated) {
    return;
  }
  if (appState.socket) {
    appState.socket.removeEventListener("close", handleSocketClose);
    appState.socket.close();
  }
  renderSocketState("connecting");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/v1/events`);
  appState.socket = socket;
  socket.addEventListener("open", handleSocketOpen);
  socket.addEventListener("message", handleSocketMessage);
  socket.addEventListener("close", handleSocketClose);
  socket.addEventListener("error", handleSocketError);
}

// 启动低频状态轮询作为事件流之外的兜底
function startStatusPolling() {
  if (appState.pollingTimer) {
    window.clearInterval(appState.pollingTimer);
  }
  appState.pollingTimer = window.setInterval(pollStatus, 5000);
}

// 执行一次静默状态轮询
function pollStatus() {
  if (appState.authenticated && document.visibilityState !== "hidden") {
    runAction(refreshStatus, { silent: true });
  }
}

// 完成控制台鉴权并启动实时同步
async function connectConsole(silent) {
  setButtonBusy("#connectButton", true, "连接中");
  appState.token = query("#tokenInput").value.trim();
  try {
    await api("/auth/session", { method: "POST" });
    appState.authenticated = true;
    query("#authPanel").hidden = true;
    await Promise.all([
      refreshStatus(),
      refreshControls(),
      refreshConfiguration(),
      refreshDecks(),
      refreshModels(),
      refreshAssets(),
    ]);
    openEventSocket();
    startStatusPolling();
    if (appState.session?.running) {
      await Promise.all([
        refreshChatHistory(),
        refreshObservation(),
      ]);
    }
    if (!silent) {
      notify("控制台已连接");
    }
    return true;
  } catch (error) {
    appState.authenticated = false;
    query("#authPanel").hidden = false;
    if (error.status === 401) {
      setText("#authHint", "服务已启用访问令牌，请输入正确令牌");
    } else {
      setText("#authHint", error.message || "连接失败");
    }
    updateRuntimeAvailability();
    if (!silent) {
      throw error;
    }
    return false;
  } finally {
    setButtonBusy("#connectButton", false);
  }
}

// 处理用户主动连接控制台
function handleConnect() {
  runAction(connectConsole.bind(null, false));
}

// 启动 Link 游戏会话并按需加载模型
async function startSession() {
  appState.busy.add("session");
  setButtonBusy("#startButton", true, "正在启动");
  updateRuntimeAvailability();
  try {
    const status = await api("/session/start", { method: "POST" });
    renderStatus(status);
    await Promise.all([
      refreshControls(),
      refreshConfiguration(),
      refreshDecks(),
      refreshModels(),
      refreshAssets(),
      refreshChatHistory(),
      refreshObservation(),
    ]);
    notify("Link 会话已启动");
  } finally {
    appState.busy.delete("session");
    setButtonBusy("#startButton", false);
    updateRuntimeAvailability();
  }
}

// 停止当前游戏会话并保留控制服务
async function stopSession() {
  appState.busy.add("session");
  setButtonBusy("#stopButton", true, "正在停止");
  updateRuntimeAvailability();
  try {
    await api("/session/stop", { method: "POST" });
    appState.controls = null;
    appState.observation = null;
    await Promise.all([
      refreshStatus(),
      refreshControls(),
      refreshConfiguration(),
      refreshDecks(),
      refreshModels(),
      refreshAssets(),
    ]);
    renderChatHistory([]);
    renderObservation(null);
    notify("Link 会话已停止");
  } finally {
    appState.busy.delete("session");
    setButtonBusy("#stopButton", false);
    updateRuntimeAvailability();
  }
}

// 刷新当前可用的全部控制台数据
async function refreshAll() {
  await probePublicService();
  if (!appState.authenticated) {
    return;
  }
  await refreshStatus();
  await Promise.all([refreshControls(), refreshConfiguration(), refreshDecks(), refreshModels(), refreshAssets()]);
  if (appState.session?.running) {
    await Promise.all([
      refreshChatHistory(),
      refreshObservation(),
    ]);
  }
  notify("控制台数据已刷新");
}

// 处理标签页重新可见后的状态补偿刷新
function handleVisibilityChange() {
  if (document.visibilityState === "visible") {
    pollStatus();
  }
}

// 为全部静态控件注册交互监听器
function bindInterfaceEvents() {
  const navItems = queryAll("[data-page]");
  for (const item of navItems) {
    item.addEventListener("click", handleNavigationClick);
  }
  const pageLinks = queryAll("[data-go-page]");
  for (const item of pageLinks) {
    item.addEventListener("click", handlePageLinkClick);
  }
  query("#themeToggle").addEventListener("click", toggleTheme);
  query("#mobileNavToggle").addEventListener("click", toggleMobileNavigation);
  query("#connectButton").addEventListener("click", handleConnect);
  query("#globalRefresh").addEventListener("click", runAction.bind(null, refreshAll, {}));
  query("#startButton").addEventListener("click", runAction.bind(null, startSession, {}));
  query("#stopButton").addEventListener("click", runAction.bind(null, stopSession, {}));
  query("#thresholdInput").addEventListener("input", updateThresholdOutput);
  query("#configurationForm").addEventListener("submit", handleConfigurationSubmit);
  query("#testServerButton").addEventListener("click", runAction.bind(null, testServerConfiguration, {}));
  query("#testLlmButton").addEventListener("click", runAction.bind(null, testLlmConfiguration, {}));
  query("#saveLlmApiKeyButton").addEventListener("click", runAction.bind(null, saveLlmApiKey, {}));
  query("#clearLlmApiKeyButton").addEventListener("click", runAction.bind(null, clearLlmApiKey, {}));
  query("#strategyForm").addEventListener("submit", handleStrategySubmit);
  query("#chatSettingsForm").addEventListener("submit", handleChatSettingsSubmit);
  query("#refreshChatButton").addEventListener("click", runAction.bind(null, refreshChatHistory, {}));
  query("#refreshModelsButton").addEventListener("click", runAction.bind(null, refreshModels, {}));
  query("#refreshDecksButton").addEventListener("click", runAction.bind(null, refreshDecks, {}));
  query("#ydkFileInput").addEventListener("change", updateYdkFileLabel);
  query("#localDeckImportForm").addEventListener("submit", handleLocalDeckImport);
  query("#ydkDropzone").addEventListener("dragenter", preventYdkFileDrag);
  query("#ydkDropzone").addEventListener("dragover", preventYdkFileDrag);
  query("#ydkDropzone").addEventListener("dragleave", leaveYdkFileDrag);
  query("#ydkDropzone").addEventListener("drop", acceptDroppedYdk);
  query("#deckOperationInput").addEventListener("change", updateDeckOperationFields);
  query("#deckEditorForm").addEventListener("submit", handleDeckEditorSubmit);
  query("#closeDeckEditorButton").addEventListener("click", closeDeckEditor);
  query("#deckEditorDialog").addEventListener("click", handleDeckEditorBackdrop);
  query("#refreshAssetsButton").addEventListener("click", runAction.bind(null, refreshAssets, {}));
  query("#syncCardDataButton").addEventListener("click", runAction.bind(null, syncCardData, {}));
  query("#syncSemanticsButton").addEventListener("click", runAction.bind(null, syncSemanticAssets, {}));
  query("#rebuildSemanticsButton").addEventListener("click", runAction.bind(null, rebuildSemanticAssets, {}));
  query("#updateMetaStaplesButton").addEventListener("click", runAction.bind(null, updateMetaStaples, {}));
  query("#gkgFileInput").addEventListener("change", updateGkgFileLabel);
  query("#gkgImportForm").addEventListener("submit", handleGkgImport);
  query("#gkgDropzone").addEventListener("dragenter", preventFileDrag);
  query("#gkgDropzone").addEventListener("dragover", preventFileDrag);
  query("#gkgDropzone").addEventListener("dragleave", leaveFileDrag);
  query("#gkgDropzone").addEventListener("drop", acceptDroppedGkg);
  query("#importLocalPackageButton").addEventListener("click", runAction.bind(null, importLocalPackage, {}));
  query("#chatSendForm").addEventListener("submit", handleChatSubmit);
  query("#chatInput").addEventListener("input", updateChatLength);
  query("#chatInput").addEventListener("keydown", handleChatKeydown);
  query("#refreshObservationButton").addEventListener("click", runAction.bind(null, refreshObservation, {}));
  query("#copyObservationButton").addEventListener("click", runAction.bind(null, copyObservation, {}));
  query("#downloadObservationButton").addEventListener("click", runAction.bind(null, downloadObservation, {}));
  query("#eventSearchInput").addEventListener("input", renderEvents);
  query("#eventCategoryInput").addEventListener("change", renderEvents);
  query("#pauseEventsButton").addEventListener("click", toggleEventsPaused);
  query("#clearEventsButton").addEventListener("click", clearEvents);
  query("#exportEventsButton").addEventListener("click", runAction.bind(null, exportEvents, {}));
  const helpButtons = queryAll("[data-help-title]");
  for (const button of helpButtons) {
    button.addEventListener("click", openHelpDialog);
  }
  query("#closeHelpDialogButton").addEventListener("click", closeHelpDialog);
  query("#helpDialog").addEventListener("click", handleHelpDialogBackdrop);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("hashchange", handleHashChange);
}

// 初始化主题、界面监听和服务自动探测
async function initializeConsole() {
  restoreTheme();
  bindInterfaceEvents();
  showPage(location.hash.slice(1) || "overview");
  updateRuntimeAvailability();
  renderSocketState("idle");
  const online = await probePublicService();
  if (online) {
    await connectConsole(true);
  }
}

document.addEventListener("DOMContentLoaded", initializeConsole);
