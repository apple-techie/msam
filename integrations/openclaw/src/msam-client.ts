/**
 * MSAM HTTP Client with Circuit Breaker
 */

// ── Types ────────────────────────────────────────────────────────────────────

export interface MsamClientConfig {
  baseUrl: string;
  apiKey?: string;
  timeoutMs?: number;
}

export interface StoreRequest {
  content: string;
  stream?: string;
  profile?: string;
  use_llm_annotate?: boolean;
  source_type?: string;
  metadata?: Record<string, unknown>;
  agent_id?: string;
  embedding?: number[];
}

export interface StoreResponse {
  stored: boolean;
  atom_id: string;
  stream: string;
  profile: string;
  annotations: {
    arousal: number;
    valence: number;
    topics: string[];
    encoding_confidence: number;
  };
  triples_extracted: number;
}

export interface QueryRequest {
  query: string;
  mode?: string;
  top_k?: number;
  token_budget?: number;
  agent_id?: string;
  stream?: string;
}

export interface QueryAtom {
  id: string;
  content: string;
  stream: string;
  similarity: number;
  score: number;
  confidence_tier: string;
  topics: string[];
}

export interface QueryResponse {
  query: string;
  mode: string;
  confidence_tier: string;
  triples: unknown[];
  atoms: QueryAtom[];
  total_tokens: number;
  items_returned: number;
  query_type: string;
  latency_ms: number;
  gated: boolean;
  gated_reason: string;
}

export interface ContextResponse {
  context: string;
  atoms_used: number;
  total_tokens: number;
  compression_ratio?: number;
}

export interface FeedbackRequest {
  atom_ids: string[];
  response_text: string;
  feedback?: string;
}

export interface TombstoneResponse {
  success: boolean;
  atom_id?: string;
  previous_state?: string;
  reason?: string;
}

export interface StatsResponse {
  total_atoms: number;
  active_atoms: number;
  by_stream: Record<string, number>;
  by_profile: Record<string, number>;
  total_accesses: number;
  avg_activation: number;
  est_active_tokens: number;
  db_size_kb: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  timestamp: number;
}

// ── Circuit Breaker ──────────────────────────────────────────────────────────

type BreakerState = "closed" | "open" | "half-open";

interface BreakerStats {
  state: BreakerState;
  failures: number[];
  lastTrip: number;
}

const BREAKER_WINDOW_MS = 5 * 60 * 1000; // 5 minutes
const BREAKER_THRESHOLD = 3;

export class CircuitBreaker {
  private stats: BreakerStats = {
    state: "closed",
    failures: [],
    lastTrip: 0,
  };

  private logger?: { info: (...args: any[]) => void; warn: (...args: any[]) => void };

  constructor(logger?: any) {
    this.logger = logger;
  }

  get state(): BreakerState {
    return this.stats.state;
  }

  get isOpen(): boolean {
    return this.stats.state === "open";
  }

  recordFailure(): void {
    const now = Date.now();
    this.stats.failures.push(now);
    // Prune old failures outside window
    this.stats.failures = this.stats.failures.filter(
      (t) => now - t < BREAKER_WINDOW_MS,
    );

    if (this.stats.failures.length >= BREAKER_THRESHOLD) {
      this.stats.state = "open";
      this.stats.lastTrip = now;
      this.logger?.warn(
        `[msam-bridge] Circuit breaker TRIPPED after ${BREAKER_THRESHOLD} failures in ${BREAKER_WINDOW_MS / 1000}s`,
      );
    }
  }

  recordSuccess(): void {
    if (this.stats.state === "half-open") {
      this.stats.state = "closed";
      this.stats.failures = [];
      this.logger?.info("[msam-bridge] Circuit breaker CLOSED after successful half-open probe");
    }
  }

  tryHalfOpen(): void {
    if (this.stats.state === "open") {
      this.stats.state = "half-open";
      this.logger?.info("[msam-bridge] Circuit breaker entering HALF-OPEN state");
    }
  }

  reset(): void {
    this.stats = { state: "closed", failures: [], lastTrip: 0 };
  }
}

// ── MSAM Client ──────────────────────────────────────────────────────────────

export class MsamClient {
  private baseUrl: string;
  private apiKey?: string;
  private timeoutMs: number;
  public breaker: CircuitBreaker;

  constructor(config: MsamClientConfig, logger?: any) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.apiKey = config.apiKey;
    this.timeoutMs = config.timeoutMs ?? 2000;
    this.breaker = new CircuitBreaker(logger);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    if (this.breaker.isOpen) {
      throw new Error("MSAM circuit breaker is open");
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (this.apiKey) {
        headers["X-API-Key"] = this.apiKey;
      }

      const res = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`MSAM ${method} ${path} failed: ${res.status} ${text}`);
      }

      const data = (await res.json()) as T;
      this.breaker.recordSuccess();
      return data;
    } catch (err) {
      this.breaker.recordFailure();
      throw err;
    } finally {
      clearTimeout(timeout);
    }
  }

  async health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("GET", "/v1/health");
  }

  async store(req: StoreRequest): Promise<StoreResponse> {
    return this.request<StoreResponse>("POST", "/v1/store", req);
  }

  async query(req: QueryRequest): Promise<QueryResponse> {
    return this.request<QueryResponse>("POST", "/v1/query", req);
  }

  async context(agentId?: string): Promise<ContextResponse> {
    return this.request<ContextResponse>("POST", "/v1/context", {
      top_k: 5,
      agent_id: agentId,
    });
  }

  async feedback(req: FeedbackRequest): Promise<unknown> {
    return this.request<unknown>("POST", "/v1/feedback", req);
  }

  async tombstone(atomId: string): Promise<TombstoneResponse> {
    return this.request<TombstoneResponse>("POST", "/v1/tombstone", {
      atom_id: atomId,
    });
  }

  async stats(): Promise<StatsResponse> {
    return this.request<StatsResponse>("GET", "/v1/stats");
  }

  async decay(): Promise<unknown> {
    return this.request<unknown>("POST", "/v1/decay", {});
  }
}
