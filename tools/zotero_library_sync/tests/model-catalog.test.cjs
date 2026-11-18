"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { Bridge, fixture, ui, ROOT, OP } = require("./helpers.cjs");

// Every request uses the injected, in-memory bridge transport from helpers.cjs.
const WHEN = "2026-10-26T12:34:56+00:00";
const DOC = "https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/chat.md";
const CODING = "https://open.bigmodel.cn/api/coding/paas/v4";
const detail = (display_name, max_output_tokens = 131072) => ({ display_name, max_output_tokens,
  context_window: 1048576, effort_levels: ["low", "high", "max"], default_effort: "high", highest_effort: "max" });
function catalog(provider = "glm", source = "official_docs") {
  const models = provider === "glm" ? ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal"]
    : ["deepseek-flash", "deepseek-v4-pro"];
  return { provider, models, source, notice: "synthetic candidate notice", fetched_at: WHEN, endpoint_kind: "standard",
    reference: provider === "glm" ? DOC : "https://api.deepseek.com/models",
    details: Object.fromEntries(models.map(id => [id, detail(id === "deepseek-flash" ? "DeepSeek-V4.1-Flash-1017" : id,
      provider === "glm" ? 131072 : 393216)])) };
}
async function fetchList(u, phase, value) {
  u.f.operationResult = value;
  await u.controller.modelAction(phase, "models"); await u.controller.tick();
}
async function pick(u, phase, label) {
  await u.el(phase + "-model-toggle").emit("click");
  const row = u.el(phase + "-model-options").children.find(row => row.textContent === label);
  assert.ok(row, label); await row.emit("click");
}
function blank() {
  const f = fixture(); f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  return ui(f);
}

test("parameter help distinguishes live API capabilities from official snapshots", () => {
  for (const source of ["official_docs", "official_cache", "bundled_catalog", "provider_api"]) {
    const provider = source === "provider_api" ? "deepseek" : "glm";
    const profile = { ...Bridge.hydrationDefaults(provider), model: provider === "glm" ? "glm-5.3" : "deepseek-flash" };
    const choices = { ...Bridge.modelChoices(catalog(provider, source), provider), endpoint: Bridge.endpointSignature(profile) };
    const help = Bridge.modelParameterHelp(profile, choices);
    for (const field of ["effort", "tokens"]) {
      assert.match(help[field], source === "provider_api" ? /本次接口返回的能力元数据/ : /官方文档参数快照（非实时）/);
      if (source !== "provider_api") assert.doesNotMatch(help[field], /本次接口返回的能力元数据/);
    }
  }
});

test("model catalog allowlist retains bounded public metadata and discards all unlisted fields", () => {
  const source = catalog("deepseek", "provider_api");
  source.credentials = { api_key: "synthetic-root-secret" };
  source.details["deepseek-flash"].headers = { Authorization: "synthetic-nested-secret" };
  const got = Bridge.modelChoices(source, "deepseek");
  assert.deepEqual(Object.keys(got).sort(), ["provider", "models", "source", "notice", "details", "fetched_at", "endpoint_kind", "reference"].sort());
  assert.equal(got.models[0], "deepseek-flash");
  assert.equal(got.details["deepseek-flash"].display_name, "DeepSeek-V4.1-Flash-1017");
  assert.equal(got.details["deepseek-flash"].highest_effort, "max");
  assert.equal(got.details["deepseek-flash"].default_effort, "high");
  assert.equal(got.details["deepseek-flash"].max_output_tokens, 393216);
  assert.doesNotMatch(JSON.stringify(got), /synthetic-.*secret|Authorization|credentials/);
  assert.equal(Bridge.modelChoices(catalog(), "glm").reference, DOC); // Encoded official document paths are valid.
  assert.equal(Bridge.modelChoices({ ...catalog(), fetched_at: null, source: "bundled_catalog" }, "glm").fetched_at, null);
  assert.throws(() => Bridge.modelChoices({ provider: "glm", models: ["__proto__"], source: "provider_api", notice: "safe" }, "glm"), { code: "MODEL" });
  const proto = Bridge.modelChoices({ provider: "glm", models: ["constructor"], source: "provider_api", notice: "safe",
    details: JSON.parse('{"constructor":{"display_name":"inert"}}') }, "glm");
  assert.equal(Object.getPrototypeOf(proto.details), Object.prototype);
  assert.equal(Object.hasOwn(proto.details, "constructor"), true);
});

