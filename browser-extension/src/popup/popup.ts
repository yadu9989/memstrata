// src/popup/popup.ts — toolbar popup logic.
export {};

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} not found`);
  return el;
}

const badge     = $('status-badge');
const apiKeyEl  = $('api-key') as HTMLInputElement;
const saveKeyBtn = $('save-key-btn');
const projectEl = $('project-id') as HTMLInputElement;
const saveProjectBtn = $('save-project-btn');
const savingsBlock  = $('savings-block');
const savingsAmount = $('savings-amount');

function setStatus(connected: boolean): void {
  badge.textContent = connected ? 'online' : 'offline';
  badge.className = `badge ${connected ? 'online' : 'offline'}`;
}

async function init(): Promise<void> {
  // Retrieve current status from service worker.
  chrome.runtime.sendMessage({ type: 'GET_STATUS' }, res => {
    setStatus(res?.connected ?? false);
  });

  // Populate saved values.
  chrome.storage.local.get(['apiKey', 'projectId', 'monthlySavingsUsd'], result => {
    if (result['apiKey']) {
      apiKeyEl.value = '•'.repeat(20);
      apiKeyEl.dataset['saved'] = 'true';
    }
    if (result['projectId']) {
      projectEl.value = result['projectId'];
    }
    const usd = result['monthlySavingsUsd'] as number | undefined;
    if (usd != null && usd > 0) {
      savingsAmount.textContent = `$${usd.toFixed(2)}`;
      savingsBlock.style.display = '';
    }
  });

  saveKeyBtn.addEventListener('click', () => {
    const key = apiKeyEl.dataset['saved'] === 'true' ? '' : apiKeyEl.value.trim();
    if (!key && apiKeyEl.dataset['saved'] !== 'true') {
      apiKeyEl.focus();
      return;
    }
    const actualKey = apiKeyEl.dataset['saved'] === 'true'
      ? undefined
      : key;
    if (actualKey !== undefined) {
      chrome.runtime.sendMessage(
        { type: 'SAVE_API_KEY', key: actualKey },
        res => { if (res?.ok) setStatus(true); },
      );
    }
  });

  saveProjectBtn.addEventListener('click', () => {
    const pid = projectEl.value.trim();
    if (!pid) return;
    chrome.runtime.sendMessage(
      { type: 'SAVE_PROJECT_ID', projectId: pid },
      () => { saveProjectBtn.textContent = 'Saved ✓'; setTimeout(() => { saveProjectBtn.textContent = 'Set project'; }, 1500); },
    );
  });
}

init();
