"""Push path: state event filtering, command_id mapping, coalescing, heartbeat shape."""
import json
import threading
import time
import unittest
from unittest import mock

from helpers import AREAS, DEVICES, ENTITY_REGISTRY_DISPLAY, FLOORS, agent, regs, st

RAW_REGISTRIES = {"floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": ENTITY_REGISTRY_DISPLAY}


class StateEventTest(unittest.TestCase):
    def setUp(self):
        agent.CONTEXT_TO_COMMAND = agent.LRU(500)
        self.regs = regs()

    def test_exposed_entity_message(self):
        agent.CONTEXT_TO_COMMAND.put("ctx_cmd", "cmd_42")
        new = st("light.kouzina", "on", friendly_name="Φως κουζίνας", brightness=51)
        new["context"] = {"id": "ctx_cmd", "parent_id": None, "user_id": "u1"}
        msg = agent.state_event_message({"entity_id": "light.kouzina", "old_state": None, "new_state": new}, self.regs)
        self.assertEqual(msg["type"], "state")
        self.assertEqual(msg["entity_id"], "light.kouzina")
        self.assertEqual(msg["state"], "on")
        self.assertEqual(msg["attrs"]["brightness_pct"], 20)
        self.assertEqual(msg["context"], {"id": "ctx_cmd", "parent_id": None, "user_id": "u1"})
        self.assertEqual(msg["command_id"], "cmd_42")
        self.assertEqual(msg["last_changed"], new["last_changed"])
        self.assertNotIn("friendly_name", msg["attrs"])

    def test_parent_id_maps_to_command(self):
        agent.CONTEXT_TO_COMMAND.put("ctx_parent", "cmd_7")
        new = st("cover.ypno", "closing", current_position=30)
        new["context"] = {"id": "ctx_child", "parent_id": "ctx_parent", "user_id": None}
        msg = agent.state_event_message({"entity_id": "cover.ypno", "new_state": new}, self.regs)
        self.assertEqual(msg["command_id"], "cmd_7")

    def test_unknown_context_has_no_command(self):
        new = st("light.saloni", "off")
        msg = agent.state_event_message({"entity_id": "light.saloni", "new_state": new}, self.regs)
        self.assertIsNone(msg["command_id"])
        self.assertEqual(msg["context"]["id"], "ctx_light_saloni")

    def test_not_exposed_entities_are_dropped(self):
        cases = [
            st("media_player.projector", "on", device_class="projector"),  # media device_class outside §15.2
            st("switch.config_thing", "on"),           # entity_category config
            st("binary_sensor.kinisi", "on", device_class="motion"),
            st("sensor.power", "5", device_class="power"),
            st("script.other", "on"),                  # no arvio label
            st("automation.fevgo", "on", id="arvio_fevgo"),
        ]
        for new in cases:
            self.assertIsNone(agent.state_event_message({"entity_id": new["entity_id"], "new_state": new}, self.regs), new["entity_id"])
        self.assertIsNone(agent.state_event_message({"entity_id": "light.saloni", "new_state": None}, self.regs))
        self.assertIsNone(agent.state_event_message(None, self.regs))

    def test_doorbell_call_state_event(self):
        eid = "binary_sensor.front_button_pressed"
        new = st(eid, "on")
        self.assertIsNone(agent.state_event_message({"entity_id": eid, "new_state": new}, self.regs))
        msg = agent.state_event_message({"entity_id": eid, "new_state": new}, self.regs, {eid})
        self.assertEqual(msg["type"], "state")
        self.assertEqual(msg["entity_id"], eid)
        self.assertEqual(msg["state"], "on")

    def test_handle_ha_event_dispatch(self):
        sent = []
        with mock.patch.object(agent, "registry_snapshot", return_value=self.regs), \
             mock.patch.object(agent, "PUSH", agent.Coalescer(sent.append, 1.0, clock=lambda: 100.0)), \
             mock.patch.object(agent, "schedule_registry_refresh") as sched:
            agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "light.saloni", "new_state": st("light.saloni", "on")}})
            agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "media_player.projector", "new_state": st("media_player.projector", "on", device_class="projector")}})
            agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": "media_player.tv", "new_state": st("media_player.tv", "on")}})
            agent.handle_ha_event({"event_type": "area_registry_updated", "data": {"action": "update", "area_id": "saloni"}})
            agent.handle_ha_event({"event_type": "call_service", "data": {}})
        self.assertEqual([m["entity_id"] for m in sent], ["light.saloni", "media_player.tv"])
        sched.assert_called_once_with("area_registry_updated")


