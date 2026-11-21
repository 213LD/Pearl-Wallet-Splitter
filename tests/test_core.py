"""Comprehensive unit and integration tests for Pearl Wallet Splitter core logic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pearl_split.api import DesktopApi
from pearl_split.calculator import (
    MemberInfo,
    PrefixRule,
    WorkerBinding,
    allocate_exact_amounts,
    classify_worker,
    compute_worker_delta,
    suggest_prefix,
)
from pearl_split.collector_kits import export_client_kit, export_server_bundle
from pearl_split.collector_server import CollectorServerController, WEB_DASHBOARD_HTML, build_cli_parser
from pearl_split.db import Database
from pearl_split.miner_binder import diff_console_lines, unwrap_console_rows
from pearl_split.poller import BackgroundPoller
from pearl_split.scraper import (
    BlockRewardRecord,
    PaymentRecord,
    PoolDashboardData,
    PoolScraper,
    WorkerReading,
    extract_wallet_address,
    format_hashrate_ths,
    parse_dashboard_html,
    parse_hashrate_ths,
    parse_payments_html,
)
from pearl_split.srb_parser import (
    SrbTelemetryParser,
    build_wrapped_bat_content,
    parse_bat_file,
)

DEMO_WALLET = "prl1examplewalletdemo00000000000000000000000000000000000000000"

SAMPLE_DASHBOARD_HTML = """
<!DOCTYPE html>
<html><body>
<div class="emUnAR">
  <span>你好</span>，
  <span>4 部矿机正在挖 PRL ，实时算力 537.79 TH/s ， 24小时平均算力 371.23 TH/s 。</span>
</div>
<div class="eth"><div class="eth_b">PRL 价格： CNY￥9.5</div></div>
<div class="gHXKsE"><h3>在线/离线矿机</h3><div class="iSSiSGn"><b>4/</b><b>0</b></div></div>
<div class="gHXKsE"><h3>未支付余额</h3><div class="iSSiSGn"><b>0.433648 PRL (1.408768 个待成熟)</b></div></div>
<div class="gHXKsE"><h3>最近24小时奖励 / 已支付总额</h3><div class="iSSiSGn"><b>3.623848 PRL</b> / <b>68.630173 PRL</b></div></div>
<div class="online online_n">
  <div class="wk_tabs">
    <a class="wk_tab active" href="?status=online">在线矿机(4)</a>
    <a class="wk_tab" href="?status=offline">离线矿机(0)</a>
  </div>
  <table class="hTNNTI">
    <tbody>
      <tr class="grid"><td>alice_rig2</td><td>175.09 TH/s</td><td>82.01 TH/s</td><td>76.31 TH/s</td><td>79.88 TH/s</td><td>11646</td><td>1 分钟前</td></tr>
      <tr class="grid"><td>dave_02</td><td>87.54 TH/s</td><td>97.13 TH/s</td><td>70.99 TH/s</td><td>73.94 TH/s</td><td>11295</td><td>1 分钟前</td></tr>
      <tr class="grid"><td>alice_main</td><td>200.11 TH/s</td><td>211.89 TH/s</td><td>224.50 TH/s</td><td>177.84 TH/s</td><td>40907</td><td>51 秒前</td></tr>
      <tr class="grid"><td>alice_mini</td><td>75.04 TH/s</td><td>50.74 TH/s</td><td>55.66 TH/s</td><td>57.80 TH/s</td><td>7074</td><td>3 分钟前</td></tr>
    </tbody>
  </table>
</div>
</body></html>
"""

SAMPLE_PAYMENTS_HTML = """
<html><body>
<table class="hTNNTI">
  <tbody>
    <tr class="grid">
      <td>2026-11-15 00:00:01</td>
      <td>5.544981 <b class="blue">PRL</b></td>
      <td><a href="https://explorer.pearlresearch.ai/tx/abc123">abc123</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""


class TestScraperParsing(unittest.TestCase):
    def test_extract_wallet_address(self) -> None:
        raw = DEMO_WALLET
        url = f"https://www.ntminerpool.com/prl/index?back={raw}"
        self.assertEqual(extract_wallet_address(raw), raw)
        self.assertEqual(extract_wallet_address(url), raw)
        with self.assertRaises(ValueError):
            extract_wallet_address("")
        with self.assertRaises(ValueError):
            extract_wallet_address("https://www.ntminerpool.com/prl/index")

    def test_parse_dashboard_and_payments(self) -> None:
        addr = DEMO_WALLET
        data, max_page = parse_dashboard_html(SAMPLE_DASHBOARD_HTML, wallet=addr, fetched_at="2026-11-15 05:00:00")
        self.assertEqual(max_page, 1)
        self.assertEqual(data.online_count, 4)
        self.assertEqual(data.offline_count, 0)
        self.assertEqual(data.live_hashrate, "537.79 TH/s")
        self.assertEqual(data.avg_24h_hashrate, "371.23 TH/s")
        self.assertAlmostEqual(data.prl_price_cny or 0.0, 9.5)
        self.assertAlmostEqual(data.unpaid_prl, 0.433648)
        self.assertAlmostEqual(data.immature_prl, 1.408768)
        self.assertEqual(len(data.workers), 4)
        self.assertEqual(data.workers[2].name, "alice_main")
        self.assertEqual(data.workers[2].shares, 40907)

        pays = parse_payments_html(SAMPLE_PAYMENTS_HTML)
        self.assertEqual(len(pays), 1)
        self.assertEqual(pays[0].paid_at, "2026-11-15 00:00:01")
        self.assertAlmostEqual(pays[0].amount_prl, 5.544981)
        self.assertEqual(pays[0].tx_hash, "abc123")


class TestCalculator(unittest.TestCase):
    def test_classification_priority(self) -> None:
        members = {
            1: MemberInfo(1, "alice", "#10b981"),
            2: MemberInfo(2, "bob", "#3b82f6"),
            3: MemberInfo(3, "bob_vip", "#f59e0b"),
        }
        prefixes = [
            PrefixRule("alice", 1),
            PrefixRule("bob", 2),
            PrefixRule("bob_vip", 3),
        ]
        bindings = [
            WorkerBinding("alice_rig2", 2, wallet_id=None),  # global override
            WorkerBinding("bob_special", 1, wallet_id=9),  # wallet-specific override
        ]

        m1, r1 = classify_worker("bob_rig1", members, prefixes, bindings)
        self.assertEqual(m1.id, 2)
        self.assertTrue(r1.startswith("prefix:"))

        # Longest prefix wins
        m2, _ = classify_worker("bob_vip_01", members, prefixes, bindings)
        self.assertEqual(m2.id, 3)

        # Manual binding overrides prefix
        m3, r3 = classify_worker("alice_rig2", members, prefixes, bindings)
        self.assertEqual(m3.id, 2)
        self.assertEqual(r3, "manual_global")

        # Wallet-specific binding overrides everything on wallet 9
        m4, r4 = classify_worker("bob_special", members, prefixes, bindings, wallet_id=9)
        self.assertEqual(m4.id, 1)
        self.assertEqual(r4, "manual_wallet")

        # Unassigned
        m5, r5 = classify_worker("unknown_99", members, prefixes, bindings)
        self.assertIsNone(m5)
        self.assertEqual(r5, "unassigned")

    def test_suggest_prefix(self) -> None:
        self.assertEqual(suggest_prefix("charlie_01"), "charlie")
        self.assertEqual(suggest_prefix("dave2"), "dave")
        self.assertEqual(suggest_prefix("alice_main"), "alice")

    def test_delta_and_exact_cny_allocation(self) -> None:
        self.assertEqual(compute_worker_delta(1000, 1500), (500, False))
        self.assertEqual(compute_worker_delta(1500, 200, False), (0, True))
        self.assertEqual(compute_worker_delta(1500, 200, True), (200, True))

        # 3-way equal split of 100.00 CNY -> 33.34 + 33.33 + 33.33 = 100.00 exact!
        alloc = allocate_exact_amounts(
            [
                {"member_id": 1, "member_name": "A", "member_color": "#111", "delta_shares": 1000},
                {"member_id": 2, "member_name": "B", "member_color": "#222", "delta_shares": 1000},
                {"member_id": 3, "member_name": "C", "member_color": "#333", "delta_shares": 1000},
            ],
            total_cny=100.0,
            total_prl=10.0,
        )
        total_cny = round(sum(a["cny_earned"] for a in alloc), 2)
        total_prl = round(sum(a["prl_earned"] for a in alloc), 6)
        self.assertEqual(total_cny, 100.0)
        self.assertEqual(total_prl, 10.0)
        self.assertEqual(sorted(a["cny_earned"] for a in alloc), [33.33, 33.33, 33.34])


class TestPublicOnboardingAndSecurity(unittest.TestCase):
    def test_fresh_db_empty_and_onboarding_wizard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "fresh.db")
            try:
                self.assertEqual(db.list_wallets(), [])
                self.assertEqual(db.list_members(), [])
                poller = BackgroundPoller(db, PoolScraper())
                poller.fetch_wallet_once = lambda *a, **kw: None  # type: ignore[method-assign]
                api = DesktopApi(db, poller)

                st0 = api.get_state()
                self.assertTrue(st0["needs_onboarding"])
                self.assertIsNone(st0["active_wallet"])
                self.assertEqual(st0["wallets"], [])
                self.assertGreaterEqual(len(st0["fee_pipeline_templates"]), 1)
                self.assertEqual([p["key"] for p in st0["fee_pipeline_presets"]], ["simple_direct", "multi_hop", "blank"])

                # Complete onboarding wizard with ntminerpool URL, members & multi_hop preset
                res = api.complete_onboarding(
                    {
                        "address_or_url": f"https://www.ntminerpool.com/prl/index?back={DEMO_WALLET}",
                        "alias": "Demo主钱包",
                        "notes": "首次向导创建",
                        "wallet_type": "official",
                        "pipeline_preset": "multi_hop",
                        "members": [
                            {"name": "Alice", "color": "#10b981", "prefixes": "alice"},
                            {"name": "Bob", "color": "#3b82f6", "prefixes": ["bob"]},
                        ],
                    }
                )
                st1 = res["state"]
                self.assertFalse(st1["needs_onboarding"])
                self.assertEqual(st1["active_wallet"]["alias"], "Demo主钱包")
                self.assertEqual({m["name"] for m in st1["members"]}, {"Alice", "Bob"})
                self.assertEqual(len(st1["fee_pipeline_templates"]), 6)

                # Empty friend DB can directly import a host sync bundle without pre-existing wallet
                host_db = Database(Path(tmp) / "host.db")
                try:
                    host_poller = BackgroundPoller(host_db, PoolScraper())
                    host_poller.fetch_wallet_once = lambda *a, **kw: None  # type: ignore[method-assign]
                    host_api = DesktopApi(host_db, host_poller)
                    host_api.complete_onboarding({"address_or_url": DEMO_WALLET, "alias": "主节点钱包"})
                    sync_pkg = host_api.get_wallet_sync_bundle(host_db.list_wallets()[0]["id"])
                    fresh_friend_db = Database(Path(tmp) / "fresh_friend.db")
                    try:
                        ff_poller = BackgroundPoller(fresh_friend_db, PoolScraper())
                        ff_poller.fetch_wallet_once = lambda *a, **kw: None  # type: ignore[method-assign]
                        ff_api = DesktopApi(fresh_friend_db, ff_poller)
                        imp = ff_api.import_sync_payload(sync_pkg["sync_code"], sync_members=True)
                        self.assertTrue(imp["import_result"]["wallet_created"])
                        self.assertFalse(imp["state"]["needs_onboarding"])
                    finally:
                        fresh_friend_db.close()
                finally:
                    host_db.close()
            finally:
                db.close()

    def test_security_hardening_dashboard_xss_and_download_scheme(self) -> None:
        from pearl_split.kernel_manager import KernelUpdateManager

        self.assertNotIn('onclick="selectClient(', WEB_DASHBOARD_HTML)
        self.assertIn("data-cid=", WEB_DASHBOARD_HTML)

        mgr = KernelUpdateManager()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                mgr.start_deploy(
                    kernel_dir=tmp,
                    download_url="file:///C:/evil.zip",
                    asset_name="evil.zip",
                    tag_name="1.0.0",
                    mode="full_deploy",
                )


