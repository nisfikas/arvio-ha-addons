"""The LAN path needs a caller who signs: a paired panel or a signed-in app. Whoever else is on the
Wi-Fi, or scans the QR on the hub's screen, gets nothing, and the key never crosses the Wi-Fi
(docs/ADR/017 in the arvio repo)."""
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

from helpers import agent, signed

# The cloud's own output: services/api/tests/home-routes.test.ts makes this very token.
VECTOR = "eyJodWIiOiJodWJfdmVjdG9yIiwic3ViIjoidXNlcl8xIiwiZXhwIjoxNzk2NTE1MjAwfQ.n_sejrkW7DY4MBlbhp7lKJXpSxACZ3j5BWMuGApKDKw"
VECTOR_BODY, VECTOR_KEY = VECTOR.split(".")
VECTOR_EXP = 1796515200
RELAY = "relay-token-for-the-test"
HUB = "hub_vector"
SECRET = "panel-secret"
EARLIER = VECTOR_EXP - 1000


def make_token(relay=RELAY, hub=HUB, exp=VECTOR_EXP, raw=None):
    enc = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    key = hmac.new(hashlib.sha256(relay.encode()).hexdigest().encode(), b"arvio-lan-token-v1", hashlib.sha256).digest()
    body = enc(raw if raw is not None else json.dumps({"hub": hub, "sub": "user_1", "exp": exp}).encode())
    return (body + b"." + enc(hmac.new(key, body, hashlib.sha256).digest())).decode()


class Remote(unittest.TestCase):
    """A hub with a cloud, as every sold hub is."""

    def setUp(self):
        for target, new in (("mode", "remote"), ("hub_id", HUB), ("RELAY_TOKEN", RELAY)):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(agent.time, "time", return_value=EARLIER)
        p.start()
        self.addCleanup(p.stop)


