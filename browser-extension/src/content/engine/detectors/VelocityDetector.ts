export type { Candidate } from '../types';
import type { Candidate } from '../types';
import { normalizeContainer } from '../types';

const VELOCITY_PARAMS = {
  MIN_CHUNKS: 3,
  MIN_RATE: 3.0,           // chunks/sec; slower = probably typing
  MIN_AVG_CHARS: 2,        // 1-char-per-mutation = typing pattern
  MIN_SPAN_MS: 200,        // need sustained activity, not single burst
  WINDOW_MS: 5000,         // sliding window for rate measurement
  EXCLUDE_SELECTOR:
    'textarea, input, [contenteditable="true"], [contenteditable=""]',
};

interface VelocityState {
  node: Element;
  mutations: Array<{ ts: number; charsAdded: number }>;
  startedAt: number;
  lastUpdate: number;
  fired: boolean;
}

function isDebug(): boolean {
  try { return localStorage.getItem('ML_DEBUG') === 'true'; } catch { return false; }
}

export class VelocityDetector {
  name = 'velocity' as const;
  baseConfidence = 0.6;

  private observer: MutationObserver | null = null;
  private states = new WeakMap<Element, VelocityState>();
  private allStates = new Set<VelocityState>();
  private onCandidate: ((c: Candidate) => void) | null = null;

  initialScan(_root: Element): Candidate[] {
    return []; // velocity is purely runtime-observed
  }

  start(root: Element, onCandidate: (c: Candidate) => void): void {
    this.onCandidate = onCandidate;
    this.observer = new MutationObserver((mutations) =>
      this.onMutations(mutations)
    );
    this.observer.observe(root, {
      characterData: true,
      childList: true,
      subtree: true,
      attributes: false, // CRITICAL: see REVIEW §6 Cause 2 — prevents mutation loop
    });
  }

  stop(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.allStates.clear();
    this.onCandidate = null;
  }

  // Called by ShadowPiercer to route shadow-root mutations through the same pipeline.
  processMutations(mutations: MutationRecord[]): void {
    this.onMutations(mutations);
  }

  private onMutations(mutations: MutationRecord[]): void {
    const debug = isDebug();

    for (const m of mutations) {
      // 1. Get the raw leaf element touched by this mutation
      const rawTarget = this.getTextTarget(m);
      if (!rawTarget) continue;

      // 2. Exclude user input paths before doing anything else
      if (this.isInsideInput(rawTarget)) {
        if (debug) console.log('[ML:velocity] skipped — inside input:', rawTarget);
        continue;
      }

      const charsAdded = this.computeCharsAdded(m);
      if (debug) console.log('[ML:velocity] mutation type=%s charsAdded=%d raw=', m.type, charsAdded, rawTarget);
      if (charsAdded === 0) continue;

      // 3. Normalize to the nearest logical assistant-turn container.
      //    This ensures that velocity events from many child text nodes all
      //    accumulate on one container node — the same node SemanticAttrDetector
      //    emits for — so the two candidates combine in DetectorChain.
      const target = normalizeContainer(rawTarget);
      if (debug && target !== rawTarget) {
        console.log('[ML:velocity] normalized leaf → container:', target);
      }

      const now = performance.now();
      let state = this.states.get(target);
      if (!state) {
        state = {
          node: target,
          mutations: [],
          startedAt: now,
          lastUpdate: now,
          fired: false,
        };
        this.states.set(target, state);
        this.allStates.add(state);
      }

      state.mutations.push({ ts: now, charsAdded });
      state.lastUpdate = now;

      // Trim mutations outside the sliding window
      state.mutations = state.mutations.filter(
        (mut) => now - mut.ts < VELOCITY_PARAMS.WINDOW_MS
      );

      if (!state.fired) {
        const span = state.lastUpdate - state.startedAt;
        const rate = span > 0 ? state.mutations.length / (span / 1000) : 0;
        const totalChars = state.mutations.reduce((sum, mut) => sum + mut.charsAdded, 0);
        const avgChars = state.mutations.length > 0 ? totalChars / state.mutations.length : 0;

        if (debug) {
          const chunks = state.mutations.length;
          const failing: string[] = [];
          if (chunks < VELOCITY_PARAMS.MIN_CHUNKS)
            failing.push(`MIN_CHUNKS (need ${VELOCITY_PARAMS.MIN_CHUNKS}, have ${chunks})`);
          if (span < VELOCITY_PARAMS.MIN_SPAN_MS)
            failing.push(`MIN_SPAN_MS (need ${VELOCITY_PARAMS.MIN_SPAN_MS}ms, have ${Math.round(span)}ms)`);
          if (rate < VELOCITY_PARAMS.MIN_RATE)
            failing.push(`MIN_RATE (need ${VELOCITY_PARAMS.MIN_RATE}/s, have ${rate.toFixed(2)}/s)`);
          if (avgChars < VELOCITY_PARAMS.MIN_AVG_CHARS)
            failing.push(`MIN_AVG_CHARS (need ${VELOCITY_PARAMS.MIN_AVG_CHARS}, have ${avgChars.toFixed(2)})`);
          if (failing.length > 0) {
            console.log('[ML:velocity] NOT streaming — failing:', failing.join(', '));
          } else {
            console.log('[ML:velocity] isStreaming=true chunks=%d span=%dms rate=%.2f/s avgChars=%.2f',
              chunks, Math.round(span), rate, avgChars);
          }
        }

        if (this.isStreaming(state)) {
          state.fired = true;
          if (debug) console.log('[ML:velocity] FIRED candidate on', target);
          this.onCandidate?.({
            node: target,
            detectorName: this.name,
            confidence: this.baseConfidence,
            detectedAt: now,
          });
        }
      }
    }
  }

  private getTextTarget(m: MutationRecord): Element | null {
    if (m.type === 'characterData') {
      return m.target.parentElement;
    }
    if (m.type === 'childList' && m.addedNodes.length > 0) {
      return m.target as Element;
    }
    return null;
  }

  private isInsideInput(node: Node): boolean {
    let cur: Node | null = node;
    while (cur) {
      if (cur.nodeType === Node.ELEMENT_NODE) {
        const el = cur as Element;
        if (el.matches?.(VELOCITY_PARAMS.EXCLUDE_SELECTOR)) return true;
      }
      cur = cur.parentNode;
    }
    return false;
  }

  private computeCharsAdded(m: MutationRecord): number {
    if (m.type === 'characterData') {
      const newLen = (m.target.textContent || '').length;
      const oldLen = (m.oldValue || '').length;
      return Math.max(0, newLen - oldLen);
    }
    if (m.type === 'childList') {
      let chars = 0;
      m.addedNodes.forEach((n) => {
        chars += (n.textContent || '').length;
      });
      return chars;
    }
    return 0;
  }

  private isStreaming(s: VelocityState): boolean {
    if (s.mutations.length < VELOCITY_PARAMS.MIN_CHUNKS) return false;

    const span = s.lastUpdate - s.startedAt;
    if (span < VELOCITY_PARAMS.MIN_SPAN_MS) return false;

    const rate = s.mutations.length / (span / 1000);
    if (rate < VELOCITY_PARAMS.MIN_RATE) return false;

    const totalChars = s.mutations.reduce((sum, mut) => sum + mut.charsAdded, 0);
    const avgChars = totalChars / s.mutations.length;
    if (avgChars < VELOCITY_PARAMS.MIN_AVG_CHARS) return false;

    return true;
  }
}
