"""PyWebView JS-to-Python API bridge."""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import subprocess
import webbrowser
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .alert_engine import (
    format_test_alert_message,
    normalize_alert_config,
    send_alert_message,
)
from .calculator import (
    allocate_exact_amounts,
    classify_worker,
    compute_worker_delta,
    get_pipeline_presets_meta,
    suggest_prefix,
    summarize_fees_and_rates,
)
from .collector_kits import export_client_kit, export_server_bundle
from .collector_server import CollectorServerController
from .db import Database
from .kernel_manager import (
    DEFAULT_GITHUB_REPO,
    KernelUpdateManager,
    compare_versions,
    extract_bat_config,
    fetch_github_releases,
    inspect_kernel_dir,
    normalize_github_repo,
    render_bat_content,
    save_bat_file,
)
from .miner_binder import LocalMinerBinder, scan_running_srbminer_processes
from .paths import get_data_dir, get_export_dir
from .poller import BackgroundPoller
from .scraper import format_hashrate_ths
from .srb_parser import parse_bat_file
import requests


def _parse_ts_safe(ts_str: str) -> datetime | None:
    txt = (ts_str or "").strip()
    if not txt:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def _infer_pool_paid_snapshot(
    ov: dict[str, Any],
    checkpoint_time: str,
    last_fetched_at: str = "",
) -> float | None:
    """Infer cumulative pool paid PRL at ``checkpoint_time`` without guessing on far-backdated points."""
    pool_total_paid = float(ov.get("total_paid_prl") or 0.0)
    if pool_total_paid <= 0:
        return None

    clean_t = (checkpoint_time or "").strip()
    payments = ov.get("payments") or []
    if not clean_t:
        return round(pool_total_paid, 6)

    if payments:
        future_pays = [p for p in payments if str(p.get("paid_at") or "") > clean_t]
        has_older_or_exact = any(
            str(p.get("paid_at") or "") <= clean_t for p in payments if p.get("paid_at")
        )
        if not future_pays or has_older_or_exact:
            future_sum = sum(float(p.get("amount_prl") or 0.0) for p in future_pays)
            return round(max(0.0, pool_total_paid - future_sum), 6)
        return None

    # No payment table cached yet: only stamp current pool total if checkpoint is near present/last fetch
    cp_dt = _parse_ts_safe(clean_t)
    ref_dt = _parse_ts_safe(last_fetched_at) or datetime.now()
    if cp_dt is not None and abs((ref_dt - cp_dt).total_seconds()) <= 7200:
        return round(pool_total_paid, 6)
    return None


def _compute_round_pending_info(
    ov: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    prl_price: float,
) -> dict[str, Any]:
    """Compute PRL accumulated from last withdrawal/baseline toward the next settlement."""
    unpaid_prl = float(ov.get("unpaid_prl") or 0.0)
    immature_prl = float(ov.get("immature_prl") or 0.0)
    pool_pending_prl = round(unpaid_prl + immature_prl, 6)
    pool_total_paid_prl = round(float(ov.get("total_paid_prl") or 0.0), 6)
    payments = ov.get("payments") or []
    latest_cp = checkpoints[0] if checkpoints else None

    if not latest_cp:
        baseline_pool_paid_prl: float | None = 0.0
        paid_since_baseline_prl = pool_total_paid_prl
        source = "from_genesis"
    elif latest_cp.get("pool_paid_snapshot") is not None:
        baseline_pool_paid_prl = round(float(latest_cp["pool_paid_snapshot"]), 6)
        paid_since_baseline_prl = round(max(0.0, pool_total_paid_prl - baseline_pool_paid_prl), 6)
        source = "snapshot"
    else:
        cp_time = str(latest_cp.get("checkpoint_time") or "")
        newer_pays = [p for p in payments if str(p.get("paid_at") or "") > cp_time]
        has_older_pay = any(
            str(p.get("paid_at") or "") <= cp_time for p in payments if p.get("paid_at")
        )
        newer_sum = round(sum(float(p.get("amount_prl") or 0.0) for p in newer_pays), 6)
        withdrawals = [c for c in checkpoints if c.get("kind") == "withdrawal"]
        has_baseline_before = any(c.get("kind") == "baseline" for c in checkpoints)
        all_w_have_prl = bool(withdrawals) and all(
            float(c.get("prl_amount") or 0.0) > 0 for c in withdrawals
        )
        sum_w_prl = round(sum(float(c.get("prl_amount") or 0.0) for c in withdrawals), 6)

        if has_older_pay:
            if (
                len(newer_pays) >= 6
                and all_w_have_prl
                and not has_baseline_before
                and pool_total_paid_prl > sum_w_prl + newer_sum
            ):
                paid_since_baseline_prl = round(max(newer_sum, pool_total_paid_prl - sum_w_prl), 6)
                baseline_pool_paid_prl = round(max(0.0, pool_total_paid_prl - paid_since_baseline_prl), 6)
                source = "total_minus_withdrawals"
            else:
                paid_since_baseline_prl = newer_sum
                baseline_pool_paid_prl = (
                    round(max(0.0, pool_total_paid_prl - newer_sum), 6)
                    if pool_total_paid_prl >= newer_sum
                    else None
                )
                source = "payments_after_cp"
        elif all_w_have_prl and not has_baseline_before:
            paid_since_baseline_prl = round(max(0.0, pool_total_paid_prl - sum_w_prl), 6)
            baseline_pool_paid_prl = sum_w_prl
            source = "total_minus_withdrawals"
        else:
            paid_since_baseline_prl = newer_sum
            baseline_pool_paid_prl = None
            source = "partial_payments"

    round_est_total_prl = round(paid_since_baseline_prl + pool_pending_prl, 6)
    round_est_total_cny = round(round_est_total_prl * prl_price, 2) if prl_price > 0 else 0.0

    return {
        "pool_total_paid_prl": pool_total_paid_prl,
        "baseline_pool_paid_prl": baseline_pool_paid_prl,
        "paid_since_baseline_prl": paid_since_baseline_prl,
        "pool_pending_prl": pool_pending_prl,
        "round_est_total_prl": round_est_total_prl,
        "round_est_total_cny": round_est_total_cny,
        "source": source,
        "checkpoint_id": latest_cp["id"] if latest_cp else None,
        "checkpoint_time": latest_cp["checkpoint_time"] if latest_cp else None,
        "checkpoint_label": latest_cp["label"] if latest_cp else "",
    }


SYNC_TOKEN_PREFIX = "PEARLSYNC1:"


def encode_sync_text(bundle: dict[str, Any]) -> str:
    """Encode a portable sync bundle as a compact clipboard-safe ASCII token."""
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    b64 = base64.urlsafe_b64encode(compressed).decode("ascii")
    return f"{SYNC_TOKEN_PREFIX}{b64}"


