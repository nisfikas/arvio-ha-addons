"""Wall screens: hub-local store, LAN list/pair only, relay put/delete, /panel without webfonts."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import ROOT, agent


class ScreenStoreTest(unittest.TestCase):
    def setUp(self):
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()

    def tearDown(self):
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()

    def _row(self, **patch):
        row = {
            "screen_id": "scr_aaaaaaaaaaaaaaaa",
            "site_id": "site_1",
            "hub_id": "hub_1",
            "name": "Κουζίνα",
            "hardware": "clone_4in",
            "orientation": "square",
            "pages": [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}],
            "pairing_code_hash": "deadbeef",
            "pairing_expires_at": "2099-01-01T00:00:00.000Z",
        }
        row.update(patch)
        return row

    def test_put_list_omits_hash(self):
        out = agent.put_wall_screen(self._row())
        self.assertTrue(out["ok"])
        listed = agent.list_wall_screens()["screens"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["hardware"], "clone_4in")
        self.assertEqual(listed[0]["orientation"], "square")
        self.assertNotIn("pairing_code_hash", listed[0])
        self.assertTrue(agent.SCREENS.exists())

    def test_refuses_lock_tiles(self):
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "entity", "ref": "lock.front"}]}])
            )
        self.assertIn("tile_security_forbidden", str(cm.exception))

    def test_pair_is_one_shot(self):
        code = "123456"
        digest = agent.hash_screen_pairing(code, "site_1", "scr_aaaaaaaaaaaaaaaa")
        agent.put_wall_screen(self._row(pairing_code_hash=digest))
        out = agent.pair_wall_screen(code)
        self.assertEqual(out["screen_id"], "scr_aaaaaaaaaaaaaaaa")
        with self.assertRaises(ValueError):
            agent.pair_wall_screen(code)

    def test_delete(self):
        agent.put_wall_screen(self._row())
        agent.delete_wall_screen({"screen_id": "scr_aaaaaaaaaaaaaaaa"})
        self.assertEqual(agent.list_wall_screens()["screens"], [])

    def test_lan_list_ok_writes_forbidden(self):
        agent.check_command_safety({"action": "arvio.list_screens"}, via="lan")
        agent.check_command_safety({"action": "arvio.trigger_scenario"}, via="lan")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.put_screen"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.delete_screen"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.put_screen"}, via="relay")

    def test_execute_put_via_relay(self):
        out = agent.execute_action("arvio.put_screen", "", self._row())
        self.assertTrue(out["ok"])
        self.assertEqual(out["screen"]["name"], "Κουζίνα")


class ScreenLanHttpTest(unittest.TestCase):
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
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_panel_has_no_webfonts(self):
        with mock.patch.object(agent, "APP", ROOT):
            status, raw = self.request("GET", "/panel")
        self.assertEqual(status, 200)
        html = raw.decode()
        self.assertNotIn("fonts.googleapis", html)
        self.assertNotIn("fonts.gstatic", html)
        self.assertIn("Κωδικός", html)

    def test_api_screens_omits_hash(self):
        digest = agent.hash_screen_pairing("123456", "site_1", "scr_aaaaaaaaaaaaaaaa")
        agent.put_wall_screen(
            {
                "screen_id": "scr_aaaaaaaaaaaaaaaa",
                "site_id": "site_1",
                "name": "Πάνελ",
                "hardware": "generic_square",
                "orientation": "square",
                "pages": [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "allOff"}]}],
                "pairing_code_hash": digest,
                "pairing_expires_at": "2099-01-01T00:00:00.000Z",
            }
        )
        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "entities", return_value=[]
        ), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=[]):
            status, raw = self.request("GET", "/api/screens")
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertEqual(body["screens"][0]["name"], "Πάνελ")
        self.assertNotIn("pairing_code_hash", body["screens"][0])
        dumped = json.dumps(body)
        self.assertNotIn("deadbeef", dumped)
        self.assertNotIn(digest, dumped)

    def test_pair_http(self):
        digest = agent.hash_screen_pairing("654321", "site_1", "scr_bbbbbbbbbbbbbbbb")
        agent.put_wall_screen(
            {
                "screen_id": "scr_bbbbbbbbbbbbbbbb",
                "site_id": "site_1",
                "name": "Χολ",
                "hardware": "generic_square",
                "orientation": "square",
                "pages": [{"id": "pg_bbbbbbbb", "tiles": [{"kind": "clock"}]}],
                "pairing_code_hash": digest,
                "pairing_expires_at": "2099-01-01T00:00:00.000Z",
            }
        )
        status, raw = self.request("POST", "/api/screens/pair", {"code": "654321"})
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertEqual(body["screen_id"], "scr_bbbbbbbbbbbbbbbb")


if __name__ == "__main__":
    unittest.main()
