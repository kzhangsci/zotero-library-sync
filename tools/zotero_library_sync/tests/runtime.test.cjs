"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { ROOT, PATHS, Element, Document } = require("./helpers.cjs");
function runtime() {
  const counters = { registered: 0, destroyed: 0, opens: 0, launches: [], writes: [], xhrs: [], missing: new Set(), directories: new Set(), dialogs: [] };
  const docs = new Document();
  docs.elements.set("menu_ToolsPopup", new Element()); docs.elements.set("zotero-itemmenu", new Element());
  const win = { document: docs, ZoteroPane: { getSelectedItems: () => [] }, closed: false,
    openDialog(url, name, features, args) {
      counters.opens++; counters.dialog = { url, name, features };
      // A Gecko-like dialog window: event listeners, its own panel document
      // (parsed from the real panel.xhtml) and a close that fires "unload".
      const dialog = Object.assign(new Element("window"), { document: new Document(), closed: false, arguments: [args],
        focus() {}, close() { if (this.closed) return; this.closed = true; void this.emit("unload"); } });
      counters.dialogs.push(dialog);
      return dialog;
    },
    alert(message) { counters.alerts = (counters.alerts || []); counters.alerts.push(String(message)); } };
  const file = () => ({ initWithPath(p) { this.path = p; this.leafName = p.split("\\").at(-1); },
    exists() { return !counters.missing.has(this.path); }, isFile() { return !counters.directories.has(this.path); } });
  const classes = {
    "@mozilla.org/addons/addon-manager-startup;1": { getService: () => ({ registerChrome() { counters.registered++; return { destruct() { counters.destroyed++; } }; } }) },
    "@mozilla.org/file/local;1": { createInstance: file },
    "@mozilla.org/process/util;1": { createInstance: () => ({ init(f) { this.file = f; }, runwAsync(args, n, observer, hold) { counters.launches.push({ path: this.file.path, args, n, observer, hold }); } }) }
  };
  class XHR {
    constructor(options) {
      this.options = options; this.headers = {}; this.status = 200;
      this.responseText = '{"active":false,"view":null,"text":"ok"}';
      this.channel = { bypassProxy: false, notificationCallbacks: null, QueryInterface() { return this; } };
      counters.xhrs.push(this);
    }
    get withCredentials() { return this._withCredentials || false; }
    set withCredentials(value) {
      // Gecko ESR140 SetWithCredentials rejects *any* assignment for mIsAnon,
      // including false. A plain JS property masked the 0.1.2 init regression.
      counters.credentialsWrites = (counters.credentialsWrites || 0) + 1;
      if (this.options.mozAnon) {
        const error = new Error("XMLHttpRequest must not be sending.");
        error.name = "InvalidStateError";
        throw error;
      }
      this._withCredentials = value;
    }
    open(method, url, async) { Object.assign(this, { method, url, async }); }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) {
      this.body = body;
      queueMicrotask(() => counters.xhrSend ? counters.xhrSend(this) : this.onload());
    }
    abort() { this.aborted = true; this.onabort?.(); }
  }
  const context = vm.createContext({
    Zotero: { initializationPromise: Promise.resolve(), uiReadyPromise: Promise.resolve(),
      getMainWindows: () => [win], getMainWindow: () => win, Libraries: { userLibraryID: 1 }, Items: { get: () => null },
      Prefs: { get: key => key.endsWith(".repo") ? PATHS.repo : PATHS.pythonw, set: (...args) => counters.writes.push(args) } },
    Services: { io: { newURI: value => value },
      scriptloader: { loadSubScript(uri) { vm.runInContext(fs.readFileSync(path.join(ROOT, uri.split("/").at(-1)), "utf8"), context); } } },
    Cc: classes, Ci: { nsIFile: {}, nsIProcess: {}, amIAddonManagerStartup: {}, nsIHttpChannelInternal: {}, nsIChannelEventSink: {} },
    Cr: { NS_ERROR_NO_INTERFACE: 1, NS_BINDING_ABORTED: 2 },
    ChromeUtils: { generateQI: () => function () { return this; } },
    IOUtils: { exists: async p => !p.endsWith("library-sync.json"), stat: async () => ({ size: 100 }), readUTF8: async () => "{}" },
    XMLHttpRequest: XHR, URL,
    setTimeout, clearTimeout
  });
  vm.runInContext(fs.readFileSync(path.join(ROOT, "bootstrap.js"), "utf8"), context);
  return { context, counters, win, docs };
}
test("bootstrap waits for initialization/UI and shutdown cancels deferred startup", async () => {
  const r = runtime(); let ready;
  r.context.Zotero.uiReadyPromise = new Promise(resolve => { ready = resolve; });
  const start = r.context.startup({ rootURI: "chrome://test/" });
  await Promise.resolve(); assert.equal(r.counters.registered, 0);
  r.context.shutdown(); ready(); await start;
  assert.equal(r.counters.registered, 0); assert.equal(r.docs.getElementById("menu_ToolsPopup").children.length, 0);
});

