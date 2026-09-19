"""Venue session store: put_venue hashes, LAN PIN, wall without PII."""
from __future__ import annotations

import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import ROOT, agent

SITE = "site_gym1"
HUB = "hub_gym1"
BK = "bk_cccccccccccccccc"
PIN = "424242"
NOW = datetime(2026, 9, 18, 16, 25, tzinfo=timezone.utc).timestamp()


def pin_hash(pin: str = PIN, site_id: str = SITE, booking_id: str = BK) -> str:
    return hashlib.sha256(f"arvio.venue.pin|{site_id}|{booking_id}|{pin}".encode("utf-8")).hexdigest()


def snapshot(**patch):
    zone = {
        "zone_id": "zn_aaaaaaaaaaaaaaaa",
        "name": "Γυμναστήριο",
        "entry_lock_entity_ids": ["lock.front"],
        "egress_lock_entity_ids": ["lock.front"],
        "occupancy": {"kind": "sensor", "entity_id": "sensor.gym_people"},
        "policy": {"zone_capacity": 8, "on_zone_exceed": "deny_entry", "on_party_exceed": "wall_warn"},
        "prep_lead_min": 10,
        "recipe": {
            "on_start": {
                "scene_id": "scene.gym_on",
                "media": {
                    "player_entity_ids": ["media_player.gym"],
                    "media_id": "library://playlist/1",
                    "media_type": "playlist",
                    "volume": 0.4,
                },
            },
            "on_end": {"media": "stop"},
        },
    }
    row = {
        "site_id": SITE,
        "hub_id": HUB,
        "time_zone": "Europe/Athens",
        "generated_at": "2026-09-18T15:00:00.000Z",
        "zone": zone,
        "bookings": [
            {
                "booking_id": BK,
                "zone_id": "zn_aaaaaaaaaaaaaaaa",
                "starts_at": "2026-09-18T16:30:00.000Z",
                "ends_at": "2026-09-18T17:30:00.000Z",
                "party_size": 2,
                "pin_hash": pin_hash(),
                "package_name": "60′ ιδιωτική",
                "window": {
                    "opens_at": "2026-09-18T16:20:00.000Z",
                    "closes_at": "2026-09-18T17:35:00.000Z",
                },
            }
        ],
    }
    if "zone" in patch:
        row["zone"] = {**zone, **patch.pop("zone")}
    row.update(patch)
    return row