class CoalescerTest(unittest.TestCase):
    def test_one_per_second_latest_wins(self):
        now = [100.0]
        sent = []
        c = agent.Coalescer(sent.append, 1.0, clock=lambda: now[0])
        self.assertTrue(c.offer("light.a", {"n": 1}))
        self.assertFalse(c.offer("light.a", {"n": 2}))
        self.assertFalse(c.offer("light.a", {"n": 3}))
        self.assertTrue(c.offer("light.b", {"n": 4}))  # other key unaffected
        self.assertEqual(sent, [{"n": 1}, {"n": 4}])
        self.assertEqual(c.flush(), 0)
        now[0] = 100.5
        self.assertEqual(c.flush(), 0)
        now[0] = 101.0
        self.assertEqual(c.flush(), 1)
        self.assertEqual(sent[-1], {"n": 3})
        self.assertEqual(c.pending(), 0)
        # right after a flush the key is rate limited again
        self.assertFalse(c.offer("light.a", {"n": 5}))
        now[0] = 102.5
        self.assertEqual(c.flush(), 1)
        self.assertEqual(sent[-1], {"n": 5})
        self.assertTrue(c.offer("light.b", {"n": 6}))

    def test_send_errors_do_not_leak(self):
        def boom(_msg):
            raise RuntimeError("socket closed")
        c = agent.Coalescer(boom, 1.0, clock=lambda: 1.0)
        with self.assertRaises(RuntimeError):
            c.offer("k", {})
        # relay_send itself never raises when not connected
        agent.RELAY_WS = None
        self.assertFalse(agent.relay_send({"type": "heartbeat"}))


class RelaySendTest(unittest.TestCase):
    def test_sends_json_when_connected(self):
        frames = []

        class Ws:
            def send(self, data):
                frames.append(data)

        with mock.patch.object(agent, "RELAY_WS", Ws()):
            self.assertTrue(agent.relay_send({"type": "model", "snapshot_version": 3}))
            agent.push_model_changed(4)
        self.assertIn('"type": "model"', frames[0])
        self.assertIn('"snapshot_version": 4', frames[1])

        class Broken:
            def send(self, data):
                raise OSError("gone")

        with mock.patch.object(agent, "RELAY_WS", Broken()):
            self.assertFalse(agent.relay_send({"type": "heartbeat"}))


class RelayResultTest(unittest.TestCase):
    def test_relay_command_posts_ack(self):
        posted = []
        with mock.patch.object(agent, "relay_http", side_effect=lambda m, p, b=None, timeout=25: posted.append((p, b))), \
             mock.patch.object(agent, "handle_command", return_value={"command_id": "c9", "ok": False, "error": "expired"}):
            agent._handle_relay_command({"command_id": "c9", "action": "light.turn_on"})
        path, body = posted[0]
        self.assertEqual(path, "/v1/hub/results")
        self.assertEqual(body["command_id"], "c9")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "expired")
        self.assertEqual(body["data"]["error"], "expired")


