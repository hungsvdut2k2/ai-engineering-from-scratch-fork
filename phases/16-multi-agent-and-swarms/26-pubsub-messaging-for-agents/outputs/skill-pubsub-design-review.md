---
name: pubsub-design-review
description: Review a publish-subscribe design for a multi-agent system — topic naming, subscription layout, delivery guarantees, idempotency, ordering, dead-letter handling, and loop guards.
version: 1.0.0
phase: 16
lesson: 26
tags: [pubsub, messaging, event-driven, multi-agent, idempotency, dead-letter-queue]
---

Given a set of agents and the events that flow between them (a design document, broker config, or handler code), review the pub/sub design.

Ask for these inputs if they are absent:

1. The agents and, for each, the events it publishes and the events it reacts to.
2. The broker and its delivery guarantee.
3. The side effects of each handler: LLM calls, charges, writes, outbound messages.
4. Which events must be handled in order, and relative to what (run, user, document).
5. Expected event volume and the slowest handler's duration.

Produce:

1. Fit. For each interaction, state whether pub/sub is the right pattern. Recommend a direct call when the sender needs the answer, and a work queue when exactly one receiver must act on a command.
2. Topic naming. Require hierarchical names that state a fact in the past tense. Flag command-style topics and topics that encode the receiver's name.
3. Subscription layout. List each subscription, its pattern, and its members. For every pair of consumers, state whether they should fan out (separate subscriptions) or compete (same subscription). Flag any pair placed the wrong way round.
4. Delivery guarantee. State at-most-once or at-least-once per subscription. Flag at-most-once on any event whose loss changes money, state, or user-visible output.
5. Idempotency. For every at-least-once handler with a side effect, name the idempotency key and where it is stored. Require the effect and the processed-id record to commit together, or the key to be passed downstream.
6. Ordering. Name the routing key for each ordered stream. State the head-of-line blocking this causes and confirm it is bounded by a delivery limit.
7. Dead letters. Require a delivery limit, a dead-letter destination per subscription, an alert on its depth, and an owner who replays or discards.
8. Loop and trace guards. Require a correlation id and a hop count on every message. List every cycle in the publish-react graph.
9. Backpressure. State what happens when a subscriber falls behind: bounded queue, drop policy, or publisher blocking.

Hard rejects:
- An at-least-once handler that charges, emails, or writes without an idempotency key. The first lost acknowledgement repeats the effect.
- Unlimited redelivery with no dead-letter destination. One malformed message stops the subscription.
- A cycle in the publish-react graph with no hop limit. Two agents can trigger each other without end and spend tokens on every turn.
- Two consumers that must each see every event placed in one subscription. Each sees only part of the stream.

Refusal rules:
- If handler side effects are unknown, refuse to approve a delivery guarantee until they are listed.
- If no one owns the dead-letter queue, refuse to approve; unowned dead letters are silent data loss.

Output: a one-page review with a pass, gap, or reject verdict for each of the nine items, a table of subscriptions (pattern, members, guarantee, routing key, idempotency key), and the first failure test to run: lose one acknowledgement on the most expensive handler and confirm the effect happens once.
