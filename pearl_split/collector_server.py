"""Zero-dependency HTTP & SQLite Telemetry Collector Backend Server.

Collects real-time SRBMiner console output and real hashrate/hardware telemetry
from multiple distributed mining clients, provides REST APIs and a built-in Web UI,
and runs either embedded inside PearlSplit.exe or standalone on any Windows/Linux server.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

try:
    from .srb_parser import SrbTelemetryParser, format_ths
    from .alert_engine import (
        fetch_online_workers_stdlib,
        format_offline_alert_message,
        format_poll_failure_alert_message,
        format_recovery_alert_message,
        format_test_alert_message,
        normalize_alert_config,
        send_alert_message,
    )
except ImportError:
    from srb_parser import SrbTelemetryParser, format_ths  # type: ignore[no-redef]
    from alert_engine import (  # type: ignore[no-redef]
        fetch_online_workers_stdlib,
        format_offline_alert_message,
        format_poll_failure_alert_message,
        format_recovery_alert_message,
        format_test_alert_message,
        normalize_alert_config,
        send_alert_message,
    )


_CN_TZ_ALIASES = {"Asia/Shanghai", "Asia/Chongqing", "Asia/Hong_Kong", "Asia/Taipei"}


def _now_str() -> str:
    tz_name = (os.environ.get("TZ") or "").strip()
    if tz_name:
        if ZoneInfo is not None:
            try:
                return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        if tz_name in _CN_TZ_ALIASES:
            local_off = datetime.now().astimezone().utcoffset()
            if local_off is None or local_off == timedelta(0):
                return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_lan_ips() -> list[str]:
    """Return likely LAN IPv4 addresses for easy client configuration hints."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


