// src/content/shared/augmenter.ts
//
// React-aware input setter + Shadow-DOM floating button.
//
// Hard Rule 57 — NO synthetic ClipboardEvent, NO fake isTrusted, NO simulated
//   paste/keystrokes. The only write path is setReactInputValue (for React
//   textarea/input elements) or setContentEditableValue (for ProseMirror divs).
//   Both dispatch real InputEvents, which is the same mechanism used by browser
//   autofill, password managers, and accessibility tools.
//
// Hard Rule 58 — Context is NEVER injected automatically. The floating button
//   becomes visible after the user types; injection happens only on explicit
//   button click. No auto-augmentation, no silent mutation.

import type { ContextBlock, BaselineStatus } from './memstrata_client.js';
import { recordRewriteTelemetry } from './memstrata_client.js';
import { RewriteEngine } from './RewriteEngine.js';
import { DiffView } from './DiffView.js';

// ─── Augmentation mode (Phase 34) ────────────────────────────────────────────

/** Read the augmentation mode from extension storage (default: 'append'). */
async function getAugmentationMode(): Promise<'append' | 'rewrite'> {
  if (typeof chrome === 'undefined' || !chrome.storage?.local) return 'append';
  return new Promise((resolve) => {
    chrome.storage.local.get('augmentationMode', (result) => {
      if (chrome.runtime?.lastError) return resolve('append');
      resolve(result['augmentationMode'] === 'rewrite' ? 'rewrite' : 'append');
    });
  });
}

// ─── React-aware setters ──────────────────────────────────────────────────────

/**
 * Inject a new value into a React-controlled <textarea> or <input>.
 *
 * React's controlled-input mechanism caches the last-set value to decide
 * whether to dispatch onChange. Writing el.value = x directly updates the
 * DOM but leaves the tracker stale, so React's onChange never fires.
 * Going through the *prototype* setter invalidates the tracker, and the
 * subsequent real InputEvent causes React to read event.target.value and
 * update its internal state — identical to how browser autofill works.
 */
export function setReactInputValue(
  el: HTMLTextAreaElement | HTMLInputElement,
  value: string,
): void {
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;

  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  if (!descriptor?.set) {
    throw new Error('Native value setter not available on this element');
  }

  descriptor.set.call(el, value);

  // A real InputEvent (not synthetic ClipboardEvent) bubbles into React's
  // SyntheticEvent system. React reads event.target.value → triggers onChange.
  el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: false }));
}

/**
 * Inject text into a ProseMirror / generic contenteditable element.
 *
 * Uses document.execCommand('insertText') which is the browser-native mechanism
 * for inserting text into contenteditable — the same path as IME input and
 * OS-level accessibility APIs. It does NOT use the Clipboard API or dispatch
 * any ClipboardEvent (Hard Rule 57 preserved).
 *
 * ProseMirror intercepts the resulting beforeinput/input events and updates
 * its own state accordingly.
 */
export function setContentEditableValue(el: HTMLElement, value: string): void {
  el.focus();

  // Select entire current content before replacing.
  const selection = window.getSelection();
  if (selection) {
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  // insertText goes through the browser's native text-insertion pipeline —
  // NOT the clipboard pipeline. No ClipboardEvent is dispatched.
  document.execCommand('insertText', false, value);
}

// ─── Prompt textarea finder ───────────────────────────────────────────────────

export type PromptElement =
  | HTMLTextAreaElement
  | HTMLInputElement
  | HTMLElement;

/**
 * Locate the active prompt input element.
 * Caller passes site-specific selectors; falls back to any visible contenteditable.
 */
export function findPromptElement(
  specificSelectors: string[],
): PromptElement | null {
  for (const sel of specificSelectors) {
    const el = document.querySelector<HTMLElement>(sel);
    if (el) return el;
  }
  // Generic fallback: first visible contenteditable that is not the page body.
  const fallback = document.querySelector<HTMLElement>(
    '[contenteditable="true"]:not(body)',
  );
  return fallback ?? null;
}

// ─── Prompt injection ─────────────────────────────────────────────────────────

export function buildAugmentedPrompt(
  contextText: string,
  originalText: string,
): string {
  return `<ContextBlock>\n${contextText.trim()}\n</ContextBlock>\n\n${originalText}`;
}

function getCurrentText(el: PromptElement): string {
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
    return el.value;
  }
  return el.innerText;
}

