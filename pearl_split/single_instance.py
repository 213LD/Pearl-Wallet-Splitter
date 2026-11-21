"""Windows single-instance lock and background-window wake helper (stdlib only)."""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

_IS_WINDOWS = sys.platform == "win32"

_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_ASFW_ANY = 0xFFFFFFFF
_SW_SHOW = 5
_SW_RESTORE = 9
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    _kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateEventW.restype = wintypes.HANDLE

    _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenEventW.restype = wintypes.HANDLE

    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.SetEvent.restype = wintypes.BOOL

    _kernel32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.WaitForMultipleObjects.restype = wintypes.DWORD

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _user32.AllowSetForegroundWindow.restype = wintypes.BOOL

    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL

    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL

    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL

    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL


def build_instance_key(data_dir: str | Path) -> str:
    """Build a canonical per-installation lock key from the resolved data directory."""
    resolved = Path(data_dir).resolve()
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class SingleInstanceGuard:
    """Enforces one desktop process per data_dir and wakes an existing instance on relaunch."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.key = build_instance_key(self.data_dir)
        self.mutex_name = f"Local\\PearlWalletSplitter_Mutex_{self.key}"
        self.wake_event_name = f"Local\\PearlWalletSplitter_Wake_{self.key}"
        self._mutex_handle: Any = None
        self._wake_event_handle: Any = None
        self._stop_event_handle: Any = None
        self._listener_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def acquire_or_activate(self, retry_timeout: float = 1.5) -> bool:
        """Acquire primary lock or signal the running instance to show its window.

        Returns True when this process owns the lock; False when another instance
        already owns it and has been signaled.
        """
        if not _IS_WINDOWS:
            return True

        with self._lock:
            if self._mutex_handle:
                return True

            ctypes.set_last_error(0)
            mutex_h = _kernel32.CreateMutexW(None, True, self.mutex_name)
            err = ctypes.get_last_error()
            if not mutex_h:
                return True

            if err == _ERROR_ALREADY_EXISTS:
                _kernel32.CloseHandle(mutex_h)
                _user32.AllowSetForegroundWindow(_ASFW_ANY)
                deadline = time.monotonic() + max(0.0, float(retry_timeout))
                while True:
                    ev = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, self.wake_event_name)
                    if ev:
                        try:
                            _kernel32.SetEvent(ev)
                        finally:
                            _kernel32.CloseHandle(ev)
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                return False

            self._mutex_handle = mutex_h
            self._wake_event_handle = _kernel32.CreateEventW(None, False, False, self.wake_event_name)
            self._stop_event_handle = _kernel32.CreateEventW(None, True, False, None)
            return True

    def start_wake_listener(self, on_wake: Callable[[], None]) -> None:
        """Start daemon thread listening for secondary-instance activation signals."""
        if not _IS_WINDOWS:
            return

        with self._lock:
            if self._listener_thread is not None and self._listener_thread.is_alive():
                return
            wake_h = self._wake_event_handle
            stop_h = self._stop_event_handle
            if not wake_h or not stop_h:
                return

            def _wait_loop() -> None:
                handles = (wintypes.HANDLE * 2)(wake_h, stop_h)
                while True:
                    res = _kernel32.WaitForMultipleObjects(2, handles, False, _INFINITE)
                    if res == _WAIT_OBJECT_0:
                        try:
                            on_wake()
                        except Exception:
                            pass
                        continue
                    break

            th = threading.Thread(target=_wait_loop, name="single-instance-wake", daemon=True)
            self._listener_thread = th
            th.start()

    def release(self) -> None:
        """Stop the wake listener and close Win32 synchronization handles (idempotent)."""
        if not _IS_WINDOWS:
            return

        with self._lock:
            stop_h = self._stop_event_handle
            th = self._listener_thread
            if stop_h:
                try:
                    _kernel32.SetEvent(stop_h)
                except Exception:
                    pass

        if th is not None and th.is_alive():
            th.join(timeout=1.0)

        with self._lock:
            self._listener_thread = None
            for attr in ("_wake_event_handle", "_stop_event_handle", "_mutex_handle"):
                h = getattr(self, attr)
                if h:
                    try:
                        _kernel32.CloseHandle(h)
                    except Exception:
                        pass
                    setattr(self, attr, None)


def _extract_hwnd(window: Any) -> int:
    native = getattr(window, "native", None)
    handle_obj = getattr(native, "Handle", None)
    if handle_obj is None:
        return 0
    for meth in ("ToInt64", "ToInt32"):
        fn = getattr(handle_obj, meth, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    try:
        return int(handle_obj)
    except Exception:
        return 0


def bring_window_to_front(window: Any) -> None:
    """Restore a hidden or minimized pywebview window and raise it to the foreground."""
    for step in ("show", "restore", "show"):
        fn = getattr(window, step, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    if not _IS_WINDOWS:
        return

    hwnd = _extract_hwnd(window)
    if not hwnd:
        return

    hwnd_val = ctypes.c_void_p(hwnd)
    try:
        cmd = _SW_RESTORE if _user32.IsIconic(hwnd_val) else _SW_SHOW
        _user32.ShowWindow(hwnd_val, cmd)
        flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW
        _user32.SetWindowPos(hwnd_val, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)
        _user32.SetWindowPos(hwnd_val, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)
        _user32.BringWindowToTop(hwnd_val)
        _user32.SetForegroundWindow(hwnd_val)
    except Exception:
        pass
