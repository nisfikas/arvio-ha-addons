"""Agent 0.1.21 «Μουσική» (§15): model fields, art, browse/search, services, volume_max, push, ownership, version."""
import base64
import hashlib
import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import (
    CAST_SPEAKER_FEATURES,
    MA_SPEAKER_FEATURES,
    ROOT,
    SPEAKER_PICTURE,
    STATES,
    agent,
    build,
    regs,
    st,
)

FUTURE = "2999-01-01T00:00:00Z"
MA_ENTRIES = [
    {"entry_id": "zha1", "domain": "zha", "disabled_by": None},
    {"entry_id": "ma_old", "domain": "music_assistant", "disabled_by": "user"},
    {"entry_id": "ma1", "domain": "music_assistant", "disabled_by": None},
]


def by_id(model: dict) -> dict:
    return {e["entity_id"]: e for e in model["entities"]}


class MediaFakeHa:
    """agent.ha() stand-in that flips media states / attributes on service calls."""

    def __init__(self, states: dict, entries=None, response=None):
        self.states = {eid: {"entity_id": eid, "state": s, "attributes": dict(a)} for eid, (s, a) in states.items()}
        self.calls = []
        self.reads = []
        self.entries = entries if entries is not None else []
        self.response = response

    def __call__(self, path, method="GET", body=None, timeout=10):
        if path.startswith("/services/"):
            self.calls.append((path, body))
            base = path.split("?", 1)[0]
            svc = base.rsplit("/", 1)[1]
            eid = body.get("entity_id") if isinstance(body, dict) else None
            s = self.states.get(eid)
            changed = []
            if s is not None:
                if svc == "volume_set":
                    s["attributes"]["volume_level"] = body["volume_level"]
                elif svc == "select_source":
                    s["attributes"]["source"] = body["source"]
                elif svc == "volume_mute":
                    s["attributes"]["is_volume_muted"] = body["is_volume_muted"]
                elif svc == "media_play":
                    s["state"] = "playing"
                elif svc == "media_pause":
                    s["state"] = "paused"
                elif svc == "turn_off":
                    s["state"] = "off"
                changed.append({**s, "context": {"id": "ctx_media"}})
            if "return_response" in path:
                return {"changed_states": changed, "service_response": self.response}
            return changed
        if path == "/states":
            self.reads.append(path)
            return list(self.states.values())
        if path.startswith("/states/"):
            self.reads.append(path)
            return self.states.get(path[len("/states/"):])
        if path == "/config":
            return {"version": "2025.8.1", "time_zone": "Europe/Athens"}
        if path == "/config/config_entries/entry":
            return self.entries
        return None


def media_states() -> dict:
    return {
        "media_player.saloni": ("playing", {
            "friendly_name": "Ηχείο σαλονιού", "device_class": "speaker", "volume_level": 0.35, "source": "Spotify",
            "supported_features": MA_SPEAKER_FEATURES, "media_content_id": "spotify://track/abc",
            "entity_picture": SPEAKER_PICTURE, "is_volume_muted": False,
        }),
        "media_player.kouzina": ("paused", {"device_class": "speaker", "volume_level": 0.2, "supported_features": CAST_SPEAKER_FEATURES}),
        "media_player.tv": ("off", {}),
    }


class FakePillow:
    """Minimal PIL.Image stand-in: `save` size depends on the JPEG quality."""

    def __init__(self, sizes: dict):
        self.sizes = sizes
        self.opened = []
        self.thumbnails = []
        self.qualities = []
        outer = self

        class Img:
            def thumbnail(self_, box):
                outer.thumbnails.append(box)

            def convert(self_, mode):
                return self_

            def save(self_, buf, format, quality, optimize):
                outer.qualities.append(quality)
                buf.write(b"\xff\xd8\xff" + b"J" * outer.sizes[quality])

        self.Img = Img

    def open(self, fp):
        data = fp.read()
        if data.startswith(b"BROKEN"):
            raise OSError("cannot identify image file")
        self.opened.append(data)
        return self.Img()


# --- 1. version pins -----------------------------------------------------------------------


class VersionPinTest(unittest.TestCase):
    def test_three_places_agree(self):
        self.assertEqual(agent.AGENT_VERSION, "0.1.37")
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('\nversion: "0.1.37"\n', cfg)
        docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('io.hass.version="0.1.37"', docker)
        self.assertIn("COPY panel.html", docker)
        self.assertRegex(docker, r"pillow", "Pillow must be installed for the art resize path")
        self.assertIn("0.1.21", (ROOT / "DOCS.md").read_text(encoding="utf-8"))


# --- 2. model: media_player domain, attrs subset, art_hash, ma_available -----------------------


class MediaModelTest(unittest.TestCase):
    def test_media_entities_and_attrs_subset(self):
        m = build()
        e = by_id(m)
        self.assertIn("media_player", agent.HOME_ENTITY_DOMAINS)
        sp = e["media_player.saloni"]
        self.assertEqual(sp["domain"], "media_player")
        self.assertEqual(sp["device_class"], "speaker")
        self.assertEqual(sp["area_id"], "saloni")
        self.assertEqual(sp["supported_features"], MA_SPEAKER_FEATURES)
        a = sp["attrs"]
        expected_keys = {
            "media_title", "media_artist", "media_album_name", "app_name", "media_content_id", "media_content_type",
            "media_duration", "media_position", "media_position_updated_at", "volume_level", "is_volume_muted",
            "source", "source_list", "group_members", "shuffle", "repeat", "supported_features", "platform", "art_hash",
        }
        self.assertEqual(set(a), expected_keys)
        self.assertEqual(a["media_title"], "Blue in Green")
        self.assertEqual(a["media_artist"], "Miles Davis")
        self.assertEqual(a["media_album_name"], "Kind of Blue")
        self.assertEqual(a["app_name"], "Spotify")
        self.assertEqual(a["media_content_id"], "spotify://track/abc")
        self.assertEqual(a["media_content_type"], "music")
        self.assertEqual(a["media_duration"], 337)
        self.assertEqual(a["media_position"], 42.5)
        self.assertEqual(a["media_position_updated_at"], "2026-09-11T10:00:00+00:00")
        self.assertEqual(a["volume_level"], 0.35)
        self.assertIs(a["is_volume_muted"], False)
        self.assertEqual(a["source_list"], ["Spotify", "Radio"])
        self.assertEqual(a["group_members"], ["media_player.saloni", "media_player.kouzina"])
        self.assertIs(a["shuffle"], False)
        self.assertEqual(a["repeat"], "off")
        self.assertEqual(a["supported_features"], MA_SPEAKER_FEATURES)
        self.assertEqual(a["platform"], "music_assistant")  # entity registry `pl`
        self.assertEqual(a["art_hash"], hashlib.sha1(("spotify://track/abc" + SPEAKER_PICTURE).encode()).hexdigest())
        self.assertNotIn("entity_picture", a)  # never the raw (token-bearing) URL
        caps = sp["capabilities"]
        self.assertTrue(caps["search"]); self.assertTrue(caps["browse"]); self.assertTrue(caps["grouping"])
        self.assertTrue(caps["volume"]); self.assertTrue(caps["next_previous"])
        # cast speaker: no picture → art_hash null; platform from registry
        ko = e["media_player.kouzina"]
        self.assertIsNone(ko["attrs"]["art_hash"])
        self.assertEqual(ko["attrs"]["platform"], "cast")
        self.assertFalse(ko["capabilities"]["search"])
        self.assertEqual(ko["attrs"]["source_list"], [])
        # no registry entry, no device_class → still exposed (null device_class), platform null
        tv = e["media_player.tv"]
        self.assertIsNone(tv["device_class"])
        self.assertIsNone(tv["attrs"]["platform"])
        self.assertIsNone(tv["attrs"]["art_hash"])
        # device_class outside speaker|tv|receiver|null → hidden
        self.assertNotIn("media_player.projector", e)
        for dc in ("speaker", "tv", "receiver", None, ""):
            self.assertTrue(agent.media_player_exposed(dc), dc)
        self.assertFalse(agent.media_player_exposed("projector"))

    def test_art_hash_changes_with_content_or_picture(self):
        h1 = agent.media_art_hash("a", "/api/p?token=1&cache=x")
        self.assertEqual(agent.media_art_hash("a", "/api/p?token=1&cache=x"), h1)
        self.assertNotEqual(agent.media_art_hash("b", "/api/p?token=1&cache=x"), h1)
        self.assertNotEqual(agent.media_art_hash("a", "/api/p?token=1&cache=y"), h1)
        self.assertIsNone(agent.media_art_hash("a", None))
        self.assertIsNone(agent.media_art_hash("a", ""))
        self.assertEqual(len(h1), 40)

    def test_ma_available_in_model_root(self):
        m = build()
        self.assertIs(m["ma_available"], False)
        self.assertIsNone(m["ma_config_entry_id"])
        m2 = build(ma_config_entry_id="ma1")
        self.assertIs(m2["ma_available"], True)
        self.assertEqual(m2["ma_config_entry_id"], "ma1")

    def test_registry_platform_in_both_shapes_and_fingerprint(self):
        disp = agent.normalize_entity_registry({"entity_categories": {}, "entities": [{"ei": "media_player.x", "pl": "sonos"}]})
        lst = agent.normalize_entity_registry([{"entity_id": "media_player.x", "platform": "sonos"}])
        self.assertEqual(disp["media_player.x"]["platform"], "sonos")
        self.assertEqual(lst["media_player.x"]["platform"], "sonos")
        other = agent.normalize_entity_registry([{"entity_id": "media_player.x", "platform": "cast"}])
        self.assertNotEqual(agent.registry_fingerprint([], {}, {}, lst), agent.registry_fingerprint([], {}, {}, other))

    def test_legacy_list_entities_includes_media(self):
        fake = MediaFakeHa({**media_states(), "media_player.projector": ("on", {"device_class": "projector"}),
                            "light.a": ("on", {"friendly_name": "A"})})
        with mock.patch.object(agent, "ha", side_effect=fake):
            items = {i["entity_id"]: i for i in agent.entities()}
        self.assertIn("media_player.saloni", items)
        self.assertIn("light.a", items)
        self.assertNotIn("media_player.projector", items)
        self.assertEqual(items["media_player.saloni"]["volume_level"], 0.35)
        self.assertEqual(items["media_player.saloni"]["media_content_id"], "spotify://track/abc")
        self.assertTrue(items["media_player.saloni"]["art_hash"])


