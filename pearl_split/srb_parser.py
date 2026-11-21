"""Zero-dependency SRBMiner console/log parser and .bat configuration analyzer.

Designed with Python stdlib only so it can be shared identically by the desktop app,
standalone collector backend server, and lightweight client reporter scripts.
"""

from __future__ import annotations

import math
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TIMESTAMP_PREFIX_RE = re.compile(
    r"^\s*\[?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2})\]?\s*(.*)$"
)
HASHRATE_UNIT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)", re.IGNORECASE)
UNIT_TO_THS = {
    "H/S": 1e-12,
    "KH/S": 1e-9,
    "MH/S": 1e-6,
    "GH/S": 1e-3,
    "TH/S": 1.0,
    "PH/S": 1e3,
}

TOTAL_HASH_PATTERNS = [
    re.compile(r"Total\s*(?:\[[^\]]+\]\s*)?(?:hash\s*rate|hashrate|speed)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)", re.IGNORECASE),
    re.compile(r"\[hashrate\].*?Total\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)", re.IGNORECASE),
]
DEVICE_TAG_RE = re.compile(r"\b(GPU\s*\d+|CPU(?:\s*Thread\s*\d+)?)\b", re.IGNORECASE)
DEVICE_NAME_RE = re.compile(r"(?:GPU\s*\d+|Device\s*\d+)\s*\[([^\]]+)\]", re.IGNORECASE)
TEMP_RE = re.compile(r"(?:Temp(?:erature)?\s*[:=]|\bt\s*[:=])\s*(\d+)\s*°?C?", re.IGNORECASE)
TEMP_INLINE_RE = re.compile(r"\b(\d{2,3})\s*°?C\b")
FAN_RE = re.compile(r"(?:Fan\s*[:=]|\bf\s*[:=])\s*(\d+)\s*%?", re.IGNORECASE)
FAN_INLINE_RE = re.compile(r"\b(\d{1,3})\s*%")
POWER_RE = re.compile(r"(?:Power\s*[:=]|\bp\s*[:=])?\s*([0-9]+(?:\.[0-9]+)?)\s*W\b", re.IGNORECASE)
TOTAL_POWER_RE = re.compile(r"Total\s*power\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*W", re.IGNORECASE)
LATENCY_RE = re.compile(r"[\(\[]\s*([0-9]+(?:\.[0-9]+)?)\s*ms\s*[\)\]]", re.IGNORECASE)
TOTAL_SHARES_SUMMARY_RE = re.compile(
    r"(?:Total\s*shares\s*[:=]\s*(\d+).*?Rejected\s*[:=]\s*(\d+))|(?:\[(\d+)\s*accepted\s*,\s*(\d+)\s*rejected\])",
    re.IGNORECASE,
)
SHARE_STATS_RE = re.compile(
    r"Share\s*stats\s*\[\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*%?\s*\]",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"(SRBMiner[-\w]*\s+v?[0-9]+(?:\.[0-9]+)+)", re.IGNORECASE)
ALG_RE = re.compile(
    r"(?:Algorithm|Alg)\s*[:=]\s*([A-Za-z0-9_\-]+)|\[\s*A\d+\s*-\s*([A-Za-z0-9_\-]+)\s*\]",
    re.IGNORECASE,
)
ALG_SHARE_RE = re.compile(r"ms\]\s*\[([A-Za-z0-9_\-]+)\]\[\d+\]", re.IGNORECASE)
UPTIME_RE = re.compile(r"Uptime:\s*(?:(\d+)\s*d\s+)?(\d{1,2}):(\d{2}):(\d{2})", re.IGNORECASE)
POOL_CONNECT_RE = re.compile(r"(?:Connected\s+to\s+pool|Using\s+pool|Pool)\s*[:=]\s*([^\s,\]]+)", re.IGNORECASE)
WORKER_LINE_RE = re.compile(r"\bWorker\s*[:=]\s*([A-Za-z0-9_.\-]+)", re.IGNORECASE)