test("malformed or failed model lists cannot masquerade as successful current metadata", () => {
  const base = catalog();
  for (const delta of [{ ok: false }, { error: "synthetic failure" }, { source: "live" }, { fetched_at: "today" },
    { fetched_at: 123 }, { endpoint_kind: "bad kind" }, { reference: "http://example.org/docs" },
    { reference: "https://secret@example.org/docs" }, { reference: "https://example.org/docs?key=secret" },
    { reference: "https://127.0.0.1/docs" }, { reference: "https://example.org/\nraw" }]) {
    assert.throws(() => Bridge.modelChoices({ ...base, ...delta }, "glm"), { code: "MODEL" });
  }
  for (const entry of [{ display_name: "x".repeat(257) }, { display_name: "bad\nname" }, { display_name: "\u202eID" },
    { max_output_tokens: -1 }, { max_output_tokens: "131072" }, { context_window: 0 },
    { highest_effort: "default" }, { highest_effort: "bad level" }, { highest_effort: "max", effort_levels: ["low"] }]) {
    assert.throws(() => Bridge.modelChoices({ ...base, details: { "glm-5.3": entry } }, "glm"), { code: "MODEL" });
  }
});

test("new blank templates default to thinking on + effort high without changing legacy defaults or the meaning of default", async t => {
  for (const [provider, cap] of [["glm", 131072], ["deepseek", 393216], ["tongji", 32768]]) {
    const p = Bridge.profileDefaults(provider);
    assert.equal(p.model, ""); assert.equal(p.max_tokens, cap); assert.equal(p.reasoning_effort, "high");
    assert.equal(p.thinking, provider === "tongji" ? "default" : "enabled");
    assert.equal(Bridge.profile({ ...p, model: "known", reasoning_effort: "default", thinking: "default" }).reasoning_effort, "default");
  }
  // Fresh glm drafts explicitly name the Coding Plan endpoint; the model stays
  // an explicit user choice and saved profiles never migrate.
  assert.equal(Bridge.profileDefaults("glm").base_url, CODING);
  assert.equal(Bridge.profileDefaults("zcode").reasoning_effort, "high");
  assert.deepEqual(Bridge.defaults().qc, { provider: "glm", model: "", reasoning_effort: "default", timeout: 1500, max_tokens: 32768 });
  const u = blank(); t.after(() => u.controller.close()); await u.controller.init();
  for (const phase of ["qc", "zh"]) {
    assert.equal(Number(u.el(phase + "-max-tokens").value), 131072);
    assert.equal(u.el(phase + "-effort").value, "high"); assert.equal(u.el(phase + "-thinking").value, "enabled");
    assert.equal(u.el(phase + "-base-url").value, CODING); assert.equal(u.el(phase + "-model").value, "");
  }
  // Explicit "restore defaults" returns the user-preferred high, not the old max.
  u.el("qc-effort").value = "low"; u.el("qc-thinking").value = "disabled";
  await u.el("qc-defaults").emit("click");
  assert.equal(u.el("qc-effort").value, "high"); assert.equal(u.el("qc-thinking").value, "enabled");
  assert.match(u.el("qc-provider-notice").textContent, /用户偏好默认.*effort=high/);
  // Provider switches migrate only untouched defaults; manual values survive.
  u.el("qc-effort").value = "high";
  const pickProvider = async label => {
    await u.el("qc-provider-toggle").emit("click");
    const row = u.el("qc-provider-options").children.find(row => row.textContent === label);
    assert.ok(row, label); await row.emit("click");
  };
  await pickProvider("DeepSeek");
  assert.equal(u.el("qc-effort").value, "high"); assert.equal(u.el("qc-thinking").value, "enabled");
  assert.equal(u.el("qc-base-url").value, "https://api.deepseek.com");
  await pickProvider("同济校内 API（独立 Key）");
  assert.equal(u.el("qc-effort").value, "high");   // Tongji's thinking-on is the effort itself
  assert.equal(u.el("qc-thinking").value, "default");
  assert.equal(u.f.posts("/v1/models").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
});

test("saved legacy and extended profiles survive init, metadata loading and reload without highest-value migration", async () => {
  for (const configured of [false, true]) {
    for (const max_tokens of [32768, 100000, 131072]) {
      const f = fixture();
      const legacy = { provider: "glm", model: "glm-5.3", reasoning_effort: "default", timeout: 123, max_tokens };
      const extended = { ...Bridge.profileDefaults("deepseek"), model: "deepseek-flash", reasoning_effort: "high",
        thinking: "disabled", max_tokens: 11111, timeout: 456, extra_body: { temperature: 0.2 } };
      f.publicConfig = { configured, config: { schema: 1, qc: legacy, zh: extended }, credentials: { glm: true, deepseek: true } };
      const original = structuredClone(f.publicConfig), u = ui(f); await u.controller.init();
      const qc = u.controller.readModel("qc"), zh = u.controller.readModel("zh");
      assert.equal(qc.thinking, "default"); assert.equal(qc.reasoning_effort, "default"); assert.equal(qc.max_tokens, max_tokens);
      assert.deepEqual(zh, extended);
      await fetchList(u, "qc", catalog()); await fetchList(u, "zh", catalog("deepseek", "provider_api"));
      await u.controller.reloadConfig(); await u.controller.connect(false); await u.controller.tick();
      assert.deepEqual(u.controller.readModel("qc"), qc); assert.deepEqual(u.controller.readModel("zh"), zh);
      assert.deepEqual(f.publicConfig, original); assert.deepEqual(u.controller.savedConfig, original.config);
      assert.equal(f.posts("/v1/config").length, 0); assert.equal(f.posts("/v1/test").length, 0);
      u.controller.close();
    }
  }
});

test("display and version labels search and select as labels but only API IDs enter profile requests", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  const other = u.controller.readModel("qc"), saved = structuredClone(u.controller.savedConfig);
  await fetchList(u, "zh", catalog("deepseek", "provider_api"));
  assert.equal(u.el("zh-model").value, "deepseek-test-model");
  const label = "DeepSeek-V4.1-Flash-1017 · API ID: deepseek-flash";
  u.el("zh-model").value = "1017"; await u.el("zh-model").emit("input");
  assert.deepEqual(u.el("zh-model-options").children.map(row => row.textContent), [label]);
  await u.el("zh-model-options").children[0].emit("click");
  assert.equal(u.el("zh-model").value, "deepseek-flash"); assert.equal(Number(u.el("zh-max-tokens").value), 393216);
  assert.equal(u.el("zh-effort").value, "high"); assert.equal(u.el("zh-thinking").value, "enabled");
  assert.match(u.el("zh-maximum-notice").textContent, /推荐默认参数/);
  assert.match(u.el("zh-maximum-notice").textContent, /尚未保存/);
  assert.deepEqual(u.controller.readModel("qc"), other); assert.deepEqual(u.controller.savedConfig, saved);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
  await u.controller.modelAction("zh", "test"); // Mock transport only; verifies the outgoing field.
  const body = JSON.parse(u.f.posts("/v1/test")[0].body);
  assert.equal(body.model, "deepseek-flash"); assert.equal(body.max_tokens, 393216); assert.equal(body.role, "zh");
  assert.doesNotMatch(body.model, /DeepSeek-V4|1017|API ID/);
});

