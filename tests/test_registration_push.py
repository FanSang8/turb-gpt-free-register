from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import runpy
import threading

import pytest
import requests

import main
from config import push_to_api as cfg
from core import account_export, protocol_registration, push_to_api, registration_output


@pytest.fixture(autouse=True)
def delivery_config(monkeypatch):
    monkeypatch.setattr(cfg, "REGISTER_PUSH_ONLY", True)
    monkeypatch.setattr(cfg, "PUSH_API_URL", "https://receiver.example")
    monkeypatch.setattr(cfg, "PUSH_API_TOKEN", "test-admin-key")
    monkeypatch.setattr(cfg, "PUSH_API_INCLUDE_PROXY", True)


def response(body=None, status=200):
    result = Mock(status_code=status, text="access_token=must-not-log")
    result.json.return_value = body if body is not None else {"added": 1}
    return result


@pytest.mark.parametrize("url", ["https://receiver.example", "https://receiver.example/api/accounts/"])
def test_push_contract(monkeypatch, url):
    monkeypatch.setattr(cfg, "PUSH_API_URL", url)
    post = Mock(return_value=response())
    monkeypatch.setattr(push_to_api.requests, "post", post)
    assert push_to_api.push_account(email="main@outlook.com", access_token="test-token",
                                   source_type="outlook_external", proxy_used="http://proxy.example")["ok"]
    assert post.call_args.args == ("https://receiver.example/api/accounts",)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-admin-key"
    assert post.call_args.kwargs["json"] == {"accounts": [{
        "email": "main@outlook.com", "access_token": "test-token", "type": "free",
        "source_type": "outlook_external", "proxy": "http://proxy.example",
    }]}
    assert post.call_args.kwargs["verify"] is True


@pytest.mark.parametrize("include_proxy", [False, True])
@pytest.mark.parametrize("proxy", [None, "", "http://user:secret@proxy.example:8080",
                                 "socks5h://user:secret@proxy.example:1080"])
def test_push_proxy_only_exported_when_enabled(monkeypatch, include_proxy, proxy):
    monkeypatch.setattr(cfg, "PUSH_API_INCLUDE_PROXY", include_proxy)
    post = Mock(return_value=response())
    monkeypatch.setattr(push_to_api.requests, "post", post)
    assert push_to_api.push_account(email="x@example.com", access_token="test-token",
                                   proxy_used=proxy)["ok"]
    account = post.call_args.kwargs["json"]["accounts"][0]
    if include_proxy and proxy:
        assert account["proxy"] == proxy
    else:
        assert "proxy" not in account
        assert "secret" not in str(post.call_args)