test("empty preferences discover standalone install without persisting paths", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  r.context.Zotero.Prefs.get = () => "";
  r.context.Services.dirsvc = { get: () => ({ path: "C:\\Users\\Synthetic\\AppData\\Local" }) };
  const p = r.context.LibrarySync.getPaths();
  assert.equal(p.repo, "C:\\Users\\Synthetic\\AppData\\Local\\LibrarySync\\app");
  assert.equal(p.pythonw, p.repo + "\\runtime\\pythonw.exe");
  assert.equal(r.counters.writes.length, 0);
  assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});

test("XPI-only upgrade does not invent an absent standalone installation", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  r.context.Zotero.Prefs.get = () => "";
  r.context.Services.dirsvc = { get: () => ({ path: "C:\\Users\\Synthetic\\AppData\\Local" }) };
  const candidate = "C:\\Users\\Synthetic\\AppData\\Local\\LibrarySync\\app";
  for (const absent of [candidate + "\\tools\\library_plugin_bridge.py", candidate + "\\runtime\\pythonw.exe"]) {
    r.counters.missing.clear(); r.counters.missing.add(absent);
    const p = r.context.LibrarySync.getPaths();
    assert.equal(p.repo, ""); assert.equal(p.pythonw, "");
  }
  assert.equal(r.counters.writes.length, 0); assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});

test("explicit existing paths stay authoritative and a missing executable is never guessed", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  assert.equal(r.context.LibrarySync.getPaths().repo, PATHS.repo);
  assert.equal(r.context.LibrarySync.getPaths().pythonw, PATHS.pythonw);
  r.context.Zotero.Prefs.get = key => key.endsWith(".repo") ? PATHS.repo : "";
  r.counters.missing.add(PATHS.repo + "\\runtime\\pythonw.exe");
  const p = r.context.LibrarySync.getPaths();
  assert.equal(p.repo, PATHS.repo); assert.equal(p.pythonw, "");
  assert.equal(r.counters.writes.length, 0); r.context.shutdown();
});

test("missing backend script or pythonw blocks saving and launching without replacing preferences", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  for (const [absent, message] of [[PATHS.repo + "\\tools\\library_plugin_bridge.py", "后台程序目录"], [PATHS.pythonw, "未找到 pythonw.exe"]]) {
    r.counters.missing.clear(); r.counters.missing.add(absent);
    assert.throws(() => r.context.LibrarySync.setPaths(PATHS), e => e.code === "PATH" && e.message.includes(message));
    await assert.rejects(async () => client.io.launch(PATHS.pythonw, [PATHS.repo + "\\tools\\library_plugin_bridge.py", "--serve", "--apply"]), e => e.code === "PATH" && e.message.includes(message));
  }
  r.counters.missing.clear(); r.counters.directories.add(PATHS.pythonw);
  assert.throws(() => r.context.LibrarySync.setPaths(PATHS), { code: "PATH" });
  assert.equal(r.counters.writes.length, 0); assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});

