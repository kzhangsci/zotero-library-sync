"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { Bridge, fixture, ui } = require("./helpers.cjs");

const CAPS = ["keys-v1", "mineru-v1", "translation-policy-v1", "model-choices-v1", "credential-reveal-v1", "model-profiles-v2", "model-test-role-v1", "tongji-api-v1"];
const schoolProfile = () => ({ ...Bridge.profileDefaults("tongji"), model: "DeepSeek-V4.1-Flash" });
function schoolUI() {
  const f = fixture(); f.state.capabilities = CAPS;
  f.publicConfig.credentials.tongji = true; f.keys.tongji = "synthetic-school-secret-only";
  return ui(f);
}
async function selectSchool(u, phase = "qc") {
  u.el(phase + "-provider").value = "tongji";
  await u.el(phase + "-provider").emit("change");
  u.el(phase + "-model").value = "DeepSeek-V4.1-Flash";
  await u.el(phase + "-model").emit("input");
}

test("school profile keeps exact campus IDs and independent conservative request defaults", () => {
  const p = schoolProfile(); assert.deepEqual(Bridge.profile(p), p);
  assert.equal(p.base_url, "https://agi.tongji.edu.cn/v1");
  assert.equal(p.models_url, "https://agi.tongji.edu.cn/v1/models");
  assert.equal(p.model, "DeepSeek-V4.1-Flash"); assert.equal(p.max_tokens, 32768);
  assert.equal(p.reasoning_effort, "default"); assert.equal(p.thinking, "default");
  assert.equal(Bridge.endpointKind(p), "tongji"); assert.equal(Bridge.catalogEndpointKind(p), "tongji");
  assert.equal(Bridge.publicModelDocs(p), false); assert.equal(Bridge.modelMaximum(p), null);
  const help = Bridge.modelParameterHelp(p);
  assert.deepEqual(help.effortLevels, ["none", "low", "high", "xhigh", "max"]);
  assert.equal(help.defaultEffort, "high"); assert.match(help.tokens, /不是.*最大输出/);
  assert.match(help.endpointHelp, /非对话模型/);
  assert.deepEqual(Bridge.modelParameterHelp({ ...p, model: "MiMo-V2.6-Flash" }).effortLevels, ["none", "high"]);
  for (const model of ["deepseek-flash", "glm-5.3", "DeepSeek-V4.1-Flash-Unknown"]) {
    assert.deepEqual(Bridge.modelParameterHelp({ ...p, model }).effortLevels, []);
    assert.equal(Bridge.modelMaximum({ ...p, model }), null);
  }
});

test("school thinking is controlled only by effort and cannot be smuggled through extra JSON", () => {
  for (const thinking of ["enabled", "disabled"]) assert.throws(() => Bridge.profile({ ...schoolProfile(), thinking }), { code: "INPUT" });
  for (const extra_body of [{ enable_thinking: true }, { chat_template_kwargs: {} }, { nested: [{ "Enable.Thinking": false }] }])
    assert.throws(() => Bridge.profile({ ...schoolProfile(), extra_body }), { code: "INPUT" });
  assert.equal(Bridge.profile({ ...schoolProfile(), reasoning_effort: "none" }).reasoning_effort, "none");
  assert.deepEqual(Bridge.profile({ ...schoolProfile(), extra_body: { temperature: 0.2 } }).extra_body, { temperature: 0.2 });
  assert.equal(Bridge.profile({ ...schoolProfile(), max_tokens: 1048576 }).max_tokens, 1048576);
  assert.throws(() => Bridge.profile({ ...schoolProfile(), max_tokens: 1048577 }), { code: "INPUT" });
  // The school-specific rejection must not change older custom-provider contracts.
  assert.deepEqual(Bridge.profile({ ...Bridge.profileDefaults("deepseek"), model: "custom", extra_body: { enable_thinking: true } }).extra_body, { enable_thinking: true });
});

