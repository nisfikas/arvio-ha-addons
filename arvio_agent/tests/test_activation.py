"""Arvio Core on the customer's own machine: the activation code (arvio docs/plans/2026-11-14-core-self-hosted.md §5)."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from helpers import agent


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Activation(unittest.TestCase):
    def setUp(self):
        agent.save_hub({})
        # opts() rewrites these globals; other tests read them (mode decides the LAN path).
        self.names = ("CLOUD", "SERIAL", "ACTIVATION_CODE", "mode", "RELAY_URL", "RELAY_TOKEN", "err")
        self.saved = {n: getattr(agent, n) for n in self.names}
        self.env = os.environ.copy()
        agent.CLOUD = "https://cloud.example"
        agent.ACTIVATION_CODE = "ARV-7K3M-9QXT"
        agent.SERIAL = "core-0a1b2c3d"

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(agent, n, v)
        os.environ.clear()
        os.environ.update(self.env)
        agent.save_hub({})

    def test_the_code_gives_the_box_a_hub(self):
        sent = []

        def urlopen(req, timeout=0):
            sent.append((req.full_url, json.loads(req.data)))
            return _Answer(json.dumps({"hub_id": "hub_12345678", "state": "claimed"}).encode())

        st = {}
        with mock.patch.object(agent.urllib.request, "urlopen", urlopen):
            self.assertTrue(agent.activate_remote(st, "pk1"))
            self.assertTrue(agent.activate_remote(st, "pk1"))  # already has a hub: nothing sent
        self.assertEqual(sent, [("https://cloud.example/v1/hubs/activate", {"code": "ARV-7K3M-9QXT", "serial": "core-0a1b2c3d", "enroll_public_key": "pk1"})])
        self.assertEqual(agent.load_hub()["hub_id"], "hub_12345678")

    def test_a_refused_code_is_not_sent_again(self):
        calls = []

        def urlopen(req, timeout=0):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"code_used"}'))

        st = {}
        with mock.patch.object(agent.urllib.request, "urlopen", urlopen):
            self.assertFalse(agent.activate_remote(st, "pk1"))
            self.assertFalse(agent.activate_remote(st, "pk1"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(agent.err, "activate:code_used")
            agent.ACTIVATION_CODE = "ARV-2222-3333"  # a new code is tried
            self.assertFalse(agent.activate_remote(st, "pk1"))
        self.assertEqual(len(calls), 2)

    def test_a_network_failure_is_tried_again(self):
        calls = []

        def urlopen(req, timeout=0):
            calls.append(1)
            raise urllib.error.URLError("no route")

        st = {}
        with mock.patch.object(agent.urllib.request, "urlopen", urlopen):
            agent.activate_remote(st, "pk1")
            agent.activate_remote(st, "pk1")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("activation_refused", agent.load_hub())

    def test_a_box_with_a_code_and_no_serial_makes_its_own_once(self):
        os.environ.pop("ARVIO_SERIAL", None)
        os.environ["ARVIO_ACTIVATION_CODE"] = "ARV-7K3M-9QXT"
        agent.SERIAL = "rpi-lab-1"
        agent.opts()
        first = agent.SERIAL
        self.assertRegex(first, r"^core-[0-9a-f]{8}$")
        agent.SERIAL = "rpi-lab-1"
        agent.opts()
        self.assertEqual(agent.SERIAL, first)
        self.assertEqual(agent.load_hub()["serial"], first)

    def test_without_a_code_nothing_changes(self):
        os.environ.pop("ARVIO_ACTIVATION_CODE", None)
        os.environ.pop("ARVIO_SERIAL", None)
        agent.SERIAL = "rpi-lab-1"
        agent.opts()
        self.assertEqual(agent.SERIAL, "rpi-lab-1")
        self.assertEqual(agent.ACTIVATION_CODE, "")


if __name__ == "__main__":
    unittest.main()
