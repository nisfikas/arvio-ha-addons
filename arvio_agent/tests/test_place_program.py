"""Place programs: compile artifacts to HA config, LAN forbidden, no delay."""
import unittest
from unittest import mock

from helpers import agent


STEM = "arvio_pp_aabbccdd"
APPLY = {
    "program_id": "pp_aabbccddeeff0011",
    "rev": 1,
    "area_id": "room_201",
    "scenes": [
        {"id": f"{STEM}_arr", "name": f"{STEM}_arr", "entities": {"light.a201": {"state": "on", "brightness": 102}}},
        {"id": f"{STEM}_vac", "name": f"{STEM}_vac", "entities": {"light.a201": {"state": "off"}}},
    ],
    "automations": [
        {
            "id": f"{STEM}_t_arr",
            "alias": f"{STEM}_t_arr",
            "mode": "queued",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.d201", "to": "on"}],
            "action": [{"action": "scene.turn_on", "target": {"entity_id": f"scene.{STEM}_arr"}}],
        },
        {
            "id": f"{STEM}_t_vac",
            "alias": f"{STEM}_t_vac",
            "mode": "restart",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.p201", "to": "off", "for": {"seconds": 300}}],
            "action": [{"action": "scene.turn_on", "target": {"entity_id": f"scene.{STEM}_vac"}}],
        },
    ],
}


class PlaceProgramTest(unittest.TestCase):
    def test_relay_only(self):
        for a in ("arvio.place_program_apply", "arvio.place_program_remove", "arvio.place_program_check"):
            self.assertIn(a, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(a, agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.place_program_apply"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")

    def test_apply_posts_scenes_then_automations_and_files_area(self):
        calls = []

        def fake_or_raise(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            return {}

        def fake_ha(path, method="GET", body=None, timeout=10):
            if path == "/states":
                return [
                    {"entity_id": f"scene.{STEM}_arr", "attributes": {"id": f"{STEM}_arr"}},
                    {"entity_id": f"scene.{STEM}_vac", "attributes": {"id": f"{STEM}_vac"}},
                ]
            return None

        ws = []
        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_or_raise), \
             mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent, "ha_ws_command", side_effect=lambda t, extra=None, timeout=20.0: ws.append((t, extra))), \
             mock.patch.object(agent, "schedule_registry_refresh"), \
             mock.patch.object(agent.time, "sleep"):
            out = agent.place_program_apply(APPLY)
        self.assertTrue(out["ok"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], f"/config/scene/config/{STEM}_arr")
        self.assertEqual(calls[1][1], f"/config/scene/config/{STEM}_vac")
        self.assertEqual(calls[2][1], f"/config/automation/config/{STEM}_t_arr")
        self.assertEqual(calls[3][1], f"/config/automation/config/{STEM}_t_vac")
        self.assertEqual(ws[0][0], "config/entity_registry/update")
        self.assertEqual(ws[0][1]["area_id"], "room_201")

    def test_refuses_delay_and_foreign_ids(self):
        bad = {
            **APPLY,
            "automations": [
                {
                    "id": f"{STEM}_t_vac",
                    "mode": "restart",
                    "trigger": [{"platform": "state", "entity_id": "binary_sensor.p201", "to": "off"}],
                    "action": [{"delay": {"seconds": 300}}, {"action": "scene.turn_on"}],
                }
            ],
        }
        with self.assertRaises(ValueError) as cm:
            agent.place_program_apply(bad)
        self.assertEqual(str(cm.exception), "delay_forbidden")
        foreign = {**APPLY, "scenes": [{"id": "arvio_vrady", "name": "x", "entities": {"light.a201": "on"}}]}
        with self.assertRaises(ValueError):
            agent.place_program_apply(foreign)

    def test_remove_only_stem_ids(self):
        calls = []

        def fake_or_raise(path, method="GET", body=None, timeout=30):
            calls.append((method, path))
            return {}

        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_or_raise), \
             mock.patch.object(agent, "schedule_registry_refresh"):
            out = agent.place_program_remove({
                "program_id": "pp_aabbccddeeff0011",
                "scene_ids": [f"{STEM}_arr", f"{STEM}_vac"],
                "automation_ids": [f"{STEM}_t_arr", f"{STEM}_t_vac"],
            })
            self.assertTrue(out["ok"])
            self.assertEqual(calls[0], ("DELETE", f"/config/scene/config/{STEM}_arr"))
            with self.assertRaises(ValueError):
                agent.place_program_remove({
                    "program_id": "pp_aabbccddeeff0011",
                    "scene_ids": ["arvio_old_201"],
                })

    def test_check_returns_entities_and_missing(self):
        def fake_ha(path, method="GET", body=None, timeout=10):
            if path.endswith(f"{STEM}_arr"):
                return {"id": f"{STEM}_arr", "entities": {"light.a201": {"state": "on"}}}
            return None

        with mock.patch.object(agent, "ha", side_effect=fake_ha):
            out = agent.place_program_check({
                "program_id": "pp_aabbccddeeff0011",
                "scene_ids": [f"{STEM}_arr", f"{STEM}_vac"],
                "automation_ids": [],
            })
        self.assertEqual(out["scenes"][0]["id"], f"{STEM}_arr")
        self.assertEqual(out["missing"], [f"{STEM}_vac"])
