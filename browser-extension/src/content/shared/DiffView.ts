// src/content/shared/DiffView.ts
//
// Phase 34 — Mandatory diff view for Rewrite mode (Hard Rule 67).
//
// Shows a side-by-side diff of the original vs. rewritten prompt.
// The submit flow is blocked until the user explicitly clicks
// "Confirm rewrite" or "Cancel & use original".
//
// Hard Rule 67: auto-rewrite without showing this diff is rejected.

import type { RewriteResult } from './RewriteEngine.js';

const HOST_ID = 'memstrata-diff-view-root';

const STYLES = `
  :host { all: initial; }
  #ml-dv-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 2147483645;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  #ml-dv-dialog {
    background: #13171f;
    border: 1px solid #2a3050;
    border-radius: 14px;
    width: min(720px, 95vw);
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 24px 64px rgba(0,0,0,0.7);
    font: 13px/1.5 system-ui, -apple-system, sans-serif;
    color: #c8d0da;
    overflow: hidden;
  }
  #ml-dv-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 20px;
    border-bottom: 1px solid #1e2436;
    flex-shrink: 0;
  }
  #ml-dv-title {
    font-size: 13px;
    font-weight: 600;
    color: #e8eaf0;
  }
  #ml-dv-badge {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    background: #1e2336;
    color: #7dd3a8;
    border: 1px solid #2a3348;
    border-radius: 6px;
    padding: 2px 8px;
  }
  #ml-dv-savings {
    font-size: 11px;
    color: #5a6878;
    margin-left: auto;
  }
  #ml-dv-body {
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .ml-dv-section {
    padding: 14px 20px;
    border-bottom: 1px solid #1e2436;
  }
  .ml-dv-section:last-child { border-bottom: none; }
  .ml-dv-section-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .09em;
    text-transform: uppercase;
    color: #3e4a5e;
    margin-bottom: 10px;
  }
  .ml-dv-mono {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    color: #c8d0da;
  }
  .ml-dv-banner {
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
    margin-bottom: 10px;
    line-height: 1.5;
  }
  .ml-dv-banner-pending {
    background: #2a1e10;
    color: #d4956a;
    border: 1px solid #3d2a1a;
  }
  .ml-dv-banner-unavail {
    background: #1e1e2a;
    color: #8a94a8;
    border: 1px solid #2d3348;
  }
  .ml-dv-banner-nohistory {
    background: #1a2a1a;
    color: #7dd3a8;
    border: 1px solid #2a3d2a;
  }
  .ml-dv-banner-short {
    background: #1e2336;
    color: #7a8cb8;
    border: 1px solid #2a3348;
  }
  .ml-dv-turn {
    background: #1a1e2c;
    border: 1px solid #252c42;
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }
  .ml-dv-turn:last-child { margin-bottom: 0; }
  .ml-dv-turn-meta {
    display: flex;
    gap: 14px;
    font-size: 11px;
    color: #4a5878;
    margin-bottom: 6px;
  }
  .ml-dv-turn-role { font-weight: 700; text-transform: uppercase; color: #7dd3a8; }
  .ml-dv-turn-role.user { color: #80b4d4; }
  .ml-dv-turn-text {
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    color: #c8d0da;
    max-height: 96px;
    overflow: hidden;
  }
  .ml-dv-tag { color: #4a5878; }
  #ml-dv-footer {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 20px;
    border-top: 1px solid #1e2436;
    flex-shrink: 0;
  }
  #ml-dv-hint {
    font-size: 11px;
    color: #3e4a5e;
    flex: 1;
  }
  button {
    padding: 7px 16px;
    border-radius: 8px;
    font: 500 13px/1 system-ui, sans-serif;
    cursor: pointer;
    border: 1px solid;
    transition: background 100ms ease;
  }
  #ml-dv-cancel {
    background: #1a1e2c;
    border-color: #2d3348;
    color: #8a94a8;
  }
  #ml-dv-cancel:hover { background: #222636; border-color: #3d4560; }
  #ml-dv-confirm {
    background: #3a7a5a;
    border-color: #4a8a6a;
    color: #d1fae5;
  }
  #ml-dv-confirm:hover { background: #478a68; }
`;

