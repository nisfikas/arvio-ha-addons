"""Standalone (NAS) OTA: fetch the published add-on, check it, stage it, roll back a bad one."""
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, agent

sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402

PREFIX = "arvio-ha-addons-main/arvio_agent/"


def tarball(files: dict, extra=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(PREFIX + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info)
    return buf.getvalue()


def addon(version: str) -> dict:
    return {
        "agent.py": f'AGENT_VERSION = "{version}"\n'.encode(),
        "occupancy.py": b"X = 1\n",
        "config.yaml": f'name: "Arvio Agent"\nversion: "{version}"\n'.encode(),
        "ui.html": b"<html></html>",
        "tests/test_x.py": b"raise SystemExit\n",
    }


class StandaloneUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = {
            "STANDALONE_LIVE": self.tmp / "agent-app",
            "STANDALONE_PREV": self.tmp / "agent-app.prev",
            "STANDALONE_NEW": self.tmp / "agent-app.new",
            "STANDALONE_TRIAL": self.tmp / "agent-app.trial",
        }
        self.patches = [mock.patch.object(agent, k, v) for k, v in self.paths.items()]
        self.patches += [
            mock.patch.object(launcher, "LIVE", self.paths["STANDALONE_LIVE"]),
            mock.patch.object(launcher, "PREV", self.paths["STANDALONE_PREV"]),
            mock.patch.object(launcher, "TRIAL", self.paths["STANDALONE_TRIAL"]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_extract_keeps_only_the_agent_files(self):
        link = tarfile.TarInfo(PREFIX + "evil.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        up = tarfile.TarInfo(PREFIX + "../escape.py")
        up.size = 0
        other = tarfile.TarInfo("arvio-ha-addons-main/arvio_updater/updater.py")
        other.size = 0
        files = agent.extract_agent_files(tarball(addon("0.1.52"), [link, up, other]))
        self.assertEqual(sorted(files), ["agent.py", "config.yaml", "occupancy.py", "ui.html"])
        with self.assertRaises(ValueError):
            agent.extract_agent_files(tarball({"ui.html": b"x"}))

    def test_stage_swaps_and_keeps_the_previous(self):
        agent.stage_standalone(agent.extract_agent_files(tarball(addon("0.1.52"))), "0.1.52")
        live = self.paths["STANDALONE_LIVE"]
        self.assertIn("0.1.52", (live / "agent.py").read_text())
        self.assertEqual(self.paths["STANDALONE_TRIAL"].read_text(), "0.1.52")
        agent.stage_standalone(agent.extract_agent_files(tarball(addon("0.1.53"))), "0.1.53")
        self.assertIn("0.1.53", (live / "agent.py").read_text())
        self.assertIn("0.1.52", (self.paths["STANDALONE_PREV"] / "agent.py").read_text())
        # The launcher's rollback puts the previous one back and stops the trial.
        launcher.rollback()
        self.assertIn("0.1.52", (live / "agent.py").read_text())
        self.assertFalse(self.paths["STANDALONE_TRIAL"].exists())

    def test_broken_code_is_never_staged(self):
        files = agent.extract_agent_files(tarball({**addon("0.1.52"), "agent.py": b"def (:\n"}))
        with self.assertRaises(Exception):
            agent.stage_standalone(files, "0.1.52")
        self.assertFalse(self.paths["STANDALONE_LIVE"].exists())

    def fetch(self, version: str):
        resp = mock.MagicMock()
        resp.read.return_value = tarball(addon(version))
        resp.__enter__.return_value = resp
        return mock.patch.object(agent.urllib.request, "urlopen", return_value=resp)

    def test_update_checks_launcher_and_version_then_restarts(self):
        with mock.patch.dict(os.environ, {"ARVIO_LAUNCHER": ""}), self.assertRaises(RuntimeError):
            agent.standalone_update("0.1.52")
        with mock.patch.dict(os.environ, {"ARVIO_LAUNCHER": "1"}):
            with self.fetch("0.1.52"), self.assertRaises(ValueError):
                agent.standalone_update("0.1.60")
            with self.fetch(agent.AGENT_VERSION):
                self.assertEqual(agent.standalone_update("")["note"], "already up to date")
            with self.fetch("9.9.9"), mock.patch.object(agent.threading, "Timer") as timer:
                out = agent.standalone_update("9.9.9")
        self.assertEqual(out["staged_version"], "9.9.9")
        self.assertEqual(timer.call_args.args[0], 2.0)
        self.assertTrue((self.paths["STANDALONE_LIVE"] / "agent.py").is_file())

    def test_ha_container_takes_the_standalone_path(self):
        with mock.patch.object(agent, "uses_supervisor", return_value=False), \
             mock.patch.object(agent, "standalone_update", return_value={"ok": True, "method": "standalone"}) as up, \
             mock.patch.object(agent, "save_hub"), mock.patch.object(agent, "load_hub", return_value={}):
            out = agent.apply_agent_update({"target_version": "0.1.52"})
        up.assert_called_once_with("0.1.52")
        self.assertEqual(out["method"], "standalone")

    def test_launcher_rolls_back_a_version_that_dies_early(self):
        agent.stage_standalone(agent.extract_agent_files(tarball(addon("0.1.52"))), "0.1.52")
        runs = []

        def run_once(here):
            runs.append(here)
            return 1 if len(runs) == 1 else 0

        with mock.patch.object(launcher, "run_once", side_effect=run_once):
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(runs[0], self.paths["STANDALONE_LIVE"])
        self.assertEqual(runs[1], launcher.IMAGE)  # no previous staged version → the image's code
        self.assertFalse(self.paths["STANDALONE_LIVE"].exists())


if __name__ == "__main__":
    unittest.main()
