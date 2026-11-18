"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { Bridge, fixture, ui, ROOT } = require("./helpers.cjs");

function blankUI() {
  const f = fixture();
  f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  return ui(f);
}
function selectPreset(u, phase, id) {
  u.el(phase + "-habit").value = Bridge.habitPresets().find(p => p.id === id).label;
}

test("reference presets retain their model choices without claiming the user's current client configuration", () => {
  assert.equal(Bridge.habitPreset("zcode-main").model, "glm-5.2");
  assert.equal(Bridge.habitPreset("zcode-main").reasoning_effort, "max");
  for (const [id, model] of [["zcode-main", "glm-5.2"], ["zcode-glm53", "glm-5.3"], ["dsh", "deepseek-flash"]]) {
    const p = Bridge.habitPreset(id);
    assert.equal(p.model, model); assert.equal(p.reasoning_effort, "max");
    assert.equal(p.thinking, "enabled"); assert.equal(p.max_tokens, p.provider === "glm" ? 131072 : 393216);
    assert.equal(p.timeout, 1500); assert.equal(p.protocol, "openai_chat_completions");
    assert.deepEqual(Object.keys(p).sort(), Object.keys(Bridge.profileDefaults(p.provider)).sort());
    assert.deepEqual(Bridge.profile(p), p); assert.ok(!p.base_url.includes("anthropic"));
  }
  const changed = Bridge.habitPreset("dsh"); changed.extra_body.temperature = 0.2;
  assert.deepEqual(Bridge.habitPreset("dsh").extra_body, {});
  assert.throws(() => Bridge.habitPreset("../../client-credentials"), { code: "INPUT" });
  for (const preset of Bridge.habitPresets()) {
    assert.match(preset.label, /内置预设/);
    assert.match(preset.note, /插件内置模板/);
    assert.match(preset.note, /不读取、不同步/);
    assert.match(preset.note, /不代表客户端当前配置.*按自身情况确认/);
    assert.doesNotMatch(preset.label + preset.note, /主模型|配置快照|实时导入|账号迁移|已读取当前/);
  }
});

test("public preset guidance describes explicit draft replacement and contains no local configuration claims", () => {
  const markup = fs.readFileSync(path.join(ROOT, "panel.xhtml"), "utf8");
  for (const phase of ["qc", "zh"]) {
    assert.ok(markup.includes(`id="${phase}-apply-habit" type="button">填入预设（保存后生效）`));
  }
  assert.match(markup, /打开或刷新不会自动套用预设/);
  assert.match(markup, /不代表客户端当前配置/);
  assert.doesNotMatch(markup, /仅空白配置|本机非敏感配置快照|本次核对的 ZCode|ZCode 主模型/);
  const source = fs.readFileSync(path.join(ROOT, "bridge-client.js"), "utf8");
  // Habit presets must never claim live client configuration. The zcode login
  // provider is different — but since beta.14 its help text must state the
  // honest unavailability, never a "no-key login that just works" claim.
  assert.doesNotMatch(source, /本机 dsh|dsh 已显式选择|manually checked snapshot/);
  const presetScope = source.slice(source.indexOf("function habitPresets"), source.indexOf("function habitPreset"));
  assert.doesNotMatch(presetScope, /本机 ZCode/);
  // beta.14 honest-routing language replaces both the old auto-route claim and
  // the stopped pass's "explicit bigmodel CLI route" claim.
  assert.match(source, /ZCode 登录 · Coding Plan：beta\.14 起新版后台不再经本机 CLI 直连执行/);
  assert.match(source, /不复制本机 ZCode 凭据/);
});

