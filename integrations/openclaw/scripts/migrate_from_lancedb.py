#!/usr/bin/env python3
"""Migrate lancedb-pro memories to MSAM."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error

MSAM_URL = "http://127.0.0.1:3901"
BACKUP_FILE = Path.home() / ".openclaw/memory/backups/memory-backup-2026-03-12.jsonl"
# Double-nested path (known quirk)
DB_PATH = Path.home() / ".msam/~/.msam/msam.db"

CATEGORY_TO_STREAM = {
    "preference": "semantic",
    "fact": "semantic",
    "decision": "episodic",
    "entity": "semantic",
    "other": "semantic",
}

SCOPE_TO_AGENT = {
    "agent:enduru": "enduru",
    "agent:enduru-group": "enduru-group",
    "agent:enduru-kainotomic": "enduru-kainotomic",
    "agent:enduru-botchat": "enduru-botchat",
    "global": "enduru",
}


def msam_store(content: str, stream: str, agent_id: str, category: str, importance: float) -> dict:
    payload = json.dumps({
        "content": content,
        "stream": stream,
        "agent_id": agent_id,
        "source_type": "migration",
        "metadata": {"category": category, "importance": importance, "migrated_from": "lancedb-pro"},
    }).encode()

    req = urllib.request.Request(
        f"{MSAM_URL}/v1/store",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    if not BACKUP_FILE.exists():
        print(f"ERROR: {BACKUP_FILE} not found")
        sys.exit(1)

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)

    # Read backup
    memories = []
    with open(BACKUP_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                memories.append(json.loads(line))

    print(f"Found {len(memories)} memories to migrate")

    # Store each memory via API, collect atom_id -> original_timestamp
    migrations = []
    skipped = 0
    errors = 0

    for i, mem in enumerate(memories):
        text = mem["text"]
        category = mem.get("category", "other")
        scope = mem.get("scope", "global")
        importance = mem.get("importance", 0.7)
        timestamp_ms = mem.get("timestamp", 0)

        agent_id = SCOPE_TO_AGENT.get(scope, "enduru")
        stream = CATEGORY_TO_STREAM.get(category, "semantic")

        # Convert ms timestamp to ISO
        if timestamp_ms > 0:
            original_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        else:
            original_dt = None

        try:
            result = msam_store(text, stream, agent_id, category, importance)
            atom_id = result["atom_id"]
            migrations.append({
                "atom_id": atom_id,
                "original_ts": original_dt.isoformat() if original_dt else None,
                "lancedb_id": mem["id"],
            })
            print(f"  [{i+1}/{len(memories)}] OK {atom_id[:8]} <- {mem['id'][:8]} ({category})")
        except Exception as e:
            print(f"  [{i+1}/{len(memories)}] ERROR: {e}")
            errors += 1

    print(f"\nStored: {len(migrations)}, Errors: {errors}, Skipped: {skipped}")

    # Now fix timestamps via raw SQL
    if migrations:
        print("\nFixing timestamps via raw SQL...")
        conn = sqlite3.connect(str(DB_PATH))
        fixed = 0
        for m in migrations:
            if m["original_ts"]:
                try:
                    conn.execute(
                        "UPDATE atoms SET created_at = ? WHERE id = ?",
                        (m["original_ts"], m["atom_id"]),
                    )
                    fixed += 1
                except Exception as e:
                    print(f"  WARN: Could not fix timestamp for {m['atom_id']}: {e}")
        conn.commit()
        conn.close()
        print(f"Fixed {fixed} timestamps")

    # Write migration log
    log_path = Path.home() / ".msam/migration_log.json"
    with open(log_path, "w") as f:
        json.dump({
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "source": str(BACKUP_FILE),
            "total": len(memories),
            "stored": len(migrations),
            "errors": errors,
            "mappings": migrations,
        }, f, indent=2)
    print(f"\nMigration log: {log_path}")
    print("Done!")


if __name__ == "__main__":
    main()