test("descriptor follows bounded public config; invalid config never falls back", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const read = [];
  r.context.IOUtils.exists = async () => true;
  r.context.IOUtils.readUTF8 = async p => {
    read.push(p);
    return p.endsWith("library-sync.json") ? JSON.stringify({ schema_version: 1, data_dir: "C:\\SyntheticState" }) : '{"fixture":true}';
  };
  const client = r.context.LibrarySync.createClient();
  const value = await client.io.readJSON(PATHS.repo + "\\.secrets\\plugin_bridge.json");
  assert.equal(value.fixture, true);
  assert.equal(read.at(-1), "C:\\SyntheticState\\.secrets\\plugin_bridge.json");
  r.context.IOUtils.readUTF8 = async () => JSON.stringify({ schema_version: 1, data_dir: "..\\unsafe" });
  await assert.rejects(client.io.readJSON(PATHS.repo + "\\.secrets\\plugin_bridge.json"));
  r.context.IOUtils.stat = async () => ({ size: 20000 });
  await assert.rejects(client.io.readJSON(PATHS.repo + "\\.secrets\\plugin_bridge.json"));
  assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});
test("XHR works without hiddenDOMWindow/fetch, direct loopback with native timeout", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  const response = await client.io.request("http://127.0.0.1:48765/v1/status", {
    method: "GET", headers: { "X-Library-Sync-Token": "synthetic-token" }
  });
  const xhr = r.counters.xhrs[0];
  assert.equal(response.data.active, false); assert.equal(xhr.options.mozAnon, true);
  assert.equal(xhr.timeout, 10000); assert.equal(xhr.withCredentials, false);
  assert.equal(r.counters.credentialsWrites || 0, 0);
  assert.equal(xhr.channel.bypassProxy, true); assert.equal(xhr.async, true);
  assert.equal(xhr.headers["X-Library-Sync-Token"], "synthetic-token");
  assert.equal(xhr.headers["Cache-Control"], "no-store");
  assert.equal(r.counters.writes.length, 0); assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});
test("XHR rejects external destinations and redirects before forwarding credentials", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  for (const url of ["https://example.com/v1/config", "http://localhost:48765/v1/config", "http://127.0.0.1:48765/v1/config?key=x"]) {
    await assert.rejects(client.io.request(url, { method: "GET" }), { code: "TRANSPORT" });
  }
  assert.equal(r.counters.xhrs.length, 0);
  let decision;
  r.counters.xhrSend = xhr => xhr.channel.notificationCallbacks.asyncOnChannelRedirect(null, {}, 0,
    { onRedirectVerifyCallback(code) { decision = code; } });
  await assert.rejects(client.io.request("http://127.0.0.1:48765/v1/config", { method: "POST", body: "synthetic-only" }), { code: "REDIRECT" });
  assert.equal(decision, r.context.Cr.NS_BINDING_ABORTED);
  assert.equal(r.counters.xhrs.length, 1); assert.equal(r.counters.xhrs[0].aborted, true);
  r.context.shutdown();
});

