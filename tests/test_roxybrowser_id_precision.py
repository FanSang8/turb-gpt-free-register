import json
import unittest
from unittest.mock import patch

from core.roxybrowser_client import RoxyBrowserClient, _cfg


class RoxyIdPrecisionTests(unittest.TestCase):
    workspace = "35405925685260288"
    project = "35405925756563456"
    profile = "35405925756563457"

    def setUp(self):
        for key, value in {
            "ROXY_WORKSPACE_ID": self.workspace,
            "ROXY_PROJECT_ID": self.project,
            "ROXY_CREATE_USE_PROXY_POOL": False,
            "ROXY_PROFILE_CREATE_PAYLOAD": {},
        }.items():
            context = patch.object(_cfg, key, value)
            context.start()
            self.addCleanup(context.stop)
        self.client = RoxyBrowserClient(token="")
        self.calls = []

        def request(method, path, *, params=None, json_body=None):
            body = json.loads(json.dumps(json_body if json_body is not None else params))
            self.calls.append((path, body))
            return {
                "code": 0,
                "data": {"dirId": self.profile, "debuggerAddress": "127.0.0.1:9222"},
            }

        self.client.request = request

    @patch("core.roxybrowser_client._wait_for_create_slot")
    def test_create_keeps_workspace_and_project_as_strings(self, _wait):
        self.assertEqual(self.client.create_profile(), self.profile)
        body = self.calls[-1][1]
        self.assertEqual(body["workspaceId"], self.workspace)
        self.assertEqual(body["projectId"], self.project)

    def test_open_close_delete_keep_profile_id_as_string(self):
        with patch.object(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False):
            self.client.open_profile(self.profile)
        self.client.close_profile(self.profile)
        self.client.delete_profile(self.profile)
        self.assertEqual(len(self.calls), 3)
        for path, body in self.calls:
            self.assertEqual(body["workspaceId"], self.workspace)
            if path == _cfg.ROXY_DELETE_PATH:
                self.assertEqual(body["dirIds"], [self.profile])
            else:
                self.assertEqual(body["dirId"], self.profile)


if __name__ == "__main__":
    unittest.main()
