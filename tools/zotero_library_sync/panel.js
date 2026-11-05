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
      this.selections = []; this.rowSignatures = new Map(); this.selectionControls = [];
      this.testStates = {}; this.profileProviders = {};
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
        attach(phase + "-provider", [{ value: "glm", label: "GLM（智谱 Key）" }, { value: "deepseek", label: "DeepSeek" }, { value: "tongji", label: "同济校内 API（独立 Key）" }], () => this.modelFieldChanged(phase, "provider"));
        attach(phase + "-model", [], () => this.selectModelOption(phase), true, true);
        attach(phase + "-effort", ["default"], () => this.modelFieldChanged(phase, "effort"));
        attach(phase + "-thinking", ["default", "enabled", "disabled"], () => this.modelFieldChanged(phase, "thinking"));
        attach(phase + "-protocol", ["openai_chat_completions"], () => this.modelFieldChanged(phase, "protocol"));
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
        if (["glm", "deepseek", "tongji"].includes(provider)) {
          const old = previous && Bridge.profileDefaults(previous), next = Bridge.profileDefaults(provider);
          // Only migrate unchanged provider defaults. Custom endpoints and parameters survive.
          for (const [field, key] of [["base-url", "base_url"], ["models-url", "models_url"]]) {
            if (old && this.el(phase + "-" + field).value === old[key]) this.el(phase + "-" + field).value = next[key];
          }
          if (provider === "tongji" || previous === "tongji") {
            for (const [field, key] of [["effort", "reasoning_effort"], ["max-tokens", "max_tokens"]]) {
              if (old && String(this.el(phase + "-" + field).value) === String(old[key])) this.el(phase + "-" + field).value = next[key];
            }
          }
          if (provider === "tongji") this.el(phase + "-thinking").value = "default";
          this.profileProviders[phase] = provider; this.el(phase + "-provider").value = provider;
        }
        this.endpointConfirmed[phase] = false; this.clearModelOptions(phase);
        this.text(phase + "-provider-notice", provider === "tongji"
          ? "已选择同济独立 Key；未改动的地址和模板参数已调整，thinking 固定为 default。请填写校内模型 ID，reasoning_effort 控制思考；手工地址及参数已保留，草稿尚未保存。"
          : "切换服务商仅更新未改动的默认地址；已保留模型及手工参数。请核对 Key 来源、API 地址与模型；可展开高级参数并恢复默认值。");
      }
      if (["model", "thinking", "effort"].includes(field)) this.updateEffortOptions(phase);
      if (["base-url", "models-url", "protocol"].includes(field)) {
        this.endpointConfirmed[phase] = false; this.clearModelOptions(phase);
      }
      delete this.testStates[phase]; this.text(phase + "-test-state", "配置已更改，尚未测试此配置。");
      this.configDirty = true; this.invalidate(); this.update();
    }
    applyProviderDefaults(phase) {
      const provider = this.el(phase + "-provider").value.trim().toLowerCase();
      if (!["glm", "deepseek", "tongji"].includes(provider)) {
        this.text(phase + "-effort-notice", "Provider / Key 来源请填 glm、deepseek 或 tongji；API 地址可另行自填。");
        this.configDirty = true; this.invalidate(); this.update(); return;
      }
      this.el(phase + "-provider").value = provider;
      const defaults = Bridge.profileDefaults(provider);
      for (const [field, key] of [["base-url", "base_url"], ["models-url", "models_url"], ["protocol", "protocol"], ["thinking", "thinking"], ["effort", "reasoning_effort"], ["timeout", "timeout"], ["max-tokens", "max_tokens"]]) {
        this.el(phase + "-" + field).value = defaults[key];
      }
      this.el(phase + "-extra-body").value = "{}"; this.endpointConfirmed[phase] = false;
      this.clearModelOptions(phase); this.updateEffortOptions(phase);
      delete this.testStates[phase]; this.text(phase + "-test-state", "默认参数已恢复，尚未测试。");
      this.text(phase + "-provider-notice", "已将地址、协议、thinking、effort、超时、输出长度及附加参数恢复为插件初始值（不是服务端默认值）；模型 ID 保留，草稿尚未保存，请核对后保存。");
      this.configDirty = true; this.invalidate(); this.update();
    }
    modelContext(phase) {
      return { provider: this.el(phase + "-provider").value.trim().toLowerCase(), model: this.el(phase + "-model").value.trim(),
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
        : "限制单次生成的输出 token 数，不是输入长度或上下文窗口（context）。输入 + 生成内容还须满足模型上下文限制；提高此值不会扩大 context。思考模型需给推理与最终正文留足预算，过小可能截断。新建空白模板显式使用 GLM 131072 / DeepSeek 393216、effort=max、thinking=enabled；已保存配置不会自动覆盖。具体模型能力未知时请手填，default 仍表示服务端默认。");
      const suggestions = [...new Set(["default", ...help.effortLevels])];
      this.pickers[phase + "-effort"]?.setOptions(suggestions);
      this.text(phase + "-effort-notice", (detail?.effort_levels?.length
        ? `候选来自此模型的${choices.source === "provider_api" ? "接口元数据" : "官方文档参数快照（非实时）"}；${detail.default_effort ? "服务端默认 " + detail.default_effort + "。" : ""}` : "") + help.effort);
      this.text(phase + "-thinking-notice", help.thinking);
      this.text(phase + "-tokens-notice", help.tokens);
      this.text(phase + "-endpoint-notice", help.endpointHelp);
      this.text(phase + "-maximum-notice", help.maximum
        ? `可填写此模型最高参数：max_tokens=${help.maximum.max_tokens}，reasoning_effort=${help.maximum.reasoning_effort}。仅修改本角色草稿，需保存后生效。`
        : "尚未核实此模型 / 端点的完整最高参数；当前数值可能来自模板或原配置，不代表该模型支持。请按官方文档手填，插件不改动当前参数。");
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
      this.configDirty = true; this.invalidate(); this.update();
      this.message(`${phase.toUpperCase()} 已填入“${selected.label}”预设，草稿尚未保存；请核对后点击“保存模型配置”。`);
    }
    confirmEndpoint(phase, profile) {
      const defaults = Bridge.profileDefaults(profile.provider);
      if (profile.base_url === defaults.base_url && profile.models_url === defaults.models_url) return profile;
      if (!this.endpointConfirmed[phase]) {
        const yes = this.window.confirm(`使用自定义 API 地址？\n${phase.toUpperCase()} 将向下面地址发送所选 ${profile.provider} 的 Key 和请求；论文运行时也会发送待处理正文。\nAPI：${profile.base_url}\n模型列表：${profile.models_url || "不请求列表"}\n协议：${profile.protocol}\n确认这是你信任的服务？`);
        if (!yes || this.closed) throw new Bridge.SafeError("INPUT", "未确认自定义 API 地址，未发送请求。");
        this.endpointConfirmed[phase] = true;
      }
      return { ...profile, endpoint_confirmed: true };
    }
    async init(selected = false) {
      const paths = this.host.getPaths();
      this.el("repo").value = paths.repo; this.el("pythonw").value = paths.pythonw;
      this.initPickers();
      this.fillConfig({ configured: false, config: { schema: 1, qc: Bridge.profileDefaults("glm"), zh: Bridge.profileDefaults("glm") }, credentials: this.credentials }, false);
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
      this.listen("preview-rows", "change", event => this.changeSelection(event.target));
      this.listen("connect", "click", () => this.connect(true));
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
          this.modelFieldChanged(phase, "model"); this.applyModelMaximum(phase);
        });
        this.listen(phase + "-provider", "change", () => this.modelFieldChanged(phase, "provider"));
        for (const field of ["provider", "model", "effort", "thinking", "protocol", "base-url", "models-url", "extra-body", "timeout", "max-tokens"]) {
          this.listen(`${phase}-${field}`, "input", () => this.modelFieldChanged(phase, field));
        }
      }
      for (const id of ["glm-key", "deepseek-key", "tongji-key"]) this.listen(id, "input", () => {
        this.configDirty = true; this.invalidate(); this.update();
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
      this.listen("mineru-key", "input", () => { this.invalidate(); this.update(); });
      if (selected) this.captureSelection();
      this.update();
      // Initial open is read-only. Only the explicit connect button can launch a service.
      await this.connect(false);
      if (!this.closed) this.timer = this.window.setInterval(() => { void this.tick(); }, 3000);
    }
    invalidate() {
      this.preview = null; this.startResult = null; this.startAttempted = false;
      this.selections = []; this.selectionControls = [];
      this.text("operation", "范围或配置已改变，请重新预演");
      this.text("preview-text", "尚无有效预演。请先预演再确认开始。");
      this.el("preview-rows").replaceChildren();
      this.el("deferred-notice").hidden = true; this.text("deferred-notice", "");
    }
    update() {
      if (this.closed) return;
      const locked = this.busy || !!this.pending;
      const lockReason = locked ? "请等待本次操作完成" : "";
      const connectionReason = this.pathsDirty ? "路径已修改，请先保存并重新连接" : !this.connected ? "请先连接本机服务" : "";
      const activeReason = this.active ? "已有任务正在运行；修改可留作下次任务草稿" : "";
      const readyReason = connectionReason || this.requiredTongjiReason() || (!this.configurationReady() ? "请先保存所需模型配置与 Key" : "") ||
        (this.configDirty ? "存在未保存修改，请先保存配置" : "") ||
        (!this.capabilities.includes("translation-policy-v1") ? "请更新服务以支持翻译选项" : "");
      const refreshReason = !this.capabilities.includes("source-refresh-v1") ? "本机 bridge 不支持更换全文 PDF，请更新 bridge 后重新连接。" : "";
      const refreshPreviewReason = this.el("refresh-source").checked ? refreshReason : "";
      const scopeReason = lockReason || readyReason || (!this.statusKnown ? "当前状态读取未成功，请刷新状态" : "") || activeReason;
      this.disable("connect", this.busy ? "正在处理操作" : this.pathsDirty ? "请先保存修改后的路径" : "");
      this.disable("save-paths", lockReason);
      this.disable("select-items", lockReason);
      this.disable("reload-config", lockReason || connectionReason);
      for (const [id, capability] of [["save-config", "model-profiles-v2"], ["save-keys", "keys-v1"], ["save-mineru", "mineru-v1"]]) {
        this.disable(id, lockReason || connectionReason || activeReason ||
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
        for (const action of ["test", "models"]) this.disable(phase + "-" + action,
          lockReason || connectionReason || activeReason ||
          (this.el(phase + "-provider").value === "tongji" && !this.capabilities.includes("tongji-api-v1") ? "本机服务尚不支持同济 API，请更新 bridge 后重新连接" : "") ||
          (!this.credentials[this.el(phase + "-provider").value] && !(action === "models" && Bridge.publicModelDocs(this.modelContext(phase))) ? "请先保存所选服务商的 Key" : "") ||
          (!this.capabilities.includes("model-profiles-v2") || action === "test" && !this.capabilities.includes("model-test-role-v1") ? "本机服务不支持此模型操作，请更新" : ""));
        this.el(phase + "-max-tokens").max = String(Bridge.tokenInputLimit(this.el(phase + "-provider").value));
        this.disable(phase + "-maximum", this.habitReason() ||
          (!Bridge.modelMaximum(this.modelContext(phase), this.modelOptions[phase]) ? "尚未核实该模型 / 端点的最高参数，请按文档手填。" : ""));
      }
      for (const id of ["repo", "pythonw", "workers", "chunk-parallel", "limit", "translate", "glm-key", "deepseek-key", "tongji-key", "mineru-key",
        ...["qc", "zh"].flatMap(p => ["provider", "model", "effort", "thinking", "protocol", "base-url", "models-url", "extra-body", "timeout", "max-tokens", "defaults", "habit"].map(f => p + "-" + f))]) {
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
      for (const action of ["pause", "resume", "resume-finalize"]) this.disable(action, this.runActionReason(action));
      // Legacy receipts may allow finalization without supporting content resume.
      this.el("resume").hidden = !!this.displayedStatus?.view && !this.active &&
        this.displayedStatus.view.can_resume !== true && !this.isPauseRequested() && this.capabilities.includes("pause-resume-v1");
      this.el("resume-reason").hidden ||= this.el("resume").hidden;
      for (const control of this.selectionControls) {
        control.disabled = locked || this.startAttempted || control.unavailable ||
          control.selectionField === "translate" && !this.selections.find(row => row.key === control.selectionKey)?.include;
        control.setAttribute("title", control.disabled ? control.selectionReason ||
          (this.startAttempted ? "该预演已提交，逐篇选项已固定" : locked ? lockReason : "请先包含此文献") : "");
      }
      this.text("draft-state", (this.active ? "运行中的任务使用冻结配置；这里的修改用于下一次任务。" : "配置用于下次任务。") +
        (this.configDirty ? " 有未保存修改。" : " 当前无未保存的模型修改。"));
      this.updateKeyDisplays();
    }
    async action(fn, allowPending = false) {
      if (this.closed || this.busy || (this.pending && !allowPending)) return;
      this.epoch++; // An older polling response must not overwrite a click's fresh state.
      this.busy = true; this.update();
      try { await fn(); } catch (e) { if (!this.closed) this.error(e); }
      finally { this.busy = false; this.update(); }
    }
    async connect(launch) {
      return this.action(async () => {
        if (this.pathsDirty) throw new Bridge.SafeError("PATH", "请先保存修改后的路径。");
        if (!this.el("repo").value.trim() || !this.el("pythonw").value.trim()) {
          this.el("paths-details").open = true;
          throw new Bridge.SafeError("PATH", "尚未找到后台程序。XPI 只安装界面；已有用户请填写原程序目录和 pythonw.exe，新用户先安装 Windows 后台包。填写后点击“保存路径”，再连接。");
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
        this.text("history-notice", "尚未读取新目录的历史。"); this.text("task-notice", "");
        this.text("frozen-config", "暂无本任务配置。"); this.el("status-error").hidden = true; this.el("copy-status").hidden = true;
        this.el("repo").value = value.repo; this.el("pythonw").value = value.pythonw;
        this.el("glm-key").value = ""; this.el("deepseek-key").value = ""; this.el("tongji-key").value = ""; this.el("mineru-key").value = "";
        this.capabilities = []; this.text("mineru-state", "尚未核实新目录的 MinerU 保存状态。");
        for (const phase of ["qc", "zh"]) this.clearModelOptions(phase);
        this.invalidate(); this.fillConfig({ configured: false, config: { schema: 1, qc: Bridge.profileDefaults("glm"), zh: Bridge.profileDefaults("glm") }, credentials: { glm: false, deepseek: false, tongji: false } }, false);
        this.text("connection", "路径已保存，请连接服务"); this.text("status-text", "尚未读取新目录的状态。");
        this.text("summary", "尚未读取状态"); this.text("report-path", this.reportHint());
        this.el("status-rows").replaceChildren(); this.message("已保存 Repo 与 Pythonw 两个路径。");
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
      if (!this.configurationReady() || this.configDirty) throw new Bridge.SafeError("CONFIG", "请先保存所需模型配置与 Key；运行状态仍可查看。");
    }
    configurationReady(value = null) {
      const config = value ? value.config : this.savedConfig, credentials = value ? value.credentials : this.credentials;
      return !!config && ["qc", ...(this.el("translate").checked ? ["zh"] : [])].every(
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
        if (this.closed || epoch !== this.epoch || !this.connected || !this.pending) return;
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
              if (pending.kind === "models") {
                this.clearModelOptions(pending.phase);
                this.text(pending.phase + "-model-notice", "本次获取模型列表失败；旧列表已作废，请重试或手填模型 ID。");
              }
              if (pending.kind === "test") this.text(pending.phase + "-test-state", "测试失败 · " + new Date().toLocaleString());
            }
          } else if (pending.kind === "preview") this.renderPreview(result.result);
          else this.renderModelResult(pending, result.result);
        }
      } catch (e) { if (!this.closed && epoch === this.epoch) this.error(e); }
      finally { this.ticking = false; this.update(); }
    }
    renderStatus(status, epoch = this.epoch) {
      if (this.closed || epoch !== this.epoch) return;
      this.active = status.active; this.statusKnown = true; this.currentStatus = status;
      if (Array.isArray(status.capabilities)) this.capabilities = status.capabilities;
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
        this.rows("status-rows", (Array.isArray(v.papers) ? v.papers : []).map(p => [
          p.citekey || p.key, p.title, p.phase_label || p.phase, duration(p.phase_elapsed_seconds ?? p.elapsed_seconds),
          (p.accepted === true ? "已验收" : "未验收") + (p.note ? " · " + p.note : "")
        ]));
        const notices = Array.isArray(v.notices) ? v.notices.filter(x => typeof x === "string") : [];
        if (typeof v.resumed_as === "string" && Bridge.RUN_ID.test(v.resumed_as)) notices.unshift("此任务已派发恢复：" + v.resumed_as + "。请查看恢复任务。");
        if (this.isPauseRequested(v)) notices.unshift("已请求暂停，正在等待安全边界；可能需等整篇 QC / 翻译步骤结束。尚未确认可关机。");
        else if (v.status === "paused") notices.unshift(this.active && history
          ? "此历史任务已暂停；另有控制器正在运行，尚不可据此关机。" : v.safe_to_shutdown === true && v.owner_alive !== true
          ? "已安全暂停，后台已确认可以关机；可继续未完成步骤。" : "任务已暂停；尚未收到可关机确认。");
        if (["failed", "partial", "interrupted", "blocked", "attention"].includes(v.status)) {
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
          return p ? role.toUpperCase() + ": " + [p.provider, p.model, "effort=" + p.reasoning_effort,
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
    showCurrentTask() {
      this.selectedRun = null; this.viewEpoch++; const reading = this.showPage("task");
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
        this.selectedRun = runId; this.showPage("task"); this.displayStatus(status, true);
      });
    }
    isPauseRequested(v = this.displayedStatus?.view) {
      return !!v && v.status !== "paused" && v.terminal !== true &&
        (v.pause_requested === true || v.status === "pause_requested" || v.id === this.pauseRun);
    }
    actionExplanation(action, v) {
      const key = action === "resume-finalize" ? "resume_finalize" : action;
      const value = v?.actions?.[key];
      if (typeof value === "string") return value;
      return value?.reason || value?.message || "";
    }
    runActionReason(action, ignoreBusy = false) {
      const v = this.displayedStatus?.view;
      if ((!ignoreBusy && this.busy) || this.pending) return "请等待本次操作完成";
      if (!this.connected || this.pathsDirty) return "请先连接本机服务";
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
        const label = action === "pause" ? "完成当前步骤后暂停（可能包括整篇 QC / 翻译）；不终止进程"
          : action === "resume-finalize" ? "仅重试尚未完成的收尾步骤" : "校验恢复记录并继续未完成步骤，使用原任务冻结配置";
        if (!this.window.confirm(label + "？\n任务 " + runId) || this.closed) return;
        if (action === "pause") {
          const result = await this.client.pause(runId);
          if (this.closed) return;
          this.pauseRun = result.status === "pause_requested" ? runId : null;
          this.displayStatus({ ...detail, view: { ...detail.view, status: result.status,
            pause_requested: result.status === "pause_requested", can_pause: false, safe_to_shutdown: false } }, historical);
          this.message(result.message);
        } else {
          const result = await this.client.resume(runId, action === "resume-finalize");
          if (this.closed) return;
          this.resumePending = result.run_id; this.startResult = result; this.showCurrentTask();
          const label = result.already_submitted ? "已关联先前派发的恢复任务："
            : result.joined ? result.resume_of ? "已关联恢复任务：" : "已有任务正在运行，已关联：" : "恢复请求已提交：";
          this.message(label + result.run_id + "；请查看后台进度与验收结果。", false, result.run_id);
        }
        this.renderStatus(await this.client.getStatus());
      });
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
      return { discovered: discovered.size, ready: items.filter(p => !p.blocked?.length).length,
               deferredCount: deferred.length, deferred };
    }
    renderPreview(result) {
      if (!result || !Bridge.RUN_ID.test(result.preview_id) || typeof result.text !== "string" ||
          !Array.isArray(result.items) || !Array.isArray(result.deferred)) {
        throw new Bridge.SafeError("PROTOCOL", "预演响应不完整，请检查 bridge 合同。");
      }
      this.preview = result; this.startResult = null;
      this.startAttempted = false;
      this.selections = result.items.map(p => ({ key: p.key, include: !p.blocked?.length,
        translate: p.require_translation !== false && p.translate !== false && this.el("translate").checked }));
      if (new Set(result.items.map(p => p.key)).size !== result.items.length ||
          result.items.some(p => typeof p.key !== "string" || !Bridge.ITEM_KEY.test(p.key))) {
        this.preview = null; this.selections = [];
        throw new Bridge.SafeError("PROTOCOL", "预演须包含唯一 Zotero item key；不能用临时 citekey 作为文献身份。");
      }
      this.text("preview-text", result.text);
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
          const actions = Array.isArray(p.actions) ? p.actions.map(a => typeof a === "string" ? a : a?.action).filter(Boolean).join("、") : "";
          const reason = Array.isArray(p.blocked) && p.blocked.length ? p.blocked.join("；") : p.reason || p.note || "按预演计划执行";
          for (const value of [p.citekey || p.key, p.title, [reason, actions && "待执行：" + actions].filter(Boolean).join("；")]) {
            const cell = this.doc.createElementNS(htmlNS, "td"); cell.textContent = this.client.redact(value || "—"); tr.appendChild(cell);
          }
          target.appendChild(tr);
        }
      }
      this.selectionSummary();
    }
    selectionSummary() {
      const selected = this.selections.filter(row => row.include);
      const counts = this.previewCounts();
      const countsText = this.preview
        ? `发现 ${counts.discovered} 篇：可执行 ${counts.ready} 篇、暂缓 ${counts.deferredCount} 篇。`
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
      this.selectionSummary(); this.update();
    }
    start() {
      if (this.closed) return;
      if (this.startResult || this.active && !this.startAttempted) {
        this.showCurrentTask(); return;
      }
      return this.action(async () => {
        this.requireReady();
        if (!this.preview) throw new Bridge.SafeError("PREVIEW", "请先完成预演。");
        const selections = Bridge.selections(this.selections);
        const id = this.preview.preview_id;
        // Refresh both server gates immediately before the irreversible confirmation.
        this.renderStatus(await this.client.getStatus());
        this.requireReady();
        const current = await this.client.getConfig();
        if (this.closed) return;
        const schoolReason = this.requiredTongjiReason(current);
        if (schoolReason) throw new Bridge.SafeError("CAPABILITY", schoolReason);
        if (!this.configurationReady(current)) { this.fillConfig(current); throw new Bridge.SafeError("CONFIG", "服务尚未完整配置，不能开始同步。"); }
        if (this.active && !this.startAttempted) throw new Bridge.SafeError("ACTIVE", "已有任务运行，请查看状态。");
        const counts = this.previewCounts(this.preview);
        const yes = this.window.confirm(`确认执行预演 ${id}？\n本次选择 ${selections.filter(p => p.include).length} 篇，其中 ${selections.filter(p => p.include && p.translate).length} 篇允许按需翻译；暂缓 ${counts.deferredCount} 篇。\n` +
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
      return { provider: this.el(`${phase}-provider`).value.trim().toLowerCase(), model: this.el(`${phase}-model`).value,
        reasoning_effort: this.el(`${phase}-effort`).value,
        thinking: this.el(`${phase}-thinking`).value, protocol: this.el(`${phase}-protocol`).value,
        base_url: this.el(`${phase}-base-url`).value.trim().replace(/\/+$/, ""), models_url: this.el(`${phase}-models-url`).value.trim(),
        extra_body: extra, endpoint_confirmed: this.endpointConfirmed[phase],
        timeout: Number(this.el(`${phase}-timeout`).value), max_tokens: Number(this.el(`${phase}-max-tokens`).value) };
    }
    fillConfig(value, verified = true, preserveDraft = false) {
      if (this.closed) return;
      this.configured = value.configured; this.credentials = value.credentials; this.configDirty = preserveDraft;
      this.credentialsVerified = verified;
      this.savedConfig = value.config;
      for (const phase of preserveDraft ? [] : ["qc", "zh"]) {
        // Legacy five-field profiles omitted thinking intentionally. A new-template
        // default must not convert that omission into an explicit enabled request.
        const profile = { ...Bridge.profileDefaults(value.config[phase].provider), thinking: "default", ...value.config[phase] };
        for (const [field, key] of [["provider", "provider"], ["model", "model"], ["effort", "reasoning_effort"],
          ["thinking", "thinking"], ["protocol", "protocol"], ["base-url", "base_url"], ["models-url", "models_url"],
          ["timeout", "timeout"], ["max-tokens", "max_tokens"]]) this.el(`${phase}-${field}`).value = profile[key];
        this.el(`${phase}-extra-body`).value = JSON.stringify(profile.extra_body || {}, null, 2);
        this.endpointConfirmed[phase] = profile.endpoint_confirmed === true;
        this.profileProviders[phase] = profile.provider;
        if (this.testStates[phase]?.profile !== JSON.stringify(Bridge.profile(profile, true))) {
          delete this.testStates[phase]; this.text(phase + "-test-state", "尚未测试当前配置。");
        }
        const options = this.modelOptions[phase];
        if (options?.provider !== profile.provider || options?.endpoint &&
            options.endpoint !== JSON.stringify([profile.base_url, profile.models_url, profile.protocol])) this.clearModelOptions(phase);
        this.updateEffortOptions(phase);
        this.updateHabitNote(phase);
      }
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
        const schoolReason = this.tongjiSupportReason(keysOnly);
        if (schoolReason) throw new Bridge.SafeError("CAPABILITY", schoolReason);
        let normalized = keysOnly ? null : Bridge.config({ schema: 1, qc: this.readModel("qc"), zh: this.readModel("zh") });
        if (normalized) for (const phase of ["qc", "zh"]) normalized[phase] = this.confirmEndpoint(phase, normalized[phase]);
        const keys = this.changedKeys(["glm", "deepseek", "tongji"]);
        if (keysOnly && !Object.keys(keys).length) { this.message("Key 没有修改，无需重复保存。"); return; }
        // A broken JSON/model draft must never prevent independent Key storage.
        const keepModelDraft = keysOnly && (this.configDirty || ["qc", "zh"].some(phase =>
          this.el(phase + "-model").value !== this.savedConfig?.[phase]?.model));
        try {
          this.invalidate();
          const value = keysOnly ? await this.client.saveKeys(keys) : await this.client.saveConfig(normalized, keys);
          if (this.closed) return;
          for (const [p, key] of Object.entries(keys)) { this.loadedKeys[p] = key; this.keyDirty[p] = false; }
          this.fillConfig(value, true, keepModelDraft);
          this.message(`${keysOnly ? "API Keys" : "模型配置"}已由本机服务确认保存。Key 保留在输入框，可手动显示 / 隐藏；保存不验证有效期。`);
        } catch (e) {
          this.text("credentials", "本次保存未获确认；输入已保留。请核实后重试，或重新读取已存配置。");
          throw e;
        } finally { keys.glm = ""; keys.deepseek = ""; keys.tongji = ""; }
      });
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
        this.invalidate();
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
        this.invalidate(); this.credentialsVerified = false;
        this.fillConfig(await this.client.getConfig());
        await this.loadKeys(["glm", "deepseek", "tongji"], true);
        this.message("已重新读取模型配置。");
      });
    }
    modelAction(phase, kind) {
      return this.action(async () => {
        if (!this.connected || this.pathsDirty) throw new Bridge.SafeError("CONNECT", "请先连接 bridge。");
        if (!this.capabilities.includes("model-profiles-v2")) throw new Bridge.SafeError("CAPABILITY", "请更新空闲的 bridge 后重新连接，新模型参数不能发送给旧服务。");
        if (kind === "test" && !this.capabilities.includes("model-test-role-v1")) throw new Bridge.SafeError("CAPABILITY", "本地服务尚未支持区分 QC / ZH 测试；请更新空闲的 bridge 后重新连接，未发送测试请求。");
        let model = Bridge.profile(this.readModel(phase), kind === "models");
        if (model.provider === "tongji" && !this.capabilities.includes("tongji-api-v1")) throw new Bridge.SafeError("CAPABILITY", "本机服务尚不支持同济 API，请更新 bridge 后重新连接；尚未发送请求。");
        const docsOnly = kind === "models" && Bridge.publicModelDocs(model);
        // Official GLM documentation fetching is credential-free. Saving/testing a
        // Coding Plan address still follows the existing endpoint confirmation.
        if (!docsOnly) model = this.confirmEndpoint(phase, model);
        if (!this.credentials[model.provider] && !docsOnly) throw new Bridge.SafeError("CONFIG", "请先保存所选 provider 的密钥。");
        if (kind === "models") {
          this.clearModelOptions(phase);
          this.text(phase + "-model-notice", "正在获取模型列表；旧列表已作废，等待本次结果。");
          this.text("model-result", "正在获取模型列表，尚无本次结果。");
        }
        let id;
        try { id = kind === "models" ? await this.client.models(model.provider, model) : await this.client.test(model, phase); }
        catch (e) {
          if (kind === "models") {
            this.text(phase + "-model-notice", "本次模型列表请求失败；旧列表已作废，请重试或手填模型 ID。");
            this.text("model-result", "模型列表请求失败，尚无本次结果。");
          }
          throw e;
        }
        if (this.closed) return;
        this.pending = { id, kind, phase, provider: model.provider, profile: JSON.stringify(model), endpoint: Bridge.endpointSignature(model) }; this.message("模型请求已提交，等待结果…");
        this.text("model-result", `操作 ${id} · ${kind === "models" ? "读取模型列表" : "测试模型"}`);
      });
    }
    renderModelResult(pending, result) {
      if (this.closed) return;
      if (pending.kind === "models") {
        let choices;
        try { choices = Bridge.modelChoices(result, pending.provider); }
        catch (e) {
          this.clearModelOptions(pending.phase);
          this.text(pending.phase + "-model-notice", "本次模型列表响应无效；没有可用的新列表，请重试或手填。");
          this.text("model-result", "模型列表响应无效；旧列表已作废，当前模型及参数未改动。"); throw e;
        }
        if (this.el(pending.phase + "-provider").value !== pending.provider) {
          this.message("服务商已改变，丢弃旧模型列表；当前模型未改动。"); return;
        }
        const current = this.modelContext(pending.phase);
        if (!pending.endpoint || pending.endpoint !== Bridge.endpointSignature(current)) {
          this.message("API 地址或协议已改变，旧模型列表未应用。"); return;
        }
        if (choices.endpoint_kind && choices.endpoint_kind !== Bridge.catalogEndpointKind(current)) {
          this.clearModelOptions(pending.phase);
          this.message("模型列表端点类型与当前 API 地址不符，列表未应用；请重新获取或手填。", true); return;
        }
        if (Bridge.endpointKind(current) === "glm_coding_plan") {
          const models = choices.models.filter(id => ["glm-5.3", "glm-5.3-flash"].includes(id));
          choices = { ...choices, models, details: Object.fromEntries(models.filter(id => Object.hasOwn(choices.details, id)).map(id => [id, choices.details[id]])) };
        }
        this.modelOptions[pending.phase] = { ...choices, endpoint: Bridge.endpointSignature(current) };
        this.pickers[pending.phase + "-model"].setOptions(choices.models.map(id => ({ value: id,
          label: this.client.redact(choices.details[id]?.display_name ? `${choices.details[id].display_name} · API ID: ${id}` : id) })));
        this.updateEffortOptions(pending.phase);
        const source = { bundled_catalog: "内置官方文档候选（非实时，不是账号可用清单）",
          official_docs: "本次联网获取的官方文档候选（不是账号可用清单）",
          official_cache: "官方文档缓存候选（非实时，不是账号可用清单）",
          provider_api: "本次服务商 API 返回的模型列表（不保证调用权限）" }[choices.source];
        const note = `${source}；${choices.models.length} 项。获取时间：${choices.fetched_at || "未提供"}。` +
          (choices.endpoint_kind ? `端点类型：${choices.endpoint_kind}。` : "") + (choices.reference ? `参考：${choices.reference}` : "");
        this.text(pending.phase + "-model-notice", note);
        this.text("model-result", `${pending.phase.toUpperCase()}：${note}\n${choices.notice}\n展示名 / 版本名仅供识别，请求使用 API ID。选择已知模型会在本角色草稿填写最高参数，请保存模型配置。候选列表不能代替实际测试。`);
        this.modelResultPhase = pending.phase;
        this.message("模型候选已载入，当前模型保持不变。"); this.update(); return;
      }
      // Preserve only documented public test fields; never expose raw responses/credentials.
      const filtered = JSON.stringify(Bridge.testResult(result), null, 2);
      this.modelResultPhase = null;
      this.testStates[pending.phase] = { ok: result?.ok === true, at: new Date().toLocaleString(), profile: pending.profile };
      this.text(pending.phase + "-test-state", `${result?.ok === true ? "测试通过" : "测试未确认通过"} · ${this.testStates[pending.phase].at}；只代表本次请求。`);
      this.text("model-result", `${pending.phase.toUpperCase()} ${pending.kind === "models" ? "模型列表" : "测试结果"}\n${filtered ?? "（空结果）"}`);
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
      this.applyModelMaximum(phase);
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
      this.handlers = []; this.pending = null;
      this.el("glm-key").value = ""; this.el("deepseek-key").value = ""; this.el("tongji-key").value = ""; this.el("mineru-key").value = "";
      this.client.close(); this.host.panelClosed?.(this.window);
      // No HTTP request, no process termination. Worker ownership belongs to bridge.
    }
  }
  return { Controller, duration };
})();
if (typeof module !== "undefined") module.exports = LibrarySyncPanel;
else {
  window.addEventListener("DOMContentLoaded", async () => {
    const { host, selected } = window.arguments[0];
    const controller = new LibrarySyncPanel.Controller(window, host, host.createClient());
    window.librarySyncController = controller;
    window.addEventListener("unload", () => controller.close(), { once: true });
    await controller.init(selected);
  }, { once: true });
}
