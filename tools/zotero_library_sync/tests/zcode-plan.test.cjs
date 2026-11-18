"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { Bridge, fixture, ui, RUN } = require("./helpers.cjs");

// beta.14 final acceptance: the ZCode login provider has NO callable CLI
// route. The new backend reports both plans truthfully unsupported with
// fixed reason codes (no glm_client routing, no v2 credential copies, no
// private OAuth adapter, no silent rerouting to a paid provider). Saved
// zcode profiles still parse, display and can be migrated away from; an
// old (pre-zcode-plans-v1) bridge FAILS CLOSED too — the beta.13 route
// copied this machine's full ZCode credentials, so no legacy zcode request
// is ever sent; users must upgrade the bridge or migrate to API keys.
const BASE_CAPS = ["keys-v1", "mineru-v1", "translation-policy-v1", "model-choices-v1",
  "credential-reveal-v1", "model-profiles-v2", "model-test-role-v1"];
const PLANS = {
  coding: { supported: false, reason: "login_route_unavailable", provider_id: "bigmodel",
            models: ["glm-5.3", "glm-5.3-flash"] },
  start: { supported: false, reason: "start_route_not_implemented",
           provider_id: "builtin:bigmodel-start-plan", models: [] }
};
function plansUI({ startSupported = false, startModels = [], newBridge = true } = {}) {
  const f = fixture();
  if (newBridge) f.state = { ...f.state, capabilities: [...BASE_CAPS, "zcode-plans-v1"] };
  f.publicConfig.credentials.zcode = true;
  f.publicConfig.zcodePlans = { coding: PLANS.coding,
    start: { ...PLANS.start, supported: startSupported, models: startModels } };
  return ui(f);
}
async function selectZcode(u, phase = "qc") {
  await u.el(phase + "-provider-toggle").emit("click");
  const row = u.el(phase + "-provider-options").children.find(row => row.textContent === "ZCode 登录（本机 CLI 直连，新版停用）");
  assert.ok(row, "provider picker must still offer the (disabled) ZCode login for legacy configs");
  await row.emit("click");
  u.el(phase + "-model").value = "glm-5.3";
  await u.el(phase + "-model").emit("input");
  u.el(phase + "-effort").value = "max";
  await u.el(phase + "-effort").emit("input");
}

test("zcode profiles validate the explicit plan and legacy profiles default to coding", () => {
  const base = { provider: "zcode", model: "glm-5.3", reasoning_effort: "max" };
  assert.equal(Bridge.profile(base).plan, "coding");
  assert.equal(Bridge.profile({ ...base, plan: "coding" }).plan, "coding");
  assert.deepEqual(Bridge.profileDefaults("zcode").plan, "coding");
  // Fresh templates prefer the user default high; drafts stay editable even
  // though the provider cannot run.
  assert.equal(Bridge.profileDefaults("zcode").reasoning_effort, "high");
  // No recommendation is invented for a provider with no callable route.
  assert.equal(Bridge.modelRecommended({ ...Bridge.profileDefaults("zcode"), model: "glm-5.3" }), null);
  assert.throws(() => Bridge.profile({ ...base, plan: "plus" }), { code: "INPUT" });
  assert.throws(() => Bridge.profile({ ...Bridge.profileDefaults("glm"), model: "glm-5.3", plan: "coding" }), { code: "INPUT" });
  for (const effort of ["default", "low", "high", "max"]) {
    assert.equal(Bridge.profile({ ...base, reasoning_effort: effort }).reasoning_effort, effort);
  }
  assert.throws(() => Bridge.profile({ ...base, reasoning_effort: "xhigh" }), { code: "INPUT" });
  // The start plan shape keeps parsing (legacy display); the backend gates runs.
  assert.equal(Bridge.profile({ ...base, plan: "start", model: "glm-5.3-flash" }).plan, "start");
});

test("selecting zcode shows the plan picker and the honest unavailable state, sending nothing", async t => {
  const u = plansUI(); t.after(() => u.controller.close()); await u.controller.init();
  await selectZcode(u);
  assert.equal(u.controller.readModel("qc").provider, "zcode");
  assert.equal(u.controller.readModel("qc").plan, "coding");
  assert.equal(u.el("qc-plan-row").hidden, false);
  assert.equal(u.el("qc-plan").value, "coding");
  const notice = u.el("qc-provider-notice").textContent + u.el("qc-endpoint-notice").textContent;
  assert.match(notice, /已停用|不可用/);
  assert.match(notice, /不复制本机 ZCode 凭据/);
  assert.doesNotMatch(notice, /无需 Key|自动路由|无头 ZCode 进程，速度/);
  assert.equal(u.el("qc-test").disabled, true);
  assert.equal(u.el("qc-models").disabled, true);
  assert.match(u.el("qc-test-reason").textContent, /ZCode 登录/);
  assert.equal(u.el("save-config").disabled, true);
  await u.controller.modelAction("qc", "test");
  await u.controller.modelAction("qc", "models");
  await u.controller.saveConfig();
  for (const route of ["/v1/test", "/v1/models", "/v1/config"]) assert.equal(u.f.posts(route).length, 0);
  // Draft fields stay editable for migration; nothing was overwritten.
  assert.equal(u.el("qc-model").value, "glm-5.3");
  assert.equal(u.el("qc-effort").value, "max");
});

