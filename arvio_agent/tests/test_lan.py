"""Unauthenticated LAN command path: explicit allowlist, same-origin only (no CORS grants)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent


class LanPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def request(self, method, path, body=None, headers=None):
        """→ (status, headers, raw body) for any method (OPTIONS included)."""
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_commands_route(self):
        agent.COMMAND_ACKS = agent.LRU(500)
        calls = []

        def fake_execute(action, entity_id, payload=None, target=None):
            if not entity_id and target is None and not action.startswith("arvio."):
                raise ValueError("entity_id required")  # mirrors the real execute_action
            calls.append((action, entity_id, payload, target))
            return {"ok": True, "entity_id": entity_id, "state": "on", "affected_entity_ids": [entity_id]}

        with mock.patch.object(agent, "hub_id", "hub_lan"), \
             mock.patch.object(agent, "execute_action", side_effect=fake_execute):
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "light.turn_on", "entity_id": "light.a"})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["via"], "local-agent")
            self.assertTrue(body["command_id"].startswith("local_"))
            self.assertEqual(body["affected_entity_ids"], ["light.a"])
            for action in ("lock.unlock", "lock.lock", "alarm_control_panel.alarm_disarm",
                           "alarm_control_panel.alarm_arm_away", "arvio.batch", "arvio.model",
                           "arvio.upsert_scenario", "arvio.zigbee_permit", "backup.create", "agent.update",
                           "light.toggle", "arvio.put_screen", "arvio.delete_screen"):
                status, body = self.post("/v1/hubs/hub_lan/commands", {"action": action, "entity_id": "lock.a",
                                                                       "payload": {"confirm_dangerous": True}})
                self.assertEqual(status, 403, action)
                self.assertIn("lan_forbidden", body["error"])
            # read-only agent actions and arvio_ scripts are allowed locally
            for action, eid in (("arvio.list_entities", ""), ("arvio.pairing_status", ""),
                                ("script.turn_on", "script.arvio_kalinyxta"), ("climate.set_temperature", "climate.a")):
                status, body = self.post("/v1/hubs/hub_lan/commands", {"action": action, "entity_id": eid})
                self.assertEqual(status, 200, action)
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "script.turn_on", "entity_id": "script.other"})
            self.assertEqual(status, 403)
            self.assertIn("lan_forbidden", body["error"])
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "light.turn_off", "target": {"floor_id": "x"}})
            # target is ignored on the LAN route (never forwarded) → falls to entity_id required
            self.assertEqual(status, 400)
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "shell_command.rm", "entity_id": "x.y"})
            self.assertEqual(status, 403)
            status, _ = self.post("/v1/hubs/other/commands", {"action": "light.turn_on", "entity_id": "light.a"})
            self.assertEqual(status, 400)
        self.assertEqual([c[0] for c in calls], ["light.turn_on", "arvio.list_entities", "arvio.pairing_status",
                                                 "script.turn_on", "climate.set_temperature"])
        self.assertIsNone(calls[0][3])

    def test_api_service_route(self):
        with mock.patch.object(agent, "call_service", return_value={"ok": True, "state": "on"}) as cs:
            status, body = self.post("/api/service/light/turn_on", {"entity_id": "light.a"})
            self.assertEqual(status, 200)
            cs.assert_called_once_with("light", "turn_on", {"entity_id": "light.a"})
            status, body = self.post("/api/service/lock/unlock", {"entity_id": "lock.a"})
            self.assertEqual(status, 403)
            self.assertIn("lan_forbidden", body["error"])
            status, body = self.post("/api/service/homeassistant/restart", {"entity_id": "x.y"})
            self.assertEqual(status, 403)
            for domain, service, eid in (("arvio", "model", "x.y"), ("backup", "create", "x.y"),
                                         ("light", "toggle", "light.a"), ("script", "turn_on", "script.other")):
                status, body = self.post(f"/api/service/{domain}/{service}", {"entity_id": eid})
                self.assertEqual(status, 403, f"{domain}.{service}")
            self.assertEqual(cs.call_count, 1)
            status, _ = self.post("/api/service/script/turn_on", {"entity_id": "script.arvio_nyxta"})
            self.assertEqual(status, 200)
            status, _ = self.post("/api/service/cover/set_cover_position", {"entity_id": "cover.a"})
            self.assertEqual(status, 200)
            self.assertEqual(cs.call_count, 3)

    def test_same_origin_only_no_cors_grants(self):
        agent.save_hub({"hub_id": "hub_lan"})
        origin = {"Origin": "http://evil.example"}
        status, headers, _ = self.request("GET", "/health", headers=origin)
        self.assertEqual(status, 200)
        self.assertFalse([k for k in headers if k.lower().startswith("access-control-")], headers)
        with mock.patch.object(agent, "hub_id", "hub_lan"):
            status, headers, _ = self.request("POST", "/v1/hubs/hub_lan/commands",
                                              {"action": "lock.lock", "entity_id": "lock.a"}, headers=origin)
        self.assertEqual(status, 403)
        self.assertFalse([k for k in headers if k.lower().startswith("access-control-")], headers)
        # a cross-origin preflight gets a plain 204 with no grants → the browser fails it closed
        status, headers, body = self.request(
            "OPTIONS", "/v1/hubs/hub_lan/commands",
            headers={**origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
        )
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertFalse([k for k in headers if k.lower().startswith("access-control-")], headers)
        self.assertEqual(headers.get("Allow"), "GET, POST, OPTIONS")

    def test_health_carries_snapshot_version(self):
        agent.save_hub({"hub_id": "hub_lan", "snapshot_version": 7})
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            body = json.loads(r.read().decode())
        self.assertEqual(body["agent_version"], "0.1.35")
        self.assertIn("ma_available", body)
        self.assertEqual(body["snapshot_version"], 7)

    def test_status_omits_presence_code(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as r:
            body = json.loads(r.read().decode())
        self.assertNotIn("presence_code", body)
        self.assertIn("hub_id", body)

    def test_claim_proxies_are_forbidden(self):
        for path in (
            "/v1/hubs/hub_lan/claims",
            "/v1/claims/claim_x/presence",
            "/v1/claims/claim_x/redeem",
        ):
            status, _headers, raw = self.request("POST", path, {"code": "123456", "token": "t", "site_id": "s"})
            self.assertEqual(status, 403, path)
            body = json.loads(raw.decode())
            self.assertEqual(body.get("error_code") or body.get("error"), "lan_forbidden")

    def test_ui_paints_presence_code(self):
        agent.code = "405421"
        with mock.patch.object(agent, "APP", __import__("pathlib").Path(__file__).resolve().parents[1]):
            status, _headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode()
        self.assertIn(">405421<", html)
        self.assertNotIn("presence_code", html)


if __name__ == "__main__":
    unittest.main()
