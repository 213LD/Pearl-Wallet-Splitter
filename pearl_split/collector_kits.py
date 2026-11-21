"""Generators for standalone backend collector server & distributed miner reporter kits."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


STANDALONE_PYTHON_CLIENT = '''"""Zero-dependency SRBMiner client agent that reports live output & real hashrate to Pearl Collector Server."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from srb_parser import SrbTelemetryParser, build_wrapped_bat_content, parse_bat_file, parse_srb_cmdline

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def post_ingest(server_url: str, token: str, payload: dict) -> None:
    url = server_url.rstrip("/") + "/api/v1/ingest"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-Collector-Token", token)
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pearl SRBMiner Telemetry Client Agent")
    ap.add_argument("--server", required=True, help="Collector server URL, e.g. http://<NAS_IP>:8765")
    ap.add_argument("--token", default="", help="Optional server access token")
    ap.add_argument("--worker", default="", help="Worker/client display name")
    ap.add_argument("--wallet", default="", help="Optional PRL wallet address")
    ap.add_argument("--bat", default="", help="Path to mining .bat file to run and monitor")
    ap.add_argument("--log-file", default="", help="Path to existing SRBMiner log file to tail")
    ap.add_argument("--interval", type=float, default=3.0, help="Push interval seconds")
    args = ap.parse_args()

    parser = SrbTelemetryParser()
    host = socket.gethostname()
    worker_name = args.worker.strip()
    log_path: Path | None = Path(args.log_file) if args.log_file else None
    proc = None
    temp_bat: Path | None = None
    source_mode = "log_tail"

    if args.bat:
        source_mode = "bat_managed"
        bat_info = parse_bat_file(args.bat)
        if not worker_name:
            worker_name = bat_info.get("worker") or host
        parser.seed_metadata(
            algorithm=bat_info.get("algorithm") or "",
            pool_url=bat_info.get("pool") or "",
            worker_name=worker_name,
        )
        bat_p = Path(bat_info["bat_path"])
        if bat_info.get("resolved_log_file"):
            log_path = Path(bat_info["resolved_log_file"])
            launch_bat = bat_p
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = Path.home() / f".pearl_srb_{bat_p.stem}_{stamp}.log"
            temp_bat = Path.home() / f".pearl_run_{bat_p.stem}_{stamp}.bat"
            temp_bat.write_text(build_wrapped_bat_content(bat_p, log_path), encoding="utf-8")
            launch_bat = temp_bat
        print(f"[Pearl Agent] Launching .bat: {bat_p} -> log: {log_path}")
        proc = subprocess.Popen(["cmd.exe", "/c", str(launch_bat)], cwd=str(bat_p.parent))
    elif not log_path:
        print("Error: 请指定 --bat <挖矿脚本.bat> 或 --log-file <矿机日志.log>")
        sys.exit(1)

    if not worker_name:
        worker_name = host

    client_id = f"{host.lower()}:{worker_name}"
    offset = 0
    if log_path.exists() and not args.bat:
        sz = log_path.stat().st_size
        offset = max(0, sz - 65536)

    last_pushed_seq = 0
    last_push_ts = 0.0
    try:
        while True:
            if log_path.exists():
                sz = log_path.stat().st_size
                if sz < offset:
                    offset = 0
                if sz > offset:
                    with open(log_path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    for raw_l in decode_bytes(chunk).splitlines():
                        parser.feed_line(raw_l)

            now = time.time()
            if (now - last_push_ts) >= max(1.0, args.interval):
                last_push_ts = now
                rep = parser.get_report(since_seq=last_pushed_seq)
                latest_seq = rep["latest_seq"]
                status_str = "running" if (proc is None or proc.poll() is None) else "stopped"
                payload = {
                    "client_id": client_id,
                    "client_name": worker_name,
                    "worker_name": rep.get("worker_name") or worker_name,
                    "wallet_address": args.wallet,
                    "host_name": host,
                    "os_info": sys.platform,
                    "source_mode": source_mode,
                    "miner_status": status_str,
                    "algorithm": rep["algorithm"],
                    "miner_version": rep["miner_version"],
                    "pool_url": rep["pool_url"],
                    "uptime_sec": rep["uptime_sec"],
                    "total_hashrate_ths": rep["current_hashrate_ths"],
                    "session_avg_ths": rep["session_avg_ths"],
                    "avg_5m_ths": rep["avg_5m_ths"],
                    "avg_15m_ths": rep["avg_15m_ths"],
                    "min_hashrate_ths": rep["min_hashrate_ths"],
                    "max_hashrate_ths": rep["max_hashrate_ths"],
                    "stability_pct": rep["stability_pct"],
                    "hashrate_raw_text": rep["current_hashrate_text"],
                    "accepted_shares": rep["accepted_shares"],
                    "rejected_shares": rep["rejected_shares"],
                    "stale_shares": rep["stale_shares"],
                    "accept_rate_pct": rep["accept_rate_pct"],
                    "shares_per_hour": rep["shares_per_hour"],
                    "avg_latency_ms": rep["avg_latency_ms"],
                    "total_power_w": rep["total_power_w"],
                    "efficiency_ths_per_kw": rep["efficiency_ths_per_kw"],
                    "devices": rep["devices"],
                    "samples": rep["samples"][-60:],
                    "log_lines": rep["lines"][-120:],
                }
                try:
                    post_ingest(args.server, args.token, payload)
                    last_pushed_seq = latest_seq
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Pushed OK | "
                        f"{rep['current_hashrate_text']} | Shares: {rep['accepted_shares']} | "
                        f"New lines: {len(rep['lines'])}"
                    )
                except Exception as exc:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Push error: {exc}")
                if status_str == "stopped":
                    break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\\nStopping client agent...")
    finally:
        if proc and proc.poll() is None and os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        if temp_bat and temp_bat.exists():
            try:
                temp_bat.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    main()
