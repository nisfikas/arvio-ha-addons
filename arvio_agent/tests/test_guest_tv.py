"""Guest TV (docs/plans/2026-11-07-guest-tv.md in the monorepo): a TV in a rented room reads its own
welcome page and nothing else, and commands nothing. The checks are the domain's
(packages/domain/tests/guest-tv.test.ts runs the same samples)."""
import json
import pathlib
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent, signed

import guest_tv

ROOT = pathlib.Path(__file__).resolve().parents[1]
HUB = "hub_tv"
RELAY = "relay-token-for-the-test"
TV_SECRET = "tv-secret"
PANEL_SECRET = "panel-secret"
STAY = {"id": "sty_abc123", "guest_name": "Anna", "check_in": "2026-11-07T13:00:00Z", "check_out": "2026-11-10T09:00:00Z", "language": "en"}
PROFILE = {
    "tv": {"blocks": [{"kind": "logo"}, {"kind": "wifi"}, {"kind": "stay"}, {"kind": "apps"}], "theme": {"preset": "afisa"}, "apps": ["netflix"]},
    "wifi_name": "Villa",
    "wifi_password": "secret-wifi",
}


def code(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    raise AssertionError("did not raise")


class ParseTest(unittest.TestCase):
    def test_the_domains_samples(self):
        tv = guest_tv.parse_guest_tv({
            "blocks": [
                {"kind": "welcome", "title": {"el": " Καλώς ήρθατε ", "en": "Welcome"}, "text": "Στο δωμάτιο 3"},
                {"kind": "sections", "items": [{"title": "Check-out", "text": {"el": "Κλειδιά", "en": "Keys"}}]},
                {"kind": "rules", "items": ["Όχι κάπνισμα", {"en": "Quiet after 23:00"}]},
                {"kind": "contacts", "items": [{"name": "Reception", "phone": "+30 210 000 0000"}]},
                {"kind": "link", "url": "https://example.com/menu", "caption": {"en": "Menu"}},
                {"kind": "weather"},
            ],
            "theme": {"preset": "afisa", "mode": "dark"},
            "apps": ["live_tv", "netflix"],
        })
        self.assertEqual(tv["blocks"][0], {"kind": "welcome", "title": {"el": "Καλώς ήρθατε", "en": "Welcome"}, "text": {"el": "Στο δωμάτιο 3", "en": None}})
        self.assertEqual(tv["theme"], {"preset": "afisa", "mode": "dark", "colors": None})
        self.assertEqual(guest_tv.parse_guest_tv({"blocks": []})["apps"], [])
        refusals = [
            ({"blocks": [{"kind": "video"}]}, "invalid_block"),
            ({"blocks": [{"kind": "wifi"}, {"kind": "wifi"}]}, "duplicate_block"),
            ({"blocks": [{"kind": "weather"}, {"kind": "weather"}]}, "duplicate_block"),
            ({"blocks": [{"kind": "text", "text": "x"}] * 17}, "invalid_blocks"),
            ({"blocks": [{"kind": "link", "url": "http://example.com", "caption": "x"}]}, "invalid_link"),
            ({"blocks": [{"kind": "link", "url": "javascript:alert(1)", "caption": "x"}]}, "invalid_link"),
            ({"blocks": [{"kind": "welcome", "title": {"el": " ", "en": ""}}]}, "invalid_welcome"),
            ({"blocks": [{"kind": "sections", "items": []}]}, "invalid_sections"),
            ({"blocks": [{"kind": "contacts", "items": [{"name": "A", "phone": "call me"}]}]}, "invalid_contacts"),
            ({"blocks": [{"kind": "text", "text": "x" * 801}]}, "invalid_text"),
            ({"blocks": [], "apps": ["netflix", "netflix"]}, "invalid_apps"),
            ({"blocks": [], "apps": ["com.netflix.ninja"]}, "invalid_apps"),
            ({"blocks": [], "theme": {"preset": "neon"}}, "invalid_theme"),
            ({"blocks": [], "theme": {"colors": {"bg": "#141414", "ink": "#444444", "accent": "#ffffff"}}}, "low_contrast"),
            ({"blocks": [], "theme": {"colors": {"bg": "#141414", "ink": "#ffffff", "accent": "#2a2a2a"}}}, "low_contrast"),
        ]
        for raw, want in refusals:
            self.assertEqual(code(lambda: guest_tv.parse_guest_tv(raw)), want, raw)
        ok = guest_tv.parse_guest_tv({"blocks": [], "theme": {"colors": {"bg": "#141414", "ink": "#EEEBE5", "accent": "#e0603a"}}})
        self.assertEqual(ok["theme"]["colors"], {"bg": "#141414", "ink": "#eeebe5", "accent": "#e0603a"})
        self.assertEqual(round(guest_tv.contrast_ratio("#000000", "#ffffff")), 21)

    def test_a_stay(self):
        stay = {"id": "sty_abc123", "check_in": "2026-11-07T13:00:00Z", "check_out": "2026-11-10T09:00:00.000Z"}
        self.assertEqual(guest_tv.parse_guest_stay(stay), {
            "id": "sty_abc123", "guest_name": None, "check_in": "2026-11-07T13:00:00.000Z",
            "check_out": "2026-11-10T09:00:00.000Z", "language": "en", "pass_url": None, "source": "manual",
        })
        self.assertIsNone(guest_tv.parse_guest_stay(None))
        for bad in ({"check_out": stay["check_in"]}, {"check_out": "2028-01-01T00:00:00Z"}, {"pass_url": "http://x.y/g"},
                    {"source": "Booking.com"}, {"id": "x"}, {"check_in": "07/11/2026"}):
            self.assertEqual(code(lambda: guest_tv.parse_guest_stay({**stay, **bad})), "invalid_stay", bad)

    def test_the_logo_is_a_png_or_a_jpeg(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        self.assertEqual(guest_tv.parse_guest_logo({"mime": "image/png", "data_base64": png})["mime"], "image/png")
        self.assertEqual(code(lambda: guest_tv.parse_guest_logo({"mime": "image/jpeg", "data_base64": png})), "invalid_logo")
        self.assertEqual(code(lambda: guest_tv.parse_guest_logo({"mime": "image/svg+xml", "data_base64": png})), "invalid_logo")

    def test_the_page_gets_the_profile_only_when_it_changed(self):
        row = {"name": "Δωμάτιο 3", "stay": STAY}
        doc = guest_tv.parse_put_guest_tv(PROFILE)
        first = guest_tv.tv_body(row, doc, have=None, now="t", tz="Europe/Athens", weather=None, hub=HUB)
        self.assertEqual(first["tv"]["wifi"], {"name": "Villa", "password": "secret-wifi"})
        self.assertEqual(first["tv"]["stay"]["guest_name"], "Anna")
        again = guest_tv.tv_body(row, doc, have=first["version"], now="t", tz="Europe/Athens", weather=None, hub=HUB)
        self.assertTrue(again["same"])
        self.assertNotIn("tv", again)
        moved = guest_tv.tv_body({**row, "stay": None}, doc, have=first["version"], now="t", tz="Europe/Athens", weather=None, hub=HUB)
        self.assertIn("tv", moved)


class GuestKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent.H)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        for target, new in (("mode", "remote"), ("hub_id", HUB), ("RELAY_TOKEN", RELAY)):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        fake_call = lambda domain, service, data, *a, **k: self.sent.append(f"{domain}.{service}") or {"ok": True}
        fake_ha = lambda path, *a, **k: [{"entity_id": "light.a", "state": "on", "attributes": {}}] if path == "/states" else None
        for target, new in (("call_service", fake_call), ("ha", fake_ha)):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)
        agent.save_screens({"screens": {
            "scr_tv": {"screen_id": "scr_tv", "name": "Δωμάτιο 3", "kind": "guest_tv", "stay": STAY,
                       "screen_secret_hash": agent.hash_screen_secret(TV_SECRET)},
            "scr_wall": {"screen_id": "scr_wall", "name": "Σαλόνι", "screen_secret_hash": agent.hash_screen_secret(PANEL_SECRET)},
        }})
        agent.put_guest_tv(PROFILE)
        self.addCleanup(agent.save_screens, {"screens": {}})
        self.addCleanup(lambda: agent.GUEST_TV.unlink(missing_ok=True))

    def raw(self, method, path, data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def get(self, path, secret=None, headers=None):
        h = {**(signed("GET", path, b"", secret=secret) if secret else {}), **(headers or {})}
        return self.raw("GET", path, None, h)

    def test_a_tv_reads_its_own_page(self):
        status, body, headers = self.get("/api/tv?screen_id=scr_tv", TV_SECRET)
        self.assertEqual(status, 200, body)
        out = json.loads(body)
        self.assertEqual(out["tv"]["name"], "Δωμάτιο 3")
        self.assertEqual(out["tv"]["apps"], ["netflix"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertTrue(agent.load_screens()["screens"]["scr_tv"]["last_tv_poll_at"])
        status, body, _ = self.get(f"/api/tv?screen_id=scr_tv&have={out['version']}", TV_SECRET)
        self.assertEqual(json.loads(body).get("same"), True)

    def test_nobody_else_reads_it(self):
        self.assertEqual(self.get("/api/tv?screen_id=scr_tv")[0], 403)
        self.assertEqual(self.get("/api/tv?screen_id=scr_tv", "wrong")[0], 403)
        # A wall panel's member key is not the TV's key, and a wall screen is not a TV.
        self.assertEqual(self.get("/api/tv?screen_id=scr_tv", PANEL_SECRET)[0], 403)
        self.assertEqual(self.get("/api/tv?screen_id=scr_wall", PANEL_SECRET)[0], 422)
        self.assertEqual(self.get("/api/tv?screen_id=scr_nope", TV_SECRET)[0], 404)

    def test_the_tv_key_moves_nothing_and_reads_nothing_else(self):
        for path, body in (("/api/service/light/turn_on", {"entity_id": "light.a"}),
                           (f"/v1/hubs/{HUB}/commands", {"action": "light.turn_on", "entity_id": "light.a"})):
            data = json.dumps(body).encode()
            self.assertEqual(self.raw("POST", path, data, signed("POST", path, data, secret=TV_SECRET))[0], 403, path)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.get("/api/screens?screen_id=scr_tv", TV_SECRET)[0], 403)
        status, body, _ = self.get("/api/status", TV_SECRET)
        self.assertNotIn("entities", json.loads(body))
        self.assertFalse(agent.paired_screen_secret_ok(TV_SECRET))
        self.assertTrue(agent.paired_screen_secret_ok(PANEL_SECRET))

    def test_a_tv_shell_may_call_the_tv_paths_from_its_own_origin_and_nothing_else(self):
        status, _, headers = self.raw("OPTIONS", "/api/tv?screen_id=scr_tv", headers={"Origin": "null"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn(agent.LAN_SIG_HEADER, headers.get("Access-Control-Allow-Headers", ""))
        status, _, headers = self.raw("OPTIONS", "/api/screens", headers={"Origin": "null"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.get("/api/tv?screen_id=scr_tv", TV_SECRET, {"Origin": "null"})[0], 200)
        self.assertEqual(self.get("/api/screens?screen_id=scr_wall", PANEL_SECRET, {"Origin": "null"})[0], 403)
        # Six digits only from the hub's own origin: a web page could guess them.
        status, body, _ = self.raw("POST", "/api/screens/pair", json.dumps({"code": "123456"}).encode(), {"Origin": "https://evil.example"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "token_required"))

    def test_the_tv_page_never_pairs_with_a_wall_screens_code(self):
        rows = agent.load_screens()
        rows["screens"]["scr_wall"].update(pairing_code_hash=agent.hash_screen_pairing("123456", "", "scr_wall"), pairing_used_at=None, pairing_expires_at=None)
        agent.save_screens(rows)
        self.assertEqual(code(lambda: agent.pair_wall_screen("123456", "guest_tv")), "wrong_screen_kind")
        self.assertTrue(agent.pairing_open(agent.load_screens()["screens"]["scr_wall"]), "the code is not spent")

    def test_a_tv_turned_into_a_wall_screen_pairs_again(self):
        agent.put_wall_screen({"screen_id": "scr_tv", "name": "Δωμάτιο 3", "kind": "guest_tv", "stay": None})
        self.assertIsNone(agent.load_screens()["screens"]["scr_tv"]["stay"])
        self.assertTrue(agent.load_screens()["screens"]["scr_tv"]["screen_secret_hash"])
        agent.put_wall_screen({"screen_id": "scr_tv", "name": "Δωμάτιο 3", "kind": "wall", "pages": [{"id": "p1", "tiles": [{"kind": "clock"}]}]})
        row = agent.load_screens()["screens"]["scr_tv"]
        self.assertNotIn("screen_secret_hash", row)
        self.assertNotIn("kind", row)
        self.assertEqual(code(lambda: agent.put_wall_screen({"screen_id": "scr_x", "name": "x", "kind": "fridge"})), "invalid_kind")

    def test_a_bad_profile_from_the_cloud_leaves_the_last_good_one(self):
        before = agent.GUEST_TV.read_text(encoding="utf-8")
        self.assertEqual(code(lambda: agent.put_guest_tv({"tv": {"blocks": [{"kind": "video"}]}})), "invalid_block")
        self.assertEqual(agent.GUEST_TV.read_text(encoding="utf-8"), before)

    def test_the_page_is_one_file_with_its_fonts(self):
        status, body, headers = self.raw("GET", "/tv")
        self.assertEqual(status, 200)
        self.assertIn(b"data:font/woff2;base64,", body)
        self.assertNotIn(b'"/panel/fonts/', body)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        status, _, _ = self.raw("GET", "/tv", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)


class PageTest(unittest.TestCase):
    page = (ROOT / "tv.html").read_text(encoding="utf-8")

    def test_it_runs_on_a_2018_samsung(self):
        """Chromium M56 (contract §3.1): none of what came later."""
        script = "\n".join(re.findall(r"<script>(.*?)</script>", self.page, re.S))
        # The signing block is the panel's byte for byte; the page polyfills its one padStart.
        script = re.sub(r"// lan-sign:start.*?// lan-sign:end", "", script, flags=re.S)
        self.assertIn("String.prototype.padStart = function", self.page)
        style = "\n".join(re.findall(r"<style>(.*?)</style>", self.page, re.S))
        for bad in (r"display:\s*grid", r"(?<![-\w])gap\s*:", r"\bclamp\(", r"\bmin\(", r"\bmax\(", r"aspect-ratio", r"\binset\s*:", r":is\(",
                    r":focus-visible", r"display:\s*contents", r"#[0-9a-fA-F]{8}\b"):
            self.assertIsNone(re.search(bad, style), bad)
        for bad in (r"\.\.\.\s*[A-Za-z_({\[]", r"\.padStart\(", r"\.padEnd\(", r"\.finally\(", r"\.flat\(", r"\?\.", r"\?\?", r"\bglobalThis\b"):
            self.assertIsNone(re.search(bad, script), bad)

    def test_it_signs_exactly_as_the_wall_panel_does(self):
        panel = (ROOT / "panel.html").read_text(encoding="utf-8")
        block = lambda html: html.split("// lan-sign:start", 1)[1].split("// lan-sign:end", 1)[0]
        self.assertEqual(block(self.page), block(panel))

    def test_it_has_the_wall_panels_themes(self):
        panel = (ROOT / "panel.html").read_text(encoding="utf-8")
        themes = lambda html: re.search(r"const THEMES = \{.*?\n\s*\};", html, re.S).group(0)
        self.assertEqual(themes(self.page), themes(panel))

    def test_every_word_has_greek_and_english(self):
        words = re.search(r"var S = \{\s*el: \{(.*?)\},\s*en: \{(.*?)\},\s*\};", self.page, re.S)
        keys = lambda block: sorted(re.findall(r"(\w+):", re.sub(r'"[^"]*"', '""', block)))
        self.assertEqual(keys(words.group(1)), keys(words.group(2)))

    def test_no_platform_name_reaches_a_guest(self):
        visible = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", self.page, flags=re.S)
        for word in ("Home Assistant", "SFK"):
            self.assertNotIn(word, visible)


class ShippedTest(unittest.TestCase):
    def test_the_images_carry_the_tv_files(self):
        for name in ("Dockerfile", "Dockerfile.standalone"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for f in ("guest_tv.py", "tv.html"):
                self.assertIn(f, text, f"{name} {f}")


if __name__ == "__main__":
    unittest.main()
