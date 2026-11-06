"""The notification centre on the hub: what counts as an event, ring photos, devices that go away."""
import base64
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from helpers import agent


def state(entity_id, value, device_class=None, name="Κουζίνα", last_changed=None):
    attrs = {"friendly_name": name}
    if device_class:
        attrs["device_class"] = device_class
    return {"entity_id": entity_id, "state": value, "attributes": attrs, "last_changed": last_changed}


class HomeEventCodeTest(unittest.TestCase):
    def code(self, entity_id, new, old, device_class=None):
        return agent.home_event_code(entity_id, state(entity_id, new, device_class), state(entity_id, old, device_class))

    def test_danger_only_on_the_change_from_off(self):
        self.assertEqual(self.code("binary_sensor.a", "on", "off", "smoke"), "smoke")
        self.assertEqual(self.code("binary_sensor.a", "on", "off", "gas"), "gas")
        self.assertEqual(self.code("binary_sensor.a", "on", "off", "carbon_monoxide"), "co")
        self.assertEqual(self.code("binary_sensor.a", "on", "off", "moisture"), "leak")

    def test_a_restart_rings_nobody(self):
        self.assertIsNone(self.code("binary_sensor.a", "on", "unknown", "smoke"))
        self.assertIsNone(self.code("binary_sensor.a", "on", "unavailable", "smoke"))
        self.assertIsNone(self.code("binary_sensor.a", "off", "on", "smoke"))
        self.assertIsNone(agent.home_event_code("binary_sensor.a", state("binary_sensor.a", "on", "smoke"), None))

    def test_a_door_or_motion_is_not_an_event(self):
        self.assertIsNone(self.code("binary_sensor.a", "on", "off", "door"))
        self.assertIsNone(self.code("binary_sensor.a", "on", "off", "motion"))
        self.assertIsNone(self.code("light.a", "on", "off"))

    def test_a_battery_counts_once_on_the_way_down(self):
        self.assertEqual(self.code("sensor.a_battery", "20", "21", "battery"), "battery_low")
        self.assertEqual(self.code("binary_sensor.a_battery", "on", "off", "battery"), "battery_low")
        self.assertIsNone(self.code("sensor.a_battery", "19", "20", "battery"))
        self.assertIsNone(self.code("sensor.a_battery", "40", "41", "battery"))
        self.assertIsNone(self.code("sensor.a_battery", "unavailable", "21", "battery"))
        self.assertIsNone(self.code("sensor.temp", "10", "30", "temperature"))

    def test_arming_from_anywhere(self):
        panel = "alarm_control_panel.spiti"
        self.assertEqual(self.code(panel, "armed_away", "arming"), "armed_away")
        self.assertEqual(self.code(panel, "armed_away", "disarmed"), "armed_away")
        self.assertEqual(self.code(panel, "armed_vacation", "disarmed"), "armed_away")
        self.assertEqual(self.code(panel, "armed_home", "disarmed"), "armed_home")
        self.assertEqual(self.code(panel, "armed_night", "disarmed"), "armed_night")
        self.assertEqual(self.code(panel, "disarmed", "armed_away"), "disarmed")
        self.assertEqual(self.code(panel, "disarmed", "triggered"), "disarmed")

    def test_the_alarm_going_off(self):
        self.assertEqual(self.code("alarm_control_panel.spiti", "triggered", "armed_away"), "alarm")
        self.assertEqual(self.code("alarm_control_panel.spiti", "triggered", "pending"), "alarm")

    def test_a_restart_or_a_step_on_the_way_says_nothing(self):
        panel = "alarm_control_panel.spiti"
        self.assertIsNone(self.code(panel, "armed_away", "unavailable"))
        self.assertIsNone(self.code(panel, "disarmed", "unknown"))
        self.assertIsNone(self.code(panel, "arming", "disarmed"))
        self.assertIsNone(self.code(panel, "pending", "armed_away"))
        self.assertIsNone(self.code(panel, "armed_away", "armed_away"))

    def test_the_name_is_clean_and_short(self):
        self.assertEqual(agent.home_event_name(state("a.b", "on", name="  Κουζίνα \n πάνω ")), "Κουζίνα πάνω")
        self.assertEqual(len(agent.home_event_name(state("a.b", "on", name="x" * 300))), agent.HOME_EVENT_NAME_MAX)


