"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { Bridge, OP, RUN, PATHS, TOKEN, MANIFEST, fixture } = require("./helpers.cjs");

test("missing installation preserves the actionable path error without launch retries", async () => {
  const f = fixture(); f.manifest = null; let attempts = 0;
  f.io.launch = async () => { attempts++; throw new Bridge.SafeError("PATH", "后台程序目录中没有 tools\\library_plugin_bridge.py"); };
  await assert.rejects(f.client.connect(true), e => e.code === "PATH" && e.message.includes("library_plugin_bridge.py"));
  assert.equal(attempts, 1); assert.equal(f.requests.length, 0); assert.equal(f.sleeps.length, 0);
});

test("history and per-run status use fixed path segments with no query or traversal", async () => {
  const f = fixture(), view = { id: RUN, status: "paused", phase: "qc", started: "synthetic-time", finished: null,
    can_pause: false, can_resume: true, can_resume_finalize: false, safe_to_shutdown: true, report: "fixture.md", model_config: {} };
  f.override = r => ({ status: 200, data: new URL(r.url).pathname === "/v1/history"
    ? { active: false, runs: [view], capabilities: ["history-v1"] } : { active: false, view, text: "paused" } });
  assert.deepEqual((await f.client.getHistory()).runs, [view]);
  assert.deepEqual((await f.client.getStatus(RUN)).view, view);
  assert.equal(new URL(f.requests.at(-1).url).pathname, "/v1/status/" + RUN);
  assert.ok(f.requests.every(r => r.method === "GET" && !r.url.includes("?")));
  const before = f.requests.length;
  for (const id of ["../x", "a/b", "a?x=y", "a#b", "a%2fb", "", null, 1]) {
    if (id !== null) await assert.rejects(f.client.getStatus(id), { code: "INPUT" });
  }
  for (const route of ["/v1/status?run_id=" + RUN, "/v1/status/../x", "/v1/status/" + RUN + "?x=y", "/v1/history?token=x"]) {
    await assert.rejects(f.client.request("GET", route), { code: "ROUTE" });
  }
  assert.equal(f.requests.length, before);
});

test("history shape and selected run identity are validated before display", async () => {
  for (const data of [{ runs: [] }, { active: "false", runs: [], capabilities: [] },
    { active: false, runs: [{ id: "../x", status: "paused" }], capabilities: [] },
    { active: false, runs: [{ id: RUN, status: "paused" }, { id: RUN, status: "paused" }], capabilities: [] }]) {
    const f = fixture(); f.override = () => ({ status: 200, data });
    await assert.rejects(f.client.getHistory(), { code: "PROTOCOL" });
  }
  const f = fixture(); f.override = () => ({ status: 200, data: { active: false, text: "wrong", view: { id: "other" } } });
  await assert.rejects(f.client.getStatus(RUN), { code: "PROTOCOL" });
});

test("business, expired-operation, server, finalization and transport failures have separate classifications", async () => {
  for (const [route, status, data, category] of [
    ["/v1/status", 400, { error: "文献 citekey 路径无效 " + TOKEN }, "task"],
    ["/v1/config", 422, { error: "invalid model" }, "validation"],
    ["/v1/operations/" + OP, 404, { error: "expired" }, "operation"],
    ["/v1/history", 503, { error: "try later" }, "server"],
    ["/v1/status", 400, { error: "finalize failed", error_code: "FINALIZE_FAILED" }, "finalize"]]) {
    const f = fixture(); f.override = () => ({ status, data });
    await assert.rejects(f.client.request("GET", route), e => e.code === "HTTP" && e.httpStatus === status &&
      Bridge.errorCategory(e) === category && !e.message.includes(TOKEN));
    assert.equal(f.requests.length, 1); assert.equal(f.launches.length, 0);
  }
  const f = fixture(); f.online = false;
  await assert.rejects(f.client.getStatus(), e => Bridge.errorCategory(e) === "connection");
});

