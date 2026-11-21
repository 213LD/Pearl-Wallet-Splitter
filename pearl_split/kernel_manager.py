"""SRBMiner-MULTI kernel directory binding, GitHub updater, selective extractor, and .bat generator."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .miner_binder import CREATE_NO_WINDOW, scan_running_srbminer_processes
from .srb_parser import decode_text_bytes, parse_srb_cmdline, read_text_tolerant

DEFAULT_GITHUB_REPO = "doktor83/SRBMiner-Multi"
DEFAULT_KERNEL_EXE_NAME = "SRBMiner-MULTI.exe"
KERNEL_META_FILENAME = ".pearl_kernel_meta.json"

_VERSION_NUMS_RE = re.compile(r"(\d+)(?:[._\-]+(\d+))?(?:[._\-]+(\d+))?(?:[._\-]+(\d+))?")
_REPO_CLEAN_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_github_repo(raw_repo: str) -> str:
    text = (raw_repo or "").strip().strip("/")
    if not text:
        return DEFAULT_GITHUB_REPO
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip("/")
            break
    if text.endswith(".git"):
        text = text[:-4]
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2:
        candidate = f"{parts[0]}/{parts[1]}"
        if _REPO_CLEAN_RE.match(candidate):
            return candidate
    raise ValueError("GitHub 仓库格式应为 owner/repo（例如 doktor83/SRBMiner-Multi）")


def parse_version_tuple(raw: str) -> tuple[int, ...]:
    """Extract comparable integer tuple from strings like 'v3.8.8' or 'SRBMiner-Multi-3-8-8'."""
    text = (raw or "").strip()
    if not text:
        return ()
    m = _VERSION_NUMS_RE.search(text)
    if not m:
        return ()
    nums = [int(g) for g in m.groups() if g is not None]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def format_version_label(raw: str) -> str:
    parts = parse_version_tuple(raw)
    if not parts:
        return (raw or "").strip()
    if len(parts) == 1:
        return f"{parts[0]}.0"
    return ".".join(str(x) for x in parts)


def compare_versions(local_ver: str, remote_ver: str) -> int | None:
    """Return -1 if remote > local, 0 if equal, 1 if local > remote, None if uncomparable."""
    lt = parse_version_tuple(local_ver)
    rt = parse_version_tuple(remote_ver)
    if not lt or not rt:
        return None
    n = max(len(lt), len(rt))
    lt_pad = lt + (0,) * (n - len(lt))
    rt_pad = rt + (0,) * (n - len(rt))
    if lt_pad < rt_pad:
        return -1
    if lt_pad > rt_pad:
        return 1
    return 0


def read_pe_file_version(exe_path: Path) -> str:
    """Safely read Windows PE FileVersion resource without launching the executable."""
    if os.name != "nt" or not exe_path.is_file():
        return ""
    try:
        version_dll = ctypes.windll.version
        dummy = wintypes.DWORD(0)
        size = version_dll.GetFileVersionInfoSizeW(str(exe_path), ctypes.byref(dummy))
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(exe_path), 0, size, buf):
            return ""

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        ptr = ctypes.c_void_p()
        length = wintypes.UINT(0)
        if not version_dll.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        if not ptr.value or length.value < ctypes.sizeof(VS_FIXEDFILEINFO):
            return ""
        info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            return ""
        major = (info.dwFileVersionMS >> 16) & 0xFFFF
        minor = info.dwFileVersionMS & 0xFFFF
        patch = (info.dwFileVersionLS >> 16) & 0xFFFF
        build = info.dwFileVersionLS & 0xFFFF
        if (major, minor, patch, build) == (0, 0, 0, 0):
            return ""
        if build == 0:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch}.{build}"
    except Exception:
        return ""


def find_kernel_exe(kernel_dir: Path) -> Path | None:
    """Find SRBMiner-MULTI.exe case-insensitively inside kernel_dir."""
    if not kernel_dir.is_dir():
        return None
    for preferred in ("SRBMiner-MULTI.exe", "SRBMiner-Multi.exe"):
        candidate = kernel_dir / preferred
        if candidate.is_file():
            return candidate
    try:
        for child in kernel_dir.iterdir():
            if child.is_file() and child.name.lower() == "srbminer-multi.exe":
                return child
    except OSError:
        return None
    return None


def _norm_path_key(p: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def inspect_kernel_dir(kernel_dir: str | Path, linked_bat_path: str = "") -> dict[str, Any]:
    """Inspect bound miner kernel directory: exe presence, version, .bat scripts, and running locks."""
    raw = str(kernel_dir or "").strip()
    if not raw:
        return {
            "kernel_dir": "",
            "exists": False,
            "is_empty": True,
            "exe_exists": False,
            "exe_path": "",
            "exe_name": DEFAULT_KERNEL_EXE_NAME,
            "exe_size": 0,
            "exe_modified_at": "",
            "exe_version": "",
            "version_source": "",
            "meta": {},
            "bat_files": [],
            "running_processes": [],
        }

    kdir = Path(raw)
    exists = kdir.is_dir()
    exe_path = find_kernel_exe(kdir) if exists else None

    meta: dict[str, Any] = {}
    meta_file = kdir / KERNEL_META_FILENAME
    if exists and meta_file.is_file():
        try:
            loaded = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}

    exe_size = 0
    exe_mtime = ""
    exe_version = ""
    version_source = ""
    if exe_path is not None:
        st = exe_path.stat()
        exe_size = int(st.st_size)
        exe_mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if meta.get("version") and int(meta.get("exe_size") or -1) == exe_size:
            exe_version = str(meta["version"])
            version_source = "meta"
        else:
            pe_ver = read_pe_file_version(exe_path)
            if pe_ver:
                exe_version = pe_ver
                version_source = "pe"
            elif meta.get("version"):
                exe_version = str(meta["version"])
                version_source = "meta_stale"
            else:
                folder_ver = format_version_label(kdir.name)
                if parse_version_tuple(kdir.name):
                    exe_version = folder_ver
                    version_source = "folder_name"

    bat_files: list[dict[str, Any]] = []
    linked_key = _norm_path_key(linked_bat_path) if linked_bat_path else ""
    is_empty = True
    if exists:
        try:
            entries = sorted(kdir.iterdir(), key=lambda c: (not c.is_file(), c.name.lower()))
            is_empty = len(entries) == 0
            for child in entries:
                if child.is_file() and child.suffix.lower() in (".bat", ".cmd"):
                    cst = child.stat()
                    bat_files.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "size": int(cst.st_size),
                            "modified_at": datetime.fromtimestamp(cst.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "is_linked": bool(linked_key and _norm_path_key(child) == linked_key),
                        }
                    )
        except OSError:
            pass

    running_in_dir: list[dict[str, Any]] = []
    if exists:
        dir_key = _norm_path_key(kdir)
        exe_key = _norm_path_key(exe_path) if exe_path else ""
        for proc in scan_running_srbminer_processes():
            if not proc.get("is_primary_miner"):
                continue
            p_exe = _norm_path_key(proc.get("executable_path") or "") if proc.get("executable_path") else ""
            p_dir = _norm_path_key(proc.get("work_dir") or "") if proc.get("work_dir") else ""
            if (exe_key and p_exe == exe_key) or (dir_key and p_dir == dir_key):
                running_in_dir.append(proc)

    return {
        "kernel_dir": str(kdir),
        "exists": exists,
        "is_empty": is_empty,
        "exe_exists": exe_path is not None,
        "exe_path": str(exe_path) if exe_path else "",
        "exe_name": exe_path.name if exe_path else DEFAULT_KERNEL_EXE_NAME,
        "exe_size": exe_size,
        "exe_modified_at": exe_mtime,
        "exe_version": exe_version,
        "version_source": version_source,
        "meta": meta,
        "bat_files": bat_files,
        "running_processes": running_in_dir,
    }


def _quote_cmd_arg(val: str) -> str:
    txt = str(val or "").strip()
    if not txt:
        return '""'
    if any(ch in txt for ch in (" ", "\t", "&", "|", "(", ")")) and not (txt.startswith('"') and txt.endswith('"')):
        return f'"{txt}"'
    return txt


def _quote_join_tokens(tokens: list[str]) -> str:
    return " ".join(_quote_cmd_arg(t) for t in tokens if str(t or "").strip())


def extract_bat_config(bat_path: str | Path) -> dict[str, Any]:
    """Parse an existing SRBMiner .bat file into editable structured fields while preserving extra args."""
    p = Path(bat_path)
    if not p.is_file():
        raise FileNotFoundError(f"未找到 .bat 脚本文件: {p}")

    raw_content = read_text_tolerant(p)
    raw_lines = raw_content.splitlines()

    # Join Windows cmd caret (^) line continuations logically
    logical_lines: list[str] = []
    buf = ""
    for line in raw_lines:
        stripped_r = line.rstrip()
        if stripped_r.endswith("^"):
            buf += stripped_r[:-1] + " "
        else:
            buf += line
            logical_lines.append(buf)
            buf = ""
    if buf:
        logical_lines.append(buf)

    srb_line = ""
    srb_idx = -1
    has_pause = False
    has_loop = False
    for idx, line in enumerate(logical_lines):
        s = line.strip()
        slow = s.lower()
        if not s or slow.startswith("rem ") or slow.startswith("::"):
            continue
        if "srbminer" in slow and srb_idx < 0:
            srb_line = s
            srb_idx = idx
        if re.match(r"^pause\b", slow):
            has_pause = True
        if re.match(r"^goto\s+:?loop\b", slow) or slow == ":loop":
            has_loop = True

    preamble_lines: list[str] = []
    stop_idx = srb_idx if srb_idx >= 0 else len(logical_lines)
    for line in logical_lines[:stop_idx]:
        s = line.strip()
        slow = s.lower()
        if not s:
            continue
        if (
            slow == "@echo off"
            or slow.startswith("chcp ")
            or slow.startswith("cd /d")
            or slow == ":loop"
            or slow == "setlocal"
        ):
            continue
        preamble_lines.append(s)

    base = parse_srb_cmdline(srb_line)
    executable = base.get("executable") or "%~dp0SRBMiner-MULTI.exe"
    algorithm = base.get("algorithm") or ""
    pool = base.get("pool") or ""
    wallet = base.get("wallet") or ""
    worker = base.get("worker") or ""
    password = ""
    log_file = base.get("log_file") or ""
    disable_cpu = False
    api_enable = bool(base.get("api_enable"))
    api_port = str(base.get("api_port") or 21550)
    extra_tokens: list[str] = []

    if srb_line:
        cmd_clean = re.sub(r'^\s*start\s+(?:(?:"[^"]*"|/\w+)\s+)*', "", srb_line, flags=re.IGNORECASE)
        try:
            tokens = [t.strip('"').strip("'") for t in shlex.split(cmd_clean, posix=False)]
        except ValueError:
            tokens = [t.strip('"').strip("'") for t in cmd_clean.split()]

        exe_consumed = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            ltok = tok.lower()
            if not exe_consumed and "srbminer" in ltok:
                executable = tok
                exe_consumed = True
                i += 1
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if ltok in ("--algorithm", "-a") and nxt:
                algorithm = nxt
                i += 2
            elif ltok in ("--pool", "-p") and nxt:
                pool = nxt
                i += 2
            elif ltok in ("--wallet", "-w") and nxt:
                wallet = nxt
                i += 2
            elif ltok in ("--worker", "--rig-name") and nxt:
                worker = nxt
                i += 2
            elif ltok == "--password" and nxt:
                password = nxt
                i += 2
            elif ltok == "--log-file" and nxt:
                log_file = nxt
                i += 2
            elif ltok == "--disable-cpu":
                disable_cpu = True
                i += 1
            elif ltok == "--api-enable":
                api_enable = True
                i += 1
            elif ltok == "--api-port" and nxt:
                api_port = nxt
                i += 2
            else:
                extra_tokens.append(tok)
                i += 1

    return {
        "bat_path": str(p),
        "bat_dir": str(p.parent),
        "bat_name": p.name,
        "executable": executable,
        "algorithm": algorithm,
        "pool": pool,
        "wallet": wallet,
        "worker": worker,
        "password": password,
        "disable_cpu": disable_cpu,
        "api_enable": api_enable,
        "api_port": api_port if api_enable else "21550",
        "log_file": log_file,
        "extra_args": _quote_join_tokens(extra_tokens),
        "preamble": "\n".join(preamble_lines),
        "has_loop": has_loop,
        "has_pause": has_pause,
        "raw_content": raw_content,
    }


def render_bat_content(cfg: dict[str, Any]) -> str:
    """Render a clean Windows SRBMiner-MULTI .bat launch script from structured fields."""
    lines = ["@echo off", "chcp 65001 >nul", 'cd /d "%~dp0"']

    preamble = str(cfg.get("preamble") or "").strip()
    if preamble:
        for pl in preamble.splitlines():
            s = pl.rstrip()
            if s.strip():
                lines.append(s)

    has_loop = bool(cfg.get("has_loop"))
    has_pause = bool(cfg.get("has_pause", not has_loop))
    if has_loop:
        lines.append(":loop")

    exe = str(cfg.get("executable") or "%~dp0SRBMiner-MULTI.exe").strip() or "%~dp0SRBMiner-MULTI.exe"
    cmd_parts = [_quote_cmd_arg(exe)]

    algo = str(cfg.get("algorithm") or "").strip()
    if algo:
        cmd_parts.extend(["--algorithm", _quote_cmd_arg(algo)])

    pool = str(cfg.get("pool") or "").strip()
    if pool:
        cmd_parts.extend(["--pool", _quote_cmd_arg(pool)])

    wallet = str(cfg.get("wallet") or "").strip()
    if wallet:
        cmd_parts.extend(["--wallet", _quote_cmd_arg(wallet)])

    worker = str(cfg.get("worker") or "").strip()
    if worker and not wallet.lower().endswith("." + worker.lower()):
        cmd_parts.extend(["--worker", _quote_cmd_arg(worker)])

    password = str(cfg.get("password") or "").strip()
    if password:
        cmd_parts.extend(["--password", _quote_cmd_arg(password)])

    if bool(cfg.get("disable_cpu")):
        cmd_parts.append("--disable-cpu")

    if bool(cfg.get("api_enable")):
        cmd_parts.append("--api-enable")
        port_txt = str(cfg.get("api_port") or "21550").strip()
        if port_txt:
            cmd_parts.extend(["--api-port", port_txt])

    log_file = str(cfg.get("log_file") or "").strip()
    if log_file:
        cmd_parts.extend(["--log-file", _quote_cmd_arg(log_file)])

    extra_args = str(cfg.get("extra_args") or "").strip()
    if extra_args:
        cmd_parts.append(extra_args)

    lines.append(" ".join(cmd_parts))

    if has_loop:
        lines.append("echo [Pearl] SRBMiner 已退出，5 秒后自动重启...")
        lines.append("timeout /t 5 /nobreak >nul")
        lines.append("goto loop")
    elif has_pause:
        lines.append("pause")

    return "\r\n".join(lines) + "\r\n"


def save_bat_file(
    target_path: str | Path,
    cfg: dict[str, Any] | None = None,
    raw_content: str | None = None,
    backup_existing: bool = True,
) -> dict[str, Any]:
    """Generate or overwrite a .bat launch script with optional .bak safety copy."""
    raw_p = str(target_path or "").strip()
    if not raw_p:
        raise ValueError("请先指定 .bat 脚本路径或文件名")

    p = Path(raw_p)
    if p.suffix.lower() not in (".bat", ".cmd"):
        p = p.with_suffix(".bat")
    p.parent.mkdir(parents=True, exist_ok=True)

    if raw_content is not None:
        text = raw_content.replace("\r\n", "\n").replace("\n", "\r\n")
        if not text.endswith("\r\n"):
            text += "\r\n"
    else:
        text = render_bat_content(cfg or {})

    backup_path = ""
    if p.exists() and backup_existing:
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)
        backup_path = str(bak)

    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    parsed = extract_bat_config(p)
    return {
        "saved_path": str(p),
        "backup_path": backup_path,
        "config": parsed,
    }


def _apply_github_proxy(url: str, proxy_prefix: str) -> str:
    clean_proxy = (proxy_prefix or "").strip()
    if not clean_proxy:
        return url
    if "{url}" in clean_proxy:
        return clean_proxy.replace("{url}", url)
    return clean_proxy.rstrip("/") + "/" + url.lstrip("/")


def _select_windows_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not assets:
        return None

    def _score(asset: dict[str, Any]) -> tuple[int, int]:
        name = str(asset.get("name") or "").lower()
        if not name or "sha256" in name or name.endswith((".asc", ".sig", ".txt")):
            return (-1, 0)
        is_linux = "linux" in name or "tar.gz" in name or "tar.xz" in name
        if name.endswith(".zip") and "win64" in name:
            return (100, int(asset.get("size") or 0))
        if name.endswith(".zip") and "win" in name and not is_linux:
            return (80, int(asset.get("size") or 0))
        if name.endswith(".zip") and not is_linux:
            return (60, int(asset.get("size") or 0))
        if name.endswith(".7z") and "win" in name:
            return (40, int(asset.get("size") or 0))
        if not is_linux and (name.endswith(".zip") or name.endswith(".7z")):
            return (20, int(asset.get("size") or 0))
        return (-1, 0)

    ranked = sorted(assets, key=_score, reverse=True)
    best = ranked[0]
    if _score(best)[0] < 0:
        return None
    return {
        "name": str(best.get("name") or ""),
        "size": int(best.get("size") or 0),
        "download_count": int(best.get("download_count") or 0),
        "download_url": str(best.get("browser_download_url") or ""),
        "digest": str(best.get("digest") or ""),
    }


def _fetch_releases_atom_fallback(owner_repo: str, per_page: int, timeout: float = 12.0) -> list[dict[str, Any]]:
    """Fallback release discovery via GitHub Atom feed when REST API hits anonymous rate limits."""
    atom_url = f"https://github.com/{owner_repo}/releases.atom"
    resp = requests.get(atom_url, headers={"User-Agent": "PearlSplit-KernelUpdater"}, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    releases: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns)[:per_page]:
        link_el = entry.find("atom:link", ns)
        href = link_el.attrib.get("href", "") if link_el is not None else ""
        tag = href.rstrip("/").rsplit("/", 1)[-1] if "/tag/" in href else ""
        title = (entry.findtext("atom:title", "", ns) or tag).strip()
        updated = (entry.findtext("atom:updated", "", ns) or "").strip()
        content_el = entry.find("atom:content", ns)
        raw_body = (content_el.text or "") if content_el is not None else ""
        clean_body = re.sub(r"<[^>]+>", "\n", raw_body)
        clean_body = re.sub(r"\n{3,}", "\n\n", clean_body).strip()
        ver_str = format_version_label(tag or title)
        dashed = ver_str.replace(".", "-")
        guess_name = f"SRBMiner-Multi-{dashed}-win64.zip" if dashed else ""
        guess_url = (
            f"https://github.com/{owner_repo}/releases/download/{tag}/{guess_name}"
            if (tag and guess_name)
            else ""
        )
        releases.append(
            {
                "tag_name": tag or title,
                "version": ver_str,
                "name": title,
                "published_at": updated.replace("T", " ").replace("Z", "")[:19],
                "html_url": href or f"https://github.com/{owner_repo}/releases",
                "body": clean_body or "(由 releases.atom 读取，完整更新日志请点击页面链接)",
                "prerelease": False,
                "asset": {
                    "name": guess_name,
                    "size": 0,
                    "download_count": 0,
                    "download_url": guess_url,
                    "digest": "",
                }
                if guess_url
                else None,
            }
        )
    return releases


def fetch_github_releases(
    repo: str = DEFAULT_GITHUB_REPO,
    github_proxy: str = "",
    per_page: int = 8,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Query GitHub releases for SRBMiner-MULTI and normalize changelogs + Windows assets."""
    owner_repo = normalize_github_repo(repo)
    api_url = f"https://api.github.com/repos/{owner_repo}/releases?per_page={max(1, min(20, int(per_page)))}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PearlSplit-KernelUpdater",
    }

    source_note = "github_api"
    raw_list: list[dict[str, Any]] = []
    api_err: Exception | None = None

    for candidate_url in (api_url, _apply_github_proxy(api_url, github_proxy)) if github_proxy else (api_url,):
        try:
            resp = requests.get(candidate_url, headers=headers, timeout=timeout)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                raise RuntimeError("GitHub API 匿名请求次数受限")
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                raw_list = [x for x in data if isinstance(x, dict) and not x.get("draft")]
                break
        except Exception as exc:
            api_err = exc

    releases: list[dict[str, Any]] = []
    if raw_list:
        for item in raw_list:
            tag = str(item.get("tag_name") or item.get("name") or "").strip()
            pub = str(item.get("published_at") or "").replace("T", " ").replace("Z", "")[:19]
            asset = _select_windows_asset(list(item.get("assets") or []))
            releases.append(
                {
                    "tag_name": tag,
                    "version": format_version_label(tag or str(item.get("name") or "")),
                    "name": str(item.get("name") or tag).strip(),
                    "published_at": pub,
                    "html_url": str(item.get("html_url") or f"https://github.com/{owner_repo}/releases"),
                    "body": str(item.get("body") or "").strip() or "（该版本未填写更新说明）",
                    "prerelease": bool(item.get("prerelease")),
                    "asset": asset,
                }
            )
    else:
        try:
            releases = _fetch_releases_atom_fallback(owner_repo, per_page=per_page, timeout=timeout)
            source_note = "github_atom_fallback"
        except Exception as atom_exc:
            raise RuntimeError(f"拉取 GitHub 更新失败: {api_err or atom_exc}") from (api_err or atom_exc)

    if not releases:
        raise RuntimeError(f"仓库 {owner_repo} 暂无可用的 Release 版本")

    return {
        "repo": owner_repo,
        "fetched_at": _now_str(),
        "source": source_note,
        "latest": releases[0],
        "releases": releases,
    }


