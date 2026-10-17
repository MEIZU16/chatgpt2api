import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.account_service import AccountService, MainModelQuotaExhaustedError
from services.log_service import _image_error_response
from services.protocol import conversation
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    ImageToolUnavailableError,
    is_image_tool_unavailable_reply,
)
from services.storage.json_storage import JSONStorageBackend

# 主模型额度用完后上游的真实回复（英文 / 中英混合两种）
MINI_REPLY_EN = (
    "It looks like image creation is temporarily unavailable. Do you want to try something else? "
    "The Instant limit has been reached, and the Mini model is text-only right now."
)
MINI_REPLY_ZH = (
    "It looks like image creation is temporarily unavailable. Do you want to try something else? "
    "当前图像生成工具暂时不可用，因为 Instant 模式的使用限额已达到，而当前可用的 Mini 模型仅支持文本，无法生成或编辑图片。"
)


class FakeBackend:
    def __init__(self, access_token: str = "", **_kwargs) -> None:
        self.access_token = access_token
        self.progress_callback = None

    def close(self) -> None:
        pass


def reason_limit(remaining: int, reset_after: str = "2999-01-01T00:00:00Z") -> list[dict]:
    return [
        {"feature_name": "image_gen", "remaining": 5},
        {"feature_name": "reason", "remaining": remaining, "reset_after": reset_after},
    ]


class DetectionTests(unittest.TestCase):
    def test_detects_mini_fallback_replies(self) -> None:
        self.assertTrue(is_image_tool_unavailable_reply(MINI_REPLY_EN))
        self.assertTrue(is_image_tool_unavailable_reply(MINI_REPLY_ZH))
        self.assertTrue(is_image_tool_unavailable_reply("图像生成工具当前不可用，因为你已达到 Instant 使用上限，而 Mini 模型仅支持文本。"))

    def test_ignores_other_replies(self) -> None:
        self.assertFalse(is_image_tool_unavailable_reply(""))
        self.assertFalse(is_image_tool_unavailable_reply("I can't help create that image."))
        self.assertFalse(is_image_tool_unavailable_reply('{"size":"1024x1024","n":1}'))

    def test_stream_raises_without_polling(self) -> None:
        backend = mock.Mock()
        events = [{"type": "conversation.done", "conversation_id": "conv-1", "text": MINI_REPLY_EN}]
        with mock.patch.object(conversation, "conversation_events", lambda *args, **kwargs: iter(events)):
            with self.assertRaises(ImageToolUnavailableError) as ctx:
                list(conversation.stream_image_outputs(backend, ConversationRequest(model="gpt-image-2", prompt="cat")))
        self.assertEqual(ctx.exception.conversation_id, "conv-1")
        backend.resolve_conversation_image_urls.assert_not_called()


class RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(self._tmp_dir.name) / "accounts.json"))
        self.service.add_account_items(
            [
                {"access_token": "token-a", "email": "a@example.com", "status": "正常", "quota": 5,
                 "limits_progress": reason_limit(3)},
                {"access_token": "token-b", "email": "b@example.com", "status": "正常", "quota": 5,
                 "limits_progress": reason_limit(3)},
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

    def _ready(self) -> list[str]:
        with self.service._lock:
            return self.service._list_ready_candidate_tokens(require_main_model=True)

    def test_mini_fallback_retries_on_another_account(self) -> None:
        used_tokens: list[str] = []

        def fake_stream(backend, request, index, total):
            used_tokens.append(backend.access_token)
            yield ImageOutput(kind="progress", model=request.model, index=index, total=total, text="...")
            if len(used_tokens) == 1:
                raise ImageToolUnavailableError(MINI_REPLY_ZH, conversation_id="conv-1")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])

        request = ConversationRequest(model="gpt-image-2", prompt="edit", images=["data:image/png;base64,AAAA"],
                                      message_as_error=True)
        with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
            outputs = conversation._generate_single_image(request, 1, 1)

        self.assertEqual(len(used_tokens), 2)
        self.assertNotEqual(used_tokens[0], used_tokens[1])
        self.assertEqual([item.kind for item in outputs], ["progress", "result"])
        # 第一个号暂避生图，缓存的主模型额度记为 0；第二个号不受影响，两个号都没有占着的生图槽位
        self.assertEqual(self._ready(), [used_tokens[1]])
        first = self.service.get_account(used_tokens[0])
        self.assertEqual([i["remaining"] for i in first["limits_progress"] if i["feature_name"] == "reason"], [0])
        self.assertEqual(self.service._image_inflight, {})

    def test_all_accounts_downgraded_returns_429(self) -> None:
        used_tokens: list[str] = []

        def fake_stream(backend, request, index, total):
            used_tokens.append(backend.access_token)
            raise ImageToolUnavailableError(MINI_REPLY_EN)
            yield  # pragma: no cover

        with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
            with self.assertRaises(ImageGenerationError) as ctx:
                conversation._generate_single_image(ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1)

        exc = ctx.exception
        self.assertEqual(sorted(used_tokens), ["token-a", "token-b"])
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.code, "image_model_quota_exhausted")
        response = _image_error_response(exc)
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"image_model_quota_exhausted", response.body)
        self.assertEqual(self.service._image_inflight, {})

    def test_retry_count_is_bounded(self) -> None:
        calls: list[str] = []

        def fake_stream(backend, request, index, total):
            calls.append(backend.access_token)
            raise ImageToolUnavailableError(MINI_REPLY_EN)
            yield  # pragma: no cover

        # 暂避不生效时（例如刷新又把额度写回来），重试次数仍然有上限
        with mock.patch.object(self.service, "sideline_main_model_exhausted", return_value=60):
            with mock.patch.object(conversation, "stream_image_outputs", fake_stream):
                with self.assertRaises(ImageGenerationError) as ctx:
                    conversation._generate_single_image(ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1)

        self.assertEqual(ctx.exception.code, "image_model_quota_exhausted")
        self.assertEqual(len(calls), 6)


