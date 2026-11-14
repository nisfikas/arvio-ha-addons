"""A state nothing read back (an IR air conditioner, an RTS motor) reaches Home marked, so it says «στάλθηκε»."""
import unittest

from helpers import agent

AC = {"assumed_state": True, "hvac_modes": ["off", "cool"], "temperature": 24, "min_temp": 22, "max_temp": 26}


class AssumedStateTest(unittest.TestCase):
    def test_marked_on_any_domain(self):
        self.assertIs(agent.typed_attrs("climate", "cool", AC)["assumed_state"], True)
        self.assertEqual(agent.typed_attrs("switch", "on", {"assumed_state": True}), {"assumed_state": True})

    def test_only_a_real_true(self):
        self.assertNotIn("assumed_state", agent.typed_attrs("climate", "cool", {**AC, "assumed_state": "yes"}))
        self.assertNotIn("assumed_state", agent.typed_attrs("light", "on", {"brightness": 255}))


if __name__ == "__main__":
    unittest.main()
