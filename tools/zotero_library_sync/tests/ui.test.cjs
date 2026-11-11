"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { OP, RUN, TOKEN, fixture, ui: baseUI, Panel, Bridge } = require("./helpers.cjs");

// Extend only this suite's isolated fixture; production services are never contacted.
function ui(f = fixture()) {
  const request = f.io.request;
  f.io.request = async (url, options) => {
    if (new URL(url).pathname === "/v1/history") {
      const r = { url, ...options }; f.requests.push(r);
      const response = await f.override?.(r);
      if (response) return response;
      if (!f.online) throw new Error("synthetic offline");
      return { status: 200, data: { runs: [], capabilities: [], active: f.state.active } };
    }
    return request(url, options);
  };
  return baseUI(f);
}

test("no installed backend opens path settings with guidance and starts nothing", async () => {
  const u = ui(); u.host.getPaths = () => ({ repo: "", pythonw: "" });
  await u.controller.init();
  assert.equal(u.el("paths-details").open, true);
  assert.match(u.el("message").textContent, /XPI 只安装界面/);
  assert.equal(u.f.requests.length, 0); assert.equal(u.f.launches.length, 0);
  await u.el("connect").emit("click");
  assert.equal(u.f.launches.length, 0); assert.equal(u.f.requests.length, 0);
  u.controller.close();
});

// Drive the inline picker through bubbling mouse events, not a synthetic change
// on the former native select. Keep this helper local to the UI contract tests.
async function clickOption(u, id, label) {
  const toggle = u.el(id + "-toggle"), list = u.el(id + "-options");
  await toggle.emit("click", { button: 0 });
  assert.equal(list.hidden, false);
  assert.equal(toggle.attrs["aria-expanded"], "true");
  const option = list.children.find(row => row.textContent === label);
  assert.ok(option, `Missing ${id} option: ${label}`);
  assert.equal(option.tagName, "button");
  assert.equal(option.attrs.type, "button");
  assert.equal(option.attrs.role, "option");
  await option.emit("click", { button: 0 });
  assert.equal(list.hidden, true);
  assert.equal(toggle.attrs["aria-expanded"], "false");
  return option;
}

test("disconnected UI says credentials unknown and model shortcut never launches", async () => {
  const f = fixture(); f.online = false;
  const u = ui(f); await u.controller.init();
  assert.match(u.el("credentials").textContent, /无法核实/);
  assert.equal(u.el("save-config").disabled, true);
  const count = f.requests.length;
  await u.el("jump-models").emit("click");
  assert.equal(u.el("models-title").scrollOptions.block, "start");
  assert.equal(u.el("models-title").focusOptions.preventScroll, true);
  assert.equal(f.requests.length, count); assert.equal(f.launches.length, 0);
  u.controller.close();
});
test("connecting preserves unsaved model and key inputs but never auto-saves", async () => {
  const f = fixture(); f.online = false;
  f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  const u = ui(f); await u.controller.init();
  u.el("qc-model").value = "glm-test-model"; await u.el("qc-model").emit("input");
  u.el("zh-model").value = "glm-test-model"; await u.el("zh-model").emit("input");
  u.el("glm-key").value = "synthetic-unsaved-only"; await u.el("glm-key").emit("input");
  f.online = true; await u.controller.connect(true);
  assert.equal(u.el("qc-model").value, "glm-test-model");
  assert.equal(u.el("glm-key").value, "synthetic-unsaved-only");
  assert.equal(u.controller.configDirty, true); assert.equal(u.el("save-config").disabled, false);
  assert.match(u.el("credentials").textContent, /GLM：未保存/);
  assert.equal(f.posts("/v1/config").length, 0); assert.equal(f.launches.length, 0);
  u.controller.close();
});

test("initial UI only reads; 3-second poll and no config still show status", async () => {
  const f = fixture(); f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  const u = ui(f); await u.controller.init();
  assert.ok(f.requests.every(x => x.method === "GET" ||
    x.method === "POST" && new URL(x.url).pathname === "/v1/credentials/reveal"));
  assert.deepEqual(f.posts("/v1/credentials/reveal").map(r => JSON.parse(r.body)), [{ provider: "mineru", confirm: true }]);
  assert.equal(f.launches.length, 0);
  assert.equal([...u.intervals.values()][0].ms, 3000);
  assert.equal(u.el("status-text").textContent, f.state.text); assert.equal(u.el("save-config").disabled, false);
  assert.equal(u.el("discover").disabled, true); assert.equal(u.el("start").disabled, true);
  await u.controller.tick(); assert.equal(f.requests.at(-1).method, "GET"); u.controller.close();
});
test("empty selection never becomes discovery; explicit discovery sends empty keys", async () => {
  const u = ui(); await u.controller.init(); u.host.getSelection = () => ({ items: [], ignored: 1 });
  u.controller.captureSelection(); assert.equal(u.el("preview").disabled, true);
  await u.el("preview").emit("click"); assert.equal(u.f.posts("/v1/preview").length, 0);
  await u.el("discover").emit("click"); assert.deepEqual(JSON.parse(u.f.posts("/v1/preview")[0].body).keys, []);
  assert.equal(u.el("start").disabled, true); u.controller.close();
});
test("preview must finish and cancel confirmation sends no start; repeated start is idempotent", async () => {
  const u = ui(); await u.controller.init(true);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
  u.f.operationStatus = "running"; await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(u.el("start").disabled, true);
  u.f.operationStatus = "completed"; await u.controller.tick(); assert.equal(u.el("start").disabled, false);
  u.window.confirmResult = false; await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
  u.window.confirmResult = true; await u.controller.start(); await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 1);
  assert.deepEqual(JSON.parse(u.f.posts("/v1/start")[0].body), { preview_id: RUN, confirm: true, selections: [{ key: "ABCDEFG2", include: true, translate: true }] });
  assert.equal(u.controller.startResult.run_id, RUN); assert.equal(u.controller.page, "task"); assert.equal(u.el("start").textContent, "查看当前任务");
  u.controller.close();
});
test("input changes invalidate old preview; active run prevents new preview", async () => {
  const u = ui(); await u.controller.init(true); await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  await u.el("workers").emit("input"); assert.equal(u.controller.preview, null); assert.equal(u.el("start").disabled, true);
  u.f.state.active = true; await u.controller.tick(); const n = u.f.posts("/v1/preview").length;
  await u.controller.previewScope([]); assert.equal(u.f.posts("/v1/preview").length, n); u.controller.close();
});
test("dispatch banner expires on its status or a newer run; later form errors survive polling", async () => {
  const u = ui(); await u.controller.init();
  const submitted = "20261105_125719_6d2155", completed = "20261105_153207_85bfe6";
  const status = id => ({ active: false, text: "", view: { id, status: "completed", terminal: true, papers: [] } });
  u.controller.message("提交回执", false, submitted);
  u.controller.renderStatus(status("20261105_102028_6e3be1"));
  assert.equal(u.el("message").hidden, false);
  u.controller.renderStatus(status(completed));
  assert.equal(u.el("message").hidden, true);
  assert.ok(u.el("summary").textContent.includes(completed));
  u.controller.message("提交回执", false, submitted);
  u.controller.renderStatus(status(submitted));
  assert.equal(u.el("message").hidden, true);
  u.controller.message("设置未保存", true); u.controller.renderStatus(status(completed));
  assert.equal(u.el("message").textContent, "设置未保存");
  assert.equal(u.el("message").hidden, false); u.controller.close();
});
test("titles, logs and backend errors render as plain text with report path", async () => {
  const u = ui(); await u.controller.init();
  const hostile = '<img src=x onerror="throw 1"> & <script>evil()</script>';
  u.f.state = { active: false, text: hostile, view: { status: "failed", id: RUN, phase: "qc", elapsed_seconds: 65,
    phase_elapsed_seconds: 5, report: "C:\\reports\\failure.md", papers: [{ key: "ABCDEFG2", title: hostile, accepted: false }] } };
  await u.controller.tick();
  assert.equal(u.el("status-text").textContent, hostile); assert.ok(u.el("status-rows").textContent.includes(hostile));
  assert.ok(u.el("summary").textContent.includes("0时01分05秒"));
  assert.ok(u.el("report-path").textContent.includes("C:\\reports\\failure.md"));
  u.f.preview.items[0].title = hostile; await u.controller.previewScope([]); await u.controller.tick();
  assert.ok(u.el("preview-rows").textContent.includes(hostile)); u.controller.close();
});
test("passwords survive successful and failed saves; unchanged keys are omitted and never go to prefs", async () => {
  const u = ui(); await u.controller.init(); let prefWrites = 0; u.host.setPaths = () => { prefWrites++; };
  const originalDeepseek = u.f.keys.deepseek;
  u.el("glm-key").value = "synthetic-ui-key-only"; await u.el("glm-key").emit("input");
  await u.controller.saveConfig();
  assert.equal(u.el("glm-key").value, "synthetic-ui-key-only");
  assert.equal(u.el("glm-key").type, "password");
  assert.equal(u.el("deepseek-key").value, originalDeepseek);
  assert.deepEqual(JSON.parse(u.f.posts("/v1/config")[0].body).keys, { glm: "synthetic-ui-key-only" });
  await u.controller.saveConfig();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/config")[1].body).keys, {});
  await u.controller.saveConfig(true); assert.equal(u.f.posts("/v1/keys").length, 0);
  u.f.override = r => new URL(r.url).pathname === "/v1/config" && r.method === "POST"
    ? { status: 400, data: { error: `请先保存 GLM 密钥 synthetic-ui-key-again ${originalDeepseek} ${TOKEN}` } } : undefined;
  u.el("glm-key").value = "synthetic-ui-key-again"; await u.el("glm-key").emit("input");
  await u.controller.saveConfig();
  assert.equal(u.el("glm-key").value, "synthetic-ui-key-again");
  assert.equal(u.el("deepseek-key").value, originalDeepseek);
  assert.equal(u.el("glm-key").type, "password");
  assert.match(u.el("credentials").textContent, /未获确认.*输入已保留/);
  assert.match(u.el("message").textContent, /请先保存 GLM 密钥/);
  for (const secret of [TOKEN, originalDeepseek, "synthetic-ui-key-again"]) assert.ok(!u.el("message").textContent.includes(secret));
  assert.equal(prefWrites, 0); assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});
