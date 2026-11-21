"""Local Windows .bat / SRBMiner process & log output binder with remote telemetry push."""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .paths import get_data_dir
from .srb_parser import (
    SrbTelemetryParser,
    build_wrapped_bat_content,
    decode_text_bytes,
    parse_bat_file,
    parse_srb_cmdline,
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CONSOLE_READ_LOCK = threading.Lock()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_TS_LINE_START_RE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}\]")


def unwrap_console_rows(rows: list[str], console_width: int = 120) -> list[str]:
    """Reassemble logical console lines that wrapped at Windows console buffer column boundaries."""
    merged: list[str] = []
    min_wrap_len = max(50, int(max(60, console_width) * 0.68))
    for r in rows:
        txt = r.rstrip()
        if not txt:
            continue
        if (
            merged
            and not _TS_LINE_START_RE.match(txt)
            and _TS_LINE_START_RE.match(merged[-1])
            and len(merged[-1]) >= min_wrap_len
        ):
            if txt[0].isspace() or len(merged[-1]) >= console_width - 1 or merged[-1][-1] in "-./:":
                merged[-1] = merged[-1] + txt
            else:
                merged[-1] = merged[-1] + " " + txt
        else:
            merged.append(txt)
    return merged


def diff_console_lines(prev_lines: list[str], curr_lines: list[str]) -> list[str]:
    """Compute newly appeared lines when comparing two consecutive console buffer snapshots."""
    if not curr_lines:
        return []
    if not prev_lines:
        return list(curr_lines)
    if curr_lines == prev_lines:
        return []

    max_k = min(len(prev_lines), len(curr_lines))
    for k in range(max_k, 0, -1):
        if prev_lines[-k:] == curr_lines[:k]:
            return curr_lines[k:]

    # Fallback: anchor on recent lines (in case the very last line finished wrapping on next tick)
    for back_idx in range(min(4, len(prev_lines))):
        anchor = prev_lines[-1 - back_idx]
        for idx in range(len(curr_lines) - 1, -1, -1):
            if curr_lines[idx] == anchor:
                return curr_lines[idx + 1 + back_idx :] if idx + 1 + back_idx < len(curr_lines) else []
            if back_idx == 0 and len(anchor) >= 24 and curr_lines[idx].startswith(anchor):
                return curr_lines[idx:]

    return list(curr_lines)


def _snapshot_console_lines(pid: int, max_rows: int = 120) -> tuple[list[str], str | None]:
    """Safely peek the visible/recent console buffer lines of a Windows process."""
    if os.name != "nt":
        return [], "仅支持 Windows 控制台抓取"

    with _CONSOLE_READ_LOCK:
        k32 = ctypes.windll.kernel32
        attached = False
        handle = None
        try:
            k32.SetConsoleCtrlHandler(None, True)
            k32.FreeConsole()
            if not k32.AttachConsole(int(pid)):
                err = ctypes.GetLastError()
                if err == 5:
                    return [], "无权限附加到目标控制台（若矿机以管理员运行，请使用「绑定 .bat 启动」或 --log-file）"
                if err == 6:
                    return [], "目标进程没有独立控制台窗口（可能以无窗口/后台服务或 Windows Terminal 分离模式运行）"
                return [], f"AttachConsole 失败 (错误码 {err})"
            attached = True

            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            FILE_SHARE_RW = 3
            OPEN_EXISTING = 3
            handle = k32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
            if not handle or handle == wintypes.HANDLE(-1).value:
                return [], "无法打开目标控制台输出缓冲区"

            class COORD(ctypes.Structure):
                _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

            class SMALL_RECT(ctypes.Structure):
                _fields_ = [
                    ("Left", wintypes.SHORT),
                    ("Top", wintypes.SHORT),
                    ("Right", wintypes.SHORT),
                    ("Bottom", wintypes.SHORT),
                ]

            class CSBI(ctypes.Structure):
                _fields_ = [
                    ("dwSize", COORD),
                    ("dwCursorPosition", COORD),
                    ("wAttributes", wintypes.WORD),
                    ("srWindow", SMALL_RECT),
                    ("dwMaximumWindowSize", COORD),
                ]

            csbi = CSBI()
            if not k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(csbi)):
                return [], "读取控制台缓冲区信息失败"

            width = max(1, int(csbi.dwSize.X))
            end_y = min(max(int(csbi.dwCursorPosition.Y) + 1, int(csbi.srWindow.Bottom) + 1), int(csbi.dwSize.Y))
            start_y = max(0, end_y - max_rows)

            lines: list[str] = []
            buf = ctypes.create_unicode_buffer(width)
            read = wintypes.DWORD()
            for y in range(start_y, end_y):
                coord_val = (y << 16) | 0
                if k32.ReadConsoleOutputCharacterW(handle, buf, width, wintypes.DWORD(coord_val), ctypes.byref(read)):
                    txt = buf.value[: read.value].rstrip()
                    if txt:
                        lines.append(txt)
            return unwrap_console_rows(lines, console_width=width), None
        except Exception as exc:
            return [], str(exc)
        finally:
            if handle and handle != wintypes.HANDLE(-1).value:
                try:
                    k32.CloseHandle(handle)
                except Exception:
                    pass
            if attached:
                try:
                    k32.FreeConsole()
                except Exception:
                    pass


