/* Zotero bootstrap lifecycle, following the official Make It Red example. */
var LibrarySync;
var LibrarySyncBridge;
var librarySyncChrome;
var librarySyncGeneration = 0;

function install() {}
function uninstall() {}

async function startup({ rootURI }) {
  const generation = ++librarySyncGeneration;
  await Zotero.initializationPromise;
  await Zotero.uiReadyPromise;
  if (generation !== librarySyncGeneration) return;
  // Zotero 10.0.6's bootstrap sandbox supplies privileged XHR and timers.
  if (typeof XMLHttpRequest === "undefined") Cu.importGlobalProperties(["XMLHttpRequest"]);
  if (typeof IOUtils === "undefined") Cu.importGlobalProperties(["IOUtils"]);
  const manager = Cc["@mozilla.org/addons/addon-manager-startup;1"].getService(Ci.amIAddonManagerStartup);
  librarySyncChrome = manager.registerChrome(Services.io.newURI(rootURI + "manifest.json"), [
    ["content", "library-sync", ""]
  ]);
  Services.scriptloader.loadSubScript(rootURI + "bridge-client.js");
  Services.scriptloader.loadSubScript(rootURI + "runtime.js");
  LibrarySync.init();
  for (const window of Zotero.getMainWindows()) LibrarySync.addToWindow(window);
}

function onMainWindowLoad({ window }) { LibrarySync?.addToWindow(window); }
function onMainWindowUnload({ window }) { LibrarySync?.removeFromWindow(window); }

function shutdown() {
  librarySyncGeneration++;
  LibrarySync?.shutdown();
  librarySyncChrome?.destruct();
  librarySyncChrome = null;
  LibrarySync = undefined;
  LibrarySyncBridge = undefined;
}
