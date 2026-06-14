// V5.2-E E.11 — TierGate runtime tier detection.
//
// The gate fails CLOSED: only an explicit `{tier:"pro"}` response from
// the daemon enables augmentation. Every other outcome (404, network
// error, malformed body, missing chrome.runtime, free-tier reply) must
// resolve to "free".

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { getTier, isProTier, clearTierCache } from '../../src/shared/TierGate';

// Each test gets a fresh chrome.runtime.sendMessage spy. The default
// behavior is the "happy path" return shape used by the real service
// worker's PROXY_FETCH handler. Individual tests override as needed.
type SendMessageImpl = (msg: unknown, cb: (response: unknown) => void) => void;

function installChromeRuntime(impl: SendMessageImpl): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).chrome = {
    runtime: {
      sendMessage: vi.fn(impl),
      lastError: undefined,
    },
  };
}

beforeEach(() => {
  clearTierCache();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (globalThis as any).chrome;
});

describe('getTier()', () => {
  it('returns "pro" when daemon responds with {tier:"pro"}', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { tier: 'pro', plan: 'pro' } }),
    );
    expect(await getTier()).toBe('pro');
  });

  it('returns "free" when daemon responds with {tier:"free"}', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { tier: 'free', plan: 'free' } }),
    );
    expect(await getTier()).toBe('free');
  });

  it('treats 404 (Open daemon, no /system/tier route) as free', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: false, status: 404, data: null }),
    );
    expect(await getTier()).toBe('free');
  });

  it('treats network error (sendResponse with error) as free', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: false, error: 'Failed to fetch' }),
    );
    expect(await getTier()).toBe('free');
  });

  it('treats missing chrome.runtime as free', async () => {
    // No installChromeRuntime() call — chrome is undefined.
    expect(await getTier()).toBe('free');
  });

  it('treats malformed body (no tier field) as free', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { plan: 'pro' } }),
    );
    expect(await getTier()).toBe('free');
  });

  it('treats non-"pro" tier strings as free', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { tier: 'enterprise' } }),
    );
    expect(await getTier()).toBe('free');
  });

  it('caches the result for 60 s — second call does not re-fetch', async () => {
    const sendMessage = vi.fn((_msg, cb: (response: unknown) => void) =>
      cb({ ok: true, status: 200, data: { tier: 'pro', plan: 'pro' } }),
    );
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).chrome = { runtime: { sendMessage, lastError: undefined } };

    expect(await getTier()).toBe('pro');
    expect(await getTier()).toBe('pro');
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it('clearTierCache() forces a re-probe on the next call', async () => {
    const sendMessage = vi.fn((_msg, cb: (response: unknown) => void) =>
      cb({ ok: true, status: 200, data: { tier: 'pro' } }),
    );
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).chrome = { runtime: { sendMessage, lastError: undefined } };

    await getTier();
    clearTierCache();
    await getTier();
    expect(sendMessage).toHaveBeenCalledTimes(2);
  });
});

describe('isProTier()', () => {
  it('returns true for pro daemon', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { tier: 'pro' } }),
    );
    expect(await isProTier()).toBe(true);
  });

  it('returns false for free daemon', async () => {
    installChromeRuntime((_msg, cb) =>
      cb({ ok: true, status: 200, data: { tier: 'free' } }),
    );
    expect(await isProTier()).toBe(false);
  });
});
