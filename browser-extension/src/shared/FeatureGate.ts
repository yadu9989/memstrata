// src/shared/FeatureGate.ts
//
// Phase 33 — two-tier billing feature gate for the browser extension.
//
// Fetches /license/plan-features from the core server (via PROXY_FETCH so
// page-level CSP doesn't block localhost requests) and caches the result.
//
// Hard Rule 64: fails-open — if the server is offline or the request fails,
// all features are treated as active so the extension never breaks.

const BASE_URL = 'http://localhost:8000';
const CACHE_TTL_MS = 60_000;  // re-fetch at most once per minute

export type FeatureFlag =
  | 'mcp_server'
  | 'local_dashboard'
  | 'browser_ext'
  | 'harness'
  | 'vscode_ext'
  | 'money_tab'
  | 'money_tab_chat_only'
  | 'team_sync'
  | 'shared_dashboard';

// Fail-open feature set: used when the core is unreachable.
const FAIL_OPEN_FEATURES: FeatureFlag[] = [
  'mcp_server', 'local_dashboard', 'browser_ext',
  'harness', 'vscode_ext', 'money_tab',
];

interface FeatureCache {
  features: FeatureFlag[];
  fetchedAt: number;
}

let _cache: FeatureCache | null = null;

async function proxyFetch(url: string): Promise<unknown> {
  return new Promise((resolve) => {
    if (typeof chrome === 'undefined' || !chrome.runtime?.sendMessage) {
      resolve(null);
      return;
    }
    chrome.runtime.sendMessage(
      { type: 'PROXY_FETCH', url, options: { method: 'GET', headers: { 'Content-Type': 'application/json' } } },
      (response) => {
        if (chrome.runtime.lastError) { resolve(null); return; }
        resolve(response?.data ?? null);
      },
    );
  });
}

async function fetchPlanFeatures(): Promise<FeatureFlag[]> {
  const now = Date.now();
  if (_cache && now - _cache.fetchedAt < CACHE_TTL_MS) {
    return _cache.features;
  }

  try {
    const data = await proxyFetch(`${BASE_URL}/license/plan-features`);
    if (data && typeof data === 'object' && Array.isArray((data as Record<string, unknown>)['features'])) {
      const features = (data as { features: FeatureFlag[] }).features;
      _cache = { features, fetchedAt: now };
      return features;
    }
  } catch {
    // fall through
  }

  // Fail-open
  _cache = { features: FAIL_OPEN_FEATURES, fetchedAt: now };
  return FAIL_OPEN_FEATURES;
}

export class FeatureGate {
  private _features: FeatureFlag[] = FAIL_OPEN_FEATURES;
  private _loaded = false;

  async load(): Promise<void> {
    this._features = await fetchPlanFeatures();
    this._loaded = true;
  }

  isActive(feature: FeatureFlag): boolean {
    return this._features.includes(feature);
  }

  isLoaded(): boolean {
    return this._loaded;
  }
}

/** Convenience: returns true if *feature* is active for the current plan. */
export async function isFeatureActive(feature: FeatureFlag): Promise<boolean> {
  const features = await fetchPlanFeatures();
  return features.includes(feature);
}

/** Clear the cache (for tests or after plan changes). */
export function clearFeatureCache(): void {
  _cache = null;
}
