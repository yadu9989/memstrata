// universal_content_script.ts
//
// Single entry point replacing 9 per-provider content scripts.
// Uses a 4-layer detector chain (ARIA > semantic > velocity > structural)
// with confidence-based voting. Detection is host-agnostic; per-provider
// tuning lives in provider_hints.json (locally bundled, CDN-refreshed).
//
// Claude.ai hang fix: document_idle in manifest + safeInit + requestIdleCallback
// ensures we never touch the DOM before the SPA has mounted.
//
// Hard Rule 65: Telemetry reports detection blackouts (5 min, 50+ mutations,
// zero layers fired) so failures are always observable, never silent.

import { DetectorChain } from './engine/DetectorChain';
import { AriaLiveDetector } from './engine/detectors/AriaLiveDetector';
import { SemanticAttrDetector } from './engine/detectors/SemanticAttrDetector';
import { VelocityDetector } from './engine/detectors/VelocityDetector';
import { StructuralDetector } from './engine/detectors/StructuralDetector';
import { ShadowPiercer } from './engine/ShadowPiercer';
import { StreamWatcher } from './engine/StreamWatcher';
import { Telemetry } from './engine/Telemetry';
import { ConfigLoader } from './config/ConfigLoader';
import type { ProviderHints } from './engine/types';
import { recordChatTurn, getStoredProjectId } from './shared/memstrata_client';
import { getExternalSessionId } from './engine/SessionDetector';
// Phase 32: NL command interceptor (Hard Rule 66)
import { NLCommandDetector } from './shared/NLCommandDetector';
import { NLCommandConfirmDialog } from './shared/NLCommandConfirmDialog';
// Phase 33: feature gate (Hard Rule 64)
import { isFeatureActive } from '../shared/FeatureGate';
// V5.2-E E.11: daemon tier probe — gates augmentation UI on the Pro overlay.
import { getTier } from '../shared/TierGate';
// Phase 34: augmenter UI (floating button) + rewrite mode
import { setReactInputValue, getAugmenterUI, findPromptElement } from './shared/augmenter';
import type { AugmenterUI } from './shared/augmenter';
import { fetchContext, fetchBaselineStatus } from './shared/memstrata_client';
import type { ContextBlock } from './shared/memstrata_client';
import { TurnExtractor } from './engine/TurnExtractor';
import { StreamInterceptorBridge } from './engine/StreamInterceptorBridge';

// ── Session state ──────────────────────────────────────────────────────────────

function mkSessionId(): string {
  return `ml-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

let sessionId = mkSessionId();
let externalSessionId: string | null = null;
let turnCounter = 0;
let activeChain: DetectorChain | null = null;

// ── Stream-pause deduplication ────────────────────────────────────────────────
// Maps each promoted DOM node to a stable message_id so the backend can UPSERT
// (rather than INSERT) when a paused model resumes and fires onComplete again.
const nodeMessageIds = new WeakMap<Element, string>();
// Tracks message_ids seen this session — prevents double-counting turnCounter.
let seenMessageIds = new Set<string>();

function getOrAssignMessageId(node: Element): string {
  let id = nodeMessageIds.get(node);
  if (!id) {
    id = `mlmsg-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    try { node.setAttribute('data-ml-msg-id', id); } catch { /* read-only node */ }
    nodeMessageIds.set(node, id);
  }
  return id;
}
let activeShadowPiercer: ShadowPiercer | null = null;
let activeTelemetry: Telemetry | null = null;
let activeMutationCounter: MutationObserver | null = null;
let activeAugmenterUI: AugmenterUI | null = null;
let activeAugInputHandler: ((evt: Event) => void) | null = null;
let activeInterceptorBridge: StreamInterceptorBridge | null = null;

// ── Augmenter debounce + response caches ──────────────────────────────────────
// These prevent the extension from firing network requests on every keystroke
// (the DDOS bug). All cache entries are keyed by project_id and expire by TTL.
let augDebounceTimer: ReturnType<typeof setTimeout> | null = null;

let _cachedPid: string | null = null;
let _cachedPidAt = 0;                          // ms timestamp
const PID_TTL_MS = 30_000;

