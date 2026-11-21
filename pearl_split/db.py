"""SQLite persistence layer for wallets, members, prefixes, offline worker cache, and settlements."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .alert_engine import normalize_alert_config
from .calculator import (
    DEFAULT_FEE_PIPELINE_STEPS,
    PIPELINE_PRESETS,
    MemberInfo,
    PrefixRule,
    WorkerBinding,
    allocate_exact_amounts,
    classify_worker,
    compute_worker_delta,
    normalize_fee_payload,
    normalize_pipeline_steps,
    normalize_wallet_type,
    summarize_fees_and_rates,
)
from .scraper import PoolDashboardData, extract_wallet_address, parse_hashrate_ths

DEFAULT_SETTINGS = {
    "auto_poll_enabled": "1",
    "auto_poll_interval_sec": "60",
    "minimize_to_tray": "1",
    "active_wallet_id": "",
    "show_local_miner_panel": "0",
    "local_bat_path": "",
    "local_log_path": "",
    "telemetry_push_enabled": "0",
    "telemetry_client_name": "",
    "collector_mode": "local",
    "collector_server_url": "http://127.0.0.1:8765",
    "collector_token": "",
    "local_collector_enabled": "0",
    "local_collector_port": "8765",
    "miner_kernel_dir": "",
    "miner_kernel_bat_path": "",
    "miner_github_repo": "doktor83/SRBMiner-Multi",
    "miner_github_proxy": "",
    "dust_seeds_json": "{}",
    "fee_pipeline_templates_json": json.dumps(DEFAULT_FEE_PIPELINE_STEPS, ensure_ascii=False),
    "alert_enabled": "0",
    "alert_provider": "pushplus",
    "alert_pushplus_token": "",
    "alert_pushplus_topic": "",
    "alert_pushplus_channel": "wechat",
    "alert_wxpusher_app_token": "",
    "alert_wxpusher_topic_ids": "",
    "alert_wxpusher_uids": "",
    "alert_offline_confirm_polls": "2",
    "alert_notify_recovery": "1",
    "alert_include_password": "1",
    "alert_poll_fail_minutes": "30",
    "alert_standby_sync_enabled": "0",
    "alert_standby_url": "",
    "alert_standby_token": "",
    "alert_desktop_lease_sec": "180",
    "alert_standby_poll_sec": "60",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Thread-safe SQLite wrapper for the Pearl Split desktop app."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._init_schema()
        self._seed_defaults()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT UNIQUE NOT NULL,
                    alias TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_fetched_at TEXT,
                    last_overview_json TEXT
                );

                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color TEXT NOT NULL DEFAULT '#10b981',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS member_prefixes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                    prefix TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_name TEXT NOT NULL COLLATE NOCASE,
                    wallet_id INTEGER REFERENCES wallets(id) ON DELETE CASCADE,
                    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    UNIQUE(worker_name, wallet_id)
                );

                CREATE TABLE IF NOT EXISTS worker_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    worker_name TEXT NOT NULL,
                    last_shares INTEGER NOT NULL DEFAULT 0,
                    peak_shares INTEGER NOT NULL DEFAULT 0,
                    current_hashrate TEXT NOT NULL DEFAULT '--',
                    avg_1h_hashrate TEXT NOT NULL DEFAULT '--',
                    avg_6h_hashrate TEXT NOT NULL DEFAULT '--',
                    avg_24h_hashrate TEXT NOT NULL DEFAULT '--',
                    last_seen_online_at TEXT NOT NULL,
                    last_pool_seen_text TEXT NOT NULL DEFAULT '',
                    is_currently_online INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'live',
                    UNIQUE(wallet_id, worker_name)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    checkpoint_time TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'withdrawal',
                    label TEXT NOT NULL DEFAULT '',
                    prl_amount REAL NOT NULL DEFAULT 0.0,
                    cny_amount REAL NOT NULL DEFAULT 0.0,
                    tx_hash TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    prev_checkpoint_id INTEGER REFERENCES checkpoints(id) ON DELETE SET NULL,
                    total_delta_shares INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoint_workers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
                    worker_name TEXT NOT NULL,
                    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
                    member_name_snapshot TEXT NOT NULL DEFAULT '未分配',
                    prev_shares INTEGER NOT NULL DEFAULT 0,
                    curr_shares INTEGER NOT NULL DEFAULT 0,
                    delta_shares INTEGER NOT NULL DEFAULT 0,
                    share_source TEXT NOT NULL DEFAULT 'live'
                );

                CREATE TABLE IF NOT EXISTS settlement_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
                    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
                    member_name TEXT NOT NULL,
                    member_color TEXT NOT NULL DEFAULT '#64748b',
                    delta_shares INTEGER NOT NULL DEFAULT 0,
                    ratio REAL NOT NULL DEFAULT 0.0,
                    prl_earned REAL NOT NULL DEFAULT 0.0,
                    cny_earned REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settlement_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT '',
                    prev_checkpoint_id INTEGER REFERENCES checkpoints(id) ON DELETE SET NULL,
                    checkpoint_time TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    prl_amount REAL NOT NULL DEFAULT 0.0,
                    cny_amount REAL NOT NULL DEFAULT 0.0,
                    notes TEXT NOT NULL DEFAULT '',
                    workers_json TEXT NOT NULL DEFAULT '[]',
                    offline_hint TEXT NOT NULL DEFAULT '',
                    fees_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exchange_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_id INTEGER REFERENCES wallets(id) ON DELETE SET NULL,
                    trade_time TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT '交易所',
                    from_asset TEXT NOT NULL DEFAULT 'PRL',
                    to_asset TEXT NOT NULL DEFAULT 'USDT',
                    source_amount REAL NOT NULL DEFAULT 0.0,
                    transfer_fee REAL NOT NULL DEFAULT 0.0,
                    sold_amount REAL NOT NULL DEFAULT 0.0,
                    unit_price REAL NOT NULL DEFAULT 0.0,
                    receive_gross REAL NOT NULL DEFAULT 0.0,
                    trade_fee REAL NOT NULL DEFAULT 0.0,
                    trade_fee_unit TEXT NOT NULL DEFAULT '',
                    receive_net REAL NOT NULL DEFAULT 0.0,
                    dust_delta REAL NOT NULL DEFAULT 0.0,
                    dust_asset TEXT NOT NULL DEFAULT '',
                    linked_checkpoint_id INTEGER REFERENCES checkpoints(id) ON DELETE SET NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_worker_cache_wallet
                    ON worker_cache(wallet_id);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_wallet_time
                    ON checkpoints(wallet_id, checkpoint_time DESC);
                CREATE INDEX IF NOT EXISTS idx_cp_workers_cp
                    ON checkpoint_workers(checkpoint_id);
                CREATE INDEX IF NOT EXISTS idx_allocs_cp
                    ON settlement_allocations(checkpoint_id);
                CREATE INDEX IF NOT EXISTS idx_drafts_wallet
                    ON settlement_drafts(wallet_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_trades_wallet
                    ON exchange_trades(wallet_id, trade_time DESC);
                CREATE INDEX IF NOT EXISTS idx_trades_cp
                    ON exchange_trades(linked_checkpoint_id);

                CREATE TABLE IF NOT EXISTS alert_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    wallet_id INTEGER,
                    wallet_alias TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT 'offline',
                    worker_names TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'pushplus',
                    source TEXT NOT NULL DEFAULT 'desktop',
                    ok INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_alert_logs_id
                    ON alert_logs(id DESC);
                """
            )
            existing_wc_cols = {
                r[1] for r in self._conn.execute("PRAGMA table_info(worker_cache)").fetchall()
            }
            had_alert_col = "offline_alert_sent" in existing_wc_cols

            self._ensure_column("wallets", "wallet_type", "TEXT NOT NULL DEFAULT 'official'")
            self._ensure_column("checkpoints", "fees_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("checkpoints", "pool_paid_snapshot", "REAL")
            self._ensure_column("settlement_drafts", "fees_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("exchange_trades", "venue", "TEXT NOT NULL DEFAULT '交易所'")
            self._ensure_column("exchange_trades", "from_asset", "TEXT NOT NULL DEFAULT 'PRL'")
            self._ensure_column("exchange_trades", "to_asset", "TEXT NOT NULL DEFAULT 'USDT'")
            self._ensure_column("exchange_trades", "transfer_fee", "REAL NOT NULL DEFAULT 0.0")

            self._ensure_column("worker_cache", "remote_tool", "TEXT NOT NULL DEFAULT 'ToDesk'")
            self._ensure_column("worker_cache", "remote_code", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("worker_cache", "remote_password", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("worker_cache", "remote_note", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("worker_cache", "alert_enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("worker_cache", "offline_alert_sent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("worker_cache", "consecutive_offline_polls", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("worker_cache", "last_alert_at", "TEXT NOT NULL DEFAULT ''")

            if not had_alert_col:
                # Avoid false alerts for rigs that were already offline before upgrading
                self._conn.execute(
                    "UPDATE worker_cache SET offline_alert_sent = 1 WHERE is_currently_online = 0"
                )
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, col_def: str) -> None:
        cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def _seed_defaults(self) -> None:
        with self._lock:
            for k, v in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def update_settings(self, updates: dict[str, Any]) -> dict[str, str]:
        with self._lock:
            for k, v in updates.items():
                self._conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(k), str(v)),
                )
            self._conn.commit()
            return self.get_settings()

    # ------------------------------------------------------------------
    # Wallets
    # ------------------------------------------------------------------
    def list_wallets(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wallets ORDER BY is_active DESC, id ASC"
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                overview = {}
                if d.get("last_overview_json"):
                    try:
                        overview = json.loads(d["last_overview_json"])
                    except json.JSONDecodeError:
                        overview = {}
                d["overview"] = overview
                d.pop("last_overview_json", None)
                counts = self._conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN is_currently_online = 1 THEN 1 ELSE 0 END) AS cached_online, "
                    "SUM(CASE WHEN is_currently_online = 0 THEN 1 ELSE 0 END) AS cached_offline, "
                    "COUNT(*) AS cached_total "
                    "FROM worker_cache WHERE wallet_id = ?",
                    (d["id"],),
                ).fetchone()
                d["cached_online"] = counts["cached_online"] or 0
                d["cached_offline"] = counts["cached_offline"] or 0
                d["cached_total"] = counts["cached_total"] or 0
                result.append(d)
            return result

    @staticmethod
    def _clean_wallet_type(wallet_type: str) -> str:
        return normalize_wallet_type(wallet_type)

    def add_wallet(
        self, address_or_url: str, alias: str = "", notes: str = "", wallet_type: str = "official"
    ) -> dict[str, Any]:
        addr = extract_wallet_address(address_or_url)
        clean_alias = (alias or "").strip() or f"钱包 {addr[:6]}...{addr[-4:]}"
        wtype = self._clean_wallet_type(wallet_type)
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM wallets WHERE address = ?", (addr,)
            ).fetchone()
            if existing:
                raise ValueError("该钱包地址已存在")
            cur = self._conn.execute(
                "INSERT INTO wallets (address, alias, notes, wallet_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (addr, clean_alias, (notes or "").strip(), wtype, _now()),
            )
            wallet_id = cur.lastrowid
            self._conn.commit()
            self.update_settings({"active_wallet_id": str(wallet_id)})
            return next(w for w in self.list_wallets() if w["id"] == wallet_id)

    def update_wallet(
        self,
        wallet_id: int,
        alias: str,
        notes: str = "",
        is_active: int = 1,
        wallet_type: str | None = None,
    ) -> None:
        clean_alias = (alias or "").strip()
        if not clean_alias:
            raise ValueError("钱包别名不能为空")
        with self._lock:
            if wallet_type is None:
                self._conn.execute(
                    "UPDATE wallets SET alias = ?, notes = ?, is_active = ? WHERE id = ?",
                    (clean_alias, (notes or "").strip(), 1 if is_active else 0, wallet_id),
                )
            else:
                wtype = self._clean_wallet_type(wallet_type)
                self._conn.execute(
                    "UPDATE wallets SET alias = ?, notes = ?, is_active = ?, wallet_type = ? WHERE id = ?",
                    (clean_alias, (notes or "").strip(), 1 if is_active else 0, wtype, wallet_id),
                )
            self._conn.commit()

    def delete_wallet(self, wallet_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))
            self._conn.commit()
            wallets = self.list_wallets()
            self.update_settings({"active_wallet_id": str(wallets[0]["id"]) if wallets else ""})

    # ------------------------------------------------------------------
    # Members, Prefixes & Manual Worker Bindings
    # ------------------------------------------------------------------
    def _load_classification_context(
        self,
    ) -> tuple[dict[int, MemberInfo], list[PrefixRule], list[WorkerBinding]]:
        with self._lock:
            m_rows = self._conn.execute("SELECT id, name, color FROM members ORDER BY id ASC").fetchall()
            members = {
                r["id"]: MemberInfo(id=r["id"], name=r["name"], color=r["color"])
                for r in m_rows
            }
            p_rows = self._conn.execute("SELECT prefix, member_id FROM member_prefixes").fetchall()
            prefixes = [PrefixRule(prefix=r["prefix"], member_id=r["member_id"]) for r in p_rows]
            b_rows = self._conn.execute(
                "SELECT worker_name, member_id, wallet_id FROM worker_bindings"
            ).fetchall()
            bindings = [
                WorkerBinding(
                    worker_name=r["worker_name"],
                    member_id=r["member_id"],
                    wallet_id=r["wallet_id"],
                )
                for r in b_rows
            ]
            return members, prefixes, bindings

    def list_members(self) -> list[dict[str, Any]]:
        with self._lock:
            m_rows = self._conn.execute("SELECT * FROM members ORDER BY id ASC").fetchall()
            p_rows = self._conn.execute(
                "SELECT id, member_id, prefix FROM member_prefixes ORDER BY LENGTH(prefix) DESC, prefix ASC"
            ).fetchall()
            b_rows = self._conn.execute(
                "SELECT b.id, b.worker_name, b.wallet_id, b.member_id, w.alias AS wallet_alias "
                "FROM worker_bindings b LEFT JOIN wallets w ON b.wallet_id = w.id "
                "ORDER BY b.worker_name ASC"
            ).fetchall()

            prefixes_by_m: dict[int, list[dict[str, Any]]] = {}
            for p in p_rows:
                prefixes_by_m.setdefault(p["member_id"], []).append(dict(p))

            bindings_by_m: dict[int, list[dict[str, Any]]] = {}
            for b in b_rows:
                bindings_by_m.setdefault(b["member_id"], []).append(dict(b))

            out = []
            for m in m_rows:
                d = dict(m)
                d["prefixes"] = prefixes_by_m.get(m["id"], [])
                d["bindings"] = bindings_by_m.get(m["id"], [])
                out.append(d)
            return out

    def create_member(self, name: str, color: str = "#10b981", prefixes: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("成员名称不能为空")
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM members WHERE LOWER(name) = LOWER(?)", (clean_name,)
            ).fetchone()
            if dup:
                raise ValueError(f"成员 {clean_name} 已存在")
            now = _now()
            cur = self._conn.execute(
                "INSERT INTO members (name, color, notes, created_at) VALUES (?, ?, ?, ?)",
                (clean_name, color or "#10b981", (notes or "").strip(), now),
            )
            member_id = cur.lastrowid
            for p in (prefixes or []):
                cp = p.strip()
                if cp:
                    self._add_prefix_unlocked(member_id, cp)
            self._conn.commit()
            return next(m for m in self.list_members() if m["id"] == member_id)

    def update_member(self, member_id: int, name: str, color: str, notes: str = "") -> None:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("成员名称不能为空")
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM members WHERE LOWER(name) = LOWER(?) AND id != ?",
                (clean_name, member_id),
            ).fetchone()
            if dup:
                raise ValueError(f"成员名称 {clean_name} 已被占用")
            self._conn.execute(
                "UPDATE members SET name = ?, color = ?, notes = ? WHERE id = ?",
                (clean_name, color or "#10b981", (notes or "").strip(), member_id),
            )
            self._conn.commit()

    def delete_member(self, member_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
            self._conn.commit()

    def _add_prefix_unlocked(self, member_id: int, prefix: str) -> None:
        cp = (prefix or "").strip()
        if not cp:
            raise ValueError("前缀不能为空")
        existing = self._conn.execute(
            "SELECT mp.id, m.name AS owner FROM member_prefixes mp "
            "JOIN members m ON mp.member_id = m.id "
            "WHERE LOWER(mp.prefix) = LOWER(?)",
            (cp,),
        ).fetchone()
        if existing:
            raise ValueError(f"前缀 '{cp}' 已绑定给成员 {existing['owner']}")
        self._conn.execute(
            "INSERT INTO member_prefixes (member_id, prefix, created_at) VALUES (?, ?, ?)",
            (member_id, cp, _now()),
        )

    def add_prefix(self, member_id: int, prefix: str) -> None:
        with self._lock:
            self._add_prefix_unlocked(member_id, prefix)
            self._conn.commit()

    def delete_prefix(self, prefix_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM member_prefixes WHERE id = ?", (prefix_id,))
            self._conn.commit()

    def bind_worker(self, worker_name: str, member_id: int, wallet_id: int | None = None) -> None:
        clean_worker = (worker_name or "").strip()
        if not clean_worker:
            raise ValueError("矿机名不能为空")
        with self._lock:
            if wallet_id is None:
                self._conn.execute(
                    "DELETE FROM worker_bindings WHERE LOWER(worker_name) = LOWER(?) AND wallet_id IS NULL",
                    (clean_worker,),
                )
            else:
                self._conn.execute(
                    "DELETE FROM worker_bindings WHERE LOWER(worker_name) = LOWER(?) AND wallet_id = ?",
                    (clean_worker, wallet_id),
                )
            self._conn.execute(
                "INSERT INTO worker_bindings (worker_name, wallet_id, member_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (clean_worker, wallet_id, member_id, _now()),
            )
            self._conn.commit()

    def delete_worker_binding(self, binding_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM worker_bindings WHERE id = ?", (binding_id,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Live Snapshot Ingestion & Offline Worker Cache
    # ------------------------------------------------------------------
    def save_pool_fetch(self, wallet_id: int, data: PoolDashboardData) -> dict[str, Any]:
        """Persist scraped pool overview & update worker high-water / offline cache."""
        with self._lock:
            prev_ov_row = self._conn.execute(
                "SELECT last_overview_json FROM wallets WHERE id = ?",
                (wallet_id,),
            ).fetchone()
            prev_payments: list[dict[str, Any]] = []
            prev_rewards: list[dict[str, Any]] = []
            prev_est_daily: float = 0.0
            if prev_ov_row and prev_ov_row["last_overview_json"]:
                try:
                    prev_ov = json.loads(prev_ov_row["last_overview_json"])
                    if isinstance(prev_ov.get("payments"), list):
                        prev_payments = prev_ov["payments"]
                    if isinstance(prev_ov.get("block_rewards"), list):
                        prev_rewards = prev_ov["block_rewards"]
                    prev_est_daily = float(prev_ov.get("est_daily_prl") or 0.0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    prev_payments = []
                    prev_rewards = []

            merged_payments_map: dict[str, dict[str, Any]] = {}
            for p_dict in prev_payments:
                if not isinstance(p_dict, dict):
                    continue
                key = str(p_dict.get("tx_hash") or f"{p_dict.get('paid_at')}_{p_dict.get('amount_prl')}")
                merged_payments_map[key] = p_dict
            for p in data.payments:
                p_dict = p.to_dict()
                key = str(p_dict.get("tx_hash") or f"{p_dict.get('paid_at')}_{p_dict.get('amount_prl')}")
                merged_payments_map[key] = p_dict

            merged_payments = sorted(
                merged_payments_map.values(),
                key=lambda x: str(x.get("paid_at") or ""),
                reverse=True,
            )

            merged_rewards_map: dict[str, dict[str, Any]] = {}
            for b_dict in prev_rewards:
                if not isinstance(b_dict, dict):
                    continue
                key = f"{b_dict.get('height')}_{b_dict.get('found_at')}"
                merged_rewards_map[key] = b_dict
            for b in data.block_rewards:
                b_dict = b.to_dict()
                key = f"{b_dict.get('height')}_{b_dict.get('found_at')}"
                merged_rewards_map[key] = b_dict

            merged_rewards = sorted(
                merged_rewards_map.values(),
                key=lambda x: (str(x.get("found_at") or ""), int(x.get("height") or 0)),
                reverse=True,
            )[:120]

            overview = {
                "online_count": data.online_count,
                "offline_count": data.offline_count,
                "live_hashrate": data.live_hashrate,
                "avg_24h_hashrate": data.avg_24h_hashrate,
                "prl_price_cny": data.prl_price_cny,
                "est_daily_prl": data.est_daily_prl if data.est_daily_prl > 0 else prev_est_daily,
                "unpaid_prl": data.unpaid_prl,
                "immature_prl": data.immature_prl,
                "reward_24h_prl": data.reward_24h_prl,
                "total_paid_prl": data.total_paid_prl,
                "payments": merged_payments,
                "block_rewards": merged_rewards,
            }
            self._conn.execute(
                "UPDATE wallets SET last_fetched_at = ?, last_overview_json = ? WHERE id = ?",
                (data.fetched_at, json.dumps(overview, ensure_ascii=False), wallet_id),
            )

            existing_rows = self._conn.execute(
                "SELECT worker_name, last_shares, peak_shares, is_currently_online, source, "
                "alert_enabled, offline_alert_sent, consecutive_offline_polls, "
                "remote_tool, remote_code, remote_password, remote_note, last_seen_online_at "
                "FROM worker_cache WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchall()
            existing_map = {r["worker_name"]: dict(r) for r in existing_rows}
            members, prefixes, bindings = self._load_classification_context()

            seen_online_names: set[str] = set()
            newly_woken_workers: list[dict[str, Any]] = []
            recovered_alert_workers: list[dict[str, Any]] = []
            offline_candidates: list[dict[str, Any]] = []

            for w in data.workers:
                is_online = 1 if w.status == "online" else 0
                if is_online:
                    seen_online_names.add(w.name)
                prev = existing_map.get(w.name)
                prev_peak = prev["peak_shares"] if prev else 0
                new_peak = max(prev_peak, w.shares)

                m_obj, _ = classify_worker(
                    w.name, members, prefixes, bindings, wallet_id=wallet_id
                )
                m_name = m_obj.name if m_obj else "未分配"

                if prev and prev["is_currently_online"] == 0 and is_online == 1:
                    newly_woken_workers.append(
                        {
                            "worker_name": w.name,
                            "prev_shares": prev["last_shares"],
                            "new_shares": w.shares,
                        }
                    )
                if (
                    prev
                    and is_online == 1
                    and int(prev.get("offline_alert_sent") or 0) == 1
                    and int(prev.get("alert_enabled") if prev.get("alert_enabled") is not None else 1) == 1
                ):
                    recovered_alert_workers.append(
                        {
                            "worker_name": w.name,
                            "member_name": m_name,
                            "current_hashrate": w.current_hashrate,
                            "shares": w.shares,
                            "last_seen_online_at": prev.get("last_seen_online_at") or "",
                        }
                    )

                self._conn.execute(
                    """
                    INSERT INTO worker_cache (
                        wallet_id, worker_name, last_shares, peak_shares,
                        current_hashrate, avg_1h_hashrate, avg_6h_hashrate, avg_24h_hashrate,
                        last_seen_online_at, last_pool_seen_text, is_currently_online, source,
                        offline_alert_sent, consecutive_offline_polls
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', 0, ?)
                    ON CONFLICT(wallet_id, worker_name) DO UPDATE SET
                        last_shares = excluded.last_shares,
                        peak_shares = MAX(worker_cache.peak_shares, excluded.last_shares),
                        current_hashrate = excluded.current_hashrate,
                        avg_1h_hashrate = excluded.avg_1h_hashrate,
                        avg_6h_hashrate = excluded.avg_6h_hashrate,
                        avg_24h_hashrate = excluded.avg_24h_hashrate,
                        last_seen_online_at = excluded.last_seen_online_at,
                        last_pool_seen_text = excluded.last_pool_seen_text,
                        is_currently_online = excluded.is_currently_online,
                        source = 'live',
                        offline_alert_sent = CASE
                            WHEN excluded.is_currently_online = 1 THEN 0
                            ELSE worker_cache.offline_alert_sent
                        END,
                        consecutive_offline_polls = CASE
                            WHEN excluded.is_currently_online = 1 THEN 0
                            ELSE worker_cache.consecutive_offline_polls
                        END
                    """,
                    (
                        wallet_id,
                        w.name,
                        w.shares,
                        new_peak,
                        w.current_hashrate,
                        w.avg_1h_hashrate,
                        w.avg_6h_hashrate,
                        w.avg_24h_hashrate,
                        data.fetched_at,
                        w.last_seen_text,
                        is_online,
                        0 if is_online else 1,
                    ),
                )

            # Mark previously seen workers that are absent from online list as offline-cached
            # (preserving their last_shares and last_seen_online_at!)
            for w_name, prev_info in existing_map.items():
                if w_name not in seen_online_names:
                    offline_hr = "离线(手动)" if prev_info.get("source") == "manual" else "离线"
                    next_streak = int(prev_info.get("consecutive_offline_polls") or 0) + 1
                    self._conn.execute(
                        "UPDATE worker_cache SET is_currently_online = 0, current_hashrate = ?, "
                        "consecutive_offline_polls = ? WHERE wallet_id = ? AND worker_name = ?",
                        (offline_hr, next_streak, wallet_id, w_name),
                    )
                    m_obj, _ = classify_worker(
                        w_name, members, prefixes, bindings, wallet_id=wallet_id
                    )
                    offline_candidates.append(
                        {
                            "worker_name": w_name,
                            "member_name": m_obj.name if m_obj else "未分配",
                            "last_shares": int(prev_info.get("last_shares") or 0),
                            "last_seen_online_at": prev_info.get("last_seen_online_at") or "",
                            "remote_tool": prev_info.get("remote_tool") or "ToDesk",
                            "remote_code": prev_info.get("remote_code") or "",
                            "remote_password": prev_info.get("remote_password") or "",
                            "remote_note": prev_info.get("remote_note") or "",
                            "alert_enabled": int(
                                prev_info.get("alert_enabled")
                                if prev_info.get("alert_enabled") is not None
                                else 1
                            ),
                            "offline_alert_sent": int(prev_info.get("offline_alert_sent") or 0),
                            "consecutive_offline_polls": next_streak,
                        }
                    )

            self._conn.commit()
            return {
                "newly_woken": newly_woken_workers,
                "offline_candidates": offline_candidates,
                "recovered_alert_workers": recovered_alert_workers,
            }

    def upsert_manual_worker_cache(
        self, wallet_id: int, worker_name: str, shares: int
    ) -> None:
        """Manually insert or update a cached offline worker's share count."""
        clean_name = (worker_name or "").strip()
        if not clean_name:
            raise ValueError("矿机名称不能为空")
        val = max(0, int(shares))
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO worker_cache (
                    wallet_id, worker_name, last_shares, peak_shares,
                    current_hashrate, avg_1h_hashrate, avg_6h_hashrate, avg_24h_hashrate,
                    last_seen_online_at, last_pool_seen_text, is_currently_online, source,
                    offline_alert_sent
                ) VALUES (?, ?, ?, ?, '离线(手动)', '--', '--', '--', ?, '手动录入', 0, 'manual', 1)
                ON CONFLICT(wallet_id, worker_name) DO UPDATE SET
                    last_shares = excluded.last_shares,
                    peak_shares = MAX(worker_cache.peak_shares, excluded.last_shares),
                    last_seen_online_at = excluded.last_seen_online_at,
                    last_pool_seen_text = '手动录入',
                    source = 'manual'
                """,
                (wallet_id, clean_name, val, val, now),
            )
            self._conn.commit()

    def _ensure_worker_cache_if_missing(
        self, wallet_id: int, worker_name: str, shares: int, seen_at: str
    ) -> None:
        """Seed worker_cache only if the worker is not already tracked for this wallet."""
        clean_name = (worker_name or "").strip()
        if not clean_name:
            return
        val = max(0, int(shares))
        self._conn.execute(
            """
            INSERT OR IGNORE INTO worker_cache (
                wallet_id, worker_name, last_shares, peak_shares,
                current_hashrate, avg_1h_hashrate, avg_6h_hashrate, avg_24h_hashrate,
                last_seen_online_at, last_pool_seen_text, is_currently_online, source,
                offline_alert_sent
            ) VALUES (?, ?, ?, ?, '离线(历史基准)', '--', '--', '--', ?, '历史基准导入', 0, 'manual', 1)
            """,
            (wallet_id, clean_name, val, val, seen_at or _now()),
        )

    def delete_cached_worker(self, wallet_id: int, worker_name: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM worker_cache WHERE wallet_id = ? AND worker_name = ?",
                (wallet_id, worker_name),
            )
            self._conn.commit()

    def get_wallet_workers(self, wallet_id: int) -> list[dict[str, Any]]:
        """Return all online + locally-cached offline workers for a wallet, classified by member."""
        with self._lock:
            members, prefixes, bindings = self._load_classification_context()
            latest_cp = self._conn.execute(
                "SELECT id FROM checkpoints WHERE wallet_id = ? "
                "ORDER BY checkpoint_time DESC, id DESC LIMIT 1",
                (wallet_id,),
            ).fetchone()
            baseline_map: dict[str, int] = {}
            if latest_cp:
                cp_rows = self._conn.execute(
                    "SELECT worker_name, curr_shares FROM checkpoint_workers WHERE checkpoint_id = ?",
                    (latest_cp["id"],),
                ).fetchall()
                baseline_map = {r["worker_name"]: int(r["curr_shares"]) for r in cp_rows}

            rows = self._conn.execute(
                "SELECT * FROM worker_cache WHERE wallet_id = ? "
                "ORDER BY is_currently_online DESC, last_shares DESC, worker_name ASC",
                (wallet_id,),
            ).fetchall()

            result = []
            for r in rows:
                d = dict(r)
                member, match_rule = classify_worker(
                    d["worker_name"], members, prefixes, bindings, wallet_id=wallet_id
                )
                base_s = baseline_map.get(d["worker_name"], 0)
                delta_s, reset_flag = compute_worker_delta(base_s, int(d["last_shares"]))
                d["member_id"] = member.id if member else None
                d["member_name"] = member.name if member else "未分配"
                d["member_color"] = member.color if member else "#64748b"
                d["match_rule"] = match_rule
                d["baseline_shares"] = base_s
                d["round_delta_shares"] = delta_s
                d["reset_detected"] = reset_flag
                d["live_hashrate_ths"] = (
                    parse_hashrate_ths(d["current_hashrate"]) if d["is_currently_online"] else 0.0
                )
                d["avg_24h_hashrate_ths"] = parse_hashrate_ths(d["avg_24h_hashrate"])
                result.append(d)
            return result

    # ------------------------------------------------------------------
    # Checkpoints & Settlements
    # ------------------------------------------------------------------
    def _get_wallet_type_unlocked(self, wallet_id: int) -> str:
        row = self._conn.execute(
            "SELECT wallet_type FROM wallets WHERE id = ?", (int(wallet_id),)
        ).fetchone()
        return self._clean_wallet_type(row["wallet_type"] if row else "official")

    @staticmethod
    def _parse_fees_dict(raw_json: str | None, wallet_type: str = "official") -> dict[str, Any]:
        try:
            loaded = json.loads(raw_json or "{}")
        except (json.JSONDecodeError, TypeError):
            loaded = {}
        return normalize_fee_payload(loaded if isinstance(loaded, dict) else {}, wallet_type=wallet_type)

    def _enrich_checkpoint_row(self, row: sqlite3.Row, wallet_type: str) -> dict[str, Any]:
        d = dict(row)
        wtype = self._clean_wallet_type(d.get("wallet_type") or wallet_type)
        fees = self._parse_fees_dict(d.get("fees_json"), wallet_type=wtype)
        d["wallet_type"] = wtype
        d["fees"] = fees
        d["fee_summary"] = summarize_fees_and_rates(
            fees,
            cny_amount=float(d.get("cny_amount") or 0.0),
            prl_amount=float(d.get("prl_amount") or 0.0),
        )
        return d

    def _sync_checkpoint_trades_unlocked(self, checkpoint_id: int, linked_trade_ids: list[int]) -> None:
        self._conn.execute(
            "UPDATE exchange_trades SET linked_checkpoint_id = NULL WHERE linked_checkpoint_id = ?",
            (int(checkpoint_id),),
        )
        if linked_trade_ids:
            placeholders = ",".join("?" for _ in linked_trade_ids)
            self._conn.execute(
                f"UPDATE exchange_trades SET linked_checkpoint_id = ? WHERE id IN ({placeholders})",
                (int(checkpoint_id), *[int(x) for x in linked_trade_ids]),
            )

    def list_checkpoints(self, wallet_id: int) -> list[dict[str, Any]]:
        with self._lock:
            wtype = self._get_wallet_type_unlocked(wallet_id)
            rows = self._conn.execute(
                "SELECT * FROM checkpoints WHERE wallet_id = ? "
                "ORDER BY checkpoint_time DESC, id DESC",
                (wallet_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = self._enrich_checkpoint_row(r, wtype)
                allocs = self._conn.execute(
                    "SELECT * FROM settlement_allocations WHERE checkpoint_id = ? "
                    "ORDER BY delta_shares DESC, member_name ASC",
                    (d["id"],),
                ).fetchall()
                d["allocations"] = [dict(a) for a in allocs]
                workers = self._conn.execute(
                    "SELECT * FROM checkpoint_workers WHERE checkpoint_id = ? "
                    "ORDER BY delta_shares DESC, curr_shares DESC, worker_name ASC",
                    (d["id"],),
                ).fetchall()
                d["workers"] = [dict(w) for w in workers]
                out.append(d)
            return out

    def preview_settlement(
        self,
        wallet_id: int,
        prev_checkpoint_id: int | None = None,
    ) -> dict[str, Any]:
        """Build a live settlement draft comparing current/cached worker shares against baseline."""
        with self._lock:
            members, prefixes, bindings = self._load_classification_context()
            cached_workers = {
                w["worker_name"]: w for w in self.get_wallet_workers(wallet_id)
            }

            prev_map: dict[str, int] = {}
            prev_cp_info: dict[str, Any] | None = None
            if prev_checkpoint_id is not None:
                cp_row = self._conn.execute(
                    "SELECT * FROM checkpoints WHERE id = ? AND wallet_id = ?",
                    (prev_checkpoint_id, wallet_id),
                ).fetchone()
                if cp_row:
                    prev_cp_info = dict(cp_row)
                    pw_rows = self._conn.execute(
                        "SELECT worker_name, curr_shares FROM checkpoint_workers WHERE checkpoint_id = ?",
                        (prev_checkpoint_id,),
                    ).fetchall()
                    prev_map = {r["worker_name"]: int(r["curr_shares"]) for r in pw_rows}

            all_names = sorted(
                set(cached_workers.keys()) | set(prev_map.keys()),
                key=lambda n: (
                    0 if (cached_workers.get(n, {}).get("is_currently_online") == 1) else 1,
                    -(cached_workers.get(n, {}).get("last_shares", prev_map.get(n, 0))),
                    n.lower(),
                ),
            )

            draft_workers = []
            for name in all_names:
                cw = cached_workers.get(name)
                prev_shares = prev_map.get(name, 0)
                if cw:
                    curr_shares = int(cw["last_shares"])
                    if cw["is_currently_online"]:
                        source = "live"
                    elif cw["source"] == "manual":
                        source = "manual"
                    else:
                        source = "offline_cache"
                    last_seen_at = cw["last_seen_online_at"]
                    is_online = bool(cw["is_currently_online"])
                    hashrate = cw["current_hashrate"]
                else:
                    # Existed in previous checkpoint, never seen since
                    curr_shares = prev_shares
                    source = "offline_missing"
                    last_seen_at = prev_cp_info["checkpoint_time"] if prev_cp_info else ""
                    is_online = False
                    hashrate = "离线(历史基准)"

                member, match_rule = classify_worker(
                    name, members, prefixes, bindings, wallet_id=wallet_id
                )
                delta, reset_detected = compute_worker_delta(prev_shares, curr_shares)

                draft_workers.append(
                    {
                        "worker_name": name,
                        "member_id": member.id if member else None,
                        "member_name": member.name if member else "未分配",
                        "member_color": member.color if member else "#64748b",
                        "match_rule": match_rule,
                        "prev_shares": prev_shares,
                        "curr_shares": curr_shares,
                        "delta_shares": delta,
                        "reset_detected": reset_detected,
                        "share_source": source,
                        "is_online": is_online,
                        "last_seen_at": last_seen_at,
                        "current_hashrate": hashrate,
                    }
                )

            return {
                "prev_checkpoint": prev_cp_info,
                "workers": draft_workers,
                "now": _now(),
            }

    def save_checkpoint_and_settle(
        self,
        wallet_id: int,
        kind: str,
        checkpoint_time: str,
        label: str,
        prl_amount: float,
        cny_amount: float,
        tx_hash: str,
        notes: str,
        prev_checkpoint_id: int | None,
        worker_entries: list[dict[str, Any]],
        fees: dict[str, Any] | None = None,
        pool_paid_snapshot: float | None = None,
    ) -> dict[str, Any]:
        """Persist a baseline snapshot or withdrawal settlement and exact member allocations."""
        clean_time = (checkpoint_time or "").strip() or _now()
        clean_kind = "baseline" if kind == "baseline" else "withdrawal"
        clean_pool_snap = (
            round(max(0.0, float(pool_paid_snapshot)), 6)
            if pool_paid_snapshot is not None
            else None
        )

        with self._lock:
            members, prefixes, bindings = self._load_classification_context()
            now = _now()
            wtype = self._get_wallet_type_unlocked(wallet_id)
            clean_fees = normalize_fee_payload(fees, wallet_type=wtype) if clean_kind == "withdrawal" else {}
            fees_json = json.dumps(clean_fees, ensure_ascii=False) if clean_fees else "{}"

            # Compute per-worker deltas and member aggregates
            processed_workers: list[dict[str, Any]] = []
            member_agg: dict[str, dict[str, Any]] = {}
            total_delta = 0

            for entry in worker_entries:
                w_name = (entry.get("worker_name") or "").strip()
                if not w_name:
                    continue
                prev_s = max(0, int(entry.get("prev_shares", 0)))
                curr_s = max(0, int(entry.get("curr_shares", 0)))
                treat_reset = bool(entry.get("treat_reset_as_curr", False))

                if clean_kind == "baseline":
                    prev_s = 0
                    delta = 0
                elif "override_delta" in entry and entry["override_delta"] is not None:
                    delta = max(0, int(entry["override_delta"]))
                else:
                    delta, _ = compute_worker_delta(
                        prev_s, curr_s, treat_reset_as_curr=treat_reset
                    )

                # Resolve member (respect manual override in entry if provided, else classify)
                explicit_mid = entry.get("member_id")
                if explicit_mid is not None and int(explicit_mid) in members:
                    m_obj = members[int(explicit_mid)]
                else:
                    m_obj, _ = classify_worker(
                        w_name, members, prefixes, bindings, wallet_id=wallet_id
                    )

                m_id = m_obj.id if m_obj else None
                m_name = m_obj.name if m_obj else "未分配"
                m_color = m_obj.color if m_obj else "#64748b"
                share_source = str(entry.get("share_source") or "live")

                processed_workers.append(
                    {
                        "worker_name": w_name,
                        "member_id": m_id,
                        "member_name_snapshot": m_name,
                        "prev_shares": prev_s,
                        "curr_shares": curr_s,
                        "delta_shares": delta,
                        "share_source": share_source,
                    }
                )

                # For withdrawal settlements, keep manual/missing offline shares in cache;
                # for historical baselines, never overwrite newer live/cached shares, only seed missing ones.
                if clean_kind == "withdrawal" and share_source in ("manual", "offline_missing"):
                    self.upsert_manual_worker_cache(wallet_id, w_name, curr_s)
                elif clean_kind == "baseline":
                    self._ensure_worker_cache_if_missing(wallet_id, w_name, curr_s, clean_time)

                agg_key = f"id:{m_id}" if m_id is not None else "unassigned"
                if agg_key not in member_agg:
                    member_agg[agg_key] = {
                        "member_id": m_id,
                        "member_name": m_name,
                        "member_color": m_color,
                        "delta_shares": 0,
                    }
                member_agg[agg_key]["delta_shares"] += delta
                total_delta += delta

            cur = self._conn.execute(
                """
                INSERT INTO checkpoints (
                    wallet_id, checkpoint_time, kind, label, prl_amount, cny_amount,
                    tx_hash, notes, prev_checkpoint_id, total_delta_shares, fees_json,
                    pool_paid_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_id,
                    clean_time,
                    clean_kind,
                    (label or "").strip(),
                    max(0.0, float(prl_amount or 0.0)),
                    max(0.0, float(cny_amount or 0.0)),
                    (tx_hash or "").strip(),
                    (notes or "").strip(),
                    prev_checkpoint_id,
                    total_delta,
                    fees_json,
                    clean_pool_snap,
                    now,
                ),
            )
            checkpoint_id = int(cur.lastrowid)

            for pw in processed_workers:
                self._conn.execute(
                    """
                    INSERT INTO checkpoint_workers (
                        checkpoint_id, worker_name, member_id, member_name_snapshot,
                        prev_shares, curr_shares, delta_shares, share_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        pw["worker_name"],
                        pw["member_id"],
                        pw["member_name_snapshot"],
                        pw["prev_shares"],
                        pw["curr_shares"],
                        pw["delta_shares"],
                        pw["share_source"],
                    ),
                )

            if clean_kind == "withdrawal":
                allocs = allocate_exact_amounts(
                    list(member_agg.values()),
                    total_cny=float(cny_amount or 0.0),
                    total_prl=float(prl_amount or 0.0),
                )
                for al in allocs:
                    self._conn.execute(
                        """
                        INSERT INTO settlement_allocations (
                            checkpoint_id, member_id, member_name, member_color,
                            delta_shares, ratio, prl_earned, cny_earned
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            checkpoint_id,
                            al["member_id"],
                            al["member_name"],
                            al["member_color"],
                            al["delta_shares"],
                            al["ratio"],
                            al["prl_earned"],
                            al["cny_earned"],
                        ),
                    )
                if clean_fees.get("linked_trade_ids"):
                    self._sync_checkpoint_trades_unlocked(checkpoint_id, clean_fees["linked_trade_ids"])

            self._conn.commit()
            return next(c for c in self.list_checkpoints(wallet_id) if c["id"] == checkpoint_id)

    def update_settlement_amounts(
        self,
        checkpoint_id: int,
        cny_amount: float,
        prl_amount: float,
        label: str = "",
        tx_hash: str = "",
        notes: str = "",
        fees: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update actual CNY / PRL received (and optional fees) on an existing withdrawal."""
        with self._lock:
            cp = self._conn.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
            if not cp:
                raise ValueError("未找到该结算记录")

            alloc_rows = self._conn.execute(
                "SELECT * FROM settlement_allocations WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchall()

            new_cny = max(0.0, float(cny_amount or 0.0))
            new_prl = max(0.0, float(prl_amount or 0.0))
            wallet_id = int(cp["wallet_id"])

            if fees is not None:
                wtype = self._get_wallet_type_unlocked(wallet_id)
                clean_fees = normalize_fee_payload(fees, wallet_type=wtype)
                new_fees_json = json.dumps(clean_fees, ensure_ascii=False)
                self._conn.execute(
                    "UPDATE checkpoints SET cny_amount = ?, prl_amount = ?, label = ?, tx_hash = ?, notes = ?, fees_json = ? "
                    "WHERE id = ?",
                    (
                        new_cny,
                        new_prl,
                        (label or "").strip(),
                        (tx_hash or "").strip(),
                        (notes or "").strip(),
                        new_fees_json,
                        checkpoint_id,
                    ),
                )
                self._sync_checkpoint_trades_unlocked(checkpoint_id, clean_fees.get("linked_trade_ids") or [])
            else:
                self._conn.execute(
                    "UPDATE checkpoints SET cny_amount = ?, prl_amount = ?, label = ?, tx_hash = ?, notes = ? "
                    "WHERE id = ?",
                    (
                        new_cny,
                        new_prl,
                        (label or "").strip(),
                        (tx_hash or "").strip(),
                        (notes or "").strip(),
                        checkpoint_id,
                    ),
                )

            if cp["kind"] == "withdrawal" and alloc_rows:
                recomputed = allocate_exact_amounts(
                    [dict(r) for r in alloc_rows],
                    total_cny=new_cny,
                    total_prl=new_prl,
                )
                for item in recomputed:
                    self._conn.execute(
                        "UPDATE settlement_allocations SET ratio = ?, prl_earned = ?, cny_earned = ? "
                        "WHERE id = ?",
                        (item["ratio"], item["prl_earned"], item["cny_earned"], item["id"]),
                    )

            self._conn.commit()
            wallet_id = cp["wallet_id"]
            return next(c for c in self.list_checkpoints(wallet_id) if c["id"] == checkpoint_id)

    def update_baseline_checkpoint(
        self,
        checkpoint_id: int,
        checkpoint_time: str,
        label: str,
        notes: str,
        worker_entries: list[dict[str, Any]],
        pool_paid_snapshot: float | None = None,
        update_pool_paid_snapshot: bool = False,
    ) -> dict[str, Any]:
        """Update an existing baseline snapshot's metadata and per-worker shares."""
        with self._lock:
            cp = self._conn.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
            if not cp:
                raise ValueError("未找到该基准记录")
            wallet_id = cp["wallet_id"]
            members, prefixes, bindings = self._load_classification_context()
            clean_time = (checkpoint_time or "").strip() or cp["checkpoint_time"]

            if update_pool_paid_snapshot:
                clean_snap = (
                    round(max(0.0, float(pool_paid_snapshot)), 6)
                    if pool_paid_snapshot is not None
                    else None
                )
                self._conn.execute(
                    "UPDATE checkpoints SET checkpoint_time = ?, label = ?, notes = ?, pool_paid_snapshot = ? WHERE id = ?",
                    (clean_time, (label or "").strip(), (notes or "").strip(), clean_snap, checkpoint_id),
                )
            else:
                self._conn.execute(
                    "UPDATE checkpoints SET checkpoint_time = ?, label = ?, notes = ? WHERE id = ?",
                    (clean_time, (label or "").strip(), (notes or "").strip(), checkpoint_id),
                )
            self._conn.execute(
                "DELETE FROM checkpoint_workers WHERE checkpoint_id = ?", (checkpoint_id,)
            )

            for entry in worker_entries:
                w_name = (entry.get("worker_name") or "").strip()
                if not w_name:
                    continue
                curr_s = max(0, int(entry.get("curr_shares", 0)))
                m_obj, _ = classify_worker(
                    w_name, members, prefixes, bindings, wallet_id=wallet_id
                )
                self._conn.execute(
                    """
                    INSERT INTO checkpoint_workers (
                        checkpoint_id, worker_name, member_id, member_name_snapshot,
                        prev_shares, curr_shares, delta_shares, share_source
                    ) VALUES (?, ?, ?, ?, 0, ?, 0, 'manual')
                    """,
                    (
                        checkpoint_id,
                        w_name,
                        m_obj.id if m_obj else None,
                        m_obj.name if m_obj else "未分配",
                        curr_s,
                    ),
                )
                self._ensure_worker_cache_if_missing(wallet_id, w_name, curr_s, clean_time)

            self._conn.commit()
            return next(c for c in self.list_checkpoints(wallet_id) if c["id"] == checkpoint_id)

    def set_checkpoint_pool_snapshot(
        self,
        checkpoint_id: int,
        pool_paid_snapshot: float | None,
    ) -> dict[str, Any]:
        """Set or clear the pool cumulative paid PRL snapshot recorded at a checkpoint."""
        with self._lock:
            cp = self._conn.execute(
                "SELECT wallet_id FROM checkpoints WHERE id = ?", (int(checkpoint_id),)
            ).fetchone()
            if not cp:
                raise ValueError("未找到对应的提现/基准记录")
            clean_snap = (
                round(max(0.0, float(pool_paid_snapshot)), 6)
                if pool_paid_snapshot is not None
                else None
            )
            self._conn.execute(
                "UPDATE checkpoints SET pool_paid_snapshot = ? WHERE id = ?",
                (clean_snap, int(checkpoint_id)),
            )
            self._conn.commit()
            wallet_id = int(cp["wallet_id"])
            return next(c for c in self.list_checkpoints(wallet_id) if c["id"] == int(checkpoint_id))

    def delete_checkpoint(self, checkpoint_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE exchange_trades SET linked_checkpoint_id = NULL WHERE linked_checkpoint_id = ?",
                (checkpoint_id,),
            )
            self._conn.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
            self._conn.commit()

    def get_global_member_totals(self) -> list[dict[str, Any]]:
        """Aggregate total CNY, PRL, and delta shares across all wallets & withdrawals."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    sa.member_name,
                    COALESCE(m.color, sa.member_color) AS member_color,
                    SUM(sa.delta_shares) AS total_shares,
                    SUM(sa.prl_earned) AS total_prl,
                    SUM(sa.cny_earned) AS total_cny,
                    COUNT(DISTINCT sa.checkpoint_id) AS settlement_count
                FROM settlement_allocations sa
                JOIN checkpoints c ON sa.checkpoint_id = c.id
                LEFT JOIN members m ON sa.member_id = m.id
                WHERE c.kind = 'withdrawal'
                GROUP BY sa.member_name
                ORDER BY total_cny DESC, total_shares DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_settlements(self) -> list[dict[str, Any]]:
        """List all withdrawal settlements across all wallets for ledger/export."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.*, w.alias AS wallet_alias, w.address AS wallet_address, w.wallet_type AS wallet_type
                FROM checkpoints c
                JOIN wallets w ON c.wallet_id = w.id
                ORDER BY c.checkpoint_time DESC, c.id DESC
                """
            ).fetchall()
            out = []
            for r in rows:
                d = self._enrich_checkpoint_row(r, str(r["wallet_type"] or "official"))
                allocs = self._conn.execute(
                    "SELECT * FROM settlement_allocations WHERE checkpoint_id = ? "
                    "ORDER BY delta_shares DESC, member_name ASC",
                    (d["id"],),
                ).fetchall()
                d["allocations"] = [dict(a) for a in allocs]
                out.append(d)
            return out

    # ------------------------------------------------------------------
    # Persistent Settlement Drafts (save & resume for offline miners)
    # ------------------------------------------------------------------
    def _enrich_draft_row(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        wallet_id = int(d["wallet_id"])
        try:
            raw_workers = json.loads(d.get("workers_json") or "[]")
        except json.JSONDecodeError:
            raw_workers = []

        cached_map = {w["worker_name"]: w for w in self.get_wallet_workers(wallet_id)}
        members, prefixes, bindings = self._load_classification_context()

        enriched_workers: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        offline_names: list[str] = []
        syncable_offline_count = 0
        syncable_any_count = 0
        total_delta = 0

        for item in raw_workers:
            w_name = str(item.get("worker_name") or "").strip()
            if not w_name:
                continue
            seen_names.add(w_name)
            prev_s = max(0, int(item.get("prev_shares") or 0))
            curr_s = max(0, int(item.get("curr_shares") or 0))
            treat_reset = bool(item.get("treat_reset_as_curr", False))
            share_source = str(item.get("share_source") or "offline_missing")

            mid = item.get("member_id")
            if mid is not None and int(mid) in members:
                m_obj = members[int(mid)]
                m_id = m_obj.id
                m_name = m_obj.name
                m_color = m_obj.color
            else:
                m_obj, _ = classify_worker(
                    w_name, members, prefixes, bindings, wallet_id=wallet_id
                )
                m_id = m_obj.id if m_obj else None
                m_name = m_obj.name if m_obj else (item.get("member_name") or "未分配")
                m_color = m_obj.color if m_obj else (item.get("member_color") or "#64748b")

            delta, reset_detected = compute_worker_delta(
                prev_s, curr_s, treat_reset_as_curr=treat_reset
            )
            total_delta += delta

            cw = cached_map.get(w_name)
            live_cache_shares = int(cw["last_shares"]) if cw else None
            live_cache_online = bool(cw["is_currently_online"]) if cw else False
            has_newer_cache = (live_cache_shares is not None) and (live_cache_shares != curr_s)

            if share_source != "live":
                offline_names.append(w_name)
                if has_newer_cache:
                    syncable_offline_count += 1
            if has_newer_cache:
                syncable_any_count += 1

            enriched_workers.append(
                {
                    "worker_name": w_name,
                    "member_id": m_id,
                    "member_name": m_name,
                    "member_color": m_color,
                    "prev_shares": prev_s,
                    "curr_shares": curr_s,
                    "delta_shares": delta,
                    "reset_detected": reset_detected,
                    "treat_reset_as_curr": treat_reset,
                    "share_source": share_source,
                    "is_online": share_source == "live",
                    "last_seen_at": str(item.get("last_seen_at") or ""),
                    "hashrate": str(item.get("hashrate") or "--"),
                    "live_cache_shares": live_cache_shares,
                    "live_cache_online": live_cache_online,
                    "has_newer_cache": has_newer_cache,
                }
            )

        new_cache_workers: list[dict[str, Any]] = []
        for cw_name, cw in cached_map.items():
            if cw_name in seen_names:
                continue
            m_obj, _ = classify_worker(
                cw_name, members, prefixes, bindings, wallet_id=wallet_id
            )
            new_cache_workers.append(
                {
                    "worker_name": cw_name,
                    "member_id": m_obj.id if m_obj else None,
                    "member_name": m_obj.name if m_obj else "未分配",
                    "member_color": m_obj.color if m_obj else "#64748b",
                    "prev_shares": 0,
                    "curr_shares": int(cw["last_shares"]),
                    "share_source": "live" if cw["is_currently_online"] else cw.get("source", "offline_cache"),
                    "is_online": bool(cw["is_currently_online"]),
                    "last_seen_at": cw["last_seen_online_at"],
                    "hashrate": cw["current_hashrate"],
                }
            )

        wtype = self._get_wallet_type_unlocked(wallet_id)
        fees = self._parse_fees_dict(d.get("fees_json"), wallet_type=wtype)

        return {
            "id": int(d["id"]),
            "wallet_id": wallet_id,
            "wallet_type": wtype,
            "title": d["title"],
            "prev_checkpoint_id": d["prev_checkpoint_id"],
            "checkpoint_time": d["checkpoint_time"],
            "label": d["label"],
            "prl_amount": float(d["prl_amount"] or 0.0),
            "cny_amount": float(d["cny_amount"] or 0.0),
            "notes": d["notes"],
            "offline_hint": d["offline_hint"],
            "fees": fees,
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "workers": enriched_workers,
            "new_cache_workers": new_cache_workers,
            "worker_count": len(enriched_workers),
            "offline_count": len(offline_names),
            "offline_names": offline_names,
            "syncable_offline_count": syncable_offline_count,
            "syncable_any_count": syncable_any_count,
            "total_delta_shares": total_delta,
        }

    def list_settlement_drafts(self, wallet_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM settlement_drafts WHERE wallet_id = ? ORDER BY updated_at DESC, id DESC",
                (int(wallet_id),),
            ).fetchall()
            return [self._enrich_draft_row(r) for r in rows]

    def get_settlement_draft(self, draft_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM settlement_drafts WHERE id = ?",
                (int(draft_id),),
            ).fetchone()
            if not row:
                raise ValueError("未找到该结算草稿")
            return self._enrich_draft_row(row)

    def save_settlement_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        wallet_id = int(payload["wallet_id"])
        draft_id = int(payload["draft_id"]) if payload.get("draft_id") else None
        raw_prev_cp = payload.get("prev_checkpoint_id")
        prev_cp_id = int(raw_prev_cp) if raw_prev_cp else None
        cp_time = str(payload.get("checkpoint_time") or "").strip() or _now()
        label = str(payload.get("label") or "").strip()
        prl_amount = max(0.0, float(payload.get("prl_amount") or 0.0))
        cny_amount = max(0.0, float(payload.get("cny_amount") or 0.0))
        notes = str(payload.get("notes") or "").strip()

        clean_workers: list[dict[str, Any]] = []
        offline_names: list[str] = []
        for entry in list(payload.get("worker_entries") or []):
            w_name = str(entry.get("worker_name") or "").strip()
            if not w_name:
                continue
            src = str(entry.get("share_source") or "live")
            if src != "live":
                offline_names.append(w_name)
            mid_val = entry.get("member_id")
            clean_workers.append(
                {
                    "worker_name": w_name,
                    "member_id": int(mid_val) if mid_val not in (None, "") else None,
                    "member_name": str(entry.get("member_name") or "未分配"),
                    "member_color": str(entry.get("member_color") or "#64748b"),
                    "prev_shares": max(0, int(entry.get("prev_shares") or 0)),
                    "curr_shares": max(0, int(entry.get("curr_shares") or 0)),
                    "share_source": src,
                    "treat_reset_as_curr": bool(entry.get("treat_reset_as_curr", False)),
                    "last_seen_at": str(entry.get("last_seen_at") or ""),
                    "hashrate": str(entry.get("hashrate") or "--"),
                }
            )

        now = _now()
        title = str(payload.get("title") or "").strip()
        if not title:
            title = label or f"{cp_time[:16]} · ￥{cny_amount:.2f}"
        offline_hint = str(payload.get("offline_hint") or "").strip()
        if not offline_hint and offline_names:
            preview = ", ".join(offline_names[:3])
            suffix = f" 等{len(offline_names)}台" if len(offline_names) > 3 else ""
            offline_hint = f"待核实离线: {preview}{suffix}"

        workers_json = json.dumps(clean_workers, ensure_ascii=False)
        with self._lock:
            wtype = self._get_wallet_type_unlocked(wallet_id)
            clean_fees = normalize_fee_payload(payload.get("fees"), wallet_type=wtype)
            fees_json = json.dumps(clean_fees, ensure_ascii=False)

            if prev_cp_id is not None:
                exists_cp = self._conn.execute(
                    "SELECT 1 FROM checkpoints WHERE id = ? AND wallet_id = ?",
                    (prev_cp_id, wallet_id),
                ).fetchone()
                if not exists_cp:
                    prev_cp_id = None

            if draft_id is not None:
                existing = self._conn.execute(
                    "SELECT id FROM settlement_drafts WHERE id = ? AND wallet_id = ?",
                    (draft_id, wallet_id),
                ).fetchone()
                if existing:
                    self._conn.execute(
                        """
                        UPDATE settlement_drafts
                        SET title = ?, prev_checkpoint_id = ?, checkpoint_time = ?,
                            label = ?, prl_amount = ?, cny_amount = ?, notes = ?,
                            workers_json = ?, offline_hint = ?, fees_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            title,
                            prev_cp_id,
                            cp_time,
                            label,
                            prl_amount,
                            cny_amount,
                            notes,
                            workers_json,
                            offline_hint,
                            fees_json,
                            now,
                            draft_id,
                        ),
                    )
                    self._conn.commit()
                    return self.get_settlement_draft(draft_id)

            cur = self._conn.execute(
                """
                INSERT INTO settlement_drafts (
                    wallet_id, title, prev_checkpoint_id, checkpoint_time, label,
                    prl_amount, cny_amount, notes, workers_json, offline_hint, fees_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_id,
                    title,
                    prev_cp_id,
                    cp_time,
                    label,
                    prl_amount,
                    cny_amount,
                    notes,
                    workers_json,
                    offline_hint,
                    fees_json,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)
            self._conn.commit()
            return self.get_settlement_draft(new_id)

    def delete_settlement_draft(self, draft_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settlement_drafts WHERE id = ?", (int(draft_id),))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Custom Fee Pipeline Templates, Generic Exchange Trades & Multi-Asset Residual Pool
    # ------------------------------------------------------------------
    def get_fee_pipeline_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            raw_json = self.get_settings().get("fee_pipeline_templates_json")
            if raw_json is None:
                return normalize_pipeline_steps(DEFAULT_FEE_PIPELINE_STEPS)
            try:
                loaded = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                return normalize_pipeline_steps(DEFAULT_FEE_PIPELINE_STEPS)
            return normalize_pipeline_steps(loaded if isinstance(loaded, list) else DEFAULT_FEE_PIPELINE_STEPS)

    def save_fee_pipeline_templates(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned = normalize_pipeline_steps(steps)
        with self._lock:
            self.update_settings(
                {"fee_pipeline_templates_json": json.dumps(cleaned, ensure_ascii=False)}
            )
            return cleaned

    def apply_fee_pipeline_preset(self, preset_key: str) -> list[dict[str, Any]]:
        preset = PIPELINE_PRESETS.get(str(preset_key or "").strip())
        if not preset:
            raise ValueError("未知的手续费流程预设模板")
        return self.save_fee_pipeline_templates(preset["steps"])

    def list_exchange_trades(self, wallet_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql = (
                "SELECT t.*, w.alias AS wallet_alias, w.wallet_type AS wallet_type "
                "FROM exchange_trades t LEFT JOIN wallets w ON t.wallet_id = w.id "
            )
            params: tuple[Any, ...] = ()
            if wallet_id is not None:
                sql += "WHERE (t.wallet_id = ? OR t.wallet_id IS NULL) "
                params = (int(wallet_id),)
            sql += "ORDER BY t.trade_time DESC, t.id DESC"
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _clean_asset(raw: Any, default: str = "USDT") -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip().upper())[:10]
        return cleaned or default

    def add_exchange_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_wid = payload.get("wallet_id")
        wallet_id = int(raw_wid) if raw_wid not in (None, "", 0, "0") else None
        venue = str(payload.get("venue") or payload.get("platform") or "交易所").strip()[:64] or "交易所"
        from_asset = self._clean_asset(payload.get("from_asset"), default="PRL")
        to_asset = self._clean_asset(payload.get("to_asset"), default="USDT")

        trade_time = str(payload.get("trade_time") or "").strip() or _now()
        source_amount = max(0.0, float(payload.get("source_amount") or 0.0))
        sold_amount = max(0.0, float(payload.get("sold_amount") or 0.0))
        unit_price = max(0.0, float(payload.get("unit_price") or 0.0))
        trade_fee = max(0.0, float(payload.get("trade_fee") or 0.0))
        notes = str(payload.get("notes") or "").strip()

        with self._lock:
            wtype = self._get_wallet_type_unlocked(wallet_id) if wallet_id is not None else "official"
            raw_tf = payload.get("transfer_fee")
            if raw_tf is None:
                raw_tf = payload.get("transfer_fee_prl")
            transfer_fee = (
                0.0
                if (wtype == "exchange" or from_asset != "PRL")
                else max(0.0, float(raw_tf or 0.0))
            )

            trade_fee_unit = self._clean_asset(payload.get("trade_fee_unit"), default=to_asset)
            receive_gross = max(0.0, float(payload.get("receive_gross") or 0.0))
            if receive_gross <= 0 and sold_amount > 0 and unit_price > 0:
                receive_gross = round(sold_amount * unit_price, 6)

            receive_net = max(0.0, float(payload.get("receive_net") or 0.0))
            if receive_net <= 0 and receive_gross > 0:
                fee_deduct = trade_fee if trade_fee_unit == to_asset else 0.0
                receive_net = round(max(0.0, receive_gross - fee_deduct), 6)

            if unit_price <= 0 and sold_amount > 0 and receive_gross > 0:
                unit_price = round(receive_gross / sold_amount, 6)

            dust_asset = self._clean_asset(payload.get("dust_asset"), default=from_asset)
            raw_delta = payload.get("dust_delta")
            if raw_delta not in (None, ""):
                dust_delta = round(float(raw_delta), 6)
            elif source_amount > 0:
                avail_in = max(0.0, source_amount - transfer_fee)
                dust_delta = round(avail_in - sold_amount, 6)
            else:
                dust_delta = 0.0

            cur = self._conn.execute(
                """
                INSERT INTO exchange_trades (
                    wallet_id, trade_time, venue, from_asset, to_asset,
                    source_amount, transfer_fee, sold_amount, unit_price, receive_gross,
                    trade_fee, trade_fee_unit, receive_net, dust_delta, dust_asset,
                    notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_id,
                    trade_time,
                    venue,
                    from_asset,
                    to_asset,
                    round(source_amount, 6),
                    round(transfer_fee, 6),
                    round(sold_amount, 6),
                    round(unit_price, 6),
                    round(receive_gross, 6),
                    round(trade_fee, 6),
                    trade_fee_unit,
                    round(receive_net, 6),
                    dust_delta,
                    dust_asset,
                    notes,
                    _now(),
                ),
            )
            new_id = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT t.*, w.alias AS wallet_alias, w.wallet_type AS wallet_type "
                "FROM exchange_trades t LEFT JOIN wallets w ON t.wallet_id = w.id WHERE t.id = ?",
                (new_id,),
            ).fetchone()
            return dict(row)

    def delete_exchange_trade(self, trade_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM exchange_trades WHERE id = ?", (int(trade_id),))
            self._conn.commit()

    def get_residual_summary(self) -> dict[str, Any]:
        """Compute multi-asset cumulative exchange residual balances and unsettled trade stats."""
        with self._lock:
            settings = self.get_settings()
            seeds: dict[str, float] = {}
            try:
                raw_seeds = json.loads(settings.get("dust_seeds_json") or "{}")
                if isinstance(raw_seeds, dict):
                    for k, v in raw_seeds.items():
                        asset = self._clean_asset(k, default="")
                        val = round(float(v or 0.0), 6)
                        if asset and abs(val) > 1e-8:
                            seeds[asset] = val
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            from_trades: dict[str, float] = {}
            unsettled_count = 0
            t_rows = self._conn.execute(
                "SELECT from_asset, dust_asset, dust_delta, linked_checkpoint_id FROM exchange_trades"
            ).fetchall()
            for r in t_rows:
                asset = self._clean_asset(r["dust_asset"] or r["from_asset"], default="PRL")
                delta = float(r["dust_delta"] or 0.0)
                if abs(delta) > 1e-8:
                    from_trades[asset] = round(from_trades.get(asset, 0.0) + delta, 6)
                if r["linked_checkpoint_id"] is None:
                    unsettled_count += 1

            from_direct: dict[str, float] = {}
            total_fee_cny_all = 0.0
            total_settled_cny_all = 0.0
            cp_rows = self._conn.execute(
                "SELECT c.prl_amount, c.cny_amount, c.fees_json, w.wallet_type "
                "FROM checkpoints c JOIN wallets w ON c.wallet_id = w.id "
                "WHERE c.kind = 'withdrawal'"
            ).fetchall()
            for cp in cp_rows:
                wtype = self._clean_wallet_type(cp["wallet_type"])
                f = self._parse_fees_dict(cp["fees_json"], wallet_type=wtype)
                if f.get("residual_source") != "trades":
                    for asset, delta in (f.get("residual_deltas") or {}).items():
                        dval = float(delta or 0.0)
                        if abs(dval) > 1e-8:
                            from_direct[asset] = round(from_direct.get(asset, 0.0) + dval, 6)
                summ = summarize_fees_and_rates(
                    f,
                    cny_amount=float(cp["cny_amount"] or 0.0),
                    prl_amount=float(cp["prl_amount"] or 0.0),
                )
                total_fee_cny_all += float(summ.get("total_loss_cny") or summ.get("estimated_fee_cny") or 0.0)
                total_settled_cny_all += float(cp["cny_amount"] or 0.0)

            all_assets = {"PRL", "USDT"} | set(seeds.keys()) | set(from_trades.keys()) | set(from_direct.keys())
            balances: dict[str, float] = {}
            for asset in sorted(all_assets, key=lambda a: (0 if a == "PRL" else 1 if a == "USDT" else 2, a)):
                total_val = round(
                    seeds.get(asset, 0.0) + from_trades.get(asset, 0.0) + from_direct.get(asset, 0.0),
                    6,
                )
                if abs(total_val) < 1e-8:
                    total_val = 0.0
                if asset in ("PRL", "USDT") or abs(total_val) > 1e-8 or asset in seeds or asset in from_trades:
                    balances[asset] = total_val

            return {
                "balances": balances,
                "seeds": seeds,
                "from_trades": from_trades,
                "from_direct_settlements": from_direct,
                "prl_dust_balance": balances.get("PRL", 0.0),
                "usdt_dust_balance": balances.get("USDT", 0.0),
                "unsettled_trade_count": unsettled_count,
                "total_estimated_fee_cny": round(total_fee_cny_all, 2),
                "total_settled_cny": round(total_settled_cny_all, 2),
            }

    def calibrate_residual_balance(
        self, balances: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Adjust seed calibration so cumulative multi-asset dust matches user's real exchange balances."""
        if not isinstance(balances, dict):
            return self.get_residual_summary()
        with self._lock:
            cur = self.get_residual_summary()
            new_seeds = dict(cur.get("seeds") or {})
            for k, v in balances.items():
                asset = self._clean_asset(k, default="")
                if not asset:
                    continue
                if v in (None, ""):
                    continue
                target = max(0.0, float(v))
                tracked = float((cur.get("from_trades") or {}).get(asset, 0.0)) + float(
                    (cur.get("from_direct_settlements") or {}).get(asset, 0.0)
                )
                seed_val = round(target - tracked, 6)
                if abs(seed_val) < 1e-8:
                    new_seeds.pop(asset, None)
                else:
                    new_seeds[asset] = seed_val
            self.update_settings({"dust_seeds_json": json.dumps(new_seeds, ensure_ascii=False)})
            return self.get_residual_summary()

    # ------------------------------------------------------------------
    # Multi-User Checkpoint Sync Export / Import (Host -> Friends)
    # ------------------------------------------------------------------
    def export_sync_bundle(
        self,
        wallet_id: int,
        checkpoint_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Build a portable JSON-serializable checkpoint & member sync package for a wallet."""
        with self._lock:
            w_row = self._conn.execute(
                "SELECT id, address, alias, notes, wallet_type FROM wallets WHERE id = ?",
                (int(wallet_id),),
            ).fetchone()
            if not w_row:
                raise ValueError("未找到指定钱包")

            wtype = self._clean_wallet_type(w_row["wallet_type"])
            all_cps = self.list_checkpoints(int(wallet_id))
            cps_asc = sorted(all_cps, key=lambda c: (str(c.get("checkpoint_time") or ""), int(c["id"])))

            if checkpoint_ids:
                picked_set = {int(cid) for cid in checkpoint_ids}
                selected_cps = [c for c in cps_asc if int(c["id"]) in picked_set]
            else:
                selected_cps = cps_asc

            included_by_id = {int(c["id"]): c for c in selected_cps}
            portable_cps: list[dict[str, Any]] = []
            for c in selected_cps:
                prev_key = None
                pid = c.get("prev_checkpoint_id")
                if pid is not None and int(pid) in included_by_id:
                    pc = included_by_id[int(pid)]
                    prev_key = {
                        "kind": pc["kind"],
                        "checkpoint_time": pc["checkpoint_time"],
                    }

                raw_fees = dict(c.get("fees") or {})
                raw_fees.pop("linked_trade_ids", None)
                if raw_fees.get("residual_source") == "trades":
                    raw_fees["residual_source"] = "direct"

                portable_cps.append(
                    {
                        "kind": c["kind"],
                        "checkpoint_time": c["checkpoint_time"],
                        "label": c.get("label") or "",
                        "prl_amount": round(float(c.get("prl_amount") or 0.0), 6),
                        "cny_amount": round(float(c.get("cny_amount") or 0.0), 2),
                        "tx_hash": c.get("tx_hash") or "",
                        "notes": c.get("notes") or "",
                        "total_delta_shares": int(c.get("total_delta_shares") or 0),
                        "pool_paid_snapshot": c.get("pool_paid_snapshot"),
                        "prev_checkpoint_key": prev_key,
                        "fees": raw_fees if c["kind"] == "withdrawal" else {},
                        "workers": [
                            {
                                "worker_name": wk["worker_name"],
                                "member_name": wk.get("member_name_snapshot") or "未分配",
                                "prev_shares": int(wk.get("prev_shares") or 0),
                                "curr_shares": int(wk.get("curr_shares") or 0),
                                "delta_shares": int(wk.get("delta_shares") or 0),
                                "share_source": wk.get("share_source") or "live",
                            }
                            for wk in (c.get("workers") or [])
                        ],
                        "allocations": [
                            {
                                "member_name": al["member_name"],
                                "member_color": al.get("member_color") or "#64748b",
                                "delta_shares": int(al.get("delta_shares") or 0),
                                "ratio": float(al.get("ratio") or 0.0),
                                "prl_earned": float(al.get("prl_earned") or 0.0),
                                "cny_earned": float(al.get("cny_earned") or 0.0),
                            }
                            for al in (c.get("allocations") or [])
                        ],
                    }
                )

            members_list = self.list_members()
            m_name_by_id = {int(m["id"]): m["name"] for m in members_list}
            portable_members = [
                {
                    "name": m["name"],
                    "color": m["color"],
                    "prefixes": [p["prefix"] for p in (m.get("prefixes") or [])],
                }
                for m in members_list
            ]

            bind_rows = self._conn.execute(
                "SELECT worker_name, member_id FROM worker_bindings "
                "WHERE wallet_id IS NULL OR wallet_id = ?",
                (int(wallet_id),),
            ).fetchall()
            portable_bindings = [
                {
                    "worker_name": r["worker_name"],
                    "member_name": m_name_by_id[int(r["member_id"])],
                }
                for r in bind_rows
                if int(r["member_id"]) in m_name_by_id
            ]

            return {
                "format": "pearl_split_sync_v1",
                "exported_at": _now(),
                "wallet": {
                    "address": w_row["address"],
                    "alias": w_row["alias"],
                    "notes": w_row["notes"] or "",
                    "wallet_type": wtype,
                },
                "members": portable_members,
                "worker_bindings": portable_bindings,
                "checkpoints": portable_cps,
            }

    def import_sync_bundle(
        self,
        bundle: dict[str, Any],
        sync_members: bool = True,
    ) -> dict[str, Any]:
        """Idempotently merge a portable sync bundle into local DB by wallet address & checkpoint key."""
        if not isinstance(bundle, dict) or bundle.get("format") != "pearl_split_sync_v1":
            raise ValueError("不支持的同步数据包格式（需要 pearl_split_sync_v1）")
        w_info = bundle.get("wallet")
        if not isinstance(w_info, dict) or not str(w_info.get("address") or "").strip():
            raise ValueError("同步数据包缺少钱包地址信息")

        addr = str(w_info["address"]).strip()
        wtype_in = self._clean_wallet_type(w_info.get("wallet_type"))

        with self._lock:
            now = _now()
            w_row = self._conn.execute(
                "SELECT id, alias, address, wallet_type FROM wallets WHERE LOWER(TRIM(address)) = LOWER(?)",
                (addr,),
            ).fetchone()
            if w_row:
                wallet_id = int(w_row["id"])
                wallet_alias = w_row["alias"]
                wallet_created = False
                wtype = self._clean_wallet_type(w_row["wallet_type"])
            else:
                wallet_alias = str(w_info.get("alias") or "好友同步钱包").strip() or "好友同步钱包"
                notes_w = str(w_info.get("notes") or "由提现同步数据包创建").strip()
                cur = self._conn.execute(
                    "INSERT INTO wallets (address, alias, notes, is_active, wallet_type, created_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (addr, wallet_alias, notes_w, wtype_in, now),
                )
                wallet_id = int(cur.lastrowid)
                wallet_created = True
                wtype = wtype_in

            member_by_lower: dict[str, dict[str, Any]] = {
                r["name"].strip().lower(): dict(r)
                for r in self._conn.execute("SELECT * FROM members").fetchall()
            }
            members_added = 0
            if sync_members:
                for pm in bundle.get("members") or []:
                    mname = str(pm.get("name") or "").strip()
                    if not mname:
                        continue
                    mkey = mname.lower()
                    if mkey not in member_by_lower:
                        mcol = str(pm.get("color") or "#10b981").strip() or "#10b981"
                        cur = self._conn.execute(
                            "INSERT INTO members (name, color, notes, created_at) VALUES (?, ?, '', ?)",
                            (mname, mcol, now),
                        )
                        new_mid = int(cur.lastrowid)
                        member_by_lower[mkey] = {"id": new_mid, "name": mname, "color": mcol}
                        members_added += 1
                    mid = int(member_by_lower[mkey]["id"])
                    for pfx in pm.get("prefixes") or []:
                        cpfx = str(pfx or "").strip()
                        if not cpfx:
                            continue
                        ex_p = self._conn.execute(
                            "SELECT id FROM member_prefixes WHERE LOWER(prefix) = LOWER(?)",
                            (cpfx,),
                        ).fetchone()
                        if not ex_p:
                            self._conn.execute(
                                "INSERT INTO member_prefixes (member_id, prefix, created_at) VALUES (?, ?, ?)",
                                (mid, cpfx, now),
                            )

                for wb in bundle.get("worker_bindings") or []:
                    wname = str(wb.get("worker_name") or "").strip()
                    mname = str(wb.get("member_name") or "").strip()
                    mentry = member_by_lower.get(mname.lower()) if mname else None
                    if not wname or not mentry:
                        continue
                    ex_b = self._conn.execute(
                        "SELECT id FROM worker_bindings WHERE LOWER(worker_name) = LOWER(?) AND wallet_id = ?",
                        (wname, wallet_id),
                    ).fetchone()
                    if not ex_b:
                        self._conn.execute(
                            "INSERT INTO worker_bindings (worker_name, member_id, wallet_id, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (wname, int(mentry["id"]), wallet_id, now),
                        )

            members, prefixes, bindings = self._load_classification_context()
            existing_cp_by_key: dict[tuple[str, str], int] = {
                (r["kind"], r["checkpoint_time"]): int(r["id"])
                for r in self._conn.execute(
                    "SELECT id, kind, checkpoint_time FROM checkpoints WHERE wallet_id = ?",
                    (wallet_id,),
                ).fetchall()
            }
            synced_id_by_key = dict(existing_cp_by_key)

            raw_cps = [c for c in (bundle.get("checkpoints") or []) if isinstance(c, dict)]
            raw_cps.sort(
                key=lambda c: (
                    str(c.get("checkpoint_time") or ""),
                    0 if c.get("kind") == "baseline" else 1,
                )
            )

            added_cps = 0
            updated_cps = 0
            for cp in raw_cps:
                cp_kind = "baseline" if cp.get("kind") == "baseline" else "withdrawal"
                cp_time = str(cp.get("checkpoint_time") or "").strip()
                if not cp_time:
                    continue

                prev_cp_id = None
                prev_key = cp.get("prev_checkpoint_key")
                if isinstance(prev_key, dict):
                    pk_kind = "baseline" if prev_key.get("kind") == "baseline" else "withdrawal"
                    pk_time = str(prev_key.get("checkpoint_time") or "").strip()
                    prev_cp_id = synced_id_by_key.get((pk_kind, pk_time))

                clean_snap = (
                    round(max(0.0, float(cp["pool_paid_snapshot"])), 6)
                    if cp.get("pool_paid_snapshot") is not None
                    else None
                )
                raw_fees = dict(cp.get("fees") or {})
                raw_fees.pop("linked_trade_ids", None)
                if raw_fees.get("residual_source") == "trades":
                    raw_fees["residual_source"] = "direct"
                clean_fees = normalize_fee_payload(raw_fees, wallet_type=wtype) if cp_kind == "withdrawal" else {}
                fees_json = json.dumps(clean_fees, ensure_ascii=False) if clean_fees else "{}"

                total_delta = max(0, int(cp.get("total_delta_shares") or 0))
                label = str(cp.get("label") or "").strip()
                prl_amt = max(0.0, float(cp.get("prl_amount") or 0.0))
                cny_amt = max(0.0, float(cp.get("cny_amount") or 0.0))
                tx_hash = str(cp.get("tx_hash") or "").strip()
                notes = str(cp.get("notes") or "").strip()

                key = (cp_kind, cp_time)
                if key in existing_cp_by_key:
                    cp_id = existing_cp_by_key[key]
                    self._conn.execute(
                        """
                        UPDATE checkpoints
                        SET label = ?, prl_amount = ?, cny_amount = ?, tx_hash = ?, notes = ?,
                            prev_checkpoint_id = ?, total_delta_shares = ?, fees_json = ?,
                            pool_paid_snapshot = ?
                        WHERE id = ?
                        """,
                        (
                            label,
                            prl_amt,
                            cny_amt,
                            tx_hash,
                            notes,
                            prev_cp_id,
                            total_delta,
                            fees_json,
                            clean_snap,
                            cp_id,
                        ),
                    )
                    self._conn.execute("DELETE FROM checkpoint_workers WHERE checkpoint_id = ?", (cp_id,))
                    self._conn.execute("DELETE FROM settlement_allocations WHERE checkpoint_id = ?", (cp_id,))
                    updated_cps += 1
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO checkpoints (
                            wallet_id, checkpoint_time, kind, label, prl_amount, cny_amount,
                            tx_hash, notes, prev_checkpoint_id, total_delta_shares, fees_json,
                            pool_paid_snapshot, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            wallet_id,
                            cp_time,
                            cp_kind,
                            label,
                            prl_amt,
                            cny_amt,
                            tx_hash,
                            notes,
                            prev_cp_id,
                            total_delta,
                            fees_json,
                            clean_snap,
                            now,
                        ),
                    )
                    cp_id = int(cur.lastrowid)
                    existing_cp_by_key[key] = cp_id
                    added_cps += 1

                synced_id_by_key[key] = cp_id

                for wk in cp.get("workers") or []:
                    w_name = str(wk.get("worker_name") or "").strip()
                    if not w_name:
                        continue
                    m_snap_name = str(wk.get("member_name") or "").strip()
                    m_entry = member_by_lower.get(m_snap_name.lower()) if m_snap_name and m_snap_name != "未分配" else None
                    if m_entry:
                        m_id = int(m_entry["id"])
                        m_snap = m_entry["name"]
                    else:
                        m_obj, _ = classify_worker(w_name, members, prefixes, bindings, wallet_id=wallet_id)
                        m_id = m_obj.id if m_obj else None
                        m_snap = m_obj.name if m_obj else (m_snap_name or "未分配")
                    curr_s = max(0, int(wk.get("curr_shares") or 0))
                    self._conn.execute(
                        """
                        INSERT INTO checkpoint_workers (
                            checkpoint_id, worker_name, member_id, member_name_snapshot,
                            prev_shares, curr_shares, delta_shares, share_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cp_id,
                            w_name,
                            m_id,
                            m_snap,
                            max(0, int(wk.get("prev_shares") or 0)),
                            curr_s,
                            max(0, int(wk.get("delta_shares") or 0)),
                            str(wk.get("share_source") or "live"),
                        ),
                    )
                    self._ensure_worker_cache_if_missing(wallet_id, w_name, curr_s, cp_time)

                if cp_kind == "withdrawal":
                    for al in cp.get("allocations") or []:
                        aname = str(al.get("member_name") or "未分配").strip() or "未分配"
                        m_entry = member_by_lower.get(aname.lower()) if aname != "未分配" else None
                        m_id = int(m_entry["id"]) if m_entry else None
                        m_col = m_entry["color"] if m_entry else (str(al.get("member_color") or "#64748b").strip() or "#64748b")
                        self._conn.execute(
                            """
                            INSERT INTO settlement_allocations (
                                checkpoint_id, member_id, member_name, member_color,
                                delta_shares, ratio, prl_earned, cny_earned
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                cp_id,
                                m_id,
                                aname,
                                m_col,
                                max(0, int(al.get("delta_shares") or 0)),
                                float(al.get("ratio") or 0.0),
                                float(al.get("prl_earned") or 0.0),
                                float(al.get("cny_earned") or 0.0),
                            ),
                        )

            self._conn.commit()
            return {
                "wallet_id": wallet_id,
                "wallet_alias": wallet_alias,
                "wallet_address": addr,
                "wallet_created": wallet_created,
                "members_added": members_added,
                "added_checkpoints": added_cps,
                "updated_checkpoints": updated_cps,
                "total_checkpoints": added_cps + updated_cps,
            }

    # ------------------------------------------------------------------
    # Offline Alert Config, Remote Credentials & Synology Standby Sync
    # ------------------------------------------------------------------
    def get_alert_config(self) -> dict[str, Any]:
        """Load normalized alert push + standby failover config from settings."""
        s = self.get_settings()
        cfg = normalize_alert_config(
            {
                "enabled": s.get("alert_enabled", "0") == "1",
                "provider": s.get("alert_provider", "pushplus"),
                "pushplus_token": s.get("alert_pushplus_token", ""),
                "pushplus_topic": s.get("alert_pushplus_topic", ""),
                "pushplus_channel": s.get("alert_pushplus_channel", "wechat"),
                "wxpusher_app_token": s.get("alert_wxpusher_app_token", ""),
                "wxpusher_topic_ids": s.get("alert_wxpusher_topic_ids", ""),
                "wxpusher_uids": s.get("alert_wxpusher_uids", ""),
                "offline_confirm_polls": s.get("alert_offline_confirm_polls", "2"),
                "notify_recovery": s.get("alert_notify_recovery", "1") == "1",
                "include_password": s.get("alert_include_password", "1") == "1",
                "poll_fail_alert_minutes": s.get("alert_poll_fail_minutes", "30"),
                "standby_sync_enabled": s.get("alert_standby_sync_enabled", "0") == "1",
                "standby_server_url": s.get("alert_standby_url", ""),
                "standby_token": s.get("alert_standby_token", ""),
                "desktop_lease_timeout_sec": s.get("alert_desktop_lease_sec", "180"),
                "standby_poll_interval_sec": s.get("alert_standby_poll_sec", "60"),
            }
        )
        effective_url = cfg["standby_server_url"]
        effective_tok = cfg["standby_token"]
        if not effective_url and s.get("collector_mode") == "remote":
            effective_url = str(s.get("collector_server_url") or "").strip().rstrip("/")
            if not effective_tok:
                effective_tok = str(s.get("collector_token") or "").strip()
        cfg["effective_standby_url"] = effective_url
        cfg["effective_standby_token"] = effective_tok
        return cfg

    def save_alert_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist alert push settings; acknowledge currently offline rigs when newly enabled."""
        with self._lock:
            prev_cfg = self.get_alert_config()
            norm = normalize_alert_config(payload)
            updates = {
                "alert_enabled": "1" if norm["enabled"] else "0",
                "alert_provider": norm["provider"],
                "alert_pushplus_token": norm["pushplus_token"],
                "alert_pushplus_topic": norm["pushplus_topic"],
                "alert_pushplus_channel": norm["pushplus_channel"],
                "alert_wxpusher_app_token": norm["wxpusher_app_token"],
                "alert_wxpusher_topic_ids": norm["wxpusher_topic_ids"],
                "alert_wxpusher_uids": norm["wxpusher_uids"],
                "alert_offline_confirm_polls": str(norm["offline_confirm_polls"]),
                "alert_notify_recovery": "1" if norm["notify_recovery"] else "0",
                "alert_include_password": "1" if norm["include_password"] else "0",
                "alert_poll_fail_minutes": str(norm["poll_fail_alert_minutes"]),
                "alert_standby_sync_enabled": "1" if norm["standby_sync_enabled"] else "0",
                "alert_standby_url": norm["standby_server_url"],
                "alert_standby_token": norm["standby_token"],
                "alert_desktop_lease_sec": str(norm["desktop_lease_timeout_sec"]),
                "alert_standby_poll_sec": str(norm["standby_poll_interval_sec"]),
            }
            self.update_settings(updates)
            if not prev_cfg["enabled"] and norm["enabled"]:
                # Prevent immediate spam for already-offline historical rigs upon enabling alerts
                self.acknowledge_currently_offline_workers()
            return self.get_alert_config()

    def acknowledge_currently_offline_workers(self, wallet_id: int | None = None) -> None:
        """Mark already-offline workers as alerted so they only trigger after coming back online."""
        with self._lock:
            if wallet_id is not None:
                self._conn.execute(
                    "UPDATE worker_cache SET offline_alert_sent = 1 "
                    "WHERE wallet_id = ? AND is_currently_online = 0",
                    (int(wallet_id),),
                )
            else:
                self._conn.execute(
                    "UPDATE worker_cache SET offline_alert_sent = 1 WHERE is_currently_online = 0"
                )
            self._conn.commit()

    def update_worker_remote_config(
        self,
        wallet_id: int,
        worker_name: str,
        remote_tool: str = "ToDesk",
        remote_code: str = "",
        remote_password: str = "",
        remote_note: str = "",
        alert_enabled: bool = True,
    ) -> dict[str, Any]:
        """Update remote desktop credentials and per-rig alert toggle in worker_cache."""
        clean_name = (worker_name or "").strip()
        if not clean_name:
            raise ValueError("矿机名称不能为空")
        clean_tool = (remote_tool or "ToDesk").strip()[:32] or "ToDesk"
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE worker_cache
                SET remote_tool = ?, remote_code = ?, remote_password = ?,
                    remote_note = ?, alert_enabled = ?
                WHERE wallet_id = ? AND worker_name = ?
                """,
                (
                    clean_tool,
                    (remote_code or "").strip(),
                    (remote_password or "").strip(),
                    (remote_note or "").strip(),
                    1 if alert_enabled else 0,
                    int(wallet_id),
                    clean_name,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"未找到矿机缓存记录: {clean_name}")
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM worker_cache WHERE wallet_id = ? AND worker_name = ?",
                (int(wallet_id), clean_name),
            ).fetchone()
            return dict(row)

    def mark_workers_offline_alert_sent(
        self,
        wallet_id: int,
        worker_names: list[str],
        alerted_at: str,
    ) -> None:
        if not worker_names:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in worker_names)
            self._conn.execute(
                f"UPDATE worker_cache SET offline_alert_sent = 1, last_alert_at = ? "
                f"WHERE wallet_id = ? AND worker_name IN ({placeholders})",
                (alerted_at, int(wallet_id), *worker_names),
            )
            self._conn.commit()

    def reset_worker_alert_state(self, wallet_id: int, worker_name: str) -> None:
        clean_name = (worker_name or "").strip()
        with self._lock:
            self._conn.execute(
                "UPDATE worker_cache SET offline_alert_sent = 0, consecutive_offline_polls = 0 "
                "WHERE wallet_id = ? AND worker_name = ?",
                (int(wallet_id), clean_name),
            )
            self._conn.commit()

    def merge_standby_handback_states(self, handback_states: list[dict[str, Any]]) -> int:
        """Apply offline_alert_sent flags triggered by Synology Docker while Desktop was offline."""
        if not handback_states:
            return 0
        updated = 0
        with self._lock:
            wallet_by_addr = {
                w["address"].strip().lower(): int(w["id"]) for w in self.list_wallets()
            }
            for item in handback_states:
                if not isinstance(item, dict):
                    continue
                addr = str(item.get("wallet_address") or "").strip().lower()
                wname = str(item.get("worker_name") or "").strip()
                wid = wallet_by_addr.get(addr)
                if wid is None or not wname:
                    continue
                sent = int(item.get("offline_alert_sent") or 0)
                if sent == 1:
                    cur = self._conn.execute(
                        """
                        UPDATE worker_cache
                        SET is_currently_online = 0,
                            offline_alert_sent = 1,
                            consecutive_offline_polls = CASE
                                WHEN consecutive_offline_polls < 1 THEN 1
                                ELSE consecutive_offline_polls
                            END,
                            last_alert_at = CASE
                                WHEN ? != '' THEN ?
                                ELSE last_alert_at
                            END
                        WHERE wallet_id = ? AND worker_name = ?
                        """,
                        (
                            str(item.get("last_alert_at") or ""),
                            str(item.get("last_alert_at") or ""),
                            wid,
                            wname,
                        ),
                    )
                    updated += int(cur.rowcount or 0)
            self._conn.commit()
            return updated

    def add_alert_log(
        self,
        wallet_id: int | None,
        wallet_alias: str,
        event_type: str,
        worker_names: str,
        title: str,
        summary: str,
        provider: str,
        source: str,
        ok: bool,
        error: str = "",
        created_at: str = "",
        dedupe: bool = False,
    ) -> int | None:
        """Record an alert send attempt in SQLite, optionally skipping duplicate standby merges."""
        ts = (created_at or "").strip() or _now()
        with self._lock:
            if dedupe:
                dup = self._conn.execute(
                    "SELECT id FROM alert_logs WHERE created_at = ? AND event_type = ? "
                    "AND worker_names = ? AND source = ? LIMIT 1",
                    (ts, event_type, worker_names, source),
                ).fetchone()
                if dup:
                    return None
            cur = self._conn.execute(
                """
                INSERT INTO alert_logs (
                    created_at, wallet_id, wallet_alias, event_type, worker_names,
                    title, summary, provider, source, ok, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    wallet_id,
                    (wallet_alias or "").strip(),
                    (event_type or "offline").strip(),
                    (worker_names or "").strip(),
                    (title or "").strip(),
                    (summary or "").strip(),
                    (provider or "pushplus").strip(),
                    (source or "desktop").strip(),
                    1 if ok else 0,
                    (error or "").strip(),
                ),
            )
            new_id = int(cur.lastrowid)
            self._conn.execute(
                "DELETE FROM alert_logs WHERE id NOT IN ("
                "SELECT id FROM alert_logs ORDER BY id DESC LIMIT 200)"
            )
            self._conn.commit()
            return new_id

    def list_alert_logs(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alert_logs ORDER BY id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_alert_logs(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM alert_logs")
            self._conn.commit()

    def get_standby_sync_payload(self) -> dict[str, Any]:
        """Build full alert config, active wallets, and worker state snapshot for Docker standby."""
        with self._lock:
            cfg = self.get_alert_config()
            members, prefixes, bindings = self._load_classification_context()
            wallets = self.list_wallets()
            active_wallets = [w for w in wallets if w.get("is_active", 1)]
            portable_wallets = [
                {"address": w["address"], "alias": w["alias"]} for w in active_wallets
            ]
            worker_states: list[dict[str, Any]] = []
            for w in active_wallets:
                wid = int(w["id"])
                rows = self._conn.execute(
                    "SELECT * FROM worker_cache WHERE wallet_id = ?", (wid,)
                ).fetchall()
                for r in rows:
                    m_obj, _ = classify_worker(
                        r["worker_name"], members, prefixes, bindings, wallet_id=wid
                    )
                    worker_states.append(
                        {
                            "wallet_address": w["address"],
                            "wallet_alias": w["alias"],
                            "worker_name": r["worker_name"],
                            "member_name": m_obj.name if m_obj else "未分配",
                            "is_online": int(r["is_currently_online"] or 0),
                            "last_shares": int(r["last_shares"] or 0),
                            "last_seen_online_at": r["last_seen_online_at"] or "",
                            "consecutive_offline_polls": int(r["consecutive_offline_polls"] or 0),
                            "offline_alert_sent": int(r["offline_alert_sent"] or 0),
                            "last_alert_at": r["last_alert_at"] or "",
                            "alert_enabled": int(r["alert_enabled"] if r["alert_enabled"] is not None else 1),
                            "remote_tool": r["remote_tool"] or "ToDesk",
                            "remote_code": r["remote_code"] or "",
                            "remote_password": r["remote_password"] or "",
                            "remote_note": r["remote_note"] or "",
                        }
                    )
            return {
                "alert_config": cfg,
                "wallets": portable_wallets,
                "worker_states": worker_states,
            }
