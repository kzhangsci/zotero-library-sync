/* Privileged Gecko adapter. It never uses Zotero.HTTP (which may debug-log bodies). */
LibrarySync = {
  panel: null,
  panelWatch: null,
  windows: new Map(),
  sourceWindow: null,
  // Bounded wait for the panel's explicit ready handshake. The panel signals
  // as soon as its UI is wired (before any bridge/model work), so provider or
  // network failures can never hold a usable window hostage past this limit.
  PANEL_READY_TIMEOUT_MS: 30000,
  init() { this.sourceWindow = null; this.panelWatch = null; },
  localFile(path) {
    try {
      const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
      file.initWithPath(path);
      return file.exists() && file.isFile();
    } catch (_) { return false; }
  },
  getPaths() {
    let repo = Zotero.Prefs.get("extensions.library-sync.repo", true) || "";
    let pythonw = Zotero.Prefs.get("extensions.library-sync.pythonw", true) || "";
    if (Services.appinfo?.OS === "Darwin") {
      if (!repo && !pythonw) {
        const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
        const candidate = home + "/Library/Application Support/LibrarySync/app";
        const executable = candidate + "/runtime/bin/python3";
        if (this.localFile(candidate + "/tools/library_plugin_bridge.py") && this.localFile(executable)) {
          repo = candidate; pythonw = executable;
        }
      }
      if (!pythonw && repo && this.localFile(repo + "/runtime/bin/python3")) pythonw = repo + "/runtime/bin/python3";
      return {repo, pythonw};
    }
    if (!repo && !pythonw) {
      // Directory service does not inspect Zotero profiles or application secrets.
      try {
        const local = Services.dirsvc.get("LocalAppData", Ci.nsIFile).path;
        const candidate = local.replace(/\\+$/, "") + "\\LibrarySync\\app";
        const executable = candidate + "\\runtime\\pythonw.exe";
        if (this.localFile(candidate + "\\tools\\library_plugin_bridge.py") && this.localFile(executable)) {
          repo = candidate; pythonw = executable;
        }
      } catch (_) {}
    }
    if (!pythonw && repo && this.localFile(repo + "\\runtime\\pythonw.exe")) pythonw = repo + "\\runtime\\pythonw.exe";
    return { repo, pythonw };
  },
  validatePaths(value) {
    const p = LibrarySyncBridge.paths(value);
    if (!this.localFile(LibrarySyncBridge.joinPath(p.repo, "tools", "library_plugin_bridge.py"))) {
      throw new LibrarySyncBridge.SafeError("PATH", "后台程序目录中没有 tools\\library_plugin_bridge.py。这里应填写同步程序目录，不是 Zotero 或 Obsidian 文献目录；只安装 XPI 不会安装后台程序。已有用户请使用原程序目录，新用户先安装对应系统的后台包。");
    }
    if (!this.localFile(p.pythonw)) {
      throw new LibrarySyncBridge.SafeError("PATH", "未找到 Python 程序。请填写 Windows 的 pythonw.exe 或 macOS 专用环境的 python3 路径，或先完成后台安装。已保存的模型和 Key 不需要重填。");
    }
    return p;
  },
  setPaths(value) {
    const p = this.validatePaths(value);
    Zotero.Prefs.set("extensions.library-sync.repo", p.repo, true);
    Zotero.Prefs.set("extensions.library-sync.pythonw", p.pythonw, true);
    return p;
  },
  requestLoopback(url, options) {
    // Privileged XHR supplied by Zotero: native timeout, direct-loopback channel,
    // no hiddenDOMWindow/AbortController dependency and no request-body logging.
    return new Promise((resolve, reject) => {
      let xhr, settled = false, stage = "url";
      const fail = (code, message, abort = false) => {
        if (settled) return;
        settled = true;
        reject(new LibrarySyncBridge.SafeError(code, message));
        if (abort && xhr) { try { xhr.abort(); } catch (_) {} }
      };
      try {
        const target = new URL(url);
        if (target.protocol !== "http:" || target.hostname !== "127.0.0.1" ||
            target.username || target.password || target.search || target.hash ||
            Number(target.port) < 1024 || Number(target.port) > 65535 ||
            !target.pathname.startsWith("/v1/")) {
          fail("TRANSPORT", "拒绝非本机 bridge 地址。"); return;
        }
        stage = "xhr-create";
        xhr = new XMLHttpRequest({ mozAnon: true });
        stage = "xhr-open";
        xhr.open(options.method, url, true);
        stage = "xhr-timeout";
        xhr.timeout = 10000;
        // mozAnon already disables cookies/ambient credentials. Gecko rejects
        // even withCredentials=false on anonymous XHR (InvalidStateError).
        // Leave its default false value untouched, rather than disabling mozAnon.
        stage = "channel";
        const channel = xhr.channel;
        // Only this 127.0.0.1 request bypasses proxies. No global prefs change.
        channel.QueryInterface(Ci.nsIHttpChannelInternal).bypassProxy = true;
        const previous = channel.notificationCallbacks;
        channel.notificationCallbacks = {
          QueryInterface: ChromeUtils.generateQI(["nsIChannelEventSink", "nsIInterfaceRequestor"]),
          getInterface(iid) {
            if (iid.equals(Ci.nsIChannelEventSink)) return this;
            if (previous) return previous.getInterface(iid);
            throw Cr.NS_ERROR_NO_INTERFACE;
          },
          asyncOnChannelRedirect(_old, _next, _flags, callback) {
            // Refuse before any token or request body can reach another URL.
            callback.onRedirectVerifyCallback(Cr.NS_BINDING_ABORTED);
            fail("REDIRECT", "bridge 返回重定向，已阻止；没有转发密钥或重发请求。", true);
          }
        };
        stage = "headers";
        for (const [name, value] of Object.entries(options.headers || {})) xhr.setRequestHeader(name, value);
        xhr.setRequestHeader("Cache-Control", "no-store");
        xhr.onerror = () => fail("NETWORK", "本机 bridge 连接失败；未自动重发写入请求。");
        xhr.ontimeout = () => fail("NETWORK", "本机 bridge 请求超时（10 秒）；未自动重发写入请求。");
        xhr.onabort = () => fail("NETWORK", "本机 bridge 请求已中断；未自动重发写入请求。");
        xhr.onprogress = event => {
          if (event.loaded > 4 * 1024 * 1024) fail("RESPONSE", "bridge 响应超过大小限制。", true);
        };
        xhr.onload = () => {
          if (settled) return;
          try {
            const status = xhr.status;
            let data = null;
            if ((status >= 200 && status < 300) || [400, 401, 403, 409, 422, 500, 503].includes(status)) {
              if (xhr.responseText.length > 4 * 1024 * 1024) {
                fail("RESPONSE", "bridge 响应超过大小限制。"); return;
              }
              data = JSON.parse(xhr.responseText);
            }
            settled = true; resolve({ status, data });
          } catch (_) { fail("RESPONSE", "bridge 返回的 JSON 无法读取；未显示响应正文。"); }
        };
        stage = "send";
        xhr.send(options.body ?? null);
      } catch (_) {
        // Stage is a fixed label, never an exception/body/header containing keys.
        fail("TRANSPORT", `插件本机连接组件失败（${stage}）；请更新插件或报告此阶段，勿反复启动服务。`, true);
      }
    });
  },
  createClient() {
    const host = this;
    return new LibrarySyncBridge.Client({
      async readJSON(path) {
        // The public installation config chooses the descriptor's data root.
        // Keep this adapter for older clients which still derive repo/.secrets.
        const p = LibrarySyncBridge.paths(host.getPaths());
        const legacy = LibrarySyncBridge.joinPath(p.repo, ".secrets", "plugin_bridge.json");
        if (path === legacy) {
          const configPath = LibrarySyncBridge.joinPath(p.repo, "library-sync.json");
          if (await IOUtils.exists(configPath)) {
            const info = await IOUtils.stat(configPath);
            if (info.size > 16384) throw new Error("Installation config size rejected");
            const config = JSON.parse(await IOUtils.readUTF8(configPath));
            if (config.schema_version !== 1 || typeof config.data_dir !== "string") {
              throw new Error("Installation config rejected");
            }
            // setup writes absolute local paths. Reject traversal/UNC/commands.
            const data = LibrarySyncBridge.paths({ repo: config.data_dir, pythonw: p.pythonw }).repo;
            path = LibrarySyncBridge.joinPath(data, ".secrets", "plugin_bridge.json");
          }
        }
        if (!await IOUtils.exists(path)) throw { code: "MISSING" };
        const info = await IOUtils.stat(path);
        if (info.size > 8192) throw new Error("Descriptor size rejected");
        return JSON.parse(await IOUtils.readUTF8(path));
      },
      request: (url, options) => this.requestLoopback(url, options),
      launch(executable, args) {
        // nsIProcess.runwAsync uses an argument array, never cmd/PowerShell/a shell.
        host.validatePaths({ repo: args[0].replace(/[\\/]tools[\\/]library_plugin_bridge\.py$/, ""), pythonw: executable });
        const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        file.initWithPath(executable);
        if (!file.exists() || !file.isFile() || !(/^(?:pythonw\.exe|python(?:3(?:\.13)?)?)$/i.test(file.leafName))) {
          throw new Error("Pythonw unavailable");
        }
        const script = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        script.initWithPath(args[0]);
        if (!script.exists() || !script.isFile()) throw new Error("Bridge unavailable");
        const process = Cc["@mozilla.org/process/util;1"].createInstance(Ci.nsIProcess);
        process.init(file);
        process.runwAsync(args, args.length, null, false);
      },
      sleep: ms => new Promise(resolve => setTimeout(resolve, ms))
    }, () => this.getPaths());
  },
  openReference(id) {
    const references = {
      "glm53": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
      "glm-request": "https://docs.bigmodel.cn/api-reference/模型-api/对话补全",
      "glm-thinking": "https://docs.bigmodel.cn/cn/guide/capabilities/thinking",
      "glm-params": "https://docs.bigmodel.cn/cn/guide/start/concept-param",
      "deepseek-thinking": "https://api-docs.deepseek.com/guides/thinking_mode/",
      "deepseek-request": "https://api-docs.deepseek.com/api/create-chat-completion/",
      "deepseek-context": "https://api-docs.deepseek.com/quick_start/pricing/"
    };
    if (typeof id !== "string" || !Object.prototype.hasOwnProperty.call(references, id)) {
      throw new LibrarySyncBridge.SafeError("INPUT", "未知的官方文档选项。");
    }
    Zotero.launchURL(references[id]);
  },
  openZoteroItem(key) {
    // Panel-triggered item navigation: validated key, user library only.
    if (typeof key !== "string" || !LibrarySyncBridge.ITEM_KEY.test(key)) {
      throw new LibrarySyncBridge.SafeError("INPUT", "无效的 Zotero 条目 key。");
    }
    const item = Zotero.Items.getByLibraryAndKey(Zotero.Libraries.userLibraryID, key);
    if (!item) throw new LibrarySyncBridge.SafeError("INPUT", "在“我的文库”中找不到该条目。");
    const win = Zotero.getMainWindow();
    if (win?.ZoteroPane?.selectItem) { win.ZoteroPane.selectItem(item.id); return; }
    Zotero.launchURL(`zotero://select/library/items/${key}`);
  },
  copyText(text) {
    // Called only from the explicit "复制诊断" click. The panel already
    // redacted the text; no keys or raw responses reach the clipboard.
    if (typeof text !== "string" || !text) return false;
    try { Zotero.Utilities.Internal.copyTextToClipboard(text); return true; } catch (_) {}
    try {
      Cc["@mozilla.org/widget/clipboardhelper;1"].getService(Ci.nsIClipboardHelper).copyString(text);
      return true;
    } catch (_) { return false; }
  },
  getSelection() {
    const win = this.sourceWindow && !this.sourceWindow.closed ? this.sourceWindow : Zotero.getMainWindow();
    const selected = win?.ZoteroPane?.getSelectedItems() || [];
    const result = [];
    let ignored = 0;
    for (let item of selected) {
      const selectedPDF = item.isAttachment?.() && item.attachmentContentType === "application/pdf" ? item.key : null;
      if (!item.isRegularItem() && item.parentItemID) item = Zotero.Items.get(item.parentItemID);
      if (!item?.isRegularItem() || item.deleted) { ignored++; continue; }
      // The bridge contract has keys, not library IDs. Never misroute a group key.
      if (item.libraryID !== Zotero.Libraries.userLibraryID) {
        throw new LibrarySyncBridge.SafeError("SELECTION", "当前协议仅支持“我的文库”条目；请取消群组文库的选择。");
      }
      if (!LibrarySyncBridge.ITEM_KEY.test(item.key)) {
        throw new LibrarySyncBridge.SafeError("SELECTION", "选中文献没有有效的 8 位 item key。");
      }
      let row = result.find(row => row.key === item.key);
      if (!row) { row = { key: item.key, title: item.getField("title") || "（无题名）" }; result.push(row); }
      if (selectedPDF) {
        if (row.primary_key && row.primary_key !== selectedPDF) throw new LibrarySyncBridge.SafeError("SELECTION", "同一文献选择了多份 PDF，请只选要作为正本的全文 PDF。");
        row.primary_key = selectedPDF;
      }
    }
    return { items: result, ignored };
  },
  addToWindow(window) {
    if (!window.ZoteroPane || this.windows.has(window)) return;
    const doc = window.document;
    const entries = [];
    for (const [parentId, id, label, selected] of [
      ["menu_ToolsPopup", "library-sync-tools", "Library Sync 文献同步…", false],
      ["zotero-itemmenu", "library-sync-selected", "Library Sync：同步所选文献…", true]
    ]) {
      const parent = doc.getElementById(parentId);
      if (!parent) continue;
      const menu = doc.createXULElement("menuitem");
      menu.id = id;
      menu.setAttribute("label", label);
      const handler = () => this.openPanel(window, selected);
      menu.addEventListener("command", handler);
      parent.appendChild(menu);
      entries.push({ menu, handler });
    }
    this.windows.set(window, entries);
  },
  removeFromWindow(window) {
    for (const { menu, handler } of this.windows.get(window) || []) {
      menu.removeEventListener("command", handler);
      menu.remove();
    }
    this.windows.delete(window);
    if (this.sourceWindow === window) this.sourceWindow = null;
  },
  openPanel(window, selected) {
    this.sourceWindow = window;
    if (this.panel && !this.panel.closed) {
      this.panel.focus();
      if (selected) {
        if (this.panel.librarySyncController) this.panel.librarySyncController.captureSelection();
        else if (this.panel.arguments?.[0]) this.panel.arguments[0].selected = true;
      }
      return;
    }
    let dialog = null;
    try {
      dialog = window.openDialog("chrome://library-sync/content/panel.xhtml?v=0.2.0-beta.16", "library-sync-panel",
        "chrome,centerscreen,resizable,dialog=no,width=1120,height=820", { host: this, selected });
    } catch (_) { dialog = null; }
    if (!dialog || typeof dialog.addEventListener !== "function" || dialog.closed) {
      // Chrome registration or dialog creation failed. Never keep a dead
      // reference: the menu must be able to open a fresh panel immediately.
      try { dialog?.close?.(); } catch (_) {}
      this.panel = null;
      this.reportOpenFailure(window);
      return;
    }
    this.panel = dialog;
    this.watchPanel(dialog);
  },
  reportOpenFailure(window) {
    // Sanitized host-side fallback: the panel document never loaded, so there
    // is no panel DOM to write into. The message names no exceptions or keys.
    let shown = false;
    try {
      if (typeof Services?.prompt?.alert === "function") {
        Services.prompt.alert(window, "Library Sync",
          "面板窗口无法打开（chrome 注册或面板资源缺失）。请重启 Zotero 后重试；已运行的后台任务不受影响。");
        shown = true;
      }
    } catch (_) {}
    if (!shown) {
      try { window.alert?.("面板窗口无法打开（chrome 注册或面板资源缺失）。请重启 Zotero 后重试。"); } catch (_) {}
    }
  },
  clearPanelWatch() {
    const watch = this.panelWatch;
    if (!watch) return;
    this.panelWatch = null;
    if (watch.timer !== null) clearTimeout(watch.timer);
    for (const [target, event, handler] of watch.listeners) {
      try { target.removeEventListener(event, handler); } catch (_) {}
    }
  },
  panelReady(window) {
    // Explicit ready handshake from the panel controller.
    if (this.panelWatch?.window === window) this.clearPanelWatch();
  },
  watchPanel(panel) {
    this.clearPanelWatch();
    const host = this;
    const listeners = [];
    const watch = { window: panel, timer: null, listeners };
    const finish = () => { if (host.panelWatch === watch) host.clearPanelWatch(); };
    const add = (target, event, handler) => {
      target.addEventListener(event, handler);
      listeners.push([target, event, handler]);
    };
    add(panel, "library-sync-ready", finish);
    // Any close path (user, failed init, shutdown) drops the reference; the
    // panel's own controller also calls panelClosed, but a panel whose scripts
    // never ran can only be noticed here.
    add(panel, "unload", () => host.panelClosed(panel));
    watch.timer = setTimeout(() => {
      if (host.panelWatch !== watch || panel.closed) return;
      host.clearPanelWatch();
      host.markPanelFailed(panel);
    }, this.PANEL_READY_TIMEOUT_MS);
    this.panelWatch = watch;
  },
  markPanelFailed(panel) {
    // Bounded timeout without a ready handshake: the document or its scripts
    // failed. Write a sanitized hint into the document if one exists, then
    // close the dead window so reopening creates a fresh panel.
    if (this.panel === panel) this.panel = null;
    let shown = false;
    try {
      const note = panel.document?.getElementById?.("message");
      if (note) {
        note.hidden = false;
        note.setAttribute("data-error", "true");
        note.textContent = "面板初始化未在限定时间内完成（未收到就绪信号，多为升级后资源缓存残留或脚本加载失败）。本窗口将关闭；请重新打开面板，若仍失败请重启 Zotero 后重试。";
        shown = true;
      }
    } catch (_) {}
    if (!shown) {
      try {
        Services.prompt?.alert?.(panel, "Library Sync",
          "面板资源加载失败（未收到就绪信号）。请重新打开面板，若仍失败请重启 Zotero 后重试。");
      } catch (_) {}
    }
    try { if (!panel.closed) panel.close(); } catch (_) {}
  },
  panelClosed(window) {
    if (this.panel === window) this.panel = null;
    if (this.panelWatch?.window === window) this.clearPanelWatch();
  },
  shutdown() {
    for (const window of [...this.windows.keys()]) this.removeFromWindow(window);
    this.clearPanelWatch();
    if (this.panel && !this.panel.closed) this.panel.close();
    this.panel = null;
    this.sourceWindow = null;
    // Deliberately no worker/bridge stop or cancel, including disable/uninstall.
  }
};
