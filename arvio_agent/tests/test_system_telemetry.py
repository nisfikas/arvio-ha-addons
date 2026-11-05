"""Console's fleet learns what each box runs from the heartbeat. A failure here must never cost the heartbeat."""
import unittest
from unittest import mock

from helpers import agent


class SystemTelemetryTest(unittest.TestCase):
    def setUp(self):
        agent.ha_version_cache = (0.0, None)

    def report(self, update=None, origin="http://127.0.0.1:8098", config=None):
        with mock.patch.object(agent, "core_origin", return_value=origin), mock.patch.object(agent, "core_v1", return_value=update or {}), mock.patch.object(agent, "ha", return_value=config):
            return agent.system_telemetry()

    def test_image_box_reports_its_kind_version_and_how_the_update_is_going(self):
        out = self.report({"os": {"kind": "arvio_os", "version": "2026.11.05.3", "image": {"version": "2026.11.05.3"}, "last": {"id": "os-1", "status": "applied"}, "progress": {"phase": "write"}}})
        self.assertEqual(out["system_kind"], "arvio_os")
        self.assertEqual(out["system_version"], "2026.11.05.3")
        self.assertEqual(out["system_update"]["status"], "applied")
        self.assertEqual(out["system_progress"]["phase"], "write")
        self.assertIs(out["system_usb"], False)
        self.assertIs(self.report({"os": {"kind": "arvio_os", "usb": True}})["system_usb"], True)

    def test_a_core_from_before_it_said_its_kind(self):
        image = self.report({"os": {"image": {"board": "x86", "version": "2026.11.05.3"}, "last": None}})
        self.assertEqual((image["system_kind"], image["system_version"]), ("arvio_os", "2026.11.05.3"))
        nas = self.report({"os": {"image": None, "last": None}})
        self.assertEqual((nas["system_kind"], nas["system_version"]), ("core_other", None))

    def test_home_assistant_reports_its_own_version(self):
        for origin, update in (("", {}), ("http://127.0.0.1:8123", {"status": "rejected", "reason": "http_404"})):
            agent.ha_version_cache = (0.0, None)
            out = self.report(update, origin=origin, config={"version": "2026.10.4"})
            self.assertEqual(out, {"system_kind": "home_assistant", "system_version": "2026.10.4"})

    def test_a_failure_is_silent(self):
        with mock.patch.object(agent, "core_origin", return_value="http://x"), mock.patch.object(agent, "core_v1", side_effect=OSError("down")):
            self.assertEqual(agent.system_telemetry(), {})

    def test_status_reads_the_version_the_core_really_sends(self):
        with mock.patch.object(agent, "core_v1", return_value={"os": {"image": {"version": "2026.11.05.3"}}}):
            self.assertEqual(agent.os_status()["version"], "2026.11.05.3")
        with mock.patch.object(agent, "core_v1", return_value={"os": {"version": "2026.11.05.9", "image": None}}):
            self.assertEqual(agent.os_status()["version"], "2026.11.05.9")


if __name__ == "__main__":
    unittest.main()
