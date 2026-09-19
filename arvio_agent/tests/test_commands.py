"""Command safety (expiry / dedupe / confirm), target resolution, services, batch, acks."""
import threading
import time
import unittest
from unittest import mock

from helpers import agent, regs

FUTURE = "2999-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


class FakeHa:
    """Stand-in for agent.ha(): records service calls, serves states."""

    def __init__(self, states: dict, context_id="ctx_abc"):
        self.states = {k: {"entity_id": k, "state": v, "attributes": {}} for k, v in states.items()}
        self.calls = []
        self.context_id = context_id

    def __call__(self, path, method="GET", body=None, timeout=10):
        if path.startswith("/services/"):
            self.calls.append((path, body))
            ids = body.get("entity_id") if isinstance(body, dict) else None
            ids = ids if isinstance(ids, list) else ([ids] if ids else [])
            if isinstance(body, dict) and (body.get("area_id") or body.get("floor_id")):
                # native target: pretend HA resolved every entity of the domain
                domain = path.split("/")[2]
                ids = ids + [e for e in self.states if e.startswith(domain + ".")]
            changed = []
            for eid in ids:
                if eid in self.states:
                    domain = eid.split(".")[0]
                    svc = path.rsplit("/", 1)[1]
                    expected = agent._expected_state_for(domain, svc)
                    if expected:
                        self.states[eid]["state"] = expected
                    changed.append({**self.states[eid], "context": {"id": self.context_id}})
            return changed
        if path == "/states":
            return list(self.states.values())
        if path.startswith("/states/"):
            return self.states.get(path[len("/states/"):])
        if path == "/config":
            return {"version": "2025.8.1", "time_zone": "Europe/Athens"}
        return None


class SafetyTest(unittest.TestCase):
    def check(self, **cmd):
        return agent.check_command_safety(cmd, via=cmd.pop("via", "relay") if "via" in cmd else "relay")

    def test_expiry(self):
        agent.check_command_safety({"action": "light.turn_on", "expires_at": FUTURE})
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_on", "expires_at": PAST})
        self.assertEqual(cm.exception.code, "expired")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_on", "status": "expired"})
        self.assertEqual(cm.exception.code, "expired")
        # explicit clock
        agent.check_command_safety({"action": "light.turn_on", "expires_at": "1970-01-01T00:00:10Z"}, now=5.0)
        with self.assertRaises(agent.CommandRejected):
            agent.check_command_safety({"action": "light.turn_on", "expires_at": "1970-01-01T00:00:10Z"}, now=11.0)
        # unparsable expires_at is a deadline we cannot trust → rejected; missing / empty is fine
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_on", "expires_at": "whenever"})
        self.assertEqual((cm.exception.code, str(cm.exception)), ("expired", "unparsable expires_at"))
        with self.assertRaises(agent.CommandRejected):
            agent.check_command_safety({"action": "light.turn_on", "payload": {"expires_at": "soon"}})
        agent.check_command_safety({"action": "light.turn_on", "expires_at": ""})
        agent.check_command_safety({"action": "light.turn_on", "expires_at": None})

    def test_cancelled_status(self):
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_on", "status": "cancelled", "expires_at": FUTURE})
        self.assertEqual(cm.exception.code, "cancelled")
        for status in ("sent", "accepted", "", None):
            agent.check_command_safety({"action": "light.turn_on", "status": status})

    def test_allowlist(self):
        for action in ("light.turn_on", "cover.stop_cover", "scene.turn_on", "script.turn_on", "lock.lock",
                       "alarm_control_panel.alarm_arm_home", "alarm_control_panel.alarm_arm_away",
                       "alarm_control_panel.alarm_arm_night", "arvio.model", "arvio.batch"):
            agent.check_command_safety({"action": action})
        for action in ("homeassistant.restart", "shell_command.x", "automation.turn_off", "media_player.play", ""):
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": action})
            self.assertEqual(cm.exception.code, "action_not_allowed")
            self.assertIn("unknown action", cm.exception.error)  # cloud fallback trigger (§14.3)

    def test_payload_expires_at(self):
        # the cloud's TTL copy lives in payload.expires_at; the earliest deadline wins
        agent.check_command_safety({"action": "light.turn_on", "payload": {"expires_at": FUTURE}})
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_on", "expires_at": FUTURE, "payload": {"expires_at": PAST}})
        self.assertEqual(cm.exception.code, "expired")
        with self.assertRaises(agent.CommandRejected):
            agent.check_command_safety({"action": "light.turn_on", "expires_at": PAST, "payload": {"expires_at": FUTURE}})

    def test_dangerous_requires_confirm(self):
        for action in sorted(agent.HOME_DANGEROUS_ACTIONS):
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": action, "entity_id": "lock.x"})
            self.assertEqual(cm.exception.code, "confirm_required")
            with self.assertRaises(agent.CommandRejected):
                agent.check_command_safety({"action": action, "payload": {"confirm_dangerous": "true"}})
            with self.assertRaises(agent.CommandRejected):
                agent.check_command_safety({"action": action, "payload": {"confirm_dangerous": 1}})
            agent.check_command_safety({"action": action, "payload": {"confirm_dangerous": True}})
        # safe direction needs no confirm
        agent.check_command_safety({"action": "lock.lock"})
        agent.check_command_safety({"action": "alarm_control_panel.alarm_arm_away"})

    def test_target_only_for_group_actions(self):
        agent.check_command_safety({"action": "light.turn_off", "target": {"floor_id": "isogeio"}})
        agent.check_command_safety({"action": "cover.close_cover", "payload": {"target": {"area_id": "ypno"}}})
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "lock.lock", "target": {"floor_id": "isogeio"}})
        self.assertEqual(cm.exception.code, "target_not_supported")
        with self.assertRaises(agent.CommandRejected):
            agent.check_command_safety({"action": "climate.turn_off", "target": {"area_id": "ypno"}})

    def test_lan_path_is_an_explicit_allowlist(self):
        allowed = {
            "light.turn_on", "light.turn_off", "switch.turn_on", "switch.turn_off",
            "cover.open_cover", "cover.close_cover", "cover.stop_cover", "cover.set_cover_position",
            "climate.set_temperature", "climate.set_hvac_mode", "climate.set_fan_mode", "climate.set_preset_mode",
            "climate.set_swing_mode", "climate.turn_on", "climate.turn_off", "scene.turn_on", "script.turn_on",
            "arvio.list_entities", "arvio.pairing_status",
            "arvio.list_screens", "arvio.trigger_scenario",
        } | set(agent.MEDIA_PLAYER_ACTIONS) | set(agent.MUSIC_ASSISTANT_ACTIONS)  # 0.1.21: transport/volume only; library/art stay cloud-only
        self.assertEqual(set(agent.LAN_ALLOWED_ACTIONS), allowed)
        self.assertTrue(allowed <= set(agent.AGENT_SERVICE_ALLOWLIST))
        for action in sorted(allowed - {"script.turn_on"}):
            agent.check_command_safety({"action": action, "entity_id": "x.y"}, via="lan")
        # scripts: only script.arvio_* locally
        agent.check_command_safety({"action": "script.turn_on", "entity_id": "script.arvio_kalinyxta"}, via="lan")
        for eid in ("script.other", "script.arvio", "", "light.a"):
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": "script.turn_on", "entity_id": eid}, via="lan")
            self.assertEqual(cm.exception.code, "lan_forbidden")
        # everything else in the relay allowlist is forbidden on the LAN
        for action in sorted(set(agent.AGENT_SERVICE_ALLOWLIST) - allowed):
            with self.assertRaises(agent.CommandRejected) as cm:
                agent.check_command_safety({"action": action, "entity_id": "lock.a",
                                            "payload": {"confirm_dangerous": True}}, via="lan")
            self.assertEqual(cm.exception.code, "lan_forbidden", action)
        for action in ("arvio.model", "arvio.batch", "backup.create", "agent.update", "lock.lock",
                       "light.toggle", "arvio.zigbee_permit", "arvio.upsert_scenario",
                       "arvio.put_screen", "arvio.delete_screen", "arvio.put_venue", "arvio.delete_venue",
                       "arvio.venue_end_session", "arvio.device_update", "arvio.area_update", "arvio.device_remove"):
            self.assertNotIn(action, agent.LAN_ALLOWED_ACTIONS)
        # unknown actions stay "action_not_allowed" (never reach the LAN check)
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "shell_command.rm"}, via="lan")
        self.assertEqual(cm.exception.code, "action_not_allowed")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_off", "target": {"floor_id": "isogeio"}}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        with self.assertRaises(agent.CommandRejected) as cm:
            agent.check_command_safety({"action": "light.turn_off", "payload": {"target": {"area_id": "a"}}}, via="lan")
        self.assertEqual(cm.exception.code, "lan_forbidden")
        # the relay path is unchanged by the LAN allowlist
        agent.check_command_safety({"action": "arvio.model"})
        agent.check_command_safety({"action": "script.turn_on", "entity_id": "script.other"})


