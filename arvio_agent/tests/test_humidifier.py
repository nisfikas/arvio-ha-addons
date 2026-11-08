"""Dehumidifier card: humidifiers and dehumidifiers reach Home in HA's shape (docs/plans/2026-11-08-dehumidifier-card.md)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent

# What Core's Tuya driver reports for the Stardust 20L at Κρέσνας; an HA humidifier has the same names, without the step.
STARDUST = {"friendly_name": "Stardust 20L", "device_class": "dehumidifier", "humidity": 50, "current_humidity": 63,
            "min_humidity": 30, "max_humidity": 85, "target_humidity_step": 5, "mode": "normal",
            "available_modes": ["normal", "laundry", "sleep"]}


def state(st="on", **attrs):
    return {"entity_id": "humidifier.stardust_20l", "state": st, "attributes": {**STARDUST, **attrs}}


class HumidifierModelTest(unittest.TestCase):
    def test_in_the_home_and_activity_domains(self):
        self.assertIn("humidifier", agent.HOME_ENTITY_DOMAINS)
        self.assertIn("humidifier", agent.ACTIVITY_DOMAINS)

    def test_capabilities(self):
        self.assertEqual(agent.entity_capabilities("humidifier", STARDUST), {"humidity": True, "modes": True})
        # no bounds or no modes: nothing to set
        self.assertEqual(
            agent.entity_capabilities("humidifier", {"humidity": 50, "min_humidity": 30, "available_modes": []}),
            {"humidity": False, "modes": False},
        )

    def test_typed_attrs(self):
        out = agent.typed_attrs("humidifier", "on", STARDUST)
        self.assertEqual(
            out,
            {"humidity": 50, "current_humidity": 63, "min_humidity": 30, "max_humidity": 85, "target_humidity_step": 5,
             "mode": "normal", "available_modes": ["normal", "laundry", "sleep"], "action": None},
        )
        # HA: no step, an action of its own; anything that is not HA's action is none
        ha = agent.typed_attrs("humidifier", "on", {"humidity": 40, "action": "drying"})
        self.assertNotIn("target_humidity_step", ha)
        self.assertEqual(ha["action"], "drying")
        self.assertIsNone(agent.typed_attrs("humidifier", "on", {"action": "boiling"})["action"])
        self.assertEqual(agent.typed_attrs("humidifier", "off", {})["available_modes"], [])

    def test_entity_model(self):
        m = agent.entity_model_from_state(state(), None, {}, {})
        self.assertEqual((m["domain"], m["state"], m["device_class"]), ("humidifier", "on", "dehumidifier"))
        self.assertTrue(m["capabilities"]["humidity"])
        self.assertEqual(m["attrs"]["current_humidity"], 63)


class HumidifierCommandTest(unittest.TestCase):
    def test_actions_allowed_on_relay_and_lan(self):
        self.assertEqual(
            set(agent.HUMIDIFIER_ACTIONS),
            {"humidifier.turn_on", "humidifier.turn_off", "humidifier.toggle", "humidifier.set_humidity", "humidifier.set_mode"},
        )
        self.assertTrue(set(agent.HUMIDIFIER_ACTIONS) <= set(agent.AGENT_SERVICE_ALLOWLIST))
        self.assertTrue(set(agent.HUMIDIFIER_ACTIONS) <= set(agent.LAN_ALLOWED_ACTIONS))
        self.assertFalse(set(agent.HUMIDIFIER_ACTIONS) & set(agent.HOME_DANGEROUS_ACTIONS))
        for action in agent.HUMIDIFIER_ACTIONS:
            agent.check_command_safety({"action": action, "entity_id": "humidifier.stardust_20l"}, via="lan")

    def test_service_data(self):
        build = agent.build_service_data
        self.assertEqual(build("humidifier.turn_on", "humidifier.x", {"humidity": 50}), ("turn_on", {"entity_id": "humidifier.x"}))
        self.assertEqual(build("humidifier.set_humidity", "humidifier.x", {"humidity": 45, "extra": 1}),
                         ("set_humidity", {"entity_id": "humidifier.x", "humidity": 45}))
        self.assertEqual(build("humidifier.set_humidity", "humidifier.x", {"humidity": 45.0})[1]["humidity"], 45)
        for bad in ({}, {"humidity": "50"}, {"humidity": True}, {"humidity": 45.5}, {"humidity": 101}, {"humidity": -1},
                    {"humidity": float("inf")}, {"humidity": float("nan")}):
            with self.assertRaises(ValueError, msg=bad):
                build("humidifier.set_humidity", "humidifier.x", bad)
        self.assertEqual(build("humidifier.set_mode", "humidifier.x", {"mode": "sleep"}), ("set_mode", {"entity_id": "humidifier.x", "mode": "sleep"}))
        for bad in ({}, {"mode": ""}, {"mode": 3}):
            with self.assertRaises(ValueError):
                build("humidifier.set_mode", "humidifier.x", bad)
        # on / off confirm through the state; the target and the mode are the driver's to confirm
        self.assertEqual(agent._expected_state_for("humidifier", "turn_on"), "on")
        self.assertEqual(agent._expected_state_for("humidifier", "turn_off"), "off")
        self.assertIsNone(agent._expected_state_for("humidifier", "set_humidity"))

    def test_within_the_device_bounds_and_modes(self):
        check = agent.check_humidifier_data
        check("humidifier.set_humidity", {"humidity": 30}, STARDUST)
        check("humidifier.set_humidity", {"humidity": 85}, STARDUST)
        for h in (25, 90):
            with self.assertRaises(ValueError):
                check("humidifier.set_humidity", {"humidity": h}, STARDUST)
        check("humidifier.set_humidity", {"humidity": 90}, {})  # bounds unknown: the driver judges
        check("humidifier.set_mode", {"mode": "laundry"}, STARDUST)
        for attrs in (STARDUST, {}):
            with self.assertRaises(ValueError):
                check("humidifier.set_mode", {"mode": "turbo"}, attrs)

    def test_execute_reads_the_live_bounds(self):
        with mock.patch.object(agent, "ha", return_value=state()), \
             mock.patch.object(agent, "call_service", return_value={"ok": True}) as cs:
            agent.execute_action("humidifier.set_humidity", "humidifier.stardust_20l", {"humidity": 55})
            cs.assert_called_once_with("humidifier", "set_humidity", {"entity_id": "humidifier.stardust_20l", "humidity": 55})
            with self.assertRaises(ValueError):
                agent.execute_action("humidifier.set_humidity", "humidifier.stardust_20l", {"humidity": 90})
            self.assertEqual(cs.call_count, 1)


class HumidifierLanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_only_humidity_and_mode_pass(self):
        agent.COMMAND_ACKS = agent.LRU(500)
        calls = []

        def fake_execute(action, entity_id, payload=None, target=None):
            calls.append((action, payload))
            return {"ok": True, "entity_id": entity_id, "state": "on", "affected_entity_ids": [entity_id]}

        with mock.patch.object(agent, "hub_id", "hub_lan"), \
             mock.patch.object(agent, "lan_door_refusal", return_value=None), \
             mock.patch.object(agent, "execute_action", side_effect=fake_execute):
            body = {"action": "humidifier.set_humidity", "entity_id": "humidifier.stardust_20l",
                    "payload": {"humidity": 50, "mode": "sleep", "confirm_dangerous": True, "code": "1234"}}
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/hubs/hub_lan/commands",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
        self.assertEqual(calls, [("humidifier.set_humidity", {"humidity": 50, "mode": "sleep"})])


if __name__ == "__main__":
    unittest.main()