test("manual input only updates help; an explicit change fills recommended defaults (thinking on, effort high) in one role", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  const other = u.controller.readModel("zh"), saved = structuredClone(u.controller.savedConfig), requests = u.f.requests.length;
  u.el("qc-max-tokens").value = "100000"; u.el("qc-effort").value = "low"; u.el("qc-thinking").value = "disabled";
  for (const text of ["glm-", "glm-5.3", "glm-5.3-flashx-legal"]) {
    u.el("qc-model").value = text; await u.el("qc-model").emit("input");
    assert.equal(Number(u.el("qc-max-tokens").value), 100000); assert.equal(u.el("qc-effort").value, "low");
  }
  await u.el("qc-model").emit("change");
  assert.equal(Number(u.el("qc-max-tokens").value), 131072); assert.equal(u.el("qc-effort").value, "high");
  assert.equal(u.el("qc-thinking").value, "enabled");
  assert.match(u.el("qc-effort-notice").textContent, /推荐默认（用户偏好，新模板即此值）/);
  assert.match(u.el("message").textContent, /QC.*草稿尚未保存/);
  assert.deepEqual(u.controller.savedConfig, saved); assert.deepEqual(u.controller.readModel("zh"), other);
  assert.equal(u.f.requests.length, requests);
});

test("model selection fills recommended defaults with a documented fallback when high is unsupported", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  // Provider metadata without "high": the recommendation falls back to the
  // closest documented thinking-capable level, says so, and hides nothing.
  u.f.operationResult = { provider: "glm", models: ["glm-test-model"], source: "provider_api", notice: "candidates",
    details: { "glm-test-model": { effort_levels: ["low", "medium"], highest_effort: "medium", max_output_tokens: 4096 } } };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  await pick(u, "qc", "glm-test-model");
  assert.equal(u.el("qc-effort").value, "medium"); assert.equal(u.el("qc-thinking").value, "enabled");
  assert.equal(Number(u.el("qc-max-tokens").value), 4096);
  assert.match(u.el("qc-effort-notice").textContent, /已按文档支持档位回退/);
  assert.match(u.el("qc-maximum-notice").textContent, /回退到文档支持的 medium/);
  assert.deepEqual(u.el("qc-effort-options").children.map(x => x.textContent), ["default", "low", "medium"]);
  assert.equal(u.el("qc-maximum").disabled, false);   // the explicit maximum stays available
  // A documented set with no thinking-capable level fills nothing and explains.
  u.f.operationResult = { provider: "glm", models: ["glm-minimal"], source: "provider_api", notice: "candidates",
    details: { "glm-minimal": { effort_levels: ["none"] } } };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  u.el("qc-effort").value = "low";
  await pick(u, "qc", "glm-minimal");
  assert.equal(u.el("qc-effort").value, "low");
  assert.match(u.el("message").textContent, /未自动填写 thinking \/ effort/);
  // The zcode login has no callable route; it recommends nothing at all.
  assert.equal(Bridge.modelRecommended({ provider: "zcode", model: "glm-5.3" }), null);
  assert.equal(Bridge.recommendedEffort(["low", "high", "max"]), "high");
  assert.equal(Bridge.recommendedEffort(["low", "medium"]), "medium");
  assert.equal(Bridge.recommendedEffort(["none"]), null);
  assert.equal(Bridge.recommendedEffort(["low", "future-effort"]), null);
});