test("XHR preserves structured business errors for client classification", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  for (const status of [400, 401, 403, 409, 422, 500, 503]) {
    r.counters.xhrSend = xhr => { xhr.status = status; xhr.responseText = '{"error":"synthetic","code":"BUSY"}'; xhr.onload(); };
    const response = await client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET" });
    assert.equal(response.status, status);
    assert.equal(response.data.code, "BUSY");
  }
  assert.equal(r.counters.launches.length, 0);
  r.context.shutdown();
});
test("XHR timeout, malformed/large responses and local failures are bounded and sanitized", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  r.counters.xhrSend = xhr => xhr.ontimeout();
  await assert.rejects(client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET" }), { code: "NETWORK" });
  r.counters.xhrSend = xhr => { xhr.responseText = "private-content-canary"; xhr.onload(); };
  await assert.rejects(client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET" }), e => e.code === "RESPONSE" && !e.message.includes("private-content-canary"));
  r.counters.xhrSend = xhr => xhr.onprogress({ loaded: 4 * 1024 * 1024 + 1 });
  await assert.rejects(client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET" }), { code: "RESPONSE" });
  assert.equal(r.counters.xhrs.at(-1).aborted, true);
  r.context.XMLHttpRequest = class { constructor() { throw new Error("private-init-canary"); } };
  await assert.rejects(client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET" }), e => e.code === "TRANSPORT" && e.message.includes("xhr-create") && !e.message.includes("canary"));
  r.context.shutdown();
});
test("menus share one nonmodal panel; disable removes menus without stopping backend", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  assert.equal(r.docs.getElementById("menu_ToolsPopup").children.length, 1);
  r.context.onMainWindowLoad({ window: r.win }); assert.equal(r.docs.getElementById("menu_ToolsPopup").children.length, 1);
  await r.docs.getElementById("menu_ToolsPopup").children[0].emit("command");
  await r.docs.getElementById("zotero-itemmenu").children[0].emit("command");
  assert.equal(r.counters.opens, 1); assert.ok(r.counters.dialog.features.includes("dialog=no"));
  assert.equal(r.context.LibrarySync.panel.arguments[0].selected, true);
  r.context.shutdown(); assert.equal(r.counters.destroyed, 1);
  assert.equal(r.docs.getElementById("menu_ToolsPopup").children.length, 0); assert.equal(r.counters.launches.length, 0);
});

test("panel ready handshake cancels the bounded watch and keeps the window open", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  r.context.LibrarySync.PANEL_READY_TIMEOUT_MS = 15;
  r.context.LibrarySync.openPanel(r.win, false);
  const dialog = r.context.LibrarySync.panel;
  assert.ok(dialog); assert.ok(r.context.LibrarySync.panelWatch);
  // The panel's explicit ready signal settles the host handshake.
  await dialog.emit("library-sync-ready");
  assert.equal(r.context.LibrarySync.panelWatch, null);
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(dialog.closed, false);            // no timeout fallback may fire after ready
  assert.equal(r.context.LibrarySync.panel, dialog);
  r.context.LibrarySync.panelReady(dialog);      // the host callback path is idempotent
  assert.equal(r.context.LibrarySync.panelWatch, null);
  r.context.shutdown();
});

test("a panel that never signals ready is diagnosed, closed and can be reopened", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  r.context.LibrarySync.PANEL_READY_TIMEOUT_MS = 10;
  r.context.LibrarySync.openPanel(r.win, false);
  const dead = r.context.LibrarySync.panel;
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(dead.closed, true);                       // the bounded timeout closed the dead window
  assert.equal(r.context.LibrarySync.panel, null);       // no stale panel reference survives
  const note = dead.document.getElementById("message");
  assert.equal(note.getAttribute("data-error"), "true");
  assert.match(note.textContent, /就绪信号|初始化未在限定时间内完成/);
  assert.equal(r.context.LibrarySync.panelWatch, null);
  // The failed window is reopenable: a fresh dialog becomes the panel.
  r.context.LibrarySync.PANEL_READY_TIMEOUT_MS = 30000;
  r.context.LibrarySync.openPanel(r.win, false);
  assert.notEqual(r.context.LibrarySync.panel, dead);
  assert.equal(r.counters.opens, 2);
  r.context.shutdown();
});

test("openDialog failure never leaves a dead panel reference and reports via the host", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const original = r.win.openDialog;
  r.win.openDialog = () => { throw new Error("chrome registration failed (synthetic)"); };
  r.context.LibrarySync.openPanel(r.win, false);
  assert.equal(r.context.LibrarySync.panel, null);
  assert.equal(r.context.LibrarySync.panelWatch, null);
  r.win.openDialog = original;
  r.context.LibrarySync.openPanel(r.win, false);
  assert.ok(r.context.LibrarySync.panel);   // the menu still opens a fresh panel
  r.context.shutdown();
});

