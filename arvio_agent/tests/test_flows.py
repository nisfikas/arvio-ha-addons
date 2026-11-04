"""The canvas reads through the agent: what a flow did, and a rehearsal that touches nothing."""
import unittest
from unittest import mock

from helpers import agent


class FlowReads(unittest.TestCase):
    def test_runs_go_to_core_with_a_capped_limit(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"runs": [{"event": "run"}]}) as ha:
            out = agent.flow_runs({"id": "arvio_flow_a1", "limit": 9999})
        self.assertEqual(out, {"ok": True, "runs": [{"event": "run"}]})
        self.assertEqual(ha.call_args.args[0], "/arvio/flows/arvio_flow_a1/runs?limit=300")

    def test_runs_refuse_a_foreign_id(self):
        for bad in ("", "../states", "a b", "a/b"):
            with self.assertRaises(ValueError):
                agent.flow_runs({"id": bad})

    def test_dryrun_passes_only_what_core_reads(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"ran": True}) as ha:
            out = agent.flow_dryrun({"config": {"id": "x"}, "at": "2026-11-04T10:00:00Z", "fail": ["a"], "other": 1})
        self.assertEqual(out, {"ok": True, "result": {"ran": True}})
        self.assertEqual(ha.call_args.kwargs["body"], {"config": {"id": "x"}, "at": "2026-11-04T10:00:00Z", "fail": ["a"]})

    def test_dryrun_needs_a_config(self):
        with self.assertRaises(ValueError):
            agent.flow_dryrun({})

    def test_catalog_and_touched(self):
        with mock.patch.object(agent, "ha_or_raise", return_value={"cloud": ["a.b"], "buttons": []}):
            self.assertEqual(agent.flow_catalog({}), {"ok": True, "cloud": ["a.b"], "buttons": []})
        with mock.patch.object(agent, "ha_or_raise", return_value={"touched": [{"alias": "x"}]}) as ha:
            self.assertEqual(agent.entity_touched({"entity_id": "light.a"})["touched"], [{"alias": "x"}])
        self.assertEqual(ha.call_args.args[0], "/arvio/entities/light.a/touched")
        with self.assertRaises(ValueError):
            agent.entity_touched({"entity_id": "../states"})

    def test_both_are_commands_and_stay_off_the_lan(self):
        self.assertIn("arvio.flow_runs", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.flow_dryrun", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.flow_catalog", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertIn("arvio.entity_touched", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertNotIn("arvio.flow_catalog", agent.LAN_ALLOWED_ACTIONS)
        self.assertNotIn("arvio.entity_touched", agent.LAN_ALLOWED_ACTIONS)
        self.assertNotIn("arvio.flow_runs", agent.LAN_ALLOWED_ACTIONS)
        self.assertNotIn("arvio.flow_dryrun", agent.LAN_ALLOWED_ACTIONS)


if __name__ == "__main__":
    unittest.main()
