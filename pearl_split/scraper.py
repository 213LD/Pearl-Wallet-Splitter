"""Scraper for ntminerpool.com Pearl (PRL) dashboard and payment records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.ntminerpool.com"

HASHRATE_VAL_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?H/s)", re.IGNORECASE)
HASHRATE_UNIT_TO_THS = {
    "H/S": 1e-12,
    "KH/S": 1e-9,
    "MH/S": 1e-6,
    "GH/S": 1e-3,
    "TH/S": 1.0,
    "PH/S": 1e3,
}


def parse_hashrate_ths(text: str) -> float:
    """Parse a hashrate display string (e.g. '175.09 TH/s') into numeric TH/s."""
    if not text:
        return 0.0
    m = HASHRATE_VAL_RE.search(text)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).upper()
    return val * HASHRATE_UNIT_TO_THS.get(unit, 1.0)


def format_hashrate_ths(ths: float) -> str:
    """Format numeric TH/s into a clean human-readable string."""
    if ths <= 0:
        return "0.00 TH/s"
    if ths >= 1000.0:
        return f"{ths / 1000.0:.2f} PH/s"
    if ths < 1.0:
        return f"{ths * 1000.0:.2f} GH/s"
    return f"{ths:.2f} TH/s"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class WorkerReading:
    """Single miner row scraped from the pool dashboard."""

    name: str
    current_hashrate: str
    avg_1h_hashrate: str
    avg_6h_hashrate: str
    avg_24h_hashrate: str
    shares: int
    last_seen_text: str
    status: str = "online"  # "online" | "offline"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaymentRecord:
    """Pool payout entry from /prl/payments."""

    paid_at: str
    amount_prl: float
    tx_hash: str
    explorer_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BlockRewardRecord:
    """Recent block reward row from pool dashboard bottom table."""

    found_at: str
    height: int
    difficulty: str
    block_reward_prl: float
    earned_prl: float
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PoolDashboardData:
    """Parsed pool dashboard snapshot for a wallet address."""

    wallet: str
    fetched_at: str
    online_count: int = 0
    offline_count: int = 0
    live_hashrate: str = "--"
    avg_24h_hashrate: str = "--"
    prl_price_cny: float | None = None
    est_daily_prl: float = 0.0
    unpaid_prl: float = 0.0
    immature_prl: float = 0.0
    reward_24h_prl: float = 0.0
    total_paid_prl: float = 0.0
    workers: list[WorkerReading] = field(default_factory=list)
    payments: list[PaymentRecord] = field(default_factory=list)
    block_rewards: list[BlockRewardRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workers"] = [w.to_dict() for w in self.workers]
        data["payments"] = [p.to_dict() for p in self.payments]
        data["block_rewards"] = [b.to_dict() for b in self.block_rewards]
        return data


def extract_wallet_address(raw_input: str) -> str:
    """Extract a PRL wallet address from either a raw address or a pool URL."""
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("钱包地址不能为空")

    if text.startswith(("http://", "https://")) or "back=" in text:
        if not text.startswith(("http://", "https://")):
            text = "https://placeholder.local/?" + text.lstrip("?")
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        back_vals = qs.get("back") or []
        if back_vals and back_vals[0].strip():
            addr = back_vals[0].strip()
        else:
            m = re.search(r"back=([a-zA-Z0-9]+)", text)
            if not m:
                raise ValueError("无法从链接中识别 back= 钱包地址")
            addr = m.group(1)
    else:
        addr = text

    if not re.fullmatch(r"[a-zA-Z0-9]{20,128}", addr):
        raise ValueError(f"钱包地址格式看起来不正确: {addr}")
    return addr


def _parse_first_float(text: str, default: float = 0.0) -> float:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(m.group(0)) if m else default


def _parse_worker_rows(soup: BeautifulSoup, status: str) -> list[WorkerReading]:
    """Extract worker rows from a dashboard HTML document."""
    workers: list[WorkerReading] = []
    # Find the worker list table inside the .online section containing .wk_tabs
    target_table = None
    for box in soup.select(".online"):
        if box.select_one(".wk_tabs"):
            target_table = box.select_one("table.hTNNTI")
            break
    if target_table is None:
        target_table = soup.select_one("table.hTNNTI")
    if target_table is None:
        return workers

    for tr in target_table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        if tds[0].has_attr("colspan"):
            continue
        name = tds[0].get_text(strip=True)
        if not name:
            continue
        shares_raw = tds[5].get_text(strip=True).replace(",", "")
        try:
            shares = int(float(shares_raw))
        except ValueError:
            shares = 0
        workers.append(
            WorkerReading(
                name=name,
                current_hashrate=tds[1].get_text(strip=True),
                avg_1h_hashrate=tds[2].get_text(strip=True),
                avg_6h_hashrate=tds[3].get_text(strip=True),
                avg_24h_hashrate=tds[4].get_text(strip=True),
                shares=shares,
                last_seen_text=tds[6].get_text(strip=True),
                status=status,
            )
        )
    return workers


def _extract_max_page(soup: BeautifulSoup) -> int:
    """Detect the maximum page number from pagination links, if any."""
    max_page = 1
    for a in soup.select(".online a[href*='page=']"):
        if "wk_tab" in (a.get("class") or []):
            continue
        href = a.get("href", "")
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def _parse_block_rewards(soup: BeautifulSoup) -> list[BlockRewardRecord]:
    """Parse the bottom '最近的区块奖励' table from the dashboard HTML."""
    target_table = None
    for box in soup.select(".online"):
        if box.select_one(".wk_tabs"):
            continue
        h2 = box.find("h2")
        if h2 and ("区块奖励" in h2.get_text() or "奖励" in h2.get_text()):
            target_table = box.select_one("table.hTNNTI") or box.select_one("table")
            if target_table is not None:
                break
    if target_table is None:
        return []

    rows: list[BlockRewardRecord] = []
    for tr in target_table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6 or tds[0].has_attr("colspan"):
            continue
        found_at = tds[0].get_text(strip=True)
        if not found_at:
            continue
        height_raw = tds[1].get_text(strip=True).replace(",", "")
        try:
            height = int(float(height_raw))
        except ValueError:
            height = 0
        difficulty = tds[2].get_text(strip=True)
        block_reward_prl = _parse_first_float(tds[3].get_text(" ", strip=True), 0.0)
        earned_prl = _parse_first_float(tds[4].get_text(" ", strip=True), 0.0)
        status = tds[5].get_text(" ", strip=True)
        rows.append(
            BlockRewardRecord(
                found_at=found_at,
                height=height,
                difficulty=difficulty,
                block_reward_prl=block_reward_prl,
                earned_prl=earned_prl,
                status=status,
            )
        )
    return rows


def parse_dashboard_html(html: str, wallet: str, fetched_at: str | None = None) -> tuple[PoolDashboardData, int]:
    """Parse a single dashboard HTML response into overview stats, workers, and max_page."""
    soup = BeautifulSoup(html, "html.parser")
    now_str = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = PoolDashboardData(wallet=wallet, fetched_at=now_str)

    # Banner summary: "4 部矿机正在挖 PRL ，实时算力 537.79 TH/s ， 24小时平均算力 371.23 TH/s 。"
    banner_nodes = soup.select(".emUnAR span") or soup.select(".Content span")
    banner_text = " ".join(s.get_text(" ", strip=True) for s in banner_nodes) or soup.get_text(" ", strip=True)
    m_live = re.search(r"实时算力\s*([0-9.]+\s*[A-Za-z/]+)", banner_text)
    if m_live:
        result.live_hashrate = m_live.group(1).strip()
    m_avg = re.search(r"24小时平均算力\s*([0-9.]+\s*[A-Za-z/]+)", banner_text)
    if m_avg:
        result.avg_24h_hashrate = m_avg.group(1).strip()

    # Price badge
    for el in soup.select(".eth_b"):
        txt = el.get_text(" ", strip=True)
        if "PRL 价格" in txt or "价格" in txt:
            m_price = re.search(r"([0-9]+(?:\.[0-9]+)?)", txt)
            if m_price:
                result.prl_price_cny = float(m_price.group(1))

    # Overview cards (.gHXKsE)
    for card in soup.select(".gHXKsE"):
        h3 = card.find("h3")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        val_el = card.select_one(".iSSiSGn")
        val_text = val_el.get_text(" ", strip=True) if val_el else ""

        if "在线/离线矿机" in title:
            nums = re.findall(r"\d+", val_text)
            if len(nums) >= 2:
                result.online_count = int(nums[0])
                result.offline_count = int(nums[1])
        elif "预估每日收益" in title:
            m_daily = re.search(r"([0-9.]+)\s*PRL", val_text)
            if m_daily:
                result.est_daily_prl = float(m_daily.group(1))
        elif "未支付余额" in title:
            m_unpaid = re.search(r"([0-9.]+)\s*PRL", val_text)
            if m_unpaid:
                result.unpaid_prl = float(m_unpaid.group(1))
            m_imm = re.search(r"\(([0-9.]+)\s*个?待成熟", val_text)
            if m_imm:
                result.immature_prl = float(m_imm.group(1))
        elif "最近24小时奖励" in title or "已支付总额" in title:
            nums = re.findall(r"([0-9.]+)\s*PRL", val_text)
            if len(nums) >= 1:
                result.reward_24h_prl = float(nums[0])
            if len(nums) >= 2:
                result.total_paid_prl = float(nums[1])

    # Tab counts fallback
    for tab in soup.select(".wk_tab"):
        tab_txt = tab.get_text(strip=True)
        m_on = re.search(r"在线矿机\((\d+)\)", tab_txt)
        m_off = re.search(r"离线矿机\((\d+)\)", tab_txt)
        if m_on:
            result.online_count = int(m_on.group(1))
        if m_off:
            result.offline_count = int(m_off.group(1))

    result.workers = _parse_worker_rows(soup, status="online")
    result.block_rewards = _parse_block_rewards(soup)
    max_page = _extract_max_page(soup)
    return result, max_page


def parse_payments_html(html: str) -> list[PaymentRecord]:
    """Parse /prl/payments HTML table."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[PaymentRecord] = []
    table = soup.select_one("table.hTNNTI")
    if not table:
        return records

    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3 or tds[0].has_attr("colspan"):
            continue
        paid_at = tds[0].get_text(strip=True)
        amount_prl = _parse_first_float(tds[1].get_text(" ", strip=True), 0.0)
        a_tag = tds[2].find("a")
        tx_hash = a_tag.get_text(strip=True) if a_tag else tds[2].get_text(strip=True)
        explorer_url = a_tag.get("href", "") if a_tag else ""
        if paid_at:
            records.append(
                PaymentRecord(
                    paid_at=paid_at,
                    amount_prl=amount_prl,
                    tx_hash=tx_hash,
                    explorer_url=explorer_url,
                )
            )
    return records


