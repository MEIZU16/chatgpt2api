from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

from services.account_service import AccountService
from services.config import config


class CountingStorage:
    def __init__(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = list(accounts)
        self.save_count = 0

    def load_accounts(self) -> list[dict[str, Any]]:
        return list(self.accounts)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = list(accounts)
        self.save_count += 1

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        pass

    def health_check(self) -> dict[str, Any]:
        return {"ok": True}

    def get_backend_info(self) -> dict[str, Any]:
        return {"type": "memory"}


class FakeBackend:
    results: dict[str, dict[str, Any]] = {}

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def get_user_info(self) -> dict[str, Any]:
        return dict(self.results[self.access_token])

    def close(self) -> None:
        pass


class AccountRefreshPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = CountingStorage([
            {"access_token": "tok-a", "status": "正常", "quota": 5},
            {"access_token": "tok-b", "status": "正常", "quota": 5},
            {"access_token": "tok-c", "status": "正常", "quota": 5},
        ])
        self.service = AccountService(self.storage)
        FakeBackend.results = {
            "tok-a": {"status": "正常", "quota": 3},
            "tok-b": {"status": "限流", "quota": 0},
            "tok-c": {"status": "正常", "quota": 4},
        }
        backend_patch = mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend)
        backend_patch.start()
        self.addCleanup(backend_patch.stop)
        log_patch = mock.patch("services.account_service.log_service")
        self.log_service = log_patch.start()
        self.addCleanup(log_patch.stop)
        for name in ("auto_relogin_after_refresh", "auto_remove_rate_limited_accounts"):
            config_patch = mock.patch.object(type(config), name, new_callable=mock.PropertyMock, return_value=False)
            config_patch.start()
            self.addCleanup(config_patch.stop)

    def logged_summaries(self) -> list[str]:
        return [call.args[1] for call in self.log_service.add.call_args_list]

    def test_refresh_accounts_saves_once_and_logs_only_status_changes(self) -> None:
        result = self.service.refresh_accounts(["tok-a", "tok-b", "tok-c"])

        self.assertEqual(result["refreshed"], 3)
        self.assertEqual(self.storage.save_count, 1)
        saved = {item["access_token"]: item for item in self.storage.accounts}
        self.assertEqual(saved["tok-a"]["quota"], 3)
        self.assertEqual(saved["tok-b"]["status"], "限流")
        self.assertEqual(self.logged_summaries(), ["更新账号"])
        self.assertEqual(self.log_service.add.call_args.args[2]["status"], "限流")

    def test_fetch_remote_info_outside_batch_still_saves_immediately(self) -> None:
        self.service.fetch_remote_info("tok-a")

        self.assertEqual(self.storage.save_count, 1)
        self.assertEqual(self.logged_summaries(), [])

    def test_manual_update_account_keeps_logging_every_update(self) -> None:
        self.service.update_account("tok-a", {"quota": 1})

        self.assertEqual(self.storage.save_count, 1)
        self.assertEqual(self.logged_summaries(), ["更新账号"])

    def test_stale_refresh_progress_is_pruned(self) -> None:
        self.service.init_refresh_progress("old", 1)
        self.service.finish_refresh_progress("old", {"items": ["snapshot"]})
        AccountService._refresh_progress["old"]["updated_at"] = time.time() - AccountService._PROGRESS_TTL_SECONDS - 1

        self.service.init_refresh_progress("new", 1)

        self.assertIsNone(self.service.get_refresh_progress("old"))
        self.assertIsNotNone(self.service.get_refresh_progress("new"))
        self.service.clean_refresh_progress("new")

    def test_stale_relogin_progress_is_pruned(self) -> None:
        self.service.init_relogin_progress("old", 1)
        AccountService._relogin_progress["old"]["updated_at"] = time.time() - AccountService._PROGRESS_TTL_SECONDS - 1

        self.service.init_relogin_progress("new", 1)

        self.assertIsNone(self.service.get_relogin_progress("old"))
        self.assertIsNotNone(self.service.get_relogin_progress("new"))
        self.service.clean_relogin_progress("new")


if __name__ == "__main__":
    unittest.main()