test("zcode login provider: no key, no endpoint fields, fixed model set and effort levels", () => {
  const base = { provider: "zcode", model: "glm-5.3-flash", reasoning_effort: "max", timeout: 1500, max_tokens: 128000 };
  assert.deepEqual(Bridge.profile(base), { ...base, plan: "coding" });
  assert.equal(Bridge.tokenInputLimit("zcode"), 128000);
  assert.equal(Bridge.endpointKind(base), "zcode");
  assert.equal(Bridge.catalogEndpointKind(base), "zcode");
  // Endpoint/protocol/extra fields are rejected instead of silently ignored.
  assert.throws(() => Bridge.profile({ ...base, base_url: "https://x.example" }), { code: "INPUT" });
  assert.throws(() => Bridge.profile({ ...base, thinking: "enabled" }), { code: "INPUT" });
  assert.throws(() => Bridge.profile({ ...base, extra_body: {} }), { code: "INPUT" });
  assert.throws(() => Bridge.profile({ ...base, reasoning_effort: "xhigh" }), { code: "INPUT" });
  assert.throws(() => Bridge.profile({ ...base, model: "glm-4.6" }), { code: "INPUT" });
  // The CLI's documented levels all validate; fresh templates prefer high.
  for (const effort of ["default", "low", "high", "max"]) {
    assert.equal(Bridge.profile({ ...base, reasoning_effort: effort }).reasoning_effort, effort);
  }
  assert.equal(Bridge.profileDefaults("zcode").reasoning_effort, "high");
  // The bundled listing passes validation and keeps its provenance marks.
  const zcodeChoices = Bridge.modelChoices(fixtureChoicesZcode(), "zcode");
  assert.equal(zcodeChoices.source, "bundled_catalog");
  assert.equal(zcodeChoices.endpoint_kind, "zcode");
  // Defaults hydrate without endpoint URLs; empty model passes for listings.
  const listing = Bridge.profile({ ...base, model: "" }, true);
  assert.equal(listing.model, ""); assert.ok(!("base_url" in listing));
  const help = Bridge.modelParameterHelp(base, null);
  assert.deepEqual(help.effortLevels, ["default", "low", "high", "max"]);
  // Honest unavailability: no callable CLI route, no "no-key login" marketing,
  // and no invented parameter recommendation for a provider that cannot run.
  assert.match(help.endpointHelp, /beta\.14 起新版后台不再经本机 CLI 直连执行/);
  assert.match(help.endpointHelp, /不复制本机 ZCode 凭据/);
  assert.match(help.endpointHelp, /旧版后台（beta\.13 及更早）的该调用会复制本机完整凭据，插件同样不再发送/);
  assert.equal(help.maximum, null);
  assert.equal(help.recommended, null);
});

function fixtureChoicesZcode() {
  return { provider: "zcode", models: ["glm-5.3", "glm-5.3-flash"], source: "bundled_catalog",
    notice: "复用本机 ZCode 的 GLM 登录。", fetched_at: null, endpoint_kind: "zcode" };
}

test("explicit preset fills one blank role only; it never saves, reads another client, tests or starts", async () => {
  const u = blankUI(); await u.controller.init();
  assert.equal(u.el("qc-model").value, ""); assert.equal(u.el("zh-model").value, "");
  const before = { requests: u.f.requests.length, reads: u.f.reads.length, saved: structuredClone(u.controller.savedConfig),
    zh: u.controller.readModel("zh"), keys: [u.el("glm-key").value, u.el("deepseek-key").value] };
  selectPreset(u, "qc", "zcode-glm53");
  assert.equal(u.el("qc-model").value, ""); // Choosing is not applying.
  await u.el("qc-apply-habit").emit("click");
  assert.equal(u.el("qc-model").value, "glm-5.3"); assert.equal(u.el("qc-effort").value, "max");
  assert.equal(u.el("qc-base-url").value, "https://open.bigmodel.cn/api/paas/v4");
  assert.equal(u.controller.configDirty, true); assert.equal(u.el("qc-apply-habit").disabled, false);
  assert.deepEqual(u.controller.readModel("zh"), before.zh);
  assert.deepEqual(u.controller.savedConfig, before.saved);
  assert.deepEqual([u.el("glm-key").value, u.el("deepseek-key").value], before.keys);
  assert.equal(u.f.requests.length, before.requests); assert.equal(u.f.reads.length, before.reads);
  assert.equal(u.f.launches.length, 0); assert.match(u.el("qc-test-state").textContent, /尚未保存或测试/);
  assert.match(u.el("qc-habit-note").textContent, /请核对后保存/);
  assert.match(u.el("message").textContent, /QC.*草稿尚未保存/);
  selectPreset(u, "zh", "dsh"); await u.el("zh-apply-habit").emit("click");
  assert.equal(u.el("zh-provider").value, "deepseek"); assert.equal(u.el("zh-model").value, "deepseek-flash");
  assert.equal(u.el("zh-base-url").value, "https://api.deepseek.com");
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  u.controller.close();
});

