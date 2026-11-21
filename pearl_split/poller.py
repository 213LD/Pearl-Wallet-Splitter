"""Background auto-polling & wake-up watch monitor for offline miners."""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from typing import Any, Callable

import requests

from .alert_engine import (
    format_offline_alert_message,
    format_poll_failure_alert_message,
    format_recovery_alert_message,
    send_alert_message,
)
from .db import Database
from .scraper import PoolScraper


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_pool_error(exc: Exception) -> str:
    """Convert raw requests/network exceptions into clear Chinese diagnostics."""
    if isinstance(exc, requests.exceptions.Timeout):
        return "连接矿池超时 (ntminerpool.com 响应超时)"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code if exc.response is not None else "未知"
        return f"矿池网页返回 HTTP {code} 错误"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "无法连接到矿池 Dashboard (网络断开或域名解析失败)"
    msg = str(exc).strip()
    return msg or exc.__class__.__name__


class BackgroundPoller:
    """Polls active wallets periodically to preserve offline miner share counts."""

    def __init__(
        self,
        db: Database,
        scraper: PoolScraper,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        alert_sender: Callable[[dict[str, Any], str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self.scraper = scraper
        self.on_update = on_update
        self._alert_sender = alert_sender or send_alert_message
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.watch_mode = False
        self.watch_wallet_id: int | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.last_success_at: str | None = None
        self.consecutive_failures: int = 0
        self.first_failure_ts: float | None = None
        self.poll_fail_alert_sent: bool = False
        self._last_alert_retry_ts: float = 0.0
        self.standby_last_sync_at: str | None = None
        self.standby_last_error: str | None = None
        self.standby_remote_status: dict[str, Any] | None = None
        self.wallet_status: dict[int, dict[str, Any]] = {}
        self.is_fetching = False
        self.next_poll_ts: float | None = None
        self.current_interval_sec: int = 60
        self.recent_events: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="PoolPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.release_standby_lease()
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    @staticmethod
    def _resolve_standby_target(alert_cfg: dict[str, Any]) -> tuple[str, str]:
        url = str(alert_cfg.get("effective_standby_url") or alert_cfg.get("standby_server_url") or "").strip().rstrip("/")
        tok = str(alert_cfg.get("effective_standby_token") or alert_cfg.get("standby_token") or "").strip()
        return url, tok

    def sync_standby_heartbeat(self, force: bool = False) -> dict[str, Any]:
        """Push Desktop lease + worker alert state to Synology Docker and pull handback states."""
        cfg = self.db.get_alert_config()
        if not cfg["standby_sync_enabled"] and not force:
            return {"synced": False, "skipped": True}
        url, tok = self._resolve_standby_target(cfg)
        if not url:
            if force:
                with self._lock:
                    self.standby_last_error = "未填写群晖 Docker 后端地址"
                return {"synced": False, "error": self.standby_last_error}
            return {"synced": False, "skipped": True}

        sync_data = self.db.get_standby_sync_payload()
        with self._lock:
            poll_ok = self.last_error is None
            first_fail = self.first_failure_ts
            fail_sent = self.poll_fail_alert_sent

        body = {
            "desktop_host": socket.gethostname(),
            "alert_config": sync_data["alert_config"],
            "wallets": sync_data["wallets"],
            "worker_states": sync_data["worker_states"],
            "desktop_poll_ok": poll_ok,
            "desktop_first_fail_ts": first_fail,
            "desktop_poll_fail_alert_sent": fail_sent,
        }
        headers = {"X-Collector-Token": tok} if tok else {}
        try:
            resp = requests.post(
                f"{url}/api/v1/monitor/heartbeat",
                json=body,
                headers=headers,
                timeout=4,
            )
            if resp.status_code == 401:
                raise PermissionError("群晖后端鉴权失败 (Token 不匹配)")
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            handbacks = data.get("handback_worker_states") or []
            if handbacks:
                self.db.merge_standby_handback_states(handbacks)
            for slog in data.get("new_standby_logs") or []:
                if isinstance(slog, dict):
                    self.db.add_alert_log(
                        wallet_id=None,
                        wallet_alias=str(slog.get("wallet_alias") or ""),
                        event_type=str(slog.get("event_type") or "offline"),
                        worker_names=str(slog.get("worker_names") or ""),
                        title=str(slog.get("title") or ""),
                        summary=str(slog.get("summary") or ""),
                        provider=str(slog.get("provider") or "pushplus"),
                        source="docker",
                        ok=bool(slog.get("ok", 1)),
                        error=str(slog.get("error") or ""),
                        created_at=str(slog.get("created_at") or ""),
                        dedupe=True,
                    )
            now_s = _now_str()
            with self._lock:
                self.standby_last_sync_at = now_s
                self.standby_last_error = None
                self.standby_remote_status = data.get("status")
            return {"synced": True, "at": now_s, "status": data.get("status")}
        except Exception as exc:
            err_msg = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                self.standby_last_error = err_msg
            return {"synced": False, "error": err_msg}

    def release_standby_lease(self) -> None:
        """Notify Synology Docker immediately on Desktop shutdown so it takes over without delay."""
        try:
            cfg = self.db.get_alert_config()
            if not cfg["standby_sync_enabled"]:
                return
            url, tok = self._resolve_standby_target(cfg)
            if not url:
                return
            headers = {"X-Collector-Token": tok} if tok else {}
            requests.post(f"{url}/api/v1/monitor/release", json={}, headers=headers, timeout=1.5)
        except Exception:
            pass

    def trigger_now(self) -> None:
        self._wake_event.set()

    def _configured_interval(self) -> tuple[bool, int]:
        settings = self.db.get_settings()
        auto_enabled = settings.get("auto_poll_enabled", "1") == "1"
        try:
            interval = max(15, int(settings.get("auto_poll_interval_sec", "60")))
        except ValueError:
            interval = 60
        return auto_enabled, interval

    def set_watch_mode(self, enabled: bool, wallet_id: int | None = None) -> dict[str, Any]:
        auto_enabled, interval = self._configured_interval()
        with self._lock:
            self.watch_mode = bool(enabled)
            self.watch_wallet_id = wallet_id if enabled else None
            if self.watch_mode and self.watch_wallet_id is not None:
                self.current_interval_sec = 8
                self.next_poll_ts = None
                self._wake_event.set()
            else:
                self.current_interval_sec = interval if auto_enabled else 0
                self.next_poll_ts = (time.time() + interval) if auto_enabled else None
        return {"watch_mode": self.watch_mode, "watch_wallet_id": self.watch_wallet_id}

    def fetch_wallet_once(self, wallet_id: int, include_payments: bool = True) -> dict[str, Any]:
        wallets = self.db.list_wallets()
        target = next((w for w in wallets if w["id"] == wallet_id), None)
        if not target:
            raise ValueError("钱包不存在")

        auto_enabled, interval = self._configured_interval()
        with self._lock:
            self.is_fetching = True
            self.next_poll_ts = None
        if self.on_update:
            self.on_update({"type": "fetch_started", "wallet_id": wallet_id})
        try:
            data = self.scraper.fetch_dashboard(target["address"], include_payments=include_payments)
            meta = self.db.save_pool_fetch(wallet_id, data)
            with self._lock:
                self.last_error = None
                self.last_error_at = None
                self.last_success_at = data.fetched_at
                self.consecutive_failures = 0
                self.first_failure_ts = None
                self.poll_fail_alert_sent = False
                self.wallet_status[wallet_id] = {
                    "state": "ok",
                    "error": None,
                    "updated_at": data.fetched_at,
                }
            woken = meta.get("newly_woken") or []
            if woken:
                self._push_event(
                    {
                        "type": "worker_woken",
                        "wallet_id": wallet_id,
                        "wallet_alias": target["alias"],
                        "workers": woken,
                        "time": data.fetched_at,
                    }
                )
            self._process_alert_transitions(wallet_id, target["alias"], meta, data.fetched_at)
            return {"ok": True, "fetched_at": data.fetched_at, "woken": woken}
        except Exception as exc:
            friendly_err = _format_pool_error(exc)
            err_time = _now_str()
            with self._lock:
                self.last_error = friendly_err
                self.last_error_at = err_time
                self.consecutive_failures += 1
                if self.first_failure_ts is None:
                    self.first_failure_ts = time.time()
                fail_count = self.consecutive_failures
                fail_elapsed_min = (time.time() - self.first_failure_ts) / 60.0
                already_sent = self.poll_fail_alert_sent
                self.wallet_status[wallet_id] = {
                    "state": "error",
                    "error": friendly_err,
                    "updated_at": err_time,
                }
            self._maybe_send_poll_failure_alert(
                wallet_id=wallet_id,
                wallet_alias=target["alias"],
                fail_elapsed_min=fail_elapsed_min,
                fail_count=fail_count,
                already_sent=already_sent,
                friendly_err=friendly_err,
                err_time=err_time,
            )
            raise RuntimeError(friendly_err) from exc
        finally:
            with self._lock:
                self.is_fetching = False
                if self.watch_mode and self.watch_wallet_id is not None:
                    self.current_interval_sec = 8
                    self.next_poll_ts = time.time() + 8
                elif auto_enabled:
                    self.current_interval_sec = interval
                    self.next_poll_ts = time.time() + interval
                else:
                    self.current_interval_sec = 0
                    self.next_poll_ts = None

    def _process_alert_transitions(
        self,
        wallet_id: int,
        wallet_alias: str,
        meta: dict[str, Any],
        fetched_at: str,
    ) -> None:
        alert_cfg = self.db.get_alert_config()
        if not alert_cfg["enabled"]:
            return

        now_ts = time.time()
        confirm_polls = int(alert_cfg["offline_confirm_polls"])
        candidates = meta.get("offline_candidates") or []
        eligible = [
            c
            for c in candidates
            if int(c.get("alert_enabled", 1)) == 1
            and int(c.get("offline_alert_sent", 0)) == 0
            and int(c.get("consecutive_offline_polls", 0)) >= confirm_polls
        ]

        if eligible and (now_ts - self._last_alert_retry_ts) >= 120:
            title, md = format_offline_alert_message(
                wallet_alias=wallet_alias,
                worker_items=eligible,
                include_password=bool(alert_cfg["include_password"]),
                source_tag="主桌面端 PearlSplit",
                detected_at=fetched_at,
            )
            res = self._alert_sender(alert_cfg, title, md)
            w_names_str = ", ".join(c["worker_name"] for c in eligible)
            if res.get("ok"):
                self._last_alert_retry_ts = 0.0
                self.db.mark_workers_offline_alert_sent(
                    wallet_id,
                    [c["worker_name"] for c in eligible],
                    fetched_at,
                )
                self.db.add_alert_log(
                    wallet_id=wallet_id,
                    wallet_alias=wallet_alias,
                    event_type="offline",
                    worker_names=w_names_str,
                    title=title,
                    summary=f"已推送 {len(eligible)} 台矿机掉线通知",
                    provider=str(res.get("provider") or alert_cfg["provider"]),
                    source="desktop",
                    ok=True,
                    created_at=fetched_at,
                )
                self._push_event(
                    {
                        "type": "worker_offline_alert",
                        "wallet_id": wallet_id,
                        "wallet_alias": wallet_alias,
                        "workers": eligible,
                        "title": title,
                        "time": fetched_at,
                    }
                )
            else:
                self._last_alert_retry_ts = now_ts
                err_txt = str(res.get("error") or "推送失败")
                self.db.add_alert_log(
                    wallet_id=wallet_id,
                    wallet_alias=wallet_alias,
                    event_type="offline",
                    worker_names=w_names_str,
                    title=title,
                    summary="掉线通知推送失败（将在恢复前每2分钟重试）",
                    provider=str(res.get("provider") or alert_cfg["provider"]),
                    source="desktop",
                    ok=False,
                    error=err_txt,
                    created_at=fetched_at,
                )
                self._push_event(
                    {
                        "type": "alert_push_error",
                        "title": title,
                        "error": err_txt,
                        "time": fetched_at,
                    }
                )

        recovered = meta.get("recovered_alert_workers") or []
        if alert_cfg["notify_recovery"] and recovered:
            r_title, r_md = format_recovery_alert_message(
                wallet_alias=wallet_alias,
                worker_items=recovered,
                source_tag="主桌面端 PearlSplit",
                detected_at=fetched_at,
            )
            r_res = self._alert_sender(alert_cfg, r_title, r_md)
            r_names = ", ".join(w["worker_name"] for w in recovered)
            ok = bool(r_res.get("ok"))
            r_err = str(r_res.get("error") or "")
            self.db.add_alert_log(
                wallet_id=wallet_id,
                wallet_alias=wallet_alias,
                event_type="recovery",
                worker_names=r_names,
                title=r_title,
                summary=f"已通知 {len(recovered)} 台矿机恢复上线" if ok else "恢复上线通知推送失败",
                provider=str(r_res.get("provider") or alert_cfg["provider"]),
                source="desktop",
                ok=ok,
                error=r_err,
                created_at=fetched_at,
            )
            if ok:
                self._push_event(
                    {
                        "type": "worker_recovery_alert",
                        "wallet_id": wallet_id,
                        "wallet_alias": wallet_alias,
                        "workers": recovered,
                        "title": r_title,
                        "time": fetched_at,
                    }
                )
            else:
                self._push_event(
                    {
                        "type": "alert_push_error",
                        "title": r_title,
                        "error": r_err or "推送失败",
                        "time": fetched_at,
                    }
                )

    def _maybe_send_poll_failure_alert(
        self,
        wallet_id: int,
        wallet_alias: str,
        fail_elapsed_min: float,
        fail_count: int,
        already_sent: bool,
        friendly_err: str,
        err_time: str,
    ) -> None:
        alert_cfg = self.db.get_alert_config()
        if not alert_cfg["enabled"] or already_sent:
            return
        if fail_elapsed_min < float(alert_cfg["poll_fail_alert_minutes"]):
            return
        now_ts = time.time()
        if (now_ts - self._last_alert_retry_ts) < 120:
            return

        aliases = [w["alias"] for w in self.db.list_wallets() if w.get("is_active", 1)]
        title, md = format_poll_failure_alert_message(
            minutes_failed=int(round(fail_elapsed_min)),
            consecutive_failures=fail_count,
            last_error=friendly_err,
            wallet_aliases=aliases,
            source_tag="主桌面端 PearlSplit",
            detected_at=err_time,
        )
        res = self._alert_sender(alert_cfg, title, md)
        if res.get("ok"):
            with self._lock:
                self.poll_fail_alert_sent = True
            self._last_alert_retry_ts = 0.0
            self.db.add_alert_log(
                wallet_id=wallet_id,
                wallet_alias=wallet_alias,
                event_type="poll_fail",
                worker_names="",
                title=title,
                summary=f"连续失败 {int(round(fail_elapsed_min))} 分钟已群发提醒",
                provider=str(res.get("provider") or alert_cfg["provider"]),
                source="desktop",
                ok=True,
                created_at=err_time,
            )
            self._push_event(
                {
                    "type": "poll_fail_alert",
                    "title": title,
                    "minutes": int(round(fail_elapsed_min)),
                    "time": err_time,
                }
            )
        else:
            self._last_alert_retry_ts = now_ts
            err_txt = str(res.get("error") or "推送失败")
            self.db.add_alert_log(
                wallet_id=wallet_id,
                wallet_alias=wallet_alias,
                event_type="poll_fail",
                worker_names="",
                title=title,
                summary="轮询失败超时提醒推送失败",
                provider=str(res.get("provider") or alert_cfg["provider"]),
                source="desktop",
                ok=False,
                error=err_txt,
                created_at=err_time,
            )
            self._push_event(
                {
                    "type": "alert_push_error",
                    "title": title,
                    "error": err_txt,
                    "time": err_time,
                }
            )

    def _push_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.recent_events.append(event)
            if len(self.recent_events) > 50:
                self.recent_events = self.recent_events[-50:]

    def pop_events(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self.recent_events)
            self.recent_events.clear()
            return events

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            remaining_ms = (
                max(0, int((self.next_poll_ts - time.time()) * 1000))
                if self.next_poll_ts is not None
                else 0
            )
            if self.is_fetching:
                conn_state = "fetching"
            elif self.last_error:
                conn_state = "error"
            elif self.last_success_at:
                conn_state = "ok"
            else:
                conn_state = "idle"

            fail_dur = (
                max(0, int(time.time() - self.first_failure_ts))
                if self.first_failure_ts is not None
                else 0
            )
            return {
                "watch_mode": self.watch_mode,
                "watch_wallet_id": self.watch_wallet_id,
                "is_fetching": self.is_fetching,
                "conn_state": conn_state,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_success_at": self.last_success_at,
                "consecutive_failures": self.consecutive_failures,
                "first_failure_ts": self.first_failure_ts,
                "failure_duration_sec": fail_dur,
                "poll_fail_alert_sent": self.poll_fail_alert_sent,
                "standby_last_sync_at": self.standby_last_sync_at,
                "standby_last_error": self.standby_last_error,
                "standby_remote_status": self.standby_remote_status,
                "wallet_status": {str(k): dict(v) for k, v in self.wallet_status.items()},
                "remaining_ms": remaining_ms,
                "remaining_sec": (remaining_ms + 999) // 1000 if remaining_ms > 0 else 0,
                "interval_sec": self.current_interval_sec,
            }

    def _run_loop(self) -> None:
        # Initial quick fetch shortly after startup
        time.sleep(0.8)
        if not self._stop_event.is_set():
            try:
                # Merge any standby handback alert flags BEFORE first desktop poll
                self.sync_standby_heartbeat()
            except Exception:
                pass
        while not self._stop_event.is_set():
            self._wake_event.clear()
            auto_enabled, interval = self._configured_interval()

            with self._lock:
                watch = self.watch_mode
                watch_wid = self.watch_wallet_id
                self.next_poll_ts = None

            woken_all: list[dict[str, Any]] = []
            if watch and watch_wid is not None:
                with self._lock:
                    self.current_interval_sec = 8
                try:
                    res = self.fetch_wallet_once(watch_wid, include_payments=False)
                    woken_all.extend(res.get("woken") or [])
                except Exception:
                    pass
                wait_sec = 8
            elif auto_enabled:
                with self._lock:
                    self.current_interval_sec = interval
                wallets = self.db.list_wallets()
                active_wallets = [w for w in wallets if w.get("is_active", 1)]
                for w in active_wallets:
                    if self._stop_event.is_set() or self._wake_event.is_set():
                        break
                    try:
                        res = self.fetch_wallet_once(w["id"], include_payments=True)
                        woken_all.extend(res.get("woken") or [])
                    except Exception:
                        pass
                wait_sec = interval
            else:
                with self._lock:
                    self.current_interval_sec = 0
                wait_sec = 10

            if self._stop_event.is_set():
                break

            try:
                self.sync_standby_heartbeat()
            except Exception:
                pass

            with self._lock:
                if auto_enabled or (self.watch_mode and self.watch_wallet_id is not None):
                    self.next_poll_ts = time.time() + wait_sec
                else:
                    self.next_poll_ts = None

            if self.on_update:
                self.on_update({"type": "poll_scheduled", "wait_sec": wait_sec, "woken": woken_all})

            # Sleep until next_poll_ts (allows manual refresh or watch toggle to extend/adjust deadline dynamically)
            deadline_fallback = time.time() + wait_sec
            while not self._stop_event.is_set() and not self._wake_event.is_set():
                with self._lock:
                    target_ts = self.next_poll_ts
                effective_target = target_ts if target_ts is not None else deadline_fallback
                rem = effective_target - time.time()
                if rem <= 0:
                    break
                self._wake_event.wait(timeout=min(rem, 1.0))
