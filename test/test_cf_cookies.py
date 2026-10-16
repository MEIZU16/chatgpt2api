"""Cloudflare cookies fetched by the solver's real browser, cached per proxy and injected into backend sessions.

On 2026-10-16 the backend's homepage GET was challenged (403, cf-mitigated: challenge) 10/10 times
on a proxy IP where a real browser got a plain 200. Sending that browser's __cf_bm/_cfuvid cookies
from the same IP passed 10/10, so the backend asks the solver for them when it is challenged.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from curl_cffi.requests import Headers
from fastapi import HTTPException

import services.cf_cookie_cache as cache_module
import services.openai_backend_api as backend_module
from services import sentinel_solver_client as client_module
from services.cf_cookie_cache import CloudflareCookieCache, cf_cookie_cache
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI

ACCOUNT_PROXY = "http://acct:secret@198.51.100.7:12323"
OTHER_PROXY = "http://other:pass@192.0.2.44:12323"

SOLVER_DIR = Path(__file__).resolve().parent.parent / "sentinel_solver"
sys.path.insert(0, str(SOLVER_DIR))
_spec = importlib.util.spec_from_file_location("sentinel_solver_app", SOLVER_DIR / "app.py")
solver_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(solver_app)
sys.path.remove(str(SOLVER_DIR))


def solver_url(url: str = "http://solver:8080"):
    return mock.patch.object(type(config), "sentinel_solver_url", new_callable=mock.PropertyMock, return_value=url)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self) -> object:
        return self._payload


class CookieCacheTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = mock.Mock(return_value=({"__cf_bm": "bm1", "_cfuvid": "uv1", "oai-did": "x"}, None))
        self.cache = CloudflareCookieCache(self.fetcher)
        patcher = solver_url()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_fetches_keeps_only_cloudflare_cookies_and_caches_them(self):
        self.assertEqual(self.cache.get_cached(ACCOUNT_PROXY), {})
        cookies = self.cache.refresh(ACCOUNT_PROXY)
        self.assertEqual(cookies, {"__cf_bm": "bm1", "_cfuvid": "uv1"})
        self.fetcher.assert_called_once_with(ACCOUNT_PROXY)
        self.assertEqual(self.cache.get_cached(ACCOUNT_PROXY), cookies)
        # cookies are bound to the egress IP: another proxy has nothing cached
        self.assertEqual(self.cache.get_cached(OTHER_PROXY), {})

    def test_ttl_follows_cookie_expiry_and_expired_entries_are_dropped(self):
        now = time.time()
        self.fetcher.return_value = ({"__cf_bm": "bm1"}, now + 10 * 60)
        self.cache.refresh(ACCOUNT_PROXY)
        entry = self.cache._entries[ACCOUNT_PROXY]
        self.assertAlmostEqual(entry.expires_at, now + 10 * 60 - cache_module.CF_COOKIE_MIN_TTL_SECS, delta=2)
        with mock.patch.object(cache_module.time, "time", return_value=now + 11 * 60):
            self.assertEqual(self.cache.get_cached(ACCOUNT_PROXY), {})

    def test_ttl_is_capped_when_solver_gives_no_expiry(self):
        now = time.time()
        self.cache.refresh(ACCOUNT_PROXY)
        self.assertAlmostEqual(
            self.cache._entries[ACCOUNT_PROXY].expires_at, now + cache_module.CF_COOKIE_MAX_TTL_SECS, delta=2
        )

    def test_refresh_reuses_cookies_another_caller_already_replaced(self):
        first = self.cache.refresh(ACCOUNT_PROXY)
        self.fetcher.return_value = ({"__cf_bm": "bm2"}, None)
        # caller still holding older cookies gets the newer cached ones without a second browser visit
        self.assertEqual(self.cache.refresh(ACCOUNT_PROXY, stale={"__cf_bm": "old"}), first)
        self.assertEqual(self.fetcher.call_count, 1)
        # caller whose cookies are the cached ones (and still challenged) forces a new fetch
        self.assertEqual(self.cache.refresh(ACCOUNT_PROXY, stale=first), {"__cf_bm": "bm2"})
        self.assertEqual(self.fetcher.call_count, 2)

    def test_failure_backs_off_instead_of_hammering_the_solver(self):
        self.fetcher.side_effect = client_module.SentinelSolverError("503")
        self.assertEqual(self.cache.refresh(ACCOUNT_PROXY), {})
        self.assertEqual(self.cache.refresh(ACCOUNT_PROXY), {})
        self.assertEqual(self.fetcher.call_count, 1)
        later = time.time() + cache_module.CF_COOKIE_FAILURE_BACKOFF_SECS + 1
        self.fetcher.side_effect = None
        with mock.patch.object(cache_module.time, "time", return_value=later):
            self.assertEqual(self.cache.refresh(ACCOUNT_PROXY), {"__cf_bm": "bm1", "_cfuvid": "uv1"})

    def test_empty_answer_counts_as_failure(self):
        self.fetcher.return_value = ({"oai-did": "x"}, None)
        self.assertEqual(self.cache.refresh(ACCOUNT_PROXY), {})
        self.assertGreater(self.cache._entries[ACCOUNT_PROXY].failed_until, time.time())

    def test_no_fetch_without_proxy_or_solver(self):
        self.assertEqual(self.cache.refresh(""), {})
        with solver_url(""):
            self.assertEqual(self.cache.refresh(ACCOUNT_PROXY), {})
        self.fetcher.assert_not_called()


class SolverClientTests(unittest.TestCase):
    def test_fetch_posts_proxy_with_key_and_parses_answer(self):
        with solver_url(), mock.patch.object(
            type(config), "sentinel_solver_key", new_callable=mock.PropertyMock, return_value="k"
        ), mock.patch.object(
            client_module.requests, "post",
            return_value=FakeResponse(200, {"cookies": {"__cf_bm": "bm"}, "expires_at": 1792130000.5}),
        ) as post:
            cookies, expires_at = client_module.fetch_cloudflare_cookies(ACCOUNT_PROXY)
        self.assertEqual(cookies, {"__cf_bm": "bm"})
        self.assertEqual(expires_at, 1792130000.5)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://solver:8080/v1/cf-cookies")
        self.assertEqual(kwargs["json"], {"proxy": ACCOUNT_PROXY})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer k")

    def test_fetch_raises_on_solver_error(self):
        with solver_url(), mock.patch.object(client_module.requests, "post", return_value=FakeResponse(503, {"detail": "x"})):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.fetch_cloudflare_cookies(ACCOUNT_PROXY)
        with solver_url(""):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.fetch_cloudflare_cookies(ACCOUNT_PROXY)


class BackendCookieInjectionTests(unittest.TestCase):
    def setUp(self):
        cf_cookie_cache.clear()
        self.addCleanup(cf_cookie_cache.clear)
        backend_module._healthy_bootstrap_proxies.clear()
        self.addCleanup(backend_module._healthy_bootstrap_proxies.clear)
        mock.patch.object(backend_module.config, "get_proxy_settings", return_value="").start()
        self.sleep = mock.patch("services.openai_backend_api.time.sleep").start()
        self.fetcher = mock.Mock(return_value=({"__cf_bm": "fresh", "_cfuvid": "uv"}, None))
        mock.patch.object(cf_cookie_cache, "_fetcher", self.fetcher).start()
        solver_url().start()
        self.addCleanup(mock.patch.stopall)

    def make_api(self, proxy=ACCOUNT_PROXY):
        with mock.patch.object(backend_module.account_service, "get_account", return_value={"proxy": proxy}):
            api = OpenAIBackendAPI("token")
        self.addCleanup(api.close)
        return api

    def response(self, status=200, headers=None):
        response = mock.Mock(status_code=status, headers=Headers(headers or {}), text="<html></html>")
        response.json.return_value = {}
        return response

    def challenge(self):
        return self.response(403, {"CF-Mitigated": "challenge", "Content-Type": "text/html"})

    @staticmethod
    def cf_cookies(api):
        return {c.name: c.value for c in api.session.cookies.jar if c.name in ("__cf_bm", "_cfuvid")}

    def test_bootstrap_sends_cached_cookies_without_asking_the_solver(self):
        cf_cookie_cache.refresh(ACCOUNT_PROXY)
        self.fetcher.reset_mock()
        api = self.make_api()
        seen = []
        with mock.patch.object(api.session, "get", side_effect=lambda *a, **k: seen.append(self.cf_cookies(api)) or self.response()):
            api._bootstrap()
        self.assertEqual(seen, [{"__cf_bm": "fresh", "_cfuvid": "uv"}])
        self.fetcher.assert_not_called()

    def test_challenge_refreshes_cookies_and_retries_at_once(self):
        api = self.make_api()
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response()]) as get, \
                mock.patch.object(api, "_build_session") as build_session:
            api._bootstrap()
        self.assertEqual(get.call_count, 2)
        self.fetcher.assert_called_once_with(ACCOUNT_PROXY)
        self.sleep.assert_not_called()
        build_session.assert_not_called()
        self.assertEqual(self.cf_cookies(api), {"__cf_bm": "fresh", "_cfuvid": "uv"})
        self.assertEqual(cf_cookie_cache.get_cached(ACCOUNT_PROXY), {"__cf_bm": "fresh", "_cfuvid": "uv"})

    def test_refreshed_cookies_replace_the_stale_ones(self):
        api = self.make_api()
        api.session.cookies.set("__cf_bm", "stale", domain=".chatgpt.com", path="/")
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response()]):
            api._bootstrap()
        values = [c.value for c in api.session.cookies.jar if c.name == "__cf_bm"]
        self.assertEqual(values, ["fresh"])

    def test_without_cookies_falls_back_to_waiting_retries(self):
        self.fetcher.side_effect = client_module.SentinelSolverError("down")
        api = self.make_api()
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response()]) as get:
            api._bootstrap()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.5)])

    def test_refresh_happens_once_per_client(self):
        api = self.make_api()
        responses = [self.challenge(), self.response(), self.challenge(), self.response()]
        with mock.patch.object(api.session, "get", side_effect=responses):
            api._bootstrap()
            api._get_me()
        self.assertEqual(self.fetcher.call_count, 1)
        # the second challenge got the plain waiting retry instead of another browser visit
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.5)])

    def test_api_call_challenge_also_refreshes(self):
        api = self.make_api()
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response()]) as get:
            api._get_me()
        self.assertEqual(get.call_count, 2)
        self.fetcher.assert_called_once_with(ACCOUNT_PROXY)

    def test_no_proxy_never_asks_the_solver(self):
        api = self.make_api(proxy="")
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response()]):
            api._bootstrap()
        self.fetcher.assert_not_called()

    def test_failover_session_gets_that_proxys_cached_cookies(self):
        backend_module._remember_bootstrap_proxy(OTHER_PROXY, healthy=True)
        self.fetcher.side_effect = lambda proxy: ({"__cf_bm": "other-ip"}, None)
        cf_cookie_cache.refresh(OTHER_PROXY)
        self.fetcher.side_effect = client_module.SentinelSolverError("down")
        api = self.make_api()
        real_build = api._build_session
        seen = []

        def build_session(proxy=""):
            session = real_build(proxy)
            session.get = mock.Mock(side_effect=lambda *a, **k: seen.append(
                {c.name: c.value for c in session.cookies.jar if c.name == "__cf_bm"}) or self.response())
            return session

        with mock.patch.object(api.session, "get", return_value=self.challenge()), \
                mock.patch.object(api, "_build_session", side_effect=build_session):
            api._bootstrap()
        self.assertEqual(api.proxy_url, OTHER_PROXY)
        self.assertEqual(seen, [{"__cf_bm": "other-ip"}])


class FakePage:
    def __init__(self, titles):
        self.titles = list(titles)

    async def goto(self, *args, **kwargs):
        return None

    async def title(self):
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]


class FakeContext:
    def __init__(self, page, cookie_rounds):
        self.page = page
        self.cookie_rounds = list(cookie_rounds)
        self.closed = False

    async def new_page(self):
        return self.page

    async def cookies(self, url):
        return self.cookie_rounds.pop(0) if len(self.cookie_rounds) > 1 else self.cookie_rounds[0]

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.kwargs = None

    async def new_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class FakeStealth:
    async def apply_stealth_async(self, context):
        return None


def browser_cookie(name, value, expires=-1):
    return {"name": name, "value": value, "expires": expires}


class SolverEndpointTests(unittest.TestCase):
    def setUp(self):
        mock.patch.object(solver_app, "Stealth", FakeStealth).start()
        mock.patch.object(solver_app.asyncio, "sleep", mock.AsyncMock()).start()
        self.addCleanup(mock.patch.stopall)
        self.solver = solver_app.SolverPage()
        self.solver.user_agent = "UA/153"

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_returns_only_cloudflare_cookies_via_the_requested_proxy(self):
        cookies_after = [
            browser_cookie("__cf_bm", "bm", expires=2000.0),
            browser_cookie("_cfuvid", "uv"),
            browser_cookie("__cflb", "lb", expires=1500.0),
            browser_cookie("oai-did", "device"),
        ]
        context = FakeContext(FakePage(["Just a moment...", "ChatGPT"]), [[], cookies_after])
        self.solver._browser = FakeBrowser(context)
        result = self.run_async(self.solver.cloudflare_cookies("http://u:p%40ss@203.0.113.5:12323"))
        self.assertEqual(result["cookies"], {"__cf_bm": "bm", "_cfuvid": "uv", "__cflb": "lb"})
        self.assertEqual(result["expires_at"], 1500.0)
        self.assertEqual(result["user_agent"], "UA/153")
        self.assertEqual(
            self.solver._browser.kwargs["proxy"],
            {"server": "http://203.0.113.5:12323", "username": "u", "password": "p@ss"},
        )
        self.assertEqual(self.solver._browser.kwargs["user_agent"], "UA/153")
        self.assertTrue(context.closed)

    def test_stuck_on_challenge_fails_and_closes_the_context(self):
        context = FakeContext(FakePage(["Just a moment..."]), [[browser_cookie("__cf_bm", "bm")]])
        self.solver._browser = FakeBrowser(context)
        with mock.patch.object(solver_app, "CF_COOKIE_TIMEOUT_SECONDS", -1):
            with self.assertRaises(RuntimeError):
                self.run_async(self.solver.cloudflare_cookies(""))
        self.assertNotIn("proxy", self.solver._browser.kwargs)
        self.assertTrue(context.closed)

    def test_endpoint_checks_key_and_maps_failures_to_503(self):
        request = solver_app.CloudflareCookiesRequest(proxy="http://203.0.113.5:12323")
        with mock.patch.object(solver_app, "SOLVER_KEY", "k"):
            with self.assertRaises(HTTPException) as denied:
                self.run_async(solver_app.cloudflare_cookies(request, authorization="Bearer wrong"))
            self.assertEqual(denied.exception.status_code, 401)
            with mock.patch.object(solver_app.solver, "cloudflare_cookies", mock.AsyncMock(side_effect=RuntimeError("tunnel"))):
                with self.assertRaises(HTTPException) as failed:
                    self.run_async(solver_app.cloudflare_cookies(request, authorization="Bearer k"))
            self.assertEqual(failed.exception.status_code, 503)
            answer = {"cookies": {"__cf_bm": "bm"}, "expires_at": None, "user_agent": "UA"}
            with mock.patch.object(solver_app.solver, "cloudflare_cookies", mock.AsyncMock(return_value=answer)) as fetch:
                self.assertEqual(self.run_async(solver_app.cloudflare_cookies(request, authorization="Bearer k")), answer)
            fetch.assert_awaited_once_with("http://203.0.113.5:12323")


if __name__ == "__main__":
    unittest.main()