'''


POWERSHELL_REPORTER_TEMPLATE = r'''param(
    [Parameter(Mandatory=$true)][string]$ServerUrl,
    [string]$Token = "",
    [string]$WorkerName = "",
    [string]$TargetBat = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Continue"
$ServerUrl = $ServerUrl.TrimEnd("/")
$HostName = $env:COMPUTERNAME

if (-not $TargetBat -and -not $LogFile) {
    Write-Host "[Pearl 上报] 请指定 -TargetBat <原挖矿.bat> 或 -LogFile <SRBMiner日志.log>" -ForegroundColor Red
    exit 1
}

function Send-Telemetry([string[]]$Lines, [string]$StatusText, [string]$ModeName, [string]$EffWorker, [string]$Algo, [string]$Pool) {
    $cid = ($HostName.ToLower() + ":" + $EffWorker)
    $lineObjs = New-Object System.Collections.Generic.List[object]
    $nowTs = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    foreach ($l in $Lines) {
        if ($l -and $l.Trim().Length -gt 0) {
            $lineObjs.Add(@{ ts = $nowTs; line = $l })
        }
    }
    $bodyObj = @{
        client_id = $cid
        client_name = $EffWorker
        worker_name = $EffWorker
        host_name = $HostName
        os_info = "Windows-PowerShell"
        source_mode = $ModeName
        miner_status = $StatusText
        algorithm = $Algo
        pool_url = $Pool
        log_lines = @($lineObjs.ToArray())
    }
    $json = $bodyObj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $headers = @{}
    if ($Token) { $headers["X-Collector-Token"] = $Token }
    try {
        Invoke-RestMethod -Uri "$ServerUrl/api/v1/ingest" -Method Post -Body $bytes -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

$AlgoGuess = "--"
$PoolGuess = "--"
$EffWorker = $WorkerName
$Proc = $null
$TempBat = $null
$TailPath = $LogFile
$SourceMode = "ps_log_tail"

if ($TargetBat) {
    $SourceMode = "ps_bat_managed"
    if (-not (Test-Path $TargetBat)) {
        Write-Host "[Pearl 上报] 找不到脚本文件: $TargetBat" -ForegroundColor Red
        exit 1
    }
    $BatItem = Get-Item $TargetBat
    $BatDir = $BatItem.DirectoryName
    $BatBytes = [System.IO.File]::ReadAllBytes($BatItem.FullName)
    $utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $BatContent = $utf8Strict.GetString($BatBytes)
    } catch {
        $BatContent = [System.Text.Encoding]::Default.GetString($BatBytes)
    }
    if (-not $EffWorker) {
        if ($BatContent -match '--worker\s+([^\s"]+)') { $EffWorker = $Matches[1] }
        elseif ($BatContent -match '--wallet\s+[^\s".]+\.([^\s"]+)') { $EffWorker = $Matches[1] }
        else { $EffWorker = $HostName }
    }
    if ($BatContent -match '--algorithm\s+([^\s"]+)') { $AlgoGuess = $Matches[1] }
    if ($BatContent -match '--pool\s+([^\s"]+)') { $PoolGuess = $Matches[1] }

    $Stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
    if ($BatContent -match '--log-file\s+"?([^"\r\n]+)"?') {
        $RawLog = $Matches[1].Trim()
        if ([System.IO.Path]::IsPathRooted($RawLog)) { $TailPath = $RawLog } else { $TailPath = Join-Path $BatDir $RawLog }
        $RunBat = $BatItem.FullName
    } else {
        $TailPath = Join-Path $env:TEMP "pearl_srb_$($Stamp).log"
        $TempBat = Join-Path $env:TEMP "pearl_wrap_$($Stamp).bat"
        $NewLines = @("@echo off", "chcp 65001 >nul", "cd /d `"$BatDir`"")
        foreach ($rawLine in ($BatContent -split "`r?`n")) {
            $ln = $rawLine -replace '%~dp0', ($BatDir.TrimEnd('\') + '\')
            if ($ln -match '^\s*(pause\b|@echo off|chcp\s)') { continue }
            if ($ln -match 'srbminer' -and $ln -notmatch '^\s*(rem|::|echo|title)\s') {
                $ln = $ln -replace '^\s*start\s+(?:(?:"[^"]*"|/\w+)\s+)*', ''
                if ($ln -notmatch '--log-file') {
                    $ln = "$ln --log-file `"$TailPath`""
                }
            }
            $NewLines += $ln
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($TempBat, ($NewLines -join "`r`n") + "`r`n", $utf8NoBom)
        $RunBat = $TempBat
    }

    Write-Host "[Pearl 上报] 正在启动矿机并上报至 $ServerUrl (节点名: $EffWorker)..." -ForegroundColor Green
    $Proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$RunBat`"" -WorkingDirectory $BatDir -NoNewWindow -PassThru
}

if (-not $EffWorker) { $EffWorker = $HostName }

$Offset = [int64]0
$Pending = New-Object System.Collections.Generic.List[string]
$LastPush = Get-Date "2000-01-01"
if ($LogFile -and (Test-Path $TailPath)) {
    $fi = Get-Item $TailPath
    $Offset = [Math]::Max(0, $fi.Length - 32768)
}

try {
    while ($true) {
        if ($TailPath -and (Test-Path $TailPath)) {
            try {
                $fs = [System.IO.File]::Open($TailPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                if ($fs.Length -lt $Offset) { $Offset = 0 }
                if ($fs.Length -gt $Offset) {
                    $null = $fs.Seek($Offset, [System.IO.SeekOrigin]::Begin)
                    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8, $true)
                    $text = $sr.ReadToEnd()
                    $Offset = $fs.Position
                    $sr.Close()
                    foreach ($line in ($text -split "`r?`n")) {
                        if ($line.Trim().Length -gt 0) { $Pending.Add($line) }
                    }
                }
                $fs.Close()
            } catch { }
        }

        $Running = ($null -eq $Proc) -or (-not $Proc.HasExited)
        $Now = Get-Date
        if (($Now - $LastPush).TotalSeconds -ge 3 -or $Pending.Count -ge 40 -or (-not $Running)) {
            $batch = @()
            if ($Pending.Count -gt 0) {
                $batch = $Pending.ToArray()
                $Pending.Clear()
            }
            $st = if ($Running) { "running" } else { "stopped" }
            $ok = Send-Telemetry -Lines $batch -StatusText $st -ModeName $SourceMode -EffWorker $EffWorker -Algo $AlgoGuess -Pool $PoolGuess
            $LastPush = $Now
            try {
                $flag = if ($ok) { "上报正常" } else { "上报失败" }
                $Host.UI.RawUI.WindowTitle = "SRBMiner ($EffWorker) | $flag | $($Now.ToString('HH:mm:ss'))"
            } catch { }
            if (-not $Running) { break }
        }
        Start-Sleep -Milliseconds 600
    }
} finally {
    if ($TempBat -and (Test-Path $TempBat)) {
        Remove-Item $TempBat -Force -ErrorAction SilentlyContinue
    }
}
'''


DOCKERFILE_TEMPLATE = """FROM python:3.12-slim

LABEL maintainer="PearlSplit" \\
      description="Pearl Miner Telemetry Collector Backend Server"

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    TZ=Asia/Shanghai \\
    PEARL_COLLECTOR_HOST=0.0.0.0 \\
    PEARL_COLLECTOR_PORT=8765 \\
    PEARL_COLLECTOR_DB=/data/collector_telemetry.db \\
    PEARL_COLLECTOR_TOKEN=""

WORKDIR /app
RUN mkdir -p /data

COPY srb_parser.py alert_engine.py collector_server.py /app/

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \\
  CMD python3 -c "import os, urllib.request; p=os.environ.get('PEARL_COLLECTOR_PORT','8765'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "-u", "/app/collector_server.py"]
"""


DOCKER_COMPOSE_TEMPLATE = """services:
  pearl-collector:
    build:
      context: .
      dockerfile: Dockerfile
    image: pearl-collector:latest
    container_name: pearl-collector
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
      - PEARL_COLLECTOR_HOST=0.0.0.0
      - PEARL_COLLECTOR_PORT=8765
      - PEARL_COLLECTOR_DB=/data/collector_telemetry.db
      # 访问密钥：若开放到局域网外或反向代理公网访问，务必填写高强度随机密码（例如 CHANGE_ME_STRONG_TOKEN）
      - PEARL_COLLECTOR_TOKEN=
    volumes:
      - ./data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
"""


DOCKER_COMPOSE_NOBUILD_TEMPLATE = """# 免构建快速版 Compose：无需执行 docker build，直接挂载本地两份脚本运行
# 如果群晖直连 Docker Hub 较慢，可把下方 image 改为已配置镜像加速的地址，或先在 Container Manager「注册表」下载 python:3.12-slim
services:
  pearl-collector:
    image: python:3.12-slim
    container_name: pearl-collector
    restart: unless-stopped
    working_dir: /app
    command: ["python3", "-u", "/app/collector_server.py"]
    ports:
      - "8765:8765"
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
      - PEARL_COLLECTOR_HOST=0.0.0.0
      - PEARL_COLLECTOR_PORT=8765
      - PEARL_COLLECTOR_DB=/data/collector_telemetry.db
      # 访问密钥：若开放到局域网外或反向代理公网访问，务必填写高强度随机密码（例如 CHANGE_ME_STRONG_TOKEN）
      - PEARL_COLLECTOR_TOKEN=
    volumes:
      - ./collector_server.py:/app/collector_server.py:ro
      - ./alert_engine.py:/app/alert_engine.py:ro
      - ./srb_parser.py:/app/srb_parser.py:ro
      - ./data:/data
    healthcheck:
      test: ["CMD", "python3", "-c", "import os, urllib.request; p=os.environ.get('PEARL_COLLECTOR_PORT','8765'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 5s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
"""


DOCKERIGNORE_TEMPLATE = """data/
*.db
*.db-wal
*.db-shm
*.log
*.bat
*.txt
__pycache__/
*.pyc
"""


SYNOLOGY_GUIDE_TEMPLATE = """====================================================================
Pearl 矿机集群统计后端 · 群晖 NAS (Container Manager) Docker 部署教程
====================================================================

本后端完全基于 Python 3 标准库实现（零第三方依赖），SQLite 数据保存在 `./data` 目录，
支持网页端实时监控、多矿机汇总、单卡温度/功耗/算力解析与 Token 鉴权。

--------------------------------------------------------------------
方式一（最推荐）：使用 Container Manager「项目 (Project)」一键部署
--------------------------------------------------------------------
1. 在群晖 File Station 的 `docker` 共享文件夹中新建目录：
   `/docker/pearl-collector`
2. 将本文件夹内的全部文件（务必包含 `data` 子文件夹、`Dockerfile`、`docker-compose.yml`、
   `collector_server.py`、`alert_engine.py`、`srb_parser.py` 等）上传到 `/docker/pearl-collector`。
3. 如需设置访问密码（强烈建议开放到公网时设置）：
   编辑 `docker-compose.yml`，把：
     `- PEARL_COLLECTOR_TOKEN=`
   改为：
     `- PEARL_COLLECTOR_TOKEN=你的自定义密钥`
   如需修改宿主机外部端口（例如改为 9876），把 `ports` 改为 `"9876:8765"`（容器内部仍保持 8765）。
4. 打开群晖 **Container Manager** -> 左侧选择 **项目 (Project)** -> 点击 **新增 (Create)**：
   - 项目名称：`pearl-collector`
   - 路径：选择 `/docker/pearl-collector`
   - 来源：自动识别 `docker-compose.yml`（或选择“使用现有 docker-compose.yml 创建项目”）
   - 点击 **下一步**，不启用网页门户或按需勾选，点击 **完成** 启动构建与运行。
5. 等待几秒后状态变为绿色运行中：
   - 浏览器访问：`http://你的群晖IP:8765/` 即可打开 Web 实时集群看板。
   - 健康检查接口：`http://你的群晖IP:8765/health`

※ 若你的网络下载 Docker 镜像受限：
  - 可先在 Container Manager「注册表 -> 设置」配置镜像加速，或在“方式一”直接用 `docker-compose.nobuild.yml`
    的内容覆盖 `docker-compose.yml`（免 `docker build` 步骤，把 `image:` 改成你在注册表拉取的 Python 3.12 镜像即可）。

--------------------------------------------------------------------
方式二：群晖纯图形界面「新建容器」直接跑（不用 Compose 也可）
--------------------------------------------------------------------
1. 在 Container Manager **注册表** 下载官方镜像：`python:3.12-slim`（或 `python:3.12-alpine`）。
2. 确保群晖上 `/docker/pearl-collector` 放好了 `collector_server.py`、`alert_engine.py`、`srb_parser.py` 和空目录 `/docker/pearl-collector/data`。
3. 选中已下载的 `python` 镜像 -> **运行**：
   - 端口设置：本地端口 `8765` -> 容器端口 `8765` (TCP)
   - 存储空间（卷映射）：
     - `/docker/pearl-collector/collector_server.py` -> `/app/collector_server.py`（只读）
     - `/docker/pearl-collector/alert_engine.py`     -> `/app/alert_engine.py`（只读）
     - `/docker/pearl-collector/srb_parser.py`       -> `/app/srb_parser.py`（只读）
     - `/docker/pearl-collector/data`                -> `/data`（读/写）
   - 环境变量：
     - `TZ` = `Asia/Shanghai`
     - `PYTHONUNBUFFERED` = `1`
     - `PEARL_COLLECTOR_DB` = `/data/collector_telemetry.db`
     - `PEARL_COLLECTOR_TOKEN` = `你的密钥(可留空)`
   - 执行命令（Command）：
     `python3 -u /app/collector_server.py`
   - 勾选“启用自动重新启动”。

--------------------------------------------------------------------
方式三：电脑离线构建镜像导入群晖（适合群晖完全无外网访问镜像仓库）
--------------------------------------------------------------------
在有 Docker 的电脑进入本目录运行：
  docker build --platform linux/amd64 -t pearl-collector:latest .
  docker save -o pearl-collector.tar pearl-collector:latest
（若群晖为 ARM64 机型如 DS220j/DS423 等，请将 `--platform` 改为 `linux/arm64`）
上传 `pearl-collector.tar` 到群晖 -> Container Manager **映像 -> 新增 -> 从文件添加**，
然后挂载 `/docker/pearl-collector/data` 到容器 `/data`、映射 `8765:8765` 启动即可。

--------------------------------------------------------------------
四、接入 Pearl 分账助手与各朋友矿机
--------------------------------------------------------------------
1. 如群晖启用了防火墙（控制面板 -> 安全性 -> 防火墙），请放行 `8765/TCP`。
2. 如需外网 HTTPS 安全接入：在「控制面板 -> 登录门户 -> 高级 -> 反向代理服务器」新建规则，
   将域名 HTTPS 转发到 `http://127.0.0.1:8765`（后端已自动识别 `X-Forwarded-For` / `X-Real-IP`）。
3. 打开 Windows 版「Pearl 分账助手」-> 左侧 **矿机集群后端**：
   - 数据源模式选 **远程独立后端**
   - 后端服务器 URL 填 `http://你的群晖IP:8765`（或反向代理域名）
   - 鉴权 Token 与 `PEARL_COLLECTOR_TOKEN` 保持一致 -> 点击 **保存并拉取**
4. 点击 **⚡ 生成朋友矿机一键上报包**（把服务器 URL 填成群晖地址），把生成的上报包发给各朋友矿机运行即可。

--------------------------------------------------------------------
五、主桌面端关机后的微信掉线告警备援（主备自动接管）
--------------------------------------------------------------------
1. 本服务端已内置 `alert_engine.py`，支持接收 Windows 端同步的 PushPlus / WxPusher 推送配置、
   监控钱包及每台矿机远程连接配对码/固定密码。
2. 当你的主电脑开启「Pearl 分账助手」时，群晖自动处于「热备暂停」状态，由电脑端负责轮询和报警，不重复推送；
3. 当电脑关机、退出软件或超过租约时间未续约时，群晖 Docker 自动接管定时轮询矿池并在矿机掉线（或矿池连续失败超 30 分钟）时推送微信提醒；
4. 电脑重新开机启动客户端后，会自动取回群晖期间触发的告警状态，避免二次重复推送。
5. 若此前已在群晖部署过旧版容器：只需把新导出的 `collector_server.py`、`alert_engine.py`、`docker-compose.yml` 覆盖上传，
   保留原有 `./data` 目录并在 Container Manager 重建/重启项目即可无缝升级！
"""


def _write_lf_text(path: Path, content: str, executable: bool = False) -> Path:
    normalized = content.replace("\r\n", "\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(normalized)
    if executable:
        try:
            path.chmod(path.stat().st_mode | 0o755)
        except OSError:
            pass
    return path


def export_server_bundle(dest_dir: str | Path) -> dict[str, Any]:
    """Export standalone backend collector server bundle (Synology Docker / VPS / LAN)."""
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir = Path(__file__).resolve().parent

    copied_files: list[str] = []
    for fname in ("srb_parser.py", "alert_engine.py", "collector_server.py"):
        src = pkg_dir / fname
        if src.exists():
            shutil.copy2(src, out / fname)
            copied_files.append(str(out / fname))

    data_keep = data_dir / ".keep"
    _write_lf_text(data_keep, "# 群晖 Container Manager 持久化数据目录 (collector_telemetry.db 将保存在此)\n")
    copied_files.append(str(data_keep))

    dockerfile = _write_lf_text(out / "Dockerfile", DOCKERFILE_TEMPLATE)
    copied_files.append(str(dockerfile))

    compose_file = _write_lf_text(out / "docker-compose.yml", DOCKER_COMPOSE_TEMPLATE)
    copied_files.append(str(compose_file))

    compose_nobuild = _write_lf_text(out / "docker-compose.nobuild.yml", DOCKER_COMPOSE_NOBUILD_TEMPLATE)
    copied_files.append(str(compose_nobuild))

    dockerignore = _write_lf_text(out / ".dockerignore", DOCKERIGNORE_TEMPLATE)
    copied_files.append(str(dockerignore))

    syno_guide = out / "群晖Container_Manager部署教程.txt"
    syno_guide.write_text(SYNOLOGY_GUIDE_TEMPLATE.replace("\n", "\r\n"), encoding="utf-8")
    copied_files.append(str(syno_guide))

    win_bat = out / "启动收集后端服务器.bat"
    win_bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "echo 启动 Pearl 矿机集群后端收集服务器 (默认端口 8765)...\r\n"
        "echo 如需密码鉴权，请在下方命令末尾加上 --token 你的密码\r\n"
        "python collector_server.py --host 0.0.0.0 --port 8765\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    copied_files.append(str(win_bat))

    sh_file = _write_lf_text(
        out / "start_collector_server.sh",
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'cd "$(dirname "$0")"\n'
        'PORT="${1:-${PEARL_COLLECTOR_PORT:-8765}}"\n'
        'TOKEN="${PEARL_COLLECTOR_TOKEN:-}"\n'
        'if [ -n "$TOKEN" ]; then\n'
        '  exec python3 -u collector_server.py --host 0.0.0.0 --port "$PORT" --token "$TOKEN"\n'
        "else\n"
        '  exec python3 -u collector_server.py --host 0.0.0.0 --port "$PORT"\n'
        "fi\n",
        executable=True,
    )
    copied_files.append(str(sh_file))

    readme = out / "部署说明.txt"
    readme.write_text(
        "Pearl 矿机集群后端收集服务器 - 部署指南\r\n"
        "=============================================\r\n\r\n"
        "特点：完全基于 Python 3 标准库实现，无需 pip install 任何第三方包！\r\n\r\n"
        "0. 群晖 NAS (Container Manager / Docker) 部署：\r\n"
        "   已内置 `Dockerfile`、`docker-compose.yml`、`docker-compose.nobuild.yml` 与 `data` 目录，\r\n"
        "   详细步骤请查看同目录下的《群晖Container_Manager部署教程.txt》。\r\n\r\n"
        "1. Windows 本机 / 局域网主机部署：\r\n"
        "   双击运行「启动收集后端服务器.bat」即可（放行防火墙 8765 端口）。\r\n\r\n"
        "2. Linux 云服务器 / VPS 部署：\r\n"
        "   上传本文件夹中 srb_parser.py、alert_engine.py、collector_server.py、start_collector_server.sh 到服务器，运行：\r\n"
        "   nohup python3 -u collector_server.py --host 0.0.0.0 --port 8765 --token 你的访问密钥 > collector.log 2>&1 &\r\n\r\n"
        "3. 查看方式：\r\n"
        "   - 浏览器打开 http://你的服务器IP:8765/ 即可查看网页版集群实时监控与控制台输出；\r\n"
        "   - 或在「Pearl 分账助手」软件左侧「矿机集群后端」中填入服务器地址与密钥查看。\r\n",
        encoding="utf-8",
    )
    copied_files.append(str(readme))
    return {"dir": str(out), "files": copied_files}


def export_client_kit(
    dest_dir: str | Path,
    server_url: str,
    token: str = "",
    worker_hint: str = "",
) -> dict[str, Any]:
    """Export zero-Python Windows PowerShell + Python reporting scripts preconfigured for friends."""
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)
    pkg_dir = Path(__file__).resolve().parent

    clean_url = (server_url or "http://127.0.0.1:8765").strip().rstrip("/")
    clean_token = (token or "").strip()
    clean_worker = (worker_hint or "").strip()

    ps_file = out / "pearl_srb_reporter.ps1"
    ps_file.write_text(POWERSHELL_REPORTER_TEMPLATE, encoding="utf-8-sig")

    bat_file = out / "一键启动SRBMiner并上报(免Python).bat"
    worker_arg = f' -WorkerName "{clean_worker}"' if clean_worker else ""
    token_arg = f' -Token "{clean_token}"' if clean_token else ""
    bat_content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "REM ============================================================\r\n"
        "REM 使用方法1：把你原来的挖矿 .bat 文件直接用鼠标拖到本脚本图标上松开！\r\n"
        "REM 使用方法2：把下方 TARGET_BAT 改成你的挖矿脚本文件名（例如 start_prl.bat）后双击运行。\r\n"
        "REM ============================================================\r\n"
        'set "TARGET_BAT=%~1"\r\n'
        'if "%TARGET_BAT%"=="" set "TARGET_BAT=start_prl.bat"\r\n'
        f'set "SERVER_URL={clean_url}"\r\n'
        'if not exist "%TARGET_BAT%" (\r\n'
        '  echo [错误] 未找到挖矿脚本: "%TARGET_BAT%"\r\n'
        "  echo 请直接把你的挖矿 .bat 文件拖拽到本脚本图标上运行，或编辑本文件修改 TARGET_BAT。\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pearl_srb_reporter.ps1" -ServerUrl "%SERVER_URL%"{token_arg}{worker_arg} -TargetBat "%TARGET_BAT%"\r\n'
        "pause\r\n"
    )
    bat_file.write_text(bat_content, encoding="utf-8")

    files = [str(ps_file), str(bat_file)]

    srb_parser_src = pkg_dir / "srb_parser.py"
    if srb_parser_src.exists():
        shutil.copy2(srb_parser_src, out / "srb_parser.py")
        files.append(str(out / "srb_parser.py"))

    py_client = out / "collector_client.py"
    py_client.write_text(STANDALONE_PYTHON_CLIENT, encoding="utf-8")
    files.append(str(py_client))

    guide = out / "使用说明_朋友矿机端.txt"
    guide.write_text(
        "Pearl 朋友矿机端 - 一键上报真实算力与内核日志\r\n"
        "===================================================\r\n\r\n"
        f"目标服务器地址：{clean_url}\r\n"
        f"访问密钥 Token：{'(已配置)' if clean_token else '(未设置)'}\r\n\r\n"
        "【方案一：免安装 Python（推荐，Windows 原生直接用）】\r\n"
        "1. 把本文件夹里的 `pearl_srb_reporter.ps1` 和 `一键启动SRBMiner并上报(免Python).bat` 复制到你矿机目录。\r\n"
        "2. 直接把你平时用来挖矿的 `.bat` 文件用鼠标拖到 `一键启动SRBMiner并上报(免Python).bat` 图标上松开！\r\n"
        "   （或者右键编辑该 bat，把 `start_prl.bat` 改成你的 bat 文件名后双击即可）\r\n"
        "3. 矿机窗口会像往常一样正常运行，同时后台每 3 秒向服务端上报真实输出日志与单卡算力。\r\n\r\n"
        "【方案二：Python 命令行版本（适合已装 Python 的主机）】\r\n"
        f'python collector_client.py --server "{clean_url}"'
        + (f' --token "{clean_token}"' if clean_token else "")
        + ' --bat "你的挖矿脚本.bat"\r\n',
        encoding="utf-8",
    )
    files.append(str(guide))
    return {"dir": str(out), "files": files}
