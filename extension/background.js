/*
 * Reload the extension when its files change on disk, and then reload the
 * open GitHub tabs so that they run the new content script. This only runs
 * when the extension is loaded unpacked, which is how it is installed.
 */
"use strict";

const POLL_INTERVAL = 1000;

// The files that the extension is made of, according to its manifest.
function watchedFiles() {
  const manifest = chrome.runtime.getManifest();
  const files = new Set(["manifest.json", manifest.background.service_worker]);
  for (const script of manifest.content_scripts) {
    for (const file of [...(script.js ?? []), ...(script.css ?? [])]) {
      files.add(file);
    }
  }
  return [...files];
}

async function readFile(file) {
  let response;
  try {
    response = await fetch(chrome.runtime.getURL(file), { cache: "no-store" });
  } catch (error) {
    // A file that was deleted or renamed is also a change.
    return `${file}: ${error}`;
  }
  return `${file}: ${await response.text()}`;
}

let previous = null;

// The files as they were when the extension was loaded are kept in session
// storage, which survives the service worker being stopped, but not the
// extension being reloaded. Reading it every second also keeps the service
// worker running.
async function check() {
  const current = (await Promise.all(watchedFiles().map(readFile))).join("\0");
  const { loaded } = await chrome.storage.session.get("loaded");
  if (loaded === undefined) {
    await chrome.storage.session.set({ loaded: current });
  } else if (current !== loaded && current === previous) {
    // Only reload once the files have stopped changing, so that an edit to
    // several files causes one reload.
    await chrome.storage.local.set({ reloadTabs: true });
    chrome.runtime.reload();
  }
  previous = current;
}

async function reloadTabs() {
  const { reloadTabs } = await chrome.storage.local.get("reloadTabs");
  if (!reloadTabs) {
    return;
  }
  await chrome.storage.local.remove("reloadTabs");
  for (const tab of await chrome.tabs.query({ url: "https://github.com/*" })) {
    await chrome.tabs.reload(tab.id);
  }
}

async function start() {
  // An extension installed from the Chrome Web Store has an update_url.
  if ("update_url" in chrome.runtime.getManifest()) {
    return;
  }
  await reloadTabs();
  setInterval(check, POLL_INTERVAL);
}

// These start the service worker when the browser starts and when the
// extension is loaded. start() runs every time the service worker starts.
chrome.runtime.onStartup.addListener(() => {});
chrome.runtime.onInstalled.addListener(() => {});
start();
