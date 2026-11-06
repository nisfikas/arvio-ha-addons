"""The LAN path needs a caller: a paired panel or a signed-in app. Whoever else is on the Wi-Fi,
or scans the QR on the hub's screen, gets nothing (docs/ADR/017 in the arvio repo)."""
import base64
import hashlib
import hmac
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent

# The cloud's own output: services/api/tests/home-routes.test.ts makes this very token.
VECTOR = "eyJodWIiOiJodWJfdmVjdG9yIiwic3ViIjoidXNlcl8xIiwiZXhwIjoxNzk2NTE1MjAwfQ.n_sejrkW7DY4MBlbhp7lKJXpSxACZ3j5BWMuGApKDKw"
VECTOR_EXP = 1796515200
RELAY = "relay-token-for-the-test"
HUB = "hub_vector"
SECRET = "panel-secret"


def make_token(relay=RELAY, hub=HUB, exp=VECTOR_EXP):
    enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
    key = hmac.new(hashlib.sha256(relay.encode()).hexdigest().encode(), b"arvio-lan-token-v1", hashlib.sha256).digest()
    body = enc(json.dumps({"hub": hub, "sub": "user_1", "exp": exp}).encode())
    return (body + b"." + enc(hmac.new(key, body, hashlib.sha256).digest())).decode()


class Remote(unittest.TestCase):
    """A hub with a cloud, as every sold hub is."""

    def setUp(self):
        for target, new in (("mode", "remote"), ("hub_id", HUB), ("RELAY_TOKEN", RELAY)):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)


class TokenTest(Remote):
    def ok(self, token, now=VECTOR_EXP - 1):
        return agent.lan_token_ok(token, now=now)

    def test_the_clouds_token_is_good_until_it_expires(self):
        self.assertTrue(self.ok(VECTOR))
        self.assertFalse(self.ok(VECTOR, now=VECTOR_EXP))

    def test_another_hubs_key_opens_nothing_here(self):
        self.assertFalse(self.ok(make_token(hub="hub_other")))
        self.assertFalse(self.ok(make_token(relay="another-hubs-relay-token")))

    def test_a_token_that_was_changed_is_refused(self):
        body, sig = VECTOR.split(".")
        for bad in (
            "",
            None,
            "nodot",
            body,
            body + ".",
            "." + sig,
            body + "." + sig[:-1],
            body + "." + sig + "x",
            body[:-1] + "A." + sig,
            "α." + sig,
            body + ".σ",
        ):
            self.assertFalse(self.ok(bad), bad)

    def test_a_signed_body_that_is_not_what_we_expect_is_refused(self):
        enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
        key = hmac.new(hashlib.sha256(RELAY.encode()).hexdigest().encode(), b"arvio-lan-token-v1", hashlib.sha256).digest()
        for raw in (b"not json", b"[]", b'{"hub":"hub_vector"}', b'{"hub":"hub_vector","exp":"never"}', b'{"hub":"hub_vector","exp":true}'):
            body = enc(raw)
            self.assertFalse(self.ok((body + b"." + enc(hmac.new(key, body, hashlib.sha256).digest())).decode()), raw)

    def test_a_hub_with_no_relay_token_accepts_no_key_at_all(self):
        # sha256("") is public: without this, anyone could sign for a hub that has no token yet.
        with mock.patch.object(agent, "RELAY_TOKEN", ""):
            self.assertFalse(self.ok(make_token(relay="")))

    def test_a_hub_with_no_id_accepts_no_key_at_all(self):
        with mock.patch.object(agent, "hub_id", None):
            self.assertFalse(self.ok(make_token(hub="None")))