# --- 2b. Music Assistant detection (config entries) ---------------------------------------------


class MaDetectionTest(unittest.TestCase):
    def test_entry_id_from_config_entries(self):
        self.assertEqual(agent.music_assistant_entry_id(MA_ENTRIES), "ma1")  # disabled entry skipped
        self.assertEqual(agent.MA_INFO["entry_id"], "ma1")
        self.assertIsNone(agent.music_assistant_entry_id([]))
        self.assertIsNone(agent.MA_INFO["entry_id"])
        self.assertIsNone(agent.music_assistant_entry_id([{"domain": "music_assistant", "disabled_by": "user", "entry_id": "x"}]))
        fake = MediaFakeHa({}, entries=MA_ENTRIES)
        with mock.patch.object(agent, "ha", side_effect=fake):
            self.assertEqual(agent.music_assistant_entry_id(), "ma1")  # reads /config/config_entries/entry
            self.assertTrue(agent.zha_is_available())  # same read serves ZHA
        with mock.patch.object(agent, "ha", return_value=None):
            self.assertEqual(agent.config_entries(), [])
            self.assertIsNone(agent.music_assistant_entry_id())

    def test_model_fetch_sets_ma_available(self):
        from helpers import AREAS, DEVICES, ENTITY_REGISTRY_DISPLAY, FLOORS

        fake = MediaFakeHa({}, entries=MA_ENTRIES)
        fake.states = {s["entity_id"]: s for s in STATES}
        agent.REGISTRY_CACHE = agent.RegistrySnapshot()
        with mock.patch.object(agent, "ha", side_effect=fake), \
             mock.patch.object(agent, "fetch_registries", return_value={
                 "floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": ENTITY_REGISTRY_DISPLAY}), \
             mock.patch.object(agent, "fetch_arvio_automation_configs", return_value={}), \
             mock.patch.object(agent, "push_model_changed"):
            agent.save_hub({"hub_id": "hub_media"})
            m = agent.execute_action("arvio.model", "", {})
        self.assertIs(m["ma_available"], True)
        self.assertEqual(m["ma_config_entry_id"], "ma1")
        self.assertIn("media_player.saloni", by_id(m))
        fake.entries = []
        with mock.patch.object(agent, "ha", side_effect=fake), \
             mock.patch.object(agent, "fetch_registries", return_value={
                 "floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": ENTITY_REGISTRY_DISPLAY}), \
             mock.patch.object(agent, "fetch_arvio_automation_configs", return_value={}), \
             mock.patch.object(agent, "push_model_changed"):
            m = agent.execute_action("arvio.model", "", {})
        self.assertIs(m["ma_available"], False)


# --- 3. arvio.media_art ---------------------------------------------------------------------------