function el(tag: string, cls?: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function makeSection(labelText: string): { section: HTMLElement; body: HTMLElement } {
  const section = el('div', 'ml-dv-section');
  const label = el('div', 'ml-dv-section-label');
  label.textContent = labelText;
  section.appendChild(label);
  const body = el('div');
  section.appendChild(body);
  return { section, body };
}

export class DiffView {
  private _keydownHandler: ((e: KeyboardEvent) => void) | null = null;

  /**
   * Show the diff view modal. Returns a Promise resolving to true (user
   * confirmed the rewrite) or false (user cancelled, use original).
   *
   * Hard Rule 67: this method MUST be awaited before submitting the prompt.
   */
  show(result: RewriteResult): Promise<boolean> {
    return new Promise((resolve) => {
      // Guard: if document.body is not available, resolve false immediately
      // rather than throwing into an unhandled rejection.
      if (!document.body) {
        console.error('[ML:diff_view] document.body not ready — cannot mount diff modal');
        resolve(false);
        return;
      }

      try {
        document.getElementById(HOST_ID)?.remove();
      } catch {
        // HOST_ID cleanup failed — proceed without it
      }

      let host: HTMLDivElement;
      let shadow: ShadowRoot;

      try {
        host = document.createElement('div');
        host.id = HOST_ID;
        shadow = host.attachShadow({ mode: 'closed' });
      } catch (err) {
        console.error('[ML:diff_view] Shadow DOM creation failed:', err);
        resolve(false);
        return;
      }

      // Wrap all DOM construction + mount in try/catch so any unexpected
      // failure (CSP restrictions, detached frames, etc.) resolves the
      // promise as cancelled rather than leaving it forever pending.
      try {
        const style = document.createElement('style');
        style.textContent = STYLES;

        const overlay = document.createElement('div');
        overlay.id = 'ml-dv-overlay';

        const dialog = document.createElement('div');
        dialog.id = 'ml-dv-dialog';
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-label', 'Review prompt rewrite before sending');

        // ── Header ──────────────────────────────────────────────────────────
        const header = document.createElement('div');
        header.id = 'ml-dv-header';

        const badge = document.createElement('span');
        badge.id = 'ml-dv-badge';
        badge.textContent = 'MemStrata';

        const title = document.createElement('span');
        title.id = 'ml-dv-title';
        title.textContent = 'Review rewritten prompt';

        const savings = document.createElement('span');
        savings.id = 'ml-dv-savings';
        if (result.estimatedTokensSaved > 0) {
          savings.textContent = `~${result.estimatedTokensSaved.toLocaleString()} context tokens`;
        }

        header.appendChild(badge);
        header.appendChild(title);
        header.appendChild(savings);

        // ── Body (three sections) ────────────────────────────────────────────
        const body = document.createElement('div');
        body.id = 'ml-dv-body';

        // §6.3 Section 1 — Original Prompt
        const { section: s1, body: s1body } = makeSection('Original Prompt');
        const origText = el('div', 'ml-dv-mono');
        origText.textContent = result.originalPrompt;
        s1body.appendChild(origText);
        body.appendChild(s1);

        // §6.3 Section 2 — Retrieved Context
        const retrieval = result.retrievalResult;
        const turns = retrieval?.retrieved_turns ?? [];
        const ctxLabel = turns.length > 0
          ? `Retrieved Context (${turns.length} turn${turns.length !== 1 ? 's' : ''})`
          : 'Retrieved Context';
        const { section: s2, body: s2body } = makeSection(ctxLabel);

        if (retrieval === null || retrieval === undefined) {
          // Network error / backend unavailable
          const banner = el('div', 'ml-dv-banner ml-dv-banner-unavail');
          banner.textContent = 'MemStrata backend unreachable. Prompt will be sent without retrieved context.';
          s2body.appendChild(banner);
        } else if (retrieval.reason === 'draft_too_short') {
          const banner = el('div', 'ml-dv-banner ml-dv-banner-short');
          banner.textContent = 'Draft is too short for context retrieval (minimum 10 characters).';
          s2body.appendChild(banner);
        } else if (retrieval.reason === 'no_history') {
          const banner = el('div', 'ml-dv-banner ml-dv-banner-nohistory');
          banner.textContent = 'No previous turns in this chat session — context will grow as you chat.';
          s2body.appendChild(banner);
        } else if (retrieval.degraded && retrieval.reason === 'embeddings_pending') {
          const banner = el('div', 'ml-dv-banner ml-dv-banner-pending');
          banner.textContent = 'MemStrata is still indexing your chat history. Showing most recent turns chronologically.';
          s2body.appendChild(banner);
        }

        for (const turn of turns) {
          const card = el('div', 'ml-dv-turn');
          const meta = el('div', 'ml-dv-turn-meta');
          const roleEl = el('span', `ml-dv-turn-role${turn.role === 'user' ? ' user' : ''}`);
          roleEl.textContent = turn.role;
          meta.appendChild(roleEl);
          const ageEl = el('span');
          ageEl.textContent = turn.age_human;
          meta.appendChild(ageEl);
          if (turn.similarity_score !== null && turn.similarity_score !== undefined) {
            const simEl = el('span');
            simEl.textContent = `similarity ${(turn.similarity_score * 100).toFixed(0)}%`;
            meta.appendChild(simEl);
          }
          card.appendChild(meta);
          const textEl = el('div', 'ml-dv-turn-text');
          textEl.textContent = turn.text;
          card.appendChild(textEl);
          s2body.appendChild(card);
        }

        body.appendChild(s2);

        // §6.3 Section 3 — Final Tagged Prompt
        const { section: s3, body: s3body } = makeSection('Final Prompt (will be sent)');
        const finalText = el('div', 'ml-dv-mono');
        finalText.textContent = result.rewrittenPrompt;
        s3body.appendChild(finalText);
        body.appendChild(s3);

        // ── Footer ───────────────────────────────────────────────────────────
        const footer = document.createElement('div');
        footer.id = 'ml-dv-footer';

        const hint = document.createElement('span');
        hint.id = 'ml-dv-hint';
        hint.textContent = 'Escape — cancel • Enter — confirm rewrite';

        const cancelBtn = document.createElement('button');
        cancelBtn.id = 'ml-dv-cancel';
        cancelBtn.textContent = 'Cancel & use original';

        const confirmBtn = document.createElement('button');
        confirmBtn.id = 'ml-dv-confirm';
        confirmBtn.textContent = 'Confirm rewrite';

        footer.appendChild(hint);
        footer.appendChild(cancelBtn);
        footer.appendChild(confirmBtn);

        dialog.appendChild(header);
        dialog.appendChild(body);
        dialog.appendChild(footer);
        overlay.appendChild(dialog);
        shadow.appendChild(style);
        shadow.appendChild(overlay);
        document.body.appendChild(host);

        // done() unconditionally cleans up and resolves the promise — it is
        // the only resolution path so the caller can never be left hanging.
        const done = (confirmed: boolean): void => {
          try { host.remove(); } catch {}
          if (this._keydownHandler) {
            document.removeEventListener('keydown', this._keydownHandler, true);
            this._keydownHandler = null;
          }
          resolve(confirmed);
        };

        cancelBtn.addEventListener('click', () => done(false));
        confirmBtn.addEventListener('click', () => done(true));
        overlay.addEventListener('click', (e) => {
          if (e.target === overlay) done(false);
        });

        this._keydownHandler = (e: KeyboardEvent) => {
          if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); done(false); }
          if (e.key === 'Enter') { e.preventDefault(); e.stopImmediatePropagation(); done(true); }
        };
        document.addEventListener('keydown', this._keydownHandler, true);

        setTimeout(() => confirmBtn.focus(), 0);
      } catch (err) {
        console.error('[ML:diff_view] Modal mount failed:', err);
        try { host.remove(); } catch {}
        resolve(false);
      }
    });
  }

  hide(): void {
    document.getElementById(HOST_ID)?.remove();
    if (this._keydownHandler) {
      document.removeEventListener('keydown', this._keydownHandler, true);
      this._keydownHandler = null;
    }
  }
}
