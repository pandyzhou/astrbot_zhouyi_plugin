from __future__ import annotations

import asyncio
import inspect
from dataclasses import asdict
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from .runtime_settings import (
    GLOBAL_SETTING_KEYS,
    GROUP_OVERRIDE_KEYS,
    EffectiveRuntimeSettings,
    GroupRuntimeSettings,
    RuntimeSettings,
)

T = TypeVar("T")

SETTINGS_CONSTRAINTS: dict[str, dict[str, int | float | str]] = {
    "max_history_points": {"min": 168, "max": 100000, "step": 1, "unit": "点/服务器"},
    "auto_cleanup_days": {"min": 1, "max": 365, "step": 1, "unit": "天"},
    "default_trend_hours": {"min": 1, "max": 168, "step": 1, "unit": "小时"},
    "mc_lookup_timeout_seconds": {"min": 0.5, "max": 30, "step": 0.5, "unit": "秒"},
    "mc_status_timeout_seconds": {"min": 1, "max": 60, "step": 0.5, "unit": "秒"},
    "max_concurrent_queries": {"min": 1, "max": 20, "step": 1, "unit": "个"},
}


def serialize_settings(settings: Any) -> dict[str, Any]:
    """只返回前端可见的运行配置字段。"""
    values = asdict(settings)
    return {key: values[key] for key in GLOBAL_SETTING_KEYS}


def serialize_group_overrides(settings: GroupRuntimeSettings) -> dict[str, Any]:
    values = asdict(settings)
    return {
        key: values[key]
        for key in GROUP_OVERRIDE_KEYS
        if values[key] is not None
    }


def revision_payload(
    global_settings: RuntimeSettings,
    group_settings: GroupRuntimeSettings,
) -> dict[str, int]:
    return {
        "global": global_settings.revision,
        "group": group_settings.revision,
    }


def projected_effective(
    *,
    scope: str,
    proposed: RuntimeSettings | GroupRuntimeSettings,
    global_settings: RuntimeSettings,
    group_settings: GroupRuntimeSettings,
    current_effective: EffectiveRuntimeSettings,
) -> dict[str, Any]:
    if scope == "global":
        return serialize_settings(proposed)

    values = serialize_settings(current_effective)
    proposed_values = asdict(proposed)
    global_values = serialize_settings(global_settings)
    for key in GROUP_OVERRIDE_KEYS:
        value = proposed_values[key]
        values[key] = global_values[key] if value is None else value
    return values


def _signature_target(callable_obj: Callable[..., Any]) -> Callable[..., Any]:
    """AsyncMock 有 callable side_effect 时，以实际 side_effect 的签名为准。"""
    side_effect = getattr(callable_obj, "side_effect", None)
    return side_effect if callable(side_effect) else callable_obj


def accepts_keywords(callable_obj: Callable[..., Any], names: set[str]) -> bool:
    try:
        parameters = inspect.signature(_signature_target(callable_obj)).parameters.values()
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    supported = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return names.issubset(supported)


def accepts_timeout_kwargs(fetcher: Callable[..., Awaitable[Any]]) -> bool:
    """安全判断注入 fetcher 是否支持运行时超时关键字。"""
    return accepts_keywords(fetcher, {"lookup_timeout", "status_timeout"})


async def call_status_fetcher(
    fetcher: Callable[..., Awaitable[T]],
    host: str,
    *,
    lookup_timeout: float,
    status_timeout: float,
) -> T:
    """兼容只接收 host 的测试替身，不通过捕获 TypeError 降级。"""
    if accepts_timeout_kwargs(fetcher):
        return await fetcher(
            host,
            lookup_timeout=lookup_timeout,
            status_timeout=status_timeout,
        )
    return await fetcher(host)


async def gather_limited(
    calls: Iterable[Callable[[], Awaitable[T]]],
    max_concurrency: int,
) -> list[T | Exception]:
    """按输入顺序执行异步调用，限制并发并隔离单项异常。"""
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise TypeError("max_concurrency 必须是整数")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须大于等于 1")

    semaphore = asyncio.Semaphore(max_concurrency)

    async def invoke(call: Callable[[], Awaitable[T]]) -> T | Exception:
        async with semaphore:
            try:
                return await call()
            except Exception as exc:
                return exc

    return list(await asyncio.gather(*(invoke(call) for call in calls)))