# SRBMiner v3.x tabular Statistics formats:
#   #1  RTX 5080                 215.49 TH/s   225.5W   0.956     31%   52C        2760  14499       3     0     0
#       Hashrate                 215.49 TH/s   225.5W   0.956                                        3     0     0
#       1 min                    209.12 TH/s
SRB_V3_DEVICE_ROW_RE = re.compile(
    r"^\s*#(\d+)\s+(.+?)\s+([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)(.*)$",
    re.IGNORECASE,
)
SRB_V3_TOTAL_ROW_RE = re.compile(
    r"^\s*(?:Total\s+)?Hashrate\s+([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)(.*)$",
    re.IGNORECASE,
)
SRB_V3_AVG_ROW_RE = re.compile(
    r"^\s*(1\s*min|5\s*min|15\s*min|1\s*h(?:r|our)?|6\s*h(?:r|our)?)\s+([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)\b",
    re.IGNORECASE,
)


def clean_console_line(raw_line: str) -> str:
    """Remove ANSI color sequences and normalize line endings."""
    text = ANSI_ESCAPE_RE.sub("", raw_line or "")
    text = text.replace("\r", "").rstrip("\n")
    return text


def to_ths(value: float, unit: str) -> float:
    return float(value) * UNIT_TO_THS.get(unit.upper(), 1.0)


def format_ths(ths: float) -> str:
    if ths <= 0:
        return "0.00 TH/s"
    if ths >= 1000.0:
        return f"{ths / 1000.0:.2f} PH/s"
    if ths < 1e-6:
        return f"{ths * 1e9:.2f} KH/s"
    if ths < 1e-3:
        return f"{ths * 1e6:.2f} MH/s"
    if ths < 1.0:
        return f"{ths * 1000.0:.2f} GH/s"
    return f"{ths:.2f} TH/s"


def _normalize_device_id(raw_id: str) -> str:
    compact = re.sub(r"\s+", "", raw_id or "").upper()
    if compact.startswith("#") and compact[1:].isdigit():
        return f"GPU{compact[1:]}"
    if compact.startswith("CPUTHREAD"):
        return "CPU"
    return compact


@dataclass
class SrbLogLine:
    seq: int
    ts: str
    level: str
    line: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "line": self.line,
        }