class PoolScraper:
    """HTTP client for fetching live miner stats and payment history."""

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        retry = Retry(
            total=2,
            backoff_factor=0.35,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def fetch_dashboard(self, wallet: str, include_payments: bool = True) -> PoolDashboardData:
        """Fetch all online workers (all pages), offline if non-empty, and optional payments."""
        wallet = extract_wallet_address(wallet)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        first_url = f"{BASE_URL}/prl?back={wallet}&status=online&page=1&keyword="
        html = self._get(first_url)
        data, max_page = parse_dashboard_html(html, wallet=wallet, fetched_at=now_str)

        seen_names = {w.name for w in data.workers}
        # Fetch remaining pages if paginated
        for page in range(2, min(max_page + 1, 25)):
            page_url = f"{BASE_URL}/prl?back={wallet}&status=online&page={page}&keyword="
            page_html = self._get(page_url)
            soup = BeautifulSoup(page_html, "html.parser")
            page_workers = _parse_worker_rows(soup, status="online")
            new_added = 0
            for w in page_workers:
                if w.name not in seen_names:
                    seen_names.add(w.name)
                    data.workers.append(w)
                    new_added += 1
            if new_added == 0:
                break

        # If pool ever reports offline_count > 0 on the offline tab, fetch it too
        if data.offline_count > 0:
            off_url = f"{BASE_URL}/prl?back={wallet}&status=offline&page=1&keyword="
            off_html = self._get(off_url)
            off_soup = BeautifulSoup(off_html, "html.parser")
            for w in _parse_worker_rows(off_soup, status="offline"):
                if w.name not in seen_names:
                    seen_names.add(w.name)
                    data.workers.append(w)

        if include_payments:
            try:
                pay_url = f"{BASE_URL}/prl/payments?back={wallet}"
                pay_html = self._get(pay_url)
                data.payments = parse_payments_html(pay_html)
            except Exception:
                # Payments tab is supplementary; do not fail worker fetch if it hiccups
                data.payments = []

        return data
