"""Wall-panel installer (arvio docs/ADR/012): scan, identify, install, pair, close ADB — with a fake adb."""
from __future__ import annotations

import ipaddress
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import agent

import panel_install as pi

IP = "192.168.68.76"
TARGET = f"{IP}:5555"
# What panel .76 (NSPanel Pro, firmware 2.3.0) answers to IDENTIFY_SCRIPT (S0, 2026-11-02).
NSPANEL = "\n".join(
    [
        "manufacturer=rockchip",
        "model=px30_evb",
        "android=8.1.0",
        "sdk=27",
        "serial=F110812302021103600",
        "mac=88:12:ac:d0:18:4a",
        "sonoff=package:/system/priv-app/eWeLinkControlPanel/eWeLinkControlPanel.apk",
        "app=",
        "webview=  Current WebView package (name, version): (com.android.webview, 107.0.5304.105)",
    ]
)
SERIAL_HASH = pi.serial_hash("F110812302021103600")


class FakeAdb:
    """Answers by the first matching fragment of the command line; records every call."""

    def __init__(self, answers: dict | None = None):
        self.calls: list[str] = []
        self.answers = {
            "connect": (0, f"connected to {TARGET}"),
            "get-state": (0, "device"),
            "manufacturer=": (0, NSPANEL),
            "install -r -g": (0, "Performing Streamed Install\nSuccess"),
            "set-device-owner": (0, "Success: Device owner set to package systems.arvio.panel"),
            **(answers or {}),
        }

    def __call__(self, *args, serial=None, timeout=30):
        line = " ".join(args)
        self.calls.append(line)
        for key, answer in self.answers.items():
            if key in line:
                return answer(line) if callable(answer) else answer
        return 0, ""

    def available(self) -> bool:
        return True

    def ran(self, fragment: str) -> bool:
        return any(fragment in c for c in self.calls)


class IdentifyTest(unittest.TestCase):
    def test_nspanel_pro_from_its_own_answers(self):
        info = pi.identify(FakeAdb(), IP)
        self.assertEqual(info["state"], "device")
        self.assertEqual(info["label"], "NSPanel Pro")
        self.assertEqual((info["android"], info["sdk"], info["webview"]), ("8.1.0", 27, "107.0.5304.105"))
        self.assertTrue(info["supported"])
        self.assertIsNone(info["arvio_app"])
        self.assertEqual(info["serial_hash"], SERIAL_HASH)
        self.assertNotIn("F110812302021103600", json.dumps(pi.public(info)))  # the raw serial stays on the hub

    def test_unauthorized_waits_for_a_tap(self):
        info = pi.identify(FakeAdb({"get-state": (1, "error: device unauthorized.")}), IP)
        self.assertEqual(info, {"ip": IP, "state": "unauthorized"})

    def test_generic_tablet_with_old_webview_is_not_supported(self):
        old = NSPANEL.replace("rockchip", "teclast").replace("px30_evb", "P80X")
        old = old.replace("sonoff=package:/system/priv-app/eWeLinkControlPanel/eWeLinkControlPanel.apk", "sonoff=")
        old = old.replace("107.0.5304.105", "61.0.3163.98")
        info = pi.identify(FakeAdb({"manufacturer=": (0, old)}), IP)
        self.assertEqual(info["label"], "Teclast P80X")
        self.assertFalse(info["supported"])

    def test_app_version_when_arvio_is_there(self):
        info = pi.identify(FakeAdb({"manufacturer=": (0, NSPANEL.replace("app=", "app=    versionName=0.1.0"))}), IP)
        self.assertEqual(info["arvio_app"], "0.1.0")


