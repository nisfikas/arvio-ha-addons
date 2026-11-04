"""History and meters read through the agent, and never on the LAN path."""
import unittest
from unittest import mock

from helpers import agent


class HistoryAction(unittest.TestCase):
    def test_query_passes_only_clean_ids(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"series": []}) as ha:
            out = agent.history_action({"op": "query", "entity_ids": ["sensor.a_power", "../etc", "light.b"], "from": 1, "to": 2})
        self.assertEqual(out, {"ok": True, "result": {"series": []}})
        self.assertEqual(ha.call_args.args[0], "/arvio/history?entity_id=sensor.a_power&entity_id=light.b&from=1&to=2")

    def test_query_needs_an_entity(self):
        with self.assertRaises(ValueError):
            agent.history_action({"op": "query", "entity_ids": ["../etc"]})

    def test_energy_checks_period_and_date(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"total_wh": 1}) as ha:
            agent.history_action({"op": "energy", "period": "month", "date": "2026-11-19"})
        self.assertEqual(ha.call_args.args[0], "/arvio/energy?period=month&date=2026-11-19")
        for bad in ({"op": "energy", "period": "decade"}, {"op": "energy", "date": "x; rm"}):
            with self.assertRaises(ValueError):
                agent.history_action(bad)

    def test_writes_go_where_they_should(self):
        for payload, path in (
            ({"op": "set_tariffs", "tariffs": []}, "/arvio/energy/tariffs"),
            ({"op": "set_watts", "entity_id": "light.a", "watts": 42}, "/arvio/energy/watts"),
            ({"op": "set_enabled", "enabled": False}, "/arvio/history/enabled"),
            ({"op": "erase"}, "/arvio/history/erase"),
        ):
            with mock.patch.object(agent, "ha_or_raise", return_value={}) as ha:
                agent.history_action(payload)
            self.assertEqual(ha.call_args.args[0], path)
            self.assertEqual(ha.call_args.kwargs["method"], "POST")

    def test_enabled_must_be_a_boolean(self):
        with self.assertRaises(ValueError):
            agent.history_action({"op": "set_enabled", "enabled": "no"})

    def test_unknown_op(self):
        with self.assertRaises(ValueError):
            agent.history_action({"op": "drop_everything"})


class Meters(unittest.TestCase):
    def test_list_and_set(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"meters": []}) as ha:
            agent.meters_action({})
            self.assertEqual(ha.call_args.args[0], "/arvio/meters")
            agent.meters_action({"op": "set", "key": "k", "role": "grid", "extra": "dropped"})
            self.assertEqual(ha.call_args.kwargs["body"], {"key": "k", "role": "grid"})

    def test_cannot_reach_history_through_meters(self):
        with self.assertRaises(ValueError):
            agent.meters_action({"op": "query"})


class Boundaries(unittest.TestCase):
    def test_allowed_through_the_relay(self):
        self.assertIn("arvio.history", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.meters", agent.AGENT_SERVICE_ALLOWLIST)

    def test_never_on_the_lan_path(self):
        self.assertNotIn("arvio.history", agent.LAN_ALLOWED_ACTIONS)
        self.assertNotIn("arvio.meters", agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected):
            agent.check_command_safety({"action": "arvio.history", "payload": {"op": "erase"}}, via="lan")



class PanelEnergy(unittest.TestCase):
    def setUp(self):
        agent._panel_energy_cache.update(at=0.0, value=None)

    def row(self, *kinds):
        return {"pages": [{"tiles": [{"kind": k} for k in kinds]}]}

    def test_nothing_without_the_tile(self):
        with mock.patch.object(agent, "ha_or_raise") as ha:
            self.assertIsNone(agent.panel_energy(self.row("clock", "weather"), now=1000.0))
        ha.assert_not_called()

    def test_only_the_now_and_today_never_devices_or_money(self):
        report = {"now_w": 1840, "whole_house": True, "total_wh": 9400, "cents": 124, "devices": [{"name": "Κρεβατοκάμαρα"}], "bars": [{"wh": 100, "reduced": True}, {"wh": 200}]}
        with mock.patch.object(agent, "ha_or_raise", return_value=report):
            out = agent.panel_energy(self.row("energy"), now=1000.0)
        self.assertEqual(out, {"now_w": 1840, "whole_house": True, "today_wh": 9400, "hours": [100, 200], "reduced": [0]})

    def test_asks_core_at_most_every_30_seconds(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"now_w": 1}) as ha:
            agent.panel_energy(self.row("energy"), now=1000.0)
            agent.panel_energy(self.row("energy"), now=1010.0)
            self.assertEqual(ha.call_count, 1)
            agent.panel_energy(self.row("energy"), now=1031.0)
            self.assertEqual(ha.call_count, 2)

    def test_a_hub_on_home_assistant_is_a_dash_not_an_error(self):
        with mock.patch.object(agent, "ha_or_raise", side_effect=RuntimeError("404")):
            self.assertIsNone(agent.panel_energy(self.row("energy"), now=1000.0))

    def test_the_tile_needs_no_ref(self):
        with self.assertRaises(ValueError):
            agent._parse_screen_pages([{"id": "p", "tiles": [{"kind": "energy", "ref": "x"}]}])


if __name__ == "__main__":
    unittest.main()
