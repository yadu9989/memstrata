import type { Candidate, Detector } from '../types';

const SEMANTIC_SELECTORS = [
  '[role="log"]',
  '[role="feed"]',
  '[aria-label*="conversation" i]',
  '[aria-label*="messages" i]',
  '[aria-label*="chat" i]',
];

const CLASS_PATTERNS = [
  /message[s-]?list/i,
  /chat[- ]?history/i,
  /conversation[- ]?thread/i,
  /turn[s-]?container/i,
];

const MAX_DEPTH = 8;
const MIN_CHILDREN = 2;

export class StructuralDetector implements Detector {
  readonly name = 'structural' as const;
  readonly baseConfidence = 0.4;

  private messageListContainer: Element | null = null;
  private onCandidate: ((c: Candidate) => void) | null = null;
  private observer: MutationObserver | null = null;

  initialScan(root: Element): Candidate[] {
    this.messageListContainer = this.findMessageList(root);
    return [];
  }

  start(root: Element, onCandidate: (c: Candidate) => void): void {
    this.onCandidate = onCandidate;
    this.messageListContainer = this.findMessageList(root);

    this.observer = new MutationObserver((mutations) => this.onMutations(mutations, root));
    this.observer.observe(root, { childList: true, subtree: true, attributes: false });
  }

  stop(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.messageListContainer = null;
    this.onCandidate = null;
  }

  private onMutations(mutations: MutationRecord[], root: Element): void {
    // Re-scan if we haven't found a container yet
    if (!this.messageListContainer) {
      this.messageListContainer = this.findMessageList(root);
    }
    if (!this.messageListContainer) return;

    for (const m of mutations) {
      if (m.type !== 'childList') continue;
      if (m.target !== this.messageListContainer) continue;
      for (const node of Array.from(m.addedNodes)) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        this.onCandidate?.({
          node: node as Element,
          detectorName: 'structural',
          confidence: this.baseConfidence,
          detectedAt: Date.now(),
        });
      }
    }
  }

  private findMessageList(root: Element): Element | null {
    // 1. Semantic selectors
    for (const sel of SEMANTIC_SELECTORS) {
      const el = root.querySelector(sel);
      if (el) return el;
    }

    // 2. Class name patterns
    const allElements = root.querySelectorAll('*');
    for (const el of Array.from(allElements)) {
      const cls = el.className;
      if (typeof cls === 'string' && CLASS_PATTERNS.some((p) => p.test(cls))) {
        return el;
      }
    }

    // 3. Child-count heuristic: find the element with the most direct element children
    return this.findByChildCount(root, 0);
  }

  private findByChildCount(el: Element, depth: number): Element | null {
    if (depth > MAX_DEPTH) return null;

    let bestEl: Element | null = null;
    let bestCount = MIN_CHILDREN - 1; // must exceed this to qualify

    for (const child of Array.from(el.children)) {
      const directElementChildren = Array.from(child.children).length;
      if (directElementChildren > bestCount) {
        bestCount = directElementChildren;
        bestEl = child;
      }
      const deeper = this.findByChildCount(child, depth + 1);
      if (deeper) {
        const deeperCount = Array.from(deeper.children).length;
        if (deeperCount > bestCount) {
          bestCount = deeperCount;
          bestEl = deeper;
        }
      }
    }

    return bestEl;
  }
}
