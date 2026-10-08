from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import log_service as log_module
from services.log_service import LogService


def write_lines(path: Path, items: list[object]) -> None:
    lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def log_item(index: int, type: str = "call", day: str = "2026-10-01") -> dict[str, object]:
    return {"id": f"id{index:04d}", "time": f"{day} 12:00:00", "type": type, "summary": f"调用完成 {index}", "detail": {"n": index}}


class LogServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs.jsonl"
        # 用很小的块，覆盖跨块拼接行的逻辑
        patcher = mock.patch.object(log_module, "READ_CHUNK_SIZE", 37)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_list_returns_newest_first_with_limit_and_filters(self) -> None:
        items = [log_item(i, type="call" if i % 2 else "account", day="2026-10-01" if i < 10 else "2026-10-02") for i in range(20)]
        write_lines(self.path, items)
        service = LogService(self.path, max_bytes=0)

        self.assertEqual([item["id"] for item in service.list(limit=3)], ["id0019", "id0018", "id0017"])
        self.assertEqual([item["id"] for item in service.list(type="account", limit=2)], ["id0018", "id0016"])
        self.assertEqual(
            [item["id"] for item in service.list(end_date="2026-10-01", limit=200)],
            [f"id{i:04d}" for i in range(9, -1, -1)],
        )
        self.assertEqual(len(service.list(limit=200)), 20)

    def test_list_skips_blank_and_invalid_lines_and_reads_file_without_trailing_newline(self) -> None:
        self.path.write_text(
            json.dumps(log_item(1)) + "\n\nnot-json\n" + json.dumps(log_item(2)),
            encoding="utf-8",
        )
        service = LogService(self.path, max_bytes=0)

        self.assertEqual([item["id"] for item in service.list()], ["id0002", "id0001"])

    def test_delete_removes_ids_and_keeps_other_lines_verbatim(self) -> None:
        write_lines(self.path, [log_item(1), "not-json", log_item(2), log_item(3)])
        before = self.path.read_bytes().splitlines()
        service = LogService(self.path, max_bytes=0)

        self.assertEqual(service.delete(["id0002", "missing"]), {"removed": 1})

        self.assertEqual(self.path.read_bytes().splitlines(), [before[0], before[1], before[3]])
        self.assertFalse(self.path.with_name("logs.jsonl.tmp").exists())

    def test_delete_without_matches_leaves_file_untouched(self) -> None:
        write_lines(self.path, [log_item(1), log_item(2)])
        before = self.path.read_bytes()
        service = LogService(self.path, max_bytes=0)

        self.assertEqual(service.delete(["missing"]), {"removed": 0})
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_lines_without_id_can_be_listed_and_deleted(self) -> None:
        legacy = [{"time": f"2026-10-01 12:00:0{i}", "type": "call", "summary": f"旧日志 {i}", "detail": {}} for i in range(3)]
        write_lines(self.path, legacy)
        service = LogService(self.path, max_bytes=0)
        listed = service.list()
        ids = [item["id"] for item in listed]
        self.assertEqual(len(set(ids)), 3)

        self.assertEqual(service.delete([ids[1]]), {"removed": 1})

        # 剩余旧日志的 id 在删除时被固定写回，之后再查询保持不变
        remaining = service.list()
        self.assertEqual([item["id"] for item in remaining], [ids[0], ids[2]])
        self.assertEqual([item["summary"] for item in remaining], ["旧日志 2", "旧日志 0"])
        self.assertEqual(service.delete([ids[0]]), {"removed": 1})
        self.assertEqual([item["id"] for item in service.list()], [ids[2]])

    def test_add_trims_oldest_lines_when_file_exceeds_limit(self) -> None:
        service = LogService(self.path, max_bytes=2000)
        for index in range(60):
            service.add("call", f"调用 {index}", {"n": index})

        size = self.path.stat().st_size
        self.assertLessEqual(size, 2000)
        raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in raw_lines]
        self.assertEqual(parsed[-1]["summary"], "调用 59")
        summaries = [item["summary"] for item in parsed]
        self.assertEqual(summaries, [f"调用 {n}" for n in range(60 - len(summaries), 60)])

    def test_add_without_limit_never_trims(self) -> None:
        service = LogService(self.path, max_bytes=0)
        for index in range(30):
            service.add("call", f"调用 {index}")

        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 30)

    def test_max_bytes_reads_env_override(self) -> None:
        with mock.patch.dict("os.environ", {"CHATGPT2API_LOG_MAX_MB": "7"}):
            self.assertEqual(LogService(self.path).max_bytes, 7 * 1024 * 1024)
        with mock.patch.dict("os.environ", {"CHATGPT2API_LOG_MAX_MB": "bad"}):
            self.assertEqual(LogService(self.path).max_bytes, log_module.DEFAULT_LOG_MAX_MB * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
