// src/content/shared/observer.ts
//
// MutationObserver factory for capturing finalized AI response turns.
//
// Streaming-completion heuristic: 1500 ms of no new DOM mutations inside an
// assistant message node = stream done. The timer resets on every mutation.
// On completion, innerText is extracted once and the observer is disconnected.
//
// Page Visibility API: a global registry of active disposers lets callers
// pause all observers when the tab is hidden and resume when visible.

export type StreamCompleteHandler = (node: Element, finalText: string) => void;

const activeDisposers: Set<() => void> = new Set();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Pause all active streaming observers — saves CPU when tab is backgrounded.
    // We register the disposer map rather than calling dispose, so re-registration
    // can happen on visibility restore. For simplicity: disconnect on hide, and
    // let the parent content script re-observe when the tab becomes visible again.
    for (const dispose of activeDisposers) dispose();
    activeDisposers.clear();
  }
});

/**
 * Observe a single assistant message node for streaming completion.
 *
 * @param node     The DOM element that receives streaming content.
 * @param onComplete  Called once, with the final innerText, after the quiet window.
 * @param quietMs  Milliseconds of silence = stream done (default 1500).
 * @returns A disposer function that cancels the observation early.
 */
export function observeStreamingCompletion(
  node: Element,
  onComplete: StreamCompleteHandler,
  quietMs = 1500,
): () => void {
  let timer: number | null = null;

  const finish = () => {
    observer.disconnect();
    activeDisposers.delete(dispose);
    const text = (node as HTMLElement).innerText.trim();
    if (text) onComplete(node, text);
  };

  const observer = new MutationObserver(() => {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(finish, quietMs);
  });

  observer.observe(node, {
    childList: true,
    characterData: true,
    subtree: true,
  });

  // Kick off the initial timer in case the node is already fully rendered.
  timer = window.setTimeout(finish, quietMs);

  const dispose = () => {
    if (timer !== null) window.clearTimeout(timer);
    observer.disconnect();
    activeDisposers.delete(dispose);
  };

  activeDisposers.add(dispose);
  return dispose;
}

/**
 * Watch a message-list container for newly added assistant message nodes.
 *
 * @param container    The parent element housing the conversation turns.
 * @param isAssistant  Predicate: returns true when a new child is an assistant turn.
 * @param onTurnReady  Called with each finalized assistant turn text.
 * @returns A disposer that tears down the top-level observer.
 */
export function observeMessageList(
  container: Element,
  isAssistant: (node: Element) => boolean,
  onTurnReady: (node: Element, text: string) => void,
): () => void {
  const seen = new WeakSet<Element>();

  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      for (const node of Array.from(m.addedNodes)) {
        if (!(node instanceof Element)) continue;
        if (seen.has(node)) continue;
        seen.add(node);

        if (isAssistant(node)) {
          observeStreamingCompletion(node, onTurnReady);
        }
      }
    }
  });

  // subtree: true catches assistant elements added at any depth inside the
  // container (e.g. model-response nested inside a turn wrapper on Gemini,
  // or assistant-message nested inside a conversation-turn on Claude.ai).
  observer.observe(container, { childList: true, subtree: true });

  const dispose = () => observer.disconnect();
  return dispose;
}