test("empty model validation preserves both keys and does not attach a historical run report", async () => {
  const u = ui(); await u.controller.init();
  u.controller.report = "C:\\reports\\old-completed-paper.md";
  u.el("qc-model").value = "";
  u.el("glm-key").value = "synthetic-not-sent-glm"; u.el("deepseek-key").value = "synthetic-not-sent-ds";
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/config").length, 0);
  assert.equal(u.el("glm-key").value, "synthetic-not-sent-glm");
  assert.equal(u.el("deepseek-key").value, "synthetic-not-sent-ds");
  assert.match(u.el("message").textContent, /质检 QC/);
  assert.ok(!u.el("message").textContent.includes("old-completed-paper"));
  u.el("qc-model").value = "test-model"; u.el("zh-model").value = "";
  await u.controller.saveConfig(); assert.match(u.el("message").textContent, /翻译 ZH/);
  assert.equal(u.f.posts("/v1/config").length, 0); u.controller.close();
});
test("keys save independently before models; status verified, draft preserved, no task starts", async () => {
  const f = fixture(); f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  const u = ui(f); await u.controller.init();
  u.el("glm-key").value = "synthetic-glm"; u.el("qc-model").value = "draft-model";
  u.el("qc-extra-body").value = "{unfinished JSON";
  await u.controller.saveConfig(true);
  assert.equal(u.el("glm-key").value, "synthetic-glm"); assert.equal(u.el("qc-model").value, "draft-model");
  assert.equal(u.el("qc-extra-body").value, "{unfinished JSON");
  assert.match(u.el("credentials").textContent, /GLM：已保存/);
  assert.equal(f.posts("/v1/keys").length, 1); assert.equal(f.posts("/v1/config").length, 0);
  assert.deepEqual(JSON.parse(f.posts("/v1/keys")[0].body), { keys: { glm: "synthetic-glm" } });
  await u.controller.saveConfig(true); assert.equal(f.posts("/v1/keys").length, 1);
  assert.equal(f.posts("/v1/start").length, 0); assert.equal(f.posts("/v1/test").length, 0);
  assert.equal(u.el("qc-models").disabled, false); u.controller.close();
});
test("failed keys-only save retains the draft for an explicit retry without saving model settings", async () => {
  const u = ui(); await u.controller.init();
  const oldKey = u.f.keys.deepseek;
  u.el("deepseek-key").value = "synthetic-keys-only-retry"; await u.el("deepseek-key").emit("input");
  u.f.override = r => new URL(r.url).pathname === "/v1/keys"
    ? { status: 400, data: { error: "synthetic-keys-only-retry could not be saved" } } : undefined;
  await u.controller.saveConfig(true);
  assert.equal(u.el("deepseek-key").value, "synthetic-keys-only-retry");
  assert.equal(u.controller.loadedKeys.deepseek, oldKey);
  assert.match(u.el("credentials").textContent, /未获确认.*输入已保留/);
  assert.ok(!u.el("message").textContent.includes("synthetic-keys-only-retry"));
  u.f.override = undefined; await u.controller.saveConfig(true);
  assert.equal(u.f.posts("/v1/keys").length, 2);
  for (const r of u.f.posts("/v1/keys")) assert.deepEqual(JSON.parse(r.body), { keys: { deepseek: "synthetic-keys-only-retry" } });
  assert.equal(u.el("deepseek-key").value, "synthetic-keys-only-retry");
  await u.controller.saveConfig(true); assert.equal(u.f.posts("/v1/keys").length, 2);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  u.controller.close();
});
test("MinerU updates independently, retains input after success or failure and skips unchanged keys", async () => {
  const u = ui(); await u.controller.init();
  u.el("qc-model").value = ""; u.el("zh-model").value = "";
  u.el("mineru-key").value = "invalid inner whitespace";
  await u.controller.saveMineru(); assert.equal(u.f.posts("/v1/mineru").length, 0);
  assert.equal(u.el("mineru-key").value, "invalid inner whitespace");
  u.el("mineru-key").value = "synthetic-mineru-only"; await u.controller.saveMineru();
  assert.equal(u.el("mineru-key").value, "synthetic-mineru-only"); assert.match(u.el("mineru-state").textContent, /加密保存/);
  assert.match(u.el("mineru-state").textContent, /未验证有效期/);
  await u.controller.saveMineru(); assert.equal(u.f.posts("/v1/mineru").length, 1);
  u.f.override = r => { if (new URL(r.url).pathname === "/v1/mineru") throw new Error("uncertain"); };
  u.el("mineru-key").value = "synthetic-mineru-again"; await u.controller.saveMineru();
  assert.equal(u.el("mineru-key").value, "synthetic-mineru-again"); assert.match(u.el("mineru-state").textContent, /未获确认/);
  assert.equal(u.el("mineru-key").type, "password");
  assert.equal(u.f.posts("/v1/mineru").length, 2); assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});
test("clearing an existing key is explicitly rejected for each provider without any write", async () => {
  for (const provider of ["glm", "deepseek", "mineru"]) {
    const u = ui(); await u.controller.init();
    u.el(provider + "-key").value = "   "; await u.el(provider + "-key").emit("input");
    if (provider === "mineru") await u.controller.saveMineru();
    else {
      await u.controller.saveConfig(true);
      assert.match(u.el("message").textContent, /已有 Key，不能保存为空/);
      await u.controller.saveConfig();
    }
    assert.match(u.el("message").textContent, /已有 Key，不能保存为空/);
    assert.equal(u.el(provider + "-key").value, "   ");
    for (const route of ["/v1/keys", "/v1/config", "/v1/mineru", "/v1/start"]) assert.equal(u.f.posts(route).length, 0);
    u.controller.close();
  }
});
test("translation defaults on, turning off invalidates preview and sends explicit false", async () => {
  const u = ui(); await u.controller.init(true);
  assert.equal(u.el("translate").checked, true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(JSON.parse(u.f.posts("/v1/preview")[0].body).translate, true);
  u.el("translate").checked = false; await u.el("translate").emit("change");
  assert.equal(u.controller.preview, null); assert.equal(u.el("start").disabled, true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(JSON.parse(u.f.posts("/v1/preview")[1].body).translate, false);
  await u.controller.start(); assert.match(u.window.confirms[0], /不新增译文/);
  u.controller.close();
  const reopened = ui(u.f); await reopened.controller.init(); assert.equal(reopened.el("translate").checked, true);
  assert.equal(u.f.posts("/v1/start").length, 1); reopened.controller.close();
});
test("old service cannot silently ignore translation policy and running tasks lock credential writes", async () => {
  const f = fixture(); f.state.capabilities = [];
  const u = ui(f); await u.controller.init();
  assert.equal(u.el("discover").disabled, true); assert.equal(u.el("save-mineru").disabled, true);
  await u.controller.previewScope([]); assert.equal(f.posts("/v1/preview").length, 0);
  f.state.capabilities = ["keys-v1", "mineru-v1", "translation-policy-v1"]; f.state.active = true;
  await u.controller.tick();
  for (const id of ["save-config", "save-keys", "save-mineru"]) assert.equal(u.el(id).disabled, true);
  u.controller.close();
});
test("source-only with saved profiles needs only the QC provider key", async () => {
  const f = fixture(); f.publicConfig.configured = false; f.publicConfig.credentials.deepseek = false;
  const u = ui(f); await u.controller.init(true);
  assert.equal(u.el("preview").disabled, true);
  u.el("translate").checked = false; await u.el("translate").emit("change");
  assert.equal(u.el("preview").disabled, false);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  await u.controller.start(); assert.equal(f.posts("/v1/start").length, 1); u.controller.close();
});
test("source-only still requires the QC model and its saved provider key before preview", async () => {
  for (const missing of ["model", "key"]) {
    const f = fixture(); f.publicConfig.configured = false;
    if (missing === "model") f.publicConfig.config.qc.model = "";
    else f.publicConfig.credentials.glm = false;
    const u = ui(f); await u.controller.init(true);
    u.el("translate").checked = false; await u.el("translate").emit("change");
    assert.equal(u.el("preview").disabled, true); assert.equal(u.el("discover").disabled, true);
    await u.controller.previewScope(u.controller.keys); await u.controller.start();
    assert.equal(f.posts("/v1/preview").length, 0); assert.equal(f.posts("/v1/start").length, 0);
    u.controller.close();
  }
});
test("model requests send independent flat profiles; errors retain safe explanation", async () => {
  const u = ui(); await u.controller.init();
  u.f.operationResult = { provider: "deepseek", models: ["model-A", "model-B"], source: "provider_api", notice: "Live provider list" };
  await u.controller.modelAction("zh", "models"); await u.controller.tick();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/models")[0].body), {
    provider: "deepseek", model: "deepseek-test-model", reasoning_effort: "default", timeout: 1500, max_tokens: 32768,
    base_url: "https://api.deepseek.com", models_url: "https://api.deepseek.com/models",
    protocol: "openai_chat_completions", thinking: "default", extra_body: {}, endpoint_confirmed: false
  });
  assert.ok(u.el("zh-model-options").textContent.includes("model-A"));
  u.el("qc-effort").value = "high"; await u.el("qc-effort").emit("input");
  u.el("qc-thinking").value = "enabled"; await u.el("qc-thinking").emit("input");
  u.el("qc-extra-body").value = '{"temperature":0.25}'; await u.el("qc-extra-body").emit("input");
  u.el("qc-timeout").value = "123"; u.el("qc-max-tokens").value = "4096";
  await u.controller.modelAction("qc", "test");
  assert.deepEqual(JSON.parse(u.f.posts("/v1/test")[0].body), {
    role: "qc", provider: "glm", model: "glm-test-model", reasoning_effort: "high", timeout: 123, max_tokens: 4096,
    base_url: "https://open.bigmodel.cn/api/paas/v4", models_url: "",
    protocol: "openai_chat_completions", thinking: "enabled", extra_body: { temperature: 0.25 }, endpoint_confirmed: false
  });
  assert.equal(u.el("zh-effort").value, "default"); assert.equal(u.window.confirms.length, 0);
  u.f.operationStatus = "failed"; u.f.operationError = "model_listing_not_supported"; await u.controller.tick();
  assert.ok(u.el("message").textContent.includes("不支持模型列表")); assert.ok(!u.el("message").textContent.includes("报告目录")); u.controller.close();
});
test("model test buttons send explicit stage roles even when QC and ZH use the same profile", async () => {
  const f = fixture(); f.publicConfig.config.zh = structuredClone(f.publicConfig.config.qc);
  const u = ui(f); await u.controller.init();
  f.operationResult = { ok: true };
  for (const role of ["qc", "zh"]) {
    await u.el(role + "-test").emit("click");
    assert.deepEqual(JSON.parse(f.posts("/v1/test").at(-1).body), {
      role, provider: "glm", model: "glm-test-model", reasoning_effort: "default", timeout: 1500, max_tokens: 32768,
      base_url: "https://open.bigmodel.cn/api/paas/v4", models_url: "",
      protocol: "openai_chat_completions", thinking: "default", extra_body: {}, endpoint_confirmed: false
    });
    await u.controller.tick();
    assert.ok(u.el("model-result").textContent.startsWith(role.toUpperCase()));
  }
  assert.equal(f.posts("/v1/test").length, 2); assert.equal(f.posts("/v1/models").length, 0);
  assert.equal(f.posts("/v1/config").length, 0); assert.equal(f.posts("/v1/start").length, 0);
  u.controller.close();
});
test("model result keeps requested_model and numeric usage, omits nested secrets and masks a known synthetic key", async t => {
  const u = ui(); t.after(() => u.controller.close()); await u.controller.init();
  // This value comes only from the in-memory fixture's read-only reveal. Echo
  // it in an allowed public field to exercise value redaction, not name filters.
  const knownKey = u.f.keys.glm;
  assert.equal(u.el("glm-key").value, knownKey);
  assert.equal(u.f.client.revealedSensitive.get("glm"), knownKey);
  const usage = { prompt_tokens: 17, completion_tokens: 5, total_tokens: 22,
    prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 17,
    prompt_tokens_details: { cached_tokens: 0 }, completion_tokens_details: { reasoning_tokens: 3 } };
  u.f.operationResult = {
    ok: true, provider: "glm", role: "qc", requested_model: "glm-test-model", model: knownKey,
    reasoning_effort: "default", finish_reason: "stop", attempts: 1, elapsed_seconds: 0.125,
    usage: { ...usage,
      prompt_tokens_details: { ...usage.prompt_tokens_details, credentials: { api_key: "nested-prompt-canary" } },
      completion_tokens_details: { ...usage.completion_tokens_details, secret: { value: "nested-completion-canary" } },
      api_key: "usage-secret-canary", diagnostics: { arbitrary: "usage-unknown-canary" } },
    credentials: { api_key: "root-credential-canary" }, headers: { Authorization: "header-secret-canary" },
    request: { body: { secret: "request-secret-canary" } }, raw: { secret: "raw-secret-canary" },
    diagnostics: { arbitrary: "root-unknown-canary" }
  };
  await u.el("qc-test").emit("click"); await u.controller.tick();
  const rendered = u.el("model-result").textContent;
  assert.match(rendered, /^QC 测试结果\n/);
  const displayed = JSON.parse(rendered.slice(rendered.indexOf("\n") + 1));
  assert.deepEqual(displayed, {
    ok: true, provider: "glm", role: "qc", requested_model: "glm-test-model", model: "[已隐藏]",
    reasoning_effort: "default", finish_reason: "stop", attempts: 1, elapsed_seconds: 0.125, usage
  });
  for (const secret of [knownKey, "nested-prompt-canary", "nested-completion-canary", "usage-secret-canary",
    "usage-unknown-canary", "root-credential-canary", "header-secret-canary", "request-secret-canary",
    "raw-secret-canary", "root-unknown-canary"]) assert.equal(rendered.includes(secret), false);
  assert.equal(u.el("glm-key").value, knownKey); assert.equal(u.el("glm-key").type, "password");
  assert.equal(u.f.posts("/v1/test").length, 1);
  for (const route of ["/v1/config", "/v1/keys", "/v1/mineru", "/v1/start"]) assert.equal(u.f.posts(route).length, 0);
});

