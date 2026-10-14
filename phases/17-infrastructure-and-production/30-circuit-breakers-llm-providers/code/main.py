"""Phase 17 Lesson 30: circuit breakers for LLM providers.
Lesson: ../docs/en.md
Pattern source: Michael Nygard, "Release It!" (2nd edition, 2018), Stability Patterns.
State names follow Martin Fowler, "CircuitBreaker" (martinfowler.com/bliki/CircuitBreaker.html).
This deterministic simulator uses only Python's standard library.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable


CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class ProviderError(Exception):
    """A provider call failed. status is the HTTP status, or None for a timeout."""

    def __init__(self, status: int | None, message: str = "") -> None:
        super().__init__(message or f"provider error status={status}")
        self.status = status


class CircuitOpenError(Exception):
    """The breaker refused the call. The provider was never contacted."""


class AllProvidersUnavailable(Exception):
    """Every route was either open or failed."""


def counts_as_failure(status: int | None) -> bool:
    """Timeouts, 429 and 5xx blame the provider. Any other 4xx blames the request."""
    if status is None:
        return True
    return status == 429 or status >= 500


class ManualClock:
    """A clock the caller moves by hand, so no test or demo ever sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


@dataclass(frozen=True)
class BreakerConfig:
    window_size: int = 10
    minimum_calls: int = 5
    failure_rate_threshold: float = 0.5
    cooldown_seconds: float = 30.0
    cooldown_multiplier: float = 2.0
    max_cooldown_seconds: float = 300.0
    half_open_probes: int = 2


class CircuitBreaker:
    def __init__(self, name: str, clock: ManualClock, config: BreakerConfig = BreakerConfig()) -> None:
        self.name = name
        self.clock = clock
        self.config = config
        self.state = CLOSED
        self.window: deque[bool] = deque(maxlen=config.window_size)
        self.cooldown = config.cooldown_seconds
        self.opened_at = 0.0
        self.probes_issued = 0
        self.probe_successes = 0
        self.transitions: list[tuple[float, str, str]] = []

    def _move(self, new_state: str) -> None:
        self.transitions.append((self.clock.now(), self.state, new_state))
        self.state = new_state

    def _trip(self) -> None:
        self.opened_at = self.clock.now()
        self._move(OPEN)

    def failure_rate(self) -> float:
        if not self.window:
            return 0.0
        return self.window.count(False) / len(self.window)

    def allow(self) -> bool:
        if self.state == OPEN and self.clock.now() - self.opened_at >= self.cooldown:
            self.probes_issued = 0
            self.probe_successes = 0
            self._move(HALF_OPEN)
        if self.state == CLOSED:
            return True
        if self.state == HALF_OPEN and self.probes_issued < self.config.half_open_probes:
            self.probes_issued += 1
            return True
        return False

    def record_success(self) -> None:
        if self.state == OPEN:
            return
        if self.state == HALF_OPEN:
            self.probe_successes += 1
            if self.probe_successes >= self.config.half_open_probes:
                self.window.clear()
                self.cooldown = self.config.cooldown_seconds
                self._move(CLOSED)
            return
        self.window.append(True)

    def record_failure(self) -> None:
        if self.state == OPEN:
            return
        if self.state == HALF_OPEN:
            self.cooldown = min(
                self.cooldown * self.config.cooldown_multiplier,
                self.config.max_cooldown_seconds,
            )
            self._trip()
            return
        self.window.append(False)
        enough_calls = len(self.window) >= self.config.minimum_calls
        if enough_calls and self.failure_rate() >= self.config.failure_rate_threshold:
            self._trip()

    def call(self, fn: Callable[[], str]) -> str:
        if not self.allow():
            raise CircuitOpenError(self.name)
        try:
            result = fn()
        except ProviderError as error:
            if counts_as_failure(error.status):
                self.record_failure()
            else:
                self.record_success()
            raise
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


@dataclass
class ScriptedProvider:
    """A fake provider that times out during scripted outage windows."""

    name: str
    clock: ManualClock
    outages: list[tuple[float, float]] = field(default_factory=list)
    latency_seconds: float = 1.0
    timeout_seconds: float = 10.0
    calls: int = 0
    failed_calls: int = 0
    seconds_spent: float = 0.0

    def is_down(self) -> bool:
        now = self.clock.now()
        return any(start <= now < end for start, end in self.outages)

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.is_down():
            self.failed_calls += 1
            self.seconds_spent += self.timeout_seconds
            raise ProviderError(None, f"{self.name} timed out")
        self.seconds_spent += self.latency_seconds
        return f"{self.name}: {prompt}"


class FallbackRouter:
    """Tries each route in order and skips any route whose breaker is open."""

    def __init__(self, routes: list[tuple[ScriptedProvider, CircuitBreaker | None]]) -> None:
        self.routes = routes
        self.fast_fails = 0

    def complete(self, prompt: str) -> str:
        for provider, breaker in self.routes:
            try:
                if breaker is None:
                    return provider.complete(prompt)
                return breaker.call(lambda: provider.complete(prompt))
            except CircuitOpenError:
                self.fast_fails += 1
            except ProviderError as error:
                if not counts_as_failure(error.status):
                    raise
        raise AllProvidersUnavailable(prompt)


def run_outage(use_breaker: bool, ticks: int = 150, outage: tuple[float, float] = (20.0, 80.0)) -> dict:
    """Send one request per second through a primary that is down during the outage."""
    clock = ManualClock()
    primary = ScriptedProvider("primary", clock, outages=[outage])
    fallback = ScriptedProvider("fallback", clock)
    breaker = CircuitBreaker("primary", clock) if use_breaker else None
    router = FallbackRouter([(primary, breaker), (fallback, None)])

    answered = 0
    for _ in range(ticks):
        router.complete("ping")
        answered += 1
        clock.advance(1.0)

    total_seconds = primary.seconds_spent + fallback.seconds_spent
    return {
        "answered": answered,
        "primary_failed_calls": primary.failed_calls,
        "fallback_calls": fallback.calls,
        "fast_fails": router.fast_fails,
        "mean_latency_seconds": total_seconds / ticks,
        "transitions": breaker.transitions if breaker else [],
    }


def main() -> None:
    without = run_outage(use_breaker=False)
    with_breaker = run_outage(use_breaker=True)

    print("150 requests, one per second. Primary is down from t=20s to t=80s.")
    print("A failed primary call costs a 10s timeout. A healthy call costs 1s.\n")

    header = f"{'metric':28}  {'retry only':>10}  {'breaker':>10}"
    print(header)
    print("-" * len(header))
    rows = [
        ("requests answered", "answered", "d"),
        ("timeouts paid on primary", "primary_failed_calls", "d"),
        ("calls skipped (fast fail)", "fast_fails", "d"),
        ("calls sent to fallback", "fallback_calls", "d"),
        ("mean latency (seconds)", "mean_latency_seconds", ".2f"),
    ]
    for label, key, fmt in rows:
        print(f"{label:28}  {without[key]:>10{fmt}}  {with_breaker[key]:>10{fmt}}")

    print("\nbreaker transitions:")
    for at, old, new in with_breaker["transitions"]:
        print(f"  t={at:>5.0f}s  {old:>9} -> {new}")


if __name__ == "__main__":
    main()
