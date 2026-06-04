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

import type { ContextBlock, BaselineStatus } from './memory_layer_client.js';
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

// ─── Shadow-DOM floating button + banner ──────────────────────────────────────

const SHADOW_HOST_ID = 'memory-layer-augmenter-root';

const BUTTON_STYLES = `
  :host { all: initial; }
  #ml-btn-wrap {
    position: fixed;
    z-index: 2147483647;
    pointer-events: none;
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
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease;
    white-space: nowrap;
  }
  #ml-btn:hover { background: #374151; border-color: #9ca3af; }
  #ml-btn:active { background: #111827; }
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

export class AugmenterUI {
  private host: HTMLElement;
  private shadow: ShadowRoot;
  private wrap: HTMLElement;
  private btn: HTMLButtonElement;
  private banner: HTMLElement;
  private baselineMsg: HTMLElement;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private originalText = '';

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
    this.btn.setAttribute('aria-label', 'Add Memory Layer project context to prompt');

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
  }

  /** Update button position to float near the prompt element. */
  private repositionNear(el: PromptElement): void {
    const rect = el.getBoundingClientRect();
    this.wrap.style.bottom = `${window.innerHeight - rect.top + 8}px`;
    this.wrap.style.left = `${rect.left}px`;
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
            ? `Baseline week: ${days} day${days !== 1 ? 's' : ''} left — Memory Layer is measuring without injecting`
            : 'Baseline week in progress — measuring without injecting';
        this.baselineMsg.classList.add('visible');
        this.btn.style.display = 'none';
        return;
      }

      // Pre-fetch token count for display only; never auto-injects.
      const ctx = await fetchCtx();
      const mode = await getAugmentationMode();
      const modeLabel = mode === 'rewrite' ? '↺ Rewrite with context' : '＋ Add project context';
      if (ctx) {
        this.btn.textContent = `${modeLabel} (${ctx.token_count.toLocaleString()} tokens)`;
      } else {
        this.btn.textContent = modeLabel;
      }

      this.btn.style.display = '';
      this.baselineMsg.classList.remove('visible');
      this.banner.classList.remove('visible');

      // Wire click handler — ONLY injection point (Hard Rule 58).
      // Phase 34: in rewrite mode, show mandatory diff view before writing
      // (Hard Rule 67). In append mode, existing behaviour is unchanged.
      this.btn.onclick = async () => {
        const latestCtx = ctx ?? await fetchCtx();
        if (!latestCtx) return;

        this.originalText = getCurrentText(el);
        const currentMode = await getAugmentationMode();

        if (currentMode === 'rewrite') {
          // Hard Rule 67: generate rewrite and show diff before submitting.
          const engine = new RewriteEngine();
          const result = engine.generate(this.originalText, latestCtx.text);
          const diffView = new DiffView();
          const confirmed = await diffView.show(result);
          diffView.hide();
          if (!confirmed) return; // user cancelled → leave textarea unchanged
          // Hard Rule 57: write through the React-aware setter.
          writeText(el, result.rewrittenPrompt);
          this.showBanner(el, latestCtx);
        } else {
          // Append mode (default) — unchanged behaviour.
          const augmented = buildAugmentedPrompt(latestCtx.text, this.originalText);
          // Hard Rule 57: only setReactInputValue / setContentEditableValue paths.
          writeText(el, augmented);
          this.showBanner(el, latestCtx);
        }
      };
    }, 250);
  }

  private showBanner(el: PromptElement, ctx: ContextBlock): void {
    this.banner.innerHTML = '';

    const msg = document.createTextNode(
      `Memory Layer added project state (${ctx.token_count.toLocaleString()} tokens) — `,
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
