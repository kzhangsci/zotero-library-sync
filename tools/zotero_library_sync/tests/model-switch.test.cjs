"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { Bridge, fixture, ui, OP, RUN } = require("./helpers.cjs");

const REBIND_CAPS = ["keys-v1", "mineru-v1", "translation-policy-v1", "model-choices-v1",
  "credential-reveal-v1", "model-profiles-v2", "model-test-role-v1", "preview-rebind-v1", "catalog-cache-v1"];
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

function rebindUI() {
  const f = fixture(); f.state.capabilities = [...REBIND_CAPS];
  return ui(f);
}
async function preview(u) {
  await u.controller.init(true);
  await u.controller.previewScope(u.controller.keys);
  await u.controller.tick();
}

test("model edits after a preview keep per-paper selections and block start until saved", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  const include = u.controller.selectionControls.find(c => c.selectionField === "include");
  include.checked = false; await include.emit("change");
  include.checked = true; await include.emit("change");
  u.el("qc-model").value = "glm-switched-model"; await u.el("qc-model").emit("input");
  assert.ok(u.controller.preview); assert.equal(u.controller.modelDirty, true);
  assert.equal(u.el("start").disabled, true);
  assert.match(u.el("start-reason").textContent, /保存配置/);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
});

test("saving new models rebinds to a fresh preview id without rescanning and preserves selections", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  const include = u.controller.selectionControls.find(c => c.selectionField === "include");
  include.checked = false; await include.emit("change");
  u.el("qc-model").value = "glm-switched-model"; await u.el("qc-model").emit("input");
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/config").length, 1);
  const rebinds = u.f.posts("/v1/preview/rebind");
  assert.equal(rebinds.length, 1);
  assert.deepEqual(JSON.parse(rebinds[0].body), { preview_id: RUN, confirm: true });
  assert.equal(u.controller.modelDirty, false);
  // The rebind retired the old id; the panel adopted the returned fresh one.
  assert.equal(u.controller.preview.preview_id, RUN + "_r1");
  // The un-checked paper stays excluded: no rediscovery reset the selections.
  assert.deepEqual(u.controller.selections, [{ key: "ABCDEFG2", include: false, translate: true }]);
  assert.equal(u.el("preview").disabled, false);
  const includeAfter = u.controller.selectionControls.find(c => c.selectionField === "include");
  includeAfter.checked = true; await includeAfter.emit("change");
  await u.controller.start();
  assert.equal(u.f.posts("/v1/start").length, 1);
  // Only the NEW id reaches /v1/start; the retired one can no longer start.
  assert.equal(JSON.parse(u.f.posts("/v1/start")[0].body).preview_id, RUN + "_r1");
  assert.match(u.window.confirms.at(-1), /冻结模型.*glm-switched-model/);
});

test("keys-only save does not rebind; scope changes still discard the preview", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  u.el("glm-key").value = "synthetic-key-only"; await u.el("glm-key").emit("input");
  await u.controller.saveConfig(true);
  assert.equal(u.f.posts("/v1/preview/rebind").length, 0);
  assert.ok(u.controller.preview);
  await u.el("workers").emit("input");
  assert.equal(u.controller.preview, null); assert.equal(u.controller.modelDirty, false);
  assert.equal(u.el("start").disabled, true);
});

test("an old bridge degrades explicitly: no silent rebind, selections kept, start blocked", async t => {
  const f = fixture(); // No preview-rebind-v1 capability.
  const u = ui(f); t.after(() => u.controller.close());
  await preview(u);
  u.el("qc-model").value = "glm-switched-model"; await u.el("qc-model").emit("input");
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/preview/rebind").length, 0);
  assert.ok(u.controller.preview);
  assert.equal(u.controller.selections.length, 1);
  assert.equal(u.controller.modelDirty, true);
  assert.equal(u.el("start").disabled, true);
  assert.match(u.el("start-reason").textContent, /模型配置已修改/);
  assert.match(u.el("message").textContent, /不支持预演模型更新/);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
});

