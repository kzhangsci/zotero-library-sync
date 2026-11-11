/* Pure protocol layer. Transport and filesystem access are injected for offline tests. */
var LibrarySyncBridge = (() => {
  "use strict";
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const ITEM_KEY = /^[A-Z0-9]{8}$/;
  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  const PROVIDERS = ["glm", "deepseek", "tongji"];
  const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$/;
  const EFFORT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  const EXTENDED = ["base_url", "models_url", "protocol", "thinking", "extra_body", "endpoint_confirmed"];
  // Single version source for the diagnostics header; the packaged ?v= strings
  // and manifest version are validated separately by the Python build tests.
  const VERSION = "0.2.0-beta.9";
  class SafeError extends Error {
    constructor(code, message, details = {}) {
      super(message); this.name = "SafeError"; this.code = code;
      this.httpStatus = details.httpStatus; this.category = details.category;
    }
  }
  function errorCategory(error) {
    if (error?.category) return error.category;
    if (["NETWORK", "MISSING", "LAUNCH", "CONNECT"].includes(error?.code)) return "connection";
    if (error?.code === "AUTH") return "auth";
    if (["INPUT", "CONFIG", "PATH", "SELECTION", "PREVIEW"].includes(error?.code)) return "validation";
    if (["PROTOCOL", "RESPONSE", "REDIRECT", "TRANSPORT", "DESCRIPTOR"].includes(error?.code)) return "protocol";
    return "request";
  }
  const fail = (code, message) => { throw new SafeError(code, message); };
  const object = x => x !== null && typeof x === "object" && !Array.isArray(x);
  function integer(x, min, max, label) {
    if (!Number.isInteger(x) || x < min || x > max) fail("INPUT", `${label}须为 ${min}–${max} 的整数。`);
    return x;
  }
  function paths(value) {
    const absolute = /^[A-Za-z]:\\/;
    const clean = p => typeof p === "string" && absolute.test(p) &&
      !/[\x00-\x1f"<>|?*]/.test(p) && !p.slice(2).includes(":") &&
      !p.split("\\").some(s => s === ".." || s === "." || /[. ]$/.test(s));
    const repo = String(value?.repo || "").trim().replaceAll("/", "\\").replace(/\\+$/, "");
    const pythonw = String(value?.pythonw || "").trim().replaceAll("/", "\\");
    if (!clean(repo) || !clean(pythonw) || !/\\pythonw\.exe$/i.test(pythonw)) {
      fail("PATH", "请输入 Windows 本地绝对路径；Python 路径必须指向 pythonw.exe。不要输入命令或引号。");
    }
    return { repo, pythonw };
  }
  function descriptor(value) {
    if (!object(value) || value.schema !== 1 || value.host !== "127.0.0.1" ||
        !Number.isInteger(value.port) || value.port < 1024 || value.port > 65535 ||
        !Number.isSafeInteger(value.pid) || value.pid < 1 ||
        typeof value.token !== "string" || !/^[\x21-\x7e]{16,512}$/.test(value.token)) {
      fail("DESCRIPTOR", "bridge 连接文件无效：须为 schema 1、127.0.0.1、合法端口、PID 和随机 token。");
    }
    // Never expose this object in UI, errors, prefs or logging.
    return { schema: 1, host: "127.0.0.1", port: value.port, token: value.token, pid: value.pid };
  }
  function model(value) {
    if (!object(value) || !PROVIDERS.includes(value.provider) ||
        typeof value.reasoning_effort !== "string" || !EFFORT.test(value.reasoning_effort) || typeof value.model !== "string" ||
        !MODEL_ID.test(value.model.trim()) || value.model.includes("://")) {
      fail("INPUT", "请填写有效模型名称，并选择 provider 与 reasoning effort。");
    }
    return { provider: value.provider, model: value.model.trim(), reasoning_effort: value.reasoning_effort };
  }
  function profileDefaults(provider) {
    if (!PROVIDERS.includes(provider)) fail("INPUT", "Provider / Key 来源须为 glm、deepseek 或 tongji；API 地址可另行自填。");
    if (provider === "tongji") return { provider, model: "", reasoning_effort: "default", timeout: 1500, max_tokens: 32768,
      base_url: "https://agi.tongji.edu.cn/v1", models_url: "https://agi.tongji.edu.cn/v1/models",
      protocol: "openai_chat_completions", thinking: "default", extra_body: {}, endpoint_confirmed: false };
    return { provider, model: "", reasoning_effort: "max", timeout: 1500, max_tokens: provider === "glm" ? 131072 : 393216,
      base_url: provider === "glm" ? "https://open.bigmodel.cn/api/paas/v4" : "https://api.deepseek.com",
      models_url: provider === "glm" ? "" : "https://api.deepseek.com/models",
      protocol: "openai_chat_completions", thinking: "enabled", extra_body: {}, endpoint_confirmed: false };
  }
  // Defensive input limits, not a statement of any model's output capability.
  function tokenInputLimit(provider) { return provider === "glm" ? 131072 : provider === "tongji" ? 1048576 : 393216; }
  // Built-in habit templates. Their names recall how ZCode / DeepSeek Harness
  // (dsh) are commonly used, but this adapter never reads those clients'
  // files, live settings or credentials, and never claims to import them.
  function habitPresets() {
    return [
      { id: "zcode-main", label: "内置预设 · GLM-5.2 · max（仿 ZCode 习惯）", provider: "glm", model: "glm-5.2", effort: "max",
        note: "插件内置模板：GLM-5.2 / max，命名参考 ZCode CLI 的常见默认用法；不读取、不同步 ZCode 客户端配置或凭据，也不代表客户端当前配置，请按自身情况确认。" },
      { id: "zcode-glm53", label: "内置预设 · GLM-5.3 · max（仿 ZCode 习惯）", provider: "glm", model: "glm-5.3", effort: "max",
        note: "插件内置模板：GLM-5.3 / max，命名参考 ZCode CLI 的常见用法；不读取、不同步 ZCode 客户端配置或凭据，也不代表客户端当前配置，请按自身情况确认。" },
      { id: "dsh", label: "内置预设 · deepseek-flash · max（仿 dsh 习惯）", provider: "deepseek", model: "deepseek-flash", effort: "max",
        note: "插件内置模板：deepseek-flash / max，命名参考 DeepSeek Harness（dsh）的常见用法；不读取、不同步 dsh 客户端配置或凭据，也不代表客户端当前配置，请按自身情况确认。" }
    ];
  }
  function habitPreset(id) {
    const preset = habitPresets().find(p => p.id === id);
    if (!preset) fail("INPUT", "请选择已列出的习惯预设。");
    return { ...profileDefaults(preset.provider), model: preset.model,
      reasoning_effort: preset.effort, thinking: "enabled" };
  }
  function endpointKind(value) {
    if (value?.protocol !== "openai_chat_completions" || !PROVIDERS.includes(value?.provider)) return "custom";
    // Match the backend catalog scope: even an official base with a nondefault
    // listing URL is custom, and cannot inherit official model capabilities.
    const modelsURL = String(value.models_url ?? profileDefaults(value.provider).models_url).trim();
    if (modelsURL && modelsURL !== profileDefaults(value.provider).models_url) return "custom";
    try {
      const url = new URL(value.base_url);
      if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.port) return "custom";
      const path = url.pathname.replace(/\/+$/, "");
      if (value.provider === "glm" && url.hostname === "open.bigmodel.cn") {
        if (path === "/api/paas/v4") return "glm_standard";
        if (path === "/api/coding/paas/v4") return "glm_coding_plan";
      }
      if (value.provider === "deepseek" && url.hostname === "api.deepseek.com" && ["", "/v1"].includes(path)) return "deepseek";
      if (value.provider === "tongji" && url.hostname === "agi.tongji.edu.cn" && path === "/v1") return "tongji";
    } catch (_) { /* Unknown or incomplete drafts have no official capabilities. */ }
    return "custom";
  }
  function catalogEndpointKind(value) {
    const kind = endpointKind(value);
    return ["custom", "tongji"].includes(kind) ? kind : kind === "glm_coding_plan" ? "coding_plan" : "standard";
  }
  function endpointSignature(value) {
    return JSON.stringify([String(value?.base_url || "").trim().replace(/\/+$/, ""),
      String(value?.models_url || "").trim(), value?.protocol]);
  }
  function publicModelDocs(value) {
    return ["glm_standard", "glm_coding_plan"].includes(endpointKind(value)) && !String(value.models_url || "").trim();
  }
  function modelDetails(value, choices) {
    const name = String(value?.model || "").trim(), kind = endpointKind(value);
    if (!choices || choices.provider !== value?.provider || choices.endpoint !== endpointSignature(value) ||
        !choices.models?.includes(name)) return null;
    if (choices.endpoint_kind && choices.endpoint_kind !== catalogEndpointKind(value)) return null;
    // Catalogs describe their official endpoint, never a third-party proxy.
    if (["official_docs", "official_cache", "bundled_catalog"].includes(choices.source)) {
      if (kind === "custom") return null;
    } else if (choices.source === "provider_api") {
      if (choices.reference !== undefined && choices.reference !== value.models_url) return null;
      if (kind === "custom" && (value.endpoint_confirmed !== true || !value.models_url || choices.reference !== value.models_url)) return null;
    } else return null;
    if (kind === "glm_coding_plan" && !["glm-5.3", "glm-5.3-flash"].includes(name)) return null;
    return Object.hasOwn(choices.details || {}, name) ? choices.details[name] : null;
  }
  function modelMaximum(value, choices) {
    if (!PROVIDERS.includes(value?.provider)) return null;
    const name = String(value?.model || "").trim(), kind = endpointKind(value);
    const knownGLM = kind === "glm_standard" && ["glm-5.2", "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal"].includes(name);
    const knownDS = kind === "deepseek" && ["deepseek-flash", "deepseek-v4-pro"].includes(name);
    const detail = modelDetails(value, choices);
    const maxTokens = detail?.max_output_tokens ?? (knownGLM ? 131072 : knownDS ? 393216 : null);
    const highest = detail?.highest_effort ?? (!detail?.effort_levels && (knownGLM || knownDS) ? "max" : null);
    // Array order and default_effort do not establish the highest supported effort.
    if (!Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > tokenInputLimit(value?.provider) ||
        !highest || highest === "default" || detail?.effort_levels?.length && !detail.effort_levels.includes(highest)) return null;
    return { max_tokens: maxTokens, reasoning_effort: highest,
      ...(knownGLM || knownDS ? { thinking: "enabled" } : {}) };
  }
  function modelParameterHelp(value, choices) {
    const p = value?.provider, name = String(value?.model || "").trim(), kind = endpointKind(value);
    const glm53 = kind === "glm_standard" && ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal"].includes(name);
    const glm52 = kind === "glm_standard" && name === "glm-5.2";
    const deepseek = kind === "deepseek" && ["deepseek-flash", "deepseek-v4-pro"].includes(name);
    let thinking = "未核实此模型 / 地址的思考默认行为；请按服务商文档设置，default 不代表关闭。";
    let effort = "此模型未绑定固定 effort 档位；可按文档手填，不会自动更改当前值。";
    let tokens = "尚未核实此模型 / 地址的输出上限，请按文档手填；输入框校验范围不代表模型能力。";
    let effortLevels = [], defaultEffort = "";
    if (p === "tongji") {
      thinking = "同济平台使用 reasoning_effort 控制思考，不发送 thinking；此项固定为 default。none 表示关闭思考，default 表示省略参数、采用服务端默认。";
      effort = "请使用校内模型详情或接口返回的 reasoning_effort 档位；平台可能自动调整不支持的档位，不能套用同名厂商模型参数。";
      tokens = "32768 是插件的可编辑请求初值，不是同济平台或此模型的最大输出；模型输出上限与上下文窗口须按校内模型详情核实。";
      if (kind === "tongji" && ["DeepSeek-V4.1-Flash", "MiMo-V2.6-Flash"].includes(name)) {
        effortLevels = name === "DeepSeek-V4.1-Flash" ? ["none", "low", "high", "xhigh", "max"] : ["none", "high"];
        defaultEffort = "high";
        effort = `用户提供的同济模型文档列出 ${effortLevels.join(" / ")}，默认 high；none 关闭思考，default 省略参数。平台可能调整不支持的档位，实际行为以校内服务为准。`;
      }
    } else if (glm53 || glm52) {
      defaultEffort = "max";
      effortLevels = glm53 ? ["low", "high", "max"] : ["max", "xhigh", "high", "medium", "low", "minimal", "none"];
      thinking = glm53
        ? "官方 GLM-5.3 系列默认 enabled，必须开启思考；disabled 不受支持。default 与 enabled 当前均开启，但前者依赖服务端默认。"
        : "官方 GLM-5.2 默认 enabled，由模型判断是否需要思考；enabled 不是强制每次深思。default 依赖服务端默认。";
      effort = glm53 ? "官方 GLM-5.3 系列支持 low / high / max，默认 max；其它档位不能照搬 Coding Plan 的映射。"
        : "官方 GLM-5.2 默认 max；none / minimal 放弃思考，low / medium 映射 high，xhigh 映射 max。";
      tokens = "官方通用 API 此模型输出上限 131072（128K），省略 max_tokens 时默认 65536（64K）；插件新模板显式填写最高值，已存配置不会自动覆盖。";
      if (name === "glm-5.3") tokens = "官方 GLM-5.3 上下文窗口为 1M；" + tokens;
    } else if (deepseek) {
      defaultEffort = "high"; effortLevels = ["low", "high", "max"];
      thinking = "官方当前 DeepSeek 默认 enabled；default 与 enabled 当前均开启，但前者依赖服务端默认。reasoning_effort=none 也会关闭思考。";
      effort = "官方当前 DeepSeek 默认 high；max 更深入。内置 DeepSeek 预设显式使用 max，因此 effort=default 与此预设不同。";
      tokens = "官方当前模型上下文 1M，最大输出 393216（384K）；两者不同。省略 max_tokens 时：非思考 8K、思考 64K、max 思考 128K；插件会发送输入框数值。";
      if (value.thinking === "disabled" && !["default", "none"].includes(value.reasoning_effort)) {
        thinking += " 当前 disabled 与显式思考 effort 可能冲突，请按接口要求调整；插件不会静默替换。";
      }
    }
    const detail = modelDetails(value, choices);
    if (detail?.effort_levels?.length) {
      effortLevels = detail.effort_levels;
      defaultEffort = detail.default_effort || "";
      effort = `${choices?.source === "provider_api" ? "本次接口返回的能力元数据" : "已核实官方文档参数快照（非实时）"}给出 effort 候选 ${effortLevels.join(" / ")}；` +
        (detail.highest_effort ? `最高 ${detail.highest_effort}。` : "未明确最高档位，请按文档手填。") +
        "default 仍表示不发送 reasoning_effort，采用服务端默认。";
    }
    if (detail?.default_effort) defaultEffort = detail.default_effort;
    if (detail?.max_output_tokens) tokens = `${choices?.source === "provider_api" ? "本次接口返回的能力元数据" : "已核实官方文档参数快照（非实时）"}：输出上限 ${detail.max_output_tokens}。` +
      (detail.context_window ? `上下文窗口 ${detail.context_window}，与输出上限不同。` : "输出上限不等于上下文窗口。") +
      "获取列表不会覆盖当前参数；按最高值填写后仍需保存。";
    const endpointHelp = p === "tongji"
      ? "同济校内 API 使用独立 Key 和校内模型 ID，保留大小写；需具备相应网络和账户权限。模型列表可能包含 embedding 等非对话模型，请选择支持 Chat Completions 的模型用于 QC / ZH。连接失败不会自动换用其他服务。"
      : p === "glm"
      ? kind === "glm_coding_plan"
        ? "当前为 GLM Coding Plan 端点：仅采用对应 overview 的 glm-5.3 / glm-5.3-flash 清单，不含 FlashX；同名展示不证明底层模型相同，能力须以此端点元数据为准。"
        : "GLM 通用 API 与 Coding Plan 的支持清单不同；Coding Plan 使用独立地址和对应 overview，同名展示不证明底层模型相同。"
      : "模型展示名 / 服务版本与 API ID 分开；请求始终使用 API ID。";
    return { thinking, effort, tokens, effortLevels, defaultEffort, endpointHelp, maximum: modelMaximum(value, choices) };
  }
  function endpoint(value, base = false) {
    if (typeof value !== "string" || !value || value.length > 2048 || /[^\x21-\x7e]|[?#\\%]/.test(value)) fail("INPUT", "API 地址必须为无凭据、查询参数或片段的 HTTPS 地址。");
    let url;
    try { url = new URL(value); } catch (_) { fail("INPUT", "API 地址格式无效。"); }
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    if (url.protocol !== "https:" || url.username || url.password || !host.includes(".") ||
        /(^localhost$|\.(?:localhost|local|localdomain|internal|lan)$)/.test(host) ||
        /^(?:0|10|127|169\.254|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.|$)/.test(host) ||
        value.split("/").some(s => s === "." || s === "..")) fail("INPUT", "API 地址必须指向可信任的公网 HTTPS 服务，不能指向本机或内网。");
    if (base && /\/chat\/completions\/?$/i.test(url.pathname)) fail("INPUT", "API 基址不要包含 /chat/completions，程序会追加该路径。");
    return value.replace(/\/+$/, base ? "" : "/");
  }
  function extraBody(value) {
    const blocked = new Set("model messages stream streamoptions maxtokens maxcompletiontokens reasoningeffort thinking headers header extraheaders defaultheaders authorization authentication auth apikey xapikey key keys token accesstoken bearer cookie cookies password secret credentials credential baseurl modelsurl url endpoint protocol provider endpointconfirmed proxy proxies verify cert timeout tools toolchoice toolcalls functions functioncall paralleltoolcalls extrabody extraquery query params".split(" "));
    let count = 0;
    const walk = (item, depth = 0) => {
      if (++count > 256 || depth > 5) fail("INPUT", "附加参数 JSON 过大或嵌套过深。");
      if (object(item)) {
        if (Object.keys(item).length > 64) fail("INPUT", "附加参数字段过多。");
        for (const [key, child] of Object.entries(item)) {
          if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(key) || blocked.has(key.replace(/[_.-]/g, "").toLowerCase())) fail("INPUT", "附加参数不能包含凭据、地址、模型核心字段或工具调用。");
          walk(child, depth + 1);
        }
      } else if (Array.isArray(item)) item.forEach(child => walk(child, depth + 1));
      else if (typeof item === "string") { if (item.length > 4096) fail("INPUT", "附加参数文本过长。"); }
      else if (typeof item === "number") { if (!Number.isFinite(item) || Math.abs(item) > Number.MAX_SAFE_INTEGER) fail("INPUT", "附加参数数值无效。"); }
      else if (item !== null && typeof item !== "boolean") fail("INPUT", "附加参数须为 JSON。");
    };
    if (!object(value)) fail("INPUT", "附加参数须为 JSON 对象。");
    walk(value);
    if (JSON.stringify(value).length > 16384) fail("INPUT", "附加参数 JSON 过大。");
    return JSON.parse(JSON.stringify(value));
  }
  function profile(value, allowEmpty = false) {
    const empty = allowEmpty && typeof value?.model === "string" && !value.model.trim();
    const result = { ...model(empty ? { ...value, model: "model-list" } : value),
      timeout: integer(value.timeout ?? 1500, 1, 1500, "timeout"),
      max_tokens: integer(value.max_tokens ?? 32768, 1, tokenInputLimit(value.provider), "max_tokens") };
    if (empty) result.model = "";
    if (!EXTENDED.some(k => Object.hasOwn(value, k))) return result;
    const d = profileDefaults(result.provider);
    if ((value.protocol ?? d.protocol) !== d.protocol) fail("INPUT", "当前支持 openai_chat_completions；Responses / Anthropic 需要独立适配，不能仅更改名称或 URL。");
    if (!["default", "enabled", "disabled"].includes(value.thinking ?? "default")) fail("INPUT", "Thinking 须为 default、enabled 或 disabled。");
    if (result.provider === "tongji" && (value.thinking ?? "default") !== "default") fail("INPUT", "同济平台请使用 reasoning_effort 控制思考，thinking 须为 default。");
    const extra = extraBody(value.extra_body ?? {});
    if (result.provider === "tongji") {
      const checkThinking = item => {
        if (Array.isArray(item)) item.forEach(checkThinking);
        else if (object(item)) for (const [key, child] of Object.entries(item)) {
          if (["enablethinking", "chattemplatekwargs"].includes(key.replace(/[_.-]/g, "").toLowerCase()))
            fail("INPUT", "同济平台请使用 reasoning_effort 控制思考，不要附加 enable_thinking 或 chat_template_kwargs。");
          checkThinking(child);
        }
      };
      checkThinking(extra);
    }
    if (value.endpoint_confirmed !== undefined && typeof value.endpoint_confirmed !== "boolean") fail("INPUT", "API 地址确认状态无效。");
    return { ...result, base_url: endpoint(value.base_url ?? d.base_url, true),
      models_url: (value.models_url ?? d.models_url) === "" ? "" : endpoint(value.models_url ?? d.models_url),
      protocol: d.protocol, thinking: value.thinking ?? "default", extra_body: extra,
      endpoint_confirmed: value.endpoint_confirmed ?? false };
  }
  function config(value) {
    if (!object(value) || value.schema !== 1) fail("INPUT", "模型配置 schema 必须为 1。");
    const result = { schema: 1 };
    for (const phase of ["qc", "zh"]) {
      if (typeof value[phase]?.model !== "string" || !value[phase].model.trim()) {
        fail("INPUT", `请填写${phase === "qc" ? "质检 QC" : "翻译 ZH"}的模型 ID；若只想先存密钥，请点击“仅保存 API Keys”。本次尚未发送，输入框保留。`);
      }
      result[phase] = profile(value[phase]);
    }
    return result;
  }
  function defaults() {
    const phase = () => ({ provider: "glm", model: "", reasoning_effort: "default", timeout: 1500, max_tokens: 32768 });
    return { schema: 1, qc: phase(), zh: phase() };
  }
  function publicConfig(value) {
    if (!object(value) || typeof value.configured !== "boolean" || !object(value.credentials) ||
        !["glm", "deepseek"].every(p => typeof value.credentials[p] === "boolean") ||
        value.credentials.tongji !== undefined && typeof value.credentials.tongji !== "boolean" ||
        !(value.config === null && !value.configured || object(value.config) && value.config.schema === 1)) {
      fail("PROTOCOL", "bridge 配置响应格式不符。");
    }
    // Unconfigured servers may return empty model names. Only keep public fields.
    const result = value.config === null
      ? { schema: 1, qc: profileDefaults("glm"), zh: profileDefaults("glm") } : defaults();
    for (const phase of ["qc", "zh"]) {
      if (value.config === null) break;
      const row = value.config[phase];
      if (!object(row) || !PROVIDERS.includes(row.provider) || !EFFORT.test(row.reasoning_effort) ||
          typeof row.model !== "string" || !Number.isInteger(row.timeout) || !Number.isInteger(row.max_tokens)) {
        fail("PROTOCOL", "bridge 模型配置响应格式不符。");
      }
      result[phase] = profile(row, true);
    }
    return { configured: value.configured, config: result,
      credentials: { glm: value.credentials.glm, deepseek: value.credentials.deepseek, tongji: value.credentials.tongji === true } };
  }
  function status(value) {
    if (!object(value) || typeof value.active !== "boolean" ||
        !(value.view === null || object(value.view)) || typeof value.text !== "string") {
      fail("PROTOCOL", "bridge 状态响应格式不符。");
    }
    return value;
  }
  function previewBody(value) {
    if (!object(value) || !Array.isArray(value.keys) || value.keys.length > 25 || !value.keys.every(k => typeof k === "string" && ITEM_KEY.test(k))) {
      fail("INPUT", "最多选择 25 篇文献；key 必须为 8 位大写字母/数字，空列表仅用于检测新增。");
    }
    const translate = value.translate === undefined ? true : value.translate;
    if (typeof translate !== "boolean") fail("INPUT", "翻译选项必须为布尔值。");
    const refresh = value.refresh_source === undefined ? false : value.refresh_source;
    if (typeof refresh !== "boolean") fail("INPUT", "更换全文 PDF 选项必须为布尔值。");
    const body = { keys: [...new Set(value.keys)], translate, workers: integer(value.workers, 1, 4, "workers"),
      chunk_parallel: integer(value.chunk_parallel, 1, 2, "chunk_parallel"), limit: integer(value.limit, 0, 25, "limit") };
    // Keep the legacy payload byte-for-field compatible unless explicitly opted in.
    if (refresh) {
      if (!body.keys.length) fail("INPUT", "重新检测 / 更换全文 PDF 仅适用于所选文献，不能用于自动发现。");
      body.refresh_source = true;
      if (value.primary_keys !== undefined) {
        if (!object(value.primary_keys) || Object.entries(value.primary_keys).some(([parent, attachment]) =>
          !body.keys.includes(parent) || !ITEM_KEY.test(parent) || typeof attachment !== "string" ||
          !ITEM_KEY.test(attachment) || attachment === parent)) fail("INPUT", "全文 PDF 候选必须映射所选父条目到有效的附件 key。");
        if (Object.keys(value.primary_keys).length) body.primary_keys = { ...value.primary_keys };
      }
    }
    return body;
  }
  function route(method, path) {
    const allowed = method === "GET"
      ? ["/v1/status", "/v1/history", "/v1/config", "/v1/mineru"].includes(path) ||
        (path.startsWith("/v1/status/") && RUN_ID.test(path.slice(11))) ||
        (path.startsWith("/v1/operations/") && UUID.test(path.slice(15)))
      : method === "POST" && ["/v1/config", "/v1/keys", "/v1/mineru", "/v1/credentials/reveal", "/v1/test", "/v1/models", "/v1/preview", "/v1/preview/rebind", "/v1/start", "/v1/pause", "/v1/resume"].includes(path);
    if (!allowed) fail("ROUTE", "拒绝协议以外的 bridge 请求。");
  }
  function selections(value) {
    if (!Array.isArray(value) || !value.length || value.length > 25 ||
        value.some(row => !object(row) || typeof row.key !== "string" || !ITEM_KEY.test(row.key) ||
          typeof row.include !== "boolean" || typeof row.translate !== "boolean") ||
        new Set(value.map(row => row.key)).size !== value.length || !value.some(row => row.include)) {
      fail("INPUT", "逐篇选项须使用唯一 Zotero item key 和布尔值，并至少勾选一篇文献。");
    }
    return value.map(({ key, include, translate }) => ({ key, include, translate }));
  }
  function keyUpdates(keys, providers = PROVIDERS) {
    if (!object(keys) || Object.keys(keys).some(p => !providers.includes(p))) fail("INPUT", "密钥字段无效。");
    const sanitized = {};
    for (const p of providers) {
      if (keys[p] === undefined || keys[p] === "") continue;
      if (typeof keys[p] !== "string") fail("INPUT", "密钥格式无效；未发送。");
      const value = keys[p].trim();
      if (!value) continue;
      if (!/^[\x21-\x7e]{1,4096}$/.test(value)) fail("INPUT", "密钥格式无效：请只粘贴 Key，不要包含内部空格、换行或说明文字。未发送，输入保留。");
      sanitized[p] = value;
    }
    return sanitized;
  }
  function mineruStatus(value) {
    if (!object(value) || typeof value.configured !== "boolean" || typeof value.saved !== "boolean" ||
        !["environment", "encrypted", "legacy_file", "missing"].includes(value.source)) fail("PROTOCOL", "MinerU 保存状态响应格式不符。");
    return { configured: value.configured, saved: value.saved, source: value.source };
  }
  function modelChoices(value, provider) {
    if (!object(value) || value.provider !== provider || !PROVIDERS.includes(provider) ||
        value.ok !== undefined && value.ok !== true || value.error ||
        !["provider_api", "bundled_catalog", "official_docs", "official_cache"].includes(value.source) || !Array.isArray(value.models) ||
        value.models.length > 512 || !value.models.every(m => typeof m === "string" && MODEL_ID.test(m) && !m.includes("://")) ||
        typeof value.notice !== "string" || value.notice.length > 1000) fail("MODEL", "模型选项响应无效；当前模型未改动。");
    const metadata = {};
    if (value.fetched_at === null) metadata.fetched_at = null;
    else if (value.fetched_at !== undefined) {
      if (typeof value.fetched_at !== "string" || value.fetched_at.length > 64 ||
          !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value.fetched_at) ||
          !Number.isFinite(Date.parse(value.fetched_at))) fail("MODEL", "模型列表获取时间无效。");
      metadata.fetched_at = value.fetched_at;
    }
    if (value.endpoint_kind !== undefined) {
      if (typeof value.endpoint_kind !== "string" || !EFFORT.test(value.endpoint_kind)) fail("MODEL", "模型列表端点类型无效。");
      metadata.endpoint_kind = value.endpoint_kind;
    }
    if (value.cache !== undefined) {
      if (!["hit", "stale"].includes(value.cache)) fail("MODEL", "模型列表缓存标记无效。");
      metadata.cache = value.cache;
    }
    if (value.reference !== undefined) {
      if (typeof value.reference !== "string" || value.reference.length > 2048 || /[\x00-\x20\x7f]/.test(value.reference)) fail("MODEL", "模型列表参考地址无效。");
      try {
        const url = new URL(value.reference);
        if (url.protocol !== "https:" || url.username || url.password || url.search || value.reference.includes("\\")) throw new Error();
        endpoint(url.origin);
      }
      catch (_) { fail("MODEL", "模型列表参考地址须为无凭据的 HTTPS 地址。"); }
      metadata.reference = value.reference;
    }
    const details = {};
    if (value.details !== undefined) {
      if (!object(value.details) || Object.keys(value.details).length > 512) fail("MODEL", "模型参数候选响应无效。");
      for (const [id, entry] of Object.entries(value.details)) {
        if (!value.models.includes(id) || !object(entry)) fail("MODEL", "模型参数候选与模型 ID 不符。");
        const result = {};
        if (entry.effort_levels !== undefined) {
          if (!Array.isArray(entry.effort_levels) || entry.effort_levels.length > 16 || !entry.effort_levels.every(x => typeof x === "string" && EFFORT.test(x))) fail("MODEL", "模型 effort 候选无效。");
          result.effort_levels = [...new Set(entry.effort_levels)];
        }
        for (const key of ["default_effort", "highest_effort"]) {
          if (entry[key] === undefined) continue;
          if (typeof entry[key] !== "string" || !EFFORT.test(entry[key]) ||
              key === "highest_effort" && (entry[key] === "default" || result.effort_levels?.length && !result.effort_levels.includes(entry[key]))) fail("MODEL", "模型默认 / 最高 effort 无效。");
          result[key] = entry[key];
        }
        for (const key of ["max_output_tokens", "context_window"]) {
          if (entry[key] === undefined) continue;
          if (!Number.isSafeInteger(entry[key]) || entry[key] < 1) fail("MODEL", "模型 token 能力无效。");
          result[key] = entry[key];
        }
        if (entry.display_name !== undefined) {
          if (typeof entry.display_name !== "string" || !entry.display_name.trim() || entry.display_name.length > 256 ||
              /[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/.test(entry.display_name)) fail("MODEL", "模型展示名无效。");
          result.display_name = entry.display_name;
        }
        Object.defineProperty(details, id, { value: result, enumerable: true });
      }
    }
    return { provider, models: [...new Set(value.models)], source: value.source, notice: value.notice, details, ...metadata };
  }
  function testResult(value) {
    if (!object(value)) fail("MODEL", "模型测试响应格式无效。");
    const result = {};
    if (typeof value.ok === "boolean") result.ok = value.ok;
    for (const key of ["provider", "role", "requested_model", "model", "reasoning_effort", "finish_reason"]) {
      if (typeof value[key] === "string" && value[key].length <= 256) result[key] = value[key];
    }
    for (const key of ["attempts", "elapsed_seconds"]) {
      if (typeof value[key] === "number" && Number.isFinite(value[key]) && value[key] >= 0) result[key] = value[key];
    }
    if (object(value.usage)) {
      const usage = {};
      const counts = (source, names) => Object.fromEntries(names.filter(k =>
        Number.isSafeInteger(source[k]) && source[k] >= 0).map(k => [k, source[k]]));
      Object.assign(usage, counts(value.usage, ["prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"]));
      for (const [group, fields] of [["prompt_tokens_details", ["cached_tokens"]], ["completion_tokens_details", ["reasoning_tokens"]]]) {
        if (object(value.usage[group])) usage[group] = counts(value.usage[group], fields);
      }
      result.usage = usage;
    }
    return result;
  }
  class Client {
    constructor(io, getPaths) {
      this.io = io; this.getPaths = getPaths; this.session = null; this.connecting = null;
      this.closed = false; this.sensitive = new Set(); this.revealedSensitive = new Map();
    }
    redact(value) {
      let text = String(value ?? "");
      for (const secret of [...this.sensitive, ...this.revealedSensitive.values()]) text = text.split(secret).join("[已隐藏]");
      return text.replace(/\bsk-[A-Za-z0-9_-]+/g, "[已隐藏]")
        .replace(/((?:token|api[_ -]?key|authorization|X-Library-Sync-Token)\s*[=:]\s*)[^\s,;]+/gi, "$1[已隐藏]");
    }
    ensureOpen() { if (this.closed) fail("CLOSED", "面板已关闭。"); }
    async readDescriptor() {
      this.ensureOpen();
      const p = paths(this.getPaths());
      let data;
      try { data = await this.io.readJSON(p.repo + "\\.secrets\\plugin_bridge.json"); }
      catch (e) {
        this.ensureOpen();
        if (e?.code === "MISSING") fail("MISSING", "尚无 bridge 连接文件。请点击“连接 / 启动服务”。");
        fail("DESCRIPTOR", "无法读取 bridge 连接文件；请检查路径、权限与 JSON 格式。");
      }
      this.ensureOpen();
      const parsed = descriptor(data);
      this.sensitive.add(parsed.token);
      return parsed;
    }
    async request(method, path, body) {
      route(method, path);
      this.ensureOpen();
      if (!this.session) {
        const loaded = await this.readDescriptor();
        this.ensureOpen();
        this.session = loaded;
      }
      for (let attempt = 0; attempt < 2; attempt++) {
        this.ensureOpen();
        const current = this.session;
        if (!current) fail("CLOSED", "连接已释放，请重新连接。");
        let response;
        try {
          response = await this.io.request(`http://127.0.0.1:${current.port}${path}`, {
            method, headers: { "X-Library-Sync-Token": current.token, "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
            redirect: "error", credentials: "omit", cache: "no-store"
          });
        } catch (e) {
          // An adapter failure is not evidence that the server is down.
          if (e instanceof SafeError && ["TRANSPORT", "REDIRECT", "RESPONSE"].includes(e.code)) throw e;
          fail("NETWORK", "bridge 无响应或请求超时。请重新连接并查看状态；未自动重发写入请求。");
        }
        if (this.closed) fail("CLOSED", "面板已关闭。");
        if ([401, 403].includes(response.status)) {
          if (attempt === 0) {
            const refreshed = await this.readDescriptor();
            this.ensureOpen();
            this.session = refreshed;
            continue;
          }
          fail("AUTH", "bridge 授权已失效；重新读取后仍未通过，请检查服务后手动连接。");
        }
        // The bridge owns safe_error(); display only its short {error} field after redaction.
        if (response.status < 200 || response.status >= 300) {
          if (path === "/v1/credentials/reveal") fail("CREDENTIAL", "无法显示已存 Key；请核实保存状态及服务版本。未修改任何密钥。");
          const detail = typeof response.data?.error === "string"
            ? "：" + this.redact(response.data.error.slice(0, 1000)) : "；请查看状态与报告目录。";
          const code = String(response.data?.code || response.data?.error_code || "").toLowerCase();
          const category = code.includes("finaliz") ? "finalize"
            : path.startsWith("/v1/status") ? "task"
            : path.startsWith("/v1/operations/") && [400, 404, 410].includes(response.status) ? "operation"
            : response.status >= 500 ? "server" : [400, 409, 422].includes(response.status) ? "validation" : "request";
          throw new SafeError("HTTP", `bridge 请求失败（HTTP ${Number(response.status) || 0}）${detail}`,
            { httpStatus: response.status, category });
        }
        if (!object(response.data)) fail("PROTOCOL", "bridge 返回了无效 JSON 对象。");
        return response.data;
      }
    }
    async connect(launch = false) {
      if (this.connecting) return this.connecting;
      this.connecting = this._connect(launch);
      try { return await this.connecting; } finally { this.connecting = null; }
    }
    async _connect(launch) {
      this.session = null;
      try { return await this.getStatus(); }
      catch (e) {
        if (!launch || !["MISSING", "NETWORK"].includes(e.code)) throw e;
      }
      if (this.closed) fail("CLOSED", "面板已关闭。");
      const p = paths(this.getPaths());
      try { await this.io.launch(p.pythonw, [p.repo + "\\tools\\library_plugin_bridge.py", "--serve", "--apply"]); }
      catch (e) {
        if (e instanceof SafeError && e.code === "PATH") throw e;
        fail("LAUNCH", "无法启动本机后台程序。请核对程序目录、pythonw.exe 和启动权限；XPI 仅包含插件界面。");
      }
      // Launch is not success. Confirm a live, authenticated status endpoint.
      for (let attempt = 0; attempt < 8; attempt++) {
        if (this.closed) fail("CLOSED", "面板已关闭；已启动的服务仍独立运行。");
        await this.io.sleep(500);
        this.session = null;
        try { return await this.getStatus(); }
        catch (e) { if (!["MISSING", "NETWORK"].includes(e.code)) throw e; }
      }
      fail("LAUNCH", "已请求启动进程，但尚未确认 bridge 可用。请查看报告目录并手动重连；这不表示同步成功。");
    }
    async getStatus(runId = null) {
      if (runId !== null && (typeof runId !== "string" || !RUN_ID.test(runId))) fail("INPUT", "无效任务 ID。");
      const result = status(await this.request("GET", "/v1/status" + (runId === null ? "" : "/" + runId)));
      if (runId !== null && result.view?.id !== runId) fail("PROTOCOL", "任务状态 ID 不匹配；未替换当前视图。");
      return result;
    }
    async getHistory() {
      const result = await this.request("GET", "/v1/history");
      if ((result.active !== undefined && typeof result.active !== "boolean") || !Array.isArray(result.runs) || result.runs.length > 1000 ||
          result.runs.some(run => !object(run) || typeof run.id !== "string" || !RUN_ID.test(run.id) || typeof run.status !== "string") ||
          new Set(result.runs.map(run => run.id)).size !== result.runs.length ||
          !Array.isArray(result.capabilities) || result.capabilities.some(c => typeof c !== "string")) {
        fail("PROTOCOL", "运行历史响应无效；保留上次列表。");
      }
      return result;
    }
    async getConfig() { return publicConfig(await this.request("GET", "/v1/config")); }
    async getMineru() { return mineruStatus(await this.request("GET", "/v1/mineru")); }
    async saveConfig(value, keys) {
      this.ensureOpen();
      const normalized = config(value), sanitized = keyUpdates(keys);
      for (const secret of Object.values(sanitized)) this.sensitive.add(secret);
      return publicConfig(await this.request("POST", "/v1/config", { config: normalized, keys: sanitized }));
    }
    async saveKeys(keys) {
      this.ensureOpen();
      const sanitized = keyUpdates(keys);
      for (const secret of Object.values(sanitized)) this.sensitive.add(secret);
      return publicConfig(await this.request("POST", "/v1/keys", { keys: sanitized }));
    }
    async saveMineru(value) {
      this.ensureOpen();
      const key = keyUpdates({ mineru: value }, ["mineru"]).mineru || "";
      if (key) this.sensitive.add(key);
      return mineruStatus(await this.request("POST", "/v1/mineru", { key }));
    }
    async revealKey(provider) {
      if (![...PROVIDERS, "mineru"].includes(provider)) fail("INPUT", "不支持的密钥类型。");
      try {
        const value = await this.request("POST", "/v1/credentials/reveal", { provider, confirm: true });
        this.ensureOpen();
        if (!object(value) || value.provider !== provider || typeof value.key !== "string" ||
            !/^[\x21-\x7e]{1,4096}$/.test(value.key) ||
            !["encrypted", "legacy_file", "environment"].includes(value.source)) {
          fail("CREDENTIAL", "已存 Key 响应无效；未显示，未修改任何密钥。");
        }
        this.revealedSensitive.set(provider, value.key);
        return { provider, key: value.key, source: value.source };
      } catch (_) {
        // Never render the response, exception detail, or a secret-shaped field.
        fail("CREDENTIAL", "无法显示已存 Key；请核实保存状态及服务版本。未修改任何密钥。");
      }
    }
    forgetRevealed(provider) { this.revealedSensitive.delete(provider); }
    async operationRequest(path, body) {
      const result = await this.request("POST", path, body);
      if (!UUID.test(result.operation)) fail("PROTOCOL", "bridge 未返回有效 operation UUID。");
      return result.operation;
    }
    preview(value) { return this.operationRequest("/v1/preview", previewBody(value)); }
    async rebind(previewId) {
      // Lightweight model-snapshot update for an existing preview. A change
      // issues a NEW preview id (the old one can no longer start); the panel
      // must adopt the returned id as its current preview.
      if (!RUN_ID.test(previewId)) fail("INPUT", "缺少有效预演 ID，请先预演。");
      const result = await this.request("POST", "/v1/preview/rebind", { preview_id: previewId, confirm: true });
      if (!RUN_ID.test(result.preview_id) || result.rebound !== true) {
        fail("PROTOCOL", "预演模型更新响应无效；预演未被修改。");
      }
      return result;
    }
    test(value, role = "qc") {
      if (!["qc", "zh"].includes(role)) fail("INPUT", "测试角色须为 qc 或 zh。");
      return this.operationRequest("/v1/test", { ...(EXTENDED.some(k => Object.hasOwn(value, k)) ? profile(value) : model(value)), role });
    }
    models(provider, value = null, refresh = true) {
      if (!PROVIDERS.includes(provider)) fail("INPUT", "请选择有效 provider。");
      if (value && value.provider !== provider) fail("INPUT", "模型列表的 Provider 与配置不一致。");
      // Auto-loaded catalogs ask the bridge for its TTL cache (refresh:false);
      // the manual button forces a remote refresh. Omitting the flag keeps the
      // legacy request body byte-for-byte compatible.
      return this.operationRequest("/v1/models", { ...(value ? profile(value, true) : { provider }),
        ...(refresh ? {} : { refresh: false }) });
    }
    async operation(id) {
      if (!UUID.test(id)) fail("INPUT", "无效 operation UUID。");
      const result = await this.request("GET", "/v1/operations/" + id);
      if (!["running", "completed", "failed"].includes(result.status)) fail("PROTOCOL", "bridge 操作状态无效。");
      return result;
    }
    async start(previewId, choices) {
      if (!RUN_ID.test(previewId)) fail("INPUT", "缺少有效预演 ID，请先预演。");
      const result = await this.request("POST", "/v1/start", { preview_id: previewId, confirm: true,
        ...(choices === undefined ? {} : { selections: selections(choices) }) });
      if (!RUN_ID.test(result.run_id) || typeof result.started !== "boolean") fail("PROTOCOL", "bridge 未返回有效 run_id 和 started。");
      return { run_id: result.run_id, started: result.started };
    }
    async pause(runId) {
      if (typeof runId !== "string" || !RUN_ID.test(runId)) fail("INPUT", "无效任务 ID。");
      const result = await this.request("POST", "/v1/pause", { run_id: runId, confirm: true });
      if (result.run_id !== runId || !["pause_requested", "paused"].includes(result.status) || typeof result.message !== "string") {
        fail("PROTOCOL", "暂停响应无效；请刷新任务状态，未自动重试。");
      }
      return result;
    }
    async resume(runId, finalizeOnly = false) {
      if (typeof runId !== "string" || !RUN_ID.test(runId) || typeof finalizeOnly !== "boolean") fail("INPUT", "恢复选项无效。");
      const result = await this.request("POST", "/v1/resume", { run_id: runId, confirm: true, finalize_only: finalizeOnly });
      const joined = result.joined === true || result.already_submitted === true;
      const validSource = typeof result.resume_of === "string" && RUN_ID.test(result.resume_of);
      if (typeof result.run_id !== "string" || !RUN_ID.test(result.run_id) || typeof result.started !== "boolean" ||
          (result.joined !== undefined && typeof result.joined !== "boolean") ||
          (result.already_submitted !== undefined && typeof result.already_submitted !== "boolean") ||
          (!validSource && !(result.resume_of === undefined && joined && result.started === false))) {
        fail("PROTOCOL", "恢复响应无效；请刷新任务状态，未自动重试。");
      }
      // A failed preflight child retries the original paused source. Keep both
      // server IDs intact; the returned run_id is the new attempt to display.
      return { ...result, joined };
    }
    close() { this.closed = true; this.session = null; this.sensitive.clear(); this.revealedSensitive.clear(); }
  }
  return { Client, SafeError, errorCategory, selections, paths, descriptor, config, defaults, profile, profileDefaults, tokenInputLimit,
    habitPresets, habitPreset, modelParameterHelp, modelMaximum, modelDetails, endpointKind, catalogEndpointKind, endpointSignature, publicModelDocs,
    previewBody, model, keyUpdates, modelChoices, testResult, UUID, ITEM_KEY, RUN_ID, VERSION };
})();
if (typeof module !== "undefined") module.exports = LibrarySyncBridge;
