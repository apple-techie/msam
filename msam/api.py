#!/usr/bin/env python3
"""
MSAM Metrics API -- Lightweight HTTP API for Grafana JSON datasource.
Runs on port 3001 (localhost only).
Grafana connects via simpod-json-datasource plugin.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timezone


from .config import get_config as _get_config
_cfg = _get_config()

from flask import Flask, jsonify, request
from flask_cors import CORS
from .metrics import get_metrics_db, get_retrieval_history, get_system_history, log_system_snapshot
from .core import get_stats

app = Flask(__name__)

# Security: restrict CORS origins and optionally require API key
_api_origins = _cfg("api", "allowed_origins", ["http://127.0.0.1:3000", "http://localhost:3000", "https://mac-studio.tail3c92ee.ts.net:8444"])
_api_key = _cfg("api", "api_key", None)

CORS(app, origins=_api_origins)

@app.before_request
def _check_api_key():
    """Require API key if configured. Skip for health endpoint."""
    if _api_key and request.path != "/":
        provided = request.headers.get("X-API-Key") or request.args.get("api_key")
        if provided != _api_key:
            return jsonify({"error": "unauthorized", "message": "Valid X-API-Key header required"}), 401


@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "msam-metrics"})


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    return jsonify(stats)


@app.route("/api/retrieval")
def api_retrieval():
    limit = request.args.get("limit", 100, type=int)
    rows = get_retrieval_history(limit)
    return jsonify(rows)


@app.route("/api/system")
def api_system():
    limit = request.args.get("limit", 100, type=int)
    rows = get_system_history(limit)
    return jsonify(rows)


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    log_system_snapshot()
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


# ─── Grafana JSON Datasource Protocol ────────────────────────────

@app.route("/grafana/")
def grafana_health():
    return "ok"


@app.route("/grafana/search", methods=["POST"])
def grafana_search():
    return jsonify([
        # Existing retrieval metrics
        "retrieval_tokens",
        "retrieval_latency",
        "retrieval_atoms",
        "retrieval_activation",
        "retrieval_similarity",
        # System metrics
        "system_total_atoms",
        "system_active_atoms",
        "system_total_tokens",
        "system_db_size",
        "system_accesses",
        # Comparison
        "comparison_savings",
        "comparison_savings_startup",
        "comparison_savings_query",
        # Store
        "store_count",
        "store_tokens",
        # Atom states
        "atom_state_active",
        "atom_state_fading",
        "atom_state_dormant",
        # Streams
        "stream_semantic",
        "stream_episodic",
        "stream_procedural",
        # Retrieval modes
        "mode_task_count",
        "mode_companion_count",
        # --- NEW: Access events ---
        "access_events_count",
        "access_events_tokens",
        "access_events_latency",
        "access_events_atoms",
        "access_activation_min",
        "access_activation_max",
        "access_activation_p50",
        "access_activation_p90",
        "access_by_type_retrieve",
        "access_by_type_store",
        "access_by_type_context",
        "access_by_type_query",
        "access_by_type_snapshot",
        "access_by_caller_heartbeat",
        "access_by_caller_session",
        "access_by_caller_conversation",
        "access_by_caller_canary",
        # --- NEW: Token budget ---
        "token_budget_stored_pct",
        "token_budget_retrieved_pct",
        # --- NEW: Canary metrics ---
        "canary_latency",
        "canary_top_score",
        "canary_atoms",
        # --- NEW: Emotional metrics ---
        "emotional_arousal",
        "emotional_valence",
        "emotional_intensity",
        "emotional_warmth",
        # --- NEW: Topic frequency ---
        "topic_frequency",
        # --- NEW: Embedding latency ---
        "embedding_latency",
        "embedding_success_rate",
        # --- NEW: Age distribution ---
        "age_bucket_lt1d",
        "age_bucket_1to3d",
        "age_bucket_3to7d",
        "age_bucket_7to14d",
        "age_bucket_14to30d",
        "age_bucket_gt30d",
        # --- NEW: Continuity ---
        "continuity_overlap",
        # --- NEW: Retrieval miss rate ---
        "retrieval_miss_count",
        # --- NEW: Decay metrics ---
        "decay_tokens_freed",
        "decay_atoms_faded",
        "decay_atoms_compacted",
        "decay_budget_before",
        "decay_budget_after",
        # --- NEW: Triple metrics ---
        "triple_total_count",
        "triple_unique_subjects",
        "triple_unique_predicates",
        "triple_entity_reuse_rate",
        "triple_extraction_count",
        "triple_extraction_latency",
        "triple_extraction_skip_rate",
        "triple_hybrid_total_tokens",
        "triple_hybrid_triple_tokens",
        "triple_hybrid_atom_tokens",
        "triple_hybrid_efficiency",
        "triple_hybrid_latency",
        "triple_hybrid_efficiency_vs_md",
        # --- Feedback & lifecycle ---
        "contribution_rate",
        "pinned_atom_count",
        "working_memory_count",
        "confidence_avg",
        "forgetting_events_count",
    ])


def _parse_time_range(body):
    """Extract ISO time range from Grafana query body."""
    range_obj = body.get("range", {})
    ts_from = range_obj.get("from", "")
    ts_to = range_obj.get("to", "")
    return ts_from, ts_to


def _query_timeseries(table, field, ts_from, ts_to):
    """Query a timeseries from a metrics table with time-range filtering."""
    conn = get_metrics_db()
    
    if ts_from and ts_to:
        rows = conn.execute(
            f"SELECT timestamp, {field} as value FROM {table} "
            f"WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (ts_from, ts_to)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT timestamp, {field} as value FROM {table} ORDER BY timestamp"
        ).fetchall()
    
    conn.close()
    
    datapoints = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
            val = r["value"]
            if val is not None:
                datapoints.append([val, ts])
        except (ValueError, TypeError):
            continue
    
    return datapoints


def _query_access_events_by_filter(filter_col, filter_val, ts_from, ts_to):
    """Query access_events filtered by a column value, returning count=1 per event."""
    conn = get_metrics_db()
    if ts_from and ts_to:
        rows = conn.execute(
            f"SELECT timestamp FROM access_events WHERE {filter_col} = ? "
            f"AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (filter_val, ts_from, ts_to)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT timestamp FROM access_events WHERE {filter_col} = ? ORDER BY timestamp",
            (filter_val,)
        ).fetchall()
    conn.close()
    datapoints = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
            datapoints.append([1, ts])
        except (ValueError, TypeError):
            continue
    return datapoints


def _query_canary_timeseries(field, ts_from, ts_to, query_filter=None):
    """Query canary_metrics timeseries, optionally filtered by query name."""
    conn = get_metrics_db()
    if query_filter:
        cond = "WHERE query = ?"
        params_base = (query_filter,)
    else:
        cond = "WHERE 1=1"
        params_base = ()

    if ts_from and ts_to:
        sql = f"SELECT timestamp, {field} as value FROM canary_metrics {cond} AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
        rows = conn.execute(sql, params_base + (ts_from, ts_to)).fetchall()
    else:
        sql = f"SELECT timestamp, {field} as value FROM canary_metrics {cond} ORDER BY timestamp"
        rows = conn.execute(sql, params_base).fetchall()
    conn.close()

    datapoints = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
            val = r["value"]
            if val is not None:
                datapoints.append([val, ts])
        except (ValueError, TypeError):
            continue
    return datapoints


_last_auto_snapshot = [0]  # mutable for closure

def _maybe_snapshot():
    """Auto-snapshot at most once per 60 seconds when Grafana polls."""
    import time as _time
    now = _time.time()
    if now - _last_auto_snapshot[0] > 60:
        _last_auto_snapshot[0] = now
        try:
            log_system_snapshot()
        except Exception:
            pass


@app.route("/grafana/query", methods=["POST"])
def grafana_query():
    _maybe_snapshot()
    body = request.json or {}
    ts_from, ts_to = _parse_time_range(body)
    results = []
    
    for target in body.get("targets", []):
        metric = target.get("target", "")
        datapoints = []
        
        # ── Retrieval metrics ──────────────────────────────────
        if metric == "retrieval_miss_count":
            datapoints = _query_access_events_by_filter("event_type", "retrieval_miss", ts_from, ts_to)

        elif metric.startswith("retrieval_"):
            field_map = {
                "retrieval_tokens": "tokens_used",
                "retrieval_latency": "latency_ms",
                "retrieval_atoms": "atoms_returned",
                "retrieval_activation": "avg_activation",
                "retrieval_similarity": "avg_similarity",
            }
            field = field_map.get(metric, "tokens_used")
            datapoints = _query_timeseries("retrieval_metrics", field, ts_from, ts_to)
        
        # ── System metrics ─────────────────────────────────────
        elif metric.startswith("system_"):
            field_map = {
                "system_total_atoms": "total_atoms",
                "system_active_atoms": "active_atoms",
                "system_total_tokens": "total_tokens",
                "system_db_size": "db_size_kb",
                "system_accesses": "total_accesses",
            }
            field = field_map.get(metric, "total_atoms")
            datapoints = _query_timeseries("system_metrics", field, ts_from, ts_to)
        
        # ── Comparison metrics ─────────────────────────────────
        elif metric == "comparison_savings":
            datapoints = _query_timeseries("comparison_metrics", "token_savings_pct", ts_from, ts_to)
        
        elif metric in ("comparison_savings_startup", "comparison_savings_query"):
            conn = get_metrics_db()
            if metric == "comparison_savings_startup":
                condition = "query = 'session_startup_context'"
            else:
                condition = "query != 'session_startup_context'"
            
            sql = f"SELECT timestamp, token_savings_pct as value FROM comparison_metrics WHERE {condition}"
            if ts_from and ts_to:
                sql += " AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                rows = conn.execute(sql, (ts_from, ts_to)).fetchall()
            else:
                sql += " ORDER BY timestamp"
                rows = conn.execute(sql).fetchall()
            conn.close()
            
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    if r["value"] is not None:
                        datapoints.append([r["value"], ts])
                except (ValueError, TypeError):
                    continue
        
        # ── Store metrics ──────────────────────────────────────
        elif metric == "store_count":
            conn2 = get_metrics_db()
            sql = "SELECT timestamp, content_tokens as value FROM store_metrics"
            if ts_from and ts_to:
                sql += " WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                rows = conn2.execute(sql, (ts_from, ts_to)).fetchall()
            else:
                sql += " ORDER BY timestamp"
                rows = conn2.execute(sql).fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    datapoints.append([1, ts])
                except (ValueError, TypeError):
                    continue
        
        elif metric == "store_tokens":
            datapoints = _query_timeseries("store_metrics", "content_tokens", ts_from, ts_to)
        
        # ── Atom state metrics ─────────────────────────────────
        elif metric.startswith("atom_state_"):
            state_map = {
                "atom_state_active": "active_atoms",
                "atom_state_fading": "fading_atoms",
                "atom_state_dormant": "dormant_atoms",
            }
            field = state_map.get(metric, "active_atoms")
            datapoints = _query_timeseries("system_metrics", field, ts_from, ts_to)
        
        # ── Stream distribution ────────────────────────────────
        elif metric.startswith("stream_"):
            stream_name = metric.replace("stream_", "")
            conn2 = get_metrics_db()
            sql = "SELECT timestamp, streams_json FROM system_metrics"
            if ts_from and ts_to:
                sql += " WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                rows = conn2.execute(sql, (ts_from, ts_to)).fetchall()
            else:
                sql += " ORDER BY timestamp"
                rows = conn2.execute(sql).fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    streams = json.loads(r["streams_json"]) if r["streams_json"] else {}
                    val = streams.get(stream_name, 0)
                    datapoints.append([val, ts])
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        
        # ── Retrieval mode counts ──────────────────────────────
        elif metric.startswith("mode_"):
            mode_name = metric.replace("mode_", "").replace("_count", "")
            conn2 = get_metrics_db()
            sql = "SELECT timestamp FROM retrieval_metrics WHERE mode = ?"
            if ts_from and ts_to:
                sql += " AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                rows = conn2.execute(sql, (mode_name, ts_from, ts_to)).fetchall()
            else:
                sql += " ORDER BY timestamp"
                rows = conn2.execute(sql, (mode_name,)).fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    datapoints.append([1, ts])
                except (ValueError, TypeError):
                    continue

        # ── NEW: Access event metrics ──────────────────────────
        elif metric == "access_events_count":
            conn2 = get_metrics_db()
            if ts_from and ts_to:
                rows = conn2.execute(
                    "SELECT timestamp FROM access_events WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (ts_from, ts_to)
                ).fetchall()
            else:
                rows = conn2.execute("SELECT timestamp FROM access_events ORDER BY timestamp").fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    datapoints.append([1, ts])
                except (ValueError, TypeError):
                    continue

        elif metric == "access_events_tokens":
            datapoints = _query_timeseries("access_events", "tokens_used", ts_from, ts_to)

        elif metric == "access_events_latency":
            datapoints = _query_timeseries("access_events", "latency_ms", ts_from, ts_to)

        elif metric == "access_events_atoms":
            datapoints = _query_timeseries("access_events", "atoms_accessed", ts_from, ts_to)

        elif metric == "access_activation_min":
            datapoints = _query_timeseries("access_events", "activation_min", ts_from, ts_to)

        elif metric == "access_activation_max":
            datapoints = _query_timeseries("access_events", "activation_max", ts_from, ts_to)

        elif metric == "access_activation_p50":
            datapoints = _query_timeseries("access_events", "activation_p50", ts_from, ts_to)

        elif metric == "access_activation_p90":
            datapoints = _query_timeseries("access_events", "activation_p90", ts_from, ts_to)

        # Access by event_type
        elif metric == "access_by_type_retrieve":
            # Retrievals log as "snapshot" in metrics, not "retrieve"
            datapoints = _query_access_events_by_filter("event_type", "snapshot", ts_from, ts_to)

        elif metric == "access_by_type_store":
            datapoints = _query_access_events_by_filter("event_type", "store", ts_from, ts_to)

        elif metric == "access_by_type_context":
            datapoints = _query_access_events_by_filter("event_type", "context", ts_from, ts_to)

        elif metric == "access_by_type_query":
            datapoints = _query_access_events_by_filter("event_type", "query", ts_from, ts_to)

        elif metric == "access_by_type_snapshot":
            datapoints = _query_access_events_by_filter("event_type", "snapshot", ts_from, ts_to)

        # Access by caller
        elif metric == "access_by_caller_heartbeat":
            # Heartbeat logs as "cron" in metrics, not "heartbeat"
            datapoints = _query_access_events_by_filter("caller", "cron", ts_from, ts_to)

        elif metric == "access_by_caller_session":
            datapoints = _query_access_events_by_filter("caller", "session_startup", ts_from, ts_to)

        elif metric == "access_by_caller_conversation":
            datapoints = _query_access_events_by_filter("caller", "conversation", ts_from, ts_to)

        elif metric == "access_by_caller_canary":
            datapoints = _query_access_events_by_filter("caller", "canary", ts_from, ts_to)

        # ── NEW: Token budget % ────────────────────────────────
        elif metric == "token_budget_stored_pct":
            # Total tokens in DB as % of 40K budget -- database fullness
            conn2 = get_metrics_db()
            if ts_from and ts_to:
                rows = conn2.execute(
                    "SELECT timestamp, total_tokens FROM system_metrics "
                    "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (ts_from, ts_to)
                ).fetchall()
            else:
                rows = conn2.execute(
                    "SELECT timestamp, total_tokens FROM system_metrics ORDER BY timestamp"
                ).fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    _budget = _cfg('storage', 'token_budget_ceiling', 40000)
                    pct = (r["total_tokens"] / _budget) * 100 if r["total_tokens"] else 0
                    datapoints.append([pct, ts])
                except (ValueError, TypeError):
                    continue

        elif metric == "token_budget_retrieved_pct":
            # Tokens actually retrieved per access as % of 40K budget -- context cost
            conn2 = get_metrics_db()
            if ts_from and ts_to:
                rows = conn2.execute(
                    "SELECT timestamp, tokens_used FROM access_events "
                    "WHERE tokens_used > 0 AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (ts_from, ts_to)
                ).fetchall()
            else:
                rows = conn2.execute(
                    "SELECT timestamp, tokens_used FROM access_events "
                    "WHERE tokens_used > 0 ORDER BY timestamp"
                ).fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    _budget = _cfg('storage', 'token_budget_ceiling', 40000)
                    pct = (r["tokens_used"] / _budget) * 100 if r["tokens_used"] else 0
                    datapoints.append([pct, ts])
                except (ValueError, TypeError):
                    continue

        # ── NEW: Canary metrics ────────────────────────────────
        elif metric == "canary_latency":
            # Only identity canary (not startup context canary)
            datapoints = _query_canary_timeseries("latency_ms", ts_from, ts_to, query_filter="agent identity core traits")

        elif metric == "canary_top_score":
            datapoints = _query_canary_timeseries("top_score", ts_from, ts_to, query_filter="agent identity core traits")

        elif metric == "canary_atoms":
            datapoints = _query_canary_timeseries("atoms_returned", ts_from, ts_to, query_filter="agent identity core traits")

        # ── NEW: Decay metrics ─────────────────────────────────
        elif metric.startswith("decay_"):
            field_map = {
                "decay_tokens_freed": "tokens_freed",
                "decay_atoms_faded": "atoms_faded",
                "decay_atoms_compacted": "atoms_compacted",
                "decay_budget_before": "budget_before_pct",
                "decay_budget_after": "budget_after_pct",
            }
            field = field_map.get(metric)
            if field:
                datapoints = _query_timeseries("decay_metrics", field, ts_from, ts_to)

        # ── NEW: Emotional metrics ─────────────────────────────
        elif metric.startswith("emotional_"):
            field_map = {
                "emotional_arousal": "arousal",
                "emotional_valence": "valence",
                "emotional_intensity": "intensity",
                "emotional_warmth": "warmth",
            }
            field = field_map.get(metric)
            if field:
                datapoints = _query_timeseries("emotional_metrics", field, ts_from, ts_to)

        # ── NEW: Topic frequency ───────────────────────────────
        elif metric == "topic_frequency":
            conn2 = get_metrics_db()
            if ts_from and ts_to:
                rows = conn2.execute(
                    "SELECT timestamp, frequency FROM topic_timeseries WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (ts_from, ts_to)
                ).fetchall()
            else:
                rows = conn2.execute("SELECT timestamp, frequency FROM topic_timeseries ORDER BY timestamp").fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    datapoints.append([r["frequency"], ts])
                except (ValueError, TypeError):
                    continue

        # ── NEW: Embedding latency ─────────────────────────────
        elif metric == "embedding_latency":
            datapoints = _query_timeseries("embedding_metrics", "latency_ms", ts_from, ts_to)

        elif metric == "embedding_success_rate":
            conn2 = get_metrics_db()
            if ts_from and ts_to:
                rows = conn2.execute(
                    "SELECT timestamp, success FROM embedding_metrics WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (ts_from, ts_to)
                ).fetchall()
            else:
                rows = conn2.execute("SELECT timestamp, success FROM embedding_metrics ORDER BY timestamp").fetchall()
            conn2.close()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                    datapoints.append([r["success"], ts])
                except (ValueError, TypeError):
                    continue

        # ── NEW: Age distribution ──────────────────────────────
        elif metric.startswith("age_bucket_"):
            field_map = {
                "age_bucket_lt1d": "bucket_lt1d",
                "age_bucket_1to3d": "bucket_1to3d",
                "age_bucket_3to7d": "bucket_3to7d",
                "age_bucket_7to14d": "bucket_7to14d",
                "age_bucket_14to30d": "bucket_14to30d",
                "age_bucket_gt30d": "bucket_gt30d",
            }
            field = field_map.get(metric)
            if field:
                datapoints = _query_timeseries("age_distribution", field, ts_from, ts_to)

        # ── NEW: Continuity score ──────────────────────────────
        elif metric == "continuity_overlap":
            datapoints = _query_timeseries("continuity_metrics", "overlap_score", ts_from, ts_to)

        # ── NEW: Retrieval miss count ──────────────────────────
        elif metric == "retrieval_miss_count":
            datapoints = _query_access_events_by_filter("event_type", "retrieval_miss", ts_from, ts_to)

        # ── NEW: Triple metrics ────────────────────────────────
        elif metric.startswith("triple_total_") or metric.startswith("triple_unique_") or metric == "triple_entity_reuse_rate":
            field_map = {
                "triple_total_count": "total_triples",
                "triple_unique_subjects": "unique_subjects",
                "triple_unique_predicates": "unique_predicates",
                "triple_entity_reuse_rate": "entity_reuse_rate",
            }
            field = field_map.get(metric)
            if field:
                datapoints = _query_timeseries("triple_store_stats", field, ts_from, ts_to)

        elif metric.startswith("triple_extraction_"):
            if metric == "triple_extraction_count":
                conn2 = get_metrics_db()
                sql = "SELECT timestamp, triples_extracted as value FROM triple_extraction_metrics WHERE skipped = 0"
                if ts_from and ts_to:
                    sql += " AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                    rows = conn2.execute(sql, (ts_from, ts_to)).fetchall()
                else:
                    sql += " ORDER BY timestamp"
                    rows = conn2.execute(sql).fetchall()
                conn2.close()
                for r in rows:
                    try:
                        ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                        datapoints.append([r["value"], ts])
                    except (ValueError, TypeError):
                        continue

            elif metric == "triple_extraction_latency":
                datapoints = _query_timeseries("triple_extraction_metrics", "latency_ms", ts_from, ts_to)

            elif metric == "triple_extraction_skip_rate":
                conn2 = get_metrics_db()
                sql = "SELECT timestamp, skipped as value FROM triple_extraction_metrics"
                if ts_from and ts_to:
                    sql += " WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp"
                    rows = conn2.execute(sql, (ts_from, ts_to)).fetchall()
                else:
                    sql += " ORDER BY timestamp"
                    rows = conn2.execute(sql).fetchall()
                conn2.close()
                for r in rows:
                    try:
                        ts = datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
                        datapoints.append([r["value"], ts])
                    except (ValueError, TypeError):
                        continue

        elif metric in ("contribution_rate", "pinned_atom_count", "working_memory_count",
                        "confidence_avg", "forgetting_events_count"):
            # Live-computed metrics from main MSAM database
            try:
                from .config import get_data_dir as _gdd
                msam_db_path = str(_gdd() / _cfg('storage', 'db_path', 'msam.db'))
                msam_conn = sqlite3.connect(msam_db_path)
                msam_conn.row_factory = sqlite3.Row
                now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)

                if metric == "contribution_rate":
                    r = msam_conn.execute("""
                        SELECT COALESCE(
                            CAST(SUM(CASE WHEN contributed=1 THEN 1 ELSE 0 END) AS REAL) /
                            NULLIF(SUM(CASE WHEN contributed IN (0,1) THEN 1 ELSE 0 END), 0),
                            0
                        ) as rate FROM access_log
                        WHERE accessed_at > datetime('now', '-24 hours')
                    """).fetchone()
                    datapoints = [[round(r[0] * 100, 2), now_ts]]

                elif metric == "pinned_atom_count":
                    r = msam_conn.execute(
                        "SELECT COUNT(*) FROM atoms WHERE state='active' AND is_pinned = 1"
                    ).fetchone()
                    datapoints = [[r[0], now_ts]]

                elif metric == "working_memory_count":
                    r = msam_conn.execute(
                        "SELECT COUNT(*) FROM atoms WHERE stream='working' AND state='active'"
                    ).fetchone()
                    datapoints = [[r[0], now_ts]]

                elif metric == "confidence_avg":
                    r = msam_conn.execute(
                        "SELECT AVG(encoding_confidence) FROM atoms WHERE state='active'"
                    ).fetchone()
                    datapoints = [[round(r[0], 4) if r[0] else 0, now_ts]]

                elif metric == "forgetting_events_count":
                    r = msam_conn.execute(
                        "SELECT COUNT(*) FROM forgetting_log WHERE timestamp > datetime('now', '-24 hours')"
                    ).fetchone()
                    datapoints = [[r[0], now_ts]]

                msam_conn.close()
            except Exception:
                datapoints = []

        elif metric.startswith("triple_hybrid_"):
            field_map = {
                "triple_hybrid_total_tokens": "total_tokens",
                "triple_hybrid_triple_tokens": "triple_tokens",
                "triple_hybrid_atom_tokens": "atom_tokens",
                "triple_hybrid_efficiency": "efficiency_vs_atoms_pct",
                "triple_hybrid_efficiency_vs_md": "efficiency_vs_md_pct",
                "triple_hybrid_latency": "latency_ms",
            }
            field = field_map.get(metric)
            if field:
                datapoints = _query_timeseries("triple_hybrid_metrics", field, ts_from, ts_to)

        results.append({"target": metric, "datapoints": datapoints})
    
    return jsonify(results)


@app.route("/api/topic_frequency")
def api_topic_frequency():
    """Topic hit frequency across all retrievals."""
    conn = get_metrics_db()
    rows = conn.execute("SELECT topics_hit FROM retrieval_metrics WHERE topics_hit IS NOT NULL").fetchall()
    conn.close()
    
    freq = {}
    for r in rows:
        try:
            topics = json.loads(r["topics_hit"]) if r["topics_hit"] else []
            for t in topics:
                freq[t] = freq.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    
    sorted_topics = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return jsonify([{"topic": t, "count": c} for t, c in sorted_topics])


# ─── Grafana Annotations (optional) ─────────────────────────────

@app.route("/grafana/annotations", methods=["POST"])
def grafana_annotations():
    """Return store events as annotations on graphs."""
    body = request.json or {}
    ts_from, ts_to = _parse_time_range(body.get("range", body))
    
    conn = get_metrics_db()
    if ts_from and ts_to:
        rows = conn.execute(
            "SELECT timestamp, atom_id, stream, profile FROM store_metrics "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (ts_from, ts_to)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, atom_id, stream, profile FROM store_metrics ORDER BY timestamp"
        ).fetchall()
    conn.close()
    
    annotations = []
    for r in rows:
        try:
            ts = int(datetime.fromisoformat(r["timestamp"]).timestamp() * 1000)
            annotations.append({
                "time": ts,
                "title": f"Stored: {r['stream']}/{r['profile']}",
                "text": f"Atom {r['atom_id'][:8]}",
                "tags": [r["stream"], r["profile"]],
            })
        except (ValueError, TypeError):
            continue
    
    return jsonify(annotations)


# ─── Triple Metrics Endpoints ─────────────────────────────────────

@app.route("/api/agreement_rate")
def api_agreement_rate():
    """Get current agreement rate for sycophancy detection."""
    from .metrics import get_agreement_rate
    agent_id = request.args.get("agent_id", "default")
    window = request.args.get("window", 20, type=int)
    return jsonify(get_agreement_rate(agent_id=agent_id, window=window))


@app.route("/api/triples/stats")
def api_triple_stats():
    """Current triple store statistics."""
    try:
        from .triples import get_triple_stats
        return jsonify(get_triple_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/triples/extraction")
def api_triple_extraction():
    """Triple extraction history for Grafana."""
    limit = request.args.get("limit", 100, type=int)
    conn = get_metrics_db()
    try:
        rows = conn.execute("""
            SELECT timestamp, atom_id, triples_extracted, triples_stored, latency_ms, skipped
            FROM triple_extraction_metrics ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/triples/hybrid")
