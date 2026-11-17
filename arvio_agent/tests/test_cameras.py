"""Cameras — model, snapshot, ONVIF/generic config flow, HLS path. Agent 0.1.30."""
import base64
import unittest
from unittest import mock

import helpers
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


class CameraPtzTest(unittest.TestCase):
    """camera.ptz (Arvio Core's ONVIF driver, 15/11): fields checked, the model says what the camera can do."""

    def ptz_states(self, **attrs):
        states = [s for s in helpers.STATES if s["entity_id"] != "camera.front"]
        states.append(
            helpers.st("camera.front", "idle", friendly_name="Είσοδος", supported_features=2, **attrs)
        )
        return by_id(build(states=states))["camera.front"]

    def test_model_says_ptz_and_presets(self):
        e = self.ptz_states(
            ptz_modes=["ContinuousMove", "RelativeMove", "GotoPreset", "Stop", "Wobble"],
            ptz_zoom=True,
            ptz_presets=[{"token": "1", "name": "Είσοδος"}, {"token": "", "name": "x"}, "bad", {"token": "2"}],
        )
        self.assertTrue(e["capabilities"]["ptz"])
        self.assertEqual(e["attrs"]["ptz_modes"], ["ContinuousMove", "RelativeMove", "GotoPreset", "Stop"])
        self.assertTrue(e["attrs"]["ptz_zoom"])
        self.assertEqual(e["attrs"]["ptz_presets"], [{"token": "1", "name": "Είσοδος"}, {"token": "2", "name": "2"}])

    def test_fixed_camera_has_no_ptz(self):
        e = by_id(build())["camera.front"]
        self.assertFalse(e["capabilities"]["ptz"])
        self.assertNotIn("ptz_modes", e["attrs"])

    def test_service_data_only_known_fields(self):
        self.assertEqual(
            agent.build_service_data("camera.ptz", "camera.front", {"pan": "LEFT", "speed": 0.5, "client_command_id": "x", "evil": 1}),
            ("ptz", {"entity_id": "camera.front", "move_mode": "ContinuousMove", "pan": "LEFT", "speed": 0.5}),
        )
        self.assertEqual(
            agent.build_service_data("camera.ptz", "camera.front", {"move_mode": "GotoPreset", "preset": "2"})[1]["preset"], "2"
        )
        for bad in ({"pan": "UP"}, {"move_mode": "ContinuousMove"}, {"pan": "LEFT", "speed": 2}, {"pan": "LEFT", "speed": True},
                    {"move_mode": "GotoPreset"}, {"move_mode": "AbsoluteMove", "pan": "LEFT"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                agent.build_service_data("camera.ptz", "camera.front", bad)

    def test_relay_only(self):
        self.assertIn("camera.ptz", agent.AGENT_SERVICE_ALLOWLIST)
        self.assertNotIn("camera.ptz", agent.LAN_ALLOWED_ACTIONS)


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


class RecordingTest(unittest.TestCase):
    """arvio.recording (event clips on Arvio Core, agent 0.1.105): only known fields reach Core, never LAN."""

    CLIP = "/api/clips/camera.front/20261116T120000-0123abcd"

    def test_clip_paths(self):
        self.assertEqual(agent.camera_safe_ha_path(f"{self.CLIP}/index.m3u8"), f"{self.CLIP}/index.m3u8")
        self.assertEqual(agent.camera_safe_ha_path(f"{self.CLIP}/012.ts"), f"{self.CLIP}/012.ts")
        for bad in (f"{self.CLIP}/clip.json", f"{self.CLIP}/index.m3u8?x=1", "/api/clips/camera.front/../index.m3u8", f"{self.CLIP}"):
            self.assertIsNone(agent.camera_safe_ha_path(bad), bad)
        replay = "/api/replay/camera.front/0123456789abcdef"
        self.assertEqual(agent.camera_safe_ha_path(f"{replay}/003.ts"), f"{replay}/003.ts")
        self.assertIsNone(agent.camera_safe_ha_path(f"{replay}/poster.jpg"))
        self.assertIsNone(agent.camera_safe_ha_path("/api/replay/camera.front/xyz/index.m3u8"))
        archive = "/api/archive/camera.front/1763287200000"
        self.assertEqual(agent.camera_safe_ha_path(f"{archive}/20261116-100000.ts"), f"{archive}/20261116-100000.ts")
        self.assertIsNone(agent.camera_safe_ha_path(f"{archive}/x.ts"))

    def test_forwards_known_fields_only(self):
        self.assertNotIn("arvio.recording", agent.LAN_ALLOWED_ACTIONS)
        self.assertIn("arvio.recording", agent.AGENT_SERVICE_ALLOWLIST)
        with mock.patch.object(agent, "ha_ws_command", return_value={"clips": []}) as ws:
            out = agent.recording({"op": "clips", "entity_id": "camera.front", "id": 9, "type": "auth"})
        self.assertEqual(out, {"ok": True, "result": {"clips": []}})
        ws.assert_called_once_with("arvio/recording", {"op": "clips", "entity_id": "camera.front"}, timeout=30.0)
        self.assertEqual(agent.recording({"op": "rm -rf"}), {"ok": False, "error": "bad_op"})
        with mock.patch.object(agent, "ha_ws_command", return_value={"path": "/api/replay/x"}) as ws:
            agent.recording({"op": "replay", "entity_id": "camera.front", "from": 1, "to": 2})
        ws.assert_called_once_with("arvio/recording", {"op": "replay", "entity_id": "camera.front", "from": 1, "to": 2}, timeout=30.0)

    def test_core_refusal_and_home_assistant(self):
        with mock.patch.object(agent, "ha_ws_command", side_effect=RuntimeError("serial_mismatch")):
            self.assertEqual(agent.recording({"op": "format", "path": "/dev/sda", "last4": "0000"}), {"ok": False, "error": "serial_mismatch"})
        with mock.patch.object(agent, "ha_ws_command", side_effect=RuntimeError("Unknown command.")):
            self.assertEqual(agent.recording({"op": "status"}), {"ok": False, "error": "not_supported"})


class CloudClipsTest(unittest.TestCase):
    """Arvio Cloud copies: the cloud's go-ahead, Core's upload, the cloud told; stops without the add-on."""

    def test_round_uploads_and_commits(self):
        calls = []

        def ws(t, extra, timeout=0):
            calls.append(("ws", extra["op"]))
            if extra["op"] == "cloud_pending":
                return {"clips": [{"clip_id": "20261116T120000-0123abcd", "entity_id": "camera.front", "cause": "person", "at": 5, "size": 1008}]}
            self.assertEqual(extra["put_url"], "https://bucket/x")
            return {"segments": [{"d": 2, "len": 1008}]}

        def call(path, body, timeout=0):
            calls.append(("cloud", path))
            if path == "clips/upload":
                return 200, {"put_url": "https://bucket/x", "key_hex": "00" * 16}
            self.assertEqual(body["segments"], [{"d": 2, "len": 1008}])
            self.assertEqual(body["cause"], "person")
            return 200, {"ok": True}

        self.assertEqual(agent.cloud_clips_round(ws, call), agent.CLOUD_CLIPS_EVERY_S)
        self.assertEqual(calls, [("ws", "cloud_pending"), ("cloud", "clips/upload"), ("ws", "cloud_upload"), ("cloud", "clips/commit")])

    def test_no_addon_or_no_recorder_waits_an_hour(self):
        pending = lambda t, e, timeout=0: {"clips": [{"clip_id": "x", "entity_id": "camera.front", "size": 1}]}
        self.assertEqual(agent.cloud_clips_round(pending, lambda p, b, timeout=0: (402, {"error": "not_entitled"})), agent.CLOUD_CLIPS_IDLE_S)
        def ha(t, e, timeout=0):
            raise RuntimeError("Unknown command.")
        self.assertEqual(agent.cloud_clips_round(ha, lambda p, b, timeout=0: (200, {})), agent.CLOUD_CLIPS_IDLE_S)
        self.assertEqual(agent.cloud_clips_round(pending, lambda p, b, timeout=0: (0, None)), agent.CLOUD_CLIPS_RETRY_S)

    def test_cloud_247_round(self):
        calls = []
        names = ["20261116-090000.ts", "20261116-090100.ts"]

        def ws(t, extra, timeout=0):
            calls.append(("ws", extra["op"]))
            if extra["op"] == "cloud_rec_pending":
                return {"chunks": [{"entity_id": "camera.gate", "names": names, "start": 1, "end": 120001, "size": 1024}]}
            self.assertEqual((extra["names"], extra["put_url"]), (names, "https://bucket/r"))
            return {"segments": [{"d": 60, "len": 512}, {"d": 60, "len": 512}]}

        def call(path, body, timeout=0):
            calls.append(("cloud", path))
            if path == "recordings/upload":
                self.assertEqual(body, {"entity_id": "camera.gate", "start": 1, "size": 1024})
                return 200, {"put_url": "https://bucket/r", "key_hex": "00" * 16}
            self.assertEqual((body["end"], len(body["segments"])), (120001, 2))
            return 200, {"ok": True}

        self.assertEqual(agent.cloud_rec_round(ws, call), agent.CLOUD_REC_SENT_S, "sent: look again soon, a backlog drains")
        self.assertEqual(agent.cloud_rec_round(lambda t, e, timeout=0: {"chunks": []}, call), agent.CLOUD_REC_EVERY_S, "nothing ready: 15″")
        self.assertEqual(calls, [("ws", "cloud_rec_pending"), ("cloud", "recordings/upload"), ("ws", "cloud_rec_upload"), ("cloud", "recordings/commit")])
        pending = lambda t, e, timeout=0: {"chunks": [{"entity_id": "camera.gate", "names": names, "start": 1, "end": 2, "size": 1}]}
        self.assertEqual(agent.cloud_rec_round(pending, lambda p, b, timeout=0: (402, {})), agent.CLOUD_CLIPS_IDLE_S)

    def test_archive_byte_range_passes_through(self):
        seen = {}

        class Resp:
            status = 206
            headers = {"Content-Type": "video/mp2t", "Content-Range": "bytes 0-9/100", "Accept-Ranges": "bytes"}

            def read(self, n):
                return b"0123456789"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=0):
            seen["range"] = req.get_header("Range")
            return Resp()

        path = "/api/archive/camera.front/1794898290000/20261117-065100.ts"
        with mock.patch.object(agent, "TOKEN", "t"), mock.patch.object(agent.urllib.request, "urlopen", opener):
            status, headers, _, body = agent.ha_ui_http_via_core("GET", path, "", {"range": "bytes=0-9"}, b"")
            self.assertEqual((status, headers.get("content-range"), body, seen["range"]), (206, "bytes 0-9/100", b"0123456789", "bytes=0-9"))
            agent.ha_ui_http_via_core("GET", path, "", {"Range": "bytes=0-9, 20-30"}, b"")
            self.assertIsNone(seen["range"], "only one plain range")

    def test_never_from_the_relay(self):
        self.assertEqual(agent.recording({"op": "cloud_upload", "put_url": "https://evil", "clip_id": "x"}), {"ok": False, "error": "bad_op"})
        self.assertEqual(agent.recording({"op": "cloud_rec_upload", "put_url": "https://evil"}), {"ok": False, "error": "bad_op"})
