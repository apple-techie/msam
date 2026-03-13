/**
 * OpenClaw tool definitions for MSAM bridge.
 * Maps memory_recall/store/forget/update/list/stats to MSAM REST API.
 */

import { Type } from "@sinclair/typebox";
import { stringEnum } from "openclaw/plugin-sdk";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import type { MsamClient, QueryAtom } from "./msam-client.js";

const MEMORY_CATEGORIES = [
  "preference",
  "fact",
  "decision",
  "entity",
  "other",
] as const;

// Map OpenClaw categories to MSAM streams
const CATEGORY_TO_STREAM: Record<string, string> = {
  preference: "semantic",
  fact: "semantic",
  decision: "episodic",
  entity: "semantic",
  other: "semantic",
};

export interface ToolDeps {
  client: MsamClient;
  agentId: string;
  fallbackStore?: (text: string, category: string) => Promise<{ id: string } | null>;
  fallbackRecall?: (query: string, limit: number) => Promise<any[]>;
  dualWrite: boolean;
}

function resolveAgentId(runtimeAgentId: unknown, fallback: string): string {
  if (typeof runtimeAgentId === "string" && runtimeAgentId.trim().length > 0)
    return runtimeAgentId;
  return fallback;
}

function formatAtom(atom: QueryAtom, idx: number): string {
  return `${idx + 1}. [${atom.id}] [${atom.stream}] ${atom.content} (${(atom.similarity * 100).toFixed(0)}% sim, tier: ${atom.confidence_tier})`;
}