function writeText(el: PromptElement, value: string): void {
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
    setReactInputValue(el, value);
  } else {
    setContentEditableValue(el, value);
  }
}

// ─── Submit-button guard (Hard Rule 67) ──────────────────────────────────────

/**
 * Find the AI provider's submit button and disable it for the duration of the
 * diff review. Returns a restore function. This prevents keyboard shortcuts
 * from accidentally submitting the un-reviewed prompt while the diff modal
 * is open. (The visual overlay already blocks mouse clicks.)
 *
 * Fails open: if no selector matches or DOM access throws, returns a no-op
 * so the caller can proceed without crashing the augment flow.
 */
function lockSubmitButton(): () => void {
  try {
    const SELECTORS = [
      'button[data-testid="send-button"]',
      'button[aria-label*="Send" i]',
      'button[aria-label*="send" i]',
      '[data-testid="fruitjuice-send-button"]',
      'form button[type="submit"]',
      'button.send-button',
    ];
    let locked: HTMLButtonElement | null = null;
    for (const sel of SELECTORS) {
      const btn = document.querySelector<HTMLButtonElement>(sel);
      if (btn && !btn.disabled) {
        btn.disabled = true;
        locked = btn;
        break;
      }
    }
    return (): void => { if (locked) locked.disabled = false; };
  } catch {
    // DOM not accessible or selector threw — fail open with a no-op unlock
    return (): void => {};
  }
}

// ─── Shadow-DOM floating button + banner ──────────────────────────────────────

const SHADOW_HOST_ID = 'memstrata-augmenter-root';

const BUTTON_STYLES = `
  :host { all: initial; }
  #ml-btn-wrap {
    position: fixed;
    z-index: 2147483647;
    pointer-events: none;
    user-select: none;
  }
  #ml-btn {
    pointer-events: all;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    border: 1px solid #6b7280;
    border-radius: 8px;
    background: #1f2937;
    color: #f9fafb;
    font: 500 13px/1.4 system-ui, sans-serif;
    cursor: grab;
    transition: background 120ms ease, border-color 120ms ease;
    white-space: nowrap;
    touch-action: none;
  }
  #ml-btn:hover { background: #374151; border-color: #9ca3af; }
  #ml-btn:active { background: #111827; cursor: grabbing; }
  #ml-banner {
    pointer-events: all;
    margin-top: 6px;
    padding: 8px 12px;
    border-radius: 8px;
    background: #064e3b;
    color: #d1fae5;
    font: 13px/1.4 system-ui, sans-serif;
    display: none;
    align-items: center;
    gap: 8px;
  }
  #ml-banner.visible { display: flex; }
  .ml-action {
    cursor: pointer;
    text-decoration: underline;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    padding: 0;
  }
  .ml-sep { opacity: 0.4; }
  #ml-baseline-msg {
    pointer-events: all;
    padding: 6px 12px;
    border-radius: 8px;
    background: #1e3a5f;
    color: #bfdbfe;
    font: 12px/1.4 system-ui, sans-serif;
    display: none;
    white-space: nowrap;
  }
  #ml-baseline-msg.visible { display: block; }
`;

// Storage key for persisted drag position
const DRAG_POS_KEY = 'ml_btn_pos';

export class AugmenterUI {
  externalSessionId: string | null = null;
  providerId: string | null = null;

  private host: HTMLElement;
  private shadow: ShadowRoot;
  private wrap: HTMLElement;
  private btn: HTMLButtonElement;
  private banner: HTMLElement;
  private baselineMsg: HTMLElement;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private originalText = '';