class RoutesTest(Remote):
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
        super().setUp()
        agent.save_screens({"screens": {"scr_a": {"screen_id": "scr_a", "screen_secret_hash": agent.hash_screen_secret(SECRET)}, "scr_old": {"screen_id": "scr_old"}}})
        self.addCleanup(agent.save_screens, {"screens": {}})
        self.sent = []

        def fake_call(domain, service, data, *a, **k):
            self.sent.append(f"{domain}.{service}:{data.get('entity_id')}")
            return {"ok": True}

        def fake_ha(path, *a, **k):
            if path == "/states":
                return [{"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "Γραφείο Νικόλας"}}]
            names = {"switch.plain": "Πρίζα", "cover.garage": "Γκαραζόπορτα"}
            eid = path.rsplit("/", 1)[-1]
            return {"entity_id": eid, "state": "off", "attributes": {"friendly_name": names[eid]}} if eid in names else None

        for target, new in (("call_service", fake_call), ("ha", fake_ha)):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)

    def request(self, method, path, body=None, secret=None, token=None):
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers[agent.SCREEN_SECRET_HEADER] = secret
        if token is not None:
            headers[agent.LAN_TOKEN_HEADER] = token
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def both_routes(self, action, entity_id, **who):
        domain, service = action.split(".", 1)
        return (
            self.request("POST", f"/api/service/{domain}/{service}", {"entity_id": entity_id}, **who)[0],
            self.request("POST", f"/v1/hubs/{HUB}/commands", {"action": action, "entity_id": entity_id}, **who)[0],
        )

    def test_whoever_is_on_the_wifi_moves_nothing(self):
        for action, eid in (("light.turn_on", "light.a"), ("switch.turn_on", "switch.plain"), ("cover.open_cover", "cover.blind"),
                            ("climate.turn_off", "climate.a"), ("scene.turn_on", "scene.x"), ("script.turn_on", "script.arvio_nyxta"),
                            ("media_player.media_pause", "media_player.a"), ("vacuum.start", "vacuum.a")):
            self.assertEqual(self.both_routes(action, eid), (403, 403), action)
            self.assertEqual(self.both_routes(action, eid, secret="wrong", token="wrong"), (403, 403), action)
        self.assertEqual(self.sent, [])

    def test_nor_reads_the_house_through_a_command(self):
        for action in ("arvio.list_entities", "arvio.pairing_status"):
            status, _ = self.request("POST", f"/v1/hubs/{HUB}/commands", {"action": action, "entity_id": ""})
            self.assertEqual(status, 403, action)

    def test_a_signed_in_app_does(self):
        self.assertEqual(self.both_routes("light.turn_on", "light.a", token=VECTOR), (200, 200))
        self.assertEqual(self.sent, ["light.turn_on:light.a"] * 2)

    def test_a_paired_panel_does(self):
        self.assertEqual(self.both_routes("light.turn_on", "light.a", secret=SECRET), (200, 200))

    def test_a_panel_paired_before_secrets_does_not(self):
        # It has nothing to prove itself with, like everyone else on the Wi-Fi: it needs pairing again.
        self.assertEqual(self.both_routes("light.turn_on", "light.a", secret=""), (403, 403))

    def test_the_key_does_not_change_the_rule_for_doors(self):
        self.assertEqual(self.both_routes("cover.open_cover", "cover.garage", token=VECTOR), (403, 403))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.both_routes("cover.open_cover", "cover.garage", secret=SECRET), (200, 200))

    def test_the_cloud_path_needs_no_key(self):
        agent.check_command_safety({"action": "light.turn_on", "entity_id": "light.a"}, via="relay")

    def test_a_box_with_no_cloud_has_nobody_to_ask(self):
        with mock.patch.object(agent, "mode", "embedded"):
            self.assertEqual(self.both_routes("light.turn_on", "light.a"), (200, 200))
            self.assertEqual(self.both_routes("cover.open_cover", "cover.garage"), (403, 403))

    def test_the_status_names_no_device_without_a_caller(self):
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertNotIn("entities", body)
        self.assertNotIn("Νικόλας", raw.decode("unicode_escape", "ignore"))
        self.assertEqual(body["hub_id"], HUB)
        for who in ({"token": VECTOR}, {"secret": SECRET}):
            _, raw = self.request("GET", "/api/status", **who)
            self.assertEqual([e["entity_id"] for e in json.loads(raw)["entities"]], ["light.a"], who)

    def page(self, state, hub=HUB):
        agent.code = "405421"
        with mock.patch.object(agent, "APP", pathlib.Path(__file__).resolve().parents[1]), \
             mock.patch.object(agent, "hub_state", state), mock.patch.object(agent, "hub_id", hub):
            status, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        return raw.decode()

    def test_the_page_shows_the_code_only_while_the_box_has_no_owner(self):
        for state in ("inventory", "prepared", "assigned"):
            self.assertIn(">405421<", self.page(state), state)
        self.assertIn(">405421<", self.page("prepared", hub=None))
        for state in ("claimed", "active", "suspended"):
            self.assertNotIn("405421", self.page(state), state)

    def test_the_code_is_not_predictable(self):
        with mock.patch.object(agent.secrets, "randbelow", return_value=42) as rnd:
            agent.rotate()
        rnd.assert_called_once_with(1000000)
        self.assertEqual(agent.code, "000042")


if __name__ == "__main__":
    unittest.main()