class BackoffTest(unittest.TestCase):
    def test_ladder_jitter_and_reset(self):
        no_jitter = agent.Backoff(rand=lambda a, b: 0.0)
        self.assertEqual([no_jitter.next_sleep() for _ in range(8)], [1, 2, 4, 8, 16, 32, 60, 60])
        no_jitter.reset()
        self.assertEqual(no_jitter.next_sleep(), 1)
        max_jitter = agent.Backoff(rand=lambda a, b: b)  # jitter ≤ delay / 2
        self.assertEqual([max_jitter.next_sleep() for _ in range(7)], [1.5, 3, 6, 12, 24, 48, 90])
        for _ in range(50):
            s = agent.Backoff().next_sleep()
            self.assertTrue(1.0 <= s <= 1.5, s)
        self.assertEqual((agent.BACKOFF_MIN_S, agent.BACKOFF_MAX_S), (1.0, 60.0))


class RelayBackoffTest(unittest.TestCase):
    def test_relay_loop_backs_off_and_hello_resets(self):
        sleeps = []

        class Stop(Exception):
            pass

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 5:
                raise Stop

        backoff = agent.Backoff(rand=lambda a, b: 0.0)
        with mock.patch.object(agent, "RELAY_URL", "https://relay.test"), \
             mock.patch.object(agent, "hub_id", "hub_x"), \
             mock.patch.object(agent, "RELAY_BACKOFF", backoff), \
             mock.patch.object(agent, "relay_http", side_effect=OSError("relay down")) as http:
            with self.assertRaises(Stop):
                agent.relay_loop(sleep=sleep)
        self.assertEqual(sleeps, [1, 2, 4, 8, 16])  # exponential, not a flat 1 s
        self.assertEqual(http.call_count, 5)  # one register attempt per round
        # a relay hello resets the ladder to 1 s
        with mock.patch.object(agent, "RELAY_BACKOFF", backoff), \
             mock.patch.object(agent, "relay_ok", False), mock.patch.object(agent, "relay_err", "x"):
            agent.handle_relay_message(json.dumps({"type": "hello", "hub_id": "hub_x"}))
            self.assertTrue(agent.relay_ok)
            self.assertEqual(agent.relay_err, "")
        self.assertEqual(backoff.delay, 1.0)
        self.assertEqual(backoff.next_sleep(), 1)
        # long-poll failure after a good session also backs off from 1 s again
        sleeps.clear()
        with mock.patch.object(agent, "RELAY_URL", "https://relay.test"), \
             mock.patch.object(agent, "hub_id", "hub_x"), \
             mock.patch.object(agent, "RELAY_BACKOFF", backoff), \
             mock.patch.object(agent, "relay_http", side_effect=OSError("relay down")):
            with self.assertRaises(Stop):
                agent.relay_loop(sleep=sleep)
        self.assertEqual(sleeps[:2], [2, 4])  # continues the ladder (one step was consumed above)

    def test_relay_message_dispatch(self):
        got = threading.Event()
        seen = []

        def handled(cmd):
            seen.append(cmd)
            got.set()

        with mock.patch.object(agent, "_handle_relay_command", side_effect=handled):
            agent.handle_relay_message("not json")
            agent.handle_relay_message(json.dumps(["list"]))
            agent.handle_relay_message(json.dumps({"type": "command", "command": "nope"}))
            agent.handle_relay_message(json.dumps({"type": "command", "command": {"command_id": "c1", "action": "light.turn_on"}}))
            self.assertTrue(got.wait(2))
        self.assertEqual(seen, [{"command_id": "c1", "action": "light.turn_on"}])


