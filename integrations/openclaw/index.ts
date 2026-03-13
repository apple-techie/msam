/**
 * msam-bridge: OpenClaw memory plugin bridging to MSAM
 *
 * Provides all 6 memory tools, auto-capture/recall hooks,
 * circuit breaker, and VoC logging.
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { MsamClient } from "./src/msam-client.js";
import { registerAllTools } from "./src/tools.js";
import { computeVoc, logVocDecision } from "./src/voc.js";

// ── Capture patterns ────────────────────────────────────────────────────────

const MEMORY_TRIGGERS =
  /\b(remember|prefer|decided|always|never|important|my .+ is|email|phone|address|birthday|name is|call me|i like|i hate|i love|i want|i need|don't forget)\b/i;

const CAPTURE_EXCLUDE =
  /\b(forget|delete|remove|clear|wipe|erase|list|show|recall|search|find|what do you know|what have you)\b.*\b(memor|remember)/i;

function shouldCapture(text: string): boolean {
  if (text.length < 10 || text.length > 500) return false;
  if (/<[^>]+>/.test(text)) return false;
  if (CAPTURE_EXCLUDE.test(text)) return false;
  return MEMORY_TRIGGERS.test(text);
}

function detectCategory(
  text: string,
): "preference" | "fact" | "decision" | "entity" | "other" {
  if (/\b(prefer|like|love|hate|want|always|never|favorite|rather)\b/i.test(text))
    return "preference";
  if (/\b(decided|decision|chose|choose|going with|settled on)\b/i.test(text))
    return "decision";
  if (/\b(email|phone|address|name|birthday|company|title|role)\b/i.test(text))
    return "entity";
  if (/\b(is|are|was|were|has|have|does|do)\b/i.test(text)) return "fact";
  return "other";
}

// ── Config ──────────────────────────────────────────────────────────────────

interface BridgeConfig {
  msamUrl: string;
  msamApiKey?: string;
  agentId: string;
  vocEnabled: boolean;
  autoCapture: boolean;
  autoRecall: boolean;
  enableManagementTools: boolean;
}

function parseConfig(raw: Record<string, unknown>): BridgeConfig {
  return {
    msamUrl: (raw.msamUrl as string) || "http://127.0.0.1:3901",
    msamApiKey: (raw.msamApiKey as string) || undefined,
    agentId: (raw.agentId as string) || "default",
    vocEnabled: raw.vocEnabled === true,
    autoCapture: raw.autoCapture !== false,
    autoRecall: raw.autoRecall !== false,
    enableManagementTools: raw.enableManagementTools !== false,
  };
}

// ── Session state ───────────────────────────────────────────────────────────

let recalledAtomIds: Set<string> = new Set();

// ── Plugin ──────────────────────────────────────────────────────────────────

const msamBridgePlugin = {
  id: "msam-bridge",
  name: "Memory (MSAM Bridge)",
  description:
    "Multi-Stream Adaptive Memory bridge for OpenClaw. ACT-R scoring, confidence-gated retrieval, knowledge graph, cognitive decay.",
  kind: "memory" as const,

  register(api: OpenClawPluginApi) {
    const config = parseConfig((api as any).pluginConfig || {});
    const logger = (api as any).logger || console;

    const client = new MsamClient(
      {
        baseUrl: config.msamUrl,
        apiKey: config.msamApiKey,
        timeoutMs: 2000,
      },
      logger,
    );

    // ── Register tools ────────────────────────────────────────────────────
    registerAllTools(api, { client, agentId: config.agentId });

    // ── Hook: before_agent_start ──────────────────────────────────────────
    if (config.autoRecall) {
      api.on("before_agent_start", async (event: any, ctx: any) => {
        const agentId = ctx?.agentId || config.agentId;
        const messages = event?.messages || [];
        const lastUserMsg = [...messages]
          .reverse()
          .find((m: any) => m.role === "user");
        const prompt = lastUserMsg?.content || "";

        if (typeof prompt !== "string" || prompt.length < 15) {
          return {};
        }

        if (client.breaker.isOpen) {
          client.breaker.tryHalfOpen();
        }

        try {
          const [contextResult, queryResult] = await Promise.all([
            client.context(agentId).catch(() => null),
            client.query({ query: prompt, top_k: 5, agent_id: agentId }).catch(() => null),
          ]);

          recalledAtomIds = new Set();
          if (queryResult?.atoms) {
            for (const atom of queryResult.atoms) {
              recalledAtomIds.add(atom.id);
            }
          }

          if (config.vocEnabled) {
            const vocScore = computeVoc(prompt, queryResult);
            logVocDecision(client, prompt, vocScore, agentId, logger).catch(() => {});
          }

          const parts: string[] = [];

          if (contextResult?.context && contextResult.context.trim().length > 0) {
            parts.push(`<msam-context>\n${contextResult.context}\n</msam-context>`);
          }

          if (queryResult?.atoms && queryResult.atoms.length > 0) {
            const memoryText = queryResult.atoms
              .map(
                (a) =>
                  `- ${a.content} (${a.confidence_tier}, ${(a.similarity * 100).toFixed(0)}%)`,
              )
              .join("\n");
            parts.push(
              `<relevant-memories source="msam" confidence="${queryResult.confidence_tier}">\n${memoryText}\n</relevant-memories>`,
            );
          }

          if (parts.length === 0) return {};

          return { prependContext: parts.join("\n\n") };
        } catch (error) {
          logger.warn(
            `[msam-bridge] before_agent_start failed: ${error instanceof Error ? error.message : String(error)}`,
          );
          return {};
        }
      });
    }

    // ── Hook: agent_end ──────────────────────────────────────────────────
    if (config.autoCapture) {
      api.on("agent_end", async (event: any, ctx: any) => {
        const agentId = ctx?.agentId || config.agentId;
        const messages = event?.messages || [];

        if (client.breaker.isOpen) return;

        const userMessages = messages
          .filter((m: any) => m.role === "user")
          .map((m: any) => (typeof m.content === "string" ? m.content : ""))
          .filter((t: string) => shouldCapture(t));

        let stored = 0;
        for (const text of userMessages.slice(-5)) {
          if (stored >= 3) break;

          const category = detectCategory(text);
          try {
            await client.store({
              content: text,
              stream: category === "decision" ? "episodic" : "semantic",
              agent_id: agentId,
              source_type: "auto_capture",
              metadata: { category },
            });
            stored++;
          } catch (err) {
            logger.warn(
              `[msam-bridge] auto-capture failed: ${err instanceof Error ? err.message : String(err)}`,
            );
          }
        }

        if (recalledAtomIds.size > 0) {
          const assistantMsg = [...messages]
            .reverse()
            .find((m: any) => m.role === "assistant");
          const responseText =
            typeof assistantMsg?.content === "string"
              ? assistantMsg.content.slice(0, 500)
              : "";

          if (responseText.length > 0) {
            client
              .feedback({
                atom_ids: Array.from(recalledAtomIds),
                response_text: responseText,
              })
              .catch((err) => {
                logger.warn(`[msam-bridge] feedback submission failed: ${err}`);
              });
          }
          recalledAtomIds = new Set();
        }
      });
    }

    // ── Hook: command:new ────────────────────────────────────────────────
    api.registerHook("command:new", async (event: any) => {
      if (client.breaker.isOpen) return;

      const agentId = config.agentId;
      const timestamp = event?.timestamp || new Date().toISOString();

      try {
        await client.store({
          content: `Session boundary at ${timestamp}. New conversation started.`,
          stream: "episodic",
          agent_id: agentId,
          source_type: "session_boundary",
          metadata: { event: "command:new" },
        });
      } catch (err) {
        logger.warn(
          `[msam-bridge] command:new capture failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    });

    // ── Health service ───────────────────────────────────────────────────
    api.registerService({
      id: "msam-health",
      async start() {
        try {
          const health = await client.health();
          logger.info(
            `[msam-bridge] Connected to MSAM v${health.version} at ${config.msamUrl}`,
          );
        } catch (err) {
          logger.warn(
            `[msam-bridge] MSAM not reachable at startup: ${err instanceof Error ? err.message : String(err)}`,
          );
        }
      },
      async stop() {
        logger.info("[msam-bridge] Shutting down");
      },
    });

    logger.info(
      `[msam-bridge] Registered: agent=${config.agentId}, url=${config.msamUrl}, voc=${config.vocEnabled}`,
    );
  },
};

export default msamBridgePlugin;
export { shouldCapture, detectCategory };