test("pause and resume send explicit confirmation and strict finalize-only booleans", async () => {
  const f = fixture();
  f.override = r => ({ status: 200, data: new URL(r.url).pathname === "/v1/pause"
    ? { run_id: RUN, status: "pause_requested", message: "wait for step" }
    : { run_id: RUN + "_r", started: true, joined: false, resume_of: RUN } });
  assert.equal((await f.client.pause(RUN)).status, "pause_requested");
  for (const finalizeOnly of [false, true]) await f.client.resume(RUN, finalizeOnly);
  assert.deepEqual(JSON.parse(f.posts("/v1/pause")[0].body), { run_id: RUN, confirm: true });
  assert.deepEqual(f.posts("/v1/resume").map(r => JSON.parse(r.body)), [false, true].map(finalize_only => ({ run_id: RUN, confirm: true, finalize_only })));
  const count = f.requests.length;
  await assert.rejects(f.client.resume(RUN, "false"), { code: "INPUT" });
  await assert.rejects(f.client.pause("../bad"), { code: "INPUT" });
  assert.equal(f.requests.length, count);
});

test("malformed pause/resume responses and uncertain transport never trigger automatic writes", async () => {
  for (const data of [{ run_id: RUN, status: "stopped", message: "bad" }, { run_id: "other", status: "paused", message: "bad" }]) {
    const f = fixture(); f.override = () => ({ status: 200, data });
    await assert.rejects(f.client.pause(RUN), { code: "PROTOCOL" }); assert.equal(f.requests.length, 1);
  }
  const f = fixture(); f.override = () => ({ status: 200, data: { run_id: RUN, started: true, joined: true, resume_of: "../invalid" } });
  await assert.rejects(f.client.resume(RUN), { code: "PROTOCOL" });
  f.override = () => { throw new Error("synthetic network uncertainty"); };
  await assert.rejects(f.client.pause(RUN), { code: "NETWORK" });
  await assert.rejects(f.client.resume(RUN, true), { code: "NETWORK" });
  assert.equal(f.requests.length, 3);
});

test("backend resume variants normalize optional joined without claiming an unrelated run is a recovery", async () => {
  const f = fixture();
  for (const data of [{ run_id: RUN + "_r", started: true, resume_of: RUN },
    { run_id: RUN + "_r", started: false, already_submitted: true, resume_of: RUN },
    { run_id: "other_active", started: false, joined: true },
    { run_id: RUN + "_r", started: false, already_submitted: true }]) {
    f.override = () => ({ status: 200, data });
    const result = await f.client.resume(RUN);
    assert.equal(result.joined, !data.started); assert.equal(result.resume_of, data.resume_of);
  }
  f.override = () => ({ status: 200, data: { runs: [], capabilities: [] } });
  assert.deepEqual((await f.client.getHistory()).runs, []);
});

test("failed preflight child retries a valid original source and preserves the actual new run ID", async () => {
  const f = fixture(), child = RUN + "_failed_child", source = RUN, next = RUN + "_retry";
  f.override = () => ({ status: 200, data: { run_id: next, started: true, resume_of: source } });
  const result = await f.client.resume(child);
  assert.deepEqual(JSON.parse(f.posts("/v1/resume")[0].body), { run_id: child, confirm: true, finalize_only: false });
  assert.deepEqual(result, { run_id: next, started: true, resume_of: source, joined: false });
  assert.equal(f.posts("/v1/resume").length, 1);
  for (const resume_of of ["", "../parent", "parent?x=1", null, 42]) {
    f.override = () => ({ status: 200, data: { run_id: next, started: true, resume_of } });
    await assert.rejects(f.client.resume(child), { code: "PROTOCOL" });
  }
});

