"""Arvio scenes via HA scene-config API + Hue dimming capability. Agent 0.1.32."""
import unittest
from unittest import mock

from helpers import agent


class DimmableCapabilityTest(unittest.TestCase):
    def test_hue_colour_bulb_off_is_still_dimmable(self):
        # Hue reports ["color_temp", "xy"]; when OFF there is no `brightness` attribute.
        caps = agent.entity_capabilities("light", {"supported_color_modes": ["color_temp", "xy"]})
        self.assertTrue(caps["brightness"])
        self.assertTrue(caps["color"])
        self.assertTrue(caps["color_temp"])
        caps = agent.entity_capabilities("light", {"supported_color_modes": ["color_temp"]})
        self.assertTrue(caps["brightness"])
        self.assertFalse(caps["color"])

    def test_onoff_only_is_not_dimmable(self):
        caps = agent.entity_capabilities("light", {"supported_color_modes": ["onoff"]})
        self.assertFalse(caps["brightness"])
        self.assertFalse(agent.light_modes_dimmable(["unknown"]))
        self.assertFalse(agent.light_modes_dimmable("brightness"))
        self.assertTrue(agent.light_modes_dimmable(["brightness"]))


class SceneParseTest(unittest.TestCase):
    def test_accepts_ha_shaped_entities(self):
        sid, name, area, icon, ents = agent.parse_scene_payload(
            {
                "id": "arvio_vrady",
                "name": " Βράδυ ",
                "area_id": "saloni",
                "entities": {
                    "light.sofa": {"state": "on", "brightness": 102, "color_temp_kelvin": 2700},
                    "light.off": {"state": "off", "brightness": 200},
                    "switch.plug": "off",
                    "cover.rolo": {"state": "open", "current_position": 50},
                },
            }
        )
        self.assertEqual((sid, name, area, icon), ("arvio_vrady", "Βράδυ", "saloni", None))
        self.assertEqual(ents["light.sofa"], {"state": "on", "brightness": 102, "color_temp_kelvin": 2700})
        self.assertEqual(ents["light.off"], {"state": "off"})
        self.assertEqual(ents["switch.plug"], {"state": "off"})
        self.assertEqual(ents["cover.rolo"], {"state": "open", "current_position": 50})

    def test_rejects_locks_foreign_ids_and_bad_values(self):
        base = {"id": "arvio_x", "name": "A"}
        with self.assertRaises(ValueError):
            agent.parse_scene_payload({**base, "entities": {"lock.door": {"state": "on"}}})
        with self.assertRaises(ValueError):
            agent.parse_scene_payload({"id": "movie", "name": "A", "entities": {"light.a": "on"}})
        with self.assertRaises(ValueError):
            agent.parse_scene_payload({**base, "entities": {"light.a": {"state": "on", "brightness": 0}}})
        with self.assertRaises(ValueError):
            agent.parse_scene_payload({**base, "entities": {"light.a": {"state": "on", "rgb_color": [1, 2, 3], "color_temp_kelvin": 3000}}})
        with self.assertRaises(ValueError):
            agent.parse_scene_payload({**base, "entities": {}})

    def test_relay_only(self):
        for a in ("arvio.upsert_scene", "arvio.delete_scene", "arvio.scene_config"):
            self.assertIn(a, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(a, agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.upsert_scene"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")

    def test_typed_attrs_exposes_scene_id(self):
        out = agent.typed_attrs("scene", "unknown", {"id": "arvio_vrady", "entity_id": ["light.a"]})
        self.assertEqual(out["scene_id"], "arvio_vrady")
        self.assertEqual(out["scene_entity_ids"], ["light.a"])
        self.assertIsNone(agent.typed_attrs("scene", "unknown", {})["scene_id"])


class SceneFlowTest(unittest.TestCase):
    def test_upsert_posts_config_then_files_scene_in_room(self):
        calls = []

        def fake_or_raise(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            return {}

        def fake_ha(path, method="GET", body=None, timeout=10):
            if path == "/states":
                return [
                    {"entity_id": "scene.vrady", "attributes": {"id": "arvio_vrady", "friendly_name": "Βράδυ"}},
                    {"entity_id": "scene.other", "attributes": {"id": "1234"}},
                ]
            return None

        ws = []
        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_or_raise), \
             mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent, "ha_ws_command", side_effect=lambda t, extra=None, timeout=20.0: ws.append((t, extra))), \
             mock.patch.object(agent, "schedule_registry_refresh"), \
             mock.patch.object(agent.time, "sleep"):
            out = agent.upsert_scene(
                {
                    "id": "arvio_vrady",
                    "name": "Βράδυ",
                    "area_id": "saloni",
                    "entities": {"light.sofa": {"state": "on", "brightness": 102}},
                }
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["entity_id"], "scene.vrady")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/config/scene/config/arvio_vrady")
        self.assertEqual(calls[0][2]["entities"]["light.sofa"], {"state": "on", "brightness": 102})
        self.assertNotIn("area_id", calls[0][2])
        self.assertEqual(ws[-1], ("config/entity_registry/update", {"entity_id": "scene.vrady", "area_id": "saloni"}))

    def test_delete_resolves_id_from_entity_and_refuses_foreign(self):
        calls = []

        def fake_or_raise(path, method="GET", body=None, timeout=30):
            calls.append((method, path))
            return {}

        def fake_ha(path, method="GET", body=None, timeout=10):
            if path == "/states/scene.vrady":
                return {"attributes": {"id": "arvio_vrady"}}
            if path == "/states/scene.hue":
                return {"attributes": {"id": "hue_movie"}}
            return None

        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_or_raise), \
             mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent, "schedule_registry_refresh"):
            out = agent.delete_scene({}, "scene.vrady")
            self.assertEqual(out, {"ok": True, "id": "arvio_vrady", "deleted": True})
            self.assertEqual(calls, [("DELETE", "/config/scene/config/arvio_vrady")])
            with self.assertRaises(ValueError):
                agent.delete_scene({}, "scene.hue")
