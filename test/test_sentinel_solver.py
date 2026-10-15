from __future__ import annotations

import os
import unittest
from http.cookiejar import Cookie, CookieJar
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import openai_backend_api as backend_module
from services import sentinel_solver_client as client_module
from services.config import config
from services.openai_backend_api import DEFAULT_IMPERSONATE, ChatRequirements, OpenAIBackendAPI
from utils import pow as pow_module


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self) -> object:
        return self._payload


def solver_config(url: str = "http://solver:8080", key: str = "solver-key"):
    return mock.patch.multiple(
        type(config),
        sentinel_solver_url=mock.PropertyMock(return_value=url),
        sentinel_solver_key=mock.PropertyMock(return_value=key),
    )


class SentinelSolverClientTests(unittest.TestCase):
    def setUp(self) -> None:
        client_module._user_agent_cache.update(url="", value={}, expires_at=0.0)

    def test_solve_turnstile_calls_solver_with_key(self) -> None:
        with solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=FakeResponse(200, {"token": "solved"})
        ) as post:
            self.assertEqual(client_module.solve_turnstile("dx-value", "p-value"), "solved")

        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://solver:8080/v1/turnstile")
        self.assertEqual(kwargs["json"], {"dx": "dx-value", "p": "p-value"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer solver-key")

    def test_solve_turnstile_sends_prepare_response_for_page_module(self) -> None:
        prepare = {"prepare_token": "t", "turnstile": {"required": True, "dx": "dx"}}
        with solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=FakeResponse(200, {"token": "solved"})
        ) as post:
            client_module.solve_turnstile("dx", "p", prepare)
        self.assertEqual(post.call_args.kwargs["json"]["requirements"], prepare)

    def test_solve_turnstile_fails_closed_when_solver_fails(self) -> None:
        with solver_config(), mock.patch.object(client_module.requests, "post", side_effect=RuntimeError("connection refused")):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")
        with solver_config(), mock.patch.object(client_module.requests, "post", return_value=FakeResponse(503, {"detail": "x"})):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")
        with solver_config(), mock.patch.object(client_module.requests, "post", return_value=FakeResponse(200, {"token": ""})):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")

    def test_solve_turnstile_without_solver_raises(self) -> None:
        with solver_config(url=""), mock.patch.object(client_module.requests, "post") as post:
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")
        post.assert_not_called()

    def test_solver_user_agent_is_cached(self) -> None:
        with solver_config(), mock.patch.object(
            client_module.requests, "get", return_value=FakeResponse(200, {"user_agent": "SolverUA/1.0"})
        ) as get:
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
        get.assert_called_once()

    def test_solver_user_agent_empty_without_solver(self) -> None:
        with solver_config(url=""), mock.patch.object(client_module.requests, "get") as get:
            self.assertEqual(client_module.solver_user_agent(), "")
        get.assert_not_called()

    def test_solver_environment_shares_health_cache(self) -> None:
        health = {"user_agent": "SolverUA/1.0", "environment": {"language": "zh-CN", "hardware_concurrency": 28}}
        with solver_config(), mock.patch.object(client_module.requests, "get", return_value=FakeResponse(200, health)) as get:
            self.assertEqual(client_module.solver_environment(), {"language": "zh-CN", "hardware_concurrency": 28})
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
        get.assert_called_once()


class PowTests(unittest.TestCase):
    ENVIRONMENT = {
        "screen_sum": 2080,
        "date_suffix": "GMT+0800 (中国标准时间)",
        "utc_offset_minutes": 480,
        "heap_limit": 4395630592,
        "language": "zh-CN",
        "languages": "en-US,en",
        "hardware_concurrency": 28,
    }

    def test_config_uses_solver_environment(self) -> None:
        config_fields = pow_module.build_pow_config("UA/1", ["https://chatgpt.com/a.js"], "build", self.ENVIRONMENT)
        self.assertEqual(len(config_fields), 25)
        self.assertEqual(config_fields[0], 2080)
        self.assertTrue(config_fields[1].endswith("GMT+0800 (中国标准时间)"))
        self.assertEqual(config_fields[2], 4395630592)
        self.assertEqual(config_fields[7:9], ["zh-CN", "en-US,en"])
        self.assertEqual(config_fields[16], 28)
        self.assertLess(config_fields[13], 60000)

    def test_proof_token_matches_web_fnv_check(self) -> None:
        # seed/difficulty 取自 2026-10-15 抓到的真实 prepare 响应
        token = pow_module.build_proof_token("0.6466531835161229", "06e4f1", "UA/1", environment=self.ENVIRONMENT)
        self.assertTrue(token.startswith("gAAAAAB"))
        self.assertTrue(token.endswith("~S"))
        answer = token[len("gAAAAAB"):-len("~S")]
        self.assertLessEqual(pow_module._fnv1a_32("0.6466531835161229" + answer)[:6], "06e4f1")


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}
        self.cookies = SimpleNamespace(jar=CookieJar())

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected url {url}")


