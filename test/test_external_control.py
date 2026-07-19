from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.external_control import ExternalRegisterController
from webui.app import create_app


class ExternalControlTests(unittest.TestCase):
    def test_reconcile_only_fills_target_inflight(self) -> None:
        controller = ExternalRegisterController()
        jobs = [{"status": "running"}, {"status": "pending"}, {"status": "success"}]
        with (
            patch("core.external_control.db.list_jobs", side_effect=lambda limit=100000: list(jobs)),
            patch(
                "core.external_control.registration.submit_registration",
                return_value=[{"id": 4}, {"id": 5}],
            ) as submit,
            patch("core.external_control.registration.get_executor_workers", return_value=2),
        ):
            result = controller.reconcile({
                "enabled": True,
                "target_inflight": 4,
                "workers": 2,
                "min_interval_seconds": 0,
                "reason": "pending_requests_need_accounts",
            })

        submit.assert_called_once_with(count=2, email_source="external", workers=2)
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["target_inflight"], 4)
        self.assertTrue(result["enabled"])

    def test_disabled_control_does_not_submit(self) -> None:
        controller = ExternalRegisterController()
        with (
            patch("core.external_control.db.list_jobs", return_value=[]),
            patch("core.external_control.registration.submit_registration") as submit,
            patch("core.external_control.registration.get_executor_workers", return_value=1),
        ):
            result = controller.reconcile({"enabled": False, "target_inflight": 4})
        submit.assert_not_called()
        self.assertFalse(result["enabled"])

    def test_http_control_endpoint_requires_token(self) -> None:
        with (
            patch.dict(os.environ, {"REGISTER_CONTROL_TOKEN": "secret"}),
            patch("webui.app.external_register_controller.status", return_value={"active": 0}),
        ):
            client = create_app().test_client()
            self.assertEqual(client.get("/api/external/control/status").status_code, 401)
            response = client.get(
                "/api/external/control/status",
                headers={"Authorization": "Bearer secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
