"""The LAN path has no login: a door must not open for whoever is on the Wi-Fi."""
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent, signed

SECRET = "panel-secret"
RELAY = "switch.front_door_relay"

STATES = {
    RELAY: {"friendly_name": "Ρελέ 1"},
    "switch.plain": {"friendly_name": "Πρίζα κουζίνας"},
    "switch.table_lamp": {"friendly_name": "Πορτατίφ σαλονιού"},
    "switch.outdoor_light": {"friendly_name": "Outdoor light"},
    "switch.clock": {"friendly_name": "Clock"},
    "switch.named_door": {"friendly_name": "Πόρτα εισόδου"},
    "switch.named_front": {"friendly_name": "Εξώπορτα"},
    "switch.relay_1": {"friendly_name": "Ρελέ 2"},
    "switch.garage_relay": {"friendly_name": "Relay"},
    "cover.garage": {"device_class": "garage", "friendly_name": "Χ"},
    "cover.ha_garage": {"device_class": "garage_door", "friendly_name": "Χ"},
    "cover.gate": {"device_class": "gate", "friendly_name": "Χ"},
    "cover.motor_door": {"device_class": "door", "friendly_name": "Χ"},
    "cover.blind": {"device_class": "blind", "friendly_name": "Ρολό σαλονιού"},
    "cover.balcony": {"device_class": "shutter", "friendly_name": "Ρολό πόρτας μπαλκονιού"},
    "cover.no_class": {"friendly_name": "Ρολό κουζίνας"},
    "cover.named_garage": {"friendly_name": "Γκαραζόπορτα"},
}


class LanDoorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        agent.save_screens(
            {
                "screens": {
                    "scr_door": {
                        "screen_id": "scr_door",
                        "screen_secret_hash": agent.hash_screen_secret(SECRET),
                        "doorbell": {"camera_entity_id": "camera.front", "unlock_entity_id": RELAY},
                        "pages": [
                            {
                                "id": "p1",
                                "tiles": [{"kind": "entity", "ref": "switch.plain"}, {"kind": "entity", "ref": RELAY}],
                            }
                        ],
                    },
                    "scr_old": {"screen_id": "scr_old"},  # paired before 0.1.50: no secret at all
                }
            }
        )
        self.addCleanup(agent.save_screens, {"screens": {}})
        self.sent = []

        def fake_call(domain, service, data, *a, **k):
            self.sent.append(f"{domain}.{service}:{data.get('entity_id')}")
            return {"ok": True}

        def fake_ha(path, *a, **k):
            eid = path.rsplit("/", 1)[-1]
            attrs = STATES.get(eid)
            return None if attrs is None else {"entity_id": eid, "state": "off", "attributes": attrs}

        for target, new in (("call_service", fake_call), ("ha", fake_ha), ("hub_id", "hub_lan")):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)

    def post(self, path, body, secret=None):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers.update(signed("POST", path, data, secret=secret))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def both_routes(self, action, entity_id, secret=None):
        """(status on /api/service, status on /v1/hubs/…/commands)"""
        domain, service = action.split(".", 1)
        return (
            self.post(f"/api/service/{domain}/{service}", {"entity_id": entity_id}, secret),
            self.post("/v1/hubs/hub_lan/commands", {"action": action, "entity_id": entity_id}, secret),
        )

    def assert_refused(self, action, entity_id, secret=None):
        self.sent.clear()
        self.assertEqual(self.both_routes(action, entity_id, secret), (403, 403), f"{action} {entity_id}")
        self.assertEqual(self.sent, [], f"{action} {entity_id} reached the device")

    def assert_allowed(self, action, entity_id, secret=None):
        self.sent.clear()
        self.assertEqual(self.both_routes(action, entity_id, secret), (200, 200), f"{action} {entity_id}")
        self.assertEqual(len(self.sent), 2)

    def test_a_doorbells_relay_never_opens_from_the_lan(self):
        self.assert_refused("switch.turn_on", RELAY)
        self.assert_refused("switch.turn_off", RELAY)
        self.assert_refused("switch.turn_on", RELAY, secret="wrong")

    def test_a_paired_panel_has_only_the_doorbell_for_it(self):
        # The doorbell asks for a recent ring. A plain switch.turn_on would go round that.
        self.assert_refused("switch.turn_on", RELAY, secret=SECRET)

    def test_every_switch_at_once_is_refused(self):
        for action in ("switch.turn_on", "cover.open_cover", "light.turn_on"):
            self.assert_refused(action, "all")
            self.assert_refused(action, "all", secret=SECRET)

    def test_a_malformed_id_or_a_list_is_refused(self):
        for bad in (f"switch.plain,{RELAY}", "switch.plain switch.x", "SWITCH.PLAIN", "switch.", ".x", "switch"):
            self.assert_refused("switch.turn_on", bad)

    def test_a_command_with_no_entity_reaches_nothing(self):
        self.sent.clear()
        body = {"action": "switch.turn_on", "entity_id": ""}
        self.assertEqual(self.post("/v1/hubs/hub_lan/commands", body), 400)
        self.assertEqual(self.sent, [])

    def test_garage_gate_and_motor_door_need_a_paired_panel(self):
        for eid in ("cover.garage", "cover.ha_garage", "cover.gate", "cover.motor_door"):
            for action in ("cover.open_cover", "cover.close_cover", "cover.stop_cover"):
                self.assert_refused(action, eid)
                self.assert_refused(action, eid, secret="wrong")
                self.assert_refused(action, eid, secret="")
            self.assert_allowed("cover.open_cover", eid, secret=SECRET)

    def test_a_relay_named_like_a_door_needs_a_paired_panel(self):
        for eid in ("switch.named_door", "switch.named_front", "switch.garage_relay", "cover.named_garage"):
            action = "cover.open_cover" if eid.startswith("cover.") else "switch.turn_on"
            self.assert_refused(action, eid)
            self.assert_allowed(action, eid, secret=SECRET)

    def test_ordinary_things_still_work_with_no_secret(self):
        for eid in ("switch.plain", "switch.table_lamp", "switch.outdoor_light", "switch.clock", "switch.relay_1"):
            self.assert_allowed("switch.turn_on", eid)
        for eid in ("cover.blind", "cover.balcony", "cover.no_class"):
            self.assert_allowed("cover.open_cover", eid)

    def test_a_lamp_needs_no_lookup(self):
        calls = []
        with mock.patch.object(agent, "ha", side_effect=lambda *a, **k: calls.append(a)):
            self.assert_allowed("light.turn_on", "light.saloni")
        self.assertEqual(calls, [])

    def test_when_it_cannot_tell_it_refuses(self):
        self.assert_refused("switch.turn_on", "switch.unknown")
        self.assert_refused("cover.open_cover", "cover.unknown")
        with mock.patch.object(agent, "ha", return_value=None):
            self.assert_refused("switch.turn_on", "switch.plain")

    def test_a_panel_paired_before_secrets_cannot_vouch(self):
        for secret in ("", "anything"):
            self.assert_refused("cover.open_cover", "cover.garage", secret=secret)

    def test_the_doorbell_itself_still_opens_it(self):
        with mock.patch.object(agent, "doorbell_rang_recently", return_value=True):
            status = self.post("/api/screens/doorbell/unlock", {"screen_id": "scr_door"}, secret=SECRET)
        self.assertEqual(status, 200)
        self.assertEqual(self.sent, [f"switch.turn_on:{RELAY}"])

    def test_the_cloud_path_is_not_changed_by_this(self):
        agent.check_command_safety({"action": "switch.turn_on", "entity_id": RELAY}, via="relay")
        agent.check_command_safety({"action": "cover.open_cover", "entity_id": "cover.garage"}, via="relay")

    def test_the_panel_does_not_show_a_relay_it_cannot_use(self):
        rows = [
            {"entity_id": RELAY, "state": "off", "name": "Ρελέ 1", "domain": "switch"},
            {"entity_id": "switch.plain", "state": "off", "name": "Πρίζα", "domain": "switch"},
        ]
        with mock.patch.object(agent, "entities", return_value=rows), \
             mock.patch.object(agent, "registries_for_commands", return_value={}):
            out = agent.panel_snapshot("scr_door", SECRET)
        ids = [e["entity_id"] for e in out["entities"]]
        self.assertIn("switch.plain", ids)
        self.assertNotIn(RELAY, ids)
        # A tile the installer placed on it goes too: without its entity it shows the bare id.
        tiles = [t["ref"] for t in out["screens"][0]["pages"][0]["tiles"]]
        self.assertEqual(tiles, ["switch.plain"])

    def test_the_panel_sends_its_secret_with_commands(self):
        page = (pathlib.Path(__file__).resolve().parents[1] / "panel.html").read_text(encoding="utf-8")
        start = page.index("async function cmd(action, entityId, payload)")
        self.assertIn('screenFetch("/v1/hubs/"', page[start : start + 400])


class DoorNameTest(unittest.TestCase):
    def test_words(self):
        door = ["Πόρτα", "ΠΟΡΤΑ ΕΙΣΟΔΟΥ", "Εξώπορτα", "Γκαραζόπορτα", "Γκαράζ", "Πύλη", "Κλειδαριά", "Θυροτηλέφωνο",
                "switch.front_door", "Door", "garage", "Main gate", "Door strike", "Smart lock"]
        not_door = ["Πορτατίφ", "Πορτοκαλί φως", "Doorbell chime", "Outdoor", "Clock", "Navigate", "Φως σαλονιού",
                    "Ρελέ 1"]
        for name in door:
            self.assertTrue(agent.named_like_a_door(name), name)
        for name in not_door:
            self.assertFalse(agent.named_like_a_door(name), name)


if __name__ == "__main__":
    unittest.main()