class BackendSentinelTests(unittest.TestCase):
    def test_chat_requirements_finalize_uses_web_fields_and_solver_user_agent(self) -> None:
        api = OpenAIBackendAPI(access_token="token")
        api.session = FakeSession({
            "/prepare": FakeResponse(200, {
                "prepare_token": "prepare-token",
                "proofofwork": {"required": True, "seed": "seed", "difficulty": "0fffff"},
                "turnstile": {"required": True, "dx": "dx-value"},
            }),
            "/finalize": FakeResponse(200, {"token": "requirements-token"}),
        })
        with mock.patch.object(backend_module, "solver_user_agent", return_value="SolverUA/1.0"), mock.patch.object(
            backend_module, "build_legacy_requirements_token", return_value="p-value"
        ) as build_p, mock.patch.object(
            backend_module, "build_proof_token", return_value="proof-value"
        ) as build_proof, mock.patch.object(
            backend_module, "solve_turnstile", return_value="turnstile-value"
        ) as solve:
            requirements = api._get_chat_requirements()

        self.assertEqual(build_p.call_args.args[0], "SolverUA/1.0")
        self.assertEqual(build_proof.call_args.args[2], "SolverUA/1.0")
        solve.assert_called_once()
        self.assertEqual(solve.call_args.args[:2], ("dx-value", "p-value"))
        self.assertEqual(solve.call_args.args[2]["prepare_token"], "prepare-token")
        finalize_url, finalize_kwargs = api.session.calls[-1]
        self.assertTrue(finalize_url.endswith("/sentinel/chat-requirements/finalize"))
        self.assertEqual(finalize_kwargs["json"], {
            "prepare_token": "prepare-token",
            "proofofwork": "proof-value",
            "turnstile": "turnstile-value",
        })
        self.assertEqual(requirements.token, "requirements-token")
        self.assertEqual(requirements.turnstile_token, "turnstile-value")

    def test_bootstrap_omits_oai_headers_and_restores_them(self) -> None:
        api = OpenAIBackendAPI(access_token="token")
        seen: dict[str, str] = {}

        def fake_get(url, headers=None, timeout=None):
            seen.update(api.session.headers)
            seen.update(headers or {})
            return FakeResponse(200, "<html></html>")

        api.session.get = fake_get
        oai_before = {k: v for k, v in api.session.headers.items() if k.lower().startswith("oai-")}
        self.assertTrue(oai_before)
        with mock.patch.object(backend_module, "ensure_ok"):
            api._bootstrap()
        self.assertFalse([k for k in seen if k.lower().startswith("oai-")])
        self.assertEqual({k: v for k, v in api.session.headers.items() if k.lower().startswith("oai-")}, oai_before)

    def test_finalize_drops_only_the_restricted_oai_sc_cookie(self) -> None:
        def cookie(name: str) -> Cookie:
            return Cookie(0, name, "v", None, False, ".chatgpt.com", True, True, "/", True, True, None, False, None, None, {})

        api = OpenAIBackendAPI(access_token="token")
        api.session = FakeSession({
            "/prepare": FakeResponse(200, {"prepare_token": "p", "proofofwork": {"required": False}}),
            "/finalize": FakeResponse(200, {"token": "requirements-token"}),
        })
        for name in ("oai-sc", "__cf_bm", "oai-did"):
            api.session.cookies.jar.set_cookie(cookie(name))
        with mock.patch.object(backend_module, "build_legacy_requirements_token", return_value="p-value"):
            api._get_chat_requirements()
        self.assertEqual(sorted(c.name for c in api.session.cookies.jar), ["__cf_bm", "oai-did"])

    def test_sentinel_user_agent_falls_back_to_request_user_agent(self) -> None:
        api = OpenAIBackendAPI(access_token="token")
        with mock.patch.object(backend_module, "solver_user_agent", return_value=""):
            self.assertEqual(api._sentinel_user_agent(), api.user_agent)

    def test_image_headers_include_turnstile_token(self) -> None:
        api = OpenAIBackendAPI(access_token="token")
        headers = api._image_headers(
            "/backend-api/f/conversation",
            ChatRequirements(token="requirements", proof_token="proof", turnstile_token="turnstile"),
            "conduit",
            "text/event-stream",
        )
        self.assertEqual(headers["OpenAI-Sentinel-Turnstile-Token"], "turnstile")
        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof")
        self.assertEqual(headers["X-Conduit-Token"], "conduit")

    def test_legacy_impersonation_is_upgraded(self) -> None:
        with mock.patch.object(backend_module.account_service, "get_account", return_value={"impersonate": "chrome110"}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], DEFAULT_IMPERSONATE)
        with mock.patch.object(backend_module.account_service, "get_account", return_value={"impersonate": "safari17_0"}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], "safari17_0")
        with mock.patch.object(backend_module.account_service, "get_account", return_value={}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], DEFAULT_IMPERSONATE)


class SentinelSolverConfigTests(unittest.TestCase):
    def test_env_overrides_config_and_key_is_hidden(self) -> None:
        env = {"CHATGPT2API_SENTINEL_SOLVER_URL": "http://env-solver:8080/", "CHATGPT2API_SENTINEL_SOLVER_KEY": "env-key"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(
            config.data, {"sentinel_solver_url": "http://file-solver", "sentinel_solver_key": "file-key"}
        ):
            self.assertEqual(config.sentinel_solver_url, "http://env-solver:8080")
            self.assertEqual(config.sentinel_solver_key, "env-key")
            public = config.get()
        self.assertEqual(public["sentinel_solver_url"], "http://env-solver:8080")
        self.assertNotIn("sentinel_solver_key", public)


if __name__ == "__main__":
    unittest.main()
