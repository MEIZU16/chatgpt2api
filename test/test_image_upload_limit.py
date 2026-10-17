import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.account_service import AccountService, UploadQuotaExhaustedError
from services.log_service import _image_error_response
from services.protocol import conversation
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    is_upload_limit_error,
)
from services.storage.json_storage import JSONStorageBackend
from utils.helper import UpstreamHTTPError

UPLOAD_LIMIT_BODY = {
    "detail": {
        "code": "throttled",
        "reason": "upload_protection_limit_exceeded",
        "message": "你已达到文件上传上限。请23小时内重试。",
    }
}


def upload_limit_error() -> UpstreamHTTPError:
    return UpstreamHTTPError("/backend-api/files", 429, UPLOAD_LIMIT_BODY)


class FakeBackend:
    def __init__(self, access_token: str = "", **_kwargs) -> None:
        self.access_token = access_token
        self.progress_callback = None

    def close(self) -> None:
        pass


class UploadLimitDetectionTests(unittest.TestCase):
    def test_detects_upload_protection_429(self) -> None:
        self.assertTrue(is_upload_limit_error(upload_limit_error()))
        self.assertTrue(is_upload_limit_error(UpstreamHTTPError("/backend-api/files", 429, "too many")))
        self.assertTrue(is_upload_limit_error(UpstreamHTTPError("/backend-api/x", 429, UPLOAD_LIMIT_BODY)))

    def test_ignores_other_errors(self) -> None:
        self.assertFalse(is_upload_limit_error(UpstreamHTTPError("/backend-api/files", 500, UPLOAD_LIMIT_BODY)))
        self.assertFalse(is_upload_limit_error(UpstreamHTTPError("/backend-api/conversation", 429, "slow down")))
        self.assertFalse(is_upload_limit_error(RuntimeError("upload_protection_limit_exceeded")))


class UploadLimitRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.service = AccountService(JSONStorageBackend(Path(self._tmp_dir.name) / "accounts.json"))
        self.service.add_account_items(
            [
                {"access_token": "token-a", "email": "a@example.com", "status": "正常", "quota": 5},
                {"access_token": "token-b", "email": "b@example.com", "status": "正常", "quota": 5},
            ]
        )
        self.service.fetch_remote_info = lambda access_token, event="fetch_remote_info": self.service.get_account(
            access_token
        )
        patches = [
            mock.patch.object(conversation, "account_service", self.service),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "_remove_image_conversation_later", lambda *args, **kwargs: None),
            mock.patch("services.account_service.log_service"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp_dir.cleanup)

    def _edit_request(self) -> ConversationRequest:
        return ConversationRequest(model="gpt-image-2", prompt="edit it", images=["data:image/png;base64,AAAA"])

    def test_upload_limit_on_first_account_retries_on_another(self) -> None:
        used_tokens: list[str] = []

        def fake_stream(backend, request, index, total):
            used_tokens.append(backend.access_token)
            if len(used_tokens) == 1:
                raise upload_limit_error()
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])

        with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
            outputs = conversation._generate_single_image(self._edit_request(), 1, 1)

        self.assertEqual(len(used_tokens), 2)
        self.assertNotEqual(used_tokens[0], used_tokens[1])
        self.assertEqual([item.kind for item in outputs], ["result"])
        self.assertFalse(self.service.is_upload_available(used_tokens[0]))
        self.assertTrue(self.service.is_upload_available(used_tokens[1]))
        self.assertEqual(self.service._image_inflight, {})

    def test_all_accounts_upload_limited_returns_429(self) -> None:
        used_tokens: list[str] = []

        def fake_stream(backend, request, index, total):
            used_tokens.append(backend.access_token)
            raise upload_limit_error()
            yield  # pragma: no cover

        with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
            with self.assertRaises(ImageGenerationError) as ctx:
                conversation._generate_single_image(self._edit_request(), 1, 1)

        exc = ctx.exception
        self.assertEqual(sorted(used_tokens), ["token-a", "token-b"])
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.code, "upload_limit_exceeded")
        self.assertEqual(exc.error_type, "insufficient_quota")
        response = _image_error_response(exc)
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"upload_limit_exceeded", response.body)
        self.assertIn(b"upload quota", response.body)

    def test_retry_count_is_bounded(self) -> None:
        calls: list[str] = []

        def fake_stream(backend, request, index, total):
            calls.append(backend.access_token)
            raise upload_limit_error()
            yield  # pragma: no cover

        # 冷却不生效时（例如上游对新账号也返回 429），重试次数仍然有上限
        with mock.patch.object(self.service, "sideline_upload_account"):
            with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
                with self.assertRaises(ImageGenerationError) as ctx:
                    conversation._generate_single_image(self._edit_request(), 1, 1)

        self.assertEqual(ctx.exception.code, "upload_limit_exceeded")
        self.assertEqual(len(calls), 6)

    def test_generation_without_images_ignores_upload_cooldown(self) -> None:
        self.service.sideline_upload_account("token-a")
        self.service.sideline_upload_account("token-b")

        def fake_stream(backend, request, index, total):
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])

        with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
            outputs = conversation._generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="draw"), 1, 1
            )
        self.assertEqual([item.kind for item in outputs], ["result"])


class UploadAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(self._tmp_dir.name) / "accounts.json"))
        self.service.fetch_remote_info = lambda access_token, event="fetch_remote_info": self.service.get_account(
            access_token
        )
        patcher = mock.patch("services.account_service.log_service")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_edit_prefers_accounts_with_cached_upload_quota(self) -> None:
        self.service.add_account_items(
            [
                {
                    "access_token": "token-no-upload",
                    "status": "正常",
                    "quota": 5,
                    "limits_progress": [
                        {"feature_name": "image_gen", "remaining": 5},
                        {"feature_name": "file_upload", "remaining": 0, "reset_after": "2999-01-01T00:00:00Z"},
                    ],
                },
                {
                    "access_token": "token-upload",
                    "status": "正常",
                    "quota": 5,
                    "limits_progress": [{"feature_name": "file_upload", "remaining": 1}],
                },
            ]
        )
        for _ in range(3):
            token = self.service.get_available_access_token(require_upload=True)
            self.service.release_image_slot(token)
            self.assertEqual(token, "token-upload")

    def test_expired_upload_reset_makes_account_available_again(self) -> None:
        self.service.add_account_items(
            [
                {
                    "access_token": "token-reset",
                    "status": "正常",
                    "quota": 5,
                    "limits_progress": [
                        {"feature_name": "file_upload", "remaining": 0, "reset_after": "2000-01-01T00:00:00Z"},
                    ],
                },
            ]
        )
        self.assertTrue(self.service.is_upload_available("token-reset"))

    def test_raises_upload_quota_error_when_only_upload_is_missing(self) -> None:
        self.service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 5}])
        self.service.sideline_upload_account("token-a")
        with self.assertRaises(UploadQuotaExhaustedError):
            self.service.get_available_access_token(require_upload=True)
        token = self.service.get_available_access_token()
        self.service.release_image_slot(token)
        self.assertEqual(token, "token-a")

    def test_upload_cooldown_expires(self) -> None:
        self.service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 5}])
        self.service.sideline_upload_account("token-a", seconds=0)
        self.assertTrue(self.service.is_upload_available("token-a"))


if __name__ == "__main__":
    unittest.main()
