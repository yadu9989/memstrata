import type { Candidate, Detector } from '../types';
import { normalizeContainer } from '../types';

const SEMANTIC_SELECTORS = [
  '[role="article"]',
  '[data-message-author-role="assistant"]',
  '[data-testid*="conversation-turn"]',
  '[data-testid="assistant-message"]',   // Grok, Meta AI
  '[data-renderer="lm"]',               // Perplexity
  '[class*="assistant-message" i]',     // generic
  '[class*="message-bubble" i]',        // Grok
  '[aria-label*="assistant" i]',
  '[aria-label*="response" i]',
];

const SELECTOR = SEMANTIC_SELECTORS.join(',');

export class SemanticAttrDetector implements Detector {
  name = 'semantic_attr' as const;
  baseConfidence = 0.8;

  private observer: MutationObserver | null = null;
  private seen = new WeakSet<Element>();
  private onCandidate: ((c: Candidate) => void) | null = null;

  initialScan(root: Element): Candidate[] {
    const matches = root.querySelectorAll(SELECTOR);
    return Array.from(matches)
      .map((el) => normalizeContainer(el))
      .filter((el) => !this.seen.has(el))
      .map((el) => {
        this.seen.add(el);
        return {
          node: el,
          detectorName: this.name,
          confidence: this.computeConfidence(el),
          detectedAt: performance.now(),
        };
      });
  }

  start(root: Element, onCandidate: (c: Candidate) => void): void {
    this.onCandidate = onCandidate;
    this.initialScan(root).forEach((c) => onCandidate(c));

    this.observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type !== 'childList') continue;
        m.addedNodes.forEach((n) => {
          if (n.nodeType !== Node.ELEMENT_NODE) return;
          const el = n as Element;
          this.tryEmit(el);
          el.querySelectorAll?.(SELECTOR).forEach((inner) => this.tryEmit(inner));
        });
      }
    });

    this.observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: false,
    });
  }

  stop(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.onCandidate = null;
  }

  private tryEmit(el: Element): void {
    // Check the raw element first so we know it semantically matches,
    // then normalize up to the nearest logical container for node-identity
    // alignment with VelocityDetector candidates.
    const matched = el.matches?.(SELECTOR) ? el : null;
    if (!matched) return;

    const container = normalizeContainer(matched);
    if (this.seen.has(container)) return;
    this.seen.add(container);
    this.onCandidate?.({
      node: container,
      detectorName: this.name,
      confidence: this.computeConfidence(container),
      detectedAt: performance.now(),
    });
  }

  private computeConfidence(node: Element): number {
    if (node.getAttribute('data-message-author-role') === 'assistant') return 0.95;

    const ariaLabel = node.getAttribute('aria-label')?.toLowerCase() ?? '';
    if (ariaLabel.includes('assistant')) return 0.9;

    return this.baseConfidence;
  }
}