def _safe_extract_path(root: Path, rel_path: str) -> Path:
    clean_parts = [p for p in Path(rel_path).parts if p not in ("", ".", "/")]
    if not clean_parts or any(p == ".." for p in clean_parts):
        raise ValueError(f"压缩包内存在非法路径: {rel_path}")
    target = (root / Path(*clean_parts)).resolve()
    root_res = root.resolve()
    try:
        target.relative_to(root_res)
    except ValueError as exc:
        raise ValueError(f"压缩包路径越界风险: {rel_path}") from exc
    return target


def _decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    """Fix CP437 mojibake on Chinese ZIP filenames when the UTF-8 flag bit (0x800) is unset."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gb18030", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def _extract_archive_file(
    archive_path: Path,
    target_dir: Path,
    mode: str,
    backup_exe: bool = True,
) -> dict[str, Any]:
    """Extract either only SRBMiner-MULTI.exe or full runtime tree from archive_path cleanly."""
    if mode not in ("exe_only", "full_deploy"):
        raise ValueError(f"不支持的部署模式: {mode}")

    target_dir.mkdir(parents=True, exist_ok=True)
    existing_exe = find_kernel_exe(target_dir)
    dest_exe_name = existing_exe.name if existing_exe else DEFAULT_KERNEL_EXE_NAME
    dest_exe_path = target_dir / dest_exe_name

    name_lower = archive_path.name.lower()
    extracted_files: list[str] = []
    skipped_files: list[str] = []
    exe_found = False

    def _backup_existing_binary() -> str:
        if existing_exe and existing_exe.is_file() and backup_exe:
            bak = existing_exe.with_suffix(existing_exe.suffix + ".bak")
            shutil.copy2(existing_exe, bak)
            return str(bak)
        return ""

    backup_path = ""

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            infos = [(zi, _decode_zip_member_name(zi)) for zi in zf.infolist() if not zi.is_dir()]
            if mode == "exe_only":
                matched_pair = next((pair for pair in infos if Path(pair[1]).name.lower() == "srbminer-multi.exe"), None)
                if matched_pair is None:
                    raise RuntimeError("压缩包中未找到 SRBMiner-MULTI.exe")
                matched, _ = matched_pair
                exe_found = True
                staged = target_dir / f".pearl_stage_{dest_exe_name}.tmp"
                try:
                    with zf.open(matched, "r") as src, open(staged, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=262144)
                    backup_path = _backup_existing_binary()
                    os.replace(staged, dest_exe_path)
                    extracted_files.append(str(dest_exe_path))
                finally:
                    if staged.exists():
                        staged.unlink(missing_ok=True)
            else:
                # Strip single common root folder if present (e.g., SRBMiner-Multi-3-8-8/...)
                top_segments = {
                    Path(fname).parts[0]
                    for _zi, fname in infos
                    if len(Path(fname).parts) > 1
                }
                has_root_files = any(len(Path(fname).parts) == 1 for _zi, fname in infos)
                strip_prefix = next(iter(top_segments)) if (len(top_segments) == 1 and not has_root_files) else ""

                for zi, fname in infos:
                    parts = list(Path(fname).parts)
                    if strip_prefix and parts and parts[0] == strip_prefix:
                        parts = parts[1:]
                    if not parts:
                        continue
                    rel = "/".join(parts)
                    dest = _safe_extract_path(target_dir, rel)
                    # Preserve user's existing custom .bat / .cmd / .log files if deploying over non-empty folder
                    if dest.exists() and dest.suffix.lower() in (".bat", ".cmd", ".log"):
                        skipped_files.append(str(dest))
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.name.lower() == "srbminer-multi.exe":
                        exe_found = True
                        backup_path = _backup_existing_binary()
                        staged = dest.parent / f".pearl_stage_{dest.name}.tmp"
                        try:
                            with zf.open(zi, "r") as src, open(staged, "wb") as dst:
                                shutil.copyfileobj(src, dst, length=262144)
                            os.replace(staged, dest)
                        finally:
                            if staged.exists():
                                staged.unlink(missing_ok=True)
                    else:
                        with zf.open(zi, "r") as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst, length=262144)
                    extracted_files.append(str(dest))

    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            if mode == "exe_only":
                matched = next((m for m in members if Path(m.name).name.lower() == "srbminer-multi.exe"), None)
                if matched is None:
                    raise RuntimeError("压缩包中未找到 SRBMiner-MULTI.exe")
                exe_found = True
                staged = target_dir / f".pearl_stage_{dest_exe_name}.tmp"
                try:
                    src = tf.extractfile(matched)
                    if src is None:
                        raise RuntimeError("读取压缩包中的 SRBMiner-MULTI.exe 失败")
                    with src, open(staged, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=262144)
                    backup_path = _backup_existing_binary()
                    os.replace(staged, dest_exe_path)
                    extracted_files.append(str(dest_exe_path))
                finally:
                    if staged.exists():
                        staged.unlink(missing_ok=True)
            else:
                top_segments = {
                    Path(m.name).parts[0]
                    for m in members
                    if len(Path(m.name).parts) > 1
                }
                has_root_files = any(len(Path(m.name).parts) == 1 for m in members)
                strip_prefix = next(iter(top_segments)) if (len(top_segments) == 1 and not has_root_files) else ""
                for m in members:
                    parts = list(Path(m.name).parts)
                    if strip_prefix and parts and parts[0] == strip_prefix:
                        parts = parts[1:]
                    if not parts:
                        continue
                    rel = "/".join(parts)
                    dest = _safe_extract_path(target_dir, rel)
                    if dest.exists() and dest.suffix.lower() in (".bat", ".cmd", ".log"):
                        skipped_files.append(str(dest))
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    if dest.name.lower() == "srbminer-multi.exe":
                        exe_found = True
                        backup_path = _backup_existing_binary()
                    with src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=262144)
                    extracted_files.append(str(dest))

    elif name_lower.endswith(".7z"):
        # Fallback for .7z archives using Windows 11 bsdtar (`tar.exe`) or 7-Zip if installed
        with tempfile.TemporaryDirectory(prefix="pearl_7z_") as tmp_extract:
            cmd = None
            tar_exe = shutil.which("tar")
            seven_z = shutil.which("7z") or (
                r"C:\Program Files\7-Zip\7z.exe" if Path(r"C:\Program Files\7-Zip\7z.exe").is_file() else ""
            )
            if seven_z:
                cmd = [seven_z, "x", "-y", f"-o{tmp_extract}", str(archive_path)]
            elif tar_exe:
                cmd = [tar_exe, "-xf", str(archive_path), "-C", tmp_extract]
            if not cmd:
                raise RuntimeError("当前环境不支持解压 .7z 格式，请选择 .zip 发布包")
            res = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=120)
            if res.returncode != 0:
                err_txt = decode_text_bytes(res.stderr or res.stdout or b"")
                raise RuntimeError(f"解压 .7z 失败: {err_txt}")
            all_files = [p for p in Path(tmp_extract).rglob("*") if p.is_file()]
            if mode == "exe_only":
                matched = next((p for p in all_files if p.name.lower() == "srbminer-multi.exe"), None)
                if matched is None:
                    raise RuntimeError("压缩包中未找到 SRBMiner-MULTI.exe")
                exe_found = True
                backup_path = _backup_existing_binary()
                shutil.copy2(matched, dest_exe_path)
                extracted_files.append(str(dest_exe_path))
            else:
                base_root = Path(tmp_extract)
                roots = [c for c in base_root.iterdir()]
                copy_root = roots[0] if (len(roots) == 1 and roots[0].is_dir()) else base_root
                for src_f in copy_root.rglob("*"):
                    if not src_f.is_file():
                        continue
                    rel = str(src_f.relative_to(copy_root))
                    dest = _safe_extract_path(target_dir, rel)
                    if dest.exists() and dest.suffix.lower() in (".bat", ".cmd", ".log"):
                        skipped_files.append(str(dest))
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.name.lower() == "srbminer-multi.exe":
                        exe_found = True
                        backup_path = _backup_existing_binary()
                    shutil.copy2(src_f, dest)
                    extracted_files.append(str(dest))
    else:
        raise RuntimeError(f"不支持的压缩包格式: {archive_path.name}")

    return {
        "mode": mode,
        "exe_found": exe_found,
        "exe_path": str(dest_exe_path) if dest_exe_path.is_file() else "",
        "backup_path": backup_path,
        "extracted_count": len(extracted_files),
        "skipped_count": len(skipped_files),
    }


def _stop_processes_for_dir(target_dir: Path) -> list[int]:
    """Terminate running SRBMiner processes belonging to target_dir so the binary lock releases."""
    if os.name != "nt" or not target_dir.is_dir():
        return []
    dir_key = _norm_path_key(target_dir)
    exe = find_kernel_exe(target_dir)
    exe_key = _norm_path_key(exe) if exe else ""
    killed_pids: list[int] = []
    for proc in scan_running_srbminer_processes():
        if not proc.get("is_primary_miner"):
            continue
        p_exe = _norm_path_key(proc.get("executable_path") or "") if proc.get("executable_path") else ""
        p_dir = _norm_path_key(proc.get("work_dir") or "") if proc.get("work_dir") else ""
        if (exe_key and p_exe == exe_key) or (dir_key and p_dir == dir_key):
            pid = int(proc["pid"])
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=8,
            )
            killed_pids.append(pid)

    if killed_pids:
        # Poll briefly for Windows file handles to unlock cleanly
        for _ in range(16):
            still = [
                int(p["pid"])
                for p in scan_running_srbminer_processes()
                if int(p["pid"]) in killed_pids
            ]
            if not still:
                break
            time.sleep(0.25)
    return killed_pids


class KernelUpdateManager:
    """Manages async GitHub release downloads, selective exe replacement, and archive cleanup."""

    def __init__(self, on_complete: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._on_complete = on_complete
        self._thread: threading.Thread | None = None
        self._task: dict[str, Any] = {
            "running": False,
            "phase": "idle",  # idle | downloading | verifying | stopping | extracting | cleaning | done | error
            "mode": "",
            "message": "就绪",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "speed_kbps": 0.0,
            "archive_deleted": False,
            "done": False,
            "error": "",
            "result": None,
        }

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._task)

    def _update(self, **kwargs: Any) -> None:
        with self._lock:
            self._task.update(kwargs)

    def start_deploy(
        self,
        *,
        kernel_dir: str,
        download_url: str,
        asset_name: str,
        tag_name: str,
        repo: str = DEFAULT_GITHUB_REPO,
        mode: str = "exe_only",
        github_proxy: str = "",
        expected_digest: str = "",
        stop_running: bool = True,
        backup_exe: bool = True,
        miner_binder: Any = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._task.get("running"):
                raise RuntimeError("已有内核下载/更新任务正在执行，请等待当前任务完成")
            kdir = Path(str(kernel_dir or "").strip())
            if not str(kernel_dir or "").strip():
                raise ValueError("请先选择或填写矿机内核目录路径")
            if not (download_url or "").strip():
                raise ValueError("缺少发行版压缩包下载地址")
            if not download_url.strip().lower().startswith(("https://", "http://")):
                raise ValueError("下载地址必须以 https:// 或 http:// 开头")
            if mode not in ("exe_only", "full_deploy"):
                raise ValueError("部署模式必须是 exe_only 或 full_deploy")
            if mode == "exe_only" and not kdir.is_dir():
                raise ValueError(f"绑定的内核目录不存在: {kdir}（若要在新电脑首次安装，请点「新电脑完整部署内核」）")

            self._task = {
                "running": True,
                "phase": "downloading",
                "mode": mode,
                "message": f"正在下载 {asset_name or '内核压缩包'}...",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "percent": 0.0,
                "speed_kbps": 0.0,
                "archive_deleted": False,
                "done": False,
                "error": "",
                "result": None,
            }

        args = (
            kdir,
            download_url.strip(),
            (asset_name or "").strip() or Path(urllib.parse.urlparse(download_url).path).name or "srbminer.zip",
            (tag_name or "").strip(),
            repo,
            github_proxy.strip(),
            expected_digest.strip(),
            mode,
            bool(stop_running),
            bool(backup_exe),
            miner_binder,
        )
        t = threading.Thread(target=self._run_worker, args=args, name="KernelUpdater", daemon=True)
        self._thread = t
        t.start()
        return self.get_status()

    def _run_worker(
        self,
        kdir: Path,
        download_url: str,
        asset_name: str,
        tag_name: str,
        repo: str,
        github_proxy: str,
        expected_digest: str,
        mode: str,
        stop_running: bool,
        backup_exe: bool,
        miner_binder: Any,
    ) -> None:
        tmp_dir_obj = tempfile.TemporaryDirectory(prefix="pearl_kernel_dl_")
        archive_path = Path(tmp_dir_obj.name) / asset_name
        archive_deleted = False
        try:
            kdir.mkdir(parents=True, exist_ok=True)
            eff_url = _apply_github_proxy(download_url, github_proxy)
            headers = {"User-Agent": "PearlSplit-KernelUpdater"}

            t0 = time.monotonic()
            downloaded = 0
            sha256_h = hashlib.sha256()
            with requests.get(eff_url, headers=headers, stream=True, timeout=(15, 60)) as resp:
                resp.raise_for_status()
                total_len = int(resp.headers.get("Content-Length") or 0)
                self._update(total_bytes=total_len)
                with open(archive_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=131072):
                        if not chunk:
                            continue
                        f.write(chunk)
                        sha256_h.update(chunk)
                        downloaded += len(chunk)
                        elapsed = max(0.1, time.monotonic() - t0)
                        pct = (downloaded / total_len * 100.0) if total_len > 0 else 0.0
                        self._update(
                            downloaded_bytes=downloaded,
                            percent=round(min(99.5, pct), 1),
                            speed_kbps=round(downloaded / 1024.0 / elapsed, 1),
                        )

            if downloaded <= 0 or not archive_path.is_file():
                raise RuntimeError("下载的压缩包为空，请检查网络或代理设置")

            if expected_digest.lower().startswith("sha256:"):
                expected_hex = expected_digest.split(":", 1)[1].strip().lower()
                actual_hex = sha256_h.hexdigest().lower()
                if expected_hex and actual_hex != expected_hex:
                    raise RuntimeError(f"压缩包 SHA256 校验不匹配 (预期 {expected_hex[:12]}...，实际 {actual_hex[:12]}...)")

            killed_pids: list[int] = []
            if stop_running:
                self._update(phase="stopping", message="正在检查并停止当前目录运行中的 SRBMiner 进程...")
                if miner_binder is not None:
                    try:
                        bound_bat = str(getattr(miner_binder, "bat_path", "") or "")
                        if bound_bat and _norm_path_key(Path(bound_bat).parent) == _norm_path_key(kdir):
                            miner_binder.stop_managed_bat()
                    except Exception:
                        pass
                killed_pids = _stop_processes_for_dir(kdir)

            phase_msg = (
                "正在从压缩包仅提取 SRBMiner-MULTI.exe 并覆盖..."
                if mode == "exe_only"
                else "正在完整解压新内核文件到目标目录..."
            )
            self._update(phase="extracting", message=phase_msg)
            extract_res = _extract_archive_file(
                archive_path=archive_path,
                target_dir=kdir,
                mode=mode,
                backup_exe=backup_exe,
            )

            self._update(phase="cleaning", message="正在清理并删除已下载的内核压缩包...")
            if archive_path.exists():
                archive_path.unlink(missing_ok=True)
                archive_deleted = True

            # Record installed kernel version metadata
            exe_p = find_kernel_exe(kdir)
            meta_payload = {
                "repo": repo,
                "tag_name": tag_name,
                "version": format_version_label(tag_name) or tag_name,
                "asset_name": asset_name,
                "mode": mode,
                "exe_name": exe_p.name if exe_p else DEFAULT_KERNEL_EXE_NAME,
                "exe_size": int(exe_p.stat().st_size) if exe_p and exe_p.is_file() else 0,
                "updated_at": _now_str(),
            }
            (kdir / KERNEL_META_FILENAME).write_text(
                json.dumps(meta_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            done_verb = "仅覆盖更新 SRBMiner-MULTI.exe 完成" if mode == "exe_only" else "新电脑完整部署 SRBMiner 内核完成"
            self._update(
                running=False,
                phase="done",
                percent=100.0,
                archive_deleted=archive_deleted,
                done=True,
                error="",
                message=f"{done_verb}（版本 {meta_payload['version']}），压缩包已自动删除",
                result={
                    **extract_res,
                    "version": meta_payload["version"],
                    "tag_name": tag_name,
                    "killed_pids": killed_pids,
                    "archive_deleted": archive_deleted,
                    "kernel_info": inspect_kernel_dir(kdir),
                },
            )
            if self._on_complete:
                self._on_complete()
        except PermissionError as exc:
            self._update(
                running=False,
                phase="error",
                done=True,
                error=f"写入失败（SRBMiner-MULTI.exe 可能仍被矿机进程或杀毒软件占用）: {exc}",
                message="覆盖内核失败：文件被占用",
            )
        except Exception as exc:
            self._update(
                running=False,
                phase="error",
                done=True,
                error=str(exc),
                message=f"更新失败: {exc}",
            )
        finally:
            try:
                if archive_path.exists():
                    archive_path.unlink(missing_ok=True)
                    archive_deleted = True
            except Exception:
                pass
            try:
                tmp_dir_obj.cleanup()
            except Exception:
                pass
            if archive_deleted:
                self._update(archive_deleted=True)
