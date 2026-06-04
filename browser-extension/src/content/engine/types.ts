export interface Candidate {
  node: Element;
  detectorName: 'aria_live' | 'semantic_attr' | 'velocity' | 'structural';
  confidence: number;
  detectedAt: number;
}

export interface Detector {
  name: string;
  baseConfidence: number;
  initialScan(root: Element): Candidate[];
  start(root: Element, onCandidate: (c: Candidate) => void): void;
  stop(): void;
}

export interface StreamMeta {
  durationMs: number;
  chunkCount: number;
  finalCharCount: number;
  detectedBy: string[];
  confidence: number;
}

export interface ProviderHints {
  provider_id: string;
  debounce_ms?: number;
  init_delay_ms?: number;
  path_filter?: string;
  preferred_detectors?: string[];
  exclude_selectors?: string[];
  shadow_hosts?: string[];
  url_session_pattern?: string;
}

// Shared selector used by VelocityDetector and SemanticAttrDetector to resolve
// a leaf mutation target up to the nearest logical assistant-turn container.
// The order here matters only for readability; closest() returns the nearest
// ancestor so the most-specific (innermost) container always wins.
export const CONTAINER_NORMALIZE_SELECTOR = [
  '[data-testid="assistant-message"]',     // Grok, Meta AI
  '[data-message-author-role="assistant"]', // ChatGPT
  '[data-renderer="lm"]',                  // Perplexity
  '[class*="assistant-message" i]',        // generic
  '[class*="message-bubble" i]',           // Grok
  '[class*="assistant" i]',               // generic
  '[class*="bot" i]',                     // generic
  '[class*="message" i]',                 // broad fallback
  '[role="article"]',                      // ChatGPT semantic
  '[aria-live]',                           // ARIA streaming containers
].join(',');

export function normalizeContainer(node: Element): Element {
  return node.closest(CONTAINER_NORMALIZE_SELECTOR) ?? node;
}
