"""Camoufox lifecycle and the project's Playwright WebDriver adapter."""
from __future__ import annotations

import logging
import sys
import uuid

from config import camoufox as _cfg
from core.cloakbrowser_driver import CloakOpenResult, CloakSeleniumDriver

logger = logging.getLogger(__name__)


class CamoufoxSeleniumDriver(CloakSeleniumDriver):
    def __init__(self, *, manager, **kwargs):
        super().__init__(**kwargs)
        self._manager = manager
        self._registration_log_prefix = "[Camoufox注册]"

    @property
    def execute_cdp_cmd(self):
        # Shared page helpers use hasattr() to choose their non-CDP fallback.
        raise AttributeError("Camoufox Firefox does not support Chromium CDP")

    def quit(self):
        manager, self._manager = self._manager, None
        if manager is None:
            return
        try:
            try:
                self.context.close()
            finally:
                manager.__exit__(None, None, None)
        finally:
            relay, self._proxy_relay = self._proxy_relay, None
            if relay is not None:
                relay.close()


def build_camoufox_driver(proxy: str | None = None):
    """None selects the pool; an empty string explicitly disables the proxy."""
    relay = None
    manager = None
    entered = False
    original_proxy = ""
    try:
        # Import before opening a relay, so missing dependencies leave no threads.
        from camoufox.sync_api import Camoufox
        if bool(_cfg.CAMOUFOX_USE_PROXY):
            from config.proxy import pick_proxy
            from core.proxy_chain import open_proxy_pool_proxy
            original_proxy = str((pick_proxy() if proxy is None else proxy) or "").strip()
            proxy_url, relay = open_proxy_pool_proxy(original_proxy, measure_transport=True)
        else:
            proxy_url = ""
        options = {
            "headless": bool(_cfg.CAMOUFOX_HEADLESS),
            "humanize": bool(_cfg.CAMOUFOX_HUMANIZE),
            "geoip": bool(_cfg.CAMOUFOX_GEOIP),
        }
        locale = str(_cfg.CAMOUFOX_LOCALE or "").strip()
        if locale:
            options["locale"] = locale
        if proxy_url:
            from core.proxy_utils import parse_proxy_url
            parsed = parse_proxy_url(proxy_url)
            options["proxy"] = {
                "server": f"{parsed.scheme.replace('socks5h', 'socks5')}://"
                          f"{'[' + parsed.host + ']' if ':' in parsed.host else parsed.host}:{parsed.port}",
            }
            if parsed.username:
                options["proxy"]["username"] = parsed.username
            if parsed.password:
                options["proxy"]["password"] = parsed.password
        logger.info("[Camoufox] 启动 Firefox：headless=%s geoip=%s proxy=%s",
                    options["headless"], options["geoip"], bool(proxy_url))
        manager = Camoufox(**options)
        browser = manager.__enter__()
        entered = True
        context = browser.new_context()
        page = context.new_page()
        driver = CamoufoxSeleniumDriver(
            manager=manager, browser=browser, context=context, page=page, proxy_relay=relay,
        )
        driver.set_page_load_timeout(max(1, int(_cfg.CAMOUFOX_TIMEOUT)))
        return driver, CloakOpenResult(
            profile_id=f"camoufox-{uuid.uuid4().hex}",
            raw={"driver": "camoufox", "proxy": proxy_url or "",
                 "proxy_pool_target": original_proxy, "headless": options["headless"]},
        )
    except BaseException:
        error = sys.exc_info()
        try:
            # Camoufox.__enter__ already stops Playwright if launch itself fails.
            if entered:
                manager.__exit__(*error)
        except Exception:
            logger.debug("[Camoufox] 启动失败后的清理异常", exc_info=True)
        finally:
            if relay is not None:
                relay.close()
        raise