  // Drag state
  private _isDragging = false;
  private _dragStartX = 0;
  private _dragStartY = 0;
  private _wrapStartLeft = 0;
  private _wrapStartBottom = 0;
  private _hasDragged = false;
  private _suppressNextClick = false;
  // Whether position has been locked by a drag (overrides repositionNear)
  private _positionLocked = false;

  constructor() {
    this.host = document.createElement('div');
    this.host.id = SHADOW_HOST_ID;
    this.shadow = this.host.attachShadow({ mode: 'closed' });

    const style = document.createElement('style');
    style.textContent = BUTTON_STYLES;

    this.wrap = document.createElement('div');
    this.wrap.id = 'ml-btn-wrap';

    this.btn = document.createElement('button');
    this.btn.id = 'ml-btn';
    this.btn.textContent = '＋ Add project context';
    this.btn.setAttribute('aria-label', 'Add MemStrata project context to prompt');

    this.banner = document.createElement('div');
    this.banner.id = 'ml-banner';
    this.banner.setAttribute('role', 'status');
    this.banner.setAttribute('aria-live', 'polite');

    this.baselineMsg = document.createElement('div');
    this.baselineMsg.id = 'ml-baseline-msg';

    this.wrap.appendChild(this.btn);
    this.wrap.appendChild(this.banner);
    this.wrap.appendChild(this.baselineMsg);
    this.shadow.appendChild(style);
    this.shadow.appendChild(this.wrap);

    document.body.appendChild(this.host);

    // Restore persisted drag position (if any)
    this._loadPersistedPosition();

    // Drag: pointerdown on the button starts a drag
    this.btn.addEventListener('pointerdown', (e: PointerEvent) => {
      if (e.button !== 0) return; // left button only
      this._isDragging = true;
      this._hasDragged = false;
      this._dragStartX = e.clientX;
      this._dragStartY = e.clientY;
      this._wrapStartLeft = parseInt(this.wrap.style.left || '0', 10);
      this._wrapStartBottom = parseInt(this.wrap.style.bottom || '0', 10);
      this.btn.setPointerCapture(e.pointerId);
      this.btn.style.cursor = 'grabbing';
      e.preventDefault();
    });

    this.btn.addEventListener('pointermove', (e: PointerEvent) => {
      if (!this._isDragging) return;
      const dx = e.clientX - this._dragStartX;
      const dy = e.clientY - this._dragStartY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
        this._hasDragged = true;
      }
      if (this._hasDragged) {
        const newLeft   = Math.max(0, Math.min(window.innerWidth  - 20, this._wrapStartLeft   + dx));
        const newBottom = Math.max(0, Math.min(window.innerHeight - 20, this._wrapStartBottom - dy));
        this.wrap.style.left   = `${newLeft}px`;
        this.wrap.style.bottom = `${newBottom}px`;
      }
    });

    this.btn.addEventListener('pointerup', (e: PointerEvent) => {
      if (!this._isDragging) return;
      this._isDragging = false;
      this.btn.style.cursor = '';
      if (this._hasDragged) {
        this._positionLocked = true;
        this._suppressNextClick = true;
        this._savePosition(
          parseInt(this.wrap.style.left   || '0', 10),
          parseInt(this.wrap.style.bottom || '0', 10),
        );
      }
    });

    // Double-click resets position to auto (follows textarea)
    this.btn.addEventListener('dblclick', () => {
      this._positionLocked = false;
      this._clearPersistedPosition();
    });