class TestDatabaseOfflineAndSettlementFlow(unittest.TestCase):
    def _seed_demo_data(self) -> None:
        self.db.add_wallet(DEMO_WALLET, alias="默认主钱包", notes="演示测试钱包", wallet_type="official")
        for name, color, prefixes in (
            ("alice", "#10b981", ["alice"]),
            ("bob", "#3b82f6", ["bob"]),
            ("dave", "#f59e0b", ["dave"]),
            ("charlie", "#a855f7", ["charlie"]),
        ):
            self.db.create_member(name=name, color=color, prefixes=prefixes)

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmpdir.name) / "test.db")
        self._seed_demo_data()

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_offline_cache_and_two_withdrawals(self) -> None:
        wallets = self.db.list_wallets()
        wid = wallets[0]["id"]
        addr = wallets[0]["address"]

        # Fetch 1: alice_main (10000) and bob_rig1 (2000) are online
        f1 = PoolDashboardData(
            wallet=addr,
            fetched_at="2026-11-14 10:00:00",
            online_count=2,
            workers=[
                WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 10000, "1分钟前"),
                WorkerReading("bob_rig1", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 2000, "1分钟前"),
            ],
            payments=[PaymentRecord("2026-11-14 09:00:00", 5.0, "tx0")],
        )
        self.db.save_pool_fetch(wid, f1)

        # Save baseline checkpoint #1
        draft1 = self.db.preview_settlement(wid, prev_checkpoint_id=None)
        cp1 = self.db.save_checkpoint_and_settle(
            wallet_id=wid,
            kind="baseline",
            checkpoint_time="2026-11-14 10:00:00",
            label="初始基准",
            prl_amount=0,
            cny_amount=0,
            tx_hash="",
            notes="",
            prev_checkpoint_id=None,
            worker_entries=draft1["workers"],
        )

        # Fetch 2: bob_rig1 mines up to 5000 while online
        f2 = PoolDashboardData(
            wallet=addr,
            fetched_at="2026-11-14 18:00:00",
            online_count=2,
            workers=[
                WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 16000, "1分钟前"),
                WorkerReading("bob_rig1", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 5000, "1分钟前"),
            ],
        )
        self.db.save_pool_fetch(wid, f2)

        # Fetch 3: bob_rig1 goes offline! Pool page drops bob_rig1 completely (only alice_main left online)
        f3 = PoolDashboardData(
            wallet=addr,
            fetched_at="2026-11-15 04:00:00",
            online_count=1,
            workers=[
                WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 19000, "1分钟前"),
            ],
        )
        self.db.save_pool_fetch(wid, f3)

        workers = self.db.get_wallet_workers(wid)
        bob = next(w for w in workers if w["worker_name"] == "bob_rig1")
        self.assertEqual(bob["is_currently_online"], 0)
        self.assertEqual(bob["last_shares"], 5000)

        # Preview settlement against cp1: alice_main delta = 19000 - 10000 = 9000, bob_rig1 delta = 5000 - 2000 = 3000
        draft2 = self.db.preview_settlement(wid, prev_checkpoint_id=cp1["id"])
        w_map = {w["worker_name"]: w for w in draft2["workers"]}
        self.assertEqual(w_map["alice_main"]["delta_shares"], 9000)
        self.assertEqual(w_map["bob_rig1"]["delta_shares"], 3000)
        self.assertEqual(w_map["bob_rig1"]["share_source"], "offline_cache")

        # Confirm withdrawal of 120.00 CNY -> alice gets 90.00 (75%), bob gets 30.00 (25%)
        cp2 = self.db.save_checkpoint_and_settle(
            wallet_id=wid,
            kind="withdrawal",
            checkpoint_time="2026-11-15 04:00:00",
            label="第一次分红",
            prl_amount=12.0,
            cny_amount=120.0,
            tx_hash="",
            notes="",
            prev_checkpoint_id=cp1["id"],
            worker_entries=draft2["workers"],
        )
        alloc_map = {a["member_name"]: a for a in cp2["allocations"]}
        self.assertAlmostEqual(alloc_map["alice"]["cny_earned"], 90.0)
        self.assertAlmostEqual(alloc_map["bob"]["cny_earned"], 30.0)

        # Update actual CNY later to 100.00 -> alice gets 75.00, bob gets 25.00
        updated = self.db.update_settlement_amounts(cp2["id"], cny_amount=100.0, prl_amount=12.0)
        alloc_map2 = {a["member_name"]: a for a in updated["allocations"]}
        self.assertAlmostEqual(alloc_map2["alice"]["cny_earned"], 75.0)
        self.assertAlmostEqual(alloc_map2["bob"]["cny_earned"], 25.0)

    def test_manual_historical_baseline_preserves_live_cache(self) -> None:
        wallets = self.db.list_wallets()
        wid = wallets[0]["id"]
        addr = wallets[0]["address"]

        # Current live fetch has alice_main at 40912
        self.db.save_pool_fetch(
            wid,
            PoolDashboardData(
                wallet=addr,
                fetched_at="2026-11-15 05:00:00",
                online_count=1,
                workers=[
                    WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 40912, "1分钟前"),
                ],
            ),
        )

        # User manually enters historical baseline from last withdrawal (alice_main=35000, offline bob_old=2000)
        hist_cp = self.db.save_checkpoint_and_settle(
            wallet_id=wid,
            kind="baseline",
            checkpoint_time="2026-11-15 00:00:01",
            label="上次手动记录基准",
            prl_amount=0,
            cny_amount=0,
            tx_hash="",
            notes="",
            prev_checkpoint_id=None,
            worker_entries=[
                {"worker_name": "alice_main", "curr_shares": 35000, "share_source": "manual"},
                {"worker_name": "bob_old", "curr_shares": 2000, "share_source": "manual"},
            ],
        )

        # Live cache for alice_main must remain 40912, while bob_old is seeded at 2000
        workers = {w["worker_name"]: w for w in self.db.get_wallet_workers(wid)}
        self.assertEqual(workers["alice_main"]["last_shares"], 40912)
        self.assertEqual(workers["bob_old"]["last_shares"], 2000)

        draft = self.db.preview_settlement(wid, prev_checkpoint_id=hist_cp["id"])
        w_map = {w["worker_name"]: w for w in draft["workers"]}
        self.assertEqual(w_map["alice_main"]["delta_shares"], 5912)
        self.assertEqual(w_map["bob_old"]["delta_shares"], 0)

        # Edit baseline later to 36000 -> delta becomes 4912
        self.db.update_baseline_checkpoint(
            checkpoint_id=hist_cp["id"],
            checkpoint_time="2026-11-15 00:00:01",
            label="修正基准",
            notes="",
            worker_entries=[
                {"worker_name": "alice_main", "curr_shares": 36000},
                {"worker_name": "bob_old", "curr_shares": 2000},
            ],
        )
        draft2 = self.db.preview_settlement(wid, prev_checkpoint_id=hist_cp["id"])
        w_map2 = {w["worker_name"]: w for w in draft2["workers"]}
        self.assertEqual(w_map2["alice_main"]["delta_shares"], 4912)

    def test_poller_countdown_status(self) -> None:
        import time as _t

        class DummyScraper:
            def fetch_dashboard(self, wallet, include_payments=True):
                return PoolDashboardData(
                    wallet=wallet,
                    fetched_at="2026-11-15 06:00:00",
                    online_count=1,
                    prl_price_cny=10.0,
                    unpaid_prl=2.0,
                    immature_prl=3.0,
                    workers=[
                        WorkerReading("alice_main", "200.5 TH/s", "200 TH/s", "200 TH/s", "180.0 TH/s", 5000, "1分钟前"),
                    ],
                )

        poller = BackgroundPoller(self.db, DummyScraper())
        poller.current_interval_sec = 60
        poller.next_poll_ts = _t.time() + 42.2
        st = poller.get_status()
        self.assertEqual(st["interval_sec"], 60)
        self.assertGreaterEqual(st["remaining_sec"], 41)
        self.assertLessEqual(st["remaining_sec"], 43)

        # Toggling watch mode immediately sets 8s interval
        wid = self.db.list_wallets()[0]["id"]
        poller.set_watch_mode(True, wid)
        st_watch = poller.get_status()
        self.assertTrue(st_watch["watch_mode"])
        self.assertEqual(st_watch["interval_sec"], 8)

        # Manual fetch resets countdown deadline cleanly
        api = DesktopApi(self.db, poller)
        res = api.refresh_wallet(wid)
        st_after = res["state"]["poller"]
        self.assertEqual(st_after["interval_sec"], 8)
        self.assertGreaterEqual(st_after["remaining_sec"], 7)

        # Verify member_live_summary has round_delta_shares, live_hashrate_ths, and est_unpaid_cny
        m_summary = {m["member_name"]: m for m in res["state"]["member_live_summary"]}
        self.assertAlmostEqual(m_summary["alice"]["live_hashrate_ths"], 200.5)
        self.assertEqual(m_summary["alice"]["round_delta_shares"], 5000)
        self.assertAlmostEqual(m_summary["alice"]["est_unpaid_prl"], 5.0)
        self.assertAlmostEqual(m_summary["alice"]["est_unpaid_cny"], 50.0)

    def test_hashrate_parsing(self) -> None:
        self.assertAlmostEqual(parse_hashrate_ths("175.09 TH/s"), 175.09)
        self.assertAlmostEqual(parse_hashrate_ths("500 GH/s"), 0.5)
        self.assertEqual(parse_hashrate_ths("离线"), 0.0)
        self.assertEqual(format_hashrate_ths(200.5), "200.50 TH/s")
        self.assertEqual(format_hashrate_ths(0.0), "0.00 TH/s")

    def test_poller_connection_error_tracking_and_recovery(self) -> None:
        import requests

        class FlakyScraper:
            def __init__(self) -> None:
                self.should_fail = True

            def fetch_dashboard(self, wallet, include_payments=True):
                if self.should_fail:
                    raise requests.exceptions.ConnectTimeout("timed out")
                return PoolDashboardData(
                    wallet=wallet,
                    fetched_at="2026-11-15 07:00:00",
                    online_count=1,
                    unpaid_prl=1.25,
                    immature_prl=2.75,
                    workers=[
                        WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 5000, "1分钟前"),
                    ],
                )

        scraper = FlakyScraper()
        poller = BackgroundPoller(self.db, scraper)
        wid = self.db.list_wallets()[0]["id"]

        st0 = poller.get_status()
        self.assertEqual(st0["conn_state"], "idle")

        with self.assertRaises(RuntimeError):
            poller.fetch_wallet_once(wid)

        st_err = poller.get_status()
        self.assertEqual(st_err["conn_state"], "error")
        self.assertEqual(st_err["consecutive_failures"], 1)
        self.assertIn("超时", st_err["last_error"])
        self.assertEqual(st_err["wallet_status"][str(wid)]["state"], "error")

        scraper.should_fail = False
        poller.fetch_wallet_once(wid)
        st_ok = poller.get_status()
        self.assertEqual(st_ok["conn_state"], "ok")
        self.assertEqual(st_ok["consecutive_failures"], 0)
        self.assertIsNone(st_ok["last_error"])
        self.assertEqual(st_ok["wallet_status"][str(wid)]["state"], "ok")

    def test_parse_block_rewards_and_daily_est(self) -> None:
        html = """
        <html><body>
          <div class="emUnAR"><span>4 部矿机正在挖 PRL ，实时算力 500 TH/s ， 24小时平均算力 450 TH/s 。</span></div>
          <div class="gHXKsE"><h3>预估每日收益</h3><div class="iSSiSGn"><b>5.008112 PRL</b></div></div>
          <div class="online online_n">
            <h2>最近的区块奖励</h2>
            <table class="hTNNTI">
              <tbody>
                <tr><td>2026-11-15 16:00:00</td><td>980102</td><td>12.34 T</td><td>50.00 PRL</td><td>0.412345 PRL</td><td>待确认</td></tr>
                <tr><td>2026-11-15 15:40:00</td><td>980101</td><td>12.30 T</td><td>50.00 PRL</td><td>0.510000 PRL</td><td>已确认</td></tr>
              </tbody>
            </table>
          </div>
        </body></html>
        """
        parsed, _ = parse_dashboard_html(html, "prl1x")
        self.assertAlmostEqual(parsed.est_daily_prl, 5.008112)
        self.assertEqual(len(parsed.block_rewards), 2)
        self.assertEqual(parsed.block_rewards[0].height, 980102)
        self.assertAlmostEqual(parsed.block_rewards[0].earned_prl, 0.412345)
        self.assertEqual(parsed.block_rewards[0].status, "待确认")

        wid = self.db.list_wallets()[0]["id"]
        self.db.save_pool_fetch(wid, parsed)
        newer = PoolDashboardData(
            wallet="prl1x",
            fetched_at="2026-11-15 16:20:00",
            est_daily_prl=5.5,
            block_rewards=[
                BlockRewardRecord("2026-11-15 16:20:00", 980103, "12.40 T", 50.0, 0.6, "已确认"),
                BlockRewardRecord("2026-11-15 16:00:00", 980102, "12.34 T", 50.0, 0.412345, "已确认"),
            ],
        )
        self.db.save_pool_fetch(wid, newer)
        ov = next(w for w in self.db.list_wallets() if w["id"] == wid)["overview"]
        self.assertAlmostEqual(ov["est_daily_prl"], 5.5)
        heights = [b["height"] for b in ov["block_rewards"]]
        self.assertEqual(heights, [980103, 980102, 980101])
        self.assertEqual(ov["block_rewards"][1]["status"], "已确认")

    def test_srb_parser_and_bat_analyzer(self) -> None:
        parser = SrbTelemetryParser()
        sample_lines = [
            "[2026-11-15 16:00:00] SRBMiner-MULTI v2.6.8",
            "[2026-11-15 16:00:01] Algorithm: pearlhash | Connected to pool: stratum+tcp://pool.ntminerpool.com:3333",
            "[2026-11-15 16:00:02] Worker: alice_rig2",
            "\x1b[36m[2026-11-15 16:00:10] GPU0 [RTX 4070]: 120.00 TH/s t=58C f=60% p=180W | GPU1 [RTX 4060]: 80.00 TH/s t=55C f=50% p=120W | Total: 200.00 TH/s\x1b[0m",
            "[2026-11-15 16:00:15] Share accepted (42ms) GPU0",
            "[2026-11-15 16:00:20] GPU0: 122.00 TH/s | GPU1: 78.00 TH/s | Total: 200.00 TH/s",
            "[2026-11-15 16:00:25] Share rejected (stale share)",
            "[2026-11-15 16:00:30] Total: 210.00 TH/s, Total power: 310 W",
        ]
        for line in sample_lines:
            parser.feed_line(line)

        rep = parser.get_report()
        self.assertEqual(rep["miner_version"], "SRBMiner-MULTI v2.6.8")
        self.assertEqual(rep["algorithm"], "pearlhash")
        self.assertEqual(rep["worker_name"], "alice_rig2")
        self.assertAlmostEqual(rep["current_hashrate_ths"], 210.0)
        self.assertGreater(rep["session_avg_ths"], 200.0)
        self.assertGreater(rep["stability_pct"], 90.0)
        self.assertEqual(rep["accepted_shares"], 1)
        self.assertEqual(rep["rejected_shares"], 1)
        self.assertAlmostEqual(rep["accept_rate_pct"], 50.0)
        self.assertAlmostEqual(rep["last_latency_ms"], 42.0)
        self.assertEqual(len(rep["devices"]), 2)
        self.assertEqual(rep["devices"][0]["name"], "RTX 4070")
        self.assertAlmostEqual(rep["total_power_w"], 310.0)

        with tempfile.TemporaryDirectory() as tmp:
            bat_path = Path(tmp) / "start_prl.bat"
            original_bytes = (
                b"@echo off\r\n"
                b'start "miner" %~dp0SRBMiner-MULTI.exe --algorithm pearlhash '
                b"--pool stratum+tcp://pool.ntminerpool.com:3333 --wallet prl1test --worker alice_main\r\n"
                b"pause\r\n"
            )
            bat_path.write_bytes(original_bytes)
            parsed_bat = parse_bat_file(bat_path)
            self.assertEqual(parsed_bat["algorithm"], "pearlhash")
            self.assertEqual(parsed_bat["worker"], "alice_main")
            self.assertTrue(parsed_bat["has_pause"])
            self.assertTrue(parsed_bat["uses_start"])

            wrapped = build_wrapped_bat_content(bat_path, Path(tmp) / "out.log")
            self.assertIn("chcp 65001 >nul", wrapped)
            self.assertIn("--log-file", wrapped)
            self.assertNotIn("pause", wrapped.lower())
            self.assertIn(str(Path(tmp)), wrapped)
            self.assertEqual(bat_path.read_bytes(), original_bytes)

    def test_diff_console_lines(self) -> None:
        p1 = ["line1", "line2", "line3", "line4"]
        p2 = ["line3", "line4", "line5", "line6"]
        self.assertEqual(diff_console_lines(p1, p2), ["line5", "line6"])
        self.assertEqual(diff_console_lines(p1, p1), [])
        self.assertEqual(diff_console_lines([], p2), p2)

    def test_collector_server_http_ingest_and_powershell_fallback(self) -> None:
        import socket
        import requests

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            ctrl = CollectorServerController(Path(tmp) / "col.db")
            ctrl.start(host="127.0.0.1", port=free_port, token="secret123")
            try:
                base = f"http://127.0.0.1:{free_port}"
                r_unauth = requests.get(f"{base}/api/v1/clients", timeout=3)
                self.assertEqual(r_unauth.status_code, 401)

                payload = {
                    "client_id": "rig-alpha",
                    "client_name": "alice_rig2",
                    "worker_name": "alice_rig2",
                    "log_lines": [
                        "[2026-11-15 17:00:01] Algorithm: pearlhash",
                        "[2026-11-15 17:00:05] GPU0 [RTX 4070]: 150.00 TH/s p=165W | Total: 150.00 TH/s",
                        "[2026-11-15 17:00:08] Share accepted (35ms) GPU0",
                    ],
                }
                r_post = requests.post(
                    f"{base}/api/v1/ingest",
                    json=payload,
                    headers={"X-Collector-Token": "secret123"},
                    timeout=3,
                )
                self.assertEqual(r_post.status_code, 200)

                r_list = requests.get(
                    f"{base}/api/v1/clients",
                    headers={"X-Collector-Token": "secret123"},
                    timeout=3,
                )
                self.assertEqual(r_list.status_code, 200)
                data = r_list.json()
                self.assertEqual(data["clients_total"], 1)
                self.assertAlmostEqual(data["total_real_hashrate_ths"], 150.0)
                self.assertEqual(data["total_accepted_shares"], 1)

                r_det = requests.get(
                    f"{base}/api/v1/clients/rig-alpha",
                    headers={"X-Collector-Token": "secret123"},
                    timeout=3,
                )
                self.assertEqual(r_det.status_code, 200)
                det = r_det.json()
                self.assertEqual(len(det["logs"]), 3)
                self.assertEqual(det["client"]["devices"][0]["name"], "RTX 4070")

                r_del = requests.delete(
                    f"{base}/api/v1/clients/rig-alpha",
                    headers={"X-Collector-Token": "secret123"},
                    timeout=3,
                )
                self.assertEqual(r_del.status_code, 200)
                self.assertEqual(ctrl.get_store().list_clients()["clients_total"], 0)
            finally:
                ctrl.stop()

    def test_export_bundles_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            srv_res = export_server_bundle(Path(tmp) / "srv")
            srv_names = {Path(p).name for p in srv_res["files"]}
            self.assertIn("collector_server.py", srv_names)
            self.assertIn("srb_parser.py", srv_names)
            self.assertIn("alert_engine.py", srv_names)

            cli_res = export_client_kit(
                Path(tmp) / "cli",
                server_url="http://192.0.2.5:8765",
                token="tok99",
                worker_hint="bob_rig1",
            )
            cli_names = {Path(p).name for p in cli_res["files"]}
            self.assertIn("pearl_srb_reporter.ps1", cli_names)
            self.assertIn("一键启动SRBMiner并上报(免Python).bat", cli_names)
            self.assertIn("collector_client.py", cli_names)
            bat_txt = (Path(tmp) / "cli" / "一键启动SRBMiner并上报(免Python).bat").read_text(encoding="utf-8")
            self.assertIn("http://192.0.2.5:8765", bat_txt)
            self.assertIn("tok99", bat_txt)
            self.assertIn("bob_rig1", bat_txt)

    def test_srbminer_v3_8_wrapped_console_table_parsing(self) -> None:
        raw_console_rows = [
            "[2026-11-15 18:29:11] GPU1[t0] share accepted [  731ms] [pearlhash][0]",
            "[2026-11-15 18:29:26] GPU1[t0] share accepted [  729ms] [pearlhash][0]",
            "[2026-11-15 18:29:32] Job received [4091773c_2400000  ] block height 143542 [diff: 2400000] [pearlhash][0]",
            "[2026-11-15 18:29:35] ------------------------------------[ SRBMiner-MULTI v3.8.8 - Statistics ]---------------- Uptime:",
            " 0d 00:02:00",
            "[2026-11-15 18:29:35] ----------------------------------------------[ A0 - pearlhash ]----------------------------------",
            "------------",
            "[2026-11-15 18:29:35]  ID Name                        Hashrate    Power     Eff     Fan  Temp        CClk   MClk       A",
            "     R    HW",
            "[2026-11-15 18:29:35] --------------------------------------------------------------------------------------------------",
            "------------",
            "[2026-11-15 18:29:35] #1  RTX 5080                 215.49 TH/s   225.5W   0.956     31%   52C        2760  14499       3",
            "     0     0",
            "[2026-11-15 18:29:35] --------------------------------------------------------------------------------------------------",
            "------------",
            "[2026-11-15 18:29:35]     Hashrate                 215.49 TH/s   225.5W   0.956                                        3",
            "     0     0",
            "[2026-11-15 18:29:35] --------------------------------------------------------------------------------------------------",
            "------------",
            "[2026-11-15 18:29:35]     1 min                    209.12 TH/s",
            "[2026-11-15 18:29:35]     15 min                   204.56 TH/s",
            "[2026-11-15 18:29:35]     1 hr                     204.56 TH/s",
            "[2026-11-15 18:29:35]     6 hr                     204.56 TH/s",
            "[2026-11-15 18:29:35] -------------------------------------------------------------------------------- Share stats [100.",
            "00% / 0.00%]",
            "[2026-11-15 18:29:55] GPU1[t0] share accepted [  542ms] [pearlhash][0]",
            "[2026-11-15 18:29:57] GPU1[t0] share accepted [  549ms] [pearlhash][0]",
            "[2026-11-15 18:29:58] GPU1[t0] share accepted [  385ms] [pearlhash][0]",
            "[2026-11-15 18:30:05] ------------------------------------[ SRBMiner-MULTI v3.8.8 - Statistics ]---------------- Uptime:",
            " 0d 00:02:30",
            "[2026-11-15 18:30:05] ----------------------------------------------[ A0 - pearlhash ]----------------------------------",
            "------------",
            "[2026-11-15 18:30:05] #1  RTX 5080                 212.19 TH/s   222.8W   0.952     31%   54C        2760  14499       6",
            "     0     0",
            "[2026-11-15 18:30:05]     Hashrate                 212.19 TH/s   222.8W   0.952                                        6",
            "     0     0",
            "[2026-11-15 18:30:05]     1 min                    213.84 TH/s",
            "[2026-11-15 18:30:05]     15 min                   206.12 TH/s",
            "[2026-11-15 18:30:05]     1 hr                     206.12 TH/s",
            "[2026-11-15 18:30:05]     6 hr                     206.12 TH/s",
            "[2026-11-15 18:30:05] -------------------------------------------------------------------------------- Share stats [100.",
            "00% / 0.00%]",
        ]
        unwrapped = unwrap_console_rows(raw_console_rows, console_width=120)
        self.assertTrue(any("Uptime: 0d 00:02:30" in ln for ln in unwrapped))
        self.assertTrue(any("Share stats [100.00% / 0.00%]" in ln for ln in unwrapped))
        self.assertTrue(any("212.19 TH/s" in ln and "0     0" in ln for ln in unwrapped))

        parser = SrbTelemetryParser()
        for ln in unwrapped:
            parser.feed_line(ln)

        rep = parser.get_report()
        self.assertEqual(rep["miner_version"], "SRBMiner-MULTI v3.8.8")
        self.assertEqual(rep["algorithm"], "pearlhash")
        self.assertEqual(rep["miner_uptime_text"], "0d 00:02:30")
        self.assertGreaterEqual(rep["uptime_sec"], 150)
        self.assertAlmostEqual(rep["current_hashrate_ths"], 212.19)
        self.assertEqual(rep["sample_count"], 2)
        self.assertAlmostEqual(rep["session_avg_ths"], 213.84)
        self.assertAlmostEqual(rep["miner_avg_1m_ths"], 213.84)
        self.assertAlmostEqual(rep["miner_avg_15m_ths"], 206.12)
        self.assertAlmostEqual(rep["miner_avg_1h_ths"], 206.12)
        self.assertAlmostEqual(rep["miner_avg_6h_ths"], 206.12)
        self.assertAlmostEqual(rep["total_power_w"], 222.8)
        self.assertAlmostEqual(rep["efficiency_th_per_w"], 0.952)
        self.assertEqual(rep["accepted_shares"], 6)
        self.assertEqual(rep["rejected_shares"], 0)
        self.assertEqual(rep["hw_errors"], 0)
        self.assertAlmostEqual(rep["avg_latency_ms"], 587.2)

        self.assertEqual(len(rep["devices"]), 1)
        gpu = rep["devices"][0]
        self.assertEqual(gpu["id"], "GPU1")
        self.assertEqual(gpu["name"], "RTX 5080")
        self.assertAlmostEqual(gpu["hashrate_ths"], 212.19)
        self.assertAlmostEqual(gpu["avg_hashrate_ths"], 213.84)
        self.assertEqual(gpu["fan_pct"], 31)
        self.assertEqual(gpu["temp_c"], 54)
        self.assertAlmostEqual(gpu["power_w"], 222.8)
        self.assertAlmostEqual(gpu["eff_value"], 0.952)
        self.assertEqual(gpu["accepted_shares"], 6)

    def test_settlement_draft_save_resume_and_consume(self) -> None:
        wallets = self.db.list_wallets()
        wid = wallets[0]["id"]
        addr = wallets[0]["address"]

        self.db.save_pool_fetch(
            wid,
            PoolDashboardData(
                wallet=addr,
                fetched_at="2026-11-15 01:00:00",
                online_count=1,
                workers=[
                    WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 30000, "1分钟前"),
                ],
            ),
        )
        base_preview = self.db.preview_settlement(wid, prev_checkpoint_id=None)
        base_cp = self.db.save_checkpoint_and_settle(
            wallet_id=wid,
            kind="baseline",
            checkpoint_time="2026-11-15 01:00:00",
            label="初始基准",
            prl_amount=0.0,
            cny_amount=0.0,
            tx_hash="",
            notes="",
            prev_checkpoint_id=None,
            worker_entries=base_preview["workers"],
        )

        col_ctrl = CollectorServerController(Path(self.tmpdir.name) / "col_draft.db")
        try:
            api = DesktopApi(
                self.db,
                BackgroundPoller(self.db, PoolScraper()),
                collector_server=col_ctrl,
            )

            saved_res = api.save_settlement_draft(
                {
                    "wallet_id": wid,
                    "title": "第2次分红草稿-等charlie出差开机",
                    "prev_checkpoint_id": base_cp["id"],
                    "checkpoint_time": "2026-11-15 12:00:00",
                    "label": "等charlie回来",
                    "prl_amount": 10.0,
                    "cny_amount": 100.0,
                    "notes": "先冻结线上矿机，charlie_01 暂为 0",
                    "worker_entries": [
                        {
                            "worker_name": "alice_main",
                            "prev_shares": 30000,
                            "curr_shares": 36000,
                            "share_source": "live",
                            "hashrate": "210 TH/s",
                        },
                        {
                            "worker_name": "charlie_01",
                            "prev_shares": 0,
                            "curr_shares": 0,
                            "share_source": "offline_missing",
                            "hashrate": "离线(无记录)",
                        },
                    ],
                }
            )
            draft = saved_res["draft"]
            draft_id = draft["id"]
            self.assertEqual(draft["offline_count"], 1)
            self.assertEqual(draft["offline_names"], ["charlie_01"])
            self.assertEqual(draft["total_delta_shares"], 6000)

            st1 = api.get_state(wid)
            self.assertEqual(len(st1["drafts"]), 1)
            self.assertEqual(st1["drafts"][0]["id"], draft_id)

            self.db.upsert_manual_worker_cache(wid, "charlie_01", 2000)
            self.db.upsert_manual_worker_cache(wid, "alice_main", 40907)
            self.db.upsert_manual_worker_cache(wid, "dave_new", 500)

            loaded = api.load_settlement_draft(draft_id)
            w_loaded = {w["worker_name"]: w for w in loaded["workers"]}
            self.assertEqual(w_loaded["alice_main"]["curr_shares"], 36000)
            self.assertEqual(w_loaded["charlie_01"]["curr_shares"], 0)
            self.assertTrue(w_loaded["charlie_01"]["has_newer_cache"])
            self.assertEqual(w_loaded["charlie_01"]["live_cache_shares"], 2000)
            self.assertTrue(w_loaded["alice_main"]["has_newer_cache"])
            self.assertEqual(w_loaded["alice_main"]["live_cache_shares"], 40907)
            self.assertEqual(loaded["syncable_offline_count"], 1)
            self.assertEqual(len(loaded["new_cache_workers"]), 1)
            self.assertEqual(loaded["new_cache_workers"][0]["worker_name"], "dave_new")

            updated_res = api.save_settlement_draft(
                {
                    "wallet_id": wid,
                    "draft_id": draft_id,
                    "title": "第2次分红草稿-已补录charlie_01",
                    "prev_checkpoint_id": base_cp["id"],
                    "checkpoint_time": "2026-11-15 12:00:00",
                    "label": "已补录charlie_01",
                    "prl_amount": 10.0,
                    "cny_amount": 100.0,
                    "notes": "charlie_01 已补录 2000",
                    "worker_entries": [
                        {
                            "worker_name": "alice_main",
                            "prev_shares": 30000,
                            "curr_shares": 36000,
                            "share_source": "live",
                            "hashrate": "210 TH/s",
                        },
                        {
                            "worker_name": "charlie_01",
                            "prev_shares": 0,
                            "curr_shares": 2000,
                            "share_source": "manual",
                            "hashrate": "离线(手动补录)",
                        },
                    ],
                }
            )
            self.assertEqual(updated_res["draft"]["id"], draft_id)
            self.assertEqual(updated_res["draft"]["total_delta_shares"], 8000)
            self.assertEqual(len(api.get_state(wid)["drafts"]), 1)

            settle_res = api.save_checkpoint_and_settle(
                {
                    "wallet_id": wid,
                    "kind": "withdrawal",
                    "checkpoint_time": "2026-11-15 12:00:00",
                    "label": "最终发放",
                    "prl_amount": 10.0,
                    "cny_amount": 100.0,
                    "tx_hash": "",
                    "notes": "由草稿恢复并正式入账",
                    "prev_checkpoint_id": base_cp["id"],
                    "consume_draft_id": draft_id,
                    "worker_entries": [
                        {
                            "worker_name": "alice_main",
                            "prev_shares": 30000,
                            "curr_shares": 36000,
                            "share_source": "live",
                        },
                        {
                            "worker_name": "charlie_01",
                            "prev_shares": 0,
                            "curr_shares": 2000,
                            "share_source": "manual",
                        },
                    ],
                }
            )
            alloc_map = {a["member_name"]: a for a in settle_res["saved"]["allocations"]}
            self.assertAlmostEqual(alloc_map["alice"]["cny_earned"], 75.0)
            self.assertAlmostEqual(alloc_map["charlie"]["cny_earned"], 25.0)
            self.assertEqual(settle_res["state"]["drafts"], [])

            tmp_draft = api.save_settlement_draft(
                {
                    "wallet_id": wid,
                    "title": "临时废弃草稿",
                    "cny_amount": 50.0,
                    "worker_entries": [{"worker_name": "alice_main", "prev_shares": 0, "curr_shares": 100}],
                }
            )["draft"]
            self.assertEqual(len(api.get_state(wid)["drafts"]), 1)
            after_del = api.delete_settlement_draft(tmp_draft["id"], wid)
            self.assertEqual(after_del["drafts"], [])
        finally:
            col_ctrl.stop()

    def test_collector_docker_bundle_and_env_config(self) -> None:
        import os
        import socket
        import requests

        with tempfile.TemporaryDirectory() as tmp:
            srv_dir = Path(tmp) / "pearl_collector_server"
            res = export_server_bundle(srv_dir)
            names = {Path(p).name for p in res["files"]}
            for expected in (
                "Dockerfile",
                "docker-compose.yml",
                "docker-compose.nobuild.yml",
                ".dockerignore",
                ".keep",
                "collector_server.py",
                "srb_parser.py",
                "alert_engine.py",
                "start_collector_server.sh",
                "群晖Container_Manager部署教程.txt",
            ):
                self.assertIn(expected, names)

            self.assertTrue((srv_dir / "data").is_dir())
            for lf_name in ("Dockerfile", "docker-compose.yml", "docker-compose.nobuild.yml", "start_collector_server.sh"):
                raw_bytes = (srv_dir / lf_name).read_bytes()
                self.assertNotIn(b"\r\n", raw_bytes, f"{lf_name} must use Unix LF line endings")

            dockerfile_txt = (srv_dir / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("alert_engine.py", dockerfile_txt)
            nobuild_txt = (srv_dir / "docker-compose.nobuild.yml").read_text(encoding="utf-8")
            self.assertIn("alert_engine.py", nobuild_txt)

            compose_txt = (srv_dir / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertIn("PEARL_COLLECTOR_DB=/data/collector_telemetry.db", compose_txt)
            self.assertIn("./data:/data", compose_txt)
            self.assertIn("8765:8765", compose_txt)

            env_backup = {k: os.environ.get(k) for k in ("PEARL_COLLECTOR_HOST", "PEARL_COLLECTOR_PORT", "PEARL_COLLECTOR_TOKEN", "PEARL_COLLECTOR_DB")}
            try:
                os.environ["PEARL_COLLECTOR_HOST"] = "127.0.0.1"
                os.environ["PEARL_COLLECTOR_PORT"] = "9876"
                os.environ["PEARL_COLLECTOR_TOKEN"] = "syno-tok"
                os.environ["PEARL_COLLECTOR_DB"] = "/data/custom.db"
                parsed = build_cli_parser().parse_args([])
                self.assertEqual(parsed.host, "127.0.0.1")
                self.assertEqual(parsed.port, 9876)
                self.assertEqual(parsed.token, "syno-tok")
                self.assertEqual(parsed.db, "/data/custom.db")
            finally:
                for k, v in env_backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            ctrl = CollectorServerController(Path(tmp) / "nested" / "col.db")
            ctrl.start(host="127.0.0.1", port=free_port, token="proxy-secret")
            try:
                base = f"http://127.0.0.1:{free_port}"
                r_health = requests.get(f"{base}/health", timeout=3)
                self.assertEqual(r_health.status_code, 200)
                self.assertTrue(r_health.json()["auth_required"])

                r_post = requests.post(
                    f"{base}/api/v1/ingest",
                    json={"client_id": "nas-rig", "client_name": "bob_01", "log_lines": []},
                    headers={"X-Collector-Token": "proxy-secret", "X-Forwarded-For": "203.0.113.45, 198.51.100.1"},
                    timeout=3,
                )
                self.assertEqual(r_post.status_code, 200)
                clients = ctrl.get_store().list_clients()["clients"]
                self.assertEqual(clients[0]["remote_ip"], "203.0.113.45")
            finally:
                ctrl.stop()

    def test_kernel_manager_bat_github_check_and_selective_deploy(self) -> None:
        import hashlib
        import io
        import socket
        import threading
        import time
        import zipfile
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest.mock import patch

        from pearl_split.kernel_manager import (
            _extract_archive_file,
            compare_versions,
            extract_bat_config,
            fetch_github_releases,
            inspect_kernel_dir,
            save_bat_file,
        )

        self.assertEqual(compare_versions("3.8.8", "3.8.9"), -1)
        self.assertEqual(compare_versions("v3.8.8", "3.8.8"), 0)
        self.assertEqual(compare_versions("3.9.0", "3.8.8"), 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kdir = root / "SRBMiner-Multi-3-8-8"
            kdir.mkdir(parents=True)
            old_exe = kdir / "SRBMiner-MULTI.exe"
            old_exe.write_bytes(b"OLD_EXE_388")

            orig_bat = kdir / "start_custom.bat"
            orig_bat.write_text(
                "@echo off\r\n"
                "set GPU_FORCE_64BIT_PTR=1\r\n"
                ":loop\r\n"
                "SRBMiner-MULTI.exe --algorithm pearlhash --pool stratum+tcp://pool.ntminerpool.com:3333 "
                "--wallet prl1wallet --worker alice01 --disable-cpu --api-enable --api-port 21555 "
                "--gpu-cclock0 2450 --gpu-moffset0 1000\r\n"
                "goto loop\r\n",
                encoding="utf-8",
            )
            parsed = extract_bat_config(orig_bat)
            self.assertEqual(parsed["algorithm"], "pearlhash")
            self.assertEqual(parsed["pool"], "stratum+tcp://pool.ntminerpool.com:3333")
            self.assertEqual(parsed["wallet"], "prl1wallet")
            self.assertEqual(parsed["worker"], "alice01")
            self.assertTrue(parsed["disable_cpu"])
            self.assertTrue(parsed["api_enable"])
            self.assertEqual(parsed["api_port"], "21555")
            self.assertTrue(parsed["has_loop"])
            self.assertIn("set GPU_FORCE_64BIT_PTR=1", parsed["preamble"])
            self.assertIn("--gpu-cclock0", parsed["extra_args"])
            self.assertIn("--gpu-moffset0", parsed["extra_args"])

            parsed["worker"] = "alice02"
            save_res = save_bat_file(orig_bat, cfg=parsed, backup_existing=True)
            self.assertTrue(Path(save_res["backup_path"]).is_file())
            updated_bytes = orig_bat.read_bytes()
            self.assertIn(b"\r\n", updated_bytes)
            self.assertIn(b"--worker alice02", updated_bytes)
            self.assertIn(b"--gpu-cclock0 2450", updated_bytes)
            self.assertIn(b"set GPU_FORCE_64BIT_PTR=1", updated_bytes)

            info_before = inspect_kernel_dir(kdir, linked_bat_path=str(orig_bat))
            self.assertTrue(info_before["exe_exists"])
            self.assertEqual(info_before["exe_version"], "3.8.8")

            fake_releases_payload = [
                {
                    "tag_name": "3.8.9",
                    "name": "SRBMiner-MULTI v3.8.9",
                    "published_at": "2026-11-15T12:00:00Z",
                    "html_url": "https://github.com/doktor83/SRBMiner-Multi/releases/tag/3.8.9",
                    "body": "- Improved pearlhash efficiency by 2%\n- Fixed telemetry stability",
                    "prerelease": False,
                    "draft": False,
                    "assets": [
                        {
                            "name": "SRBMiner-Multi-3-8-9-Linux.tar.xz",
                            "size": 1000,
                            "download_count": 5,
                            "browser_download_url": "https://example.invalid/linux.tar.xz",
                        },
                        {
                            "name": "SRBMiner-Multi-3-8-9-win64.zip",
                            "size": 2048,
                            "download_count": 50,
                            "browser_download_url": "https://example.invalid/win64.zip",
                        },
                    ],
                }
            ]

            class FakeResp:
                status_code = 200
                text = ""

                def raise_for_status(self) -> None:
                    return None

                def json(self) -> list:
                    return fake_releases_payload

            with patch("pearl_split.kernel_manager.requests.get", return_value=FakeResp()):
                rel = fetch_github_releases("https://github.com/doktor83/SRBMiner-Multi")
                self.assertEqual(rel["repo"], "doktor83/SRBMiner-Multi")
                self.assertEqual(rel["latest"]["version"], "3.8.9")
                self.assertIn("Improved pearlhash efficiency", rel["latest"]["body"])
                self.assertEqual(rel["latest"]["asset"]["name"], "SRBMiner-Multi-3-8-9-win64.zip")

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("SRBMiner-Multi-3-8-9/SRBMiner-MULTI.exe", b"NEW_EXE_389_BINARY")
                zf.writestr("SRBMiner-Multi-3-8-9/WinRing0x64.sys", b"DRIVER_BYTES")
                zf.writestr("SRBMiner-Multi-3-8-9/start-mining-sample.bat", b"@echo off\r\necho sample\r\n")
            zip_bytes = zip_buf.getvalue()
            zip_sha256 = "sha256:" + hashlib.sha256(zip_bytes).hexdigest()

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                srv_port = sock.getsockname()[1]

            class ZipHandler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_GET(self) -> None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(zip_bytes)))
                    self.end_headers()
                    self.wfile.write(zip_bytes)

            httpd = HTTPServer(("127.0.0.1", srv_port), ZipHandler)
            srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            srv_thread.start()
            col_ctrl = CollectorServerController(root / "col_kernel.db")
            try:
                api = DesktopApi(
                    self.db,
                    BackgroundPoller(self.db, PoolScraper()),
                    collector_server=col_ctrl,
                )
                dl_url = f"http://127.0.0.1:{srv_port}/SRBMiner-Multi-3-8-9-win64.zip"

                api.start_kernel_deploy(
                    {
                        "kernel_dir": str(kdir),
                        "download_url": dl_url,
                        "asset_name": "SRBMiner-Multi-3-8-9-win64.zip",
                        "tag_name": "3.8.9",
                        "digest": zip_sha256,
                        "mode": "exe_only",
                        "stop_running": False,
                        "backup_exe": True,
                    }
                )
                for _ in range(50):
                    st = api.get_kernel_task_status()
                    if st["done"]:
                        break
                    time.sleep(0.05)
                self.assertEqual(st["phase"], "done", st.get("error"))
                self.assertTrue(st["archive_deleted"])
                self.assertEqual(old_exe.read_bytes(), b"NEW_EXE_389_BINARY")
                self.assertEqual((kdir / "SRBMiner-MULTI.exe.bak").read_bytes(), b"OLD_EXE_388")
                self.assertFalse((kdir / "WinRing0x64.sys").exists())
                self.assertFalse((kdir / "start-mining-sample.bat").exists())
                self.assertEqual(list(kdir.glob("*.zip")), [])
                self.assertEqual(inspect_kernel_dir(kdir)["exe_version"], "3.8.9")

                new_pc_dir = root / "fresh_pc_miner"
                api.start_kernel_deploy(
                    {
                        "kernel_dir": str(new_pc_dir),
                        "download_url": dl_url,
                        "asset_name": "SRBMiner-Multi-3-8-9-win64.zip",
                        "tag_name": "3.8.9",
                        "digest": zip_sha256,
                        "mode": "full_deploy",
                        "stop_running": False,
                        "backup_exe": False,
                    }
                )
                for _ in range(50):
                    st2 = api.get_kernel_task_status()
                    if st2["done"]:
                        break
                    time.sleep(0.05)
                self.assertEqual(st2["phase"], "done", st2.get("error"))
                self.assertTrue(st2["archive_deleted"])
                self.assertEqual((new_pc_dir / "SRBMiner-MULTI.exe").read_bytes(), b"NEW_EXE_389_BINARY")
                self.assertEqual((new_pc_dir / "WinRing0x64.sys").read_bytes(), b"DRIVER_BYTES")
                self.assertTrue((new_pc_dir / "start-mining-sample.bat").is_file())
                self.assertEqual(list(new_pc_dir.glob("*.zip")), [])

                evil_zip = root / "evil.zip"
                with zipfile.ZipFile(evil_zip, "w") as zf:
                    zf.writestr("../../outside.exe", b"bad")
                with self.assertRaises(ValueError):
                    _extract_archive_file(evil_zip, root / "safe_target", mode="full_deploy")
            finally:
                httpd.shutdown()
                httpd.server_close()
                col_ctrl.stop()

    def test_chinese_filenames_and_encoding_safety(self) -> None:
        import json
        import zipfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from pearl_split.kernel_manager import _extract_archive_file
        from pearl_split.miner_binder import scan_running_srbminer_processes
        from pearl_split.srb_parser import decode_text_bytes

        sample = "C:\\挖矿目录\\start - 副本.bat"
        self.assertEqual(decode_text_bytes(sample.encode("utf-16")), sample)
        self.assertEqual(decode_text_bytes(sample.encode("utf-8-sig")), sample)
        self.assertEqual(decode_text_bytes(sample.encode("utf-8")), sample)
        self.assertEqual(decode_text_bytes(sample.encode("gb18030")), sample)

        cim_rows = [
            {
                "ProcessId": 1001,
                "ParentProcessId": 500,
                "Name": "cmd.exe",
                "ExecutablePath": r"C:\Windows\system32\cmd.exe",
                "CommandLine": r'C:\Windows\system32\cmd.exe /c ""D:\我的矿机\SRBMiner\start - 副本.bat" "',
            },
            {
                "ProcessId": 1002,
                "ParentProcessId": 1001,
                "Name": "SRBMiner-MULTI.exe",
                "ExecutablePath": r"D:\我的矿机\SRBMiner\SRBMiner-MULTI.exe",
                "CommandLine": "SRBMiner-MULTI.exe --algorithm pearlhash --worker alice_main",
            },
            {
                "ProcessId": 1003,
                "ParentProcessId": 500,
                "Name": "python.exe",
                "ExecutablePath": r"C:\Python\python.exe",
                "CommandLine": "python -c scan_running_srbminer_processes",
            },
        ]
        for enc in ("utf-8", "cp936"):
            fake_proc = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(cim_rows, ensure_ascii=False).encode(enc),
                stderr=b"",
            )
            with patch("pearl_split.miner_binder.subprocess.run", return_value=fake_proc):
                scanned = scan_running_srbminer_processes()
                names = [p["name"] for p in scanned]
                self.assertNotIn("python.exe", names)
                srb = next(p for p in scanned if p["name"] == "SRBMiner-MULTI.exe")
                self.assertEqual(srb["parent_bat"], r"D:\我的矿机\SRBMiner\start - 副本.bat")

        with tempfile.TemporaryDirectory() as tmp:
            import io

            root = Path(tmp)
            zpath = root / "中文包.zip"
            out_dir = root / "解压目录"
            ph_dir = b"12345678"
            gbk_dir = "内核目录".encode("gbk")
            ph_bat = b"1234 - 1234.bat"
            gbk_bat = "启动 - 副本.bat".encode("gbk")
            self.assertEqual(len(ph_dir), len(gbk_dir))
            self.assertEqual(len(ph_bat), len(gbk_bat))

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"{ph_dir.decode('ascii')}/SRBMiner-MULTI.exe", b"EXE_BYTES")
                zf.writestr(f"{ph_dir.decode('ascii')}/{ph_bat.decode('ascii')}", b"@echo off\r\n")
            raw_zip = buf.getvalue().replace(ph_dir, gbk_dir).replace(ph_bat, gbk_bat)
            zpath.write_bytes(raw_zip)

            res = _extract_archive_file(zpath, out_dir, mode="full_deploy", backup_exe=False)
            self.assertTrue(res["exe_found"])
            self.assertEqual((out_dir / "SRBMiner-MULTI.exe").read_bytes(), b"EXE_BYTES")
            self.assertTrue((out_dir / "启动 - 副本.bat").is_file())

    def test_custom_fee_pipeline_conversions_trades_and_multi_asset_dust(self) -> None:
        from pearl_split.calculator import (
            normalize_fee_payload,
            normalize_pipeline_steps,
            normalize_wallet_type,
            summarize_fees_and_rates,
        )

        # 1. Wallet type rule: official allows only_official_wallet fee steps; exchange forces them to 0
        self.assertEqual(normalize_wallet_type("exchange"), "exchange")
        self.assertEqual(normalize_wallet_type("official"), "official")

        sample_entries = [
            {
                "step_id": "wallet_to_exchange",
                "label": "官方钱包转交易所",
                "category": "transfer",
                "amount": 0.02,
                "currency": "PRL",
                "only_official_wallet": True,
            },
            {
                "step_id": "exchange_withdraw",
                "label": "平台出金手续费",
                "category": "withdraw",
                "amount": 0.5,
                "currency": "USDT",
                "only_official_wallet": False,
            },
        ]
        f_off = normalize_fee_payload({"fee_entries": sample_entries}, wallet_type="official")
        self.assertEqual(len(f_off["fee_entries"]), 2)
        self.assertAlmostEqual(f_off["fee_entries"][0]["amount"], 0.02)

        f_ex = normalize_fee_payload({"fee_entries": sample_entries}, wallet_type="exchange")
        self.assertEqual(len(f_ex["fee_entries"]), 1)
        self.assertEqual(f_ex["fee_entries"][0]["step_id"], "exchange_withdraw")

        # 2. Multi-hop conversions + arbitrary currencies + generic FX spread loss
        multi_hop = normalize_fee_payload(
            {
                "fee_entries": [
                    {"step_id": "s1", "label": "上链转币", "category": "transfer", "amount": 0.01, "currency": "PRL", "only_official_wallet": True},
                    {"step_id": "s2", "label": "交易所现货费", "category": "trade", "amount": 0.02, "currency": "USDT"},
                    {"step_id": "s3", "label": "跨平台提现费", "category": "withdraw", "amount": 0.5, "currency": "USDT"},
                    {"step_id": "s4", "label": "外币出金兑换费", "category": "fx", "amount": 0.08, "currency": "EUR"},
                    {"step_id": "s5", "label": "银行出金费", "category": "payout", "amount": 2.0, "currency": "CNY"},
                ],
                "conversions": [
                    {"label": "交易所卖PRL", "from_asset": "PRL", "to_asset": "USDT", "sold_amount": 10.0, "unit_price": 1.0, "received_amount": 9.98},
                    {"label": "场外售USDT", "from_asset": "USDT", "to_asset": "EUR", "sold_amount": 9.48, "unit_price": 0.90, "received_amount": 8.532},
                    {"label": "欧元结汇", "from_asset": "EUR", "to_asset": "CNY", "sold_amount": 8.232, "unit_price": 7.70, "market_ref_rate": 7.85, "received_amount": 63.39},
                ],
                "residual_deltas": {"PRL": 0.005, "USDT": -0.012, "EUR": 0.01},
            },
            wallet_type="official",
        )
        summ = summarize_fees_and_rates(multi_hop, cny_amount=59.89, prl_amount=10.01)
        self.assertTrue(summ["has_any"])
        self.assertEqual(len(summ["fee_items"]), 5)
        self.assertAlmostEqual(summ["rate_map"]["EUR"], 7.70, places=4)
        self.assertAlmostEqual(summ["rate_map"]["USDT"], 6.93, places=4)
        self.assertAlmostEqual(summ["fx_spread_loss_cny"], 1.23, places=2)
        self.assertGreater(summ["loss_rate_pct"], 0.0)
        self.assertEqual(summ["residual_deltas"]["EUR"], 0.01)

        col_ctrl = CollectorServerController(Path(self.tmpdir.name) / "col_fees.db")
        try:
            api = DesktopApi(self.db, BackgroundPoller(self.db, PoolScraper()), collector_server=col_ctrl)
            w_official_id = self.db.list_wallets()[0]["id"]

            # Customize default fee pipeline & verify persistence + preset switch
            custom_steps = normalize_pipeline_steps(
                [
                    {"id": "tx_prl", "name": "钱包转账", "category": "transfer", "fee_currency": "PRL", "only_official_wallet": True},
                    {"id": "c2c_fee", "name": "C2C 服务费", "category": "payout", "fee_currency": "CNY", "only_official_wallet": False},
                ]
            )
            tpl_res = api.save_fee_pipeline_templates(custom_steps, active_wallet_id=w_official_id)
            self.assertEqual([x["id"] for x in tpl_res["state"]["fee_pipeline_templates"]], ["tx_prl", "c2c_fee"])

            # Add exchange-type wallet
            ex_state = api.add_wallet("prl1exchangewalletdemo000000000000000000", "交易所直充钱包", "免转币费", wallet_type="exchange")
            w_ex = next(w for w in ex_state["wallets"] if w["alias"] == "交易所直充钱包")
            self.assertEqual(w_ex["wallet_type"], "exchange")

            addr_off = self.db.list_wallets()[0]["address"]
            self.db.save_pool_fetch(
                w_official_id,
                PoolDashboardData(
                    wallet=addr_off,
                    fetched_at="2026-11-16 10:00:00",
                    online_count=1,
                    workers=[WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 50000, "1m")],
                ),
            )

            # 3. Record batched generic exchange trades tracking PRL, USDT & EUR dust accumulation + consumption
            t1_res = api.add_exchange_trade(
                {
                    "wallet_id": w_official_id,
                    "venue": "交易所A",
                    "from_asset": "PRL",
                    "to_asset": "USDT",
                    "trade_time": "2026-11-16 10:10:00",
                    "source_amount": 5.544981,
                    "transfer_fee": 0.01,
                    "sold_amount": 5.53,
                    "unit_price": 0.85,
                    "trade_fee": 0.01,
                    "trade_fee_unit": "USDT",
                    "notes": "第1笔卖出留下微量PRL残余",
                },
                active_wallet_id=w_official_id,
            )
            t1 = t1_res["trade"]
            self.assertAlmostEqual(t1["dust_delta"], 0.004981)
            self.assertAlmostEqual(t1_res["state"]["residual_summary"]["balances"]["PRL"], 0.004981)

            # Second trade later consumes exact 0.004981 PRL residual -> clears to 0!
            t2_res = api.add_exchange_trade(
                {
                    "wallet_id": w_official_id,
                    "venue": "交易所A",
                    "from_asset": "PRL",
                    "to_asset": "USDT",
                    "trade_time": "2026-11-16 14:00:00",
                    "source_amount": 5.200000,
                    "transfer_fee": 0.01,
                    "sold_amount": 5.194981,
                    "unit_price": 0.88,
                    "trade_fee": 0.01,
                    "trade_fee_unit": "USDT",
                },
                active_wallet_id=w_official_id,
            )
            t2 = t2_res["trade"]
            self.assertAlmostEqual(t2["dust_delta"], -0.004981)
            self.assertAlmostEqual(t2_res["state"]["residual_summary"]["balances"]["PRL"], 0.0)

            # Third trade USDT -> EUR leaves +0.025 USDT dust
            t3_res = api.add_exchange_trade(
                {
                    "wallet_id": w_official_id,
                    "venue": "换汇平台B",
                    "from_asset": "USDT",
                    "to_asset": "EUR",
                    "trade_time": "2026-11-16 15:00:00",
                    "source_amount": 9.0,
                    "sold_amount": 8.975,
                    "unit_price": 0.92,
                    "trade_fee": 0.02,
                    "trade_fee_unit": "EUR",
                },
                active_wallet_id=w_official_id,
            )
            t3 = t3_res["trade"]
            self.assertAlmostEqual(t3["dust_delta"], 0.025)
            self.assertAlmostEqual(t3_res["state"]["residual_summary"]["balances"]["USDT"], 0.025)

            # 4. Commit settlement linking all 3 trades -> must NOT double-count trade dust!
            draft_workers = api.preview_settlement(w_official_id)["workers"]
            settle1 = api.save_checkpoint_and_settle(
                {
                    "wallet_id": w_official_id,
                    "kind": "withdrawal",
                    "checkpoint_time": "2026-11-16 16:00:00",
                    "label": "合批提现+自定义手续费",
                    "prl_amount": 10.744981,
                    "cny_amount": 60.0,
                    "worker_entries": draft_workers,
                    "fees": {
                        "fee_entries": [
                            {"step_id": "tx_prl", "label": "钱包转账", "category": "transfer", "amount": 0.02, "currency": "PRL", "only_official_wallet": True},
                            {"step_id": "c2c_fee", "label": "C2C 服务费", "category": "payout", "amount": 2.0, "currency": "CNY", "only_official_wallet": False},
                        ],
                        "conversions": [
                            {"label": "交易所卖出", "from_asset": "PRL", "to_asset": "USDT", "sold_amount": 10.724981, "unit_price": 0.86, "received_amount": 9.25},
                        ],
                        "residual_deltas": {"USDT": 0.025},
                        "residual_source": "trades",
                        "linked_trade_ids": [t1["id"], t2["id"], t3["id"]],
                    },
                }
            )
            rs1 = settle1["state"]["residual_summary"]
            self.assertAlmostEqual(rs1["balances"]["PRL"], 0.0)
            self.assertAlmostEqual(rs1["balances"]["USDT"], 0.025)
            self.assertEqual(rs1["unsettled_trade_count"], 0)

            # Draft save & load strips official-only fees cleanly for exchange wallets
            draft_saved = api.save_settlement_draft(
                {
                    "wallet_id": w_ex["id"],
                    "title": "交易所钱包草稿带自定义费",
                    "cny_amount": 30.0,
                    "worker_entries": [{"worker_name": "alice_main", "prev_shares": 0, "curr_shares": 100}],
                    "fees": {
                        "fee_entries": [
                            {"step_id": "tx_prl", "label": "钱包转账", "category": "transfer", "amount": 99.0, "currency": "PRL", "only_official_wallet": True},
                            {"step_id": "c2c_fee", "label": "C2C 服务费", "category": "payout", "amount": 1.2, "currency": "CNY", "only_official_wallet": False},
                        ],
                        "residual_deltas": {"USDT": -0.01},
                    },
                }
            )["draft"]
            self.assertEqual(len(draft_saved["fees"]["fee_entries"]), 1)
            self.assertEqual(draft_saved["fees"]["fee_entries"][0]["step_id"], "c2c_fee")

            # Multi-asset residual calibration (including custom EUR residual)
            cal_res = api.calibrate_residual_balance(
                {"PRL": 0.012, "USDT": 0.0, "EUR": 0.55},
                active_wallet_id=w_official_id,
            )
            self.assertAlmostEqual(cal_res["residual_summary"]["balances"]["PRL"], 0.012)
            self.assertAlmostEqual(cal_res["residual_summary"]["balances"]["USDT"], 0.0)
            self.assertAlmostEqual(cal_res["residual_summary"]["balances"]["EUR"], 0.55)

            # Deleting linked checkpoint restores trades to unsettled state
            after_del = api.delete_checkpoint(w_official_id, settle1["saved"]["id"])
            self.assertEqual(after_del["residual_summary"]["unsettled_trade_count"], 3)
        finally:
            col_ctrl.stop()

    def test_round_pending_estimate_tracks_pool_paid_and_withdrawals(self) -> None:
        from pearl_split.scraper import PaymentRecord

        api = DesktopApi(self.db, BackgroundPoller(self.db, PoolScraper()))
        wid = self.db.list_wallets()[0]["id"]
        addr = self.db.list_wallets()[0]["address"]

        self.db.save_pool_fetch(
            wid,
            PoolDashboardData(
                wallet=addr,
                fetched_at="2026-11-16 12:00:00",
                online_count=2,
                prl_price_cny=10.0,
                unpaid_prl=2.0,
                immature_prl=1.0,
                total_paid_prl=60.0,
                workers=[
                    WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 6000, "1m"),
                    WorkerReading("bob_main", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 4000, "1m"),
                ],
                payments=[
                    PaymentRecord("2026-11-16 11:00:00", 5.0, "0xold1"),
                ],
            ),
        )
        st0 = api.get_state(wid)
        self.assertEqual(st0["round_pending_info"]["source"], "from_genesis")
        self.assertAlmostEqual(st0["round_pending_info"]["paid_since_baseline_prl"], 60.0)
        self.assertAlmostEqual(st0["round_pending_info"]["round_est_total_prl"], 63.0)
        m0 = {m["member_name"]: m for m in st0["member_live_summary"]}
        self.assertAlmostEqual(m0["alice"]["est_unpaid_prl"], 37.8)
        self.assertAlmostEqual(m0["bob"]["est_unpaid_prl"], 25.2)

        draft = api.preview_settlement(wid)
        saved = api.save_checkpoint_and_settle(
            {
                "wallet_id": wid,
                "kind": "withdrawal",
                "checkpoint_time": "2026-11-16 12:00:00",
                "label": "第一次提现",
                "prl_amount": 60.0,
                "cny_amount": 600.0,
                "worker_entries": draft["workers"],
            }
        )
        cp_id = saved["saved"]["id"]
        self.assertAlmostEqual(saved["saved"]["pool_paid_snapshot"], 60.0)
        self.assertEqual(saved["state"]["round_pending_info"]["source"], "snapshot")
        self.assertAlmostEqual(saved["state"]["round_pending_info"]["paid_since_baseline_prl"], 0.0)
        self.assertAlmostEqual(saved["state"]["round_pending_info"]["round_est_total_prl"], 3.0)

        self.db.save_pool_fetch(
            wid,
            PoolDashboardData(
                wallet=addr,
                fetched_at="2026-11-17 20:00:00",
                online_count=2,
                prl_price_cny=10.0,
                unpaid_prl=1.40,
                immature_prl=1.00,
                total_paid_prl=75.35,
                workers=[
                    WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 9000, "1m"),
                    WorkerReading("bob_main", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 5000, "1m"),
                ],
                payments=[
                    PaymentRecord("2026-11-17 18:00:00", 5.05, "0xnew3"),
                ],
            ),
        )
        st1 = api.get_state(wid)
        rp1 = st1["round_pending_info"]
        self.assertEqual(rp1["source"], "snapshot")
        self.assertAlmostEqual(rp1["paid_since_baseline_prl"], 15.35)
        self.assertAlmostEqual(rp1["pool_pending_prl"], 2.40)
        self.assertAlmostEqual(rp1["round_est_total_prl"], 17.75)
        m1 = {m["member_name"]: m for m in st1["member_live_summary"]}
        self.assertAlmostEqual(m1["alice"]["est_paid_pending_prl"], 11.5125)
        self.assertAlmostEqual(m1["alice"]["est_pool_pending_prl"], 1.80)
        self.assertAlmostEqual(m1["alice"]["est_unpaid_prl"], 13.3125)
        self.assertAlmostEqual(m1["alice"]["est_unpaid_cny"], 133.12)
        self.assertAlmostEqual(m1["bob"]["est_unpaid_prl"], 4.4375)

        cleared = api.set_checkpoint_pool_snapshot(wid, cp_id, None)["state"]
        self.assertEqual(cleared["round_pending_info"]["source"], "payments_after_cp")
        self.assertAlmostEqual(cleared["round_pending_info"]["paid_since_baseline_prl"], 5.05)

        re_cal = api.set_checkpoint_pool_snapshot(wid, cp_id, 65.0)["state"]
        self.assertEqual(re_cal["round_pending_info"]["source"], "snapshot")
        self.assertAlmostEqual(re_cal["round_pending_info"]["paid_since_baseline_prl"], 10.35)
        self.assertAlmostEqual(re_cal["round_pending_info"]["round_est_total_prl"], 12.75)

    def test_friend_sync_export_import_roundtrip_and_idempotency(self) -> None:
        from pearl_split.scraper import PaymentRecord

        host_api = DesktopApi(self.db, BackgroundPoller(self.db, PoolScraper()))
        wid = self.db.list_wallets()[0]["id"]
        addr = self.db.list_wallets()[0]["address"]

        host_api.create_member("Amigo", "#ec4899", ["AMG"], "")
        self.db.save_pool_fetch(
            wid,
            PoolDashboardData(
                wallet=addr,
                fetched_at="2026-11-16 10:00:00",
                online_count=2,
                prl_price_cny=10.0,
                unpaid_prl=1.0,
                immature_prl=0.5,
                total_paid_prl=20.0,
                workers=[
                    WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 5000, "1m"),
                    WorkerReading("AMG_rig1", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 5000, "1m"),
                ],
                payments=[PaymentRecord("2026-11-16 09:00:00", 5.0, "0xh1")],
            ),
        )
        draft1 = host_api.preview_settlement(wid)
        cp1 = host_api.save_checkpoint_and_settle(
            {
                "wallet_id": wid,
                "kind": "withdrawal",
                "checkpoint_time": "2026-11-16 10:00:00",
                "label": "第一轮联合提现",
                "prl_amount": 20.0,
                "cny_amount": 200.0,
                "worker_entries": draft1["workers"],
                "fees": {
                    "fee_entries": [
                        {"step_id": "payout", "label": "平台提现", "category": "payout", "amount": 2.0, "currency": "CNY"}
                    ]
                },
            }
        )["saved"]

        sync_pkg = host_api.get_wallet_sync_bundle(wid)
        self.assertTrue(sync_pkg["sync_code"].startswith("PEARLSYNC1:"))
        self.assertEqual(sync_pkg["withdrawal_count"], 1)

        sync_file_path = Path(self.tmpdir.name) / "host_sync.json"
        file_res = host_api.export_sync_file(wid, output_path=str(sync_file_path))
        self.assertFalse(file_res["cancelled"])
        self.assertTrue(sync_file_path.exists())

        friend_db = Database(Path(self.tmpdir.name) / "friend_pc.db")
        try:
            friend_api = DesktopApi(friend_db, BackgroundPoller(friend_db, PoolScraper()))
            f_wallet = friend_db.add_wallet(addr, alias="默认主钱包")
            f_wid = f_wallet["id"]
            friend_db.create_member("alice", "#10b981", ["alice"])
            friend_db.save_pool_fetch(
                f_wid,
                PoolDashboardData(
                    wallet=addr,
                    fetched_at="2026-11-17 18:00:00",
                    online_count=2,
                    prl_price_cny=10.0,
                    unpaid_prl=2.0,
                    immature_prl=1.0,
                    total_paid_prl=30.0,
                    workers=[
                        WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 8000, "1m"),
                        WorkerReading("AMG_rig1", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 6000, "1m"),
                    ],
                ),
            )

            wrapped_code = sync_pkg["sync_code"][:40] + "\n  " + sync_pkg["sync_code"][40:]
            prev = friend_api.preview_sync_import(wrapped_code)
            self.assertEqual(prev["withdrawal_count"], 1)
            self.assertEqual(prev["existing_wallet_alias"], "默认主钱包")

            imp = friend_api.import_sync_payload(wrapped_code, sync_members=True)
            self.assertEqual(imp["import_result"]["added_checkpoints"], 1)
            self.assertEqual(imp["import_result"]["updated_checkpoints"], 0)
            self.assertEqual(imp["import_result"]["members_added"], 4)

            f_st = imp["state"]
            self.assertAlmostEqual(f_st["round_pending_info"]["paid_since_baseline_prl"], 10.0)
            self.assertAlmostEqual(f_st["round_pending_info"]["round_est_total_prl"], 13.0)
            f_members = {m["member_name"]: m for m in f_st["member_live_summary"]}
            self.assertEqual(f_members["alice"]["round_delta_shares"], 3000)
            self.assertEqual(f_members["Amigo"]["round_delta_shares"], 1000)
            self.assertAlmostEqual(f_members["Amigo"]["est_unpaid_prl"], 3.25)
            self.assertAlmostEqual(f_members["Amigo"]["est_unpaid_cny"], 32.5)
            self.assertAlmostEqual(f_st["checkpoints"][0]["fee_summary"]["estimated_fee_cny"], 2.0)

            self.db.save_pool_fetch(
                wid,
                PoolDashboardData(
                    wallet=addr,
                    fetched_at="2026-11-17 18:00:00",
                    online_count=2,
                    prl_price_cny=10.0,
                    unpaid_prl=2.0,
                    immature_prl=1.0,
                    total_paid_prl=30.0,
                    workers=[
                        WorkerReading("alice_main", "200 TH/s", "200 TH/s", "200 TH/s", "200 TH/s", 8000, "1m"),
                        WorkerReading("AMG_rig1", "100 TH/s", "100 TH/s", "100 TH/s", "100 TH/s", 6000, "1m"),
                    ],
                ),
            )
            draft2 = host_api.preview_settlement(wid, cp1["id"])
            host_api.save_checkpoint_and_settle(
                {
                    "wallet_id": wid,
                    "kind": "withdrawal",
                    "checkpoint_time": "2026-11-17 18:00:00",
                    "label": "第二轮提现",
                    "prl_amount": 10.0,
                    "cny_amount": 100.0,
                    "prev_checkpoint_id": cp1["id"],
                    "worker_entries": draft2["workers"],
                }
            )
            sync_file_path_2 = Path(self.tmpdir.name) / "host_sync_2.json"
            host_api.export_sync_file(wid, output_path=str(sync_file_path_2))
            f_prev2 = friend_api.open_and_preview_sync_file(str(sync_file_path_2))
            self.assertEqual(f_prev2["preview"]["withdrawal_count"], 2)

            imp2 = friend_api.import_sync_payload(f_prev2["raw_text"], sync_members=True)
            self.assertEqual(imp2["import_result"]["added_checkpoints"], 1)
            self.assertEqual(imp2["import_result"]["updated_checkpoints"], 1)
            f_cps = imp2["state"]["checkpoints"]
            self.assertEqual(len(f_cps), 2)
            self.assertEqual(f_cps[0]["prev_checkpoint_id"], f_cps[1]["id"])
            self.assertAlmostEqual(imp2["state"]["round_pending_info"]["paid_since_baseline_prl"], 0.0)
        finally:
            friend_db.close()

    def test_alert_engine_formatting_and_stdlib_parser(self) -> None:
        from pearl_split.alert_engine import (
            format_offline_alert_message,
            format_poll_failure_alert_message,
            format_recovery_alert_message,
            parse_online_workers_stdlib,
        )

        workers, max_page = parse_online_workers_stdlib(SAMPLE_DASHBOARD_HTML)
        self.assertEqual(max_page, 1)
        self.assertEqual(len(workers), 4)
        by_name = {w["name"]: w for w in workers}
        self.assertEqual(set(by_name.keys()), {"alice_mini", "dave_02", "alice_main", "alice_rig2"})
        self.assertEqual(by_name["alice_main"]["shares"], 40907)

        with self.assertRaises(RuntimeError):
            parse_online_workers_stdlib("<html><body>invalid pool page</body></html>")

        rig = [
            {
                "worker_name": "bob_rig1",
                "member_name": "bob",
                "remote_tool": "ToDesk",
                "remote_code": "123 456 789",
                "remote_password": "secret88",
                "remote_note": "机房2号机",
            }
        ]
        _, c_plain = format_offline_alert_message(
            "主钱包",
            rig,
            include_password=True,
            detected_at="2026-11-20 10:00:00",
        )
        self.assertIn("bob_rig1", c_plain)
        self.assertIn("ToDesk", c_plain)
        self.assertIn("123 456 789", c_plain)
        self.assertIn("secret88", c_plain)
        self.assertIn("机房2号机", c_plain)
        self.assertIn("2026-11-20 10:00:00", c_plain)

        _, c_hidden = format_offline_alert_message(
            "主钱包",
            rig,
            include_password=False,
            detected_at="2026-11-20 10:00:00",
        )
        self.assertIn("123 456 789", c_hidden)
        self.assertNotIn("secret88", c_hidden)
        self.assertIn("隐藏", c_hidden)

        t_rec, c_rec = format_recovery_alert_message("主钱包", rig, detected_at="2026-11-20 10:05:00")
        self.assertIn("恢复", t_rec + c_rec)
        self.assertIn("bob_rig1", c_rec)

        t_fail, c_fail = format_poll_failure_alert_message(
            31,
            42,
            "连接超时",
            wallet_aliases=["主钱包"],
            detected_at="2026-11-20 10:30:00",
        )
        self.assertIn("31 分钟", t_fail + c_fail)
        self.assertIn("42", c_fail)
        self.assertIn("连接超时", c_fail)
        self.assertIn("主钱包", c_fail)

    def test_offline_alert_debounce_dedup_recovery_and_30min_poll_fail(self) -> None:
        import time as _time

        class ControllableScraper:
            def __init__(self) -> None:
                self.online_names = ["alice_main", "bob_rig1", "charlie_01"]
                self.should_fail = False
                self.shares = 1000

            def fetch_dashboard(self, wallet: str, include_payments: bool = True) -> PoolDashboardData:
                if self.should_fail:
                    raise RuntimeError("pool gateway timeout")
                self.shares += 50
                workers = [
                    WorkerReading(name, "120 TH/s", "120 TH/s", "120 TH/s", "120 TH/s", self.shares, "1分钟前")
                    for name in self.online_names
                ]
                return PoolDashboardData(
                    wallet=wallet,
                    fetched_at="2026-11-20 10:00:00",
                    online_count=len(workers),
                    prl_price_cny=10.0,
                    unpaid_prl=1.0,
                    immature_prl=1.0,
                    workers=workers,
                )

        sent: list[dict[str, str]] = []

        def mock_sender(cfg: dict, title: str, content: str) -> dict:
            sent.append({"provider": str(cfg.get("provider", "")), "title": title, "content": content})
            return {"ok": True, "provider": str(cfg.get("provider", "pushplus")), "detail": "mock ok"}

        scraper = ControllableScraper()
        poller = BackgroundPoller(self.db, scraper, alert_sender=mock_sender)
        api = DesktopApi(self.db, poller)
        wid = self.db.list_wallets()[0]["id"]

        scraper.online_names = ["alice_main", "bob_rig1", "charlie_01"]
        poller.fetch_wallet_once(wid)
        scraper.online_names = ["alice_main", "bob_rig1"]
        poller.fetch_wallet_once(wid)

        api.save_alert_config(
            {
                "enabled": True,
                "provider": "pushplus",
                "pushplus_token": "pp-tok-test",
                "pushplus_topic": "grp01",
                "offline_confirm_polls": 2,
                "poll_fail_alert_minutes": 30,
                "notify_recovery": True,
                "include_password": True,
            }
        )
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 0)

        api.update_worker_remote_config(wid, "charlie_01", {"alert_enabled": False})
        api.update_worker_remote_config(
            wid,
            "bob_rig1",
            {
                "remote_tool": "ToDesk",
                "remote_code": "987654321",
                "remote_password": "fixpw99",
                "remote_note": "显卡1",
                "alert_enabled": True,
            },
        )

        scraper.online_names = ["alice_main"]
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 0)

        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 1)
        self.assertIn("bob_rig1", sent[0]["content"])
        self.assertIn("987654321", sent[0]["content"])
        self.assertIn("fixpw99", sent[0]["content"])
        self.assertNotIn("charlie_01", sent[0]["content"])

        poller.fetch_wallet_once(wid)
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 1)

        scraper.online_names = ["alice_main", "bob_rig1"]
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 2)
        self.assertIn("恢复", sent[1]["title"] + sent[1]["content"])
        self.assertIn("bob_rig1", sent[1]["content"])

        scraper.online_names = ["alice_main"]
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 2)
        poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 3)

        scraper.should_fail = True
        with self.assertRaises(RuntimeError):
            poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 3)

        poller.first_failure_ts = _time.time() - 31 * 60
        with self.assertRaises(RuntimeError):
            poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 4)
        self.assertIn("连续失败超 31 分钟", sent[3]["title"] + sent[3]["content"])
        self.assertIn("自行关注", sent[3]["title"] + sent[3]["content"])

        with self.assertRaises(RuntimeError):
            poller.fetch_wallet_once(wid)
        self.assertEqual(len(sent), 4)

        scraper.should_fail = False
        scraper.online_names = ["alice_main", "bob_rig1"]
        poller.fetch_wallet_once(wid)
        self.assertIsNone(poller.first_failure_ts)
        self.assertFalse(poller.poll_fail_alert_sent)

    def test_desktop_docker_active_passive_failover_and_handback(self) -> None:
        import socket
        import requests

        class FailoverScraper:
            def __init__(self) -> None:
                self.online_names = ["alice_main", "bob_rig1"]

            def fetch_dashboard(self, wallet: str, include_payments: bool = True) -> PoolDashboardData:
                workers = [
                    WorkerReading(name, "150 TH/s", "150 TH/s", "150 TH/s", "150 TH/s", 5000, "1分钟前")
                    for name in self.online_names
                ]
                return PoolDashboardData(
                    wallet=wallet,
                    fetched_at="2026-11-20 10:10:00",
                    online_count=len(workers),
                    prl_price_cny=10.0,
                    unpaid_prl=1.5,
                    immature_prl=2.0,
                    workers=workers,
                )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]

        desktop_sent: list[dict[str, str]] = []
        standby_sent: list[dict[str, str]] = []

        def desktop_sender(cfg: dict, title: str, content: str) -> dict:
            desktop_sent.append({"title": title, "content": content})
            return {"ok": True, "provider": "pushplus"}

        def standby_sender(cfg: dict, title: str, content: str) -> dict:
            standby_sent.append({"title": title, "content": content})
            return {"ok": True, "provider": "pushplus"}

        ctrl = CollectorServerController(Path(self.tmpdir.name) / "standby_col.db", enable_standby_monitor=False)
        ctrl.start(host="127.0.0.1", port=free_port, token="syno-alert-tok")
        try:
            scraper = FailoverScraper()
            poller = BackgroundPoller(self.db, scraper, alert_sender=desktop_sender)
            api = DesktopApi(self.db, poller)
            wid = self.db.list_wallets()[0]["id"]

            poller.fetch_wallet_once(wid)
            api.update_worker_remote_config(
                wid,
                "bob_rig1",
                {
                    "remote_tool": "ToDesk",
                    "remote_code": "555666777",
                    "remote_password": "syno-pw",
                    "alert_enabled": True,
                },
            )
            api.save_alert_config(
                {
                    "enabled": True,
                    "provider": "pushplus",
                    "pushplus_token": "pp-standby-tok",
                    "offline_confirm_polls": 2,
                    "notify_recovery": True,
                    "include_password": True,
                    "standby_sync_enabled": True,
                    "standby_server_url": f"http://127.0.0.1:{free_port}",
                    "standby_token": "syno-alert-tok",
                    "desktop_lease_timeout_sec": 180,
                    "standby_poll_interval_sec": 90,
                }
            )
            sync_res = api.sync_standby_now()
            self.assertTrue(sync_res["sync"]["synced"])
            self.assertEqual(sync_res["sync"]["status"]["mode"], "paused_by_desktop")

            standby_online = [{"name": "alice_main", "shares": 5000}]
            mock_fetcher = lambda addr: {"fetched_at": "2026-11-20 10:15:00", "workers": list(standby_online)}
            pause_res = ctrl.get_store().run_standby_poll_once(
                fetcher=mock_fetcher,
                sender=standby_sender,
            )
            self.assertEqual(pause_res["action"], "paused_by_desktop")
            self.assertEqual(len(standby_sent), 0)

            poller.release_standby_lease()
            r_status = requests.get(
                f"http://127.0.0.1:{free_port}/api/v1/monitor/status",
                headers={"X-Collector-Token": "syno-alert-tok"},
                timeout=3,
            )
            self.assertEqual(r_status.status_code, 200)
            self.assertEqual(r_status.json()["mode"], "standby_active")

            ctrl.get_store().run_standby_poll_once(
                fetcher=mock_fetcher,
                sender=standby_sender,
            )
            self.assertEqual(len(standby_sent), 0)

            ctrl.get_store().run_standby_poll_once(
                fetcher=mock_fetcher,
                sender=standby_sender,
            )
            self.assertEqual(len(standby_sent), 1)
            self.assertIn("群晖 Docker", standby_sent[0]["title"] + standby_sent[0]["content"])
            self.assertIn("bob_rig1", standby_sent[0]["content"])
            self.assertIn("555666777", standby_sent[0]["content"])

            ctrl.get_store().run_standby_poll_once(
                fetcher=mock_fetcher,
                sender=standby_sender,
            )
            self.assertEqual(len(standby_sent), 1)

            hb_res = poller.sync_standby_heartbeat()
            self.assertTrue(hb_res["synced"])
            self.assertEqual(hb_res["status"]["mode"], "paused_by_desktop")

            scraper.online_names = ["alice_main"]
            poller.fetch_wallet_once(wid)
            poller.fetch_wallet_once(wid)
            self.assertEqual(len(desktop_sent), 0)
        finally:
            ctrl.stop()