let _cachedBaseline: { pid: string; value: { in_baseline: boolean; days_remaining: number | null } } | null = null;
let _cachedBaselineAt = 0;
const BASELINE_TTL_MS = 60_000;

// Context cache is keyed by (project_id, external_session_id, provider) so two
// chat threads in different tabs never share an entry — switching tabs always
// fetches the active thread's own history.
let _cachedCtx: {
  pid: string;
  esid: string | null;
  provider: string | null;
  value: ContextBlock | null;
} | null = null;
let _cachedCtxAt = 0;
const CTX_TTL_MS = 30_000;

async function getProjectIdCached(): Promise<string> {
  const now = Date.now();
  if (_cachedPid !== null && now - _cachedPidAt < PID_TTL_MS) return _cachedPid;
  const pid = (await getStoredProjectId()) ?? 'default';
  _cachedPid = pid;
  _cachedPidAt = now;
  return pid;
}

async function fetchBaselineCached(pid: string): Promise<{ in_baseline: boolean; days_remaining: number | null }> {
  const now = Date.now();
  if (_cachedBaseline?.pid === pid && now - _cachedBaselineAt < BASELINE_TTL_MS) {
    return _cachedBaseline.value;
  }
  const value = await fetchBaselineStatus(pid).catch(() => ({ in_baseline: false, days_remaining: null }));
  _cachedBaseline = { pid, value };
  _cachedBaselineAt = now;
  return value;
}

async function fetchContextCached(
  pid: string,
  esid: string | null,
  provider: string | null,
): Promise<ContextBlock | null> {
  const now = Date.now();
  if (
    _cachedCtx &&
    _cachedCtx.pid === pid &&
    _cachedCtx.esid === esid &&
    _cachedCtx.provider === provider &&
    now - _cachedCtxAt < CTX_TTL_MS
  ) {
    return _cachedCtx.value;
  }
  const value = await fetchContext(pid, esid, provider).catch(() => null);
  _cachedCtx = { pid, esid, provider, value };
  _cachedCtxAt = now;
  return value;
}

function clearAugCaches(): void {
  if (augDebounceTimer !== null) { clearTimeout(augDebounceTimer); augDebounceTimer = null; }
  _cachedPid = null; _cachedPidAt = 0;
  _cachedBaseline = null; _cachedBaselineAt = 0;
  _cachedCtx = null; _cachedCtxAt = 0;
}

// ── Core init ──────────────────────────────────────────────────────────────────

function isDebug(): boolean {
  try { return localStorage.getItem('ML_DEBUG') === 'true'; } catch { return false; }
}

