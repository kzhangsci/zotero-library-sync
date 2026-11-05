/* Inline HTML picker for the Zotero panel; no native popup or privileged APIs. */
var LibrarySyncPicker = (() => {
  "use strict";

  const HTML = "http://www.w3.org/1999/xhtml";
  let sequence = 0;

  function normalize(options) {
    if (!Array.isArray(options)) throw new TypeError("Picker options must be an array");
    return options.map(option => {
      if (typeof option === "string") return { value: option, label: option };
      if (option && typeof option.value === "string" && typeof option.label === "string") {
        return { value: option.value, label: option.label };
      }
      throw new TypeError("Picker options must be strings or {value, label} strings");
    });
  }

  class Picker {
    constructor({ input, button, list }, { options = [], onSelect = () => {}, editable = true, searchable = false } = {}) {
      const normalized = normalize(options);
      if (typeof onSelect !== "function") throw new TypeError("Picker onSelect must be a function");
      if (typeof editable !== "boolean") throw new TypeError("Picker editable must be a boolean");
      const document = input?.ownerDocument || list?.ownerDocument || button?.ownerDocument;
      if (!input || !button || !list || !document?.createElementNS) {
        throw new TypeError("Picker requires input, button and list DOM elements");
      }
      this._input = input;
      this._button = button;
      this._list = list;
      this._document = document;
      this._onSelect = onSelect;
      this._searchable = searchable && editable;
      this._allOptions = [];
      this._id = ++sequence;
      this._disabled = Boolean(input.disabled || button.disabled);
      this._destroyed = false;
      this._expanded = false;
      this._active = -1;
      this._options = [];
      this._rows = [];
      this._listeners = [];

      if (!list.id) {
        let id = `library-sync-picker-${this._id}`;
        while (document.getElementById?.(id)) id = `library-sync-picker-${++sequence}`;
        list.id = id;
      }
      list.setAttribute("role", "listbox");
      input.readOnly = !editable;
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", this._searchable ? "list" : "none");
      input.setAttribute("aria-readonly", String(!editable));
      button.setAttribute("type", "button");
      for (const element of [input, button]) {
        element.setAttribute("aria-controls", list.id);
        element.setAttribute("aria-haspopup", "listbox");
      }
      this.setOptions(normalized);
      this.setDisabled(this._disabled);

      this._listen(button, "click", event => {
        event.preventDefault();
        if (this._unavailable()) return;
        if (this._expanded) this.close();
        else { if (this._options !== this._allOptions) this._render(this._allOptions); this._open(1); }
      });
      this._listen(list, "click", event => {
        const index = this._rowIndex(event.target);
        if (index < 0) return;
        event.preventDefault();
        this._select(index);
      });
      for (const element of [input, button, list]) {
        this._listen(element, "keydown", event => this._keydown(event));
      }
      // Editing is independent of picking. In particular, an old highlighted
      // suggestion must not overwrite a newly typed model on a later Enter.
      this._listen(input, "input", event => {
        if (!this._searchable || this._disabled || event.isComposing) { this.close(); return; }
        const query = input.value.trim().toLocaleLowerCase();
        this._render(this._allOptions.filter(option => !query ||
          (option.value + " " + option.label).toLocaleLowerCase().includes(query)));
        if (this._rows.length) {
          this._open(1);
          // Typing only filters; Enter needs an explicit arrow or pointer choice.
          this._highlight(-1);
        }
      });
      this._listen(input, "compositionstart", () => this.close());
      for (const type of ["click", "focusin"]) {
        this._listen(document, type, event => {
          if (!this._inside(event.target)) this.close();
        });
      }
    }

    _listen(element, type, listener) {
      element.addEventListener(type, listener);
      this._listeners.push([element, type, listener]);
    }

    _inside(target) {
      for (let node = target; node; node = node.parentNode) {
        if (node === this._input || node === this._button || node === this._list) return true;
      }
      return false;
    }

    _rowIndex(target) {
      for (let node = target; node && node !== this._list; node = node.parentNode) {
        const index = this._rows.indexOf(node);
        if (index >= 0 && node.parentNode === this._list) return index;
      }
      return -1;
    }

    _unavailable() {
      return this._destroyed || this._disabled || this._input.disabled || this._button.disabled;
    }

    _highlight(index) {
      this._active = index;
      this._rows.forEach((row, i) => row.setAttribute("aria-selected", String(i === index)));
      const row = this._rows[index];
      if (row) {
        this._input.setAttribute("aria-activedescendant", row.id);
        row.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      } else {
        this._input.removeAttribute("aria-activedescendant");
      }
    }

    _open(direction) {
      if (this._unavailable() || !this._rows.length) return;
      this._expanded = true;
      this._list.hidden = false;
      for (const element of [this._input, this._button]) element.setAttribute("aria-expanded", "true");
      const selected = this._options.findIndex(option => option.value === this._input.value);
      this._highlight(selected >= 0 ? selected : (direction < 0 ? this._rows.length - 1 : 0));
      this._input.focus();
    }

    _select(index) {
      if (this._unavailable() || !this._expanded || !this._options[index]) return;
      const value = this._options[index].value;
      this._input.value = value;
      this.close();
      this._input.focus();
      this._onSelect(value);
    }

    _keydown(event) {
      if (this._unavailable() || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!this._rows.length) return;
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        if (!this._expanded) this._open(direction);
        else {
          const focused = this._rowIndex(event.target);
          const current = focused >= 0 ? focused : this._active;
          this._highlight(current < 0 ? (direction < 0 ? this._rows.length - 1 : 0)
            : (current + direction + this._rows.length) % this._rows.length);
          this._input.focus();
        }
      } else if (event.key === "Enter" && this._expanded) {
        event.preventDefault();
        // A mouse-focused option may differ from the input's active descendant.
        const focused = this._rowIndex(event.target);
        this._select(focused >= 0 ? focused : this._active);
      } else if (event.key === "Escape" && this._expanded) {
        event.preventDefault();
        this.close();
        this._input.focus();
      } else if (event.key === "Tab") {
        this.close();
      }
    }

    setOptions(options) {
      if (this._destroyed) return;
      const normalized = normalize(options);
      this._allOptions = normalized;
      this._render(normalized);
    }

    _render(normalized) {
      const rows = normalized.map((option, index) => {
        const row = this._document.createElementNS(HTML, "button");
        row.setAttribute("type", "button");
        row.setAttribute("role", "option");
        row.setAttribute("class", "picker-option");
        row.setAttribute("aria-selected", "false");
        row.id = `${this._list.id}-option-${this._id}-${index}`;
        row.tabIndex = -1;
        row.disabled = this._disabled;
        row.textContent = option.label;
        return row;
      });
      this.close();
      this._options = normalized;
      this._rows = rows;
      this._list.replaceChildren(...rows);
      this._button.disabled = this._disabled || this._allOptions.length === 0;
      this._button.setAttribute("aria-disabled", String(this._button.disabled));
    }

    setDisabled(disabled) {
      if (this._destroyed) return;
      if (typeof disabled !== "boolean") throw new TypeError("Picker disabled must be a boolean");
      this._disabled = disabled;
      this._input.disabled = disabled;
      this._button.disabled = disabled || this._allOptions.length === 0;
      this._input.setAttribute("aria-disabled", String(disabled));
      this._button.setAttribute("aria-disabled", String(this._button.disabled));
      this._rows.forEach(row => { row.disabled = disabled; });
      if (disabled) this.close();
    }

    close() {
      if (this._destroyed) return;
      this._expanded = false;
      this._list.hidden = true;
      for (const element of [this._input, this._button]) element.setAttribute("aria-expanded", "false");
      this._highlight(-1);
    }

    destroy() {
      if (this._destroyed) return;
      this.close();
      this._destroyed = true;
      for (const [element, type, listener] of this._listeners) element.removeEventListener(type, listener);
      this._listeners = [];
      this._rows = [];
      this._options = [];
      this._allOptions = [];
      this._onSelect = null;
      this._list.replaceChildren();
      // An empty option set disables only the toggle, not the whole widget.
      // Do not leave that derived state behind when the panel remounts it.
      this._button.disabled = this._disabled;
      this._button.setAttribute("aria-disabled", String(this._disabled));
    }
  }

  return { Picker };
})();

if (typeof module !== "undefined" && module.exports) module.exports = LibrarySyncPicker;