class RegistryGateTest(unittest.TestCase):
    def test_state_events_dropped_until_registry_loaded_then_model_push(self):
        sent = []
        r = regs()
        fp = agent.registry_fingerprint(r["floors"], r["areas"], r["devices"], r["entity_regs"])
        agent.save_hub({"hub_id": "hub_gate", "snapshot_version": 3, "registry_fingerprint": fp})
        agent.STATE_FEED_RECENT = agent.LRU(1000)
        with mock.patch.object(agent, "REGISTRY_CACHE", agent.RegistrySnapshot()), \
             mock.patch.object(agent, "PUSH", agent.Coalescer(sent.append, 1.0, clock=lambda: 100.0)), \
             mock.patch.object(agent, "fetch_registries", return_value=RAW_REGISTRIES):
            self.assertFalse(agent.registry_snapshot().loaded)
            agent.handle_ha_event({"event_type": "state_changed",
                                   "data": {"entity_id": "light.saloni", "new_state": st("light.saloni", "on")}})
            self.assertEqual(sent, [])  # no registry → cannot tell exposed from hidden → dropped
            self.assertIsNotNone(agent.STATE_FEED_RECENT.get("light.saloni"))  # commands waiting on it are still fed
            self.assertTrue(agent.refresh_registry_cache("periodic"))
            self.assertEqual([m["type"] for m in sent], ["model"])  # loaded → clients refetch (no bump: version stays)
            self.assertEqual(sent[0]["snapshot_version"], 3)
            agent.handle_ha_event({"event_type": "state_changed",
                                   "data": {"entity_id": "light.saloni", "new_state": st("light.saloni", "off")}})
            self.assertEqual([m["type"] for m in sent], ["model", "state"])
            self.assertTrue(agent.refresh_registry_cache("periodic"))  # warm + unchanged → silent
            self.assertEqual(len(sent), 2)
        self.assertEqual(agent.get_snapshot_version(), 3)


class RefreshLockTest(unittest.TestCase):
    def test_concurrent_refresh_runs_once(self):
        gate = threading.Event()
        entered = threading.Event()
        fetches = []

        def slow_fetch():
            fetches.append(1)
            entered.set()
            gate.wait(3)
            return RAW_REGISTRIES

        agent.save_hub({"hub_id": "hub_lock", "snapshot_version": 1})
        results = []
        with mock.patch.object(agent, "fetch_registries", side_effect=slow_fetch), \
             mock.patch.object(agent, "push_model_changed") as pushed, \
             mock.patch.object(agent, "REGISTRY_CACHE", agent.RegistrySnapshot()):
            t1 = threading.Thread(target=lambda: results.append(agent.refresh_registry_cache("a")))
            t1.start()
            self.assertTrue(entered.wait(2))
            t2 = threading.Thread(target=lambda: results.append(agent.refresh_registry_cache("b", force_bump=True)))
            t2.start()
            time.sleep(0.1)
            self.assertEqual(len(fetches), 1)  # the second call did not stack another HA round-trip
            self.assertTrue(t2.is_alive())  # …it waits for the in-flight one instead
            gate.set()
            t1.join(3)
            t2.join(3)
            self.assertEqual(results, [True, True])
            self.assertEqual(len(fetches), 1)
            self.assertTrue(agent.registry_snapshot().loaded)
            # first load pushed once; the skipped-but-forced call still bumped + pushed
            self.assertEqual([c.args[0] for c in pushed.call_args_list], [1, 2])
            # a failed fetch releases the lock for the next caller
            with mock.patch.object(agent, "fetch_registries", side_effect=RuntimeError("HA down")):
                self.assertFalse(agent.refresh_registry_cache("c"))
            with mock.patch.object(agent, "fetch_registries", return_value=RAW_REGISTRIES):
                self.assertTrue(agent.refresh_registry_cache("d"))
        self.assertFalse(agent.REGISTRY_REFRESH_LOCK.locked())


