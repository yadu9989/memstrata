// src/service_worker.ts
//
// MV3 background service worker. Manages:
//   - Extension install / update lifecycle.
//   - localhost:8000 health polling (30 s interval).
//   - Badge icon reflecting connection state.
//   - Message relay between popup and content scripts.

import { djb2Hash, RollingHashCache } from './shared/dedup';
const _recentTurnHashes = new RollingHashCache();

const BASE_URL = 'http://localhost:8000';
const HEALTH_POLL_INTERVAL_MS = 30_000;

let isConnected = false;

// ── Badge helpers ─────────────────────────────────────────────────────────────

function setBadge(connected: boolean): void {
  chrome.action.setBadgeText({ text: connected ? '' : '⚠' });
  chrome.action.setBadgeBackgroundColor({ color: connected ? '#10b981' : '#ef4444' });
  chrome.action.setTitle({
    title: connected
      ? 'Memory Layer — connected'
      : 'Memory Layer — offline (start localhost:8000)',
  });
}

// ── Health polling ────────────────────────────────────────────────────────────

async function pollHealth(): Promise<void> {
  try {
    const key = await getApiKey();
    const r = await fetch(`${BASE_URL}/health`, {
      method: 'GET',
      headers: { 'X-API-Key': key || '' },
    });
    isConnected = r.ok;
  } catch {
    isConnected = false;
  }
  setBadge(isConnected);
}

async function getApiKey(): Promise<string | null> {
  if (!chrome?.storage?.local) return null;
  return new Promise(resolve => {
    chrome.storage.local.get('apiKey', result => {
      resolve(result['apiKey'] ?? null);
    });
  });
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(details => {
  if (details.reason === 'install') {
    chrome.action.openPopup?.().catch(() => {
      // openPopup may not be available in all contexts — safe to ignore.
    });
  }
  pollHealth();
});

// MV3 service workers can be suspended; use chrome.alarms for long-running polling.
chrome.alarms.create('health-poll', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'health-poll') pollHealth();
});

// ── Message relay & CSP-safe fetch proxy ──────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'GET_STATUS') {
    sendResponse({ connected: isConnected });
    return false;
  }
  
  if (msg.type === 'SAVE_API_KEY') {
    chrome.storage.local.set({ apiKey: msg.key }, () => {
      pollHealth().then(() => sendResponse({ ok: true }));
    });
    return true; // async
  }
  
  if (msg.type === 'SAVE_PROJECT_ID') {
    chrome.storage.local.set({ projectId: msg.projectId }, () => {
      sendResponse({ ok: true });
    });
    return true; // async
  }

  // Phase 32: NL command "show_memory" — open the extension popup/side panel
  if (msg.type === 'open_side_panel') {
    // Try the sidePanel API (Chrome 114+) first; fall back to opening the popup.
    if (typeof (chrome.sidePanel as { open?: unknown } | undefined)?.open === 'function') {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tabId = tabs[0]?.id;
        if (tabId !== undefined) {
          (chrome.sidePanel as { open: (opts: { tabId: number }) => Promise<void> })
            .open({ tabId })
            .then(() => sendResponse({ ok: true }))
            .catch(() => { chrome.action.openPopup?.().catch(() => {}); sendResponse({ ok: true }); });
        } else {
          chrome.action.openPopup?.().catch(() => {});
          sendResponse({ ok: true });
        }
      });
    } else {
      chrome.action.openPopup?.().catch(() => {});
      sendResponse({ ok: true });
    }
    return true; // async
  }
  
  // Content scripts on pages with strict CSP (e.g. Gemini) cannot fetch()
  // to localhost directly. They send a PROXY_FETCH message here.
  if (msg.type === 'PROXY_FETCH') {
    console.log("🟢 [ServiceWorker] Received PROXY_FETCH for:", msg.url);
    console.log("🟢 [ServiceWorker] Fetch options:", msg.options);

    // Dedup: if this is a telemetry/session POST, hash the text and drop duplicates
    if (
      msg.url.endsWith('/telemetry/session') &&
      msg.options?.method === 'POST' &&
      typeof msg.options?.body === 'string'
    ) {
      try {
        const body = JSON.parse(msg.options.body);
        if (typeof body?.text === 'string' && body.text.length > 0) {
          const hash = djb2Hash(body.text);
          if (_recentTurnHashes.isDuplicate(hash)) {
            console.log('[SW:dedup] duplicate turn hash=%d, dropping', hash);
            sendResponse({ ok: true, status: 200, data: { id: 0, duplicate: true } });
            return true;
          }
          _recentTurnHashes.add(hash);
        }
      } catch {
        // parse error: let the request through
      }
    }

    fetch(msg.url, msg.options)
      .then(async (res) => {
        console.log("🟢 [ServiceWorker] Fetch success! Status:", res.status);
        const text = await res.text();
        let data = null;
        try { data = JSON.parse(text); } catch (e) {}
        sendResponse({ ok: res.ok, status: res.status, data });
      })
      .catch((err) => {
        console.error("🔴 [ServiceWorker] Fetch FAILED:", err);
        sendResponse({ ok: false, error: err.message });
      });
    return true; // async
  }

  return false;
});