"""A remote reaches the model for Partner to list it, with the presses it declares."""
import unittest

from helpers import agent

REMOTE = {
    "entity_id": "event.grapheio_tilecheiristerio_400",
    "state": "unknown",
    "attributes": {"friendly_name": "Τηλεχειριστήριο", "device_class": "button", "event_types": ["button1_pressed", "button2_held_down"]},
}


class RemoteModelTest(unittest.TestCase):
    def test_in_the_model_with_its_presses(self):
        self.assertIn("event", agent.HOME_ENTITY_DOMAINS)
        self.assertEqual(agent.typed_attrs("event", "unknown", REMOTE["attributes"]), {"event_types": ["button1_pressed", "button2_held_down"]})

    def test_the_model_entry_partner_reads(self):
        m = agent.entity_model_from_state(REMOTE, None, {}, {})
        self.assertEqual((m["domain"], m["name"], m["attrs"]), ("event", "Τηλεχειριστήριο", {"event_types": ["button1_pressed", "button2_held_down"]}))

    def test_a_remote_with_no_list_still_says_nothing_false(self):
        self.assertEqual(agent.typed_attrs("event", "unknown", {"event_types": "not a list"}), {})

    def test_not_for_panels_and_never_on_the_lan_allowlist(self):
        self.assertFalse(any(a.startswith("event.") for a in agent.LAN_ALLOWED_ACTIONS))
        self.assertFalse(any(a.startswith("event.") for a in agent.AGENT_SERVICE_ALLOWLIST))


if __name__ == "__main__":
    unittest.main()
