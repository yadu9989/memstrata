// src/options/options.ts — preferences page logic.
export {};

function $<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

const enableClaude  = $<HTMLInputElement>('enable-claude');
const enableChatGPT = $<HTMLInputElement>('enable-chatgpt');
const btnPosition   = $<HTMLSelectElement>('btn-position');
const theme         = $<HTMLSelectElement>('theme');
const saveBtn       = $('save-btn');
const savedMsg      = $('saved-msg');

chrome.storage.local.get(['options'], result => {
  const opts = result['options'] ?? {};
  enableClaude.checked  = opts.enableClaude  ?? true;
  enableChatGPT.checked = opts.enableChatGPT ?? true;
  btnPosition.value     = opts.btnPosition   ?? 'above';
  theme.value           = opts.theme         ?? 'dark';
});

saveBtn.addEventListener('click', () => {
  const opts = {
    enableClaude:  enableClaude.checked,
    enableChatGPT: enableChatGPT.checked,
    btnPosition:   btnPosition.value,
    theme:         theme.value,
  };
  chrome.storage.local.set({ options: opts }, () => {
    savedMsg.style.display = 'block';
    setTimeout(() => { savedMsg.style.display = 'none'; }, 2000);
  });
});