def decode_sync_payload(raw_text: str) -> dict[str, Any]:
    """Decode either a ``PEARLSYNC1:...`` clipboard token or raw JSON sync text."""
    txt = (raw_text or "").strip().lstrip("\ufeff")
    if not txt:
        raise ValueError("请先粘贴同步码或选择同步文件")

    if txt.startswith(SYNC_TOKEN_PREFIX):
        payload_b64 = re.sub(r"\s+", "", txt[len(SYNC_TOKEN_PREFIX) :])
        pad = (-len(payload_b64)) % 4
        if pad:
            payload_b64 += "=" * pad
        try:
            raw_bytes = zlib.decompress(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
            obj = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"同步码损坏或不完整：{exc}") from exc
    else:
        try:
            obj = json.loads(txt)
        except Exception as exc:
            raise ValueError(f"无法识别为有效的同步码或 JSON 文件：{exc}") from exc

    if not isinstance(obj, dict) or obj.get("format") != "pearl_split_sync_v1":
        raise ValueError("内容不是有效的 PearlSplit 提现同步数据 (pearl_split_sync_v1)")
    return obj


class DesktopApi:
    """Exposed as ``window.pywebview.api`` inside the WebView frontend."""

    def __init__(
        self,
        db: Database,
        poller: BackgroundPoller,
        miner_binder: LocalMinerBinder | None = None,
        collector_server: CollectorServerController | None = None,
    ) -> None:
        self.db = db
        self.poller = poller
        self.miner_binder = miner_binder or LocalMinerBinder(db.get_settings)
        self.collector_server = collector_server or CollectorServerController(get_data_dir() / "collector_telemetry.db")
        self.kernel_manager = KernelUpdateManager()
        self._window = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    # ------------------------------------------------------------------
    # State & Refresh
    # ------------------------------------------------------------------
    def get_state(self, wallet_id: int | None = None) -> dict[str, Any]:
        wallets = self.db.list_wallets()
        settings = self.db.get_settings()

        if wallet_id is None:
            try:
                saved_id = int(settings.get("active_wallet_id", "0"))
            except ValueError:
                saved_id = 0
            active = next((w for w in wallets if w["id"] == saved_id), wallets[0] if wallets else None)
        else:
            active = next((w for w in wallets if w["id"] == int(wallet_id)), wallets[0] if wallets else None)
            if active:
                self.db.update_settings({"active_wallet_id": str(active["id"])})

        active_id = active["id"] if active else None
        workers = self.db.get_wallet_workers(active_id) if active_id else []
        members = self.db.list_members()
        checkpoints = self.db.list_checkpoints(active_id) if active_id else []
        drafts = self.db.list_settlement_drafts(active_id) if active_id else []
        global_totals = self.db.get_global_member_totals()

        # Group current workers by member for instant overview
        member_live_summary: dict[str, dict[str, Any]] = {}
        unassigned_workers = []
        for m in members:
            member_live_summary[f"id:{m['id']}"] = {
                "member_id": m["id"],
                "member_name": m["name"],
                "member_color": m["color"],
                "online_count": 0,
                "offline_cached_count": 0,
                "total_shares": 0,
                "round_delta_shares": 0,
                "live_hashrate_ths": 0.0,
                "avg_24h_hashrate_ths": 0.0,
            }

        for w in workers:
            if w["member_id"] is None:
                unassigned_workers.append(
                    {
                        **w,
                        "suggested_prefix": suggest_prefix(w["worker_name"]),
                    }
                )
                key = "unassigned"
                if key not in member_live_summary:
                    member_live_summary[key] = {
                        "member_id": None,
                        "member_name": "未分配",
                        "member_color": "#64748b",
                        "online_count": 0,
                        "offline_cached_count": 0,
                        "total_shares": 0,
                        "round_delta_shares": 0,
                        "live_hashrate_ths": 0.0,
                        "avg_24h_hashrate_ths": 0.0,
                    }
            else:
                key = f"id:{w['member_id']}"

            bucket = member_live_summary[key]
            if w["is_currently_online"]:
                bucket["online_count"] += 1
            else:
                bucket["offline_cached_count"] += 1
            bucket["total_shares"] += int(w["last_shares"])
            bucket["round_delta_shares"] += int(w.get("round_delta_shares", w["last_shares"]))
            bucket["live_hashrate_ths"] += float(w.get("live_hashrate_ths", 0.0))
            bucket["avg_24h_hashrate_ths"] += float(w.get("avg_24h_hashrate_ths", 0.0))

        total_all_shares = sum(b["total_shares"] for b in member_live_summary.values())
        total_round_delta = sum(b["round_delta_shares"] for b in member_live_summary.values())
        ov = (active.get("overview") if active else None) or {}
        prl_price = float(ov.get("prl_price_cny") or 0.0)
        round_pending_info = _compute_round_pending_info(ov, checkpoints, prl_price)
        pool_pending_prl = float(round_pending_info["pool_pending_prl"])
        paid_pending_prl = float(round_pending_info["paid_since_baseline_prl"])
        round_est_total_prl = float(round_pending_info["round_est_total_prl"])

        for bucket in member_live_summary.values():
            t_ratio = (bucket["total_shares"] / total_all_shares) if total_all_shares > 0 else 0.0
            r_ratio = (
                (bucket["round_delta_shares"] / total_round_delta)
                if total_round_delta > 0
                else t_ratio
            )
            bucket["total_ratio"] = t_ratio
            bucket["round_ratio"] = r_ratio
            bucket["live_hashrate_ths"] = round(bucket["live_hashrate_ths"], 2)
            bucket["avg_24h_hashrate_ths"] = round(bucket["avg_24h_hashrate_ths"], 2)
            bucket["live_hashrate_text"] = format_hashrate_ths(bucket["live_hashrate_ths"])
            bucket["avg_24h_hashrate_text"] = format_hashrate_ths(bucket["avg_24h_hashrate_ths"])
            bucket["est_pool_pending_prl"] = round(pool_pending_prl * r_ratio, 6)
            bucket["est_paid_pending_prl"] = round(paid_pending_prl * r_ratio, 6)
            bucket["est_unpaid_prl"] = round(round_est_total_prl * r_ratio, 6)
            bucket["est_unpaid_cny"] = (
                round(bucket["est_unpaid_prl"] * prl_price, 2) if prl_price > 0 else 0.0
            )

        latest_cp = checkpoints[0] if checkpoints else None
        latest_baseline_info = (
            {
                "id": latest_cp["id"],
                "checkpoint_time": latest_cp["checkpoint_time"],
                "kind": latest_cp["kind"],
                "label": latest_cp["label"],
                "total_round_delta": total_round_delta,
                "total_all_shares": total_all_shares,
            }
            if latest_cp
            else {
                "id": None,
                "checkpoint_time": None,
                "kind": None,
                "label": "从 0 份额起算 (尚未建立提现基准)",
                "total_round_delta": total_round_delta,
                "total_all_shares": total_all_shares,
            }
        )

        return {
            "needs_onboarding": len(wallets) == 0,
            "wallets": wallets,
            "active_wallet": active,
            "workers": workers,
            "unassigned_workers": unassigned_workers,
            "member_live_summary": list(member_live_summary.values()),
            "latest_baseline_info": latest_baseline_info,
            "round_pending_info": round_pending_info,
            "members": members,
            "checkpoints": checkpoints,
            "drafts": drafts,
            "exchange_trades": self.db.list_exchange_trades(None),
            "residual_summary": self.db.get_residual_summary(),
            "fee_pipeline_templates": self.db.get_fee_pipeline_templates(),
            "fee_pipeline_presets": get_pipeline_presets_meta(),
            "global_totals": global_totals,
            "settings": settings,
            "alert_config": self.db.get_alert_config(),
            "alert_logs": self.db.list_alert_logs(limit=80),
            "poller": self.poller.get_status(),
            "collector_server": self.collector_server.get_status(),
            "events": self.poller.pop_events(),
            "data_dir": str(get_data_dir()),
        }

    def refresh_wallet(self, wallet_id: int) -> dict[str, Any]:
        res = self.poller.fetch_wallet_once(int(wallet_id), include_payments=True)
        return {"refresh": res, "state": self.get_state(int(wallet_id))}

    def set_watch_mode(self, enabled: bool, wallet_id: int | None = None) -> dict[str, Any]:
        status = self.poller.set_watch_mode(
            bool(enabled), int(wallet_id) if wallet_id is not None else None
        )
        return status

    def update_settings(self, updates: dict[str, Any]) -> dict[str, str]:
        res = self.db.update_settings(updates)
        if any(k in updates for k in ("auto_poll_enabled", "auto_poll_interval_sec")):
            self.poller.trigger_now()
        return res

    # ------------------------------------------------------------------
    # Wallets
    # ------------------------------------------------------------------
    def add_wallet(
        self, address_or_url: str, alias: str = "", notes: str = "", wallet_type: str = "official"
    ) -> dict[str, Any]:
        wallet = self.db.add_wallet(address_or_url, alias=alias, notes=notes, wallet_type=wallet_type)
        try:
            self.poller.fetch_wallet_once(wallet["id"], include_payments=True)
        except Exception:
            pass
        return self.get_state(wallet["id"])

    def update_wallet(
        self,
        wallet_id: int,
        alias: str,
        notes: str = "",
        is_active: bool = True,
        wallet_type: str | None = None,
    ) -> dict[str, Any]:
        self.db.update_wallet(
            int(wallet_id),
            alias=alias,
            notes=notes,
            is_active=1 if is_active else 0,
            wallet_type=wallet_type,
        )
        return self.get_state(int(wallet_id))

    def delete_wallet(self, wallet_id: int) -> dict[str, Any]:
        self.db.delete_wallet(int(wallet_id))
        return self.get_state(None)

    def complete_onboarding(self, payload: dict[str, Any]) -> dict[str, Any]:
        """First-run onboarding wizard: add primary ntminerpool wallet, optional members, and fee preset."""
        src = payload if isinstance(payload, dict) else {}
        addr = str(src.get("address_or_url") or src.get("address") or "").strip()
        if not addr:
            raise ValueError("请输入奈特矿池 (ntminerpool.com) 钱包地址或 Dashboard 链接")
        alias = str(src.get("alias") or "").strip() or "主钱包"
        notes = str(src.get("notes") or "").strip()
        wtype = str(src.get("wallet_type") or "official").strip()

        preset_key = str(src.get("pipeline_preset") or "").strip()
        if preset_key:
            self.db.apply_fee_pipeline_preset(preset_key)

        for m in list(src.get("members") or []):
            if not isinstance(m, dict):
                continue
            mname = str(m.get("name") or "").strip()
            if not mname:
                continue
            mcol = str(m.get("color") or "#10b981").strip() or "#10b981"
            raw_pf = m.get("prefixes") or []
            if isinstance(raw_pf, str):
                prefixes = [x.strip() for x in re.split(r"[,，\s]+", raw_pf) if x.strip()]
            else:
                prefixes = [str(x).strip() for x in raw_pf if str(x).strip()]
            try:
                self.db.create_member(name=mname, color=mcol, prefixes=prefixes, notes="")
            except Exception:
                pass

        wallet = self.db.add_wallet(addr, alias=alias, notes=notes, wallet_type=wtype)
        try:
            self.poller.fetch_wallet_once(wallet["id"], include_payments=True)
        except Exception:
            pass
        self.poller.trigger_now()
        return {"wallet": wallet, "state": self.get_state(wallet["id"])}

    # ------------------------------------------------------------------
    # Members, Prefixes & Bindings
    # ------------------------------------------------------------------
    def create_member(self, name: str, color: str = "#10b981", prefixes: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        self.db.create_member(name=name, color=color, prefixes=prefixes or [], notes=notes)
        return self.get_state(None)

    def update_member(self, member_id: int, name: str, color: str, notes: str = "") -> dict[str, Any]:
        self.db.update_member(int(member_id), name=name, color=color, notes=notes)
        return self.get_state(None)

    def delete_member(self, member_id: int) -> dict[str, Any]:
        self.db.delete_member(int(member_id))
        return self.get_state(None)

    def add_prefix(self, member_id: int, prefix: str) -> dict[str, Any]:
        self.db.add_prefix(int(member_id), prefix=prefix)
        return self.get_state(None)

    def delete_prefix(self, prefix_id: int) -> dict[str, Any]:
        self.db.delete_prefix(int(prefix_id))
        return self.get_state(None)

    def bind_worker(self, worker_name: str, member_id: int, wallet_id: int | None = None) -> dict[str, Any]:
        self.db.bind_worker(
            worker_name=worker_name,
            member_id=int(member_id),
            wallet_id=int(wallet_id) if wallet_id is not None else None,
        )
        return self.get_state(None)

    def delete_worker_binding(self, binding_id: int) -> dict[str, Any]:
        self.db.delete_worker_binding(int(binding_id))
        return self.get_state(None)

    def quick_assign_worker(
        self,
        worker_name: str,
        mode: str,
        member_id: int | None = None,
        new_member_name: str = "",
        new_member_color: str = "#06b6d4",
        prefix: str = "",
        wallet_id: int | None = None,
    ) -> dict[str, Any]:
        """Assign an unknown worker via one of:
        - ``bind_worker``: bind exact worker name to existing member
        - ``add_prefix``: add prefix to existing member for future auto-recognition
        - ``new_member_prefix``: create new member + bind prefix
        - ``new_member_worker``: create new member + bind exact worker
        """
        if mode in ("new_member_prefix", "new_member_worker"):
            created = self.db.create_member(
                name=new_member_name,
                color=new_member_color,
                prefixes=[prefix] if (mode == "new_member_prefix" and prefix.strip()) else [],
            )
            if mode == "new_member_worker":
                self.db.bind_worker(worker_name, created["id"], wallet_id=None)
        elif mode == "add_prefix":
            if member_id is None:
                raise ValueError("请选择目标成员")
            self.db.add_prefix(int(member_id), prefix)
        elif mode == "bind_worker":
            if member_id is None:
                raise ValueError("请选择目标成员")
            self.db.bind_worker(
                worker_name,
                int(member_id),
                wallet_id=int(wallet_id) if wallet_id is not None else None,
            )
        else:
            raise ValueError(f"未知分配模式: {mode}")

        return self.get_state(int(wallet_id) if wallet_id is not None else None)

    # ------------------------------------------------------------------
    # Offline Worker Cache Manual Management
    # ------------------------------------------------------------------
    def upsert_manual_worker(self, wallet_id: int, worker_name: str, shares: int) -> dict[str, Any]:
        self.db.upsert_manual_worker_cache(int(wallet_id), worker_name=worker_name, shares=int(shares))
        return self.get_state(int(wallet_id))

    def delete_cached_worker(self, wallet_id: int, worker_name: str) -> dict[str, Any]:
        self.db.delete_cached_worker(int(wallet_id), worker_name=worker_name)
        return self.get_state(int(wallet_id))

    # ------------------------------------------------------------------
    # Settlement & Checkpoints
    # ------------------------------------------------------------------
    def preview_settlement(self, wallet_id: int, prev_checkpoint_id: int | None = None) -> dict[str, Any]:
        return self.db.preview_settlement(
            int(wallet_id),
            prev_checkpoint_id=int(prev_checkpoint_id) if prev_checkpoint_id else None,
        )

    def calculate_preview_split(
        self,
        worker_entries: list[dict[str, Any]],
        cny_amount: float,
        prl_amount: float = 0.0,
    ) -> dict[str, Any]:
        """Compute live split preview using exact backend rounding rules."""
        members = {m["id"]: m for m in self.db.list_members()}
        agg: dict[str, dict[str, Any]] = {}
        total_delta = 0
        computed_workers = []

        for entry in worker_entries:
            prev_s = max(0, int(entry.get("prev_shares", 0)))
            curr_s = max(0, int(entry.get("curr_shares", 0)))
            treat_reset = bool(entry.get("treat_reset_as_curr", False))
            if entry.get("override_delta") is not None:
                delta = max(0, int(entry["override_delta"]))
                reset_detected = curr_s < prev_s
            else:
                delta, reset_detected = compute_worker_delta(
                    prev_s, curr_s, treat_reset_as_curr=treat_reset
                )

            mid = entry.get("member_id")
            if mid is not None and int(mid) in members:
                m = members[int(mid)]
                m_id = m["id"]
                m_name = m["name"]
                m_color = m["color"]
            else:
                m_id = None
                m_name = entry.get("member_name") or "未分配"
                m_color = entry.get("member_color") or "#64748b"

            computed_workers.append(
                {
                    **entry,
                    "delta_shares": delta,
                    "reset_detected": reset_detected,
                }
            )
            key = f"id:{m_id}" if m_id is not None else "unassigned"
            if key not in agg:
                agg[key] = {
                    "member_id": m_id,
                    "member_name": m_name,
                    "member_color": m_color,
                    "delta_shares": 0,
                    "worker_count": 0,
                    "active_worker_count": 0,
                }
            agg[key]["delta_shares"] += delta
            agg[key]["worker_count"] += 1
            if delta > 0:
                agg[key]["active_worker_count"] += 1
            total_delta += delta

        allocations = allocate_exact_amounts(
            list(agg.values()),
            total_cny=float(cny_amount or 0.0),
            total_prl=float(prl_amount or 0.0),
        )
        allocations.sort(key=lambda a: (-a["delta_shares"], a["member_name"]))
        return {
            "total_delta_shares": total_delta,
            "workers": computed_workers,
            "allocations": allocations,
        }

    def save_checkpoint_and_settle(self, payload: dict[str, Any]) -> dict[str, Any]:
        wallet_id = int(payload["wallet_id"])
        kind = str(payload.get("kind") or "withdrawal")
        cp_time = str(payload.get("checkpoint_time") or "")
        explicit_snap = payload.get("pool_paid_snapshot")
        if explicit_snap is not None and str(explicit_snap).strip() != "":
            pool_paid_snap: float | None = float(explicit_snap)
        else:
            w = next((item for item in self.db.list_wallets() if item["id"] == wallet_id), None)
            w_ov = (w.get("overview") if w else None) or {}
            w_fetched = str((w.get("last_fetched_at") if w else "") or "")
            pool_paid_snap = _infer_pool_paid_snapshot(w_ov, cp_time, w_fetched)

        saved = self.db.save_checkpoint_and_settle(
            wallet_id=wallet_id,
            kind=kind,
            checkpoint_time=cp_time,
            label=str(payload.get("label") or ""),
            prl_amount=float(payload.get("prl_amount") or 0.0),
            cny_amount=float(payload.get("cny_amount") or 0.0),
            tx_hash=str(payload.get("tx_hash") or ""),
            notes=str(payload.get("notes") or ""),
            prev_checkpoint_id=(
                int(payload["prev_checkpoint_id"]) if payload.get("prev_checkpoint_id") else None
            ),
            worker_entries=list(payload.get("worker_entries") or []),
            fees=payload.get("fees") if isinstance(payload.get("fees"), dict) else None,
            pool_paid_snapshot=pool_paid_snap,
        )
        if kind == "withdrawal" and payload.get("consume_draft_id"):
            try:
                self.db.delete_settlement_draft(int(payload["consume_draft_id"]))
            except Exception:
                pass
        return {"saved": saved, "state": self.get_state(wallet_id)}

    def save_settlement_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        saved = self.db.save_settlement_draft(payload)
        return {"draft": saved, "state": self.get_state(saved["wallet_id"])}

    def load_settlement_draft(self, draft_id: int) -> dict[str, Any]:
        return self.db.get_settlement_draft(int(draft_id))

    def delete_settlement_draft(self, draft_id: int, wallet_id: int | None = None) -> dict[str, Any]:
        self.db.delete_settlement_draft(int(draft_id))
        return self.get_state(int(wallet_id) if wallet_id is not None else None)

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
        updated = self.db.update_settlement_amounts(
            checkpoint_id=int(checkpoint_id),
            cny_amount=float(cny_amount or 0.0),
            prl_amount=float(prl_amount or 0.0),
            label=label,
            tx_hash=tx_hash,
            notes=notes,
            fees=fees,
        )
        return {"updated": updated, "state": self.get_state(updated["wallet_id"])}

    def add_exchange_trade(self, payload: dict[str, Any], active_wallet_id: int | None = None) -> dict[str, Any]:
        trade = self.db.add_exchange_trade(payload)
        wid = int(active_wallet_id) if active_wallet_id is not None else trade.get("wallet_id")
        return {"trade": trade, "state": self.get_state(wid)}

    def delete_exchange_trade(self, trade_id: int, active_wallet_id: int | None = None) -> dict[str, Any]:
        self.db.delete_exchange_trade(int(trade_id))
        return self.get_state(int(active_wallet_id) if active_wallet_id is not None else None)

    def calibrate_residual_balance(
        self,
        balances: dict[str, Any] | None = None,
        active_wallet_id: int | None = None,
    ) -> dict[str, Any]:
        summary = self.db.calibrate_residual_balance(balances=balances)
        return {
            "residual_summary": summary,
            "state": self.get_state(int(active_wallet_id) if active_wallet_id is not None else None),
        }

    def save_fee_pipeline_templates(
        self,
        steps: list[dict[str, Any]],
        active_wallet_id: int | None = None,
    ) -> dict[str, Any]:
        saved = self.db.save_fee_pipeline_templates(steps)
        return {
            "templates": saved,
            "state": self.get_state(int(active_wallet_id) if active_wallet_id is not None else None),
        }

    def apply_fee_pipeline_preset(
        self,
        preset_key: str,
        active_wallet_id: int | None = None,
    ) -> dict[str, Any]:
        saved = self.db.apply_fee_pipeline_preset(preset_key)
        return {
            "templates": saved,
            "state": self.get_state(int(active_wallet_id) if active_wallet_id is not None else None),
        }

    def summarize_settlement_fees(
        self,
        fees: dict[str, Any] | None,
        wallet_type: str = "official",
        cny_amount: float = 0.0,
        prl_amount: float = 0.0,
        prl_ref_cny: float = 0.0,
    ) -> dict[str, Any]:
        payload = dict(fees or {})
        payload["wallet_type"] = wallet_type
        return summarize_fees_and_rates(
            payload,
            cny_amount=float(cny_amount or 0.0),
            prl_amount=float(prl_amount or 0.0),
            prl_ref_cny=float(prl_ref_cny or 0.0),
        )

    def update_baseline_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        has_snap = "pool_paid_snapshot" in payload
        raw_snap = payload.get("pool_paid_snapshot")
        snap_val = float(raw_snap) if (has_snap and raw_snap not in (None, "")) else None
        updated = self.db.update_baseline_checkpoint(
            checkpoint_id=int(payload["checkpoint_id"]),
            checkpoint_time=str(payload.get("checkpoint_time") or ""),
            label=str(payload.get("label") or ""),
            notes=str(payload.get("notes") or ""),
            worker_entries=list(payload.get("worker_entries") or []),
            pool_paid_snapshot=snap_val,
            update_pool_paid_snapshot=has_snap,
        )
        return {"updated": updated, "state": self.get_state(updated["wallet_id"])}

    def set_checkpoint_pool_snapshot(
        self,
        wallet_id: int,
        checkpoint_id: int,
        pool_paid_snapshot: float | None = None,
    ) -> dict[str, Any]:
        snap_val = float(pool_paid_snapshot) if pool_paid_snapshot not in (None, "") else None
        updated = self.db.set_checkpoint_pool_snapshot(int(checkpoint_id), snap_val)
        return {"updated": updated, "state": self.get_state(int(wallet_id))}

    def delete_checkpoint(self, wallet_id: int, checkpoint_id: int) -> dict[str, Any]:
        self.db.delete_checkpoint(int(checkpoint_id))
        return self.get_state(int(wallet_id))

    # ------------------------------------------------------------------
    # Exports & Utilities
    # ------------------------------------------------------------------
    def export_settlements_csv(self, wallet_id: int | None = None) -> dict[str, Any]:
        all_rows = self.db.get_all_settlements()
        if wallet_id is not None:
            all_rows = [r for r in all_rows if r["wallet_id"] == int(wallet_id)]

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"pearl_settlements_{stamp}.csv"
        save_path: Path | None = None

        if self._window is not None:
            try:
                import webview

                selected = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    directory=str(get_export_dir()),
                    save_filename=default_name,
                    file_types=("CSV 文件 (*.csv)",),
                )
                if selected:
                    chosen = selected[0] if isinstance(selected, (list, tuple)) else selected
                    save_path = Path(chosen)
                else:
                    return {"cancelled": True}
            except Exception:
                save_path = None

        if save_path is None:
            save_path = get_export_dir() / default_name

        with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "结算ID",
                    "类型",
                    "钱包别名",
                    "钱包地址",
                    "提现时间",
                    "标签",
                    "提现PRL",
                    "实收人民币(CNY)",
                    "总增量份额",
                    "成员",
                    "成员增量份额",
                    "占比(%)",
                    "应得PRL",
                    "应得人民币(CNY)",
                    "交易哈希",
                    "流程手续费明细",
                    "折合总损耗(CNY)",
                    "残余变动明细",
                    "备注",
                ]
            )
            for r in all_rows:
                allocs = r.get("allocations") or []
                fs = r.get("fee_summary") or {}
                fobj = r.get("fees") or {}
                loss_cny = fs.get("total_loss_cny") or fs.get("estimated_fee_cny") or 0
                fee_desc = "; ".join(
                    f"{it.get('label', '')}:{it.get('amount', 0)}{it.get('unit', '')}"
                    for it in (fs.get("fee_items") or [])
                )
                res_desc = "; ".join(
                    f"{k}:{'+' if float(v) > 0 else ''}{v}"
                    for k, v in (fobj.get("residual_deltas") or {}).items()
                    if abs(float(v or 0)) > 1e-8
                )
                if not allocs:
                    writer.writerow(
                        [
                            r["id"],
                            "基准快照" if r["kind"] == "baseline" else "提现结算",
                            r["wallet_alias"],
                            r["wallet_address"],
                            r["checkpoint_time"],
                            r["label"],
                            r["prl_amount"],
                            r["cny_amount"],
                            r["total_delta_shares"],
                            "-",
                            0,
                            "0.00",
                            0,
                            0,
                            r["tx_hash"],
                            fee_desc,
                            loss_cny,
                            res_desc,
                            r["notes"],
                        ]
                    )
                else:
                    for al in allocs:
                        writer.writerow(
                            [
                                r["id"],
                                "提现结算",
                                r["wallet_alias"],
                                r["wallet_address"],
                                r["checkpoint_time"],
                                r["label"],
                                r["prl_amount"],
                                r["cny_amount"],
                                r["total_delta_shares"],
                                al["member_name"],
                                al["delta_shares"],
                                f"{al['ratio'] * 100:.2f}",
                                f"{al['prl_earned']:.6f}",
                                f"{al['cny_earned']:.2f}",
                                r["tx_hash"],
                                fee_desc,
                                loss_cny,
                                res_desc,
                                r["notes"],
                            ]
                        )

        return {"cancelled": False, "path": str(save_path)}

    def export_backup_json(self) -> dict[str, Any]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = get_export_dir() / f"pearl_backup_{stamp}.json"
        payload = {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "wallets": self.db.list_wallets(),
            "members": self.db.list_members(),
            "settlements": self.db.get_all_settlements(),
            "settings": self.db.get_settings(),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(out_path)}

    def get_wallet_sync_bundle(
        self,
        wallet_id: int,
        checkpoint_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        clean_ids = [int(x) for x in checkpoint_ids] if checkpoint_ids else None
        bundle = self.db.export_sync_bundle(int(wallet_id), checkpoint_ids=clean_ids)
        sync_code = encode_sync_text(bundle)
        cps = bundle.get("checkpoints") or []
        return {
            "bundle": bundle,
            "sync_code": sync_code,
            "wallet_alias": bundle["wallet"]["alias"],
            "wallet_address": bundle["wallet"]["address"],
            "checkpoint_count": len(cps),
            "withdrawal_count": sum(1 for c in cps if c.get("kind") == "withdrawal"),
            "baseline_count": sum(1 for c in cps if c.get("kind") == "baseline"),
        }

    def export_sync_file(
        self,
        wallet_id: int,
        checkpoint_ids: list[int] | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        clean_ids = [int(x) for x in checkpoint_ids] if checkpoint_ids else None
        bundle = self.db.export_sync_bundle(int(wallet_id), checkpoint_ids=clean_ids)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_alias = re.sub(r'[\\/:*?"<>|]+', "_", bundle["wallet"]["alias"] or "wallet").strip() or "wallet"
        default_name = f"PearlSplit_同步_{safe_alias}_{stamp}.json"

        save_path: Path | None = Path(output_path) if output_path else None
        if save_path is None and self._window is not None:
            try:
                import webview

                selected = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    directory=str(get_export_dir()),
                    save_filename=default_name,
                    file_types=("PearlSplit 同步包 (*.json)",),
                )
                if selected:
                    chosen = selected[0] if isinstance(selected, (list, tuple)) else selected
                    save_path = Path(chosen)
                else:
                    return {"cancelled": True}
            except Exception:
                save_path = None

        if save_path is None:
            save_path = get_export_dir() / default_name

        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "cancelled": False,
            "path": str(save_path),
            "checkpoint_count": len(bundle.get("checkpoints") or []),
            "wallet_alias": bundle["wallet"]["alias"],
        }

    def preview_sync_import(self, sync_text: str) -> dict[str, Any]:
        bundle = decode_sync_payload(sync_text)
        w_info = bundle.get("wallet") or {}
        addr = str(w_info.get("address") or "").strip()
        existing = next(
            (w for w in self.db.list_wallets() if w["address"].strip().lower() == addr.lower()),
            None,
        )
        cps = bundle.get("checkpoints") or []
        withdrawals = [c for c in cps if c.get("kind") == "withdrawal"]
        baselines = [c for c in cps if c.get("kind") == "baseline"]
        latest_cp = cps[-1] if cps else None
        return {
            "ok": True,
            "exported_at": bundle.get("exported_at") or "",
            "wallet_address": addr,
            "wallet_alias": w_info.get("alias") or "",
            "existing_wallet_alias": existing["alias"] if existing else None,
            "checkpoint_count": len(cps),
            "withdrawal_count": len(withdrawals),
            "baseline_count": len(baselines),
            "member_count": len(bundle.get("members") or []),
            "latest_time": latest_cp.get("checkpoint_time") if latest_cp else "",
            "latest_label": latest_cp.get("label") if latest_cp else "",
        }

    def open_and_preview_sync_file(self, file_path: str | None = None) -> dict[str, Any]:
        chosen_path: Path | None = Path(file_path) if file_path else None
        if chosen_path is None:
            if self._window is None:
                raise ValueError("未指定同步文件路径")
            import webview

            selected = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(get_export_dir()),
                allow_multiple=False,
                file_types=("PearlSplit 同步文件 (*.json)", "所有文件 (*.*)"),
            )
            if not selected:
                return {"cancelled": True}
            chosen = selected[0] if isinstance(selected, (list, tuple)) else selected
            chosen_path = Path(chosen)

        raw_text = chosen_path.read_text(encoding="utf-8-sig")
        preview = self.preview_sync_import(raw_text)
        return {
            "cancelled": False,
            "file_path": str(chosen_path),
            "raw_text": raw_text,
            "preview": preview,
        }

    def import_sync_payload(self, sync_text: str, sync_members: bool = True) -> dict[str, Any]:
        bundle = decode_sync_payload(sync_text)
        res = self.db.import_sync_bundle(bundle, sync_members=bool(sync_members))
        return {
            "import_result": res,
            "state": self.get_state(res["wallet_id"]),
        }

    def open_url(self, url: str) -> None:
        if url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def open_data_dir(self) -> None:
        path = str(get_data_dir())
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def open_folder_path(self, folder: str) -> None:
        p = Path(folder)
        if p.exists():
            if os.name == "nt":
                os.startfile(str(p))
            else:
                subprocess.Popen(["xdg-open", str(p)])

    # ------------------------------------------------------------------
    # Local .bat / SRBMiner Process Output Binder
    # ------------------------------------------------------------------
    def browse_local_file(self, kind: str = "bat") -> dict[str, Any]:
        if self._window is None:
            return {"cancelled": True}
        import webview

        if kind == "log":
            ftypes = ("SRBMiner 日志文件 (*.log;*.txt)", "所有文件 (*.*)")
        else:
            ftypes = ("批处理脚本 (*.bat;*.cmd)", "所有文件 (*.*)")

        selected = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=ftypes,
        )
        if selected:
            chosen = selected[0] if isinstance(selected, (list, tuple)) else selected
            return {"cancelled": False, "path": str(chosen)}
        return {"cancelled": True}

    def inspect_bat_file(self, bat_path: str) -> dict[str, Any]:
        return parse_bat_file(bat_path)

    def scan_local_miners(self) -> list[dict[str, Any]]:
        return scan_running_srbminer_processes()

    def bind_bat_start(self, bat_path: str, auto_inject_log: bool = True) -> dict[str, Any]:
        self.db.update_settings({"local_bat_path": bat_path, "show_local_miner_panel": "1"})
        return self.miner_binder.bind_and_start_bat(bat_path, auto_inject_log=bool(auto_inject_log))

    def bind_running_pid(self, pid: int, log_path_override: str = "") -> dict[str, Any]:
        self.db.update_settings({"show_local_miner_panel": "1"})
        return self.miner_binder.attach_running_pid(int(pid), log_path_override=log_path_override)

    def bind_log_file(self, log_path: str) -> dict[str, Any]:
        self.db.update_settings({"local_log_path": log_path, "show_local_miner_panel": "1"})
        return self.miner_binder.attach_log_file(log_path)

    def stop_bound_miner(self) -> dict[str, Any]:
        self.miner_binder.stop_managed_bat()
        return self.miner_binder.get_status()

    def unbind_local_miner(self) -> dict[str, Any]:
        self.miner_binder.unbind(stop_managed_process=True)
        return self.miner_binder.get_status()

    def clear_local_miner_logs(self) -> dict[str, Any]:
        self.miner_binder.clear_logs()
        return self.miner_binder.get_status()

    def send_local_miner_stats_key(self) -> dict[str, Any]:
        sent = self.miner_binder.send_stats_key()
        return {"sent": sent}

    def get_local_miner_status(self, since_seq: int = 0) -> dict[str, Any]:
        return self.miner_binder.get_status(since_seq=int(since_seq or 0))

    def push_local_telemetry_now(self) -> dict[str, Any]:
        return self.miner_binder.push_now()

    # ------------------------------------------------------------------
    # Collector Backend Server & Distributed Cluster Clients
    # ------------------------------------------------------------------
    def _annotate_clients_with_members(self, summary: dict[str, Any]) -> dict[str, Any]:
        members, prefixes, bindings = self.db._load_classification_context()
        for c in summary.get("clients") or []:
            wname = c.get("worker_name") or c.get("client_name") or ""
            member, reason = classify_worker(wname, members, prefixes, bindings)
            c["member_id"] = member.id if member else None
            c["member_name"] = member.name if member else "未分配"
            c["member_color"] = member.color if member else "#64748b"
            c["assign_reason"] = reason
        return summary

    def get_collector_status(self) -> dict[str, Any]:
        return {
            "server": self.collector_server.get_status(),
            "settings": self.db.get_settings(),
        }

    def start_local_collector(self, host: str = "0.0.0.0", port: int = 8765, token: str = "") -> dict[str, Any]:
        p = int(port or 8765)
        tok = (token or "").strip()
        status = self.collector_server.start(host=host or "0.0.0.0", port=p, token=tok)
        self.db.update_settings(
            {
                "local_collector_enabled": "1",
                "local_collector_port": str(p),
                "collector_token": tok,
                "collector_mode": "local",
                "collector_server_url": f"http://127.0.0.1:{p}",
            }
        )
        return self.get_collector_status()

    def stop_local_collector(self) -> dict[str, Any]:
        self.collector_server.stop()
        self.db.update_settings({"local_collector_enabled": "0"})
        return self.get_collector_status()

    def fetch_collector_clients(self, server_url: str = "", token: str = "") -> dict[str, Any]:
        settings = self.db.get_settings()
        mode = settings.get("collector_mode", "local")
        url = (server_url or settings.get("collector_server_url") or "").strip().rstrip("/")
        tok = token if token != "" else (settings.get("collector_token") or "")

        store = self.collector_server.get_store()
        if (mode == "local" or not url) and store is not None:
            summary = store.list_clients()
            return self._annotate_clients_with_members({"ok": True, "source": "local", **summary})

        if mode == "local" and not server_url:
            return {
                "ok": False,
                "not_running": True,
                "error": "本机内置后端尚未启动（可点击「启动本机服务」或切换为「远程独立后端」）",
                "clients": [],
            }

        if not url:
            return {"ok": False, "error": "本地收集服务未开启，且未填写远程服务器地址", "clients": []}

        headers = {"X-Collector-Token": tok} if tok else {}
        resp = requests.get(f"{url}/api/v1/clients", headers=headers, timeout=5)
        if resp.status_code == 401:
            raise PermissionError("服务器 Token 鉴权失败，请检查访问密钥")
        resp.raise_for_status()
        summary = resp.json()
        return self._annotate_clients_with_members({"ok": True, "source": "remote", **summary})

    def fetch_collector_client_detail(
        self,
        client_id: str,
        limit: int = 250,
        server_url: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        settings = self.db.get_settings()
        mode = settings.get("collector_mode", "local")
        url = (server_url or settings.get("collector_server_url") or "").strip().rstrip("/")
        tok = token if token != "" else (settings.get("collector_token") or "")

        store = self.collector_server.get_store()
        if (mode == "local" or not url) and store is not None:
            detail = store.get_client_detail(client_id, limit=int(limit))
            if not detail:
                raise ValueError("未找到该客户端节点")
            return detail

        headers = {"X-Collector-Token": tok} if tok else {}
        resp = requests.get(
            f"{url}/api/v1/clients/{client_id}",
            params={"limit": int(limit)},
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_collector_client(self, client_id: str, server_url: str = "", token: str = "") -> dict[str, Any]:
        settings = self.db.get_settings()
        mode = settings.get("collector_mode", "local")
        url = (server_url or settings.get("collector_server_url") or "").strip().rstrip("/")
        tok = token if token != "" else (settings.get("collector_token") or "")

        store = self.collector_server.get_store()
        if (mode == "local" or not url) and store is not None:
            store.delete_client(client_id)
            return {"ok": True}

        headers = {"X-Collector-Token": tok} if tok else {}
        resp = requests.delete(f"{url}/api/v1/clients/{client_id}", headers=headers, timeout=5)
        resp.raise_for_status()
        return {"ok": True}

    def export_collector_server_bundle(self) -> dict[str, Any]:
        out_dir = get_export_dir() / "pearl_collector_server"
        res = export_server_bundle(out_dir)
        self.open_folder_path(str(out_dir))
        return res

    def export_collector_client_kit(
        self,
        server_url: str = "",
        token: str = "",
        worker_hint: str = "",
    ) -> dict[str, Any]:
        settings = self.db.get_settings()
        st = self.collector_server.get_status()
        eff_url = (server_url or "").strip()
        if not eff_url:
            if st["running"] and st["lan_urls"]:
                eff_url = st["lan_urls"][0]
            else:
                eff_url = settings.get("collector_server_url") or "http://127.0.0.1:8765"
        eff_tok = token if token != "" else (settings.get("collector_token") or "")
        out_dir = get_export_dir() / "pearl_miner_client_kit"
        res = export_client_kit(out_dir, server_url=eff_url, token=eff_tok, worker_hint=worker_hint)
        self.open_folder_path(str(out_dir))
        return res

    # ------------------------------------------------------------------
    # Miner Kernel Updater, GitHub Release Checker & .bat Configurator
    # ------------------------------------------------------------------
    def browse_local_folder(self, directory: str = "") -> dict[str, Any]:
        if self._window is None:
            return {"cancelled": True}
        import webview

        dlg_type = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        kwargs: dict[str, Any] = {}
        clean_dir = (directory or "").strip()
        if clean_dir and Path(clean_dir).is_dir():
            kwargs["directory"] = clean_dir
        selected = self._window.create_file_dialog(dlg_type, **kwargs)
        if selected:
            chosen = selected[0] if isinstance(selected, (list, tuple)) else selected
            return {"cancelled": False, "path": str(chosen)}
        return {"cancelled": True}

    def save_kernel_settings(self, payload: dict[str, Any]) -> dict[str, str]:
        updates: dict[str, str] = {}
        if "miner_kernel_dir" in payload:
            updates["miner_kernel_dir"] = str(payload.get("miner_kernel_dir") or "").strip()
        if "miner_kernel_bat_path" in payload:
            updates["miner_kernel_bat_path"] = str(payload.get("miner_kernel_bat_path") or "").strip()
        if "miner_github_repo" in payload:
            raw_repo = str(payload.get("miner_github_repo") or "").strip()
            updates["miner_github_repo"] = normalize_github_repo(raw_repo) if raw_repo else DEFAULT_GITHUB_REPO
        if "miner_github_proxy" in payload:
            updates["miner_github_proxy"] = str(payload.get("miner_github_proxy") or "").strip()
        return self.db.update_settings(updates)

    def inspect_kernel_dir(self, kernel_dir: str = "", save_setting: bool = False) -> dict[str, Any]:
        settings = self.db.get_settings()
        target_dir = (kernel_dir or settings.get("miner_kernel_dir") or "").strip()
        if not target_dir and settings.get("local_bat_path"):
            bat_p = Path(settings["local_bat_path"])
            if bat_p.parent.is_dir():
                target_dir = str(bat_p.parent)
        if save_setting and target_dir:
            settings = self.db.update_settings({"miner_kernel_dir": target_dir})

        linked_bat = settings.get("miner_kernel_bat_path") or settings.get("local_bat_path") or ""
        info = inspect_kernel_dir(target_dir, linked_bat_path=linked_bat)
        return {
            "info": info,
            "settings": settings,
            "task": self.kernel_manager.get_status(),
        }

    def check_kernel_updates(
        self,
        repo: str = "",
        github_proxy: str = "",
        kernel_dir: str = "",
    ) -> dict[str, Any]:
        settings = self.db.get_settings()
        eff_repo = normalize_github_repo(repo or settings.get("miner_github_repo") or DEFAULT_GITHUB_REPO)
        eff_proxy = (github_proxy if github_proxy != "" else settings.get("miner_github_proxy") or "").strip()
        eff_dir = (kernel_dir or settings.get("miner_kernel_dir") or "").strip()

        self.db.update_settings({"miner_github_repo": eff_repo, "miner_github_proxy": eff_proxy})
        rel_data = fetch_github_releases(repo=eff_repo, github_proxy=eff_proxy, per_page=10)
        linked_bat = settings.get("miner_kernel_bat_path") or settings.get("local_bat_path") or ""
        dir_info = inspect_kernel_dir(eff_dir, linked_bat_path=linked_bat) if eff_dir else None

        local_ver = dir_info["exe_version"] if dir_info else ""
        latest_ver = rel_data["latest"]["version"] if rel_data.get("latest") else ""
        if not dir_info or not dir_info.get("exe_exists"):
            update_status = "not_installed"
        elif not local_ver or not latest_ver:
            update_status = "unknown"
        else:
            cmp_val = compare_versions(local_ver, latest_ver)
            if cmp_val is None:
                update_status = "unknown"
            elif cmp_val < 0:
                update_status = "upgrade"
            elif cmp_val == 0:
                update_status = "latest"
            else:
                update_status = "ahead"

        return {
            **rel_data,
            "local_version": local_ver,
            "update_status": update_status,
            "kernel_info": dir_info,
        }

    def start_kernel_deploy(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.db.get_settings()
        eff_dir = str(payload.get("kernel_dir") or settings.get("miner_kernel_dir") or "").strip()
        eff_repo = normalize_github_repo(
            str(payload.get("repo") or settings.get("miner_github_repo") or DEFAULT_GITHUB_REPO)
        )
        eff_proxy = str(payload.get("github_proxy") if "github_proxy" in payload else (settings.get("miner_github_proxy") or "")).strip()

        self.db.update_settings(
            {
                "miner_kernel_dir": eff_dir,
                "miner_github_repo": eff_repo,
                "miner_github_proxy": eff_proxy,
            }
        )
        return self.kernel_manager.start_deploy(
            kernel_dir=eff_dir,
            download_url=str(payload.get("download_url") or ""),
            asset_name=str(payload.get("asset_name") or ""),
            tag_name=str(payload.get("tag_name") or ""),
            repo=eff_repo,
            mode=str(payload.get("mode") or "exe_only"),
            github_proxy=eff_proxy,
            expected_digest=str(payload.get("digest") or ""),
            stop_running=bool(payload.get("stop_running", True)),
            backup_exe=bool(payload.get("backup_exe", True)),
            miner_binder=self.miner_binder,
        )

    def get_kernel_task_status(self) -> dict[str, Any]:
        return self.kernel_manager.get_status()

    def load_kernel_bat(self, bat_path: str) -> dict[str, Any]:
        cfg = extract_bat_config(bat_path)
        self.db.update_settings({"miner_kernel_bat_path": cfg["bat_path"]})
        return cfg

    def preview_kernel_bat(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"content": render_bat_content(cfg or {})}

    def save_kernel_bat(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.db.get_settings()
        raw_target = str(payload.get("target_path") or "").strip()
        kernel_dir = str(payload.get("kernel_dir") or settings.get("miner_kernel_dir") or "").strip()
        if raw_target and not Path(raw_target).is_absolute() and ("\\" not in raw_target and "/" not in raw_target):
            if not kernel_dir:
                raise ValueError("请先绑定内核目录，或填写完整的 .bat 文件路径")
            raw_target = str(Path(kernel_dir) / raw_target)
        elif not raw_target:
            if not kernel_dir:
                raise ValueError("请先绑定内核目录或填写 .bat 脚本路径")
            raw_target = str(Path(kernel_dir) / "start_prl.bat")

        use_raw = bool(payload.get("use_raw", False))
        raw_content = str(payload.get("raw_content") or "") if use_raw else None
        backup_existing = bool(payload.get("backup_existing", True))

        saved = save_bat_file(
            target_path=raw_target,
            cfg=payload.get("config") or {},
            raw_content=raw_content,
            backup_existing=backup_existing,
        )
        saved_path = saved["saved_path"]
        updates: dict[str, str] = {
            "miner_kernel_bat_path": saved_path,
            "miner_kernel_dir": str(Path(saved_path).parent),
        }
        if payload.get("bind_as_home_bat"):
            updates["local_bat_path"] = saved_path
        updated_settings = self.db.update_settings(updates)

        started_status = None
        if payload.get("start_now"):
            started_status = self.bind_bat_start(saved_path, auto_inject_log=True)

        return {
            **saved,
            "settings": updated_settings,
            "kernel_info": inspect_kernel_dir(updates["miner_kernel_dir"], linked_bat_path=saved_path),
            "miner_status": started_status,
        }

    # ------------------------------------------------------------------
    # Offline Alert Push & Remote Credentials & Synology Standby Failover
    # ------------------------------------------------------------------
    def save_alert_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.db.save_alert_config(payload or {})
        sync_res = self.poller.sync_standby_heartbeat(force=False)
        return {
            "alert_config": cfg,
            "standby_sync": sync_res,
            "state": self.get_state(None),
        }

    def update_worker_remote_config(
        self,
        wallet_id: int,
        worker_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        p = payload if isinstance(payload, dict) else {}
        updated = self.db.update_worker_remote_config(
            wallet_id=int(wallet_id),
            worker_name=str(worker_name or ""),
            remote_tool=str(p.get("remote_tool") or "ToDesk"),
            remote_code=str(p.get("remote_code") or ""),
            remote_password=str(p.get("remote_password") or ""),
            remote_note=str(p.get("remote_note") or ""),
            alert_enabled=bool(p.get("alert_enabled", True)),
        )
        self.poller.sync_standby_heartbeat(force=False)
        return {"updated": updated, "state": self.get_state(int(wallet_id))}

    def reset_worker_alert_state(self, wallet_id: int, worker_name: str) -> dict[str, Any]:
        self.db.reset_worker_alert_state(int(wallet_id), str(worker_name or ""))
        self.poller.sync_standby_heartbeat(force=False)
        return {"state": self.get_state(int(wallet_id))}

    def send_test_alert(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = normalize_alert_config(payload) if payload is not None else self.db.get_alert_config()
        title, md = format_test_alert_message(
            source_tag="主桌面端 PearlSplit",
            include_password=bool(cfg["include_password"]),
        )
        sender = getattr(self.poller, "_alert_sender", send_alert_message)
        res = sender(cfg, title, md)
        self.db.add_alert_log(
            wallet_id=None,
            wallet_alias="测试",
            event_type="test",
            worker_names="",
            title=title,
            summary="桌面端发送测试消息" if res.get("ok") else "桌面端测试消息发送失败",
            provider=str(res.get("provider") or cfg["provider"]),
            source="desktop",
            ok=bool(res.get("ok")),
            error=str(res.get("error") or ""),
        )
        return {"result": res, "state": self.get_state(None)}

    def sync_standby_now(self) -> dict[str, Any]:
        sync_res = self.poller.sync_standby_heartbeat(force=True)
        return {"sync": sync_res, "state": self.get_state(None)}

    def send_standby_test_alert(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = normalize_alert_config(payload) if payload is not None else self.db.get_alert_config()
        url = str(cfg.get("standby_server_url") or "").strip().rstrip("/")
        tok = str(cfg.get("standby_token") or "").strip()
        if not url:
            saved_cfg = self.db.get_alert_config()
            url = str(saved_cfg.get("effective_standby_url") or saved_cfg.get("standby_server_url") or "").strip().rstrip("/")
            if not tok:
                tok = str(saved_cfg.get("effective_standby_token") or saved_cfg.get("standby_token") or "").strip()
        if not url:
            raise ValueError("请先填写群晖 Docker 后端地址")

        self.poller.sync_standby_heartbeat(force=True)
        headers = {"X-Collector-Token": tok} if tok else {}
        resp = requests.post(
            f"{url}/api/v1/monitor/test",
            json={"alert_config": cfg},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 401:
            raise PermissionError("群晖后端鉴权失败 (Token 不匹配)")
        resp.raise_for_status()
        res = resp.json() if resp.content else {"ok": False, "error": "空响应"}
        self.poller.sync_standby_heartbeat(force=True)
        return {"result": res, "state": self.get_state(None)}

    def clear_alert_logs(self) -> dict[str, Any]:
        self.db.clear_alert_logs()
        return {"state": self.get_state(None)}
