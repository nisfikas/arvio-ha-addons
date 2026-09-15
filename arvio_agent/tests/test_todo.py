"""Λίστα για ψώνια (`arvio.todo`): documented services only, the envelope unwrapped, the gates closed."""
import unittest
from unittest import mock

from helpers import agent


class TodoItemsFromResponse(unittest.TestCase):
    """`todo.get_items` answers `{entity_id: {items: [...]}}` even for one entity, and every value is a string."""

    def test_unwraps_the_entity_keyed_envelope(self):
        resp = {
            "todo.shopping_list": {
                "items": [
                    {"uid": "a1", "summary": "Γάλα", "status": "needs_action"},
                    {"uid": "b2", "summary": "Ψωμί", "status": "needs_action"},
                ]
            }
        }
        self.assertEqual(
            agent.todo_items_from_response(resp, "todo.shopping_list"),
            [
                {"uid": "a1", "summary": "Γάλα", "status": "needs_action"},
                {"uid": "b2", "summary": "Ψωμί", "status": "needs_action"},
            ],
        )

    def test_a_differently_keyed_envelope_still_has_exactly_one_value(self):
        resp = {"todo.other_name": {"items": [{"uid": "x", "summary": "Αυγά", "status": "needs_action"}]}}
        out = agent.todo_items_from_response(resp, "todo.shopping_list")
        self.assertEqual([i["summary"] for i in out], ["Αυγά"])

    def test_two_values_are_ambiguous_and_answer_nothing(self):
        resp = {"todo.a": {"items": [{"summary": "ένα"}]}, "todo.b": {"items": [{"summary": "δύο"}]}}
        self.assertEqual(agent.todo_items_from_response(resp, "todo.missing"), [])

    def test_a_missing_uid_is_none_and_a_blank_summary_is_dropped(self):
        # `_api_items_factory` drops None fields entirely, so uid may simply be absent.
        resp = {"todo.l": {"items": [{"summary": "Τυρί"}, {"summary": "   "}, {"uid": "z"}, "not a dict"]}}
        self.assertEqual(agent.todo_items_from_response(resp, "todo.l"), [{"uid": None, "summary": "Τυρί", "status": "needs_action"}])

    def test_garbage_answers_an_empty_list_rather_than_raising(self):
        for bad in (None, [], "", {"todo.l": None}, {"todo.l": {}}, {"todo.l": {"items": None}}):
            self.assertEqual(agent.todo_items_from_response(bad, "todo.l"), [])

    def test_the_cap_holds(self):
        resp = {"todo.l": {"items": [{"uid": str(i), "summary": f"ψώνιο {i}"} for i in range(500)]}}
        self.assertEqual(len(agent.todo_items_from_response(resp, "todo.l")), agent.TODO_MAX_ITEMS)


class TodoAction(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = []

        def call(domain, service, data):
            self.calls.append((domain, service, dict(data)))
            return "ctx"

        def call_response(domain, service, data):
            self.responses.append((domain, service, dict(data)))
            return "ctx", {"todo.shopping_list": {"items": [{"uid": "a1", "summary": "Γάλα", "status": "needs_action"}]}}

        self.p1 = mock.patch.object(agent, "ha_call_service", side_effect=call)
        self.p2 = mock.patch.object(agent, "ha_call_service_response", side_effect=call_response)
        self.p1.start()
        self.p2.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)

    def test_list_reads_only_and_asks_for_needs_action_explicitly(self):
        out = agent.todo_action({"entity_id": "todo.shopping_list", "op": "list"})
        self.assertEqual(self.calls, [], "a read must write nothing")
        # services.yaml shows a needs_action default that the schema does not have: omitting it
        # would return completed items too, so it is always sent.
        self.assertEqual(self.responses, [("todo", "get_items", {"entity_id": "todo.shopping_list", "status": ["needs_action"]})])
        self.assertEqual(out, {"ok": True, "entity_id": "todo.shopping_list", "items": [{"uid": "a1", "summary": "Γάλα", "status": "needs_action"}]})

    def test_add_writes_with_the_plain_call_then_reads_the_fresh_list(self):
        out = agent.todo_action({"entity_id": "todo.shopping_list", "op": "add", "item": "  Γάλα  "})
        # add_item returns nothing, so asking it for a response would be a 400 either way.
        self.assertEqual(self.calls, [("todo", "add_item", {"entity_id": "todo.shopping_list", "item": "Γάλα"})])
        self.assertEqual(len(self.responses), 1, "one round trip, never two")
        self.assertEqual(out["items"][0]["summary"], "Γάλα")

    def test_tick_completes_by_uid_or_by_text(self):
        agent.todo_action({"entity_id": "todo.shopping_list", "op": "tick", "item": "a1"})
        self.assertEqual(self.calls, [("todo", "update_item", {"entity_id": "todo.shopping_list", "item": "a1", "status": "completed"})])

    def test_every_op_answers_with_the_list_as_it_now_stands(self):
        for op, item in (("list", None), ("add", "Ψωμί"), ("tick", "a1")):
            payload = {"entity_id": "todo.shopping_list", "op": op}
            if item:
                payload["item"] = item
            self.assertIn("items", agent.todo_action(payload))

    def test_the_entity_must_be_a_todo_list(self):
        for eid in ("", "light.kitchen", "media_player.kitchen", "todo", "shopping_list"):
            with self.assertRaises(ValueError):
                agent.todo_action({"entity_id": eid, "op": "list"})

    def test_an_unknown_op_is_refused_and_writes_nothing(self):
        for op in ("remove", "clear", "delete", "update", "LIST"):
            with self.assertRaises(ValueError):
                agent.todo_action({"entity_id": "todo.l", "op": op})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.responses, [])

    def test_a_write_without_an_item_is_refused_before_it_reaches_the_house(self):
        for op in ("add", "tick"):
            for item in (None, "", "   "):
                with self.assertRaises(ValueError):
                    agent.todo_action({"entity_id": "todo.l", "op": op, "item": item})
        with self.assertRaises(ValueError):
            agent.todo_action({"entity_id": "todo.l", "op": "add", "item": "x" * (agent.TODO_ITEM_MAX + 1)})
        self.assertEqual(self.calls, [])

    def test_op_defaults_to_list_so_a_bare_call_never_writes(self):
        agent.todo_action({"entity_id": "todo.shopping_list"})
        self.assertEqual(self.calls, [])


class TodoGates(unittest.TestCase):
    """The shopping list is the household's own words: allowed on the authenticated path, never on the LAN."""

    def test_allowed_on_the_authenticated_path(self):
        self.assertIn("arvio.todo", agent.AGENT_SERVICE_ALLOWLIST)
        agent.check_command_safety({"action": "arvio.todo", "payload": {"entity_id": "todo.l"}}, via="relay")

    def test_refused_on_the_unauthenticated_lan_path(self):
        self.assertNotIn("arvio.todo", agent.LAN_ALLOWED_ACTIONS)
        with self.assertRaises(agent.CommandRejected) as ctx:
            agent.check_command_safety({"action": "arvio.todo", "payload": {"entity_id": "todo.l"}}, via="lan")
        self.assertEqual(ctx.exception.code, "lan_forbidden")

    def test_it_is_not_lumped_in_with_music(self):
        # MEDIA_ACTIONS is exactly the set the LAN path forwards; the list must not join it.
        self.assertNotIn("arvio.todo", agent.MEDIA_ACTIONS)


if __name__ == "__main__":
    unittest.main()