class MediaArtTest(unittest.TestCase):
    def setUp(self):
        agent.MEDIA_ART_CACHE = agent.LRU(agent.MEDIA_ART_CACHE_SIZE)
        self.fake = MediaFakeHa(media_states())
        self.fetches = []

    def fetch(self, data: bytes, mime="image/png"):
        def _fetch(picture, timeout=10):
            self.fetches.append(picture)
            return data, mime
        return _fetch

    def test_resize_with_pillow_and_quality_step_down(self):
        pil = FakePillow({80: 50_000, 60: 45_000, 40: 30_000})
        out, mime, reason = agent.resize_art(b"\x89PNG\r\n\x1a\n" + b"x" * 200_000, image_mod=pil)
        self.assertIsNone(reason)
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(len(out), agent.MEDIA_ART_WIRE_MAX_BYTES)
        self.assertEqual(pil.thumbnails, [(256, 256)])
        self.assertEqual(pil.qualities, [80, 60, 40])  # steps down until it fits the 40 KB cap
        # first try fits → one encode at quality 80
        pil2 = FakePillow({80: 12_000})
        out, mime, reason = agent.resize_art(b"\xff\xd8\xff" + b"x" * 500, image_mod=pil2)
        self.assertEqual((len(out), mime, reason, pil2.qualities), (12_003, "image/jpeg", None, [80]))
        # cannot get under the cap even at quality 40 → too_large
        pil3 = FakePillow({80: 90_000, 60: 80_000, 40: 70_000})
        self.assertEqual(agent.resize_art(b"x" * 10, image_mod=pil3), (None, None, "too_large"))
        # undecodable → decode_failed
        _, _, reason = agent.resize_art(b"BROKEN", image_mod=FakePillow({80: 1}))
        self.assertTrue(reason.startswith("decode_failed"))

    def test_no_pillow_fallback(self):
        small = b"\xff\xd8\xff" + b"j" * 5_000
        self.assertEqual(agent.resize_art(small, image_mod=False), (small, "image/jpeg", None))
        png = b"\x89PNG\r\n\x1a\n" + b"p" * 100
        self.assertEqual(agent.resize_art(png, image_mod=False)[1], "image/png")
        # above the wire cap → too_large (the 40 KB cap is hard on both paths)
        self.assertEqual(agent.resize_art(b"\xff\xd8\xff" + b"j" * 50_000, image_mod=False), (None, None, "too_large"))
        self.assertEqual(agent.resize_art(b"\xff\xd8\xff" + b"j" * 70_000, image_mod=False), (None, None, "too_large"))
        self.assertEqual(agent.MEDIA_ART_NO_PILLOW_MAX_BYTES, 60 * 1024)
        self.assertEqual(agent.MEDIA_ART_WIRE_MAX_BYTES, 40 * 1024)
        self.assertEqual(agent.MEDIA_ART_MAX_PX, 256)
        self.assertEqual(agent.MEDIA_ART_JPEG_QUALITY, 80)
        # autodetect: without Pillow installed the fallback is used
        with mock.patch.object(agent, "_pillow_image", return_value=None):
            self.assertEqual(agent.resize_art(small)[0], small)

    def test_media_art_action_with_pillow_and_cache(self):
        pil = FakePillow({80: 20_000})
        raw = b"\x89PNG\r\n\x1a\n" + b"x" * 300_000
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "fetch_entity_picture", side_effect=self.fetch(raw)), \
             mock.patch.object(agent, "_pillow_image", return_value=pil):
            out = agent.execute_action("arvio.media_art", "", {"entity_id": "media_player.saloni"})
            self.assertTrue(out["ok"])
            self.assertEqual(out["entity_id"], "media_player.saloni")
            self.assertEqual(out["art_hash"], agent.media_art_hash("spotify://track/abc", SPEAKER_PICTURE))
            self.assertEqual(out["mime"], "image/jpeg")
            self.assertIsNone(out["reason"])
            decoded = base64.b64decode(out["data_base64"])
            self.assertEqual(len(decoded), 20_003)
            self.assertLessEqual(len(decoded), agent.MEDIA_ART_WIRE_MAX_BYTES)
            self.assertEqual(self.fetches, [SPEAKER_PICTURE])
            self.assertNotIn("cached", out)
            # same art_hash → served from the LRU, no second fetch
            again = agent.execute_action("arvio.media_art", "media_player.saloni", {})
            self.assertTrue(again["cached"])
            self.assertEqual(again["data_base64"], out["data_base64"])
            self.assertEqual(self.fetches, [SPEAKER_PICTURE])
            self.assertIn(out["art_hash"], agent.MEDIA_ART_CACHE)
            # a new track → new hash → refetch
            self.fake.states["media_player.saloni"]["attributes"]["media_content_id"] = "spotify://track/def"
            new = agent.execute_action("arvio.media_art", "", {"entity_id": "media_player.saloni"})
            self.assertNotEqual(new["art_hash"], out["art_hash"])
            self.assertEqual(len(self.fetches), 2)
        self.assertEqual(agent.MEDIA_ART_CACHE.capacity, 50)

    def test_media_art_without_pillow(self):
        small = b"\xff\xd8\xff" + b"j" * 1000
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "fetch_entity_picture", side_effect=self.fetch(small, "image/jpeg")), \
             mock.patch.object(agent, "_pillow_image", return_value=None):
            out = agent.media_art({"entity_id": "media_player.saloni"})
            self.assertEqual(base64.b64decode(out["data_base64"]), small)
            self.assertEqual(out["mime"], "image/jpeg")
        agent.MEDIA_ART_CACHE = agent.LRU(50)
        self.fetches = []
        big = b"\xff\xd8\xff" + b"j" * 100_000
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "fetch_entity_picture", side_effect=self.fetch(big, "image/jpeg")), \
             mock.patch.object(agent, "_pillow_image", return_value=None):
            out = agent.media_art({"entity_id": "media_player.saloni"})
            self.assertEqual((out["data_base64"], out["reason"]), (None, "too_large"))
            self.assertTrue(out["art_hash"])
            again = agent.media_art({"entity_id": "media_player.saloni"})  # too_large is cached (deterministic)
            self.assertTrue(again["cached"])
            self.assertEqual(len(self.fetches), 1)

    def test_no_picture_and_fetch_failure(self):
        with mock.patch.object(agent, "ha", side_effect=self.fake):
            out = agent.media_art({"entity_id": "media_player.kouzina"})
        self.assertEqual((out["art_hash"], out["data_base64"], out["reason"]), (None, None, "no_picture"))

        def boom(picture, timeout=10):
            raise RuntimeError("HTTP 500")

        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "fetch_entity_picture", side_effect=boom):
            out = agent.media_art({"entity_id": "media_player.saloni"})
            self.assertIsNone(out["data_base64"])
            self.assertTrue(out["reason"].startswith("fetch_failed"))
            self.assertNotIn(out["art_hash"], agent.MEDIA_ART_CACHE)  # transient → retried next time
        with self.assertRaises(ValueError):
            agent.media_art({"entity_id": "light.a"})
        with mock.patch.object(agent, "ha", return_value=None):
            with self.assertRaises(RuntimeError):
                agent.media_art({"entity_id": "media_player.saloni"})

    def test_fetch_goes_through_supervisor_proxy_with_token(self):
        seen = []

        class Resp:
            def __init__(self, req):
                seen.append(req)
                self.headers = {"Content-Type": "image/png; charset=binary"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b"\x89PNG" + b"x" * 10

        with mock.patch.object(agent, "TOKEN", "sup_tok"), \
             mock.patch.object(urllib.request, "urlopen", side_effect=lambda req, timeout=10: Resp(req)):
            data, mime = agent.fetch_entity_picture(SPEAKER_PICTURE)
            self.assertEqual(mime, "image/png")
            self.assertEqual(data[:4], b"\x89PNG")
            req = seen[0]
            self.assertEqual(req.full_url, "http://supervisor/core/api/media_player_proxy/media_player.saloni?token=tok123&cache=abc")
            self.assertEqual(req.get_header("Authorization"), "Bearer sup_tok")
            # absolute https pictures are fetched directly, without the add-on token
            agent.fetch_entity_picture("https://i.scdn.co/image/abc")
            self.assertEqual(seen[1].full_url, "https://i.scdn.co/image/abc")
            self.assertIsNone(seen[1].get_header("Authorization"))
        with self.assertRaises(RuntimeError):
            agent.fetch_entity_picture("/local/cover.png")  # not reachable through the core proxy
        with mock.patch.object(agent, "TOKEN", ""), mock.patch.object(agent, "read_token", return_value=""):
            with self.assertRaises(RuntimeError):
                agent.fetch_entity_picture(SPEAKER_PICTURE)


# --- 4. arvio.media_browse --------------------------------------------------------------------------


def browse_child(i: int, thumb=None, **kw) -> dict:
    return {
        "title": f"Item {i}", "media_class": "playlist", "media_content_type": "playlist",
        "media_content_id": f"spotify://playlist/{i}", "can_play": True, "can_expand": True,
        "thumbnail": thumb, "children_media_class": "track", **kw,
    }


class MediaBrowseTest(unittest.TestCase):
    def setUp(self):
        agent.MA_INFO["entry_id"] = None

    def tearDown(self):
        agent.MA_INFO["entry_id"] = None

    def test_mapping_thumbnails_and_cap(self):
        result = {
            "title": "Spotify", "media_class": "directory", "media_content_type": "library", "media_content_id": "spotify://",
            "can_play": False, "can_expand": True, "thumbnail": None,
            "children": [
                browse_child(1, thumb="https://i.scdn.co/image/1"),
                browse_child(2, thumb="/api/media_player_proxy/media_player.saloni/browse_media/playlist/2?token=x"),
                browse_child(3, thumb="http://insecure.example/3.jpg"),
                browse_child(4, thumb=None, can_play=False, can_expand=True, media_content_id=""),
                "junk",
            ],
        }
        page = agent.browse_result_to_page(result)
        self.assertEqual(page["title"], "Spotify")
        self.assertEqual(page["media_content_id"], "spotify://")
        self.assertEqual(page["media_content_type"], "library")
        self.assertEqual(page["media_class"], "directory")
        self.assertEqual(page["total"], 5)
        self.assertFalse(page["truncated"])
        items = page["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual(set(items[0]), {"title", "media_content_id", "media_content_type", "media_class", "can_play", "can_expand", "thumbnail", "thumbnail_hash"})
        self.assertEqual(items[0]["thumbnail"], "https://i.scdn.co/image/1")  # phone can load it
        self.assertEqual(items[0]["thumbnail_hash"], hashlib.sha1(b"https://i.scdn.co/image/1").hexdigest())
        self.assertIsNone(items[1]["thumbnail"])  # proxy URL needs the HA token → stripped
        self.assertEqual(items[1]["thumbnail_hash"], hashlib.sha1(result["children"][1]["thumbnail"].encode()).hexdigest())
        self.assertIsNone(items[2]["thumbnail"])  # plain http is not good enough
        self.assertTrue(items[2]["thumbnail_hash"])
        self.assertIsNone(items[3]["thumbnail"]); self.assertIsNone(items[3]["thumbnail_hash"]); self.assertIsNone(items[3]["media_content_id"])
        self.assertIs(items[3]["can_play"], False)
        self.assertEqual(items[1]["media_content_id"], "spotify://playlist/2")
        self.assertEqual(items[1]["media_class"], "playlist")
        for it in items:
            self.assertNotIn("children_media_class", it)
        # cap 200
        big = {**result, "children": [browse_child(i) for i in range(350)]}
        page = agent.browse_result_to_page(big)
        self.assertEqual((len(page["items"]), page["total"], page["truncated"]), (200, 350, True))
        self.assertEqual(agent.MEDIA_BROWSE_MAX_ITEMS, 200)
        # garbage in → empty page
        self.assertEqual(agent.browse_result_to_page(None)["items"], [])

    def test_action_calls_ha_ws_browse_media(self):
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            return {"title": "Root", "media_content_id": "", "media_content_type": "", "children": [browse_child(1)]}

        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.execute_action("arvio.media_browse", "", {"entity_id": "media_player.saloni"})
            self.assertTrue(out["ok"])
            self.assertEqual(out["entity_id"], "media_player.saloni")
            self.assertEqual(len(out["items"]), 1)
            self.assertEqual(ws_calls[-1], ("media_player/browse_media", {"entity_id": "media_player.saloni"}))
            agent.execute_action("arvio.media_browse", "media_player.saloni",
                                 {"media_content_id": "spotify://playlist/1", "media_content_type": "playlist"})
            self.assertEqual(ws_calls[-1], ("media_player/browse_media", {
                "entity_id": "media_player.saloni", "media_content_id": "spotify://playlist/1", "media_content_type": "playlist"}))
            cmd_ws.assert_not_called()  # browse rides the read-only query channel, never the call_service one
        with self.assertRaises(ValueError):
            agent.media_browse({"entity_id": "light.a"})
        with mock.patch.object(agent, "ha_ws_query_command", side_effect=agent.HaWsUnavailable("no ws")):
            with self.assertRaises(RuntimeError):
                agent.media_browse({"entity_id": "media_player.saloni"})

    def test_root_uses_music_assistant_library_playlists(self):
        agent.MA_INFO["entry_id"] = "ma1"
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            if extra and extra.get("service") == "get_library":
                return {"context": {"id": "ctx_lib"}, "response": {"items": [
                    {"name": "Discover Weekly", "uri": "spotify://playlist/dw", "media_type": "playlist",
                     "image": "https://i.scdn.co/1"},
                    {"name": "no uri"},
                    {"name": "Liked", "uri": "library://playlist/2", "media_type": "playlist"},
                ], "media_type": "playlist"}}
            return {"title": "Cast", "children": [browse_child(1)]}

        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.media_browse({"entity_id": "media_player.saloni"})
            cmd_ws.assert_not_called()
        self.assertEqual(ws_calls[0], ("call_service", {
            "domain": "music_assistant", "service": "get_library",
            "service_data": {"config_entry_id": "ma1", "media_type": "playlist", "limit": 50, "order_by": "name"},
            "return_response": True,
        }))
        self.assertEqual(out["source"], "music_assistant")
        self.assertEqual(out["title"], "Playlists")
        self.assertEqual([i["media_content_id"] for i in out["items"]],
                         ["spotify://playlist/dw", "library://playlist/2"])
        self.assertEqual(out["items"][0]["title"], "Discover Weekly")
        self.assertEqual(out["items"][0]["thumbnail"], "https://i.scdn.co/1")
        self.assertTrue(out["items"][0]["can_play"])
        self.assertFalse(out["items"][0]["can_expand"], "playlists play whole; MA browse_media is album/artist only")
        # expanding a node still uses the player's browse_media
        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws):
            nested = agent.media_browse({"entity_id": "media_player.saloni",
                                        "media_content_id": "spotify://playlist/dw",
                                        "media_content_type": "playlist"})
        self.assertEqual(ws_calls[-1], ("media_player/browse_media", {
            "entity_id": "media_player.saloni", "media_content_id": "spotify://playlist/dw",
            "media_content_type": "playlist"}))
        self.assertEqual(nested["items"][0]["media_content_id"], "spotify://playlist/1")
        self.assertNotIn("source", nested)
        # get_library failure on a group player must not fall through to browse_media
        ws_calls.clear()

        def fail_library(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            if extra and extra.get("service") == "get_library":
                raise RuntimeError("no such service")
            return {"title": "Cast", "children": [browse_child(9)]}

        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fail_library), \
             mock.patch.object(agent, "music_assistant_entry_id", return_value="ma1"):
            fallback = agent.media_browse({"entity_id": "media_player.office_group"})
        self.assertEqual(fallback["source"], "music_assistant")
        self.assertEqual(fallback["items"], [])
        self.assertTrue(fallback["ok"])
        self.assertTrue(all(c[0] != "media_player/browse_media" for c in ws_calls))
        self.assertEqual(agent.ma_library_items(None, "playlist"), [])
        self.assertEqual(agent.ma_library_items({"items": "nope"}, "playlist"), [])

    def test_root_looks_up_entry_id_when_cache_empty(self):
        agent.MA_INFO["entry_id"] = None
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            if extra and extra.get("service") == "get_library":
                return {"context": {"id": "ctx"}, "response": {"items": [
                    {"name": "Liked", "uri": "library://playlist/2", "media_type": "playlist"},
                ]}}
            raise RuntimeError("Entity does not support browse media")

        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "music_assistant_entry_id", return_value="ma1"):
            out = agent.media_browse({"entity_id": "media_player.office_group"})
        self.assertEqual(out["source"], "music_assistant")
        self.assertEqual(out["items"][0]["title"], "Liked")
        self.assertEqual(ws_calls[0][1]["service"], "get_library")

    def test_browse_media_unsupported_returns_empty_page(self):
        agent.MA_INFO["entry_id"] = None
        with mock.patch.object(agent, "music_assistant_entry_id", return_value=None), \
             mock.patch.object(agent, "ha_ws_query_command",
                               side_effect=RuntimeError("Entity does not support browse media")):
            out = agent.media_browse({"entity_id": "media_player.office_group"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"], [])
        with mock.patch.object(agent, "music_assistant_entry_id", return_value=None), \
             mock.patch.object(agent, "ha_ws_query_command",
                               side_effect=agent.HaWsUnavailable("no ws")):
            with self.assertRaises(RuntimeError):
                agent.media_browse({"entity_id": "media_player.saloni"})


# --- 5. arvio.media_search ------------------------------------------------------------------------


MA_RESPONSE = {
    "artists": [{"name": "Miles Davis", "uri": "library://artist/1", "media_type": "artist", "image": "https://img/1"}],
    "albums": [{"name": "Kind of Blue", "uri": "spotify://album/2", "media_type": "album", "artists": [{"name": "Miles Davis"}], "image": None}],
    "tracks": [{"name": "So What", "uri": "spotify://track/3", "media_type": "track",
                "artists": [{"name": "Miles Davis"}, {"name": "Bill Evans"}], "album": {"name": "Kind of Blue"},
                "image": "/api/proxy/3"}],
    "playlists": [{"name": "Jazz", "uri": "spotify://playlist/4", "media_type": "playlist"}],
    "radio": [{"name": "Jazz FM", "uri": "tunein://radio/5", "media_type": "radio"}, {"name": "no uri"}],
}


class MediaSearchTest(unittest.TestCase):
    def setUp(self):
        self.fake = MediaFakeHa(media_states(), entries=[], response=MA_RESPONSE)
        agent.MA_INFO["entry_id"] = None

    def tearDown(self):
        agent.MA_INFO["entry_id"] = None

    def test_search_media_ws_when_player_supports_it(self):
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            return {"result": [browse_child(1, thumb="https://x/1", media_class="track", media_content_type="track")]}

        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.execute_action("arvio.media_search", "", {"entity_id": "media_player.saloni", "query": "miles"})
            self.assertEqual(ws_calls, [("media_player/search_media", {"entity_id": "media_player.saloni", "search_query": "miles"})])
            self.assertEqual((out["ok"], out["source"], out["query"]), (True, "ha", "miles"))
            self.assertEqual(out["items"][0]["media_content_id"], "spotify://playlist/1")
            self.assertEqual(out["items"][0]["media_class"], "track")
            self.assertNotIn("reason", out)
            # media_type is a class filter (documented `media_filter_classes`), NOT the search context
            agent.media_search({"entity_id": "media_player.saloni", "query": "miles", "media_type": "album"})
            self.assertEqual(ws_calls[-1][1], {"entity_id": "media_player.saloni", "search_query": "miles",
                                               "media_filter_classes": ["album"]})
            self.assertNotIn("media_content_type", ws_calls[-1][1])
            # media_content_id/type = the browse node to search within, passed through unchanged
            agent.media_search({"entity_id": "media_player.saloni", "query": "miles", "media_type": "track",
                                "media_content_id": "spotify://playlist/1", "media_content_type": "playlist"})
            self.assertEqual(ws_calls[-1][1], {"entity_id": "media_player.saloni", "search_query": "miles",
                                               "media_filter_classes": ["track"],
                                               "media_content_id": "spotify://playlist/1", "media_content_type": "playlist"})
            # an empty context id is not sent
            agent.media_search({"entity_id": "media_player.saloni", "query": "miles", "media_content_id": ""})
            self.assertNotIn("media_content_id", ws_calls[-1][1])
            cmd_ws.assert_not_called()  # search never holds the call_service channel
        self.assertEqual(self.fake.calls, [])  # no MA service call

    def test_music_assistant_fallback(self):
        # the cast speaker has no SEARCH_MEDIA → music_assistant.search with return_response
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            return {"context": {"id": "ctx_ma"}, "response": MA_RESPONSE}

        agent.MA_INFO["entry_id"] = "ma1"
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.media_search({"entity_id": "media_player.kouzina", "query": "miles", "media_type": "track"})
            cmd_ws.assert_not_called()  # the MA search return_response call rides the query channel too
        self.assertEqual(ws_calls, [("call_service", {
            "domain": "music_assistant", "service": "search",
            "service_data": {"config_entry_id": "ma1", "name": "miles", "limit": 10, "media_type": ["track"]},
            "return_response": True,
        })])
        self.assertEqual(out["source"], "music_assistant")
        items = out["items"]
        self.assertEqual([i["media_content_id"] for i in items], ["spotify://track/3"])
        self.assertEqual(items[0]["title"], "So What")
        self.assertEqual(items[0]["media_content_type"], "track")
        self.assertIsNone(items[0]["thumbnail"])  # MA proxy image → hash only
        self.assertTrue(items[0]["thumbnail_hash"])
        self.assertEqual(items[0]["artist"], "Miles Davis · Bill Evans")
        self.assertEqual(items[0]["album"], "Kind of Blue")
        self.assertTrue(items[0]["can_play"]); self.assertFalse(items[0]["can_expand"])
        for key in ("title", "media_content_id", "media_content_type", "media_class", "can_play", "can_expand", "thumbnail", "thumbnail_hash"):
            self.assertIn(key, items[0])
        # without media_type: tracks first so a song title is not buried under artists
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command"):
            mixed = agent.media_search({"entity_id": "media_player.kouzina", "query": "miles"})
        self.assertEqual([i["media_content_id"] for i in mixed["items"]],
                         ["spotify://track/3", "spotify://playlist/4", "spotify://album/2", "library://artist/1", "tunein://radio/5"])
        self.assertEqual([i["media_content_type"] for i in mixed["items"]], ["track", "playlist", "album", "artist", "radio"])
        self.assertEqual(mixed["items"][3]["thumbnail"], "https://img/1")
        self.assertTrue(mixed["items"][3]["can_expand"]); self.assertTrue(mixed["items"][1]["can_expand"])
        # REST fallback (no websocket) uses POST …?return_response and reads service_response
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command", side_effect=agent.HaWsUnavailable("no ws")):
            out = agent.media_search({"entity_id": "media_player.kouzina", "query": "miles"})
        self.assertEqual(self.fake.calls[-1][0], "/services/music_assistant/search?return_response")
        self.assertEqual(self.fake.calls[-1][1], {"config_entry_id": "ma1", "name": "miles", "limit": 10})
        self.assertEqual(len(out["items"]), 5)
        # MA entry unknown yet → looked up from the config entries on demand
        agent.MA_INFO["entry_id"] = None
        self.fake.entries = MA_ENTRIES
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws):
            out = agent.media_search({"entity_id": "media_player.kouzina", "query": "x"})
        self.assertEqual(out["source"], "music_assistant")
        self.assertEqual(agent.MA_INFO["entry_id"], "ma1")

    def test_unavailable(self):
        with mock.patch.object(agent, "ha", side_effect=self.fake), \
             mock.patch.object(agent, "ha_ws_query_command") as ws, \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.execute_action("arvio.media_search", "media_player.kouzina", {"query": "miles"})
            ws.assert_not_called()
            cmd_ws.assert_not_called()
        self.assertEqual(out, {"ok": True, "entity_id": "media_player.kouzina", "query": "miles", "source": None,
                               "items": [], "reason": "search_unavailable"})
        with self.assertRaises(ValueError):
            agent.media_search({"entity_id": "media_player.kouzina", "query": "  "})
        with self.assertRaises(ValueError):
            agent.media_search({"entity_id": "switch.a", "query": "x"})
        self.assertEqual(agent.ma_search_items(None), [])
        self.assertEqual(len(agent.ma_search_items({"tracks": [{"name": "t", "uri": f"u{i}"} for i in range(300)]})), 200)
        self.assertEqual([i["media_content_type"] for i in agent.ma_search_items(MA_RESPONSE)],
                         ["track", "playlist", "album", "artist", "radio"])
        self.assertEqual(len(agent.ma_search_items(MA_RESPONSE, media_type="track")), 1)


# --- 6. services: allowlists, service data, volume_max, acks -----------------------------------------


MEDIA_SERVICES = [
    "media_player.media_play", "media_player.media_pause", "media_player.media_play_pause", "media_player.media_stop",
    "media_player.media_next_track", "media_player.media_previous_track", "media_player.volume_set",
    "media_player.volume_mute", "media_player.volume_up", "media_player.volume_down", "media_player.select_source",
    "media_player.join", "media_player.unjoin", "media_player.play_media", "media_player.turn_on",
    "media_player.turn_off", "media_player.shuffle_set", "media_player.repeat_set",
    "music_assistant.play_media", "music_assistant.transfer_queue", "music_assistant.get_queue",
]


class MediaAllowlistTest(unittest.TestCase):
    def test_every_media_service_on_relay_and_lan(self):
        library_actions = ["arvio.media_art", "arvio.media_browse", "arvio.media_search"]
        for action in MEDIA_SERVICES + library_actions:
            self.assertIn(action, agent.AGENT_SERVICE_ALLOWLIST, action)
            self.assertIn(action, agent.MEDIA_ACTIONS)
            agent.check_command_safety({"action": action, "entity_id": "media_player.saloni"})
            if action in library_actions:
                # personal data: cloud-only until the LAN path is behind HA ingress
                self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS, action)
                with self.assertRaises(agent.CommandRejected):
                    agent.check_command_safety({"action": action, "entity_id": "media_player.saloni"}, via="lan")
            else:
                self.assertIn(action, agent.LAN_ALLOWED_ACTIONS, action)
                agent.check_command_safety({"action": action, "entity_id": "media_player.saloni"}, via="lan")
            self.assertNotIn(action, agent.HOME_DANGEROUS_ACTIONS)
            self.assertNotIn(action, agent.HOME_SECURITY_ACTIONS)
            self.assertNotIn(action, agent.TARGET_ACTIONS)
        self.assertEqual(len(agent.MEDIA_PLAYER_ACTIONS), 18)
        # not services: media_player.play / seek / toggle stay unknown; no target on media
        for action in ("media_player.play", "media_player.media_seek", "media_player.toggle", "music_assistant.play_announcement"):
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": action})
            self.assertEqual(cm.exception.code, "action_not_allowed")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "media_player.media_pause", "target": {"area_id": "saloni"}})
        self.assertEqual(cm.exception.code, "target_not_supported")