async function actualInit(): Promise<void> {
  const hostname = window.location.hostname;
  const config = await ConfigLoader.load();
  const hints = ((config as { providers?: Record<string, ProviderHints> }).providers)?.[hostname];

  if (isDebug()) {
    console.log('[ML:init] host=%s hints=%s path=%s', hostname, hints ? 'found' : 'NOT IN LIST', window.location.pathname);
  }

  if (!hints) return; // host not in provider list

  // Phase 33: gate browser extension activation behind the plan feature.
  // Hard Rule 64: fail-open — if the check throws, allow activation.
  const browserExtActive = await isFeatureActive('browser_ext').catch(() => true);
  if (!browserExtActive) {
    if (isDebug()) console.log('[ML:init] browser_ext feature not active — skipping init');
    return;
  }

  externalSessionId = getExternalSessionId(
    window.location.pathname,
    window.location.hash,
    hints,
  );

  if (isDebug()) {
    console.log('[ML:init] external_session_id=%s', externalSessionId ?? '(none)');
  }

  // Optional path filter (e.g., github.com/copilot only)
  if (hints.path_filter && !window.location.pathname.startsWith(hints.path_filter)) {
    if (isDebug()) console.log('[ML:init] path_filter=%s — skipping this path', hints.path_filter);
    return;
  }

  // Per-provider init delay (Claude.ai: 500ms to let React mount)
  if (hints.init_delay_ms) {
    await new Promise<void>((r) => setTimeout(r, hints.init_delay_ms as number));
  }

  // Tear down any previous chain/piercer/telemetry from a prior SPA navigation
  activeChain?.stop();
  activeShadowPiercer?.dispose();
  activeTelemetry?.dispose();
  activeMutationCounter?.disconnect();
  activeInterceptorBridge?.dispose();
  activeInterceptorBridge = null;

  sessionId = mkSessionId();
  turnCounter = 0;
  seenMessageIds = new Set<string>();

  if (isDebug()) {
    const ariaLiveCount = document.querySelectorAll('[aria-live]').length;
    const roleLogCount  = document.querySelectorAll('[role="log"],[role="feed"]').length;
    const shadowCount   = Array.from(document.querySelectorAll('*')).filter((el) => el.shadowRoot !== null).length;
    const bodyChildren  = document.body.children.length;
    console.log(
      '[ML:init] DOM snapshot — aria-live=%d role-log/feed=%d shadow-roots=%d body-children=%d',
      ariaLiveCount, roleLogCount, shadowCount, bodyChildren,
    );
    if (shadowCount > 0) {
      console.warn(
        '[ML:init] Shadow DOM detected (%d roots). ShadowPiercer will attach to declared host tags.',
        shadowCount,
      );
    }
  }

  // ── Telemetry (Hard Rule 65) ────────────────────────────────────────────────

  const telemetry = new Telemetry(hints.provider_id);
  activeTelemetry = telemetry;

  const mutationCounter = new MutationObserver((mutations) => {
    mutations.forEach(() => telemetry.recordMutation());
  });
  mutationCounter.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: false, // never true — mutation-loop safety
  });
  activeMutationCounter = mutationCounter;

  // ── SSE Interceptor Bridge (Tier 1) ────────────────────────────────────────
  // Inject fetch_interceptor.js into the page's MAIN world once per init cycle.
  const interceptorBridge = new StreamInterceptorBridge();
  activeInterceptorBridge = interceptorBridge;
  interceptorBridge.inject().catch(() => {}); // fire-and-forget; failures are non-fatal

  // ── Detector chain ──────────────────────────────────────────────────────────

  if (isDebug()) console.log('[ML:init] starting DetectorChain with 4 detectors + ShadowPiercer');

  // Hold a named reference so ShadowPiercer can call processMutations on it.
  const velocityDet = new VelocityDetector();

  const chain = new DetectorChain([
    new AriaLiveDetector(),
    new SemanticAttrDetector(),
    velocityDet,
    new StructuralDetector(),
  ]);
  activeChain = chain;

  const onPromoted = (node: Element, confidence: number): void => {
    if (isDebug()) console.log('[ML:promoted] node promoted conf=%.4f', confidence, node);
    // Assign a stable message_id to this DOM node. Used as the backend UPSERT key
    // so a stream-paused model that causes onComplete to fire more than once for
    // the same node updates the existing row rather than inserting a duplicate.
    const messageId = getOrAssignMessageId(node);
    const watcher = new StreamWatcher(
      node,
      hints.debounce_ms ?? 1500,
      confidence,
      ['detector'],
      async (text, _meta) => {
        // Deregister from bridge — this stream is done.
        interceptorBridge.setActiveWatcher(null);
        // Defense-in-depth: primary filter is in StreamWatcher → isStreamingArtifact(),
        // but guard here too so any regression in that path cannot pollute telemetry.
        if (TurnExtractor.isStreamingArtifact(text)) return;
        try {
          const projectId = await getStoredProjectId();
          // Only increment turnCounter the first time we see this message node.
          // If onComplete fires again for the same node (stream-pause race),
          // the backend UPSERT will update the row without creating a duplicate.
          if (!seenMessageIds.has(messageId)) {
            seenMessageIds.add(messageId);
            turnCounter += 1;
          }
          await recordChatTurn({
            project_id: projectId ?? 'default',
            session_id: sessionId,
            external_session_id: externalSessionId,
            turn_id: turnCounter,
            message_id: messageId,
            client_source: 'chat',
            role: 'assistant',
            text,
            provider: hints.provider_id,
            model: null,
            char_count: text.length,
          });
        } catch {
          // Server offline — never disrupt the user's chat session
        }
      },
      hints,
    );
    // Tier 1: register this watcher with the SSE bridge so a network-layer
    // stream-complete signal can fire it before the debounce expires.
    interceptorBridge.setActiveWatcher(() => watcher.signalSseComplete());
  };

  chain.start(
    document.body,
    onPromoted,
    // Telemetry hook: called for every candidate signal, before voting
    (detectorName) => telemetry.recordLayerFire(detectorName),
  );

  // ── Shadow DOM piercing (e.g., Microsoft Copilot Web Components) ────────────

  const shadowHosts = hints.shadow_hosts ?? [];
  const piercer = new ShadowPiercer(
    shadowHosts,
    (mutations) => velocityDet.processMutations(mutations),
  );
  piercer.start(document.body);
  activeShadowPiercer = piercer;

  // ── Phase 32: NL command interceptor (Hard Rule 66) ────────────────────
  // Intercepts Enter key on textarea/input/contenteditable before the AI
  // provider sees it. If the text matches a command pattern, shows a
  // confirmation dialog and either executes the command or passes through.

  const nlDetector = new NLCommandDetector();
  const nlConfirm = new NLCommandConfirmDialog();

  const nlKeydownHandler = async (evt: KeyboardEvent): Promise<void> => {
    if (evt.key !== 'Enter' || evt.shiftKey || evt.metaKey || evt.ctrlKey) return;

    const target = evt.target as HTMLElement;
    const isInput =
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLInputElement ||
      target.isContentEditable;
    if (!isInput) return;

    const text =
      target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement
        ? target.value.trim()
        : target.innerText.trim();
    if (!text) return;

    const cmd = nlDetector.detect(text);
    if (!cmd) return;

    // Prevent the event synchronously (before any await) so the AI provider
    // never sees the Enter key for this command text.
    evt.preventDefault();
    evt.stopImmediatePropagation();

    // Show confirmation dialog (Hard Rule 66: mandatory for every command)
    const confirmed = await nlConfirm.show(cmd);
    if (!confirmed) return; // user cancelled — text stays in textarea

    if (cmd.confirmationLevel === 'double') {
      const secondConfirm = await nlConfirm.show({
        ...cmd,
        description: `FINAL CONFIRMATION: ${cmd.description}. This cannot be undone.`,
      });
      if (!secondConfirm) return;
    }

    const ctx = { chatSessionId: externalSessionId, providerId: hints.provider_id, rawInput: text };
    await cmd.execute(ctx).catch(() => {});

    // Clear the textarea after the command has been executed
    try {
      if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
        setReactInputValue(target, '');
      } else {
        target.innerText = '';
      }
    } catch {
      // Textarea write failed — non-critical
    }
  };

  document.addEventListener('keydown', nlKeydownHandler as unknown as EventListener, true);
  // Store a reference so we can remove it on SPA navigation
  (window as Window & { _mlNlHandler?: EventListener })._mlNlHandler = nlKeydownHandler as unknown as EventListener;

  // ── Phase 34: AugmenterUI (floating "+ Add project context" button) ────────
  // Create the Shadow-DOM button and wire it to every textarea input event.
  // Hard Rule 58: the button appearing is not injection; only a click injects.
  //
  // V5.2-E E.11 — addendum §3.4 Option B: the augmenter only renders when
  // the local daemon reports tier "pro". On a free tier OR Open-only
  // daemon (no /system/tier route) we tear down any prior UI and skip
  // mount; capture continues unchanged via the streaming interceptor
  // bridge above.

  const tier = await getTier();
  if (tier !== 'pro') {
    activeAugmenterUI?.destroy();
    activeAugmenterUI = null;
    if (activeAugInputHandler) {
      document.removeEventListener('input', activeAugInputHandler, true);
      activeAugInputHandler = null;
    }
    // Fall through to watchUrlChanges so SPA navigation still tears
    // down per-session state cleanly.
    watchUrlChanges(() => {
      chain.stop();
      piercer.dispose();
      activeChain = null;
      activeShadowPiercer = null;
      const prev = (window as Window & { _mlNlHandler?: EventListener })._mlNlHandler;
      if (prev) document.removeEventListener('keydown', prev, true);
      activeInterceptorBridge?.dispose();
      activeInterceptorBridge = null;
    });
    return;
  }

  activeAugmenterUI?.destroy();
  activeAugmenterUI = getAugmenterUI();
  const augUI = activeAugmenterUI;
  augUI.externalSessionId = externalSessionId;
  augUI.providerId = hints.provider_id;

  if (activeAugInputHandler) {
    document.removeEventListener('input', activeAugInputHandler, true);
  }

  activeAugInputHandler = (evt: Event): void => {
    // isTrusted guard: skip synthetic InputEvents dispatched by setReactInputValue
    // to prevent a feedback loop (our own setter re-triggering this listener).
    if (!(evt as InputEvent).isTrusted) return;

    const target = evt.target as HTMLElement;
    const isPromptInput =
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLInputElement ||
      (target.isContentEditable && target !== document.body);
    if (!isPromptInput) return;

    // Debounce: cancel any pending tick and start a fresh 250 ms window.
    // ALL async work (chrome.storage + network) lives inside the timeout so
    // rapid keystrokes produce exactly one request cycle, not N.
    if (augDebounceTimer !== null) { clearTimeout(augDebounceTimer); augDebounceTimer = null; }
    augDebounceTimer = setTimeout(async () => {
      augDebounceTimer = null;
      const pid = await getProjectIdCached();
      const baselineStatus = await fetchBaselineCached(pid);
      // Close over the active externalSessionId so each chat thread fetches
      // its own context. SPA navigation re-runs actualInit and refreshes both.
      augUI.onTextareaInput(
        target,
        () => fetchContextCached(pid, externalSessionId, hints.provider_id),
        baselineStatus,
      );
    }, 250);
  };

  document.addEventListener('input', activeAugInputHandler, true);

  // SPA navigation: reinit on URL change (chat sites navigate without full reload)
  // externalSessionId is re-detected inside the new actualInit call.
  watchUrlChanges(() => {
    chain.stop();
    piercer.dispose();
    activeChain = null;
    activeShadowPiercer = null;
    // Remove the NL handler from the previous session
    const prev = (window as Window & { _mlNlHandler?: EventListener })._mlNlHandler;
    if (prev) document.removeEventListener('keydown', prev, true);
    // Dispose the SSE bridge for this session
    activeInterceptorBridge?.dispose();
    activeInterceptorBridge = null;
    // Remove the augmenter input handler, clear caches, and destroy the UI
    clearAugCaches();
    if (activeAugInputHandler) {
      document.removeEventListener('input', activeAugInputHandler, true);
      activeAugInputHandler = null;
    }
    activeAugmenterUI?.destroy();
    activeAugmenterUI = null;
    // Clear message dedup set so the new session starts fresh.
    seenMessageIds = new Set<string>();
    safeInit(actualInit);
  });
}