class ActivityLogTest(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "activity.jsonl"
        agent._activity_pending.clear()
        self.addCleanup(agent._activity_pending.clear)

    def note(self, entity_id, new, old, device_class=None, context=None):
        new_state = state(entity_id, new, device_class)
        if context:
            new_state["context"] = context
        agent.activity_note(entity_id, new_state, state(entity_id, old, device_class))

    def test_a_change_and_nothing_more(self):
        self.note("cover.garage", "open", "closed", context={"user_id": "u1"})
        self.note("light.a", "on", "off", context={"parent_id": "p1"})
        agent.activity_flush(self.path)
        rows = agent.activity_log({}, self.path)["rows"]
        self.assertEqual([(r["e"], r["o"], r["s"], r.get("v")) for r in rows], [("light.a", "off", "on", "automation"), ("cover.garage", "closed", "open", "user")])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_who_is_at_home_is_not_the_installers_business(self):
        for device_class in ("motion", "occupancy", "presence"):
            self.note("binary_sensor.a", "on", "off", device_class)
        self.note("person.maria", "home", "not_home")
        self.note("device_tracker.phone", "home", "not_home")
        self.note("sensor.temp", "21", "20", "temperature")
        self.assertEqual(agent.activity_log({}, self.path)["rows"], [])

    def test_a_door_contact_is_a_device_like_any_other(self):
        self.note("binary_sensor.porta", "on", "off", "door")
        self.assertEqual(len(agent.activity_log({}, self.path)["rows"]), 1)

    def test_an_attribute_changing_is_not_a_row(self):
        self.note("light.a", "on", "on")
        self.assertEqual(agent.activity_log({}, self.path)["rows"], [])

    def test_never_a_songs_title(self):
        new = state("media_player.a", "playing")
        new["attributes"]["media_title"] = "Μυστικό"
        row = agent.activity_row("media_player.a", new, state("media_player.a", "idle"))
        self.assertNotIn("Μυστικό", json.dumps(row, ensure_ascii=False))

    def test_pages_walk_back_and_are_bounded(self):
        for i in range(5):
            agent._activity_pending.append(json.dumps({"t": f"2026-11-07T10:0{i}:00+00:00", "e": "light.a", "s": str(i)}))
        with mock.patch.object(agent.time, "time", return_value=1_794_000_000.0):
            first = agent.activity_log({"limit": 2}, self.path)["rows"]
            self.assertEqual([r["s"] for r in first], ["4", "3"])
            older = agent.activity_log({"limit": 2, "before": first[-1]["t"]}, self.path)["rows"]
        self.assertEqual([r["s"] for r in older], ["2", "1"])

    def test_old_rows_go_and_the_file_stays_small(self):
        agent._activity_pending.append(json.dumps({"t": "2020-01-01T00:00:00+00:00", "e": "light.old", "s": "on"}))
        for i in range(4):
            self.note(f"light.new{i}", "on", "off")
        with mock.patch.object(agent, "ACTIVITY_MAX_LINES", 1):
            agent.activity_flush(self.path)
        self.assertEqual([r["e"] for r in agent.activity_log({}, self.path)["rows"]], ["light.new3"])
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_the_box_notes_itself(self):
        agent.activity_note_system("agent 0.1.86 started")
        self.assertEqual(agent.activity_log({}, self.path)["rows"][0]["s"], "agent 0.1.86 started")

    def test_the_cloud_may_ask(self):
        self.assertIn("arvio.activity", agent.CAMERA_AGENT_ACTIONS)


class RingPhotosTest(unittest.TestCase):
    RING = "0a1b2c3d-1111-4222-8333-444455556666"

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        for patch in (
            mock.patch.object(agent, "RING_PHOTOS_DIR", self.dir / "rings"),
            mock.patch.object(agent, "RING_PHOTOS_OFF", self.dir / "rings.off"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def keep(self, raw=b"\xff\xd8\xff photo"):
        with mock.patch.object(agent, "fetch_camera_proxy", return_value=(raw, "image/jpeg")), mock.patch.object(agent, "resize_art", return_value=(raw, "image/jpeg", None)):
            agent.keep_ring_photo(self.RING, "camera.door")

    def test_kept_and_read_back_by_its_id(self):
        self.keep()
        out = agent.ring_photo({"ring_id": self.RING})
        self.assertTrue(out["ok"])
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertEqual(base64.b64decode(out["data_base64"]), b"\xff\xd8\xff photo")
        self.assertEqual((self.dir / "rings" / f"{self.RING}.img").stat().st_mode & 0o777, 0o600)

    def test_an_id_cannot_walk_out_of_the_folder(self):
        for bad in ("../../options", "", "0a1b2c3d", f"{self.RING}/../x"):
            with self.assertRaises(ValueError):
                agent.ring_photo({"ring_id": bad})

    def test_a_ring_with_no_photo_says_so(self):
        self.assertFalse(agent.ring_photo({"ring_id": self.RING})["ok"])

    def test_a_camera_that_refuses_costs_only_the_picture(self):
        with mock.patch.object(agent, "fetch_camera_proxy", side_effect=OSError("offline")):
            agent.keep_ring_photo(self.RING, "camera.door")
        self.assertFalse(agent.ring_photo({"ring_id": self.RING})["ok"])

    def test_old_ones_go_and_the_count_stays_bounded(self):
        self.keep()
        rings = self.dir / "rings"
        old = rings / "old.img"
        old.write_bytes(b"x")
        past = time.time() - agent.RING_PHOTO_MAX_AGE_S - 60
        import os

        os.utime(old, (past, past))
        agent.prune_ring_photos()
        self.assertFalse(old.exists())
        self.assertTrue((rings / f"{self.RING}.img").exists())
        with mock.patch.object(agent, "RING_PHOTO_MAX_FILES", 0):
            agent.prune_ring_photos()
        self.assertFalse((rings / f"{self.RING}.img").exists())

    def test_off_erases_and_on_keeps_again(self):
        self.keep()
        out = agent.ring_photos({"op": "set", "enabled": False})
        self.assertEqual((out["enabled"], out["count"]), (False, 0))
        self.assertFalse(agent.ring_photos_enabled())
        self.assertFalse(agent.ring_photo({"ring_id": self.RING})["ok"])
        self.assertTrue(agent.ring_photos({"op": "set", "enabled": True})["enabled"])
        self.assertTrue(agent.ring_photos_enabled())
        self.assertTrue(agent.ring_photos({})["enabled"])
        with self.assertRaises(ValueError):
            agent.ring_photos({"op": "set", "enabled": "no"})

    def test_the_camera_is_the_one_the_call_sensor_belongs_to(self):
        screens = {"screens": {"s1": {"doorbell": {"camera_entity_id": "camera.vto_main", "call_entity_id": "binary_sensor.custom_call"}}}}
        with mock.patch.object(agent, "load_screens", return_value=screens):
            self.assertEqual(agent.doorbell_camera_for_call("binary_sensor.custom_call"), "camera.vto_main")
            self.assertEqual(agent.doorbell_camera_for_call("binary_sensor.vto_button_pressed"), "camera.vto_main")
        snap = {"entity_regs": {"camera.gate_main": {}, "light.a": {}}}
        with mock.patch.object(agent, "load_screens", return_value={}), mock.patch.object(agent, "registry_snapshot", return_value=snap):
            self.assertEqual(agent.doorbell_camera_for_call("binary_sensor.gate_doorbell"), "camera.gate_main")
            self.assertIsNone(agent.doorbell_camera_for_call("binary_sensor.other_doorbell"))

    def test_the_cloud_may_ask_for_them(self):
        self.assertIn("arvio.ring_photo", agent.CAMERA_AGENT_ACTIONS)
        self.assertIn("arvio.ring_photos", agent.CAMERA_AGENT_ACTIONS)


class DeviceWatchTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "watch.json"

    def at(self, minutes_ago):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.NOW - minutes_ago * 60, timezone.utc).isoformat()

    def tick(self, states, regs=None, now=None):
        return agent.device_watch_tick(states, regs or {}, now=now or self.NOW, path=self.path)

    def test_reports_once_per_outage(self):
        gone = [state("light.kipos", "unavailable", name="Κήπος", last_changed=self.at(40))]
        self.assertEqual(self.tick(gone), [("light.kipos", "Κήπος")])
        self.assertEqual(self.tick(gone), [], "still gone: already said")
        self.assertEqual(self.tick([state("light.kipos", "on", last_changed=self.at(1))]), [])
        self.assertEqual(self.tick(gone), [("light.kipos", "Κήπος")], "back and gone again is a new outage")

    def test_survives_a_restart_of_the_agent(self):
        gone = [state("light.kipos", "unavailable", last_changed=self.at(40))]
        self.tick(gone)
        self.assertEqual(json.loads(self.path.read_text()), ["light.kipos"])
        self.assertEqual(self.tick(gone), [])

    def test_a_short_blink_says_nothing(self):
        self.assertEqual(self.tick([state("light.kipos", "unavailable", last_changed=self.at(10))]), [])

    def test_one_line_per_device_and_only_when_all_of_it_is_gone(self):
        regs = {"light.a": {"device_id": "d1"}, "switch.a": {"device_id": "d1"}}
        half = [state("light.a", "unavailable", last_changed=self.at(40)), state("switch.a", "on", last_changed=self.at(40))]
        self.assertEqual(self.tick(half, regs), [])
        whole = [state("light.a", "unavailable", last_changed=self.at(40)), state("switch.a", "unavailable", last_changed=self.at(40))]
        self.assertEqual(len(self.tick(whole, regs)), 1)

    def test_hidden_and_foreign_entities_do_not_count(self):
        regs = {"light.a": {"hidden": True}}
        rows = [state("light.a", "unavailable", last_changed=self.at(40)), state("update.x", "unavailable", last_changed=self.at(40))]
        self.assertEqual(self.tick(rows, regs), [])

    def test_a_whole_integration_falling_over_stays_a_few_lines(self):
        rows = [state(f"light.l{i}", "unavailable", last_changed=self.at(40)) for i in range(20)]
        self.assertEqual(len(self.tick(rows)), agent.DEVICE_WATCH_PER_TICK)
        self.assertEqual(self.tick(rows), [])


if __name__ == "__main__":
    unittest.main()
