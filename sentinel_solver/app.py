"""sentinel 求解服务：常驻一个无头 Chrome 的 chatgpt.com 页面，替主服务计算 Turnstile / SO token。

2026-10-14 起 chatgpt.com 的对话接口要求 Turnstile token 必须在真实的 chatgpt.com 页面里算出，
纯 Python/Node 模拟的结果会被拒（403）。主服务不打包浏览器，改为通过 HTTP 调用本服务。

环境变量：
- SENTINEL_SOLVER_KEY：可选，设置后请求需带 `Authorization: Bearer <key>`
- SENTINEL_SOLVER_PROXY：可选，浏览器出网代理（如 http://user:pass@host:7890），应与主服务出口一致
- SENTINEL_SOLVER_USER_AGENT：可选，默认按内置 Chromium 版本生成 Linux Chrome UA
- SENTINEL_SOLVER_TIMEZONE：可选，页面时区（如 Asia/Tokyo），应与出口 IP 所在地一致；默认跟随系统
- SENTINEL_SOLVER_REFRESH_SECONDS：页面定期重开的间隔，默认 1800
- SENTINEL_SOLVER_HEADLESS：默认 true；调试时可设为 false
- PORT：监听端口，默认 8080
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright_stealth import Stealth
from pydantic import BaseModel

from turnstile_result import is_retryable_solve_error, validate_turnstile_token

CHATGPT_URL = "https://chatgpt.com/"
CHALLENGE_TITLES = ("请稍候", "Just a moment", "Attention Required")
VM_SCRIPT = (Path(__file__).parent / "vm.js").read_text(encoding="utf-8")
BASE_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""
# 优先调用 chatgpt.com 自带的 turnstile 模块在本页面里算 token：手工移植的 VM 要跟着前端改动同步，
# 页面模块则始终是最新版。思路来自 basketikun/chatgpt2api#418（pansx），页面模块做法见 239573049/WebImage（MIT）。
# 模块文件名带内容哈希、每次前端发布都会变，所以求解时现场查找，固定路径只作最后兜底。
PINNED_TURNSTILE_ASSET = "/unauth-mweb/assets/turnstile-DykPLDEM.js"
DISCOVER_TURNSTILE_ASSET = """() => {
  const pattern = /\\/turnstile-[^/"']+\\.js/;
  const seen = [];
  const push = (url) => { if (url && pattern.test(url) && !seen.includes(url)) seen.push(url); };
  try { performance.getEntriesByType('resource').forEach((e) => push(e.name)); } catch (err) {}
  try { Array.from(document.scripts).forEach((s) => push(s.src)); } catch (err) {}
  return seen;
}"""
SCAN_TURNSTILE_ASSET = """async (pattern) => {
  const re = new RegExp(pattern);
  for (const script of Array.from(document.scripts)) {
    if (!script.src) continue;
    try {
      const hit = (await (await fetch(script.src)).text()).match(re);
      if (hit) return hit[0];
    } catch (err) {}
  }
  return null;
}"""
SOLVE_VIA_PAGE_MODULE = """async ([requirements, p, asset]) => {
  try {
    const mod = await import(asset);
    const turnstile = mod.turnstile || (mod.default && mod.default.turnstile);
    if (!turnstile || typeof turnstile.getEnforcementToken !== 'function') return 'ERR:getEnforcementToken missing';
    // 模块用 WeakMap 以 requirements 为键取 p；token 缓存也是 WeakMap，不能动，否则会直接回显 p 而不跑挑战。
    // 这个钩子挂在全局原型上：两次求解交错时，先结束的那次会把钩子摘掉，后一次就拿错 p 去解 dx，
    // 报 "Unexpected token '<' ... is not valid JSON"。所以 Python 侧用 _solve_lock 让页面模块求解逐个进行。
    const cache = turnstile.tokenCache;
    const original = WeakMap.prototype.get;
    WeakMap.prototype.get = function (target) {
      if (this === cache) return original.call(this, target);
      return target === requirements ? p : original.call(this, target);
    };
    let token;
    try {
      token = await turnstile.getEnforcementToken(requirements);
    } finally {
      WeakMap.prototype.get = original;
    }
    return String(token === undefined || token === null ? '' : token);
  } catch (error) {
    return 'ERR:' + String(error).slice(0, 300);
  }
}"""
SOLVE_VIA_VM = "([dx, p]) => window.__sentinelVM.turnstile(dx, p)"
# page.evaluate 没有单次超时，首次加载模块可能较慢，放宽上下文默认超时
PAGE_MODULE_TIMEOUT_MS = 120000
PAGE_MODULE_ATTEMPTS = 3

# 主服务据此生成 requirements/proof token 里的浏览器字段，使其与算 Turnstile 的页面一致
ENVIRONMENT_SCRIPT = """() => ({
    screen_sum: screen.width + screen.height,
    date_suffix: new Date().toString().replace(/^.*? (GMT[+-]\\d{4})/, '$1'),
    utc_offset_minutes: -new Date().getTimezoneOffset(),
    heap_limit: (performance.memory && performance.memory.jsHeapSizeLimit) || null,
    language: navigator.language,
    languages: (navigator.languages || []).join(','),
    hardware_concurrency: navigator.hardwareConcurrency,
})"""

SOLVER_KEY = os.getenv("SENTINEL_SOLVER_KEY", "").strip()
PROXY = os.getenv("SENTINEL_SOLVER_PROXY", "").strip()
USER_AGENT = os.getenv("SENTINEL_SOLVER_USER_AGENT", "").strip()
TIMEZONE = os.getenv("SENTINEL_SOLVER_TIMEZONE", "").strip()
REFRESH_SECONDS = max(60, int(os.getenv("SENTINEL_SOLVER_REFRESH_SECONDS", "1800")))
HEADLESS = os.getenv("SENTINEL_SOLVER_HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
READY_TIMEOUT_SECONDS = 60
CF_COOKIE_NAMES = ("__cf_bm", "_cfuvid", "__cflb", "cf_clearance")
CF_COOKIE_TIMEOUT_SECONDS = 30
# 同时最多开几个取 cookie 的页面，避免一波挑战把容器内存打满
CF_COOKIE_CONCURRENCY = 2


def playwright_proxy(url: str) -> dict[str, str]:
    """Chromium 不认 URL 里的账号密码，需拆成 Playwright 的 username/password 字段。"""
    parts = urlsplit(url)
    if not parts.username:
        return {"server": url}
    server = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return {"server": server, "username": unquote(parts.username), "password": unquote(parts.password or "")}


class SolverPage:
    """维护一个已通过 Cloudflare 校验的 chatgpt.com 页面，按需重开。"""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._opened_at = 0.0
        self._stale = False
        self._lock = asyncio.Lock()
        # 页面模块求解改写的是全局 WeakMap 原型，不能并发；单次求解远不到 1 秒，排队的代价很小
        self._solve_lock = asyncio.Lock()
        # 每次重开页面加一；失败的请求只能作废它用过的那一代页面，一波并发失败只会重开一次
        self._generation = 0
        # 每代页面上还在执行的 evaluate 数；被替换下来的上下文等它们跑完再关，免得别的请求看到页面被关
        self._leases: dict[int, int] = {}
        self._retired: dict[int, BrowserContext] = {}
        self.user_agent = USER_AGENT
        self.environment: dict[str, object] = {}
        self._turnstile_asset = ""
        self._cf_cookie_slots = asyncio.Semaphore(CF_COOKIE_CONCURRENCY)
        self.last_error = ""

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": HEADLESS,
            # channel="chromium" 使用完整 Chromium 的新版无头模式；默认的 headless shell 过不了 Cloudflare
            "channel": "chromium",
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        }
        if PROXY:
            launch_kwargs["proxy"] = playwright_proxy(PROXY)
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        if not self.user_agent:
            major = self._browser.version.split(".")[0]
            self.user_agent = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
            )
        try:
            await self.page()
        except Exception as exc:  # 启动时页面暂时打不开不致命，首个请求会重试
            self.last_error = str(exc)
            print(f"[sentinel-solver] initial page open failed: {exc}", flush=True)

    async def stop(self) -> None:
        for context in list(self._retired.values()):
            await self._close_quietly(context)
        self._retired.clear()
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def page(self) -> Page:
        async with self._lock:
            return await self._fresh_page()

    async def _fresh_page(self) -> Page:
        """调用方须持有 self._lock。"""
        stale = self._stale or self._page is None or time.monotonic() - self._opened_at > REFRESH_SECONDS
        if not stale and not await self._is_ready(self._page):
            stale = True
        if stale:
            await self._reopen()
        return self._page

    @asynccontextmanager
    async def _lease(self):
        """借用当前页面执行一段操作；借用期间这代页面即使被替换也不会关闭。产出 (page, generation)。"""
        async with self._lock:
            page = await self._fresh_page()
            generation = self._generation
            self._leases[generation] = self._leases.get(generation, 0) + 1
        try:
            yield page, generation
        finally:
            self._leases[generation] -= 1
            if not self._leases[generation]:
                del self._leases[generation]
                retired = self._retired.pop(generation, None)
                if retired is not None:
                    await self._close_quietly(retired)

    async def invalidate(self, generation: int | None = None) -> None:
        """作废页面，下次使用时重开；给了 generation 时只在它仍是当前这代页面时才作废。"""
        async with self._lock:
            if generation is None or generation == self._generation:
                self._stale = True

    @staticmethod
    async def _close_quietly(context: BrowserContext) -> None:
        try:
            await context.close()
        except Exception:
            pass

    async def _reopen(self) -> None:
        if self._context is not None:
            if self._leases.get(self._generation):
                # 还有请求在旧页面上执行，等最后一个归还时再关
                self._retired[self._generation] = self._context
            else:
                await self._close_quietly(self._context)
            self._context = None
        self._page = None
        self._generation += 1
        context_kwargs = {
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": self.user_agent,
        }
        if TIMEZONE:
            context_kwargs["timezone_id"] = TIMEZONE
        context = await self._browser.new_context(**context_kwargs)
        context.set_default_timeout(PAGE_MODULE_TIMEOUT_MS)
        await context.add_init_script(BASE_INIT_SCRIPT)
        await context.add_init_script(VM_SCRIPT)
        await Stealth().apply_stealth_async(context)
        page = await context.new_page()
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while not await self._is_ready(page):
            if time.monotonic() > deadline:
                title = await page.title()
                await context.close()
                raise RuntimeError(f"chatgpt.com page not ready (title={title!r}), likely stuck on a Cloudflare challenge")
            await asyncio.sleep(1)
        self.environment = await page.evaluate(ENVIRONMENT_SCRIPT)
        self._context = context
        self._page = page
        self._opened_at = time.monotonic()
        self._stale = False
        self.last_error = ""
        print(f"[sentinel-solver] chatgpt.com page ready: {await page.title()!r}", flush=True)

    @staticmethod
    async def _is_ready(page: Page | None) -> bool:
        if page is None or page.is_closed():
            return False
        try:
            title = await page.title()
            if any(marker in title for marker in CHALLENGE_TITLES):
                return False
            return await page.evaluate("location.hostname === 'chatgpt.com' && typeof window.__sentinelVM === 'object'")
        except Exception:
            return False

    async def turnstile_asset(self) -> str:
        """找到本页面实际加载的 turnstile 模块地址；找不到时扫描页面脚本，再不行用固定路径。"""
        if self._turnstile_asset:
            return self._turnstile_asset
        try:
            loaded = await self.evaluate(DISCOVER_TURNSTILE_ASSET, None)
            if loaded:
                self._turnstile_asset = str(loaded[0])
            else:
                scanned = await self.evaluate(SCAN_TURNSTILE_ASSET, r"/unauth-mweb/assets/turnstile-[A-Za-z0-9_-]+\.js")
                self._turnstile_asset = str(scanned or "")
        except Exception as exc:
            self.last_error = str(exc)
        if not self._turnstile_asset:
            self._turnstile_asset = PINNED_TURNSTILE_ASSET
        print(f"[sentinel-solver] turnstile asset: {self._turnstile_asset}", flush=True)
        return self._turnstile_asset

    async def solve_turnstile(self, requirements: dict[str, Any] | None, dx: str, p: str) -> tuple[bool, str]:
        """优先用页面自带模块求解，失败再退回移植的 VM；返回 (是否成功, token 或失败原因)。"""
        if requirements:
            async with self._solve_lock:
                ok, result = await self._solve_via_page_module(requirements, p)
            if ok:
                return True, result
        if not dx:
            return False, self.last_error or "no dx and no requirements supplied"
        result = str(await self.evaluate(SOLVE_VIA_VM, [dx, p]) or "")
        if result.startswith("ERR:"):
            return False, result[4:]
        error = validate_turnstile_token(result, p)
        return (False, error) if error else (True, result)

    async def _solve_via_page_module(self, requirements: dict[str, Any], p: str) -> tuple[bool, str]:
        """调用方须持有 self._solve_lock。"""
        error = ""
        generation: int | None = None
        for attempt in range(1, PAGE_MODULE_ATTEMPTS + 1):
            asset = await self.turnstile_asset()
            try:
                value, generation = await self._evaluate(SOLVE_VIA_PAGE_MODULE, [requirements, p, asset])
            except Exception as exc:
                # evaluate 已经作废过出错的页面并重试过一次
                error, generation = str(exc), None
                break
            raw = str(value or "")
            if raw.startswith("ERR:"):
                error = raw[4:]
                break
            error = validate_turnstile_token(raw, p)
            if not error:
                return True, raw
            # 只有冷页面上的 VM 超时值得在热页面上重试
            if not is_retryable_solve_error(error) or attempt == PAGE_MODULE_ATTEMPTS:
                break
            print(f"[sentinel-solver] {error} (attempt {attempt}/{PAGE_MODULE_ATTEMPTS})", flush=True)
        self.last_error = error[:300]
        print(f"[sentinel-solver] page module solve failed: {error[:300]}", flush=True)
        # 下次重新查找模块并重开页面；页面若已被别的请求重开过，就沿用新页面和新找到的模块
        if generation is not None and generation == self._generation:
            self._turnstile_asset = ""
            await self.invalidate(generation)
        return False, error

    async def evaluate(self, expression: str, arg: object) -> object:
        """在页面里执行；页面失效时重开一次再试。"""
        value, _ = await self._evaluate(expression, arg)
        return value

    async def _evaluate(self, expression: str, arg: object) -> tuple[object, int]:
        """同 evaluate，另返回执行所在的页面代数。"""
        for attempt in range(2):
            async with self._lease() as (page, generation):
                try:
                    return await page.evaluate(expression, arg), generation
                except Exception as exc:
                    self.last_error = str(exc)
                    if attempt:
                        raise
            await self.invalidate(generation)
        raise RuntimeError("unreachable")

    async def cloudflare_cookies(self, proxy: str) -> dict[str, object]:
        """走指定代理新开一个上下文打开 chatgpt.com，取回 Cloudflare 发的 cookie。

        真实浏览器在后端被挑战的 IP 上通常直接 200，只拿到 __cf_bm/_cfuvid 等 cookie；
        后端带上它们即可通过。cookie 绑定出口 IP，只能给同一个代理用。
        """
        if self._browser is None:
            raise RuntimeError("browser not started")
        context_kwargs: dict[str, Any] = {
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": self.user_agent,
        }
        if proxy:
            context_kwargs["proxy"] = playwright_proxy(proxy)
        if TIMEZONE:
            context_kwargs["timezone_id"] = TIMEZONE
        async with self._cf_cookie_slots:
            context = await self._browser.new_context(**context_kwargs)
            try:
                await Stealth().apply_stealth_async(context)
                page = await context.new_page()
                await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
                deadline = time.monotonic() + CF_COOKIE_TIMEOUT_SECONDS
                while True:
                    title = await page.title()
                    cookies = await context.cookies(CHATGPT_URL)
                    names = {cookie["name"] for cookie in cookies}
                    challenged = any(marker in title for marker in CHALLENGE_TITLES)
                    if not challenged and "__cf_bm" in names:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"no Cloudflare cookies (title={title!r}, challenged={challenged})")
                    await asyncio.sleep(1)
            finally:
                await context.close()
        picked = [cookie for cookie in cookies if cookie["name"] in CF_COOKIE_NAMES]
        expiries = [float(cookie["expires"]) for cookie in picked if float(cookie.get("expires") or -1) > 0]
        return {
            "cookies": {cookie["name"]: cookie["value"] for cookie in picked},
            "expires_at": min(expiries) if expiries else None,
            "user_agent": self.user_agent,
        }

    def status(self) -> dict[str, object]:
        return {
            "ready": self._page is not None and not self._page.is_closed(),
            "page_age_seconds": int(time.monotonic() - self._opened_at) if self._opened_at else None,
            "browser_version": self._browser.version if self._browser else None,
            "user_agent": self.user_agent,
            "timezone": TIMEZONE or None,
            "environment": self.environment,
            "last_error": self.last_error or None,
        }


solver = SolverPage()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await solver.start()
    yield
    await solver.stop()


app = FastAPI(title="chatgpt2api sentinel solver", lifespan=lifespan)


class TurnstileRequest(BaseModel):
    p: str
    dx: str = ""
    # 完整的 chat-requirements/prepare 响应，只含挑战材料，不含账号 token、cookie 或用户提示词
    requirements: dict[str, Any] | None = None


class CloudflareCookiesRequest(BaseModel):
    # 主服务要用的出口代理；cookie 绑定出口 IP，必须用同一个代理去取
    proxy: str = ""


class SORequest(BaseModel):
    collector_dx: str
    snapshot_dx: str
    p: str


def require_key(authorization: str | None) -> None:
    if not SOLVER_KEY:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, SOLVER_KEY):
        raise HTTPException(status_code=401, detail="invalid solver key")


@app.get("/health")
async def health() -> dict[str, object]:
    return solver.status()


@app.post("/v1/turnstile")
async def solve_turnstile(body: TurnstileRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_key(authorization)
    try:
        ok, token = await solver.solve_turnstile(body.requirements, body.dx, body.p)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"turnstile solve failed: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=503, detail=f"turnstile solve failed: {token[:300]}")
    return {"token": token}


@app.post("/v1/cf-cookies")
async def cloudflare_cookies(
    body: CloudflareCookiesRequest, authorization: str | None = Header(default=None)
) -> dict[str, object]:
    require_key(authorization)
    try:
        return await solver.cloudflare_cookies(body.proxy.strip())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"cf cookies failed: {str(exc)[:300]}") from exc


@app.post("/v1/so")
async def solve_so(body: SORequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_key(authorization)
    try:
        token = await solver.evaluate(
            "([c, s, p]) => window.__sentinelVM.so(c, s, p)", [body.collector_dx, body.snapshot_dx, body.p]
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"so solve failed: {exc}") from exc
    return {"token": str(token or "")}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=False)
