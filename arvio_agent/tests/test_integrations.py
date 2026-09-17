"""Partner add-device — HA config flows without HA UI. Agent 0.1.30."""
import unittest
from unittest import mock

from helpers import agent


class IntegrationAllowlistTest(unittest.TestCase):
    def test_relay_only(self):
        for action in (
            "arvio.integration_setup",
            "arvio.integration_discovery",
            "arvio.integration_handlers",
        ):
            self.assertIn(action, agent.AGENT_SERVICE_ALLOWLIST, action)
            self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS, action)
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": action}, via="lan")
            self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.integration_handlers"})


class IntegrationHandlersTest(unittest.TestCase):
    def test_strips_core_and_cloud(self):
        with mock.patch.object(
            agent,
            "ha_or_raise",
            return_value=["hue", "onvif", "cloud", "hassio", "mqtt"],
        ):
            out = agent.integration_handlers()
        self.assertTrue(out["ok"])
        self.assertEqual([h["handler"] for h in out["handlers"]], ["hue", "mqtt", "onvif"])


class IntegrationDiscoveryTest(unittest.TestCase):
    def test_ws_progress_drops_unique_id(self):
        raw = [
            {
                "flow_id": "flow_huehub01",
                "handler": "hue",
                "step_id": "discovery_confirm",
                "context": {"source": "zeroconf", "unique_id": "secret-mac"},
            },
            {"flow_id": "nope", "handler": "hue"},
            {"flow_id": "flow_cloud001", "handler": "cloud"},
        ]
        with mock.patch.object(agent, "ha_ws_command", return_value=raw) as ws:
            out = agent.integration_discovery()
        ws.assert_called_once_with("config_entries/flow/progress")
        self.assertEqual(len(out["flows"]), 1)
        self.assertEqual(out["flows"][0]["handler"], "hue")
        self.assertEqual(out["flows"][0]["source"], "zeroconf")
        self.assertNotIn("secret-mac", str(out))


class IntegrationSetupTest(unittest.TestCase):
    def test_starts_continues_aborts_and_renders_unknown_steps(self):
        calls = []

        def fake_ha(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            if path == "/config/config_entries/flow" and method == "POST":
                return {
                    "type": "form",
                    "flow_id": "flow_abc12345",
                    "handler": "hue",
                    "step_id": "init",
                    "errors": {},
                    "data_schema": [
                        {"name": "host", "type": "string", "default": "1.2.3.4"},
                        {"name": "password", "type": "string", "default": "leak-me"},
                    ],
                }
            if path.endswith("/flow_abc12345") and method == "GET":
                return {
                    "type": "form",
                    "flow_id": "flow_abc12345",
                    "handler": "hue",
                    "step_id": "discovery_confirm",
                    "data_schema": [],
                }
            if path.endswith("/flow_abc12345") and method == "POST":
                return {"type": "create_entry", "title": "Hue Bridge"}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path))

        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_ha):
            start = agent.integration_setup({"handler": "hue"})
            self.assertEqual(start["phase"], "form")
            self.assertEqual(start["step_id"], "init")
            self.assertEqual(start["data_schema"][0]["default"], "1.2.3.4")
            self.assertNotIn("default", start["data_schema"][1])
            peeked = agent.integration_setup({"flow_id": "flow_abc12345"})
            self.assertEqual(peeked["step_id"], "discovery_confirm")
            created = agent.integration_setup({
                "flow_id": "flow_abc12345",
                "user_input": {"host": "1.2.3.4"},
            })
            self.assertEqual(created["phase"], "created")
            aborted = agent.integration_setup({"flow_id": "flow_abc12345", "abort": True})
            self.assertEqual(aborted["phase"], "abort")
            menu = None

        self.assertEqual(calls[0][1], "/config/config_entries/flow")
        self.assertEqual(calls[0][2]["handler"], "hue")
        with self.assertRaises(ValueError):
            agent.integration_setup({"handler": "cloud"})
        with mock.patch.object(
            agent,
            "ha_or_raise",
            return_value={
                "type": "external",
                "flow_id": "flow_abc12345",
                "handler": "google_assistant",
                "url": "https://accounts.google.com/o/oauth",
            },
        ):
            ext = agent.integration_setup({"handler": "google_assistant"})
        self.assertEqual(ext["phase"], "sfk")
        self.assertNotIn("accounts.google.com", str(ext))
        with mock.patch.object(
            agent,
            "ha_or_raise",
            return_value={
                "type": "menu",
                "flow_id": "flow_abc12345",
                "handler": "mqtt",
                "step_id": "user",
                "menu_options": ["broker", "addon"],
            },
        ):
            menu = agent.integration_setup({"handler": "mqtt"})
        self.assertEqual(menu["phase"], "menu")
        self.assertEqual(menu["menu_options"][0]["value"], "broker")