class CollectorStore:
    """Thread-safe SQLite store + server-side SRBMiner log parser cache."""

    def __init__(self, db_path: str | Path, max_logs_per_client: int = 800) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_logs_per_client = max_logs_per_client
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Some NAS network mounts do not support WAL locking; fallback safely.
            self._conn.execute("PRAGMA journal_mode=DELETE")
        self._parsers: dict[str, SrbTelemetryParser] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS collector_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    worker_name TEXT NOT NULL DEFAULT '',
                    wallet_address TEXT NOT NULL DEFAULT '',
                    host_name TEXT NOT NULL DEFAULT '',
                    os_info TEXT NOT NULL DEFAULT '',
                    source_mode TEXT NOT NULL DEFAULT '',
                    miner_status TEXT NOT NULL DEFAULT 'running',
                    algorithm TEXT NOT NULL DEFAULT '--',
                    miner_version TEXT NOT NULL DEFAULT 'SRBMiner-MULTI',
                    pool_url TEXT NOT NULL DEFAULT '--',
                    uptime_sec INTEGER NOT NULL DEFAULT 0,
                    total_hashrate_ths REAL NOT NULL DEFAULT 0,
                    session_avg_ths REAL NOT NULL DEFAULT 0,
                    avg_5m_ths REAL NOT NULL DEFAULT 0,
                    avg_15m_ths REAL NOT NULL DEFAULT 0,
                    min_hashrate_ths REAL NOT NULL DEFAULT 0,
                    max_hashrate_ths REAL NOT NULL DEFAULT 0,
                    stability_pct REAL NOT NULL DEFAULT 0,
                    hashrate_raw_text TEXT NOT NULL DEFAULT '0.00 TH/s',
                    accepted_shares INTEGER NOT NULL DEFAULT 0,
                    rejected_shares INTEGER NOT NULL DEFAULT 0,
                    stale_shares INTEGER NOT NULL DEFAULT 0,
                    accept_rate_pct REAL NOT NULL DEFAULT 100,
                    shares_per_hour REAL NOT NULL DEFAULT 0,
                    avg_latency_ms REAL,
                    total_power_w REAL NOT NULL DEFAULT 0,
                    efficiency_ths_per_kw REAL,
                    devices_json TEXT NOT NULL DEFAULT '[]',
                    samples_json TEXT NOT NULL DEFAULT '[]',
                    remote_ip TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_heartbeat_ts REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collector_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    line TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_collector_logs_client_id
                    ON collector_logs(client_id, id DESC);

                CREATE TABLE IF NOT EXISTS standby_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    config_json TEXT NOT NULL DEFAULT '{}',
                    wallets_json TEXT NOT NULL DEFAULT '[]',
                    workers_meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS standby_worker_state (
                    wallet_address TEXT NOT NULL,
                    worker_name TEXT NOT NULL,
                    member_name TEXT NOT NULL DEFAULT '未分配',
                    is_online INTEGER NOT NULL DEFAULT 1,
                    last_shares INTEGER NOT NULL DEFAULT 0,
                    last_seen_online_at TEXT NOT NULL DEFAULT '',
                    consecutive_offline_polls INTEGER NOT NULL DEFAULT 0,
                    offline_alert_sent INTEGER NOT NULL DEFAULT 0,
                    last_alert_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (wallet_address, worker_name)
                );

                CREATE TABLE IF NOT EXISTS standby_runtime (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL DEFAULT 'disabled',
                    desktop_host TEXT NOT NULL DEFAULT '',
                    desktop_last_heartbeat_ts REAL NOT NULL DEFAULT 0,
                    desktop_last_heartbeat_at TEXT NOT NULL DEFAULT '',
                    last_poll_at TEXT NOT NULL DEFAULT '',
                    last_poll_ok INTEGER NOT NULL DEFAULT 1,
                    last_poll_error TEXT NOT NULL DEFAULT '',
                    first_fail_ts REAL,
                    poll_fail_alert_sent INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS standby_alert_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    wallet_alias TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT 'offline',
                    worker_names TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'pushplus',
                    ok INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _get_parser(self, client_id: str) -> SrbTelemetryParser:
        parser = self._parsers.get(client_id)
        if parser is None:
            parser = SrbTelemetryParser(max_lines=200, max_samples=240)
            self._parsers[client_id] = parser
        return parser

    def ingest(self, payload: dict[str, Any], remote_ip: str = "") -> dict[str, Any]:
        now_ts = time.time()
        now_str = _now_str()

        raw_cid = str(payload.get("client_id") or payload.get("client_name") or remote_ip or "unknown-client").strip()
        client_id = raw_cid[:128]
        client_name = str(payload.get("client_name") or payload.get("worker_name") or client_id).strip()[:128]
        worker_name = str(payload.get("worker_name") or client_name).strip()[:128]
        wallet_address = str(payload.get("wallet_address") or "").strip()[:160]
        host_name = str(payload.get("host_name") or "").strip()[:128]
        os_info = str(payload.get("os_info") or "").strip()[:128]
        source_mode = str(payload.get("source_mode") or "agent").strip()[:64]
        miner_status = str(payload.get("miner_status") or "running").strip()[:32]

        raw_lines = payload.get("log_lines") or []
        if isinstance(raw_lines, dict) or isinstance(raw_lines, str):
            raw_lines = [raw_lines]
        parsed_entries: list[dict[str, Any]] = []

        with self._lock:
            parser = self._get_parser(client_id)
            if worker_name:
                parser.seed_metadata(worker_name=worker_name)

            for item in raw_lines:
                if isinstance(item, str):
                    entry = parser.feed_line(item)
                    if entry is not None:
                        parsed_entries.append(entry.to_dict())
                elif isinstance(item, dict):
                    line_txt = str(item.get("line") or "").strip()
                    if not line_txt:
                        continue
                    ts_ov = str(item.get("ts") or "") or None
                    entry = parser.feed_line(line_txt, ts_override=ts_ov)
                    if entry is not None:
                        d = entry.to_dict()
                        if item.get("seq") is not None:
                            try:
                                d["seq"] = int(item["seq"])
                            except (TypeError, ValueError):
                                pass
                        parsed_entries.append(d)

            srv_report = parser.get_report()
            has_client_stats = payload.get("total_hashrate_ths") is not None and (
                float(payload.get("total_hashrate_ths") or 0) > 0
                or int(payload.get("accepted_shares") or 0) > 0
                or isinstance(payload.get("devices"), list) and len(payload.get("devices")) > 0
            )

            # Check existing stored snapshot in case heartbeat has no new log lines
            existing = self._conn.execute(
                "SELECT * FROM collector_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            ex = dict(existing) if existing else {}

            if has_client_stats:
                total_ths = float(payload.get("total_hashrate_ths") or 0.0)
                session_avg_ths = float(payload.get("session_avg_ths") or total_ths)
                avg_5m_ths = float(payload.get("avg_5m_ths") or session_avg_ths)
                avg_15m_ths = float(payload.get("avg_15m_ths") or session_avg_ths)
                min_ths = float(payload.get("min_hashrate_ths") or total_ths)
                max_ths = float(payload.get("max_hashrate_ths") or total_ths)
                stability_pct = float(payload.get("stability_pct") or (100.0 if total_ths > 0 else 0.0))
                hashrate_raw_text = str(payload.get("hashrate_raw_text") or format_ths(total_ths))
                accepted = int(payload.get("accepted_shares") or 0)
                rejected = int(payload.get("rejected_shares") or 0)
                stale = int(payload.get("stale_shares") or 0)
                accept_rate = float(payload.get("accept_rate_pct") or 100.0)
                shares_per_hour = float(payload.get("shares_per_hour") or 0.0)
                avg_lat = payload.get("avg_latency_ms")
                total_power_w = float(payload.get("total_power_w") or 0.0)
                eff = payload.get("efficiency_ths_per_kw")
                devices = payload.get("devices") or []
                samples = payload.get("samples") or srv_report["samples"][-80:]
                algorithm = str(payload.get("algorithm") or srv_report["algorithm"] or "--")
                miner_version = str(payload.get("miner_version") or srv_report["miner_version"] or "SRBMiner-MULTI")
                pool_url = str(payload.get("pool_url") or srv_report["pool_url"] or "--")
                uptime_sec = int(payload.get("uptime_sec") or srv_report["uptime_sec"] or 0)
            elif parsed_entries or srv_report["sample_count"] > 0 or srv_report["accepted_shares"] > 0:
                # Server-side parsed telemetry from thin/PowerShell clients
                total_ths = float(srv_report["current_hashrate_ths"])
                session_avg_ths = float(srv_report["session_avg_ths"])
                avg_5m_ths = float(srv_report["avg_5m_ths"])
                avg_15m_ths = float(srv_report["avg_15m_ths"])
                min_ths = float(srv_report["min_hashrate_ths"])
                max_ths = float(srv_report["max_hashrate_ths"])
                stability_pct = float(srv_report["stability_pct"])
                hashrate_raw_text = str(srv_report["current_hashrate_text"])
                accepted = int(srv_report["accepted_shares"])
                rejected = int(srv_report["rejected_shares"])
                stale = int(srv_report["stale_shares"])
                accept_rate = float(srv_report["accept_rate_pct"])
                shares_per_hour = float(srv_report["shares_per_hour"])
                avg_lat = srv_report["avg_latency_ms"]
                total_power_w = float(srv_report["total_power_w"])
                eff = srv_report["efficiency_ths_per_kw"]
                devices = srv_report["devices"]
                samples = srv_report["samples"][-80:]
                algorithm = str(payload.get("algorithm") or srv_report["algorithm"] or ex.get("algorithm") or "--")
                miner_version = str(payload.get("miner_version") or srv_report["miner_version"] or ex.get("miner_version") or "SRBMiner-MULTI")
                pool_url = str(payload.get("pool_url") or srv_report["pool_url"] or ex.get("pool_url") or "--")
                uptime_sec = int(payload.get("uptime_sec") or srv_report["uptime_sec"] or ex.get("uptime_sec") or 0)
            else:
                # Pure heartbeat preserving last known stats
                total_ths = float(ex.get("total_hashrate_ths") or 0.0)
                session_avg_ths = float(ex.get("session_avg_ths") or 0.0)
                avg_5m_ths = float(ex.get("avg_5m_ths") or 0.0)
                avg_15m_ths = float(ex.get("avg_15m_ths") or 0.0)
                min_ths = float(ex.get("min_hashrate_ths") or 0.0)
                max_ths = float(ex.get("max_hashrate_ths") or 0.0)
                stability_pct = float(ex.get("stability_pct") or 0.0)
                hashrate_raw_text = str(ex.get("hashrate_raw_text") or "0.00 TH/s")
                accepted = int(ex.get("accepted_shares") or 0)
                rejected = int(ex.get("rejected_shares") or 0)
                stale = int(ex.get("stale_shares") or 0)
                accept_rate = float(ex.get("accept_rate_pct") or 100.0)
                shares_per_hour = float(ex.get("shares_per_hour") or 0.0)
                avg_lat = ex.get("avg_latency_ms")
                total_power_w = float(ex.get("total_power_w") or 0.0)
                eff = ex.get("efficiency_ths_per_kw")
                try:
                    devices = json.loads(ex.get("devices_json") or "[]")
                except Exception:
                    devices = []
                try:
                    samples = json.loads(ex.get("samples_json") or "[]")
                except Exception:
                    samples = []
                algorithm = str(payload.get("algorithm") or ex.get("algorithm") or "--")
                miner_version = str(payload.get("miner_version") or ex.get("miner_version") or "SRBMiner-MULTI")
                pool_url = str(payload.get("pool_url") or ex.get("pool_url") or "--")
                uptime_sec = int(payload.get("uptime_sec") or ex.get("uptime_sec") or 0)

            first_seen = ex.get("first_seen_at") or now_str
            self._conn.execute(
                """
                INSERT INTO collector_clients (
                    client_id, client_name, worker_name, wallet_address, host_name, os_info,
                    source_mode, miner_status, algorithm, miner_version, pool_url, uptime_sec,
                    total_hashrate_ths, session_avg_ths, avg_5m_ths, avg_15m_ths,
                    min_hashrate_ths, max_hashrate_ths, stability_pct, hashrate_raw_text,
                    accepted_shares, rejected_shares, stale_shares, accept_rate_pct, shares_per_hour,
                    avg_latency_ms, total_power_w, efficiency_ths_per_kw, devices_json, samples_json,
                    remote_ip, first_seen_at, last_seen_at, last_heartbeat_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_name=excluded.client_name,
                    worker_name=excluded.worker_name,
                    wallet_address=CASE WHEN excluded.wallet_address != '' THEN excluded.wallet_address ELSE collector_clients.wallet_address END,
                    host_name=excluded.host_name,
                    os_info=excluded.os_info,
                    source_mode=excluded.source_mode,
                    miner_status=excluded.miner_status,
                    algorithm=excluded.algorithm,
                    miner_version=excluded.miner_version,
                    pool_url=excluded.pool_url,
                    uptime_sec=excluded.uptime_sec,
                    total_hashrate_ths=excluded.total_hashrate_ths,
                    session_avg_ths=excluded.session_avg_ths,
                    avg_5m_ths=excluded.avg_5m_ths,
                    avg_15m_ths=excluded.avg_15m_ths,
                    min_hashrate_ths=excluded.min_hashrate_ths,
                    max_hashrate_ths=excluded.max_hashrate_ths,
                    stability_pct=excluded.stability_pct,
                    hashrate_raw_text=excluded.hashrate_raw_text,
                    accepted_shares=excluded.accepted_shares,
                    rejected_shares=excluded.rejected_shares,
                    stale_shares=excluded.stale_shares,
                    accept_rate_pct=excluded.accept_rate_pct,
                    shares_per_hour=excluded.shares_per_hour,
                    avg_latency_ms=excluded.avg_latency_ms,
                    total_power_w=excluded.total_power_w,
                    efficiency_ths_per_kw=excluded.efficiency_ths_per_kw,
                    devices_json=excluded.devices_json,
                    samples_json=excluded.samples_json,
                    remote_ip=excluded.remote_ip,
                    last_seen_at=excluded.last_seen_at,
                    last_heartbeat_ts=excluded.last_heartbeat_ts
                """,
                (
                    client_id,
                    client_name,
                    worker_name,
                    wallet_address,
                    host_name,
                    os_info,
                    source_mode,
                    miner_status,
                    algorithm,
                    miner_version,
                    pool_url,
                    uptime_sec,
                    total_ths,
                    session_avg_ths,
                    avg_5m_ths,
                    avg_15m_ths,
                    min_ths,
                    max_ths,
                    stability_pct,
                    hashrate_raw_text,
                    accepted,
                    rejected,
                    stale,
                    accept_rate,
                    shares_per_hour,
                    avg_lat,
                    total_power_w,
                    eff,
                    json.dumps(devices, ensure_ascii=False),
                    json.dumps(samples[-80:], ensure_ascii=False),
                    remote_ip,
                    first_seen,
                    now_str,
                    now_ts,
                ),
            )

            if parsed_entries:
                self._conn.executemany(
                    "INSERT INTO collector_logs (client_id, seq, ts, level, line, created_ts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            client_id,
                            int(e.get("seq") or 0),
                            str(e.get("ts") or now_str),
                            str(e.get("level") or "info"),
                            str(e.get("line") or ""),
                            now_ts,
                        )
                        for e in parsed_entries
                    ],
                )
                # Trim old logs per client cleanly
                self._conn.execute(
                    """
                    DELETE FROM collector_logs
                    WHERE client_id = ?
                      AND id NOT IN (
                        SELECT id FROM collector_logs
                        WHERE client_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                      )
                    """,
                    (client_id, client_id, self.max_logs_per_client),
                )
            self._conn.commit()
            return {"ok": True, "client_id": client_id, "ingested_lines": len(parsed_entries), "server_time": now_str}

    def list_clients(self, online_timeout_sec: int = 35) -> dict[str, Any]:
        now_ts = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM collector_clients ORDER BY last_heartbeat_ts DESC, client_name ASC"
            ).fetchall()
            clients: list[dict[str, Any]] = []
            total_online_ths = 0.0
            total_power_w = 0.0
            online_clients = 0
            total_devices = 0
            total_accepted = 0
            total_rejected = 0

            for r in rows:
                d = dict(r)
                is_online = (now_ts - float(d["last_heartbeat_ts"])) <= online_timeout_sec and d.get("miner_status") != "stopped"
                d["is_online"] = is_online
                d["seconds_ago"] = max(0, int(now_ts - float(d["last_heartbeat_ts"])))
                try:
                    d["devices"] = json.loads(d.pop("devices_json") or "[]")
                except Exception:
                    d["devices"] = []
                try:
                    d["samples"] = json.loads(d.pop("samples_json") or "[]")
                except Exception:
                    d["samples"] = []

                total_devices += len(d["devices"])
                total_accepted += int(d.get("accepted_shares") or 0)
                total_rejected += int(d.get("rejected_shares") or 0)
                if is_online:
                    online_clients += 1
                    total_online_ths += float(d.get("total_hashrate_ths") or 0.0)
                    total_power_w += float(d.get("total_power_w") or 0.0)
                clients.append(d)

            total_sub = total_accepted + total_rejected
            return {
                "server_time": _now_str(),
                "clients_total": len(clients),
                "clients_online": online_clients,
                "total_real_hashrate_ths": round(total_online_ths, 4),
                "total_real_hashrate_text": format_ths(total_online_ths),
                "total_power_w": round(total_power_w, 1),
                "total_devices": total_devices,
                "total_accepted_shares": total_accepted,
                "total_rejected_shares": total_rejected,
                "overall_accept_rate_pct": round((total_accepted / total_sub * 100.0) if total_sub > 0 else 100.0, 2),
                "clients": clients,
            }

    def get_client_detail(self, client_id: str, limit: int = 250, since_id: int = 0) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM collector_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            if not row:
                return None
            now_ts = time.time()
            d = dict(row)
            d["is_online"] = (now_ts - float(d["last_heartbeat_ts"])) <= 35 and d.get("miner_status") != "stopped"
            d["seconds_ago"] = max(0, int(now_ts - float(d["last_heartbeat_ts"])))
            try:
                d["devices"] = json.loads(d.pop("devices_json") or "[]")
            except Exception:
                d["devices"] = []
            try:
                d["samples"] = json.loads(d.pop("samples_json") or "[]")
            except Exception:
                d["samples"] = []

            if since_id > 0:
                log_rows = self._conn.execute(
                    "SELECT id, seq, ts, level, line FROM collector_logs WHERE client_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (client_id, since_id, limit),
                ).fetchall()
                logs = [dict(lr) for lr in log_rows]
            else:
                log_rows = self._conn.execute(
                    "SELECT id, seq, ts, level, line FROM collector_logs WHERE client_id = ? ORDER BY id DESC LIMIT ?",
                    (client_id, limit),
                ).fetchall()
                logs = [dict(lr) for lr in reversed(log_rows)]

            return {"client": d, "logs": logs}

    def delete_client(self, client_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM collector_logs WHERE client_id = ?", (client_id,))
            self._conn.execute("DELETE FROM collector_clients WHERE client_id = ?", (client_id,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Standby Offline Alert Failover (Desktop Lease <-> Docker Standby)
    # ------------------------------------------------------------------
    def _load_standby_config_unlocked(self) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, dict[str, Any]]]:
        row = self._conn.execute("SELECT * FROM standby_config WHERE id = 1").fetchone()
        if not row:
            return normalize_alert_config({}), [], {}
        try:
            cfg = normalize_alert_config(json.loads(row["config_json"] or "{}"))
        except Exception:
            cfg = normalize_alert_config({})
        try:
            wallets = json.loads(row["wallets_json"] or "[]")
            if not isinstance(wallets, list):
                wallets = []
        except Exception:
            wallets = []
        try:
            wmeta = json.loads(row["workers_meta_json"] or "{}")
            if not isinstance(wmeta, dict):
                wmeta = {}
        except Exception:
            wmeta = {}
        return cfg, wallets, wmeta

    def _add_standby_log_unlocked(
        self,
        wallet_alias: str,
        event_type: str,
        worker_names: str,
        title: str,
        summary: str,
        provider: str,
        ok: bool,
        error: str = "",
        created_at: str = "",
    ) -> None:
        ts = (created_at or "").strip() or _now_str()
        self._conn.execute(
            """
            INSERT INTO standby_alert_logs (
                created_at, wallet_alias, event_type, worker_names,
                title, summary, provider, ok, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, wallet_alias, event_type, worker_names, title, summary, provider, 1 if ok else 0, error),
        )
        self._conn.execute(
            "DELETE FROM standby_alert_logs WHERE id NOT IN ("
            "SELECT id FROM standby_alert_logs ORDER BY id DESC LIMIT 120)"
        )

    def receive_desktop_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept live config + lease renewal from Desktop EXE and return any standby handback alerts."""
        now_ts = time.time()
        now_s = _now_str()
        cfg = normalize_alert_config(payload.get("alert_config") or {})
        raw_wallets = payload.get("wallets") or []
        wallets = [
            {"address": str(w.get("address") or "").strip(), "alias": str(w.get("alias") or "").strip()}
            for w in raw_wallets
            if isinstance(w, dict) and str(w.get("address") or "").strip()
        ]
        raw_states = [w for w in (payload.get("worker_states") or []) if isinstance(w, dict)]
        desktop_host = str(payload.get("desktop_host") or "Desktop").strip()[:128]
        desktop_poll_ok = bool(payload.get("desktop_poll_ok", True))
        desktop_first_fail = payload.get("desktop_first_fail_ts")
        desktop_fail_sent = 1 if payload.get("desktop_poll_fail_alert_sent") else 0

        with self._lock:
            rt_row = self._conn.execute("SELECT * FROM standby_runtime WHERE id = 1").fetchone()
            prev_mode = str(rt_row["mode"]) if rt_row else "disabled"

            existing_states = {
                (r["wallet_address"].strip().lower(), r["worker_name"]): dict(r)
                for r in self._conn.execute("SELECT * FROM standby_worker_state").fetchall()
            }

            handback_states: list[dict[str, Any]] = []
            for st_row in existing_states.values():
                if int(st_row.get("offline_alert_sent") or 0) == 1 and int(st_row.get("is_online") or 0) == 0:
                    handback_states.append(
                        {
                            "wallet_address": st_row["wallet_address"],
                            "worker_name": st_row["worker_name"],
                            "offline_alert_sent": 1,
                            "last_alert_at": st_row.get("last_alert_at") or "",
                        }
                    )

            workers_meta: dict[str, dict[str, Any]] = {}
            seen_keys: set[tuple[str, str]] = set()
            for ws in raw_states:
                addr = str(ws.get("wallet_address") or "").strip()
                wname = str(ws.get("worker_name") or "").strip()
                if not addr or not wname:
                    continue
                k_str = f"{addr.lower()}||{wname}"
                seen_keys.add((addr, wname))
                workers_meta[k_str] = {
                    "wallet_alias": str(ws.get("wallet_alias") or "").strip(),
                    "member_name": str(ws.get("member_name") or "未分配").strip(),
                    "remote_tool": str(ws.get("remote_tool") or "ToDesk").strip() or "ToDesk",
                    "remote_code": str(ws.get("remote_code") or "").strip(),
                    "remote_password": str(ws.get("remote_password") or "").strip(),
                    "remote_note": str(ws.get("remote_note") or "").strip(),
                    "alert_enabled": int(ws.get("alert_enabled") if ws.get("alert_enabled") is not None else 1),
                }

                is_on = 1 if int(ws.get("is_online") or 0) == 1 else 0
                desk_sent = 1 if int(ws.get("offline_alert_sent") or 0) == 1 else 0
                consec_off = int(ws.get("consecutive_offline_polls") or 0)
                desk_alert_at = str(ws.get("last_alert_at") or "").strip()
                ex = existing_states.get((addr.lower(), wname))
                # If Docker already alerted while standby was active, preserve offline+alerted flag
                # even before Desktop's post-wakeup poll catches up (avoids re-alert if Desktop restarts quickly)
                if (
                    prev_mode == "standby_active"
                    and ex
                    and int(ex.get("is_online") or 0) == 0
                    and int(ex.get("offline_alert_sent") or 0) == 1
                    and desk_sent == 0
                ):
                    is_on = 0
                    desk_sent = 1
                    consec_off = max(consec_off, int(ex.get("consecutive_offline_polls") or 1))
                    desk_alert_at = str(ex.get("last_alert_at") or desk_alert_at)

                self._conn.execute(
                    """
                    INSERT INTO standby_worker_state (
                        wallet_address, worker_name, member_name, is_online, last_shares,
                        last_seen_online_at, consecutive_offline_polls, offline_alert_sent, last_alert_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet_address, worker_name) DO UPDATE SET
                        member_name = excluded.member_name,
                        is_online = excluded.is_online,
                        last_shares = excluded.last_shares,
                        last_seen_online_at = excluded.last_seen_online_at,
                        consecutive_offline_polls = excluded.consecutive_offline_polls,
                        offline_alert_sent = excluded.offline_alert_sent,
                        last_alert_at = excluded.last_alert_at
                    """,
                    (
                        addr,
                        wname,
                        workers_meta[k_str]["member_name"],
                        is_on,
                        int(ws.get("last_shares") or 0),
                        str(ws.get("last_seen_online_at") or ""),
                        consec_off,
                        desk_sent,
                        desk_alert_at,
                    ),
                )

            # Remove obsolete workers absent from Desktop snapshot
            for ex_addr, ex_name in existing_states:
                if not any(a.lower() == ex_addr and n == ex_name for a, n in seen_keys):
                    self._conn.execute(
                        "DELETE FROM standby_worker_state WHERE LOWER(wallet_address) = ? AND worker_name = ?",
                        (ex_addr, ex_name),
                    )

            self._conn.execute(
                """
                INSERT INTO standby_config (id, config_json, wallets_json, workers_meta_json, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_json = excluded.config_json,
                    wallets_json = excluded.wallets_json,
                    workers_meta_json = excluded.workers_meta_json,
                    updated_at = excluded.updated_at
                """,
                (
                    json.dumps(cfg, ensure_ascii=False),
                    json.dumps(wallets, ensure_ascii=False),
                    json.dumps(workers_meta, ensure_ascii=False),
                    now_s,
                ),
            )

            next_mode = "paused_by_desktop" if (cfg["enabled"] and wallets) else "disabled"
            self._conn.execute(
                """
                INSERT INTO standby_runtime (
                    id, mode, desktop_host, desktop_last_heartbeat_ts, desktop_last_heartbeat_at,
                    last_poll_at, last_poll_ok, last_poll_error, first_fail_ts,
                    poll_fail_alert_sent, consecutive_failures
                ) VALUES (1, ?, ?, ?, ?, ?, ?, '', ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    mode = excluded.mode,
                    desktop_host = excluded.desktop_host,
                    desktop_last_heartbeat_ts = excluded.desktop_last_heartbeat_ts,
                    desktop_last_heartbeat_at = excluded.desktop_last_heartbeat_at,
                    first_fail_ts = CASE
                        WHEN ? = 1 THEN NULL
                        ELSE COALESCE(?, standby_runtime.first_fail_ts)
                    END,
                    poll_fail_alert_sent = CASE
                        WHEN ? = 1 THEN 0
                        ELSE MAX(standby_runtime.poll_fail_alert_sent, ?)
                    END,
                    last_poll_error = CASE WHEN ? = 1 THEN '' ELSE standby_runtime.last_poll_error END,
                    consecutive_failures = CASE WHEN ? = 1 THEN 0 ELSE standby_runtime.consecutive_failures END
                """,
                (
                    next_mode,
                    desktop_host,
                    now_ts,
                    now_s,
                    now_s if desktop_poll_ok else "",
                    1 if desktop_poll_ok else 0,
                    None if desktop_poll_ok else desktop_first_fail,
                    0 if desktop_poll_ok else desktop_fail_sent,
                    1 if desktop_poll_ok else 0,
                    desktop_first_fail,
                    1 if desktop_poll_ok else 0,
                    desktop_fail_sent,
                    1 if desktop_poll_ok else 0,
                    1 if desktop_poll_ok else 0,
                ),
            )
            standby_logs = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM standby_alert_logs ORDER BY id DESC LIMIT 50"
                ).fetchall()
            ]
            self._conn.commit()

        return {
            "ok": True,
            "mode": next_mode,
            "handback_worker_states": handback_states,
            "new_standby_logs": standby_logs,
            "status": self.get_standby_status(),
        }

    def release_desktop_lease(self) -> dict[str, Any]:
        """Immediately expire Desktop's active lease so Docker can assume polling on exit."""
        with self._lock:
            cfg, wallets, _ = self._load_standby_config_unlocked()
            next_mode = "standby_active" if (cfg["enabled"] and wallets) else "disabled"
            self._conn.execute(
                """
                INSERT INTO standby_runtime (id, mode, desktop_last_heartbeat_ts)
                VALUES (1, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    mode = excluded.mode,
                    desktop_last_heartbeat_ts = 0
                """,
                (next_mode,),
            )
            self._conn.commit()
        return {"ok": True, "status": self.get_standby_status()}

    def get_standby_status(self) -> dict[str, Any]:
        now_ts = time.time()
        with self._lock:
            cfg, wallets, _ = self._load_standby_config_unlocked()
            rt_row = self._conn.execute("SELECT * FROM standby_runtime WHERE id = 1").fetchone()
            rt = dict(rt_row) if rt_row else {}
            worker_cnt = int(self._conn.execute("SELECT COUNT(*) FROM standby_worker_state").fetchone()[0])
            logs = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM standby_alert_logs ORDER BY id DESC LIMIT 20"
                ).fetchall()
            ]

        lease_sec = int(cfg["desktop_lease_timeout_sec"])
        last_hb_ts = float(rt.get("desktop_last_heartbeat_ts") or 0.0)
        elapsed_hb = max(0, int(now_ts - last_hb_ts)) if last_hb_ts > 0 else None
        if not cfg["enabled"] or not wallets:
            eff_mode = "disabled"
        elif last_hb_ts > 0 and (now_ts - last_hb_ts) < lease_sec:
            eff_mode = "paused_by_desktop"
        else:
            eff_mode = "standby_active"

        return {
            "mode": eff_mode,
            "enabled": bool(cfg["enabled"]),
            "provider": cfg["provider"],
            "desktop_host": rt.get("desktop_host") or "",
            "desktop_last_heartbeat_at": rt.get("desktop_last_heartbeat_at") or "",
            "seconds_since_desktop_heartbeat": elapsed_hb,
            "lease_timeout_sec": lease_sec,
            "standby_poll_interval_sec": int(cfg["standby_poll_interval_sec"]),
            "tracked_wallets": len(wallets),
            "tracked_workers": worker_cnt,
            "last_poll_at": rt.get("last_poll_at") or "",
            "last_poll_ok": bool(rt.get("last_poll_ok", 1)),
            "last_poll_error": rt.get("last_poll_error") or "",
            "poll_fail_alert_sent": bool(rt.get("poll_fail_alert_sent", 0)),
            "recent_logs": logs,
        }

    def send_standby_test(self, config_override: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            saved_cfg, _, _ = self._load_standby_config_unlocked()
        cfg = normalize_alert_config(config_override) if config_override else saved_cfg
        title, md = format_test_alert_message(
            source_tag="群晖 Docker 备援节点",
            include_password=bool(cfg["include_password"]),
        )
        res = send_alert_message(cfg, title, md)
        with self._lock:
            self._add_standby_log_unlocked(
                wallet_alias="测试",
                event_type="test",
                worker_names="",
                title=title,
                summary="群晖 Docker 备援发送测试通知" if res.get("ok") else "群晖 Docker 发送测试通知失败",
                provider=str(res.get("provider") or cfg["provider"]),
                ok=bool(res.get("ok")),
                error=str(res.get("error") or ""),
            )
            self._conn.commit()
        return res

    def run_standby_poll_once(
        self,
        fetcher: Any = None,
        sender: Any = None,
    ) -> dict[str, Any]:
        """Run one standby poll cycle when Desktop lease is expired; no-op when Desktop is active."""
        with self._lock:
            cfg, wallets, wmeta = self._load_standby_config_unlocked()
            rt_row = self._conn.execute("SELECT * FROM standby_runtime WHERE id = 1").fetchone()
            rt = dict(rt_row) if rt_row else {}

        if not cfg["enabled"] or not wallets:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO standby_runtime (id, mode) VALUES (1, 'disabled') "
                    "ON CONFLICT(id) DO UPDATE SET mode = 'disabled'"
                )
                self._conn.commit()
            return {"action": "disabled"}

        now_ts = time.time()
        last_hb_ts = float(rt.get("desktop_last_heartbeat_ts") or 0.0)
        lease_sec = int(cfg["desktop_lease_timeout_sec"])
        if last_hb_ts > 0 and (now_ts - last_hb_ts) < lease_sec:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO standby_runtime (id, mode) VALUES (1, 'paused_by_desktop') "
                    "ON CONFLICT(id) DO UPDATE SET mode = 'paused_by_desktop'"
                )
                self._conn.commit()
            return {"action": "paused_by_desktop"}

        with self._lock:
            self._conn.execute(
                "INSERT INTO standby_runtime (id, mode) VALUES (1, 'standby_active') "
                "ON CONFLICT(id) DO UPDATE SET mode = 'standby_active'"
            )
            self._conn.commit()

        fetch_fn = fetcher or fetch_online_workers_stdlib
        send_fn = sender or send_alert_message
        any_error: str | None = None
        pushed_count = 0

        for w_item in wallets:
            addr = str(w_item.get("address") or "").strip()
            w_alias = str(w_item.get("alias") or addr[:10]).strip()
            if not addr:
                continue
            try:
                snap = fetch_fn(addr)
                fetched_at = str(snap.get("fetched_at") or _now_str())
                online_workers = snap.get("workers") or []
            except Exception as exc:
                any_error = str(exc).strip() or exc.__class__.__name__
                continue

            seen_online = {str(w.get("name") or "").strip() for w in online_workers if w.get("name")}
            eligible_offline: list[dict[str, Any]] = []
            recovered_list: list[dict[str, Any]] = []

            with self._lock:
                ex_rows = self._conn.execute(
                    "SELECT * FROM standby_worker_state WHERE wallet_address = ?",
                    (addr,),
                ).fetchall()
                ex_map = {r["worker_name"]: dict(r) for r in ex_rows}

                for ow in online_workers:
                    wname = str(ow.get("name") or "").strip()
                    if not wname:
                        continue
                    mk = f"{addr.lower()}||{wname}"
                    meta = wmeta.get(mk) or {}
                    m_name = str(meta.get("member_name") or (ex_map.get(wname) or {}).get("member_name") or "未分配")
                    shares = int(ow.get("shares") or 0)
                    prev = ex_map.get(wname)
                    if (
                        prev
                        and int(prev.get("offline_alert_sent") or 0) == 1
                        and int(meta.get("alert_enabled", 1)) == 1
                    ):
                        recovered_list.append(
                            {
                                "worker_name": wname,
                                "member_name": m_name,
                                "current_hashrate": str(ow.get("current_hashrate") or "--"),
                                "shares": shares,
                                "last_seen_online_at": prev.get("last_seen_online_at") or "",
                            }
                        )
                    self._conn.execute(
                        """
                        INSERT INTO standby_worker_state (
                            wallet_address, worker_name, member_name, is_online, last_shares,
                            last_seen_online_at, consecutive_offline_polls, offline_alert_sent
                        ) VALUES (?, ?, ?, 1, ?, ?, 0, 0)
                        ON CONFLICT(wallet_address, worker_name) DO UPDATE SET
                            member_name = excluded.member_name,
                            is_online = 1,
                            last_shares = excluded.last_shares,
                            last_seen_online_at = excluded.last_seen_online_at,
                            consecutive_offline_polls = 0,
                            offline_alert_sent = 0
                        """,
                        (addr, wname, m_name, shares, fetched_at),
                    )

                for wname, prev in ex_map.items():
                    if wname in seen_online:
                        continue
                    next_streak = int(prev.get("consecutive_offline_polls") or 0) + 1
                    self._conn.execute(
                        "UPDATE standby_worker_state SET is_online = 0, consecutive_offline_polls = ? "
                        "WHERE wallet_address = ? AND worker_name = ?",
                        (next_streak, addr, wname),
                    )
                    mk = f"{addr.lower()}||{wname}"
                    meta = wmeta.get(mk) or {}
                    a_enabled = int(meta.get("alert_enabled", 1))
                    if (
                        a_enabled == 1
                        and int(prev.get("offline_alert_sent") or 0) == 0
                        and next_streak >= int(cfg["offline_confirm_polls"])
                    ):
                        eligible_offline.append(
                            {
                                "worker_name": wname,
                                "member_name": str(meta.get("member_name") or prev.get("member_name") or "未分配"),
                                "last_shares": int(prev.get("last_shares") or 0),
                                "last_seen_online_at": prev.get("last_seen_online_at") or "",
                                "remote_tool": str(meta.get("remote_tool") or "ToDesk"),
                                "remote_code": str(meta.get("remote_code") or ""),
                                "remote_password": str(meta.get("remote_password") or ""),
                                "remote_note": str(meta.get("remote_note") or ""),
                            }
                        )
                self._conn.commit()

            if eligible_offline:
                title, md = format_offline_alert_message(
                    wallet_alias=w_alias,
                    worker_items=eligible_offline,
                    include_password=bool(cfg["include_password"]),
                    source_tag="群晖 Docker 备援接管",
                    detected_at=fetched_at,
                )
                p_res = send_fn(cfg, title, md)
                w_str = ", ".join(x["worker_name"] for x in eligible_offline)
                with self._lock:
                    if p_res.get("ok"):
                        pushed_count += 1
                        for x in eligible_offline:
                            self._conn.execute(
                                "UPDATE standby_worker_state SET offline_alert_sent = 1, last_alert_at = ? "
                                "WHERE wallet_address = ? AND worker_name = ?",
                                (fetched_at, addr, x["worker_name"]),
                            )
                    self._add_standby_log_unlocked(
                        wallet_alias=w_alias,
                        event_type="offline",
                        worker_names=w_str,
                        title=title,
                        summary=f"群晖接管推送 {len(eligible_offline)} 台掉线" if p_res.get("ok") else "群晖推送掉线失败",
                        provider=str(p_res.get("provider") or cfg["provider"]),
                        ok=bool(p_res.get("ok")),
                        error=str(p_res.get("error") or ""),
                        created_at=fetched_at,
                    )
                    self._conn.commit()

            if cfg["notify_recovery"] and recovered_list:
                r_title, r_md = format_recovery_alert_message(
                    wallet_alias=w_alias,
                    worker_items=recovered_list,
                    source_tag="群晖 Docker 备援接管",
                    detected_at=fetched_at,
                )
                r_res = send_fn(cfg, r_title, r_md)
                r_str = ", ".join(x["worker_name"] for x in recovered_list)
                with self._lock:
                    if r_res.get("ok"):
                        pushed_count += 1
                    self._add_standby_log_unlocked(
                        wallet_alias=w_alias,
                        event_type="recovery",
                        worker_names=r_str,
                        title=r_title,
                        summary=f"群晖接管推送 {len(recovered_list)} 台上线" if r_res.get("ok") else "群晖推送上线失败",
                        provider=str(r_res.get("provider") or cfg["provider"]),
                        ok=bool(r_res.get("ok")),
                        error=str(r_res.get("error") or ""),
                        created_at=fetched_at,
                    )
                    self._conn.commit()

        poll_ts_str = _now_str()
        if any_error is None:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE standby_runtime
                    SET mode = 'standby_active', last_poll_at = ?, last_poll_ok = 1,
                        last_poll_error = '', first_fail_ts = NULL,
                        poll_fail_alert_sent = 0, consecutive_failures = 0
                    WHERE id = 1
                    """,
                    (poll_ts_str,),
                )
                self._conn.commit()
            return {"action": "polled", "ok": True, "pushed": pushed_count}

        with self._lock:
            rt_now = dict(self._conn.execute("SELECT * FROM standby_runtime WHERE id = 1").fetchone() or {})
            first_fail = rt_now.get("first_fail_ts")
            if first_fail is None:
                first_fail = time.time()
            consec = int(rt_now.get("consecutive_failures") or 0) + 1
            fail_sent = int(rt_now.get("poll_fail_alert_sent") or 0)
            elapsed_min = (time.time() - float(first_fail)) / 60.0
            self._conn.execute(
                """
                UPDATE standby_runtime
                SET mode = 'standby_active', last_poll_at = ?, last_poll_ok = 0,
                    last_poll_error = ?, first_fail_ts = ?, consecutive_failures = ?
                WHERE id = 1
                """,
                (poll_ts_str, any_error, first_fail, consec),
            )
            self._conn.commit()

        if fail_sent == 0 and elapsed_min >= float(cfg["poll_fail_alert_minutes"]):
            aliases = [str(w.get("alias") or "") for w in wallets if w.get("alias")]
            f_title, f_md = format_poll_failure_alert_message(
                minutes_failed=int(round(elapsed_min)),
                consecutive_failures=consec,
                last_error=any_error,
                wallet_aliases=aliases,
                source_tag="群晖 Docker 备援接管",
                detected_at=poll_ts_str,
            )
            f_res = send_fn(cfg, f_title, f_md)
            with self._lock:
                if f_res.get("ok"):
                    pushed_count += 1
                    self._conn.execute(
                        "UPDATE standby_runtime SET poll_fail_alert_sent = 1 WHERE id = 1"
                    )
                self._add_standby_log_unlocked(
                    wallet_alias="、".join(aliases) or "监控钱包",
                    event_type="poll_fail",
                    worker_names="",
                    title=f_title,
                    summary=f"群晖接管轮询失败超 {int(round(elapsed_min))} 分钟已报警",
                    provider=str(f_res.get("provider") or cfg["provider"]),
                    ok=bool(f_res.get("ok")),
                    error=str(f_res.get("error") or ""),
                    created_at=poll_ts_str,
                )
                self._conn.commit()

        return {"action": "polled", "ok": False, "error": any_error, "pushed": pushed_count}


WEB_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Pearl Miner Telemetry Collector - 矿机集群实时监控中心</title>
<style>
  :root { --bg:#0b1117; --card:#131c26; --card2:#182433; --b:#243447; --txt:#e2e8f0; --mut:#94a3b8; --pri:#10b981; --cyan:#06b6d4; --warn:#f59e0b; --err:#ef4444; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font-family:"Segoe UI","Microsoft YaHei",sans-serif; padding:20px; }
  .wrap { max-width:1360px; margin:0 auto; }
  .top { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; margin-bottom:18px; }
  h1 { font-size:20px; color:#fff; }
  .sub { font-size:12px; color:var(--mut); margin-top:4px; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  input, button { background:var(--card2); color:var(--txt); border:1px solid var(--b); border-radius:8px; padding:7px 11px; font-size:13px; }
  button { cursor:pointer; }
  button.primary { background:var(--pri); color:#042016; font-weight:700; border-color:transparent; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--b); border-radius:12px; padding:14px 16px; }
  .card .lbl { font-size:12px; color:var(--mut); }
  .card .val { font-size:22px; font-weight:700; margin-top:6px; color:#fff; font-family:Consolas,monospace; }
  .grid { display:grid; grid-template-columns:380px 1fr; gap:14px; }
  @media(max-width:920px){ .grid{ grid-template-columns:1fr; } }
  .panel { background:var(--card); border:1px solid var(--b); border-radius:12px; padding:14px; }
  .panel h2 { font-size:15px; margin-bottom:10px; }
  .cli-item { padding:10px 12px; border:1px solid var(--b); background:var(--card2); border-radius:10px; margin-bottom:8px; cursor:pointer; }
  .cli-item.active { border-color:var(--pri); background:rgba(16,185,129,0.1); }
  .badge { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:600; }
  .on { background:rgba(16,185,129,0.18); color:#34d399; }
  .off { background:rgba(239,68,68,0.18); color:#f87171; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; margin:8px 0 12px; }
  th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--b); }
  th { color:var(--mut); font-weight:600; }
  .term { background:#060a0e; border:1px solid #1e2d3d; border-radius:10px; padding:10px; height:360px; overflow-y:auto; font-family:Consolas,"Cascadia Mono",monospace; font-size:12px; line-height:1.5; }
  .l-share { color:#34d399; }
  .l-hashrate { color:#38bdf8; }
  .l-warn { color:#fbbf24; }
  .l-error { color:#f87171; }
  .l-pool { color:#c084fc; }
  .l-info { color:#cbd5e1; }
  .mono { font-family:Consolas,monospace; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <h1>⚡ Pearl Miner Telemetry 集中监控后端</h1>
      <div class="sub" id="srv-meta">正在连接监控数据源...</div>
      <div class="sub" id="standby-meta" style="margin-top:4px;color:#34d399;">掉线告警备援：检查中...</div>
    </div>
    <div class="row">
      <input type="password" id="tok" placeholder="访问密钥 Token (若未设留空)" />
      <button class="primary" onclick="loadSummary()">立即刷新</button>
    </div>
  </div>

  <div class="cards">
    <div class="card"><div class="lbl">在线 / 总矿机客户端</div><div class="val" id="c-clients">0 / 0</div></div>
    <div class="card"><div class="lbl">真实内核总算力</div><div class="val" id="c-hash" style="color:var(--pri)">0.00 TH/s</div></div>
    <div class="card"><div class="lbl">GPU/设备总功耗</div><div class="val" id="c-power">0 W</div></div>
    <div class="card"><div class="lbl">累计份额 (验收率)</div><div class="val" id="c-shares">0 (100%)</div></div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>在线 / 离线矿机节点</h2>
      <div id="client-list">暂无客户端上报</div>
    </div>
    <div class="panel">
      <h2 id="det-title">选择一个客户端查看真实算力与内核日志</h2>
      <div id="det-metrics" style="font-size:13px;color:var(--mut);margin-bottom:8px;"></div>
      <table><thead><tr><th>设备</th><th>型号</th><th>实时算力</th><th>均值</th><th>温度</th><th>风扇</th><th>功耗</th></tr></thead><tbody id="det-devs"></tbody></table>
      <div class="term" id="det-term">等待日志数据...</div>
    </div>
  </div>
</div>
<script>
const qp = new URLSearchParams(location.search);
const tokEl = document.getElementById("tok");
try {
  const savedTok = localStorage.getItem("pearl_collector_token") || "";
  if (qp.get("token")) { tokEl.value = qp.get("token"); }
  else if (savedTok) { tokEl.value = savedTok; }
  tokEl.addEventListener("change", () => { try { localStorage.setItem("pearl_collector_token", tokEl.value.trim()); } catch (_) {} });
} catch (_) {
  if (qp.get("token")) tokEl.value = qp.get("token");
}
let activeId = "";
function hdrs() { const t = tokEl.value.trim(); return t ? {"X-Collector-Token": t} : {}; }
function esc(s) { return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
async function loadSummary() {
  try {
    const r = await fetch("/api/v1/clients", {headers: hdrs()});
    if (!r.ok) { document.getElementById("srv-meta").textContent = "鉴权失败或错误: HTTP " + r.status; return; }
    const d = await r.json();
    try {
      const mr = await fetch("/api/v1/monitor/status", {headers: hdrs()});
      if (mr.ok) {
        const ms = await mr.json();
        const sbEl = document.getElementById("standby-meta");
        if (ms.mode === "standby_active") {
          sbEl.style.color = "#fbbf24";
          sbEl.textContent = `🟠 群晖备援接管轮询中 (${ms.tracked_wallets}钱包/${ms.tracked_workers}矿机 · 上次 ${ms.last_poll_at || "--"})`;
        } else if (ms.mode === "paused_by_desktop") {
          sbEl.style.color = "#34d399";
          sbEl.textContent = `🟢 主桌面端(${ms.desktop_host})在线监控中 · 群晖热备待命 (${ms.seconds_since_desktop_heartbeat ?? 0}s/${ms.lease_timeout_sec}s)`;
        } else {
          sbEl.style.color = "#94a3b8";
          sbEl.textContent = "⚪ 掉线告警备援：未开启（可在 Windows 客户端「掉线告警 & 远控」开启同步）";
        }
      }
    } catch (_) {}
    document.getElementById("srv-meta").textContent = "最后刷新: " + d.server_time + " · 在线阈值 35 秒";
    document.getElementById("c-clients").textContent = d.clients_online + " / " + d.clients_total;
    document.getElementById("c-hash").textContent = d.total_real_hashrate_text;
    document.getElementById("c-power").textContent = Math.round(d.total_power_w) + " W";
    document.getElementById("c-shares").textContent = d.total_accepted_shares + " (" + d.overall_accept_rate_pct + "%)";
    const list = document.getElementById("client-list");
    if (!d.clients.length) { list.innerHTML = "<div style='color:var(--mut);font-size:13px;'>暂无矿机客户端上报数据</div>"; return; }
    if (!activeId || !d.clients.find(x => x.client_id === activeId)) activeId = d.clients[0].client_id;
    list.innerHTML = d.clients.map(c => `
      <div class="cli-item ${c.client_id === activeId ? 'active' : ''}" data-cid="${esc(c.client_id)}">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong>${esc(c.client_name)}</strong>
          <span class="badge ${c.is_online ? 'on' : 'off'}">${c.is_online ? '在线' : '离线 (' + c.seconds_ago + 's)'}</span>
        </div>
        <div class="mono" style="font-size:13px;color:var(--pri);margin-top:4px;">${esc(c.hashrate_raw_text)} · 均 ${c.session_avg_ths.toFixed(2)} TH/s</div>
        <div style="font-size:11.5px;color:var(--mut);margin-top:3px;">${esc(c.algorithm)} · 接收 ${c.accepted_shares} / 拒 ${c.rejected_shares} · ${esc(c.remote_ip)}</div>
      </div>`).join("");
    await loadDetail();
  } catch (e) { document.getElementById("srv-meta").textContent = "请求异常: " + e; }
}
document.getElementById("client-list").addEventListener("click", (ev) => {
  const card = ev.target.closest("[data-cid]");
  if (card && card.dataset.cid) selectClient(card.dataset.cid);
});
async function selectClient(id) { activeId = id; await loadSummary(); }
async function loadDetail() {
  if (!activeId) return;
  const r = await fetch("/api/v1/clients/" + encodeURIComponent(activeId) + "?limit=220", {headers: hdrs()});
  if (!r.ok) return;
  const {client: c, logs} = await r.json();
  document.getElementById("det-title").textContent = `${c.client_name} (${c.host_name || c.remote_ip}) · ${c.algorithm}`;
  document.getElementById("det-metrics").textContent =
    `实时: ${c.hashrate_raw_text} | 均值: ${c.session_avg_ths.toFixed(2)} TH/s | 5m均: ${c.avg_5m_ths.toFixed(2)} | 稳定度: ${c.stability_pct}% | 验收率: ${c.accept_rate_pct}% | 延迟: ${c.avg_latency_ms ?? '--'}ms | 矿池: ${c.pool_url}`;
  const devs = c.devices || [];
  document.getElementById("det-devs").innerHTML = devs.length
    ? devs.map(dv => `<tr><td class="mono">${esc(dv.id)}</td><td>${esc(dv.name)}</td><td class="mono" style="color:var(--pri)">${esc(dv.hashrate_text)}</td><td class="mono">${Number(dv.avg_hashrate_ths||0).toFixed(2)} TH/s</td><td>${dv.temp_c != null ? dv.temp_c + '℃' : '--'}</td><td>${dv.fan_pct != null ? dv.fan_pct + '%' : '--'}</td><td>${dv.power_w != null ? dv.power_w + 'W' : '--'}</td></tr>`).join("")
    : `<tr><td colspan="7" style="color:var(--mut)">暂无单卡明细</td></tr>`;
  const term = document.getElementById("det-term");
  const atBottom = term.scrollTop + term.clientHeight >= term.scrollHeight - 30;
  term.innerHTML = logs.map(l => `<div class="l-${esc(l.level)}">${esc(l.line)}</div>`).join("") || "<div style='color:var(--mut)'>暂无日志输出</div>";
  if (atBottom) term.scrollTop = term.scrollHeight;
}
loadSummary();
setInterval(loadSummary, 3000);
</script>
</body>
</html>
"""


