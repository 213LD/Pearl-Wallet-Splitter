"""Zero-dependency WeChat alert engine (PushPlus / WxPusher) and stdlib ntminerpool parser.

Designed to run both inside the PearlSplit Windows desktop package and standalone
inside Synology NAS / Docker containers without third-party packages.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

POOL_BASE_URL = "https://www.ntminerpool.com"
_CN_TZ_ALIASES = {"Asia/Shanghai", "Asia/Chongqing", "Asia/Hong_Kong", "Asia/Taipei"}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_ALERT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider": "pushplus",
    "pushplus_token": "",
    "pushplus_topic": "",
    "pushplus_channel": "wechat",
    "wxpusher_app_token": "",
    "wxpusher_topic_ids": "",
    "wxpusher_uids": "",
    "offline_confirm_polls": 2,
    "notify_recovery": True,
    "include_password": True,
    "poll_fail_alert_minutes": 30,
    "standby_sync_enabled": False,
    "standby_server_url": "",
    "standby_token": "",
    "desktop_lease_timeout_sec": 180,
    "standby_poll_interval_sec": 60,
}


def now_str() -> str:
    """Return local datetime string respecting container TZ environment if set."""
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


def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def normalize_alert_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Sanitize and clamp alert configuration fields with safe defaults."""
    src = raw if isinstance(raw, dict) else {}
    provider = str(src.get("provider") or "pushplus").strip().lower()
    if provider not in ("pushplus", "wxpusher"):
        provider = "pushplus"

    channel = str(src.get("pushplus_channel") or "wechat").strip() or "wechat"
    standby_url = str(src.get("standby_server_url") or "").strip().rstrip("/")

    return {
        "enabled": _to_bool(src.get("enabled"), False),
        "provider": provider,
        "pushplus_token": str(src.get("pushplus_token") or "").strip(),
        "pushplus_topic": str(src.get("pushplus_topic") or "").strip(),
        "pushplus_channel": channel,
        "wxpusher_app_token": str(src.get("wxpusher_app_token") or "").strip(),
        "wxpusher_topic_ids": str(src.get("wxpusher_topic_ids") or "").strip(),
        "wxpusher_uids": str(src.get("wxpusher_uids") or "").strip(),
        "offline_confirm_polls": _clamp_int(src.get("offline_confirm_polls"), 1, 5, 2),
        "notify_recovery": _to_bool(src.get("notify_recovery"), True),
        "include_password": _to_bool(src.get("include_password"), True),
        "poll_fail_alert_minutes": _clamp_int(src.get("poll_fail_alert_minutes"), 5, 240, 30),
        "standby_sync_enabled": _to_bool(src.get("standby_sync_enabled"), False),
        "standby_server_url": standby_url,
        "standby_token": str(src.get("standby_token") or "").strip(),
        "desktop_lease_timeout_sec": _clamp_int(src.get("desktop_lease_timeout_sec"), 60, 1800, 180),
        "standby_poll_interval_sec": _clamp_int(src.get("standby_poll_interval_sec"), 30, 600, 60),
    }


def _parse_int_list(raw: str) -> list[int]:
    out: list[int] = []
    for tok in re.split(r"[,;\s]+", (raw or "").strip()):
        if not tok:
            continue
        try:
            v = int(tok)
            if v > 0 and v not in out:
                out.append(v)
        except ValueError:
            continue
    return out


def _parse_str_list(raw: str) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[,;\s]+", (raw or "").strip()):
        if tok and tok not in out:
            out.append(tok)
    return out


def _post_json(url: str, body_obj: dict[str, Any], timeout: int = 12) -> dict[str, Any]:
    data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def send_pushplus(
    token: str,
    title: str,
    content_md: str,
    topic: str = "",
    channel: str = "wechat",
    timeout: int = 12,
) -> dict[str, Any]:
    """Send a Markdown notification via PushPlus official endpoint."""
    clean_tok = (token or "").strip()
    if not clean_tok:
        return {"ok": False, "provider": "pushplus", "error": "PushPlus Token 不能为空", "code": None}

    payload: dict[str, Any] = {
        "token": clean_tok,
        "title": (title or "Pearl 矿机通知")[:120],
        "content": content_md or "",
        "template": "markdown",
        "channel": (channel or "wechat").strip() or "wechat",
    }
    clean_topic = (topic or "").strip()
    if clean_topic:
        payload["topic"] = clean_topic

    try:
        res = _post_json("https://www.pushplus.plus/send", payload, timeout=timeout)
        code = res.get("code")
        ok = code == 200
        err = None if ok else (str(res.get("msg") or f"PushPlus 返回错误 code={code}"))
        return {"ok": ok, "provider": "pushplus", "error": err, "code": code, "raw": res}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return {"ok": False, "provider": "pushplus", "error": f"HTTP {exc.code}: {body[:200]}", "code": exc.code}
    except Exception as exc:
        return {"ok": False, "provider": "pushplus", "error": f"PushPlus 请求失败: {exc}", "code": None}