def scan_running_srbminer_processes() -> list[dict[str, Any]]:
    """Discover running SRBMiner-MULTI and mining .bat cmd processes on Windows."""
    if os.name != "nt":
        return []

    ps_script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=6,
            creationflags=CREATE_NO_WINDOW,
        )
        stdout_txt = decode_text_bytes(res.stdout or b"").strip()
        if res.returncode != 0 or not stdout_txt:
            return []
        raw = json.loads(stdout_txt)
        procs = raw if isinstance(raw, list) else [raw]
    except Exception:
        return []

    by_pid: dict[int, dict[str, Any]] = {}
    for p in procs:
        if isinstance(p, dict) and p.get("ProcessId"):
            by_pid[int(p["ProcessId"])] = p

    found: list[dict[str, Any]] = []
    for pid, p in by_pid.items():
        name = str(p.get("Name") or "")
        cmd = str(p.get("CommandLine") or "")
        exe = str(p.get("ExecutablePath") or "")
        low_name = name.lower()
        low_cmd = cmd.lower()

        is_srb = "srbminer" in low_name or (
            "srbminer" in low_cmd
            and low_name not in ("powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe")
        )
        is_bat_miner = (
            low_name == "cmd.exe"
            and ".bat" in low_cmd
            and any(k in low_cmd for k in ("srb", "pearl", "prl", "miner", "mine"))
        )
        if not is_srb and not is_bat_miner:
            continue

        parent_pid = int(p.get("ParentProcessId") or 0)
        parent = by_pid.get(parent_pid) or {}
        parent_cmd = str(parent.get("CommandLine") or "")
        parent_name = str(parent.get("Name") or "")

        bat_match = re.search(
            r'"([A-Za-z]:\\[^"\r\n]+?\.(?:bat|cmd))"|([A-Za-z]:\\[^"\r\n]+?\.(?:bat|cmd))|([^\s"]+\.(?:bat|cmd))',
            parent_cmd or cmd,
            re.IGNORECASE,
        )
        bat_source = next((g for g in bat_match.groups() if g), "") if bat_match else ""

        cli_info = parse_srb_cmdline(cmd)
        work_dir = str(Path(exe).parent) if exe else (str(Path(bat_source).parent) if bat_source else "")
        detected_log = ""
        if cli_info["log_file"]:
            lp = Path(cli_info["log_file"])
            detected_log = str(lp if lp.is_absolute() or not work_dir else Path(work_dir) / lp)
        elif work_dir and Path(work_dir).exists():
            # Look for very recently modified .log files in miner directory or Logs/
            candidates: list[Path] = []
            wd = Path(work_dir)
            for pattern_dir in (wd, wd / "Logs", wd / "logs"):
                if pattern_dir.is_dir():
                    candidates.extend(pattern_dir.glob("*.log"))
            now_ts = time.time()
            fresh = [c for c in candidates if c.is_file() and (now_ts - c.stat().st_mtime) < 3600]
            if fresh:
                fresh.sort(key=lambda c: c.stat().st_mtime, reverse=True)
                detected_log = str(fresh[0])

        found.append(
            {
                "pid": pid,
                "parent_pid": parent_pid,
                "parent_name": parent_name,
                "parent_bat": bat_source,
                "name": name,
                "executable_path": exe,
                "work_dir": work_dir,
                "command_line": cmd,
                "detected_log_file": detected_log,
                "is_primary_miner": is_srb,
                **cli_info,
            }
        )

    found.sort(key=lambda x: (0 if x["is_primary_miner"] else 1, x["pid"]))
    return found