def _make_handler_class(store: CollectorStore, token: str) -> type[BaseHTTPRequestHandler]:
    clean_token = (token or "").strip()

    class CollectorHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Collector-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _is_authorized(self, query_params: dict[str, list[str]], body_token: str = "") -> bool:
            if not clean_token:
                return True
            hdr = (self.headers.get("X-Collector-Token") or "").strip()
            qtok = (query_params.get("token") or [""])[0].strip()
            candidate = hdr or body_token or qtok
            return bool(candidate) and hmac.compare_digest(candidate, clean_token)

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Collector-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/":
                self._send_html(WEB_DASHBOARD_HTML)
                return

            if path == "/health":
                summary = store.list_clients()
                sb = store.get_standby_status()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "server_time": summary["server_time"],
                        "clients_online": summary["clients_online"],
                        "clients_total": summary["clients_total"],
                        "standby_mode": sb["mode"],
                        "auth_required": bool(clean_token),
                    },
                )
                return

            if not self._is_authorized(qs):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid or missing X-Collector-Token"})
                return

            if path == "/api/v1/monitor/status":
                self._send_json(HTTPStatus.OK, store.get_standby_status())
                return

            if path in ("/api/v1/clients", "/api/v1/summary"):
                self._send_json(HTTPStatus.OK, store.list_clients())
                return

            if path.startswith("/api/v1/clients/"):
                tail = unquote(path[len("/api/v1/clients/") :]).strip("/")
                if tail.endswith("/logs"):
                    cid = tail[: -len("/logs")].strip("/")
                else:
                    cid = tail
                try:
                    limit = max(10, min(800, int((qs.get("limit") or ["250"])[0])))
                except ValueError:
                    limit = 250
                try:
                    since_id = int((qs.get("since_id") or ["0"])[0])
                except ValueError:
                    since_id = 0
                detail = store.get_client_detail(cid, limit=limit, since_id=since_id)
                if not detail:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Client not found"})
                else:
                    self._send_json(HTTPStatus.OK, detail)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path not in (
                "/api/v1/ingest",
                "/api/v1/monitor/heartbeat",
                "/api/v1/monitor/release",
                "/api/v1/monitor/test",
            ):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length < 0 or length > 8 * 1024 * 1024:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"})
                    return
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
                if not isinstance(payload, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON object expected"})
                    return
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"Malformed JSON: {exc}"})
                return

            if not self._is_authorized(qs, body_token=str(payload.get("token") or "")):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid or missing X-Collector-Token"})
                return

            if parsed.path == "/api/v1/monitor/heartbeat":
                self._send_json(HTTPStatus.OK, store.receive_desktop_heartbeat(payload))
                return
            if parsed.path == "/api/v1/monitor/release":
                self._send_json(HTTPStatus.OK, store.release_desktop_lease())
                return
            if parsed.path == "/api/v1/monitor/test":
                ov_cfg = payload.get("alert_config") if isinstance(payload.get("alert_config"), dict) else None
                self._send_json(HTTPStatus.OK, store.send_standby_test(config_override=ov_cfg))
                return

            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            real_ip = (self.headers.get("X-Real-IP") or "").strip()
            remote_ip = forwarded or real_ip or (self.client_address[0] if self.client_address else "")
            res = store.ingest(payload, remote_ip=remote_ip)
            self._send_json(HTTPStatus.OK, res)

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if not self._is_authorized(qs):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid or missing X-Collector-Token"})
                return
            if parsed.path.startswith("/api/v1/clients/"):
                cid = unquote(parsed.path[len("/api/v1/clients/") :]).strip("/")
                store.delete_client(cid)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    return CollectorHandler