def send_wxpusher(
    app_token: str,
    title: str,
    content_md: str,
    topic_ids: str = "",
    uids: str = "",
    timeout: int = 12,
) -> dict[str, Any]:
    """Send a Markdown notification via WxPusher official API."""
    clean_tok = (app_token or "").strip()
    if not clean_tok:
        return {"ok": False, "provider": "wxpusher", "error": "WxPusher AppToken 不能为空", "code": None}

    parsed_topics = _parse_int_list(topic_ids)
    parsed_uids = _parse_str_list(uids)
    if not parsed_topics and not parsed_uids:
        return {
            "ok": False,
            "provider": "wxpusher",
            "error": "请至少填写一个 WxPusher Topic ID 或 UID",
            "code": None,
        }

    payload: dict[str, Any] = {
        "appToken": clean_tok,
        "summary": (title or "Pearl 矿机通知")[:100],
        "content": content_md or "",
        "contentType": 3,
    }
    if parsed_topics:
        payload["topicIds"] = parsed_topics
    if parsed_uids:
        payload["uids"] = parsed_uids

    try:
        res = _post_json("https://wxpusher.zjiecode.com/api/send/message", payload, timeout=timeout)
        code = res.get("code")
        ok = code == 1000 or res.get("success") is True
        err = None if ok else (str(res.get("msg") or f"WxPusher 返回错误 code={code}"))
        return {"ok": ok, "provider": "wxpusher", "error": err, "code": code, "raw": res}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return {"ok": False, "provider": "wxpusher", "error": f"HTTP {exc.code}: {body[:200]}", "code": exc.code}
    except Exception as exc:
        return {"ok": False, "provider": "wxpusher", "error": f"WxPusher 请求失败: {exc}", "code": None}


def send_alert_message(
    config: dict[str, Any],
    title: str,
    content_md: str,
    timeout: int = 12,
) -> dict[str, Any]:
    """Dispatch notification cleanly using the configured push provider without raising."""
    cfg = normalize_alert_config(config)
    if cfg["provider"] == "wxpusher":
        return send_wxpusher(
            app_token=cfg["wxpusher_app_token"],
            title=title,
            content_md=content_md,
            topic_ids=cfg["wxpusher_topic_ids"],
            uids=cfg["wxpusher_uids"],
            timeout=timeout,
        )
    return send_pushplus(
        token=cfg["pushplus_token"],
        title=title,
        content_md=content_md,
        topic=cfg["pushplus_topic"],
        channel=cfg["pushplus_channel"],
        timeout=timeout,
    )


def format_offline_alert_message(
    wallet_alias: str,
    worker_items: list[dict[str, Any]],
    include_password: bool = True,
    source_tag: str = "主桌面端 PearlSplit",
    detected_at: str = "",
) -> tuple[str, str]:
    """Format an offline alert title and Markdown card including remote credentials."""
    ts = detected_at or now_str()
    if len(worker_items) == 1:
        w = worker_items[0]
        member = w.get("member_name") or "未分配"
        title = f"🔴 矿机掉线提醒：{w.get('worker_name', '')} ({member})"
    else:
        first = worker_items[0].get("worker_name", "")
        title = f"🔴 {len(worker_items)} 台矿机掉线提醒：{first} 等"

    lines = [
        f"### {title}",
        f"- **监控钱包**：{wallet_alias or '默认钱包'}",
        f"- **检测时间**：{ts}",
        f"- **触发节点**：{source_tag}",
        "",
        "---",
    ]
    for idx, w in enumerate(worker_items, start=1):
        wname = str(w.get("worker_name") or "")
        member = str(w.get("member_name") or "未分配")
        last_seen = str(w.get("last_seen_online_at") or "未知")
        shares = int(w.get("last_shares") or 0)
        tool = str(w.get("remote_tool") or "ToDesk").strip() or "ToDesk"
        code = str(w.get("remote_code") or "").strip()
        pwd = str(w.get("remote_password") or "").strip()
        note = str(w.get("remote_note") or "").strip()

        if pwd:
            pwd_text = f"`{pwd}`" if include_password else "已开启隐私隐藏（未随消息发送，请私聊机主）"
        else:
            pwd_text = "未设置固定密码（请前往软件补充）"

        lines.append(f"**{idx}. 矿机 `{wname}`（归属：{member}）**")
        lines.append(f"- 最后在线：{last_seen} ｜ 累计份额：{shares:,}")
        lines.append(f"- 远程工具：{tool}")
        lines.append(f"- 设备配对码：`{code}`" if code else "- 设备配对码：未填写（请前往「掉线告警 & 远控」补充）")
        lines.append(f"- 连接密码：{pwd_text}")
        if note:
            lines.append(f"- 备注说明：{note}")
        lines.append("")

    lines.append("---")
    lines.append("> 已锁定本台矿机离线告警，直到重新上线才会重置，不会重复刷屏。")
    return title, "\n".join(lines)


