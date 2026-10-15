"""调用 sentinel 求解服务（sentinel_solver/）计算 Turnstile token。

2026-10-14 起 chatgpt.com 只接受在真实页面里算出的 Turnstile；缺失时对话接口返回 403，
低质量时返回伪装成 404 的 "Model not found"。求解服务未配置或求解失败时直接报错（SentinelSolverError），
不再退回本地求解器：它已跟不上新版挑战程序，算出的 token 只会换来伪装 404，并白白耗掉重试次数。
求解服务只收到挑战材料（dx、p、prepare 响应），账号 token、cookie 和用户提示词都留在主服务。
"""
from __future__ import annotations

import threading
import time

from curl_cffi import requests

from services.config import config
from utils.log import logger

SOLVER_TIMEOUT_SECONDS = 20
# 页面自带的 turnstile 模块在冷页面上首次加载会慢一些
SOLVER_SOLVE_TIMEOUT_SECONDS = 60
USER_AGENT_CACHE_SECONDS = 600

_user_agent_lock = threading.Lock()
_user_agent_cache: dict[str, object] = {"url": "", "value": {}, "expires_at": 0.0}


class SentinelSolverError(RuntimeError):
    """求解服务未配置，或没能算出 Turnstile token。"""


def _solver_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.sentinel_solver_key:
        headers["Authorization"] = f"Bearer {config.sentinel_solver_key}"
    return headers


def _solver_profile() -> dict:
    """求解服务 /health 的结果（页面 UA 与浏览器环境）；未配置或获取失败时返回空 dict。"""
    base_url = config.sentinel_solver_url
    if not base_url:
        return {}
    with _user_agent_lock:
        now = time.monotonic()
        if _user_agent_cache["url"] == base_url and now < float(_user_agent_cache["expires_at"]):
            return dict(_user_agent_cache["value"])
        value: dict = {}
        try:
            response = requests.get(f"{base_url}/health", headers=_solver_headers(), timeout=SOLVER_TIMEOUT_SECONDS)
            if response.status_code == 200:
                payload = response.json() or {}
                if payload.get("user_agent"):
                    value = payload
        except Exception as exc:
            logger.warning({"event": "sentinel_solver_health_error", "error": str(exc)})
        # 失败时短暂缓存空值，避免每个请求都去探测一个挂掉的求解服务
        _user_agent_cache.update(url=base_url, value=value, expires_at=now + (USER_AGENT_CACHE_SECONDS if value else 30))
        return dict(value)


def solver_user_agent() -> str:
    """求解页面使用的 UA；未配置求解服务或获取失败时返回空串。

    requirements/proof token 里编码的 UA 必须与算 Turnstile 的页面一致，
    否则对话接口返回伪装成 404 的 "Model not found"。
    """
    return str(_solver_profile().get("user_agent") or "")


def solver_environment() -> dict:
    """求解页面的时区、语言、核数等浏览器环境，用于生成 requirements/proof token；拿不到时返回空 dict。"""
    environment = _solver_profile().get("environment")
    return dict(environment) if isinstance(environment, dict) else {}


def solve_turnstile(dx: str, p: str, requirements: dict | None = None) -> str:
    """dx 为 prepare 响应里的 turnstile.dx，p 为 prepare 请求提交的 requirements token。

    requirements 是完整的 prepare 响应，传给求解服务后它会调用页面自带的 turnstile 模块，
    不传时只能用移植的 VM。
    """
    base_url = config.sentinel_solver_url
    if not base_url:
        raise SentinelSolverError(
            "sentinel solver is not configured (CHATGPT2API_SENTINEL_SOLVER_URL / config.json sentinel_solver_url)"
        )
    body: dict[str, object] = {"dx": dx, "p": p}
    if requirements:
        body["requirements"] = requirements
    try:
        response = requests.post(
            f"{base_url}/v1/turnstile",
            headers=_solver_headers(),
            json=body,
            timeout=SOLVER_SOLVE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning({"event": "sentinel_solver_error", "error": str(exc)})
        raise SentinelSolverError(f"sentinel solver request failed: {exc}") from exc
    if response.status_code != 200:
        logger.warning({
            "event": "sentinel_solver_failed",
            "status": response.status_code,
            "body": str(response.text or "")[:200],
        })
        raise SentinelSolverError(f"sentinel solver returned HTTP {response.status_code}: {str(response.text or '')[:200]}")
    token = str((response.json() or {}).get("token") or "")
    if not token:
        raise SentinelSolverError("sentinel solver returned an empty turnstile token")
    return token
