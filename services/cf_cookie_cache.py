"""按出口代理缓存由求解服务真实浏览器取回的 Cloudflare cookie。

2026-10-16 起，不少代理 IP 上后端直接请求 chatgpt.com 首页会被 Cloudflare 托管挑战（403，
cf-mitigated: challenge）。同一 IP 上真实浏览器不会被挑战，只是拿到 __cf_bm/_cfuvid 等 cookie；
把这些 cookie 带进后端请求即可稳定通过。cookie 绑定出口 IP，所以按代理分别缓存。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

from services.config import config
from services.sentinel_solver_client import fetch_cloudflare_cookies
from utils.log import logger

CF_COOKIE_NAMES = ("__cf_bm", "_cfuvid", "__cflb", "cf_clearance")
# __cf_bm 一般 30 分钟过期，提前一些丢弃
CF_COOKIE_MAX_TTL_SECS = 25 * 60
CF_COOKIE_MIN_TTL_SECS = 60
# 求解服务取 cookie 失败后，这段时间内同一代理不再重试，避免每个请求都去开浏览器
CF_COOKIE_FAILURE_BACKOFF_SECS = 60

CookieFetcher = Callable[[str], tuple[dict[str, str], float | None]]


@dataclass
class _Entry:
    cookies: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0
    failed_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class CloudflareCookieCache:
    def __init__(self, fetcher: CookieFetcher | None = None) -> None:
        self._fetcher = fetcher or fetch_cloudflare_cookies
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _entry(self, proxy_url: str) -> _Entry:
        with self._lock:
            return self._entries.setdefault(proxy_url, _Entry())

    def get_cached(self, proxy_url: str) -> dict[str, str]:
        """当前代理未过期的 cookie；没有时返回空 dict，不会触发求解。"""
        if not proxy_url:
            return {}
        entry = self._entry(proxy_url)
        with entry.lock:
            if entry.cookies and time.time() < entry.expires_at:
                return dict(entry.cookies)
        return {}

    def refresh(self, proxy_url: str, stale: dict[str, str] | None = None) -> dict[str, str]:
        """让求解服务为该代理重新取 cookie。

        stale 是调用方刚用过、仍被挑战的 cookie。若别的线程在此期间已经换了新的，直接复用，
        不再重复开浏览器。未配置求解服务、没有代理或处于失败冷却期时返回空 dict。
        """
        if not proxy_url or not config.sentinel_solver_url:
            return {}
        entry = self._entry(proxy_url)
        with entry.lock:
            now = time.time()
            if entry.cookies and now < entry.expires_at and entry.cookies != (stale or {}):
                return dict(entry.cookies)
            if now < entry.failed_until:
                return {}
            try:
                fetched, cookie_expires_at = self._fetcher(proxy_url)
            except Exception as exc:
                entry.failed_until = now + CF_COOKIE_FAILURE_BACKOFF_SECS
                logger.warning({"event": "cf_cookie_refresh_failed", "error": str(exc)[:300]})
                return {}
            cookies = {name: value for name, value in (fetched or {}).items() if name in CF_COOKIE_NAMES and value}
            if not cookies:
                entry.failed_until = now + CF_COOKIE_FAILURE_BACKOFF_SECS
                logger.warning({"event": "cf_cookie_refresh_empty"})
                return {}
            expires_at = now + CF_COOKIE_MAX_TTL_SECS
            if cookie_expires_at:
                expires_at = min(expires_at, float(cookie_expires_at) - CF_COOKIE_MIN_TTL_SECS)
            entry.cookies = cookies
            entry.expires_at = max(expires_at, now + CF_COOKIE_MIN_TTL_SECS)
            entry.failed_until = 0.0
            return dict(cookies)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


cf_cookie_cache = CloudflareCookieCache(lambda proxy_url: fetch_cloudflare_cookies(proxy_url))
