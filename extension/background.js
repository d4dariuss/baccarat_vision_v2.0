// Background service worker: relays requests from the content script to the
// local engine server. This is needed because the game frame is https, and a
// content script there can't fetch http://127.0.0.1 directly (mixed content) —
// but the extension service worker can (host_permissions covers localhost).

// Balance/currency relayed from whichever frame can see it (the game frame and
// the SpinQuest wrapper are different origins; this bridges them).
let relayedBalance = null;

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "setBalance") {
    if (msg.data && (msg.data.balance || msg.data.currency)) relayedBalance = msg.data;
    sendResponse({ ok: true });
    return false;
  }
  if (msg && msg.type === "getBalance") {
    sendResponse({ ok: true, data: relayedBalance });
    return false;
  }
  if (msg && msg.type === "api") {
    const url = "http://127.0.0.1:8777" + msg.path;
    // Abort a hung request so the content script never waits forever on an
    // offline/stalled engine (it treats a null response as "offline").
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 4000);
    fetch(url, {
      method: msg.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: msg.body ? JSON.stringify(msg.body) : undefined,
      signal: ctrl.signal,
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }))
      .finally(() => clearTimeout(timer));
    return true; // keep the message channel open for the async response
  }
});