test("start selections retain excluded and source-only entries without mutating the caller", async () => {
  const f = fixture(), selections = Object.freeze([
    Object.freeze({ key: "ABCDEFG2", include: true, translate: false }),
    Object.freeze({ key: "ABCDEFG3", include: false, translate: true })]);
  await f.client.start(RUN, selections);
  assert.deepEqual(JSON.parse(f.posts("/v1/start")[0].body), { preview_id: RUN, confirm: true, selections });
  const count = f.requests.length;
  for (const invalid of [[], [{ key: "ABCDEFG2", include: false, translate: false }],
    [{ key: "ABCDEFG2", include: "true", translate: true }], [{ key: "cite:key", include: true, translate: false }],
    [selections[0], selections[0]], Array(26).fill(selections[0])]) {
    await assert.rejects(f.client.start(RUN, invalid), { code: "INPUT" });
  }
  assert.equal(f.requests.length, count);
});

test("local transport/redirect/response failures never trigger another bridge launch", async () => {
  for (const code of ["TRANSPORT", "REDIRECT", "RESPONSE"]) {
    const f = fixture();
    f.override = () => { throw new Bridge.SafeError(code, "fixed safe diagnostic"); };
    await assert.rejects(f.client.connect(true), { code });
    assert.equal(f.launches.length, 0); assert.equal(f.requests.length, 1);
  }
});