test("inline model picker mouse clicks fill only its role and keep the preview awaiting saved models", async () => {
  const u = ui(); await u.controller.init(true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3-flash", "glm-5.3-flashx"], source: "bundled_catalog", notice: "文档候选，未验证账号权限" };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  assert.equal(u.el("qc-model").value, "glm-test-model");
  assert.equal(u.el("qc-model").disabled, false);
  assert.match(u.el("qc-model-notice").textContent, /不是账号可用清单/);
  await clickOption(u, "qc-model", "glm-5.3-flash");
  assert.equal(u.el("qc-model").value, "glm-5.3-flash");
  assert.equal(u.el("qc-model").value, "glm-5.3-flash"); assert.equal(u.el("zh-model").value, "deepseek-test-model");
  assert.equal(u.controller.configDirty, true); assert.equal(u.controller.modelDirty, true);
  // The discovery scope and per-paper selections survive a model draft edit.
  assert.ok(u.controller.preview); assert.deepEqual(u.controller.selections.map(row => row.key), ["ABCDEFG2"]);
  assert.equal(u.el("start").disabled, true);
  assert.match(u.el("start-reason").textContent, /存在未保存修改/);
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
  u.el("qc-model").value = "my-custom-model"; await u.el("qc-model").emit("input");
  assert.equal(u.el("qc-model").value, "my-custom-model");
  await clickOption(u, "qc-provider", "DeepSeek");
  assert.equal(u.el("qc-model").disabled, false); assert.equal(u.controller.modelOptions.qc, null);
  assert.equal(u.el("qc-model").value, "my-custom-model"); u.controller.close();
});
test("refreshed or empty model choices cannot reuse stale option clicks or overwrite a typed model", async () => {
  const u = ui(); await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["old-model"], source: "bundled_catalog", notice: "old" };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  const stale = u.el("qc-model-options").children[0];
  u.f.operationResult = { provider: "glm", models: ["current-model"], source: "bundled_catalog", notice: "current" };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  await u.el("qc-model-toggle").emit("click", { button: 0 });
  await stale.emit("click", { button: 0 });
  assert.equal(u.el("qc-model").value, "glm-test-model");
  await u.el("qc-model-options").children[0].emit("click", { button: 0 });
  assert.equal(u.el("qc-model").value, "current-model");
  u.f.operationResult = { provider: "glm", models: [], source: "provider_api", notice: "empty" };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  assert.equal(u.el("qc-model-options").children.length, 0);
  assert.equal(u.el("qc-model-toggle").disabled, true);
  assert.equal(u.el("qc-model").value, "current-model"); assert.equal(u.el("qc-model").disabled, false);
  u.el("qc-model").value = "hand-typed-model"; await u.el("qc-model").emit("input");
  assert.equal(u.el("qc-model").value, "hand-typed-model");
  u.window.close(); await stale.emit("click", { button: 0 });
  assert.equal(u.el("qc-model").value, "hand-typed-model");
  assert.equal(u.f.posts("/v1/config").length, 0); assert.equal(u.f.posts("/v1/test").length, 0);
});
test("provider picker preserves manually edited endpoint parameters and the typed model", async () => {
  const u = ui(); await u.controller.init();
  const zh = u.controller.readModel("zh");
  u.el("qc-model").value = "my-custom-model"; await u.el("qc-model").emit("input");
  u.el("qc-base-url").value = "https://old.example.com/v1";
  u.el("qc-models-url").value = "https://old.example.com/models";
  u.el("qc-protocol").value = "old-protocol"; u.el("qc-thinking").value = "enabled";
  u.el("qc-extra-body").value = '{"temperature":0.5}'; u.el("qc-effort").value = "custom-level";
  u.controller.endpointConfirmed.qc = true;
  await clickOption(u, "qc-provider", "DeepSeek");
  assert.deepEqual(u.controller.readModel("qc"), {
    provider: "deepseek", model: "my-custom-model", reasoning_effort: "custom-level", timeout: 1500, max_tokens: 32768,
    base_url: "https://old.example.com/v1", models_url: "https://old.example.com/models",
    protocol: "old-protocol", thinking: "enabled", extra_body: { temperature: 0.5 }, endpoint_confirmed: false
  });
  assert.deepEqual(u.controller.readModel("zh"), zh);
  assert.equal(u.controller.modelOptions.qc, null); assert.equal(u.controller.configDirty, true);
  for (const route of ["/v1/config", "/v1/test", "/v1/models", "/v1/start"]) assert.equal(u.f.posts(route).length, 0);
  u.controller.close();
});
test("chunk picker mouse selection invalidates preview and disabled pickers cannot alter a pending request", async () => {
  const u = ui(); await u.controller.init(true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  await clickOption(u, "chunk-parallel", "2");
  assert.equal(u.el("chunk-parallel").value, "2"); assert.equal(u.controller.preview, null);
  assert.equal(u.el("start").disabled, true);
  await u.controller.previewScope(u.controller.keys);
  assert.equal(JSON.parse(u.f.posts("/v1/preview").at(-1).body).chunk_parallel, 2);
  const input = u.el("chunk-parallel"), toggle = u.el("chunk-parallel-toggle"), list = u.el("chunk-parallel-options");
  assert.equal(input.disabled, true); assert.equal(toggle.disabled, true);
  await toggle.emit("click", { button: 0 }); await list.children[0].emit("click", { button: 0 });
  assert.equal(input.value, "2"); assert.equal(list.hidden, true);
  assert.equal(u.f.posts("/v1/preview").length, 2); u.controller.close();
});
test("model results for stale providers and malformed IDs cannot replace model choices", async () => {
  const u = ui(); await u.controller.init();
  await u.controller.modelAction("qc", "models");
  const record = { ...u.controller.catalogPending.qc };
  u.el("qc-provider").value = "deepseek";
  u.controller.renderModelResult({ ...record, kind: "models" }, { provider: "glm", models: ["glm-one"], source: "bundled_catalog", notice: "candidate" });
  assert.equal(u.controller.modelOptions.qc, null);
  u.el("qc-provider").value = "glm";
  u.controller.renderModelResult({ ...record, kind: "models" }, { provider: "glm", models: ["<img src=x>"], source: "bundled_catalog", notice: "bad" });
  assert.equal(u.controller.modelOptions.qc, null);
  assert.match(u.el("qc-model-notice").textContent, /响应无效/); u.controller.close();
});
test("pending model lists are bound to the exact base URL, models URL and protocol", async () => {
  for (const [field, replacement] of [["base-url", "https://other.example.com/v1"],
    ["models-url", "https://other.example.com/models"], ["protocol", "changed-protocol"]]) {
    const u = ui(); await u.controller.init();
    await u.controller.modelAction("qc", "models");
    assert.deepEqual(JSON.parse(u.controller.catalogPending.qc.endpoint),
      ["https://open.bigmodel.cn/api/paas/v4", "", "openai_chat_completions"]);
    u.el("qc-" + field).value = replacement; await u.el("qc-" + field).emit("input");
    u.f.operationResult = { provider: "glm", models: ["stale-model"], source: "bundled_catalog", notice: "old endpoint" };
    await u.controller.tick();
    assert.equal(u.controller.catalogPending.qc, undefined); assert.equal(u.controller.modelOptions.qc, null);
    assert.equal(u.el("qc-model-options").children.length, 0);
    assert.equal(u.el("qc-model").value, "glm-test-model");
    assert.match(u.el("message").textContent, /地址或协议已改变.*未应用/);
    assert.equal(u.f.posts("/v1/models").length, 1); u.controller.close();
  }
});
test("custom endpoint confirmation is required for flat model profiles and resets after an endpoint edit", async () => {
  const u = ui(); await u.controller.init();
  u.el("qc-base-url").value = "https://models.example.com/v1"; await u.el("qc-base-url").emit("input");
  u.el("qc-models-url").value = "https://models.example.com/models"; await u.el("qc-models-url").emit("input");
  u.window.confirmResult = false; await u.controller.modelAction("qc", "models");
  assert.equal(u.f.posts("/v1/models").length, 0); assert.equal(u.controller.endpointConfirmed.qc, false);
  u.window.confirmResult = true; await u.controller.modelAction("qc", "models");
  assert.deepEqual(JSON.parse(u.f.posts("/v1/models")[0].body), {
    provider: "glm", model: "glm-test-model", reasoning_effort: "default", timeout: 1500, max_tokens: 32768,
    base_url: "https://models.example.com/v1", models_url: "https://models.example.com/models",
    protocol: "openai_chat_completions", thinking: "default", extra_body: {}, endpoint_confirmed: true
  });
  u.f.operationResult = { provider: "glm", models: [], source: "provider_api", notice: "empty" }; await u.controller.tick();
  await u.controller.modelAction("qc", "test"); assert.equal(u.window.confirms.length, 2);
  u.f.operationResult = { ok: true }; await u.controller.tick();
  u.el("qc-models-url").value = "https://models.example.com/new-models"; await u.el("qc-models-url").emit("input");
  assert.equal(u.controller.endpointConfirmed.qc, false);
  u.window.confirmResult = false; await u.controller.modelAction("qc", "test");
  assert.equal(u.f.posts("/v1/test").length, 1); assert.equal(u.window.confirms.length, 3);
  assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});
test("effort suggestions use exact model metadata, allow mouse choice and preserve hand-typed values", async () => {
  for (const source of ["bundled_catalog", "provider_api"]) {
    const u = ui(); await u.controller.init();
    u.el("qc-effort").value = "hand-typed-level"; await u.el("qc-effort").emit("input");
    u.f.operationResult = { provider: "glm", models: ["glm-test-model", "glm-test-model-preview", "glm-other"], source, notice: "candidates",
      details: {
        "glm-test-model": { effort_levels: ["minimal", "high"], default_effort: "high" },
        "glm-other": { effort_levels: ["low", "medium"] }
      } };
    await u.controller.modelAction("qc", "models"); await u.controller.tick();
    const levels = () => u.el("qc-effort-options").children.map(row => row.textContent);
    assert.deepEqual(levels(), ["default", "minimal", "high"]);
    assert.equal(u.el("qc-effort").value, "hand-typed-level");
    assert.match(u.el("qc-effort-notice").textContent, /服务端默认 high/);
    assert.match(u.el("qc-effort-notice").textContent, source === "provider_api" ? /接口元数据/ : /官方文档/);
    await clickOption(u, "qc-effort", "minimal"); assert.equal(u.el("qc-effort").value, "minimal");
    await clickOption(u, "qc-model", "glm-other");
    assert.deepEqual(levels(), ["default", "low", "medium"]); assert.equal(u.el("qc-effort").value, "minimal");
    await clickOption(u, "qc-model", "glm-test-model-preview");
    assert.deepEqual(levels(), ["default"]); assert.equal(u.el("qc-effort").value, "minimal");
    assert.match(u.el("qc-effort-notice").textContent, /未绑定固定 effort 档位/);
    u.el("qc-model").value = "GLM-test-model"; await u.el("qc-model").emit("input");
    assert.deepEqual(levels(), ["default"]);
    u.el("qc-effort").value = "another-custom-level"; await u.el("qc-effort").emit("input");
    assert.equal(u.el("qc-effort").value, "another-custom-level");
    assert.equal(u.el("zh-effort").value, "default");
    assert.equal(u.f.posts("/v1/test").length, 0); assert.equal(u.f.posts("/v1/config").length, 0);
    u.controller.close();
  }
});
test("init loads synthetic saved keys into the single password inputs using read-only POST reveals", async () => {
  const u = ui(); await u.controller.init();
  // Gecko owns the password eye and :-moz-revealed. This fake DOM cannot
  // exercise native reveal behavior; that requires real-Gecko acceptance.
  for (const p of ["glm", "deepseek", "mineru"]) {
    assert.equal(u.el(p + "-key").value, u.f.keys[p]); assert.equal(u.el(p + "-key").type, "password");
    assert.equal(u.el(p + "-revealed"), null);
    assert.equal(u.el(p + "-reveal") === null, true, `Redundant ${p}-reveal button must not exist`);
    assert.ok(!u.el("message").textContent.includes(u.f.keys[p]));
  }
  assert.equal(typeof u.controller.revealSecret, "undefined");
  // confirm is the transport contract, not an interactive confirmation dialog.
  assert.deepEqual(u.f.posts("/v1/credentials/reveal").map(r => JSON.parse(r.body)),
    ["glm", "deepseek", "mineru"].map(provider => ({ provider, confirm: true })));
  assert.ok(u.f.requests.every(r => r.method === "GET" ||
    r.method === "POST" && new URL(r.url).pathname === "/v1/credentials/reveal"));
  assert.equal(u.window.confirms.length, 0); assert.equal(u.timeouts.size, 0); assert.equal(u.f.launches.length, 0);
  u.controller.close();
});
test("panel adds no automatic key-hide listeners or timers; unchanged password inputs are not resubmitted", async () => {
  const u = ui(); await u.controller.init();
  const inputs = ["glm", "deepseek", "mineru"].map(p => u.el(p + "-key"));
  for (const target of [u.window, u.doc, ...inputs]) {
    for (const event of ["blur", "focusout", "visibilitychange"]) {
      assert.equal(target.listeners.get(event)?.size || 0, 0, `Unexpected ${event} listener on ${target.id || target.tagName}`);
    }
  }
  const requests = u.f.requests.length;
  await u.window.emit("blur");
  u.doc.hidden = true; await u.doc.emit("visibilitychange"); u.doc.hidden = false;
  for (const p of ["glm", "deepseek", "mineru"]) {
    await u.el(p + "-key").emit("blur"); await u.el(p + "-key").emit("input");
    assert.equal(u.el(p + "-key").type, "password"); assert.equal(u.el(p + "-key").value, u.f.keys[p]);
  }
  assert.equal(u.f.requests.length, requests); assert.equal(u.timeouts.size, 0);
  await u.controller.saveConfig(true); await u.controller.saveMineru();
  assert.equal(u.f.posts("/v1/keys").length, 0);
  assert.equal(u.f.posts("/v1/mineru").length, 0); assert.equal(u.f.posts("/v1/start").length, 0);
  await u.controller.saveConfig();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/config")[0].body).keys, {});
  for (const p of ["glm", "deepseek", "mineru"]) {
    assert.equal(u.el(p + "-key").type, "password"); assert.equal(u.el(p + "-key").value, u.f.keys[p]);
  }
  assert.equal(u.timeouts.size, 0); assert.equal(u.window.confirms.length, 0);
  u.controller.close();
  for (const p of ["glm", "deepseek", "mineru"]) {
    assert.equal(u.el(p + "-key").value, ""); assert.equal(u.el(p + "-key").type, "password");
  }
  assert.equal(u.timeouts.size, 0); assert.equal(u.f.client.revealedSensitive.size, 0);
});
test("explicit reload replaces key drafts with saved synthetic values without saving them", async () => {
  const u = ui(); await u.controller.init();
  for (const p of ["glm", "deepseek", "mineru"]) {
    u.el(p + "-key").value = "synthetic-unsaved-" + p; await u.el(p + "-key").emit("input");
    u.f.keys[p] = "synthetic-reloaded-" + p;
  }
  await u.el("reload-config").emit("click"); await u.el("reload-mineru").emit("click");
  for (const p of ["glm", "deepseek", "mineru"]) {
    assert.equal(u.el(p + "-key").value, u.f.keys[p]); assert.equal(u.el(p + "-key").type, "password");
  }
  await u.controller.saveConfig(true); await u.controller.saveMineru();
  for (const route of ["/v1/config", "/v1/keys", "/v1/mineru", "/v1/start"]) assert.equal(u.f.posts(route).length, 0);
  assert.equal(u.f.posts("/v1/credentials/reveal").length, 6); assert.equal(u.window.confirms.length, 0);
  u.controller.close();
});
test("late init or reload cannot resurrect keys or polling after the panel closes", async () => {
  for (const phase of ["init", "reload"]) {
    const u = ui(); if (phase === "reload") await u.controller.init();
    let resolve, markStarted;
    const started = new Promise(r => { markStarted = r; });
    u.f.override = r => new URL(r.url).pathname === "/v1/credentials/reveal"
      ? new Promise(r => { resolve = r; markStarted(); }) : undefined;
    const pending = phase === "init" ? u.controller.init() : u.controller.reloadConfig();
    await started;
    u.window.close(); const requests = u.f.requests.length;
    resolve({ status: 200, data: { provider: "glm", key: "synthetic-late-secret", source: "encrypted" } });
    await pending;
    for (const p of ["glm", "deepseek", "mineru"]) {
      assert.equal(u.el(p + "-key").value, ""); assert.equal(u.el(p + "-key").type, "password");
    }
    assert.deepEqual(u.controller.loadedKeys, {}); assert.equal(u.f.client.revealedSensitive.size, 0);
    assert.equal(u.f.client.sensitive.size, 0); assert.equal(u.intervals.size, 0); assert.equal(u.timeouts.size, 0);
    assert.equal(u.controller.handlers.length, 0); assert.equal(u.f.requests.length, requests);
    assert.ok(!u.el("message").textContent.includes("synthetic-late-secret"));
    await u.controller.tick(); await u.el("glm-key").emit("input");
    assert.equal(u.el("glm-key").value, ""); assert.equal(u.f.requests.length, requests);
  }
});
test("blur during initial key loading still fills the password input without prompting or starting a timer", async () => {
  const u = ui(); let resolve, markStarted;
  const started = new Promise(r => { markStarted = r; });
  u.f.override = r => new URL(r.url).pathname === "/v1/credentials/reveal" && JSON.parse(r.body).provider === "glm"
    ? new Promise(r => { resolve = r; markStarted(); }) : undefined;
  const pending = u.controller.init(); await started; await u.window.emit("blur");
  resolve({ status: 200, data: { provider: "glm", key: "synthetic-late-blur", source: "encrypted" } });
  await pending;
  assert.equal(u.el("glm-key").value, "synthetic-late-blur"); assert.equal(u.el("glm-key").type, "password");
  assert.equal(u.window.confirms.length, 0); assert.equal(u.timeouts.size, 0);
  assert.equal(u.f.posts("/v1/credentials/reveal").length, 3); u.controller.close();
});
test("failed model requests never cite the unrelated completed paper report", async () => {
  const u = ui(); await u.controller.init(); u.controller.report = "C:\\reports\\old-paper.md";
  await u.controller.modelAction("qc", "models"); u.f.operationStatus = "failed"; u.f.operationError = "model_listing_not_supported";
  await u.controller.tick();
  assert.match(u.el("model-result").textContent, /手填模型 ID/);
  assert.ok(!u.el("message").textContent.includes("old-paper")); u.controller.close();
});
test("connection failure during operation permits manual reconnect and resumes polling", async () => {
  const u = ui(); await u.controller.init(); await u.controller.previewScope([]);
  u.f.online = false; await u.controller.tick(); assert.equal(u.controller.connected, false);
  assert.equal(u.el("connect").disabled, false); const n = u.f.requests.length;
  await u.controller.tick(); assert.equal(u.f.requests.length, n);
  u.f.online = true; await u.controller.connect(true); await u.controller.tick();
  assert.equal(u.controller.preview.preview_id, RUN); assert.equal(u.f.posts("/v1/preview").length, 1); u.controller.close();
});
test("lost start response allows explicit same-preview retry; no automatic resend", async () => {
  const u = ui(); await u.controller.init(); await u.controller.previewScope([]); await u.controller.tick();
  let once = true;
  u.f.override = r => { if (new URL(r.url).pathname === "/v1/start" && once) { once = false; u.f.state.active = true; throw new Error("lost"); } };
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 1); await u.controller.tick();
  assert.equal(u.controller.connected, false);
  assert.equal(u.el("start").disabled, true);
  await u.controller.connect(true);
  assert.equal(u.el("start").disabled, false); await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 2);
  assert.ok(u.f.posts("/v1/start").every(r => JSON.parse(r.body).preview_id === RUN)); u.controller.close();
});
test("close while operation runs clears timers, secrets and listeners without cancel/stop", async () => {
  const u = ui(); await u.controller.init(); await u.controller.previewScope([]);
  const n = u.f.requests.length; u.el("glm-key").value = "unsaved-synthetic-key"; u.window.close();
  assert.equal(u.intervals.size, 0); assert.equal(u.el("glm-key").value, "");
  for (const p of ["glm", "deepseek", "mineru"]) {
    assert.equal(u.el(p + "-key").value, ""); assert.equal(u.el(p + "-key").type, "password");
  }
  assert.equal(u.timeouts.size, 0); assert.equal(u.f.client.sensitive.size, 0); assert.equal(u.f.client.revealedSensitive.size, 0);
  for (const target of [u.doc, u.window, ...u.doc.elements.values()]) {
    for (const listeners of target.listeners.values()) assert.equal(listeners.size, 0, `Leaked listener on ${target.id || target.tagName}`);
  }
  await u.controller.tick(); await u.controller.start(); assert.equal(u.f.requests.length, n);
  assert.equal(u.controller.handlers.length, 0); assert.ok(u.f.requests.every(x => !/cancel|stop/.test(x.url)));
});
test("close during an awaited status discards results and cannot launch later work", async () => {
  const u = ui(); await u.controller.init(); let resolve;
  u.f.override = () => new Promise(r => { resolve = r; });
  const pending = u.controller.tick(); const before = u.el("status-text").textContent;
  u.controller.close(); resolve({ status: 200, data: { active: true, view: null, text: "late" } }); await pending;
  assert.equal(u.el("status-text").textContent, before); assert.equal(u.intervals.size, 0);
});
test("changing endpoint clears old effort metadata but preserves a manually selected effort", async () => {
  const u = ui(); await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-test-model"], source: "provider_api", notice: "old endpoint",
    details: { "glm-test-model": { effort_levels: ["high", "max"], default_effort: "high" } } };
  await u.controller.modelAction("qc", "models"); await u.controller.tick();
  await clickOption(u, "qc-effort", "max");
  u.el("qc-base-url").value = "https://new.example/v1"; await u.el("qc-base-url").emit("input");
  assert.deepEqual(u.el("qc-effort-options").children.map(x => x.textContent), ["default"]);
  assert.equal(u.el("qc-effort").value, "max");
  assert.ok(!u.el("qc-effort-notice").textContent.includes("接口元数据"));
  u.controller.close();
});
test("stage duration handles unknown and invalid values", () => {
  assert.equal(Panel.duration(null), "—"); assert.equal(Panel.duration(NaN), "—"); assert.equal(Panel.duration(3661), "1时01分01秒");
});
test("expired operation releases controls and displays the bridge explanation", async () => {
  const u = ui(); await u.controller.init(); await u.controller.previewScope([]);
  u.f.override = r => new URL(r.url).pathname.includes("/operations/")
    ? { status: 400, data: { error: "操作不存在或已过期，请重新预览" } } : undefined;
  await u.controller.tick(); assert.equal(u.controller.pending, null); assert.equal(u.controller.connected, true);
  assert.equal(u.el("discover").disabled, false); assert.ok(u.el("message").textContent.includes("已过期")); u.controller.close();
});

