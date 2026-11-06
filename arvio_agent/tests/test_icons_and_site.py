"""An installer's icons live on the hub, and the hub learns where it is from the cloud."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from helpers import agent


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.dir.name)
        self.patches = [
            mock.patch.object(agent, "ICONS", d / "icons.json"),
            mock.patch.object(agent, "SITE_UNITS", d / "site_units.json"),
            mock.patch.object(agent, "_icons_cache", None),
            mock.patch.object(agent, "schedule_registry_refresh"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()


class IconSet(Base):
    def test_a_device_and_a_room_keep_their_icon_and_can_lose_it(self):
        agent.icon_set({"entity_id": "light.saloni", "icon": "mdi:chandelier"})
        agent.icon_set({"area_id": "saloni", "icon": "mdi:sofa"})
        self.assertEqual(agent.picked_icons()["entities"], {"light.saloni": "mdi:chandelier"})
        self.assertEqual(agent.picked_icons()["areas"], {"saloni": "mdi:sofa"})
        agent._icons_cache = None  # as after a restart
        self.assertEqual(agent.picked_icons()["entities"], {"light.saloni": "mdi:chandelier"})
        agent.icon_set({"entity_id": "light.saloni", "icon": None})
        self.assertEqual(agent.picked_icons()["entities"], {})
        agent.schedule_registry_refresh.assert_called()

    def test_refuses_what_is_not_an_icon_or_has_no_single_target(self):
        for bad in (
            {"entity_id": "light.x", "icon": "<svg>"},
            {"entity_id": "light.x", "icon": 3},
            {"entity_id": "../etc", "icon": "mdi:sofa"},
            {"entity_id": "light.x", "area_id": "a", "icon": "mdi:sofa"},
            {"icon": "mdi:sofa"},
            {"area_id": "a b/c", "icon": "mdi:sofa"},
        ):
            with self.assertRaises(ValueError, msg=bad):
                agent.icon_set(bad)

    def test_a_lock_or_an_alarm_keeps_the_icon_that_shows_its_state(self):
        for eid in ("lock.front", "alarm_control_panel.home"):
            with self.assertRaises(ValueError):
                agent.icon_set({"entity_id": eid, "icon": "mdi:lightbulb"})

    def test_the_lan_path_cannot_reach_it(self):
        for action in ("arvio.icon_set", "arvio.site_config"):
            self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS)


class SiteConfig(Base):
    def test_tells_the_core_where_it_is_and_never_its_units(self):
        with mock.patch.object(agent, "ha_ws_command") as ws:
            out = agent.site_config({"location_name": "Βίλα", "time_zone": "Europe/London", "latitude": 37.5, "longitude": 23.7, "units": {"temperature": "F", "currency": "USD", "hours": 12}})
        self.assertTrue(out["ok"])
        ws.assert_called_once_with("config/core/update", {"location_name": "Βίλα", "time_zone": "Europe/London", "latitude": 37.5, "longitude": 23.7})
        self.assertEqual(agent.site_units(), {"temperature": "F", "currency": "USD", "hours": 12})

    def test_without_a_place_the_core_keeps_the_one_it_has(self):
        core, units = agent.parse_site_config({"location_name": "Βίλα", "time_zone": "Europe/Athens"})
        self.assertNotIn("latitude", core)
        self.assertEqual(units, agent.SITE_UNITS_DEFAULT)

    def test_refuses_half_a_place_or_a_bad_zone(self):
        for bad in (
            {"location_name": "Β", "time_zone": "Europe/Athens", "latitude": 37.5},
            {"location_name": "Β", "time_zone": "Europe/Athens", "latitude": 95, "longitude": 23},
            {"location_name": "Β", "time_zone": "Europe/Athens", "latitude": "37", "longitude": "23"},
            {"location_name": "Β", "time_zone": "no such; rm"},
            {"location_name": "", "time_zone": "Europe/Athens"},
        ):
            with self.assertRaises(ValueError, msg=bad):
                agent.parse_site_config(bad)

    def test_a_house_nobody_set_reads_as_celsius_and_24_hours(self):
        self.assertEqual(agent.site_units(), {"temperature": "C", "currency": "EUR", "hours": 24})


if __name__ == "__main__":
    unittest.main()