@pytest.mark.parametrize("primary,alias,expected", [
    (None, None, True), ("false", None, False), ("true", None, True),
    (None, "false", False), ("true", "false", False), ("false", "true", True),
])
def test_push_proxy_environment_overrides(monkeypatch, primary, alias, expected):
    from config import env_loader
    monkeypatch.setattr(env_loader, "_LOADED", True)
    for key, value in (("PUSH_API_INCLUDE_PROXY", primary),
                       ("REGISTER_PUSH_API_INCLUDE_PROXY", alias)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    settings = runpy.run_path(cfg.__file__)
    assert settings["PUSH_API_INCLUDE_PROXY"] is expected


def test_delivery_omits_proxy_without_changing_captured_proxy(monkeypatch):
    monkeypatch.setattr(cfg, "PUSH_API_INCLUDE_PROXY", False)
    monkeypatch.setattr("core.email_provider.release_email", Mock())
    post = Mock(return_value=response())
    monkeypatch.setattr(push_to_api.requests, "post", post)
    proxy = "http://user:secret@proxy.example:8080"
    with registration_output.registration_output() as output:
        registration_output.capture_account(
            email="x@example.com", access_token="test-token", email_source="remail", proxy_used=proxy)
        result = registration_output.deliver_registration({"success": True}, output)
        assert output.account["proxy_used"] == proxy
    assert result["success"] is True
    assert post.call_args.kwargs["json"] == {"accounts": [{
        "email": "x@example.com", "access_token": "test-token", "type": "free",
        "source_type": "outlook_external",
    }]}


@pytest.mark.parametrize("body,status", [({"errors": ["invalid"]}, 200), ({"added": 0}, 200),
                                        ({"ok": False}, 200), ({}, 401), ({}, 503), ({}, 302), ({}, 200)])
def test_failed_push_never_echoes_credentials(monkeypatch, body, status):
    monkeypatch.setattr(push_to_api.requests, "post", Mock(return_value=response(body, status)))
    result = push_to_api.push_account(email="x@example.com", access_token="test-token")
    assert result["status"] == "failed"
    assert "access_token" not in result["message"]


def test_transport_timeout_is_redacted(monkeypatch):
    monkeypatch.setattr(push_to_api.requests, "post",
                        Mock(side_effect=requests.Timeout("https://user:secret@host token=secret")))
    result = push_to_api.push_account(email="x@example.com", access_token="test-token")
    assert result["status"] == "failed"
    assert "secret" not in result["message"]


@pytest.mark.parametrize("url", ["", "receiver.example", "https://receiver.example?key=secret"])
def test_missing_push_config_cannot_start_browser_or_purchase(monkeypatch, url):
    monkeypatch.setattr(cfg, "PUSH_API_URL", url)
    register = Mock()
    monkeypatch.setattr(main, "_run_registration", register)
    assert main.run_registration(None, "Test")["success"] is False
    register.assert_not_called()


@pytest.mark.parametrize("delivered", [True, False])
def test_register_then_push_without_local_account_history(monkeypatch, delivered):
    events = []
    insert = Mock(side_effect=AssertionError("must not save credentials"))
    monkeypatch.setattr("core.db.insert_account", insert)
    monkeypatch.setattr("core.db.checkpoint_registration_account", insert)
    monkeypatch.setattr("core.email_provider.release_email", lambda *a, **kw: events.append(kw["status"]))
    def register(**kwargs):
        events.append("register")
        assert protocol_registration.checkpoint_registration_password(Mock(), "fresh@outlook.com", "password") is None
        callback = protocol_registration.install_registration_checkpoint(
            Mock(), "fresh@outlook.com", {"accessToken": "token"}, "token", "password")
        callback(totp_secret="test-secret")
        assert account_export.save_account_data("fresh@outlook.com", "token", "test-secret",
                                               email_source="remail", proxy_used="http://proxy.example") is None
        events.append("browser_closed")
        return {"success": True, "email": "fresh@outlook.com", "access_token": "token",
                "totp_secret": "test-secret", "account_id": None}
    def push(**kwargs):
        events.append("push")
        assert kwargs["source_type"] == "outlook_external"
        assert kwargs["proxy_used"] == "http://proxy.example"
        return {"ok": delivered, "status": "success" if delivered else "failed", "message": "HTTP result"}
    monkeypatch.setattr(main, "_run_registration", register)
    monkeypatch.setattr(push_to_api, "push_account", push)
    result = main.run_registration(None, "Test")
    assert events == ["register", "browser_closed", "push", "used"]
    assert result["registration_success"] is True
    assert result["success"] is delivered
    assert result["account_id"] is None
    assert "access_token" not in result and "totp_secret" not in result
    assert registration_output._CURRENT.get() is None
    insert.assert_not_called()


def test_concurrent_delivery_does_not_mix_accounts(monkeypatch):
    barrier = threading.Barrier(8)
    calls = []
    lock = threading.Lock()
    monkeypatch.setattr("core.email_provider.release_email", Mock())
    def register(**kwargs):
        email = kwargs["email"]
        account_export.save_account_data(email, f"token:{email}", email_source="remail")
        barrier.wait(timeout=5)
        return {"success": True, "email": email}
    def push(**kwargs):
        with lock:
            calls.append(kwargs)
        return {"ok": True, "status": "success", "message": "added=1"}
    monkeypatch.setattr(main, "_run_registration", register)
    monkeypatch.setattr(push_to_api, "push_account", push)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: main.run_registration(f"mail{index}@outlook.com", "Test"), range(8)))
    assert all(item["success"] for item in results)
    assert len(calls) == 8
    assert all(call["access_token"] == f"token:{call['email']}" for call in calls)


def test_cleanup_exception_after_token_does_not_lose_delivery(monkeypatch):
    monkeypatch.setattr("core.email_provider.release_email", Mock())
    def register(**kwargs):
        account_export.save_account_data("fresh@outlook.com", "token", email_source="remail")
        raise RuntimeError("browser close failed")
    monkeypatch.setattr(main, "_run_registration", register)
    push = Mock(return_value={"ok": True, "status": "success", "message": "added=1"})
    monkeypatch.setattr(push_to_api, "push_account", push)
    result = main.run_registration(None, "Test")
    assert result["success"] is True
    assert result["registration_success"] is True
    push.assert_called_once()