test("real HTTP status business failures preserve connection, settings and pending operations", async () => {
  for (const status of [400, 422, 500]) {
    const u = ui(); await u.controller.init(); await u.controller.modelAction("qc", "test");
    u.f.operationResult = { ok: true };
    u.f.override = r => new URL(r.url).pathname === "/v1/status"
      ? { status, data: { error: "文献 citekey 路径无效 " + TOKEN } } : undefined;
    await u.controller.tick();
    assert.equal(u.controller.connected, true); assert.equal(u.controller.statusKnown, false);
    assert.equal(u.el("save-config").disabled, false); assert.equal(u.el("qc-test").disabled, false);
    assert.equal(u.el("discover").disabled, true); assert.match(u.el("discover-reason").textContent, /状态/);
    assert.match(u.el("connection").textContent, /已连接/); assert.doesNotMatch(u.el("connection").textContent, /中断/);
    assert.match(u.el("status-error").textContent, /citekey 路径无效/);
    assert.ok(!u.el("status-error").textContent.includes(TOKEN));
    assert.match(u.el("model-result").textContent, /"ok": true/);
    assert.equal(u.controller.pending, null);
    u.f.override = undefined; await u.controller.tick();
    assert.equal(u.controller.statusKnown, true); assert.equal(u.el("status-error").hidden, true);
    u.controller.close();
  }
});

