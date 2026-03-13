# OpenClaw MSAM Bridge Plugin

Bridges OpenClaw's memory plugin system to MSAM (Multi-Stream Adaptive Memory).

## Features

- **6 memory tools**: recall, store, forget, update, list, stats
- **Auto-capture**: Extracts key facts from conversations via agent_end hook
- **Auto-recall**: Injects relevant memories via before_agent_start hook
- **Circuit breaker**: 3 failures in 5min trips to lancedb-pro fallback
- **VoC logging**: Value of Computation scoring (log-only, Phase 1)
- **Dual-write**: Writes to both MSAM and lancedb-pro during transition
- **Session feedback**: Recalled atom IDs fed back via /v1/feedback

## Setup

1. Symlink to OpenClaw plugins:
   ```bash
   ln -s ~/msam/integrations/openclaw ~/.openclaw/workspace/plugins/msam-bridge
   ```

2. Add to `openclaw.json`:
   ```json
   {
     "plugins": {
       "slots": { "memory": "msam-bridge" },
       "entries": {
         "msam-bridge": {
           "enabled": true,
           "config": {
             "msamUrl": "http://127.0.0.1:3901",
             "agentId": "enduru",
             "vocEnabled": false,
             "dualWrite": true
           }
         }
       }
     }
   }
   ```

3. Ensure MSAM is running (`launchctl load ~/Library/LaunchAgents/dev.msam.plist`)

## Migration

```bash
python3 scripts/migrate_from_lancedb.py
```

Reads lancedb-pro JSONL backup, stores atoms via MSAM API, preserves original timestamps.

## Architecture

```
OpenClaw Agent
  -> msam-bridge plugin
    -> MSAM REST API (primary)
    -> lancedb-pro (dual-write fallback)
```
