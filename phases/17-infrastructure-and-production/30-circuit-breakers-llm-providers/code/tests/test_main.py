"""Deterministic tests for the circuit breaker lesson."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("lesson29_circuit_breaker", MODULE_PATH)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = main
SPEC.loader.exec_module(main)


def timeout() -> str:
    raise main.ProviderError(None)


def ok() -> str:
    return "ok"


class BreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = main.ManualClock()
        self.breaker = main.CircuitBreaker("primary", self.clock)

    def fail(self, times: int) -> None:
        for _ in range(times):
            with self.assertRaises(main.ProviderError):
                self.breaker.call(timeout)

    def open_breaker(self) -> None:
        self.fail(self.breaker.config.minimum_calls)
        self.assertEqual(self.breaker.state, main.OPEN)

    def test_starts_closed_and_passes_calls_through(self) -> None:
        self.assertEqual(self.breaker.state, main.CLOSED)
        self.assertEqual(self.breaker.call(ok), "ok")

    def test_does_not_trip_below_minimum_calls(self) -> None:
        self.fail(self.breaker.config.minimum_calls - 1)
        self.assertEqual(self.breaker.state, main.CLOSED)
        self.assertEqual(self.breaker.failure_rate(), 1.0)

    def test_trips_at_threshold_and_stops_calling_the_provider(self) -> None:
        self.open_breaker()
        touched = []
        with self.assertRaises(main.CircuitOpenError):
            self.breaker.call(lambda: touched.append(1) or "ok")
        self.assertEqual(touched, [])

    def test_failure_rate_uses_the_sliding_window(self) -> None:
        for _ in range(10):
            self.breaker.call(ok)
        self.fail(4)
        self.assertEqual(self.breaker.state, main.CLOSED)
        self.fail(1)
        self.assertEqual(self.breaker.state, main.OPEN)

    def test_half_open_admits_only_the_probe_budget(self) -> None:
        self.open_breaker()
        self.clock.advance(29.0)
        self.assertFalse(self.breaker.allow())
        self.clock.advance(1.0)
        self.assertTrue(self.breaker.allow())
        self.assertEqual(self.breaker.state, main.HALF_OPEN)
        self.assertTrue(self.breaker.allow())
        self.assertFalse(self.breaker.allow())

    def test_successful_probes_close_and_reset(self) -> None:
        self.open_breaker()
        self.clock.advance(30.0)
        self.breaker.call(ok)
        self.assertEqual(self.breaker.state, main.HALF_OPEN)
        self.breaker.call(ok)
        self.assertEqual(self.breaker.state, main.CLOSED)
        self.assertEqual(len(self.breaker.window), 0)
        self.assertEqual(self.breaker.cooldown, 30.0)

    def test_failed_probe_reopens_with_longer_capped_cooldown(self) -> None:
        self.open_breaker()
        expected = [60.0, 120.0, 240.0, 300.0, 300.0]
        for cooldown in expected:
            self.clock.advance(self.breaker.cooldown)
            self.fail(1)
            self.assertEqual(self.breaker.state, main.OPEN)
            self.assertEqual(self.breaker.cooldown, cooldown)

    def test_request_errors_do_not_count_against_the_provider(self) -> None:
        def bad_request() -> str:
            raise main.ProviderError(400)

        for _ in range(20):
            with self.assertRaises(main.ProviderError):
                self.breaker.call(bad_request)
        self.assertEqual(self.breaker.state, main.CLOSED)
        self.assertEqual(self.breaker.failure_rate(), 0.0)
        self.assertTrue(main.counts_as_failure(None))
        self.assertTrue(main.counts_as_failure(429))
        self.assertTrue(main.counts_as_failure(503))
        self.assertFalse(main.counts_as_failure(401))

    def test_unexpected_exception_counts_as_failure(self) -> None:
        def broken() -> str:
            raise ConnectionResetError("socket closed")

        for _ in range(self.breaker.config.minimum_calls):
            with self.assertRaises(ConnectionResetError):
                self.breaker.call(broken)
        self.assertEqual(self.breaker.state, main.OPEN)

    def test_late_results_while_open_do_not_move_the_cooldown(self) -> None:
        self.open_breaker()
        opened_at = self.breaker.opened_at
        self.clock.advance(10.0)
        self.breaker.record_failure()
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, main.OPEN)
        self.assertEqual(self.breaker.opened_at, opened_at)
        self.assertEqual(len(self.breaker.transitions), 1)


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = main.ManualClock()
        self.primary = main.ScriptedProvider("primary", self.clock, outages=[(0.0, 100.0)])
        self.fallback = main.ScriptedProvider("fallback", self.clock)
        self.breaker = main.CircuitBreaker("primary", self.clock)

    def test_router_falls_back_then_fast_fails(self) -> None:
        router = main.FallbackRouter([(self.primary, self.breaker), (self.fallback, None)])
        answers = [router.complete("q") for _ in range(8)]
        self.assertTrue(all(answer == "fallback: q" for answer in answers))
        self.assertEqual(self.primary.calls, 5)
        self.assertEqual(router.fast_fails, 3)

    def test_request_error_is_not_failed_over(self) -> None:
        class Rejecting(main.ScriptedProvider):
            def complete(self, prompt: str) -> str:
                self.calls += 1
                raise main.ProviderError(400)

        rejecting = Rejecting("primary", self.clock)
        router = main.FallbackRouter([(rejecting, self.breaker), (self.fallback, None)])
        with self.assertRaises(main.ProviderError):
            router.complete("q")
        self.assertEqual(self.fallback.calls, 0)

    def test_all_routes_down_raises(self) -> None:
        router = main.FallbackRouter([(self.primary, self.breaker)])
        with self.assertRaises(main.AllProvidersUnavailable):
            router.complete("q")


class SimulationTests(unittest.TestCase):
    def test_breaker_cuts_timeouts_without_losing_requests(self) -> None:
        without = main.run_outage(use_breaker=False)
        with_breaker = main.run_outage(use_breaker=True)
        self.assertEqual(without["answered"], 150)
        self.assertEqual(with_breaker["answered"], 150)
        self.assertEqual(without["primary_failed_calls"], 60)
        self.assertEqual(with_breaker["primary_failed_calls"], 6)
        self.assertLess(with_breaker["mean_latency_seconds"], without["mean_latency_seconds"])

    def test_simulation_ends_closed(self) -> None:
        transitions = main.run_outage(use_breaker=True)["transitions"]
        self.assertEqual(transitions[0][1:], (main.CLOSED, main.OPEN))
        self.assertEqual(transitions[-1][1:], (main.HALF_OPEN, main.CLOSED))


if __name__ == "__main__":
    unittest.main()