test("initial status 400 is reachable and does not launch an extra service", async () => {
  const f = fixture(); f.override = r => new URL(r.url).pathname === "/v1/status"
    ? { status: 400, data: { error: "status business failure" } } : undefined;
  const u = ui(f); await u.controller.init();
  assert.equal(u.controller.connected, true); assert.equal(u.controller.statusKnown, false);
  assert.equal(u.controller.credentialsVerified, true); assert.equal(f.launches.length, 0);
  assert.match(u.el("status-error").textContent, /status business failure/);
  await u.controller.connect(true); assert.equal(f.launches.length, 0);
  u.controller.close();
});

test("malformed status keeps connection and stale task details, while 401/403 stop polling", async () => {
  const u = ui(); await u.controller.init();
  u.f.override = r => new URL(r.url).pathname === "/v1/status" ? { status: 200, data: { active: "false" } } : undefined;
  await u.controller.tick(); assert.equal(u.controller.connected, true); assert.equal(u.controller.statusKnown, false);
  assert.match(u.el("status-error").textContent, /PROTOCOL/); u.controller.close();
  for (const status of [401, 403]) {
    const v = ui(); await v.controller.init();
    v.f.override = () => ({ status, data: { error: "synthetic unauthorized" } });
    const before = v.f.requests.length; await v.controller.tick();
    assert.equal(v.controller.connected, false); assert.match(v.el("connection").textContent, /鉴权失败/);
    assert.equal(v.f.requests.length - before, 2);
    await v.controller.tick(); assert.equal(v.f.requests.length - before, 2);
    assert.equal(v.f.launches.length, 0); v.controller.close();
  }
});