    // Start hidden — repositionNear() is called lazily after the first input event.
    this.hide();
  }

  /** Update button position to float near the prompt element.
   *  No-op if the user has dragged the button to a custom position. */
  private repositionNear(el: PromptElement): void {
    if (this._positionLocked) return;
    const rect = el.getBoundingClientRect();
    this.wrap.style.bottom = `${window.innerHeight - rect.top + 8}px`;
    this.wrap.style.left = `${rect.left}px`;
  }

  private _savePosition(left: number, bottom: number): void {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage?.local) {
        chrome.storage.local.set({ [DRAG_POS_KEY]: { left, bottom } });
      }
    } catch { /* storage unavailable */ }
  }

  private _clearPersistedPosition(): void {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage?.local) {
        chrome.storage.local.remove(DRAG_POS_KEY);
      }
    } catch { /* storage unavailable */ }
  }

  private _loadPersistedPosition(): void {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage?.local) {
        chrome.storage.local.get(DRAG_POS_KEY, (result) => {
          if (chrome.runtime?.lastError) return;
          const pos = result[DRAG_POS_KEY] as { left: number; bottom: number } | undefined;
          if (pos) {
            this.wrap.style.left   = `${pos.left}px`;
            this.wrap.style.bottom = `${pos.bottom}px`;
            this._positionLocked = true;
          }
        });
      }
    } catch { /* storage unavailable */ }
  }

  /**
   * Called by content scripts whenever the prompt textarea has input.
   * Debounces 250 ms before showing the button (Hard Rule 58 preserved —
   * the button appearing is not injection; the click is the trigger).
   */
  onTextareaInput(
    el: PromptElement,
    fetchCtx: () => Promise<ContextBlock | null>,
    baselineStatus: BaselineStatus | null,
  ): void {
    const text = getCurrentText(el);

    if (!text.trim()) {
      this.hide();
      return;
    }

    if (this.debounceTimer !== null) clearTimeout(this.debounceTimer);
    this.debounceTimer = setTimeout(async () => {
      this.repositionNear(el);

      if (baselineStatus?.in_baseline) {
        const days = baselineStatus.days_remaining;
        this.baselineMsg.textContent =
          days != null
            ? `Baseline week: ${days} day${days !== 1 ? 's' : ''} left — MemStrata is measuring without injecting`
            : 'Baseline week in progress — measuring without injecting';
        this.baselineMsg.classList.add('visible');
        this.btn.style.display = 'none';
        return;
      }

      // Pre-fetch token count for display only; never auto-injects.
      const ctx = await fetchCtx();
      const mode = await getAugmentationMode();
      const modeLabel = mode === 'rewrite' ? '↺ Rewrite with context' : '＋ Add project context';
      if (ctx && ctx.token_count > 0) {
        this.btn.textContent = `${modeLabel} (${ctx.token_count.toLocaleString()} tokens)`;
      } else {
        // token_count === 0 means server is reachable but no history recorded yet.
        this.btn.textContent = `${modeLabel} — No context yet`;
      }

      this.btn.style.display = '';
      this.baselineMsg.classList.remove('visible');
      this.banner.classList.remove('visible');

      // Wire click handler — ONLY injection point (Hard Rule 58).
      // Phase 34: in rewrite mode, show mandatory diff view before writing
      // (Hard Rule 67). In append mode, existing behaviour is unchanged.
      this.btn.onclick = async () => {
        if (this._suppressNextClick) { this._suppressNextClick = false; return; }
        try {
          const latestCtx = ctx ?? await fetchCtx();
          if (!latestCtx || latestCtx.token_count === 0) {
            // Server reachable but no chat history recorded yet (or offline).
            // Surface this to the user instead of silently no-op-ing.
            this.btn.textContent = 'No context recorded yet — chat with an AI first';
            return;
          }

          this.originalText = getCurrentText(el);
          const currentMode = await getAugmentationMode();

          if (currentMode === 'rewrite') {
            // Hard Rule 67: generate rewrite and show diff before submitting.
            // Disable the provider's send button for the review window so
            // keyboard shortcuts can't submit the un-reviewed prompt.
            // lockSubmitButton() fails open — returns no-op if no button found.
            const unlockSubmit = lockSubmitButton();
            try {
              const engine = new RewriteEngine();
              const result = await engine.generateWithRetrieval(
                this.originalText,
                this.externalSessionId ?? '',
                this.providerId ?? '',
              );
              const diffView = new DiffView();
              const confirmed = await diffView.show(result);
              diffView.hide();

              // §7 — Per-rewrite telemetry (fire-and-forget; never blocks workflow)
              try {
                const turns = result.retrievalResult?.retrieved_turns ?? [];
                const simScores = turns
                  .map((t) => t.similarity_score)
                  .filter((s): s is number => s !== null && s !== undefined);
                const avgSim = simScores.length > 0
                  ? simScores.reduce((a, b) => a + b, 0) / simScores.length
                  : null;
                const nowMs = Date.now();
                const ageDistHours = turns
                  .map((t) => Math.round((nowMs - new Date(t.captured_at).getTime()) / 3_600_000))
                  .filter((h) => h >= 0);
                const rewriteId = typeof crypto !== 'undefined' && crypto.randomUUID
                  ? crypto.randomUUID()
                  : `rw-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

                recordRewriteTelemetry({
                  rewrite_id: rewriteId,
                  external_session_id: this.externalSessionId ?? undefined,
                  provider_id: this.providerId ?? '',
                  draft_prompt_chars: this.originalText.length,
                  retrieved_turn_count: turns.length,
                  retrieved_turn_avg_similarity: avgSim,
                  retrieved_turn_age_dist_hours: ageDistHours.length > 0 ? ageDistHours : undefined,
                  user_confirmed: confirmed,
                  delimiter_format: turns.length > 0 ? 'xml_tags' : 'none',
                  token_budget_used: result.retrievalResult?.token_budget_used,
                  token_budget_total: result.retrievalResult?.token_budget_total,
                  degraded: result.retrievalResult?.degraded ?? false,
                  degraded_reason: result.retrievalResult?.reason ?? null,
                }).catch(() => {});
              } catch {
                // Telemetry failure never disrupts the rewrite workflow
              }

              if (!confirmed) return; // user cancelled → leave textarea unchanged
              // Hard Rule 57: write through the React-aware setter.
              writeText(el, result.rewrittenPrompt);
              this.showBanner(el, latestCtx);
            } finally {
              unlockSubmit(); // always re-enable, even if an error occurred
            }
          } else {
            // Append mode (default) — unchanged behaviour.
            const augmented = buildAugmentedPrompt(latestCtx.text, this.originalText);
            // Hard Rule 57: only setReactInputValue / setContentEditableValue paths.
            writeText(el, augmented);
            this.showBanner(el, latestCtx);
          }
        } catch (err) {
          console.error('[ML:click_error]', err);
        }
      };
    }, 250);
  }

  private showBanner(el: PromptElement, ctx: ContextBlock): void {
    this.banner.innerHTML = '';

    const msg = document.createTextNode(
      `MemStrata added project state (${ctx.token_count.toLocaleString()} tokens) — `,
    );
    this.banner.appendChild(msg);

    const undoBtn = document.createElement('button');
    undoBtn.className = 'ml-action';
    undoBtn.textContent = 'undo';
    undoBtn.onclick = () => {
      writeText(el, this.originalText);
      this.banner.classList.remove('visible');
    };
    this.banner.appendChild(undoBtn);

    const sep = document.createElement('span');
    sep.className = 'ml-sep';
    sep.textContent = ' · ';
    this.banner.appendChild(sep);

    const hideBtn = document.createElement('button');
    hideBtn.className = 'ml-action';
    hideBtn.textContent = 'hide for this session';
    hideBtn.onclick = () => this.destroy();
    this.banner.appendChild(hideBtn);

    this.banner.classList.add('visible');
  }

  hide(): void {
    if (this.debounceTimer !== null) clearTimeout(this.debounceTimer);
    this.btn.style.display = 'none';
    this.banner.classList.remove('visible');
    this.baselineMsg.classList.remove('visible');
  }

  destroy(): void {
    this.hide();
    this.host.remove();
  }
}

/** Returns the singleton AugmenterUI, creating it on first call. */
export function getAugmenterUI(): AugmenterUI {
  const existing = document.getElementById(SHADOW_HOST_ID);
  if (existing) existing.remove();
  return new AugmenterUI();
}