class MediaServiceDataTest(unittest.TestCase):
    def sd(self, action, payload, eid="media_player.saloni"):
        return agent.build_service_data(action, eid, payload, "2025.8.1")

    def test_volume_clamp_and_volume_max(self):
        self.assertEqual(agent.clamp_volume(0.5), 0.5)
        self.assertEqual(agent.clamp_volume(1.7), 1.0)
        self.assertEqual(agent.clamp_volume(-1), 0.0)
        self.assertEqual(agent.clamp_volume("0.25"), 0.25)
        self.assertEqual(agent.clamp_volume(0.9, 0.6), 0.6)  # cloud entity_meta.volume_max
        self.assertEqual(agent.clamp_volume(0.3, 0.6), 0.3)
        self.assertEqual(agent.clamp_volume(0.9, 1.5), 0.9)
        self.assertEqual(agent.clamp_volume(0.9, "junk"), 0.9)
        with self.assertRaises(ValueError):
            agent.clamp_volume("loud")
        self.assertEqual(self.sd("media_player.volume_set", {"volume_level": 0.8, "volume_max": 0.5})[1],
                         {"entity_id": "media_player.saloni", "volume_level": 0.5})
        self.assertEqual(self.sd("media_player.volume_set", {"volume_level": 0.42})[1]["volume_level"], 0.42)
        with self.assertRaises(ValueError):
            self.sd("media_player.volume_set", {})

    def test_volume_up_plan(self):
        self.assertEqual(agent.media_volume_up_plan(0.3, None), ("up", None))
        self.assertEqual(agent.media_volume_up_plan(0.3, 0.6), ("up", None))
        self.assertEqual(agent.media_volume_up_plan(0.55, 0.6), ("set", 0.6))  # one step would cross the ceiling
        self.assertEqual(agent.media_volume_up_plan(0.6, 0.6), ("refuse", 0.6))
        self.assertEqual(agent.media_volume_up_plan(0.9, 0.6), ("refuse", 0.6))
        self.assertEqual(agent.media_volume_up_plan(None, 0.6), ("refuse", 0.6))  # unknown level: never guess upwards
        self.assertEqual(agent.media_volume_up_plan(0.5, 2), ("up", None))  # ceiling clamps to 1.0
        self.assertEqual(agent.media_volume_up_plan(0.95, 2), ("set", 1.0))
        self.assertEqual(agent.MEDIA_VOLUME_STEP, 0.1)

    def test_media_player_service_data(self):
        for action in ("media_player.media_play", "media_player.media_next_track", "media_player.unjoin", "media_player.turn_off"):
            self.assertEqual(self.sd(action, {"volume_level": 1})[1], {"entity_id": "media_player.saloni"})
        self.assertEqual(self.sd("media_player.volume_mute", {"is_volume_muted": False})[1]["is_volume_muted"], False)
        self.assertEqual(self.sd("media_player.volume_mute", {})[1]["is_volume_muted"], True)
        self.assertEqual(self.sd("media_player.select_source", {"source": "Radio"})[1]["source"], "Radio")
        with self.assertRaises(ValueError):
            self.sd("media_player.select_source", {})
        self.assertEqual(self.sd("media_player.join", {"group_members": ["media_player.kouzina", "media_player.tv"]})[1],
                         {"entity_id": "media_player.saloni", "group_members": ["media_player.kouzina", "media_player.tv"]})
        self.assertEqual(self.sd("media_player.join", {"group_members": "media_player.kouzina"})[1]["group_members"], ["media_player.kouzina"])
        with self.assertRaises(ValueError):
            self.sd("media_player.join", {"group_members": []})
        svc, data = self.sd("media_player.play_media", {"media_content_id": "spotify://playlist/1", "media_content_type": "playlist", "enqueue": "next"})
        self.assertEqual((svc, data), ("play_media", {"entity_id": "media_player.saloni", "media_content_id": "spotify://playlist/1",
                                                      "media_content_type": "playlist", "enqueue": "next"}))
        self.assertNotIn("enqueue", self.sd("media_player.play_media", {"media_content_id": "x", "media_content_type": "music"})[1])
        with self.assertRaises(ValueError):
            self.sd("media_player.play_media", {"media_content_id": "x"})
        with self.assertRaises(ValueError):
            self.sd("media_player.play_media", {"media_content_id": "x", "media_content_type": "music", "enqueue": "later"})
        self.assertEqual(self.sd("media_player.shuffle_set", {"shuffle": False})[1]["shuffle"], False)
        self.assertEqual(self.sd("media_player.repeat_set", {"repeat": "one"})[1]["repeat"], "one")
        self.assertEqual(self.sd("media_player.repeat_set", {})[1]["repeat"], "off")
        with self.assertRaises(ValueError):
            self.sd("media_player.repeat_set", {"repeat": "forever"})

    def test_music_assistant_service_data(self):
        svc, data = self.sd("music_assistant.play_media", {"media_id": "spotify://playlist/1", "media_type": "playlist",
                                                           "enqueue": "replace_next", "radio_mode": 1, "artist": "Miles"})
        self.assertEqual(svc, "play_media")
        self.assertEqual(data, {"entity_id": "media_player.saloni", "media_id": "spotify://playlist/1", "media_type": "playlist",
                                "artist": "Miles", "enqueue": "replace_next", "radio_mode": True})
        listed = self.sd("music_assistant.play_media", {"media_id": ["a", "b"]})[1]
        self.assertEqual(listed["media_id"], ["a", "b"])
        self.assertEqual(listed["enqueue"], "replace")
        with self.assertRaises(ValueError):
            self.sd("music_assistant.play_media", {"media_type": "track"})
        with self.assertRaises(ValueError):
            self.sd("music_assistant.play_media", {"media_id": "x", "enqueue": "soon"})
        self.assertEqual(self.sd("music_assistant.transfer_queue", {"source_player": "media_player.kouzina", "auto_play": 0})[1],
                         {"entity_id": "media_player.saloni", "source_player": "media_player.kouzina", "auto_play": False})
        self.assertEqual(self.sd("music_assistant.transfer_queue", {})[1], {"entity_id": "media_player.saloni"})

    def test_expected_attrs(self):
        self.assertEqual(agent.media_expected_attrs("media_player.volume_set", {"volume_level": 0.4}), {"volume_level": 0.4})
        self.assertEqual(agent.media_expected_attrs("media_player.select_source", {"source": "Radio"}), {"source": "Radio"})
        self.assertEqual(agent.media_expected_attrs("media_player.volume_mute", {"is_volume_muted": True}), {"is_volume_muted": True})
        self.assertEqual(agent.media_expected_attrs("media_player.shuffle_set", {"shuffle": True}), {"shuffle": True})
        self.assertEqual(agent.media_expected_attrs("media_player.repeat_set", {"repeat": "all"}), {"repeat": "all"})
        for action in ("media_player.media_play", "media_player.join", "music_assistant.play_media", "media_player.volume_up"):
            self.assertIsNone(agent.media_expected_attrs(action, {}))
        self.assertTrue(agent._attrs_match({"attributes": {"volume_level": 0.41}}, {"volume_level": 0.4}))  # device rounding
        self.assertFalse(agent._attrs_match({"attributes": {"volume_level": 0.5}}, {"volume_level": 0.4}))
        self.assertFalse(agent._attrs_match({"attributes": {}}, {"volume_level": 0.4}))
        self.assertTrue(agent._attrs_match({"attributes": {"source": "Radio"}}, {"source": "Radio"}))
        self.assertTrue(agent._attrs_match(None, None))
        self.assertFalse(agent._attrs_match(None, {"source": "x"}))

    def test_expected_states(self):
        self.assertEqual(agent._expected_state_for("media_player", "media_play"), "playing")
        self.assertEqual(agent._expected_state_for("media_player", "media_pause"), "paused")
        self.assertEqual(agent._expected_state_for("media_player", "media_stop"), "idle")
        self.assertEqual(agent._expected_state_for("media_player", "turn_on"), "on")
        self.assertEqual(agent._expected_state_for("media_player", "turn_off"), "off")
        for svc in ("media_play_pause", "media_next_track", "volume_set", "select_source", "join", "play_media", "volume_up"):
            self.assertIsNone(agent._expected_state_for("media_player", svc), svc)
        self.assertIsNone(agent._expected_state_for("music_assistant", "play_media"))
        self.assertTrue(agent._state_matches("media_player", "playing", "buffering"))
        self.assertTrue(agent._state_matches("media_player", "idle", "paused"))
        self.assertFalse(agent._state_matches("media_player", "idle", "playing"))
        self.assertTrue(agent._state_matches("media_player", "on", "idle"))
        self.assertFalse(agent._state_matches("media_player", "on", "standby"))
        self.assertTrue(agent._state_matches("media_player", "off", "standby"))
        self.assertFalse(agent._state_matches("media_player", "paused", "playing"))