test("disabled primary actions have adjacent explanations and submitted start only opens tasks", async () => {
  const u = ui(); await u.controller.init();
  for (const id of ["preview", "start", "pause", "resume", "resume-finalize", "current-task"]) {
    assert.equal(u.el(id).disabled, true); assert.ok(u.el(id + "-reason").textContent.length > 0);
    assert.equal(u.el(id + "-reason").hidden, false);
    assert.equal(u.el(id).attrs["aria-describedby"], id + "-reason");
  }
  await u.controller.previewScope([]); await u.controller.tick(); await u.el("start").emit("click");
  assert.equal(u.controller.page, "task");
  u.controller.showPage("settings"); u.controller.configDirty = true; u.controller.update();
  const count = u.f.posts("/v1/start").length;
  await u.el("start").emit("click");
  assert.equal(u.controller.page, "task"); assert.equal(u.f.posts("/v1/start").length, count);
  assert.equal(u.el("start").textContent, "查看当前任务"); u.controller.close();
});

function taskFixture(view, active = false) {
  const f = fixture(); f.state = { active, text: "synthetic current", view: structuredClone(view) };
  const capabilities = ["keys-v1", "mineru-v1", "translation-policy-v1", "credential-reveal-v1", "model-profiles-v2", "model-test-role-v1", "pause-resume-v1", "history-v1", "item-selection-v1"];
  f.history = [structuredClone(view)];
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities, ...structuredClone(f.state) } };
    if (path === "/v1/history") return { status: 200, data: { runs: f.history, active: f.state.active, capabilities: [] } };
    if (path.startsWith("/v1/status/")) {
      const id = path.slice(11), found = f.state.view?.id === id ? f.state.view : f.history.find(v => v.id === id);
      return { status: 200, data: { active: f.state.active, view: structuredClone(found), text: "synthetic detail" } };
    }
    if (path === "/v1/pause") {
      f.state.view = { ...f.state.view, status: "pause_requested", pause_requested: true, can_pause: false, safe_to_shutdown: false };
      return { status: 200, data: { run_id: view.id, status: "pause_requested", message: "请求已保存，等待步骤结束" } };
    }
    if (path === "/v1/resume") {
      f.state = { active: true, text: "resuming", view: { ...view, id: RUN + "_resume", status: "running", can_resume: false } };
      return { status: 200, data: { run_id: f.state.view.id, started: true, joined: false, resume_of: view.id } };
    }
  };
  return f;
}

test("history rows open exact IDs without starting or changing current controller ownership", async () => {
  const historical = { id: RUN + "_old", status: "failed", phase: "git", report: "old-report.md", can_resume_finalize: true };
  const f = taskFixture({ id: RUN, status: "running", can_pause: true }, true); f.history = [historical];
  const u = ui(f); await u.controller.init();
  await u.el("nav-task").emit("click");
  await u.el("history-rows").children[0].children[3].children[0].emit("click");
  assert.equal(u.controller.selectedRun, historical.id); assert.equal(u.controller.active, true);
  assert.equal(u.el("status-title").textContent, "历史任务"); assert.match(u.el("report-path").textContent, /old-report/);
  assert.equal(u.el("resume-finalize").disabled, true); assert.match(u.el("resume-finalize-reason").textContent, /已有任务/);
  u.controller.message("配置保存结果"); await u.controller.tick();
  assert.equal(u.el("message").textContent, "配置保存结果"); assert.equal(u.controller.selectedRun, historical.id);
  await u.el("current-task").emit("click"); assert.equal(u.controller.selectedRun, null);
  assert.equal(u.controller.displayedStatus.view.id, RUN); assert.equal(u.el("pause").disabled, false);
  assert.equal(f.posts("/v1/start").length, 0); assert.equal(f.posts("/v1/resume").length, 0);
  u.controller.close();
});

test("missing legacy history endpoint keeps current status and explains limited capability", async () => {
  const f = fixture(); f.override = r => new URL(r.url).pathname === "/v1/history" ? { status: 404, data: {} } : undefined;
  const u = ui(f); await u.controller.init();
  await u.el("nav-task").emit("click");
  assert.equal(u.controller.connected, true); assert.equal(u.controller.statusKnown, true);
  assert.match(u.el("history-notice").textContent, /暂未提供/); assert.equal(u.el("discover").disabled, false);
  u.controller.close();
});

test("history failures keep previous rows and cannot overwrite settings feedback", async () => {
  const f = taskFixture({ id: RUN, status: "completed" }), u = ui(f); await u.controller.init();
  await u.el("nav-task").emit("click");
  const row = u.el("history-rows").children[0], override = f.override;
  f.override = r => new URL(r.url).pathname === "/v1/history" ? { status: 500, data: { error: "history unavailable" } } : override(r);
  u.controller.message("设置已保存"); await u.el("refresh-history").emit("click");
  assert.equal(u.controller.connected, true); assert.equal(u.el("history-rows").children[0], row);
  assert.equal(u.el("message").textContent, "设置已保存"); assert.match(u.el("history-notice").textContent, /history unavailable/);
  u.controller.close();
});

test("pause requires click and confirmation; requested pause never claims safe shutdown", async () => {
  const f = taskFixture({ id: RUN, status: "running", can_pause: true, safe_to_shutdown: false }, true), u = ui(f);
  await u.controller.init(); await u.controller.tick(); assert.equal(f.posts("/v1/pause").length, 0);
  u.window.confirmResult = false; await u.el("pause").emit("click"); assert.equal(f.posts("/v1/pause").length, 0);
  u.window.confirmResult = true; await u.el("pause").emit("click");
  assert.equal(f.posts("/v1/pause").length, 1); assert.equal(u.el("pause").disabled, true);
  assert.equal(u.el("resume").disabled, true); assert.match(u.el("task-notice").textContent, /等待安全边界/);
  assert.match(u.el("task-notice").textContent, /整篇 QC/); assert.doesNotMatch(u.el("task-notice").textContent, /已安全暂停/);
  await u.el("pause").emit("click"); await u.controller.tick(); assert.equal(f.posts("/v1/pause").length, 1);
  f.state = { ...f.state, active: false, view: { ...f.state.view, status: "paused", pause_requested: true, can_resume: true, safe_to_shutdown: false } };
  await u.controller.tick(); assert.equal(u.el("resume").disabled, false); assert.match(u.el("task-notice").textContent, /尚未收到可关机确认/);
  f.state.view.safe_to_shutdown = true; await u.controller.tick(); assert.match(u.el("task-notice").textContent, /已安全暂停/);
  assert.equal(f.posts("/v1/resume").length, 0); u.controller.close();
});

test("resume respects receipt flags; legacy finalize-only sends true and ignores draft config", async () => {
  for (const finalize of [false, true]) {
    const view = { id: RUN, status: finalize ? "failed" : "paused", can_resume: !finalize, can_resume_finalize: finalize,
      model_config: { qc: { provider: "glm", model: "frozen-qc", reasoning_effort: "high", timeout: 120, max_tokens: 2048 } } };
    const f = taskFixture(view), u = ui(f); await u.controller.init();
    assert.equal(u.el(finalize ? "resume" : "resume-finalize").disabled, true);
    assert.match(u.el("frozen-config").textContent, /frozen-qc/);
    u.el("qc-model").value = "next-run-model"; await u.el("qc-model").emit("input");
    const action = finalize ? "resume-finalize" : "resume";
    u.window.confirmResult = false; await u.el(action).emit("click"); assert.equal(f.posts("/v1/resume").length, 0);
    u.window.confirmResult = true; await u.el(action).emit("click");
    assert.deepEqual(JSON.parse(f.posts("/v1/resume")[0].body), { run_id: RUN, confirm: true, finalize_only: finalize });
    assert.equal(u.controller.page, "task"); assert.equal(u.el("qc-model").value, "next-run-model");
    assert.equal(f.posts("/v1/config").length, 0); assert.equal(f.posts("/v1/start").length, 0);
    await u.controller.tick(); assert.equal(f.posts("/v1/resume").length, 1);
    u.controller.close();
  }
});

test("legacy runs without receipt cannot resume and backend action reasons stay visible", async () => {
  const f = taskFixture({ id: RUN, status: "interrupted", can_resume: false, can_resume_finalize: false,
    actions: { resume: "缺少文件 receipt", resume_finalize: { reason: "收尾基线不可核实" } } });
  const u = ui(f); await u.controller.init();
  assert.match(u.el("resume-reason").textContent, /缺少文件 receipt/);
  assert.equal(u.el("resume").hidden, true);
  assert.match(u.el("resume-finalize-reason").textContent, /收尾基线/);
  await u.controller.runAction("resume"); await u.el("resume-finalize").emit("click");
  assert.equal(f.posts("/v1/resume").length, 0); u.controller.close();
});

test("pause rechecks ownership and refuses a changed current controller", async () => {
  const f = taskFixture({ id: RUN, status: "running", can_pause: true }, true), u = ui(f); await u.controller.init();
  f.state.view = { id: RUN + "_new", status: "running", can_pause: true };
  await u.el("pause").emit("click");
  assert.equal(f.posts("/v1/pause").length, 0); assert.match(u.el("message").textContent, /当前任务已改变/);
  u.controller.close();
});