def api_triple_hybrid():
    """Hybrid retrieval metrics for Grafana."""
    limit = request.args.get("limit", 100, type=int)
    conn = get_metrics_db()
    try:
        rows = conn.execute("""
            SELECT timestamp, query, mode, triples_count, triple_tokens,
                   atoms_count, atom_tokens, total_tokens, latency_ms, efficiency_vs_atoms_pct
            FROM triple_hybrid_metrics ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/triples/store-history")
def api_triple_store_history():
    """Triple store stats over time for Grafana."""
    limit = request.args.get("limit", 100, type=int)
    conn = get_metrics_db()
    try:
        rows = conn.execute("""
            SELECT timestamp, total_triples, unique_subjects, unique_predicates,
                   unique_objects, entity_reuse_rate, avg_subject_length, avg_object_length
            FROM triple_store_stats ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


if __name__ == "__main__":
    _host = _cfg('api', 'host', '127.0.0.1')
    _port = _cfg('api', 'port', 3001)
    app.run(host=_host, port=_port, debug=False)


# ─── Knowledge Graph Visualization ───────────────────────────────

@app.route("/grafana/graph/fields", methods=["POST"])
def grafana_graph_fields():
    """Return node and edge frames for Grafana Node Graph panel."""
    from .config import get_data_dir as _gdd_local
    db_path = str(_gdd_local() / _cfg('storage', 'db_path', 'msam.db'))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    triples = conn.execute(
        "SELECT id, subject, predicate, object, confidence FROM triples WHERE state = 'active'"
    ).fetchall()
    conn.close()

    if not triples:
        return jsonify({"nodes": [], "edges": []})

    # Build unique nodes
    entity_set = set()
    for t in triples:
        entity_set.add(t["subject"])
        entity_set.add(t["object"])

    # Count connections per entity
    entity_connections = {}
    for t in triples:
        entity_connections[t["subject"]] = entity_connections.get(t["subject"], 0) + 1
        entity_connections[t["object"]] = entity_connections.get(t["object"], 0) + 1

    # Assign stable IDs
    entity_list = sorted(entity_set)
    entity_to_id = {e: str(i) for i, e in enumerate(entity_list)}

    nodes = []
    for entity in entity_list:
        nodes.append({
            "id": entity_to_id[entity],
            "title": entity,
            "mainStat": str(entity_connections.get(entity, 0)),
            "arc__connections": entity_connections.get(entity, 0),
        })

    edges = []
    for t in triples:
        edges.append({
            "id": t["id"],
            "source": entity_to_id[t["subject"]],
            "target": entity_to_id[t["object"]],
            "mainStat": t["predicate"],
        })

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/grafana/graph/stats")
def grafana_graph_stats():
    """Quick stats about the knowledge graph."""
    from .config import get_data_dir as _gdd_local
    db_path = str(_gdd_local() / _cfg('storage', 'db_path', 'msam.db'))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM triples WHERE state = 'active'").fetchone()[0]
    subjects = conn.execute("SELECT COUNT(DISTINCT subject) FROM triples WHERE state = 'active'").fetchone()[0]
    objects = conn.execute("SELECT COUNT(DISTINCT object) FROM triples WHERE state = 'active'").fetchone()[0]
    predicates = conn.execute("SELECT COUNT(DISTINCT predicate) FROM triples WHERE state = 'active'").fetchone()[0]

    # Top entities by connection count
    top = conn.execute("""
        SELECT entity, COUNT(*) as cnt FROM (
            SELECT subject as entity FROM triples WHERE state = 'active'
            UNION ALL
            SELECT object as entity FROM triples WHERE state = 'active'
        ) GROUP BY entity ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    conn.close()

    return jsonify({
        "total_triples": total,
        "unique_subjects": subjects,
        "unique_objects": objects,
        "unique_predicates": predicates,
        "unique_entities": subjects + objects,
        "top_entities": [{"entity": r["entity"], "connections": r["cnt"]} for r in top],
    })


@app.route("/graph")
def knowledge_graph_viewer():
    """Interactive knowledge graph visualization using D3.js force-directed layout."""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>MSAM Knowledge Graph</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0b0e17; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; overflow: hidden; }
  #controls { position: fixed; top: 12px; left: 12px; z-index: 10; display: flex; gap: 8px; align-items: center; }
  #controls input { background: #1a1e2e; border: 1px solid #333; color: #e0e0e0; padding: 6px 10px; border-radius: 6px; font-size: 13px; width: 200px; }
  #controls .stat { background: #1a1e2e; border: 1px solid #333; padding: 6px 12px; border-radius: 6px; font-size: 12px; }
  #controls .stat b { color: #7eb8f7; }
  #tooltip { position: fixed; background: #1a1e2e; border: 1px solid #555; padding: 8px 12px; border-radius: 6px; font-size: 12px; pointer-events: none; display: none; z-index: 100; max-width: 300px; }
  svg { width: 100vw; height: 100vh; }
  .link { stroke-opacity: 0.4; }
  .link:hover { stroke-opacity: 1; }
  .node circle { stroke: #0b0e17; stroke-width: 1.5px; cursor: pointer; }
  .node text { font-size: 10px; fill: #ccc; pointer-events: none; }
  .label-bg { fill: #0b0e17; opacity: 0.7; }
</style>
</head>
<body>
<div id="controls">
  <input type="text" id="search" placeholder="Search entities...">
  <div class="stat">Nodes: <b id="nodeCount">0</b></div>
  <div class="stat">Edges: <b id="edgeCount">0</b></div>
  <div class="stat">Triples: <b id="tripleCount">0</b></div>
</div>
<div id="tooltip"></div>
<svg></svg>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const width = window.innerWidth, height = window.innerHeight;
const svg = d3.select("svg").attr("viewBox", [0, 0, width, height]);
const g = svg.append("g");

// Zoom
svg.call(d3.zoom().scaleExtent([0.1, 8]).on("zoom", e => g.attr("transform", e.transform)));

const tooltip = d3.select("#tooltip");

// Color scale by connection count
const color = d3.scaleSequential(d3.interpolateViridis).domain([1, 20]);

fetch("/grafana/graph/fields", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
.then(r => r.json())
.then(data => {
  const nodes = data.nodes;
  const links = data.edges.map(e => ({...e, source: e.source, target: e.target}));

  document.getElementById("nodeCount").textContent = nodes.length;
  document.getElementById("edgeCount").textContent = links.length;
  document.getElementById("tripleCount").textContent = links.length;

  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(80))
    .force("charge", d3.forceManyBody().strength(-120))
    .force("center", d3.forceCenter(width/2, height/2))
    .force("collision", d3.forceCollide(20));

  const link = g.append("g").selectAll("line")
    .data(links).join("line")
    .attr("class", "link")
    .attr("stroke", "#4a5568")
    .attr("stroke-width", 1)
    .on("mouseover", (e, d) => {
      tooltip.style("display", "block")
        .style("left", e.pageX + 10 + "px")
        .style("top", e.pageY - 10 + "px")
        .html("<b>" + (nodes.find(n=>n.id===d.source.id)||{}).title + "</b> → " + d.mainStat + " → <b>" + (nodes.find(n=>n.id===d.target.id)||{}).title + "</b>");
    })
    .on("mouseout", () => tooltip.style("display", "none"));

  const node = g.append("g").selectAll(".node")
    .data(nodes).join("g")
    .attr("class", "node")
    .call(d3.drag()
      .on("start", (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on("drag", (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on("end", (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; })
    );

  node.append("circle")
    .attr("r", d => Math.max(4, Math.min(20, d.arc__connections * 1.5)))
    .attr("fill", d => color(d.arc__connections))
    .on("mouseover", (e, d) => {
      tooltip.style("display", "block")
        .style("left", e.pageX + 10 + "px")
        .style("top", e.pageY - 10 + "px")
        .html("<b>" + d.title + "</b><br>Connections: " + d.arc__connections);
      // Highlight connected
      link.attr("stroke", l => (l.source.id===d.id||l.target.id===d.id) ? "#7eb8f7" : "#4a5568")
          .attr("stroke-width", l => (l.source.id===d.id||l.target.id===d.id) ? 2 : 1)
          .attr("stroke-opacity", l => (l.source.id===d.id||l.target.id===d.id) ? 1 : 0.15);
      node.select("circle").attr("opacity", n => {
        if(n.id===d.id) return 1;
        return links.some(l => (l.source.id===d.id && l.target.id===n.id)||(l.target.id===d.id && l.source.id===n.id)) ? 1 : 0.15;
      });
      node.select("text").attr("opacity", n => {
        if(n.id===d.id) return 1;
        return links.some(l => (l.source.id===d.id && l.target.id===n.id)||(l.target.id===d.id && l.source.id===n.id)) ? 1 : 0.15;
      });
    })
    .on("mouseout", () => {
      tooltip.style("display", "none");
      link.attr("stroke", "#4a5568").attr("stroke-width", 1).attr("stroke-opacity", 0.4);
      node.select("circle").attr("opacity", 1);
      node.select("text").attr("opacity", 1);
    });

  // Labels for nodes with 3+ connections
  node.filter(d => d.arc__connections >= 3).append("text")
    .attr("dx", 12).attr("dy", 4)
    .text(d => d.title);

  sim.on("tick", () => {
    link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
    node.attr("transform", d => "translate("+d.x+","+d.y+")");
  });

  // Search
  document.getElementById("search").addEventListener("input", function() {
    const q = this.value.toLowerCase();
    if(!q) {
      node.select("circle").attr("opacity", 1);
      node.select("text").attr("opacity", 1);
      link.attr("stroke-opacity", 0.4);
      return;
    }
    node.select("circle").attr("opacity", d => d.title.toLowerCase().includes(q) ? 1 : 0.1);
    node.select("text").attr("opacity", d => d.title.toLowerCase().includes(q) ? 1 : 0.1);
    link.attr("stroke-opacity", l => {
      const s = (nodes.find(n=>n.id===l.source.id)||{}).title||"";
      const t = (nodes.find(n=>n.id===l.target.id)||{}).title||"";
      return s.toLowerCase().includes(q)||t.toLowerCase().includes(q) ? 0.8 : 0.05;
    });
  });
});
</script>
</body>
</html>"""

