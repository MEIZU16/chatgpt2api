"""Concurrent requests sharing the solver's one chatgpt.com page.

On 2026-10-16 about 5 simultaneous /v1/turnstile requests broke each other: the page module's
WeakMap hook is installed on the global prototype, so interleaved solves read the wrong p and
failed with "Unexpected token '<' ... is not valid JSON". Each failure reopened the page, which
closed it under the requests still running ("Target page, context or browser has been closed").
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SOLVER_DIR = Path(__file__).resolve().parent.parent / "sentinel_solver"
sys.path.insert(0, str(SOLVER_DIR))
_spec = importlib.util.spec_from_file_location("sentinel_solver_app_concurrency", SOLVER_DIR / "app.py")
solver_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(solver_app)
sys.path.remove(str(SOLVER_DIR))

ASSET = "https://chatgpt.com/unauth-mweb/assets/turnstile-Abc123.js"
SLOW_EXPRESSION = "window.__slow()"
FLAKY_EXPRESSION = "window.__flaky()"
CLOSED_ERROR = "Page.evaluate: Target page, context or browser has been closed"


async def yield_a_few(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


class FakeSolverPage:
    """Stands in for the chatgpt.com page; the module solve reproduces the global WeakMap hook."""

    def __init__(self, browser: "FakeSolverBrowser", generation: int) -> None:
        self.browser = browser
        self.generation = generation
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, *args, **kwargs):
        return None

    async def title(self) -> str:
        return "ChatGPT"

    async def evaluate(self, expression: str, arg: object = None) -> object:
        if self.closed:
            raise RuntimeError(CLOSED_ERROR)
        browser = self.browser
        if expression.startswith("location.hostname"):
            return True
        if expression == solver_app.ENVIRONMENT_SCRIPT:
            return {"language": "zh-CN"}
        if expression == solver_app.DISCOVER_TURNSTILE_ASSET:
            browser.discoveries += 1
            await yield_a_few()
            return [ASSET]
        if expression == solver_app.SOLVE_VIA_PAGE_MODULE:
            return await self._solve_via_module(*arg)
        if expression == solver_app.SOLVE_VIA_VM:
            return f"vm-token-for-{arg[1]}"
        if expression == SLOW_EXPRESSION:
            await browser.release_slow.wait()
            if self.closed:
                raise RuntimeError(CLOSED_ERROR)
            return "slow-done"
        if expression == FLAKY_EXPRESSION:
            await yield_a_few()
            if self.generation in browser.broken_generations:
                raise RuntimeError("Execution context was destroyed")
            return f"flaky-ok-{self.generation}"
        raise AssertionError(f"unexpected expression: {expression[:60]}")

    async def _solve_via_module(self, requirements, p, asset):
        browser = self.browser
        if asset != ASSET:
            return f"ERR:TypeError: Failed to fetch dynamically imported module: {asset}"
        if self.generation in browser.broken_generations:
            return "ERR:getEnforcementToken missing"
        browser.active_solves += 1
        browser.max_active_solves = max(browser.max_active_solves, browser.active_solves)
        original = browser.weakmap_hook
        browser.weakmap_hook = p
        try:
            await yield_a_few()
            seen = browser.weakmap_hook
        finally:
            browser.weakmap_hook = original
            browser.active_solves -= 1
        if self.closed:
            raise RuntimeError(CLOSED_ERROR)
        if seen != p:
            return "ERR:SyntaxError: Unexpected token '<', \"<ymaw...\" is not valid JSON"
        return f"token-for-{p}"


class FakeSolverContext:
    def __init__(self, page: FakeSolverPage) -> None:
        self.page = page
        self.closed = False

    def set_default_timeout(self, timeout) -> None:
        return None

    async def add_init_script(self, script) -> None:
        return None

    async def new_page(self) -> FakeSolverPage:
        return self.page

    async def close(self) -> None:
        self.closed = True
        self.page.closed = True


class FakeSolverBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeSolverContext] = []
        self.discoveries = 0
        self.broken_generations: set[int] = set()
        self.weakmap_hook = None
        self.active_solves = 0
        self.max_active_solves = 0
        self.release_slow = asyncio.Event()

    async def new_context(self, **kwargs) -> FakeSolverContext:
        context = FakeSolverContext(FakeSolverPage(self, len(self.contexts) + 1))
        self.contexts.append(context)
        return context

    @property
    def reopens(self) -> int:
        return len(self.contexts) - 1


class FakeStealth:
    async def apply_stealth_async(self, context):
        return None


class ConcurrentSolveTests(unittest.TestCase):
    def setUp(self):
        mock.patch.object(solver_app, "Stealth", FakeStealth).start()
        self.addCleanup(mock.patch.stopall)

    def run_async(self, coro_factory):
        async def runner():
            solver = solver_app.SolverPage()
            solver.user_agent = "UA/153"
            solver._browser = FakeSolverBrowser()
            await solver.page()
            return solver, await coro_factory(solver)

        return asyncio.run(runner())

    def test_concurrent_solves_on_one_page_all_succeed_without_reopening(self):
        async def burst(solver):
            return await asyncio.gather(
                *(solver.solve_turnstile({"seed": i}, "", f"p{i}") for i in range(6)), return_exceptions=True
            )

        solver, results = self.run_async(burst)
        self.assertEqual(results, [(True, f"token-for-p{i}") for i in range(6)])
        browser = solver._browser
        self.assertEqual(browser.max_active_solves, 1)
        self.assertEqual(browser.reopens, 0)
        self.assertEqual(browser.discoveries, 1)
        self.assertEqual(solver._turnstile_asset, ASSET)

    def test_burst_failing_on_one_page_reopens_it_once_and_rediscovers_once(self):
        async def burst(solver):
            solver._browser.broken_generations.add(1)
            return await asyncio.gather(
                *(solver.solve_turnstile({"seed": i}, "dx", f"p{i}") for i in range(6)), return_exceptions=True
            )

        solver, results = self.run_async(burst)
        browser = solver._browser
        self.assertEqual(browser.reopens, 1)
        # once for the first page, once more after its module failed
        self.assertEqual(browser.discoveries, 2)
        # the first request fell back to the VM; nobody saw a closed page
        self.assertEqual(results[0], (True, "vm-token-for-p0"))
        self.assertEqual(results[1:], [(True, f"token-for-p{i}") for i in range(1, 6)])
        self.assertTrue(browser.contexts[0].closed)
        self.assertFalse(browser.contexts[1].closed)

    def test_concurrent_evaluate_failures_on_one_page_reopen_it_once(self):
        async def burst(solver):
            solver._browser.broken_generations.add(1)
            return await asyncio.gather(
                *(solver.evaluate(FLAKY_EXPRESSION, None) for _ in range(5)), return_exceptions=True
            )

        solver, results = self.run_async(burst)
        self.assertEqual(results, ["flaky-ok-2"] * 5)
        self.assertEqual(solver._browser.reopens, 1)

    def test_reopen_waits_for_in_flight_requests_before_closing_the_old_page(self):
        async def scenario(solver):
            browser = solver._browser
            slow = asyncio.create_task(solver.evaluate(SLOW_EXPRESSION, None))
            await yield_a_few()
            # a module failure on the same page invalidates it; the next solve reopens it
            browser.broken_generations.add(1)
            failed = await solver.solve_turnstile({"seed": 0}, "", "p0")
            solved = await solver.solve_turnstile({"seed": 1}, "", "p1")
            old_context_closed_early = browser.contexts[0].closed
            browser.release_slow.set()
            return failed, solved, old_context_closed_early, await slow

        solver, (failed, solved, closed_early, slow_result) = self.run_async(scenario)
        browser = solver._browser
        self.assertFalse(failed[0])
        self.assertEqual(solved, (True, "token-for-p1"))
        self.assertEqual(browser.reopens, 1)
        self.assertFalse(closed_early)
        self.assertEqual(slow_result, "slow-done")
        # closed once its last request returned
        self.assertTrue(browser.contexts[0].closed)
        self.assertEqual(solver._retired, {})
        self.assertEqual(solver._leases, {})

    def test_stale_invalidate_from_an_old_page_does_not_reopen_the_new_one(self):
        async def scenario(solver):
            await solver.invalidate(1)
            await solver.page()
            await solver.invalidate(1)
            await solver.page()

        solver, _ = self.run_async(scenario)
        self.assertEqual(solver._browser.reopens, 1)


if __name__ == "__main__":
    unittest.main()
