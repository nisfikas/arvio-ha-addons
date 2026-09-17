"""Light Group helper via HA group config flow. Agent 0.1.30."""
import unittest
from unittest import mock

from helpers import agent


class LightGroupParseTest(unittest.TestCase):
    def test_two_lights(self):
        name, ids, area = agent.parse_light_group_payload(
            {"name": "  Σαλόνι  ", "entity_ids": ["light.a", "light.a", "light.b"], "area_id": "saloni"}
        )
        self.assertEqual(name, "Σαλόνι")
        self.assertEqual(ids, ["light.a", "light.b"])
        self.assertEqual(area, "saloni")

    def test_rejects_bad_lists(self):
        with self.assertRaises(ValueError):
            agent.parse_light_group_payload({"name": "A", "entity_ids": ["light.a"]})
        with self.assertRaises(ValueError):
            agent.parse_light_group_payload({"name": "A", "entity_ids": ["light.a", "switch.b"]})
        with self.assertRaises(ValueError):
            agent.parse_light_group_payload({"name": "", "entity_ids": ["light.a", "light.b"]})

    def test_relay_only(self):
        self.assertIn("arvio.light_group", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertNotIn("arvio.light_group", agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.light_group"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")


class LightGroupFlowTest(unittest.TestCase):
    def test_menu_then_form_create(self):
        posts = []

        def fake_or_raise(path, method="GET", body=None, timeout=30):
            posts.append((method, path, body))
            if method == "POST" and path == "/config/config_entries/flow":
                return {
                    "type": "menu",
                    "flow_id": "flowgrp001aa",
                    "step_id": "user",
                    "menu_options": ["light", "switch"],
                }
            if method == "POST" and path.endswith("/flowgrp001aa") and body and body.get("next_step_id") == "light":
                return {"type": "form", "flow_id": "flowgrp001aa", "step_id": "light"}
            if method == "POST" and body and body.get("entities"):
                self.assertEqual(body["hide_members"], False)
                self.assertEqual(body["name"], "Σαλόνι φώτα")
                return {"type": "create_entry", "title": body["name"], "flow_id": "flowgrp001aa"}
            raise AssertionError((method, path, body))

        def fake_ha(path, method="GET", body=None, timeout=10):
            if path == "/states":
                return [
                    {"entity_id": "light.sofa", "attributes": {"friendly_name": "Καναπές"}},
                    {
                        "entity_id": "light.saloni_fota",
                        "attributes": {"friendly_name": "Σαλόνι φώτα"},
                    },
                ]
            return None

        ws = []

        def fake_ws(msg_type, extra=None, timeout=20.0):
            ws.append((msg_type, extra))
            return None

        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_or_raise), \
             mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent, "ha_ws_command", side_effect=fake_ws), \
             mock.patch.object(agent, "schedule_registry_refresh"), \
             mock.patch.object(agent.time, "sleep"):
            out = agent.light_group(
                {
                    "name": "Σαλόνι φώτα",
                    "entity_ids": ["light.sofa", "light.orofi"],
                    "area_id": "saloni",
                }
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["entity_id"], "light.saloni_fota")
        self.assertEqual(ws[-1][0], "config/entity_registry/update")
        self.assertEqual(ws[-1][1]["area_id"], "saloni")
        self.assertEqual(posts[0][2], {"handler": "group"})