class LRUTest(unittest.TestCase):
    def test_eviction_and_recency(self):
        lru = agent.LRU(3)
        lru.put("a", 1); lru.put("b", 2); lru.put("c", 3)
        self.assertEqual(lru.get("a"), 1)  # touch a → b is oldest
        lru.put("d", 4)
        self.assertIsNone(lru.get("b"))
        self.assertEqual(lru.get("a"), 1)
        self.assertEqual(len(lru), 3)
        self.assertIn("d", lru)
        self.assertEqual(agent.COMMAND_ACKS.capacity, 500)
        self.assertEqual(agent.CONTEXT_TO_COMMAND.capacity, 500)


class HandleCommandTest(unittest.TestCase):
    def setUp(self):
        agent.COMMAND_ACKS = agent.LRU(500)
        agent.CONTEXT_TO_COMMAND = agent.LRU(500)
        self.calls = []

        def fake_execute(action, entity_id, payload=None, target=None):
            self.calls.append((action, entity_id, payload, target))
            return {
                "ok": True, "entity_id": entity_id, "state": "on",
                "state_snapshot": {"entity_id": entity_id, "state": "on", "attrs": {}},
                "affected_entity_ids": [entity_id] if entity_id else [],
                "ha_context_id": f"ctx_{len(self.calls)}",
            }

        self.patcher = mock.patch.object(agent, "execute_action", side_effect=fake_execute)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_ack_shape(self):
        ack = agent.handle_command({"command_id": "c1", "action": "light.turn_on", "entity_id": "light.a",
                                    "expires_at": FUTURE, "payload": {"brightness_pct": 40}})
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["command_id"], "c1")
        self.assertEqual(ack["affected_entity_ids"], ["light.a"])
        self.assertEqual(ack["ha_context_id"], "ctx_1")
        self.assertEqual(ack["state_snapshot"]["state"], "on")
        self.assertTrue(ack["hub_ts"].endswith("Z"))
        self.assertIsNone(ack["error"])
        self.assertEqual(ack["state"], "on")  # legacy flat field kept
        self.assertEqual(agent.CONTEXT_TO_COMMAND.get("ctx_1"), "c1")
        self.assertEqual(self.calls[0][2], {"brightness_pct": 40})
        self.assertNotIn("error_code", ack)

    def test_unknown_action_ack(self):
        ack = agent.handle_command({"command_id": "u1", "action": "media_player.play", "entity_id": "media_player.tv"})
        self.assertFalse(ack["ok"])
        self.assertIn("unknown action", ack["error"])
        self.assertEqual(ack["error_code"], "action_not_allowed")
        self.assertEqual(self.calls, [])

    def test_client_command_id_is_echoed_and_deduped(self):
        cmd = {"command_id": "relay_1", "idempotency_key": "idem_1", "action": "light.turn_on", "entity_id": "light.a",
               "payload": {"client_command_id": "app-uuid-1", "brightness_pct": 10}}
        ack = agent.handle_command(cmd)
        self.assertEqual(ack["command_id"], "app-uuid-1")  # the Home app's own id
        self.assertEqual(agent.CONTEXT_TO_COMMAND.get("ctx_1"), "app-uuid-1")  # push events carry it too
        # a retry delivered under a new relay id but the same client id is a duplicate
        again = agent.handle_command({"command_id": "relay_2", "action": "light.turn_on", "entity_id": "light.a",
                                      "payload": {"client_command_id": "app-uuid-1"}})
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.calls), 1)
        # a rejection echoes the client id as well
        bad = agent.handle_command({"command_id": "relay_3", "action": "lock.unlock", "entity_id": "lock.a",
                                    "payload": {"client_command_id": "app-uuid-2"}})
        self.assertEqual((bad["command_id"], bad["error"]), ("app-uuid-2", "confirm_required"))

    def test_duplicate_command_id_returns_cached_ack(self):
        cmd = {"command_id": "dup", "idempotency_key": "dup", "action": "light.turn_on", "entity_id": "light.a"}
        first = agent.handle_command(cmd)
        second = agent.handle_command(dict(cmd))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["ha_context_id"], first["ha_context_id"])
        self.assertNotIn("duplicate", first)

    def test_duplicate_idempotency_key_with_new_command_id(self):
        agent.handle_command({"command_id": "k1", "idempotency_key": "same-key", "action": "switch.turn_on", "entity_id": "switch.a"})
        again = agent.handle_command({"command_id": "k2", "idempotency_key": "same-key", "action": "switch.turn_on", "entity_id": "switch.a"})
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["command_id"], "k1")

    def test_expired_is_rejected_and_not_executed_nor_cached(self):
        ack = agent.handle_command({"command_id": "e1", "action": "light.turn_on", "entity_id": "light.a", "expires_at": PAST})
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "expired")
        self.assertEqual(self.calls, [])
        self.assertNotIn("e1", agent.COMMAND_ACKS)
        ack2 = agent.handle_command({"command_id": "e2", "action": "light.turn_on", "entity_id": "light.a", "status": "expired"})
        self.assertEqual(ack2["error"], "expired")

    def test_dangerous_without_confirm_rejected(self):
        ack = agent.handle_command({"command_id": "d1", "action": "lock.unlock", "entity_id": "lock.a", "expires_at": FUTURE})
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "confirm_required")
        self.assertEqual(self.calls, [])
        ok = agent.handle_command({"command_id": "d2", "action": "lock.unlock", "entity_id": "lock.a", "expires_at": FUTURE,
                                   "payload": {"confirm_dangerous": True, "code": "1234"}})
        self.assertTrue(ok["ok"])
        self.assertEqual(self.calls[0][2]["code"], "1234")

    def test_ha_failure_becomes_failed_ack_and_is_cached(self):
        self.patcher.stop()
        with mock.patch.object(agent, "execute_action", side_effect=RuntimeError("HA POST failed")):
            ack = agent.handle_command({"command_id": "f1", "action": "light.turn_on", "entity_id": "light.a"})
            again = agent.handle_command({"command_id": "f1", "action": "light.turn_on", "entity_id": "light.a"})
        self.patcher.start()
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "HA POST failed")
        self.assertTrue(again["duplicate"])

    def test_lan_path(self):
        ack = agent.handle_command({"command_id": "l1", "action": "lock.lock", "entity_id": "lock.a"}, via="lan")
        self.assertEqual(ack["error"], "lan_forbidden")
        ack = agent.handle_command({"command_id": "l2", "action": "light.turn_on", "entity_id": "light.a"}, via="lan")
        self.assertTrue(ack["ok"])

    def test_target_passthrough(self):
        agent.handle_command({"command_id": "t1", "action": "light.turn_off", "target": {"floor_id": "isogeio"}})
        self.assertEqual(self.calls[0][3], {"floor_id": "isogeio"})
        agent.handle_command({"command_id": "t2", "action": "light.turn_off", "payload": {"target": {"area_id": "saloni"}}})
        self.assertEqual(self.calls[1][3], {"area_id": "saloni"})

    def test_batch(self):
        ack = agent.handle_command({
            "command_id": "b1", "action": "arvio.batch", "expires_at": FUTURE,
            "payload": {"group_id": "g1", "calls": [
                {"command_id": "b1a", "action": "light.turn_off", "target": {"floor_id": "isogeio"}},
                {"command_id": "b1b", "action": "switch.turn_off", "entity_id": "switch.a"},
                {"command_id": "b1c", "action": "lock.unlock", "entity_id": "lock.a"},
                {"action": "arvio.batch", "payload": {"calls": []}},
                {"command_id": "b1b", "action": "switch.turn_off", "entity_id": "switch.a"},
            ]},
        })
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "partial_failure")
        self.assertEqual(ack["group_id"], "g1")
        acks = ack["results"]  # HubCommandAck[] ({results:[…]} is accepted by the cloud)
        self.assertEqual([a["command_id"] for a in acks], ["b1a", "b1b", "b1c", "b1:3", "b1b"])
        self.assertTrue(acks[0]["ok"]); self.assertTrue(acks[1]["ok"])
        # security actions never ride in a batch (§14.3) — even with confirm_dangerous
        self.assertEqual((acks[2]["error"], acks[2]["error_code"]), ("security_in_batch", "security_in_batch"))
        self.assertIn("unknown action", acks[3]["error"])
        self.assertEqual(acks[3]["error_code"], "action_not_allowed")
        self.assertTrue(acks[4]["duplicate"])  # per-call dedupe inside the batch
        self.assertEqual(ack["affected_entity_ids"], ["switch.a"])
        self.assertEqual(ack["error_code"], "partial_failure")
        self.assertEqual(len(self.calls), 2)
        for a in acks:
            for key in ("command_id", "ok", "state_snapshot", "affected_entity_ids", "ha_context_id", "hub_ts", "error"):
                self.assertIn(key, a)
            if not a["ok"]:
                self.assertTrue(a.get("error_code"), a)
        # per-call fields as the cloud sends them: confirm_dangerous + expires_at on the call
        per_call = agent.handle_command({
            "command_id": "b5", "action": "arvio.batch", "expires_at": FUTURE,
            "payload": {"group_id": "g5", "calls": [
                {"command_id": "b5a", "action": "lock.unlock", "entity_id": "lock.a", "confirm_dangerous": True,
                 "payload": {"code": "1234"}, "expires_at": FUTURE},
                {"command_id": "b5b", "action": "light.turn_off", "entity_id": "light.a", "expires_at": PAST},
                {"command_id": "b5c", "action": "light.turn_off", "entity_id": "light.a", "expires_at": FUTURE},
            ]},
        })
        r = per_call["results"]
        for action in sorted(agent.HOME_SECURITY_ACTIONS):
            self.assertIn(action, agent.AGENT_SERVICE_ALLOWLIST)
        self.assertEqual(r[0]["error_code"], "security_in_batch")  # confirm_dangerous does not help
        self.assertNotIn("b5a", agent.COMMAND_ACKS)  # never executed, never cached
        self.assertEqual((r[1]["error"], r[1]["error_code"]), ("expired", "expired"))
        self.assertTrue(r[2]["ok"])
        self.assertEqual(self.calls[-1][0], "light.turn_off")
        # expiry inherited by every call
        exp = agent.handle_command({"command_id": "b2", "action": "arvio.batch", "expires_at": PAST,
                                    "payload": {"calls": [{"action": "light.turn_off", "entity_id": "light.a"}]}})
        self.assertEqual(exp["error"], "expired")
        self.assertEqual(len(self.calls), 3)  # b1a, b1b, b5c — nothing from the expired batch
        bad = agent.handle_command({"command_id": "b3", "action": "arvio.batch", "payload": {"calls": []}})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error_code"], "invalid_command")
        too_many = agent.handle_command({"command_id": "b4", "action": "arvio.batch",
                                         "payload": {"calls": [{"action": "light.turn_off", "entity_id": "light.a"}] * 61}})
        self.assertIn("batch too large", too_many["error"])
        self.assertEqual(too_many["error_code"], "invalid_command")

    def test_every_rejection_carries_error_code(self):
        cases = {
            "r1": ({"action": "media_player.play", "entity_id": "x.y"}, "action_not_allowed"),
            "r2": ({"action": "light.turn_on", "entity_id": "light.a", "expires_at": PAST}, "expired"),
            "r3": ({"action": "light.turn_on", "entity_id": "light.a", "expires_at": "nope"}, "expired"),
            "r4": ({"action": "light.turn_on", "entity_id": "light.a", "status": "cancelled"}, "cancelled"),
            "r5": ({"action": "lock.unlock", "entity_id": "lock.a"}, "confirm_required"),
            "r6": ({"action": "lock.lock", "target": {"area_id": "a"}}, "target_not_supported"),
        }
        for cid, (cmd, code) in cases.items():
            ack = agent.handle_command({"command_id": cid, **cmd})
            self.assertFalse(ack["ok"], cid)
            self.assertEqual(ack["error_code"], code, cid)
        ack = agent.handle_command({"command_id": "r7", "action": "lock.lock", "entity_id": "lock.a"}, via="lan")
        self.assertEqual(ack["error_code"], "lan_forbidden")
        self.assertEqual(self.calls, [])
        # executor errors: ValueError → invalid_command, anything else → execution_failed
        self.patcher.stop()
        try:
            with mock.patch.object(agent, "execute_action", side_effect=ValueError("entity_id required")):
                ack = agent.handle_command({"command_id": "r8", "action": "light.turn_on"})
            self.assertEqual((ack["error"], ack["error_code"]), ("entity_id required", "invalid_command"))
            with mock.patch.object(agent, "execute_action", side_effect=RuntimeError("HA down")):
                ack = agent.handle_command({"command_id": "r9", "action": "light.turn_on", "entity_id": "light.a"})
            self.assertEqual((ack["error"], ack["error_code"]), ("HA down", "execution_failed"))
            # an executor result with ok:false keeps its stable error string as the code
            with mock.patch.object(agent, "execute_action", return_value={"ok": False, "error": "state_not_confirmed",
                                                                          "affected_entity_ids": ["light.a"]}):
                ack = agent.handle_command({"command_id": "r10", "action": "light.turn_on", "entity_id": "light.a"})
            self.assertEqual(ack["error_code"], "state_not_confirmed")
        finally:
            self.patcher.start()
        # make_ack itself never leaves a failed ack without a code; ok acks carry none
        self.assertEqual(agent.make_ack("m1", ok=False, error="weird")["error_code"], "execution_failed")
        self.assertEqual(agent.make_ack("m2", {"ok": False, "error": "partial_failure"})["error_code"], "partial_failure")
        self.assertNotIn("error_code", agent.make_ack("m3", {"ok": True}))