test("preview summary separates discovered, executable and deferred with visible reasons", async () => {
  const u = ui(); await u.controller.init(true);
  u.f.preview = { preview_id: RUN, text: "发现 2 篇", items: [{ key: "ABCDEFG2", citekey: "Sample2026", title: "English" }],
    deferred: [{ key: "9UTJAVFW", citekey: "Gallmeier2023GeoscientificModelDevelopment-EvaluationLLC4320",
      title: "Evaluation of LLC4320",
      blocked: ["IntakeBlocked: missing/unmapped publicationTitle: 'Geoscientific Model Development'"] }] };
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(u.el("deferred-notice").hidden, false);
  assert.match(u.el("deferred-notice").textContent, /9UTJAVFW/);
  assert.match(u.el("deferred-notice").textContent, /Geoscientific Model Development/);
  assert.match(u.el("deferred-notice").textContent, /暂缓条目/);
  assert.match(u.el("selection-notice").textContent, /发现 2 篇：可执行 1 篇、暂缓 1 篇/);
  assert.match(u.el("operation").textContent, /发现 2 篇 · 可执行 1 篇 · 暂缓 1 篇/);
  assert.match(u.el("message").textContent, /暂缓 1 篇/);
  await u.el("workers").emit("input");
  assert.equal(u.el("deferred-notice").hidden, true);
  assert.equal(u.el("deferred-notice").textContent, "");
  u.controller.close();
});

test("zero deferred previews keep the prominent notice area hidden", async () => {
  const u = ui(); await u.controller.init(true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(u.el("deferred-notice").hidden, true);
  assert.equal(u.el("deferred-notice").textContent, "");
  assert.match(u.el("selection-notice").textContent, /暂缓 0 篇/);
  u.controller.close();
});

test("explicit blocked-only items count as deferred and keep start disabled", async () => {
  const u = ui(); await u.controller.init(true);
  u.f.preview = { preview_id: RUN, text: "发现 1 篇", items: [{ key: "ABCDEFG2", citekey: "Broken2026-EN",
    title: "Corrupt fulltext item", blocked: ["文献目录有未提交原文但无法读取或解析（ScannerError）"] }], deferred: [] };
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.match(u.el("operation").textContent, /发现 1 篇 · 可执行 0 篇 · 暂缓 1 篇/);
  assert.match(u.el("selection-notice").textContent, /发现 1 篇：可执行 0 篇、暂缓 1 篇/);
  assert.equal(u.el("deferred-notice").hidden, false);
  assert.match(u.el("deferred-notice").textContent, /Broken2026-EN（ABCDEFG2） Corrupt fulltext item/);
  assert.match(u.el("deferred-notice").textContent, /无法读取或解析/);
  assert.equal(u.el("preview-rows").children.length, 1);  // no duplicated table row
  assert.equal(u.el("start").disabled, true);
  assert.match(u.el("start-reason").textContent, /至少勾选一篇文献/);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
  u.controller.close();
});

test("automatic mixed preview merges blocked items into the deferred summary without duplicates", async () => {
  const u = ui(); await u.controller.init(true);
  u.f.preview = { preview_id: RUN, text: "发现 3 篇", items: [
    { key: "ABCDEFG2", citekey: "Ready2026-Paper", title: "Ready item" },
    { key: "ABCDEFG3", citekey: "Blocked2026-EN", title: "Blocked in items", blocked: ["无法核实为本流水线上次未完成产物"] }],
    deferred: [{ key: "9UTJAVFW", citekey: "Gallmeier2023GMD-EvaluationLLC4320",
      title: "Evaluation of LLC4320", blocked: ["超出本批上限；保留到下次确认"] }] };
  await u.controller.previewScope([]); await u.controller.tick();
  assert.match(u.el("operation").textContent, /发现 3 篇 · 可执行 1 篇 · 暂缓 2 篇/);
  assert.match(u.el("selection-notice").textContent, /发现 3 篇：可执行 1 篇、暂缓 2 篇/);
  const notice = u.el("deferred-notice").textContent;
  assert.match(notice, /Gallmeier2023GMD-EvaluationLLC4320（9UTJAVFW） Evaluation of LLC4320/);
  assert.match(notice, /Blocked2026-EN（ABCDEFG3） Blocked in items/);
  assert.equal(notice.split("\n").length, 3);  // header + two deduplicated entries
  assert.ok(!notice.includes("Ready2026"));
  assert.equal(u.el("preview-rows").children.length, 3);  // table rows unchanged: 2 items + 1 deferred
  assert.equal(u.el("start").disabled, false);
  u.controller.close();
});

test("explicit mixed ready+blocked preview counts blocked items in the confirm dialog", async () => {
  const u = ui(); await u.controller.init(true);
  u.f.preview = { preview_id: RUN, text: "发现 2 篇", items: [
    { key: "ABCDEFG2", citekey: "Ready2026-Paper", title: "Ready item" },
    { key: "ABCDEFG3", citekey: "Blocked2026-EN", title: "Blocked in items",
      blocked: ["无法核实为本流水线上次未完成产物"] }], deferred: [] };
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.match(u.el("operation").textContent, /发现 2 篇 · 可执行 1 篇 · 暂缓 1 篇/);
  assert.equal(u.el("start").disabled, false);
  u.window.confirmResult = false;
  await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 0);
  const dialog = u.window.confirms.at(-1);
  assert.match(dialog, /暂缓 1 篇/);
  assert.ok(!/暂缓 0 篇/.test(dialog));
  assert.match(dialog, /本次选择 1 篇/);
  u.controller.close();
});

test("overlapping items and deferred arrays deduplicate counts and notice by key", async () => {
  const u = ui(); await u.controller.init(true);
  const blockedItem = { key: "ABCDEFG3", citekey: "Blocked2026-EN", title: "Blocked",
    blocked: ["无法核实为本流水线上次未完成产物"] };
  u.f.preview = { preview_id: RUN, text: "重叠夹具", items: [
    { key: "ABCDEFG2", citekey: "Ready2026-Paper", title: "Ready item" }, blockedItem],
    deferred: [blockedItem, { key: "9UTJAVFW", citekey: "Gallmeier2023GMD-EvaluationLLC4320",
      title: "Evaluation of LLC4320", blocked: ["超出本批上限；保留到下次确认"] }] };
  await u.controller.previewScope([]); await u.controller.tick();
  assert.match(u.el("operation").textContent, /发现 3 篇 · 可执行 1 篇 · 暂缓 2 篇/);
  assert.match(u.el("selection-notice").textContent, /发现 3 篇：可执行 1 篇、暂缓 2 篇/);
  const notice = u.el("deferred-notice").textContent;
  assert.equal(notice.split("\n").length, 3);  // header + two unique entries
  assert.equal(notice.split("Blocked2026-EN").length - 1, 1);  // listed exactly once
  assert.ok(!notice.includes("Ready2026"));
  u.controller.close();
});

test("per-paper include and translation are frozen as separate selections and original plan survives", async () => {
  const u = ui(); await u.controller.init();
  u.f.preview.items = [{ key: "ABCDEFG2", title: "English", require_translation: true, actions: [{ action: "copy-original" }] },
    { key: "ABCDEFG3", title: "中文原文", require_translation: false }, { key: "ABCDEFG4", title: "Later", require_translation: true }];
  await u.controller.previewScope([]); await u.controller.tick();
  const original = JSON.stringify(u.controller.preview);
  Object.freeze(u.controller.preview); u.controller.preview.items.forEach(Object.freeze);
  const control = (key, field) => u.controller.selectionControls.find(input => input.selectionKey === key && input.selectionField === field);
  assert.equal(control("ABCDEFG3", "translate").disabled, true);
  assert.match(u.el("preview-rows").textContent, /无需翻译/); assert.match(u.el("preview-rows").textContent, /copy-original/);
  control("ABCDEFG2", "translate").checked = false; await control("ABCDEFG2", "translate").emit("change");
  control("ABCDEFG4", "include").checked = false; await control("ABCDEFG4", "include").emit("change");
  assert.equal(control("ABCDEFG4", "translate").disabled, true);
  assert.equal(JSON.stringify(u.controller.preview), original);
  await u.el("start").emit("click");
  assert.deepEqual(JSON.parse(u.f.posts("/v1/start")[0].body).selections, [
    { key: "ABCDEFG2", include: true, translate: false }, { key: "ABCDEFG3", include: true, translate: false },
    { key: "ABCDEFG4", include: false, translate: true }]);
  assert.equal(JSON.stringify(u.controller.preview), original); u.controller.close();
});

test("zero included papers blocks start with an explanation and sends nothing", async () => {
  const u = ui(); await u.controller.init(); await u.controller.previewScope([]); await u.controller.tick();
  const input = u.controller.selectionControls.find(input => input.selectionField === "include");
  input.checked = false; await input.emit("change");
  assert.equal(u.el("start").disabled, true); assert.match(u.el("start-reason").textContent, /至少勾选/);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});

test("unchanged polling preserves table nodes and selected task text", async () => {
  const f = taskFixture({ id: RUN, status: "running", papers: [{ key: "ABCDEFG2", title: "Sample", accepted: false }] }, true);
  const u = ui(f); await u.controller.init(); const row = u.el("status-rows").children[0];
  await u.controller.tick(); assert.equal(u.el("status-rows").children[0], row);
  u.window.getSelection = () => ({ isCollapsed: false, containsNode: el => el === u.el("status-rows") });
  f.state.view.papers[0].title = "updated"; await u.controller.tick();
  assert.equal(u.el("status-rows").children[0], row);
  u.window.getSelection = () => ({ isCollapsed: true }); await u.controller.tick();
  assert.match(u.el("status-rows").textContent, /updated/); u.controller.close();
});