export function registerAllTools(
  api: OpenClawPluginApi,
  deps: ToolDeps,
): void {
  // ── memory_recall ──────────────────────────────────────────────────────
  api.registerTool(
    (toolCtx) => {
      const agentId = resolveAgentId((toolCtx as any)?.agentId, deps.agentId);
      return {
        name: "memory_recall",
        label: "Memory Recall",
        description:
          "Search long-term memories using MSAM's hybrid retrieval (embedding + keyword + knowledge graph). Returns confidence-gated results with ACT-R activation scoring.",
        parameters: Type.Object({
          query: Type.String({ description: "Search query" }),
          limit: Type.Optional(
            Type.Number({ description: "Max results (default: 5, max: 20)" }),
          ),
          scope: Type.Optional(Type.String({ description: "Agent scope filter" })),
          category: Type.Optional(stringEnum(MEMORY_CATEGORIES)),
        }),
        async execute(_toolCallId, params) {
          const { query, limit = 5, scope, category } = params as {
            query: string;
            limit?: number;
            scope?: string;
            category?: string;
          };

          try {
            const result = await deps.client.query({
              query,
              top_k: Math.min(Math.max(1, limit), 20),
              agent_id: agentId,
              stream: category ? CATEGORY_TO_STREAM[category] : undefined,
            });

            if (result.atoms.length === 0) {
              return {
                content: [{ type: "text", text: "No relevant memories found." }],
                details: { count: 0, query, confidence_tier: result.confidence_tier },
              };
            }

            const text = result.atoms.map(formatAtom).join("\n");
            return {
              content: [
                {
                  type: "text",
                  text: `Found ${result.atoms.length} memories (${result.confidence_tier} confidence):\n\n${text}`,
                },
              ],
              details: {
                count: result.atoms.length,
                confidence_tier: result.confidence_tier,
                memories: result.atoms.map((a) => ({
                  id: a.id,
                  text: a.content,
                  score: a.score,
                  similarity: a.similarity,
                  stream: a.stream,
                })),
                triples: result.triples,
                latency_ms: result.latency_ms,
              },
            };
          } catch (error) {
            // Fallback to lancedb-pro
            if (deps.fallbackRecall) {
              try {
                const fallbackResults = await deps.fallbackRecall(query, limit);
                return {
                  content: [
                    {
                      type: "text",
                      text: `[fallback] Found ${fallbackResults.length} memories via lancedb-pro`,
                    },
                  ],
                  details: { source: "lancedb-pro-fallback", count: fallbackResults.length },
                };
              } catch {
                // Both failed
              }
            }
            return {
              content: [
                {
                  type: "text",
                  text: `Memory recall failed: ${error instanceof Error ? error.message : String(error)}`,
                },
              ],
              details: { error: "recall_failed" },
            };
          }
        },
      };
    },
    { name: "memory_recall" },
  );

  // ── memory_store ───────────────────────────────────────────────────────
  api.registerTool(
    (toolCtx) => {
      const agentId = resolveAgentId((toolCtx as any)?.agentId, deps.agentId);
      return {
        name: "memory_store",
        label: "Memory Store",
        description:
          "Save information to MSAM long-term memory. Auto-annotates arousal, valence, and topics.",
        parameters: Type.Object({
          text: Type.String({ description: "Information to remember" }),
          importance: Type.Optional(
            Type.Number({ description: "Importance score 0-1 (default: 0.7)" }),
          ),
          category: Type.Optional(stringEnum(MEMORY_CATEGORIES)),
          scope: Type.Optional(Type.String({ description: "Memory scope" })),
        }),
        async execute(_toolCallId, params) {
          const { text, importance = 0.7, category = "other", scope } = params as {
            text: string;
            importance?: number;
            category?: string;
            scope?: string;
          };

          try {
            const result = await deps.client.store({
              content: text,
              stream: CATEGORY_TO_STREAM[category] || "semantic",
              agent_id: agentId,
              source_type: "tool",
              metadata: { importance, category, scope },
            });

            // Dual-write to lancedb-pro (best-effort)
            if (deps.dualWrite && deps.fallbackStore) {
              deps.fallbackStore(text, category).catch(() => {});
            }

            return {
              content: [
                {
                  type: "text",
                  text: `Stored: "${text.slice(0, 100)}${text.length > 100 ? "..." : ""}" (atom: ${result.atom_id}, stream: ${result.stream})`,
                },
              ],
              details: {
                action: "created",
                id: result.atom_id,
                stream: result.stream,
                profile: result.profile,
                annotations: result.annotations,
                triples_extracted: result.triples_extracted,
              },
            };
          } catch (error) {
            // Fallback
            if (deps.fallbackStore) {
              try {
                const fb = await deps.fallbackStore(text, category);
                return {
                  content: [
                    { type: "text", text: `[fallback] Stored via lancedb-pro: "${text.slice(0, 80)}..."` },
                  ],
                  details: { source: "lancedb-pro-fallback", id: fb?.id },
                };
              } catch {
                // Both failed
              }
            }
            return {
              content: [
                {
                  type: "text",
                  text: `Memory storage failed: ${error instanceof Error ? error.message : String(error)}`,
                },
              ],
              details: { error: "store_failed" },
            };
          }
        },
      };
    },
    { name: "memory_store" },
  );

  // ── memory_forget ──────────────────────────────────────────────────────
  api.registerTool(
    (toolCtx) => {
      const agentId = resolveAgentId((toolCtx as any)?.agentId, deps.agentId);
      return {
        name: "memory_forget",
        label: "Memory Forget",
        description: "Tombstone a specific memory by ID, or search and confirm.",
        parameters: Type.Object({
          query: Type.Optional(Type.String({ description: "Search query to find memory" })),
          memoryId: Type.Optional(Type.String({ description: "Specific atom ID to tombstone" })),
          scope: Type.Optional(Type.String({ description: "Scope filter" })),
        }),
        async execute(_toolCallId, params) {
          const { query, memoryId } = params as {
            query?: string;
            memoryId?: string;
            scope?: string;
          };

          try {
            if (memoryId) {
              const result = await deps.client.tombstone(memoryId);
              if (result.success) {
                return {
                  content: [{ type: "text", text: `Memory ${memoryId} tombstoned.` }],
                  details: { action: "tombstoned", id: memoryId, previous_state: result.previous_state },
                };
              }
              return {
                content: [{ type: "text", text: `Could not tombstone: ${result.reason}` }],
                details: { error: result.reason },
              };
            }

            if (query) {
              const results = await deps.client.query({
                query,
                top_k: 5,
                agent_id: agentId,
              });

              if (results.atoms.length === 0) {
                return {
                  content: [{ type: "text", text: "No matching memories found." }],
                  details: { count: 0 },
                };
              }

              if (results.atoms.length === 1 && results.atoms[0].similarity > 0.8) {
                const atom = results.atoms[0];
                const tombResult = await deps.client.tombstone(atom.id);
                return {
                  content: [
                    { type: "text", text: `Forgotten: "${atom.content.slice(0, 80)}"` },
                  ],
                  details: { action: "tombstoned", id: atom.id },
                };
              }

              const list = results.atoms
                .map((a) => `- [${a.id.slice(0, 8)}] ${a.content.slice(0, 60)}${a.content.length > 60 ? "..." : ""}`)
                .join("\n");
              return {
                content: [
                  {
                    type: "text",
                    text: `Found ${results.atoms.length} candidates. Specify memoryId to delete:\n${list}`,
                  },
                ],
                details: { action: "candidates", count: results.atoms.length },
              };
            }

            return {
              content: [{ type: "text", text: "Provide either 'query' or 'memoryId'." }],
              details: { error: "missing_param" },
            };
          } catch (error) {
            return {
              content: [
                {
                  type: "text",
                  text: `Memory forget failed: ${error instanceof Error ? error.message : String(error)}`,
                },
              ],
              details: { error: "forget_failed" },
            };
          }
        },
      };
    },
    { name: "memory_forget" },
  );

  // ── memory_update ──────────────────────────────────────────────────────
  api.registerTool(
    (toolCtx) => {
      const agentId = resolveAgentId((toolCtx as any)?.agentId, deps.agentId);
      return {
        name: "memory_update",
        label: "Memory Update",
        description:
          "Update a memory: tombstone the old atom and store a new one with updated content.",
        parameters: Type.Object({
          memoryId: Type.String({ description: "Atom ID to update" }),
          text: Type.Optional(Type.String({ description: "New text content" })),
          importance: Type.Optional(Type.Number({ description: "New importance 0-1" })),
          category: Type.Optional(stringEnum(MEMORY_CATEGORIES)),
        }),
        async execute(_toolCallId, params) {
          const { memoryId, text, importance, category } = params as {
            memoryId: string;
            text?: string;
            importance?: number;
            category?: string;
          };

          if (!text && importance === undefined && !category) {
            return {
              content: [
                { type: "text", text: "Nothing to update. Provide at least one of: text, importance, category." },
              ],
              details: { error: "no_updates" },
            };
          }

          try {
            // Tombstone old atom
            await deps.client.tombstone(memoryId);

            // Store new atom with updated content
            const newContent = text || "(updated metadata only)";
            const result = await deps.client.store({
              content: newContent,
              stream: category ? CATEGORY_TO_STREAM[category] || "semantic" : "semantic",
              agent_id: agentId,
              source_type: "tool_update",
              metadata: { importance, category, replaces: memoryId },
            });

            return {
              content: [
                {
                  type: "text",
                  text: `Updated: tombstoned ${memoryId.slice(0, 8)}..., created ${result.atom_id}`,
                },
              ],
              details: {
                action: "updated",
                old_id: memoryId,
                new_id: result.atom_id,
              },
            };
          } catch (error) {
            return {
              content: [
                {
                  type: "text",
                  text: `Memory update failed: ${error instanceof Error ? error.message : String(error)}`,
                },
              ],
              details: { error: "update_failed" },
            };
          }
        },
      };
    },
    { name: "memory_update" },
  );

  // ── memory_stats ───────────────────────────────────────────────────────
  api.registerTool(
    () => ({
      name: "memory_stats",
      label: "Memory Statistics",
      description: "Get MSAM memory statistics: atom counts, streams, activation scores.",
      parameters: Type.Object({}),
      async execute() {
        try {
          const stats = await deps.client.stats();
          const lines = [
            `Memory Statistics (MSAM):`,
            `  Total atoms: ${stats.total_atoms}`,
            `  Active atoms: ${stats.active_atoms}`,
            `  Avg activation: ${stats.avg_activation.toFixed(2)}`,
            `  Est active tokens: ${stats.est_active_tokens}`,
            `  DB size: ${stats.db_size_kb.toFixed(0)} KB`,
            `  Circuit breaker: ${deps.client.breaker.state}`,
            ``,
            `By stream:`,
            ...Object.entries(stats.by_stream).map(([s, c]) => `  ${s}: ${c}`),
            ``,
            `By profile:`,
            ...Object.entries(stats.by_profile).map(([p, c]) => `  ${p}: ${c}`),
          ];

          return {
            content: [{ type: "text", text: lines.join("\n") }],
            details: { stats, breaker_state: deps.client.breaker.state },
          };
        } catch (error) {
          return {
            content: [
              {
                type: "text",
                text: `Stats failed: ${error instanceof Error ? error.message : String(error)}`,
              },
            ],
            details: { error: "stats_failed" },
          };
        }
      },
    }),
    { name: "memory_stats" },
  );

  // ── memory_list ────────────────────────────────────────────────────────
  api.registerTool(
    (toolCtx) => {
      const agentId = resolveAgentId((toolCtx as any)?.agentId, deps.agentId);
      return {
        name: "memory_list",
        label: "Memory List",
        description: "List recent memories by querying MSAM with a broad query.",
        parameters: Type.Object({
          limit: Type.Optional(Type.Number({ description: "Max to list (default: 10)" })),
          scope: Type.Optional(Type.String({ description: "Scope filter" })),
          category: Type.Optional(stringEnum(MEMORY_CATEGORIES)),
        }),
        async execute(_toolCallId, params) {
          const { limit = 10, category } = params as {
            limit?: number;
            scope?: string;
            category?: string;
          };

          try {
            // MSAM doesn't have a list endpoint, so use a broad query
            const result = await deps.client.query({
              query: "*",
              top_k: Math.min(Math.max(1, limit), 50),
              agent_id: agentId,
              stream: category ? CATEGORY_TO_STREAM[category] : undefined,
            });

            if (result.atoms.length === 0) {
              return {
                content: [{ type: "text", text: "No memories found." }],
                details: { count: 0 },
              };
            }

            const text = result.atoms.map(formatAtom).join("\n");
            return {
              content: [
                {
                  type: "text",
                  text: `Recent memories (${result.atoms.length}):\n\n${text}`,
                },
              ],
              details: {
                count: result.atoms.length,
                memories: result.atoms.map((a) => ({
                  id: a.id,
                  text: a.content,
                  stream: a.stream,
                  similarity: a.similarity,
                })),
              },
            };
          } catch (error) {
            return {
              content: [
                {
                  type: "text",
                  text: `Memory list failed: ${error instanceof Error ? error.message : String(error)}`,
                },
              ],
              details: { error: "list_failed" },
            };
          }
        },
      };
    },
    { name: "memory_list" },
  );
}
