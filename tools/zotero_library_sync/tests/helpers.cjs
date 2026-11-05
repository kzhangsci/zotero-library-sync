"use strict";
const fs = require("node:fs");
const path = require("node:path");
const Bridge = require("../bridge-client.js");
const Panel = require("../panel.js");
const ROOT = path.resolve(__dirname, "..");
const OP = "12345678-1234-4123-8123-123456789abc";
const RUN = "20261023_120000_abcdef";
const PATHS = { repo: "C:\\test repo\\文献", pythonw: "C:\\Python312\\pythonw.exe" };
const TOKEN = "synthetic-descriptor-canary-only";
const MANIFEST = { schema: 1, host: "127.0.0.1", port: 48765, pid: 123, token: TOKEN };
function config() {
  const value = Bridge.defaults(); value.qc.model = "glm-test-model";
  value.zh.model = "deepseek-test-model"; value.zh.provider = "deepseek";
  return { configured: true, config: value, credentials: { glm: true, deepseek: true } };
}
function fixture() {
  const f = { requests: [], launches: [], reads: [], sleeps: [], state: { active: false, view: null, text: "当前无任务" },
    publicConfig: config(), operationStatus: "completed", manifest: { ...MANIFEST }, online: true,
    keys: Object.fromEntries(["glm", "deepseek", "mineru"].map(p => [p, "synthetic-reveal-only-" + p])),
    preview: { preview_id: RUN, text: "将处理一篇文献", items: [{ key: "ABCDEFG2", citekey: "Sample2026", title: "示例" }], deferred: [] } };
  f.io = {
    async readJSON(p) { f.reads.push(p); if (!f.manifest) throw { code: "MISSING" }; return { ...f.manifest }; },
    async request(url, options) {
      const request = { url, ...options }; f.requests.push(request);
      if (f.override) { const result = await f.override(request); if (result) return result; }
      if (!f.online) throw new Error("synthetic network failure");
      const route = new URL(url).pathname;
      let data;
      if (route === "/v1/status") data = { capabilities: ["keys-v1", "mineru-v1", "translation-policy-v1", "model-choices-v1", "credential-reveal-v1", "model-profiles-v2", "model-test-role-v1"], ...f.state };
      else if (route === "/v1/config") {
        if (options.method === "POST") {
          const body = JSON.parse(options.body);
          f.publicConfig = { ...f.publicConfig, configured: true, config: body.config };
          for (const [p, key] of Object.entries(body.keys)) if (key) { f.publicConfig.credentials[p] = true; f.keys[p] = key; }
        }
        data = f.publicConfig;
      } else if (route === "/v1/keys") {
        for (const [p, key] of Object.entries(JSON.parse(options.body).keys)) if (key) { f.publicConfig.credentials[p] = true; f.keys[p] = key; }
        data = f.publicConfig;
      } else if (route === "/v1/mineru") {
        if (options.method === "POST" && JSON.parse(options.body).key) f.keys.mineru = JSON.parse(options.body).key;
        data = { configured: true, saved: true, source: "encrypted" };
      } else if (route === "/v1/credentials/reveal") {
        const provider = JSON.parse(options.body).provider;
        data = { provider, key: f.keys[provider], source: "encrypted" };
      } else if (["/v1/preview", "/v1/test", "/v1/models"].includes(route)) data = { operation: OP };
      else if (route === "/v1/operations/" + OP) data = { status: f.operationStatus, result: f.operationResult ?? f.preview, error: f.operationError };
      else if (route === "/v1/start") { data = { run_id: RUN, started: true }; f.state = { ...f.state, active: true }; }
      else throw new Error("Unexpected route");
      return { status: 200, data: structuredClone(data) };
    },
    async launch(exe, args) { f.launches.push({ exe, args }); if (f.onLaunch) f.onLaunch(); },
    async sleep(ms) { f.sleeps.push(ms); }
  };
  f.client = new Bridge.Client(f.io, () => PATHS);
  f.posts = route => f.requests.filter(r => r.method === "POST" && new URL(r.url).pathname === route);
  return f;
}
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = new Map(); this.value = ""; this._text = ""; this.disabled = false; this.id = ""; }
  set innerHTML(_) { throw new Error("Markup injection forbidden"); }
  get innerHTML() { throw new Error("Markup inspection forbidden"); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(""); }
  appendChild(el) { el.parent = this; el.parentNode = this; this.children.push(el); return el; }
  replaceChildren(...children) { this._text = ""; this.children = []; children.forEach(x => this.appendChild(x)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  scrollIntoView(options) { this.scrollOptions = options; }
  focus(options) { this.focusOptions = options; }
  addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(fn); }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  async emit(name, data = {}) {
    if (name === "click" && this.disabled) return;
    const event = { target: this, preventDefault() { this.defaultPrevented = true; }, ...data };
    for (let current = this; current; current = current.parentNode) {
      for (const fn of current.listeners.get(name) || []) await fn(event);
    }
    return event;
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
}
class Document extends Element {
  constructor() {
    super("document"); this.hidden = false;
    this.elements = new Map();
    const markup = fs.readFileSync(path.join(ROOT, "panel.xhtml"), "utf8");
    for (const match of markup.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
      const el = new Element(); el.ownerDocument = this; el.id = match[1];
      // Mirror the real DOM: markup attributes stay readable via getAttribute.
      for (const attr of match[0].matchAll(/([:a-zA-Z-]+)="([^"]*)"/g)) el.attrs[attr[1]] = attr[2];
      el.value = /\bvalue="([^"]*)"/.exec(match[0])?.[1] || "";
      el.type = /\btype="([^"]*)"/.exec(match[0])?.[1] || "text";
      el.checked = /\bchecked="checked"/.test(match[0]);
      el.hidden = /\bhidden="hidden"/.test(match[0]);
      this.elements.set(el.id, el);
    }
    this.elements.get("chunk-parallel").value = "1";
  }
  getElementById(id) { return this.elements.get(id) || null; }
  createElementNS(ns, tag) { const el = new Element(tag); el.ownerDocument = this; return el; }
  createXULElement(tag) { return new Element(tag); }
}
function ui(f = fixture()) {
  const doc = new Document(); const intervals = new Map(); const timeouts = new Map(); let seq = 0;
  const window = Object.assign(new Element("window"), { document: doc, closed: false, confirms: [], confirmResult: true,
    setInterval(fn, ms) { intervals.set(++seq, { fn, ms }); return seq; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn, ms) { timeouts.set(++seq, { fn, ms }); return seq; },
    clearTimeout(id) { timeouts.delete(id); },
    confirm(value) { this.confirms.push(value); return this.confirmResult; },
    close() { this.closed = true; controller.close(); } });
  let saved = { ...PATHS };
  const host = { getPaths: () => saved, setPaths(value) { saved = Bridge.paths(value); return saved; },
    createClient: () => new Bridge.Client(f.io, () => saved),
    getSelection: () => ({ items: [{ key: "ABCDEFG2", title: "当前条目" }], ignored: 0 }), panelClosed() {} };
  const controller = new Panel.Controller(window, host, f.client);
  return { f, controller, window, host, doc, intervals, timeouts, el: id => doc.getElementById(id) };
}
module.exports = { Bridge, Panel, ROOT, OP, RUN, PATHS, TOKEN, MANIFEST, fixture, ui, Element, Document };