test("a failed rebind keeps the preview, blocks start and names the reason", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  u.el("qc-model").value = "glm-switched-model"; await u.el("qc-model").emit("input");
  u.f.rebindOverride = () => ({ status: 400, data: { error: "预览已过期，请重新预演；不会自动重试旧任务" } });
  await u.controller.saveConfig();
  assert.equal(u.f.posts("/v1/preview/rebind").length, 1);
  assert.ok(u.controller.preview); assert.equal(u.controller.modelDirty, true);
  assert.match(u.el("message").textContent, /重新预演/);
  assert.equal(u.el("start").disabled, true);
  await u.controller.start(); assert.equal(u.f.posts("/v1/start").length, 0);
  // A fresh preview is always the explicit escape hatch.
  u.f.rebindOverride = null;
  await u.controller.previewScope(u.controller.keys); await u.controller.tick();
  assert.equal(u.controller.modelDirty, false); assert.equal(u.el("start").disabled, false);
});

test("a rebind whose items no longer match the selections is rejected as protocol damage", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  u.el("qc-model").value = "glm-switched-model"; await u.el("qc-model").emit("input");
  u.f.rebindResult = { preview_id: RUN + "_r9", rebound: true, text: "mismatch", items: [], deferred: [] };
  await u.controller.saveConfig();
  assert.equal(u.controller.modelDirty, true);
  assert.equal(u.el("start").disabled, true);
});

test("provider switch auto-loads trusted default catalogs without an extra click or dialog", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  assert.equal(u.f.posts("/v1/models").length, 0);
  await u.el("qc-provider-toggle").emit("click", { button: 0 });
  await u.el("qc-provider-options").children.find(row => row.textContent === "DeepSeek").emit("click", { button: 0 });
  const posts = u.f.posts("/v1/models");
  assert.equal(posts.length, 1); // DeepSeek default endpoint auto-load, no manual button.
  assert.equal(JSON.parse(posts[0].body).provider, "deepseek");
  assert.equal(JSON.parse(posts[0].body).refresh, false);
  assert.equal(u.window.confirms.length, 0); // Trusted default endpoints need no confirmation.
  await flush();
  assert.ok(u.controller.catalogPending.qc);
  // A catalog fetch never locks the whole form: no busy/pending, inputs usable.
  assert.equal(u.controller.busy, false); assert.equal(u.controller.pending, null);
  assert.equal(u.el("qc-effort").disabled, false);
  u.f.operationResult = { provider: "deepseek", models: ["deepseek-flash"], source: "provider_api",
    notice: "synthetic listing", reference: "https://api.deepseek.com/models" };
  await u.controller.tick();
  assert.deepEqual(u.controller.modelOptions.qc.models, ["deepseek-flash"]);
  // A custom endpoint is never auto-requested.
  u.el("qc-base-url").value = "https://custom.example/v1"; await u.el("qc-base-url").emit("input");
  const before = u.f.posts("/v1/models").length;
  await u.controller.tick();
  assert.equal(u.f.posts("/v1/models").length, before);
});

test("QC and ZH share one cached catalog; manual refresh forces a new request", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "bundled_catalog", notice: "docs" };
  await u.controller.catalogAction("qc", true);
  await u.controller.tick();
  assert.equal(u.f.posts("/v1/models").length, 1);
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3"]);
  // Same provider+endpoint for the other role: cache hit, no second request.
  // Fresh glm drafts default to the Coding Plan, so align the endpoint with
  // QC's hydrated general endpoint before the switch event fires.
  u.el("zh-provider").value = "glm";
  u.el("zh-base-url").value = u.el("qc-base-url").value;
  u.el("zh-models-url").value = "";
  await u.el("zh-provider").emit("change");
  await u.controller.catalogAction("zh", false);
  assert.equal(u.f.posts("/v1/models").length, 1);
  assert.ok(u.controller.modelOptions.zh);
  assert.match(u.el("zh-model-notice").textContent, /缓存命中/);
  // The manual button forces a fresh request.
  await u.controller.catalogAction("qc", true);
  assert.equal(u.f.posts("/v1/models").length, 2);
  assert.equal(JSON.parse(u.f.posts("/v1/models")[1].body).refresh, undefined);
});

test("an old bridge without catalog-cache-v1 auto-loads via the legacy body without the refresh flag", async t => {
  const f = fixture(); f.state.capabilities = [...REBIND_CAPS].filter(c => c !== "catalog-cache-v1");
  const u = ui(f); t.after(() => u.controller.close());
  await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "bundled_catalog", notice: "docs" };
  await u.controller.catalogAction("qc", false);   // the auto-load path
  assert.equal(u.f.posts("/v1/models").length, 1);
  assert.equal(JSON.parse(u.f.posts("/v1/models")[0].body).refresh, undefined);
  await u.controller.tick();
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3"]);
});