class MediaExecuteTest(unittest.TestCase):
    def setUp(self):
        self.fake = MediaFakeHa(media_states(), response={"items": [{"name": "So What"}], "current_index": 0})
        self.patches = [
            mock.patch.object(agent, "ha", side_effect=self.fake),
            mock.patch.object(agent, "ha_ws_shared_command", side_effect=agent.HaWsUnavailable("no ws")),
            mock.patch.object(agent, "ha_ws_query_command", side_effect=agent.HaWsUnavailable("no ws")),
            mock.patch.object(agent, "HA_INFO", {"version": "2025.8.1", "time_zone": "Europe/Athens"}),
            mock.patch.object(agent, "WAIT_FOR_STATE_S", 0.3),
            mock.patch.object(agent, "MEDIA_CALL_ASYNC", False),
            mock.patch.object(agent, "STATE_FEED_LIVE", False),
            mock.patch.object(agent, "REGISTRY_CACHE", agent.RegistrySnapshot(**{k: v for k, v in regs().items() if k != "loaded_at"}, loaded_at=1.0)),
        ]
        for p in self.patches:
            p.start()
        agent.STATE_FEED_RECENT = agent.LRU(1000)
        agent.COMMAND_ACKS = agent.LRU(500)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_volume_set_confirms_via_attribute(self):
        out = agent.execute_action("media_player.volume_set", "media_player.saloni", {"volume_level": 0.5})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertEqual(self.fake.calls, [("/services/media_player/volume_set", {"entity_id": "media_player.saloni", "volume_level": 0.5})])
        self.assertEqual(out["state"], "playing")  # state untouched
        self.assertEqual(out["state_snapshot"]["attrs"]["volume_level"], 0.5)
        self.assertEqual(out["state_snapshot"]["attrs"]["platform"], "music_assistant")  # registry platform in the snapshot
        self.assertEqual(out["volume_level"], 0.5)
        self.assertEqual(out["ha_context_id"], "ctx_media")
        # a stubborn device that keeps its old volume: we still ack ok — Home keeps
        # the optimistic level; waiting 2 s here queued the next skip behind silence.
        stubborn = MediaFakeHa(media_states())
        orig = stubborn.__call__

        def no_change(path, method="GET", body=None, timeout=10):
            if path.startswith("/services/"):
                stubborn.calls.append((path, body))
                return []
            return orig(path, method, body, timeout)

        with mock.patch.object(agent, "ha", side_effect=no_change):
            out = agent.execute_action("media_player.volume_set", "media_player.saloni", {"volume_level": 0.5})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertEqual(out["state_snapshot"]["attrs"]["volume_level"], 0.35)

    def test_media_ack_does_not_wait_for_spotify(self):
        release = threading.Event()
        orig = agent.ha_call_service

        def blocked(domain, service, data):
            release.wait(2)
            return orig(domain, service, data)

        with mock.patch.object(agent, "MEDIA_CALL_ASYNC", True), mock.patch.object(agent, "ha_call_service", side_effect=blocked):
            t0 = time.monotonic()
            out = agent.execute_action("media_player.volume_set", "media_player.saloni", {"volume_level": 0.5})
            elapsed = time.monotonic() - t0
        release.set()
        self.assertTrue(out["ok"])
        self.assertLess(elapsed, 0.5)

    def test_play_media_waits_even_when_volume_is_async(self):
        orig = agent.ha_call_service

        def delayed(domain, service, data):
            if service == "play_media":
                time.sleep(0.12)
            return orig(domain, service, data)

        with mock.patch.object(agent, "MEDIA_CALL_ASYNC", True), mock.patch.object(agent, "ha_call_service", side_effect=delayed):
            t0 = time.monotonic()
            out = agent.execute_action(
                "music_assistant.play_media",
                "media_player.saloni",
                {"media_id": "x", "media_type": "playlist", "enqueue": "replace"},
            )
            elapsed = time.monotonic() - t0
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(elapsed, 0.1)

    def test_select_source_and_play_pause_states(self):
        out = agent.execute_action("media_player.select_source", "media_player.saloni", {"source": "Radio"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["state_snapshot"]["attrs"]["source"], "Radio")
        out = agent.execute_action("media_player.media_pause", "media_player.saloni", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "paused")
        out = agent.execute_action("media_player.media_play", "media_player.saloni", {})
        self.assertEqual(out["state"], "playing")
        # stateless: next track → no wait, one call, ok
        reads_before = len(self.fake.reads)
        out = agent.execute_action("media_player.media_next_track", "media_player.saloni", {})
        self.assertTrue(out["ok"])
        self.assertEqual(self.fake.calls[-1][0], "/services/media_player/media_next_track")
        self.assertEqual(len(self.fake.reads), reads_before + 1)  # a single snapshot read, no polling
        with self.assertRaises(ValueError):
            agent.execute_action("media_player.media_play", "", {})

    def test_volume_max_on_volume_set_and_volume_up(self):
        out = agent.execute_action("media_player.volume_set", "media_player.saloni", {"volume_level": 0.95, "volume_max": 0.6})
        self.assertTrue(out["ok"])
        self.assertEqual(self.fake.calls[-1][1]["volume_level"], 0.6)
        # volume_up: already at the ceiling → refused, nothing sent, ack ok:false with a stable code
        ack = agent.handle_command({"command_id": "vu1", "action": "media_player.volume_up", "entity_id": "media_player.saloni",
                                    "payload": {"volume_max": 0.6}, "expires_at": FUTURE})
        self.assertFalse(ack["ok"])
        self.assertEqual((ack["error"], ack["error_code"]), ("volume_max_reached", "volume_max_reached"))
        self.assertEqual(ack["volume_max"], 0.6)
        self.assertEqual(ack["state_snapshot"]["attrs"]["volume_level"], 0.6)
        self.assertEqual(self.fake.calls[-1][0], "/services/media_player/volume_set")  # no volume_up went out
        # one step would cross the ceiling → volume_set to the ceiling instead
        self.fake.states["media_player.saloni"]["attributes"]["volume_level"] = 0.55
        ack = agent.handle_command({"command_id": "vu2", "action": "media_player.volume_up", "entity_id": "media_player.saloni",
                                    "payload": {"volume_max": 0.6}})
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["clamped_to_volume_max"])
        self.assertEqual(self.fake.calls[-1], ("/services/media_player/volume_set", {"entity_id": "media_player.saloni", "volume_level": 0.6}))
        self.assertEqual(ack["service"], "media_player.volume_up")
        # well below → plain volume_up
        self.fake.states["media_player.saloni"]["attributes"]["volume_level"] = 0.2
        ack = agent.handle_command({"command_id": "vu3", "action": "media_player.volume_up", "entity_id": "media_player.saloni",
                                    "payload": {"volume_max": 0.6}})
        self.assertTrue(ack["ok"])
        self.assertEqual(self.fake.calls[-1], ("/services/media_player/volume_up", {"entity_id": "media_player.saloni"}))
        # no volume_max → no state read before the call
        reads = len(self.fake.reads)
        agent.execute_action("media_player.volume_up", "media_player.kouzina", {})
        self.assertEqual(self.fake.calls[-1][0], "/services/media_player/volume_up")
        self.assertEqual(len(self.fake.reads), reads + 1)

    def test_music_assistant_actions_and_get_queue_response(self):
        out = agent.execute_action("music_assistant.play_media", "media_player.saloni",
                                   {"media_id": "spotify://playlist/1", "media_type": "playlist", "enqueue": "replace"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.fake.calls[-1], ("/services/music_assistant/play_media", {
            "entity_id": "media_player.saloni", "media_id": "spotify://playlist/1", "media_type": "playlist", "enqueue": "replace"}))
        self.fake.states["media_player.saloni"]["state"] = "idle"
        self.fake.calls.clear()
        kicked = agent.execute_action("music_assistant.play_media", "media_player.saloni",
                                      {"media_id": "library://playlist/14", "media_type": "playlist"})
        self.assertTrue(kicked["ok"])
        self.assertEqual([c[0] for c in self.fake.calls], [
            "/services/music_assistant/play_media",
            "/services/media_player/media_play",
        ])
        self.assertEqual(self.fake.calls[0][1]["enqueue"], "replace")
        self.assertEqual(kicked["state"], "playing")
        self.fake.states["media_player.saloni"]["state"] = "idle"
        self.fake.calls.clear()
        agent.execute_action("music_assistant.play_media", "media_player.saloni",
                             {"media_id": "library://playlist/14", "media_type": "playlist", "enqueue": "add"})
        self.assertEqual([c[0] for c in self.fake.calls], ["/services/music_assistant/play_media"])
        self.assertEqual(out["state_snapshot"]["entity_id"], "media_player.saloni")
        agent.execute_action("music_assistant.transfer_queue", "media_player.kouzina", {"source_player": "media_player.saloni", "auto_play": True})
        self.assertEqual(self.fake.calls[-1][1], {"entity_id": "media_player.kouzina", "source_player": "media_player.saloni", "auto_play": True})
        # get_queue: the response lands in the ack data
        ack = agent.handle_command({"command_id": "q1", "action": "music_assistant.get_queue", "entity_id": "media_player.saloni"})
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["queue"], {"items": [{"name": "So What"}], "current_index": 0})
        self.assertEqual(self.fake.calls[-1][0], "/services/music_assistant/get_queue?return_response")
        self.assertEqual(ack["affected_entity_ids"], ["media_player.saloni"])
        self.assertIsNone(ack["state_snapshot"])
        # …and over the websocket the documented return_response flag is used
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            return {"context": {"id": "ctx_q"}, "response": {"items": []}}

        with mock.patch.object(agent, "ha_ws_query_command", side_effect=fake_ws), \
             mock.patch.object(agent, "ha_ws_shared_command") as cmd_ws:
            out = agent.execute_action("music_assistant.get_queue", "media_player.saloni", {})
            cmd_ws.assert_not_called()  # return_response reads ride the query channel
        self.assertEqual(ws_calls, [("call_service", {"domain": "music_assistant", "service": "get_queue",
                                                      "service_data": {"entity_id": "media_player.saloni"}, "return_response": True})])
        self.assertEqual((out["queue"], out["ha_context_id"]), ({"items": []}, "ctx_q"))


