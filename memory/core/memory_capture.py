"""Passive group capture helpers for Memory."""

import weakref
from typing import Any

from astrbot.api import logger, sp
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType

SESSION_PLUGIN_NAMES = ("astrbot_zhouyi_plugin",)
_ACTIVE_SERVICE_REF: weakref.ReferenceType | None = None


def set_active_service(service: Any) -> None:
    """Track the active service for passive filter side effects."""
    global _ACTIVE_SERVICE_REF
    _ACTIVE_SERVICE_REF = weakref.ref(service) if service is not None else None


def get_active_service() -> Any:
    if _ACTIVE_SERVICE_REF is None:
        return None
    return _ACTIVE_SERVICE_REF()


async def is_session_enabled(session_id: str) -> bool:
    """Mirror AstrBot's session-level shutdown check for passive capture."""
    try:
        session_services = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_service_config",
            default={},
        )
    except Exception as exc:
        logger.debug(f"[{session_id}] 读取会话总开关失败，默认允许捕获: {exc}")
        return True

    if not isinstance(session_services, dict):
        return True
    session_enabled = session_services.get("session_enabled")
    return True if session_enabled is None else bool(session_enabled)


async def is_plugin_enabled_for_session(session_id: str) -> bool:
    """Mirror AstrBot session-level plugin disable checks for passive capture."""
    try:
        session_plugin_config = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_plugin_config",
            default={},
        )
    except Exception as exc:
        logger.debug(f"[{session_id}] 读取会话插件开关失败，默认允许捕获: {exc}")
        return True

    if not isinstance(session_plugin_config, dict):
        return True
    session_config = session_plugin_config.get(session_id, {})
    if not isinstance(session_config, dict):
        return True
    disabled_plugins = session_config.get("disabled_plugins", [])
    if not isinstance(disabled_plugins, list):
        return True
    return not any(name in disabled_plugins for name in SESSION_PLUGIN_NAMES)


class MemoryCaptureFilter(CustomFilter):
    """Schedule group-message capture without waking AstrBot's message pipeline."""

    def __init__(self, raise_error: bool = True, service=None, **kwargs) -> None:
        if not isinstance(raise_error, bool) and service is None:
            service = raise_error
            raise_error = True
        super().__init__(raise_error=raise_error, **kwargs)
        self._service_ref = weakref.ref(service) if service is not None else None

    def _get_service(self):
        if self._service_ref is not None:
            return self._service_ref()
        return get_active_service()

    @staticmethod
    def _passes_global_whitelist(event: AstrMessageEvent, cfg) -> bool:
        platform_settings = (
            cfg.get("platform_settings", {}) if isinstance(cfg, dict) else {}
        )
        if not platform_settings.get("enable_id_white_list", False):
            return True

        whitelist = [
            str(item).strip()
            for item in platform_settings.get("id_whitelist", [])
            if str(item).strip()
        ]
        if not whitelist or event.get_platform_name() == "webchat":
            return True

        if platform_settings.get("wl_ignore_admin_on_group", False):
            try:
                if (
                    getattr(event, "role", None) == "admin"
                    and event.get_message_type() == MessageType.GROUP_MESSAGE
                ):
                    return True
            except Exception:
                pass

        try:
            group_id = str(event.get_group_id()).strip()
        except Exception:
            group_id = ""

        return event.unified_msg_origin in whitelist or group_id in whitelist

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        service = self._get_service()
        if not service or getattr(service, "_terminating", False) is True:
            return False
        bootstrap = getattr(service, "bootstrap", None)
        config_manager = getattr(service, "config_manager", None)
        if bootstrap is None or not bootstrap.is_initialized:
            return False
        if config_manager is None or not config_manager.get(
            "session_manager.enable_full_group_capture", True
        ):
            return False
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return False
        except Exception as exc:
            logger.debug(f"Memory 被动群消息捕获类型检查失败: {exc}")
            return False

        if not self._passes_global_whitelist(event, cfg):
            return False

        service._schedule_passive_group_capture(event)
        return False