// ── Safe init pattern (fixes Claude.ai hang; see REVIEW_PHASE20_REFACTOR §6) ──

function safeInit(fn: () => void | Promise<void>): void {
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    deferredInit(fn);
  } else {
    document.addEventListener('DOMContentLoaded', () => deferredInit(fn), { once: true });
  }
}

function deferredInit(fn: () => void | Promise<void>): void {
  if ('requestIdleCallback' in window) {
    (window as Window & { requestIdleCallback: (cb: () => void, opts?: object) => void })
      .requestIdleCallback(fn, { timeout: 5000 });
  } else {
    setTimeout(fn, 200);
  }
}

// ── SPA URL watcher ───────────────────────────────────────────────────────────

function watchUrlChanges(onChange: () => void): void {
  let lastUrl = location.href;

  const check = (): void => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      onChange();
    }
  };

  const origPush = history.pushState.bind(history);
  const origReplace = history.replaceState.bind(history);

  // Monkey-patch History API — any-cast required for TypeScript strict mode
  (history as unknown as Record<string, unknown>)['pushState'] =
    (...args: Parameters<typeof origPush>): void => { origPush(...args); check(); };
  (history as unknown as Record<string, unknown>)['replaceState'] =
    (...args: Parameters<typeof origReplace>): void => { origReplace(...args); check(); };

  window.addEventListener('popstate', check);
  setInterval(check, 1000); // polling fallback for sites that bypass History API
}

// ── Entry point ───────────────────────────────────────────────────────────────

safeInit(actualInit);
