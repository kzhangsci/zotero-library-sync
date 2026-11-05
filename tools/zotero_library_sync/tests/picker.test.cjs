"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { Picker } = require("../picker.js");

// A deliberately small DOM with bubbling mouse/keyboard events. No panel,
// service, XUL, browser, credential store or real UI is loaded by this suite.
class Event {
  constructor(type, init = {}) {
    Object.assign(this, { type, bubbles: true, target: null, defaultPrevented: false }, init);
  }
  preventDefault() { this.defaultPrevented = true; }
}
class MouseEvent extends Event {}
class KeyboardEvent extends Event {}

class Element {
  constructor(document, tagName, namespaceURI = "http://www.w3.org/1999/xhtml") {
    Object.assign(this, { ownerDocument: document, tagName, namespaceURI, parentNode: null,
      children: [], listeners: new Map(), attrs: new Map(), value: "", disabled: false,
      readOnly: false, hidden: false, _text: "" });
  }
  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  removeAttribute(name) { this.attrs.delete(name); }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering is forbidden"); }
  get innerHTML() { throw new Error("HTML parsing is forbidden"); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  appendChild(child) {
    if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(node => node !== child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = "";
    for (const child of children) this.appendChild(child);
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    // Snapshot the propagation path, as the DOM does when a handler removes a row.
    const path = [this];
    if (event.bubbles) for (let node = this.parentNode; node; node = node.parentNode) path.push(node);
    for (const node of path) {
      event.currentTarget = node;
      for (const listener of [...(node.listeners.get(event.type) || [])]) listener(event);
    }
    return !event.defaultPrevented;
  }
  focus() {
    this.ownerDocument.activeElement = this;
    this.dispatchEvent(new Event("focusin"));
  }
  scrollIntoView(options) { this.scrollOptions = options; }
  listenerCount() { return [...this.listeners.values()].reduce((total, listeners) => total + listeners.size, 0); }
}

class Document extends Element {
  constructor() { super(null, "#document", null); this.created = []; }
  createElementNS(namespace, tag) {
    assert.equal(namespace, "http://www.w3.org/1999/xhtml");
    assert.equal(tag, "button", "only inline HTML option buttons may be created");
    const element = new Element(this, tag, namespace);
    this.created.push(element);
    return element;
  }
  createElement() { throw new Error("Use the explicit HTML namespace inside XUL documents"); }
  getElementById(id) {
    function find(node) {
      if (node.id === id) return node;
      for (const child of node.children) { const result = find(child); if (result) return result; }
      return null;
    }
    return find(this);
  }
}

function fixture(options = ["first", { value: "second-id", label: "第二项" }, "third"], settings = {}) {
  const document = settings.document || new Document();
  const container = document.appendChild(new Element(document, "div"));
  const input = container.appendChild(new Element(document, "input"));
  input.value = settings.value ?? "hand-written-model";
  const button = container.appendChild(new Element(document, "button"));
  const list = container.appendChild(new Element(document, "div"));
  const selected = [];
  const picker = new Picker({ input, button, list }, {
    options, editable: settings.editable ?? true, searchable: settings.searchable ?? false,
    onSelect(value) {
      // Both event paths must commit and close before notifying the panel.
      assert.equal(input.value, value);
      assert.equal(list.hidden, true);
      assert.equal(button.getAttribute("aria-expanded"), "false");
      selected.push(value);
      settings.onSelect?.(value);
    }
  });
  const click = element => element.dispatchEvent(new MouseEvent("click", { button: 0 }));
  const key = (name, element = input, init = {}) => {
    const event = new KeyboardEvent("keydown", { key: name, ...init });
    element.dispatchEvent(event);
    return event;
  };
  const count = () => [document, input, button, list, ...document.created]
    .reduce((total, element) => total + element.listenerCount(), 0);
  return { picker, document, container, input, button, list, selected, click, key, count };
}

test("exports the same Picker contract in CommonJS and a plain browser script", () => {
  assert.deepEqual(Object.keys(require("../picker.js")), ["Picker"]);
  const sandbox = {};
  vm.runInNewContext(fs.readFileSync(require.resolve("../picker.js"), "utf8"), sandbox);
  assert.equal(typeof sandbox.LibrarySyncPicker.Picker, "function");
  for (const method of ["setOptions", "setDisabled", "close", "destroy"]) {
    assert.equal(typeof Picker.prototype[method], "function");
  }
});

test("mouse click selects a labeled option exactly once through real bubbling handlers", () => {
  const u = fixture();
  assert.equal(u.input.readOnly, false);
  assert.equal(u.list.hidden, true);
  u.click(u.button);
  assert.equal(u.list.hidden, false);
  assert.equal(u.button.getAttribute("aria-expanded"), "true");
  assert.equal(u.input.getAttribute("aria-expanded"), "true");
  assert.equal(u.input.getAttribute("aria-controls"), u.list.id);
  assert.equal(u.list.getAttribute("role"), "listbox");
  const option = u.list.children[1];
  assert.equal(option.tagName, "button");
  assert.equal(option.getAttribute("type"), "button");
  assert.equal(option.getAttribute("role"), "option");
  assert.equal(option.textContent, "第二项");
  option.focus(); // Input blur must not close the list before the mouse's click.
  assert.equal(u.list.hidden, false);
  u.click(option);
  assert.equal(u.input.value, "second-id");
  assert.deepEqual(u.selected, ["second-id"]);
  assert.equal(u.document.activeElement, u.input);
  u.picker.destroy();
});

test("toggle opens and closes without selecting or rewriting manual text", () => {
  const u = fixture();
  u.click(u.button); u.click(u.button);
  assert.equal(u.list.hidden, true);
  assert.equal(u.input.value, "hand-written-model");
  assert.equal(u.button.getAttribute("aria-expanded"), "false");
  assert.deepEqual(u.selected, []);
  u.picker.destroy();
});

test("arrow navigation highlights without committing; Enter shares the mouse selection path", () => {
  const u = fixture();
  assert.equal(u.key("ArrowDown").defaultPrevented, true);
  assert.equal(u.list.children[0].getAttribute("aria-selected"), "true");
  assert.equal(u.input.value, "hand-written-model");
  u.key("ArrowDown");
  const active = u.list.children[1];
  assert.equal(active.getAttribute("aria-selected"), "true");
  assert.equal(u.input.getAttribute("aria-activedescendant"), active.id);
  assert.deepEqual(active.scrollOptions, { block: "nearest", inline: "nearest" });
  assert.deepEqual(u.selected, []);
  u.key("Enter");
  assert.deepEqual(u.selected, ["second-id"]);
  assert.equal(u.input.getAttribute("aria-activedescendant"), null);
  // Closed Enter must leave a manually entered value alone.
  u.input.value = "custom";
  assert.equal(u.key("Enter").defaultPrevented, false);
  assert.equal(u.input.value, "custom");
  assert.deepEqual(u.selected, ["second-id"]);
  u.picker.destroy();
});

test("ArrowUp opens at the last option, wraps, and existing values reopen at their match", () => {
  const u = fixture();
  u.key("ArrowUp", u.button);
  assert.equal(u.list.children[2].getAttribute("aria-selected"), "true");
  u.key("ArrowDown");
  assert.equal(u.list.children[0].getAttribute("aria-selected"), "true");
  u.key("ArrowUp"); u.key("Enter");
  assert.deepEqual(u.selected, ["third"]);
  u.click(u.button);
  assert.equal(u.list.children[2].getAttribute("aria-selected"), "true");
  u.picker.destroy();
});

test("Enter from a mouse-focused option selects that option once", () => {
  const u = fixture();
  u.click(u.button);
  const row = u.list.children[1];
  row.focus();
  assert.equal(u.key("Enter", row).defaultPrevented, true);
  assert.deepEqual(u.selected, ["second-id"]);
  // A subsequent synthetic click cannot repeat selection after close.
  u.click(row);
  assert.deepEqual(u.selected, ["second-id"]);
  u.picker.destroy();
});

test("arrow navigation continues from a mouse-focused row and returns focus to the input", () => {
  const u = fixture();
  u.click(u.button);
  const row = u.list.children[1];
  row.focus();
  u.key("ArrowDown", row);
  assert.equal(u.document.activeElement, u.input);
  assert.equal(u.list.children[2].getAttribute("aria-selected"), "true");
  u.key("Enter", u.document.activeElement);
  assert.deepEqual(u.selected, ["third"]);
  u.picker.destroy();
});

test("Escape and Tab close without committing; Tab retains ordinary focus navigation", () => {
  const u = fixture();
  u.click(u.button);
  assert.equal(u.key("Escape").defaultPrevented, true);
  assert.equal(u.list.hidden, true);
  assert.equal(u.input.value, "hand-written-model");
  u.click(u.button);
  assert.equal(u.key("Tab").defaultPrevented, false);
  assert.equal(u.list.hidden, true);
  assert.deepEqual(u.selected, []);
  u.picker.destroy();
});

test("typing closes stale suggestions and never calls onSelect or changes typed text", () => {
  const u = fixture();
  u.click(u.button); u.key("ArrowDown");
  u.input.value = "  custom-model.v2  ";
  u.input.dispatchEvent(new Event("input"));
  assert.equal(u.list.hidden, true);
  u.key("Enter");
  u.picker.setOptions(["replacement"]);
  assert.equal(u.input.value, "  custom-model.v2  ");
  assert.deepEqual(u.selected, []);
  u.picker.destroy();
});

test("IME composition and modified arrow keys do not commit or consume editing keys", () => {
  const u = fixture();
  u.click(u.button);
  assert.equal(u.key("Enter", u.input, { isComposing: true }).defaultPrevented, false);
  for (const modifier of ["altKey", "ctrlKey", "metaKey", "shiftKey"]) {
    assert.equal(u.key("ArrowDown", u.input, { [modifier]: true }).defaultPrevented, false);
  }
  assert.equal(u.list.children[0].getAttribute("aria-selected"), "true");
  assert.deepEqual(u.selected, []);
  u.picker.destroy();
});

test("disabled picker closes, ignores forced mouse/keyboard events and re-enables cleanly", () => {
  const u = fixture();
  u.click(u.button);
  const row = u.list.children[1];
  u.picker.setDisabled(true);
  assert.equal(u.input.disabled, true);
  assert.equal(u.button.disabled, true);
  assert.equal(row.disabled, true);
  assert.equal(u.list.hidden, true);
  u.click(u.button); u.click(row); u.key("ArrowDown"); u.key("Enter");
  assert.equal(u.input.value, "hand-written-model");
  assert.deepEqual(u.selected, []);
  u.picker.setOptions(["new"]);
  assert.equal(u.list.children[0].disabled, true);
  u.picker.setDisabled(false);
  assert.equal(u.input.disabled, false);
  assert.equal(u.button.disabled, false);
  u.click(u.button); u.click(u.list.children[0]);
  assert.deepEqual(u.selected, ["new"]);
  u.picker.destroy();
});

test("external native disabled flags also prevent callbacks", () => {
  for (const name of ["input", "button"]) {
    const u = fixture();
    u.click(u.button); u[name].disabled = true;
    u.click(u.list.children[0]); u.key("Enter");
    assert.deepEqual(u.selected, []);
    assert.equal(u.input.value, "hand-written-model");
    u.picker.destroy();
  }
});

test("empty options preserve editable input and never expose an empty selection", () => {
  const u = fixture([]);
  assert.equal(u.button.disabled, true);
  assert.equal(u.input.disabled, false);
  assert.equal(u.input.readOnly, false);
  u.click(u.button); u.key("ArrowDown"); u.key("ArrowUp"); u.key("Enter");
  assert.equal(u.list.hidden, true);
  assert.equal(u.input.value, "hand-written-model");
  assert.deepEqual(u.selected, []);
  u.picker.setOptions([""]);
  assert.equal(u.button.disabled, false);
  u.click(u.button); u.key("Enter");
  assert.deepEqual(u.selected, [""]);
  u.picker.setOptions([]);
  assert.equal(u.list.children.length, 0);
  assert.equal(u.list.hidden, true);
  u.picker.destroy();
});

test("replacing options invalidates highlighted indices and detached stale buttons", () => {
  const supplied = [{ value: "original", label: "original label" }];
  const u = fixture(supplied);
  supplied[0].value = "caller mutation";
  u.click(u.button); u.click(u.list.children[0]);
  assert.deepEqual(u.selected, ["original"]);
  u.click(u.button);
  const stale = u.list.children[0];
  u.picker.setOptions([{ value: "fresh", label: "fresh label" }]);
  assert.equal(stale.parentNode, null);
  u.key("Enter"); u.click(stale);
  assert.deepEqual(u.selected, ["original"]);
  u.click(u.button);
  // Even a stale node reattached by external code has no live option identity.
  u.list.appendChild(stale);
  u.click(stale);
  assert.deepEqual(u.selected, ["original"]);
  u.click(u.list.children[0]);
  assert.deepEqual(u.selected, ["original", "fresh"]);
  u.picker.destroy();
});

test("option text and values stay inert; all rows use namespace-qualified HTML buttons", () => {
  const hostile = '<img src=x onerror="evil()"><script>evil()</script> & "';
  const u = fixture([{ value: hostile, label: hostile }]);
  const row = u.list.children[0];
  assert.equal(row.namespaceURI, "http://www.w3.org/1999/xhtml");
  assert.equal(row.getAttribute("class"), "picker-option");
  assert.equal(row.textContent, hostile);
  assert.equal(row.children.length, 0);
  assert.equal(row.getAttribute("onclick"), null);
  assert.equal(row.getAttribute("href"), null);
  u.click(u.button); u.click(row);
  assert.equal(u.input.value, hostile);
  assert.deepEqual(u.selected, [hostile]);
  u.picker.destroy();
});

test("outside clicks/focus close without stealing focus or changing values", () => {
  const u = fixture();
  const outside = u.document.appendChild(new Element(u.document, "input"));
  u.click(u.button); outside.focus();
  assert.equal(u.list.hidden, true);
  assert.equal(u.document.activeElement, outside);
  u.click(u.button); u.click(outside);
  assert.equal(u.list.hidden, true);
  assert.equal(u.input.value, "hand-written-model");
  assert.deepEqual(u.selected, []);
  u.picker.destroy();
});

test("setOptions never adds row listeners and destroy removes only its own listeners", () => {
  const u = fixture();
  let inputEvents = 0;
  const external = () => { inputEvents++; };
  u.input.addEventListener("input", external);
  const count = u.count();
  for (let i = 0; i < 60; i++) {
    u.picker.setOptions([`model-${i}`, `other-${i}`]);
    assert.equal(u.count(), count);
    assert.ok(u.document.created.every(row => row.listenerCount() === 0));
  }
  u.click(u.button);
  const stale = u.list.children[0];
  u.picker.destroy(); u.picker.destroy();
  assert.equal(u.count(), 1);
  assert.equal(u.list.hidden, true);
  assert.equal(u.list.children.length, 0);
  assert.equal(u.input.getAttribute("aria-activedescendant"), null);
  u.click(u.button); u.click(stale); u.key("ArrowDown"); u.key("Enter");
  u.input.value = "after closepanel";
  u.input.dispatchEvent(new Event("input"));
  assert.equal(inputEvents, 1);
  assert.equal(u.input.value, "after closepanel");
  assert.deepEqual(u.selected, []);
  const created = u.document.created.length;
  u.picker.setOptions(["after-destroy"]); u.picker.setDisabled(true); u.picker.close();
  assert.equal(u.document.created.length, created);
  assert.equal(u.input.disabled, false);
  assert.equal(u.count(), 1);
});

test("a new picker on the same markup does not call the destroyed picker", () => {
  const u = fixture();
  u.picker.destroy();
  const selected = [];
  const next = new Picker(u, { options: ["new-model"], onSelect: value => selected.push(value), editable: true });
  u.click(u.button); u.click(u.list.children[0]);
  assert.deepEqual(selected, ["new-model"]);
  assert.deepEqual(u.selected, []);
  next.destroy();
  assert.equal(u.count(), 0);
});

test("remount after an empty option set does not inherit an artificial disabled state", () => {
  const u = fixture([]);
  assert.equal(u.button.disabled, true);
  u.picker.destroy();
  const selected = [];
  const next = new Picker(u, { options: ["available"], onSelect: value => selected.push(value) });
  assert.equal(u.input.disabled, false);
  assert.equal(u.button.disabled, false);
  u.click(u.button); u.click(u.list.children[0]);
  assert.deepEqual(selected, ["available"]);
  next.destroy();
  assert.equal(u.count(), 0);
});

test("independent pickers have unique accessible IDs and selecting one does not select another", () => {
  const a = fixture();
  const b = fixture(["b"], { document: a.document });
  assert.notEqual(a.list.id, b.list.id);
  assert.notEqual(a.list.children[0].id, b.list.children[0].id);
  a.click(a.button);
  b.click(b.button);
  assert.equal(a.list.hidden, true);
  b.click(b.list.children[0]);
  assert.deepEqual(a.selected, []);
  assert.deepEqual(b.selected, ["b"]);
  a.picker.destroy(); b.picker.destroy();
  assert.equal(a.count(), 0);
});

test("editable false is readonly, but still allows mouse and keyboard selection", () => {
  const u = fixture(["fixed"], { editable: false });
  assert.equal(u.input.readOnly, true);
  assert.equal(u.input.getAttribute("aria-readonly"), "true");
  u.key("ArrowDown"); u.key("Enter");
  assert.deepEqual(u.selected, ["fixed"]);
  u.picker.setDisabled(true); u.picker.setDisabled(false);
  assert.equal(u.input.readOnly, true);
  u.picker.destroy();
});

test("invalid option updates fail atomically and preserve existing text and options", () => {
  const u = fixture();
  u.click(u.button);
  const row = u.list.children[0];
  const count = u.count();
  for (const invalid of [null, {}, "text", [null], [1], [{ value: "x" }], [{ value: 1, label: "x" }]]) {
    assert.throws(() => u.picker.setOptions(invalid), TypeError);
    assert.equal(u.list.children[0], row);
    assert.equal(u.list.hidden, false);
    assert.equal(u.input.value, "hand-written-model");
    assert.equal(u.count(), count);
  }
  assert.throws(() => u.picker.setDisabled("false"), TypeError);
  u.picker.destroy();
});

test("searchable model filters labels and IDs without committing on typing or plain Enter", () => {
  const u = fixture(["glm-alpha", "glm-beta", { value: "custom/gamma", label: "Gamma 专用" }], { searchable: true });
  assert.equal(u.input.getAttribute("aria-autocomplete"), "list");
  u.click(u.button);
  const old = u.list.children[0];
  u.input.value = " GAMMA "; u.input.dispatchEvent(new Event("input"));
  assert.equal(u.list.hidden, false); assert.equal(u.list.children.length, 1);
  assert.equal(u.list.children[0].textContent, "Gamma 专用");
  assert.equal(u.input.getAttribute("aria-activedescendant"), null);
  u.key("Enter"); u.click(old);
  assert.equal(u.input.value, " GAMMA "); assert.deepEqual(u.selected, []);
  u.key("ArrowDown"); u.key("Enter");
  assert.deepEqual(u.selected, ["custom/gamma"]);
  u.picker.destroy();
});

test("no search match preserves manual text and the toggle can reopen all candidates", () => {
  const u = fixture(["first", "second"], { searchable: true });
  u.input.value = "manual-unlisted"; u.input.dispatchEvent(new Event("input"));
  assert.equal(u.list.hidden, true); assert.equal(u.button.disabled, false);
  u.key("Enter"); assert.equal(u.input.value, "manual-unlisted");
  u.click(u.button); assert.equal(u.list.children.length, 2);
  u.key("Escape"); assert.equal(u.input.value, "manual-unlisted");
  u.input.value = "second"; u.input.dispatchEvent(new Event("input"));
  u.key("ArrowUp"); u.key("Enter"); assert.deepEqual(u.selected, ["second"]);
  u.picker.destroy();
});

test("search composition, disable and option refresh never overwrite a draft or select a stale row", () => {
  const u = fixture(["first", "second"], { searchable: true });
  u.click(u.button);
  u.input.dispatchEvent(new Event("compositionstart")); assert.equal(u.list.hidden, true);
  u.input.value = "first"; u.input.dispatchEvent(new Event("input", { isComposing: true }));
  u.key("Enter", u.input, { isComposing: true }); assert.deepEqual(u.selected, []);
  u.input.dispatchEvent(new Event("input")); const stale = u.list.children[0];
  u.picker.setOptions(["fresh"]); u.click(stale); u.key("Enter");
  assert.equal(u.input.value, "first"); assert.deepEqual(u.selected, []);
  u.picker.setDisabled(true); u.input.dispatchEvent(new Event("input"));
  assert.equal(u.list.hidden, true); assert.equal(u.input.value, "first");
  u.picker.destroy(); assert.equal(u.count(), 0);
});