test("descriptor rejects external hosts, invalid ports, schema, PID and token", () => {
  for (const delta of [{ host: "localhost" }, { host: "127.0.0.1.evil" }, { host: "0.0.0.0" },
    { port: 1023 }, { port: 65536 }, { port: "1200" }, { port: true }, { schema: 2 }, { pid: 0 }, { token: "x\r\ny" }]) {
    assert.throws(() => Bridge.descriptor({ ...MANIFEST, ...delta }), { code: "DESCRIPTOR" });
  }
  assert.equal(Bridge.descriptor(MANIFEST).port, 48765);
});
test("paths support spaces and Chinese; executable must be pythonw without shell", () => {
  assert.deepEqual(Bridge.paths(PATHS), PATHS);
  for (const pythonw of ["pythonw.exe", "C:\\Python\\python.exe", "C:\\Python\\pythonw.exe --serve", "\\\\server\\pythonw.exe", '"C:\\Python\\pythonw.exe"']) {
    assert.throws(() => Bridge.paths({ ...PATHS, pythonw }), { code: "PATH" });
  }
  assert.throws(() => Bridge.paths({ ...PATHS, repo: "C:\\repo\\..\\secrets" }), { code: "PATH" });
});
test("existing server reused, header-only auth and no automatic sync", async () => {
  const f = fixture(); await f.client.connect(true);
  assert.equal(f.launches.length, 0); assert.equal(f.requests.length, 1);
  const r = f.requests[0]; assert.equal(r.method, "GET");
  assert.ok(!r.url.includes(TOKEN)); assert.equal(r.headers["X-Library-Sync-Token"], TOKEN);
  assert.equal(r.redirect, "error"); assert.equal(r.credentials, "omit");
});
test("launch uses exact argument array and requires live status, coalesces double connect", async () => {
  const f = fixture(); f.manifest = null;
  f.onLaunch = () => { f.manifest = MANIFEST; };
  await Promise.all([f.client.connect(true), f.client.connect(true)]);
  assert.deepEqual(f.launches, [{ exe: PATHS.pythonw, args: [PATHS.repo + "\\tools\\library_plugin_bridge.py", "--serve", "--apply"] }]);
  assert.equal(f.posts("/v1/start").length, 0);
});
test("read-only open never launches; failed launch handshake is bounded", async () => {
  const f = fixture(); f.manifest = null;
  await assert.rejects(f.client.connect(false), { code: "MISSING" }); assert.equal(f.launches.length, 0);
  await assert.rejects(f.client.connect(true), { code: "LAUNCH" });
  assert.equal(f.launches.length, 1); assert.equal(f.sleeps.length, 8);
});
test("invalid descriptor does not launch or send credentials", async () => {
  const f = fixture(); f.manifest.host = "attacker.invalid";
  await assert.rejects(f.client.connect(true), { code: "DESCRIPTOR" });
  assert.equal(f.requests.length, 0); assert.equal(f.launches.length, 0);
});
test("auth expiry rereads once; persistent auth failure neither loops nor relaunches", async () => {
  const f = fixture(); f.override = () => ({ status: 403, data: { error: "denied" } });
  await assert.rejects(f.client.connect(true), { code: "AUTH" });
  assert.equal(f.reads.length, 2); assert.equal(f.requests.length, 2); assert.equal(f.launches.length, 0);
  const g = fixture(); let n = 0;
  g.override = () => { if (++n === 1) { g.manifest.token = "rotated-synthetic-canary-only"; return { status: 401 }; } };
  await g.client.getStatus(); assert.equal(g.reads.length, 2);
  assert.equal(g.requests[1].headers["X-Library-Sync-Token"], g.manifest.token);
});
test("transport uncertainty never retries POST; exception details do not escape", async () => {
  const f = fixture(); f.override = () => { throw new Error(TOKEN); };
  await assert.rejects(f.client.start(RUN), e => e.code === "NETWORK" && !e.message.includes(TOKEN));
  assert.equal(f.posts("/v1/start").length, 1);
});
test("initial null config becomes defaults without blocking status", async () => {
  const f = fixture(); f.publicConfig = { configured: false, config: null, credentials: { glm: false, deepseek: false } };
  const value = await f.client.getConfig(); assert.equal(value.configured, false);
  assert.equal(value.config.qc.timeout, 1500); assert.equal(value.config.zh.max_tokens, 131072);
  assert.equal(value.config.qc.reasoning_effort, "max"); assert.equal(value.config.qc.model, "");
  assert.equal(value.config.qc.thinking, "enabled");
  assert.equal((await f.client.getStatus()).active, false);
});
test("profile bounds match backend and providers remain independent", () => {
  const c = fixture().publicConfig.config; c.qc.max_tokens = 131072; c.zh.max_tokens = 393216;
  assert.deepEqual(Bridge.config(c), c);
  for (const delta of [{ timeout: 1501 }, { timeout: 0 }, { timeout: 1.5 }, { max_tokens: 131073 },
    { model: "https://invalid.test/model" }, { model: "x\n" + "bad" }, { model: "x".repeat(257) }, { reasoning_effort: "bad effort" }]) {
    assert.throws(() => Bridge.config({ ...c, qc: { ...c.qc, ...delta } }), { code: "INPUT" });
  }
});
test("save omits empty keys and safe HTTP400 errors are redacted", async () => {
  const f = fixture(); await f.client.saveConfig(f.publicConfig.config, { glm: "", deepseek: "   " });
  assert.deepEqual(JSON.parse(f.posts("/v1/config")[0].body).keys, {});
  f.override = () => ({ status: 400, data: { error: "模型配置已变化 " + TOKEN } });
  await assert.rejects(f.client.start(RUN), e => e.message.includes("模型配置已变化") && !e.message.includes(TOKEN));
});
test("operation contracts, route allowlist and preview bounds", async () => {
  const f = fixture(); assert.equal(await f.client.preview({ keys: [], workers: 4, chunk_parallel: 2, limit: 25 }), OP);
  assert.equal((await f.client.operation(OP)).status, "completed");
  assert.equal(JSON.parse(f.posts("/v1/preview")[0].body).keys.length, 0);
  for (const delta of [{ workers: 0 }, { workers: 5 }, { chunk_parallel: 3 }, { limit: 26 }, { keys: ["bad"] }, { keys: Array(26).fill("ABCDEFG2") }]) {
    assert.throws(() => Bridge.previewBody({ keys: [], workers: 1, chunk_parallel: 1, limit: 0, ...delta }), { code: "INPUT" });
  }
  for (const route of ["/v1/status?token=x", "/v1/stop", "/v1/cancel", "https://example.invalid", "/v1/operations/../../x"]) {
    await assert.rejects(f.client.request("GET", route), { code: "ROUTE" });
  }
});
test("close sends nothing and prevents later requests", async () => {
  const f = fixture(); await f.client.getStatus(); const before = f.requests.length; f.client.close();
  await assert.rejects(f.client.getStatus(), { code: "CLOSED" }); assert.equal(f.requests.length, before);
});

