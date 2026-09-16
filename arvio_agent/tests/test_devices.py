"""Partner device registry writes — documented HA WS only, never LAN."""
import unittest
from unittest import mock

from helpers import agent


class DeviceRegistryParseTest(unittest.TestCase):
    def test_ids_and_extra(self):
        did, eid = agent.parse_device_registry_ids(
            {"device_id": "abc123def456", "entity_id": "light.saloni"}
        )
        self.assertEqual((did, eid), ("abc123def456", "light.saloni"))
        with self.assertRaises(ValueError):
            agent.parse_device_registry_ids({"device_id": "../x"})
        with self.assertRaises(ValueError):
            agent.parse_device_registry_ids({"entity_id": "not-an-id"})
        extra = agent.parse_device_update_extra({"name": "  Καναπές  ", "area_id": "saloni"})
        self.assertEqual(extra["name_by_user"], "Καναπές")
        self.assertEqual(extra["area_id"], "saloni")
        self.assertEqual(agent.parse_device_update_extra({"area_id": None})["area_id"], None)
        with self.assertRaises(ValueError):
            agent.parse_device_update_extra({})
        with self.assertRaises(ValueError):
            agent.parse_device_update_extra({"name": ""})

    def test_update_and_remove_call_documented_ws(self):
        calls = []

        def fake_ws(msg_type, extra=None, timeout=20.0):
            calls.append((msg_type, extra))
            return None

        with mock.patch.object(agent, "ha_ws_command", side_effect=fake_ws), \
             mock.patch.object(agent, "schedule_registry_refresh"):
            out = agent.device_update({"device_id": "dev_kitchen_01", "name": "Κουζίνα"})
            self.assertTrue(out["ok"])
            self.assertEqual(calls[-1][0], "config/device_registry/update")
            self.assertEqual(calls[-1][1]["device_id"], "dev_kitchen_01")
            self.assertEqual(calls[-1][1]["name_by_user"], "Κουζίνα")
            out = agent.device_update({"entity_id": "light.saloni", "area_id": "saloni"})
            self.assertEqual(calls[-1][0], "config/entity_registry/update")
            self.assertEqual(calls[-1][1]["entity_id"], "light.saloni")
            out = agent.device_remove({"device_id": "dev_kitchen_01"})
            self.assertEqual(calls[-1][0], "config/device_registry/remove")
            out = agent.device_remove({}, "light.orphan")
            self.assertEqual(calls[-1][0], "config/entity_registry/remove")

    def test_not_on_lan_allowlist(self):
        self.assertIn("arvio.device_update", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.device_remove", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertNotIn("arvio.device_update", agent.LAN_ALLOWED_ACTIONS)
        self.assertNotIn("arvio.device_remove", agent.LAN_ALLOWED_ACTIONS)


class HaUiPathTest(unittest.TestCase):
    def test_safe_path_blocks_supervisor_and_traversal(self):
        self.assertEqual(agent.ha_ui_safe_path("/"), "/")
        self.assertEqual(agent.ha_ui_safe_path("/lovelace"), "/lovelace")
        self.assertEqual(agent.ha_ui_safe_path("/api/websocket"), "/api/websocket")
        self.assertIsNone(agent.ha_ui_safe_path("/supervisor/info"))
        self.assertEqual(agent.ha_ui_safe_path("/api/hassio/addons"), "/api/hassio/addons")
        self.assertEqual(
            agent.ha_ui_safe_path("/api/hassio_ingress/slug/"),
            "/api/hassio_ingress/slug/",
        )
        self.assertNotIn("authorization", agent.HA_UI_HOP)
        self.assertIsNone(agent.ha_ui_safe_path("/v1/hub/ws"))
        self.assertIsNone(agent.ha_ui_safe_path("/static/../data"))
        self.assertIsNone(agent.ha_ui_safe_path("http://evil"))
        self.assertEqual(agent.ha_ui_safe_query("foo=1&bar=2"), "foo=1&bar=2")
        self.assertEqual(agent.ha_ui_safe_query("x=;rm"), "")
