/* All variable document content is plain text; no markup interpolation. */
var LibrarySyncPanel = (() => {
  "use strict";
  const htmlNS = "http://www.w3.org/1999/xhtml";
  const Bridge = typeof module !== "undefined" ? require("./bridge-client.js") : LibrarySyncBridge;
  const Pickers = typeof module !== "undefined" ? require("./picker.js") : LibrarySyncPicker;
  function duration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
    const n = Math.floor(seconds);
    return `${Math.floor(n / 3600)}时${String(Math.floor(n / 60) % 60).padStart(2, "0")}分${String(n % 60).padStart(2, "0")}秒`;
  }
  const objectLike = x => x !== null && typeof x === "object" && !Array.isArray(x);
  class Controller {
    constructor(window, host, client) {
      this.window = window; this.doc = window.document; this.host = host; this.client = client;
      this.closed = false; this.busy = false; this.ticking = false; this.connected = false;
      this.configured = false; this.credentials = { glm: false, deepseek: false, tongji: false };
      this.savedConfig = null; this.capabilities = [];
      this.credentialsVerified = false; this.mineruConfigured = null;
      this.modelOptions = { qc: null, zh: null };
      this.modelResultPhase = null;
      this.revealEpoch = 0; this.loadedKeys = {}; this.keyDirty = {};
      this.pickers = {}; this.endpointConfirmed = { qc: false, zh: false };
      this.configDirty = false; this.pathsDirty = false; this.active = false; this.statusKnown = false;
      this.keys = null; this.preview = null; this.pending = null; this.startResult = null; this.startAttempted = false;
      this.primaryKeys = {};
      this.timer = null; this.report = ""; this.handlers = []; this.epoch = 0;
      this.page = "sync"; this.currentStatus = null; this.displayedStatus = null;
      this.selectedRun = null; this.viewEpoch = 0; this.viewKnown = false;
      this.history = []; this.pauseRun = null; this.resumePending = null;
      this.historyLoading = null;
      this.selections = []; this.rowSignatures = new Map(); this.selectionControls = []; this.rowDescriptions = new Map();
      this.testStates = {}; this.profileProviders = {};
      // Per-paper diagnosis view (beta.15): the selected paper's details stay
      // mounted in their own section, so status-table refreshes never destroy
      // an open diagnosis; remedy results render into the same section.
      this.diagnosis = null; this.remedyBusy = false; this.acronymTitles = [];
      // Model drafts are separate from the discovery scope: editing them keeps
      // the preview and per-paper selections, only marking the binding stale.
      this.modelDirty = false;
      // Catalog state is per (provider, endpoint, protocol, key generation);
      // requests never use the form-wide this.pending lock. Every submission
      // takes a per-phase token: a late POST or poll answer from a superseded
      // request can never overwrite or delete a newer one.
      this.modelCatalogs = new Map(); this.catalogPending = {}; this.catalogInflight = {}; this.catalogTokens = {};
      this.keyEpoch = { glm: 0, deepseek: 0, tongji: 0 };
      // Sanitized per-plan support metadata from the bridge (zcode-plans-v1).
      this.zcodePlans = null;
      // Boot lifecycle: sanitized stage labels for the host handshake.
      this.bootStage = "constructed"; this.readySignaled = false;
    }
    el(id) { return this.doc.getElementById(id); }
    text(id, value) {
      if (this.closed) return;
      const el = this.el(id), safe = this.client.redact(value);
      if (el && el.textContent !== safe && !this.hasSelection(el)) el.textContent = safe;
    }
    hasSelection(el) {
      const selection = this.window.getSelection?.();
      return !!selection && !selection.isCollapsed && selection.containsNode?.(el, true);
    }
    showPage(page) {
      if (this.closed || !["sync", "task", "settings"].includes(page)) return;
      const previous = this.page; this.page = page;
      for (const name of ["sync", "task", "settings"]) {
        this.el(name + "-page").hidden = page !== name;
        this.el("nav-" + name).setAttribute("aria-pressed", String(page === name));
      }
      for (const picker of Object.values(this.pickers)) picker.close();
      if (page === "task" && previous !== "task") return this.loadHistory();
    }
    disable(id, reason = "") {
      const el = this.el(id); if (!el) return;
      el.disabled = !!reason;
      el.setAttribute("title", this.client.redact(reason));
      const note = this.el(id + "-reason");
      if (note) {
        note.hidden = !reason; this.text(id + "-reason", reason);
        el.setAttribute("aria-describedby", id + "-reason");
      }
    }
    message(value, error = false, submissionId = null) {
      this.submissionBanner = submissionId;
      this.text("message", value);
      this.el("message").hidden = !value;
      this.el("message").setAttribute("data-error", String(error));
    }
    reportHint(report = this.report) { return report ? `报告保存路径：${report}` : `报告目录：${this.host.getPaths().repo}\\reports（具体文件尚未由服务报告）`; }
    error(error) {
      const safe = error instanceof Bridge.SafeError || error?.name === "SafeError"
        ? error.message : "操作失败。请检查连接、配置和报告。";
      // A model form error is not an error from the last historical paper run.
      this.connectionError(error);
      this.message(safe, true);
    }
    connectionError(error) {
      if (!["NETWORK", "AUTH"].includes(error?.code)) return;
      this.connected = false; this.statusKnown = false; this.viewKnown = false;
      this.text("connection", error.code === "AUTH"
        ? "服务鉴权失败（401/403），请核实授权后重新连接" : "连接中断或超时，请手动重新连接");
    }
    statusError(error, history = false, runId = null) {
      if (this.closed) return;
      this.connectionError(error);
      if (history) this.viewKnown = false; else this.statusKnown = false;
      const safe = error?.name === "SafeError" ? error.message : "状态读取失败，请查看报告。";
      const run = runId || (history ? this.selectedRun : this.currentStatus?.view?.id) || this.startResult?.run_id || "尚未提供";
      const view = history ? this.displayedStatus?.view?.id === run ? this.displayedStatus.view : null : this.currentStatus?.view;
      this.el("status-error").hidden = false;
      this.text("status-error", `${history ? "历史任务" : "当前任务"}状态读取失败，保留上次结果。\n任务 ${run} · ${Bridge.errorCategory(error)} · ${error?.code || "UNKNOWN"}${error?.httpStatus ? " / HTTP " + error.httpStatus : ""}\n${safe}\n${this.reportHint(view?.report || "")}`);
      this.el("copy-status").hidden = false;
      if (this.connected) this.text("connection", "已连接 127.0.0.1；状态读取需处理");
    }
    diagnosticsText() {
      // Composed only from already-redacted display state; the clipboard
      // receives what the user sees, never stored keys or raw responses.
      const v = this.displayedStatus?.view;
      const parts = [`Library Sync ${Bridge.VERSION} 诊断 · ${new Date().toLocaleString()}`];
      if (v?.id) parts.push(`任务 ${v.id} · ${v.status_text || v.status || "—"} · 阶段 ${v.phase_label || v.phase || "—"} · 验收 ${v.accepted ?? "—"}/${v.total ?? "—"} · 索引 ${v.index_status || "—"} · Git ${v.git_status || "—"}`);
      if (typeof v?.error === "string" && v.error.trim()) parts.push(`控制器错误：${v.error.trim()}`);
      for (const p of Array.isArray(v?.papers) ? v.papers : []) {
        parts.push(`${p.citekey || p.key || "—"}（${p.phase_label || p.phase || "—"}，${p.accepted === true ? "已验收" : "未验收"}）${p.note ? "：" + p.note : ""}`);
      }
      const notice = this.el("task-notice").textContent;
      if (notice.trim()) parts.push(notice);
      if (!this.el("status-error").hidden) parts.push(this.el("status-error").textContent);
      parts.push(this.el("report-path").textContent);
      const frozen = this.el("frozen-config").textContent;
      if (frozen.trim()) parts.push(frozen);
      if (this.el("message").getAttribute("data-error") === "true") parts.push(this.el("message").textContent);
      return this.client.redact(parts.filter(x => typeof x === "string" && x.trim()).join("\n"));
    }
    copyDiagnostics() {
      if (this.closed) return;
      const text = this.diagnosticsText();
      if (!text.trim() || text.trim() === `Library Sync ${Bridge.VERSION} 诊断`) {
        this.message("暂无可复制的诊断。"); return;
      }
      // Clipboard access only on this explicit user click.
      let copied = false;
      try { copied = this.host.copyText?.(text) === true; } catch (_) { copied = false; }
      this.message(copied ? "已复制脱敏诊断到剪贴板（不含任何 Key）。" : "复制失败：请用鼠标选中状态文本后手动复制。", !copied);
    }
    listen(id, event, handler) {
      this.listenTarget(this.el(id), event, handler);
    }
    listenTarget(target, event, handler) {
      target.addEventListener(event, handler);
      this.handlers.push([target, event, handler]);
    }
    initPickers() {
      const attach = (id, options, onSelect, editable = true, searchable = false) => {
        this.pickers[id] = new Pickers.Picker({ input: this.el(id), button: this.el(id + "-toggle"), list: this.el(id + "-options") },
          { options, onSelect, editable, searchable });
      };
      attach("chunk-parallel", ["1", "2"], () => { this.invalidate(); this.update(); }, false);
      for (const phase of ["qc", "zh"]) {
        attach(phase + "-provider", [{ value: "glm", label: "GLM（默认 Coding Plan）" }, { value: "deepseek", label: "DeepSeek" }, { value: "tongji", label: "同济校内 API（独立 Key）" },
          { value: "zcode", label: "ZCode 登录（本机 CLI 直连，新版停用）" }], () => this.modelFieldChanged(phase, "provider"));
        attach(phase + "-model", [], () => this.selectModelOption(phase), true, true);
        attach(phase + "-effort", ["default"], () => this.modelFieldChanged(phase, "effort"));
        attach(phase + "-thinking", ["default", "enabled", "disabled"], () => this.modelFieldChanged(phase, "thinking"));
        attach(phase + "-protocol", ["openai_chat_completions"], () => this.modelFieldChanged(phase, "protocol"));
        attach(phase + "-plan", [{ value: "coding", label: "Coding Plan · bigmodel 服务商" }, { value: "start", label: "Start Plan · 以本机 CLI 支持为准" }],
          () => this.modelFieldChanged(phase, "plan"), false);
        attach(phase + "-habit", Bridge.habitPresets().map(p => p.label), () => this.updateHabitNote(phase), false);
        this.updateHabitNote(phase);
      }
    }
    modelFieldChanged(phase, field) {
      if (this.closed) return;
      if (field === "provider") {
        const provider = this.el(phase + "-provider").value.trim().toLowerCase();
        const previous = this.profileProviders[phase];
        if (provider === previous) return;
        if (["glm", "deepseek", "tongji", "zcode"].includes(provider)) {
          const oldTemplates = previous ? [Bridge.profileDefaults(previous), Bridge.hydrationDefaults(previous)] : [];
          const next = Bridge.profileDefaults(provider);
          // A provider without endpoint fields (the zcode login) cannot have
          // user-edited URLs, so switching away adopts the new provider's
          // trusted defaults instead of leaving empty, invalid inputs.
          const noEndpoints = previous === "zcode";
          const untouched = (field, key) => noEndpoints || oldTemplates.some(d => String(this.el(phase + "-" + field).value) === String(d[key]));
          // Only migrate unchanged provider defaults. Custom endpoints and parameters survive.
          for (const [field, key] of [["base-url", "base_url"], ["models-url", "models_url"]]) {
            if (next[key] !== undefined) { if (untouched(field, key)) this.el(phase + "-" + field).value = next[key]; }
            else this.el(phase + "-" + field).value = "";
          }
          if (noEndpoints) {
            // A zcode draft never carried protocol or thinking values (the
            // login sends neither): adopt the new provider's template instead
            // of leaving inputs that cannot validate or misstate the choice.
            this.el(phase + "-protocol").value = next.protocol ?? "";
            if (next.thinking !== undefined) this.el(phase + "-thinking").value = next.thinking;
          }
          if (provider === "tongji" || previous === "tongji" || provider === "zcode" || previous === "zcode") {
            for (const [field, key] of [["effort", "reasoning_effort"], ["max-tokens", "max_tokens"]]) {
              if (untouched(field, key) && next[key] !== undefined) this.el(phase + "-" + field).value = String(next[key]);
            }
          }
          if (provider === "tongji") this.el(phase + "-thinking").value = "default";
          if (provider === "zcode") this.el(phase + "-plan").value = "coding";
          this.profileProviders[phase] = provider; this.el(phase + "-provider").value = provider;
        }
        this.endpointConfirmed[phase] = false; this.clearModelOptions(phase);
        // Recognized default endpoints immediately offer the bundled offline
        // snapshot; custom addresses stay empty until an explicit fetch.
        this.presentBundledCatalog(phase);
        this.text(phase + "-provider-notice", provider === "tongji"
          ? "已选择同济独立 Key；未改动的地址和模板参数已调整，thinking 固定为 default。请填写校内模型 ID，reasoning_effort 控制思考；手工地址及参数已保留，草稿尚未保存。"
          : provider === "zcode"
          ? this.zcodePlanNotice(phase, "coding")
          : "切换服务商仅更新未改动的默认地址；已保留模型及手工参数。请核对 Key 来源、API 地址与模型；可展开高级参数并恢复默认值。");
        if (provider !== previous) this.autoLoadCatalog(phase);
      }
      if (field === "plan") {
        this.text(phase + "-provider-notice", this.zcodePlanNotice(phase, this.el(phase + "-plan").value === "start" ? "start" : "coding"));
      }
      if (["model", "thinking", "effort"].includes(field)) this.updateEffortOptions(phase);
      if (["base-url", "models-url", "protocol"].includes(field)) {
        this.endpointConfirmed[phase] = false; this.clearModelOptions(phase);
        this.presentBundledCatalog(phase);
      }
      delete this.testStates[phase]; this.text(phase + "-test-state", "配置已更改，尚未测试此配置。");
      this.configDirty = true; this.markModelDirty(); this.update();
    }
    applyProviderDefaults(phase) {
      const provider = this.el(phase + "-provider").value.trim().toLowerCase();
      if (!["glm", "deepseek", "tongji", "zcode"].includes(provider)) {
        this.text(phase + "-effort-notice", "Provider / Key 来源请填 glm、deepseek、tongji 或 zcode；API 地址可另行自填。");
        this.configDirty = true; this.markModelDirty(); this.update(); return;
      }
      this.el(phase + "-provider").value = provider;
      const defaults = Bridge.profileDefaults(provider);
      for (const [field, key] of [["base-url", "base_url"], ["models-url", "models_url"], ["protocol", "protocol"], ["thinking", "thinking"], ["effort", "reasoning_effort"], ["timeout", "timeout"], ["max-tokens", "max_tokens"], ["plan", "plan"]]) {
        if (key === "plan" && provider !== "zcode") continue;
        this.el(phase + "-" + field).value = defaults[key] ?? "";
      }
      this.el(phase + "-extra-body").value = "{}"; this.endpointConfirmed[phase] = false;
      this.clearModelOptions(phase); this.updateEffortOptions(phase);
      delete this.testStates[phase]; this.text(phase + "-test-state", "默认参数已恢复，尚未测试。");
      this.text(phase + "-provider-notice", "已将地址、协议、thinking、effort、超时、输出长度及附加参数恢复为插件初始值（用户偏好默认：thinking 开启、effort=high；同济 thinking 固定 default、由 effort 表达；ZCode 订阅仍显式选择）。这不是服务端默认值；模型 ID 保留，草稿尚未保存，请核对后保存。");
      this.configDirty = true; this.markModelDirty(); this.update();
    }
    modelContext(phase) {
      const provider = this.el(phase + "-provider").value.trim().toLowerCase();
      return { provider, model: this.el(phase + "-model").value.trim(),
        ...(provider === "zcode" ? { plan: this.el(phase + "-plan").value === "start" ? "start" : "coding" } : {}),
        base_url: this.el(phase + "-base-url").value.trim().replace(/\/+$/, ""), models_url: this.el(phase + "-models-url").value.trim(),
        protocol: this.el(phase + "-protocol").value, endpoint_confirmed: this.endpointConfirmed[phase],
        thinking: this.el(phase + "-thinking").value, reasoning_effort: this.el(phase + "-effort").value };
    }
    updateEffortOptions(phase) {
      const context = this.modelContext(phase);
      const choices = this.modelOptions[phase];
      const help = Bridge.modelParameterHelp(context, choices), detail = Bridge.modelDetails(context, choices);
      const school = context.provider === "tongji";
      this.text(phase + "-thinking-help", school
        ? "同济平台不发送 thinking，此项固定为 default；请使用 reasoning_effort 统一设置思考开关和投入强度。"
        : "default：不发送 thinking，采用服务端默认，不等于关闭；enabled：明确请求开启；disabled：明确请求关闭，需模型支持。enabled 不表示最高推理强度。");
      this.text(phase + "-effort-help", school
        ? "同济平台由 reasoning_effort 统一控制思考：none 关闭，default 省略参数、采用服务端默认；其余档位按校内模型详情填写，平台可能调整不支持的值。"
        : "effort 控制思考投入；较高档位通常更慢、可能消耗更多输出。default 不发送 reasoning_effort，既不是最低档也不是关闭。与 thinking 分别设置，不保证所有组合都有效。");
      this.text(phase + "-max-tokens-help", school
        ? "32768 是同济配置的可编辑请求初值，不代表校内模型最大输出。max_tokens 限制单次生成长度，不能扩大上下文窗口；请按所选校内模型详情核实输出与上下文限制，并为推理和最终正文保留足够预算。"
        : "限制单次生成的输出 token 数，不是输入长度或上下文窗口（context）。输入 + 生成内容还须满足模型上下文限制；提高此值不会扩大 context。思考模型需给推理与最终正文留足预算，过小可能截断。新建空白模板显式使用 GLM 131072 / DeepSeek 393216、effort=high、thinking=enabled；已保存配置不会自动覆盖。具体模型能力未知时请手填，default 仍表示服务端默认。");
      const suggestions = [...new Set(["default", ...help.effortLevels])];
      this.pickers[phase + "-effort"]?.setOptions(suggestions);
      const recommendedNote = help.recommended
        ? `推荐默认（用户偏好，新模板即此值）：${help.recommended.thinking ? `thinking=${help.recommended.thinking}、` : ""}effort=${help.recommended.reasoning_effort}` +
          `${help.recommended.fallback ? "（该模型档位无 high，已按文档支持档位回退）" : ""}；与服务端默认、最高档相互独立，均可另行手选。`
        : "";
      this.text(phase + "-effort-notice", (detail?.effort_levels?.length
        ? `候选来自此模型的${choices.source === "provider_api" ? "接口元数据" : "官方文档参数快照（非实时）"}；${detail.default_effort ? "服务端默认 " + detail.default_effort + "。" : ""}` : "") + recommendedNote + help.effort);
      this.text(phase + "-thinking-notice", help.thinking);
      this.text(phase + "-tokens-notice", help.tokens);
      this.text(phase + "-endpoint-notice", help.endpointHelp);
      this.text(phase + "-maximum-notice", help.maximum
        ? `可填写此模型最高参数：max_tokens=${help.maximum.max_tokens}，reasoning_effort=${help.maximum.reasoning_effort}。仅修改本角色草稿，需保存后生效。`
        : "尚未核实此模型 / 端点的完整最高参数；当前数值可能来自模板或原配置，不代表该模型支持。请按官方文档手填，插件不改动当前参数。");
    }
    applyModelDefaults(phase) {
      // Selecting a known model fills the USER-PREFERRED defaults (thinking ON
      // + effort high, falling back to a documented supported level when high
      // is absent). The maximum stays a separate explicit action and every
      // other option in the pickers remains selectable.
      if (this.closed) return;
      const recommended = Bridge.modelRecommended(this.modelContext(phase), this.modelOptions[phase]);
      if (!recommended) {
        this.updateEffortOptions(phase);
        this.message(`${phase.toUpperCase()} 未能核实该模型 / 端点的文档档位，未自动填写 thinking / effort；当前参数未改动，请按官方文档手选，模型草稿需保存后生效。`);
        return;
      }
      if (recommended.max_tokens !== undefined) this.el(phase + "-max-tokens").value = recommended.max_tokens;
      this.el(phase + "-effort").value = recommended.reasoning_effort;
      if (recommended.thinking) this.el(phase + "-thinking").value = recommended.thinking;
      this.modelFieldChanged(phase, "effort");
      const fallbackNote = recommended.fallback
        ? `（该模型文档档位无 high，已回退到文档支持的 ${recommended.reasoning_effort}；其它档位仍可手选）` : "";
      this.text(phase + "-maximum-notice", `已按模型文档填写推荐默认参数（thinking 开启、effort=${recommended.reasoning_effort}` +
        `${recommended.max_tokens !== undefined ? `，max_tokens=${recommended.max_tokens}` : ""}）${fallbackNote}；官方默认与最高档另见说明。本角色草稿尚未保存，请保存模型配置。`);
      this.message(`${phase.toUpperCase()} 已填写推荐默认参数（thinking 开启、effort=${recommended.reasoning_effort}）${fallbackNote}，草稿尚未保存；请保存模型配置。`);
    }
    applyModelMaximum(phase) {
      if (this.closed) return;
      const reason = this.habitReason();
      if (reason) { this.text(phase + "-maximum-notice", reason); return; }
      const maximum = Bridge.modelMaximum(this.modelContext(phase), this.modelOptions[phase]);
      if (!maximum) {
        this.updateEffortOptions(phase);
        this.message(`${phase.toUpperCase()} 尚未核实该模型 / 端点的最高参数，请手填；当前参数未改动，模型草稿需保存后生效。`); return;
      }
      this.el(phase + "-max-tokens").value = maximum.max_tokens;
      this.el(phase + "-effort").value = maximum.reasoning_effort;
      if (maximum.thinking) this.el(phase + "-thinking").value = maximum.thinking;
      this.modelFieldChanged(phase, "effort");
      this.text(phase + "-maximum-notice", `已填写最高参数：max_tokens=${maximum.max_tokens}，reasoning_effort=${maximum.reasoning_effort}；本角色草稿尚未保存，请保存模型配置。`);
      this.message(`${phase.toUpperCase()} 已按该模型填写最高参数，草稿尚未保存；请保存模型配置。`);
    }
    updateHabitNote(phase) {
      const preset = Bridge.habitPresets().find(p => p.label === this.el(phase + "-habit").value);
      this.text(phase + "-habit-note", preset?.note || "请选择习惯预设。");
    }
    habitReason() {
      if (this.closed || this.busy || this.pending) return "请等待本次操作完成";
      if (!this.connected || this.pathsDirty || !this.credentialsVerified) return "请先连接并读取已有配置，避免覆盖尚未读取的设置。";
      if (this.active) return "已有任务正在运行，请结束后再填写习惯预设。";
      return "";
    }
    applyHabitPreset(phase) {
      if (this.closed) return;
      const reason = this.habitReason();
      if (reason) { this.text(phase + "-habit-note", reason); return; }
      const selected = Bridge.habitPresets().find(p => p.label === this.el(phase + "-habit").value);
      if (!selected) { this.text(phase + "-habit-note", "请选择已列出的习惯预设。"); return; }
      const profile = Bridge.habitPreset(selected.id);
      for (const [field, key] of [["provider", "provider"], ["model", "model"], ["effort", "reasoning_effort"],
        ["thinking", "thinking"], ["protocol", "protocol"], ["base-url", "base_url"], ["models-url", "models_url"],
        ["timeout", "timeout"], ["max-tokens", "max_tokens"]]) this.el(`${phase}-${field}`).value = profile[key];
      this.el(phase + "-extra-body").value = "{}";
      this.profileProviders[phase] = profile.provider; this.endpointConfirmed[phase] = false;
      this.clearModelOptions(phase);
      delete this.testStates[phase]; this.text(phase + "-test-state", "习惯预设已填入，尚未保存或测试。");
      this.text(phase + "-habit-note", selected.note + ` 已覆盖本角色草稿，尚未保存；thinking=enabled，effort=${profile.reasoning_effort}，输出上限 ${profile.max_tokens}，超时 1500 秒。请核对后保存。`);
      this.configDirty = true; this.markModelDirty(); this.update();
      this.message(`${phase.toUpperCase()} 已填入“${selected.label}”预设，草稿尚未保存；请核对后点击“保存模型配置”。`);
    }
    confirmEndpoint(phase, profile) {
      const trusted = [Bridge.profileDefaults(profile.provider), Bridge.hydrationDefaults(profile.provider)];
      if (trusted.some(d => profile.base_url === d.base_url && profile.models_url === d.models_url)) return profile;
      if (!this.endpointConfirmed[phase]) {
        const yes = this.window.confirm(`使用自定义 API 地址？\n${phase.toUpperCase()} 将向下面地址发送所选 ${profile.provider} 的 Key 和请求；论文运行时也会发送待处理正文。\nAPI：${profile.base_url}\n模型列表：${profile.models_url || "不请求列表"}\n协议：${profile.protocol}\n确认这是你信任的服务？`);
        if (!yes || this.closed) throw new Bridge.SafeError("INPUT", "未确认自定义 API 地址，未发送请求。");
        this.endpointConfirmed[phase] = true;
      }
      return { ...profile, endpoint_confirmed: true };
    }
    catalogIdentity(phase) {
      // Same provider + endpoint + protocol + credential generation share one
      // catalog. A key edit bumps the epoch, so late responses cannot apply.
      const context = this.modelContext(phase);
      return JSON.stringify([context.provider, Bridge.endpointSignature(context),
        this.keyEpoch[context.provider] || 0, this.credentials[context.provider] === true]);
    }
    catalogCacheGet(identity, ignoreTtl = false) {
      const entry = this.modelCatalogs.get(identity);
      if (!entry || !ignoreTtl && Date.now() - entry.at > 600000) return null;
      return entry;
    }
    catalogCacheSet(identity, choices) {
      if (this.modelCatalogs.size >= 8 && !this.modelCatalogs.has(identity)) {
        this.modelCatalogs.delete(this.modelCatalogs.keys().next().value);
      }
      this.modelCatalogs.set(identity, { choices, at: Date.now() });
    }
    readZcodePlans(value) {
      // Sanitized per-plan support metadata from the bridge; bounded strings only.
      if (!objectLike(value)) return null;
      const result = {};
      for (const plan of ["coding", "start"]) {
        const row = objectLike(value[plan]) ? value[plan] : {};
        result[plan] = {
          supported: row.supported === true,
          reason: typeof row.reason === "string" ? row.reason.slice(0, 200) : "",
          providerId: typeof row.provider_id === "string" ? row.provider_id.slice(0, 64) : "",
          models: Array.isArray(row.models) ? row.models.filter(m => typeof m === "string" && m.length <= 128).slice(0, 64) : []
        };
      }
      return result;
    }
    zcodePlanNotice(phase, plan) {
      if (!this.capabilities.includes("zcode-plans-v1")) {
        return "已选择 ZCode 登录：本机 bridge 是旧版（beta.13 及更早），其 ZCode 调用会复制本机完整凭据，beta.14 起已停用——测试、列表与保存均禁用，不再发送旧版请求。请更新本机 bridge 后重新连接，或先将该角色改选 API Key 服务商；已保存的 ZCode 配置仍显示于此、不会被改写。";
      }
      if (plan === "start") {
        const info = this.zcodePlans?.start;
        if (info?.supported === true) {
          return "已选 ZCode 登录 · Start Plan：走本机 CLI 的 Start Plan 服务商（zcode.z.ai 计划端点）。模型按本机实际清单列出（" +
            (info.models.length ? info.models.join(" / ") : "未提供") + "），不代表其它订阅权限；请测试连接确认。";
        }
        return "ZCode 登录 · Start Plan 在本机不可用：" + this.zcodeReasonText("start") +
          "。测试、列表与保存已禁用：不会改走 Coding Plan，也不会另发私有 OAuth 请求；请改选 API Key 服务商。";
      }
      const info = this.zcodePlans?.coding;
      if (info?.supported === true) {
        return "已选 ZCode 登录 · Coding Plan：显式走本机 CLI 的 bigmodel Coding Plan 服务商（open.bigmodel.cn/api/anthropic），不是自动路由。模型可选 " +
          (info?.models.length ? info.models.join(" / ") : "glm-5.3 / glm-5.3-flash") +
          "，推理档 default / low / high / max；每次调用启动无头 ZCode 进程，速度慢于直连 API，请保持 ZCode 已登录，测试连接可验证订阅可用性。";
      }
      const listed = info?.models.length ? "本机 CLI 配置提及模型 " + info.models.join(" / ") + "（仅显示，不代表可用）。" : "";
      return "ZCode 登录 · Coding Plan 在本机不可用：" + this.zcodeReasonText("coding") +
        "。测试、列表与保存已禁用，不会改走其它订阅或计费服务商。" + listed +
        "已保存的 ZCode 配置仍显示于此、不会被改写；请改选 API Key 服务商（GLM / DeepSeek / 同济）。";
    }
    zcodeReasonText(plan) {
      // Fixed reason-code explanations only; unknown backend codes collapse
      // into a generic sentence instead of echoing arbitrary text.
      const fixed = {
        login_route_unavailable: "本插件未实现安全的本机登录直连（不复制本机 ZCode 凭据、不启动无头 CLI 进程，安装版 CLI 也没有无头按模型调用入口）",
        start_route_not_implemented: "本插件没有可调用的 Start Plan 路由",
        cli_executable_unavailable: "未找到已安装的 zcode CLI 可执行文件",
        cli_config_unreadable: "无法读取本机 zcode CLI 配置",
        provider_absent: "本机 CLI 配置中没有 bigmodel 服务商",
        provider_config_unsupported: "本机 CLI 的 bigmodel 服务商不是官方登录托管的 Coding Plan 配置（端点或鉴权方式不符）",
        provider_models_unreadable: "本机 CLI 服务商的模型信息不可读",
        cli_adapter_unavailable: "随插件发布的运行时不包含 CLI 适配器"
      };
      const reason = this.zcodePlans?.[plan]?.reason;
      return reason && fixed[reason] ? fixed[reason] : "该订阅在本机不可用";
    }
    zcodeUnsupportedReason(phase) {
      if (this.el(phase + "-provider").value.trim().toLowerCase() !== "zcode") return "";
      if (!this.capabilities.includes("zcode-plans-v1")) {
        // beta.14 security decision: fail closed against the beta.13 route,
        // which copied this machine's full ZCode credentials into every
        // request. Never send legacy zcode requests; the draft stays visible
        // and editable so the user can migrate to an API-key provider.
        return "ZCode 登录已停用：本机 bridge 是旧版（beta.13 及更早），其 ZCode 调用会复制本机完整凭据，beta.14 起不再发送此类请求；请更新本机 bridge 后重新连接，或先将该角色改选 API Key 服务商再保存。";
      }
      const plan = this.el(phase + "-plan").value === "start" ? "start" : "coding";
      if (this.zcodePlans?.[plan]?.supported === true) return "";
      return "ZCode 登录（" + (plan === "start" ? "Start Plan" : "Coding Plan") + "）在本机不可用：" +
        this.zcodeReasonText(plan) + "；请改选 API Key 服务商";
    }
    savedZcodeReason(value = null) {
      // Fail closed for REQUIRED SAVED zcode roles: the saved login flag is
      // display-only, never an executable route — on the old bridge the
      // legacy request would reach the retired beta.13 login route, on the
      // new bridge every zcode route is unavailable. This reads the SAVED
      // profile (value or savedConfig), not the unsaved dropdown draft, and
      // honors translate=false: an unused zh zcode route must not block the
      // QC-only path.
      const config = value ? value.config : this.savedConfig;
      if (!config) return "";
      for (const role of ["qc", ...(this.el("translate").checked ? ["zh"] : [])]) {
        const profile = config[role];
        if (profile?.provider !== "zcode") continue;
        const label = role === "qc" ? "QC" : "翻译";
        if (!this.capabilities.includes("zcode-plans-v1")) {
          return "已保存的" + label + "角色使用 ZCode 登录，本机 bridge 是旧版，该调用路由不可用；" +
            "请更新本机 bridge 后重新连接，或先将该角色改选 API Key 服务商保存。";
        }
        const plan = profile.plan === "start" ? "start" : "coding";
        if (this.zcodePlans?.[plan]?.supported === true) continue;
        return "已保存的" + label + "角色使用 ZCode 登录（" + (plan === "start" ? "Start Plan" : "Coding Plan") +
          "），该调用路由在本机不可用：" + this.zcodeReasonText(plan) + "；请将该角色改选 API Key 服务商保存。";
      }
      return "";
    }
    presentBundledCatalog(phase) {
      // Offline snapshot seed for recognized default endpoints. Never cached in
      // modelCatalogs, never claimed as live, and never covering custom URLs.
      if (this.closed) return false;
      const context = this.modelContext(phase);
      const raw = Bridge.bundledCatalog(context);
      if (!raw) return false;
      let choices;
      try { choices = Bridge.modelChoices(raw, context.provider); }
      catch (_) { return false; }
      this.modelOptions[phase] = { ...choices, endpoint: Bridge.endpointSignature(context),
        epoch: this.keyEpoch[context.provider] || 0, seeded: true };
      this.pickers[phase + "-model"]?.setOptions(choices.models.map(id => ({ value: id,
        label: this.client.redact(choices.details[id]?.display_name ? `${choices.details[id].display_name} · API ID: ${id}` : id) })));
      this.updateEffortOptions(phase);
      this.text(phase + "-model-notice", "内置官方文档候选（离线快照，不是账号可用清单）；" + choices.models.length +
        " 项。点击“获取模型列表”联网刷新；刷新失败会保留这些候选。获取时间：未联网。");
      return true;
    }
    autoLoadCatalog(phase) {
      // Selecting a provider loads its catalog immediately — but only for the
      // trusted default endpoints and only via the merged, cached request.
      // Custom addresses always need the explicit button and confirmation.
      // A locally seeded bundled snapshot is not a fetch result: it must not
      // suppress the async refresh, whose live answer replaces it.
      if (this.closed || !this.connected || this.pathsDirty || this.busy || this.pending) return;
      if (!this.capabilities.includes("model-profiles-v2")) return;
      const context = this.modelContext(phase);
      if (context.provider === "tongji" && !this.capabilities.includes("tongji-api-v1")) return;
      if (!["glm_standard", "glm_coding_plan", "deepseek", "tongji", "zcode"].includes(Bridge.endpointKind(context))) return;
      const unsupported = this.zcodeUnsupportedReason(phase);
      if (unsupported) return;
      const options = this.modelOptions[phase];
      if (options?.provider === context.provider && options.endpoint === Bridge.endpointSignature(context)
          && options.seeded !== true) return;
      void this.catalogAction(phase, false);
    }
    async catalogAction(phase, force) {
      // Model listings never enter this.pending: fetching a catalog must not
      // lock the whole form, and QC/ZH may load different catalogs in parallel.
      if (this.closed) return;
      let submitted = false;
      try {
        if (this.busy || this.pending) return;
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接 bridge。");
        if (!this.capabilities.includes("model-profiles-v2")) throw new Bridge.SafeError("CAPABILITY", "请更新空闲的 bridge 后重新连接，新模型参数不能发送给旧服务。");
        const unsupportedPlan = this.zcodeUnsupportedReason(phase);
        if (unsupportedPlan) throw new Bridge.SafeError("CAPABILITY", unsupportedPlan);
        let model = Bridge.profile(this.readModel(phase), true);
        if (model.provider === "tongji" && !this.capabilities.includes("tongji-api-v1")) throw new Bridge.SafeError("CAPABILITY", "本机服务尚不支持同济 API，请更新 bridge 后重新连接；尚未发送请求。");
        if (model.provider === "zcode") {
          // No key exists for the local login; only its presence is checked.
          if (this.credentials.zcode !== true) throw new Bridge.SafeError("CONFIG", "本机 ZCode 尚未登录 GLM；请打开 ZCode 登录后重试，或改用 API Key 服务商。");
        } else {
          const docsOnly = Bridge.publicModelDocs(model);
          // Official GLM documentation fetching is credential-free. Listings for
          // any other endpoint follow the existing endpoint confirmation.
          if (!docsOnly) model = this.confirmEndpoint(phase, model);
          if (!this.credentials[model.provider] && !docsOnly) throw new Bridge.SafeError("CONFIG", "请先保存所选 provider 的密钥。");
          const typed = this.el(model.provider + "-key").value;
          if (typed && typed !== this.loadedKeys[model.provider]) {
            // A draft key must not be presented as the saved account: save first.
            throw new Bridge.SafeError("CONFIG", "该服务商 Key 已修改但尚未保存；请先保存 Key 再获取模型目录，目录不代表未保存的草稿凭据。");
          }
        }
        const identity = this.catalogIdentity(phase);
        const pending = this.catalogPending[phase];
        // In flight from submission until the pending record exists: rapid
        // repeated same-identity clicks merge into the one request instead of
        // racing duplicate remote POSTs past the record check.
        if (pending?.identity === identity || this.catalogInflight[phase]?.identity === identity) return;
        if (!force) {
          const cached = this.catalogCacheGet(identity);
          if (cached) { this.presentModelChoices(phase, cached.choices, "hit"); return; }
          const stale = this.catalogCacheGet(identity, true);
          if (stale) this.presentModelChoices(phase, stale.choices, "stale"); // Show, then still fetch fresh below.
        }
        const status = force
          ? "正在获取模型列表；成功后替换当前候选，失败保留现有候选。"
          : "正在按所选服务商自动获取模型列表（默认可信地址）；失败保留现有候选。";
        // An async auto refresh must not erase the provenance of the candidates
        // still on screen: keep the current source label (bundled offline
        // snapshot, cache, live listing) and only append the refresh status.
        const prior = this.el(phase + "-model-notice").textContent;
        this.text(phase + "-model-notice", !force && this.modelOptions[phase] && prior
          ? `${prior}（${status}）` : status);
        submitted = true;
        // The token is taken BEFORE the request: a slower, older POST resolving
        // later must not overwrite the record of a newer submission.
        const token = (this.catalogTokens[phase] = (this.catalogTokens[phase] || 0) + 1);
        this.catalogInflight[phase] = { identity, token };
        let id;
        try {
          // Old bridges reject unknown body fields: refresh:false only goes to
          // bridges advertising catalog-cache-v1; the legacy body means "fetch
          // live", which is what a forced refresh does anyway.
          id = await this.client.models(model.provider, model,
            this.capabilities.includes("catalog-cache-v1") ? force : true);
        } finally {
          if (this.catalogInflight[phase]?.token === token) delete this.catalogInflight[phase];
        }
        if (this.closed || this.catalogTokens[phase] !== token) return;
        this.catalogPending[phase] = { phase, id, token, identity, provider: model.provider, endpoint: Bridge.endpointSignature(model) };
        if (force) this.text("model-result", `操作 ${id} · 读取模型列表`);
        this.update();
      } catch (e) {
        if (this.closed) return;
        if (submitted) {
          this.text(phase + "-model-notice", "本次模型列表请求失败；保留现有候选与手填模型，可重试。");
          this.text("model-result", "模型列表请求失败；上次候选（如有）保留，不当作本次结果。可重试或手填模型 ID。");
        }
        this.error(e);
      }
    }
    applyRawCatalog(phase, record, raw) {
      if (this.closed) return;
      let choices;
      try { choices = Bridge.modelChoices(raw, record.provider); }
      catch (_) {
        this.text(phase + "-model-notice", "本次模型列表响应无效；保留现有候选，请重试或手填模型 ID。");
        this.text("model-result", "模型列表响应无效；当前模型及参数未改动。");
        return;
      }
      if (this.el(phase + "-provider").value.trim().toLowerCase() !== record.provider) {
        this.message("服务商已改变，丢弃旧模型列表；当前模型未改动。"); return;
      }
      const current = this.modelContext(phase);
      if (!record.endpoint || record.endpoint !== Bridge.endpointSignature(current)) {
        this.message("API 地址或协议已改变，旧模型列表未应用。"); return;
      }
      if (this.catalogIdentity(phase) !== record.identity) {
        this.message("Key 或配置已改变，本次目录响应未应用。"); return;
      }
      if (choices.cache === "stale") {
        // The bridge kept its last trusted list after a failed refresh.
        this.presentModelChoices(phase, choices, "stale");
        this.message("本次目录刷新失败，已显示上次候选（缓存，非实时）；可重试或手填模型 ID。", true);
        return;
      }
      if (choices.endpoint_kind && choices.endpoint_kind !== Bridge.catalogEndpointKind(current)) {
        this.clearModelOptions(phase);
        this.message("模型列表端点类型与当前 API 地址不符，列表未应用；请重新获取或手填。", true); return;
      }
      if (Bridge.endpointKind(current) === "glm_coding_plan") {
        const models = choices.models.filter(id => ["glm-5.3", "glm-5.3-flash"].includes(id));
        choices = { ...choices, models, details: Object.fromEntries(models.filter(id => Object.hasOwn(choices.details, id)).map(id => [id, choices.details[id]])) };
      }
      // A bridge cache hit must not renew the panel-side TTL: repeated hits
      // can never extend a stale entry's lifetime.
      if (choices.cache !== "hit") this.catalogCacheSet(record.identity, choices);
      this.presentModelChoices(phase, choices, choices.cache === "hit" ? "hit" : "");
      this.message("模型候选已载入，当前模型保持不变。"); this.update();
    }
    presentModelChoices(phase, choices, cacheMode = "") {
      if (this.closed) return;
      const context = this.modelContext(phase);
      this.modelOptions[phase] = { ...choices, endpoint: Bridge.endpointSignature(context),
        epoch: this.keyEpoch[context.provider] || 0 };
      this.pickers[phase + "-model"]?.setOptions(choices.models.map(id => ({ value: id,
        label: this.client.redact(choices.details[id]?.display_name ? `${choices.details[id].display_name} · API ID: ${id}` : id) })));
      this.updateEffortOptions(phase);
      const source = { bundled_catalog: "内置官方文档候选（非实时，不是账号可用清单）",
        official_docs: "本次联网获取的官方文档候选（不是账号可用清单）",
        official_cache: "官方文档缓存候选（非实时，不是账号可用清单）",
        provider_api: "本次服务商 API 返回的模型列表（不保证调用权限）" }[choices.source];
      // A served cache is never wording-level evidence of a fresh live listing.
      const cachedSources = { provider_api: "缓存的服务商 API 模型列表（非实时，不保证调用权限）",
        official_docs: "缓存的官方文档候选（非实时，不是账号可用清单）",
        official_cache: "官方文档缓存候选（非实时，不是账号可用清单）",
        bundled_catalog: "内置官方文档候选（非实时，不是账号可用清单）" };
      const labeled = cacheMode ? (cachedSources[choices.source] || source) : source;
      const cacheNote = cacheMode === "hit" ? "缓存命中（10 分钟内，本次未发起新请求）。"
        : cacheMode === "stale" ? "上次候选缓存（非实时；获取时间见下）。" : "";
      const note = `${cacheNote}${labeled}；${choices.models.length} 项。获取时间：${choices.fetched_at || "未提供"}。` +
        (choices.endpoint_kind ? `端点类型：${choices.endpoint_kind}。` : "") + (choices.reference ? `参考：${choices.reference}` : "");
      this.text(phase + "-model-notice", note);
      this.text("model-result", `${phase.toUpperCase()}：${note}\n${choices.notice}\n展示名 / 版本名仅供识别，请求使用 API ID。选择已知模型会在本角色草稿填写推荐默认参数（thinking 开启、effort=high，无 high 时按文档档位回退；最高参数可点按钮另填），请保存模型配置。候选列表不能代替实际测试。`);
      this.modelResultPhase = phase;
      this.update();
    }
    async init(selected = false) {
      this.bootStage = "paths";
      const paths = this.host.getPaths();
      this.el("repo").value = paths.repo; this.el("pythonw").value = paths.pythonw;
      this.bootStage = "pickers";
      this.initPickers();
      this.fillConfig({ configured: false, config: { schema: 1, qc: Bridge.profileDefaults("glm"), zh: Bridge.profileDefaults("glm") }, credentials: this.credentials }, false);
      this.bootStage = "listeners";
      this.listen("close-panel", "click", () => this.window.close());
      for (const id of ["glm53", "glm-request", "glm-thinking", "glm-params", "deepseek-thinking", "deepseek-request", "deepseek-context"]) {
        this.listen("docs-" + id, "click", () => {
          try { this.host.openReference(id); }
          catch (_) { this.message("无法打开官方文档，请检查默认浏览器。", true); }
        });
      }
      this.listen("jump-models", "click", () => {
        this.showPage("settings");
        this.el("models-title").scrollIntoView({ block: "start", behavior: "smooth" });
        this.el("models-title").focus({ preventScroll: true });
      });
      for (const page of ["sync", "task", "settings"]) this.listen("nav-" + page, "click", () => this.showPage(page));
      this.listen("current-task", "click", () => this.showCurrentTask());
      this.listen("view-resumed", "click", () => this.viewHistory(this.displayedStatus?.view?.resumed_as));
      this.listen("refresh-history", "click", () => this.action(() => this.loadHistory()));
      this.listen("history-rows", "click", event => {
        for (let node = event.target; node && node !== this.el("history-rows"); node = node.parentNode) {
          if (node.historyRun) return this.viewHistory(node.historyRun);
        }
      });
      this.listen("pause", "click", () => this.runAction("pause"));
      this.listen("resume", "click", () => this.runAction("resume"));
      this.listen("resume-finalize", "click", () => this.runAction("resume-finalize"));
      this.listen("recover-pause", "click", () => this.runAction("recover-pause"));
      this.listen("diagnosis-close", "click", () => this.closeDiagnosis());
      this.listen("status-rows", "click", event => {
        for (let node = event.target; node && node !== this.el("status-rows"); node = node.parentNode) {
          if (node.diagnosisKey) return this.showDiagnosis(node.diagnosisKey);
        }
      });
      this.listen("diagnosis-remedies", "click", event => {
        for (let node = event.target; node && node !== this.el("diagnosis-remedies"); node = node.parentNode) {
          if (node.remedyAction) return this.remedyAction(node.remedyAction, node.remedyMeta || {});
        }
      });
      this.listen("acronym-save", "click", () => this.saveAcronym());
      this.listen("preview-rows", "change", event => this.changeSelection(event.target));
      this.listen("connect", "click", () => this.connect(true));
      this.listen("restart-backend", "click", () => this.restartBackend());
      this.listen("save-paths", "click", () => this.savePaths());
      this.listen("select-items", "click", () => this.captureSelection());
      this.listen("discover", "click", () => this.previewScope([]));
      this.listen("preview", "click", () => this.previewScope(this.keys));
      this.listen("start", "click", () => this.start());
      this.listen("refresh", "click", () => this.tick());
      this.listen("copy-status", "click", () => this.copyDiagnostics());
      this.listen("save-config", "click", () => this.saveConfig());
      this.listen("save-keys", "click", () => this.saveConfig(true));
      this.listen("reload-config", "click", () => this.reloadConfig());
      this.listen("save-mineru", "click", () => this.saveMineru());
      this.listen("reload-mineru", "click", () => this.action(async () => {
        this.fillMineru(await this.client.getMineru()); await this.loadKeys(["mineru"], true);
      }));
      for (const provider of ["glm", "deepseek", "tongji", "mineru"]) {
        this.listen(provider + "-key", "input", () => {
          this.keyDirty[provider] = true;
          const value = this.el(provider + "-key").value;
          if (value) this.client.sensitive.add(value);
          if (value !== this.loadedKeys[provider]) for (const phase of ["qc", "zh"]) {
            if (this.el(phase + "-provider").value === provider) {
              delete this.testStates[phase]; this.text(phase + "-test-state", "Key 已修改，尚未测试此凭据。");
            }
          }
          this.updateKeyDisplays();
        });
      }
      for (const phase of ["qc", "zh"]) {
        this.clearModelOptions(phase);
        this.el(`${phase}-max-tokens`).max = String(Bridge.tokenInputLimit(this.el(`${phase}-provider`).value));
        this.listen(phase + "-test", "click", () => this.modelAction(phase, "test"));
        this.listen(phase + "-models", "click", () => this.modelAction(phase, "models"));
        this.listen(phase + "-defaults", "click", () => this.applyProviderDefaults(phase));
        this.listen(phase + "-apply-habit", "click", () => this.applyHabitPreset(phase));
        this.listen(phase + "-maximum", "click", () => this.applyModelMaximum(phase));
        this.listen(phase + "-model", "change", () => {
          this.modelFieldChanged(phase, "model"); this.applyModelDefaults(phase);
        });
        this.listen(phase + "-provider", "change", () => this.modelFieldChanged(phase, "provider"));
        for (const field of ["provider", "model", "effort", "thinking", "protocol", "base-url", "models-url", "extra-body", "timeout", "max-tokens"]) {
          this.listen(`${phase}-${field}`, "input", () => this.modelFieldChanged(phase, field));
        }
      }
      for (const id of ["glm-key", "deepseek-key", "tongji-key"]) this.listen(id, "input", () => {
        const provider = id.replace("-key", "");
        this.keyEpoch[provider] = (this.keyEpoch[provider] || 0) + 1; // Catalog identity changes with credentials.
        // A draft key retires the old account's listing; the typed model stays.
        for (const phase of ["qc", "zh"]) if (this.el(phase + "-provider").value === provider) {
          this.clearModelOptions(phase);
          this.text(phase + "-model-notice", "该服务商 Key 已修改但尚未保存；现有候选已作废，请先保存 Key 再获取目录。");
        }
        this.configDirty = true; this.update();
      });
      for (const id of ["repo", "pythonw"]) this.listen(id, "input", () => {
        this.clearSecrets(); this.pathsDirty = true; this.invalidate(); this.update();
      });
      for (const id of ["workers", "chunk-parallel", "limit"]) this.listen(id, "input", () => { this.invalidate(); this.update(); });
      this.listen("translate", "change", () => { this.invalidate(); this.update(); });
      this.listen("refresh-source", "change", () => {
        if (!this.keys?.length || !this.capabilities.includes("source-refresh-v1")) {
          this.el("refresh-source").checked = false;
          this.message(!this.keys?.length ? "请先选择当前文献；自动发现不支持更换全文 PDF。" : "本机 bridge 不支持更换全文 PDF，请更新 bridge 后重新连接。", true);
        }
        this.invalidate(); this.update();
      });
      this.listen("mineru-key", "input", () => this.update());
      if (selected) this.captureSelection();
      this.update();
      this.bootStage = "ready";
      // Explicit handshake: the full settings UI is usable from here on. This
      // fires BEFORE any bridge/model work, so a missing backend, offline
      // machine or failed login can never hold the window past the host's
      // bounded ready timeout.
      this.signalReady();
      // Initial open is read-only. Only the explicit connect button can launch a service.
      await this.connect(false);
      if (!this.closed) this.timer = this.window.setInterval(() => { void this.tick(); }, 3000);
    }
    signalReady() {
      if (this.closed || this.readySignaled) return;
      this.readySignaled = true;
      const boot = this.el("panel-boot");
      if (boot) boot.hidden = true;
      try { this.host.panelReady?.(this.window); } catch (_) { /* handshake only */ }
      try {
        if (typeof this.window.Event === "function" && typeof this.window.dispatchEvent === "function") {
          this.window.librarySyncReady = true;
          this.window.dispatchEvent(new this.window.Event("library-sync-ready"));
        }
      } catch (_) { /* the host fallback callback already ran */ }
    }
    bootFailure(error, stage = this.bootStage) {
      // Fixed stage labels and SafeError messages only; never exception bodies.
      if (this.closed) return;
      const safe = error instanceof Bridge.SafeError || error?.name === "SafeError" ? error.message : "";
      const text = `面板初始化失败（阶段 ${stage || "unknown"}）。` +
        (safe || "请关闭本窗口重新打开；若仍失败请重启 Zotero 后重试。已运行的后台任务不受影响。");
      try {
        const message = this.el("message");
        if (message) { message.hidden = false; message.setAttribute("data-error", "true"); message.textContent = this.client.redact(text); }
        const boot = this.el("panel-boot");
        if (boot) boot.textContent = "面板初始化失败；本窗口可关闭后重新打开。";
      } catch (_) { /* the static markup still shows its own guidance */ }
    }
    invalidate() {
      this.preview = null; this.startResult = null; this.startAttempted = false;
      this.modelDirty = false; this.acronymTitles = [];
      this.el("acronym-section").hidden = true;
      this.selections = []; this.selectionControls = [];
      this.text("operation", "范围或配置已改变，请重新预演");
      this.text("preview-text", "尚无有效预演。请先预演再确认开始。");
      this.el("preview-rows").replaceChildren();
      this.el("deferred-notice").hidden = true; this.text("deferred-notice", "");
    }
    markModelDirty() {
      if (!this.preview || this.startAttempted) return;
      this.modelDirty = true;
      this.text("operation", `预演 ${this.preview.preview_id} 保留；模型草稿已修改，保存后自动更新预演绑定，逐篇选择不变`);
    }
    update() {
      if (this.closed) return;
      const locked = this.busy || !!this.pending;
      const lockReason = locked ? "请等待本次操作完成" : "";
      const connectionReason = this.pathsDirty ? "路径已修改，请先保存并重新连接" : !this.connected ? "请先连接本机服务" : "";
      const activeReason = this.active ? "已有任务正在运行；修改可留作下次任务草稿" : "";
      const readyReason = connectionReason || this.requiredTongjiReason() || this.savedZcodeReason() ||
        (!this.configurationReady() ? "请先保存所需模型配置与 Key" : "") ||
        (this.configDirty ? "存在未保存修改，请先保存配置" : "") ||
        (this.modelDirty ? "模型配置已修改，保存后需更新预演绑定或重新预演" : "") ||
        (!this.capabilities.includes("translation-policy-v1") ? "请更新服务以支持翻译选项" : "");
      const refreshReason = !this.capabilities.includes("source-refresh-v1") ? "本机 bridge 不支持更换全文 PDF，请更新 bridge 后重新连接。" : "";
      const refreshPreviewReason = this.el("refresh-source").checked ? refreshReason : "";
      const scopeReason = lockReason || readyReason || (!this.statusKnown ? "当前状态读取未成功，请刷新状态" : "") || activeReason;
      this.disable("connect", this.busy ? "正在处理操作" : this.pathsDirty ? "请先保存修改后的路径" : "");
      this.disable('restart-backend', this.restartReason());
      this.disable("save-paths", lockReason);
      this.disable("select-items", lockReason);
      this.disable("reload-config", lockReason || connectionReason);
      for (const [id, capability] of [["save-config", "model-profiles-v2"], ["save-keys", "keys-v1"], ["save-mineru", "mineru-v1"]]) {
        this.disable(id, lockReason || connectionReason ||
          (id !== "save-config" ? activeReason : "") ||
          (id === "save-config" && (this.zcodeUnsupportedReason("qc") || this.zcodeUnsupportedReason("zh")) ? "所选 ZCode 登录本机不可用，不能保存无法执行的配置；请将该角色改选 API Key 服务商" : "") ||
          (id !== "save-mineru" && this.tongjiSupportReason(id === "save-keys") || "") ||
          (!this.capabilities.includes(capability) ? "本机服务不支持此设置，请更新服务" : ""));
      }
      this.disable("reload-mineru", lockReason || connectionReason || (!this.capabilities.includes("mineru-v1") ? "本机服务不支持读取此设置" : ""));
      this.disable("refresh-source", lockReason || connectionReason || (!this.keys?.length ? "仅选择模式可用，请先选择当前文献；自动发现不能更换全文。" : refreshReason));
      this.el("refresh-source").setAttribute("aria-describedby", "refresh-source-help refresh-source-reason");
      this.disable("preview", scopeReason || refreshPreviewReason || (!this.keys?.length ? "请先选择文献" : ""));
      this.disable("discover", scopeReason);
      const viewTask = this.startResult || this.active && !this.startAttempted;
      const count = this.selections.filter(row => row.include).length;
      this.el("start").textContent = viewTask ? "查看当前任务" : this.startAttempted ? "重试确认同一任务" : "确认同步 " + count + " 篇";
      this.disable("start", viewTask ? "" : lockReason || readyReason || refreshPreviewReason ||
        (!this.statusKnown ? "当前状态读取未成功，请刷新状态" : "") ||
        (!this.preview ? "请先完成预演" : !count ? "请至少勾选一篇文献" : ""));
      for (const phase of ["qc", "zh"]) {
        const planCapable = this.capabilities.includes("zcode-plans-v1");
        const isZcode = this.el(phase + "-provider").value.trim().toLowerCase() === "zcode";
        this.el(phase + "-plan-row").hidden = !isZcode || !planCapable;
        this.disable(phase + "-plan", lockReason || (isZcode && !planCapable ? "本机 bridge 不支持订阅选择；旧版 ZCode 调用已停用，请更新 bridge 或改选 API Key 服务商" : ""));
        for (const action of ["test", "models"]) this.disable(phase + "-" + action,
          lockReason || connectionReason || activeReason ||
          this.zcodeUnsupportedReason(phase) ||
          (this.el(phase + "-provider").value === "tongji" && !this.capabilities.includes("tongji-api-v1") ? "本机服务尚不支持同济 API，请更新 bridge 后重新连接" : "") ||
          (!this.credentials[this.el(phase + "-provider").value] && !(action === "models" && Bridge.publicModelDocs(this.modelContext(phase))) ? "请先保存所选服务商的 Key" : "") ||
          (!this.capabilities.includes("model-profiles-v2") || action === "test" && !this.capabilities.includes("model-test-role-v1") ? "本机服务不支持此模型操作，请更新" : ""));
        this.el(phase + "-max-tokens").max = String(Bridge.tokenInputLimit(this.el(phase + "-provider").value));
        this.disable(phase + "-maximum", this.habitReason() ||
          (!Bridge.modelMaximum(this.modelContext(phase), this.modelOptions[phase]) ? "尚未核实该模型 / 端点的最高参数，请按文档手填。" : ""));
      }
      for (const id of ["repo", "pythonw", "workers", "chunk-parallel", "limit", "translate", "glm-key", "deepseek-key", "tongji-key", "mineru-key",
        ...["qc", "zh"].flatMap(p => ["provider", "model", "effort", "thinking", "protocol", "base-url", "models-url", "extra-body", "timeout", "max-tokens", "defaults", "habit", "plan"].map(f => p + "-" + f))]) {
        this.disable(id, lockReason);
      }
      for (const phase of ["qc", "zh"]) this.disable(phase + "-apply-habit", this.habitReason());
      for (const phase of ["qc", "zh"]) if (this.el(phase + "-provider").value === "tongji") {
        this.disable(phase + "-thinking", "同济平台通过 reasoning_effort 控制思考，此项固定为 default");
      }
      for (const [id, picker] of Object.entries(this.pickers)) {
        picker.setDisabled(this.el(id).disabled);
        this.el(id + "-toggle").setAttribute("title", locked ? lockReason :
          this.el(id + "-toggle").disabled ? "暂无候选，可直接手填；模型候选需点击获取" : "展开候选");
      }
      this.disable("refresh", lockReason || connectionReason);
      this.disable("refresh-history", lockReason || connectionReason);
      this.disable("current-task", !this.selectedRun ? "正在显示当前或最近任务" : "");
      const resumed = this.displayedStatus?.view?.resumed_as;
      this.el("view-resumed").hidden = typeof resumed !== "string" || !Bridge.RUN_ID.test(resumed);
      this.disable("view-resumed", this.el("view-resumed").hidden ? "暂无已派发的恢复任务" : lockReason || connectionReason);
      this.el("view-resumed-reason").hidden ||= this.el("view-resumed").hidden;
      for (const action of ["pause", "resume", "resume-finalize", "recover-pause"]) this.disable(action, this.runActionReason(action));
      // Legacy receipts may allow finalization without supporting content resume.
      this.el("resume").hidden = !!this.displayedStatus?.view && !this.active &&
        this.displayedStatus.view.can_resume !== true && !this.isPauseRequested() && this.capabilities.includes("pause-resume-v1");
      this.el("resume-reason").hidden ||= this.el("resume").hidden;
      const canRecover = this.displayedStatus?.view?.can_recover_pause === true;
      this.el("recover-pause").hidden = !canRecover && !this.isLegacyPauseRequested();
      this.el("recover-pause-reason").hidden ||= this.el("recover-pause").hidden;
      for (const control of this.selectionControls) {
        control.disabled = locked || this.startAttempted || control.unavailable ||
          control.selectionField === "translate" && !this.selections.find(row => row.key === control.selectionKey)?.include;
        control.setAttribute("title", control.disabled ? control.selectionReason ||
          (this.startAttempted ? "该预演已提交，逐篇选项已固定" : locked ? lockReason : "请先包含此文献") : "");
      }
      this.text("draft-state", (this.active ? "运行中的任务使用冻结配置；这里的修改用于下一次任务。" : "配置用于下次任务。") +
        (this.configDirty ? " 有未保存修改。" : this.modelDirty ? " 模型已保存，但预演绑定未更新；开始前需更新绑定或重新预演。" : " 当前无未保存的模型修改。"));
      this.updateKeyDisplays();
    }
    async action(fn, allowPending = false) {
      if (this.closed || this.busy || (this.pending && !allowPending)) return;
      this.epoch++; // An older polling response must not overwrite a click's fresh state.
      this.busy = true; this.update();
      try { await fn(); } catch (e) { if (!this.closed) this.error(e); }
      finally { this.busy = false; this.update(); }
    }
    restartReason() {
      if (this.busy || this.pending || this.remedyBusy || Object.keys(this.catalogPending).length) return '请等待后台操作完成';
      if (!this.connected || !this.statusKnown || this.pathsDirty) return '请先连接并核实当前后台';
      if (this.active) return '任务运行中不能重启后台';
      if (typeof this.host.restartBackend !== 'function') return '当前宿主不支持后台升级，请更新 XPI';
      return '';
    }
    async restartBackend() {
      const reason = this.restartReason();
      if (reason) { this.message(reason, true); return; }
      let replaced = false;
      await this.action(async () => {
        const status = await this.client.getStatus();
        if (status.active) throw new Bridge.SafeError('RESTART', '任务正在运行，未重启');
        const pid = status.backend?.pid || this.client.session?.pid;
        this.message('正在核验并更新空闲后台；旧版未完成的列表查询/连接测试可能取消…');
        await this.host.restartBackend(pid);
        this.epoch++; this.client.close(); this.client = this.host.createClient();
        this.connected = false; this.statusKnown = false; this.capabilities = [];
        if (this.preview) this.invalidate(); // Preview tokens belonged to the old service.
        replaced = true;
      });
      if (replaced && !this.closed) await this.connect(true);
    }
    async connect(launch) {
      return this.action(async () => {
        if (this.pathsDirty) throw new Bridge.SafeError("PATH", "请先保存修改后的路径。");
        if (!this.el("repo").value.trim() || !this.el("pythonw").value.trim()) {
          this.el("paths-details").open = true;
          throw new Bridge.SafeError("PATH", "尚未找到后台程序。XPI 只安装界面；已有用户请填写原程序目录和 Python 执行文件，新用户先安装对应系统的后台包。填写后点击“保存路径”，再连接。");
        }
        const keepDraft = this.configDirty;
        this.connected = false; this.statusKnown = false; this.configured = false; this.credentialsVerified = false;
        this.message(launch ? "正在连接 bridge；仅在未连接时请求启动服务…" : "正在只读检查已有 bridge…");
        try {
          let status;
          try { status = await this.client.connect(launch); }
          catch (e) {
            if (!["HTTP", "PROTOCOL", "RESPONSE"].includes(e.code)) throw e;
            // A response from the status service proves reachability, not valid progress.
            this.connected = true; this.statusError(e);
          }
          if (this.closed) return;
          this.connected = true;
          if (status) this.renderStatus(status);
          // A config failure must not suppress the status panel or its polling.
          this.fillConfig(await this.client.getConfig(), true, keepDraft);
          if (this.capabilities.includes("mineru-v1")) this.fillMineru(await this.client.getMineru());
          else this.text("mineru-state", "本机服务仍是旧版，尚不能核实或更新 MinerU Key。");
          await this.loadKeys();
          const currentSettings = ["translation-policy-v1", "model-profiles-v2", "credential-reveal-v1"].every(c => this.capabilities.includes(c));
          this.message(currentSettings
            ? "bridge 已连接；可先保存 API Keys / MinerU Key，再填写模型并预演。"
            : "已连接旧版 bridge；新设置暂禁用。请先更新并重启空闲的本机服务，再重新连接。", !currentSettings);
        } catch (e) {
          if (!this.connected) this.text("connection", "未连接；状态暂不可用");
          throw e;
        }
      }, true);
    }
    savePaths() {
      return this.action(async () => {
        this.clearSecrets(); this.mineruConfigured = null;
        const value = this.host.setPaths({ repo: this.el("repo").value, pythonw: this.el("pythonw").value });
        this.epoch++;
        this.client.close(); this.client = this.host.createClient();
        this.pathsDirty = false; this.connected = false; this.configured = false; this.configDirty = false;
        this.statusKnown = false; this.report = "";
        this.currentStatus = null; this.displayedStatus = null; this.selectedRun = null;
        this.viewKnown = false; this.viewEpoch++; this.pauseRun = null; this.resumePending = null;
        this.history = []; this.rowSignatures.clear(); this.el("history-rows").replaceChildren();
        this.modelCatalogs.clear(); this.catalogPending = {};
        // Supersede any in-flight catalog POST from the previous repository.
        this.catalogTokens = { qc: (this.catalogTokens.qc || 0) + 1, zh: (this.catalogTokens.zh || 0) + 1 };
        this.text("history-notice", "尚未读取新目录的历史。"); this.text("task-notice", "");
        this.text("frozen-config", "暂无本任务配置。"); this.el("status-error").hidden = true; this.el("copy-status").hidden = true;
        this.el("repo").value = value.repo; this.el("pythonw").value = value.pythonw;
        this.el("glm-key").value = ""; this.el("deepseek-key").value = ""; this.el("tongji-key").value = ""; this.el("mineru-key").value = "";
        this.capabilities = []; this.text("mineru-state", "尚未核实新目录的 MinerU 保存状态。");
        for (const phase of ["qc", "zh"]) this.clearModelOptions(phase);
        this.invalidate(); this.fillConfig({ configured: false, config: { schema: 1, qc: Bridge.profileDefaults("glm"), zh: Bridge.profileDefaults("glm") }, credentials: { glm: false, deepseek: false, tongji: false } }, false);
        this.text("connection", "路径已保存，请连接服务"); this.text("status-text", "尚未读取新目录的状态。");
        this.text("summary", "尚未读取状态"); this.text("report-path", this.reportHint());
        this.el("status-rows").replaceChildren(); this.closeDiagnosis(); this.message("已保存 Repo 与 Pythonw 两个路径。");
      });
    }
    captureSelection() {
      if (this.closed || this.busy || this.pending) return;
      try {
        const selected = this.host.getSelection();
        this.keys = selected.items.map(x => x.key);
        this.primaryKeys = Object.fromEntries(selected.items.filter(x => x.primary_key !== undefined).map(x => [x.key, x.primary_key]));
        if (!this.keys.length) this.el("refresh-source").checked = false;
        this.invalidate();
        this.text("selection", selected.items.map(x => `${x.key}  ${x.title}${x.primary_key ? "  · 全文候选附件 " + x.primary_key : ""}`).join("\n") || "未选中可同步的文献。空选择不会触发新增发现。");
        this.text("scope", `所选文献 ${this.keys.length} 篇${selected.ignored ? `；忽略 ${selected.ignored} 个无父文献条目` : ""}`);
      } catch (e) { this.keys = null; this.primaryKeys = {}; this.el("refresh-source").checked = false; this.invalidate(); this.error(e); }
      this.update();
    }
    requireReady() {
      if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先保存路径并连接 bridge。");
      const schoolReason = this.requiredTongjiReason();
      if (schoolReason) throw new Bridge.SafeError("CAPABILITY", schoolReason);
      if (!this.capabilities.includes("translation-policy-v1")) throw new Bridge.SafeError("CAPABILITY", "本机 bridge 仍是旧版；请更新服务再预演，不能忽略本次翻译选项。");
      if (this.el("refresh-source").checked && !this.capabilities.includes("source-refresh-v1")) throw new Bridge.SafeError("CAPABILITY", "本机 bridge 不支持更换全文 PDF，请更新 bridge 后重新连接；尚未发送更换请求。");
      const zcodeReason = this.savedZcodeReason();
      if (zcodeReason) throw new Bridge.SafeError("CAPABILITY", zcodeReason);
      if (!this.configurationReady() || this.configDirty) throw new Bridge.SafeError("CONFIG", "请先保存所需模型配置与 Key；运行状态仍可查看。");
    }
    configurationReady(value = null) {
      const config = value ? value.config : this.savedConfig, credentials = value ? value.credentials : this.credentials;
      // A saved zcode login flag is not an executable route: required saved
      // zcode roles fail closed here even though the credential exists.
      return !!config && !this.savedZcodeReason(value) && ["qc", ...(this.el("translate").checked ? ["zh"] : [])].every(
        role => !!config[role]?.model && !!credentials[config[role]?.provider]);
    }
    requiredTongjiReason(value = null) {
      const config = value ? value.config : this.savedConfig;
      if (!this.capabilities.includes("tongji-api-v1") && ["qc", ...(this.el("translate").checked ? ["zh"] : [])].some(
        role => config?.[role]?.provider === "tongji")) {
        return "本机服务尚不支持所需角色的同济 API，请更新 bridge 后重新连接；尚未发送预演或开始请求";
      }
      return "";
    }
    previewScope(keys) {
      if (!this.closed && !this.busy && !this.pending && Array.isArray(keys) && !keys.length) {
        this.el("refresh-source").checked = false; this.primaryKeys = {}; this.keys = [];
        this.invalidate(); this.update();
      }
      return this.action(async () => {
        this.requireReady();
        if (!Array.isArray(keys)) throw new Bridge.SafeError("SELECTION", "请先选择文献或点击检测新增。");
        this.renderStatus(await this.client.getStatus());
        if (this.closed) return;
        if (this.active) throw new Bridge.SafeError("ACTIVE", "已有任务运行，请查看当前进度。");
        this.requireReady(); // The fresh status may advertise different capabilities.
        const refresh = this.el("refresh-source").checked;
        const body = Bridge.previewBody({ keys, workers: Number(this.el("workers").value),
          chunk_parallel: Number(this.el("chunk-parallel").value), limit: Number(this.el("limit").value),
          translate: this.el("translate").checked,
          ...(refresh ? { refresh_source: true, primary_keys: this.primaryKeys } : {}) });
        this.invalidate(); this.keys = [...body.keys];
        this.text("scope", keys.length ? `所选文献 ${keys.length} 篇` : "任意分类新增发现（由控制器基线确定）");
        if (!keys.length) this.text("selection", "扫描任意分类的新增文献；无有效发现基线时由控制器报告阻碍。");
        const id = await this.client.preview(body);
        if (this.closed) return;
        this.pending = { id, kind: "preview" };
        this.text("operation", `预演进行中 · ${id}`); this.message("正在预演，尚未启动同步。");
      });
    }
    async tick() {
      if (this.closed || this.ticking || this.busy || !this.connected || this.pathsDirty) return;
      this.ticking = true;
      const epoch = this.epoch, viewEpoch = this.viewEpoch, selected = this.selectedRun;
      try {
        try { this.renderStatus(await this.client.getStatus(), epoch); }
        catch (e) { if (!this.closed && epoch === this.epoch) this.statusError(e); }
        if (this.closed || epoch !== this.epoch || !this.connected) return;
        if (selected && viewEpoch === this.viewEpoch) {
          try {
            const value = await this.client.getStatus(selected);
            if (!this.closed && epoch === this.epoch && viewEpoch === this.viewEpoch) this.displayStatus(value, true);
          } catch (e) { if (!this.closed && epoch === this.epoch && viewEpoch === this.viewEpoch) this.statusError(e, true); }
        }
        if (this.closed || epoch !== this.epoch || !this.connected) return;
        // A bundled offline seed shown at open (or after an endpoint edit) is
        // not a fetch result: recognized credential-free GLM doc endpoints
        // auto-refresh asynchronously on the next poll instead of requiring
        // the manual button. Keyed listings keep their explicit button.
        for (const phase of ["qc", "zh"]) {
          const options = this.modelOptions[phase];
          if (options?.seeded !== true || options.seedRefreshed) continue;
          const context = this.modelContext(phase);
          if (!this.busy && !this.pending && this.connected && !this.pathsDirty
              && this.capabilities.includes("model-profiles-v2")
              && Bridge.publicModelDocs(context)) {
            this.modelOptions[phase] = { ...options, seedRefreshed: true };
            this.autoLoadCatalog(phase);
          }
        }
        if (this.closed || epoch !== this.epoch || !this.connected) return;
        if (this.pending) {
          const pending = this.pending;
          let result;
          try { result = await this.client.operation(pending.id); }
          catch (e) {
            if (!this.closed && epoch === this.epoch && pending === this.pending) {
              this.error(e);
              if (e.code === "HTTP" && [400, 404, 410].includes(e.httpStatus)) {
                this.pending = null;
                if (pending.kind === "preview") this.invalidate();
                this.text("operation", "操作已失效，请重新预演或测试");
              }
            }
            return;
          }
          if (this.closed || epoch !== this.epoch || pending !== this.pending) return;
          if (result.status !== "running") {
            this.pending = null;
            if (result.status === "failed") {
              if (pending.kind === "preview") {
                this.preview = null;
                this.text("operation", "操作失败 · " + pending.id);
                this.message("预演失败：" + this.client.redact(result.error || "请查看报告"), true);
              } else {
                const detail = result.error === "model_listing_not_supported"
                  ? "当前服务不支持模型列表查询；请更新到新版候选列表，或手填模型 ID。Key 保存状态未改变。"
                  : "模型操作失败：" + this.client.redact(result.error || "请检查所选服务商、模型及权限");
                this.text("model-result", detail); this.message(detail, true);
                if (pending.kind === "test") this.text(pending.phase + "-test-state", "测试失败 · " + new Date().toLocaleString());
              }
            } else if (pending.kind === "preview") this.renderPreview(result.result);
            else this.renderModelResult(pending, result.result);
          }
        }
        // Model catalog listings poll independently: they never locked the form
        // and their late responses are dropped when the endpoint or key epoch
        // has moved on. All pending listings are polled in parallel so one slow
        // role cannot delay another role's completed result.
        if (this.closed || epoch !== this.epoch || !this.connected) return;
        const phases = Object.keys(this.catalogPending);
        if (!phases.length) return;
        const polled = await Promise.all(phases.map(async phase => {
          const record = this.catalogPending[phase];
          if (!record) return null;
          try { return { phase, record, result: await this.client.operation(record.id) }; }
          catch (e) { return { phase, record, error: e }; }
        }));
        if (this.closed || epoch !== this.epoch || !this.connected) return;
        for (const entry of polled) {
          if (!entry) continue;
          const { phase, record } = entry;
          // A newer submission for this phase must keep its own record.
          if (this.catalogPending[phase] !== record) continue;
          if (entry.error) {
            delete this.catalogPending[phase];
            this.text(phase + "-model-notice", "本次模型列表请求失败；保留现有候选与手填模型，可重试。");
            if (entry.error.code === "HTTP" && [400, 404, 410].includes(entry.error.httpStatus)) {
              this.text("model-result", "目录操作已失效，请重新获取或手填模型 ID。");
            } else this.error(entry.error);
            continue;
          }
          if (entry.result.status !== "running") {
            delete this.catalogPending[phase];
            if (entry.result.status === "failed") {
              const detail = entry.result.error === "model_listing_not_supported"
                ? "当前服务不支持模型列表查询；请更新到新版候选列表，或手填模型 ID。Key 保存状态未改变。"
                : "模型列表获取失败：" + this.client.redact(entry.result.error || "请检查所选服务商、模型及权限");
              this.text("model-result", detail + "；上次候选（如有）保留。");
              this.text(phase + "-model-notice", "本次获取失败；保留现有候选与手填模型，可重试或手填模型 ID。");
              this.message(detail, true);
            } else this.applyRawCatalog(phase, record, entry.result.result);
          }
        }
      } catch (e) { if (!this.closed && epoch === this.epoch) this.error(e); }
      finally { this.ticking = false; this.update(); }
    }
    renderStatus(status, epoch = this.epoch) {
      if (this.closed || epoch !== this.epoch) return;
      this.active = status.active; this.statusKnown = true; this.currentStatus = status;
      if (Array.isArray(status.capabilities)) this.capabilities = status.capabilities;
      const backend = status.backend;
      const current = this.capabilities.includes('diagnosis-v1') && this.capabilities.includes('paper-recovery-v1');
      this.text('backend-status', `插件 ${Bridge.VERSION} · 后台 ${backend?.version || '旧版（未报告版本）'}` +
        (backend?.disk_changed ? ' · 程序文件已更新，请重启空闲后台' : !current ? ' · 缺少诊断/恢复能力，请更新程序后重启空闲后台' : ' · 诊断与单篇恢复已就绪'));
      this.text("connection", "已连接 127.0.0.1");
      const view = status.view;
      if (this.pauseRun && (view?.id !== this.pauseRun || view?.status === "paused" || view?.terminal === true)) this.pauseRun = null;
      if (this.resumePending && (status.active || view?.id === this.resumePending && !["paused", "pause_requested"].includes(view.status))) this.resumePending = null;
      if (!this.selectedRun) this.displayStatus(status, false);
      this.update();
    }
    displayStatus(status, history = false) {
      if (this.closed) return;
      this.displayedStatus = status; this.viewKnown = true;
      if (this.statusKnown) { this.el("status-error").hidden = true; this.text("status-error", ""); }
      this.text("status-text", status.text);
      const v = status.view;
      if (v?.id && this.submissionBanner && (history || v.id === this.submissionBanner
          || v.id.slice(0, 15) > this.submissionBanner.slice(0, 15))) {
        // A dispatch acknowledgement lasts only until its status (or a newer run) is visible.
        this.message("");
      }
      this.text("status-title", history ? "历史任务" : status.active ? "当前任务" : "最近任务（历史记录）");
      if (!v) {
        this.text("summary", status.active ? "已有后台任务；详细进度尚未提供" : "当前无运行详情");
        this.report = ""; this.rows("status-rows", []); this.text("task-notice", "");
        this.text("frozen-config", "暂无本任务配置。");
      } else {
        this.report = typeof v.report === "string" ? v.report : "";
        this.text("summary", (v.status_text || v.status || "状态未知") + " · 任务 " + (v.id || "—") + " · " + (v.phase_label || v.phase || "—") +
          " · 总耗时 " + duration(v.elapsed_seconds) + " · 阶段 " + duration(v.phase_elapsed_seconds) + " · 验收 " + (v.accepted ?? "—") + "/" + (v.total ?? "—") +
          " · 索引 " + (v.index_status || "—") + " · Git " + (v.git_status || "—"));
        this.renderStatusRows(Array.isArray(v.papers) ? v.papers : []);
        const notices = Array.isArray(v.notices) ? v.notices.filter(x => typeof x === "string") : [];
        if (typeof v.resumed_as === "string" && Bridge.RUN_ID.test(v.resumed_as)) notices.unshift("此任务已派发恢复：" + v.resumed_as + "。请查看恢复任务。");
        if (this.isPauseRequested(v)) notices.unshift("已请求暂停，正在等待安全边界；可能需等整篇 QC / 翻译步骤结束。尚未确认可关机。");
        else if (v.status === "paused") notices.unshift(this.active && history
          ? "此历史任务已暂停；另有控制器正在运行，尚不可据此关机。" : v.safe_to_shutdown === true && v.owner_alive !== true
          ? "已安全暂停，后台已确认可以关机；可继续未完成步骤。" : "任务已暂停；尚未收到可关机确认。");
        if (v.status === "interrupted" && !v.can_resume && !v.can_resume_finalize) {
          notices.unshift("如需继续，可先保存下次使用的模型，再到“同步”页选择原条目并预演，或点击“检测新增”。核对范围后确认，已有产物会按步骤复核。");
        } else if (["failed", "partial", "interrupted", "blocked", "attention"].includes(v.status)) {
          // State the controller's own error when the service provides it, so
          // blocked runs show an actionable reason instead of only a report path.
          const reason = typeof v.error === "string" && v.error.trim() ? v.error.trim() : "";
          notices.unshift(reason
            ? `任务需要处理：${v.status_text || v.status}。原因：${reason}`
            : `任务需要处理：${v.status_text || v.status}。具体原因和受影响文件见任务报告。`);
        }
        if (history) notices.unshift("正在查看历史任务；当前后台运行状态仍独立检查。");
        this.text("task-notice", notices.join("\n"));
        const profiles = ["qc", "zh"].map(role => {
          const p = v.model_config?.[role];
          return p ? role.toUpperCase() + ": " + [p.provider, p.model,
            ...(p.provider === "zcode" && p.plan ? ["plan=" + p.plan] : []),
            "effort=" + p.reasoning_effort,
            "thinking=" + (p.thinking ?? "default"), "timeout=" + p.timeout, "max_tokens=" + p.max_tokens,
            "protocol=" + (p.protocol || "默认"), "base_url=" + (p.base_url || "默认")].join(" · ") : role.toUpperCase() + ": 未提供冻结配置";
        });
        this.text("frozen-config", profiles.join("\n"));
      }
      this.el("copy-status").hidden = false;
      this.text("report-path", this.reportHint()); this.update();
    }
    rows(id, rows) {
      const target = this.el(id), signature = this.client.redact(JSON.stringify(rows));
      if (this.rowSignatures.get(id) === signature || this.hasSelection(target)) return;
      const scroll = target.parentNode?.parentNode;
      const top = scroll?.scrollTop, left = scroll?.scrollLeft;
      target.replaceChildren();
      for (const values of rows) {
        const tr = this.doc.createElementNS(htmlNS, "tr");
        for (const value of values) {
          const td = this.doc.createElementNS(htmlNS, "td");
          td.textContent = this.client.redact(value ?? "—"); tr.appendChild(td);
        }
        target.appendChild(tr);
      }
      this.rowSignatures.set(id, signature);
      if (scroll) { scroll.scrollTop = top; scroll.scrollLeft = left; }
    }
    renderStatusRows(papers) {
      // Like rows(), plus one accessible per-paper "详情" action. The signature
      // covers identity/phase/acceptance so identical refreshes rebuild nothing.
      const target = this.el("status-rows");
      const signature = this.client.redact(JSON.stringify(papers.map(p =>
        [p.key, p.citekey, p.title, p.phase, p.accepted, p.note])));
      if (this.rowSignatures.get("status-rows") === signature || this.hasSelection(target)) return;
      const scroll = target.parentNode?.parentNode;
      const top = scroll?.scrollTop, left = scroll?.scrollLeft;
      target.replaceChildren();
      for (const p of papers) {
        const tr = this.doc.createElementNS(htmlNS, "tr");
        for (const value of [p.citekey || p.key, p.title, p.phase_label || p.phase,
          duration(p.phase_elapsed_seconds ?? p.elapsed_seconds),
          (p.accepted === true ? "已验收" : "未验收") + (p.note ? " · " + p.note : "")]) {
          const td = this.doc.createElementNS(htmlNS, "td");
          td.textContent = this.client.redact(value ?? "—"); tr.appendChild(td);
        }
        const cell = this.doc.createElementNS(htmlNS, "td");
        const button = this.doc.createElementNS(htmlNS, "button");
        button.type = "button";
        const key = typeof p.key === "string" && Bridge.ITEM_KEY.test(p.key) ? p.key : "";
        button.diagnosisKey = key;
        button.textContent = "详情";
        button.setAttribute("aria-label", "查看诊断 " + this.client.redact(p.citekey || p.key || ""));
        if (!key) { button.disabled = true; button.setAttribute("title", "该行没有可诊断的条目 key"); }
        cell.appendChild(button); tr.appendChild(cell);
        target.appendChild(tr);
      }
      this.rowSignatures.set("status-rows", signature);
      if (scroll) { scroll.scrollTop = top; scroll.scrollLeft = left; }
    }
    closeDiagnosis() {
      if (this.closed) return;
      this.diagnosis = null;
      this.el("diagnosis-section").hidden = true;
      this.text("diagnosis-text", "尚未选择文献。");
      this.el("diagnosis-remedies").replaceChildren();
      this.text("diagnosis-result", "");
    }
    diagnosisReason() {
      if (this.busy || this.pending || this.remedyBusy) return "请等待本次操作完成";
      if (!this.connected || this.pathsDirty) return "请先连接本机服务";
      if (!this.capabilities.includes("diagnosis-v1")) return "本机 bridge 不支持单篇诊断；请更新后台程序后重新连接";
      if (this.active) return "任务运行中暂不诊断；可先查看报告或暂停任务";
      return "";
    }
    async awaitOperation(id, label, timeoutMs = 240000) {
      // Bounded in-action polling for remedy/resume-preview operations; a late
      // answer after this controller closed is dropped by the closed checks.
      const started = Date.now();
      while (!this.closed) {
        const result = await this.client.operation(id);
        if (result.status !== "running") return result;
        if (Date.now() - started > timeoutMs) throw new Bridge.SafeError("OPERATION", `${label}超时；后台可能仍在执行，请稍后刷新诊断。`);
        await new Promise(resolve => this.window.setTimeout(resolve, 1500));
      }
      throw new Bridge.SafeError("CLOSED", "面板已关闭。");
    }
    showDiagnosis(key) {
      if (this.closed) return;
      const reason = this.diagnosisReason();
      if (reason) {
        this.message(reason, true);
        if (this.connected && !this.capabilities.includes('diagnosis-v1')) {
          const row = this.displayedStatus?.view?.papers?.find(p => p.key === key);
          this.text('diagnosis-text', `${row?.citekey || key}\n${row?.note || '后台尚不支持详细诊断'}\n请使用上方“更新 / 重启空闲后台”，连接新版服务后再查看处置选项。\n${this.reportHint(this.displayedStatus?.view?.report || '')}`);
          this.el('diagnosis-remedies').replaceChildren();
          this.el('diagnosis-section').hidden = false;
          this.el('diagnosis-section').scrollIntoView({block:'start'});
        }
        return;
      }
      const runId = this.displayedStatus?.view?.id;
      if (!runId || !Bridge.RUN_ID.test(runId)) { this.message("尚未选定可诊断的任务。", true); return; }
      return this.action(async () => {
        const value = await this.client.diagnosis(runId, key);
        if (this.closed) return;
        this.diagnosis = value;
        this.renderDiagnosis();
        this.el("diagnosis-section").hidden = false;
        this.el('diagnosis-section').scrollIntoView({block:'start'});
        this.el("diagnosis-title").focus({ preventScroll: true });
      });
    }
    renderDiagnosis() {
      const value = this.diagnosis;
      if (!value) return;
      const lines = [`文献 ${value.citekey || value.key}（${value.key}）· ${
        value.accepted ? "已验收" : "未验收"}${
        value.exit_code !== undefined && value.exit_code !== null ? ` · 退出码 ${value.exit_code}` : ""}`];
      for (const row of value.classification || []) {
        lines.push(`原因分类：${row.label}`);
        for (const item of (row.evidence || []).slice(0, 6)) {
          lines.push(`  - ${item.code ? "[" + item.code + "] " : ""}${item.message}` +
            (item.file ? `（文件 ${item.file}${item.unit !== undefined ? "，单元 " + item.unit : ""}）` : ""));
        }
      }
      if (value.qa && Array.isArray(value.qa.errors) && value.qa.errors.length) {
        lines.push("验收错误（前 12 条）：");
        for (const e of value.qa.errors.slice(0, 12)) lines.push(`  - [${e.code}] ${e.message}` + (e.file ? `（${e.file}）` : ""));
      }
      const files = (value.artifacts && value.artifacts.files) || [];
      lines.push("现有产物：" + (files.length ? files.join("、") + (value.artifacts.qc_marked ? "（原文带 QC 标记）" : "（原文无 QC 标记）") : "无"));
      if (value.ledger) lines.push(`问题台账：${value.ledger.status ?? "—"}（处置记录 ${value.ledger.dispositions ?? 0} 条，更新于 ${value.ledger.updated ?? "—"}）`);
      if (value.paths) {
        lines.push("报告路径：" + (value.paths.report || "—"));
        if (value.paths.paper_folder) lines.push("论文目录：" + value.paths.paper_folder);
      }
      lines.push("最近一次尝试：" + [value.latest_attempt?.run_id, value.latest_attempt?.action,
        value.latest_attempt?.status, value.latest_attempt?.exit_code,
        value.latest_attempt?.finished].filter(x => x !== undefined && x !== null).join(" · "));
      if (value.latest_attempt?.error) lines.push("最近处置失败原因：" + value.latest_attempt.error);
      if (value.latest_attempt?.result?.message) lines.push("最近处置结果：" + value.latest_attempt.result.message);
      if (value.log_tail) lines.push("日志尾部（脱敏、截断）：\n" + value.log_tail);
      this.text("diagnosis-text", lines.join("\n"));
      const box = this.el("diagnosis-remedies");
      box.replaceChildren();
      for (const remedy of value.remedies || []) {
        if (remedy.kind === "instruction") continue; // guidance text only, no fake retry button
        const button = this.doc.createElementNS(htmlNS, "button");
        button.type = "button";
        button.textContent = this.client.redact(remedy.label);
        button.setAttribute("aria-label", this.client.redact(remedy.label));
        button.remedyAction = remedy.id;
        button.remedyMeta = remedy;
        if (remedy.available !== true) {
          button.disabled = true;
          button.setAttribute("title", this.client.redact(remedy.reason || "当前不可用"));
        }
        box.appendChild(button);
      }
      const uriButton = this.doc.createElementNS(htmlNS, "button");
      uriButton.type = "button"; uriButton.textContent = "在 Zotero 中打开";
      uriButton.remedyAction = "__open_zotero";
      uriButton.remedyMeta = {};
      const copyButton = this.doc.createElementNS(htmlNS, "button");
      copyButton.type = "button"; copyButton.textContent = "复制本篇诊断";
      copyButton.remedyAction = "__copy";
      copyButton.remedyMeta = {};
      box.appendChild(uriButton); box.appendChild(copyButton);
      this.text("diagnosis-result", "");
      const instructions = (value.remedies || []).filter(r => r.kind === "instruction");
      if (instructions.length) this.text("diagnosis-result", "需人工处理的事项：\n" +
        instructions.map(r => "- " + r.label).join("\n"));
    }
    remedyAction(action, meta) {
      if (this.closed) return;
      if (action === "__open_zotero") {
        const key = this.diagnosis?.key;
        if (!key) return;
        try {
          if (typeof this.host.openZoteroItem === "function") {
            this.host.openZoteroItem(key);
          } else {
            this.host.copyText?.("zotero://select/library/items/" + key);
            this.message("宿主不支持直接打开；已复制 Zotero 条目链接。");
          }
        } catch (_) { this.message("无法在 Zotero 中打开该条目；请手动选择。", true); }
        return;
      }
      if (action === "__copy") {
        const text = this.el("diagnosis-text").textContent;
        const copied = (() => { try { return this.host.copyText?.(text) === true; } catch (_) { return false; } })();
        this.message(copied ? "已复制脱敏诊断。" : "复制失败：请手动选中诊断文本复制。", !copied);
        return;
      }
      const reason = this.diagnosisReason();
      if (reason) { this.message(reason, true); return; }
      if (!this.capabilities.includes("remedy-v1")) { this.message("本机 bridge 不支持处置动作；请更新后台程序。", true); return; }
      if (!this.diagnosis) return;
      const runId = this.diagnosis.run_id, key = this.diagnosis.key;
      return this.action(async () => {
        this.remedyBusy = true;
        try {
          const previewId = await this.client.remedySubmit(runId, key, action);
          if (this.closed) return;
          const preview = await this.awaitOperation(previewId, "处置预览");
          if (this.closed) return;
          if (preview.status === "failed") throw new Bridge.SafeError("OPERATION", "预览失败：" + this.client.redact(preview.error || "请查看状态。"));
          const result = preview.result || {};
          const token = typeof result.token === "string" ? result.token : "";
          if (!token) throw new Bridge.SafeError("PROTOCOL", "预览未返回一次性令牌；未执行任何动作，请重试预览。");
          const params = {};
          const confirmLines = [`对 ${this.diagnosis.citekey || key} 执行「${action}」？`, "预览：",
            JSON.stringify({ ...result, token: "[已隐藏]" }, null, 2).slice(0, 4000)];
          if (Array.isArray(result.user_edit_conflicts) && result.user_edit_conflicts.length) {
            if (!this.window.confirm("检测到疑似你的手动修改：\n" + result.user_edit_conflicts.join("\n") +
              "\n\n确定要用流水线候选替换这些修改吗？（原字节会先备份）")) return;
            params.overwrite = "user_edited_confirmed";
          }
          if (action === "reparse_source") {
            if (!this.window.confirm("全新解析会对 PDF 重新跑 MinerU（费用高、耗时）。\n确认继续？")) return;
            params.explicit_fresh_parse = true;
          }
          if (!this.window.confirm(confirmLines.join("\n") + "\n\n确认执行？")) return;
          const applyId = await this.client.remedySubmit(runId, key, action, { confirm: true, params, token });
          if (this.closed) return;
          const applied = await this.awaitOperation(applyId, "处置执行", 600000);
          if (this.closed) return;
          const text = applied.status === "failed"
            ? "处置失败：" + this.client.redact(applied.error || "请查看任务报告。")
            : "处置结果：" + this.client.redact(JSON.stringify(applied.result, null, 2)).slice(0, 6000);
          this.text("diagnosis-result", text);
          this.message(applied.status === "failed" ? "处置失败；详情见诊断区。" : "处置完成；详情见诊断区。", applied.status === "failed");
          // Refresh the diagnosis view so statuses/remedies move with reality.
          const fresh = await this.client.diagnosis(runId, key);
          if (!this.closed) { this.diagnosis = fresh; this.renderDiagnosis(); this.text("diagnosis-result", text); }
        } finally { this.remedyBusy = false; }
      });
    }
    async saveAcronym() {
      if (this.closed || this.busy) return;
      if (!this.connected || this.pathsDirty) { this.message("请先连接本机服务。", true); return; }
      if (!this.capabilities.includes("acronyms-v1")) { this.message("本机 bridge 不支持保存期刊缩写；请更新后台程序。", true); return; }
      const title = this.el("acronym-title").value.trim();
      const proposed = this.el("acronym-proposed").value.trim();
      if (!title || !proposed) { this.message("请填写刊名与缩写。", true); return; }
      try {
        this.busy = true; this.update();
        const result = await this.client.saveAcronym(title, proposed);
        this.message(result.message || "期刊缩写已保存。");
        this.el("acronym-proposed").value = "";
        this.acronymTitles = this.acronymTitles.filter(x => x !== title);
        if (this.acronymTitles.length) this.el("acronym-title").value = this.acronymTitles[0];
        else { this.el("acronym-section").hidden = true; }
      } catch (e) { this.error(e); }
      finally { this.busy = false; this.update(); }
    }
    showCurrentTask() {
      this.selectedRun = null; this.viewEpoch++; this.closeDiagnosis();
      const reading = this.showPage("task");
      if (this.currentStatus) this.displayStatus(this.currentStatus, false);
      this.el("status-title").focus({ preventScroll: true });
      return reading;
    }
    async loadHistory() {
      if (this.closed || !this.connected || this.pathsDirty) return;
      if (this.historyLoading && this.historyEpoch === this.epoch) return this.historyLoading;
      const reading = this.readHistory();
      this.historyLoading = reading; this.historyEpoch = this.epoch;
      try { await reading; } finally {
        if (this.historyLoading === reading) this.historyLoading = null;
        this.update();
      }
    }
    async readHistory() {
      const epoch = this.epoch;
      try {
        const value = await this.client.getHistory();
        if (this.closed || epoch !== this.epoch) return;
        this.history = value.runs;
        this.capabilities = [...new Set([...this.capabilities, ...value.capabilities])];
        this.text("history-notice", "最近任务 " + value.runs.length + " 项；点击查看只读取状态，不会恢复任务。");
        this.rows("history-rows", value.runs.map(run => [run.id, (run.status_text || run.status) + " · " + (run.phase || "—"),
          (run.started ?? "—") + " / " + (run.finished ?? "—"), ""]));
        const rows = this.el("history-rows").children;
        for (let i = 0; i < rows.length; i++) {
          const cell = rows[i].children[3];
          if (cell.children.length) continue;
          const button = this.doc.createElementNS(htmlNS, "button");
          button.type = "button"; button.historyRun = value.runs[i].id;
          button.textContent = "查看历史任务"; button.setAttribute("aria-label", "查看任务 " + value.runs[i].id);
          cell.appendChild(button);
        }
      } catch (e) {
        if (this.closed || epoch !== this.epoch) return;
        this.connectionError(e);
        this.text("history-notice", [400, 404, 405].includes(e.httpStatus)
          ? "本机服务暂未提供运行历史；当前任务仍可查看。"
          : "历史读取失败，保留上次列表：" + (e?.name === "SafeError" ? e.message : "请重试"));
      }
    }
    viewHistory(runId) {
      return this.action(async () => {
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接服务再读取历史。");
        const epoch = this.epoch, viewEpoch = ++this.viewEpoch;
        let status;
        try { status = await this.client.getStatus(runId); }
        catch (e) { this.statusError(e, true, runId); throw e; }
        if (this.closed || epoch !== this.epoch || viewEpoch !== this.viewEpoch) return;
        this.selectedRun = runId; this.showPage("task"); this.closeDiagnosis(); this.displayStatus(status, true);
      });
    }
    isPauseRequested(v = this.displayedStatus?.view) {
      return !!v && v.status !== "paused" && v.terminal !== true &&
        (v.pause_requested === true || v.status === "pause_requested" || v.id === this.pauseRun);
    }
    isLegacyPauseRequested(v = this.displayedStatus?.view) {
      return !!v && v.status === "blocked" && v.pause_requested === true && v.can_resume !== true &&
        v.terminal === true && this.capabilities.includes("pause-recovery-v1");
    }
    modelConfigLine(profile) {
      if (!profile) return "未提供";
      return `${profile.provider} / ${profile.model}，effort=${profile.reasoning_effort}` +
        (profile.provider === "zcode" && profile.plan ? `，plan=${profile.plan}` : "");
    }
    actionExplanation(action, v) {
      const key = action === "resume-finalize" ? "resume_finalize" : action;
      const value = v?.actions?.[key];
      if (typeof value === "string") return value;
      return value?.reason || value?.message || "";
    }
    runActionReason(action, ignoreBusy = false) {
      const v = this.displayedStatus?.view;
      if ((!ignoreBusy && this.busy) || this.pending || this.remedyBusy) return "请等待本次操作完成";
      if (!this.connected || this.pathsDirty) return "请先连接本机服务";
      if (action === "recover-pause") {
        if (!this.capabilities.includes("pause-recovery-v1")) return "后台组件版本旧；请更新后台程序（仅更新 XPI 不含 Python 变更）后重新连接";
        if (!this.statusKnown || !this.viewKnown) return "状态尚未核实，请刷新状态";
        if (!v?.id) return "暂无可操作任务";
        if (this.resumePending) return "恢复请求已提交，请刷新确认后台状态";
        if (v.resumed_as) return "此任务已派发过恢复，请查看恢复任务";
        if (this.active) return "已有任务正在运行，请等待或暂停当前任务";
        if (v.can_recover_pause === true) return "";
        if (this.isLegacyPauseRequested(v)) return this.actionExplanation("resume", v) ||
          "该任务缺少可核验的暂停凭据或已不满足恢复条件；请查看报告或重新预演这些文献";
        return "当前任务不在遗留暂停恢复范围";
      }
      if (!this.capabilities.includes("pause-resume-v1")) return "后台组件版本旧；关闭面板，待服务闲置 30 分钟退出后重连，或待任务结束后重启电脑再连接。";
      if (!this.statusKnown || !this.viewKnown) return "状态尚未核实，请刷新状态";
      if (!v?.id) return "暂无可操作任务";
      if (this.resumePending) return "恢复请求已提交，请刷新确认后台状态";
      if (this.isPauseRequested(v)) return "正在等待安全暂停；尚未确认可关机";
      if (action === "pause") {
        if (this.selectedRun) return "请返回当前任务后暂停";
        if (v.id !== this.currentStatus?.view?.id) return "当前任务已改变，请刷新后重试";
        if (!this.active) return "当前没有运行中的任务";
        if (v.can_pause !== true) return this.actionExplanation(action, v) || "后端尚未允许此任务暂停";
      } else {
        if (v.resumed_as) return "此任务已经派发恢复，请查看恢复任务";
        if (this.active) return "已有任务正在运行，请等待或暂停当前任务";
        const allowed = action === "resume-finalize" ? v.can_resume_finalize : v.can_resume;
        if (allowed !== true) return this.actionExplanation(action, v) || (action === "resume-finalize"
          ? "该任务没有可安全重试的收尾步骤" : "此任务没有可用的通用恢复记录，或当前不可恢复");
      }
      return "";
    }
    async modelChoicePreview(runId, recovery) {
      // Explicit frozen-old vs selected-new model preview before any resume.
      const id = await this.client.resumePreview(runId, recovery);
      const result = await this.awaitOperation(id, "恢复预览", 240000);
      if (result.status === "failed") throw new Bridge.SafeError("OPERATION",
        "恢复预览失败：" + this.client.redact(result.error || "请查看任务报告。"));
      return result.result || {};
    }
    formatResumePreview(preview) {
      const steps = Object.entries(preview.steps || {}).map(([key, list]) =>
        `${key}: ${Array.isArray(list) && list.length ? list.join(",") : "（无需补步骤）"}`);
      const needs = Object.entries(preview.needs_action || {}).map(([key, reasons]) =>
        `${key}: ${(Array.isArray(reasons) ? reasons : []).join("；")}`);
      const frozen = this.modelConfigLine(preview.frozen_model_config?.qc) + " / " + this.modelConfigLine(preview.frozen_model_config?.zh);
      const saved = preview.saved_model_ok
        ? this.modelConfigLine(preview.saved_model_config?.qc) + " / " + this.modelConfigLine(preview.saved_model_config?.zh)
        : "不可用：" + (preview.saved_model_error || "未配置");
      return { steps, needs, frozen, saved };
    }
    runAction(action) {
      const reason = this.runActionReason(action);
      if (reason) { this.message(reason, true); return; }
      const runId = this.displayedStatus.view.id, historical = !!this.selectedRun, viewEpoch = this.viewEpoch;
      return this.action(async () => {
        // Recheck controller ownership and the selected receipt immediately before confirmation.
        this.renderStatus(await this.client.getStatus());
        if (this.closed || viewEpoch !== this.viewEpoch) return;
        const detail = await this.client.getStatus(runId);
        if (this.closed || viewEpoch !== this.viewEpoch) return;
        this.displayStatus(detail, historical);
        const blocked = this.runActionReason(action, true);
        if (blocked) throw new Bridge.SafeError("INPUT", blocked);
        if (action === "pause") {
          const label = "完成当前步骤后暂停（可能包括整篇 QC / 翻译）；不终止进程";
          if (!this.window.confirm(label + "？\n任务 " + runId) || this.closed) return;
          const result = await this.client.pause(runId);
          if (this.closed) return;
          this.pauseRun = result.status === "pause_requested" ? runId : null;
          this.displayStatus({ ...detail, view: { ...detail.view, status: result.status,
            pause_requested: result.status === "pause_requested", can_pause: false, safe_to_shutdown: false } }, historical);
          this.message(result.message);
          this.renderStatus(await this.client.getStatus());
          return;
        }
        if (action === "recover-pause") {
          let preview = null;
          if (this.capabilities.includes("resume-model-choice-v1")) {
            try { preview = await this.modelChoicePreview(runId, true); }
            catch (e) { this.message("恢复预览读取失败，按保守方式继续确认。" , true); }
          }
          const lines = ["将以证据核验方式恢复这个遗留暂停任务（只读核验后启动新的子任务；原记录不改动）。\n任务 " + runId];
          if (preview) {
            const view = this.formatResumePreview(preview);
            lines.push("待补步骤：\n  " + (view.steps.join("\n  ") || "无"),
              "待处理（不会自动重跑）：\n  " + (view.needs.join("\n  ") || "无"),
              "原任务冻结模型：" + view.frozen, "当前已保存模型：" + view.saved);
          }
          if (!this.window.confirm(lines.join("\n") + "\n\n确认恢复？") || this.closed) return;
          let useSaved = false;
          if (preview && preview.saved_model_ok &&
              JSON.stringify(preview.frozen_model_config) !== JSON.stringify(preview.saved_model_config)) {
            useSaved = this.window.confirm("检测到当前已保存的模型配置与原任务冻结配置不同。\n" +
              "确定本次恢复的后续步骤改用当前已保存的配置吗？（取消则沿用原任务冻结配置）");
          }
          const result = await this.client.recover(runId, useSaved);
          if (this.closed) return;
          this.resumePending = result.run_id; this.startResult = result; this.showCurrentTask();
          this.message("遗留恢复已提交：" + result.run_id + "；请查看后台进度与验收结果。", false, result.run_id);
          this.renderStatus(await this.client.getStatus());
          return;
        }
        // resume / resume-finalize
        let useSaved = false;
        if (action === "resume" && this.capabilities.includes("resume-model-choice-v1")) {
          let preview = null;
          try { preview = await this.modelChoicePreview(runId, false); }
          catch (e) { this.error(e); throw e; }
          if (this.closed || viewEpoch !== this.viewEpoch) return;
          const view = this.formatResumePreview(preview);
          const label = "校验恢复记录并继续未完成步骤，使用原任务冻结配置";
          const ok = this.window.confirm(
            label + "？\n任务 " + runId +
            "\n待补步骤：\n  " + (view.steps.join("\n  ") || "无") +
            "\n待处理（不会自动重跑）：\n  " + (view.needs.join("\n  ") || "无") +
            "\n原任务冻结模型：" + view.frozen +
            "\n当前已保存模型：" + view.saved);
          if (!ok || this.closed) return;
          if (preview.saved_model_ok &&
              JSON.stringify(preview.frozen_model_config) !== JSON.stringify(preview.saved_model_config)) {
            useSaved = this.window.confirm("是否把本次恢复的后续步骤改用当前已保存的模型配置？\n" +
              "已保存：" + view.saved + "\n冻结：" + view.frozen +
              "\n（取消则沿用原任务冻结配置；已完成阶段不会重跑）");
          }
        } else {
          const label = action === "resume-finalize" ? "仅重试尚未完成的收尾步骤" : "校验恢复记录并继续未完成步骤，使用原任务冻结配置";
          if (!this.window.confirm(label + "？\n任务 " + runId) || this.closed) return;
        }
        const result = await this.client.resume(runId, action === "resume-finalize", useSaved);
        if (this.closed) return;
        this.resumePending = result.run_id; this.startResult = result; this.showCurrentTask();
        const text = result.already_submitted ? "已关联先前派发的恢复任务："
          : result.joined ? result.resume_of ? "已关联恢复任务：" : "已有任务正在运行，已关联：" : "恢复请求已提交：";
        this.message(text + result.run_id + "；请查看后台进度与验收结果。", false, result.run_id);
        this.renderStatus(await this.client.getStatus());
      });
    }
    renderAcronymSection(preview) {
      // Unknown journals surfaced by THIS preview get a journal-specific
      // edit/save box (base table + user overrides); saving then re-previewing
      // applies the mapping through the ordinary intake authorization path.
      const titles = [...new Set([...(preview.items || []), ...(preview.deferred || [])]
        .map(p => typeof p?.unmapped_publication_title === "string" ? p.unmapped_publication_title : "")
        .filter(Boolean))];
      this.acronymTitles = titles;
      const section = this.el("acronym-section");
      const show = titles.length > 0 && this.connected && this.capabilities.includes("acronyms-v1");
      section.hidden = !show;
      if (show) {
        this.el("acronym-title").value = titles[0];
        this.el("acronym-proposed").value = "";
        this.text("acronym-notice", `预演发现 ${titles.length} 个未映射刊名（${titles.join("；").slice(0, 300)}）。`
          + "随包基础表已覆盖常见海洋/地学期刊；此处补充你自己的映射（保存到个人覆盖表，优先于基础表），保存后请重新预演。"
          + "缩写是文件名/引用键短码，不是 ISO 4 缩写；新映射仍会经过键/附件授权核验。");
      }
    }
    previewCounts(preview = this.preview) {
      // Single read-only, key-deduplicated summary source. Items still carrying
      // a blocker count as deferred (they cannot execute); the table rows and
      // selection rules are NOT changed by this view.
      const items = preview?.items ?? [];
      const listed = Array.isArray(preview?.deferred) ? preview.deferred : [];
      const blocked = items.filter(p => Array.isArray(p.blocked) && p.blocked.length);
      const discovered = new Set([...items, ...listed]
        .map(p => p.key).filter(k => typeof k === "string"));
      const seen = new Set();
      const deferred = [];
      for (const p of [...listed, ...blocked]) {
        if (seen.has(p.key)) continue;
        seen.add(p.key);
        deferred.push(p);
      }
      const choices = new Map((this.selections || []).map(row => [row.key, row]));
      const readyItems = items.filter(p => !p.blocked?.length);
      // Classification follows the CURRENT per-paper translation choices: an
      // item is "pending review" only when every required step already has
      // its artifact, and that still never claims QA acceptance.
      const review = readyItems.filter(p => this.reviewItem(p, choices.get(p.key))).length;
      return { discovered: discovered.size, ready: readyItems.length,
               deferredCount: deferred.length, deferred,
               fresh: readyItems.length - review, review };
    }
    renderPreview(result) {
      if (!result || !Bridge.RUN_ID.test(result.preview_id) || typeof result.text !== "string" ||
          !Array.isArray(result.items) || !Array.isArray(result.deferred)) {
        throw new Bridge.SafeError("PROTOCOL", "预演响应不完整，请检查 bridge 合同。");
      }
      this.preview = result; this.startResult = null;
      this.startAttempted = false; this.modelDirty = false;
      this.selections = result.items.map(p => ({ key: p.key, include: !p.blocked?.length,
        translate: p.require_translation !== false && p.translate !== false && this.el("translate").checked }));
      if (new Set(result.items.map(p => p.key)).size !== result.items.length ||
          result.items.some(p => typeof p.key !== "string" || !Bridge.ITEM_KEY.test(p.key))) {
        this.preview = null; this.selections = [];
        throw new Bridge.SafeError("PROTOCOL", "预演须包含唯一 Zotero item key；不能用临时 citekey 作为文献身份。");
      }
      this.text("preview-text", result.text);
      this.renderAcronymSection(result);
      this.renderSelections();
      const counts = this.previewCounts(result);
      this.text("operation", `预演完成 · ${result.preview_id} · 发现 ${counts.discovered} 篇 · 可执行 ${counts.ready} 篇 · 暂缓 ${counts.deferredCount} 篇`);
      if (!result.items.length) { this.preview = null; this.message("预演完成，但没有可执行条目；暂缓条目与原因已列在上方。"); }
      else if (!counts.ready) this.message("预演完成，但没有可执行条目；暂缓条目与原因已列在上方。");
      else this.message(counts.deferredCount
        ? `预演已完成：可执行 ${counts.ready} 篇、暂缓 ${counts.deferredCount} 篇（题名与原因见上方暂缓清单）。核对逐篇选项后，点击“确认同步”。`
        : "预演已完成。核对逐篇包含与翻译选项后，点击“确认同步”。");
      this.update();
    }
    renderSelections() {
      const target = this.el("preview-rows"); target.replaceChildren(); this.selectionControls = [];
      for (const [items, deferred] of [[this.preview.items, false], [this.preview.deferred, true]]) {
        for (const p of items) {
          const tr = this.doc.createElementNS(htmlNS, "tr");
          const choice = this.selections.find(row => row.key === p.key);
          for (const field of ["include", "translate"]) {
            const cell = this.doc.createElementNS(htmlNS, "td");
            if (deferred) cell.textContent = field === "include" ? "暂缓" : "—";
            else {
              const input = this.doc.createElementNS(htmlNS, "input");
              input.type = "checkbox"; input.setAttribute("type", "checkbox"); input.checked = choice[field];
              input.selectionKey = p.key; input.selectionField = field;
              input.unavailable = !!p.blocked?.length || field === "translate" &&
                (p.require_translation === false || p.translate === false || !this.el("translate").checked);
              input.selectionReason = p.blocked?.length ? "存在阻碍，请先处理：" + p.blocked.join("；") :
                input.unavailable ? "无需翻译（按预演中文 / 不译规则或本次设置）" : "";
              input.setAttribute("aria-label", (field === "include" ? "包含 " : "翻译 ") + p.key);
              cell.appendChild(input); this.selectionControls.push(input);
              if (input.selectionReason) {
                const note = this.doc.createElementNS(htmlNS, "small"); note.textContent = this.client.redact(input.selectionReason); cell.appendChild(note);
              }
            }
            tr.appendChild(cell);
          }
          for (const value of [p.citekey || p.key, p.title, this.rowDescription(p, deferred)]) {
            const cell = this.doc.createElementNS(htmlNS, "td"); cell.textContent = this.client.redact(value || "—"); tr.appendChild(cell);
          }
          this.rowDescriptions.set(p.key, tr.children[tr.children.length - 1]);
          target.appendChild(tr);
        }
      }
      this.selectionSummary();
    }
    selectionSummary() {
      const selected = this.selections.filter(row => row.include);
      const counts = this.previewCounts();
      const countsText = this.preview
        ? `发现 ${counts.discovered} 篇：可执行 ${counts.ready} 篇、暂缓 ${counts.deferredCount} 篇；` +
          `可执行中预计需补齐产物 ${counts.fresh} 篇、已有成果待复核 ${counts.review} 篇（复核仍需完整 QA 验收，不代表已通过）。`
        : "";
      this.text("selection-notice", countsText + `已选 ${selected.length} 篇，其中 ${selected.filter(row => row.translate).length} 篇允许按需翻译。预演原件保留，逐篇选项将在提交时由后台校验冻结；不会删除已有译文。`);
      const notice = this.el("deferred-notice");
      notice.hidden = counts.deferred.length === 0;
      this.text("deferred-notice", counts.deferred.length
        ? "暂缓条目（本次不执行；原因如下，未改动 Zotero 与 Library）：\n" + counts.deferred.map(p =>
            `${p.citekey || p.key}（${p.key}）${p.title ? " " + p.title : ""}：` +
            `${(Array.isArray(p.blocked) ? p.blocked : []).join("；") || "未提供原因"}`).join("\n")
        : "");
    }
    changeSelection(control) {
      if (!this.selectionControls.includes(control) || control.disabled || this.busy || this.pending || this.startAttempted) return;
      const choice = this.selections.find(row => row.key === control.selectionKey);
      choice[control.selectionField] = control.checked === true;
      // Rewrite only this row's wording in place: a full re-render would reset
      // the checkboxes and steal focus while the user toggles options.
      const item = this.preview?.items.find(p => p.key === control.selectionKey);
      const cell = this.rowDescriptions.get(control.selectionKey);
      if (item && cell) cell.textContent = this.client.redact(this.rowDescription(item, false) || "—");
      this.selectionSummary(); this.update();
    }
    rowDescription(p, deferred) {
      // Third-cell wording: plan reason, queued actions and the work
      // classification under the CURRENT per-paper choices.
      const actions = Array.isArray(p.actions) ? p.actions.map(a => typeof a === "string" ? a : a?.action).filter(Boolean).join("、") : "";
      const missing = this.workClassification(p, this.selections.find(row => row.key === p.key));
      const workTag = deferred || p.blocked?.length ? "" : missing.length
        ? `预计需补齐产物（缺${missing.join("、")}）`
        : "已有成果待复核（仍需完整 QA 验收，不代表已通过）";
      const reason = Array.isArray(p.blocked) && p.blocked.length ? p.blocked.join("；") : p.reason || p.note || "按预演计划执行";
      return [reason, actions && "待执行：" + actions, workTag].filter(Boolean).join("；");
    }
    workClassification(p, choice) {
      // Display-only classification from the checked artifacts and the CURRENT
      // per-paper plan: any missing required step (fulltext, QC marker, note,
      // needed translation) means work is expected; everything present is only
      // "pending review" and never claims QA acceptance.
      const files = Array.isArray(p?.artifacts?.files) ? p.artifacts.files : [];
      const qcMarked = p?.artifacts?.qc_marked === true;
      const missing = [];
      if (!files.includes("EN") && !files.includes("MD")) missing.push("原文");
      else if (!qcMarked) missing.push("质检");
      if (!files.includes("Note")) missing.push("笔记");
      const needsTranslation = p?.require_translation !== false && choice?.translate !== false;
      if (needsTranslation && !files.includes("ZH")) missing.push("中译");
      return missing;
    }
    reviewItem(p, choice) {
      return this.workClassification(p, choice).length === 0;
    }
    start() {
      if (this.closed) return;
      if (this.startResult || this.active && !this.startAttempted) {
        this.showCurrentTask(); return;
      }
      return this.action(async () => {
        this.requireReady();
        if (!this.preview) throw new Bridge.SafeError("PREVIEW", "请先完成预演。");
        if (this.modelDirty) throw new Bridge.SafeError("CONFIG", "模型配置已修改且预演绑定未更新；请先保存并更新绑定，或重新预演。");
        const selections = Bridge.selections(this.selections);
        const id = this.preview.preview_id;
        // Refresh both server gates immediately before the irreversible confirmation.
        this.renderStatus(await this.client.getStatus());
        this.requireReady();
        const current = await this.client.getConfig();
        if (this.closed) return;
        const schoolReason = this.requiredTongjiReason(current);
        if (schoolReason) throw new Bridge.SafeError("CAPABILITY", schoolReason);
        // The irreversible confirmation re-reads the server config: a saved
        // required zcode route fails closed here too, before any POST.
        const savedZcodeReason = this.savedZcodeReason(current);
        if (savedZcodeReason) { this.fillConfig(current); throw new Bridge.SafeError("CAPABILITY", savedZcodeReason); }
        if (!this.configurationReady(current)) { this.fillConfig(current); throw new Bridge.SafeError("CONFIG", "服务尚未完整配置，不能开始同步。"); }
        if (this.active && !this.startAttempted) throw new Bridge.SafeError("ACTIVE", "已有任务运行，请查看状态。");
        const counts = this.previewCounts(this.preview);
        // Show the exact frozen models before the irreversible confirmation.
        const roles = ["qc", ...(selections.some(p => p.include && p.translate) ? ["zh"] : [])];
        const modelLines = roles.map(role => {
          const p = current.config?.[role];
          return role.toUpperCase() + ": " + (p ? `${p.provider} / ${p.model}${p.plan ? "，订阅=" + p.plan : ""}，effort=${p.reasoning_effort}` : "未提供");
        }).join("；");
        const itemOf = key => this.preview.items.find(p => p.key === key) || {};
        const choiceOf = key => selections.find(row => row.key === key);
        const yes = this.window.confirm(`确认执行预演 ${id}？\n本次选择 ${selections.filter(p => p.include).length} 篇，其中 ${selections.filter(p => p.include && p.translate).length} 篇允许按需翻译、预计需补齐产物 ${selections.filter(p => p.include && !this.reviewItem(itemOf(p.key), choiceOf(p.key))).length} 篇、已有成果待复核 ${selections.filter(p => p.include && this.reviewItem(itemOf(p.key), choiceOf(p.key))).length} 篇（仍需完整验收）；暂缓 ${counts.deferredCount} 篇。\n` +
          `本次任务冻结模型：${modelLines}。\n` +
          (selections.some(p => p.include && p.translate) ? "本次按逐篇选项执行原文、QC、按需翻译及收尾。" : "本次执行原文、QC 及收尾，不新增译文。") + "已有译文保留。" +
          "关闭本面板不会停止任务，请保持 Zotero 运行。\n是否开始？");
        if (!yes || this.closed) return;
        this.startAttempted = true;
        this.startResult = await this.client.start(id, selections);
        if (this.closed) return;
        this.message(`${this.startResult.started ? "已提交后台启动" : "已关联既有任务"}：${this.startResult.run_id}。正在读取任务状态。`, false, this.startResult.run_id);
        this.showCurrentTask();
        this.renderStatus(await this.client.getStatus());
      });
    }
    readModel(phase) {
      let extra;
      try { extra = JSON.parse(this.el(`${phase}-extra-body`).value || "{}"); }
      catch (_) { throw new Bridge.SafeError("INPUT", `${phase.toUpperCase()} 附加参数必须是有效 JSON 对象。`); }
      const provider = this.el(`${phase}-provider`).value.trim().toLowerCase();
      const base = { provider, model: this.el(`${phase}-model`).value,
        reasoning_effort: this.el(`${phase}-effort`).value,
        timeout: Number(this.el(`${phase}-timeout`).value), max_tokens: Number(this.el(`${phase}-max-tokens`).value) };
      // The zcode login has no endpoint URL, protocol or extra body; its plan
      // (explicit subscription route) only exists on plan-aware bridges, so an
      // old bridge keeps receiving the legacy body it can still validate.
      if (provider === "zcode") return this.capabilities.includes("zcode-plans-v1")
        ? { ...base, plan: this.el(`${phase}-plan`).value === "start" ? "start" : "coding" } : base;
      return { ...base,
        thinking: this.el(`${phase}-thinking`).value, protocol: this.el(`${phase}-protocol`).value,
        base_url: this.el(`${phase}-base-url`).value.trim().replace(/\/+$/, ""), models_url: this.el(`${phase}-models-url`).value.trim(),
        extra_body: extra, endpoint_confirmed: this.endpointConfirmed[phase] };
    }
    legacyBridgeModel(model) {
      // Old (pre-zcode-plans-v1) bridges reject unknown fields: a zcode draft
      // loses the plan field and keeps the five legacy fields they validate.
      if (model?.provider === "zcode" && !this.capabilities.includes("zcode-plans-v1")) {
        const { plan, ...legacy } = model;
        return legacy;
      }
      return model;
    }
    fillConfig(value, verified = true, preserveDraft = false) {
      if (this.closed) return;
      this.configured = value.configured; this.credentials = value.credentials; this.configDirty = preserveDraft;
      this.credentialsVerified = verified;
      this.savedConfig = value.config;
      const plans = this.readZcodePlans(value.zcodePlans);
      if (plans) this.zcodePlans = plans;
      for (const phase of preserveDraft ? [] : ["qc", "zh"]) {
        // Legacy five-field profiles omitted thinking intentionally. A new-template
        // default must not convert that omission into an explicit enabled request.
        // zcode hydrates separately: it has no thinking or endpoint fields, and
        // injecting the legacy thinking default would make Bridge.profile reject
        // the whole row (zcode forbids every extended field) and abort the
        // hydration of the other role. Its inputs show the honest "default"
        // (the login never sends a thinking parameter).
        const saved = value.config[phase];
        const profile = saved.provider === "zcode"
          ? { ...Bridge.hydrationDefaults("zcode"), ...saved }
          : { ...Bridge.hydrationDefaults(saved.provider), thinking: "default", ...saved };
        this.el(phase + "-plan").value = profile.plan === "start" ? "start" : "coding";
        for (const [field, key] of [["provider", "provider"], ["model", "model"], ["effort", "reasoning_effort"],
          ["thinking", "thinking"], ["protocol", "protocol"], ["base-url", "base_url"], ["models-url", "models_url"],
          ["timeout", "timeout"], ["max-tokens", "max_tokens"]]) {
          // zcode profiles carry no endpoint fields; never coerce undefined
          // into the inputs (a later .trim() would crash on it).
          const cell = profile[key];
          this.el(`${phase}-${field}`).value = cell === undefined ? "" : cell;
        }
        if (profile.provider === "zcode") this.el(phase + "-thinking").value = "default";
        this.el(`${phase}-extra-body`).value = JSON.stringify(profile.extra_body || {}, null, 2);
        this.endpointConfirmed[phase] = profile.endpoint_confirmed === true;
        this.profileProviders[phase] = profile.provider;
        // A hydrated (legacy) zcode profile immediately shows its honest
        // availability state and the migration path; other providers clear it.
        this.text(phase + "-provider-notice", profile.provider === "zcode"
          ? this.zcodePlanNotice(phase, profile.plan === "start" ? "start" : "coding") : "");
        if (this.testStates[phase]?.profile !== JSON.stringify(Bridge.profile(profile, true))) {
          delete this.testStates[phase]; this.text(phase + "-test-state", "尚未测试当前配置。");
        }
        const options = this.modelOptions[phase];
        if (options?.provider !== profile.provider || options?.endpoint &&
            options.endpoint !== JSON.stringify([profile.base_url, profile.models_url, profile.protocol]) ||
            (options.epoch ?? 0) !== (this.keyEpoch[profile.provider] || 0)) this.clearModelOptions(phase);
        if (!this.modelOptions[phase]) {
          // QC and ZH with the same trusted catalog share one cached listing;
          // without one, recognized default endpoints show the bundled
          // offline snapshot immediately while an explicit refresh can follow.
          const cached = this.catalogCacheGet(this.catalogIdentity(phase));
          if (cached) this.presentModelChoices(phase, cached.choices, "hit");
          else this.presentBundledCatalog(phase);
        }
        this.updateEffortOptions(phase);
        this.updateHabitNote(phase);
      }
      if (!preserveDraft) this.modelDirty = false;
      this.text("config-state", !verified ? "请先点击顶部“连接 / 启动服务”；连接后才能保存模型配置。"
        : preserveDraft ? "已连接；保留尚未保存的输入，请点击“保存模型配置”。"
        : value.configured ? "配置已保存。QC 与 ZH 独立选择；保存不等于已验证模型可用。" : "尚未完整配置；可先仅保存 Key，再填 QC 与 ZH 的模型 ID。运行状态仍可查看。");
      this.text("credentials", verified
        ? `GLM：${value.credentials.glm ? "已保存" : "未保存"}；DeepSeek：${value.credentials.deepseek ? "已保存" : "未保存"}；同济：${value.credentials.tongji ? "已保存" : "未保存"}（本机服务已核实）`
        : "尚未连接本机服务，无法核实密钥是否保存；输入框有内容不代表已保存。");
      this.update();
    }
    tongjiSupportReason(keysOnly = false) {
      if (this.capabilities.includes("tongji-api-v1")) return "";
      const key = this.el("tongji-key").value.trim();
      if (key && key !== this.loadedKeys.tongji || !keysOnly && ["qc", "zh"].some(p => this.el(p + "-provider").value === "tongji")) {
        return "本机服务尚不支持同济 API，请更新 bridge 后重新连接；尚未发送设置";
      }
      return "";
    }
    saveConfig(keysOnly = false) {
      return this.action(async () => {
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接 bridge；尚未发送，输入保留。");
        const unsupportedPlan = this.zcodeUnsupportedReason("qc") || this.zcodeUnsupportedReason("zh");
        if (unsupportedPlan) throw new Bridge.SafeError("CAPABILITY", unsupportedPlan);
        const schoolReason = this.tongjiSupportReason(keysOnly);
        if (schoolReason) throw new Bridge.SafeError("CAPABILITY", schoolReason);
        let normalized = keysOnly ? null : Bridge.config({ schema: 1, qc: this.readModel("qc"), zh: this.readModel("zh") });
        if (normalized) for (const phase of ["qc", "zh"]) normalized[phase] = this.confirmEndpoint(phase, normalized[phase]);
        if (normalized && !this.capabilities.includes("zcode-plans-v1")) {
          // Old bridges reject the plan field; their legacy zcode rows keep
          // the five fields they have always validated.
          for (const phase of ["qc", "zh"]) normalized[phase] = this.legacyBridgeModel(normalized[phase]);
        }
        const keys = this.changedKeys(["glm", "deepseek", "tongji"]);
        if (keysOnly && !Object.keys(keys).length) { this.message("Key 没有修改，无需重复保存。"); return; }
        if (this.active && Object.keys(keys).length) throw new Bridge.SafeError("CONFIG", "已有任务运行，不能更新密钥；请等待任务结束后保存，或仅保存模型（不改 Key）。");
        // A broken JSON/model draft must never prevent independent Key storage.
        const keepModelDraft = keysOnly && (this.configDirty || ["qc", "zh"].some(phase =>
          this.el(phase + "-model").value !== this.savedConfig?.[phase]?.model));
        const hadPreview = !!this.preview && !this.startAttempted;
        try {
          // The preview and per-paper selections are deliberately NOT cleared
          // here: a failed save must not lose the list.
          const value = keysOnly ? await this.client.saveKeys(keys) : await this.client.saveConfig(normalized, keys);
          if (this.closed) return;
          for (const [p, key] of Object.entries(keys)) { this.loadedKeys[p] = key; this.keyDirty[p] = false; }
          this.fillConfig(value, true, keepModelDraft);
          // Saved credential changes retire that provider's listings only; the
          // typed model and other providers' catalogs are untouched.
          for (const p of Object.keys(keys)) {
            this.keyEpoch[p] = (this.keyEpoch[p] || 0) + 1;
            for (const phase of ["qc", "zh"]) if (this.el(phase + "-provider").value === p) {
              this.clearModelOptions(phase);
              this.text(phase + "-model-notice", "Key 已更新保存；旧目录已按新凭据作废，请重新获取。手填模型 ID 保留。");
            }
          }
          this.message(`${keysOnly ? "API Keys" : "模型配置"}已由本机服务确认保存。Key 保留在输入框，可手动显示 / 隐藏；保存不验证有效期。`);
          if (hadPreview && !keysOnly) await this.rebindPreview();
        } catch (e) {
          this.text("credentials", "本次保存未获确认；输入已保留。请核实后重试，或重新读取已存配置。");
          throw e;
        } finally { keys.glm = ""; keys.deepseek = ""; keys.tongji = ""; }
      });
    }
    async rebindPreview() {
      // After saving new models, lightly rebind the existing preview instead of
      // rescanning the library. Scope, order, attachment plan, deferred entries
      // and per-paper selections survive; the original preview TTL still applies.
      if (this.closed || !this.preview || this.startAttempted) return;
      if (!this.capabilities.includes("preview-rebind-v1")) {
        this.modelDirty = true;
        this.text("operation", "本机 bridge 不支持预演模型更新；开始前请重新预演（逐篇选择已保留）");
        this.message("已保存模型配置，但本机 bridge 不支持预演模型更新；本次预演仍绑定保存前的模型，开始前请重新预演。逐篇选择已保留。", true);
        return;
      }
      try {
        const result = await this.client.rebind(this.preview.preview_id);
        if (this.closed) return;
        this.renderRebind(result);
        this.message("已按新保存的模型更新预演绑定；检测范围、附件计划、待办与逐篇选择保留，无需重新检测。");
      } catch (e) {
        if (this.closed) return;
        this.modelDirty = true;
        this.text("operation", "预演模型更新失败；预演保留原绑定，开始前请重新预演或再次保存。");
        this.error(e);
      }
    }
    renderRebind(result) {
      if (!result || result.rebound !== true || typeof result.text !== "string" ||
          !Array.isArray(result.items) || !Array.isArray(result.deferred)) {
        throw new Bridge.SafeError("PROTOCOL", "预演模型更新响应不完整，请检查 bridge 合同。");
      }
      const currentKeys = this.selections.map(row => row.key);
      if (JSON.stringify(result.items.map(p => p.key)) !== JSON.stringify(currentKeys)) {
        this.modelDirty = true;
        throw new Bridge.SafeError("PROTOCOL", "预演模型更新的条目与当前选择不一致；请重新预演。");
      }
      this.preview = result;
      this.modelDirty = false;
      this.text("preview-text", result.text);
      const counts = this.previewCounts(result);
      this.text("operation", `预演模型已更新 · ${result.preview_id} · 发现 ${counts.discovered} 篇 · 可执行 ${counts.ready} 篇 · 暂缓 ${counts.deferredCount} 篇`);
      this.selectionSummary();
      this.update();
    }
    fillMineru(value) {
      this.mineruConfigured = value.configured;
      const labels = { environment: "环境变量（优先于已保存 Key）", encrypted: "本机加密保存", legacy_file: "已有本机密钥文件", missing: "未配置" };
      this.text("mineru-state", `MinerU：${value.configured ? "已配置" : "未配置"}；当前来源：${labels[value.source]}。` +
        (value.source === "environment" ? "如需使用这里保存的新 Key，请先移除启动服务时的 MINERU_API_TOKEN 环境变量并重启服务。" : "") + "仅核实保存状态，未验证有效期。");
      this.updateKeyDisplays();
    }
    saveMineru() {
      return this.action(async () => {
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接 bridge；尚未发送，输入保留。");
        let key = this.changedKeys(["mineru"]).mineru;
        if (!key) { this.message("MinerU Key 没有修改，无需重复保存。"); return; }
        try {
          this.fillMineru(await this.client.saveMineru(key));
          if (this.closed) return;
          this.loadedKeys.mineru = key; this.keyDirty.mineru = false;
          await this.loadKeys(["mineru"], true);
          this.message("MinerU Key 已确认加密保存，输入框显示当前生效值。仅用于后续解析，不启动同步；未验证有效期。");
        } catch (e) {
          this.text("mineru-state", "本次保存或读回未获确认；输入已保留。请核实当前生效值。");
          throw e;
        } finally { key = ""; }
      });
    }
    reloadConfig() {
      return this.action(async () => {
        this.credentialsVerified = false;
        this.fillConfig(await this.client.getConfig());
        await this.loadKeys(["glm", "deepseek", "tongji"], true);
        if (this.preview && this.modelDirty) {
          this.text("operation", "已重新读取配置；与预演绑定的模型可能不同，请再次保存以更新绑定或重新预演");
        }
        this.message("已重新读取模型配置。");
      });
    }
    modelAction(phase, kind) {
      if (kind === "models") return this.catalogAction(phase, true);
      return this.action(async () => {
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接 bridge。");
        if (!this.capabilities.includes("model-profiles-v2")) throw new Bridge.SafeError("CAPABILITY", "请更新空闲的 bridge 后重新连接，新模型参数不能发送给旧服务。");
        if (kind === "test" && !this.capabilities.includes("model-test-role-v1")) throw new Bridge.SafeError("CAPABILITY", "本地服务尚未支持区分 QC / ZH 测试；请更新空闲的 bridge 后重新连接，未发送测试请求。");
        const unsupportedPlan = this.zcodeUnsupportedReason(phase);
        if (unsupportedPlan) throw new Bridge.SafeError("CAPABILITY", unsupportedPlan);
        let model = Bridge.profile(this.readModel(phase));
        if (model.provider === "tongji" && !this.capabilities.includes("tongji-api-v1")) throw new Bridge.SafeError("CAPABILITY", "本机服务尚不支持同济 API，请更新 bridge 后重新连接；尚未发送请求。");
        if (model.provider === "zcode") {
          // The CLI test starts a headless ZCode process and can take a minute.
          if (this.credentials.zcode !== true) throw new Bridge.SafeError("CONFIG", "本机 ZCode 尚未登录 GLM；请打开 ZCode 登录后重试，或改用 API Key 服务商。");
          this.text("model-result", "正在通过本机 ZCode 登录发送测试请求（启动无头进程，约需一分钟）…");
        } else {
          // A paid test always follows the existing endpoint confirmation.
          model = this.confirmEndpoint(phase, model);
          if (!this.credentials[model.provider]) throw new Bridge.SafeError("CONFIG", "请先保存所选 provider 的密钥。");
        }
        const id = await this.client.test(this.legacyBridgeModel(model), phase);
        if (this.closed) return;
        this.pending = { id, kind, phase, provider: model.provider, profile: JSON.stringify(model), endpoint: Bridge.endpointSignature(model) }; this.message("模型请求已提交，等待结果…");
        this.text("model-result", `操作 ${id} · 测试模型`);
      });
    }
    renderModelResult(pending, result) {
      if (this.closed) return;
      if (pending.kind === "models") {
        // Legacy direct-call entry kept for compatibility; live listings arrive
        // through the catalog records polled in tick().
        this.applyRawCatalog(pending.phase, { identity: this.catalogIdentity(pending.phase), ...pending }, result);
        return;
      }
      // Preserve only documented public test fields; never expose raw responses/credentials.
      const filtered = JSON.stringify(Bridge.testResult(result), null, 2);
      this.modelResultPhase = null;
      this.testStates[pending.phase] = { ok: result?.ok === true, at: new Date().toLocaleString(), profile: pending.profile };
      this.text(pending.phase + "-test-state", `${result?.ok === true ? "测试通过" : "测试未确认通过"} · ${this.testStates[pending.phase].at}；只代表本次请求。`);
      this.text("model-result", `${pending.phase.toUpperCase()} 测试结果\n${filtered ?? "（空结果）"}`);
      this.message("模型操作完成；结果仅说明本次模型请求，不代表文献同步完成。");
    }
    clearModelOptions(phase) {
      if (this.closed) return;
      this.modelOptions[phase] = null;
      this.pickers[phase + "-model"]?.setOptions([]);
      if (this.modelResultPhase === phase) {
        this.text("model-result", `${phase.toUpperCase()} 先前的模型列表已作废；请按当前来源 / API 地址重新获取。`);
        this.modelResultPhase = null;
      }
      this.text(phase + "-model-notice", "下拉选项尚未获取或已作废；可手填模型 ID。切换服务商 / API 地址后需重新获取。");
      this.updateEffortOptions(phase);
    }
    selectModelOption(phase) {
      const choices = this.modelOptions[phase], value = this.el(phase + "-model").value;
      if (this.closed || !value || !choices || choices.provider !== this.el(phase + "-provider").value ||
          choices.endpoint !== Bridge.endpointSignature(this.modelContext(phase)) || !choices.models.includes(value)) return;
      this.modelFieldChanged(phase, "model");
      this.applyModelDefaults(phase);
    }
    updateKeyDisplays() {
      if (this.closed) return;
      for (const p of ["glm", "deepseek", "tongji", "mineru"]) {
        const known = this.connected && !this.pathsDirty && (p === "mineru" ? this.mineruConfigured !== null : this.credentialsVerified);
        const saved = known && (p === "mineru" ? this.mineruConfigured : this.credentials[p]);
        const input = this.el(p + "-key");
        input.placeholder = !known ? "尚未连接，未读取" : saved ? "正在读取已存 Key…" : "尚未保存，请输入 Key";
      }
    }
    changedKeys(providers) {
      const updates = {};
      for (const p of providers) {
        const value = this.el(p + "-key").value.trim();
        const saved = p === "mineru" ? this.mineruConfigured : this.credentials[p];
        if (!value && saved) throw new Bridge.SafeError("INPUT", `${p.toUpperCase()} 已有 Key，不能保存为空；请重新读取已存值或输入新 Key。`);
        if (value && value !== this.loadedKeys[p]) updates[p] = value;
      }
      return Bridge.keyUpdates(updates, providers);
    }
    async loadKeys(providers = ["glm", "deepseek", "tongji", "mineru"], force = false) {
      if (!this.connected || this.pathsDirty || !this.capabilities.includes("credential-reveal-v1")) return;
      const epoch = this.revealEpoch;
      for (const p of providers) {
        if (this.closed || epoch !== this.revealEpoch) return;
        if (p === "tongji" && !this.capabilities.includes("tongji-api-v1")) continue;
        if (!force && (this.keyDirty[p] || this.el(p + "-key").value && this.el(p + "-key").value !== this.loadedKeys[p])) continue;
        const saved = p === "mineru" ? this.mineruConfigured : this.credentials[p];
        if (!saved) {
          if (force) { this.el(p + "-key").value = ""; delete this.loadedKeys[p]; this.keyDirty[p] = false; }
          continue;
        }
        const value = await this.client.revealKey(p);
        if (this.closed || epoch !== this.revealEpoch) { value.key = ""; this.client.forgetRevealed(p); return; }
        this.el(p + "-key").value = value.key;
        this.loadedKeys[p] = value.key; this.keyDirty[p] = false;
        value.key = "";
      }
      this.updateKeyDisplays();
    }
    clearSecrets() {
      this.revealEpoch++;
      for (const p of ["glm", "deepseek", "tongji", "mineru"]) {
        this.el(p + "-key").value = ""; this.el(p + "-key").type = "password";
        this.client.forgetRevealed(p);
      }
      this.loadedKeys = {}; this.keyDirty = {};
      this.updateKeyDisplays();
    }
    close() {
      if (this.closed) return;
      this.clearSecrets();
      this.closed = true; this.epoch++;
      for (const picker of Object.values(this.pickers)) picker.destroy();
      if (this.timer !== null) this.window.clearInterval(this.timer);
      for (const [el, event, handler] of this.handlers) el.removeEventListener(event, handler);
      this.handlers = []; this.pending = null; this.catalogPending = {}; this.diagnosis = null; this.remedyBusy = false;
      // Shutdown cleanup: no stale options, catalogs or handshake state may
      // survive into a reopened panel sharing this controller's memory.
      this.modelOptions = { qc: null, zh: null };
      this.modelCatalogs.clear();
      this.readySignaled = false;
      this.catalogTokens = { qc: (this.catalogTokens.qc || 0) + 1, zh: (this.catalogTokens.zh || 0) + 1 };
      this.el("glm-key").value = ""; this.el("deepseek-key").value = ""; this.el("tongji-key").value = ""; this.el("mineru-key").value = "";
      this.client.close(); this.host.panelClosed?.(this.window);
      // No HTTP request, no process termination. Worker ownership belongs to bridge.
    }
  }
  async function boot(window) {
    // Guarded DOMContentLoaded entry: a missing/invalid host argument or any
    // init failure must leave a closable window with sanitized stage info,
    // never a silent blank panel. Only fixed stage labels and SafeError
    // messages are shown; exception bodies never reach the DOM.
    let stage = "arguments", controller = null;
    try {
      const args = Array.isArray(window.arguments) && objectLike(window.arguments[0]) ? window.arguments[0] : null;
      const host = args ? args.host : null;
      if (!objectLike(host) || typeof host.getPaths !== "function" || typeof host.createClient !== "function") {
        throw new Bridge.SafeError("HOST", "面板宿主参数缺失或无效；请关闭本窗口后从菜单重新打开，若仍失败请重启 Zotero。");
      }
      stage = "create";
      controller = new Controller(window, host, host.createClient());
      window.librarySyncController = controller;
      window.addEventListener("unload", () => controller.close(), { once: true });
      stage = "init";
      await controller.init(args.selected === true);
    } catch (error) {
      if (controller) { controller.bootFailure(error, controller.bootStage || stage); return; }
      const safe = error instanceof Bridge.SafeError || error?.name === "SafeError" ? error.message : "";
      try {
        const note = window.document?.getElementById?.("message");
        if (note) {
          note.hidden = false;
          note.setAttribute("data-error", "true");
          note.textContent = `面板初始化失败（阶段 ${stage}）。` + (safe || "请关闭本窗口重新打开；若仍失败请重启 Zotero 后重试。");
        }
        const bootLine = window.document?.getElementById?.("panel-boot");
        if (bootLine) bootLine.textContent = "面板初始化失败；本窗口可关闭后重新打开。";
        window.document?.getElementById?.("close-panel")?.addEventListener?.("click", () => window.close());
      } catch (_) { /* the static markup already carries its own guidance */ }
    }
  }
  return { Controller, duration, boot };
})();
if (typeof module !== "undefined") module.exports = LibrarySyncPanel;
else {
  window.addEventListener("DOMContentLoaded", () => { void LibrarySyncPanel.boot(window); }, { once: true });
}