test("panel unload drops the panel reference so the menu opens a new window", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  r.context.LibrarySync.PANEL_READY_TIMEOUT_MS = 30000;
  r.context.LibrarySync.openPanel(r.win, false);
  const first = r.context.LibrarySync.panel;
  first.close();                       // the user closes the panel window
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(r.context.LibrarySync.panel, null);
  r.context.LibrarySync.openPanel(r.win, false);
  assert.equal(r.counters.opens, 2);
  r.context.shutdown();
});
test("Gecko adapter uses only nsIProcess argument array; only two prefs persist", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const client = r.context.LibrarySync.createClient();
  const args = [PATHS.repo + "\\tools\\library_plugin_bridge.py", "--serve", "--apply"];
  await client.io.launch(PATHS.pythonw, args);
  assert.equal(r.counters.launches.length, 1); assert.deepEqual(r.counters.launches[0].args, args);
  assert.equal(r.counters.launches[0].path, PATHS.pythonw);
  r.context.LibrarySync.setPaths(PATHS); assert.equal(r.counters.writes.length, 2);
  assert.deepEqual(r.counters.writes.map(x => x[0]).sort(), ["extensions.library-sync.pythonw", "extensions.library-sync.repo"]);
  const response = await client.io.request("http://127.0.0.1:48765/v1/status", { method: "GET", redirect: "error" });
  assert.equal(response.data.active, false); r.context.shutdown();
});
test("selection resolves attachment parent, deduplicates and rejects group keys", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const paper = { key: "ABCDEFG2", libraryID: 1, isRegularItem: () => true, getField: () => "标题" };
  const attachment = { parentItemID: 42, isRegularItem: () => false };
  r.context.Zotero.Items.get = () => paper; r.win.ZoteroPane.getSelectedItems = () => [paper, attachment];
  assert.equal(r.context.LibrarySync.getSelection().items.length, 1);
  paper.libraryID = 2; assert.throws(() => r.context.LibrarySync.getSelection(), { code: "SELECTION" }); r.context.shutdown();
});

test("selected PDF identifies a replacement source without inferring multiple PDFs", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const paper = { key: "ABCDEFG2", libraryID: 1, isRegularItem: () => true, getField: () => "Title" };
  const pdf = { key: "PDFTEST1", parentItemID: 42, isRegularItem: () => false,
    isAttachment: () => true, attachmentContentType: "application/pdf" };
  r.context.Zotero.Items.get = () => paper;
  r.win.ZoteroPane.getSelectedItems = () => [paper, pdf];
  assert.equal(r.context.LibrarySync.getSelection().items[0].primary_key, "PDFTEST1");
  r.win.ZoteroPane.getSelectedItems = () => [pdf, { ...pdf, key: "PDFTEST2" }];
  assert.throws(() => r.context.LibrarySync.getSelection(), { code: "SELECTION" });
  r.win.ZoteroPane.getSelectedItems = () => [paper];
  assert.equal(r.context.LibrarySync.getSelection().items[0].primary_key, undefined);
  r.context.shutdown();
});


test("official references only open a fixed URL in the system browser", async () => {
  const r = runtime(); await r.context.startup({ rootURI: "chrome://test/" });
  const opened = []; r.context.Zotero.launchURL = url => opened.push(url);
  r.context.LibrarySync.openReference("glm53");
  r.context.LibrarySync.openReference("deepseek-request");
  assert.deepEqual(opened, ["https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3", "https://api-docs.deepseek.com/api/create-chat-completion/"]);
  for (const id of ["https://example.org", "__proto__", "constructor", null, {}]) {
    assert.throws(() => r.context.LibrarySync.openReference(id), { code: "INPUT" });
  }
  assert.equal(opened.length, 2); assert.equal(r.counters.launches.length, 0);
  r.context.LibrarySync.openPanel(r.win, false);
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"));
  assert.equal(new URL(r.counters.dialog.url).searchParams.get("v"), manifest.version);
  r.context.shutdown();
});
