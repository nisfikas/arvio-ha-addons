"""Wall screens: hub-local store, LAN list/pair only, relay put/delete, /panel without webfonts."""
from __future__ import annotations

import base64
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import ROOT, agent


JPEG = bytes([0xFF, 0xD8, 0xFF]) + b"\x11" * 80 + bytes([0xFF, 0xD9])


class ScreenStoreTest(unittest.TestCase):
    def setUp(self):
        agent.DOORBELL_LATCH.clear()
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.WALLPAPERS.exists():
            shutil.rmtree(agent.WALLPAPERS)

    def tearDown(self):
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.WALLPAPERS.exists():
            shutil.rmtree(agent.WALLPAPERS)

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

    def test_wallpaper_file_not_in_json(self):
        b64 = base64.b64encode(JPEG).decode()
        out = agent.put_wall_screen(
            self._row(
                wallpaper={"asset_id": "wp_aaaaaaaaaaaaaaaa", "scrim": 0.4},
                wallpaper_image={"mime": "image/jpeg", "data_base64": b64, "scrim": 0.4},
            )
        )
        self.assertTrue(out["screen"]["has_wallpaper"])
        self.assertEqual(out["screen"]["wallpaper_scrim"], 0.4)
        dumped = agent.SCREENS.read_text(encoding="utf-8")
        self.assertNotIn(b64, dumped)
        self.assertTrue(agent.wallpaper_file("scr_aaaaaaaaaaaaaaaa").is_file())
        cleared = agent.put_wall_screen(self._row(wallpaper=None))
        self.assertFalse(cleared["screen"]["has_wallpaper"])
        self.assertFalse(agent.wallpaper_file("scr_aaaaaaaaaaaaaaaa").is_file())

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

    def test_keeps_tile_size(self):
        out = agent.put_wall_screen(
            self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock", "size": "l"}]}])
        )
        tile = out["screen"]["pages"][0]["tiles"][0]
        self.assertEqual(tile["size"], "l")
        self.assertEqual(tile["span"], 2)

    def test_doorbell_infer_and_latch(self):
        self.assertEqual(
            agent.doorbell_call_guesses("camera.8b014b9pajf9590_main"),
            [
                "binary_sensor.8b014b9pajf9590_button_pressed",
                "binary_sensor.8b014b9pajf9590_call",
                "binary_sensor.8b014b9pajf9590_doorbell",
            ],
        )
        self.assertEqual(
            agent.doorbell_call_ids_from_cameras(["camera.8b014b9pajf9590_main"]),
            {
                "binary_sensor.8b014b9pajf9590_button_pressed",
                "binary_sensor.8b014b9pajf9590_call",
                "binary_sensor.8b014b9pajf9590_doorbell",
            },
        )
        agent.put_wall_screen(
            self._row(
                doorbell={
                    "camera_entity_id": "camera.8b014b9pajf9590_main",
                    "unlock_entity_id": "lock.front",
                }
            )
        )
        listed = agent.list_wall_screens()["screens"][0]
        self.assertEqual(listed["doorbell"]["camera_entity_id"], "camera.8b014b9pajf9590_main")
        self.assertNotIn("ringing", listed["doorbell"])
        agent.note_doorbell_ring("binary_sensor.8b014b9pajf9590_button_pressed", {"state": "on"})
        screens = [{"doorbell": dict(listed["doorbell"])}]
        agent.attach_doorbell_live(screens, [])
        self.assertTrue(screens[0]["doorbell"]["ringing"])
        self.assertEqual(screens[0]["doorbell"]["call_entity_id"], "binary_sensor.8b014b9pajf9590_button_pressed")
        agent.note_doorbell_ring("binary_sensor.other_button_pressed", {"state": "on"})
        self.assertNotIn("binary_sensor.other_button_pressed", agent.DOORBELL_LATCH)
        agent.put_wall_screen(self._row(doorbell=None))
        self.assertNotIn("doorbell", agent.list_wall_screens()["screens"][0])


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
        agent.DOORBELL_LATCH.clear()
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.WALLPAPERS.exists():
            shutil.rmtree(agent.WALLPAPERS)

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
        self.assertIn("Κάποιος στο κουδούνι", html)

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

    def test_wallpaper_http(self):
        b64 = base64.b64encode(JPEG).decode()
        agent.put_wall_screen(
            {
                "screen_id": "scr_aaaaaaaaaaaaaaaa",
                "site_id": "site_1",
                "name": "Πάνελ",
                "hardware": "generic_square",
                "orientation": "square",
                "pages": [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}],
                "wallpaper": {"asset_id": "wp_aaaaaaaaaaaaaaaa", "scrim": 0.3},
                "wallpaper_image": {"mime": "image/jpeg", "data_base64": b64, "scrim": 0.3},
            }
        )
        status, raw = self.request("GET", "/api/screens/wallpaper?id=scr_aaaaaaaaaaaaaaaa")
        self.assertEqual(status, 200)
        self.assertEqual(raw, JPEG)
        status, _ = self.request("GET", "/api/screens/wallpaper?id=scr_nope")
        self.assertEqual(status, 404)

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

    def test_doorbell_jpeg_and_unlock(self):
        agent.put_wall_screen(
            {
                "screen_id": "scr_aaaaaaaaaaaaaaaa",
                "site_id": "site_1",
                "name": "Πάνελ",
                "hardware": "generic_square",
                "orientation": "square",
                "pages": [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}],
                "doorbell": {
                    "camera_entity_id": "camera.8b014b9pajf9590_main",
                    "unlock_entity_id": "lock.front",
                },
            }
        )
        status, _ = self.request("GET", "/api/screens/doorbell.jpg?id=scr_nope")
        self.assertEqual(status, 404)
        with mock.patch.object(agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")):
            status, raw = self.request("GET", "/api/screens/doorbell.jpg?id=scr_aaaaaaaaaaaaaaaa")
        self.assertEqual(status, 200)
        self.assertEqual(raw, JPEG)
        status, raw = self.request("POST", "/api/screens/doorbell/unlock", {"screen_id": "scr_nope"})
        self.assertEqual(status, 403)
        with mock.patch.object(agent, "call_service", return_value={"ok": True, "entity_id": "lock.front"}) as cs:
            status, raw = self.request("POST", "/api/screens/doorbell/unlock", {"screen_id": "scr_aaaaaaaaaaaaaaaa"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw.decode())["ok"])
        cs.assert_called_once_with("lock", "unlock", {"entity_id": "lock.front"})


if __name__ == "__main__":
    unittest.main()
