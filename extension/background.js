// Relays requests from the content script to the local Python server
// (localhost:8743). Routed through the background service worker rather
// than fetched directly from the content script so the request runs in the
// extension's own origin — covered by `host_permissions` in manifest.json —
// instead of the page's origin, which avoids any page-side CORS/CSP
// restrictions entirely.

const SERVER_URL = "http://127.0.0.1:8743";

async function forward(path, body) {
  const opts = body
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    : { method: "GET" };
  const res = await fetch(`${SERVER_URL}${path}`, opts);
  if (!res.ok) {
    throw new Error(`Server responded ${res.status}`);
  }
  return res.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "WPC_SCAN") {
    forward("/scan", { image: msg.image, preferFoil: !!msg.preferFoil })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // keep the message channel open for the async response
  }
  if (msg?.type === "WPC_HEALTH") {
    forward("/health")
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