test("close while reading initial descriptor never sends or restores sensitive state", async () => {
  const f = fixture(); let release;
  f.io.readJSON = () => new Promise(resolve => { release = resolve; });
  const pending = f.client.revealKey("glm");
  f.client.close(); release({ ...MANIFEST });
  await assert.rejects(pending, { code: "CREDENTIAL" });
  assert.equal(f.requests.length, 0); assert.equal(f.client.session, null);
  assert.equal(f.client.sensitive.size, 0); assert.equal(f.client.revealedSensitive.size, 0);
});

test("close during auth refresh prevents a second reveal and descriptor resurrection", async () => {
  for (const status of [401, 403]) {
    const f = fixture(); await f.client.getStatus(); let release, began;
    const refreshStarted = new Promise(resolve => { began = resolve; });
    f.io.readJSON = () => { began(); return new Promise(resolve => { release = resolve; }); };
    f.override = () => ({ status, data: { error: "denied" } });
    const pending = f.client.revealKey("glm");
    await refreshStarted; f.client.close(); release({ ...MANIFEST });
    await assert.rejects(pending, { code: "CREDENTIAL" });
    assert.equal(f.posts("/v1/credentials/reveal").length, 1);
    assert.equal(f.client.session, null); assert.equal(f.client.sensitive.size, 0);
    assert.equal(f.client.revealedSensitive.size, 0);
  }
});

