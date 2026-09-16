"""Cameras — model, snapshot, ONVIF/generic config flow, HLS path. Agent 0.1.27."""
import base64
import unittest
from unittest import mock

from helpers import agent, build


def by_id(model: dict) -> dict:
    return {e["entity_id"]: e for e in model["entities"]}


class CameraModelTest(unittest.TestCase):
    def test_camera_in_model_without_picture_token(self):
        e = by_id(build())["camera.front"]
        self.assertEqual(e["domain"], "camera")
        self.assertEqual(e["name"], "Είσοδος")
        self.assertEqual(e["state"], "idle")
        self.assertEqual(e["attrs"]["platform"], "onvif")
        self.assertTrue(e["capabilities"]["stream"])
        self.assertNotIn("entity_picture", e["attrs"])
        self.assertNotIn("SECRET", str(e))

    def test_hidden_and_wrong_id_rejected(self):
        self.assertNotIn("arvio.camera_setup", agent.LAN_ALLOWED_ACTIONS)
        self.assertIn("arvio.camera_setup", agent.AGENT_SERVICE_ALLOWLIST)
        with self.assertRaises(ValueError):
            agent.parse_camera_entity_id({"entity_id": "light.x"})
        with self.assertRaises(ValueError):
            agent.parse_camera_entity_id({"entity_id": "camera.Front"})


class CameraSnapshotTest(unittest.TestCase):
    def test_snapshot_resizes_and_never_uses_entity_picture_attr(self):
        jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00" + bytes(64) + b"\xff\xd9"
        )

        def fake_fetch(eid, width, timeout=20):
            self.assertEqual(eid, "camera.front")
            self.assertEqual(width, 480)
            return jpeg, "image/jpeg"

        with mock.patch.object(agent, "fetch_camera_proxy", side_effect=fake_fetch), \
             mock.patch.object(agent, "resize_art", return_value=(b"tinyjpg", "image/jpeg", None)):
            out = agent.camera_snapshot({"entity_id": "camera.front"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertEqual(base64.b64decode(out["data_base64"]), b"tinyjpg")
        self.assertIsNone(out["reason"])


class CameraSetupTest(unittest.TestCase):
    def test_starts_onvif_flow_and_continues(self):
        calls = []

        def fake_ha(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            if path == "/config/config_entries/flow" and method == "POST":
                return {
                    "type": "form",
                    "flow_id": "flow_abc12345",
                    "handler": "onvif",
                    "step_id": "user",
                    "errors": {},
                    "data_schema": [{"name": "auto", "type": "boolean"}],
                }
            if path.endswith("/flow_abc12345") and method == "POST":
                return {"type": "create_entry", "title": "Front - aa:bb"}
            if method == "DELETE":
                return {}
            raise AssertionError(path)

        with mock.patch.object(agent, "ha_or_raise", side_effect=fake_ha):
            start = agent.camera_setup({"handler": "onvif"})
            self.assertEqual(start["phase"], "form")
            self.assertEqual(start["step_id"], "user")
            created = agent.camera_setup({
                "handler": "onvif",
                "flow_id": "flow_abc12345",
                "user_input": {"auto": True},
            })
            self.assertEqual(created["phase"], "created")
            aborted = agent.camera_setup({"handler": "onvif", "flow_id": "flow_abc12345", "abort": True})
            self.assertEqual(aborted["phase"], "abort")
        self.assertEqual(calls[0][1], "/config/config_entries/flow")
        with self.assertRaises(ValueError):
            agent.camera_setup({"handler": "hue"})


class CameraStreamTest(unittest.TestCase):
    def test_hls_path_allowlist(self):
        self.assertEqual(
            agent.camera_safe_ha_path("/api/hls/tok/playlist.m3u8"),
            "/api/hls/tok/playlist.m3u8",
        )
        self.assertIsNone(agent.camera_safe_ha_path("/lovelace"))
        with mock.patch.object(
            agent, "ha_ws_command", return_value={"url": "/api/hls/abc/playlist.m3u8"}
        ):
            out = agent.camera_stream({"entity_id": "camera.front"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["playlist_path"], "/api/hls/abc/playlist.m3u8")
        with mock.patch.object(agent, "ha_ws_command", return_value={"url": "/api/config"}):
            bad = agent.camera_stream({}, "camera.front")
        self.assertFalse(bad["ok"])

    def test_unknown_flow_step_is_unsupported(self):
        with mock.patch.object(
            agent,
            "ha_or_raise",
            return_value={"type": "form", "flow_id": "flow_abc12345", "handler": "onvif", "step_id": "weird"},
        ):
            out = agent.camera_setup({"handler": "onvif"})
        self.assertEqual(out["phase"], "unsupported")

    def test_camera_http_uses_supervisor_not_frontend(self):
        class FakeResp:
            status = 200
            headers = {"Content-Type": "image/jpeg"}

            def read(self, _n):
                return b"jpegbytes"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(agent, "TOKEN", "tok", create=True), \
             mock.patch.object(agent.urllib.request, "urlopen", return_value=FakeResp()) as opener:
            status, headers, cookies, raw = agent.ha_ui_http_via_core(
                "GET", "/api/camera_proxy/camera.front", "width=480", {}, b""
            )
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"jpegbytes")
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(cookies, [])
        url = opener.call_args[0][0]
        self.assertTrue(str(url.full_url).startswith("http://supervisor/core/api/camera_proxy/camera.front"))
        self.assertNotIn("homeassistant:8123", str(url.full_url))