test("an explicit click can replace an existing role draft without changing the saved profile or the other role", async () => {
  const f = fixture();
  const actual = { ...Bridge.profileDefaults("glm"), model: "glm-5.3", reasoning_effort: "max", thinking: "enabled", max_tokens: 100000 };
  f.publicConfig.config = { schema: 1, qc: { ...actual }, zh: { ...actual } };
  const u = ui(f); await u.controller.init();
  const saved = structuredClone(u.controller.savedConfig), backend = structuredClone(f.publicConfig);
  const keys = [u.el("glm-key").value, u.el("deepseek-key").value];
  await u.controller.previewScope(["ABCDEFG2"]); await u.controller.tick();
  assert.ok(u.controller.preview);
  for (const phase of ["qc", "zh"]) {
    const other = phase === "qc" ? "zh" : "qc", otherDraft = u.controller.readModel(other);
    for (const p of Bridge.habitPresets()) {
      const previous = u.controller.readModel(phase), requests = f.requests.length, reads = f.reads.length;
      await u.el(phase + "-habit-toggle").emit("click");
      await u.el(phase + "-habit-options").children.find(row => row.textContent === p.label).emit("click");
      assert.equal(u.el(phase + "-habit").value, p.label);
      assert.deepEqual(u.controller.readModel(phase), previous); // Selecting alone never fills.
      assert.equal(u.el(phase + "-apply-habit").disabled, false);
      await u.el(phase + "-apply-habit").emit("click");
      assert.deepEqual(u.controller.readModel(phase), Bridge.habitPreset(p.id));
      assert.deepEqual(u.controller.readModel(other), otherDraft);
      assert.deepEqual(u.controller.savedConfig, saved); assert.deepEqual(f.publicConfig, backend);
      assert.deepEqual([u.el("glm-key").value, u.el("deepseek-key").value], keys);
      assert.equal(f.requests.length, requests); assert.equal(f.reads.length, reads);
      assert.equal(u.controller.configDirty, true); assert.ok(u.controller.preview);
      assert.equal(u.controller.modelDirty, true);
      assert.equal(u.el("start").disabled, true);
      assert.match(u.el("start-reason").textContent, /存在未保存修改|保存配置/);
      assert.match(u.el("message").textContent, new RegExp(phase.toUpperCase() + ".*草稿尚未保存"));
    }
  }
  // Loading saved settings restores them even when the preset picker still says dsh.
  await u.controller.reloadConfig();
  for (const phase of ["qc", "zh"]) {
    assert.deepEqual(u.controller.readModel(phase), actual);
    assert.equal(u.el(phase + "-habit").value, Bridge.habitPresets().find(p => p.id === "dsh").label);
    assert.doesNotMatch(u.el(phase + "-habit-note").textContent, /已覆盖|尚未保存/);
  }
  assert.equal(u.controller.configDirty, false);
  await u.controller.connect(false);
  await u.controller.tick();
  for (const phase of ["qc", "zh"]) assert.deepEqual(u.controller.readModel(phase), actual);
  assert.deepEqual(u.controller.savedConfig, saved); assert.deepEqual(f.publicConfig, backend);
  assert.equal(f.posts("/v1/config").length, 0); assert.equal(f.posts("/v1/start").length, 0);
  assert.equal(f.posts("/v1/test").length, 0); assert.equal(f.launches.length, 0); u.controller.close();
  const reopenedFixture = fixture(); reopenedFixture.publicConfig = structuredClone(f.publicConfig);
  const reopened = ui(reopenedFixture); await reopened.controller.init();
  for (const phase of ["qc", "zh"]) assert.deepEqual(reopened.controller.readModel(phase), actual);
  reopened.controller.close();
});

test("explicit presets can replace custom or malformed drafts without reading or writing saved configuration", async () => {
  const u = ui(); await u.controller.init();
  const saved = structuredClone(u.controller.savedConfig), other = u.controller.readModel("zh");
  u.el("qc-model").value = "custom-model"; u.el("qc-extra-body").value = "{broken";
  u.el("qc-base-url").value = "https://example.org/v1"; u.el("qc-timeout").value = "123";
  u.controller.endpointConfirmed.qc = true;
  await u.el("qc-extra-body").emit("input");
  assert.equal(u.el("qc-apply-habit").disabled, false);
  const requests = u.f.requests.length;
  selectPreset(u, "qc", "dsh"); await u.el("qc-apply-habit").emit("click");
  assert.deepEqual(u.controller.readModel("qc"), Bridge.habitPreset("dsh"));
  assert.deepEqual(u.controller.readModel("zh"), other); assert.deepEqual(u.controller.savedConfig, saved);
  assert.equal(u.controller.endpointConfirmed.qc, false); assert.equal(u.f.requests.length, requests);
  assert.match(u.el("qc-habit-note").textContent, /已覆盖本角色草稿，尚未保存/);
  u.controller.close();
});

