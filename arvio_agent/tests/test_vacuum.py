"""Vacuum card: the robot vacuum reaches Home, screens and wall panels in HA's shape."""
import unittest

from helpers import agent

IMOU_FEATURES = 4 | 8 | 16 | 32 | 64 | 512 | 4096 | 8192  # what systems.arvio.imou reports for the RV1 Pro Max


def state(st="docked", **attrs):
    base = {"friendly_name": "RV1 Pro Max-0310", "supported_features": IMOU_FEATURES, "battery_level": 80,
            "status": "charging", "fan_speed": "max", "fan_speed_list": ["quiet", "standard", "strong", "max"],
            "mop_fitted": False, "mop_level": "strong", "mop_level_list": ["gentle", "standard", "strong"],
            "cleaned_area": 28, "cleaning_time": 25}
    base.update(attrs)
    return {"entity_id": "vacuum.rv1_pro_max_0310", "state": st, "attributes": base}


class VacuumModelTest(unittest.TestCase):
    def test_in_the_home_domains(self):
        self.assertIn("vacuum", agent.HOME_ENTITY_DOMAINS)

    def test_capabilities_from_ha_feature_bits(self):
        self.assertEqual(
            agent.entity_capabilities("vacuum", {"supported_features": IMOU_FEATURES}),
            {"start": True, "pause": True, "stop": True, "return_home": True, "locate": True, "fan_speed": True},
        )
        # a vacuum that cannot pause or be located (no bits) shows no such buttons
        self.assertEqual(
            agent.entity_capabilities("vacuum", {"supported_features": 8192 | 16}),
            {"start": True, "pause": False, "stop": False, "return_home": True, "locate": False, "fan_speed": False},
        )

    def test_typed_attrs(self):
        out = agent.typed_attrs("vacuum", "docked", state()["attributes"])
        self.assertEqual(out["battery_level"], 80)
        self.assertEqual(out["status"], "charging")
        self.assertEqual(out["fan_speed"], "max")
        self.assertEqual(out["fan_speed_list"], ["quiet", "standard", "strong", "max"])
        self.assertIs(out["mop_fitted"], False)
        self.assertEqual(out["mop_level"], "strong")
        self.assertEqual(out["mop_level_list"], ["gentle", "standard", "strong"])
        # a robot that says nothing about a mop: nothing to show
        bare = agent.typed_attrs("vacuum", "docked", {})
        self.assertIsNone(bare["mop_fitted"])
        self.assertEqual(bare["mop_level_list"], [])
        self.assertEqual((out["cleaned_area"], out["cleaning_time"]), (28, 25))
        self.assertEqual(out["supported_features"], IMOU_FEATURES)
        # the Imou reports -100 while charging: unknown, never 0 %
        self.assertIsNone(agent.typed_attrs("vacuum", "docked", {"battery_level": -100})["battery_level"])
        self.assertEqual(agent.typed_attrs("vacuum", "docked", {"battery_level": 140})["battery_level"], 100)

    def test_entity_model(self):
        m = agent.entity_model_from_state(state("cleaning"), None, {}, {})
        self.assertIsNotNone(m)
        self.assertEqual(m["domain"], "vacuum")
        self.assertEqual(m["state"], "cleaning")
        self.assertTrue(m["capabilities"]["start"])
        self.assertEqual(m["attrs"]["battery_level"], 80)


class VacuumCommandTest(unittest.TestCase):
    def test_actions_allowed_on_relay_and_lan(self):
        self.assertEqual(
            set(agent.VACUUM_ACTIONS),
            {"vacuum.start", "vacuum.pause", "vacuum.stop", "vacuum.return_to_base", "vacuum.locate", "vacuum.set_fan_speed", "vacuum.set_mop_level"},
        )
        self.assertTrue(set(agent.VACUUM_ACTIONS) <= set(agent.AGENT_SERVICE_ALLOWLIST))
        self.assertTrue(set(agent.VACUUM_ACTIONS) <= set(agent.LAN_ALLOWED_ACTIONS))
        for action in agent.VACUUM_ACTIONS:
            agent.check_command_safety({"action": action, "entity_id": "vacuum.rv1_pro_max_0310"}, via="lan")
        self.assertNotIn("vacuum.send_command", agent.AGENT_SERVICE_ALLOWLIST)

    def test_confirmation_follows_the_state(self):
        exp = agent._expected_state_for
        ok = agent._state_matches
        self.assertEqual(exp("vacuum", "start"), "cleaning")
        self.assertTrue(ok("vacuum", "cleaning", "cleaning"))
        self.assertFalse(ok("vacuum", "cleaning", "docked"))
        self.assertTrue(ok("vacuum", exp("vacuum", "pause"), "paused"))
        self.assertTrue(ok("vacuum", exp("vacuum", "return_to_base"), "returning"))
        self.assertTrue(ok("vacuum", exp("vacuum", "return_to_base"), "docked"))
        self.assertTrue(ok("vacuum", exp("vacuum", "stop"), "docked"))
        self.assertIsNone(exp("vacuum", "locate"))  # no state: acked on the call

    def test_service_data_is_just_the_entity(self):
        self.assertEqual(agent.build_service_data("vacuum.start", "vacuum.x", {}), ("start", {"entity_id": "vacuum.x"}))

    def test_settings_carry_their_value_and_need_one(self):
        self.assertEqual(
            agent.build_service_data("vacuum.set_fan_speed", "vacuum.x", {"fan_speed": "quiet", "extra": 1}),
            ("set_fan_speed", {"entity_id": "vacuum.x", "fan_speed": "quiet"}),
        )
        self.assertEqual(
            agent.build_service_data("vacuum.set_mop_level", "vacuum.x", {"level": "gentle"}),
            ("set_mop_level", {"entity_id": "vacuum.x", "level": "gentle"}),
        )
        with self.assertRaises(ValueError):
            agent.build_service_data("vacuum.set_fan_speed", "vacuum.x", {})
        with self.assertRaises(ValueError):
            agent.build_service_data("vacuum.set_mop_level", "vacuum.x", {})
        # the settings change no state: no state to wait for
        self.assertIsNone(agent._expected_state_for("vacuum", "set_fan_speed"))
        self.assertIsNone(agent._expected_state_for("vacuum", "set_mop_level"))


if __name__ == "__main__":
    unittest.main()