test("the Start plan states its own missing route and never borrows coding availability", async t => {
  const u = plansUI(); t.after(() => u.controller.close()); await u.controller.init();
  await selectZcode(u);
  await u.el("qc-plan-toggle").emit("click");
  await u.el("qc-plan-options").children.find(row => row.textContent.includes("Start Plan")).emit("click");
  assert.equal(u.controller.readModel("qc").plan, "start");
  assert.equal(u.el("qc-plan").value, "start");
  assert.match(u.el("qc-provider-notice").textContent, /Start Plan/);
  assert.match(u.el("qc-provider-notice").textContent, /没有可调用的 Start Plan 路由/);
  assert.doesNotMatch(u.el("qc-provider-notice").textContent, /自动路由/);
  assert.equal(u.el("qc-test").disabled, true);
  assert.equal(u.el("qc-models").disabled, true);
  assert.equal(u.el("save-config").disabled, true);
  await u.controller.modelAction("qc", "test");
  await u.controller.catalogAction("qc", true);
  for (const route of ["/v1/test", "/v1/models", "/v1/config"]) assert.equal(u.f.posts(route).length, 0);
  // Even bridge metadata claiming a supported Start must not invent broader
  // entitlements: the panel lists exactly the reported models, nothing else.
  const tolerant = plansUI({ startSupported: true, startModels: ["glm-5.3-flash"] });
  t.after(() => tolerant.controller.close()); await tolerant.controller.init();
  await selectZcode(tolerant);
  await tolerant.el("qc-plan-toggle").emit("click");
  await tolerant.el("qc-plan-options").children.find(row => row.textContent.includes("Start Plan")).emit("click");
  assert.equal(tolerant.el("qc-test").disabled, false);
  assert.match(tolerant.el("qc-provider-notice").textContent, /glm-5\.3-flash/);
  assert.match(tolerant.el("qc-provider-notice").textContent, /不代表其它订阅权限/);
});

test("an old bridge fails closed for zcode: no legacy request is ever sent, with an upgrade/migrate path", async t => {
  const u = plansUI({ newBridge: false }); t.after(() => u.controller.close()); await u.controller.init();
  await selectZcode(u);
  assert.equal(u.el("qc-plan-row").hidden, true);   // plan picker hidden without zcode-plans-v1
  // beta.14 decision: the beta.13 zcode route copied this machine's full
  // credentials, so test/catalog/save disable with an upgrade-or-migrate
  // reason instead of executing legacy compatibility.
  assert.equal(u.el("qc-test").disabled, true);
  assert.equal(u.el("qc-models").disabled, true);
  assert.equal(u.el("save-config").disabled, true);
  assert.match(u.el("qc-test-reason").textContent, /旧版（beta\.13 及更早）.*复制本机完整凭据/);
  assert.match(u.el("qc-test-reason").textContent, /更新本机 bridge.*改选 API Key 服务商/);
  assert.match(u.el("qc-provider-notice").textContent, /不再发送旧版请求/);
  await u.controller.modelAction("qc", "test");
  await u.controller.modelAction("qc", "models");
  await u.controller.saveConfig();
  for (const route of ["/v1/test", "/v1/models", "/v1/config"]) assert.equal(u.f.posts(route).length, 0);
  // Migration to an API-key provider re-enables everything and saves without
  // the plan field the old backend would reject.
  await u.el("qc-provider-toggle").emit("click");
  await u.el("qc-provider-options").children.find(row => row.textContent === "GLM（默认 Coding Plan）").emit("click");
  u.el("qc-model").value = "glm-5.3"; await u.el("qc-model").emit("input");
  assert.equal(u.el("qc-test").disabled, false);
  assert.equal(u.el("save-config").disabled, false);
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/config").length, 1);
  const saved = JSON.parse(u.f.posts("/v1/config")[0].body).config;
  assert.equal(saved.qc.provider, "glm"); assert.equal("plan" in saved.qc, false);
});

