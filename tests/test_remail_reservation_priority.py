from __future__ import annotations

from pathlib import Path

from config import remail as remail_config
from core.remail_manager import RemailManager


class _PriorityRemailClient:
    def __init__(self):
        self.orders = []
        self.created_quantities = []

    def get_wallet(self):
        return {"wallet": {"consumerBalance": "1000"}}

    def get_project(self, project_id: int):
        return {
            "project": {"id": project_id},
            "products": [{
                "id": 22,
                "type": "microsoft",
                "status": "enabled",
                "purchaseEnabled": True,
                "purchasePrice": "10",
                "suffixes": [{"suffix": "outlook.com", "publicAvailable": 999}],
            }],
        }

    def create_orders(self, *, quantity: int, **kwargs):
        self.created_quantities.append(quantity)
        created = []
        start = len(self.orders)
        for index in range(quantity):
            number = start + index + 1
            order = {
                "orderNo": f"OR{number:04d}",
                "projectId": 2,
                "projectProductId": 22,
                "status": "active",
                "deliveryEmail": f"main{number}@outlook.com",
                "serviceToken": f"service-token-{number}",
            }
            self.orders.append(order)
            created.append(order)
        return created, []

    def get_order(self, order_no: str):
        return next(item for item in self.orders if item["orderNo"] == order_no)

    def list_orders(self, *, limit: int = 100):
        return list(self.orders)


def test_completed_main_child_is_prioritized_before_new_main(tmp_path: Path, monkeypatch):
    for name, value in {
        "REMAIL_ENABLED": True,
        "REMAIL_API_KEY": "rk-test",
        "REMAIL_BASE_URL": "https://remail.example",
        "REMAIL_PROJECT_ID": 2,
        "REMAIL_PRODUCT_ID": 0,
        "REMAIL_SERVICE_MODE": "purchase",
        "REMAIL_EMAIL_SUFFIX": "outlook.com",
        "REMAIL_EXPECTED_UNIT_POINTS": 10,
        "REMAIL_PURCHASE_BATCH_SIZE": 1,
        "REMAIL_MAX_PURCHASE_PER_ROUND": 8,
        "REMAIL_LOW_BALANCE_POINTS": 200,
        "REMAIL_LOW_BALANCE_ALERT_INTERVAL_SECONDS": 600,
        "REMAIL_FEISHU_WEBHOOK_URL": "",
    }.items():
        monkeypatch.setattr(remail_config, name, value)

    fake = _PriorityRemailClient()
    manager = RemailManager(
        state_path=tmp_path / "remail_state.json",
        client_factory=lambda: fake,
        clock=lambda: 1_800_000_000.0,
    )
    manager._recovered = True

    first = manager.prepare_reservations(1)
    assert [item["address_kind"] for item in first] == ["main"]
    manager.bind_reservation(first[0]["reservation_id"], 1)
    manager.mark_running(first[0]["reservation_id"], 1)
    manager.finish_reservation(first[0]["reservation_id"], success=True)

    second = manager.prepare_reservations(2)

    assert [item["address_kind"] for item in second] == ["child", "main"]
    assert second[0]["base_email"] == first[0]["email"]
    assert fake.created_quantities == [1, 1]


def test_running_main_does_not_make_child_eligible(tmp_path: Path, monkeypatch):
    for name, value in {
        "REMAIL_ENABLED": True,
        "REMAIL_API_KEY": "rk-test",
        "REMAIL_BASE_URL": "https://remail.example",
        "REMAIL_PROJECT_ID": 2,
        "REMAIL_PRODUCT_ID": 0,
        "REMAIL_SERVICE_MODE": "purchase",
        "REMAIL_EMAIL_SUFFIX": "outlook.com",
        "REMAIL_EXPECTED_UNIT_POINTS": 10,
        "REMAIL_PURCHASE_BATCH_SIZE": 1,
        "REMAIL_MAX_PURCHASE_PER_ROUND": 8,
        "REMAIL_LOW_BALANCE_POINTS": 200,
        "REMAIL_LOW_BALANCE_ALERT_INTERVAL_SECONDS": 600,
        "REMAIL_FEISHU_WEBHOOK_URL": "",
    }.items():
        monkeypatch.setattr(remail_config, name, value)

    fake = _PriorityRemailClient()
    manager = RemailManager(
        state_path=tmp_path / "remail_state.json",
        client_factory=lambda: fake,
        clock=lambda: 1_800_000_000.0,
    )
    manager._recovered = True

    first = manager.prepare_reservations(1)[0]
    manager.bind_reservation(first["reservation_id"], 1)
    manager.mark_running(first["reservation_id"], 1)

    second = manager.prepare_reservations(1)

    assert [item["address_kind"] for item in second] == ["main"]
    assert second[0]["remail_order_no"] != first["remail_order_no"]
    assert fake.created_quantities == [1, 1]