class SelectionTests(unittest.TestCase):
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

    def test_skips_accounts_with_no_main_model_quota(self) -> None:
        self.service.add_account_items(
            [
                {"access_token": "token-empty", "status": "正常", "quota": 5, "limits_progress": reason_limit(0)},
                {"access_token": "token-ok", "status": "正常", "quota": 5, "limits_progress": reason_limit(4)},
                # 付费号没有 reason 这一项，视为可用
                {"access_token": "token-paid", "status": "正常", "quota": 5,
                 "limits_progress": [{"feature_name": "image_gen", "remaining": 5}]},
            ]
        )
        picked = set()
        for _ in range(4):
            token = self.service.get_available_access_token(require_main_model=True)
            self.service.release_image_slot(token)
            picked.add(token)
        self.assertEqual(picked, {"token-ok", "token-paid"})

    def test_raises_main_model_error_when_only_main_model_is_missing(self) -> None:
        self.service.add_account_items(
            [{"access_token": "token-a", "status": "正常", "quota": 5, "limits_progress": reason_limit(0)}]
        )
        with self.assertRaises(MainModelQuotaExhaustedError):
            self.service.get_available_access_token(require_main_model=True)
        # 不要求主模型时（如 codex 生图）照常可选
        token = self.service.get_available_access_token()
        self.service.release_image_slot(token)
        self.assertEqual(token, "token-a")

    def test_main_model_error_maps_to_429(self) -> None:
        self.service.add_account_items(
            [{"access_token": "token-a", "status": "正常", "quota": 5, "limits_progress": reason_limit(0)}]
        )
        with mock.patch.object(conversation, "account_service", self.service):
            with self.assertRaises(ImageGenerationError) as ctx:
                conversation._generate_single_image(ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.code, "image_model_quota_exhausted")

    def test_passed_reset_makes_account_available_again(self) -> None:
        self.service.add_account_items(
            [{"access_token": "token-a", "status": "正常", "quota": 5,
              "limits_progress": reason_limit(0, "2000-01-01T00:00:00Z")}]
        )
        token = self.service.get_available_access_token(require_main_model=True)
        self.service.release_image_slot(token)
        self.assertEqual(token, "token-a")

    def test_sideline_lasts_until_reason_reset(self) -> None:
        self.service.add_account_items(
            [{"access_token": "token-a", "status": "正常", "quota": 5, "limits_progress": reason_limit(2)}]
        )
        self.assertEqual(self.service.sideline_main_model_exhausted("token-a"), AccountService._MAIN_MODEL_COOLDOWN_MAX_SECONDS)
        self.service.add_account_items(
            [{"access_token": "token-b", "status": "正常", "quota": 5, "limits_progress": [{"feature_name": "image_gen", "remaining": 5}]}]
        )
        self.assertEqual(self.service.sideline_main_model_exhausted("token-b"), AccountService._MAIN_MODEL_COOLDOWN_DEFAULT_SECONDS)


if __name__ == "__main__":
    unittest.main()