def format_recovery_alert_message(
    wallet_alias: str,
    worker_items: list[dict[str, Any]],
    source_tag: str = "主桌面端 PearlSplit",
    detected_at: str = "",
) -> tuple[str, str]:
    """Format a worker online-recovery notification."""
    ts = detected_at or now_str()
    if len(worker_items) == 1:
        w = worker_items[0]
        member = w.get("member_name") or "未分配"
        title = f"🟢 矿机恢复上线：{w.get('worker_name', '')} ({member})"
    else:
        first = worker_items[0].get("worker_name", "")
        title = f"🟢 {len(worker_items)} 台矿机已恢复上线：{first} 等"

    lines = [
        f"### {title}",
        f"- **监控钱包**：{wallet_alias or '默认钱包'}",
        f"- **恢复时间**：{ts}",
        f"- **触发节点**：{source_tag}",
        "",
    ]
    for w in worker_items:
        wname = str(w.get("worker_name") or "")
        member = str(w.get("member_name") or "未分配")
        hr = str(w.get("current_hashrate") or "--")
        shares = int(w.get("shares") or w.get("last_shares") or 0)
        lines.append(f"- **`{wname}`**（{member}）：当前算力 `{hr}` ｜ 累计份额 `{shares:,}`")
    lines.append("")
    lines.append("> 掉线告警状态已自动重置，若后续再次掉线将重新通知。")
    return title, "\n".join(lines)


def format_poll_failure_alert_message(
    minutes_failed: int,
    consecutive_failures: int,
    last_error: str,
    wallet_aliases: list[str] | None = None,
    source_tag: str = "主桌面端 PearlSplit",
    detected_at: str = "",
) -> tuple[str, str]:
    """Format the 30-minute continuous pool-polling failure alert."""
    ts = detected_at or now_str()
    mins = max(1, int(minutes_failed))
    title = f"⚠️ 矿池轮询连续失败超 {mins} 分钟，请自行关注矿机状态"
    w_str = "、".join(wallet_aliases or []) or "当前监控钱包"
    lines = [
        f"### {title}",
        f"- **连续失败时长**：约 {mins} 分钟（累计失败 {int(consecutive_failures)} 次）",
        f"- **触发节点**：{source_tag}",
        f"- **检测时间**：{ts}",
        f"- **影响钱包**：{w_str}",
        f"- **最近报错**：`{last_error or '连接矿池失败'}`",
        "",
        "> 因连续无法获取 ntminerpool 矿池状态，软件无法代为判断矿机是否掉线，请大家自行通过远程桌面或矿池网页确认机器运行状态！恢复拉取后将自动清除告警状态。",
    ]
    return title, "\n".join(lines)


def format_test_alert_message(
    source_tag: str = "主桌面端 PearlSplit",
    include_password: bool = True,
) -> tuple[str, str]:
    """Build a friendly verification message for PushPlus / WxPusher connectivity testing."""
    ts = now_str()
    title = f"✅ Pearl 矿机告警推送测试成功 ({source_tag})"
    pwd_hint = "开启明文发送固定密码" if include_password else "已隐藏固定密码（仅发配对码）"
    lines = [
        f"### {title}",
        f"- **发送来源**：{source_tag}",
        f"- **测试时间**：{ts}",
        f"- **密码发送策略**：{pwd_hint}",
        "",
        "后续当矿机掉线（或矿池连续超 30 分钟无法拉取）时，通知将实时推送到这里。",
    ]
    return title, "\n".join(lines)


