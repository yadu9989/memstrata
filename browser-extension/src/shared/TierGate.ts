// src/shared/TierGate.ts
//
// V5.2-E E.11 — runtime tier detection for the Pro daemon.
//
// Calls GET /system/tier on the local daemon and caches "free" | "pro".
// The extension uses this to gate augmentation surfaces: when the
// daemon reports "pro", the augmenter UI mounts and accepts user
// clicks; when "free", the augmenter UI is not rendered and capture
// proceeds unchanged.
//
// Critical contract: this gate FAILS CLOSED. If the request errors,
// times out, or the daemon returns 404 (Open-only deployment with no
// Pro overlay mounted), the cached tier becomes "free" — augmentation
// stays off. That is the opposite of FeatureGate.ts (which fails-open
// for capture features so the extension never breaks). Augmentation
// requires a Pro daemon to actually do its server-side work; showing
// a button that silently fails would be a worse UX than not showing
// the button at all.

const BASE_URL = 'http://localhost:8000';
const CACHE_TTL_MS = 60_000;  // re-probe at most once per minute

export type Tier = 'free' | 'pro';

interface TierCache {
  tier: Tier;
  fetchedAt: number;
}

let _cache: TierCache | null = null;

async function proxyFetch(url: string): Promise<{ data: unknown; ok: boolean } | null> {
  return new Promise((resolve) => {
    if (typeof chrome === 'undefined' || !chrome.runtime?.sendMessage) {
      resolve(null);
      return;
    }
    chrome.runtime.sendMessage(
      {
        type: 'PROXY_FETCH',
        url,
        options: { method: 'GET', headers: { 'Content-Type': 'application/json' } },
      },
      (response) => {
        if (chrome.runtime.lastError) {
          resolve(null);
          return;
        }
        resolve(response ?? null);
      },
    );
  });
}

/**
 * Returns the current effective tier for UI gating. Cached 60 s.
 *
 * Possible outcomes:
 *   - Daemon up + Pro overlay mounted + plan is pro/trial/team => "pro"
 *   - Daemon up + Pro overlay mounted + plan is free/lite      => "free"
 *   - Daemon up + Open-only (no Pro overlay) => 404 from /system/tier => "free"
 *   - Daemon down / network error / malformed response         => "free"
 */
export async function getTier(): Promise<Tier> {
  const now = Date.now();
  if (_cache && now - _cache.fetchedAt < CACHE_TTL_MS) {
    return _cache.tier;
  }

  const result = await proxyFetch(`${BASE_URL}/system/tier`);
  let tier: Tier = 'free';

  if (result && result.ok && result.data && typeof result.data === 'object') {
    const value = (result.data as Record<string, unknown>)['tier'];
    if (value === 'pro') {
      tier = 'pro';
    }
  }

  _cache = { tier, fetchedAt: now };
  return tier;
}

/** Convenience: returns true when augmentation UI should be enabled. */
export async function isProTier(): Promise<boolean> {
  return (await getTier()) === 'pro';
}

/** Clear the cache (for tests or after plan changes). */
export function clearTierCache(): void {
  _cache = null;
}