test("maximum button preserves model, endpoints, timeout, JSON and other role while editing only draft parameters", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  u.el("qc-model").value = "glm-5.2"; u.el("qc-timeout").value = "321";
  u.el("qc-extra-body").value = '{"temperature":0.25}'; await u.el("qc-model").emit("input");
  const before = u.controller.readModel("qc"), other = u.controller.readModel("zh"), saved = structuredClone(u.controller.savedConfig);
  const requests = u.f.requests.length, reads = u.f.reads.length;
  assert.equal(u.el("qc-maximum").disabled, false); await u.el("qc-maximum").emit("click");
  assert.deepEqual(u.controller.readModel("qc"), { ...before, max_tokens: 131072, reasoning_effort: "max", thinking: "enabled" });
  assert.deepEqual(u.controller.readModel("zh"), other); assert.deepEqual(u.controller.savedConfig, saved);
  assert.equal(u.f.requests.length, requests); assert.equal(u.f.reads.length, reads);
  assert.equal(u.controller.configDirty, true); assert.equal(u.controller.preview, null);
});

test("unknown IDs, display names, case variants and unknown endpoints never inherit provider maxima", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  for (const [provider, model, base_url] of [["glm", "glm-unknown", "https://open.bigmodel.cn/api/paas/v4"],
    ["glm", "GLM-5.3", "https://open.bigmodel.cn/api/paas/v4"], ["glm", "glm-5.3", "https://proxy.example/v1"],
    ["deepseek", "DeepSeek-V4.1-Flash-1017", "https://api.deepseek.com"], ["deepseek", "deepseek-v4-flash", "https://api.deepseek.com"]]) {
    u.controller.fillConfig({ configured: true, credentials: { glm: true, deepseek: true },
      config: { schema: 1, qc: { ...Bridge.profileDefaults(provider), model, base_url, max_tokens: 2345, reasoning_effort: "user-effort", thinking: "disabled" }, zh: u.controller.savedConfig.zh } });
    const before = u.controller.readModel("qc"); await u.el("qc-model").emit("change");
    u.controller.applyModelMaximum("qc");
    assert.deepEqual(u.controller.readModel("qc"), before); assert.equal(u.el("qc-maximum").disabled, true);
    assert.match(u.el("qc-maximum-notice").textContent, /尚未核实.*请.*手填/);
  }
  assert.equal(u.f.posts("/v1/models").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
});

test("custom metadata must match the confirmed endpoint and reference; highest is never inferred from default or array order", () => {
  const p = { ...Bridge.profileDefaults("glm"), model: "my-model", base_url: "https://custom.example/v1",
    models_url: "https://catalog.example/models", endpoint_confirmed: true };
  const choices = { provider: "glm", models: [p.model], source: "provider_api", notice: "custom",
    endpoint_kind: "custom", reference: p.models_url, details: { [p.model]: { ...detail("Team model", 8192), highest_effort: "high" } } };
  const bind = raw => ({ ...Bridge.modelChoices(raw, "glm"), endpoint: Bridge.endpointSignature(p) });
  assert.deepEqual(Bridge.modelMaximum(p, bind(choices)), { max_tokens: 8192, reasoning_effort: "high" });
  for (const delta of [{ endpoint_confirmed: false }, { base_url: "https://other.example/v1" },
    { models_url: "https://catalog.example/other" }, { provider: "deepseek" }]) assert.equal(Bridge.modelMaximum({ ...p, ...delta }, bind(choices)), null);
  for (const delta of [{ reference: "https://wrong.example/models" }, { source: "official_docs" }, { source: "official_cache" }, { endpoint_kind: "standard" }]) {
    assert.equal(Bridge.modelMaximum(p, bind({ ...choices, ...delta })), null);
  }
  for (const entry of [{ max_output_tokens: 8192, effort_levels: ["max", "low"], default_effort: "low" },
    { highest_effort: "max" }, { highest_effort: "max", max_output_tokens: 131073 }]) {
    assert.equal(Bridge.modelMaximum(p, bind({ ...choices, details: { [p.model]: entry } })), null);
  }
});