test("key edits retire the catalog identity, the old account's options and late listings", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "bundled_catalog", notice: "docs" };
  await u.controller.catalogAction("qc", true);
  await u.controller.tick();
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3"]);
  await u.controller.catalogAction("qc", true); // in flight again
  u.el("glm-key").value = "synthetic-rotated-key"; await u.el("glm-key").emit("input");
  // A draft key immediately retires the old account's listing; the typed
  // model survives and no fetch may claim to speak for the unsaved key.
  assert.equal(u.controller.modelOptions.qc, null);
  assert.match(u.el("qc-model-notice").textContent, /尚未保存/);
  u.f.operationResult = { provider: "glm", models: ["glm-4.6"], source: "bundled_catalog", notice: "rotated" };
  await u.controller.tick();
  // The response belongs to the previous credential generation: dropped.
  assert.equal(u.controller.modelOptions.qc, null);
  assert.match(u.el("message").textContent, /未应用/);
  assert.equal(u.controller.catalogPending.qc, undefined);
});

test("a draft key refuses catalog fetches until saved; saving retires only that provider", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "bundled_catalog", notice: "docs" };
  await u.controller.catalogAction("qc", true); await u.controller.tick();
  u.f.operationResult = { provider: "deepseek", models: ["deepseek-flash"], source: "provider_api",
    notice: "live", reference: "https://api.deepseek.com/models" };
  await u.controller.catalogAction("zh", true); await u.controller.tick();
  assert.ok(u.controller.modelOptions.qc); assert.ok(u.controller.modelOptions.zh);
  const typed = u.el("qc-model").value;
  u.el("glm-key").value = "synthetic-draft-key"; await u.el("glm-key").emit("input");
  assert.equal(u.controller.modelOptions.qc, null);          // glm listing retired
  assert.ok(u.controller.modelOptions.zh);                   // deepseek untouched
  assert.equal(u.el("qc-model").value, typed);               // manual model kept
  await u.controller.catalogAction("qc", true);
  assert.equal(u.f.posts("/v1/models").length, 2);           // nothing was sent
  assert.match(u.el("message").textContent, /先保存 Key/);
  // Saving the key retires that provider's generation; a fresh fetch works
  // and the other provider's catalog was never disturbed.
  u.f.operationResult = { provider: "glm", models: ["glm-5.3", "glm-5.2"], source: "bundled_catalog", notice: "docs2" };
  await u.controller.saveConfig(true);
  assert.equal(u.controller.modelOptions.qc, null);
  assert.equal(u.el("qc-model").value, typed);
  assert.ok(u.controller.modelOptions.zh);
  await u.controller.catalogAction("qc", true); await u.controller.tick();
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3", "glm-5.2"]);
  assert.ok(u.controller.modelOptions.zh);
});

test("offline catalog failures keep hand-typed models, cached candidates and the preview", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await preview(u);
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "bundled_catalog", notice: "docs" };
  await u.controller.catalogAction("qc", true);
  await u.controller.tick();
  u.el("qc-model").value = "hand-kept-model"; await u.el("qc-model").emit("input");
  u.f.online = false;
  await u.controller.catalogAction("qc", true);
  assert.equal(u.el("qc-model").value, "hand-kept-model");
  assert.ok(u.controller.preview); assert.deepEqual(u.controller.selections.map(r => r.key), ["ABCDEFG2"]);
  assert.match(u.el("qc-model-notice").textContent, /失败.*保留/);
  assert.deepEqual(u.controller.modelOptions.qc.models, ["glm-5.3"]);  // the kept list stays marked
  // After an explicit reconnect, the bridge-side stale fallback is shown as a
  // clearly labeled cache, never as a fresh live listing.
  u.f.online = true;
  await u.controller.connect(false);
  u.f.operationResult = { provider: "glm", models: ["glm-5.3", "glm-5.2"], source: "bundled_catalog",
    notice: "docs", cache: "stale", fetched_at: "2026-11-10T12:00:00+00:00" };
  await u.controller.catalogAction("qc", true); await u.controller.tick();
  assert.match(u.el("qc-model-notice").textContent, /上次候选缓存/);
  assert.equal(u.el("qc-model").value, "hand-kept-model");
});