test("history reads occur only on task entry or explicit refresh, never on polling", async () => {
  const u = ui(); await u.controller.init();
  const reads = () => u.f.requests.filter(r => new URL(r.url).pathname === "/v1/history").length;
  assert.equal(reads(), 0); await u.controller.tick(); assert.equal(reads(), 0);
  await u.el("nav-task").emit("click"); assert.equal(reads(), 1);
  await u.controller.tick(); await u.controller.tick(); assert.equal(reads(), 1);
  await u.el("nav-task").emit("click"); assert.equal(reads(), 1);
  await u.el("refresh-history").emit("click"); assert.equal(reads(), 2);
  await u.el("nav-settings").emit("click"); await u.el("nav-task").emit("click"); assert.equal(reads(), 3);
  u.controller.close();
});

test("resumed_as blocks duplicate resume and provides a read-only new-task link", async () => {
  const old = { id: RUN, status: "paused", can_resume: true, can_resume_finalize: true, resumed_as: RUN + "_resume" };
  const f = taskFixture(old); f.history.push({ id: old.resumed_as, status: "completed" });
  const u = ui(f); await u.controller.init();
  assert.equal(u.el("resume").disabled, true); assert.equal(u.el("resume-finalize").disabled, true);
  assert.match(u.el("task-notice").textContent, /已派发恢复/); assert.equal(u.el("view-resumed").hidden, false);
  await u.el("view-resumed").emit("click");
  assert.equal(u.controller.selectedRun, old.resumed_as); assert.equal(f.posts("/v1/resume").length, 0);
  u.controller.close();
});

test("an old in-flight poll cannot overwrite the refreshed state from a click", async () => {
  const f = taskFixture({ id: RUN, status: "running", can_pause: true }, true), u = ui(f); await u.controller.init();
  let release; const fallback = f.override;
  f.override = r => {
    if (new URL(r.url).pathname === "/v1/status" && !release) return new Promise(resolve => { release = resolve; });
    return fallback(r);
  };
  const polling = u.controller.tick();
  f.state = { active: false, text: "fresh", view: { id: RUN, status: "paused", can_resume: true, safe_to_shutdown: true } };
  await u.controller.connect(false);
  release({ status: 200, data: { active: true, text: "stale", view: { id: RUN, status: "running", can_pause: true } } });
  await polling;
  assert.equal(u.controller.active, false); assert.equal(u.controller.currentStatus.text, "fresh");
  assert.equal(u.el("resume").disabled, false); u.controller.close();
});

test("saved and tested states are distinct; changing credentials or reloading another profile invalidates the test", async () => {
  const u = ui(); await u.controller.init(); u.f.operationResult = { ok: true };
  await u.el("qc-test").emit("click"); await u.controller.tick();
  assert.match(u.el("qc-test-state").textContent, /测试通过/);
  assert.ok(u.controller.testStates.qc.at);
  u.f.publicConfig.config.qc.model = "different-model"; await u.controller.reloadConfig();
  assert.match(u.el("qc-test-state").textContent, /尚未测试/);
  await u.el("qc-test").emit("click"); await u.controller.tick();
  u.el("glm-key").value = "synthetic-new-credential"; await u.el("glm-key").emit("input");
  assert.match(u.el("qc-test-state").textContent, /Key 已修改/);
  assert.equal(u.controller.testStates.qc, undefined); u.controller.close();
});

test("manual refresh supersedes a late task-entry history read without losing the new list", async () => {
  const u = ui(); await u.controller.init(); let release;
  u.f.override = r => {
    if (new URL(r.url).pathname !== "/v1/history") return;
    if (!release) return new Promise(resolve => { release = resolve; });
    return { status: 200, data: { runs: [{ id: RUN, status: "paused" }], capabilities: [] } };
  };
  const entering = u.el("nav-task").emit("click");
  await u.el("refresh-history").emit("click");
  release({ status: 200, data: { runs: [], capabilities: [] } }); await entering;
  assert.equal(u.controller.history.length, 1);
  assert.equal(u.el("history-rows").children.length, 1); u.controller.close();
});

test("current status errors while viewing history use the current run's report", async () => {
  const f = taskFixture({ id: RUN, status: "running", report: "current-report.md" }, true);
  const old = { id: RUN + "_old", status: "failed", report: "unrelated-old-report.md" }; f.history = [old];
  const u = ui(f); await u.controller.init(); await u.controller.viewHistory(old.id);
  const fallback = f.override;
  f.override = r => new URL(r.url).pathname === "/v1/status"
    ? { status: 400, data: { error: "current status failed" } } : fallback(r);
  await u.controller.tick();
  assert.match(u.el("status-error").textContent, /current-report\.md/);
  assert.doesNotMatch(u.el("status-error").textContent, /unrelated-old-report/);
  assert.match(u.el("report-path").textContent, /unrelated-old-report/);
  u.controller.close();
});

test("blocked preflight child with can_resume true allows a manual retry of its original paused source", async () => {
  const source = RUN, child = RUN + "_failed", next = RUN + "_retry";
  const f = taskFixture({ id: child, status: "blocked", phase: "preflight", terminal: true, can_resume: true,
    can_resume_finalize: false, safe_to_shutdown: false, worker_resume_of: source, papers: [] });
  const fallback = f.override;
  f.override = r => {
    if (new URL(r.url).pathname !== "/v1/resume") return fallback(r);
    f.state = { active: true, text: "new attempt", view: { id: next, status: "running", can_pause: true, can_resume: false } };
    return { status: 200, data: { run_id: next, started: true, resume_of: source } };
  };
  const u = ui(f); await u.controller.init(); await u.controller.tick();
  assert.equal(u.el("resume").disabled, false); assert.equal(u.el("resume").hidden, false);
  assert.equal(f.posts("/v1/resume").length, 0); assert.doesNotMatch(u.el("task-notice").textContent, /已安全暂停/);
  u.window.confirmResult = false; await u.el("resume").emit("click"); assert.equal(f.posts("/v1/resume").length, 0);
  u.window.confirmResult = true; await u.el("resume").emit("click");
  assert.deepEqual(JSON.parse(f.posts("/v1/resume")[0].body), { run_id: child, confirm: true, finalize_only: false });
  assert.equal(u.controller.startResult.run_id, next); assert.equal(u.controller.startResult.resume_of, source);
  assert.equal(u.controller.currentStatus.view.id, next); assert.equal(u.controller.page, "task");
  assert.ok(u.el("summary").textContent.includes(next)); assert.equal(u.el("message").hidden, true);
  await u.controller.tick(); assert.equal(f.posts("/v1/resume").length, 1); u.controller.close();
});

test("old backend capabilities disable all task writes with explicit passive upgrade guidance", async () => {
  for (const active of [false, true]) {
    const f = taskFixture({ id: RUN, status: active ? "running" : "blocked", can_pause: true, can_resume: true, can_resume_finalize: true }, active);
    f.state.capabilities = ["keys-v1", "mineru-v1", "translation-policy-v1", "model-profiles-v2", "model-test-role-v1"];
    const u = ui(f); await u.controller.init();
    for (const action of ["pause", "resume", "resume-finalize"]) {
      assert.equal(u.el(action).disabled, true);
      assert.match(u.el(action + "-reason").textContent, /后台组件版本旧/);
      assert.match(u.el(action + "-reason").textContent, /关闭面板.*30 分钟.*重启电脑/);
      await u.el(action).emit("click"); await u.controller.runAction(action);
    }
    await u.controller.tick();
    assert.equal(f.posts("/v1/pause").length, 0); assert.equal(f.posts("/v1/resume").length, 0);
    assert.equal(f.launches.length, 0); assert.equal(u.window.confirms.length, 0);
    delete f.state.capabilities; await u.controller.tick();
    assert.equal(u.el(active ? "pause" : "resume").disabled, false);
    u.controller.close(); assert.ok(f.requests.every(r => !/stop|kill|terminate|shutdown/.test(r.url)));
  }
});

test("blocked tasks state the controller error and affected papers, with a redacted copy-diagnostics action", async () => {
  const view = { id: RUN, status: "blocked", status_text: "已阻断", phase: "preflight", phase_label: "预检",
    error: "RuntimeError: 待更换文献有预存未提交改动（合成场景）", papers: [{ key: "ABCDEFG2", citekey: "Sample2026",
      title: "示例", phase: "preflight", phase_label: "预检", accepted: false, note: "未验收说明 " + TOKEN }],
    notices: [], report: "C:\\synthetic reports\\blocked.md", accepted: 0, total: 1 };
  const f = taskFixture(view, false);
  const u = ui(f); await u.controller.init(); await u.controller.tick();
  assert.match(u.el("task-notice").textContent,
    /任务需要处理：已阻断。原因：RuntimeError: 待更换文献有预存未提交改动（合成场景）/);
  assert.equal(u.el("copy-status").hidden, false);
  const copies = []; u.host.copyText = text => { copies.push(text); return true; };
  await u.el("copy-status").emit("click");
  assert.equal(copies.length, 1);
  assert.match(copies[0], new RegExp("Library Sync " + Bridge.VERSION + " 诊断"));
  assert.match(copies[0], new RegExp(RUN));
  assert.match(copies[0], /Sample2026/);
  assert.match(copies[0], /blocked\.md/);
  assert.ok(!copies[0].includes(TOKEN), "diagnostics must be redacted before the clipboard");
  assert.match(u.el("message").textContent, /已复制脱敏诊断/);
  u.host.copyText = () => false;
  await u.el("copy-status").emit("click");
  assert.match(u.el("message").textContent, /复制失败.*手动复制/);
  u.controller.close();
});

test("copy diagnostics stays hidden without a status and appears for status read failures", async () => {
  const f = fixture(); f.override = r => new URL(r.url).pathname === "/v1/status"
    ? { status: 400, data: { error: "status business failure" } } : undefined;
  const u = ui(f);
  assert.equal(u.el("copy-status").hidden, true);
  await u.controller.init();
  assert.equal(u.el("copy-status").hidden, false);
  assert.match(u.el("status-error").textContent, /status business failure/);
  const copies = []; u.host.copyText = text => { copies.push(text); return true; };
  await u.el("copy-status").emit("click");
  assert.equal(copies.length, 1);
  assert.match(copies[0], /status business failure/);
  assert.ok(!copies[0].includes(TOKEN));
  assert.equal(f.launches.length, 0); u.controller.close();
});
