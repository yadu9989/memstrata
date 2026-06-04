// ShadowPiercer — attaches throttled, passive MutationObservers to named shadow hosts
// and routes mutation records into the VelocityDetector pipeline.
//
// Design constraints enforced throughout:
// - attributes:false on the rootObserver; shadow-root observers use attributeFilter
//   (targeted, never all-attributes) — prevents the mutation-loop risk from REVIEW §6 §2.
// - Read-only: never writes to any DOM element.
// - observedRoots (Set) prevents double-attachment on the same shadow root.
// - pollingStarted (WeakSet) prevents concurrent polling queues for the same host.
// - disposed flag: every entry point checks it so no work runs after dispose().

const MAX_RETRIES = 40;
const POLL_INTERVAL_MS = 50;               // 40 × 50 ms = 2 s max wait
const THROTTLE_MS = 50;                    // batch shadow mutations every 50 ms
const ATTRIBUTE_FILTER = ['data-is-typing', 'aria-busy', 'data-activity'];

function isDebug(): boolean {
  try { return localStorage.getItem('ML_DEBUG') === 'true'; } catch { return false; }
}

export class ShadowPiercer {
  private disposed = false;
  private rootObserver: MutationObserver | null = null;
  private readonly shadowObservers: MutationObserver[] = [];
  private readonly observedRoots = new Set<ShadowRoot>();
  private readonly pollingStarted = new WeakSet<Element>();
  private readonly pendingTimers = new Set<ReturnType<typeof setTimeout>>();

  /**
   * @param hostTags    Lower-case custom-element tag names to pierce.
   * @param onMutations Callback that receives batched shadow-root mutation records.
   *                    Typically: `(m) => velocityDetector.processMutations(m)`.
   * @param throttleMs  How long to batch shadow mutations before firing onMutations.
   *                    Defaults to 50ms in production; pass 0 in unit tests.
   */
  constructor(
    private readonly hostTags: string[],
    private readonly onMutations: (mutations: MutationRecord[]) => void,
    private readonly throttleMs = THROTTLE_MS,
  ) {}

  /** Begin watching `root` for shadow hosts — scans existing DOM then watches additions. */
  start(root: Element): void {
    if (this.hostTags.length === 0) return;

    // 1. Pierce any shadow hosts already present in the DOM.
    for (const tag of this.hostTags) {
      root.querySelectorAll(tag).forEach((el) => this.tryPierce(el));
    }

    // 2. Watch for shadow hosts added dynamically to the light DOM.
    this.rootObserver = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type !== 'childList') continue;
        m.addedNodes.forEach((n) => {
          if (n.nodeType !== Node.ELEMENT_NODE) return;
          const el = n as Element;
          if (this.isHostTag(el)) this.tryPierce(el);
          for (const tag of this.hostTags) {
            el.querySelectorAll(tag).forEach((child) => this.tryPierce(child));
          }
        });
      }
    });

    this.rootObserver.observe(root, {
      childList: true,
      subtree: true,
      attributes: false,
    });
  }

  /** Disconnect all observers and cancel all pending retry timers. */
  dispose(): void {
    this.disposed = true;
    this.rootObserver?.disconnect();
    this.rootObserver = null;
    for (const obs of this.shadowObservers) obs.disconnect();
    this.shadowObservers.length = 0;
    this.observedRoots.clear();
    for (const t of this.pendingTimers) clearTimeout(t);
    this.pendingTimers.clear();
  }

  private isHostTag(el: Element): boolean {
    return this.hostTags.includes(el.tagName.toLowerCase());
  }

  /**
   * Attempt to attach an observer to `host.shadowRoot`.
   *
   * If `.shadowRoot` is null (async Web Component hydration — e.g. Copilot), schedules
   * a retry every POLL_INTERVAL_MS up to MAX_RETRIES times.  Once the shadow root
   * becomes available it is pierced synchronously on the next poll tick.
   */
  private tryPierce(host: Element, retries = 0): void {
    if (this.disposed) return;

    // Prevent multiple concurrent polling queues for the same host element.
    if (retries === 0 && this.pollingStarted.has(host)) return;
    if (retries === 0) this.pollingStarted.add(host);

    const shadow = host.shadowRoot;

    if (!shadow) {
      if (retries >= MAX_RETRIES) {
        if (isDebug()) {
          console.warn(
            '[ML:shadow] gave up on <%s> — shadowRoot never appeared (%d × %dms)',
            host.tagName.toLowerCase(), MAX_RETRIES, POLL_INTERVAL_MS,
          );
        }
        return;
      }
      if (isDebug() && retries === 0) {
        console.log(
          '[ML:shadow] <%s>.shadowRoot is null — polling (up to %dms)',
          host.tagName.toLowerCase(), MAX_RETRIES * POLL_INTERVAL_MS,
        );
      }
      const timer = setTimeout(() => {
        this.pendingTimers.delete(timer);
        this.tryPierce(host, retries + 1);
      }, POLL_INTERVAL_MS);
      this.pendingTimers.add(timer);
      return;
    }

    if (this.observedRoots.has(shadow)) return;
    this.observedRoots.add(shadow);

    if (isDebug()) {
      console.log(
        '[ML:shadow] pierced <%s> shadow root (retries=%d)',
        host.tagName.toLowerCase(), retries,
      );
    }

    // Build a throttled callback that:
    //   (a) batches high-frequency attribute updates from Bot Frameworks, and
    //   (b) recursively pierces any nested shadow hosts found in added nodes.
    const callback = this.makeThrottledCallback((mutations) => {
      // (a) Route into the VelocityDetector pipeline.
      this.onMutations(mutations);

      // (b) Recursive pierce: mutations from inside a shadow root never bubble to
      // document.body, so the rootObserver is blind to nested host insertions.
      // Only the per-root observer can detect and pierce them.
      for (const m of mutations) {
        if (m.type !== 'childList') continue;
        m.addedNodes.forEach((n) => {
          if (n.nodeType !== Node.ELEMENT_NODE) return;
          const el = n as Element;
          if (this.isHostTag(el)) this.tryPierce(el);
          for (const tag of this.hostTags) {
            el.querySelectorAll(tag).forEach((child) => this.tryPierce(child));
          }
        });
      }
    });

    const obs = new MutationObserver(callback);
    obs.observe(shadow, {
      childList: true,
      subtree: true,
      characterData: true,
      // Targeted attribute watching: captures Copilot's Bot Framework typing signals
      // without the all-attributes mutation-loop risk (REVIEW §6 Cause 2).
      attributes: true,
      attributeFilter: ATTRIBUTE_FILTER,
    });
    this.shadowObservers.push(obs);
  }

  /**
   * Returns a function that batches incoming MutationRecord arrays and fires `fn`
   * once per `throttleMs` window.  The leading call arms the timer; subsequent calls
   * within the window accumulate into `pending`.
   */
  private makeThrottledCallback(
    fn: (mutations: MutationRecord[]) => void,
  ): (mutations: MutationRecord[]) => void {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pending: MutationRecord[] = [];

    return (mutations: MutationRecord[]) => {
      pending.push(...mutations);
      if (timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        const batch = pending.splice(0);
        if (batch.length > 0 && !this.disposed) fn(batch);
      }, this.throttleMs);
    };
  }
}
