import type { StreamMeta } from './types';
import { TurnExtractor } from './TurnExtractor';

export class StreamWatcher {
  private readonly candidate: Element;
  private readonly debounceMs: number;
  private readonly confidence: number;
  private readonly detectedBy: string[];
  private readonly onComplete: (text: string, meta: StreamMeta) => void;

  private startedAt: number;
  private lastActivity: number;
  private chunkCount = 0;
  private completionTimer: ReturnType<typeof setTimeout> | null = null;
  private observer: MutationObserver | null = null;

  constructor(
    candidate: Element,
    debounceMs: number,
    confidence: number,
    detectedBy: string[],
    onComplete: (text: string, meta: StreamMeta) => void,
  ) {
    this.candidate = candidate;
    this.debounceMs = debounceMs;
    this.confidence = confidence;
    this.detectedBy = detectedBy;
    this.onComplete = onComplete;
    this.startedAt = performance.now();
    this.lastActivity = this.startedAt;

    this.observer = new MutationObserver((mutations) => this.onActivity(mutations));
    this.observer.observe(candidate, {
      characterData: true,
      childList: true,
      subtree: true,
      attributes: false, // CRITICAL: prevent mutation loop
    });

    this.scheduleCompletion();
  }

  dispose(): void {
    if (this.completionTimer !== null) clearTimeout(this.completionTimer);
    this.observer?.disconnect();
    this.observer = null;
  }

  private onActivity(mutations: MutationRecord[]): void {
    this.lastActivity = performance.now();
    this.chunkCount += mutations.length;
    this.scheduleCompletion();
  }

  private scheduleCompletion(): void {
    if (this.completionTimer !== null) clearTimeout(this.completionTimer);
    this.completionTimer = setTimeout(() => this.complete(), this.debounceMs);
  }

  private complete(): void {
    const text = TurnExtractor.extract(this.candidate);
    const meta: StreamMeta = {
      durationMs: this.lastActivity - this.startedAt,
      chunkCount: this.chunkCount,
      finalCharCount: text.length,
      detectedBy: this.detectedBy,
      confidence: this.confidence,
    };
    this.observer?.disconnect();
    this.observer = null;
    if (text.length > 0) this.onComplete(text, meta);
  }
}
