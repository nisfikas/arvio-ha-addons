"""HA Container / NAS sidecar: Core URL instead of Supervisor."""
import os
import unittest

from helpers import agent


class CoreDirectTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ARVIO_HA_URL", None)
        os.environ.pop("ARVIO_HA_TOKEN", None)
        os.environ.pop("ARVIO_SERIAL", None)
        os.environ.pop("ARVIO_CLOUD_URL", None)

    def test_default_is_supervisor(self):
        os.environ.pop("ARVIO_HA_URL", None)
        self.assertTrue(agent.uses_supervisor())
        self.assertEqual(agent.core_api("/states"), "http://supervisor/core/api/states")
        self.assertEqual(agent.core_ws(), "ws://supervisor/core/websocket")
        self.assertEqual(
            agent.core_resource("/api/camera_proxy/camera.front"),
            "http://supervisor/core/api/camera_proxy/camera.front",
        )

    def test_container_urls(self):
        os.environ["ARVIO_HA_URL"] = "http://192.168.68.77:8123"
        self.assertFalse(agent.uses_supervisor())
        self.assertEqual(
            agent.core_api("/states"),
            "http://192.168.68.77:8123/api/states",
        )
        self.assertEqual(
            agent.core_ws(),
            "ws://192.168.68.77:8123/api/websocket",
        )
        self.assertEqual(
            agent.core_resource("/api/camera_proxy/camera.front"),
            "http://192.168.68.77:8123/api/camera_proxy/camera.front",
        )
        self.assertIsNone(agent.supervisor("/backups"))
        self.assertIn("Container", agent.err)

    def test_token_env(self):
        os.environ.pop("SUPERVISOR_TOKEN", None)
        os.environ.pop("HASSIO_TOKEN", None)
        os.environ["ARVIO_HA_TOKEN"] = "llat_test_token"
        self.assertEqual(agent.read_token(), "llat_test_token")

    def test_opts_env_overrides(self):
        prev = (agent.CLOUD, agent.SERIAL, agent.RELAY_URL)

        def restore():
            agent.CLOUD, agent.SERIAL, agent.RELAY_URL = prev

        self.addCleanup(restore)
        os.environ["ARVIO_CLOUD_URL"] = "https://cloud.arvio.systems/"
        os.environ["ARVIO_SERIAL"] = "nas-home-1"
        os.environ["ARVIO_RELAY_URL"] = "https://relay.arvio.systems"
        agent.opts()
        self.assertEqual(agent.CLOUD, "https://cloud.arvio.systems")
        self.assertEqual(agent.SERIAL, "nas-home-1")
        self.assertEqual(agent.RELAY_URL, "https://relay.arvio.systems")
