"""Wall screens: hub-local store, LAN list/pair only, relay put/delete, /panel without webfonts."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import re
import shutil
import threading
import time
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
        agent.PAIR_FAILS.clear()
        agent.PAIR_LOCKED_UNTIL = 0.0
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.FLOOR_PLANS.exists():
            agent.FLOOR_PLANS.unlink()
        if agent.WALLPAPERS.exists():
            shutil.rmtree(agent.WALLPAPERS)

    def tearDown(self):
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.FLOOR_PLANS.exists():
            agent.FLOOR_PLANS.unlink()
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

    def test_keeps_clock_and_weather_bold(self):
        agent.put_wall_screen(
            self._row(
                pages=[
                    {
                        "id": "pg_aaaaaaaa",
                        "tiles": [
                            {"kind": "clock", "bold": True, "size": "l"},
                            {"kind": "weather", "bold": True},
                            {"kind": "allOff", "bold": True},
                        ],
                    }
                ]
            )
        )
        tiles = agent.list_wall_screens()["screens"][0]["pages"][0]["tiles"]
        self.assertTrue(tiles[0]["bold"])
        self.assertTrue(tiles[1]["bold"])
        self.assertNotIn("bold", tiles[2])

    def test_keeps_clock_and_weather_align(self):
        agent.put_wall_screen(
            self._row(
                pages=[
                    {
                        "id": "pg_aaaaaaaa",
                        "tiles": [
                            {"kind": "clock", "align": "center", "size": "l"},
                            {"kind": "weather", "align": "right"},
                            {"kind": "allOff", "align": "center"},
                            {"kind": "clock", "align": "left"},
                        ],
                    }
                ]
            )
        )
        tiles = agent.list_wall_screens()["screens"][0]["pages"][0]["tiles"]
        self.assertEqual(tiles[0]["align"], "center")
        self.assertEqual(tiles[1]["align"], "right")
        self.assertNotIn("align", tiles[2])
        self.assertNotIn("align", tiles[3])

    def test_refuses_invalid_tile_align(self):
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(
                    pages=[
                        {
                            "id": "pg_aaaaaaaa",
                            "tiles": [{"kind": "clock", "align": "middle"}],
                        }
                    ]
                )
            )
        self.assertIn("invalid_align", str(cm.exception))

    def test_keeps_room_lights(self):
        agent.put_wall_screen(
            self._row(
                pages=[
                    {
                        "id": "pg_aaaaaaaa",
                        "tiles": [
                            {
                                "kind": "room",
                                "ref": "kitchen",
                                "lights": ["light.main", "light.main", "light.strip"],
                            },
                            {"kind": "room", "ref": "hall"},
                            {"kind": "room", "ref": "empty", "lights": []},
                        ],
                    }
                ]
            )
        )
        tiles = agent.list_wall_screens()["screens"][0]["pages"][0]["tiles"]
        self.assertEqual(tiles[0]["lights"], ["light.main", "light.strip"])
        self.assertNotIn("lights", tiles[1])
        self.assertEqual(tiles[2]["lights"], [])

    def test_refuses_room_switch_as_light(self):
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(
                    pages=[
                        {
                            "id": "pg_aaaaaaaa",
                            "tiles": [{"kind": "room", "ref": "kitchen", "lights": ["switch.fan"]}],
                        }
                    ]
                )
            )
        self.assertIn("invalid_tile_lights", str(cm.exception))

    def test_keeps_theme(self):
        agent.put_wall_screen(self._row(theme={"preset": "galini", "mode": "light", "icons": "filled"}))
        listed = agent.list_wall_screens()["screens"][0]
        self.assertEqual(listed["theme"], {"preset": "galini", "mode": "light", "icons": "filled"})
        agent.put_wall_screen(self._row())
        self.assertEqual(agent.list_wall_screens()["screens"][0]["theme"], listed["theme"])
        cleared = agent.put_wall_screen(self._row(theme=None))
        self.assertNotIn("theme", cleared["screen"])

    def test_refuses_unknown_theme(self):
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(self._row(theme={"preset": "neon", "mode": "dark", "icons": "line"}))
        self.assertIn("invalid_theme", str(cm.exception))

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

    def test_floor3d_needs_ref(self):
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "floor3d"}]}])
            )
        self.assertIn("invalid_tile_ref", str(cm.exception))
        tiles = agent.put_wall_screen(
            self._row(
                pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "floor3d", "ref": "isogeio", "size": "l"}]}]
            )
        )["screen"]["pages"][0]["tiles"]
        self.assertEqual(tiles[0]["kind"], "floor3d")
        self.assertEqual(tiles[0]["ref"], "isogeio")
        self.assertEqual(tiles[0]["size"], "l")

    def test_put_keeps_shutdown_tile_and_watch(self):
        tiles = agent.put_wall_screen(
            self._row(
                pages=[
                    {
                        "id": "pg_aaaaaaaa",
                        "tiles": [
                            {
                                "kind": "shutdown",
                                "ref": "script.201_scene_shutdown",
                                "watch": "light.201_genikos_201_137",
                            },
                            {
                                "kind": "shutdown",
                                "ref": "scenario:arvio_sd_201",
                                "watch": "light.201_genikos_201_137",
                            },
                        ],
                    }
                ]
            )
        )["screen"]["pages"][0]["tiles"]
        self.assertEqual(tiles[0]["kind"], "shutdown")
        self.assertEqual(tiles[0]["ref"], "script.201_scene_shutdown")
        self.assertEqual(tiles[0]["watch"], "light.201_genikos_201_137")
        self.assertEqual(tiles[1]["ref"], "scenario:arvio_sd_201")
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "shutdown", "ref": "script.arvio_home"}]}])
            )
        self.assertIn("invalid_tile_ref", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            agent.put_wall_screen(
                self._row(
                    pages=[
                        {
                            "id": "pg_aaaaaaaa",
                            "tiles": [
                                {
                                    "kind": "shutdown",
                                    "ref": "script.201_scene_shutdown",
                                    "watch": "switch.x",
                                }
                            ],
                        }
                    ]
                )
            )
        self.assertIn("invalid_tile_watch", str(cm.exception))
        agent.check_command_safety(
            {"action": "script.turn_on", "entity_id": "script.201_scene_shutdown"},
            via="lan",
        )
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety(
                {"action": "script.turn_on", "entity_id": "script.999_scene_shutdown"},
                via="lan",
            )
        self.assertEqual(cm.exception.code, "lan_forbidden")

    def test_put_floor_plan_and_screen_ingest(self):
        plan = {
            "floor_id": "isogeio",
            "height_cm": 270,
            "rooms": [
                {
                    "area_id": "living",
                    "poly_cm": [{"x": 0, "y": 0}, {"x": 400, "y": 0}, {"x": 400, "y": 400}, {"x": 0, "y": 400}],
                }
            ],
            "pins": [{"entity_id": "light.sofa", "x_cm": 200, "y_cm": 200, "slot": "ceiling"}],
        }
        self.assertTrue(agent.put_floor_plan(plan)["ok"])
        stored = agent.load_floor_plans()["plans"]["isogeio"]
        self.assertEqual(stored["pins"][0]["entity_id"], "light.sofa")
        agent.FLOOR_PLANS.unlink()
        agent.put_wall_screen(self._row(floor_plans=[plan]))
        self.assertEqual(agent.load_floor_plans()["plans"]["isogeio"]["rooms"][0]["area_id"], "living")

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

    def test_lan_list_and_writes_forbidden(self):
        # 0.1.50: listing every screen id + doorbell config is relay-only.
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.list_screens"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.list_screens"}, via="relay")
        agent.check_command_safety({"action": "arvio.trigger_scenario"}, via="lan")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.put_screen"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.put_floor_plan"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.delete_screen"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.put_screen"}, via="relay")
        agent.check_command_safety({"action": "arvio.put_floor_plan"}, via="relay")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.put_venue"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.put_venue"}, via="relay")

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
        screens = [{"doorbell": dict(listed["doorbell"])}]
        agent.attach_doorbell_live(screens, [])
        self.assertTrue(screens[0]["doorbell"]["ringing"])
        self.assertNotIn("binary_sensor.other_button_pressed", agent.DOORBELL_LATCH)
        agent.put_wall_screen(self._row(doorbell=None))
        self.assertNotIn("doorbell", agent.list_wall_screens()["screens"][0])

    def test_doorbell_rising_edge_notifies_cloud(self):
        watched = {"binary_sensor.vto_button_pressed"}
        with mock.patch.object(agent, "post_doorbell_ring") as post:
            agent.note_doorbell_ring("binary_sensor.vto_button_pressed", {"state": "on"}, watched)
            for _ in range(20):
                if post.called:
                    break
                time.sleep(0.01)
            post.assert_called_once_with("binary_sensor.vto_button_pressed")
            agent.note_doorbell_ring("binary_sensor.vto_button_pressed", {"state": "on"}, watched)
            time.sleep(0.05)
            post.assert_called_once()


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
        agent.PAIR_FAILS.clear()
        agent.PAIR_LOCKED_UNTIL = 0.0
        agent.WEATHER_FORECAST_CACHE.update(at=0.0, eid="", daily=None, hourly=None)
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()
        if agent.FLOOR_PLANS.exists():
            agent.FLOOR_PLANS.unlink()
        if agent.WALLPAPERS.exists():
            shutil.rmtree(agent.WALLPAPERS)

    def request(self, method, path, body=None, secret=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if secret is not None:
            headers["X-Arvio-Screen-Secret"] = secret
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    PANEL_ID = "scr_pppppppppppppppp"

    def panel_get(self, secret=None):
        """GET /api/screens for a saved panel (paired before 0.1.50 → reads without a secret)."""
        if not agent.load_screens()["screens"].get(self.PANEL_ID):
            agent.put_wall_screen(
                {
                    "screen_id": self.PANEL_ID,
                    "site_id": "site_1",
                    "name": "Πάνελ",
                    "hardware": "generic_square",
                    "orientation": "square",
                    "pages": [{"id": "pg_pppppppp", "tiles": [{"kind": "clock"}]}],
                }
            )
        return self.request("GET", f"/api/screens?screen_id={self.PANEL_ID}", secret=secret)

    def test_panel_has_no_webfonts(self):
        with mock.patch.object(agent, "APP", ROOT):
            status, raw = self.request("GET", "/panel")
        self.assertEqual(status, 200)
        html = raw.decode()
        self.assertNotIn("fonts.googleapis", html)
        self.assertNotIn("fonts.gstatic", html)
        self.assertIn("Κωδικός", html)
        self.assertIn("Κάποιος στο κουδούνι", html)
        self.assertIn("data-hvac", html)
        self.assertIn("data-clima", html)
        self.assertIn("data-temp-step", html)
        self.assertIn("clima-mode", html)
        self.assertIn("THEMES", html)
        self.assertIn("galini", html)
        self.assertIn("floor3d__svg", html)
        self.assertIn("function setOverlay", html)
        self.assertIn("54cqh", html)
        self.assertIn("tile--clock[data-size=\"l\"] .face", html)
        self.assertNotIn("three.js", html)
        self.assertNotIn("unpkg.com", html)

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
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa")
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertEqual(body["screens"][0]["name"], "Πάνελ")
        self.assertNotIn("pairing_code_hash", body["screens"][0])
        dumped = json.dumps(body)
        self.assertNotIn("deadbeef", dumped)
        self.assertNotIn(digest, dumped)

    def test_panel_snapshot_includes_climate_fields(self):
        climate = {
            "entity_id": "climate.saloni",
            "state": "heat",
            "name": "Κλιματιστικό",
            "domain": "climate",
            "current_temperature": 24.5,
            "temperature": 21,
            "hvac_mode": "heat",
            "hvac_modes": ["off", "heat", "cool"],
            "preset_mode": "none",
            "preset_modes": ["none", "eco", "sleep"],
            "fan_mode": "auto",
            "fan_modes": ["auto", "low", "high"],
            "swing_mode": "off",
            "swing_modes": ["off", "vertical"],
            "min_temp": 16,
            "max_temp": 30,
        }
        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "entities", return_value=[climate]
        ), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=[]):
            status, raw = self.panel_get()
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        row = body["entities"][0]
        self.assertEqual(row["entity_id"], "climate.saloni")
        self.assertEqual(row["current_temperature"], 24.5)
        self.assertEqual(row["temperature"], 21)
        self.assertEqual(row["hvac_modes"], ["off", "heat", "cool"])
        self.assertEqual(row["preset_modes"], ["none", "eco", "sleep"])
        self.assertEqual(row["fan_modes"], ["auto", "low", "high"])
        self.assertEqual(row["swing_modes"], ["off", "vertical"])

    def test_panel_snapshot_includes_cover_position(self):
        cover = {
            "entity_id": "cover.rola",
            "state": "open",
            "name": "Ρολό",
            "domain": "cover",
            "current_position": 80,
            "device_class": "shutter",
        }
        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "entities", return_value=[cover]
        ), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=[]):
            status, raw = self.panel_get()
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        row = body["entities"][0]
        self.assertEqual(row["current_position"], 80)
        self.assertEqual(row["device_class"], "shutter")

    def test_panel_cover_has_animated_shade(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("cv-shade", html)
        self.assertIn('data-cover="curtain"', html)
        self.assertIn("--cover-fill", html)
        self.assertIn("cover.stop_cover", html)

    def test_panel_snapshot_includes_locks_and_openings(self):
        lock = {
            "entity_id": "lock.front",
            "state": "locked",
            "name": "Πόρτα",
            "domain": "lock",
        }
        states = [
            {
                "entity_id": "binary_sensor.door",
                "state": "on",
                "attributes": {"friendly_name": "Είσοδος", "device_class": "door"},
            },
            {
                "entity_id": "binary_sensor.motion",
                "state": "on",
                "attributes": {"friendly_name": "Κίνηση", "device_class": "motion"},
            },
            {
                "entity_id": "camera.gate",
                "state": "idle",
                "attributes": {"friendly_name": "Αυλή"},
            },
        ]
        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "entities", return_value=[lock]
        ), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=states):
            status, raw = self.panel_get()
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        by_id = {e["entity_id"]: e for e in body["entities"]}
        self.assertEqual(by_id["lock.front"]["state"], "locked")
        self.assertEqual(by_id["binary_sensor.door"]["device_class"], "door")
        self.assertEqual(by_id["camera.gate"]["name"], "Αυλή")
        self.assertNotIn("binary_sensor.motion", by_id)

    def test_panel_snapshot_includes_hourly_weather(self):
        states = [
            {
                "entity_id": "weather.home",
                "state": "sunny",
                "attributes": {"temperature": 24, "humidity": 48},
            }
        ]
        daily = {
            "weather.home": {
                "forecast": [
                    {
                        "datetime": "2026-09-19T00:00:00+00:00",
                        "condition": "sunny",
                        "temperature": 28,
                        "templow": 18,
                    }
                ]
            }
        }
        hourly = {
            "weather.home": {
                "forecast": [
                    {
                        "datetime": "2026-09-19T12:00:00+00:00",
                        "condition": "sunny",
                        "temperature": 24,
                        "precipitation": 0.4,
                    }
                ]
            }
        }

        def forecasts(domain, service, data):
            self.assertEqual((domain, service), ("weather", "get_forecasts"))
            if data.get("type") == "daily":
                return None, daily
            return None, hourly

        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "entities", return_value=[]
        ), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=states), mock.patch.object(
            agent, "ha_call_service_response", side_effect=forecasts
        ):
            status, raw = self.panel_get()
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        w = body["weather"]
        self.assertEqual(w["temperature"], 24)
        self.assertEqual(w["humidity"], 48)
        self.assertEqual(w["hourly"][0]["precipitation"], 0.4)
        self.assertEqual(w["forecast"][0]["templow"], 18)

    def test_panel_weather_sheet_has_hours_and_week(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("Ώρες", html)
        self.assertIn("Εβδομάδα", html)
        self.assertIn("wx-hours", html)
        self.assertIn("centerWxNow", html)
        self.assertIn("wx-sky", html)
        self.assertIn("function wxScene", html)
        self.assertIn("colon-blink", html)
        self.assertIn('dataset.align', html)
        self.assertIn("Ο καιρός χρειάζεται τη θέση του σπιτιού στο Home Assistant", html)

    def test_panel_security_is_status_and_room_has_lamps(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('openSheet({ kind: "security" })', html)
        self.assertIn("lamp-glow", html)
        self.assertIn("lightsForRoom", html)
        self.assertNotIn("lock.unlock", html)
        self.assertNotIn("alarm_disarm", html)

    def test_panel_climate_is_a_dial_not_a_clipped_hero(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("clima-dial", html)
        self.assertIn("clima-nudge", html)
        self.assertIn("τώρα ", html)
        self.assertNotIn("clima-hero", html)
        self.assertNotIn("clima-now", html)
        self.assertIn("tempCommitTimer", html)

    def test_panel_hidden_class_really_hides(self):
        # «Ναι, άνοιξε» once stayed on screen: `.hidden` only hid the ids the rule names, so the
        # door confirm step was skipped. Every id the page toggles with "hidden" must be named.
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        rule = re.search(r"^\s*(#[^{]*\.hidden[^{]*)\{\s*display:\s*none;", html, re.M)
        self.assertIsNotNone(rule)
        hidden_ids = set(re.findall(r"#([\w-]+)\.hidden", rule.group(1)))
        consts = dict(re.findall(r'const (\w+) = document\.getElementById\("([\w-]+)"\)', html))
        toggled = {consts.get(v, v) for v in re.findall(r'(\w+)\.classList\.(?:add|remove|toggle)\("hidden"', html)}
        toggled |= set(re.findall(r'id="([\w-]+)" class="hidden"', html))
        self.assertTrue({"doorbell-open", "doorbell-yes"} <= toggled)
        self.assertEqual(toggled - hidden_ids, set())

    def test_panel_idle_returns_to_first_page(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("HOME_IDLE_MS = 60000", html)
        self.assertIn("function goHome()", html)
        self.assertIn("function armHomeIdle()", html)
        self.assertIn('document.addEventListener("pointerdown"', html)

    def test_panel_shutdown_tiles_run_listed_scripts(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("function shutdownMode(t)", html)
        self.assertIn('t.kind === "shutdown"', html)
        self.assertIn("data-shut", html)
        self.assertIn("script.turn_on", html)
        self.assertIn("arvio.trigger_scenario", html)
        self.assertIn("scenario:arvio_sd_", html)

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
        secret = self._paired()
        status, _ = self.request("GET", "/api/screens/doorbell.jpg?id=scr_nope", secret=secret)
        self.assertEqual(status, 404)
        with mock.patch.object(agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")):
            status, raw = self.request("GET", "/api/screens/doorbell.jpg?id=scr_aaaaaaaaaaaaaaaa", secret=secret)
        self.assertEqual(status, 200)
        self.assertEqual(raw, JPEG)
        status, raw = self.request("POST", "/api/screens/doorbell/unlock", {"screen_id": "scr_nope"}, secret=secret)
        self.assertEqual(status, 403)
        self.ring()
        with mock.patch.object(agent, "call_service", return_value={"ok": True, "entity_id": "lock.front"}) as cs:
            status, raw = self.unlock(secret)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw.decode())["ok"])
        cs.assert_called_once_with("lock", "unlock", {"entity_id": "lock.front"})

    # --- 0.1.50: a guest on the Wi-Fi could list the screens and open the front door ---

    CALL = "binary_sensor.8b014b9pajf9590_button_pressed"

    def _row(self, screen_id="scr_aaaaaaaaaaaaaaaa", code=None, **extra):
        row = {
            "screen_id": screen_id,
            "site_id": "site_1",
            "name": "Πάνελ",
            "hardware": "generic_square",
            "orientation": "square",
            "pages": [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}],
            "doorbell": {"camera_entity_id": "camera.8b014b9pajf9590_main", "unlock_entity_id": "lock.front"},
        }
        if code:
            row["pairing_code_hash"] = agent.hash_screen_pairing(code, "site_1", screen_id)
            row["pairing_expires_at"] = "2099-01-01T00:00:00.000Z"
            row["pairing_used_at"] = None
        row.update(extra)
        return row

    def _paired(self, code="246810", **extra):
        """Saved and paired the 0.1.50 way; returns the plain secret."""
        agent.put_wall_screen(self._row(code=code, **extra))
        status, raw = self.request("POST", "/api/screens/pair", {"code": code})
        self.assertEqual(status, 200, raw)
        return json.loads(raw.decode())["screen_secret"]

    def ring(self, eid=CALL):
        agent.note_doorbell_ring(eid, {"state": "on"})

    def unlock(self, secret=None, screen_id="scr_aaaaaaaaaaaaaaaa"):
        return self.request("POST", "/api/screens/doorbell/unlock", {"screen_id": screen_id}, secret=secret)

    def ha_mocks(self):
        lock = {"entity_id": "lock.front", "state": "locked", "name": "Πόρτα", "domain": "lock"}
        alarm = {"entity_id": "alarm_control_panel.spiti", "state": "disarmed", "attributes": {}}
        return mock.patch.multiple(
            agent,
            hub_id="hub_lan",
            entities=mock.Mock(return_value=[lock]),
            registries_for_commands=mock.Mock(return_value={"entity_regs": {}, "devices": {}, "areas": {}}),
            ha=mock.Mock(return_value=[alarm]),
        )

    def test_list_without_screen_id_leaks_no_rows_doorbell_or_states(self):
        self._paired()
        self.panel_get()  # and a panel paired before 0.1.50
        self.ring()
        with self.ha_mocks():
            status, raw = self.request("GET", "/api/screens")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode()), {"ok": True, "screens": []})
            # an unknown id is not a way round it
            status, raw404 = self.request("GET", "/api/screens?screen_id=scr_nope")
            self.assertEqual(status, 404)
            # a known panel sees its own row, never its neighbours'
            status, own = self.panel_get()
            self.assertEqual(status, 200)
        for body in (raw.decode(), raw404.decode()):
            for leak in ("scr_", "doorbell", "lock.front", "alarm_control_panel", "camera.", "entities"):
                self.assertNotIn(leak, body)
        self.assertEqual([s["screen_id"] for s in json.loads(own.decode())["screens"]], [self.PANEL_ID])
        self.assertNotIn("scr_aaaaaaaaaaaaaaaa", own.decode())

    def test_pair_returns_secret_once_and_stores_only_its_hash(self):
        agent.put_wall_screen(self._row(code="246810"))
        status, raw = self.request("POST", "/api/screens/pair", {"code": "246810"})
        self.assertEqual(status, 200)
        secret = json.loads(raw.decode())["screen_secret"]
        self.assertGreaterEqual(len(secret), 43)  # token_urlsafe(32)
        stored = agent.SCREENS.read_text(encoding="utf-8")
        self.assertNotIn(secret, stored)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        self.assertIn(digest, stored)
        # once: the code is spent, nothing comes back the second time
        status, raw = self.request("POST", "/api/screens/pair", {"code": "246810"})
        self.assertEqual(status, 400)
        self.assertNotIn(b"screen_secret", raw)
        # the hash never leaves the hub — not to the panel, not to the cloud list
        with self.ha_mocks():
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=secret)
        self.assertEqual(status, 200)
        self.assertNotIn(digest, raw.decode())
        self.assertNotIn("screen_secret", raw.decode())
        self.assertNotIn(digest, json.dumps(agent.list_wall_screens()))

    def test_layout_push_keeps_secret_and_new_code_rotates_it(self):
        old = self._paired()
        # every Partner layout save resends the same hash with pairing_used_at=null
        agent.put_wall_screen(self._row(code="246810", name="Είσοδος"))
        self.assertEqual(
            agent.load_screens()["screens"]["scr_aaaaaaaaaaaaaaaa"]["screen_secret_hash"],
            agent.hash_screen_secret(old),
        )
        status, _ = self.request("POST", "/api/screens/pair", {"code": "246810"})
        self.assertEqual(status, 400)  # a spent code stays spent
        with self.ha_mocks():
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=old)
            self.assertFalse(json.loads(raw.decode())["screens"][0]["pairing_pending"])
            # Partner «Νέος κωδικός» → the panel is told to show the code field
            agent.put_wall_screen(self._row(code="135790"))
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=old)
            self.assertTrue(json.loads(raw.decode())["screens"][0]["pairing_pending"])
            status, raw = self.request("POST", "/api/screens/pair", {"code": "135790"})
            new = json.loads(raw.decode())["screen_secret"]
            self.assertNotEqual(new, old)
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=old)
            self.assertEqual((status, json.loads(raw.decode())["error"]), (403, "bad_screen_secret"))
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=new)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw.decode())["screens"][0]["pairing_pending"])

    def test_screen_with_secret_answers_403_to_a_wrong_or_missing_one(self):
        secret = self._paired(
            wallpaper_image={"mime": "image/jpeg", "data_base64": base64.b64encode(JPEG).decode(), "scrim": 0.3}
        )
        self.ring()
        wrong = secret[:-1] + ("A" if secret[-1] != "A" else "B")
        sid = "scr_aaaaaaaaaaaaaaaa"
        with self.ha_mocks(), mock.patch.object(
            agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")
        ) as cam, mock.patch.object(agent, "call_service", return_value={"ok": True}) as cs:
            for bad in (None, "", wrong):
                for method, path in (
                    ("GET", f"/api/screens?screen_id={sid}"),
                    ("GET", f"/api/screens/wallpaper?id={sid}"),
                    ("GET", f"/api/screens/doorbell.jpg?id={sid}"),
                ):
                    status, raw = self.request(method, path, secret=bad)
                    self.assertEqual(status, 403, (bad, path))
                    self.assertEqual(json.loads(raw.decode())["error"], "bad_screen_secret")
                    self.assertNotIn(b"lock.front", raw)
                status, raw = self.unlock(bad)
                self.assertEqual(status, 403, bad)
            cam.assert_not_called()
            cs.assert_not_called()
            for path in (
                f"/api/screens?screen_id={sid}",
                f"/api/screens/wallpaper?id={sid}",
                f"/api/screens/doorbell.jpg?id={sid}",
            ):
                status, _ = self.request("GET", path, secret=secret)
                self.assertEqual(status, 200, path)
            self.assertEqual(self.unlock(secret)[0], 200)

    def test_legacy_screen_reads_but_cannot_open_door_or_camera(self):
        agent.put_wall_screen(self._row())  # paired before 0.1.50: no secret hash on the row
        self.ring()
        with self.ha_mocks(), mock.patch.object(
            agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")
        ) as cam, mock.patch.object(agent, "call_service", return_value={"ok": True}) as cs:
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw.decode())["screens"][0]["doorbell"]["ringing"])
            status, raw = self.request("GET", "/api/screens/doorbell.jpg?id=scr_aaaaaaaaaaaaaaaa")
            self.assertEqual((status, json.loads(raw.decode())["error"]), (403, "repair_required"))
            status, raw = self.unlock()
            self.assertEqual((status, json.loads(raw.decode())["error"]), (403, "repair_required"))
            # a made-up header does not help either
            self.assertEqual(self.unlock("anything")[0], 403)
        cam.assert_not_called()
        cs.assert_not_called()

    def test_unlock_needs_a_ring_and_succeeds_after_one(self):
        secret = self._paired()
        with mock.patch.object(agent, "call_service", return_value={"ok": True}) as cs:
            status, raw = self.unlock(secret)
            self.assertEqual((status, json.loads(raw.decode())["error"]), (403, "no_recent_ring"))
            # someone else's doorbell does not open this door
            other = "binary_sensor.other_button_pressed"
            agent.note_doorbell_ring(other, {"state": "on"}, {other})
            self.assertEqual(self.unlock(secret)[0], 403)
            cs.assert_not_called()
            self.ring("binary_sensor.8b014b9pajf9590_call")  # any call sensor of this screen's camera
            status, _ = self.unlock(secret)
            self.assertEqual(status, 200)
        cs.assert_called_once_with("lock", "unlock", {"entity_id": "lock.front"})

    def test_ring_window_expires_after_45_seconds(self):
        secret = self._paired()
        self.ring()
        with mock.patch.object(agent, "call_service", return_value={"ok": True}) as cs:
            agent.DOORBELL_LATCH[self.CALL] = time.monotonic() + agent.DOORBELL_LATCH_S - 44  # rang 44 s ago
            self.assertEqual(self.unlock(secret)[0], 200)
            agent.DOORBELL_LATCH[self.CALL] = time.monotonic() + agent.DOORBELL_LATCH_S - 46  # rang 46 s ago
            status, raw = self.unlock(secret)
            self.assertEqual((status, json.loads(raw.decode())["error"]), (403, "no_recent_ring"))
        self.assertEqual(cs.call_count, 1)

    def test_pairing_locks_out_after_ten_wrong_codes(self):
        agent.put_wall_screen(self._row(code="246810"))
        for i in range(10):
            status, raw = self.request("POST", "/api/screens/pair", {"code": f"{i:06d}"})
            self.assertEqual((status, json.loads(raw.decode())["error"]), (400, "pairing_mismatch"), i)
        # 11th attempt: refused for everyone — even the right code
        status, raw = self.request("POST", "/api/screens/pair", {"code": "246810"})
        self.assertEqual((status, json.loads(raw.decode())["error"]), (429, "pairing_locked"))
        self.assertNotIn(b"screen_secret", raw)
        # 15 minutes later the installer's code still works
        agent.PAIR_LOCKED_UNTIL = time.monotonic() - 1
        status, raw = self.request("POST", "/api/screens/pair", {"code": "246810"})
        self.assertEqual(status, 200)
        self.assertIn("screen_secret", json.loads(raw.decode()))

    def test_panel_poll_cannot_save_over_a_fresh_secret(self):
        agent.put_wall_screen(self._row(code="246810"))
        agent.put_wall_screen(self._row(screen_id="scr_other00000000000"))  # another panel, polling
        real_load = agent.load_screens
        loaded = threading.Event()

        def slow_load():
            doc = real_load()
            if threading.current_thread() is poller:
                loaded.set()
                time.sleep(0.3)  # the poll has read screens.json and not written it back yet
            return doc

        with mock.patch.object(agent, "load_screens", slow_load), self.ha_mocks():
            poller = threading.Thread(target=agent.panel_snapshot, args=("scr_other00000000000",))
            poller.start()
            self.assertTrue(loaded.wait(2))
            out = agent.pair_wall_screen("246810")
            poller.join()
        row = agent.load_screens()["screens"]["scr_aaaaaaaaaaaaaaaa"]
        self.assertEqual(row.get("screen_secret_hash"), agent.hash_screen_secret(out["screen_secret"]))
        self.assertTrue(row.get("pairing_used_at"))

    def test_secret_never_reaches_a_log_line(self):
        lines = []

        def record(_handler, fmt, *args):
            lines.append(fmt % args)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(agent.H, "log_message", record), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err), self.ha_mocks(), mock.patch.object(
            agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")
        ), mock.patch.object(agent, "call_service", return_value={"ok": True}):
            secret = self._paired()
            self.ring()
            self.assertEqual(self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=secret)[0], 200)
            self.assertEqual(self.request("GET", "/api/screens/doorbell.jpg?id=scr_aaaaaaaaaaaaaaaa", secret=secret)[0], 200)
            self.assertEqual(self.unlock(secret)[0], 200)
            self.assertEqual(self.unlock("wrong-secret")[0], 403)
        self.assertGreaterEqual(len(lines), 5)  # access logging on: every request line went through log_message
        logged = "\n".join(lines) + out.getvalue() + err.getvalue()
        self.assertNotIn(secret, logged)
        self.assertNotIn(agent.hash_screen_secret(secret), logged)

    def test_panel_sends_the_secret_as_a_header_only(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('headers["X-Arvio-Screen-Secret"] = secret', html)
        self.assertIn("localStorage.setItem(SECRET_KEY, body.screen_secret)", html)
        # every /api/screens* request goes through screenFetch: no bare fetch, no <img src>, no CSS url()
        self.assertEqual(re.findall(r'(?<![A-Za-z])fetch\(\s*"/api/screens', html), [])
        self.assertNotIn("url('/api/screens", html)
        self.assertNotRegex(html, r'\.src\s*=\s*"/api/screens')
        self.assertEqual(len(re.findall(r'screen(?:Fetch|BlobUrl)\(\s*"/api/screens', html)), 5)
        self.assertNotRegex(html, r'[?&]secret=')
        # the hub's refusals become calm Greek lines, not silence
        self.assertIn("repair_required:", html)
        self.assertIn("no_recent_ring:", html)
        self.assertIn("r.status === 429", html)
        self.assertIn("found.pairing_pending", html)

if __name__ == "__main__":
    unittest.main()