test("classification lists missing required steps; only complete artifacts count as pending review", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init(true);
  const art = (files, qc) => ({ files, qc_marked: qc });
  u.f.preview = { preview_id: RUN, text: "发现 6 篇", items: [
    { key: "AAAAAAA1", citekey: "OnlyNote2026", title: "note only", artifacts: art(["Note"], false), require_translation: true },
    { key: "AAAAAAA2", citekey: "EnNoQc2026", title: "en without qc", artifacts: art(["EN"], false), require_translation: true },
    { key: "AAAAAAA3", citekey: "EnQcNoZh2026", title: "qc but zh missing", artifacts: art(["EN", "MD", "Note"], true), require_translation: true },
    { key: "AAAAAAA4", citekey: "SourceOnly2026", title: "translation unchecked", artifacts: art(["EN", "MD", "Note"], true), require_translation: true },
    { key: "AAAAAAA5", citekey: "Chinese2026", title: "中文源", artifacts: art(["EN", "MD", "Note"], true), require_translation: false },
    { key: "AAAAAAA6", citekey: "Complete2026", title: "all present", artifacts: art(["EN", "MD", "ZH", "Note"], true), require_translation: true }],
    deferred: [] };
  await u.controller.previewScope([]); await u.controller.tick();
  // Per-paper translation choice is part of the classification: unchecking ZH
  // for item 4 turns it into "pending review" in real time.
  const zhBox = u.controller.selectionControls.find(c => c.selectionKey === "AAAAAAA4" && c.selectionField === "translate");
  zhBox.checked = false; await zhBox.emit("change");
  const counts = u.controller.previewCounts();
  assert.equal(counts.fresh, 3); assert.equal(counts.review, 3);
  assert.match(u.el("selection-notice").textContent,
    /可执行 6 篇、暂缓 0 篇；可执行中预计需补齐产物 3 篇、已有成果待复核 3 篇（复核仍需完整 QA 验收，不代表已通过）/);
  const rows = [...u.el("preview-rows").children].map(row => row.textContent);
  assert.match(rows[0], /OnlyNote2026.*预计需补齐产物（缺原文、中译）/);
  assert.match(rows[1], /EnNoQc2026.*预计需补齐产物（缺质检、笔记、中译）/);
  assert.match(rows[2], /EnQcNoZh2026.*预计需补齐产物（缺中译）/);
  assert.match(rows[3], /SourceOnly2026.*已有成果待复核（仍需完整 QA 验收，不代表已通过）/);
  assert.match(rows[4], /Chinese2026.*已有成果待复核/);   // 中文原文：无需 ZH
  assert.match(rows[5], /Complete2026.*已有成果待复核/);
  u.window.confirmResult = false;
  await u.controller.start();
  assert.match(u.window.confirms.at(-1), /预计需补齐产物 3 篇、已有成果待复核 3 篇/);
});

test("bridge-side cache hits are labeled and never claimed as live permission evidence", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  u.f.operationResult = { provider: "glm", models: ["glm-5.3"], source: "provider_api",
    notice: "synthetic live", cache: "hit", fetched_at: "2026-11-10T11:00:00+00:00" };
  await u.controller.catalogAction("qc", true); await u.controller.tick();
  assert.match(u.el("qc-model-notice").textContent, /缓存命中/);
  assert.match(u.el("qc-model-notice").textContent, /缓存的服务商 API 模型列表（非实时，不保证调用权限）/);
  assert.doesNotMatch(u.el("qc-model-notice").textContent, /本次服务商 API 返回/);
});

test("closing the panel discards pending catalog work without extra operations", async t => {
  const u = rebindUI(); let release, started;
  const submitted = new Promise(r => { started = r; });
  await u.controller.init();
  u.f.override = r => new URL(r.url).pathname === "/v1/models" ? new Promise(r2 => { release = r2; started(); }) : undefined;
  const fetching = u.controller.catalogAction("qc", true);
  await submitted;
  u.controller.close();
  release({ status: 200, data: { operation: OP } });
  await fetching;
  assert.equal(u.controller.catalogPending.qc, undefined);
  assert.equal(u.f.posts("/v1/start").length, 0);
});

test("a slower superseded catalog POST can never overwrite the newer pending record", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  const queue = [];
  u.f.override = r => {
    if (new URL(r.url).pathname !== "/v1/models") return undefined;
    return new Promise(resolve => { queue.push({ body: JSON.parse(r.body), resolve }); });
  };
  const oldListing = u.controller.catalogAction("qc", true);   // glm POST hangs
  await flush();
  assert.equal(queue.length, 1);
  u.controller.endpointConfirmed.qc = true;
  u.el("qc-provider").value = "deepseek";                       // same role, new identity
  const newListing = u.controller.catalogAction("qc", true);
  await flush();
  assert.equal(queue.length, 2);
  assert.equal(queue[1].body.provider, "deepseek");
  // The NEW POST resolves first and owns the pending record.
  const newOp = "12345678-1234-4123-8123-1234567890ff";
  queue[1].resolve({ status: 200, data: { operation: newOp } });
  await newListing;
  assert.equal(u.controller.catalogPending.qc.id, newOp);
  // The OLD POST resolves later: the token check discards it.
  queue[0].resolve({ status: 200, data: { operation: OP } });
  await oldListing;
  assert.equal(u.controller.catalogPending.qc.id, newOp);
  assert.equal(u.controller.catalogPending.qc.provider, "deepseek");
});

