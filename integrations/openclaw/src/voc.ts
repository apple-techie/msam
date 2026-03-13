/**
 * VoC (Value of Computation) — log-only scoring.
 * Phase 1: Compute and log. NEVER gates the LLM.
 */

import type { MsamClient, QueryResponse } from "./msam-client.js";

export interface VocScore {
  P: number;  // probability memory can handle it (inverted: high confidence = low P)
  G: number;  // goal value (urgency heuristic)
  I: number;  // information gain (retrieval confidence variance)
  tau: number; // time sensitivity
  C: number;  // compute cost estimate
  lambda: number;
  composite: number;
  decision: "memory_sufficient" | "llm_needed" | "uncertain";
}

const URGENT_KEYWORDS = /\b(urgent|critical|emergency|asap|immediately|deadline|breaking|blocker)\b/i;
const ROUTINE_KEYWORDS = /\b(fyi|update|status|check|reminder|note)\b/i;

function estimateGoalValue(query: string): number {
  if (URGENT_KEYWORDS.test(query)) return 1.0;
  if (ROUTINE_KEYWORDS.test(query)) return 0.3;
  return 0.6;
}

function estimateTimeSensitivity(query: string): number {
  if (/\b(now|today|right now|this moment|currently)\b/i.test(query)) return 1.0;
  if (/\b(soon|this week|tomorrow)\b/i.test(query)) return 0.7;
  return 0.5;
}

function computeInfoGain(queryResult: QueryResponse | null): number {
  if (!queryResult || queryResult.atoms.length === 0) return 0.9;
  const sims = queryResult.atoms.map((a) => a.similarity);
  if (sims.length < 2) return 0.5;
  const mean = sims.reduce((a, b) => a + b, 0) / sims.length;
  const variance = sims.reduce((a, b) => a + (b - mean) ** 2, 0) / sims.length;
  return Math.min(1, variance * 10); // scale variance to 0-1
}

function computeP(queryResult: QueryResponse | null): number {
  if (!queryResult || queryResult.confidence_tier === "none") return 0.9;
  if (queryResult.confidence_tier === "high") return 0.2;
  if (queryResult.confidence_tier === "medium") return 0.5;
  if (queryResult.confidence_tier === "low") return 0.7;
  return 0.6;
}

export function computeVoc(
  query: string,
  queryResult: QueryResponse | null,
  estimatedTokens: number = 1000,
): VocScore {
  const lambda = 0.3;
  const P = computeP(queryResult);
  const G = estimateGoalValue(query);
  const I = computeInfoGain(queryResult);
  const tau = estimateTimeSensitivity(query);
  // Cost estimate: rough $/1K tokens for Opus
  const C = (estimatedTokens / 1000) * 0.015;

  const composite = (P * G + lambda * I) * tau - C;

  let decision: VocScore["decision"] = "uncertain";
  if (P < 0.3) decision = "memory_sufficient";
  else if (P > 0.7) decision = "llm_needed";

  return { P, G, I, tau, C, lambda, composite, decision };
}

export async function logVocDecision(
  client: MsamClient,
  query: string,
  vocScore: VocScore,
  agentId: string,
  logger?: { info: (...args: any[]) => void },
): Promise<void> {
  const content = [
    `VoC decision for query: "${query.slice(0, 100)}"`,
    `P=${vocScore.P.toFixed(2)} G=${vocScore.G.toFixed(2)} I=${vocScore.I.toFixed(2)}`,
    `tau=${vocScore.tau.toFixed(2)} C=${vocScore.C.toFixed(4)} lambda=${vocScore.lambda}`,
    `composite=${vocScore.composite.toFixed(4)} decision=${vocScore.decision}`,
  ].join(" | ");

  logger?.info(`[msam-bridge] VoC: ${content}`);

  try {
    await client.store({
      content,
      stream: "working",
      source_type: "voc_decision",
      agent_id: agentId,
      metadata: { voc: vocScore },
    });
  } catch {
    // VoC logging is best-effort
  }
}
