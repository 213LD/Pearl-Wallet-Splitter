"""Desktop application entry point for Pearl (PRL) Miner Share & Revenue Split Calculator."""

from __future__ import annotations

import threading
import winsound
from typing import Any

import pystray
import webview
from PIL import Image

from pearl_split.api import DesktopApi
from pearl_split.collector_server import CollectorServerController
from pearl_split.db import Database
from pearl_split.miner_binder import LocalMinerBinder
from pearl_split.paths import get_asset_path, get_data_dir, get_db_path, get_web_dir
from pearl_split.poller import BackgroundPoller
from pearl_split.scraper import PoolScraper


def _load_tray_image() -> Image.Image:
    icon_path = get_asset_path("pearl.png")
    if icon_path.exists():
        return Image.open(icon_path)
    return Image.new("RGBA", (64, 64), (16, 185, 129, 255))


def main() -> None:
    db = Database(get_db_path())
    scraper = PoolScraper()

    window_holder: dict[str, Any] = {"window": None, "tray": None, "force_exit": False}

    def on_poller_update(event: dict[str, Any]) -> None:
        woken = event.get("woken") or []
        if woken:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        win = window_holder.get("window")
        if win is not None:
            def _push_js() -> None:
                try:
                    win.evaluate_js("window.__onBackendRefresh && window.__onBackendRefresh()")
                except Exception:
                    pass

            threading.Thread(target=_push_js, daemon=True).start()

    poller = BackgroundPoller(db=db, scraper=scraper, on_update=on_poller_update)
    collector_server = CollectorServerController(get_data_dir() / "collector_telemetry.db")
    miner_binder = LocalMinerBinder(get_settings_fn=db.get_settings)

    settings = db.get_settings()
    if settings.get("local_collector_enabled", "0") == "1":
        try:
            port = int(settings.get("local_collector_port") or 8765)
            token = settings.get("collector_token") or ""
            collector_server.start(host="0.0.0.0", port=port, token=token)
        except Exception:
            pass

    api = DesktopApi(
        db=db,
        poller=poller,
        miner_binder=miner_binder,
        collector_server=collector_server,
    )

    index_html = get_web_dir() / "index.html"
    window = webview.create_window(
        title="Pearl (PRL) 矿池收益分账助手",
        url=str(index_html),
        js_api=api,
        width=1260,
        height=820,
        min_size=(980, 660),
        background_color="#0b1117",
    )
    window_holder["window"] = window
    api.attach_window(window)

    def _cleanup_services() -> None:
        miner_binder.stop_all()
        collector_server.stop()
        poller.stop()
        db.close()

    def shutdown_app() -> None:
        window_holder["force_exit"] = True
        tray = window_holder.get("tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        _cleanup_services()
        try:
            window.destroy()
        except Exception:
            pass

    def show_window() -> None:
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def on_closing() -> bool:
        if window_holder["force_exit"]:
            return True
        settings_now = db.get_settings()
        if settings_now.get("minimize_to_tray", "1") == "1" and window_holder.get("tray") is not None:
            window.hide()
            return False
        window_holder["force_exit"] = True
        tray = window_holder.get("tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        _cleanup_services()
        return True

    window.events.closing += on_closing

    def setup_tray() -> None:
        menu = pystray.Menu(
            pystray.MenuItem("打开 Pearl 分账助手", lambda _icon, _item: show_window(), default=True),
            pystray.MenuItem("立即拉取全部钱包", lambda _icon, _item: poller.trigger_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", lambda _icon, _item: threading.Thread(target=shutdown_app, daemon=True).start()),
        )
        tray = pystray.Icon("PearlMinerSplit", _load_tray_image(), "Pearl (PRL) 分账助手 - 后台缓存中", menu)
        window_holder["tray"] = tray
        tray.run_detached()

    def on_started() -> None:
        setup_tray()
        poller.start()
        miner_binder.start_background_pusher()

    webview.start(func=on_started, gui="edgechromium")


if __name__ == "__main__":
    main()
