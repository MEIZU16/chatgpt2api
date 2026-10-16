import unittest
from http.cookiejar import Cookie
from unittest import mock

from curl_cffi.requests import Headers

import services.openai_backend_api as backend_module
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import UpstreamHTTPError

ACCOUNT_PROXY = "http://acct:secret@198.51.100.7:12323"
GLOBAL_PROXY = "http://glob:hunter2@203.0.113.9:8080"
OTHER_ACCOUNT_PROXY = "http://other:pass@192.0.2.44:12323"


def cookie(name: str) -> Cookie:
    return Cookie(0, name, "v", None, False, ".chatgpt.com", True, True, "/", True, True, None, False, None, None, {})


class BootstrapCloudflareRetryTests(unittest.TestCase):
    def setUp(self):
        # 这里只测等待重试和换代理；不让挑战去找求解服务刷新 Cloudflare cookie（见 test_cf_cookies.py）
        mock.patch.object(
            type(backend_module.config), "sentinel_solver_url", new_callable=mock.PropertyMock, return_value=""
        ).start()
        backend_module._healthy_bootstrap_proxies.clear()
        self.addCleanup(backend_module._healthy_bootstrap_proxies.clear)
        patcher = mock.patch.object(backend_module.config, "get_proxy_settings", return_value="")
        self.global_proxy = patcher.start()
        self.addCleanup(patcher.stop)
        self.sleep = mock.patch("services.openai_backend_api.time.sleep").start()
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

    def spy_build_session(self, api, homepage_response):
        """Let failover build a real session, but answer its homepage GET with a canned response."""
        real_build_session = api._build_session
        built = []

        def build_session(proxy=""):
            session = real_build_session(proxy)
            session.get = mock.Mock(return_value=homepage_response)
            built.append(session)
            return session

        return mock.patch.object(api, "_build_session", side_effect=build_session), built

    def test_challenge_then_ok_succeeds_on_account_proxy(self):
        api = self.make_api()
        original_session = api.session
        with mock.patch.object(api.session, "get", side_effect=[self.challenge(), self.response(200)]) as get, \
                mock.patch.object(api, "_build_session") as build_session:
            api._bootstrap()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.5)])
        build_session.assert_not_called()
        self.assertIs(api.session, original_session)
        self.assertEqual(api.proxy_url, ACCOUNT_PROXY)
        self.assertEqual(api.pow_script_sources, [backend_module.DEFAULT_POW_SCRIPT])
        self.assertIn(ACCOUNT_PROXY, backend_module._healthy_bootstrap_proxies)
        for call in get.call_args_list:
            self.assertFalse([k for k in call.kwargs["headers"] if k.lower().startswith("oai-")])

    def test_persistent_challenge_fails_over_to_global_proxy(self):
        self.global_proxy.return_value = GLOBAL_PROXY
        api = self.make_api()
        original_session = api.session
        for name in ("__cf_bm", "cf_clearance", "_cfuvid", "oai-did"):
            original_session.cookies.jar.set_cookie(cookie(name))
        oai_before = {k: v for k, v in api.session.headers.items() if k.lower().startswith("oai-")}
        build_patch, built = self.spy_build_session(api, self.response(200))
        with mock.patch.object(api.session, "get", return_value=self.challenge()) as get, build_patch as build_session, \
                mock.patch.object(backend_module.logger, "warning") as warning:
            api._bootstrap()

        self.assertEqual(get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.5), mock.call(3.0)])
        build_session.assert_called_once_with(GLOBAL_PROXY)
        self.assertIs(api.session, built[0])
        self.assertEqual(api.session.proxies, {"all": GLOBAL_PROXY})
        self.assertEqual(api.proxy_url, GLOBAL_PROXY)
        # 账号自己存的代理不变
        self.assertEqual(api.account["proxy"], ACCOUNT_PROXY)
        self.assertEqual(api.session.headers["Authorization"], "Bearer token")
        self.assertEqual({k: v for k, v in api.session.headers.items() if k.lower().startswith("oai-")}, oai_before)
        self.assertEqual(sorted(c.name for c in api.session.cookies.jar), ["oai-did"])
        sent_headers = api.session.get.call_args.kwargs["headers"]
        self.assertFalse([k for k in sent_headers if k.lower().startswith("oai-")])
        warning.assert_called_once_with({
            "event": "bootstrap_proxy_failover",
            "account_proxy": "198.51.100.7:12323",
            "failover_proxy": "203.0.113.9:8080",
        })
        self.assertNotIn(ACCOUNT_PROXY, backend_module._healthy_bootstrap_proxies)
        self.assertIn(GLOBAL_PROXY, backend_module._healthy_bootstrap_proxies)

    def test_failover_prefers_proxy_that_recently_succeeded(self):
        healthy = self.make_api(OTHER_ACCOUNT_PROXY)
        with mock.patch.object(healthy.session, "get", return_value=self.response(200)):
            healthy._bootstrap()

        self.global_proxy.return_value = GLOBAL_PROXY
        api = self.make_api()
        build_patch, built = self.spy_build_session(api, self.response(200))
        with mock.patch.object(api.session, "get", return_value=self.challenge()), build_patch as build_session:
            api._bootstrap()
        build_session.assert_called_once_with(OTHER_ACCOUNT_PROXY)
        self.assertEqual(api.proxy_url, OTHER_ACCOUNT_PROXY)

    def test_failover_proxy_also_challenged_keeps_account_session_and_raises(self):
        self.global_proxy.return_value = GLOBAL_PROXY
        api = self.make_api()
        original_session = api.session
        build_patch, built = self.spy_build_session(api, self.challenge())
        with mock.patch.object(api.session, "get", return_value=self.challenge()), build_patch as build_session:
            with self.assertRaises(UpstreamHTTPError) as ctx:
                api._bootstrap()
        self.assertEqual(ctx.exception.status_code, 403)
        build_session.assert_called_once_with(GLOBAL_PROXY)
        self.assertIs(api.session, original_session)
        self.assertEqual(api.proxy_url, ACCOUNT_PROXY)

    def test_persistent_challenge_without_failover_candidates_raises(self):
        api = self.make_api()
        with mock.patch.object(api.session, "get", return_value=self.challenge()) as get, \
                mock.patch.object(api, "_build_session") as build_session:
            with self.assertRaises(UpstreamHTTPError):
                api._bootstrap()
        self.assertEqual(get.call_count, 3)
        build_session.assert_not_called()

    def test_plain_403_fails_without_retry_or_failover(self):
        self.global_proxy.return_value = GLOBAL_PROXY
        api = self.make_api()
        with mock.patch.object(api.session, "get", return_value=self.response(403)) as get, \
                mock.patch.object(api, "_build_session") as build_session:
            with self.assertRaises(UpstreamHTTPError) as ctx:
                api._bootstrap()
        self.assertEqual(ctx.exception.status_code, 403)
        get.assert_called_once()
        self.sleep.assert_not_called()
        build_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
