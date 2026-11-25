import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from config import remail as cfg
from core import remail_client, remail_manager as manager_module
from core.remail_manager import RemailManager
from tests.test_remail_reservation_priority import _PriorityRemailClient


@pytest.fixture
def manager(tmp_path, monkeypatch):
    for name, value in {
        "REMAIL_ENABLED": True, "REMAIL_API_KEY": "rk-test",
        "REMAIL_BASE_URL": "https://remail.example", "REMAIL_PROJECT_ID": 2,
        "REMAIL_EMAIL_SUFFIX": "outlook.com", "REMAIL_SERVICE_MODE": "purchase",
        "REMAIL_PURCHASE_BATCH_SIZE": 4, "REMAIL_MAX_PURCHASE_PER_ROUND": 8,
        "REMAIL_FEISHU_WEBHOOK_URL": "",
    }.items():
        monkeypatch.setattr(cfg, name, value)
        monkeypatch.delenv(f"REGISTER_{name}", raising=False)
    fake = _PriorityRemailClient()
    result = RemailManager(state_path=tmp_path / "state.json", client_factory=lambda: fake)
    result._recovered = True
    monkeypatch.setattr(manager_module, "remail_manager", result)
    yield result, fake
    remail_client._CONTEXT_CACHE.clear()


def test_refill_keeps_paid_inventory_and_never_allocates_duplicates(manager):
    manager, fake = manager
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda _: manager.prepare_reservations(1), range(8)))
    emails = [batch[0]["email"] for batch in batches]
    assert len(set(emails)) == 8
    assert fake.created_quantities == [4, 4]
    assert manager.status()["reserved_slots"] == 8


def test_adapter_binds_job_and_child_uses_main_pickup(manager, monkeypatch):
    manager, fake = manager
    update_job = Mock(return_value=True)
    monkeypatch.setattr("core.db.update_job", update_job)
    monkeypatch.setattr("core.registration_service._THREAD_CTX.job_id", 123, raising=False)
    monkeypatch.setattr(remail_client, "_check_task_stop", Mock())
    main = remail_client.pick_account()
    assert main.reservation_id.endswith(":main")
    update_job.assert_called_once()
    remail_client.release_account(main.email, status="used")
    child = remail_client.pick_account()
    assert "+se" in child.email and child.base_email == main.email
    response = Mock(status_code=200)
    response.json.return_value = {"items": [{"verificationCode": "123456"}]}
    request = Mock(return_value=response)
    monkeypatch.setattr(remail_client.requests, "request", request)
    assert remail_client.fetch_latest_otp(child.email, max_wait=1, settle_seconds=0) == "123456"
    assert request.call_args.kwargs["params"]["email"] == main.email
    assert "Authorization" not in request.call_args.kwargs["headers"]
    remail_client.release_account(child.email, status="used")
    state = json.loads(manager.state_path.read_text())
    assert "service_token" not in state["orders"][0]
    assert manager.context_for_email(child.email) is None
    assert "st-" not in json.dumps(manager.status())


def test_restart_consumes_running_slot_and_preserves_unused_inventory(manager, monkeypatch):
    manager, _ = manager
    main = manager.prepare_reservations(1)[0]
    manager.bind_reservation(main["reservation_id"], 1)
    manager.mark_running(main["reservation_id"], 1)
    fresh = RemailManager(state_path=manager.state_path)
    monkeypatch.setattr("core.db.list_jobs", lambda **kw: [{"id": 1, "status": "running"}])
    update = Mock()
    monkeypatch.setattr("core.db.update_job", update)
    fresh.recover()
    assert fresh.status()["failed_slots"] == 1
    assert fresh.status()["available_slots"] == 7
    update.assert_called_once()


def test_ambiguous_purchase_reuses_original_idempotency_key(manager):
    manager, fake = manager
    original = fake.create_orders
    calls = []
    def purchase(**kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            raise remail_client.RemailAmbiguousPurchaseError("unknown result")
        return original(**kwargs)
    fake.create_orders = purchase
    assert manager.prepare_reservations(1)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert fake.created_quantities == [4]
    assert manager.status()["purchase_circuit_open"] is False


def test_suffix_purchase_uses_single_endpoint_and_deterministic_keys():
    client = remail_client.RemailClient("https://remail.example", "rk-test")
    response = Mock(status_code=201, content=b"{}", text="{}")
    response.json.return_value = {"orderNo": "OR1"}
    client.session.request = Mock(return_value=response)
    client.create_orders(project_id=2, product_id=None, email_suffix="outlook.com",
                         service_mode="purchase", quantity=3, idempotency_key="same-key",
                         supply="public_only")
    calls = client.session.request.call_args_list
    assert len(calls) == 3
    assert all(call.args == ("POST", "https://remail.example/v1/open/orders") for call in calls)
    assert [call.kwargs["headers"]["Idempotency-Key"] for call in calls] == ["same-key:1", "same-key:2", "same-key:3"]
    assert calls[0].kwargs["json"] == {"projectId": 2, "emailSuffix": "outlook.com"}


def test_timeout_is_ambiguous_instead_of_blindly_rebuying():
    client = remail_client.RemailClient("https://remail.example", "rk-test")
    client.session.request = Mock(side_effect=requests.Timeout())
    with pytest.raises(remail_client.RemailAmbiguousPurchaseError):
        client.create_orders(project_id=2, product_id=None, email_suffix="outlook.com",
                             service_mode="purchase", quantity=1, idempotency_key="same-key")


def test_non_json_purchase_response_is_replayed_with_same_key():
    client = remail_client.RemailClient("https://remail.example", "rk-test")
    response = Mock(status_code=201, content=b"not-json")
    response.json.side_effect = ValueError()
    client.session.request = Mock(return_value=response)
    with pytest.raises(remail_client.RemailAmbiguousPurchaseError):
        client.create_orders(project_id=2, product_id=None, email_suffix="outlook.com",
                             service_mode="purchase", quantity=1, idempotency_key="same-key")


def test_delayed_paid_delivery_is_refreshed_before_rebuying(manager):
    manager, fake = manager
    raw = fake.create_orders(quantity=1)[0][0]
    state = manager._load()
    state["orders"] = [{"order_no": raw["orderNo"], "slots": []}]
    manager._save(state)
    reserved = manager.prepare_reservations(1)
    assert len(reserved) == 1
    assert reserved[0]["email"] == raw["deliveryEmail"]
    assert fake.created_quantities == [1]
