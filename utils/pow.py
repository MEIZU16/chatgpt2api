import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Sequence

import pybase64

DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
from utils.helper import new_uuid


CORES = [8, 16, 24, 32]
DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]
SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str) -> tuple[list[str], str]:
    parser = ScriptSrcParser()
    parser.feed(html_content)
    script_sources = parser.script_sources or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


# 2026-10 网页版 requirements/proof token 的固定字段（Linux Chrome 153 实测）
DEFAULT_HEAP_LIMIT = 4395630592
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_LANGUAGES = "en-US,en"
POW_ANSWER_SUFFIX = "~S"


def _browser_date(environment: dict[str, Any]) -> str:
    """复刻浏览器 `new Date().toString()`；有求解页面环境时用它的时区，否则用本机时区。"""
    suffix = str(environment.get("date_suffix") or "")
    offset = environment.get("utc_offset_minutes")
    if suffix and isinstance(offset, (int, float)):
        now = datetime.now(timezone(timedelta(minutes=int(offset))))
    else:
        now = datetime.now().astimezone()
        suffix = f"GMT{now.strftime('%z')} ({now.tzname()})"
    return now.strftime("%a %b %d %Y %H:%M:%S") + " " + suffix


def build_pow_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    environment: dict[str, Any] | None = None,
) -> list[Any]:
    """按网页 sentinel SDK 的 25 字段格式生成浏览器指纹。

    时区、语言、核数等优先取求解页面的真实值（见 sentinel_solver /health 的 environment），
    与算 Turnstile 的页面保持一致；旧版写死美东时区、en-US 等值的组合会被判为低质量请求。
    """
    environment = environment or {}
    navigator_key = random.choice([
        "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
        "storage−[object StorageManager]",
        "locks−[object LockManager]",
        "appCodeName−Mozilla",
        "permissions−[object Permissions]",
        "share−function share() { [native code] }",
        "webdriver−false",
        "managed−[object NavigatorManagedData]",
        "canShare−function canShare() { [native code] }",
        "vendor−Google Inc.",
        "mediaDevices−[object MediaDevices]",
        "vibrate−function vibrate() { [native code] }",
        "storageBuckets−[object StorageBucketManager]",
        "mediaCapabilities−[object MediaCapabilities]",
        "cookieEnabled−true",
        "virtualKeyboard−[object VirtualKeyboard]",
        "product−Gecko",
        "presentation−[object Presentation]",
        "onLine−true",
        "mimeTypes−[object MimeTypeArray]",
        "credentials−[object CredentialsContainer]",
        "serviceWorker−[object ServiceWorkerContainer]",
        "keyboard−[object Keyboard]",
        "gpu−[object GPU]",
        "doNotTrack",
        "serial−[object Serial]",
        "pdfViewerEnabled−true",
        "language−zh-CN",
        "geolocation−[object Geolocation]",
        "userAgentData−[object NavigatorUAData]",
        "getUserMedia−function getUserMedia() { [native code] }",
        "sendBeacon−function sendBeacon() { [native code] }",
        "hardwareConcurrency−32",
        "windowControlsOverlay−[object WindowControlsOverlay]",
    ])
    window_key = random.choice([
        "0",
        "window",
        "self",
        "document",
        "name",
        "location",
        "customElements",
        "history",
        "navigation",
        "innerWidth",
        "innerHeight",
        "scrollX",
        "scrollY",
        "visualViewport",
        "screenX",
        "screenY",
        "outerWidth",
        "outerHeight",
        "devicePixelRatio",
        "screen",
        "chrome",
        "navigator",
        "onresize",
        "performance",
        "crypto",
        "indexedDB",
        "sessionStorage",
        "localStorage",
        "scheduler",
        "alert",
        "atob",
        "btoa",
        "fetch",
        "matchMedia",
        "postMessage",
        "queueMicrotask",
        "requestAnimationFrame",
        "setInterval",
        "setTimeout",
        "caches",
        "__NEXT_DATA__",
        "__BUILD_MANIFEST",
        "__NEXT_PRELOADREADY",
    ])
    script_source = random.choice(list(script_sources)) if script_sources else DEFAULT_POW_SCRIPT
    # 页面打开以来的毫秒数；旧版用 time.perf_counter()，在 Linux 上是开机时长，明显不像真实页面
    page_age_ms = random.uniform(2000, 30000)
    return [
        int(environment.get("screen_sum") or sum(random.choice(SCREEN_RESOLUTIONS))),
        _browser_date(environment),
        int(environment.get("heap_limit") or DEFAULT_HEAP_LIMIT),
        1,
        user_agent,
        script_source,
        data_build,
        str(environment.get("language") or DEFAULT_LANGUAGE),
        str(environment.get("languages") or DEFAULT_LANGUAGES),
        random.random(),
        navigator_key,
        random.choice(DOCUMENT_KEYS),
        window_key,
        page_age_ms,
        new_uuid(),
        "",
        int(environment.get("hardware_concurrency") or random.choice(CORES)),
        time.time() * 1000 - page_age_ms,
        0, 0, 0, 0, 0, 0,
        0,  # 0 = edge/chrome, 1 = firefox
    ]


def _fnv1a_32(text: str) -> str:
    """网页 sentinel SDK 的 PoW 哈希：FNV-1a 32 位，再做一轮 avalanche 混合。"""
    value = 2166136261
    for char in text:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 2246822507) & 0xFFFFFFFF
    value ^= value >> 13
    value = (value * 3266489909) & 0xFFFFFFFF
    value ^= value >> 16
    return format(value, "08x")


def _encode_config(config: list[Any]) -> str:
    return pybase64.b64encode(json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()).decode()


def _pow_generate(seed: str, difficulty: str, config: list[Any], limit: int = 500000) -> tuple[str, bool]:
    """找到使 fnv(seed + base64(config)) 的前缀不大于 difficulty 的 config；config[3] 为计数，config[9] 为耗时毫秒。"""
    started = time.perf_counter()
    for i in range(limit):
        config[3] = i
        config[9] = round((time.perf_counter() - started) * 1000)
        encoded = _encode_config(config)
        if _fnv1a_32(seed + encoded)[:len(difficulty)] <= difficulty:
            return encoded + POW_ANSWER_SUFFIX, True
    return "", False


def build_legacy_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    environment: dict[str, Any] | None = None,
) -> str:
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build, environment=environment)
    return "gAAAAAC" + _encode_config(config)


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    environment: dict[str, Any] | None = None,
) -> str:
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build, environment=environment)
    answer, solved = _pow_generate(seed, difficulty, config)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    return "gAAAAAB" + answer