class SrbTelemetryParser:
    """Stateful streaming parser that extracts real stats from SRBMiner output lines."""

    def __init__(self, max_lines: int = 400, max_samples: int = 360) -> None:
        self.max_lines = max_lines
        self.max_samples = max_samples
        self.lines: deque[SrbLogLine] = deque(maxlen=max_lines)
        self.samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self.latencies_ms: deque[float] = deque(maxlen=200)

        self.seq: int = 0
        self.started_epoch: float = time.time()
        self.started_at: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_line_at: str | None = None
        self.last_hashrate_at: str | None = None
        self.last_share_at: str | None = None

        self.miner_version: str = ""
        self.algorithm: str = ""
        self.pool_url: str = ""
        self.worker_name: str = ""
        self.pool_connected: bool = False

        self.current_total_ths: float = 0.0
        self.total_power_w: float = 0.0
        self.miner_eff: float | None = None
        self.miner_uptime_sec: int = 0
        self.miner_uptime_text: str = ""
        self.miner_avgs: dict[str, float] = {}
        self.devices: dict[str, dict[str, Any]] = {}

        self.accepted_shares: int = 0
        self.rejected_shares: int = 0
        self.stale_shares: int = 0
        self.hw_errors: int = 0
        self._explicit_accepted: int = 0
        self._explicit_rejected: int = 0
        self.pool_disconnects: int = 0
        self.error_count: int = 0

    def reset(self) -> None:
        self.lines.clear()
        self.samples.clear()
        self.latencies_ms.clear()
        self.seq = 0
        self.started_epoch = time.time()
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_line_at = None
        self.last_hashrate_at = None
        self.last_share_at = None
        self.current_total_ths = 0.0
        self.total_power_w = 0.0
        self.miner_eff = None
        self.miner_uptime_sec = 0
        self.miner_uptime_text = ""
        self.miner_avgs.clear()
        self.devices.clear()
        self.accepted_shares = 0
        self.rejected_shares = 0
        self.stale_shares = 0
        self.hw_errors = 0
        self._explicit_accepted = 0
        self._explicit_rejected = 0
        self.pool_disconnects = 0
        self.error_count = 0

    def seed_metadata(
        self,
        *,
        algorithm: str = "",
        pool_url: str = "",
        worker_name: str = "",
        miner_version: str = "",
    ) -> None:
        if algorithm and not self.algorithm:
            self.algorithm = algorithm
        if pool_url and not self.pool_url:
            self.pool_url = pool_url
        if worker_name and not self.worker_name:
            self.worker_name = worker_name
        if miner_version and not self.miner_version:
            self.miner_version = miner_version

    def _ensure_device(self, dev_id: str, ts_str: str, name: str | None = None) -> dict[str, Any]:
        dev = self.devices.setdefault(
            dev_id,
            {
                "id": dev_id,
                "name": name or dev_id,
                "hashrate_ths": 0.0,
                "hashrate_text": "0.00 TH/s",
                "avg_hashrate_ths": 0.0,
                "temp_c": None,
                "fan_pct": None,
                "power_w": None,
                "eff_value": None,
                "cclk_mhz": None,
                "mclk_mhz": None,
                "accepted_shares": 0,
                "last_updated": ts_str,
                "_sum_ths": 0.0,
                "_cnt": 0,
            },
        )
        if name and (dev["name"] == dev["id"] or name != dev_id):
            dev["name"] = name
        return dev

    def _classify_and_parse(self, clean: str, ts_str: str, epoch: float) -> str:
        m_ts = TIMESTAMP_PREFIX_RE.match(clean)
        body = m_ts.group(2) if m_ts else clean
        low = body.lower()

        m_ver = VERSION_RE.search(body)
        if m_ver:
            self.miner_version = m_ver.group(1)

        m_alg = ALG_RE.search(body)
        if m_alg:
            self.algorithm = m_alg.group(1) or m_alg.group(2) or self.algorithm
        elif not self.algorithm:
            m_alg_sh = ALG_SHARE_RE.search(body)
            if m_alg_sh:
                self.algorithm = m_alg_sh.group(1)

        m_up = UPTIME_RE.search(body)
        if m_up:
            days = int(m_up.group(1) or 0)
            hrs = int(m_up.group(2))
            mins = int(m_up.group(3))
            secs = int(m_up.group(4))
            self.miner_uptime_sec = days * 86400 + hrs * 3600 + mins * 60 + secs
            self.miner_uptime_text = f"{days}d {hrs:02d}:{mins:02d}:{secs:02d}"

        m_wrk = WORKER_LINE_RE.search(body)
        if m_wrk:
            self.worker_name = m_wrk.group(1)

        m_pool = POOL_CONNECT_RE.search(body)
        if m_pool and "disconnect" not in low:
            self.pool_url = m_pool.group(1)
            if "connected" in low or "authorized" in low:
                self.pool_connected = True

        if "authorized on pool" in low or "job received" in low or "new job" in low:
            self.pool_connected = True

        # Check pool summary counts inside total speed line
        m_sum = TOTAL_SHARES_SUMMARY_RE.search(body)
        if m_sum:
            acc = m_sum.group(1) or m_sum.group(3)
            rej = m_sum.group(2) or m_sum.group(4)
            if acc is not None:
                self._explicit_accepted = max(self._explicit_accepted, int(acc))
                self.accepted_shares = max(self.accepted_shares, self._explicit_accepted)
            if rej is not None:
                self._explicit_rejected = max(self._explicit_rejected, int(rej))
                self.rejected_shares = max(self.rejected_shares, self._explicit_rejected)

        if SHARE_STATS_RE.search(body):
            return "share"

        # Total power line
        m_tp = TOTAL_POWER_RE.search(body)
        if m_tp:
            self.total_power_w = float(m_tp.group(1))

        # Errors / disconnects
        if any(k in low for k in ("disconnected", "connection lost", "failed to connect", "timeout")):
            self.pool_disconnects += 1
            self.pool_connected = False
            self.error_count += 1
            return "error"

        # Accepted shares
        is_share_accept_line = (
            ("share accepted" in low or "result accepted" in low)
            and not TOTAL_SHARES_SUMMARY_RE.search(body)
        )
        if not is_share_accept_line and re.search(r"\baccepted\s*[\(\[]", low):
            is_share_accept_line = True

        if is_share_accept_line:
            self.accepted_shares += 1
            self.pool_connected = True
            self.last_share_at = ts_str
            m_lat = LATENCY_RE.search(body)
            if m_lat:
                self.latencies_ms.append(float(m_lat.group(1)))
            m_dev = DEVICE_TAG_RE.search(body)
            if m_dev:
                dev_id = _normalize_device_id(m_dev.group(1))
                dev = self._ensure_device(dev_id, ts_str)
                dev["accepted_shares"] += 1
                dev["last_updated"] = ts_str
            return "share"

        # Rejected / stale shares
        is_share_reject_line = (
            ("share rejected" in low or "result rejected" in low or "stale share" in low)
            and not TOTAL_SHARES_SUMMARY_RE.search(body)
        )
        if is_share_reject_line:
            self.rejected_shares += 1
            if "stale" in low:
                self.stale_shares += 1
            self.error_count += 1
            return "warn"

        # SRBMiner v3.x tabular rolling average rows (1 min / 15 min / 1 hr / 6 hr)
        m_avg = SRB_V3_AVG_ROW_RE.match(body)
        if m_avg:
            raw_lbl = re.sub(r"\s+", "", m_avg.group(1).lower())
            win_map = {
                "1min": "1m",
                "5min": "5m",
                "15min": "15m",
                "1h": "1h",
                "1hr": "1h",
                "1hour": "1h",
                "6h": "6h",
                "6hr": "6h",
                "6hour": "6h",
            }
            win_key = win_map.get(raw_lbl)
            if win_key:
                self.miner_avgs[win_key] = round(to_ths(float(m_avg.group(2)), m_avg.group(3)), 4)
            return "hashrate"

        # SRBMiner v3.x tabular Total Hashrate row:
        #   Hashrate  215.49 TH/s  225.5W  0.956  3  0  0
        m_v3_tot = SRB_V3_TOTAL_ROW_RE.match(body)
        if m_v3_tot:
            ths = to_ths(float(m_v3_tot.group(1)), m_v3_tot.group(2))
            self._record_total_sample(ths, ts_str, epoch)
            tail = m_v3_tot.group(3) or ""
            m_pwr = POWER_RE.search(tail)
            after_pwr = tail
            if m_pwr:
                self.total_power_w = round(float(m_pwr.group(1)), 1)
                after_pwr = tail[m_pwr.end() :]
            m_eff = re.match(r"^\s*([0-9]+\.[0-9]+)(.*)$", after_pwr)
            tail_nums = after_pwr
            if m_eff:
                self.miner_eff = round(float(m_eff.group(1)), 3)
                tail_nums = m_eff.group(2)
            ints = [int(x) for x in re.findall(r"\b\d+\b", tail_nums)]
            if len(ints) >= 3:
                self._explicit_accepted = max(self._explicit_accepted, ints[-3])
                self.accepted_shares = max(self.accepted_shares, ints[-3])
                self._explicit_rejected = max(self._explicit_rejected, ints[-2])
                self.rejected_shares = max(self.rejected_shares, ints[-2])
                self.hw_errors = max(self.hw_errors, ints[-1])
            return "hashrate"

        # SRBMiner v3.x tabular per-GPU/CPU row:
        #   #1  RTX 5080  215.49 TH/s  225.5W  0.956  31%  52C  2760  14499  3  0  0
        m_v3_dev = SRB_V3_DEVICE_ROW_RE.match(body)
        if m_v3_dev:
            dev_id = f"GPU{int(m_v3_dev.group(1))}"
            dev_name = re.sub(r"\s+", " ", m_v3_dev.group(2)).strip()
            ths = to_ths(float(m_v3_dev.group(3)), m_v3_dev.group(4))
            tail = m_v3_dev.group(5) or ""

            dev = self._ensure_device(dev_id, ts_str, name=dev_name)
            dev["hashrate_ths"] = round(ths, 6)
            dev["hashrate_text"] = format_ths(ths)
            dev["_sum_ths"] += ths
            dev["_cnt"] += 1
            dev["avg_hashrate_ths"] = round(dev["_sum_ths"] / dev["_cnt"], 6)
            dev["last_updated"] = ts_str

            m_pwr = POWER_RE.search(tail)
            after_pwr = tail
            if m_pwr:
                dev["power_w"] = round(float(m_pwr.group(1)), 1)
                after_pwr = tail[m_pwr.end() :]
            m_eff = re.match(r"^\s*([0-9]+\.[0-9]+)\b", after_pwr)
            if m_eff:
                dev["eff_value"] = round(float(m_eff.group(1)), 3)
            m_fan = FAN_INLINE_RE.search(tail) or FAN_RE.search(tail)
            if m_fan:
                fval = int(m_fan.group(1))
                if 0 <= fval <= 100:
                    dev["fan_pct"] = fval
            m_temp = TEMP_INLINE_RE.search(tail) or TEMP_RE.search(tail)
            if m_temp:
                tval = int(m_temp.group(1))
                if 15 <= tval <= 120:
                    dev["temp_c"] = tval

            after_temp = tail[m_temp.end() :] if m_temp else tail
            ints = [int(x) for x in re.findall(r"\b\d+\b", after_temp)]
            if len(ints) >= 5:
                dev["cclk_mhz"] = ints[0]
                dev["mclk_mhz"] = ints[1]
                dev["accepted_shares"] = max(dev["accepted_shares"], ints[-3])
            elif len(ints) == 3 and ints[0] < 1000 and ints[1] < 1000:
                dev["accepted_shares"] = max(dev["accepted_shares"], ints[0])

            dev_sum = sum(float(d.get("hashrate_ths") or 0.0) for d in self.devices.values())
            if dev_sum > 0:
                self._record_total_sample(dev_sum, ts_str, epoch)
            pwr_sum = sum(float(d["power_w"]) for d in self.devices.values() if d.get("power_w") is not None)
            if pwr_sum > 0:
                self.total_power_w = round(pwr_sum, 1)
            return "hashrate"

        # Legacy Per-device line or segments (GPU0 / GPU 1 / CPU ...), including inline multi-GPU + Total lines
        found_hashrate = False
        segments = body.split("|") if "|" in body else [body]
        for seg in segments:
            if re.search(r"\bTotal\s*(?:\[[^\]]+\]\s*)?(?:hash\s*rate|hashrate|speed)?\s*[:=]", seg, re.IGNORECASE):
                continue
            m_dev = DEVICE_TAG_RE.search(seg)
            m_hr = HASHRATE_UNIT_RE.search(seg)
            if m_dev and m_hr:
                found_hashrate = True
                dev_id = _normalize_device_id(m_dev.group(1))
                ths = to_ths(float(m_hr.group(1)), m_hr.group(2))
                m_dname = DEVICE_NAME_RE.search(seg)
                dev = self._ensure_device(
                    dev_id,
                    ts_str,
                    name=m_dname.group(1).strip() if m_dname else None,
                )
                dev["hashrate_ths"] = round(ths, 6)
                dev["hashrate_text"] = format_ths(ths)
                dev["_sum_ths"] += ths
                dev["_cnt"] += 1
                dev["avg_hashrate_ths"] = round(dev["_sum_ths"] / dev["_cnt"], 6)
                dev["last_updated"] = ts_str

                m_temp = TEMP_RE.search(seg) or TEMP_INLINE_RE.search(seg)
                if m_temp:
                    tval = int(m_temp.group(1))
                    if 15 <= tval <= 120:
                        dev["temp_c"] = tval
                m_fan = FAN_RE.search(seg) or FAN_INLINE_RE.search(seg)
                if m_fan:
                    fval = int(m_fan.group(1))
                    if 0 <= fval <= 100:
                        dev["fan_pct"] = fval
                m_pwr = POWER_RE.search(seg)
                if m_pwr:
                    dev["power_w"] = round(float(m_pwr.group(1)), 1)

        # Explicit Total hashrate line
        has_explicit_total = False
        for pat in TOTAL_HASH_PATTERNS:
            m_tot = pat.search(body)
            if m_tot:
                ths = to_ths(float(m_tot.group(1)), m_tot.group(2))
                self._record_total_sample(ths, ts_str, epoch)
                has_explicit_total = True
                found_hashrate = True
                break

        if found_hashrate:
            if not has_explicit_total:
                if len(self.devices) == 1:
                    only_dev = next(iter(self.devices.values()))
                    self._record_total_sample(float(only_dev["hashrate_ths"]), ts_str, epoch)
                elif len(self.devices) > 1:
                    dev_sum = sum(float(d.get("hashrate_ths") or 0.0) for d in self.devices.values())
                    self._record_total_sample(dev_sum, ts_str, epoch)

            pwr_sum = sum(float(d["power_w"]) for d in self.devices.values() if d.get("power_w") is not None)
            if pwr_sum > 0 and not TOTAL_POWER_RE.search(body):
                self.total_power_w = round(pwr_sum, 1)
            return "hashrate"

        if "[error]" in low or "fatal" in low or "failed" in low:
            self.error_count += 1
            return "error"
        if "[warn]" in low or "warning" in low:
            return "warn"
        if "pool" in low or "job" in low or "block" in low:
            return "pool"
        return "info"

    def _record_total_sample(self, ths: float, ts_str: str, epoch: float) -> None:
        self.current_total_ths = round(ths, 6)
        self.last_hashrate_at = ts_str
        # Merge device-sum & explicit-Total sample ONLY if same timestamp/within 2s;
        # keep every distinct periodic sample (e.g. 18:29:35 vs 18:30:05).
        if (
            self.samples
            and (
                self.samples[-1]["time"] == ts_str[-8:]
                or abs(epoch - self.samples[-1]["ts"]) < 2.0
            )
        ):
            self.samples[-1] = {
                "ts": round(epoch, 2),
                "time": ts_str[-8:],
                "ths": round(ths, 4),
            }
        else:
            self.samples.append(
                {
                    "ts": round(epoch, 2),
                    "time": ts_str[-8:],
                    "ths": round(ths, 4),
                }
            )

    def feed_line(self, raw_line: str, ts_override: str | None = None, epoch_override: float | None = None) -> SrbLogLine | None:
        clean = clean_console_line(raw_line)
        if not clean.strip():
            return None

        now_epoch = time.time()
        epoch = epoch_override if epoch_override is not None else now_epoch
        ts_str = ts_override or datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        m_ts = TIMESTAMP_PREFIX_RE.match(clean)
        if m_ts:
            raw_t = m_ts.group(1)
            if len(raw_t) == 8:
                ts_str = f"{datetime.fromtimestamp(epoch).strftime('%Y-%m-%d')} {raw_t}"
            else:
                ts_str = raw_t
            if epoch_override is None:
                try:
                    epoch = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    pass

        level = self._classify_and_parse(clean, ts_str, epoch)
        self.seq += 1
        self.last_line_at = ts_str
        entry = SrbLogLine(seq=self.seq, ts=ts_str, level=level, line=clean)
        self.lines.append(entry)
        return entry

    def get_report(self, since_seq: int = 0) -> dict[str, Any]:
        now = time.time()
        all_samples = list(self.samples)
        vals = [s["ths"] for s in all_samples if s["ths"] > 0]
        vals_5m = [s["ths"] for s in all_samples if s["ths"] > 0 and (now - s["ts"]) <= 300]
        vals_15m = [s["ths"] for s in all_samples if s["ths"] > 0 and (now - s["ts"]) <= 900]
        vals_1h = [s["ths"] for s in all_samples if s["ths"] > 0 and (now - s["ts"]) <= 3600]

        def _avg(arr: list[float]) -> float:
            return round(sum(arr) / len(arr), 4) if arr else 0.0

        session_avg = _avg(vals)
        min_ths = round(min(vals), 4) if vals else 0.0
        max_ths = round(max(vals), 4) if vals else 0.0
        if len(vals) >= 2 and session_avg > 0:
            variance = sum((v - session_avg) ** 2 for v in vals) / len(vals)
            stdev = math.sqrt(variance)
            stability = max(0.0, min(100.0, (1.0 - (stdev / session_avg)) * 100.0))
        elif vals:
            stability = 100.0
        else:
            stability = 0.0

        total_submitted = self.accepted_shares + self.rejected_shares
        accept_rate = (self.accepted_shares / total_submitted * 100.0) if total_submitted > 0 else 100.0
        uptime_sec = max(0, int(now - self.started_epoch), int(self.miner_uptime_sec))
        elapsed_hours = max(uptime_sec / 3600.0, 1.0 / 60.0)
        shares_per_hour = round(self.accepted_shares / elapsed_hours, 1) if self.accepted_shares > 0 else 0.0

        lats = list(self.latencies_ms)
        avg_lat = round(sum(lats) / len(lats), 1) if lats else None
        min_lat = round(min(lats), 1) if lats else None
        max_lat = round(max(lats), 1) if lats else None
        last_lat = round(lats[-1], 1) if lats else None

        dev_list = []
        for k in sorted(self.devices.keys()):
            d = self.devices[k]
            dev_list.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "hashrate_ths": round(float(d["hashrate_ths"]), 4),
                    "hashrate_text": d["hashrate_text"],
                    "avg_hashrate_ths": round(float(d["avg_hashrate_ths"]), 4),
                    "temp_c": d["temp_c"],
                    "fan_pct": d["fan_pct"],
                    "power_w": d["power_w"],
                    "eff_value": d.get("eff_value"),
                    "cclk_mhz": d.get("cclk_mhz"),
                    "mclk_mhz": d.get("mclk_mhz"),
                    "accepted_shares": d["accepted_shares"],
                    "last_updated": d["last_updated"],
                }
            )

        eff_ths_per_kw = (
            round(self.current_total_ths / (self.total_power_w / 1000.0), 2)
            if self.total_power_w > 0 and self.current_total_ths > 0
            else None
        )
        eff_th_per_w = (
            self.miner_eff
            if self.miner_eff is not None
            else (round(self.current_total_ths / self.total_power_w, 3) if self.total_power_w > 0 and self.current_total_ths > 0 else None)
        )

        filtered_lines = [l.to_dict() for l in self.lines if l.seq > since_seq]

        return {
            "miner_version": self.miner_version or "SRBMiner-MULTI",
            "algorithm": self.algorithm or "--",
            "pool_url": self.pool_url or "--",
            "worker_name": self.worker_name or "",
            "pool_connected": self.pool_connected,
            "started_at": self.started_at,
            "uptime_sec": uptime_sec,
            "miner_uptime_text": self.miner_uptime_text,
            "last_line_at": self.last_line_at,
            "last_hashrate_at": self.last_hashrate_at,
            "last_share_at": self.last_share_at,
            "current_hashrate_ths": round(self.current_total_ths, 4),
            "current_hashrate_text": format_ths(self.current_total_ths),
            "session_avg_ths": session_avg,
            "session_avg_text": format_ths(session_avg),
            "avg_5m_ths": _avg(vals_5m) or self.miner_avgs.get("5m", 0.0),
            "avg_15m_ths": self.miner_avgs.get("15m") or _avg(vals_15m),
            "avg_1h_ths": self.miner_avgs.get("1h") or _avg(vals_1h),
            "miner_avg_1m_ths": self.miner_avgs.get("1m", 0.0),
            "miner_avg_15m_ths": self.miner_avgs.get("15m", 0.0),
            "miner_avg_1h_ths": self.miner_avgs.get("1h", 0.0),
            "miner_avg_6h_ths": self.miner_avgs.get("6h", 0.0),
            "min_hashrate_ths": min_ths,
            "max_hashrate_ths": max_ths,
            "stability_pct": round(stability, 1),
            "sample_count": len(vals),
            "samples": all_samples[-120:],
            "accepted_shares": self.accepted_shares,
            "rejected_shares": self.rejected_shares,
            "stale_shares": self.stale_shares,
            "hw_errors": self.hw_errors,
            "accept_rate_pct": round(accept_rate, 2),
            "shares_per_hour": shares_per_hour,
            "last_latency_ms": last_lat,
            "avg_latency_ms": avg_lat,
            "min_latency_ms": min_lat,
            "max_latency_ms": max_lat,
            "total_power_w": round(self.total_power_w, 1),
            "efficiency_ths_per_kw": eff_ths_per_kw,
            "efficiency_th_per_w": eff_th_per_w,
            "pool_disconnects": self.pool_disconnects,
            "error_count": self.error_count,
            "devices": dev_list,
            "latest_seq": self.seq,
            "lines": filtered_lines,
        }