test("presets retain verified connection and busy-operation guards and ignore calls after close", async () => {
  const u = blankUI(); await u.controller.init();
  selectPreset(u, "qc", "dsh");
  for (const state of [{ connected: false }, { credentialsVerified: false }, { active: true }, { busy: true }, { pending: { id: "pending" } }]) {
    const original = Object.fromEntries(Object.keys(state).map(k => [k, u.controller[k]]));
    Object.assign(u.controller, state); u.controller.applyHabitPreset("qc");
    assert.equal(u.el("qc-model").value, ""); Object.assign(u.controller, original);
  }
  u.controller.close(); u.controller.applyHabitPreset("qc");
  assert.equal(u.el("qc-model").value, ""); assert.equal(u.f.posts("/v1/config").length, 0);
});

test("a failed config reread cannot authorize presets using stale empty settings", async () => {
  for (const action of ["connect", "reloadConfig"]) {
    const u = blankUI(); await u.controller.init();
    assert.equal(u.controller.credentialsVerified, true);
    u.f.override = r => new URL(r.url).pathname === "/v1/config"
      ? { status: 500, data: { error: "synthetic config unavailable" } } : undefined;
    await u.controller[action](false);
    assert.equal(u.controller.credentialsVerified, false);
    selectPreset(u, "qc", "dsh"); u.controller.applyHabitPreset("qc");
    assert.equal(u.el("qc-model").value, ""); assert.equal(u.el("qc-apply-habit").disabled, true);
    assert.match(u.el("qc-habit-note").textContent, /读取已有配置/);
    assert.equal(u.f.posts("/v1/config").length, 0); u.controller.close();
  }
});

test("official GLM/DeepSeek help separates server defaults, output cap and context without remapping inputs", async () => {
  const u = blankUI(); await u.controller.init();
  for (const [id, model, cap, effort] of [["zcode-glm53", "glm-5.3", "131072", "max"], ["dsh", "deepseek-flash", "393216", "high"]]) {
    const p = Bridge.habitPreset(id); p.thinking = "disabled";
    const help = Bridge.modelParameterHelp(p);
    assert.match(help.thinking, /默认 enabled/); assert.ok(help.tokens.includes(cap));
    assert.ok(help.tokens.includes("1M")); assert.equal(help.defaultEffort, effort);
    assert.equal(p.thinking, "disabled");
    u.controller.fillConfig({ configured: false, credentials: { glm: false, deepseek: false }, config: { schema: 1, qc: p, zh: Bridge.profileDefaults("glm") } });
    assert.equal(u.el("qc-thinking").value, "disabled");
    assert.ok(u.el("qc-tokens-notice").textContent.includes(cap));
    assert.deepEqual(u.el("qc-effort-options").children.map(x => x.textContent), ["default", "low", "high", "max"]);
    u.el("qc-base-url").value = "https://example.org/v1"; await u.el("qc-base-url").emit("input");
    assert.match(u.el("qc-thinking-notice").textContent, /未核实/);
    assert.deepEqual(u.el("qc-effort-options").children.map(x => x.textContent), ["default"]);
    assert.equal(u.el("qc-model").value, model); assert.equal(u.el("qc-thinking").value, "disabled");
  }
  assert.equal(u.f.posts("/v1/config").length, 0); u.controller.close();
});


test("documentation buttons use host references without bridge requests", async () => {
  const u = blankUI(); await u.controller.init();
  const opened = []; u.host.openReference = id => opened.push(id);
  const requests = u.f.requests.length;
  await u.el("docs-glm53").emit("click");
  await u.el("docs-deepseek-request").emit("click");
  assert.deepEqual(opened, ["glm53", "deepseek-request"]);
  assert.equal(u.f.requests.length, requests);
  u.host.openReference = () => { throw new Error("synthetic"); };
  await u.el("docs-glm53").emit("click");
  assert.match(u.el("message").textContent, /无法打开官方文档/);
  u.controller.close();
});