class LocalMinerBinder:
    """Manages local .bat/SRBMiner binding, output parsing, and telemetry push."""

    def __init__(
        self,
        get_settings_fn: Callable[[], dict[str, str]],
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._get_settings = get_settings_fn
        self._on_update = on_update
        self._lock = threading.Lock()
        self._parser = SrbTelemetryParser()
        self._stop_event = threading.Event()
        self._worker_threads: list[threading.Thread] = []

        self.mode: str = "none"  # none | bat_managed | pid_attach | log_tail
        self.status: str = "idle"  # idle | running | stopped | error
        self.source_label: str = "未绑定"
        self.bat_path: str = ""
        self.log_path: str = ""
        self.attached_pid: int | None = None
        self.api_port: int | None = None
        self.last_error: str | None = None
        self.using_log_tail: bool = False
        self._proc: subprocess.Popen | None = None
        self._temp_wrapper_path: Path | None = None
        self._api_extra: dict[str, Any] = {}

        # Remote telemetry pusher thread
        self._push_stop = threading.Event()
        self._push_thread: threading.Thread | None = None
        self._last_pushed_seq: int = 0
        self.last_push_at: str | None = None
        self.last_push_error: str | None = None
        self.push_count: int = 0
        self.client_id: str = f"{socket.gethostname().lower()}-{os.getpid() & 0xFFFF:x}"

    def start_background_pusher(self) -> None:
        if self._push_thread and self._push_thread.is_alive():
            return
        self._push_stop.clear()
        self._push_thread = threading.Thread(target=self._push_loop, name="TelemetryPusher", daemon=True)
        self._push_thread.start()

    def stop_all(self) -> None:
        self.unbind(stop_managed_process=True)
        self._push_stop.set()
        if self._push_thread and self._push_thread.is_alive():
            self._push_thread.join(timeout=2)

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update({"type": "miner_output"})
            except Exception:
                pass

    def _clear_workers_locked(self, stop_managed_process: bool) -> None:
        self._stop_event.set()
        if stop_managed_process and self._proc is not None:
            pid = self._proc.pid
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=4,
                    )
                else:
                    self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._temp_wrapper_path and self._temp_wrapper_path.exists():
            try:
                self._temp_wrapper_path.unlink()
            except Exception:
                pass
        self._temp_wrapper_path = None

    def unbind(self, stop_managed_process: bool = True) -> None:
        with self._lock:
            threads = list(self._worker_threads)
            self._clear_workers_locked(stop_managed_process=stop_managed_process)
            self._worker_threads.clear()
            self.mode = "none"
            self.status = "idle"
            self.source_label = "未绑定"
            self.attached_pid = None
            self.api_port = None
            self.using_log_tail = False
            self.last_error = None
            self._api_extra = {}
        for t in threads:
            if t.is_alive():
                t.join(timeout=1.5)

    def stop_managed_bat(self) -> None:
        with self._lock:
            if self.mode == "bat_managed":
                self._clear_workers_locked(stop_managed_process=True)
                self.status = "stopped"
                self._parser.feed_line(f"[{_now_str()}] [INFO] 已手动停止托管的 .bat 矿机进程。")
        self._notify()

    def clear_logs(self) -> None:
        with self._lock:
            self._parser.reset()
            self._last_pushed_seq = 0
        self._notify()

    def send_stats_key(self) -> bool:
        """Send 's' stdin key if running in pipe mode (prints summary if supported)."""
        with self._lock:
            if self._proc and self._proc.stdin and self._proc.poll() is None:
                try:
                    self._proc.stdin.write("s\n")
                    self._proc.stdin.flush()
                    return True
                except Exception:
                    return False
        return False

    def bind_and_start_bat(self, bat_path: str, auto_inject_log: bool = True) -> dict[str, Any]:
        bat_info = parse_bat_file(bat_path)
        self.unbind(stop_managed_process=True)

        p = Path(bat_info["bat_path"])
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = get_data_dir() / "srb_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Clean managed logs older than 7 days
        now_ts = time.time()
        for old in log_dir.glob("*.log"):
            try:
                if (now_ts - old.stat().st_mtime) > 7 * 86400:
                    old.unlink()
            except Exception:
                pass

        resolved_existing_log = bat_info.get("resolved_log_file") or ""
        target_log_path: str = ""
        launch_bat: Path = p
        temp_wrapper: Path | None = None

        if resolved_existing_log:
            target_log_path = resolved_existing_log
        elif auto_inject_log:
            managed_log = log_dir / f"{p.stem}_{stamp}.log"
            target_log_path = str(managed_log)
            temp_dir = get_data_dir() / "srb_temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_wrapper = temp_dir / f"_run_{p.stem}_{stamp}.bat"
            temp_wrapper.write_text(
                build_wrapped_bat_content(p, managed_log),
                encoding="utf-8",
            )
            launch_bat = temp_wrapper

        with self._lock:
            self._stop_event.clear()
            self._parser.reset()
            self._last_pushed_seq = 0
            self._parser.seed_metadata(
                algorithm=bat_info.get("algorithm") or "",
                pool_url=bat_info.get("pool") or "",
                worker_name=bat_info.get("worker") or "",
            )
            self.mode = "bat_managed"
            self.status = "running"
            self.bat_path = str(p)
            self.log_path = target_log_path
            self.api_port = bat_info.get("api_port")
            self.source_label = f".bat 托管运行: {p.name}"
            self.last_error = None
            self.using_log_tail = bool(target_log_path)
            self._temp_wrapper_path = temp_wrapper

            try:
                proc = subprocess.Popen(
                    ["cmd.exe", "/c", str(launch_bat)],
                    cwd=str(p.parent),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                self.status = "error"
                self.last_error = f"启动 .bat 失败: {exc}"
                raise RuntimeError(self.last_error) from exc

            self._proc = proc
            self.attached_pid = proc.pid
            self._parser.feed_line(
                f"[{_now_str()}] [INFO] 已由 Pearl 助手托管启动: {p} (PID: {proc.pid})"
            )

            t_stdout = threading.Thread(
                target=self._read_process_stdout,
                args=(proc,),
                name="SrbStdoutReader",
                daemon=True,
            )
            self._worker_threads = [t_stdout]
            t_stdout.start()

            if target_log_path:
                t_log = threading.Thread(
                    target=self._tail_log_loop,
                    args=(Path(target_log_path), bool(resolved_existing_log), False),
                    name="SrbLogTailer",
                    daemon=True,
                )
                self._worker_threads.append(t_log)
                t_log.start()

        self._notify()
        return self.get_status()

    def attach_running_pid(self, pid: int, log_path_override: str = "") -> dict[str, Any]:
        procs = scan_running_srbminer_processes()
        proc_info = next((x for x in procs if x["pid"] == int(pid)), None)
        if not proc_info:
            raise ValueError(f"未找到运行中的进程 PID {pid}，请重新扫描后再试")

        log_path = (log_path_override or proc_info.get("detected_log_file") or "").strip()
        self.unbind(stop_managed_process=True)

        with self._lock:
            self._stop_event.clear()
            self._parser.reset()
            self._last_pushed_seq = 0
            self._parser.seed_metadata(
                algorithm=proc_info.get("algorithm") or "",
                pool_url=proc_info.get("pool") or "",
                worker_name=proc_info.get("worker") or "",
            )
            self.mode = "pid_attach"
            self.status = "running"
            self.attached_pid = int(pid)
            self.bat_path = proc_info.get("parent_bat") or ""
            self.log_path = log_path
            self.api_port = proc_info.get("api_port") or 21550
            bat_hint = f" · 来自 {Path(self.bat_path).name}" if self.bat_path else ""
            self.source_label = f"已绑定进程 PID {pid} ({proc_info['name']}{bat_hint})"
            self.using_log_tail = bool(log_path and Path(log_path).exists())
            self.last_error = None
            self._parser.feed_line(f"[{_now_str()}] [INFO] {self.source_label}")

            if log_path:
                t_log = threading.Thread(
                    target=self._tail_log_loop,
                    args=(Path(log_path), True, True),
                    name="SrbPidLogTailer",
                    daemon=True,
                )
                self._worker_threads = [t_log]
                t_log.start()
            else:
                t_con = threading.Thread(
                    target=self._watch_console_pid_loop,
                    args=(int(pid),),
                    name="SrbConsoleWatcher",
                    daemon=True,
                )
                self._worker_threads = [t_con]
                t_con.start()

            if self.api_port:
                t_api = threading.Thread(
                    target=self._poll_srb_local_api_loop,
                    args=(int(self.api_port),),
                    name="SrbApiPoller",
                    daemon=True,
                )
                self._worker_threads.append(t_api)
                t_api.start()

        self._notify()
        return self.get_status()

    def attach_log_file(self, log_path: str) -> dict[str, Any]:
        p = Path(log_path)
        if not p.exists():
            raise FileNotFoundError(f"日志文件不存在: {p}")

        self.unbind(stop_managed_process=True)
        with self._lock:
            self._stop_event.clear()
            self._parser.reset()
            self._last_pushed_seq = 0
            self.mode = "log_tail"
            self.status = "running"
            self.log_path = str(p)
            self.using_log_tail = True
            self.source_label = f"日志文件追踪: {p.name}"
            self.last_error = None

            t_log = threading.Thread(
                target=self._tail_log_loop,
                args=(p, True, True),
                name="SrbPureLogTailer",
                daemon=True,
            )
            self._worker_threads = [t_log]
            t_log.start()

        self._notify()
        return self.get_status()

    def _read_process_stdout(self, proc: subprocess.Popen) -> None:
        if not proc.stdout:
            return
        saw_any = False
        try:
            while not self._stop_event.is_set():
                raw = proc.stdout.readline()
                if not raw:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                line_text = self._decode_bytes(raw)
                with self._lock:
                    # Once managed --log-file starts producing lines, suppress duplicate pipe lines
                    # unless it's a batch message outside SRBMiner
                    if not (self.using_log_tail and self._parser.seq > 2 and Path(self.log_path).exists() and Path(self.log_path).stat().st_size > 0):
                        if self._parser.feed_line(line_text) is not None:
                            saw_any = True
                if saw_any:
                    saw_any = False
                    self._notify()
        except Exception:
            pass
        finally:
            code = proc.poll()
            with self._lock:
                if not self._stop_event.is_set() and self.mode == "bat_managed":
                    self.status = "stopped"
                    self._parser.feed_line(f"[{_now_str()}] [INFO] .bat 进程已结束 (退出码: {code})")
            self._notify()

    def _tail_log_loop(self, log_path: Path, start_from_tail_window: bool, read_existing: bool) -> None:
        offset = 0
        initialized = False
        while not self._stop_event.is_set():
            if not log_path.exists():
                time.sleep(0.4)
                continue
            try:
                size = log_path.stat().st_size
                if not initialized:
                    initialized = True
                    if read_existing and size > 0:
                        if start_from_tail_window and size > 65536:
                            offset = size - 65536
                        else:
                            offset = 0
                    elif not read_existing and not start_from_tail_window:
                        offset = 0
                    else:
                        offset = size
                if size < offset:
                    offset = 0

                if size > offset:
                    with open(log_path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                        # If we started mid-file from tail window, discard partial first line
                        if start_from_tail_window and offset > 0 and offset == size - len(chunk):
                            nl = chunk.find(b"\n")
                            if nl != -1:
                                chunk = chunk[nl + 1 :]
                        offset = f.tell()

                    decoded = self._decode_bytes(chunk)
                    added = 0
                    with self._lock:
                        for raw_line in decoded.splitlines():
                            if self._parser.feed_line(raw_line) is not None:
                                added += 1
                    if added > 0:
                        self._notify()
            except Exception as exc:
                with self._lock:
                    self.last_error = f"读取日志文件失败: {exc}"
            self._stop_event.wait(0.6)

    def _watch_console_pid_loop(self, pid: int) -> None:
        prev_lines: list[str] = []
        consecutive_errs = 0
        while not self._stop_event.is_set():
            # Verify target PID still alive
            lines, err = _snapshot_console_lines(pid, max_rows=160)
            if err:
                consecutive_errs += 1
                with self._lock:
                    self.last_error = err
                if consecutive_errs == 1:
                    with self._lock:
                        self._parser.feed_line(f"[{_now_str()}] [WARN] {err}")
                    self._notify()
                # Check if process exited
                if not self._is_pid_running(pid):
                    with self._lock:
                        self.status = "stopped"
                        self._parser.feed_line(f"[{_now_str()}] [INFO] 目标进程 PID {pid} 已退出。")
                    self._notify()
                    break
            else:
                consecutive_errs = 0
                new_lines = diff_console_lines(prev_lines, lines)
                prev_lines = lines
                if new_lines:
                    added = 0
                    with self._lock:
                        self.last_error = None
                        for l in new_lines:
                            if self._parser.feed_line(l) is not None:
                                added += 1
                    if added > 0:
                        self._notify()
            self._stop_event.wait(1.3)

    def _poll_srb_local_api_loop(self, port: int) -> None:
        url = f"http://127.0.0.1:{port}"
        while not self._stop_event.is_set():
            try:
                r = requests.get(url, timeout=1.5)
                if r.ok:
                    data = r.json()
                    with self._lock:
                        self._api_extra = {
                            "api_active": True,
                            "api_port": port,
                            "rig_name": data.get("rig_name") or "",
                            "uptime": data.get("uptime"),
                            "devices": data.get("devices") or [],
                            "algorithms": data.get("algorithms") or [],
                        }
            except Exception:
                pass
            self._stop_event.wait(3.0)

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        if os.name != "nt":
            return False
        k32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            res = k32.WaitForSingleObject(h, 0)
            WAIT_TIMEOUT = 0x00000102
            return res == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)

    @staticmethod
    def _decode_bytes(raw: bytes) -> str:
        return decode_text_bytes(raw)

    def _push_loop(self) -> None:
        while not self._push_stop.is_set():
            settings = self._get_settings()
            push_enabled = settings.get("telemetry_push_enabled", "0") == "1"
            server_url = (settings.get("collector_server_url") or "").strip().rstrip("/")
            if push_enabled and server_url and self.mode != "none":
                self.push_now(settings=settings)
            self._push_stop.wait(3.0)

    def push_now(self, settings: dict[str, str] | None = None) -> dict[str, Any]:
        cfg = settings or self._get_settings()
        server_url = (cfg.get("collector_server_url") or "").strip().rstrip("/")
        if not server_url:
            raise ValueError("请先配置后端收集服务器地址")
        token = (cfg.get("collector_token") or "").strip()
        custom_name = (cfg.get("telemetry_client_name") or "").strip()

        with self._lock:
            report = self._parser.get_report(since_seq=self._last_pushed_seq)
            latest_seq = report["latest_seq"]
            mode = self.mode
            status = self.status
            client_name = (
                custom_name
                or report.get("worker_name")
                or socket.gethostname()
            )
            payload = {
                "client_id": f"{socket.gethostname().lower()}:{client_name}",
                "client_name": client_name,
                "worker_name": report.get("worker_name") or client_name,
                "wallet_address": (cfg.get("telemetry_wallet_address") or "").strip(),
                "host_name": socket.gethostname(),
                "os_info": f"Windows ({os.name})",
                "source_mode": mode,
                "miner_status": status,
                "algorithm": report["algorithm"],
                "miner_version": report["miner_version"],
                "pool_url": report["pool_url"],
                "uptime_sec": report["uptime_sec"],
                "total_hashrate_ths": report["current_hashrate_ths"],
                "session_avg_ths": report["session_avg_ths"],
                "avg_5m_ths": report["avg_5m_ths"],
                "avg_15m_ths": report["avg_15m_ths"],
                "min_hashrate_ths": report["min_hashrate_ths"],
                "max_hashrate_ths": report["max_hashrate_ths"],
                "stability_pct": report["stability_pct"],
                "hashrate_raw_text": report["current_hashrate_text"],
                "accepted_shares": report["accepted_shares"],
                "rejected_shares": report["rejected_shares"],
                "stale_shares": report["stale_shares"],
                "accept_rate_pct": report["accept_rate_pct"],
                "shares_per_hour": report["shares_per_hour"],
                "avg_latency_ms": report["avg_latency_ms"],
                "total_power_w": report["total_power_w"],
                "efficiency_ths_per_kw": report["efficiency_ths_per_kw"],
                "devices": report["devices"],
                "samples": report["samples"][-60:],
                "log_lines": report["lines"][-120:],
            }

        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Collector-Token"] = token

        try:
            resp = requests.post(
                f"{server_url}/api/v1/ingest",
                json=payload,
                headers=headers,
                timeout=4,
            )
            resp.raise_for_status()
            with self._lock:
                self._last_pushed_seq = max(self._last_pushed_seq, latest_seq)
                self.last_push_at = _now_str()
                self.last_push_error = None
                self.push_count += 1
            return {"ok": True, "pushed_at": self.last_push_at}
        except Exception as exc:
            err = f"上报失败: {exc}"
            with self._lock:
                self.last_push_error = err
            return {"ok": False, "error": err}

    def get_status(self, since_seq: int = 0) -> dict[str, Any]:
        with self._lock:
            report = self._parser.get_report(since_seq=since_seq)
            return {
                "mode": self.mode,
                "status": self.status,
                "source_label": self.source_label,
                "bat_path": self.bat_path,
                "log_path": self.log_path,
                "attached_pid": self.attached_pid,
                "api_port": self.api_port,
                "last_error": self.last_error,
                "api_extra": dict(self._api_extra),
                "push": {
                    "last_push_at": self.last_push_at,
                    "last_push_error": self.last_push_error,
                    "push_count": self.push_count,
                },
                "report": report,
            }