test("opening saved GLM and DeepSeek settings only reads school keys and preserves both roles", async () => {
  const u = schoolUI(), before = structuredClone(u.f.publicConfig.config);
  await u.controller.init();
  assert.deepEqual(u.controller.savedConfig, before);
  assert.equal(u.el("tongji-key").value, u.f.keys.tongji); assert.equal(u.el("tongji-key").type, "password");
  assert.match(u.el("credentials").textContent, /同济：已保存/);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/models").length, 0);
  assert.equal(u.f.posts("/v1/test").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  assert.doesNotMatch(u.f.client.redact("error " + u.f.keys.tongji), /synthetic-school-secret-only/);
  u.controller.close(); assert.equal(u.el("tongji-key").value, ""); assert.equal(u.f.client.revealedSensitive.size, 0);
});

test("selecting school migrates untouched provider templates, disables thinking and leaves the other role intact", async () => {
  const u = schoolUI(); await u.controller.init();
  u.controller.applyProviderDefaults("qc");
  const zh = u.controller.readModel("zh"), saved = structuredClone(u.controller.savedConfig);
  await selectSchool(u);
  assert.deepEqual(u.controller.readModel("qc"), schoolProfile());
  assert.equal(u.el("qc-thinking").disabled, true); assert.equal(u.el("qc-thinking-toggle").disabled, true);
  assert.deepEqual(u.controller.readModel("zh"), zh); assert.deepEqual(u.controller.savedConfig, saved);
  assert.equal(u.el("qc-maximum").disabled, true);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
  u.controller.close();
});

test("school key-only save survives broken model draft and writes only the changed school key", async () => {
  const u = schoolUI(); await u.controller.init();
  const before = structuredClone(u.f.publicConfig.config), old = [u.f.keys.glm, u.f.keys.deepseek];
  u.el("qc-extra-body").value = "{broken"; await u.el("qc-extra-body").emit("input");
  u.el("tongji-key").value = "synthetic-replaced-school-only"; await u.el("tongji-key").emit("input");
  await u.el("save-keys").emit("click");
  assert.deepEqual(JSON.parse(u.f.posts("/v1/keys")[0].body), { keys: { tongji: "synthetic-replaced-school-only" } });
  assert.deepEqual(u.f.publicConfig.config, before); assert.deepEqual([u.f.keys.glm, u.f.keys.deepseek], old);
  assert.equal(u.el("qc-extra-body").value, "{broken"); assert.equal(u.f.posts("/v1/config").length, 0);
  u.el("tongji-key").value = ""; await u.el("tongji-key").emit("input"); await u.controller.saveConfig(true);
  assert.equal(u.f.posts("/v1/keys").length, 1); assert.match(u.el("message").textContent, /不能保存为空/);
  u.controller.close();
});

test("saved school role is preserved on reload and its explicit test retains the role and ID", async () => {
  const u = schoolUI(); u.f.publicConfig.config.qc = schoolProfile();
  await u.controller.init(); await u.controller.reloadConfig();
  assert.deepEqual(u.controller.readModel("qc"), schoolProfile());
  await u.el("qc-test").emit("click");
  const request = JSON.parse(u.f.posts("/v1/test")[0].body);
  assert.equal(request.provider, "tongji"); assert.equal(request.model, "DeepSeek-V4.1-Flash");
  assert.equal(request.role, "qc"); assert.equal(request.reasoning_effort, "default");
  assert.equal(u.window.confirms.length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  u.controller.close();
});

test("school listing retains mixed model IDs but offers no invented maxima or automatic selection", async () => {
  const u = schoolUI(); await u.controller.init(); await selectSchool(u);
  await u.el("qc-models").emit("click");
  const req = JSON.parse(u.f.posts("/v1/models")[0].body); assert.equal(req.provider, "tongji");
  u.f.operationResult = { provider: "tongji", source: "provider_api", models: ["DeepSeek-V4.1-Flash", "Qwen3.5-Embedding"],
    details: {}, notice: "列表可能含非对话模型", reference: schoolProfile().models_url, endpoint_kind: "tongji" };
  await u.controller.tick();
  assert.deepEqual(u.controller.modelOptions.qc.models, ["DeepSeek-V4.1-Flash", "Qwen3.5-Embedding"]);
  assert.equal(u.el("qc-model").value, "DeepSeek-V4.1-Flash"); assert.equal(u.el("qc-maximum").disabled, true);
  assert.match(u.el("model-result").textContent, /非对话模型/); assert.equal(u.f.posts("/v1/test").length, 0);
  u.controller.close();
});

test("old bridge remains compatible for existing providers and blocks new school writes before sending", async () => {
  const u = ui(); await u.controller.init(); assert.equal(u.controller.connected, true);
  assert.equal(u.controller.credentials.tongji, false);
  await selectSchool(u); u.el("tongji-key").value = "synthetic-not-sent-school"; await u.el("tongji-key").emit("input");
  assert.equal(u.el("save-config").disabled, true); assert.equal(u.el("save-keys").disabled, true);
  await u.controller.saveConfig(true); await u.controller.modelAction("qc", "models");
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/keys").length, 0);
  assert.equal(u.f.posts("/v1/models").length, 0); assert.match(u.el("message").textContent, /不支持同济/);
  u.controller.close();
});

test("school endpoint mismatch remains explicit and failed tests cannot fall back to another provider", async () => {
  const u = schoolUI(); await u.controller.init(); await selectSchool(u);
  u.el("qc-base-url").value = "https://llmapi.tongji.edu.cn/v1"; await u.el("qc-base-url").emit("input");
  u.f.override = r => new URL(r.url).pathname === "/v1/test" ? { status: 500, data: { error: "synthetic school unavailable" } } : undefined;
  await u.controller.modelAction("qc", "test");
  assert.equal(u.window.confirms.length, 1); assert.match(u.window.confirms[0], /llmapi\.tongji/);
  assert.equal(u.f.posts("/v1/test").length, 1); assert.equal(JSON.parse(u.f.posts("/v1/test")[0].body).provider, "tongji");
  assert.equal(u.el("qc-provider").value, "tongji"); assert.equal(u.controller.pending, null);
  u.controller.close();
});

test("school field help follows each role and restores generic text when changing providers", async () => {
  const u = schoolUI(); await u.controller.init();
  const original = Object.fromEntries(["thinking-help", "effort-help", "max-tokens-help"].map(k => [k, u.el("qc-" + k).textContent]));
  const zh = u.el("zh-thinking-help").textContent;
  await selectSchool(u);
  assert.match(u.el("qc-thinking-help").textContent, /固定为 default.*reasoning_effort/);
  assert.doesNotMatch(u.el("qc-thinking-help").textContent, /enabled|disabled/);
  assert.match(u.el("qc-effort-help").textContent, /统一控制思考.*none 关闭/);
  assert.doesNotMatch(u.el("qc-effort-help").textContent, /分别设置/);
  assert.match(u.el("qc-max-tokens-help").textContent, /32768.*请求初值.*不代表/);
  assert.equal(u.el("zh-thinking-help").textContent, zh);
  u.el("qc-provider").value = "glm"; await u.el("qc-provider").emit("change");
  for (const [key, text] of Object.entries(original)) assert.equal(u.el("qc-" + key).textContent, text);
  u.controller.close();
});

test("losing school capability gates saved required roles and key reveal before any preview or start", async () => {
  const u = schoolUI(); u.f.publicConfig.config.qc = schoolProfile(); await u.controller.init();
  u.controller.keys = ["ABCDEFG2"]; u.controller.renderPreview(u.f.preview);
  u.f.state.capabilities = CAPS.filter(x => x !== "tongji-api-v1"); u.controller.renderStatus(u.f.state);
  assert.equal(u.el("preview").disabled, true); assert.equal(u.el("discover").disabled, true); assert.equal(u.el("start").disabled, true);
  assert.throws(() => u.controller.requireReady(), { code: "CAPABILITY" });
  const reveals = u.f.posts("/v1/credentials/reveal").length;
  await u.controller.loadKeys(["tongji"], true); assert.equal(u.f.posts("/v1/credentials/reveal").length, reveals);
  await u.controller.previewScope(["ABCDEFG2"]); await u.controller.start();
  assert.equal(u.f.posts("/v1/preview").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  assert.equal(u.window.confirms.length, 0); u.controller.close();
});

test("school requirement follows enabled workflow roles and is rechecked after refreshing start status", async () => {
  const u = schoolUI(); u.f.publicConfig.config.zh = schoolProfile(); await u.controller.init();
  u.el("translate").checked = false; await u.el("translate").emit("change");
  u.f.state.capabilities = CAPS.filter(x => x !== "tongji-api-v1"); u.controller.renderStatus(u.f.state);
  assert.equal(u.controller.requiredTongjiReason(), ""); assert.doesNotThrow(() => u.controller.requireReady());
  u.el("translate").checked = true; await u.el("translate").emit("change");
  assert.throws(() => u.controller.requireReady(), { code: "CAPABILITY" });
  // Capabilities can also disappear in the status refresh performed by Start.
  u.controller.capabilities = CAPS; u.controller.renderPreview(u.f.preview);
  await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 0); assert.equal(u.window.confirms.length, 0);
  assert.match(u.el("message").textContent, /不支持.*同济/); u.controller.close();
});