# --- 7. push: coalescing skips position-only updates, art_hash rides along ----------------------------


class MediaPushTest(unittest.TestCase):
    def setUp(self):
        agent.CONTEXT_TO_COMMAND = agent.LRU(500)
        self.regs = regs()

    def playing(self, position=42.5, title="Blue in Green", picture=SPEAKER_PICTURE, state="playing"):
        return st("media_player.saloni", state, friendly_name="Ηχείο", device_class="speaker", media_title=title,
                  media_content_id="spotify://track/abc", media_position=position,
                  media_position_updated_at=f"2026-09-11T10:00:{int(position):02d}+00:00", volume_level=0.35,
                  entity_picture=picture, supported_features=MA_SPEAKER_FEATURES)

    def test_position_only_change_detection(self):
        old, new = self.playing(42.5), self.playing(43.5)
        self.assertTrue(agent.media_position_only_change(old, new))
        self.assertTrue(agent.media_position_only_change(old, old))
        self.assertFalse(agent.media_position_only_change(old, self.playing(43.5, title="So What")))
        self.assertFalse(agent.media_position_only_change(old, self.playing(43.5, state="paused")))
        self.assertFalse(agent.media_position_only_change(old, self.playing(43.5, picture=SPEAKER_PICTURE + "2")))  # new art
        self.assertFalse(agent.media_position_only_change(None, new))  # first sighting → push
        self.assertFalse(agent.media_position_only_change(old, None))

    def test_state_event_message(self):
        old, new = self.playing(42.5), self.playing(43.5)
        self.assertIsNone(agent.state_event_message({"entity_id": "media_player.saloni", "old_state": old, "new_state": new}, self.regs))
        changed = self.playing(0.0, title="So What")
        msg = agent.state_event_message({"entity_id": "media_player.saloni", "old_state": old, "new_state": changed}, self.regs)
        self.assertEqual(msg["type"], "state")
        self.assertEqual(msg["state"], "playing")
        self.assertEqual(msg["attrs"]["media_title"], "So What")
        self.assertEqual(msg["attrs"]["media_position"], 0.0)
        self.assertEqual(msg["attrs"]["art_hash"], agent.media_art_hash("spotify://track/abc", SPEAKER_PICTURE))
        self.assertEqual(msg["attrs"]["platform"], "music_assistant")
        self.assertNotIn("entity_picture", msg["attrs"])
        # art change alone (same track id, new cache param) is pushed with the new hash
        art = self.playing(43.5, picture=SPEAKER_PICTURE.replace("cache=abc", "cache=zzz"))
        msg = agent.state_event_message({"entity_id": "media_player.saloni", "old_state": old, "new_state": art}, self.regs)
        self.assertNotEqual(msg["attrs"]["art_hash"], agent.media_art_hash("spotify://track/abc", SPEAKER_PICTURE))
        # no old_state (first event) → pushed
        self.assertIsNotNone(agent.state_event_message({"entity_id": "media_player.saloni", "new_state": new}, self.regs))
        # other domains are unaffected by the position rule
        light_old = st("light.saloni", "on", brightness=10)
        self.assertIsNotNone(agent.state_event_message({"entity_id": "light.saloni", "old_state": light_old,
                                                        "new_state": st("light.saloni", "on", brightness=10)}, self.regs))

    def test_handle_ha_event_coalesces_and_skips_position_ticks(self):
        sent = []
        now = [100.0]
        coal = agent.Coalescer(sent.append, 1.0, clock=lambda: now[0])
        with mock.patch.object(agent, "registry_snapshot", return_value=self.regs), \
             mock.patch.object(agent, "PUSH", coal):
            prev = self.playing(10.0)
            agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "media_player.saloni", "old_state": None, "new_state": prev}})
            self.assertEqual(len(sent), 1)
            for pos in (11.0, 12.0, 13.0):  # per-second position ticks: nothing queued, nothing sent
                cur = self.playing(pos)
                agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "media_player.saloni", "old_state": prev, "new_state": cur}})
                prev = cur
            self.assertEqual(len(sent), 1)
            self.assertEqual(coal.pending(), 0)
            # real changes inside the same second are coalesced (latest wins)
            for title in ("A", "B"):
                cur = self.playing(14.0, title=title)
                agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "media_player.saloni", "old_state": prev, "new_state": cur}})
                prev = cur
            self.assertEqual(len(sent), 1)
            self.assertEqual(coal.pending(), 1)
            now[0] = 101.0
            coal.flush()
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[-1]["attrs"]["media_title"], "B")
            self.assertIsNotNone(agent.STATE_FEED_RECENT.get("media_player.saloni"))  # waiters still fed