test("new unfamiliar effort metadata on a known model cannot silently fall back to its old highest effort", () => {
  const p = { ...Bridge.profileDefaults("deepseek"), model: "deepseek-flash" };
  const choices = { ...catalog("deepseek", "provider_api"),
    details: { "deepseek-flash": { max_output_tokens: 8192, effort_levels: ["max", "future-effort"], default_effort: "max" } } };
  const bound = { ...Bridge.modelChoices(choices, "deepseek"), endpoint: Bridge.endpointSignature(p) };
  assert.equal(Bridge.modelMaximum(p, bound), null);
  const help = Bridge.modelParameterHelp(p, bound);
  assert.match(help.effort, /未明确最高档位/); assert.match(help.tokens, /8192/);
  assert.doesNotMatch(help.tokens, /393216|尚未核实此模型/);
});

test("official base with any nondefault models URL is custom, with no official highest fallback", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  for (const models_url of ["https://open.bigmodel.cn/api/paas/v4/models", "https://catalog.example/models"]) {
    u.el("qc-model").value = "glm-5.3"; u.el("qc-models-url").value = models_url;
    await u.el("qc-models-url").emit("input");
    assert.equal(Bridge.catalogEndpointKind(u.controller.modelContext("qc")), "custom");
    assert.equal(Bridge.modelMaximum(u.controller.modelContext("qc")), null);
    assert.equal(u.el("qc-maximum").disabled, true);
    const raw = { ...catalog("glm", "provider_api"), endpoint_kind: "custom", reference: models_url,
      details: { "glm-5.3": { ...detail("Custom alias", 8192), highest_effort: "high" } } };
    await fetchList(u, "qc", raw);
    assert.ok(u.controller.modelOptions.qc); assert.equal(u.controller.endpointConfirmed.qc, true);
    await u.el("qc-maximum").emit("click");
    assert.equal(Number(u.el("qc-max-tokens").value), 8192); assert.equal(u.el("qc-effort").value, "high");
    assert.equal(u.el("qc-model").value, "glm-5.3");
    await fetchList(u, "qc", { ...raw, details: {} });
    assert.equal(u.el("qc-maximum").disabled, true); assert.equal(Bridge.modelMaximum(u.controller.modelContext("qc"), u.controller.modelOptions.qc), null);
  }
  for (const models_url of ["https://api.deepseek.com/other-models", "https://catalog.example/models"]) {
    const p = { ...Bridge.profileDefaults("deepseek"), model: "deepseek-flash", models_url };
    assert.equal(Bridge.catalogEndpointKind(p), "custom"); assert.equal(Bridge.modelMaximum(p), null);
  }
});

test("source labels always show origin and fetched time; cache and bundled data never claim live account permissions", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  for (const [source, expected] of [["official_docs", /本次联网获取.*不是账号可用清单/],
    ["official_cache", /缓存.*非实时.*不是账号可用清单/], ["bundled_catalog", /内置.*非实时.*不是账号可用清单/],
    ["provider_api", /服务商 API.*不保证调用权限/]]) {
    const value = catalog("glm", source);
    if (source === "provider_api") delete value.reference;
    await fetchList(u, "qc", value);
    for (const id of ["qc-model-notice", "model-result"]) {
      assert.match(u.el(id).textContent, expected); assert.ok(u.el(id).textContent.includes(WHEN));
    }
    assert.equal(u.el("qc-model").value, "glm-test-model");
  }
  await fetchList(u, "qc", { ...catalog(), source: "official_cache", fetched_at: null });
  assert.match(u.el("qc-model-notice").textContent, /获取时间：未提供/);
  assert.match(u.el("model-result").textContent, /缓存.*非实时/);
});

test("official GLM docs fetch without a saved key; paid tests and custom listing still require saved keys", async t => {
  const u = blank(); t.after(() => u.controller.close()); await u.controller.init();
  assert.equal(u.el("qc-models").disabled, false); assert.equal(u.el("qc-test").disabled, true);
  // The QC docs fetch needs no saved key. The settling tick also fires the
  // asynchronous initial refresh of BOTH bundled GLM doc seeds (qc and zh by
  // design since beta.14), so assert the request roles, not a total count.
  await fetchList(u, "qc", catalog());
  const listings = u.f.posts("/v1/models");
  assert.ok(listings.length >= 1);
  for (const request of listings) assert.equal(JSON.parse(request.body).provider, "glm");
  assert.equal(u.window.confirms.length, 0);
  u.el("qc-model").value = "glm-5.3"; await u.el("qc-model").emit("change");
  await u.controller.modelAction("qc", "test"); assert.equal(u.f.posts("/v1/test").length, 0);
  assert.match(u.el("message").textContent, /保存.*密钥/);
  const settled = u.f.posts("/v1/models").length;
  u.el("qc-models-url").value = "https://catalog.example/models"; await u.el("qc-models-url").emit("input");
  assert.equal(u.el("qc-models").disabled, true);
  await u.controller.modelAction("qc", "models"); assert.equal(u.f.posts("/v1/models").length, settled);
  assert.equal(u.f.posts("/v1/config").length, 0);
});

