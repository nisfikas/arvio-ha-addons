"""On Arvio Core the backups are the core's own, not the Supervisor's: Partner said «Backup άγνωστο» at SFK HQ."""
import unittest
from unittest import mock

from helpers import agent

CORE = "http://127.0.0.1:8098"
LISTED = {
    "backups": [
        {"file": "arvio-arvio-os-2026-11-10T01-30-00-125Z.arviobk", "size": 48211},
        {"file": "arvio-arvio-os-2026-11-11T01-30-00-004Z.arviobk", "size": 48907},
        {"file": "notes.txt", "size": 3},
    ]
}


class CoreBackupsTest(unittest.TestCase):
    def telemetry(self, answer, origin=CORE):
        with mock.patch.object(agent, "core_origin", return_value=origin), mock.patch.object(agent, "core_v1", return_value=answer) as core, mock.patch.object(agent, "supervisor", return_value=None) as sup:
            return agent.backup_telemetry(), core, sup

    def test_reports_the_cores_sealed_backups(self):
        out, core, sup = self.telemetry(LISTED)
        core.assert_called_once_with("/v1/backups")
        sup.assert_not_called()
        self.assertEqual(out["backup_count"], 2)
        self.assertEqual(out["last_backup_at"], "2026-11-11T01:30:00.004Z")
        self.assertEqual(out["backups"][0], {"slug": "arvio-arvio-os-2026-11-10T01-30-00-125Z.arviobk", "name": "arvio-arvio-os-2026-11-10T01-30-00-125Z.arviobk", "date": "2026-11-10T01:30:00.125Z", "size_bytes": 48211, "protected": True})

    def test_a_core_that_has_none_yet_or_refuses_says_none(self):
        for answer in ({"backups": []}, {"error": "unauthorized"}, {"status": "rejected", "reason": "http_404"}):
            out, _, _ = self.telemetry(answer)
            self.assertEqual((out["backup_count"], out["last_backup_at"]), (0, None))

    def test_a_core_that_is_down_never_costs_the_heartbeat(self):
        with mock.patch.object(agent, "core_origin", return_value=CORE), mock.patch.object(agent, "core_v1", side_effect=OSError("down")):
            self.assertEqual(agent.backup_telemetry()["backup_count"], 0)

    def test_home_assistant_still_asks_the_supervisor(self):
        sup = {"data": {"backups": [{"slug": "a1b2", "name": "Arvio", "date": "2026-11-11T19:45:56+00:00", "size": 1.5}]}}
        with mock.patch.object(agent, "core_origin", return_value=""), mock.patch.object(agent, "supervisor", return_value=sup), mock.patch.object(agent, "core_v1") as core:
            out = agent.backup_telemetry()
        core.assert_not_called()
        self.assertEqual((out["backup_count"], out["last_backup_at"]), (1, "2026-11-11T19:45:56+00:00"))

    def test_backup_now_asks_the_core(self):
        with mock.patch.object(agent, "core_origin", return_value=CORE), mock.patch.object(agent, "core_v1", return_value={"file": "arvio-x-2026-11-11T20-00-00-000Z.arviobk", "size": 9}) as core:
            self.assertEqual(agent.create_full_backup(), {"ok": True, "slug": "arvio-x-2026-11-11T20-00-00-000Z.arviobk", "name": "arvio-x-2026-11-11T20-00-00-000Z.arviobk"})
        core.assert_called_once_with("/v1/backups", "POST")
        with mock.patch.object(agent, "core_origin", return_value=CORE), mock.patch.object(agent, "core_v1", return_value={"error": "no_recipients"}):
            with self.assertRaisesRegex(RuntimeError, "no_recipients"):
                agent.create_full_backup()


if __name__ == "__main__":
    unittest.main()
