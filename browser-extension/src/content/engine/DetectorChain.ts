import type { Candidate, Detector } from './types';

export const PROMOTION_THRESHOLD = 0.7;

function isDebug(): boolean {
  try { return localStorage.getItem('ML_DEBUG') === 'true'; } catch { return false; }
}

export function combinedConfidence(candidates: { confidence: number }[]): number {
  if (candidates.length === 0) return 0;
  if (candidates.length === 1) return candidates[0].confidence;
  // Multiplicative-with-floor: p = 1 - ∏(1 - p_i), floored at max individual
  const product = candidates.reduce((acc, c) => acc * (1 - c.confidence), 1);
  const combined = 1 - product;
  const max = Math.max(...candidates.map((c) => c.confidence));
  return Math.max(combined, max);
}

export class DetectorChain {
  private detectors: Detector[];
  private candidatesByNode = new Map<Element, Candidate[]>();
  private promoted = new WeakSet<Element>();
  private onStreamPromoted: ((node: Element, conf: number) => void) | null = null;
  private onCandidateSeen?: (detectorName: string) => void;

  constructor(detectors: Detector[]) {
    this.detectors = detectors;
  }

  // onCandidateSeen is optional — used by Telemetry to track which layers are active.
  start(
    root: Element,
    onStreamPromoted: (node: Element, conf: number) => void,
    onCandidateSeen?: (detectorName: string) => void,
  ): void {
    this.onStreamPromoted = onStreamPromoted;
    this.onCandidateSeen = onCandidateSeen;
    for (const detector of this.detectors) {
      detector.start(root, (c) => this.handleCandidate(c));
    }
  }

  stop(): void {
    for (const d of this.detectors) d.stop();
    this.candidatesByNode.clear();
    this.onStreamPromoted = null;
    this.onCandidateSeen = undefined;
  }

  private handleCandidate(c: Candidate): void {
    // Notify telemetry that this detector layer has produced a signal
    this.onCandidateSeen?.(c.detectorName);

    const existing = this.candidatesByNode.get(c.node) ?? [];

    // Keep only the most recent signal per detector per node
    const deduped = existing.filter((e) => e.detectorName !== c.detectorName);
    deduped.push(c);
    this.candidatesByNode.set(c.node, deduped);

    const combined = combinedConfidence(deduped);

    if (isDebug()) {
      const perDetector = deduped.map((d) => `${d.detectorName}=${d.confidence}`).join(', ');
      const formula = deduped.length > 1
        ? `1 - ${deduped.map((d) => `(1-${d.confidence})`).join('×')} = ${combined.toFixed(4)}`
        : `${combined.toFixed(4)}`;
      console.log(
        '[ML:chain] candidate from=%s conf=%s | all=[%s] | combined=%s | threshold=%s | promoted=%s',
        c.detectorName, c.confidence, perDetector, formula,
        PROMOTION_THRESHOLD, this.promoted.has(c.node) ? 'already' : combined >= PROMOTION_THRESHOLD ? 'YES' : 'no',
      );
    }

    if (combined >= PROMOTION_THRESHOLD && !this.promoted.has(c.node)) {
      this.promoted.add(c.node);
      this.onStreamPromoted?.(c.node, combined);
    }
  }
}