test("Coding Plan docs are credential-free, exclude FlashX, and the trusted coding endpoint needs no extra confirmation", async t => {
  const u = blank(); t.after(() => u.controller.close()); await u.controller.init();
  assert.equal(u.controller.endpointConfirmed.qc, false); assert.equal(u.el("qc-models").disabled, false);
  const value = { ...catalog(), endpoint_kind: "coding_plan", reference: "https://docs.bigmodel.cn/cn/coding-plan/overview.md" };
  await fetchList(u, "qc", value);
  assert.equal(u.window.confirms.length, 0); assert.equal(u.controller.endpointConfirmed.qc, false);
  assert.equal(JSON.parse(u.f.posts("/v1/models")[0].body).endpoint_confirmed, false);
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3", "glm-5.3-flash"]);
  await pick(u, "qc", "glm-5.3-flash · API ID: glm-5.3-flash");
  assert.equal(Number(u.el("qc-max-tokens").value), 131072);
  assert.match(u.el("qc-endpoint-notice").textContent, /Coding Plan.*不含 FlashX.*不证明底层模型相同/);
  u.f.publicConfig.credentials.glm = true;
  u.controller.credentials.glm = true;
  u.el("glm-key").value = u.controller.loadedKeys.glm = "synthetic-coding-plan-key";
  u.controller.update();
  // The official coding endpoint is a trusted default: no custom-address
  // confirmation dialog gates the paid test or the save.
  u.window.confirmResult = false; await u.controller.modelAction("qc", "test"); await u.controller.tick();
  assert.equal(u.f.posts("/v1/test").length, 1); assert.equal(u.window.confirms.length, 0);
  u.el("zh-model").value = "glm-5.3"; await u.el("zh-model").emit("input");
  await u.controller.saveConfig(); assert.equal(u.f.posts("/v1/config").length, 1, u.el("message").textContent); assert.equal(u.window.confirms.length, 0);
  assert.equal(u.controller.endpointConfirmed.qc, false);
});

test("Coding Plan cannot receive a standard endpoint catalog or standard static fallback", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  u.el("qc-base-url").value = CODING; u.el("qc-model").value = "glm-5.3-flashx";
  await u.el("qc-base-url").emit("input"); await u.el("qc-model").emit("change");
  assert.equal(Bridge.modelMaximum(u.controller.modelContext("qc")), null);
  await fetchList(u, "qc", catalog());
  assert.equal(u.controller.modelOptions.qc, null); assert.match(u.el("message").textContent, /端点类型.*不符/);
  assert.equal(u.el("qc-model").value, "glm-5.3-flashx");
});

test("endpoint/provider edits invalidate stale options; recognized endpoints reseed the bundled snapshot", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  // Custom edits must leave NO options (no official seeds for custom URLs);
  // switching to an official provider immediately reseeds the bundled
  // snapshot with an honest offline label while an async refresh may follow.
  const expectations = [["base-url", "https://other.example/v1", null],
    ["models-url", "https://other.example/models", null],
    ["protocol", "other", null],
    ["provider", "deepseek", "bundled"]];
  for (const [field, replacement, expected] of expectations) {
    await u.controller.reloadConfig(); await fetchList(u, "qc", catalog());
    const max = u.el("qc-max-tokens").value, effort = u.el("qc-effort").value;
    u.el("qc-" + field).value = replacement; await u.el("qc-" + field).emit("input");
    if (expected === null) {
      assert.equal(u.controller.modelOptions.qc, null); assert.equal(u.el("qc-model-options").children.length, 0);
    } else {
      assert.ok(u.controller.modelOptions.qc); assert.equal(u.controller.modelOptions.qc.source, "bundled_catalog");
      assert.equal(u.controller.modelOptions.qc.seeded, true);
      assert.equal(u.el("qc-model-options").children.length, 2);   // deepseek-flash, deepseek-v4-pro
      assert.match(u.el("qc-model-notice").textContent, /内置官方文档候选（离线快照，不是账号可用清单）/);
    }
    assert.match(u.el("model-result").textContent, /先前.*已作废/);
    assert.equal(u.el("qc-max-tokens").value, max); assert.equal(u.el("qc-effort").value, effort);
  }
});

