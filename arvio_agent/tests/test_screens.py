"""Wall screens: hub-local store, LAN list/pair only, relay put/delete, /panel without webfonts."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import ipaddress
import json
import pathlib
import plistlib
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import ROOT, agent, signed


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

    def test_accepts_afisa(self):
        # Partner's first and default look (domain THEME_PRESETS); panel.html draws it.
        out = agent.put_wall_screen(self._row(theme={"preset": "afisa", "mode": "light", "icons": "line"}))
        self.assertEqual(out["screen"]["theme"]["preset"], "afisa")

    def test_theme_without_preset_is_afisa(self):
        # As the domain (DEFAULT_SCREEN_THEME) and panel.html: Αφίσα is the factory look.
        out = agent.put_wall_screen(self._row(theme={"mode": "light"}))
        self.assertEqual(out["screen"]["theme"], {"preset": "afisa", "mode": "light", "icons": "line"})

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

    def test_dashboard_page_summary_and_lykofos(self):
        """«Πίνακας» page, «Σύνοψη» tile and the Λυκόφως theme (2026-11-13), and Partner learns that this hub draws them."""
        page = {"id": "pg_aaaaaaaa", "layout": "dashboard", "tiles": [{"kind": "summary", "size": "m"}, {"kind": "clock"}]}
        out = agent.put_wall_screen(self._row(pages=[page], theme={"preset": "lykofos", "mode": "dark"}))["screen"]
        self.assertEqual(out["pages"][0]["layout"], "dashboard")
        self.assertEqual(out["pages"][0]["tiles"][0]["kind"], "summary")
        self.assertEqual(out["theme"]["preset"], "lykofos")
        # a plain page stays plain, an unknown layout or a summary with a ref is refused
        plain = agent.put_wall_screen(self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}]))["screen"]
        self.assertNotIn("layout", plain["pages"][0])
        for bad in ({"layout": "mosaic"}, {"tiles": [{"kind": "summary", "ref": "light.x"}]}):
            with self.assertRaises(ValueError):
                agent.put_wall_screen(self._row(pages=[{**page, **bad}]))
        can = agent.list_wall_screens()["panel"]
        self.assertIn("lykofos", can["themes"])
        self.assertIn("summary", can["tiles"])
        self.assertEqual(can["layouts"], ["dashboard"])

    def test_omichli_and_the_fixed_bar(self):
        """Ομίχλη and «Σταθερή μπάρα» (2026-11-13): only a true bar is kept, and Partner learns that this hub draws it."""
        page = {"id": "pg_aaaaaaaa", "tiles": [{"kind": "clock"}]}
        out = agent.put_wall_screen(self._row(pages=[page], theme={"preset": "omichli", "mode": "dark", "bar": True}))["screen"]
        self.assertEqual(out["theme"], {"preset": "omichli", "mode": "dark", "icons": "line", "bar": True})
        for bar in (False, "yes", 1, None):
            out = agent.put_wall_screen(self._row(pages=[page], theme={"preset": "afisa", "bar": bar}))["screen"]
            self.assertNotIn("bar", out["theme"])
        can = agent.list_wall_screens()["panel"]
        self.assertIn("omichli", can["themes"])
        self.assertIs(can["bar"], True)

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
        # About the allowlist; doors have their own tests (test_lan_doors, test_door_marks).
        no_door_rule = mock.patch.object(agent, "lan_door_refusal", return_value=None)
        no_door_rule.start()
        self.addCleanup(no_door_rule.stop)
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
        # About the allowlist; doors have their own tests (test_lan_doors, test_door_marks).
        no_door_rule = mock.patch.object(agent, "lan_door_refusal", return_value=None)
        no_door_rule.start()
        self.addCleanup(no_door_rule.stop)
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
            headers.update(signed(method, path, data or b"", secret=secret))
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

    def test_panel_snapshot_includes_what_a_speaker_plays(self):
        """SFK HQ 2026-11-09: the speaker sheet showed only «Παύση», because the snapshot carried no media fields."""
        group = {  # the hub's own «Ηχεία SFK», as Core reports it (arvio.model, 2026-11-09 08:40 UTC)
            "entity_id": "media_player.ekheia_sfk",
            "state": "playing",
            "attributes": {
                "friendly_name": "Ηχεία SFK",
                "volume_level": 0.21,
                "media_title": "Rock FM 96.9",
                "media_content_id": "https://az10.yesstreaming.net:8060/radio.mp3",
                "media_content_type": "music",
                "group_members": ["media_player.reception", "media_player.garage_speaker"],
            },
        }
        tv = {"entity_id": "media_player.tv_saloni_75", "state": "on", "attributes": {"friendly_name": "TV", "device_class": "tv"}}
        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(
            agent, "registries_for_commands", return_value={"entity_regs": {}, "devices": {}, "areas": {}}
        ), mock.patch.object(agent, "ha", return_value=[group, tv]):
            status, raw = self.panel_get()
        self.assertEqual(status, 200)
        rows = {r["entity_id"]: r for r in json.loads(raw.decode())["entities"]}
        self.assertEqual(rows["media_player.tv_saloni_75"]["device_class"], "tv")  # the sheet offers no TV remote
        row = rows["media_player.ekheia_sfk"]
        self.assertEqual(row["volume_level"], 0.21)
        self.assertEqual(row["media_title"], "Rock FM 96.9")
        self.assertEqual(row["media_content_id"], "https://az10.yesstreaming.net:8060/radio.mp3")
        self.assertEqual(row["media_content_type"], "music")
        self.assertIsNone(row["device_class"])

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
            {
                "entity_id": "scene.vradi",
                "state": "unknown",
                "attributes": {"friendly_name": "Βράδυ", "scene_look": [{"on": True, "pct": 50}]},
            },
            {"entity_id": "scene.hue", "state": "unknown", "attributes": {"friendly_name": "Hue"}},
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
        self.assertEqual(by_id["scene.vradi"]["name"], "Βράδυ")
        self.assertEqual(by_id["scene.vradi"]["scene_look"], [{"on": True, "pct": 50}])
        self.assertNotIn("scene_look", by_id["scene.hue"])

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
        self.assertIn("Δεν υπάρχει ακόμα πρόγνωση για αυτό το σπίτι.", html)

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

    def test_panel_afisa_is_the_factory_look(self):
        # Αφίσα (11/2026): the platform look; a screen with no theme (or an unknown one) gets it,
        # in the same palette Partner previews, with square corners only under that preset.
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('const preset = THEMES[t.preset] ? t.preset : "afisa";', html)
        self.assertIn('bg: "#141414", tile: "#1c1b1a", ink: "#eeebe5"', html)
        self.assertIn("--r: 4px;", html[html.index(":root {") : html.index("* { box-sizing")])
        self.assertNotIn("border-radius: 28px;", html)
        # before any theme arrives (pairing) the panel is already Αφίσα dark, logo inline (offline)
        self.assertIn('<html lang="el" data-preset="afisa" data-mode="dark">', html)
        self.assertIn('class="gate-card"', html)
        self.assertRegex(html, r'class="gate-mark" alt="Arvio" src="data:image/png;base64,[A-Za-z0-9+/=]{2000,}"')

    def test_panel_themes_are_each_their_own(self):
        """Eleven identities (Λυκόφως, 2026-11-13): each has its own CSS block, and no two share the same colours."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        table = html[html.index("const THEMES = {") : html.index("function hexRgb")]
        names = re.findall(r"^        (\w+): \{$", table, flags=re.M)
        self.assertEqual(sorted(names), sorted(agent.SCREEN_THEME_PRESETS))
        for name in names:
            if name != "afisa":
                self.assertIn(f'html[data-preset="{name}"] {{', html)
        palettes = re.findall(r'bg: "(#\w+)", tile: "(#\w+)", ink: "(#\w+)"', table)
        self.assertEqual(len(palettes), 2 * len(names))
        self.assertEqual(len(set(palettes)), 2 * len(names))

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

    def test_panel_pairing_survives_a_new_hub_address(self):
        # localStorage is per origin (http://<hub-ip>:8099): with the app's keep()/kept() the pairing
        # lives in the app, and only the one-time move from an older page still reads localStorage.
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        start, end = html.index("const pairing = {"), html.index("let lastGood")
        self.assertIn("shell.keep(k", html[start:end])
        self.assertIn("shell.kept(k)", html[start:end])
        rest = html[:start] + html[end:]
        self.assertNotRegex(rest, r"localStorage\.\w+\((KEY|SECRET_KEY)\b")

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

    # --- the doorbell at once: «όταν χτυπάν το κουδούνι δεν ανοίγει αυτόματα… πρέπει να είναι ακαριαίο» (08/11) ---

    def test_ring_wait_answers_the_moment_it_rings(self):
        secret = self._paired()
        path = "/api/screens/ring?id=scr_aaaaaaaaaaaaaaaa"
        self.assertEqual(self.request("GET", path)[0], 403)
        self.assertEqual(self.request("GET", "/api/screens/ring?id=scr_nope", secret=secret)[0], 404)
        threading.Timer(0.3, self.ring).start()
        started = time.monotonic()
        status, raw = self.request("GET", path, secret=secret)
        self.assertEqual((status, json.loads(raw)["ringing"]), (200, True))
        self.assertLess(time.monotonic() - started, 1.5)
        # while it rings, at once; nothing rings: after the wait, false
        self.assertTrue(agent.doorbell_ring_wait("scr_aaaaaaaaaaaaaaaa", secret, timeout=0.05))
        agent.DOORBELL_LATCH.clear()
        self.assertFalse(agent.doorbell_ring_wait("scr_aaaaaaaaaaaaaaaa", secret, timeout=0.05))

    def test_panel_waits_for_the_ring_instead_of_its_poll(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        wait = html[html.index("async function waitForRing()") : html.index("async function load(keepPage)")]
        self.assertIn('screenFetch("/api/screens/ring?id="', wait)
        self.assertIn("while (screen && screen.doorbell && !doorbellWasRinging && pairing.get(KEY))", wait)
        self.assertIn("syncDoorbell();", wait)
        self.assertIn("setTimeout(done, 5000)", wait)  # an error never spins
        self.assertIn("        syncDoorbell();\n        waitForRing();\n", html)

    # --- camera tiles (docs/plans/2026-11-08-panel-camera-tiles.md) ---

    CAM_TILES = [{"id": "pg_aaaaaaaa", "tiles": [{"kind": "camera", "ref": "camera.kipos", "size": "l"}, {"kind": "camera_button", "ref": "camera.g4"}]}]
    SID = "scr_aaaaaaaaaaaaaaaa"

    def test_camera_tiles_need_a_camera(self):
        agent.put_wall_screen(self._row(pages=self.CAM_TILES))
        tiles = agent.list_wall_screens()["screens"][0]["pages"][0]["tiles"]
        self.assertEqual(tiles, [{"kind": "camera", "ref": "camera.kipos", "size": "l", "span": 2}, {"kind": "camera_button", "ref": "camera.g4"}])
        for ref in (None, "", "light.kipos", "camera.Kipos", "camera.a/b"):
            with self.assertRaises(ValueError, msg=ref):
                agent.put_wall_screen(self._row(pages=[{"id": "pg_aaaaaaaa", "tiles": [{"kind": "camera_button", "ref": ref}]}]))

    def test_camera_picture_only_for_this_panels_own_tiles(self):
        secret = self._paired(pages=self.CAM_TILES)
        with mock.patch.object(agent, "fetch_camera_proxy", return_value=(JPEG, "image/jpeg")) as cam:
            status, raw = self.request("GET", f"/api/screens/camera.jpg?id={self.SID}&e=camera.kipos&w=640", secret=secret)
            self.assertEqual((status, raw), (200, JPEG))
            cam.assert_called_once_with("camera.kipos", 640)
            # the doorbell's camera is not a tile here, and no camera is without its secret
            self.assertEqual(self.request("GET", f"/api/screens/camera.jpg?id={self.SID}&e=camera.8b014b9pajf9590_main", secret=secret)[0], 404)
            status, raw = self.request("GET", f"/api/screens/camera.jpg?id={self.SID}&e=camera.kipos")
            self.assertEqual((status, json.loads(raw)["error"]), (403, "bad_screen_secret"))
            self.assertEqual(cam.call_count, 1)
        agent.put_wall_screen(self._row(pages=self.CAM_TILES, screen_id=self.PANEL_ID))  # paired before 0.1.50
        status, raw = self.request("GET", f"/api/screens/camera.jpg?id={self.PANEL_ID}&e=camera.kipos")
        self.assertEqual((status, json.loads(raw)["error"]), (403, "repair_required"))

    def test_camera_live_ticket_opens_that_stream_only(self):
        secret = self._paired(pages=self.CAM_TILES)
        stream = {"ok": True, "entity_id": "camera.kipos", "playlist_path": "/api/hls/playlist.m3u8?id=abc123"}
        files = []

        def core(method, path):
            files.append(path)
            return 200, {"content-type": "application/vnd.apple.mpegurl"}, [], b"#EXTM3U\n"

        body = {"screen_id": self.SID, "entity_id": "camera.kipos"}
        with mock.patch.object(agent, "ha", return_value=[]), mock.patch.object(agent, "camera_stream", return_value=stream), \
             mock.patch.object(agent, "camera_http_via_supervisor", side_effect=core):
            self.assertEqual(self.request("POST", "/api/screens/camera/live", body)[0], 403)
            status, raw = self.request("POST", "/api/screens/camera/live", body, secret=secret)
            self.assertEqual(status, 200, raw)
            url = json.loads(raw)["url"]
            self.assertRegex(url, r"^/api/live/[A-Za-z0-9_-]{32}/playlist\.m3u8\?id=abc123$")
            ticket = url.split("/")[3]
            self.assertEqual(self.request("GET", url), (200, b"#EXTM3U\n"))
            # what the playlist names, relative to it
            self.assertEqual(self.request("GET", f"/api/live/{ticket}/segment.ts?id=abc123&n=4")[0], 200)
            self.assertEqual(files, ["/api/hls/playlist.m3u8?id=abc123", "/api/hls/segment.ts?id=abc123&n=4"])
            for bad in (
                f"/api/live/{ticket}/segment.ts?id=other&n=4",  # another camera's stream
                f"/api/live/{ticket}/segment.ts?n=4",
                f"/api/live/{ticket}/../states",
                "/api/live/nope/playlist.m3u8?id=abc123",
            ):
                self.assertEqual(self.request("GET", bad)[0], 404, bad)
            agent.LIVE_TICKETS[ticket]["until"] = 0
            self.assertEqual(self.request("GET", url)[0], 404)
            self.assertEqual(len(files), 2)
            # a camera that is not one of this panel's tiles
            status, _ = self.request("POST", "/api/screens/camera/live", {"screen_id": self.SID, "entity_id": "camera.other"}, secret=secret)
            self.assertEqual(status, 404)

    def test_a_door_camera_never_streams_from_the_panel(self):
        secret = self._paired(pages=self.CAM_TILES)
        call = [{"entity_id": "binary_sensor.g4_doorbell", "state": "off", "attributes": {}}]
        with mock.patch.object(agent, "ha", return_value=call), mock.patch.object(agent, "camera_stream") as stream:
            status, raw = self.request("POST", "/api/screens/camera/live", {"screen_id": self.SID, "entity_id": "camera.g4"}, secret=secret)
        self.assertEqual((status, json.loads(raw)["error"]), (409, "door_camera"))
        stream.assert_not_called()

    def test_hub_serves_the_signing_code_for_both_pages(self):
        status, raw = self.request("GET", "/panel/lan-sign.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function lanSignature(", raw)
        self.assertEqual(raw, (ROOT / "lan-sign.js").read_bytes())

    def test_hub_serves_hls_js_for_the_panel(self):
        status, raw = self.request("GET", "/panel/hls.js")
        self.assertEqual(status, 200)
        self.assertIn(b".Hls=", raw[-400:])
        self.assertTrue((ROOT / "panel-app" / "hls.LICENSE").read_text().find("Apache License") > 0)

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

    def test_install_token_pairs_once_and_closes_the_code(self):
        agent.put_wall_screen(self._row(code="246810"))
        token = agent.mint_screen_pair_token("scr_aaaaaaaaaaaaaaaa")
        self.assertEqual(len(token), 43)
        self.assertNotIn(token, agent.SCREENS.read_text(encoding="utf-8"))
        # a Partner layout save during the install keeps the token
        agent.put_wall_screen(self._row(code="246810", name="Είσοδος"))
        status, raw = self.request("POST", "/api/screens/pair", {"token": token})
        self.assertEqual(status, 200, raw)
        body = json.loads(raw.decode())
        self.assertEqual(body["screen_id"], "scr_aaaaaaaaaaaaaaaa")
        row = agent.load_screens()["screens"]["scr_aaaaaaaaaaaaaaaa"]
        self.assertEqual(row["screen_secret_hash"], agent.hash_screen_secret(body["screen_secret"]))
        self.assertNotIn("pair_token_hash", row)
        # single use, and the installer's outstanding 6-digit code is spent with it
        self.assertEqual(self.request("POST", "/api/screens/pair", {"token": token})[0], 400)
        self.assertEqual(self.request("POST", "/api/screens/pair", {"code": "246810"})[0], 400)
        self.assertNotIn("pair_token", json.dumps(agent.list_wall_screens()))

    def test_install_token_expires_after_ten_minutes(self):
        agent.put_wall_screen(self._row())
        token = agent.mint_screen_pair_token("scr_aaaaaaaaaaaaaaaa")
        doc = agent.load_screens()
        doc["screens"]["scr_aaaaaaaaaaaaaaaa"]["pair_token_expires_at"] = "2020-01-01T00:00:00.000Z"
        agent.save_screens(doc)
        status, raw = self.request("POST", "/api/screens/pair", {"token": token})
        self.assertEqual((status, json.loads(raw.decode())["error"]), (400, "pairing_mismatch"))
        with self.assertRaises(ValueError):
            agent.mint_screen_pair_token("scr_missing000000000")

    def test_wrong_tokens_share_the_lockout_and_junk_is_refused(self):
        agent.put_wall_screen(self._row())
        token = agent.mint_screen_pair_token("scr_aaaaaaaaaaaaaaaa")
        status, raw = self.request("POST", "/api/screens/pair", {"token": token[:-1]})
        self.assertEqual((status, json.loads(raw.decode())["error"]), (400, "invalid_code"))
        wrong = ("A" if token[0] != "A" else "B") + token[1:]  # never the real one
        for i in range(10):
            self.assertEqual(self.request("POST", "/api/screens/pair", {"token": wrong})[0], 400, i)
        status, raw = self.request("POST", "/api/screens/pair", {"token": token})
        self.assertEqual((status, json.loads(raw.decode())["error"]), (429, "pairing_locked"))

    def test_panel_app_is_served_from_the_agent_image(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(agent, "APP", pathlib.Path(d)):
            self.assertEqual(self.request("GET", "/panel/app.json")[0], 404)
            self.assertEqual(self.request("GET", "/panel/app.apk")[0], 404)
            (pathlib.Path(d) / "panel-app").mkdir()
            meta = {"version_code": 1, "version_name": "0.1.0", "sha256": "00" * 32, "size": 6}
            (pathlib.Path(d) / "panel-app" / "app.json").write_text(json.dumps(meta), encoding="utf-8")
            (pathlib.Path(d) / "panel-app" / "arvio-panel.apk").write_bytes(b"PK\x03\x04ok")
            status, raw = self.request("GET", "/panel/app.json")
            self.assertEqual((status, json.loads(raw.decode())), (200, meta))
            self.assertEqual(self.request("GET", "/panel/app.apk"), (200, b"PK\x03\x04ok"))

    def test_panel_pairs_from_the_install_fragment_and_drops_it(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("location.hash.match(/^#pair=([A-Za-z0-9_-]{43})$/)", html)
        self.assertIn('history.replaceState(null, "", location.pathname + location.search)', html)
        self.assertIn("await pairWith({ token: pairToken }", html)
        # the app's hang check: a heartbeat every 30 s, harmless in a browser
        self.assertIn("const beat = () => window.ArvioPanel && window.ArvioPanel.alive(!!pairing.get(KEY));", html)
        self.assertIn("setInterval(beat, 30000);", html)

    def raw(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type"), e.read()

    def test_ipad_pairs_from_a_profile_the_hub_serves_once(self):
        agent.put_wall_screen(self._row(name="Σαλόνι"))
        with mock.patch.object(agent, "panel_lan", return_value=ipaddress.ip_interface("192.168.68.77/24")):
            out = agent.execute_action("arvio.panel_pair_link", "", {"screen_id": "scr_aaaaaaaaaaaaaaaa"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["expires_in"], agent.PAIR_TOKEN_TTL_S)
        m = re.fullmatch(r"http://192\.168\.68\.77:8099/panel/ipad\.mobileconfig\?t=([A-Za-z0-9_-]{43})", out["url"])
        self.assertTrue(m, out["url"])
        token = m.group(1)
        status, ctype, body = self.raw(f"/panel/ipad.mobileconfig?t={token}")
        self.assertEqual((status, ctype), (200, "application/x-apple-aspen-config"))
        profile = plistlib.loads(body)
        clip = profile["PayloadContent"][0]
        self.assertEqual(clip["PayloadType"], "com.apple.webClip.managed")
        self.assertEqual(clip["Label"], "Σαλόνι")
        self.assertEqual(clip["URL"], f"http://127.0.0.1:{self.port}/panel#pair={token}")
        self.assertTrue(clip["FullScreen"])
        self.assertEqual(clip["Icon"][:8], b"\x89PNG\r\n\x1a\n")
        # the Web Clip pairs once; after that the link is dead
        self.assertEqual(self.request("POST", "/api/screens/pair", {"token": token})[0], 200)
        self.assertEqual(self.raw(f"/panel/ipad.mobileconfig?t={token}")[0], 410)
        self.assertEqual(self.raw("/panel/ipad.mobileconfig?t=" + "A" * 43)[0], 410)
        self.assertEqual(self.raw("/panel/ipad.mobileconfig?t=junk")[0], 410)
        status, ctype, body = self.raw("/panel/icon.png")
        self.assertEqual((status, ctype, body[:4]), (200, "image/png", b"\x89PNG"))
        self.assertNotIn("arvio.panel_pair_link", agent.LAN_ALLOWED_ACTIONS)
        self.assertIn("arvio.panel_pair_link", agent.AGENT_SERVICE_ALLOWLIST)

    def test_panel_screenshot_is_asked_once_and_kept_only_in_memory(self):
        """Partner's «Στιγμιότυπο»: the panel answers once, signed, and only while an order waits."""
        sid = "scr_aaaaaaaaaaaaaaaa"
        ask = lambda s=sid: agent.execute_action("arvio.panel_screenshot", "", {"screen_id": s})
        get = lambda: agent.execute_action("arvio.panel_screenshot_get", "", {"screen_id": sid})
        def read():
            status, raw = self.request("GET", f"/api/screens?screen_id={sid}", secret=secret)
            self.assertEqual(status, 200)
            return json.loads(raw)["screens"][0]
        def put(body, key=None):
            status, raw = self.request("POST", "/api/screens/screenshot", body={"screen_id": sid, **body}, secret=key)
            return status, json.loads(raw)
        jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 64).decode()

        agent.PANEL_SHOTS.clear()
        self.assertEqual(ask("scr_nope")["error"], "screen_not_found")
        agent.put_wall_screen(self._row())
        self.assertEqual(ask()["error"], "screen_not_paired")
        secret = self._paired()
        self.assertNotIn("screenshot", read())
        self.assertIsNone(get()["data_base64"])
        # nothing goes up unasked
        self.assertEqual(put({"image": jpeg}, secret), (403, {"ok": False, "error": "no_screenshot_asked"}))
        self.assertTrue(ask()["ok"])
        agent.put_wall_screen(self._row())  # a layout pushed in between keeps the order
        self.assertNotIn("screenshot", agent.list_wall_screens()["screens"][0])
        self.assertIs(read()["screenshot"], True)
        self.assertIs(read()["screenshot"], True)  # until the picture comes
        # only the paired panel, and only a JPEG
        self.assertEqual(put({"image": jpeg})[0], 403)
        self.assertEqual(put({"image": jpeg}, "someone-else")[0], 403)
        self.assertEqual(put({"image": "data:image/jpeg;base64,iVBORw0KGgo="}, secret), (400, {"ok": False, "error": "invalid_screenshot"}))
        self.assertEqual(put({"refused": "whatever"}, secret)[0], 400)
        self.assertEqual(put({"image": jpeg, "source": "page"}, secret), (200, {"ok": True}))
        out = get()
        self.assertEqual(out["source"], "page")
        self.assertEqual(base64.b64decode(out["data_base64"])[:3], b"\xff\xd8\xff")
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertIsNone(out["refused"])
        self.assertNotIn("screenshot", read())  # answered: the order is gone
        self.assertEqual(put({"image": jpeg}, secret)[0], 403)
        # the doorbell's camera on screen: a refusal, and no picture
        self.assertTrue(ask()["ok"])
        self.assertEqual(put({"refused": "private"}, secret)[0], 200)
        self.assertEqual((get()["refused"], get()["data_base64"]), ("private", None))
        # never on the disk, and gone after a while
        self.assertNotIn("data_base64", json.dumps(agent.load_screens()))
        with mock.patch.object(agent.time, "time", return_value=agent.time.time() + agent.PANEL_SHOT_KEEP_S + 1):
            self.assertIsNone(get()["taken_at"])
        # an order the panel never took is void after its window
        self.assertTrue(ask()["ok"])
        with mock.patch.object(agent.time, "time", return_value=agent.time.time() + agent.PANEL_SHOT_TTL_S + 1):
            self.assertNotIn("screenshot", read())
        self.assertNotIn("arvio.panel_screenshot", agent.LAN_ALLOWED_ACTIONS)
        self.assertIn("arvio.panel_screenshot", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.panel_screenshot_get", agent.AGENT_SERVICE_ALLOWLIST)
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("if (found.screenshot) takeScreenshot(sid);", html)
        self.assertIn('body = { screen_id: sid, refused: "private" };', html)
        # an app without the picture (0.1.1, whose key may be gone) still answers: the page draws itself
        self.assertIn(': { screen_id: sid, image: await pageShot(), source: "page" };', html)

    def test_panel_restart_is_one_order_taken_once(self):
        """Partner's «Επανεκκίνηση»: never a boot loop, never a surprise for a panel back later."""
        restart = lambda sid="scr_aaaaaaaaaaaaaaaa": agent.execute_action("arvio.panel_restart", "", {"screen_id": sid})
        def read():
            status, raw = self.request("GET", "/api/screens?screen_id=scr_aaaaaaaaaaaaaaaa", secret=secret)
            self.assertEqual(status, 200)
            return json.loads(raw)["screens"][0]

        self.assertEqual(restart("scr_nope")["error"], "screen_not_found")
        agent.put_wall_screen(self._row())
        self.assertEqual(restart()["error"], "screen_not_paired")
        secret = self._paired()
        self.assertNotIn("restart", read())
        self.assertTrue(restart()["ok"])
        # a layout pushed in between keeps the order, and only the panel ever sees it
        agent.put_wall_screen(self._row())
        self.assertNotIn("restart", agent.list_wall_screens()["screens"][0])
        self.assertIs(read()["restart"], True)
        self.assertNotIn("restart", read())  # taken once: after the reboot it is gone
        # too late: a panel that comes back after the window does not restart
        self.assertTrue(restart()["ok"])
        with mock.patch.object(agent.time, "time", return_value=agent.time.time() + agent.PANEL_RESTART_TTL_S + 1):
            self.assertNotIn("restart", read())
        self.assertNotIn("arvio.panel_restart", agent.LAN_ALLOWED_ACTIONS)
        self.assertIn("arvio.panel_restart", agent.AGENT_SERVICE_ALLOWLIST)
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("if (window.ArvioPanel && window.ArvioPanel.reboot) window.ArvioPanel.reboot();", html)

    def test_panel_fonts_come_from_the_hub(self):
        """Αφίσα without internet: every font the page names is served, and only those files."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        urls = re.findall(r'url\("(/panel/fonts/[^"]+)"\)', html)
        self.assertEqual(set(urls), agent.PANEL_FONTS)
        for url in urls:
            status, ctype, body = self.raw(url)
            self.assertEqual((status, ctype, body[:4]), (200, "font/woff2", b"wOF2"))
        self.assertEqual(self.raw("/panel/fonts/../agent.py")[0], 404)

    def test_panel_preview_never_talks_to_a_hub(self):
        """Partner's editor shows this page: in preview it stops before pairing, /health and load()."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        boot = html[html.index("if (PREVIEW) {\n          gate.classList"):]
        self.assertLess(boot.index("return;"), boot.index('fetch("/health")'))
        self.assertIn("ev.origin === location.origin", boot)

    def test_panel_works_on_old_ipads(self):
        """iPad wall mode without the app (Safari 12-16): what that Safari cannot read has a fallback."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", html[html.index("<style>") : html.index("</style>")], flags=re.S)
        # No color-mix(): with a var() inside it voids the declaration only when applied, so a line
        # before it is no fallback. On the NSPanel Pro (WebView 107) lit lamps came out as empty rings.
        self.assertNotIn("color-mix(", css)
        # every clamp() (Safari 13.4) comes right after a plain declaration
        decls = re.findall(r"([a-z-]+):\s*([^;{}]+);", css)
        modern = lambda v: "clamp(" in v
        for i, (prop, value) in enumerate(decls):
            if modern(value):
                j = i - 1  # a chain (28vw -> clamp(vw) -> clamp(cqh)) must start with a plain value
                while j >= 0 and decls[j][0] == prop and modern(decls[j][1]):
                    j -= 1
                self.assertTrue(j >= 0 and decls[j][0] == prop and not modern(decls[j][1]), f"{prop}: {value}")
        self.assertNotRegex(css, r"(^|[\s{;])inset:")  # Safari 14.1: top/right/bottom/left
        self.assertNotIn(":is(", css)  # Safari 14
        self.assertIn("@supports not (aspect-ratio: 1)", css)
        self.assertIn("no-flex-gap", css)
        self.assertIn('document.documentElement.classList.add("no-flex-gap")', html)
        self.assertIn("window.AudioContext || window.webkitAudioContext", html)
        self.assertEqual(html.count('hourCycle: "h23",'), html.count("hour12: false,"))
        self.assertIn('addEventListener("touchstart", armHomeIdle', html)
        for token in ("?.", "??", "Object.fromEntries", ".at(", "replaceAll", "structuredClone", "AbortController"):
            self.assertNotIn(token, html[html.index("<script>") :], token)
        # full screen from the home screen, a fresh start when stuck, a spent #pair= left alone
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes" />', html)
        self.assertIn('<link rel="apple-touch-icon" href="/panel/icon.png" />', html)
        self.assertIn("navigator.standalone && pairing.get(KEY)", html)
        self.assertIn('const r = await fetch("/health", { cache: "no-store" });', html)
        # an agent update reaches the screen: the app itself loads the page only once
        self.assertIn("(v && v !== pageVersion)) location.reload();", html)

    def test_panel_is_never_a_silent_dark_screen(self):
        """Paired but not answered yet, the hub away, or a screen with no tiles: the page says which."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('<section id="status" class="notice hidden" role="status"', html)
        self.assertIn('showStatus("Σύνδεση", "Το πάνελ μιλά με το hub…");', html)
        self.assertIn('showStatus("Ψάχνω το hub",', html)
        self.assertIn('showStatus("Το hub δεν απαντά",', html)
        self.assertIn("Δεν έχει πλακίδια ακόμα.", html)
        self.assertEqual(html.count("hideStatus();"), 2)

    def test_a_panel_that_stops_painting_restarts_itself(self):
        """Κρέσνα 2026-11-12: the script ran, the WebView painted nothing, and the app's watchdog only hears the script."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        # the 04:00 fresh start holds in the app too
        self.assertIn("const nightly = new Date().getHours() === 4", html)
        watch = html[html.index("async function pageIsBlack()") : html.index("</script>", html.index("async function pageIsBlack()"))]
        self.assertIn("await pageShot()", watch)
        self.assertIn("if (paintMisses < 2) return;", watch)  # one black picture is not enough
        self.assertIn("6 * 3600000", watch)  # and never a restart loop
        self.assertIn("window.ArvioPanel.reboot()", watch)
        self.assertIn("return; // could not tell: not a miss", watch)
        self.assertIn("if (PREVIEW || !window.ArvioPanel", watch)  # never Partner's preview, never an iPad

    def test_panel_fits_its_big_numbers_on_an_old_safari(self):
        """iPad Air 2 on iPadOS 15 (no cqh): the clock overflowed its tile; the weather showed a lone «—»."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('const NO_CQ = !(window.CSS && CSS.supports && CSS.supports("height: 1cqh"));', html)
        self.assertIn("        fitFaces();\n      }", html)  # at the end of render()
        self.assertIn("Δεν υπάρχει ακόμα πρόγνωση.", html)
        self.assertNotIn("Home Assistant</p>", html)

    def test_panel_speaker_opens_its_sheet(self):
        """SFK HQ iPad 2026-11-09: a press on a speaker only paused it, and then nothing else could be done."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("const SHEET_DOMAINS = { climate: 1, cover: 1, vacuum: 1, media_player: 1 };", html)
        self.assertEqual(html.count("SHEET_DOMAINS["), 4)  # tile, room row, pin, second pin tap
        self.assertIn('} else if (sheet.kind === "media_player") {', html)
        self.assertIn('["play_media", "Παίξε ξανά"]', html)
        self.assertIn('cmd("media_player.volume_set", sheet.ref, { volume_level: level });', html)
        toggle = html[html.index("function toggleEntity(e)") : html.index("function mediaCommand(")]
        self.assertNotIn("media_player", toggle)
        # Another station or speaker from the sheet itself (SFK HQ, 2026-11-09): the hub's own browse tree.
        self.assertIn('action: "arvio.media_browse"', html)
        self.assertIn(f'const RADIO_NODE = "{agent.LAN_RADIO_NODE}";', html)  # the one node the LAN lets it list
        self.assertIn("data-media-speaker", html)
        self.assertIn('x.device_class !== "tv"', html)
        self.assertNotIn("Διάλεξε τι θα παίξει από την εφαρμογή Arvio Home.", html)

    def test_panel_never_sends_its_secret(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('"X-Arvio-Lan-Sig": signature', html)
        self.assertNotIn("X-Arvio-Screen-Secret", html)
        self.assertIn("pairing.set(SECRET_KEY, body.screen_secret)", html)
        # every /api/screens* request goes through screenFetch: no bare fetch, no <img src>, no CSS url()
        self.assertEqual(re.findall(r'(?<![A-Za-z])fetch\(\s*"/api/screens', html), [])
        self.assertNotIn("url('/api/screens", html)
        self.assertNotRegex(html, r'\.src\s*=\s*"/api/screens')
        self.assertEqual(len(re.findall(r'screen(?:Fetch|BlobUrl)\(\s*"/api/screens', html)), 9)  # + camera.jpg, camera/live, ring
        self.assertNotRegex(html, r'[?&]secret=')
        # the hub's refusals become calm Greek lines, not silence
        self.assertIn("repair_required:", html)
        self.assertIn("no_recent_ring:", html)
        self.assertIn("r.status === 429", html)
        self.assertIn("found.pairing_pending", html)

    def test_panel_camera_streams_only_while_someone_looks(self):
        """docs/plans/2026-11-08-panel-camera-tiles.md: stills on the wall, a stream only full screen, for 2 min."""
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        js = html[html.index("const CAM_TILE_MS") : html.index("async function load(keepPage)")]
        self.assertIn("const CAM_TILE_MS = 10000;", js)
        self.assertIn("const CAMVIEW_MS = 120000;", js)
        self.assertIn("v.closeAt = setTimeout(closeCamView, CAMVIEW_MS);", js)
        # a tile pulls a still, never a stream; nothing while the doorbell, full screen or a hidden page is up
        tiles = js[js.index("async function camTiles()") : js.index("function loadHls()")]
        self.assertNotIn("camera/live", tiles)
        self.assertIn("camView || !screen || document.hidden || !doorbellEl.classList.contains(\"hidden\")", tiles)
        # the stream ends with the view: on «Κλείσιμο», a hidden page, the doorbell, a lost pairing
        self.assertIn('getElementById("camview-close").addEventListener("click", closeCamView)', js)
        self.assertIn("if (document.hidden) closeCamView();", js)
        show = html[html.index("function showDoorbell()") : html.index("function syncDoorbell()")]
        self.assertIn("closeCamView();", show)
        close = js[js.index("function closeCamView()") :]
        self.assertIn("camStop(v);", close)
        self.assertIn("v.hls.destroy();", js[js.index("function camStop(v)") :])
        # hls.js first, Safari's own where it cannot run; 5 new streams a minute, then stills
        self.assertIn('s.src = "/panel/hls.js";', js)
        self.assertIn('camVideo.canPlayType("application/vnd.apple.mpegurl")', js)
        self.assertIn("if (v.restarts.length >= 5) return camStillsOnly(v, \"no_hls\");", js)
        self.assertNotIn("recoverMediaError", js)
        # a camera on screen is private to «Στιγμιότυπο»
        self.assertIn("|| sheet || camView || cameraOnPage()) body = { screen_id: sid, refused: \"private\" };", html)

if __name__ == "__main__":
    unittest.main()
