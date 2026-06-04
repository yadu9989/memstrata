import type { Candidate, Detector } from '../types';

const ARIA_LIVE_SELECTOR = '[aria-live="polite"], [aria-live="assertive"]';

export class AriaLiveDetector implements Detector {
  name = 'aria_live' as const;
  baseConfidence = 0.9;

  private observer: MutationObserver | null = null;
  private onCandidate: ((c: Candidate) => void) | null = null;

  initialScan(root: Element): Candidate[] {
    const liveRegions = root.querySelectorAll(ARIA_LIVE_SELECTOR);
    return Array.from(liveRegions).map((node) => ({
      node,
      detectorName: this.name,
      confidence: this.baseConfidence,
      detectedAt: performance.now(),
    }));
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
          if (
            el.matches?.(ARIA_LIVE_SELECTOR) ||
            el.querySelector?.(ARIA_LIVE_SELECTOR)
          ) {
            this.onCandidate?.({
              node: el,
              detectorName: this.name,
              confidence: this.baseConfidence,
              detectedAt: performance.now(),
            });
          }
        });
      }
    });

    this.observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: false, // CRITICAL: prevents mutation-loop hang
    });
  }

  stop(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.onCandidate = null;
  }
}
