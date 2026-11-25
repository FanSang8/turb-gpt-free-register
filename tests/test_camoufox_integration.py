import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import camoufox as cfg
from core import camoufox_driver as backend
from core import proxy_chain


@pytest.fixture
def runtime(monkeypatch):
    manager = Mock()
    manager.__enter__ = Mock(return_value=Mock())
    manager.__exit__ = Mock()
    factory = Mock(return_value=manager)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", SimpleNamespace(Camoufox=factory))
    relay = Mock(proxy_url="http://127.0.0.1:34567")
    relay.traffic_snapshot.return_value = {"total_bytes": 12}
    opener = Mock(return_value=(relay.proxy_url, relay))
    monkeypatch.setattr(proxy_chain, "open_proxy_pool_proxy", opener)
    monkeypatch.setattr(cfg, "CAMOUFOX_USE_PROXY", True)
    monkeypatch.setattr(cfg, "CAMOUFOX_GEOIP", False)
    monkeypatch.setattr(cfg, "CAMOUFOX_HEADLESS", True)
    monkeypatch.setattr(cfg, "CAMOUFOX_TIMEOUT", 33)
    monkeypatch.setattr(cfg, "CAMOUFOX_LOCALE", "")
    return SimpleNamespace(manager=manager, factory=factory, relay=relay, opener=opener)


def test_pool_and_manager_lifetime(runtime, monkeypatch):
    from config import proxy
    monkeypatch.setattr(proxy, "pick_proxy", lambda: "socks5://user:password@exit.example:1080")
    driver, opened = backend.build_camoufox_driver()
    runtime.opener.assert_called_once_with(
        "socks5://user:password@exit.example:1080", measure_transport=True,
    )
    assert runtime.factory.call_args.kwargs["proxy"] == {"server": runtime.relay.proxy_url}
    assert opened.raw["proxy_pool_target"].startswith("socks5://")
    assert opened.profile_id.startswith("camoufox-")
    assert driver._page_load_timeout_ms == 33000
    assert driver.proxy_transport_snapshot() == {"total_bytes": 12}
    assert not hasattr(driver, "execute_cdp_cmd")
    driver.quit()
    driver.quit()
    runtime.manager.__exit__.assert_called_once_with(None, None, None)
    runtime.relay.close.assert_called_once()


@pytest.mark.parametrize("phase", ["launch", "context", "page"])
def test_failed_launch_closes_all_owned_resources(runtime, phase):
    browser = runtime.manager.__enter__.return_value
    if phase == "launch":
        runtime.manager.__enter__.side_effect = RuntimeError("launch failed")
    elif phase == "context":
        browser.new_context.side_effect = RuntimeError("context failed")
    else:
        browser.new_context.return_value.new_page.side_effect = RuntimeError("page failed")
    with pytest.raises(RuntimeError, match="failed"):
        backend.build_camoufox_driver("http://example.test:8080")
    runtime.relay.close.assert_called_once()
    assert runtime.manager.__exit__.call_count == (phase != "launch")


def test_quit_stops_manager_and_relay_even_if_context_close_fails(runtime):
    driver, _ = backend.build_camoufox_driver("http://example.test:8080")
    driver.context.close.side_effect = RuntimeError("crashed")
    with pytest.raises(RuntimeError):
        driver.quit()
    runtime.manager.__exit__.assert_called_once()
    runtime.relay.close.assert_called_once()


def test_next_session_closes_debug_browser_on_same_worker(runtime):
    first, _ = backend.build_camoufox_driver("")
    runtime.opener.return_value = ("", None)
    second, _ = backend.build_camoufox_driver("")
    assert first._manager is None
    assert second._manager is runtime.manager
    assert runtime.manager.__exit__.call_count == 1
    second.quit()
    assert runtime.manager.__exit__.call_count == 2


@pytest.mark.parametrize("proxy,use_proxy", [("", True), (None, False)])
def test_explicit_direct_never_picks_proxy(runtime, monkeypatch, proxy, use_proxy):
    from config import proxy as proxy_cfg
    pick = Mock(side_effect=AssertionError("must not select a proxy"))
    monkeypatch.setattr(proxy_cfg, "pick_proxy", pick)
    monkeypatch.setattr(cfg, "CAMOUFOX_USE_PROXY", use_proxy)
    runtime.opener.return_value = ("", None)
    driver, opened = backend.build_camoufox_driver(proxy)
    assert "proxy" not in runtime.factory.call_args.kwargs
    assert opened.raw["proxy_pool_target"] == ""
    pick.assert_not_called()
    driver.quit()


def test_authenticated_https_proxy_shape(runtime):
    runtime.opener.return_value = ("https://a%40b:p%3Ass@proxy.example:443", None)
    driver, _ = backend.build_camoufox_driver("https://a%40b:p%3Ass@proxy.example:443")
    assert runtime.factory.call_args.kwargs["proxy"] == {
        "server": "https://proxy.example:443", "username": "a@b", "password": "p:ss",
    }
    driver.quit()


def test_proxy_setup_failure_never_falls_back_direct(runtime):
    runtime.opener.side_effect = OSError("proxy unavailable")
    with pytest.raises(OSError):
        backend.build_camoufox_driver("http://proxy.example:8080")
    runtime.factory.assert_not_called()