def decode_text_bytes(raw: bytes) -> str:
    """Decode bytes cleanly across UTF-16 BOM, UTF-8 BOM/no-BOM, and Chinese GB18030/CP936."""
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text_tolerant(path: Path) -> str:
    return decode_text_bytes(path.read_bytes())


def parse_srb_cmdline(cmd_text: str) -> dict[str, Any]:
    """Extract SRBMiner CLI arguments from a .bat line or running process CommandLine."""
    info: dict[str, Any] = {
        "executable": "",
        "algorithm": "",
        "pool": "",
        "wallet": "",
        "worker": "",
        "log_file": "",
        "api_enable": False,
        "api_port": None,
        "api_rig_name": "",
        "uses_start": False,
    }
    if not cmd_text:
        return info

    low = cmd_text.lower()
    if re.search(r"^\s*start\b", low):
        info["uses_start"] = True

    try:
        tokens = shlex.split(cmd_text, posix=False)
    except ValueError:
        tokens = cmd_text.split()

    cleaned_tokens = [t.strip('"').strip("'") for t in tokens]
    for idx, tok in enumerate(cleaned_tokens):
        ltok = tok.lower()
        if "srbminer" in ltok and (ltok.endswith(".exe") or "srbminer-multi" in ltok):
            info["executable"] = tok
        next_val = cleaned_tokens[idx + 1] if idx + 1 < len(cleaned_tokens) else ""
        if ltok in ("--algorithm", "-a") and next_val:
            info["algorithm"] = next_val
        elif ltok in ("--pool", "-p") and next_val:
            info["pool"] = next_val
        elif ltok in ("--wallet", "-w") and next_val:
            info["wallet"] = next_val
            if "." in next_val and not info["worker"]:
                parts = next_val.rsplit(".", 1)
                if parts[1]:
                    info["worker"] = parts[1]
        elif ltok in ("--worker", "--rig-name") and next_val:
            info["worker"] = next_val
        elif ltok == "--log-file" and next_val:
            info["log_file"] = next_val
        elif ltok == "--api-enable":
            info["api_enable"] = True
        elif ltok == "--api-port" and next_val:
            try:
                info["api_port"] = int(next_val)
            except ValueError:
                pass
        elif ltok == "--api-rig-name" and next_val:
            info["api_rig_name"] = next_val

    if info["api_enable"] and info["api_port"] is None:
        info["api_port"] = 21550
    return info