test("polling a slow catalog operation keeps a newer pending record intact", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  const oldOp = "12345678-1234-4123-8123-1234567890aa", newOp = "12345678-1234-4123-8123-1234567890bb";
  let releaseOld;
  u.f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/models") {
      return { status: 200, data: { operation: JSON.parse(r.body).provider === "glm" ? oldOp : newOp } };
    }
    if (path === "/v1/operations/" + oldOp) return new Promise(r2 => { releaseOld = r2; });
    return undefined;
  };
  await u.controller.catalogAction("qc", true);
  assert.equal(u.controller.catalogPending.qc.id, oldOp);
  const ticking = u.controller.tick();          // polls oldOp and hangs there
  await flush();
  u.controller.endpointConfirmed.qc = true;
  u.el("qc-provider").value = "deepseek";
  await u.controller.catalogAction("qc", true); // replaces the record meanwhile
  assert.equal(u.controller.catalogPending.qc.id, newOp);
  releaseOld({ status: 200, data: { status: "completed",
    result: { provider: "glm", models: ["stale-model"], source: "bundled_catalog", notice: "old" } } });
  await ticking;
  // The superseded poll neither applied the stale listing nor deleted the record.
  assert.equal(u.controller.catalogPending.qc.id, newOp);
  // The bundled offline seed shown at open survives untouched: the superseded
  // poll's stale-model list must never be applied over it.
  assert.ok(u.controller.modelOptions.qc);
  assert.equal(u.controller.modelOptions.qc.seeded, true);
  assert.ok(!u.controller.modelOptions.qc.models.includes("stale-model"));
  assert.equal(u.el("qc-model").value, "glm-test-model");
});

test("rapid repeated catalog clicks keep one pending record per role", async t => {
  const u = rebindUI(); t.after(() => u.controller.close());
  await u.controller.init();
  const queue = [];
  u.f.override = r => {
    if (new URL(r.url).pathname !== "/v1/models") return undefined;
    return new Promise(resolve => { queue.push({ body: JSON.parse(r.body), resolve }); });
  };
  const first = u.controller.catalogAction("qc", true);
  await flush();
  const second = u.controller.catalogAction("qc", true);  // same identity while the first POST hangs
  await flush();
  assert.equal(queue.length, 1);                          // merged into the one in-flight request
  assert.equal(queue[0].body.provider, "glm");
  queue[0].resolve({ status: 200, data: { operation: OP } });
  await first; await second;
  assert.equal(u.controller.catalogPending.qc.id, OP);
  assert.equal(Object.keys(u.controller.catalogPending).length, 1);
  // A different identity on the same role still races a fresh POST past dedup.
  u.controller.endpointConfirmed.qc = true;
  u.el("qc-provider").value = "deepseek";
  const differentProvider = u.controller.catalogAction("qc", true);
  await flush();
  assert.equal(queue.length, 2);
  assert.equal(queue[1].body.provider, "deepseek");
  const otherOp = "12345678-1234-4123-8123-1234567890ee";
  queue[1].resolve({ status: 200, data: { operation: otherOp } });
  await differentProvider;
  assert.equal(u.controller.catalogPending.qc.id, otherOp);
});

test("saving new paths discards catalog work from the previous repository", async t => {
  const u = rebindUI(); let release, started;
  const submitted = new Promise(r => { started = r; });
  await u.controller.init();
  u.f.override = r => new URL(r.url).pathname === "/v1/models" ? new Promise(r2 => { release = r2; started(); }) : undefined;
  const fetching = u.controller.catalogAction("qc", true);
  await submitted;
  await u.el("save-paths").emit("click");
  release({ status: 200, data: { operation: OP } });
  await fetching;
  assert.equal(u.controller.catalogPending.qc, undefined);   // token superseded by the path reset
  assert.equal(u.f.posts("/v1/models").length, 1);
});