test("bundled candidates appear immediately on open, then auto-refresh asynchronously and survive refresh failures", async t => {
  // A fully offline first open still shows explicit candidates for the
  // recognized default endpoint; nothing is requested during the synchronous
  // open itself. The seed is not a fetch result, so the next poll refreshes
  // the credential-free GLM doc endpoints automatically (beta.14 task
  // requirement) — the manual button is not the only way to a live listing.
  const u = blank(); t.after(() => u.controller.close()); await u.controller.init();
  assert.ok(u.controller.modelOptions.qc); assert.equal(u.controller.modelOptions.qc.source, "bundled_catalog");
  assert.equal(u.controller.modelOptions.qc.endpoint_kind, "coding_plan");   // fresh glm drafts default to Coding Plan
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3", "glm-5.3-flash"]);
  assert.equal(u.f.posts("/v1/models").length, 0); assert.equal(u.f.posts("/v1/config").length, 0);
  assert.equal(u.el("qc-model-options").children.length, 2);
  assert.match(u.el("qc-model-notice").textContent, /内置官方文档候选（离线快照，不是账号可用清单）/);
  // The async refresh fires on the poll for BOTH glm roles, keeps the seed
  // visible until its answer lands, and merges into one request per identity.
  await u.controller.tick();
  const auto = u.f.posts("/v1/models");
  assert.equal(auto.length, 2);              // qc and zh both seed the glm Coding Plan endpoint
  for (const request of auto) {
    const body = JSON.parse(request.body);
    assert.equal(body.provider, "glm"); assert.equal(body.refresh, undefined);  // legacy body: bridge refreshes live
  }
  assert.ok(u.controller.modelOptions.qc.seeded);
  // A failed refresh keeps the bundled candidates, the hand-typed model and
  // any preview; the failure is named, never presented as success.
  await u.controller.tick();                 // settle the auto operations (mock result invalid → kept)
  const model = "glm-5.3"; u.el("qc-model").value = model; await u.el("qc-model").emit("input");
  u.f.online = false;
  await u.controller.modelAction("qc", "models");
  assert.equal(u.el("qc-model").value, model);
  assert.ok(u.controller.modelOptions.qc); assert.equal(u.controller.modelOptions.qc.seeded, true);
  assert.match(u.el("qc-model-notice").textContent, /失败|无效|保留/);
  assert.equal(u.f.posts("/v1/models").length, 3);   // 2 auto refreshes + 1 failed manual retry
});

test("custom endpoints never receive bundled official candidates", async t => {
  const u = blank(); t.after(() => u.controller.close()); await u.controller.init();
  u.el("qc-base-url").value = "https://proxy.example/v1"; await u.el("qc-base-url").emit("input");
  assert.equal(u.controller.modelOptions.qc, null); assert.equal(u.el("qc-model-options").children.length, 0);
  assert.doesNotMatch(u.el("qc-model-notice").textContent, /内置官方文档候选/);
  assert.equal(Bridge.bundledCatalog({ ...Bridge.profileDefaults("glm"), base_url: "https://proxy.example/v1" }), null);
  assert.equal(Bridge.bundledCatalog({ provider: "zcode", plan: "coding", model: "glm-5.3", reasoning_effort: "max" }), null);
  for (const [provider, kind] of [["glm", "standard"], ["glm", "coding_plan"], ["deepseek", "deepseek"], ["tongji", "tongji"]]) {
    const context = provider === "glm" && kind === "coding_plan" ? Bridge.profileDefaults("glm")
      : { ...Bridge.profileDefaults(provider) };
    const seed = Bridge.bundledCatalog(context);
    assert.ok(seed, provider + "/" + kind);
    assert.equal(seed.source, "bundled_catalog");
    const validated = Bridge.modelChoices(seed, provider);   // the seed passes the public catalog contract
    assert.deepEqual(validated.models, seed.models);
  }
});

test("failed refresh and malformed success responses keep the last trusted list clearly marked", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  for (const failure of ["operation", "response", "transport"]) {
    await fetchList(u, "qc", catalog()); const before = u.controller.readModel("qc");
    if (failure === "transport") u.f.override = r => new URL(r.url).pathname === "/v1/models" ? { status: 500, data: { error: "synthetic unavailable" } } : undefined;
    await u.controller.modelAction("qc", "models");
    if (failure === "operation") { u.f.operationStatus = "failed"; u.f.operationError = "synthetic unavailable"; }
    if (failure === "response") u.f.operationResult = { ...catalog(), ok: false, error: "bad catalog" };
    await u.controller.tick();
    // The previously fetched candidates survive a failed refresh, explicitly
    // marked as kept; they are never presented as a fresh live listing.
    assert.ok(u.controller.modelOptions.qc); assert.equal(u.el("qc-model-options").children.length, 4);
    assert.doesNotMatch(u.el("qc-model-notice").textContent, /本次联网获取的官方文档候选/);
    assert.match(u.el("qc-model-notice").textContent, /失败|无效|保留/);
    assert.doesNotMatch(u.el("model-result").textContent, /synthetic candidate notice/);
    assert.deepEqual(u.controller.readModel("qc"), before);
    u.f.operationStatus = "completed"; u.f.operationError = undefined; u.f.override = null;
  }
});

