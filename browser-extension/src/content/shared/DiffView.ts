// src/content/shared/DiffView.ts
//
// Phase 34 — Mandatory diff view for Rewrite mode (Hard Rule 67).
//
// Shows a side-by-side diff of the original vs. rewritten prompt.
// The submit flow is blocked until the user explicitly clicks
// "Confirm rewrite" or "Cancel & use original".
//
// Hard Rule 67: auto-rewrite without showing this diff is rejected.

import type { RewriteResult, DiffSegment } from './RewriteEngine.js';

const HOST_ID = 'memory-layer-diff-view-root';

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
    width: min(860px, 95vw);
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
  #ml-dv-columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1px;
    background: #1e2436;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .ml-dv-col {
    background: #13171f;
    padding: 14px 16px;
    overflow-y: auto;
  }
  .ml-dv-col-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .09em;
    text-transform: uppercase;
    color: #3e4a5e;
    margin-bottom: 10px;
  }
  .ml-dv-text {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .ml-removed {
    background: #3d1515;
    color: #f08080;
    border-radius: 2px;
  }
  .ml-added {
    background: #1a3020;
    color: #7dd3a8;
    border-radius: 2px;
  }
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

function renderSegments(segments: DiffSegment[], side: 'left' | 'right'): HTMLElement {
  const container = document.createElement('div');
  container.className = 'ml-dv-text';

  for (const seg of segments) {
    if (seg.type === 'unchanged') {
      container.appendChild(document.createTextNode(seg.text));
    } else if (seg.type === 'removed' && side === 'left') {
      const span = document.createElement('span');
      span.className = 'ml-removed';
      span.textContent = seg.text;
      container.appendChild(span);
    } else if (seg.type === 'added' && side === 'right') {
      const span = document.createElement('span');
      span.className = 'ml-added';
      span.textContent = seg.text;
      container.appendChild(span);
    } else if (seg.type === 'removed' && side === 'right') {
      // removed text not shown on the right side
    } else if (seg.type === 'added' && side === 'left') {
      // added text not shown on the left side
    }
  }

  return container;
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
      document.getElementById(HOST_ID)?.remove();

      const host = document.createElement('div');
      host.id = HOST_ID;
      const shadow = host.attachShadow({ mode: 'closed' });

      const style = document.createElement('style');
      style.textContent = STYLES;

      const overlay = document.createElement('div');
      overlay.id = 'ml-dv-overlay';

      const dialog = document.createElement('div');
      dialog.id = 'ml-dv-dialog';
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-modal', 'true');
      dialog.setAttribute('aria-label', 'Review prompt rewrite before sending');

      // Header
      const header = document.createElement('div');
      header.id = 'ml-dv-header';

      const badge = document.createElement('span');
      badge.id = 'ml-dv-badge';
      badge.textContent = 'Memory Layer';

      const title = document.createElement('span');
      title.id = 'ml-dv-title';
      title.textContent = 'Review rewritten prompt';

      const savings = document.createElement('span');
      savings.id = 'ml-dv-savings';
      if (result.estimatedTokensSaved > 0) {
        savings.textContent = `~${result.estimatedTokensSaved.toLocaleString()} tokens saved`;
      }

      header.appendChild(badge);
      header.appendChild(title);
      header.appendChild(savings);

      // Columns
      const columns = document.createElement('div');
      columns.id = 'ml-dv-columns';

      const leftCol = document.createElement('div');
      leftCol.className = 'ml-dv-col';
      const leftLabel = document.createElement('div');
      leftLabel.className = 'ml-dv-col-label';
      leftLabel.textContent = 'Original';
      leftCol.appendChild(leftLabel);
      leftCol.appendChild(renderSegments(result.diff, 'left'));

      const rightCol = document.createElement('div');
      rightCol.className = 'ml-dv-col';
      const rightLabel = document.createElement('div');
      rightLabel.className = 'ml-dv-col-label';
      rightLabel.textContent = 'Rewritten';
      rightCol.appendChild(rightLabel);
      rightCol.appendChild(renderSegments(result.diff, 'right'));

      columns.appendChild(leftCol);
      columns.appendChild(rightCol);

      // Footer
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
      dialog.appendChild(columns);
      dialog.appendChild(footer);
      overlay.appendChild(dialog);
      shadow.appendChild(style);
      shadow.appendChild(overlay);
      document.body.appendChild(host);

      const done = (confirmed: boolean): void => {
        host.remove();
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