def test_roxy_real_dispatch_skips_codex_and_pushes_after_cleanup(monkeypatch):
    from config import roxybrowser, codex
    from core import roxy_registration as roxy
    from core.roxybrowser_client import RoxyOpenResult
    monkeypatch.setattr(roxybrowser, "REGISTRATION_DRIVER", "roxy")
    monkeypatch.setattr(codex, "ENABLE_CODEX_AUTO", True)
    monkeypatch.setattr(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", True)
    events = []
    client, driver = Mock(), Mock()
    client.open_profile.return_value = RoxyOpenResult("test-profile", {}, created_by_run=True)
    client.cleanup_profile.side_effect = lambda *a: events.append("cleanup")
    monkeypatch.setattr(roxy, "RoxyBrowserClient", lambda: client)
    monkeypatch.setattr(roxy, "_build_driver", lambda *a: driver)
    for name in ("RoxyLocalAssetCache", "SeleniumTrafficTracker", "BrowserDataSaver"):
        monkeypatch.setattr(roxy, name, Mock())
    for name in ("_center_browser_window", "_safe_get", "_page_warmup", "_maybe_accept",
                 "_check_manual_stop", "human_delay", "_clear_otp_inputs", "_type_otp", "_click_continue"):
        monkeypatch.setattr(roxy, name, Mock())
    monkeypatch.setattr(roxy, "_submit_email_and_wait_next", lambda *a, **kw: "otp")
    monkeypatch.setattr(roxy, "_fill_password_page_if_present", lambda *a, **kw: "test-password")
    monkeypatch.setattr(roxy, "_is_signup_password_page", lambda *a: False)
    monkeypatch.setattr(roxy, "_wait_after_email_otp_submit", lambda *a, **kw: "accepted")
    monkeypatch.setattr(roxy, "_complete_profile_page", lambda *a, **kw: True)
    monkeypatch.setattr(roxy, "_fetch_chatgpt_session", lambda *a, **kw: {"accessToken": "test-token"})
    monkeypatch.setattr(roxy, "merge_proxy_transport_traffic", lambda *a, **kw: {})
    monkeypatch.setattr(roxy, "resolve_email_source", lambda *a: "remail")
    monkeypatch.setattr("core.email_provider.release_email", Mock())
    codex_run = Mock(side_effect=AssertionError("must skip Codex"))
    monkeypatch.setattr("core.roxy_codex_oauth.run_roxy_codex_oauth", codex_run)
    def push(**kwargs):
        events.append("push")
        return {"ok": True, "status": "success", "message": "added=1"}
    monkeypatch.setattr(push_to_api, "push_account", push)
    result = main.run_registration("fresh@outlook.com", "Test", otp_code="123456")
    assert result["success"] is True
    assert events == ["cleanup", "push"]
    driver.quit.assert_called_once()
    codex_run.assert_not_called()


def test_consumed_account_cannot_be_registered_again_from_retry_button():
    from core.registration_service import get_retry_info
    info = get_retry_info({"id": 1, "status": "failed", "account_id": None,
                           "registration_success": True,
                           "push": {"status": "failed"}})
    assert info["retryable"] is False


def test_webui_blocks_jobs_before_purchasing_and_exposes_push_settings(monkeypatch):
    from webui.app import create_app
    client = create_app(auth_code="test-auth").test_client()
    headers = {"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"}
    submit = Mock()
    monkeypatch.setattr("core.registration_service.submit_registration", submit)
    monkeypatch.setattr(cfg, "PUSH_API_URL", "")
    result = client.post("/api/jobs", json={"count": 1}, headers=headers)
    assert result.status_code == 400
    submit.assert_not_called()
    fields = {field["key"]: field for field in client.get("/api/config", headers=headers).json}
    assert fields["PUSH_API_TOKEN"]["secret"] is True
    assert fields["REGISTER_PUSH_ONLY"]["value"] is True
    assert fields["PUSH_API_INCLUDE_PROXY"]["type"] == "bool"
    assert fields["PUSH_API_INCLUDE_PROXY"]["group"] == "账号推送"
    assert "REMAIL_PURCHASE_BATCH_SIZE" in fields


def test_push_proxy_setting_saved_by_webui_config_editor(tmp_path, monkeypatch):
    from config import env_loader
    from webui import config_editor
    path = tmp_path / ".env"
    path.write_text('PROXY_POOL="http://user:secret@proxy.example:8080"\n', encoding="utf-8")
    monkeypatch.setattr(env_loader, "_ENV_PATH", path)
    monkeypatch.setenv("PUSH_API_INCLUDE_PROXY", "true")
    monkeypatch.setenv("PROXY_POOL", "http://user:secret@proxy.example:8080")
    monkeypatch.delenv("REGISTER_PUSH_API_INCLUDE_PROXY", raising=False)
    result = config_editor.update_config({"PUSH_API_INCLUDE_PROXY": False})
    assert result["updated"] == ["PUSH_API_INCLUDE_PROXY"]
    assert env_loader.read_env_file()["PUSH_API_INCLUDE_PROXY"] == "False"
    assert env_loader.read_env_file()["PROXY_POOL"] == "http://user:secret@proxy.example:8080"
    assert runpy.run_path(cfg.__file__)["PUSH_API_INCLUDE_PROXY"] is False
    fields = {field["key"]: field for field in config_editor.get_config()}
    assert fields["PUSH_API_INCLUDE_PROXY"]["value"] is False


def test_job_history_persists_only_delivery_metadata(tmp_path, monkeypatch):
    from core import db
    monkeypatch.setattr(db, "_SQLITE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)
    monkeypatch.setattr(db, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_LOG_DIR", tmp_path / "logs")
    job = db.create_job("remail")
    db.update_job(job["id"], push={"ok": False, "status": "failed", "message": "HTTP 503",
                                  "access_token": "must-not-retain"},
                  registration_success=True, mailbox_reservation_id="OR1:main")
    saved = db.get_job(job["id"])
    assert saved["registration_success"] is True
    assert saved["mailbox_reservation_id"] == "OR1:main"
    assert saved["push"] == {"ok": False, "status": "failed", "message": "HTTP 503"}
    assert db.get_account_by_email("fresh@outlook.com") is None
