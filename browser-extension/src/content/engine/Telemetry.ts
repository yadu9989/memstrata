// Hard Rule 65: every detection failure is observable — blackouts are reported, never silent.

export class Telemetry {
  private hostStartedAt: number;
  private mutationsObserved = 0;
  private layersFired: Record<string, number> = {};
  private readonly intervalId: ReturnType<typeof setInterval>;

  constructor(private readonly provider: string) {
    this.hostStartedAt = Date.now();
    // Check every 60s; fires after 5 min of zero-detection with user activity present
    this.intervalId = setInterval(() => { void this.checkBlackout(); }, 60_000);
  }

  recordMutation(): void {
    this.mutationsObserved++;
  }

  recordLayerFire(layer: string): void {
    this.layersFired[layer] = (this.layersFired[layer] ?? 0) + 1;
  }

  recordBlackout(blackout: Record<string, unknown>): void {
    void fetch('http://localhost:8000/telemetry/blackout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(blackout),
    }).catch(() => {});
  }

  recordError(kind: string, err: unknown): void {
    void fetch('http://localhost:8000/telemetry/error', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind,
        message: err instanceof Error ? err.message : String(err),
        host: location.hostname,
        provider: this.provider,
        ext_version: chrome.runtime.getManifest().version,
      }),
    }).catch(() => {});
  }

  dispose(): void {
    clearInterval(this.intervalId);
  }

  private async checkBlackout(): Promise<void> {
    const duration = (Date.now() - this.hostStartedAt) / 1000;
    if (duration < 300) return;                                         // < 5 min on host
    if (Object.values(this.layersFired).reduce((a, b) => a + b, 0) > 0) return; // detection working
    if (this.mutationsObserved < 50) return;                            // no user activity

    await fetch('http://localhost:8000/telemetry/blackout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: location.hostname,
        provider: this.provider,
        duration_seconds: Math.round(duration),
        observed_mutations: this.mutationsObserved,
        layers_fired: this.layersFired,
        user_agent: navigator.userAgent,
        ext_version: chrome.runtime.getManifest().version,
      }),
    }).catch(() => {});

    // Prevent re-reporting for this session
    this.hostStartedAt = Number.MAX_SAFE_INTEGER;
  }
}
