"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { Bridge, OP, fixture, ui } = require("./helpers.cjs");
const options = { keys: ["ABCDEFG2"], workers: 1, chunk_parallel: 1, limit: 0, translate: true };
const capabilities = ["keys-v1", "mineru-v1", "translation-policy-v1", "model-profiles-v2", "model-test-role-v1", "source-refresh-v1"];
function refreshUI(primary = null) {
  const f = fixture(); f.state.capabilities = [...capabilities];
  const u = ui(f);
  u.host.getSelection = () => ({ items: [{ key: "ABCDEFG2", title: "Synthetic paper", ...(primary ? { primary_key: primary } : {}) }], ignored: 0 });
  return u;
}
async function enable(u) {
  u.el("refresh-source").checked = true; await u.el("refresh-source").emit("change");
}

test("source refresh remains opt-in and false omits both new fields for the legacy protocol", () => {
  assert.deepEqual(Bridge.previewBody(options), options);
  assert.deepEqual(Bridge.previewBody({ ...options, refresh_source: false, primary_keys: { ABCDEFG2: "ATTACH22" } }), options);
  const value = Object.freeze({ ...options, keys: Object.freeze([...options.keys]), refresh_source: true,
    primary_keys: Object.freeze({ ABCDEFG2: "ATTACH22" }) });
  const body = Bridge.previewBody(value);
  assert.deepEqual(body, value); assert.notEqual(body.primary_keys, value.primary_keys);
  assert.deepEqual(Bridge.previewBody({ ...options, refresh_source: true }), { ...options, refresh_source: true });
  assert.deepEqual(Bridge.previewBody({ ...options, refresh_source: true, primary_keys: {} }), { ...options, refresh_source: true });
});

test("source refresh rejects discovery, non-booleans and attachments outside selected parents", () => {
  for (const delta of [{ keys: [], refresh_source: true }, ...[null, 0, 1, "false", "true", {}, []].map(refresh_source => ({ refresh_source })),
    ...[null, [], "ATTACH22", { ABCDEFG3: "ATTACH22" }, { ABCDEFG2: "invalid" },
      { ABCDEFG2: "ABCDEFG2" }, { ABCDEFG2: null }, { ABCDEFG2: 12345678 }, { "../bad": "ATTACH22" }]
      .map(primary_keys => ({ refresh_source: true, primary_keys }))]) {
    assert.throws(() => Bridge.previewBody({ ...options, ...delta }), { code: "INPUT" });
  }
});

test("selecting a parent with refresh previews automatic PDF detection and changing the option invalidates approval", async () => {
  const u = refreshUI(); await u.controller.init(true);
  assert.equal(u.el("refresh-source").disabled, false); assert.equal(u.el("refresh-source").checked, false);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.ok(u.controller.preview);
  await enable(u); assert.equal(u.controller.preview, null); assert.equal(u.el("start").disabled, true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/preview").at(-1).body), { ...options, refresh_source: true });
  assert.equal(u.f.posts("/v1/start").length, 0);
  u.el("refresh-source").checked = false; await u.el("refresh-source").emit("change");
  assert.equal(u.controller.preview, null); assert.equal(u.el("start").disabled, true);
  u.controller.close(); await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
});

test("selected attachment primary_key is sent only with explicit refresh and reselection invalidates the old preview", async () => {
  const u = refreshUI("ATTACH22"); await u.controller.init(true);
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/preview")[0].body), options);
  await enable(u); await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.deepEqual(JSON.parse(u.f.posts("/v1/preview")[1].body), { ...options, refresh_source: true, primary_keys: { ABCDEFG2: "ATTACH22" } });
  assert.match(u.el("selection").textContent, /ATTACH22/);
  u.host.getSelection = () => ({ items: [{ key: "ABCDEFG2", title: "Synthetic paper", primary_key: "ATTACH33" }], ignored: 0 });
  u.controller.captureSelection(); assert.equal(u.controller.preview, null); assert.equal(u.el("start").disabled, true);
  await u.controller.previewScope(u.controller.keys);
  assert.equal(JSON.parse(u.f.posts("/v1/preview")[2].body).primary_keys.ABCDEFG2, "ATTACH33");
  assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});

test("automatic discovery clears the refresh checkbox and attachment map and cannot be toggled into refresh", async () => {
  const u = refreshUI("ATTACH22"); await u.controller.init(true); await enable(u);
  await u.el("discover").emit("click");
  assert.equal(u.el("refresh-source").checked, false); assert.equal(u.el("refresh-source").disabled, true);
  assert.deepEqual(u.controller.primaryKeys, {}); assert.deepEqual(u.controller.keys, []);
  assert.deepEqual(JSON.parse(u.f.posts("/v1/preview")[0].body), { ...options, keys: [] });
  await u.controller.tick(); await enable(u);
  assert.equal(u.el("refresh-source").checked, false); assert.equal(u.controller.preview, null);
  assert.match(u.el("message").textContent, /自动发现不支持/);
  assert.equal(u.f.posts("/v1/start").length, 0); u.controller.close();
});

test("missing source-refresh capability gives upgrade guidance and never sends an unsupported payload", async () => {
  const u = ui(); await u.controller.init(true);
  assert.equal(u.el("refresh-source").disabled, true);
  assert.match(u.el("refresh-source-reason").textContent, /更新 bridge/);
  u.el("refresh-source").checked = true; await u.controller.previewScope(u.controller.keys);
  assert.equal(u.f.posts("/v1/preview").length, 0); assert.match(u.el("message").textContent, /更新 bridge/);
  await u.el("refresh-source").emit("change"); assert.equal(u.el("refresh-source").checked, false);
  await u.controller.previewScope(u.controller.keys);
  assert.deepEqual(JSON.parse(u.f.posts("/v1/preview")[0].body), options);
  u.controller.close();
});

test("fresh status capability loss blocks refresh after a previously supported selection", async () => {
  const u = refreshUI("ATTACH22"); await u.controller.init(true); await enable(u);
  u.f.state.capabilities = capabilities.filter(c => c !== "source-refresh-v1");
  await u.controller.previewScope(u.controller.keys);
  assert.equal(u.f.posts("/v1/preview").length, 0);
  assert.match(u.el("message").textContent, /更新 bridge/); u.controller.close();
});

test("closing while a refresh preview is in flight never starts a task or accepts a late preview", async () => {
  const u = refreshUI("ATTACH22"); await u.controller.init(true); await enable(u);
  let release, entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  u.f.override = r => {
    if (new URL(r.url).pathname === "/v1/preview") {
      entered(); return new Promise(resolve => { release = resolve; });
    }
  };
  const pending = u.controller.previewScope(u.controller.keys); await waiting;
  u.controller.close(); release({ status: 200, data: { operation: OP } }); await pending;
  await u.controller.start(); await u.controller.tick();
  assert.equal(u.controller.preview, null); assert.equal(u.controller.pending, null);
  assert.equal(u.f.posts("/v1/start").length, 0); assert.equal(u.f.launches.length, 0);
});