class NetworkTest(unittest.TestCase):
    def test_lan_ip_by_deployment(self):
        sup = {"result": "ok", "data": {"interfaces": [
            {"interface": "eth1", "primary": False, "ipv4": {"address": ["10.0.0.5/24"]}},
            {"interface": "eth0", "primary": True, "ipv4": {"address": ["192.168.1.10/22"]}},
        ]}}
        # HAOS add-on (bridge network): the Supervisor knows the host
        self.assertEqual(pi.lan_interface(sup, "", "", "172.30.33.4"), ipaddress.ip_interface("192.168.1.10/22"))
        # an explicit ARVIO_LAN_IP wins over guessing
        self.assertEqual(str(pi.lan_interface(None, "http://192.168.68.77:8123", "192.168.68.80", "172.17.0.2").ip), "192.168.68.80")
        # NAS sidecar next to HA Container (bridge): HA's LAN address is the host
        self.assertEqual(str(pi.lan_interface(None, "http://192.168.68.77:8123", "", "172.17.0.2").ip), "192.168.68.77")
        # Arvio Core on host network: the core is on loopback, the route tells the LAN address
        self.assertEqual(str(pi.lan_interface(None, "http://127.0.0.1:8098", "", "192.168.68.77").ip), "192.168.68.77")
        self.assertIsNone(pi.lan_interface(None, "http://127.0.0.1:8098", "", None))

    def test_scan_network_stays_private_and_small(self):
        lan = ipaddress.ip_interface("192.168.68.77/24")
        self.assertEqual(str(pi.scan_network(None, lan)), "192.168.68.0/24")
        self.assertEqual(str(pi.scan_network(None, ipaddress.ip_interface("192.168.68.77/16"))), "192.168.68.0/22")
        self.assertEqual(str(pi.scan_network("192.168.68.0/22", lan)), "192.168.68.0/22")
        for bad in ("8.8.8.0/24", "10.0.0.0/8", "192.168.68.76", "nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                pi.scan_network(bad, lan)
        with self.assertRaises(ValueError):
            pi.scan_network(None, None)

    def test_scan_hosts_returns_only_open_ports(self):
        net = ipaddress.ip_network("192.168.68.0/29")
        seen = []
        hosts = pi.scan_hosts(net, probe=lambda ip, port: seen.append(ip) or ip in ("192.168.68.3", "192.168.68.6"))
        self.assertEqual(hosts, ["192.168.68.3", "192.168.68.6"])
        self.assertNotIn("192.168.68.0", seen)
        self.assertNotIn("192.168.68.7", seen)

    def test_hub_address_seen_by_the_panel(self):
        # /proc/net/tcp6 + tcp on panel .76 with the Mac (.75) connected; plus a listener, a
        # closing socket and an IPv4 hub (.77) in /proc/net/tcp.
        proc = (
            "   0: 00000000000000000000000000000000:15B3 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  2000        0 19023 1\n"
            "   2: 0000000000000000FFFF00004C44A8C0:15B3 0000000000000000FFFF00004B44A8C0:E80E 01 00000021:00000000 01:00000014 00000000  2000        0 12856259 2\n"
            "   3: 0000000000000000FFFF00004C44A8C0:15B3 0000000000000000FFFF00004E44A8C0:E80F 06 00000000:00000000 00:00000000 00000000  2000        0 0 2\n"
            "   1: 4C44A8C0:15B3 4D44A8C0:A232 01 00000000:00000000 00:00000000 00000000  2000        0 1 1\n"
        )
        self.assertEqual(
            pi.seen_from_panel(FakeAdb({"/proc/net/tcp": (0, proc)}), IP), ["192.168.68.75", "192.168.68.77"]
        )


class InstallTest(unittest.TestCase):
    def run_install(self, adb, *, paired=lambda: True, still_open=False):
        steps, tokens = [], []
        out = pi.install(
            adb,
            IP,
            serial_hash=SERIAL_HASH,
            apk=Path("/app/panel-app/arvio-panel.apk"),
            hub_url="http://192.168.68.77:8099",
            hub_id="hub_8593ab2d",
            mint_token=lambda: tokens.append("T" * 43) or tokens[-1],
            paired=paired,
            step=steps.append,
            probe=lambda ip, port: still_open,
            sleep=lambda s: None,
        )
        return out, steps

    def test_installs_as_device_owner_pairs_and_closes_adb(self):
        adb = FakeAdb()
        out, steps = self.run_install(adb)
        self.assertEqual(steps, ["found", "installed", "kiosk", "paired", "adb_closed"])
        self.assertEqual((out["mode"], out["adb_closed"], out["label"]), ("device_owner", True, "NSPanel Pro"))
        self.assertTrue(adb.ran("install -r -g /app/panel-app/arvio-panel.apk"))
        start = next(c for c in adb.calls if "am start" in c)
        self.assertIn("-n systems.arvio.panel/.Main", start)
        self.assertIn("--es hub http://192.168.68.77:8099 --es hub_id hub_8593ab2d --es pair " + "T" * 43, start)
        self.assertTrue(adb.ran("settings put global adb_enabled 0"))
        self.assertFalse(adb.ran("set-home-activity"))

    def test_falls_back_to_launcher_when_android_refuses_device_owner(self):
        adb = FakeAdb({"set-device-owner": (255, "java.lang.IllegalStateException: Not allowed to set the device owner because there are already some accounts on the device")})
        out, _ = self.run_install(adb)
        self.assertEqual(out["mode"], "launcher")
        self.assertTrue(adb.ran("cmd package set-home-activity systems.arvio.panel/.Main"))

    def test_another_panel_at_the_same_ip_is_refused(self):
        adb = FakeAdb({"manufacturer=": (0, NSPANEL.replace("F110812302021103600", "OTHER"))})
        with self.assertRaises(pi.InstallError) as e:
            self.run_install(adb)
        self.assertEqual(e.exception.code, "panel_changed")
        self.assertFalse(adb.ran("install"))

    def test_unauthorized_panel_is_not_touched(self):
        adb = FakeAdb({"get-state": (1, "error: device unauthorized.")})
        with self.assertRaises(pi.InstallError) as e:
            self.run_install(adb)
        self.assertEqual(e.exception.code, "adb_unauthorized")
        self.assertFalse(adb.ran("install"))

    def test_android_install_failure_keeps_its_code(self):
        adb = FakeAdb({"install -r -g": (1, "Failure [INSTALL_FAILED_OLDER_SDK: Requires newer sdk version #26]")})
        with self.assertRaises(pi.InstallError) as e:
            self.run_install(adb)
        self.assertEqual(e.exception.code, "install_failed_older_sdk")
        self.assertFalse(adb.ran("am start"))

    def test_pair_timeout_leaves_adb_for_the_retry_window(self):
        adb = FakeAdb()
        with self.assertRaises(pi.InstallError) as e, mock.patch.object(pi, "PAIR_WAIT_S", 0):
            self.run_install(adb, paired=lambda: False)
        self.assertEqual(e.exception.code, "pair_timeout")
        self.assertFalse(adb.ran("adb_enabled 0"))

    def test_adb_that_will_not_close_is_reported_not_hidden(self):
        out, steps = self.run_install(FakeAdb(), still_open=True)
        self.assertFalse(out["adb_closed"])
        self.assertEqual(steps[-1], "adb_open")


class AgentWiringTest(unittest.TestCase):
    """Relay-only actions, the screens.json side and the background job."""

    def setUp(self):
        for p in (agent.SCREENS,):
            if p.exists():
                p.unlink()
        agent.PANEL_JOBS.update(scan=None, installs={})
        agent.put_wall_screen(
            {
                "screen_id": "scr_hall000000000000",
                "site_id": "site_1",
                "name": "Χολ",
                "hardware": "nspanel_pro",
                "orientation": "square",
                "pages": [{"id": "pg_hall0000", "tiles": [{"kind": "clock"}]}],
            }
        )
        self.apk_dir = Path(tempfile.mkdtemp())
        (self.apk_dir / "panel-app").mkdir()
        (self.apk_dir / "panel-app" / "arvio-panel.apk").write_bytes(b"PK")

    def tearDown(self):
        shutil.rmtree(self.apk_dir, ignore_errors=True)
        if agent.SCREENS.exists():
            agent.SCREENS.unlink()

    def test_an_image_without_adb_says_so(self):
        self.assertFalse(pi.Adb(self.apk_dir, binary="no-such-adb-binary").available())
        no_adb = FakeAdb()
        no_adb.available = lambda: False
        with mock.patch.multiple(agent, PANEL_ADB=no_adb, APP=self.apk_dir):
            for action, payload in (("arvio.panel_scan", {}), ("arvio.panel_install", {"serial_hash": SERIAL_HASH, "screen_id": "scr_hall000000000000"})):
                self.assertEqual(agent.execute_action(action, "", payload)["error"], "no_adb", action)
        self.assertEqual(no_adb.calls, [])

    def test_panel_actions_are_relay_only(self):
        for action in ("arvio.panel_scan", "arvio.panel_install", "arvio.panel_identify", "arvio.panel_status"):
            self.assertIn(action, agent.AGENT_SERVICE_ALLOWLIST)
            self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS)
            with self.assertRaises(agent.CommandRejected) as e:
                agent.check_command_safety({"action": action}, via="lan")
            self.assertEqual(e.exception.code, "lan_forbidden")

    def wait_job(self, key):
        for _ in range(200):
            job = agent.panel_status()["installs" if key != "scan" else "scan"]
            job = job if key == "scan" else next((j for j in job if j["serial_hash"] == key), None)
            if job and job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_scan_then_install_pairs_the_screen_and_records_the_device(self):
        adb = FakeAdb()

        def am_start(line):
            # the app opens /panel#pair=<token>: panel.html posts it, the hub pairs the screen
            token = line.rsplit(" ", 1)[-1]
            agent.pair_wall_screen(token)
            return 0, "Starting: Intent { cmp=systems.arvio.panel/.Main (has extras) }"

        adb.answers["am start"] = am_start
        with mock.patch.multiple(
            agent, PANEL_ADB=adb, APP=self.apk_dir, hub_id="hub_8593ab2d",
            panel_lan=mock.Mock(return_value=ipaddress.ip_interface("192.168.68.77/24")),
        ), mock.patch.object(pi, "scan_hosts", return_value=[IP]), mock.patch.object(
            pi, "tcp_open", return_value=False
        ), mock.patch.object(pi, "PAIR_POLL_S", 0.01):
            out = agent.execute_action("arvio.panel_scan", "", {})
            self.assertTrue(out["ok"])
            scan = self.wait_job("scan")
            self.assertEqual(scan["devices"][0]["label"], "NSPanel Pro")
            out = agent.execute_action("arvio.panel_install", "", {"serial_hash": SERIAL_HASH, "screen_id": "scr_hall000000000000"})
            self.assertTrue(out["ok"])
            job = self.wait_job(SERIAL_HASH)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["steps"], ["found", "installed", "kiosk", "paired", "adb_closed"])
        start = next(c for c in adb.calls if "am start" in c)
        self.assertIn("--es hub http://192.168.68.77:8099", start)
        row = agent.load_screens()["screens"]["scr_hall000000000000"]
        self.assertTrue(row["screen_secret_hash"])
        self.assertEqual(row["device"]["serial_hash"], SERIAL_HASH)
        public = agent.public_screen(row)["device"]
        self.assertEqual((public["label"], public["mode"], public["adb_closed"]), ("NSPanel Pro", "device_owner", True))
        self.assertNotIn("mac", public)
        # a later Partner layout save keeps what the hub learned about the device
        agent.put_wall_screen({**{k: row[k] for k in ("screen_id", "site_id", "name", "hardware", "orientation", "pages")}})
        self.assertEqual(agent.load_screens()["screens"]["scr_hall000000000000"]["device"]["mode"], "device_owner")

    def test_install_needs_a_scanned_panel_a_screen_and_the_apk(self):
        with mock.patch.multiple(agent, PANEL_ADB=FakeAdb(), APP=self.apk_dir):
            out = agent.execute_action("arvio.panel_install", "", {"serial_hash": SERIAL_HASH, "screen_id": "scr_hall000000000000"})
            self.assertEqual(out["error"], "panel_not_scanned")
            agent.PANEL_JOBS["scan"] = {"state": "done", "devices": [{"ip": IP, "state": "device", "serial_hash": SERIAL_HASH}]}
            out = agent.execute_action("arvio.panel_install", "", {"serial_hash": SERIAL_HASH, "screen_id": "scr_missing000000000"})
            self.assertEqual(out["error"], "screen_not_found")
        with mock.patch.multiple(agent, PANEL_ADB=FakeAdb(), APP=Path(tempfile.gettempdir()) / "no-such-dir"):
            out = agent.execute_action("arvio.panel_install", "", {"serial_hash": SERIAL_HASH, "screen_id": "scr_hall000000000000"})
            self.assertEqual(out["error"], "no_apk")

    def test_failed_install_closes_adb_after_the_retry_window(self):
        adb = FakeAdb({"install -r -g": (1, "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]")})
        agent.PANEL_JOBS["scan"] = {"state": "done", "devices": [{"ip": IP, "state": "device", "serial_hash": SERIAL_HASH}]}
        timers = []

        class Timer:
            def __init__(self, delay, fn, args=()):
                timers.append((delay, fn, args))

            def start(self):
                pass

            def cancel(self):
                pass

        with mock.patch.multiple(agent, PANEL_ADB=adb, APP=self.apk_dir), mock.patch.object(agent.threading, "Timer", Timer), mock.patch.object(pi, "tcp_open", return_value=False):
            agent.execute_action("arvio.panel_install", "", {"serial_hash": SERIAL_HASH, "screen_id": "scr_hall000000000000"})
            job = self.wait_job(SERIAL_HASH)
            self.assertEqual((job["state"], job["error"]), ("failed", "install_failed_insufficient_storage"))
            self.assertEqual(len(timers), 1)
            delay, fn, args = timers[0]
            self.assertEqual(delay, pi.RETRY_WINDOW_S)
            self.assertFalse(adb.ran("adb_enabled 0"))
            fn(*args)
        self.assertTrue(adb.ran("settings put global adb_enabled 0"))

    def test_daily_check_reports_adb_opened_again(self):
        agent.set_screen_device("scr_hall000000000000", {"label": "NSPanel Pro", "ip": IP, "serial_hash": SERIAL_HASH,
                                                          "mode": "device_owner", "adb_closed": True})
        with mock.patch.object(pi, "tcp_open", return_value=True) as probe:
            agent.panel_watch_once()
        probe.assert_called_once_with(IP, 5555)
        device = agent.public_screen(agent.load_screens()["screens"]["scr_hall000000000000"])["device"]
        self.assertTrue(device["adb_open"])
        self.assertTrue(device["adb_checked_at"])
        known = agent.panel_status()["known"]
        self.assertEqual(known, [{"screen_id": "scr_hall000000000000", "name": "Χολ", "label": "NSPanel Pro",
                                  "mode": "device_owner", "adb_open": True, "adb_checked_at": device["adb_checked_at"]}])
        with mock.patch.object(pi, "tcp_open", return_value=False):
            agent.panel_watch_once()
        self.assertFalse(agent.panel_status()["known"][0]["adb_open"])


if __name__ == "__main__":
    unittest.main()