# --- 8. LAN path forwards harmless media payload keys only ---------------------------------------------


class MediaLanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_media_payload_keys(self):
        agent.COMMAND_ACKS = agent.LRU(500)
        calls = []

        def fake_execute(action, entity_id, payload=None, target=None):
            calls.append((action, entity_id, payload, target))
            return {"ok": True, "entity_id": entity_id, "state": "playing", "affected_entity_ids": [entity_id]}

        with mock.patch.object(agent, "hub_id", "hub_lan"), mock.patch.object(agent, "execute_action", side_effect=fake_execute):
            status, body = self.post("/v1/hubs/hub_lan/commands", {
                "action": "media_player.volume_set", "entity_id": "media_player.saloni",
                "payload": {"volume_level": 0.4, "volume_max": 0.6, "confirm_dangerous": True, "code": "1234", "target": {"area_id": "saloni"}},
            })
            self.assertEqual(status, 200, body)
            # security keys never lifted, and volume_max is cloud-only (a LAN caller cannot set its own ceiling)
            self.assertEqual(calls[-1][2], {"volume_level": 0.4})
            # flat keys (lab UI convenience)
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "media_player.select_source", "entity_id": "media_player.saloni", "source": "Spotify"})
            self.assertEqual(status, 200, body)
            self.assertEqual(calls[-1][2], {"source": "Spotify"})
            # library / playlists / art are cloud-only (personal data, LAN is unauthenticated)
            for action in ("arvio.media_search", "arvio.media_browse", "arvio.media_art"):
                status, body = self.post("/v1/hubs/hub_lan/commands", {"action": action, "entity_id": "media_player.saloni", "query": "miles"})
                self.assertEqual(status, 403, (action, body))
            # non-media actions: payload still not forwarded
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "light.turn_on", "entity_id": "light.a", "payload": {"brightness_pct": 5}})
            self.assertEqual(status, 200)
            self.assertEqual(calls[-1][2], {})
            # security actions stay forbidden on the LAN
            status, body = self.post("/v1/hubs/hub_lan/commands", {"action": "lock.unlock", "entity_id": "lock.a", "payload": {"confirm_dangerous": True}})
            self.assertEqual(status, 403)


