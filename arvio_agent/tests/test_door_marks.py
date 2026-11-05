"""An installer marks what the hub cannot recognise as a door, and a scene cannot carry a door past the rule."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent

SECRET = "panel-secret"
RELAY = "switch.front_door_relay"
REAL_EXECUTE = agent.execute_action

ATTRS = {
    RELAY: {"friendly_name": "Ρελέ 1"},
    "switch.relay_2": {"friendly_name": "Ρελέ 2"},
    "switch.plain": {"friendly_name": "Πρίζα κουζίνας"},
    "light.saloni": {"friendly_name": "Σαλόνι"},
    "light.front_gate": {"friendly_name": "Φως 3"},
    "cover.blind": {"device_class": "blind", "friendly_name": "Ρολό σαλονιού"},
    "cover.garage": {"device_class": "garage", "friendly_name": "Χ"},
    "scene.evening": {"entity_id": ["light.saloni", "switch.plain", "cover.blind"]},
    "scene.lights_only": {"entity_id": ["light.saloni"]},
    "scene.with_mark": {"entity_id": ["light.saloni", "switch.relay_2"]},
    "scene.with_relay": {"entity_id": ["light.saloni", RELAY]},
    "scene.with_garage": {"entity_id": ["cover.garage"]},
    "scene.gone_member": {"entity_id": ["switch.no_longer_here"]},
    "scene.no_list": {},
}
CONFIGS = {
    "/config/automation/config/calm": {"action": [{"service": "light.turn_on", "target": {"entity_id": "light.saloni"}}]},
    "/config/automation/config/opens": {
        "action": [{"choose": [{"sequence": [{"service": "switch.turn_on", "target": {"entity_id": "switch.relay_2"}}]}]}]
    },
    "/config/automation/config/graph": {
        "graph": {"nodes": [{"id": "n1", "config": {"entity_id": RELAY, "service": "turn_on"}}]}
    },
    "/config/automation/config/via_scene": {"action": [{"service": "scene.turn_on", "entity_id": "scene.with_mark"}]},
    "/config/script/config/arvio_calm": {"sequence": [{"service": "light.turn_off", "entity_id": "light.saloni"}]},
    "/config/script/config/arvio_opens": {"sequence": [{"service": "cover.open_cover", "entity_id": "cover.garage"}]},
}


def state(eid):
    attrs = ATTRS.get(eid)
    return None if attrs is None else {"entity_id": eid, "state": "off", "attributes": attrs}


def fake_ha(path, *a, **k):
    if path == "/states":
        return [state(e) for e in ATTRS]
    if path.startswith("/states/"):
        return state(path.rsplit("/", 1)[-1])
    return CONFIGS.get(path)


class Base(unittest.TestCase):
    def setUp(self):
        agent.save_screens(
            {
                "screens": {
                    "scr_door": {
                        "screen_id": "scr_door",
                        "screen_secret_hash": agent.hash_screen_secret(SECRET),
                        "doorbell": {"camera_entity_id": "camera.front", "unlock_entity_id": RELAY},
                    }
                }
            }
        )
        self.addCleanup(agent.save_screens, {"screens": {}})
        agent.DOOR_MARKS.unlink(missing_ok=True)
        self.addCleanup(agent.DOOR_MARKS.unlink, missing_ok=True)
        self.sent = []

        def fake_execute(action, entity_id, payload=None, target=None):
            self.sent.append((action, entity_id))
            return {"ok": True}

        def fake_call(domain, service, data, *a, **k):
            self.sent.append((f"{domain}.{service}", data.get("entity_id")))
            return {"ok": True}

        patches = [
            ("execute_action", fake_execute),
            ("call_service", fake_call),
            ("ha", fake_ha),
            ("hub_id", "hub_lan"),
            ("schedule_registry_refresh", lambda *a, **k: None),
        ]
        for target, new in patches:
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)


class MarkingTest(Base):
    def test_a_mark_is_kept_and_can_be_taken_back(self):
        self.assertEqual(agent.door_mark({"entity_id": "switch.relay_2", "door": True})["door"], True)
        self.assertIn("switch.relay_2", json.loads(agent.DOOR_MARKS.read_text())["entity_ids"])
        self.assertEqual(agent.door_marks(), {"switch.relay_2"})
        self.assertEqual(agent.door_mark({"entity_id": "switch.relay_2", "door": False})["door"], False)
        self.assertEqual(agent.door_marks(), set())

    def test_only_a_thing_that_can_be_a_door_and_a_clear_answer(self):
        for bad in (
            {"entity_id": "sensor.temperature", "door": True},
            {"entity_id": "lock.front", "door": True},
            {"entity_id": "all", "door": True},
            {"entity_id": "switch.a,switch.b", "door": True},
            {"entity_id": "switch.a", "door": "yes"},
            {"entity_id": "switch.a"},
            {},
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                agent.door_mark(bad)
        self.assertEqual(agent.door_marks(), set())

    def test_it_reaches_the_hub_through_a_command_from_the_cloud(self):
        with mock.patch.object(agent, "execute_action", REAL_EXECUTE):
            ack = agent.handle_command(
                {"command_id": "c1", "action": "arvio.door_mark", "payload": {"entity_id": "cover.blind", "door": True}}
            )
        self.assertTrue(ack["ok"])
        self.assertEqual(agent.door_marks(), {"cover.blind"})

    def test_the_model_says_what_is_marked(self):
        def entities():
            states = [state("switch.relay_2"), state("switch.plain")]
            model = agent.build_hub_model(states, None, None, None, None, None, snapshot_version=1)
            return {e["entity_id"]: e for e in model["entities"]}

        self.assertNotIn("door_marked", entities()["switch.relay_2"])
        agent.door_mark({"entity_id": "switch.relay_2", "door": True})
        self.assertIs(entities()["switch.relay_2"]["door_marked"], True)
        self.assertNotIn("door_marked", entities()["switch.plain"])
        agent.door_mark({"entity_id": "switch.relay_2", "door": False})
        self.assertNotIn("door_marked", entities()["switch.relay_2"])


class LanServer(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, body, secret=None):
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers[agent.SCREEN_SECRET_HEADER] = secret
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def command(self, action, entity_id="", payload=None, secret=None):
        self.sent.clear()
        body = {"action": action, "entity_id": entity_id}
        if payload:
            body["payload"] = payload
        return self.post("/v1/hubs/hub_lan/commands", body, secret)


class MarkedOnTheLanTest(LanServer):
    def test_the_lan_cannot_set_or_clear_a_mark(self):
        body = {"entity_id": "switch.plain", "door": False}
        self.assertEqual(self.command("arvio.door_mark", payload=body), 403)
        self.assertEqual(self.post("/api/service/arvio/door_mark", {"entity_id": "switch.plain"}), 403)
        self.assertEqual(agent.door_marks(), set())

    def test_a_neutral_relay_is_protected_once_marked(self):
        self.assertEqual(self.command("switch.turn_on", "switch.relay_2"), 200)
        agent.door_mark({"entity_id": "switch.relay_2", "door": True})
        self.assertEqual(self.command("switch.turn_on", "switch.relay_2"), 403)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.command("switch.turn_on", "switch.relay_2", secret=SECRET), 200)
        agent.door_mark({"entity_id": "switch.relay_2", "door": False})
        self.assertEqual(self.command("switch.turn_on", "switch.relay_2"), 200)

    def test_on_the_older_route_too(self):
        agent.door_mark({"entity_id": "switch.relay_2", "door": True})
        self.assertEqual(self.post("/api/service/switch/turn_on", {"entity_id": "switch.relay_2"}), 403)
        self.assertEqual(self.post("/api/service/switch/turn_on", {"entity_id": "switch.relay_2"}, SECRET), 200)

    def test_a_door_wired_as_a_light_can_be_marked(self):
        self.assertEqual(self.command("light.turn_on", "light.front_gate"), 200)
        agent.door_mark({"entity_id": "light.front_gate", "door": True})
        self.assertEqual(self.command("light.turn_on", "light.front_gate"), 403)
        self.assertEqual(self.command("light.turn_on", "light.front_gate", secret=SECRET), 200)
        self.assertEqual(self.command("light.turn_on", "light.saloni"), 200)

    def test_the_mark_beats_a_class_that_says_blind(self):
        agent.door_mark({"entity_id": "cover.blind", "door": True})
        self.assertEqual(self.command("cover.open_cover", "cover.blind"), 403)

    def test_a_doorbell_relay_stays_stricter_than_a_mark(self):
        agent.door_mark({"entity_id": RELAY, "door": True})
        self.assertEqual(self.command("switch.turn_on", RELAY, secret=SECRET), 403)

    def test_an_unreadable_list_protects_rather_than_opens(self):
        agent.DOOR_MARKS.write_text("{not json")
        self.assertIsNone(agent.door_marks())
        self.assertEqual(self.command("switch.turn_on", "switch.plain"), 403)
        self.assertEqual(self.command("switch.turn_on", "switch.plain", secret=SECRET), 200)
        self.assertEqual(self.command("light.turn_on", "light.saloni"), 200)


class ScenesAndScenariosTest(LanServer):
    def setUp(self):
        super().setUp()
        agent.door_mark({"entity_id": "switch.relay_2", "door": True})

    def scene(self, eid, secret=None):
        return self.command("scene.turn_on", eid, secret=secret)

    def scenario(self, sid, secret=None):
        return self.command("arvio.trigger_scenario", payload={"scenario_id": sid}, secret=secret)

    def test_a_scene_with_no_door_still_runs_for_anyone(self):
        self.assertEqual(self.scene("scene.lights_only"), 200)
        self.assertEqual(self.scene("scene.evening"), 200)

    def test_a_scene_with_a_door_needs_a_paired_panel(self):
        for eid in ("scene.with_mark", "scene.with_garage"):
            self.assertEqual(self.scene(eid), 403, eid)
            self.assertEqual(self.sent, [])
            self.assertEqual(self.scene(eid, secret=SECRET), 200, eid)

    def test_a_scene_with_a_doorbell_relay_never_runs_from_the_lan(self):
        self.assertEqual(self.scene("scene.with_relay"), 403)
        self.assertEqual(self.scene("scene.with_relay", secret=SECRET), 403)

    def test_a_member_that_no_longer_exists_moves_nothing(self):
        self.assertEqual(self.scene("scene.gone_member"), 200)

    def test_a_scene_it_cannot_read_needs_a_paired_panel(self):
        self.assertEqual(self.scene("scene.no_list"), 403)
        self.assertEqual(self.scene("scene.unknown"), 403)
        self.assertEqual(self.scene("scene.no_list", secret=SECRET), 200)

    def test_scenarios_are_read_however_deep_the_door_is(self):
        self.assertEqual(self.scenario("calm"), 200)
        self.assertEqual(self.scenario("opens"), 403)
        self.assertEqual(self.scenario("opens", secret=SECRET), 200)
        self.assertEqual(self.scenario("graph"), 403)
        self.assertEqual(self.scenario("graph", secret=SECRET), 403)

    def test_a_scenario_that_fires_a_scene_with_a_door(self):
        self.assertEqual(self.scenario("via_scene"), 403)
        self.assertEqual(self.scenario("via_scene", secret=SECRET), 200)

    def test_a_scenario_it_cannot_read_needs_a_paired_panel(self):
        self.assertEqual(self.scenario("unknown"), 403)
        self.assertEqual(self.scenario("unknown", secret=SECRET), 200)

    def test_scripts_too(self):
        self.assertEqual(self.command("script.turn_on", "script.arvio_calm"), 200)
        self.assertEqual(self.command("script.turn_on", "script.arvio_opens"), 403)
        self.assertEqual(self.command("script.turn_on", "script.arvio_opens", secret=SECRET), 200)
        self.assertEqual(self.command("script.turn_on", "script.arvio_unreadable"), 403)

    def test_no_state_lookup_when_nothing_could_be_a_door(self):
        calls = []

        def counting(path, *a, **k):
            calls.append(path)
            return fake_ha(path, *a, **k)

        with mock.patch.object(agent, "ha", counting):
            self.assertEqual(self.scene("scene.lights_only"), 200)
        self.assertNotIn("/states", calls)

    def test_the_cloud_path_runs_them_as_before(self):
        agent.check_command_safety({"action": "scene.turn_on", "entity_id": "scene.with_relay"}, via="relay")


if __name__ == "__main__":
    unittest.main()