test("a saved legacy zcode config still loads and displays with a migration path to API keys", async t => {
  const u = plansUI(); t.after(() => u.controller.close());
  u.f.publicConfig = { ...u.f.publicConfig, configured: true,
    config: { schema: 1,
      qc: { provider: "zcode", model: "glm-5.3", reasoning_effort: "max", timeout: 1500, max_tokens: 128000 },
      zh: { ...Bridge.profileDefaults("deepseek"), model: "deepseek-flash" } },
    credentials: { ...u.f.publicConfig.credentials, zcode: true } };
  await u.controller.init();
  // GET /v1/config did not fail: the legacy profile hydrated with its plan.
  assert.equal(u.controller.readModel("qc").provider, "zcode");
  assert.equal(u.controller.readModel("qc").plan, "coding");
  assert.equal(u.el("qc-model").value, "glm-5.3");
  assert.match(u.el("qc-provider-notice").textContent, /不可用/);
  assert.equal(u.el("qc-test").disabled, true);
  assert.equal(u.el("save-config").disabled, true);   // saving must first migrate away from zcode
  // Switching the role to an API-key provider is the actionable path.
  await u.el("qc-provider-toggle").emit("click");
  await u.el("qc-provider-options").children.find(row => row.textContent === "GLM（默认 Coding Plan）").emit("click");
  u.el("qc-model").value = "glm-5.3"; await u.el("qc-model").emit("input");
  assert.equal(u.el("save-config").disabled, false);
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/config").length, 1);
  const saved = JSON.parse(u.f.posts("/v1/config")[0].body).config;
  assert.equal(saved.qc.provider, "glm"); assert.equal(saved.qc.plan, undefined);
  assert.equal(saved.qc.model, "glm-5.3");
  assert.equal(u.controller.readModel("qc").plan, undefined);  // plan exists only on zcode drafts
});

// beta.14 final acceptance regression: a SAVED zcode role with a saved login
// flag must fail closed at readiness/preflight/confirm — the flag is a
// display fact, not an executable route. The check reads the saved config
// (not the unsaved dropdown) and honors translate=false: an unused zh zcode
// route never blocks the QC-only path.
function savedZcodeUI({ newBridge = true } = {}) {
  const u = plansUI({ newBridge });
  u.f.publicConfig = { ...u.f.publicConfig, configured: true,
    config: { schema: 1,
      qc: { provider: "zcode", model: "glm-5.3", reasoning_effort: "max", timeout: 1500, max_tokens: 128000 },
      zh: { ...Bridge.profileDefaults("deepseek"), model: "deepseek-flash" } },
    credentials: { ...u.f.publicConfig.credentials, zcode: true } };
  return u;
}

test("saved required zcode roles fail closed: preview and start never POST on the new bridge", async t => {
  const u = savedZcodeUI(); t.after(() => u.controller.close());
  await u.controller.init(true);
  assert.equal(u.controller.configurationReady(), false);
  assert.equal(u.el("preview").disabled, true);
  assert.match(u.el("preview-reason").textContent, /已保存的QC角色使用 ZCode 登录/);
  assert.match(u.el("preview-reason").textContent, /不可用/);
  assert.match(u.el("preview-reason").textContent, /改选 API Key 服务商/);
  await u.controller.previewScope(u.controller.keys);
  assert.equal(u.f.posts("/v1/preview").length, 0);
  // Even a stale preview bound to that config cannot confirm a start.
  u.controller.preview = { preview_id: RUN, items: structuredClone(u.f.preview.items), deferred: [] };
  await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 0);
});

test("saved required zcode roles fail closed on the OLD bridge: the retired beta.13 login route is never reached", async t => {
  const u = savedZcodeUI({ newBridge: false }); t.after(() => u.controller.close());
  await u.controller.init(true);
  assert.equal(u.controller.configurationReady(), false);
  assert.equal(u.el("preview").disabled, true);
  assert.match(u.el("preview-reason").textContent, /已保存的QC角色使用 ZCode 登录，本机 bridge 是旧版/);
  assert.match(u.el("preview-reason").textContent, /更新本机 bridge/);
  await u.controller.previewScope(u.controller.keys);
  assert.equal(u.f.posts("/v1/preview").length, 0);
  u.controller.preview = { preview_id: RUN, items: structuredClone(u.f.preview.items), deferred: [] };
  await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 0);
});

test("an unused zh zcode route with translate=false does not block the QC API path", async t => {
  const u = plansUI(); t.after(() => u.controller.close());
  u.f.publicConfig = { ...u.f.publicConfig, configured: true,
    config: { schema: 1,
      qc: u.f.publicConfig.config.qc,
      zh: { provider: "zcode", model: "glm-5.3", reasoning_effort: "max", timeout: 1500, max_tokens: 128000 } },
    credentials: { ...u.f.publicConfig.credentials, zcode: true } };
  await u.controller.init(true);
  // Translation on (the default): zh is required and its zcode route blocks.
  assert.equal(u.controller.configurationReady(), false);
  u.el("translate").checked = false;
  await u.el("translate").emit("change");
  assert.equal(u.controller.configurationReady(), true);
  assert.equal(u.el("preview").disabled, false);
  await u.controller.previewScope(u.controller.keys);
  await u.controller.tick();
  assert.equal(u.f.posts("/v1/preview").length, 1);
  assert.equal(JSON.parse(u.f.posts("/v1/preview")[0].body).translate, false);
  // Flipping translation back on re-blocks the same saved config.
  u.el("translate").checked = true;
  await u.el("translate").emit("change");
  assert.equal(u.controller.configurationReady(), false);
  assert.equal(u.el("preview").disabled, true);
});
