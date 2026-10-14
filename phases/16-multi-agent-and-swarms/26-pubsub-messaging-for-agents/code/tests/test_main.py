"""Deterministic tests for the pub/sub lesson."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("lesson26_pubsub", MODULE_PATH)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = main
SPEC.loader.exec_module(main)


class TopicMatchingTests(unittest.TestCase):
    def test_exact_and_single_token_wildcard(self) -> None:
        self.assertTrue(main.topic_matches("task.code.done", "task.code.done"))
        self.assertTrue(main.topic_matches("task.*.done", "task.code.done"))
        self.assertFalse(main.topic_matches("task.*.done", "task.code.failed"))
        self.assertFalse(main.topic_matches("task.*", "task.code.done"))
        self.assertFalse(main.topic_matches("task.code.done", "task.code"))

    def test_tail_wildcard_needs_at_least_one_token(self) -> None:
        self.assertTrue(main.topic_matches("task.>", "task.code"))
        self.assertTrue(main.topic_matches("task.>", "task.code.done"))
        self.assertFalse(main.topic_matches("task.>", "task"))
        self.assertFalse(main.topic_matches("task.>", "metrics.tokens"))
        self.assertFalse(main.topic_matches("task.>.done", "task.code.done"))


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = main.Broker(max_deliveries=3)
        self.received: dict[str, list[int]] = {}

    def collector(self, name: str) -> main.Handler:
        self.received[name] = []

        def handle(message: main.Message) -> bool:
            self.received[name].append(message.id)
            return True

        return handle

    def test_every_group_gets_its_own_copy(self) -> None:
        self.broker.subscribe("task.>", "audit", "a", self.collector("a"))
        self.broker.subscribe("task.>", "billing", "b", self.collector("b"))
        self.broker.publish("task.code.done", "x")
        self.broker.run()
        self.assertEqual(self.received, {"a": [1], "b": [1]})

    def test_members_of_one_group_compete(self) -> None:
        self.broker.subscribe("task.>", "review", "r1", self.collector("r1"))
        self.broker.subscribe("task.>", "review", "r2", self.collector("r2"))
        for _ in range(4):
            self.broker.publish("task.code.done", "x")
        self.broker.run()
        self.assertEqual(self.received, {"r1": [1, 3], "r2": [2, 4]})

    def test_same_key_always_reaches_the_same_member_in_order(self) -> None:
        for name in ("r1", "r2", "r3"):
            self.broker.subscribe("task.>", "review", name, self.collector(name))
        ids = [self.broker.publish("task.code.done", "x", key="run-e").id for _ in range(5)]
        self.broker.run()
        owners = [name for name, got in self.received.items() if got]
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.received[owners[0]], ids)

    def test_publish_without_subscriber_is_dropped(self) -> None:
        self.broker.subscribe("task.>", "audit", "a", self.collector("a"))
        self.broker.publish("metrics.tokens", "1840")
        self.assertEqual(self.broker.run(), 0)
        self.assertEqual([message.topic for message in self.broker.dropped], ["metrics.tokens"])

    def test_group_cannot_change_its_pattern(self) -> None:
        self.broker.subscribe("task.>", "audit", "a", self.collector("a"))
        with self.assertRaises(ValueError):
            self.broker.subscribe("metrics.>", "audit", "b", self.collector("b"))

    def test_nack_redelivers_then_dead_letters(self) -> None:
        self.broker.subscribe("task.>", "review", "r1", lambda message: False)
        self.broker.publish("task.code.done", "poison")
        self.assertEqual(self.broker.run(), 3)
        self.assertEqual([entry[3] for entry in self.broker.log], [1, 2, 3])
        self.assertEqual([(group, message.id) for group, message in self.broker.dead_letters], [("review", 1)])

    def test_handler_exception_is_a_nack_and_blocks_the_head(self) -> None:
        order: list[str] = []

        def handle(message: main.Message) -> bool:
            order.append(message.payload)
            if message.payload == "poison":
                raise ValueError("bad payload")
            return True

        self.broker.subscribe("task.>", "review", "r1", handle)
        self.broker.publish("task.code.done", "poison")
        self.broker.publish("task.code.done", "good")
        self.broker.run()
        self.assertEqual(order, ["poison", "poison", "poison", "good"])

    def test_handler_can_publish_a_follow_up_event(self) -> None:
        def planner(message: main.Message) -> bool:
            self.broker.publish("task.code.requested", message.payload)
            return True

        self.broker.subscribe("task.plan.done", "planner", "p", planner)
        self.broker.subscribe("task.code.requested", "coder", "c", self.collector("c"))
        self.broker.publish("task.plan.done", "build parser")
        self.broker.run()
        self.assertEqual(self.received["c"], [2])


class DeliveryGuaranteeTests(unittest.TestCase):
    def run_billing(self, idempotent: bool) -> tuple[list[int], main.Handler]:
        broker = main.Broker()
        charges: list[int] = []

        def charge(message: main.Message) -> bool:
            charges.append(message.id)
            return True

        inner: main.Handler = main.IdempotentHandler(charge) if idempotent else charge
        broker.subscribe("task.>", "billing", "b", main.LostAck(inner, lose={1}))
        broker.publish("task.code.done", "x")
        broker.run()
        return charges, inner

    def test_lost_ack_repeats_the_effect(self) -> None:
        charges, _ = self.run_billing(idempotent=False)
        self.assertEqual(charges, [1, 1])

    def test_idempotent_handler_absorbs_the_redelivery(self) -> None:
        charges, handler = self.run_billing(idempotent=True)
        self.assertEqual(charges, [1])
        self.assertEqual(handler.duplicates, 1)

    def test_idempotent_handler_retries_a_real_failure(self) -> None:
        calls: list[int] = []

        def fails_once(message: main.Message) -> bool:
            calls.append(message.id)
            return len(calls) > 1

        handler = main.IdempotentHandler(fails_once)
        message = main.Message(1, "task.code.done", "x")
        self.assertFalse(handler(message))
        self.assertTrue(handler(message))
        self.assertEqual(calls, [1, 1])


class DemoTests(unittest.TestCase):
    def test_demo_totals(self) -> None:
        naive = main.run_demo(idempotent=False)
        safe = main.run_demo(idempotent=True)
        self.assertEqual(safe["audited"], 6)
        self.assertEqual(naive["charges"], 6)
        self.assertEqual(safe["charges"], 5)
        self.assertEqual(safe["dead_letters"], [("review", 5)])
        self.assertEqual(safe["dropped"], ["metrics.tokens"])

    def test_demo_keeps_each_run_on_one_reviewer_in_order(self) -> None:
        reviewed = main.run_demo(idempotent=True)["reviewed"]
        handled = [item for items in reviewed.values() for item in items]
        self.assertEqual(sorted(handled), ["run-a:step-1", "run-a:step-2", "run-e:step-1", "run-e:step-3"])
        for items in reviewed.values():
            self.assertEqual(len({item.split(":")[0] for item in items}), 1)
            self.assertEqual(items, sorted(items))


if __name__ == "__main__":
    unittest.main()
