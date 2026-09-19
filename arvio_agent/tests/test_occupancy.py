"""Software occupancy: fusion, HA publish, LAN forbidden, no MAC in sources."""
import json
import unittest
from unittest import mock

from helpers import agent, st


PROFILE = {
    "site_id": "site_villa",
    "hub_id": "hub_villa",
    "rev": 1,
    "t_home_s": 120,
    "members": [
        {"member_id": "mem_aaaaaaaaaaaaaaaa", "tracker_entity_ids": ["device_tracker.nick"], "stable_mac": True},
        {"member_id": "mem_bbbbbbbbbbbbbbbb", "tracker_entity_ids": ["device_tracker.maria"], "stable_mac": True},
    ],
}


class OccupancyFusionTest(unittest.TestCase):
    def test_two_home_then_leave_after_t(self):
        occ = agent.occupancy
        p = occ.parse_profile(PROFILE)
        t0 = 1_000_000
        home = occ.fuse_home_occupancy(
            p,
            [
                {"entity_id": "device_tracker.nick", "state": "home", "last_changed_ms": t0},
                {"entity_id": "device_tracker.maria", "state": "home", "last_changed_ms": t0},
            ],
            {"phase": "away", "since_ms": t0 - 60_000},
            t0,
        )
        self.assertEqual(home["phase"], "home")
        self.assertTrue(home["occupied"])
        self.assertEqual(home["event"], "home_entered")
        away_tr = [
            {"entity_id": "device_tracker.nick", "state": "not_home", "last_changed_ms": t0},
            {"entity_id": "device_tracker.maria", "state": "not_home", "last_changed_ms": t0},
        ]
        leaving = occ.fuse_home_occupancy(p, away_tr, {"phase": "home", "since_ms": t0}, t0)
        self.assertEqual(leaving["phase"], "leaving")
        self.assertTrue(leaving["occupied"])
        gone = occ.fuse_home_occupancy(p, away_tr, {"phase": "leaving", "since_ms": t0}, t0 + 120_000)
        self.assertEqual(gone["phase"], "away")
        self.assertFalse(gone["occupied"])
        self.assertEqual(gone["event"], "home_left")

    def test_private_mac_ignored(self):
        occ = agent.occupancy
        p = occ.parse_profile({
            **PROFILE,
            "members": [
                {"member_id": "mem_aaaaaaaaaaaaaaaa", "tracker_entity_ids": ["device_tracker.iphone"], "stable_mac": False},
            ],
        })
        snap = occ.fuse_home_occupancy(
            p,
            [{"entity_id": "device_tracker.iphone", "state": "home", "last_changed_ms": 1}],
            None,
            1,
        )
        self.assertEqual(snap["phase"], "unknown")
        self.assertIsNone(snap["occupied"])
        self.assertIn("private_mac:device_tracker.iphone", snap["issues"])

    def test_parse_rejects_mac(self):
        with self.assertRaises(ValueError) as cm:
            agent.occupancy.parse_profile({**PROFILE, "mac": "aa:bb:cc:dd:ee:ff"})
        self.assertIn("forbidden_field", str(cm.exception))


class OccupancyAgentTest(unittest.TestCase):
    def tearDown(self):
        for path in (agent.OCCUPANCY_PROFILE, agent.OCCUPANCY_STATE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        with agent.OCCUPANCY_LOCK:
            agent._OCCUPANCY_TRACKERS.clear()

    def test_relay_only(self):
        for a in ("arvio.occupancy_apply", "arvio.occupancy_remove", "arvio.occupancy_sources"):
            self.assertIn(a, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(a, agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.occupancy_apply"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")

    def test_apply_posts_states_and_home_entered(self):
        calls = []

        def fake_ha(path, method="GET", body=None, timeout=10):
            calls.append((method, path, body))
            if path == "/states" and method == "GET":
                return [
                    st("device_tracker.nick", "home", friendly_name="Nick", mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.9"),
                    st("device_tracker.maria", "home", friendly_name="Maria"),
                ]
            return {}

        with mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent.time, "time", return_value=1_000.0):
            out = agent.occupancy_apply(PROFILE)
        self.assertTrue(out["ok"])
        self.assertEqual(out["occupied_state"], "on")
        posted = [c for c in calls if c[0] == "POST"]
        paths = [c[1] for c in posted]
        self.assertIn("/states/binary_sensor.arvio_home_occupied", paths)
        self.assertIn("/states/sensor.arvio_home_occupancy_confidence", paths)
        self.assertIn("/events/arvio_presence.home_entered", paths)
        occ_body = next(c[2] for c in posted if c[1].endswith("arvio_home_occupied"))
        self.assertEqual(occ_body["state"], "on")
        self.assertEqual(occ_body["attributes"]["device_class"], "occupancy")
        self.assertEqual(occ_body["attributes"]["phase"], "home")

    def test_sources_omit_mac_and_ip(self):
        def fake_ha(path, method="GET", body=None, timeout=10):
            return [
                st("device_tracker.nick", "home", friendly_name="Nick", mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.2", source_type="router"),
                st("person.maria", "not_home", friendly_name="Maria"),
                st("light.hall", "on"),
            ]

        with mock.patch.object(agent, "ha", side_effect=fake_ha):
            out = agent.occupancy_sources({})
        ids = [s["entity_id"] for s in out["sources"]]
        self.assertEqual(ids, ["device_tracker.nick", "person.maria"])
        blob = json.dumps(out)
        self.assertNotIn("aa:bb:cc", blob)
        self.assertNotIn("10.0.0.2", blob)
        for s in out["sources"]:
            self.assertEqual(set(s), {"entity_id", "name", "state", "source_type"})

    def test_home_occupied_reaches_model_unlike_motion(self):
        occ = st("binary_sensor.arvio_home_occupied", "on", friendly_name="Arvio home occupied", device_class="occupancy")
        conf = st("sensor.arvio_home_occupancy_confidence", "90", friendly_name="Arvio home occupancy confidence")
        motion = st("binary_sensor.kinisi", "on", friendly_name="PIR", device_class="motion")
        self.assertIsNotNone(agent.entity_model_from_state(occ, {}, {}, {}))
        self.assertIsNotNone(agent.entity_model_from_state(conf, {}, {}, {}))
        self.assertIsNone(agent.entity_model_from_state(motion, {}, {}, {}))

    def test_restore_does_not_reemit_event(self):
        calls = []

        def fake_ha(path, method="GET", body=None, timeout=10):
            calls.append((method, path))
            if path == "/states" and method == "GET":
                return [st("device_tracker.nick", "home"), st("device_tracker.maria", "home")]
            return {}

        with mock.patch.object(agent, "ha", side_effect=fake_ha), \
             mock.patch.object(agent.time, "time", return_value=1_000.0):
            agent.occupancy_apply(PROFILE)
            calls.clear()
            agent.occupancy_restore()
        self.assertIn("/states/binary_sensor.arvio_home_occupied", [c[1] for c in calls if c[0] == "POST"])
        self.assertNotIn("/events/arvio_presence.home_entered", [c[1] for c in calls])


if __name__ == "__main__":
    unittest.main()
