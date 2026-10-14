"""Phase 16 Lesson 26: publish-subscribe messaging for agents.
Lesson: ../docs/en.md
Pattern source: Eugster et al., "The Many Faces of Publish/Subscribe", ACM Computing Surveys 35(2), 2003.
Subject wildcards follow the NATS convention: "*" matches one token, ">" matches the rest.
This deterministic in-memory broker uses only Python's standard library.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Message:
    id: int
    topic: str
    payload: str
    key: str | None = None


@dataclass
class Delivery:
    message: Message
    attempt: int = 1


Handler = Callable[[Message], bool]


@dataclass
class Group:
    """One subscription. Its members compete: each message goes to exactly one of them."""

    name: str
    pattern: str
    members: list[tuple[str, Handler]] = field(default_factory=list)
    queue: deque[Delivery] = field(default_factory=deque)
    next_member: int = 0


def topic_matches(pattern: str, topic: str) -> bool:
    pattern_tokens = pattern.split(".")
    topic_tokens = topic.split(".")
    for index, token in enumerate(pattern_tokens):
        if token == ">":
            return index == len(pattern_tokens) - 1 and len(topic_tokens) > index
        if index >= len(topic_tokens):
            return False
        if token != "*" and token != topic_tokens[index]:
            return False
    return len(pattern_tokens) == len(topic_tokens)


class Broker:
    def __init__(self, max_deliveries: int = 3) -> None:
        self.max_deliveries = max_deliveries
        self.groups: dict[str, Group] = {}
        self.dead_letters: list[tuple[str, Message]] = []
        self.dropped: list[Message] = []
        self.log: list[tuple[str, str, int, int]] = []
        self._next_id = 1

    def subscribe(self, pattern: str, group: str, consumer: str, handler: Handler) -> None:
        existing = self.groups.setdefault(group, Group(group, pattern))
        if existing.pattern != pattern:
            raise ValueError(f"group {group!r} already subscribes to {existing.pattern!r}")
        existing.members.append((consumer, handler))

    def publish(self, topic: str, payload: str, key: str | None = None) -> Message:
        message = Message(self._next_id, topic, payload, key)
        self._next_id += 1
        matched = [group for group in self.groups.values() if topic_matches(group.pattern, topic)]
        if not matched:
            self.dropped.append(message)
        for group in matched:
            group.queue.append(Delivery(message))
        return message

    def _pick(self, group: Group, message: Message) -> tuple[str, Handler]:
        if message.key is not None:
            digest = hashlib.sha256(message.key.encode()).digest()
            index = int.from_bytes(digest[:8], "big") % len(group.members)
        else:
            index = group.next_member % len(group.members)
            group.next_member += 1
        return group.members[index]

    def _deliver(self, group: Group) -> None:
        delivery = group.queue.popleft()
        consumer, handler = self._pick(group, delivery.message)
        self.log.append((group.name, consumer, delivery.message.id, delivery.attempt))
        try:
            acked = handler(delivery.message)
        except Exception:
            acked = False
        if acked:
            return
        if delivery.attempt >= self.max_deliveries:
            self.dead_letters.append((group.name, delivery.message))
        else:
            group.queue.appendleft(Delivery(delivery.message, delivery.attempt + 1))

    def run(self) -> int:
        """Deliver until every queue is empty. Returns the number of delivery attempts."""
        attempts = 0
        while any(group.queue for group in self.groups.values()):
            for group in list(self.groups.values()):
                if group.queue:
                    self._deliver(group)
                    attempts += 1
        return attempts


class IdempotentHandler:
    """Remembers processed message ids so a redelivery does not repeat the effect."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.seen: set[int] = set()
        self.duplicates = 0

    def __call__(self, message: Message) -> bool:
        if message.id in self.seen:
            self.duplicates += 1
            return True
        acked = self.handler(message)
        if acked:
            self.seen.add(message.id)
        return acked


class LostAck:
    """Runs the handler, then loses the acknowledgement once for the chosen message ids."""

    def __init__(self, handler: Handler, lose: set[int]) -> None:
        self.handler = handler
        self.lose = set(lose)

    def __call__(self, message: Message) -> bool:
        acked = self.handler(message)
        if acked and message.id in self.lose:
            self.lose.discard(message.id)
            return False
        return acked


def run_demo(idempotent: bool) -> dict:
    """Three subscriptions on one event stream from two agent runs."""
    broker = Broker(max_deliveries=3)
    audited: list[str] = []
    reviewed: dict[str, list[str]] = {"reviewer-1": [], "reviewer-2": []}
    charges: list[int] = []

    def auditor(message: Message) -> bool:
        audited.append(message.topic)
        return True

    def reviewer(name: str) -> Handler:
        def handle(message: Message) -> bool:
            if message.payload == "malformed":
                raise ValueError("cannot parse result")
            reviewed[name].append(f"{message.key}:{message.payload}")
            return True

        return handle

    def charge(message: Message) -> bool:
        charges.append(message.id)
        return True

    billing: Handler = IdempotentHandler(charge) if idempotent else charge

    broker.subscribe("task.>", "audit", "auditor", auditor)
    broker.subscribe("task.*.done", "review", "reviewer-1", reviewer("reviewer-1"))
    broker.subscribe("task.*.done", "review", "reviewer-2", reviewer("reviewer-2"))
    broker.subscribe("task.*.done", "billing", "billing", LostAck(billing, lose={2}))

    broker.publish("task.research.done", "step-1", key="run-a")
    broker.publish("task.code.done", "step-1", key="run-e")
    broker.publish("task.code.failed", "step-2", key="run-e")
    broker.publish("task.research.done", "step-2", key="run-a")
    broker.publish("task.code.done", "malformed", key="run-e")
    broker.publish("task.code.done", "step-3", key="run-e")
    broker.publish("metrics.tokens", "1840")
    attempts = broker.run()

    return {
        "published": 7,
        "delivery_attempts": attempts,
        "audited": len(audited),
        "reviewed": reviewed,
        "charges": len(charges),
        "dead_letters": [(group, message.id) for group, message in broker.dead_letters],
        "dropped": [message.topic for message in broker.dropped],
        "log": broker.log,
    }


def main() -> None:
    naive = run_demo(idempotent=False)
    safe = run_demo(idempotent=True)

    print("7 events published by two agent runs (run-a, run-e).")
    print("Subscriptions: audit on task.>, review (2 members) and billing on task.*.done.\n")

    print(f"delivery attempts:      {safe['delivery_attempts']}")
    print(f"audit received:         {safe['audited']} events")
    for name, items in safe["reviewed"].items():
        print(f"{name} handled:    {', '.join(items)}")
    print(f"dead letters:           {safe['dead_letters']}")
    print(f"dropped, no subscriber: {safe['dropped']}\n")

    print("billing lost the acknowledgement for message 2, so the broker redelivered it.")
    print(f"charges with a plain handler:       {naive['charges']} (5 billable events)")
    print(f"charges with an idempotent handler: {safe['charges']}")


if __name__ == "__main__":
    main()