def parse_bat_file(bat_path: str | Path) -> dict[str, Any]:
    """Analyze a Windows .bat mining launch script for SRBMiner configuration."""
    p = Path(bat_path)
    if not p.exists():
        raise FileNotFoundError(f"未找到 .bat 文件: {p}")

    content = read_text_tolerant(p)
    # Join cmd caret (^) line continuations logically
    raw_lines = content.splitlines()
    logical_lines: list[str] = []
    buf = ""
    for line in raw_lines:
        stripped = line.rstrip()
        if stripped.endswith("^"):
            buf += stripped[:-1] + " "
        else:
            buf += line
            logical_lines.append(buf)
            buf = ""
    if buf:
        logical_lines.append(buf)

    srb_line = ""
    has_pause = False
    has_loop = False
    for line in logical_lines:
        s = line.strip()
        slow = s.lower()
        if not s or slow.startswith("rem ") or slow.startswith("::"):
            continue
        if "srbminer" in slow and not srb_line:
            srb_line = s
        if re.match(r"^pause\b", slow):
            has_pause = True
        if re.match(r"^goto\b", slow) or re.match(r"^:[a-z0-9_]+$", slow):
            has_loop = True

    parsed = parse_srb_cmdline(srb_line)
    # Resolve relative log_file path against bat directory
    resolved_log = ""
    if parsed["log_file"]:
        lp = Path(parsed["log_file"])
        resolved_log = str(lp if lp.is_absolute() else (p.parent / lp))

    return {
        "bat_path": str(p),
        "bat_dir": str(p.parent),
        "bat_name": p.name,
        "srb_line": srb_line,
        "has_pause": has_pause,
        "has_loop": has_loop,
        "resolved_log_file": resolved_log,
        **parsed,
        "preview_lines": raw_lines[:60],
    }


