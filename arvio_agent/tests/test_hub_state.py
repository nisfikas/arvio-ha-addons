"""hub.json survives a power cut: atomic writes, and an emptied file no longer crash-loops the agent."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import agent


class HubStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "hub.json"
        self.patch = mock.patch.object(agent, "STATE", self.state)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_atomic_write_leaves_no_temp_and_round_trips(self):
        agent.save_hub({"hub_id": "hub_x", "relay_token": "t"})
        self.assertEqual(agent.load_hub()["hub_id"], "hub_x")
        self.assertFalse((self.dir / "hub.json.tmp").exists())

    def test_empty_file_is_kept_aside_not_fatal(self):
        self.state.write_text("")
        self.assertEqual(agent.load_hub(), {})
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.dir.glob("hub.json.unreadable-*"))), 1)

    def test_not_a_dict_is_unreadable_too(self):
        self.state.write_text(json.dumps([1, 2]))
        self.assertEqual(agent.load_hub(), {})


if __name__ == "__main__":
    unittest.main()