class _PoolOnlineHTMLParser(HTMLParser):
    """Stdlib HTML parser matching ntminerpool online worker rows & pagination."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.saw_pool_structure = False
        self.saw_wk_tabs = False
        self.max_page = 1
        self.workers: list[dict[str, Any]] = []

        self._table_depth = 0
        self._in_target_table = False
        self._target_table_done = False
        self._in_tbody = False
        self._in_tr = False
        self._in_td = False
        self._td_has_colspan = False
        self._current_td_parts: list[str] = []
        self._current_row_cells: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(attr_map.get("class", "").split())

        if tag == "div":
            if "wk_tabs" in cls_tokens or "gHXKsE" in cls_tokens:
                self.saw_pool_structure = True
            if "wk_tabs" in cls_tokens:
                self.saw_wk_tabs = True

        elif tag == "a":
            href = attr_map.get("href", "")
            if "wk_tab" not in cls_tokens and "page=" in href:
                m = re.search(r"[?&]page=(\d+)", href)
                if m:
                    self.max_page = max(self.max_page, int(m.group(1)))

        elif tag == "table":
            if "hTNNTI" in cls_tokens:
                self.saw_pool_structure = True
            if self._table_depth == 0 and "hTNNTI" in cls_tokens and not self._target_table_done:
                self._in_target_table = True
            self._table_depth += 1

        elif self._in_target_table:
            if tag == "tbody":
                self._in_tbody = True
            elif tag == "tr" and self._in_tbody:
                self._in_tr = True
                self._td_has_colspan = False
                self._current_row_cells = []
            elif tag == "td" and self._in_tr:
                self._in_td = True
                if "colspan" in attr_map:
                    self._td_has_colspan = True
                self._current_td_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth > 0:
                self._table_depth -= 1
            if self._table_depth == 0 and self._in_target_table:
                self._in_target_table = False
                self._target_table_done = True
                self._in_tbody = False
        elif self._in_target_table:
            if tag == "tbody":
                self._in_tbody = False
            elif tag == "td" and self._in_td:
                self._in_td = False
                cell_text = " ".join("".join(self._current_td_parts).split()).strip()
                self._current_row_cells.append(cell_text)
            elif tag == "tr" and self._in_tr:
                self._in_tr = False
                cells = self._current_row_cells
                if not self._td_has_colspan and len(cells) >= 7:
                    name = cells[0].strip()
                    if name:
                        shares_raw = cells[5].replace(",", "").strip()
                        try:
                            shares = int(float(shares_raw))
                        except ValueError:
                            shares = 0
                        self.workers.append(
                            {
                                "name": name,
                                "current_hashrate": cells[1],
                                "avg_1h_hashrate": cells[2],
                                "avg_6h_hashrate": cells[3],
                                "avg_24h_hashrate": cells[4],
                                "shares": shares,
                                "last_seen_text": cells[6],
                            }
                        )

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_td_parts.append(data)


def parse_online_workers_stdlib(html: str) -> tuple[list[dict[str, Any]], int]:
    """Parse ntminerpool online worker rows using Python stdlib only."""
    parser = _PoolOnlineHTMLParser()
    parser.feed(html or "")
    parser.close()
    if not parser.saw_pool_structure:
        raise RuntimeError("矿池页面结构异常或正在维护，已跳过本轮掉线判定")
    return parser.workers, parser.max_page


def _http_get_text(url: str, timeout: int = 15) -> str:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    return raw.decode(charset, errors="replace")
                except LookupError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.4)
    raise last_exc if last_exc is not None else RuntimeError("网络请求失败")


def fetch_online_workers_stdlib(wallet_address: str, timeout: int = 15) -> dict[str, Any]:
    """Fetch all online workers for a PRL wallet address from ntminerpool via stdlib."""
    wallet = (wallet_address or "").strip()
    if not wallet:
        raise ValueError("钱包地址不能为空")

    first_url = f"{POOL_BASE_URL}/prl?back={wallet}&status=online&page=1&keyword="
    first_html = _http_get_text(first_url, timeout=timeout)
    workers, max_page = parse_online_workers_stdlib(first_html)
    seen = {w["name"] for w in workers}

    for page in range(2, min(max_page + 1, 25)):
        page_url = f"{POOL_BASE_URL}/prl?back={wallet}&status=online&page={page}&keyword="
        page_html = _http_get_text(page_url, timeout=timeout)
        page_workers, _ = parse_online_workers_stdlib(page_html)
        added = 0
        for w in page_workers:
            if w["name"] not in seen:
                seen.add(w["name"])
                workers.append(w)
                added += 1
        if added == 0:
            break

    return {"fetched_at": now_str(), "workers": workers}
