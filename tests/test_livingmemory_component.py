from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

MODULE_NAME = "data.plugins.astrbot_zhouyi_plugin.livingmemory.component"
PACKAGE_NAME = "data.plugins.astrbot_zhouyi_plugin.livingmemory"


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _ConfigManager:
    def __init__(self, config):
        self.config = config

    def get(self, _key, default=None):
        return default


class _BackupManager:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    async def backup_if_needed_async(self):
        return None


class _FailingInitializer:
    def __init__(self, context, config_manager, data_dir):
        self.context = context
        self.config_manager = config_manager
        self.data_dir = data_dir
        self.is_initialized = False
        self.is_failed = False
        self.error_message = None
        self._provider_check_attempts = 0
        self.memory_engine = None
        self.memory_processor = None
        self.conversation_manager = None
        self.index_validator = None
        self.db = None

    async def initialize(self):
        raise RuntimeError("initializer boom")

    async def ensure_initialized(self):
        return False

    async def stop_background_tasks(self):
        return None

    async def stop_scheduler(self):
        return None


class _Context:
    def add_llm_tools(self, *_tools):
        pass


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_component_module():
    active = {"component": None}
    logger = _Logger()
    stubs = {
        "astrbot": _module("astrbot"),
        "astrbot.api": _module("astrbot.api", logger=logger),
        "astrbot.api.event": _module(
            "astrbot.api.event",
            AstrMessageEvent=object,
            MessageEventResult=object,
        ),
        "astrbot.api.provider": _module(
            "astrbot.api.provider",
            LLMResponse=object,
            ProviderRequest=object,
        ),
        f"{PACKAGE_NAME}.core": _module(f"{PACKAGE_NAME}.core"),
        f"{PACKAGE_NAME}.core.base": _module(f"{PACKAGE_NAME}.core.base"),
        f"{PACKAGE_NAME}.core.managers": _module(f"{PACKAGE_NAME}.core.managers"),
        f"{PACKAGE_NAME}.core.base.config_manager": _module(
            f"{PACKAGE_NAME}.core.base.config_manager",
            ConfigManager=_ConfigManager,
        ),
        f"{PACKAGE_NAME}.core.command_handler": _module(
            f"{PACKAGE_NAME}.core.command_handler",
            CommandHandler=object,
        ),
        f"{PACKAGE_NAME}.core.event_handler": _module(
            f"{PACKAGE_NAME}.core.event_handler",
            EventHandler=object,
        ),
        f"{PACKAGE_NAME}.core.i18n_backend": _module(
            f"{PACKAGE_NAME}.core.i18n_backend",
            init=lambda _language: None,
            t=lambda key, **_kwargs: key,
        ),
        f"{PACKAGE_NAME}.core.managers.backup_manager": _module(
            f"{PACKAGE_NAME}.core.managers.backup_manager",
            BackupManager=_BackupManager,
        ),
        f"{PACKAGE_NAME}.core.passive_group_capture": _module(
            f"{PACKAGE_NAME}.core.passive_group_capture",
            get_active_component=lambda: active["component"],
            set_active_component=lambda component: active.__setitem__(
                "component", component
            ),
            is_plugin_enabled_for_session=lambda _session: asyncio.sleep(
                0, result=True
            ),
            is_session_enabled=lambda _session: asyncio.sleep(0, result=True),
        ),
        f"{PACKAGE_NAME}.core.plugin_initializer": _module(
            f"{PACKAGE_NAME}.core.plugin_initializer",
            PluginInitializer=_FailingInitializer,
        ),
        f"{PACKAGE_NAME}.core.tools": _module(
            f"{PACKAGE_NAME}.core.tools",
            MemoryMemorizeTool=object,
            MemorySearchTool=object,
        ),
    }
    sys.modules.pop(MODULE_NAME, None)
    with patch.dict(sys.modules, stubs):
        return importlib.import_module(MODULE_NAME)


class LivingMemoryComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_failure_is_recorded_and_terminate_is_idempotent(self):
        component_module = _load_component_module()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.LivingMemoryComponent(
                _Context(), {}, data_dir
            )
            tasks = list(component._background_tasks)
            self.assertTrue(tasks)
            await asyncio.gather(*tasks)

            self.assertEqual(component.VERSION, "2.3.6")
            self.assertFalse(component.ready)
            self.assertIn("initializer boom", component.initialization_error or "")

            await component.terminate()
            await component.terminate()
            self.assertTrue(component._terminated)

    async def test_terminate_continues_closing_resources_after_cleanup_failure(self):
        component_module = _load_component_module()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.LivingMemoryComponent(
                _Context(), {}, data_dir
            )
            await asyncio.gather(*list(component._background_tasks))

            conversation_store = type(
                "ConversationStore",
                (),
                {"close": AsyncMock()},
            )()
            initializer = type(
                "CleanupInitializer",
                (),
                {
                    "stop_background_tasks": AsyncMock(
                        side_effect=RuntimeError("background stop boom")
                    ),
                    "stop_scheduler": AsyncMock(),
                    "conversation_manager": type(
                        "ConversationManager",
                        (),
                        {"store": conversation_store},
                    )(),
                    "memory_engine": type(
                        "MemoryEngine",
                        (),
                        {"close": AsyncMock()},
                    )(),
                    "db": type(
                        "VectorDatabase",
                        (),
                        {"close": AsyncMock()},
                    )(),
                },
            )()
            component.initializer = initializer
            component.event_handler = type(
                "EventHandler",
                (),
                {"shutdown": AsyncMock()},
            )()

            await component.terminate()

            initializer.stop_background_tasks.assert_awaited_once()
            component.event_handler.shutdown.assert_awaited_once()
            initializer.stop_scheduler.assert_awaited_once()
            conversation_store.close.assert_awaited_once()
            initializer.memory_engine.close.assert_awaited_once()
            initializer.db.close.assert_awaited_once()
            self.assertTrue(component._terminated)

    def test_exposes_ten_command_business_proxies(self):
        component_module = _load_component_module()
        command_names = {
            "status",
            "search",
            "forget",
            "rebuild_index",
            "rebuild_graph",
            "webui",
            "summarize",
            "reset",
            "cleanup",
            "help",
        }
        self.assertTrue(
            command_names.issubset(vars(component_module.LivingMemoryComponent))
        )


if __name__ == "__main__":
    unittest.main()