class CollectorServerController:
    """Manages an embedded background ThreadingHTTPServer for PearlSplit or CLI use."""

    def __init__(self, db_path: str | Path, enable_standby_monitor: bool = False) -> None:
        self.db_path = Path(db_path)
        self.enable_standby_monitor = bool(enable_standby_monitor)
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._store: CollectorStore | None = None
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self.host: str = "0.0.0.0"
        self.port: int = 8765
        self.token: str = ""
        self.last_error: str | None = None
        self.started_at: str | None = None

    def _standby_monitor_loop(self, store: CollectorStore) -> None:
        last_poll_ts = 0.0
        while not self._monitor_stop.is_set():
            try:
                st = store.get_standby_status()
                if st["mode"] == "standby_active":
                    interval = max(30, int(st.get("standby_poll_interval_sec") or 60))
                    if (time.time() - last_poll_ts) >= interval:
                        last_poll_ts = time.time()
                        store.run_standby_poll_once()
            except Exception:
                pass
            self._monitor_stop.wait(timeout=5.0)

    def start(self, host: str = "0.0.0.0", port: int = 8765, token: str = "") -> dict[str, Any]:
        with self._lock:
            if self._httpd is not None:
                return self.get_status()
            self.host = (host or "0.0.0.0").strip()
            self.port = int(port)
            self.token = (token or "").strip()
            self.last_error = None
            store = CollectorStore(self.db_path)
            handler_cls = _make_handler_class(store, self.token)
            try:
                httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
            except Exception as exc:
                store.close()
                self.last_error = f"启动服务失败 ({self.host}:{self.port}): {exc}"
                raise RuntimeError(self.last_error) from exc
            self._store = store
            self._httpd = httpd
            self.started_at = _now_str()
            self._thread = threading.Thread(target=httpd.serve_forever, name="PearlCollectorHTTP", daemon=True)
            self._thread.start()
            self._monitor_stop.clear()
            if self.enable_standby_monitor:
                self._monitor_thread = threading.Thread(
                    target=self._standby_monitor_loop,
                    args=(store,),
                    name="PearlStandbyMonitor",
                    daemon=True,
                )
                self._monitor_thread.start()
            return self.get_status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            httpd = self._httpd
            store = self._store
            thread = self._thread
            m_thread = self._monitor_thread
            self._httpd = None
            self._store = None
            self._thread = None
            self._monitor_thread = None
            self.started_at = None
        self._monitor_stop.set()
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if m_thread is not None and m_thread.is_alive():
            m_thread.join(timeout=2)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        return self.get_status()

    def is_running(self) -> bool:
        return self._httpd is not None

    def get_store(self) -> CollectorStore | None:
        return self._store

    def get_status(self) -> dict[str, Any]:
        lan_ips = get_lan_ips()
        urls = [f"http://{ip}:{self.port}" for ip in lan_ips]
        return {
            "running": self._httpd is not None,
            "host": self.host,
            "port": self.port,
            "token_enabled": bool(self.token),
            "started_at": self.started_at,
            "last_error": self.last_error,
            "local_url": f"http://127.0.0.1:{self.port}",
            "lan_urls": urls,
        }


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pearl Miner Telemetry Collector Backend Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("PEARL_COLLECTOR_HOST", "0.0.0.0"),
        help="Bind IP (default: 0.0.0.0 or env PEARL_COLLECTOR_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("PEARL_COLLECTOR_PORT", 8765),
        help="Listen port (default: 8765 or env PEARL_COLLECTOR_PORT)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PEARL_COLLECTOR_TOKEN", ""),
        help="Optional auth token (or env PEARL_COLLECTOR_TOKEN)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("PEARL_COLLECTOR_DB", "collector_telemetry.db"),
        help="SQLite DB file path (default: collector_telemetry.db or env PEARL_COLLECTOR_DB)",
    )
    return parser


def run_cli() -> None:
    args = build_cli_parser().parse_args()

    ctrl = CollectorServerController(args.db, enable_standby_monitor=True)
    status = ctrl.start(host=args.host, port=args.port, token=args.token)
    print(f"[{_now_str()}] Pearl Collector Backend Server listening on {args.host}:{args.port}", flush=True)
    for u in status["lan_urls"]:
        print(f"  LAN URL: {u}", flush=True)
    print(f"  Local UI: {status['local_url']}", flush=True)
    print(f"  SQLite DB: {ctrl.db_path}", flush=True)
    print(f"  Token Auth: {'ENABLED' if args.token else 'DISABLED'}", flush=True)

    stop_event = threading.Event()

    def _handle_stop(signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[{_now_str()}] Shutting down collector server...", flush=True)
        ctrl.stop()


if __name__ == "__main__":
    run_cli()
