// src/content/shared/memory_layer_client.ts
//
// Talks to localhost:8000 (the MIT core API server) via fetch + CORS.
// API key is stored in chrome.storage.local and injected per-request.
// All failures are silent to the user except the service worker badge.

// Requests are proxied through the service worker (see PROXY_FETCH handler)
// to avoid page-level CSP blocking direct fetch() calls to localhost.

export interface ContextBlock {
  text: string;
  token_count: number;
  project_id: string;
}

export interface ChatTurnPayload {
  project_id: string;
  session_id: string;
  external_session_id: string | null;
  turn_id: number;
  role: 'assistant' | 'user';
  text: string;
  provider: string;
  model: string | null;
  char_count: number;
}

export interface BaselineStatus {
  in_baseline: boolean;
  days_remaining: number | null;
}

async function getApiKey(): Promise<string | null> {
  if (!chrome?.runtime?.id || !chrome?.storage?.local) return null;
  return new Promise((resolve) => {
    try {
      chrome.storage.local.get("apiKey", (result) => {
        if (chrome.runtime?.lastError) return resolve(null);
        resolve(result["apiKey"] ?? null);
      });
    } catch {
      resolve(null);
    }
  });
}

async function apiFetch(path: string, options: any = {}): Promise<any> {
  const key = await getApiKey();
  const headers = {
    "Content-Type": "application/json",
    "X-API-Key": key || "",
    ...(options.headers ?? {})
  };
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      { type: "PROXY_FETCH", url: `http://localhost:8000${path}`, options: { ...options, headers } },
      (response) => {
        if (chrome.runtime.lastError) return reject(new Error("Background fetch failed"));
        // Return a mock Response object to satisfy existing callers
        resolve({ ok: response?.ok, status: response?.status, json: async () => response?.data });
      }
    );
  });
}

export async function checkHealth(): Promise<boolean> {
  try {
    const r = await apiFetch('/health', { method: 'GET' });
    return r.ok;
  } catch {
    return false;
  }
}

export async function fetchContext(projectId: string): Promise<ContextBlock | null> {
  try {
    const r = await apiFetch(`/context?project_id=${encodeURIComponent(projectId)}`);
    if (!r.ok) return null;
    return r.json() as Promise<ContextBlock>;
  } catch {
    return null;
  }
}

export async function recordChatTurn(payload: ChatTurnPayload): Promise<void> {
  try {
    await apiFetch('/telemetry/session', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  } catch {
    // Silently swallow — offline core should not disrupt the user's chat session.
  }
}

export async function fetchBaselineStatus(
  projectId: string,
): Promise<BaselineStatus> {
  try {
    const r = await apiFetch(
      `/baseline/status?project_id=${encodeURIComponent(projectId)}`,
    );
    if (!r.ok) return { in_baseline: false, days_remaining: null };
    return r.json() as Promise<BaselineStatus>;
  } catch {
    return { in_baseline: false, days_remaining: null };
  }
}

export async function getStoredProjectId(): Promise<string | null> {
  if (!chrome?.runtime?.id || !chrome?.storage?.local) return null;
  return new Promise((resolve) => {
    try {
      chrome.storage.local.get("projectId", (result) => {
        if (chrome.runtime?.lastError) return resolve(null);
        resolve(result["projectId"] ?? null);
      });
    } catch {
      resolve(null);
    }
  });
}