class InFlightTest(unittest.TestCase):
    def test_concurrent_same_id_executes_once(self):
        import threading

        agent.COMMAND_ACKS = agent.LRU(500)
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_execute(action, entity_id, payload=None, target=None):
            calls.append(action)
            started.set()
            release.wait(5)
            return {"ok": True, "entity_id": entity_id, "ha_context_id": "ctx_slow"}

        results = []
        cmd = {"command_id": "same", "action": "light.turn_on", "entity_id": "light.a"}
        with mock.patch.object(agent, "execute_action", side_effect=slow_execute):
            t1 = threading.Thread(target=lambda: results.append(agent.handle_command(dict(cmd))))
            t1.start()
            self.assertTrue(started.wait(2))
            t2 = threading.Thread(target=lambda: results.append(agent.handle_command(dict(cmd))))
            t2.start()
            time.sleep(0.1)
            release.set()
            t1.join(3); t2.join(3)
        self.assertEqual(calls, ["light.turn_on"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sum(1 for r in results if r.get("duplicate")), 1)
        self.assertEqual(agent.COMMAND_INFLIGHT, {})

    def test_in_flight_timeout_ack_has_error_code(self):
        agent.COMMAND_ACKS = agent.LRU(500)
        with mock.patch.object(agent, "HOME_COMMAND_INFLIGHT_WAIT_S", 0.05):
            done = agent.COMMAND_INFLIGHT["stuck"] = threading.Event()
            try:
                ack = agent.handle_command({"command_id": "stuck", "action": "light.turn_on", "entity_id": "light.a"})
            finally:
                agent.COMMAND_INFLIGHT.pop("stuck", None)
                done.set()
        self.assertEqual((ack["ok"], ack["error"], ack["error_code"]), (False, "duplicate_in_flight", "duplicate_in_flight"))


class TargetResolutionTest(unittest.TestCase):
    def test_floor_area_entities(self):
        r = regs()
        args = (r["entity_regs"], r["devices"], r["areas"])
        self.assertEqual(agent.resolve_target_entities({"floor_id": "isogeio"}, "light", *args),
                         ["light.kouzina", "light.saloni"])  # hidden + config excluded, device-area counted
        self.assertEqual(agent.resolve_target_entities({"floor_id": "isogeio"}, "switch", *args), [])  # config excluded
        self.assertEqual(agent.resolve_target_entities({"area_id": "ypno"}, "cover", *args), ["cover.ypno"])
        self.assertEqual(agent.resolve_target_entities({"area_id": "kouzina"}, "light", *args), ["light.kouzina"])
        self.assertEqual(agent.resolve_target_entities({"entity_ids": ["light.a", "switch.b", "light.c"]}, "light", *args),
                         ["light.a", "light.c"])
        self.assertEqual(agent.resolve_target_entities({"entity_id": "light.z"}, "light", *args), ["light.z"])
        self.assertEqual(agent.resolve_target_entities({"floor_id": "orofos1", "entity_ids": ["light.extra"]}, "light", *args),
                         ["light.extra"])
        self.assertEqual(agent.resolve_target_entities({"floor_id": "nope"}, "light", *args), [])
        self.assertEqual(agent.resolve_target_entities(None, "light", *args), [])

    def test_native_target(self):
        self.assertEqual(agent.native_target_data({"floor_id": "f"}, "2024.4.0"), {"floor_id": "f"})
        self.assertEqual(agent.native_target_data({"area_id": "a", "floor_id": "f"}, "2025.1"), {"area_id": "a", "floor_id": "f"})
        self.assertIsNone(agent.native_target_data({"floor_id": "f"}, "2024.3.3"))
        self.assertIsNone(agent.native_target_data({"entity_ids": ["light.a"]}, "2025.1"))


class ServiceDataTest(unittest.TestCase):
    def test_kelvin_vs_mireds(self):
        svc, data = agent.build_service_data("light.turn_on", "light.a", {"color_temp_kelvin": 2700}, "2025.8")
        self.assertEqual((svc, data), ("turn_on", {"entity_id": "light.a", "color_temp_kelvin": 2700}))
        svc, data = agent.build_service_data("light.turn_on", "light.a", {"color_temp_kelvin": 2700}, "2022.6.1")
        self.assertEqual(data, {"entity_id": "light.a", "color_temp": 370})
        _, data = agent.build_service_data("light.turn_on", "light.a", {"brightness_pct": 45, "rgb_color": [1, 2, 3]}, None)
        self.assertEqual(data, {"entity_id": "light.a", "brightness_pct": 45, "rgb_color": [1, 2, 3]})

    def test_codes_and_positions(self):
        _, data = agent.build_service_data("lock.unlock", "lock.a", {"code": "1234", "confirm_dangerous": True}, None)
        self.assertEqual(data, {"entity_id": "lock.a", "code": "1234"})
        _, data = agent.build_service_data("lock.lock", "lock.a", {}, None)
        self.assertEqual(data, {"entity_id": "lock.a"})
        _, data = agent.build_service_data("alarm_control_panel.alarm_arm_home", "alarm_control_panel.a", {"code": ""}, None)
        self.assertEqual(data, {"entity_id": "alarm_control_panel.a"})
        _, data = agent.build_service_data("alarm_control_panel.alarm_disarm", "alarm_control_panel.a", {"code": 4321}, None)
        self.assertEqual(data["code"], "4321")
        svc, data = agent.build_service_data("cover.set_cover_position", "cover.a", {"position": 40}, None)
        self.assertEqual((svc, data["position"]), ("set_cover_position", 40))
        svc, data = agent.build_service_data("cover.stop_cover", "cover.a", {}, None)
        self.assertEqual((svc, data), ("stop_cover", {"entity_id": "cover.a"}))
        svc, data = agent.build_service_data("climate.set_hvac_mode", "climate.a", {"hvac_mode": "cool"}, None)
        self.assertEqual(data["hvac_mode"], "cool")
        self.assertEqual(agent._expected_state_for("lock", "lock"), "locked")
        self.assertEqual(agent._expected_state_for("alarm_control_panel", "alarm_arm_night"), "armed_night")
        self.assertTrue(agent._state_matches("alarm_control_panel", "armed_away", "arming"))
        self.assertIsNone(agent._expected_state_for("scene", "turn_on"))


class SharedHaWsTest(unittest.TestCase):
    """ha_ws_shared_command: lazy connect, reuse, idle ping, REST-safe failure semantics."""

    def setUp(self):
        agent._HA_CMD_CHANNEL.ws = None
        agent._HA_CMD_CHANNEL.used_at = 0.0
        agent._HA_QUERY_CHANNEL.ws = None
        agent._HA_QUERY_CHANNEL.used_at = 0.0
        self.sockets = []

    def tearDown(self):
        agent._HA_CMD_CHANNEL.ws = None
        agent._HA_QUERY_CHANNEL.ws = None

    def make_fake(self, fail_after_ping=False):
        sockets = self.sockets

        class FakeWs:
            def __init__(self, timeout=20.0):
                self.commands = []
                self.closed = False
                self.dead = False
                sockets.append(self)

            def command(self, msg_type, extra=None):
                if self.dead:
                    raise OSError("socket closed")
                self.commands.append((msg_type, extra))
                return {"context": {"id": f"ctx_{len(self.commands)}"}}

            def close(self):
                self.closed = True

        return FakeWs

    def test_connect_failure_is_unavailable(self):
        def boom(timeout=20.0):
            raise RuntimeError("missing SUPERVISOR_TOKEN")

        with mock.patch.object(agent, "HaWs", boom):
            with self.assertRaises(agent.HaWsUnavailable):
                agent.ha_ws_shared_command("call_service", {"domain": "light"})
        self.assertIsNone(agent._HA_CMD_CHANNEL.ws)

    def test_reuse_and_idle_ping(self):
        with mock.patch.object(agent, "HaWs", self.make_fake()):
            r1 = agent.ha_ws_shared_command("call_service", {"domain": "light", "service": "turn_on"})
            r2 = agent.ha_ws_shared_command("call_service", {"domain": "light", "service": "turn_off"})
            self.assertEqual((r1["context"]["id"], r2["context"]["id"]), ("ctx_1", "ctx_2"))
            self.assertEqual(len(self.sockets), 1)  # one connection, no auth per command
            self.assertEqual([c[0] for c in self.sockets[0].commands], ["call_service", "call_service"])
            # idle for a while → a ping goes first on the same socket
            agent._HA_CMD_CHANNEL.used_at = time.time() - agent.HA_CMD_WS_IDLE_PING_S - 1
            agent.ha_ws_shared_command("call_service", {"domain": "x", "service": "y"})
            self.assertEqual([c[0] for c in self.sockets[0].commands][-2:], ["ping", "call_service"])
            self.assertEqual(len(self.sockets), 1)
            # idle and dead → replaced before the real command (nothing sent twice)
            self.sockets[0].dead = True
            agent._HA_CMD_CHANNEL.used_at = time.time() - agent.HA_CMD_WS_IDLE_PING_S - 1
            out = agent.ha_ws_shared_command("call_service", {"domain": "x", "service": "z"})
            self.assertEqual(len(self.sockets), 2)
            self.assertTrue(self.sockets[0].closed)
            self.assertEqual(self.sockets[1].commands, [("call_service", {"domain": "x", "service": "z"})])
            self.assertEqual(out["context"]["id"], "ctx_1")

    def test_failure_after_send_propagates_and_drops_socket(self):
        with mock.patch.object(agent, "HaWs", self.make_fake()):
            agent.ha_ws_shared_command("ping")
            self.sockets[0].dead = True  # dies while in use (not idle) → error surfaces, no REST retry
            with self.assertRaises(OSError):
                agent.ha_ws_shared_command("call_service", {"domain": "x", "service": "z"})
            self.assertIsNone(agent._HA_CMD_CHANNEL.ws)
            self.assertTrue(self.sockets[0].closed)
            agent.ha_ws_shared_command("ping")  # next call reopens
            self.assertEqual(len(self.sockets), 2)

    def test_query_channel_is_separate_and_never_blocks_call_service(self):
        """A slow browse_media (query channel) must not delay a call_service (command channel)."""
        agent.MA_INFO["entry_id"] = None
        sockets = self.sockets
        browse_started = threading.Event()
        release_browse = threading.Event()

        class FakeWs:
            def __init__(self, timeout=20.0):
                self.commands = []
                self.closed = False
                sockets.append(self)

            def command(self, msg_type, extra=None):
                self.commands.append((msg_type, extra))
                if msg_type == "media_player/browse_media":
                    browse_started.set()
                    if not release_browse.wait(5):
                        raise TimeoutError("browse never released")
                    return {"title": "Root", "children": []}
                return {"context": {"id": f"ctx_{len(self.commands)}"}}

            def close(self):
                self.closed = True

        with mock.patch.object(agent, "HaWs", FakeWs):
            browse_out = {}
            t = threading.Thread(
                target=lambda: browse_out.update(agent.media_browse({"entity_id": "media_player.saloni"})), daemon=True
            )
            t.start()
            self.assertTrue(browse_started.wait(5))
            self.assertTrue(agent._HA_QUERY_CHANNEL.lock.locked())  # browse holds only the query lock
            self.assertFalse(agent._HA_CMD_CHANNEL.lock.locked())
            # call_service goes out at once on its own socket while the browse is still in flight
            t0 = time.monotonic()
            self.assertEqual(agent.ha_call_service("light", "turn_on", {"entity_id": "light.a"}), "ctx_1")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(browse_out, {})  # browse still in flight
            release_browse.set()
            t.join(5)
            self.assertTrue(browse_out.get("ok"))
        # two independent sockets: one per channel, and each channel keeps its own
        self.assertEqual(len(sockets), 2)
        self.assertEqual([c[0] for c in sockets[0].commands], ["media_player/browse_media"])
        self.assertEqual([c[0] for c in sockets[1].commands], ["call_service"])
        self.assertIs(agent._HA_QUERY_CHANNEL.ws, sockets[0])
        self.assertIs(agent._HA_CMD_CHANNEL.ws, sockets[1])
        # a dead query socket is dropped without touching the command one
        with mock.patch.object(agent, "HaWs", FakeWs):
            agent._HA_QUERY_CHANNEL.ws.command = mock.Mock(side_effect=OSError("gone"))
            with self.assertRaises(OSError):
                agent.ha_ws_query_command("media_player/search_media", {"entity_id": "media_player.saloni", "search_query": "x"})
            self.assertIsNone(agent._HA_QUERY_CHANNEL.ws)
            self.assertIs(agent._HA_CMD_CHANNEL.ws, sockets[1])
            agent._HA_QUERY_CHANNEL.drop(); agent._HA_CMD_CHANNEL.drop()
            self.assertTrue(sockets[1].closed)
            self.assertIsNone(agent._HA_CMD_CHANNEL.ws)


class ExecuteWithFakeHaTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHa({
            "light.saloni": "on", "light.kouzina": "on", "light.orphan": "on", "cover.ypno": "open",
            "lock.eisodos": "unlocked", "scene.vradi": "unknown",
        })
        self.patches = [
            mock.patch.object(agent, "ha", side_effect=self.fake),
            # no HA websocket in the tests → REST fallback for call_service
            mock.patch.object(agent, "ha_ws_shared_command", side_effect=agent.HaWsUnavailable("no ws")),
            mock.patch.object(agent, "registries_for_commands", return_value=regs()),
            mock.patch.object(agent, "HA_INFO", {"version": "2025.8.1", "time_zone": "Europe/Athens"}),
            mock.patch.object(agent, "WAIT_FOR_STATE_S", 0.3),
            mock.patch.object(agent, "STATE_FEED_LIVE", False),
        ]
        for p in self.patches:
            p.start()
        agent.STATE_FEED_RECENT = agent.LRU(1000)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        agent.STATE_FEED_RECENT = agent.LRU(1000)

    def test_single_entity_ack(self):
        out = agent.execute_action("light.turn_off", "light.saloni", {})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertEqual(out["state"], "off")
        self.assertEqual(out["affected_entity_ids"], ["light.saloni"])
        self.assertEqual(out["ha_context_id"], "ctx_abc")
        self.assertEqual(out["state_snapshot"]["entity_id"], "light.saloni")
        self.assertEqual(out["state_snapshot"]["state"], "off")
        self.assertIn("attrs", out["state_snapshot"])
        self.assertEqual(self.fake.calls[0], ("/services/light/turn_off", {"entity_id": "light.saloni"}))

    def test_ws_call_service_context(self):
        # documented WS call_service → result.context.id, no REST call
        ws_calls = []

        def fake_ws(msg_type, extra=None):
            ws_calls.append((msg_type, extra))
            self.fake("/services/light/turn_off", "POST", {"entity_id": "light.saloni"})  # HA flips the state
            self.fake.calls.clear()
            return {"context": {"id": "ctx_ws_1", "parent_id": None, "user_id": "agent"}}

        with mock.patch.object(agent, "ha_ws_shared_command", side_effect=fake_ws):
            out = agent.execute_action("light.turn_off", "light.saloni", {})
        self.assertEqual(ws_calls, [("call_service", {"domain": "light", "service": "turn_off",
                                                      "service_data": {"entity_id": "light.saloni"}})])
        self.assertEqual(out["ha_context_id"], "ctx_ws_1")
        self.assertEqual(out["state"], "off")
        self.assertEqual(self.fake.calls, [])  # no REST service call happened
        # HA rejecting the service surfaces as an error (never re-sent over REST)
        with mock.patch.object(agent, "ha_ws_shared_command", side_effect=RuntimeError("Invalid code")):
            with self.assertRaises(RuntimeError):
                agent.execute_action("lock.lock", "lock.eisodos", {"code": "0"})
        self.assertEqual(self.fake.calls, [])

    def test_no_fallback_state_when_ha_does_not_confirm(self):
        # §14.3: the ack carries the real HA state and ok:false when it did not change
        stubborn = FakeHa({"light.saloni": "on"})
        original = stubborn.__call__

        def no_flip(path, method="GET", body=None, timeout=10):
            if path.startswith("/services/"):
                stubborn.calls.append((path, body))
                return []  # HA accepted the call, nothing changed
            return original(path, method, body, timeout)

        with mock.patch.object(agent, "ha", side_effect=no_flip):
            out = agent.execute_action("light.turn_off", "light.saloni", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "state_not_confirmed")
        self.assertEqual(out["state"], "on")  # what HA reports, not the expected "off"
        self.assertEqual(out["state_snapshot"]["state"], "on")
        self.assertIsNone(out["ha_context_id"])
        self.assertEqual(len(stubborn.calls), 1)
        # through handle_command the ack is ok:false with the same error
        agent.COMMAND_ACKS = agent.LRU(500)
        with mock.patch.object(agent, "ha", side_effect=no_flip):
            ack = agent.handle_command({"command_id": "nc1", "action": "light.turn_off", "entity_id": "light.saloni"})
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "state_not_confirmed")
        self.assertEqual(ack["state_snapshot"]["state"], "on")
        # stateless services never wait for a state
        out = agent.execute_action("scene.turn_on", "scene.vradi", {})
        self.assertTrue(out["ok"])

    def test_floor_target_native(self):
        out = agent.execute_action("light.turn_off", "", {}, target={"floor_id": "isogeio"})
        path, body = self.fake.calls[0]
        self.assertEqual(path, "/services/light/turn_off")
        self.assertEqual(body, {"floor_id": "isogeio"})  # HA ≥ 2024.4 native target
        self.assertEqual(out["affected_entity_ids"], ["light.kouzina", "light.saloni"])
        self.assertEqual(out["state_snapshot"]["entities"]["light.saloni"]["state"], "off")

    def test_floor_target_fallback_old_ha(self):
        agent.HA_INFO["version"] = "2023.11.3"
        out = agent.execute_action("light.turn_off", "", {}, target={"floor_id": "isogeio"})
        _, body = self.fake.calls[0]
        self.assertEqual(body, {"entity_id": ["light.kouzina", "light.saloni"]})
        self.assertEqual(out["affected_entity_ids"], ["light.kouzina", "light.saloni"])

    def test_explicit_ids_ride_along_native(self):
        agent.execute_action("light.turn_off", "", {}, target={"area_id": "kouzina", "entity_ids": ["light.orphan"]})
        _, body = self.fake.calls[0]
        self.assertEqual(body, {"area_id": "kouzina", "entity_id": ["light.orphan"]})

    def test_group_wait_reads_per_entity_not_full_dump(self):
        # ≤ 8 affected entities → GET /states/{id} each; the /states dump is never polled
        reads = []
        original = self.fake.__call__

        def spy(path, method="GET", body=None, timeout=10):
            if path.startswith("/states"):
                reads.append(path)
            return original(path, method, body, timeout)

        with mock.patch.object(agent, "ha", side_effect=spy):
            out = agent.execute_action("light.turn_off", "", {}, target={"floor_id": "isogeio"})
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(set(reads)), ["/states/light.kouzina", "/states/light.saloni"])
        self.assertNotIn("/states", reads)
        # > 8 entities → one dump per poll instead of N requests
        many = FakeHa({f"light.l{i}": "on" for i in range(9)})
        reads.clear()

        def spy_many(path, method="GET", body=None, timeout=10):
            if path.startswith("/states"):
                reads.append(path)
            return many(path, method, body, timeout)

        ids = [f"light.l{i}" for i in range(9)]
        with mock.patch.object(agent, "ha", side_effect=spy_many):
            out = agent.execute_action("light.turn_off", "", {}, target={"entity_ids": ids})
        self.assertTrue(out["ok"])
        self.assertEqual(reads, ["/states"])
        self.assertEqual(agent.WAIT_FOR_STATE_REST_MAX_ENTITIES, 8)

    def test_group_wait_is_fed_by_ha_events(self):
        # HA accepted the call but reports the old state over REST; the state_changed event
        # (handle_ha_event) confirms the change without any further REST polling.
        stubborn = FakeHa({"light.saloni": "on", "light.kouzina": "on"})
        reads = []

        def no_flip(path, method="GET", body=None, timeout=10):
            if path.startswith("/services/"):
                stubborn.calls.append((path, body))
                return []
            if path.startswith("/states"):
                reads.append(path)
            return stubborn(path, method, body, timeout)

        import threading

        def events():
            time.sleep(0.05)
            for eid in ("light.saloni", "light.kouzina"):
                new = {"entity_id": eid, "state": "off", "attributes": {"friendly_name": eid},
                       "last_changed": "2026-09-11T10:00:00+00:00", "context": {"id": "ctx_ev"}}
                agent.handle_ha_event({"event_type": "state_changed", "data": {"entity_id": eid, "new_state": new}})

        with mock.patch.object(agent, "ha", side_effect=no_flip), \
             mock.patch.object(agent, "STATE_FEED_LIVE", True), \
             mock.patch.object(agent, "WAIT_FOR_STATE_S", 2.0), \
             mock.patch.object(agent, "PUSH", agent.Coalescer(lambda m: None, 1.0)):
            t = threading.Thread(target=events, daemon=True)
            started = time.time()
            t.start()
            out = agent.execute_action("light.turn_off", "", {}, target={"floor_id": "isogeio"})
            t.join(2)
        self.assertTrue(out["ok"])
        self.assertLess(time.time() - started, 1.5)  # woke up on the events, not the deadline
        self.assertEqual(out["state_snapshot"]["entities"]["light.saloni"]["state"], "off")
        self.assertEqual(out["state_snapshot"]["entities"]["light.kouzina"]["state"], "off")
        self.assertEqual(sorted(reads), ["/states/light.kouzina", "/states/light.saloni"])  # one initial read each
        # an event that predates the service call never counts as confirmation
        agent.STATE_FEED_RECENT.put("light.saloni", (time.time() - 10, {"entity_id": "light.saloni", "state": "off", "attributes": {}}))
        with mock.patch.object(agent, "ha", side_effect=no_flip), \
             mock.patch.object(agent, "STATE_FEED_LIVE", True), \
             mock.patch.object(agent, "WAIT_FOR_STATE_S", 0.2):
            out = agent.execute_action("light.turn_off", "light.saloni", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "state_not_confirmed")

    def test_target_matching_nothing(self):
        with self.assertRaises(ValueError):
            agent.execute_action("cover.close_cover", "", {}, target={"floor_id": "nope"})
        self.assertEqual(self.fake.calls, [])

    def test_entity_id_required(self):
        with self.assertRaises(ValueError):
            agent.execute_action("light.turn_on", "", {})

    def test_scene_and_stop_cover(self):
        out = agent.execute_action("scene.turn_on", "scene.vradi", {})
        self.assertTrue(out["ok"])
        self.assertEqual(self.fake.calls[0], ("/services/scene/turn_on", {"entity_id": "scene.vradi"}))
        agent.execute_action("cover.stop_cover", "cover.ypno", {})
        self.assertEqual(self.fake.calls[1][0], "/services/cover/stop_cover")

    def test_lock_with_code(self):
        out = agent.execute_action("lock.lock", "lock.eisodos", {"code": "0000"})
        self.assertEqual(self.fake.calls[0][1], {"entity_id": "lock.eisodos", "code": "0000"})
        self.assertEqual(out["state"], "locked")

    def test_arvio_model_end_to_end(self):
        from helpers import AREAS, DEVICES, ENTITY_REGISTRY_DISPLAY, FLOORS, STATES

        fake_states = FakeHa({})
        fake_states.states = {s["entity_id"]: s for s in STATES}
        agent.REGISTRY_CACHE = agent.RegistrySnapshot()  # cold agent: nothing loaded yet
        with mock.patch.object(agent, "ha", side_effect=fake_states), \
             mock.patch.object(agent, "fetch_registries", return_value={
                 "floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": ENTITY_REGISTRY_DISPLAY}), \
             mock.patch.object(agent, "fetch_arvio_automation_configs", return_value={}), \
             mock.patch.object(agent, "push_model_changed") as pushed:
            agent.save_hub({"hub_id": "hub_test"})
            m = agent.execute_action("arvio.model", "", {"limit": 4, "offset": 1})
            self.assertEqual(m["limit"], 4)
            self.assertEqual(m["offset"], 1)
            self.assertEqual(len(m["entities"]), 4)
            self.assertGreater(m["total"], 4)
            self.assertEqual(m["snapshot_version"], 1)
            self.assertEqual(m["ha_version"], "2025.8.1")
            self.assertEqual(len(m["floors"]), 2)
            # first registry load → {type:"model"} once (state events were dropped until now), no bump
            pushed.assert_called_once_with(1)
            pushed.reset_mock()
            m1b = agent.execute_action("arvio.model", "", {})
            self.assertEqual(m1b["snapshot_version"], 1)
            pushed.assert_not_called()  # warm cache, unchanged registries → silent
            # a registry change on the next read bumps the version and pushes {type:"model"}
            moved = {**ENTITY_REGISTRY_DISPLAY, "entities": ENTITY_REGISTRY_DISPLAY["entities"][1:]}
            with mock.patch.object(agent, "fetch_registries", return_value={
                    "floors": FLOORS, "areas": AREAS, "devices": DEVICES, "entity_registry": moved}):
                m2 = agent.execute_action("arvio.model", "", {})
            self.assertEqual(m2["snapshot_version"], 2)
            pushed.assert_called_once_with(2)
            self.assertEqual(m2["limit"], agent.MODEL_LIMIT_DEFAULT)
            self.assertEqual(len(m2["entities"]), m2["total"])  # no 120 cap


if __name__ == "__main__":
    unittest.main()