test("closed client cannot retain credentials passed to save methods", async () => {
  const f = fixture(); f.client.close();
  await assert.rejects(f.client.saveKeys({ glm: "synthetic-close-only" }), { code: "CLOSED" });
  await assert.rejects(f.client.saveMineru("synthetic-close-only"), { code: "CLOSED" });
  await assert.rejects(f.client.saveConfig(f.publicConfig.config, { glm: "synthetic-close-only" }), { code: "CLOSED" });
  assert.equal(f.requests.length, 0); assert.equal(f.client.sensitive.size, 0);
});
test("MinerU save and key-only save have fixed routes and never echo keys", async () => {
  const f = fixture();
  const value = await f.client.saveMineru("synthetic-mineru-token");
  assert.deepEqual(value, { configured: true, saved: true, source: "encrypted" });
  assert.deepEqual(JSON.parse(f.posts("/v1/mineru")[0].body), { key: "synthetic-mineru-token" });
  await f.client.saveKeys({ glm: "synthetic-glm-only", deepseek: "" });
  assert.deepEqual(JSON.parse(f.posts("/v1/keys")[0].body), { keys: { glm: "synthetic-glm-only" } });
  assert.equal(f.client.redact("synthetic-mineru-token synthetic-glm-only"), "[已隐藏] [已隐藏]");
  for (const key of ["bad inner space", "bad\nheader", "密钥", "x".repeat(4097)]) {
    await assert.rejects(f.client.saveMineru(key), { code: "INPUT" });
  }
  assert.equal(f.posts("/v1/mineru").length, 1);
});
test("translation is true by default and never string-coerced", () => {
  const value = { keys: [], workers: 1, chunk_parallel: 1, limit: 0 };
  assert.equal(Bridge.previewBody(value).translate, true);
  assert.equal(Bridge.previewBody({ ...value, translate: false }).translate, false);
  for (const translate of ["false", 0, 1, null, {}, []]) {
    assert.throws(() => Bridge.previewBody({ ...value, translate }), { code: "INPUT" });
  }
});
test("explicit secret reveal is a fixed authenticated POST and never an operation or automatic GET", async () => {
  const f = fixture();
  await f.client.getStatus(); await f.client.getConfig(); await f.client.getMineru();
  assert.equal(f.posts("/v1/credentials/reveal").length, 0);
  await assert.rejects(f.client.request("GET", "/v1/credentials/reveal"), { code: "ROUTE" });
  await assert.rejects(f.client.revealKey("unknown"), { code: "INPUT" });
  const value = await f.client.revealKey("glm");
  assert.equal(value.key, "synthetic-reveal-only-glm");
  const r = f.posts("/v1/credentials/reveal")[0];
  assert.deepEqual(JSON.parse(r.body), { provider: "glm", confirm: true });
  assert.equal(r.cache, "no-store"); assert.equal(r.headers["X-Library-Sync-Token"], TOKEN);
  assert.equal(f.client.redact(value.key), "[已隐藏]");
  assert.ok(!r.url.includes(value.key)); assert.ok(!r.body.includes(value.key));
  f.client.forgetRevealed("glm"); assert.equal(f.client.revealedSensitive.size, 0);
  assert.equal(f.requests.filter(x => /operations/.test(x.url)).length, 0); f.client.close();
});
test("reveal errors and malformed secret responses never expose raw payloads", async () => {
  const secret = "synthetic-reveal-error-canary";
  for (const response of [
    { status: 400, data: { error: secret } },
    { status: 200, data: { provider: "glm", key: secret, source: "bad" } },
    { status: 200, data: { provider: "deepseek", key: secret, source: "encrypted" } },
    { status: 200, data: { provider: "glm", key: "x".repeat(4097), source: "encrypted" } }
  ]) {
    const f = fixture(); f.override = () => response;
    await assert.rejects(f.client.revealKey("glm"), e => e.code === "CREDENTIAL" && !e.message.includes(secret));
    assert.equal(f.client.revealedSensitive.size, 0); assert.equal(f.posts("/v1/credentials/reveal").length, 1);
    f.client.close();
  }
});
test("model choices preserve origin and reject malformed or unbounded provider data", () => {
  const value = { provider: "glm", models: ["glm-A", "glm-A", "glm-B"], source: "bundled_catalog", notice: "not account permissions" };
  assert.deepEqual(Bridge.modelChoices(value, "glm").models, ["glm-A", "glm-B"]);
  for (const delta of [{ provider: "deepseek" }, { source: "unknown" }, { models: ["<script>x</script>"] },
    { models: Array(513).fill("glm-A") }, { notice: "x".repeat(1001) }, { models: "glm-A" }]) {
    assert.throws(() => Bridge.modelChoices({ ...value, ...delta }, "glm"), { code: "MODEL" });
  }
});

test("extended profiles retain independent model parameters without guessing effort", async () => {
  const f = fixture();
  const value = { ...Bridge.profileDefaults("deepseek"), model: "team/custom-model:latest", reasoning_effort: "medium",
    thinking: "disabled", base_url: "https://trusted.example/v1", models_url: "https://trusted.example/v1/models",
    endpoint_confirmed: true, extra_body: { temperature: 0.5, top_p: 0.9, options: { seed: 12 } } };
  assert.deepEqual(Bridge.profile(value), value);
  await f.client.test(value);
  assert.deepEqual(JSON.parse(f.posts("/v1/test")[0].body), { ...value, role: "qc" });
  await f.client.models("deepseek", { ...value, model: "" });
  assert.equal(JSON.parse(f.posts("/v1/models")[0].body).model, "");
  const stored = { schema: 1, qc: value, zh: { ...value, reasoning_effort: "max", thinking: "enabled" } };
  await f.client.saveConfig(stored, {});
  assert.deepEqual((await f.client.getConfig()).config, stored);
  assert.equal(Bridge.profile({ ...value, reasoning_effort: "default" }).thinking, "disabled");
});