def test_dispatch_uses_camoufox(monkeypatch):
    import main
    from config import roxybrowser
    from core import camoufox_registration
    run = Mock(return_value={"success": True})
    monkeypatch.setattr(roxybrowser, "REGISTRATION_DRIVER", "camoufox")
    monkeypatch.setattr(camoufox_registration, "run_camoufox_registration", run)
    result = main._run_registration("test@example.test", "Test", birthday="1990-01-01", proxy="")
    assert result["success"] is True
    assert run.call_args.kwargs["proxy"] == ""


@pytest.fixture
def flow(monkeypatch):
    from core import cloakbrowser_registration as shared
    from core import email_provider
    from config import codex, twofa
    monkeypatch.setattr(codex, "ENABLE_CODEX_AUTO", True)
    monkeypatch.setattr(twofa, "ENABLE_2FA", False)
    driver = Mock()
    driver.proxy_transport_snapshot.return_value = None
    builder = Mock(return_value=(driver, SimpleNamespace(profile_id="test", raw={})))
    monkeypatch.setattr(shared, "PlaywrightTrafficTracker", Mock())
    monkeypatch.setattr(shared, "BrowserDataSaver", Mock())
    monkeypatch.setattr(shared, "merge_proxy_transport_traffic", lambda *a, **k: {})
    monkeypatch.setattr(shared, "human_delay", Mock())
    monkeypatch.setattr(shared, "post_register_dwell", Mock())
    for name in (
        "_maybe_accept", "_check_manual_stop", "_submit_email_and_wait_next",
        "_fill_password_page_if_present", "_clear_otp_inputs", "_type_otp",
        "_click_continue",
    ):
        monkeypatch.setattr(shared, name, Mock())
    monkeypatch.setattr(shared, "_wait_after_email_otp_submit", lambda *a, **k: "accepted")
    monkeypatch.setattr(shared, "_complete_profile_page", lambda *a, **k: True)
    monkeypatch.setattr(shared, "_fetch_chatgpt_session", lambda *a, **k: {"accessToken": "test-token"})
    monkeypatch.setattr(shared, "resolve_email_source", lambda email: "manual")
    save = Mock(return_value=1)
    monkeypatch.setattr(shared, "save_account_data", save)
    release = Mock()
    monkeypatch.setattr(email_provider, "release_email", release)
    return SimpleNamespace(shared=shared, driver=driver, builder=builder, save=save, release=release)


@pytest.mark.parametrize("ephemeral,keep,should_close", [
    (True, True, True), (True, False, True), (False, False, True), (False, True, False),
])
def test_shared_flow_cleanup_and_metadata(flow, monkeypatch, ephemeral, keep, should_close):
    monkeypatch.setattr(flow.shared, "ephemeral_registration", lambda: ephemeral)
    result = flow.shared.run_browser_registration(
        "test@example.test", "Test", "1990-01-01", otp_code="000000",
        driver_builder=flow.builder, keep_browser_open=keep,
        label="Camoufox", metadata_key="camoufox", supports_codex=False,
    )
    assert result["success"] is True
    assert result["codex"]["status"] == "skipped"
    assert "Camoufox" in result["codex"]["message"]
    assert "camoufox" in flow.save.call_args.kwargs["extra"]
    assert "cloakbrowser" not in flow.save.call_args.kwargs["extra"]
    assert bool(flow.driver.quit.call_count) == should_close
    flow.release.assert_not_called()


def test_shared_flow_failure_releases_mail_and_closes(flow, monkeypatch):
    monkeypatch.setattr(flow.shared, "ephemeral_registration", lambda: True)
    flow.driver.get.side_effect = RuntimeError("test navigation failure")
    result = flow.shared.run_browser_registration(
        "test@example.test", "Test", "1990-01-01",
        driver_builder=flow.builder, label="Camoufox", supports_codex=False,
    )
    assert result["success"] is False
    assert flow.release.call_args.kwargs["status"] == "available"
    assert "Camoufox" in flow.release.call_args.kwargs["note"]
    flow.driver.quit.assert_called_once()


def test_browser_closes_when_data_saver_cleanup_fails(flow, monkeypatch):
    monkeypatch.setattr(flow.shared, "ephemeral_registration", lambda: True)
    flow.shared.BrowserDataSaver.return_value.stop.side_effect = RuntimeError("cleanup failed")
    result = flow.shared.run_browser_registration(
        "test@example.test", "Test", "1990-01-01", otp_code="000000",
        driver_builder=flow.builder, label="Camoufox", supports_codex=False,
    )
    assert result["success"] is True
    flow.driver.quit.assert_called_once()


def test_webui_and_reload_expose_settings():
    import config
    from webui.config_editor import EDITABLE_FIELDS
    assert "config.camoufox" in config._RELOADABLE_SUBMODULES
    fields = {field["key"]: field for field in EDITABLE_FIELDS}
    assert fields["CAMOUFOX_HEADLESS"]["group"] == "Camoufox"
    assert any(item["value"] == "camoufox" for item in fields["REGISTRATION_DRIVER"]["choices"])


def test_camoufox_codex_returns_explicit_unsupported_result(monkeypatch):
    from config import codex, roxybrowser
    from core import codex_oauth
    monkeypatch.setattr(codex, "CODEX_OAUTH_DRIVER", "same_as_registration")
    monkeypatch.setattr(roxybrowser, "REGISTRATION_DRIVER", "camoufox")
    result = codex_oauth.run_codex_oauth("test@example.test", force=True)
    assert result["ok"] is False
    assert "Camoufox" in result["message"]