class RegistrySnapshotTest(unittest.TestCase):
    def test_immutable_and_subscriptable(self):
        r = regs()
        snap = agent.RegistrySnapshot(floors=r["floors"], areas=r["areas"], devices=r["devices"],
                                      entity_regs=r["entity_regs"], raw={"floors": FLOORS}, loaded_at=5.0)
        self.assertEqual(snap["loaded_at"], 5.0)
        self.assertTrue(snap.loaded)
        self.assertFalse(agent.RegistrySnapshot().loaded)
        self.assertEqual(snap["areas"]["saloni"]["name"], "Σαλόνι")
        self.assertEqual(snap.raw["floors"], FLOORS)
        self.assertEqual(snap.get("nope", "dflt"), "dflt")
        with self.assertRaises(KeyError):
            snap["nope"]
        with self.assertRaises(TypeError):
            snap.areas["x"] = {}
        with self.assertRaises(TypeError):
            del snap.entity_regs["light.saloni"]
        with self.assertRaises(TypeError):
            snap.loaded_at = 1.0
        self.assertIsInstance(snap.floors, tuple)
        # readers share the one object — no per-event copies — and a refresh swaps it whole
        with mock.patch.object(agent, "REGISTRY_CACHE", snap):
            self.assertIs(agent.registry_snapshot(), snap)
            self.assertIs(agent.registry_snapshot(), agent.registry_snapshot())
            self.assertIs(agent.registries_for_commands(), snap)
        # the push builder and target resolution work on the read-only mappings
        msg = agent.state_event_message({"entity_id": "light.saloni", "new_state": st("light.saloni", "on")}, snap)
        self.assertEqual(msg["entity_id"], "light.saloni")
        self.assertEqual(
            agent.resolve_target_entities({"floor_id": "isogeio"}, "light", snap["entity_regs"], snap["devices"], snap["areas"]),
            ["light.kouzina", "light.saloni"],
        )
        self.assertEqual(agent.registry_fingerprint(snap["floors"], snap["areas"], snap["devices"], snap["entity_regs"]),
                         agent.registry_fingerprint(r["floors"], r["areas"], r["devices"], r["entity_regs"]))


class CloudHeartbeatTest(unittest.TestCase):
    """The cloud heartbeat (POST /v1/hubs/{id}/heartbeat) carries snapshot_version so the
    cloud can serve site_model_cache without asking the hub (cloud hubSnapshotVersion)."""

    def test_heartbeat_payload_carries_snapshot_version(self):
        saved = (agent.hub_id, agent.hub_state, agent.err, agent.load_hub())
        requests = []

        class FakeResp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            if req.full_url.endswith("/v1/hubs/enroll"):
                return FakeResp(b'{"hub_id": "hub_hb", "state": "prepared"}')
            return FakeResp(b"{}")

        try:
            agent.save_hub({"hub_id": "hub_hb", "snapshot_version": 17})
            agent.hub_id = "hub_hb"
            with mock.patch.object(agent.urllib.request, "urlopen", side_effect=fake_urlopen):
                agent.enroll_remote()
            hb = [r for r in requests if r.full_url.endswith("/v1/hubs/hub_hb/heartbeat")]
            self.assertEqual(len(hb), 1)
            body = json.loads(hb[0].data.decode())
            self.assertEqual(body["snapshot_version"], 17)
            self.assertEqual(body["snapshot_version"], agent.get_snapshot_version())
            self.assertIn("presence_code_hash", body)
            self.assertIn("presence_expires_at", body)
            self.assertIn("enroll_public_key", body)
        finally:
            agent.hub_id, agent.hub_state, agent.err = saved[0], saved[1], saved[2]
            agent.save_hub(saved[3])

    def test_apply_heartbeat_reply_stores_relay_token(self):
        saved = (agent.RELAY_TOKEN, agent.load_hub())
        try:
            agent.RELAY_TOKEN = "old-fleet"
            agent.save_hub({"hub_id": "hub_tok"})
            changed = agent.apply_heartbeat_reply(
                {"relay_token": "per-hub-secret", "update_channel": "lab", "agent_target_version": "0.1.23"}
            )
            self.assertTrue(changed)
            self.assertEqual(agent.RELAY_TOKEN, "per-hub-secret")
            st = agent.load_hub()
            self.assertEqual(st["relay_token"], "per-hub-secret")
            self.assertEqual(st["update_channel"], "lab")
            self.assertEqual(st["agent_target_version"], "0.1.23")
            self.assertFalse(agent.apply_heartbeat_reply({"relay_token": "per-hub-secret"}))
        finally:
            agent.RELAY_TOKEN = saved[0]
            agent.save_hub(saved[1])


if __name__ == "__main__":
    unittest.main()
