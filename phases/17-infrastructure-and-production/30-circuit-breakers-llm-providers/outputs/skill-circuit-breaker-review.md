---
name: circuit-breaker-review
description: Review a circuit breaker on an LLM provider call path — trip rule, error classification, half-open probes, scope, composition order, and fallback capacity.
version: 1.0.0
phase: 17
lesson: 29
tags: [circuit-breaker, reliability, fallback, ai-gateway, timeouts, retries]
---

Given a provider-call path (gateway config, client wrapper code, or a written design), review its circuit breaker.

Ask for these inputs if they are absent:

1. The providers, models, and regions in the route, in fallback order.
2. Timeout values, including time to first token for streaming calls.
3. Retry policy: attempts, backoff, and which errors are retried.
4. Breaker settings: window, minimum calls, threshold, cooldown, probe count.
5. Number of gateway replicas and whether breaker state is shared.
6. Rate limits of each fallback provider.

Produce:

1. Trip rule. State whether it is consecutive-count or failure-rate. Require a minimum-call guard for any rate-based rule. Flag a consecutive counter on a provider with partial failures.
2. Error classification. List which statuses count. Timeouts, connection errors, 429, and 5xx must count. 400, 401, 403, and content-filter refusals must not.
3. Half-open. Confirm a bounded probe count, a rule for closing, and a rule for reopening. Confirm every admitted call records exactly one outcome, including on unexpected exceptions.
4. Cooldown. Confirm it grows after a failed probe, has a cap, and honours `Retry-After` when present.
5. Scope. Name the breaker key. Recommend provider + model + region unless traffic per key is below the minimum-call guard.
6. Composition order. Require retry outside breaker outside timeout. Flag any path with no timeout.
7. Fallback capacity. Compare peak traffic to each fallback's rate limit. State what the caller receives when every route is open.
8. Observability. Require a metric or log for every state transition and a count of fast fails.

Hard rejects:
- A breaker with no timeout on the wrapped call. A hung provider is never counted.
- A rate-based rule with no minimum-call guard. One error after a restart opens the circuit.
- Counting 400 or content-filter responses as failures. Bad user input takes the provider out of rotation.
- Unbounded half-open. Recovery testing sends full traffic to a provider that may still be down.

Refusal rules:
- If no fallback exists and no degraded response is defined, refuse to approve; an open breaker would only convert slow errors into fast errors with no plan for the user.
- If timeouts are unknown, refuse to tune thresholds until they are measured.

Output: a one-page review with a pass, gap, or reject verdict for each of the eight items, the recommended settings, and the first chaos experiment to verify them (Phase 17 · 24: inject a provider outage and confirm the open, half-open, and closed transitions appear in the logs).