class SignatureTest(Remote):
    def test_the_same_bytes_in_python_swift_and_the_panel(self):
        # The same two values are in Partner's panels.test.ts and ArvioKitTests.swift.
        body = '{"action":"light.turn_on","entity_id":"light.σαλόνι"}'.encode()
        panel_key = hashlib.sha256(SECRET.encode()).hexdigest().encode()
        self.assertEqual(
            agent.lan_signature(panel_key, "0123abcd", "POST", "/v1/hubs/hub_a/commands", body),
            "46330ebaeb6acf2380bc6b82967de25515c5892c6fdac12e577647c8b51bba04",
        )
        self.assertEqual(
            agent.lan_signature(VECTOR_KEY.encode(), "0123abcd", "GET", "/api/status", b""),
            "81862007d4ce19115e5f94324f2fddb95c41e97f39788ad383250ae531778604",
        )

    def test_the_hub_works_out_the_second_half_of_the_clouds_token(self):
        self.assertEqual(agent.lan_app_key(VECTOR_BODY), VECTOR_KEY)

    def app_signed(self, token, target="/api/status", body=b"", method="GET", app_id=None):
        given_id, _, key = token.partition(".")
        sig = agent.lan_signature(key.encode(), "n", method, target, body)
        return agent.lan_signer("n", "GET", "/api/status", b"", sig, app_id or given_id)

    def test_an_app_signs_until_its_token_expires(self):
        self.assertIs(self.app_signed(VECTOR), True)
        with mock.patch.object(agent.time, "time", return_value=VECTOR_EXP):
            self.assertIsNone(self.app_signed(VECTOR))

    def test_a_signature_is_for_this_one_request(self):
        self.assertIsNone(self.app_signed(VECTOR, target="/v1/hubs/hub_vector/commands"))
        self.assertIsNone(self.app_signed(VECTOR, body=b'{"action":"light.turn_on"}'))
        self.assertIsNone(self.app_signed(VECTOR, method="POST"))

    def test_another_hubs_key_opens_nothing_here(self):
        self.assertIsNone(self.app_signed(make_token(hub="hub_other")))
        self.assertIsNone(self.app_signed(make_token(relay="another-hubs-relay-token")))

    def test_a_changed_or_empty_id_is_refused(self):
        for bad in (VECTOR_BODY[:-1] + "A", "α", "x"):
            self.assertIsNone(self.app_signed(VECTOR, app_id=bad), bad)

    def test_a_signed_body_that_is_not_what_we_expect_is_refused(self):
        for raw in (b"not json", b"[]", b'{"hub":"hub_vector"}', b'{"hub":"hub_vector","exp":"never"}', b'{"hub":"hub_vector","exp":true}'):
            self.assertIsNone(self.app_signed(make_token(raw=raw)), raw)

    def test_a_box_whose_clock_is_in_1970_takes_no_key(self):
        # No RTC, no internet: it would take an expired or revoked key as fresh. It has the cloud until it learns the time.
        self.assertTrue(agent.lan_app_claims_ok(VECTOR_BODY, now=EARLIER))
        self.assertFalse(agent.lan_app_claims_ok(VECTOR_BODY, now=VECTOR_EXP))
        for lost in (0.0, 86400.0 * 365, agent.LAN_CLOCK_FLOOR - 1):
            self.assertFalse(agent.lan_app_claims_ok(VECTOR_BODY, now=lost), lost)
        with mock.patch.object(agent.time, "time", return_value=0.0):
            self.assertIsNone(self.app_signed(VECTOR))

    def test_a_hub_with_no_relay_token_accepts_no_key_at_all(self):
        # sha256("") is public: without this, anyone could sign for a hub that has no token yet.
        with mock.patch.object(agent, "RELAY_TOKEN", ""):
            self.assertIsNone(self.app_signed(make_token(relay="")))

    def test_a_hub_with_no_id_accepts_no_key_at_all(self):
        with mock.patch.object(agent, "hub_id", None):
            self.assertIsNone(self.app_signed(make_token(hub="None")))

    def test_a_signature_with_odd_characters_does_not_crash(self):
        self.assertIsNone(agent.lan_signer("n", "GET", "/", b"", "σ", None))
        self.assertIsNone(agent.lan_signer("n", "GET", "/", b"", "σ", VECTOR_BODY))

    def test_a_nonce_works_once_and_not_for_long(self):
        nonce = agent.lan_nonce_new()
        self.assertTrue(agent.lan_nonce_spend(nonce))
        self.assertFalse(agent.lan_nonce_spend(nonce))
        self.assertFalse(agent.lan_nonce_spend("never-issued"))
        with mock.patch.object(agent.time, "monotonic", return_value=0):
            old = agent.lan_nonce_new()
        with mock.patch.object(agent.time, "monotonic", return_value=agent.LAN_NONCE_TTL_S + 1):
            self.assertFalse(agent.lan_nonce_spend(old))

    def test_asking_for_nonces_cannot_fill_the_hub(self):
        for _ in range(agent.LAN_NONCE_MAX + 50):
            agent.lan_nonce_new()
        self.assertLessEqual(len(agent.LAN_NONCES), agent.LAN_NONCE_MAX)

    def test_the_code_is_not_predictable(self):
        with mock.patch.object(agent.secrets, "randbelow", return_value=42) as rnd:
            agent.rotate()
        rnd.assert_called_once_with(1000000)
        self.assertEqual(agent.code, "000042")


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
        agent.save_screens(
            {
                "screens": {
                    "scr_a": {"screen_id": "scr_a", "screen_secret_hash": agent.hash_screen_secret(SECRET)},
                    "scr_old": {"screen_id": "scr_old"},
                }
            }
        )
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

    def raw(self, method, path, data=None, headers=None):
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def request(self, method, path, body=None, secret=None, token=None):
        data = None if body is None else json.dumps(body).encode()
        headers = signed(method, path, data or b"", secret=secret, token=token) if secret is not None or token is not None else {}
        return self.raw(method, path, data, headers)

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
            self.assertEqual(self.both_routes(action, eid, secret="wrong"), (403, 403), action)
            self.assertEqual(self.both_routes(action, eid, token=make_token(relay="other")), (403, 403), action)
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

    def test_a_key_sent_in_the_open_counts_for_nothing(self):
        path = f"/v1/hubs/{HUB}/commands"
        data = json.dumps({"action": "light.turn_on", "entity_id": "light.a"}).encode()
        self.assertEqual(self.raw("POST", path, data, {"X-Arvio-Lan-Token": VECTOR})[0], 403)  # as 0.1.83 asked for
        self.assertEqual(self.sent, [])

    def test_a_request_read_off_the_wifi_cannot_be_sent_again(self):
        path = f"/v1/hubs/{HUB}/commands"
        data = json.dumps({"action": "light.turn_on", "entity_id": "light.a"}).encode()
        headers = signed("POST", path, data, token=VECTOR)
        self.assertEqual(self.raw("POST", path, data, headers)[0], 200)
        self.assertEqual(self.raw("POST", path, data, headers)[0], 409)
        self.assertEqual(len(self.sent), 1)

    def test_a_signature_for_another_command_moves_nothing(self):
        path = f"/v1/hubs/{HUB}/commands"
        asked = json.dumps({"action": "light.turn_on", "entity_id": "light.a"}).encode()
        changed = json.dumps({"action": "light.turn_off", "entity_id": "light.a"}).encode()
        self.assertEqual(self.raw("POST", path, changed, signed("POST", path, asked, token=VECTOR))[0], 403)
        self.assertEqual(self.sent, [])

    def test_an_expired_nonce_does_not_read_as_a_wrong_secret(self):
        # Someone flooding the hub with requests for nonces must not make a panel forget its pairing.
        headers = signed("GET", "/api/screens?screen_id=scr_a", secret=SECRET)
        agent.LAN_NONCES.clear()
        self.assertEqual(self.raw("GET", "/api/screens?screen_id=scr_a", None, headers)[0], 409)

    def test_a_panel_reads_its_own_screen_by_signing(self):
        self.assertEqual(self.request("GET", "/api/screens?screen_id=scr_a", secret=SECRET)[0], 200)
        self.assertEqual(self.request("GET", "/api/screens?screen_id=scr_a", secret="wrong")[0], 403)
        self.assertEqual(self.request("GET", "/api/screens?screen_id=scr_a")[0], 403)

    def test_the_nonce_is_never_cached_and_never_repeats(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/lan/nonce")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers.get("Cache-Control"), "no-store")
            first = json.loads(r.read())["nonce"]
        self.assertNotEqual(first, json.loads(self.raw("GET", "/v1/lan/nonce")[1])["nonce"])

    def test_the_key_does_not_change_the_rule_for_doors(self):
        self.assertEqual(self.both_routes("cover.open_cover", "cover.garage", token=VECTOR), (403, 403))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.both_routes("cover.open_cover", "cover.garage", secret=SECRET), (200, 200))

    def test_the_cloud_path_needs_no_key(self):
        ack = agent.handle_command({"command_id": "relay_light_a", "action": "light.turn_on", "entity_id": "light.a"})
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(self.sent, ["light.turn_on:light.a"])

    def test_a_box_with_no_cloud_has_nobody_to_ask(self):
        with mock.patch.object(agent, "mode", "embedded"):
            self.assertEqual(self.both_routes("light.turn_on", "light.a"), (200, 200))
            self.assertEqual(self.both_routes("cover.open_cover", "cover.garage"), (403, 403))

    def test_the_status_names_no_device_without_a_caller(self):
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertNotIn("entities", body)
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
        for state in ("claimed", "active", "maintenance", "transferred"):
            self.assertNotIn("405421", self.page(state), state)


class ClaimedHubWithNoInternet(unittest.TestCase):
    def test_it_does_not_read_as_ready_to_claim(self):
        agent.save_hub({"hub_id": "hub_a", "state": "active"})
        self.addCleanup(agent.save_hub, {})
        with mock.patch.object(agent, "hub_state", "prepared"), \
             mock.patch.object(agent.urllib.request, "urlopen", side_effect=OSError("no internet")):
            agent.enroll_remote()
            self.assertEqual(agent.hub_state, "active")


if __name__ == "__main__":
    unittest.main()
