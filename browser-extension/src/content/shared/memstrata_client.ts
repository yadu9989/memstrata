// src/content/shared/memstrata_client.ts
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
  /** Stable DOM-node identifier; backend UPSERTs on (session_id, message_id) to
   *  prevent duplicate rows when a stream-paused model fires onComplete twice. */
  message_id?: string;
  /** Explicit client origin for dashboard Chat vs Coding split. */
  client_source?: 'chat' | 'coding' | 'browser_ext' | 'harness';
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

/**
 * Fetch deduped chat context.
 *
 * Per-thread isolation (V5.4 §2.1): when `externalSessionId` + `provider` are
 * supplied, the backend joins on chat_sessions and returns ONLY turns ingested
 * in this specific web chat thread. Pass null for both to fall back to harness
 * project-scoped retrieval.
 */
export async function fetchContext(
  projectId: string,
  externalSessionId: string | null = null,
  provider: string | null = null,
): Promise<ContextBlock | null> {
  try {
    const params = new URLSearchParams({ project_id: projectId });
    if (externalSessionId) params.set('external_session_id', externalSessionId);
    if (provider)          params.set('provider', provider);
    const r = await apiFetch(`/context?${params.toString()}`);
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

export interface RetrievedTurn {
  timeline_id: number;
  role: 'user' | 'assistant';
  text: string;
  captured_at: string;
  similarity_score: number | null;
  recency_score: number | null;
  final_score: number | null;
  age_human: string;
}

export interface RetrievalResult {
  retrieved_turns: RetrievedTurn[];
  token_budget_used?: number;
  token_budget_total?: number;
  total_session_turns?: number;
  turns_with_embeddings?: number;
  turns_pending_embedding?: number;
  turns_considered?: number;
  turns_returned?: number;
  degraded: boolean;
  reason?: string;
}

export async function fetchChatRewriteContext(
  externalSessionId: string,
  providerId: string,
  draftPrompt: string,
  tokenBudget = 1500,
): Promise<RetrievalResult | null> {
  try {
    const r = await apiFetch('/context/for-chat-rewrite', {
      method: 'POST',
      body: JSON.stringify({
        external_session_id: externalSessionId,
        provider_id: providerId,
        draft_prompt: draftPrompt,
        target_token_budget: tokenBudget,
      }),
    });
    if (!r.ok) return null;
    return r.json() as Promise<RetrievalResult>;
  } catch {
    return null;
  }
}

export interface RewriteTelemetryPayload {
  rewrite_id: string;
  external_session_id?: string;
  provider_id: string;
  draft_prompt_chars: number;
  retrieved_turn_count: number;
  retrieved_turn_avg_similarity?: number | null;
  retrieved_turn_age_dist_hours?: number[];
  user_confirmed: boolean;
  delimiter_format: string;
  token_budget_used?: number;
  token_budget_total?: number;
  degraded: boolean;
  degraded_reason?: string | null;
}

export async function recordRewriteTelemetry(payload: RewriteTelemetryPayload): Promise<void> {
  try {
    await apiFetch('/telemetry/rewrite', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  } catch {
    // Fire-and-forget — never disrupt the workflow
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