test("closing during a model-list submission discards late results without another operation or background task", async () => {
  const u = ui(); await u.controller.init(); let release, started;
  const submitted = new Promise(resolve => { started = resolve; });
  u.f.override = r => new URL(r.url).pathname === "/v1/models" ? new Promise(resolve => { release = resolve; started(); }) : undefined;
  const action = u.controller.modelAction("qc", "models"); await submitted;
  const before = u.el("qc-model").value; u.controller.close();
  release({ status: 200, data: { operation: OP } }); await action;
  u.controller.renderModelResult({ kind: "models", phase: "qc", provider: "glm" }, catalog());
  await u.controller.tick(); u.controller.applyModelMaximum("qc");
  assert.equal(u.controller.pending, null); assert.equal(u.controller.modelOptions.qc, null);
  assert.equal(u.el("qc-model").value, before); assert.equal(u.intervals.size, 0);
  assert.equal(u.f.posts("/v1/models").length, 1); assert.equal(u.f.posts("/v1/start").length, 0);
  assert.equal(u.f.posts("/v1/test").length, 0); assert.equal(u.f.posts("/v1/config").length, 0);
});

test("markup version references are generated from manifest.json, never hand-copied", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"));
  const markup = fs.readFileSync(path.join(ROOT, "panel.xhtml"), "utf8");
  const resources = [...markup.matchAll(/chrome:\/\/library-sync\/content\/[^" ]+/g)].map(match => match[0]);
  assert.equal(resources.length, 4); assert.ok(resources.every(url => url.endsWith("?v=" + manifest.version)));
  assert.match(markup, new RegExp('id="panel-version"[^>]*>v' + manifest.version.replace(/[.]/g, "\\.")));
  assert.ok(markup.includes('id="panel-boot"'));   // static boot guidance survives a total script failure
  assert.ok(fs.readFileSync(path.join(ROOT, "runtime.js"), "utf8").includes("panel.xhtml?v=" + manifest.version));
  assert.ok(fs.readFileSync(path.join(ROOT, "bridge-client.js"), "utf8").includes('VERSION = "' + manifest.version + '"'));
  for (const phase of ["qc", "zh"]) {
    assert.equal([...markup.matchAll(new RegExp(`id="${phase}-maximum"`, "g"))].length, 1);
    assert.ok(markup.includes(`id="${phase}-maximum-notice"`));
    assert.ok(markup.includes(`id="${phase}-plan"`));          // explicit ZCode subscription picker
  }
});

test("fresh glm drafts use the Coding Plan while saved generic glm hydrates the historical general endpoint", () => {
  const fresh = Bridge.profileDefaults("glm"), saved = Bridge.hydrationDefaults("glm");
  assert.equal(fresh.base_url, CODING); assert.equal(fresh.model, "");
  assert.equal(saved.base_url, "https://open.bigmodel.cn/api/paas/v4"); assert.equal(saved.model, "");
  assert.equal(Bridge.endpointKind(fresh), "glm_coding_plan"); assert.equal(Bridge.endpointKind(saved), "glm_standard");
  assert.equal(Bridge.publicModelDocs(fresh), true); assert.equal(Bridge.publicModelDocs(saved), true);
  // The old general route remains a first-class official profile, not custom.
  assert.equal(Bridge.profile({ ...saved, model: "glm-5.3" }).base_url, "https://open.bigmodel.cn/api/paas/v4");
  for (const provider of ["deepseek", "tongji"]) assert.deepEqual(Bridge.hydrationDefaults(provider), Bridge.profileDefaults(provider));
});

test("saved profiles hydrate their own endpoints: legacy glm stays general, explicit school endpoints survive", async t => {
  const f = fixture();
  f.keys.tongji = "synthetic-school-secret-only";
  f.publicConfig = { configured: true, credentials: { glm: true, deepseek: false, tongji: true },
    config: { schema: 1,
      qc: { provider: "glm", model: "glm-5.3", reasoning_effort: "default", timeout: 123, max_tokens: 32768 },
      zh: { ...Bridge.profileDefaults("tongji"), model: "DeepSeek-V4.1-Flash" } } };
  const u = ui(f); t.after(() => u.controller.close()); await u.controller.init();
  assert.equal(u.el("qc-base-url").value, "https://open.bigmodel.cn/api/paas/v4");
  assert.equal(u.controller.readModel("qc").base_url, "https://open.bigmodel.cn/api/paas/v4");
  assert.equal(u.el("zh-base-url").value, "https://agi.tongji.edu.cn/v1");
  assert.deepEqual(u.controller.readModel("zh"), { ...Bridge.profileDefaults("tongji"), model: "DeepSeek-V4.1-Flash" });
  await u.controller.reloadConfig();
  assert.equal(u.el("qc-base-url").value, "https://open.bigmodel.cn/api/paas/v4");
  // Rendering and reloading never rewrite saved models or keys.
  assert.equal(u.f.posts("/v1/config").length, 0);
});