class VenueStoreTest(unittest.TestCase):
    def setUp(self):
        if agent.VENUE.exists():
            agent.VENUE.unlink()

    def tearDown(self):
        if agent.VENUE.exists():
            agent.VENUE.unlink()

    def test_put_roundtrip_hashes_only(self):
        out = agent.put_venue(snapshot())
        self.assertTrue(out["ok"])
        self.assertEqual(out["bookings"], 1)
        dumped = agent.VENUE.read_text(encoding="utf-8")
        self.assertNotIn(PIN, dumped)
        self.assertIn(pin_hash(), dumped)
        self.assertNotIn("visitor", dumped)
        self.assertNotIn("@", dumped)

    def test_refuses_plaintext_pin(self):
        bad = snapshot()
        bad["bookings"][0]["pin_hash"] = PIN
        with self.assertRaises(ValueError) as cm:
            agent.put_venue(bad)
        self.assertIn("invalid_pin_hash", str(cm.exception))

    def test_lan_put_is_forbidden_relay_ok(self):
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.put_venue"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.delete_venue"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "arvio.venue_end_session"}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        agent.check_command_safety({"action": "arvio.put_venue"}, via="relay")
        self.assertNotIn("arvio.put_venue", agent.LAN_ALLOWED_ACTIONS)

    def test_pin_match_unlocks_entry_once(self):
        agent.put_venue(snapshot())
        calls = []

        def fake_service(domain, service, data=None):
            calls.append((domain, service, data))
            return {"ok": True}

        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            out = agent.venue_pin_unlock(PIN, now_ts=NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["booking_id"], BK)
        self.assertEqual(out["unlocked"], ["lock.front"])
        self.assertIn(("lock", "unlock", {"entity_id": "lock.front"}), calls)
        self.assertTrue(any(c[0] == "scene" for c in calls))
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            agent.venue_pin_unlock(PIN, now_ts=NOW)
        scene_starts = [c for c in calls if c[0] == "scene"]
        self.assertEqual(len(scene_starts), 1)

    def test_pin_mismatch_and_lockout(self):
        agent.put_venue(snapshot())
        with self.assertRaises(ValueError) as cm:
            agent.venue_pin_unlock("000000", now_ts=NOW)
        self.assertEqual(str(cm.exception), "pin_mismatch")
        for _ in range(4):
            with self.assertRaises(ValueError):
                agent.venue_pin_unlock("000000", now_ts=NOW)
        with self.assertRaises(ValueError) as cm:
            agent.venue_pin_unlock(PIN, now_ts=NOW)
        self.assertEqual(str(cm.exception), "pin_locked")

    def test_deny_entry_when_over_capacity(self):
        agent.put_venue(snapshot())

        def fake_ha(path, method="GET", body=None, timeout=10):
            if path.endswith("sensor.gym_people"):
                return {"state": "9"}
            return {"state": "unknown"}

        with mock.patch.object(agent, "ha", side_effect=fake_ha):
            with self.assertRaises(ValueError) as cm:
                agent.venue_pin_unlock(PIN, now_ts=NOW)
        self.assertEqual(str(cm.exception), "deny_entry")

    def test_wall_omits_pin_and_visitor(self):
        agent.put_venue(snapshot())
        wall = agent.venue_wall(now_ts=NOW)
        blob = json.dumps(wall)
        self.assertNotIn(PIN, blob)
        self.assertNotIn("pin_hash", blob)
        self.assertNotIn("visitor", blob)
        self.assertEqual(wall["phase"], "armed")
        self.assertEqual(wall["package_name"], "60′ ιδιωτική")
        self.assertTrue(wall["countdown"])

    def test_wall_shows_next_booking_when_idle(self):
        agent.put_venue(snapshot())
        before = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc).timestamp()
        wall = agent.venue_wall(now_ts=before)
        self.assertEqual(wall["phase"], "idle")
        self.assertEqual(wall["next_package_name"], "60′ ιδιωτική")
        self.assertTrue(wall["next_starts_at"])

    def test_tick_fires_on_start_at_start_time_without_pin(self):
        agent.put_venue(snapshot())
        calls = []

        def fake_service(domain, service, data=None):
            calls.append((domain, service, data))
            return {"ok": True}

        start = datetime(2026, 9, 18, 16, 30, 5, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            agent._venue_tick(now_ts=start)
        self.assertTrue(any(c[0] == "scene" for c in calls))
        # Second tick must not re-fire on_start.
        calls.clear()
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            agent._venue_tick(now_ts=start + 10)
        self.assertFalse(any(c[0] == "scene" for c in calls))

    def test_tick_auto_ends_past_grace(self):
        agent.put_venue(snapshot())
        calls = []

        def fake_service(domain, service, data=None):
            calls.append((domain, service, data))
            return {"ok": True}

        after_grace = datetime(2026, 9, 18, 17, 40, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            agent._venue_tick(now_ts=after_grace)
        self.assertIn(("media_player", "media_stop", {"entity_id": "media_player.gym"}), calls)
        # Idempotent: a second tick does not re-end.
        calls.clear()
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            agent._venue_tick(now_ts=after_grace + 30)
        self.assertFalse(any(c[0] == "media_player" for c in calls))

    def test_end_session_stops_media_never_locks_shared_door(self):
        agent.put_venue(snapshot())
        calls = []

        def fake_service(domain, service, data=None):
            calls.append((domain, service, data))
            return {"ok": True}

        active = datetime(2026, 9, 18, 16, 40, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(agent, "call_service", side_effect=fake_service):
            out = agent.venue_end_session({"booking_id": BK}, now_ts=active)
        self.assertTrue(out["ok"])
        self.assertIn(("media_player", "media_stop", {"entity_id": "media_player.gym"}), calls)
        self.assertFalse(any(c[0] == "lock" and c[1] == "lock" for c in calls))

    def test_end_session_refuses_when_no_active_booking(self):
        agent.put_venue(snapshot())
        before = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc).timestamp()
        with self.assertRaises(ValueError) as cm:
            agent.venue_end_session({"booking_id": BK}, now_ts=before)
        self.assertEqual(str(cm.exception), "not_active")
        after = datetime(2026, 9, 18, 19, 0, tzinfo=timezone.utc).timestamp()
        with self.assertRaises(ValueError) as cm:
            agent.venue_end_session({"booking_id": BK}, now_ts=after)
        self.assertEqual(str(cm.exception), "not_active")

    def test_execute_put_via_relay(self):
        out = agent.execute_action("arvio.put_venue", "", snapshot())
        self.assertTrue(out["ok"])
        self.assertEqual(out["site_id"], SITE)

    def test_session_tiles_parse_without_ref(self):
        row = {
            "screen_id": "scr_aaaaaaaaaaaaaaaa",
            "site_id": SITE,
            "hub_id": HUB,
            "name": "Τοίχος",
            "hardware": "generic_square",
            "orientation": "square",
            "pages": [
                {
                    "id": "pg_aaaaaaaa",
                    "tiles": [
                        {"kind": "session_countdown"},
                        {"kind": "occupancy"},
                        {"kind": "session_now"},
                    ],
                }
            ],
        }
        try:
            out = agent.put_wall_screen(row)
            kinds = [t["kind"] for t in out["screen"]["pages"][0]["tiles"]]
            self.assertEqual(kinds, ["session_countdown", "occupancy", "session_now"])
        finally:
            if agent.SCREENS.exists():
                agent.SCREENS.unlink()


class VenueLanHttpTest(unittest.TestCase):
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
        if agent.VENUE.exists():
            agent.VENUE.unlink()

    def tearDown(self):
        if agent.VENUE.exists():
            agent.VENUE.unlink()

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

    def test_wall_http_omits_hash(self):
        agent.put_venue(snapshot())
        with mock.patch.object(agent.time, "time", return_value=NOW):
            status, raw = self.request("GET", "/api/venue/wall")
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertNotIn("pin_hash", body)
        self.assertNotIn(PIN, json.dumps(body))
        self.assertEqual(body["ok"], True)

    def test_pin_http_mismatch(self):
        agent.put_venue(snapshot())
        status, raw = self.request("POST", "/api/venue/pin", {"pin": "000000"})
        self.assertEqual(status, 403)
        body = json.loads(raw.decode())
        self.assertEqual(body["error"], "pin_mismatch")

    def test_panel_fetches_wall(self):
        with mock.patch.object(agent, "APP", ROOT):
            status, raw = self.request("GET", "/panel")
        self.assertEqual(status, 200)
        html = raw.decode()
        self.assertIn("/api/venue/wall", html)
        self.assertIn("session_countdown", html)
        self.assertNotIn(PIN, html)