class TestSingleInstanceGuard(unittest.TestCase):
    def test_single_instance_lock_wake_and_release(self) -> None:
        import os
        import threading
        import uuid
        from pearl_split.single_instance import SingleInstanceGuard, bring_window_to_front

        with tempfile.TemporaryDirectory() as td:
            dir_a = Path(td) / f"inst_a_{uuid.uuid4().hex[:8]}"
            dir_b = Path(td) / f"inst_b_{uuid.uuid4().hex[:8]}"
            dir_a.mkdir(parents=True, exist_ok=True)
            dir_b.mkdir(parents=True, exist_ok=True)

            woke_event = threading.Event()
            wake_calls: list[int] = []

            def on_wake() -> None:
                wake_calls.append(1)
                woke_event.set()

            guard1 = SingleInstanceGuard(dir_a)
            guard_other = SingleInstanceGuard(dir_b)
            try:
                self.assertTrue(guard1.acquire_or_activate())
                guard1.start_wake_listener(on_wake)

                variant = Path(f"{dir_a}".upper() + os.sep + ".")
                guard2 = SingleInstanceGuard(variant)
                self.assertFalse(guard2.acquire_or_activate())
                self.assertTrue(woke_event.wait(timeout=2.0))
                self.assertEqual(len(wake_calls), 1)

                # Auto-reset wake event allows subsequent relaunches to wake again
                woke_event.clear()
                guard3 = SingleInstanceGuard(dir_a)
                self.assertFalse(guard3.acquire_or_activate())
                self.assertTrue(woke_event.wait(timeout=2.0))
                self.assertEqual(len(wake_calls), 2)

                # Separate data_dir runs independently
                self.assertTrue(guard_other.acquire_or_activate())
            finally:
                guard_other.release()
                guard1.release()

            # Once released, a fresh primary instance acquires cleanly
            guard_re = SingleInstanceGuard(dir_a)
            try:
                self.assertTrue(guard_re.acquire_or_activate())
            finally:
                guard_re.release()

            # Mock window bring_window_to_front helper invokes show & restore safely
            calls: list[str] = []

            class DummyWin:
                def show(self) -> None:
                    calls.append("show")

                def restore(self) -> None:
                    calls.append("restore")

            bring_window_to_front(DummyWin())
            self.assertEqual(calls, ["show", "restore", "show"])


if __name__ == "__main__":
    unittest.main()
