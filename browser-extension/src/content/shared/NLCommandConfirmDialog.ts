// src/content/shared/NLCommandConfirmDialog.ts
//
// Phase 32 — In-extension confirmation dialog for NL commands (Hard Rule 66).
//
// Uses Shadow DOM so it is fully isolated from the host page's CSS.
// Never uses the native browser `confirm()` — that would be indistinguishable
// from a phishing popup and is blocked on many sites.

import type { NLCommand } from './NLCommandDetector.js';

const HOST_ID = 'memstrata-nl-confirm-root';

const STYLES = `
  :host { all: initial; }
  #ml-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 2147483646;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  #ml-dialog {
    background: #1a1e2c;
    border: 1px solid #2d3348;
    border-radius: 12px;
    padding: 24px 28px;
    max-width: 420px;
    width: 90vw;
    box-shadow: 0 16px 48px rgba(0,0,0,0.6);
    font: 13px/1.5 system-ui, -apple-system, sans-serif;
    color: #c8d0da;
  }
  #ml-badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    background: #1e2336;
    color: #7dd3a8;
    border: 1px solid #2a3348;
    border-radius: 6px;
    padding: 2px 8px;
    margin-bottom: 12px;
  }
  #ml-title {
    font-size: 14px;
    font-weight: 600;
    color: #e8eaf0;
    margin-bottom: 8px;
  }
  #ml-desc {
    font-size: 12px;
    color: #8a94a8;
    margin-bottom: 20px;
    line-height: 1.6;
  }
  #ml-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
  button {
    padding: 7px 16px;
    border-radius: 8px;
    font: 500 13px/1 system-ui, sans-serif;
    cursor: pointer;
    border: 1px solid;
    transition: background 120ms ease, border-color 120ms ease;
  }
  #ml-cancel {
    background: #13171f;
    border-color: #2d3348;
    color: #8a94a8;
  }
  #ml-cancel:hover { background: #1e2230; border-color: #4a5070; }
  #ml-confirm {
    background: #c06a6a;
    border-color: #d07a7a;
    color: #fff;
  }
  #ml-confirm:not(.destructive) { background: #3a7a5a; border-color: #4a8a6a; }
  #ml-confirm:hover { filter: brightness(1.12); }
`;

export class NLCommandConfirmDialog {
  /**
   * Show a confirmation dialog for *cmd*. Returns a Promise that resolves to
   * true (user confirmed) or false (user cancelled).
   *
   * Hard Rule 66: this is the ONLY code path that may call cmd.execute().
   * The caller must await this and check the return value before executing.
   */
  show(cmd: Pick<NLCommand, 'id' | 'description' | 'destructive' | 'confirmationLevel'>): Promise<boolean> {
    return new Promise((resolve) => {
      // Remove any stale dialog
      document.getElementById(HOST_ID)?.remove();

      const host = document.createElement('div');
      host.id = HOST_ID;
      const shadow = host.attachShadow({ mode: 'closed' });

      const style = document.createElement('style');
      style.textContent = STYLES;

      const overlay = document.createElement('div');
      overlay.id = 'ml-overlay';

      const dialog = document.createElement('div');
      dialog.id = 'ml-dialog';
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-modal', 'true');

      const badge = document.createElement('div');
      badge.id = 'ml-badge';
      badge.textContent = 'MemStrata';

      const title = document.createElement('div');
      title.id = 'ml-title';
      title.textContent = cmd.destructive ? 'Confirm destructive action' : 'MemStrata command';

      const desc = document.createElement('div');
      desc.id = 'ml-desc';
      desc.textContent = cmd.description;

      const actions = document.createElement('div');
      actions.id = 'ml-actions';

      const cancelBtn = document.createElement('button');
      cancelBtn.id = 'ml-cancel';
      cancelBtn.textContent = 'Cancel';

      const confirmBtn = document.createElement('button');
      confirmBtn.id = 'ml-confirm';
      if (cmd.destructive) confirmBtn.classList.add('destructive');
      confirmBtn.textContent = cmd.destructive ? 'Confirm delete' : 'OK';

      const done = (result: boolean): void => {
        host.remove();
        document.removeEventListener('keydown', onKeydown, true);
        resolve(result);
      };

      cancelBtn.addEventListener('click', () => done(false));
      confirmBtn.addEventListener('click', () => done(true));
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) done(false);
      });

      const onKeydown = (e: KeyboardEvent): void => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); done(false); }
        if (e.key === 'Enter') { e.preventDefault(); e.stopImmediatePropagation(); done(true); }
      };
      document.addEventListener('keydown', onKeydown, true);

      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      dialog.appendChild(badge);
      dialog.appendChild(title);
      dialog.appendChild(desc);
      dialog.appendChild(actions);
      overlay.appendChild(dialog);
      shadow.appendChild(style);
      shadow.appendChild(overlay);
      document.body.appendChild(host);

      // Focus confirm button so keyboard works immediately
      setTimeout(() => confirmBtn.focus(), 0);
    });
  }
}