test("profile validation rejects URL credentials, wrong protocols and unsafe JSON without requests", () => {
  const value = { ...Bridge.profileDefaults("glm"), model: "glm-5.3-flash" };
  for (const base_url of ["http://api.example/v1", "https://secret@api.example/v1", "https://api.example/v1?key=secret",
    "https://localhost/v1", "https://127.0.0.1/v1", "https://192.168.1.2/v1", "https://api.example/a/../v1",
    "https://api.example/v1/chat/completions", "https://api.example/%2e/v1"]) {
    assert.throws(() => Bridge.profile({ ...value, base_url }), { code: "INPUT" });
  }
  for (const delta of [{ protocol: "anthropic_messages" }, { protocol: "openai_responses" }, { thinking: "auto" },
    { extra_body: [] }, { extra_body: { headers: { Authorization: "synthetic" } } },
    { extra_body: { options: { model: "replacement" } } }, { extra_body: { tools: [] } },
    { extra_body: { max_tokens: 1 } }, { extra_body: { reasoning_effort: "low" } },
    { endpoint_confirmed: "true" }]) {
    assert.throws(() => Bridge.profile({ ...value, ...delta }), { code: "INPUT" });
  }
});

test("model metadata is bounded, role-specific public data with no raw credential fields", () => {
  const value = { provider: "deepseek", models: ["one", "two"], source: "provider_api", notice: "live",
    details: { one: { effort_levels: ["low", "high", "max"], default_effort: "high", max_output_tokens: 32000,
      api_key: "synthetic-must-not-propagate" }, two: { effort_levels: ["minimal", "medium"] } } };
  const normalized = Bridge.modelChoices(value, "deepseek");
  assert.deepEqual(normalized.details.one.effort_levels, ["low", "high", "max"]);
  assert.deepEqual(normalized.details.two.effort_levels, ["minimal", "medium"]);
  assert.equal(normalized.details.one.api_key, undefined);
  for (const details of [{ missing: {} }, { one: { effort_levels: ["<script>"] } },
    { one: { effort_levels: Array(17).fill("low") } }, { one: { default_effort: "bad value" } }]) {
    assert.throws(() => Bridge.modelChoices({ ...value, details }, "deepseek"), { code: "MODEL" });
  }
});

test("test roles are explicit and invalid roles send nothing", async () => {
  const f = fixture(), model = f.publicConfig.config.zh;
  await f.client.test(model, "zh");
  assert.equal(JSON.parse(f.posts("/v1/test")[0].body).role, "zh");
  for (const role of ["QC", null, 0, {}, "other"]) assert.throws(() => f.client.test(model, role), { code: "INPUT" });
  assert.equal(f.posts("/v1/test").length, 1);
});

test("public test metrics preserve model and token counts but discard untrusted fields", () => {
  const input = { ok: true, requested_model: "glm-test", model: "glm-test", role: "zh",
    attempts: 1, elapsed_seconds: 1.25, secret: "unknown-secret-canary",
    raw: { api_key: "unknown-secret-canary" }, request: "unknown-secret-canary",
    usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10,
      token: "unknown-secret-canary", prompt_tokens_details: { cached_tokens: 2, authorization: "unknown-secret-canary" },
      completion_tokens_details: { reasoning_tokens: 1, password: "unknown-secret-canary" } } };
  const result = Bridge.testResult(input);
  assert.equal(result.requested_model, "glm-test"); assert.equal(result.role, "zh");
  assert.equal(result.usage.total_tokens, 10); assert.equal(result.usage.prompt_tokens_details.cached_tokens, 2);
  assert.equal(result.usage.completion_tokens_details.reasoning_tokens, 1);
  assert.ok(!JSON.stringify(result).includes("unknown-secret-canary"));
  const invalid = Bridge.testResult({ usage: { total_tokens: "unknown-secret-canary", prompt_tokens: -1, completion_tokens: Infinity } });
  assert.deepEqual(invalid.usage, {});
  const f = fixture(); f.client.sensitive.add("known-secret-canary");
  assert.ok(!f.client.redact(JSON.stringify(Bridge.testResult({ model: "known-secret-canary" }))).includes("known-secret-canary"));
});