# --- 9. review residual: scenario ownership ---------------------------------------------------------------


class ScenarioOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.fake = MediaFakeHa({
            "automation.fevgo": ("on", {"id": "arvio_fevgo", "friendly_name": "Φεύγω"}),
            "automation.other": ("on", {"id": "1699999", "friendly_name": "Other"}),
        })
        self.calls = []

        def fake_call(domain, service, data, wait_entity_ids=None, expected_attrs=None):
            self.calls.append((domain, service, data))
            return {"ok": True, "entity_id": data.get("entity_id"), "affected_entity_ids": [data.get("entity_id")]}

        self.patches = [mock.patch.object(agent, "ha", side_effect=self.fake), mock.patch.object(agent, "call_service", side_effect=fake_call)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_trigger_requires_arvio_id(self):
        for sid in ("1699999", "", "arvio_", "other_fevgo"):
            with self.assertRaises(ValueError, msg=sid):
                agent.trigger_scenario({"id": sid})
            with self.assertRaises(ValueError, msg=sid):
                agent.trigger_scenario({"id": sid, "entity_id": "automation.other"})
            with self.assertRaises(ValueError, msg=sid):
                agent.set_scenario_enabled({"id": sid, "enabled": False}, "automation.other")
        self.assertEqual(self.calls, [])
        # resolved by id via /states
        out = agent.trigger_scenario({"id": "arvio_fevgo"})
        self.assertEqual(self.calls[-1], ("automation", "trigger", {"entity_id": "automation.fevgo"}))
        self.assertEqual(out["id"], "arvio_fevgo")

    def test_explicit_entity_must_belong_to_scenario(self):
        # naming a foreign automation together with an arvio_ id is refused
        with self.assertRaises(ValueError):
            agent.trigger_scenario({"id": "arvio_fevgo", "entity_id": "automation.other"})
        with self.assertRaises(ValueError):
            agent.trigger_scenario({"id": "arvio_fevgo"}, "automation.other")
        with self.assertRaises(ValueError):
            agent.trigger_scenario({"id": "arvio_fevgo", "entity_id": "automation.missing"})
        with self.assertRaises(ValueError):
            agent.set_scenario_enabled({"id": "arvio_fevgo", "enabled": False, "entity_id": "automation.other"})
        self.assertEqual(self.calls, [])
        out = agent.trigger_scenario({"id": "arvio_fevgo", "entity_id": "automation.fevgo"})
        self.assertEqual(self.calls[-1], ("automation", "trigger", {"entity_id": "automation.fevgo"}))
        self.assertEqual(out["id"], "arvio_fevgo")
        out = agent.set_scenario_enabled({"id": "arvio_fevgo", "enabled": False}, "automation.fevgo")
        self.assertEqual(self.calls[-1], ("automation", "turn_off", {"entity_id": "automation.fevgo"}))
        self.assertIs(out["enabled"], False)
        # like delete_scenario
        with self.assertRaises(ValueError):
            agent.delete_scenario({"id": "1699999"})


if __name__ == "__main__":
    unittest.main()
