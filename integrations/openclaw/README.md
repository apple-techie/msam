# OpenClaw MSAM Bridge Plugin

Bridges OpenClaw's memory plugin system to [MSAM](https://github.com/apple-techie/msam) (Multi-Stream Adaptive Memory).

## Features

- **6 memory tools**: recall, store, forget, update, list, stats
- **Auto-capture**: Extracts key facts from conversations via `agent_end` hook
- **Auto-recall**: Injects relevant memories via `before_agent_start` hook
- **Circuit breaker**: 3 failures in 5-min window trips breaker, auto-recovers via half-open probe
- **VoC logging**: Value of Computation scoring (log-only, never gates the LLM)
- **Session feedback**: Recalled atom IDs fed back via `/v1/feedback` to reinforce useful memories
- **Multi-agent**: Routes memories per-agent via `ctx.agentId` from OpenClaw runtime

## Prerequisites

- MSAM server running (default: `http://127.0.0.1:3901`)
- OpenClaw 2026.3+ with plugin support

## Installation

1. Clone the MSAM repo and symlink the plugin into OpenClaw's plugin directory:

```bash
git clone git@github.com:apple-techie/msam.git ~/msam
ln -s ~/msam/integrations/openclaw ~/.openclaw/workspace/plugins/msam-bridge
```

2. Add to `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "load": {
      "paths": [
        "~/.openclaw/workspace/plugins/msam-bridge"
      ]
    },
    "slots": {
      "memory": "msam-bridge"
    },
    "entries": {
      "msam-bridge": {
        "enabled": true,
        "config": {
          "msamUrl": "http://127.0.0.1:3901",
          "agentId": "default",
          "vocEnabled": false,
          "autoCapture": true,
          "autoRecall": true
        }
      }
    }
  }
}
```

3. Start MSAM and restart the OpenClaw gateway:

```bash
# Start MSAM (macOS launchd)
launchctl load ~/Library/LaunchAgents/dev.msam.plist

# Or run directly
cd ~/msam && python -m msam.server

# Restart OpenClaw
openclaw gateway restart
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `msamUrl` | `http://127.0.0.1:3901` | MSAM server URL |
| `msamApiKey` | - | API key (optional) |
| `agentId` | `default` | Fallback agent ID (runtime `ctx.agentId` takes precedence) |
| `vocEnabled` | `false` | Enable VoC decision logging |
| `autoCapture` | `true` | Auto-store key facts from conversations |
| `autoRecall` | `true` | Auto-inject relevant memories before agent response |
| `enableManagementTools` | `true` | Register forget/update/list/stats tools |

## Multi-Agent Setup

The plugin reads `ctx.agentId` from the OpenClaw runtime context automatically. The `agentId` config value is only used as a fallback when runtime context is unavailable.

With `enable_sharing = true` in MSAM's `msam.toml`, all agents can read each other's memories while writes are scoped to each agent's ID.

## Tools

| Tool | Description |
|------|-------------|
| `memory_recall` | Hybrid retrieval (embedding + keyword + knowledge graph) |
| `memory_store` | Store with auto-annotation (arousal, valence, topics, triples) |
| `memory_forget` | Tombstone by ID or search-and-confirm |
| `memory_update` | Tombstone old + store new (MSAM has no in-place update) |
| `memory_list` | Broad query listing recent memories |
| `memory_stats` | Atom counts, streams, activation scores, circuit breaker state |

## Architecture

```
OpenClaw Agent
  -> msam-bridge plugin (this repo)
    -> MSAM REST API (:3901)
      -> SQLite + embeddings + knowledge graph
      -> ACT-R activation decay (hourly via launchd)
```

## Migration from lancedb-pro

If migrating from the `memory-lancedb-pro` plugin:

```bash
# Export lancedb-pro memories to JSONL first, then:
python3 scripts/migrate_from_lancedb.py
```

The migration script reads lancedb-pro JSONL backups, stores atoms via MSAM API, and preserves original timestamps via raw SQL.