def build_wrapped_bat_content(bat_path: str | Path, managed_log_path: str | Path) -> str:
    """Create a non-destructive temporary .bat runner that guarantees log output without mutating user's .bat."""
    p = Path(bat_path)
    bat_dir = str(p.parent)
    log_path = str(managed_log_path)
    content = read_text_tolerant(p)

    raw_lines = content.splitlines()
    out_lines = ["@echo off", "chcp 65001 >nul", f'cd /d "{bat_dir}"']
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        # Merge caret lines so SRBMiner options stay together
        combined = line
        while combined.rstrip().endswith("^") and i + 1 < len(raw_lines):
            i += 1
            combined = combined.rstrip()[:-1] + " " + raw_lines[i].lstrip()

        stripped = combined.strip()
        slow = stripped.lower()
        # Expand %~dp0 cleanly inside temp wrapper (lambda avoids regex backslash escapes)
        dp0_prefix = bat_dir.rstrip("\\/") + "\\"
        expanded = re.sub(r"%~dp0", lambda _: dp0_prefix, combined, flags=re.IGNORECASE)

        if re.match(r"^pause\b", slow) or slow.startswith("chcp ") or slow == "@echo off":
            i += 1
            continue

        if (
            "srbminer" in slow
            and not slow.startswith(("rem ", "::", "echo ", "title "))
        ):
            # Strip standalone `start "" ` prefix if present so process runs synchronously inside wrapper
            expanded = re.sub(
                r'^\s*start\s+(?:(?:"[^"]*"|/\w+)\s+)*',
                "",
                expanded,
                flags=re.IGNORECASE,
            )
            if "--log-file" not in slow:
                expanded = f'{expanded} --log-file "{log_path}"'
        out_lines.append(expanded)
        i += 1

    return "\r\n".join(out_lines) + "\r\n"
