"""ΡΩΤΑ ΣΤΟ ΠΑΝΕΛ: a flow's question on the wall, answered by the first signed press."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import agent, signed

SECRET = "panel-secret"
TOKEN = "0123abcd-89ab-cdef-0123-456789abcdef"


class PanelAskTest(unittest.TestCase):
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
        agent.save_screens(
            {
                "screens": {
                    "scr_a": {"screen_id": "scr_a", "screen_secret_hash": agent.hash_screen_secret(SECRET), "pages": []},
                    "scr_old": {"screen_id": "scr_old", "pages": []},  # paired before 0.1.50: no secret
                }
            }
        )
        self.addCleanup(agent.save_screens, {"screens": {}})
        self.addCleanup(agent.PANEL_ASKS.clear)
        self.core = []
        self.core_says = {"type": "create_entry"}

        def fake_core(path, method="GET", body=None, timeout=30):
            self.core.append((path, body))
            return {"flow_id": "f1", "type": "form"} if path.endswith("/flow") else self.core_says

        for target, new in (("ha_or_raise", fake_core), ("ha", lambda *a, **k: None), ("entities", lambda: []), ("registries_for_commands", lambda: {})):
            p = mock.patch.object(agent, target, new)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, **extra):
        agent.handle_ha_event({"event_type": agent.PANEL_ASK_EVENT, "data": {"token": TOKEN, "alias": "Καταιγίδα", "message": "Να κλείσουν τα ρολά;", "yes": "Ναι", "no": "Όχι", "seconds": 60, **extra}})

    def press(self, choice, secret=SECRET, screen_id="scr_a"):
        path = "/api/screens/ask"
        data = json.dumps({"screen_id": screen_id, "token": TOKEN, "choice": choice}).encode()
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers.update(signed("POST", path, data, secret=secret))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def shown(self, screen_id="scr_a"):
        return agent.panel_snapshot(screen_id, SECRET if screen_id == "scr_a" else None)["screens"][0].get("ask")

    def test_a_signed_panel_shows_the_question_and_an_old_one_does_not(self):
        self.ask()
        self.assertEqual(self.shown(), {"token": TOKEN, "alias": "Καταιγίδα", "message": "Να κλείσουν τα ρολά;", "yes": "Ναι", "no": "Όχι"})
        self.assertIsNone(self.shown("scr_old"))
        self.assertIn(agent.PANEL_ASK_EVENT, agent.HA_EVENT_TYPES)

    def test_the_first_press_goes_to_core_and_the_question_is_gone(self):
        self.ask()
        self.assertEqual(self.press("no"), 200)
        self.assertEqual(self.core, [("/config/config_entries/flow", {"handler": "arvio_panel_answer"}), ("/config/config_entries/flow/f1", {"token": TOKEN, "choice": "no"})])
        self.assertIsNone(self.shown())
        self.assertEqual(self.press("yes"), 410)
        self.assertEqual(len(self.core), 2)

    def test_the_wifi_is_not_a_key(self):
        self.ask()
        self.assertEqual(self.press("yes", secret=None), 403)
        self.assertEqual(self.press("yes", secret="wrong"), 403)
        self.assertEqual(self.press("yes", secret=None, screen_id="scr_old"), 403)
        self.assertEqual(self.core, [])
        self.assertIsNotNone(self.shown())

    def test_only_the_buttons_it_has(self):
        self.ask(no="")
        self.assertNotIn("no", self.shown())
        self.assertEqual(self.press("no"), 410)
        self.assertEqual(self.press("anything"), 410)
        self.assertEqual(self.core, [])

    def test_answered_elsewhere_or_expired_leaves_nothing_to_press(self):
        self.ask()
        agent.handle_ha_event({"event_type": agent.PANEL_ASK_EVENT, "data": {"token": TOKEN, "done": True}})
        self.assertIsNone(self.shown())
        self.ask()
        agent.PANEL_ASKS[TOKEN]["until"] = 0
        self.assertIsNone(self.shown())
        self.assertEqual(self.press("yes"), 410)
        self.core_says = {"type": "form", "errors": {"base": "not_found"}}
        self.ask()
        self.assertEqual(self.press("yes"), 410, "Core no longer waits: another panel was first")

    def test_core_away_keeps_the_question_for_another_press(self):
        self.ask()
        with mock.patch.object(agent, "ha_or_raise", side_effect=RuntimeError("core down")):
            self.assertEqual(self.press("yes"), 502)
        self.assertIsNotNone(self.shown())

    def test_nonsense_from_core_is_not_shown(self):
        for bad in ({"token": "x"}, {"token": TOKEN, "message": "", "yes": "Ναι", "seconds": 5}, {"token": TOKEN, "message": "Μ", "yes": "", "seconds": 5}, {"token": TOKEN, "message": "Μ", "yes": "Ναι", "seconds": "σύντομα"}):
            agent.handle_ha_event({"event_type": agent.PANEL_ASK_EVENT, "data": bad})
        self.assertEqual(agent.PANEL_ASKS, {})


if __name__ == "__main__":
    unittest.main()
