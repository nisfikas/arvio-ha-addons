"""Mode B geofence: a phone's home / not_home drives a per-member tracker and the home occupancy."""
import json
import unittest
from unittest import mock

from helpers import agent

NICK = "mem_aaaaaaaaaaaaaaaa"
MARIA = "mem_bbbbbbbbbbbbbbbb"


class GeofenceTest(unittest.TestCase):
    def setUp(self):
        for path in (agent.GEOFENCE_FILE, agent.OCCUPANCY_PROFILE, agent.OCCUPANCY_STATE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        agent._OCCUPANCY_TRACKERS.clear()
        self.calls = []
        self.clock = 1_000.0

    def fake_ha(self, path, method="GET", body=None, timeout=10):
        self.calls.append((method, path, body))
        if path == "/states" and method == "GET":
            return []
        return {}

    def report(self, member, state, name=None):
        with mock.patch.object(agent, "ha", side_effect=self.fake_ha), \
             mock.patch.object(agent.time, "time", return_value=self.clock):
            return agent.presence_report({"member_id": member, "state": state, "name": name})

    def posted(self, path):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1] == path]

    def test_first_arrives_last_leaves(self):
        out = self.report(NICK, "home", "Νικόλας")
        self.assertEqual(out["entity_id"], "device_tracker.arvio_" + NICK)
        tracker = self.posted("/states/device_tracker.arvio_" + NICK)[-1]
        self.assertEqual(tracker["state"], "home")
        self.assertEqual(tracker["attributes"]["friendly_name"], "Νικόλας")
        self.assertEqual(out["occupied_state"], "on")
        self.assertIn("/events/arvio_presence.home_entered", [c[1] for c in self.calls])

        self.report(MARIA, "home", "Μαρία")
        self.report(NICK, "not_home")
        # Maria is still in: the house stays occupied.
        self.assertEqual(self.posted("/states/binary_sensor.arvio_home_occupied")[-1]["state"], "on")

        self.calls.clear()
        out = self.report(MARIA, "not_home")
        self.assertEqual(out["occupied_state"], "on")  # leaving: the grace period runs first
        self.clock += 181
        with mock.patch.object(agent, "ha", side_effect=self.fake_ha), \
             mock.patch.object(agent.time, "time", return_value=self.clock):
            agent.occupancy_refresh()
        self.assertEqual(self.posted("/states/binary_sensor.arvio_home_occupied")[-1]["state"], "off")
        self.assertIn("/events/arvio_presence.home_left", [c[1] for c in self.calls])

    def test_off_drops_to_unknown_and_never_invents_away(self):
        self.report(NICK, "home")
        self.calls.clear()
        out = self.report(NICK, "off")
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(self.posted("/states/device_tracker.arvio_" + NICK)[-1]["state"], "unknown")
        self.assertEqual(json.loads(agent.GEOFENCE_FILE.read_text()), {})

    def test_partner_profile_keeps_its_bindings(self):
        agent.OCCUPANCY_PROFILE.write_text(json.dumps({
            "site_id": "site_villa", "hub_id": "hub_villa", "rev": 1, "t_home_s": 120,
            "members": [{"member_id": MARIA, "tracker_entity_ids": ["device_tracker.maria"], "stable_mac": True}],
        }))
        self.report(NICK, "home")
        profile = agent.load_occupancy_profile()
        trackers = [t for m in profile["members"] for t in m["tracker_entity_ids"]]
        self.assertEqual(trackers, ["device_tracker.maria", "device_tracker.arvio_" + NICK])
        self.assertEqual(profile["t_home_s"], 120)

    def test_restart_restores_trackers(self):
        self.report(NICK, "not_home", "Νικόλας")
        self.calls.clear()
        with mock.patch.object(agent, "ha", side_effect=self.fake_ha), \
             mock.patch.object(agent.time, "time", return_value=self.clock):
            agent.occupancy_restore()
        self.assertEqual(self.posted("/states/device_tracker.arvio_" + NICK)[-1]["state"], "not_home")

    def test_rejects_bad_reports(self):
        for bad in (
            {"member_id": "nick", "state": "home"},
            {"member_id": NICK, "state": "away"},
            {"member_id": NICK, "state": "home", "latitude": 37.9},
            {"member_id": NICK, "state": "home", "mac": "aa"},
        ):
            with self.assertRaises(ValueError):
                agent.occupancy.parse_presence_report(bad)

    def test_home_location(self):
        occ = agent.occupancy
        cfg = {"latitude": 37.98381, "longitude": 23.727539}
        self.assertEqual(occ.home_region(cfg, {"attributes": {"radius": 100}}), {"latitude": 37.98381, "longitude": 23.727539, "radius_m": 150})
        self.assertEqual(occ.home_region(cfg, {"attributes": {"radius": 9000}})["radius_m"], 2000)
        self.assertEqual(occ.home_region(cfg, None)["radius_m"], 150)
        for bad in ({}, {"latitude": 0, "longitude": 0}, {"latitude": 91, "longitude": 1}, {"latitude": True, "longitude": 1}):
            with self.assertRaises(ValueError):
                occ.home_region(bad, None)

    def test_relay_only(self):
        for a in ("arvio.presence_report", "arvio.home_location"):
            self.assertIn(a, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(a, agent.LAN_ALLOWED_ACTIONS)
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": a}, via="lan")
            self.assertEqual(cm.exception.code, "lan_forbidden")


if __name__ == "__main__":
    unittest.main()
