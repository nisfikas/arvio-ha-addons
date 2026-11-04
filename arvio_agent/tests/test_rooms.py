"""Rooms and floors: docs/plans/2026-11-05-rooms-and-floors.md"""
import unittest
from unittest import mock

import agent


def snapshot():
    return {
        "floors": [{"floor_id": "isogeio", "name": "Ισόγειο", "level": 0}],
        "areas": {
            "saloni": {"area_id": "saloni", "name": "Σαλόνι", "floor_id": "isogeio"},
            "saloni_2": {"area_id": "saloni_2", "name": "Σαλονι", "floor_id": None},
        },
        "devices": {"dev1": {"device_id": "dev1", "name": "Hue", "area_id": "saloni_2"}},
        "entity_regs": {
            "light.a": {"entity_id": "light.a", "area_id": "saloni_2"},
            "light.b": {"entity_id": "light.b", "device_id": "dev1"},
            "light.c": {"entity_id": "light.c", "area_id": "saloni"},
        },
    }


class Rooms(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def ws(kind, body=None, **_):
            self.calls.append((kind, body))
            return {"floor_id": "protos"} if kind == "config/floor_registry/create" else {}

        for name, new in (
            ("ha_ws_command", ws),
            ("schedule_registry_refresh", mock.Mock()),
            ("refresh_registry_cache", mock.Mock(return_value=True)),
            ("registry_snapshot", snapshot),
            ("ha", mock.Mock(return_value=[])),
            ("fetch_arvio_automation_configs", mock.Mock(return_value={
                "arvio_a": {"alias": "Βράδυ", "action": [{"target": {"area_id": ["saloni_2"]}}]},
                "arvio_b": {"alias": "Πρωί", "action": [{"target": {"area_id": "kouzina"}}]},
            })),
        ):
            p = mock.patch.object(agent, name, new)
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_says_what_points_at_the_room_and_touches_nothing(self):
        out = agent.area_delete({"area_id": "saloni_2", "dry_run": True})
        self.assertEqual((out["devices"], out["automations"]), (2, ["Βράδυ"]))
        self.assertEqual(self.calls, [])

    def test_a_merge_moves_devices_first_and_deletes_last(self):
        agent.area_delete({"area_id": "saloni_2", "move_to": "saloni"})
        self.assertEqual(self.calls, [
            ("config/device_registry/update", {"device_id": "dev1", "area_id": "saloni"}),
            ("config/entity_registry/update", {"entity_id": "light.a", "area_id": "saloni"}),
            ("config/area_registry/delete", {"area_id": "saloni_2"}),
        ])

    def test_with_nowhere_to_go_the_registry_clears_the_room_itself(self):
        agent.area_delete({"area_id": "saloni_2"})
        self.assertEqual(self.calls, [("config/area_registry/delete", {"area_id": "saloni_2"})])

    def test_refuses_what_makes_no_sense(self):
        for payload in ({"area_id": "../x"}, {"area_id": "nope"}, {"area_id": "saloni", "move_to": "saloni"}, {"area_id": "saloni", "move_to": "nope"}):
            with self.assertRaises(ValueError):
                agent.area_delete(payload)
        self.assertEqual(self.calls, [])

    def test_a_room_changes_floor_without_a_name(self):
        self.assertEqual(agent.parse_area_update({"area_id": "saloni", "floor_id": "isogeio"}), {"area_id": "saloni", "floor_id": "isogeio"})
        self.assertEqual(agent.parse_area_update({"area_id": "saloni", "floor_id": None}), {"area_id": "saloni", "floor_id": None})
        with self.assertRaises(ValueError):
            agent.parse_area_update({"area_id": "saloni"})
        with self.assertRaises(ValueError):
            agent.parse_area_update({"area_id": "saloni", "floor_id": "__floor_none"})
        agent.area_create({"name": "Χώλος", "floor_id": "isogeio"})
        self.assertEqual(self.calls[-1], ("config/area_registry/create", {"name": "Χώλος", "floor_id": "isogeio"}))

    def test_floors(self):
        out = agent.floor_create({"name": " Πρώτος "})
        self.assertEqual(self.calls[-1], ("config/floor_registry/create", {"name": "Πρώτος", "level": 1}))
        self.assertEqual(out["floor_id"], "protos")
        self.assertFalse(out["first"])
        agent.floor_update({"floor_id": "isogeio", "level": 2})
        self.assertEqual(self.calls[-1], ("config/floor_registry/update", {"floor_id": "isogeio", "level": 2}))
        with self.assertRaises(ValueError):
            agent.floor_update({"floor_id": "isogeio"})
        self.assertEqual(agent.floor_delete({"floor_id": "isogeio", "dry_run": True})["rooms"], ["Σαλόνι"])
        self.assertTrue(agent.floor_delete({"floor_id": "isogeio"})["last"])
        self.assertEqual(self.calls[-1], ("config/floor_registry/delete", {"floor_id": "isogeio"}))
        with self.assertRaises(ValueError):
            agent.floor_delete({"floor_id": "nope"})

    def test_cloud_may_ask_for_them_but_never_the_lan(self):
        for action in ("arvio.area_delete", "arvio.floor_create", "arvio.floor_update", "arvio.floor_delete"):
            self.assertIn(action, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS)


if __name__ == "__main__":
    unittest.main()
