from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

MODULE_NAME = "data.plugins.astrbot_zhouyi_plugin.memory.service"
PACKAGE_NAME = "data.plugins.astrbot_zhouyi_plugin.memory"


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
        self.evolving_memory_manager = None
        self.evolving_memory_store = None
        self.db = None
        self.graph_db = None
        self._initialized_callback = None

    def set_initialized_callback(self, callback):
        self._initialized_callback = callback

    async def initialize(self):
        raise RuntimeError("initializer boom")

    async def ensure_initialized(self):
        return False

    async def stop_background_tasks(self):
        return None

    async def stop_scheduler(self):
        return None

    async def cleanup_runtime_resources(self):
        return None


class _RetryInitializer(_FailingInitializer):
    async def initialize(self):
        self.retry_task = asyncio.create_task(self._recover())
        return False

    async def _recover(self):
        await asyncio.sleep(0)
        self.memory_engine = object()
        self.memory_processor = object()
        self.conversation_manager = object()
        self.index_validator = object()
        self.evolving_memory_manager = object()
        self.evolving_memory_store = object()
        self.is_initialized = True
        await self._initialized_callback()


class _RuntimeComponent:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def shutdown(self):
        return None


class _Tool(_RuntimeComponent):
    pass


class _Context:
    def __init__(self):
        self.tools = []

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_component_module(initializer_cls=_FailingInitializer):
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
        f"{PACKAGE_NAME}.core.memory_commands": _module(
            f"{PACKAGE_NAME}.core.memory_commands",
            MemoryCommands=_RuntimeComponent,
        ),
        f"{PACKAGE_NAME}.core.memory_events": _module(
            f"{PACKAGE_NAME}.core.memory_events",
            MemoryEvents=_RuntimeComponent,
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
        f"{PACKAGE_NAME}.core.memory_capture": _module(
            f"{PACKAGE_NAME}.core.memory_capture",
            get_active_service=lambda: active["component"],
            set_active_service=lambda component: active.__setitem__(
                "component", component
            ),
            is_plugin_enabled_for_session=lambda _session: asyncio.sleep(
                0, result=True
            ),
            is_session_enabled=lambda _session: asyncio.sleep(0, result=True),
        ),
        f"{PACKAGE_NAME}.core.memory_bootstrap": _module(
            f"{PACKAGE_NAME}.core.memory_bootstrap",
            MemoryBootstrap=initializer_cls,
        ),
        f"{PACKAGE_NAME}.core.tools": _module(
            f"{PACKAGE_NAME}.core.tools",
            MemoryMemorizeTool=_Tool,
            MemorySearchTool=_Tool,
        ),
    }
    sys.modules.pop(MODULE_NAME, None)
    with patch.dict(sys.modules, stubs):
        return importlib.import_module(MODULE_NAME)


class MemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_failure_is_recorded_and_terminate_is_idempotent(self):
        component_module = _load_component_module()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.MemoryService(
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

    async def test_provider_retry_success_automatically_creates_runtime_components(self):
        component_module = _load_component_module(_RetryInitializer)
        context = _Context()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.MemoryService(context, {}, data_dir)
            await asyncio.gather(*list(component._background_tasks))
            await component.bootstrap.retry_task

            self.assertTrue(component.ready)
            self.assertIsNotNone(component.events)
            self.assertIsNotNone(component.commands)
            self.assertIs(
                component.evolving_memory_manager,
                component.bootstrap.evolving_memory_manager,
            )
            self.assertIs(
                component.evolving_memory_store,
                component.bootstrap.evolving_memory_store,
            )
            self.assertEqual(len(context.tools), 1)
            self.assertIs(
                context.tools[0].kwargs["evolving_memory_manager"],
                component.evolving_memory_manager,
            )
            await component.terminate()

    async def test_after_message_sent_proxy_and_runtime_status_include_feedback_backfill(self):
        component_module = _load_component_module()
        component = object.__new__(component_module.MemoryService)
        component._ensure_plugin_ready = AsyncMock(return_value=(True, ""))
        component.events = SimpleNamespace(
            handle_after_message_sent=AsyncMock(),
            get_runtime_status=lambda: {
                "enabled": True,
                "buffered_rounds": 2,
                "task_count": 1,
            },
        )
        component.bootstrap = SimpleNamespace(
            get_runtime_status=lambda: {
                "key_facts_backfill": {"status": "running"}
            }
        )
        event = object()

        await component.handle_after_message_sent(event, "sent")
        status = component.get_runtime_status()

        component.events.handle_after_message_sent.assert_awaited_once_with(
            event, "sent"
        )
        self.assertEqual(status["key_facts_backfill"]["status"], "running")
        self.assertEqual(status["feedback"]["buffered_rounds"], 2)
        self.assertEqual(status["feedback"]["task_count"], 1)

    async def test_terminate_continues_closing_resources_after_cleanup_failure(self):
        component_module = _load_component_module()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.MemoryService(
                _Context(), {}, data_dir
            )
            await asyncio.gather(*list(component._background_tasks))

            conversation_store = type(
                "ConversationStore",
                (),
                {"close": AsyncMock()},
            )()
            bootstrap = type(
                "CleanupBootstrap",
                (),
                {
                    "stop_background_tasks": AsyncMock(
                        side_effect=RuntimeError("background stop boom")
                    ),
                    "stop_scheduler": AsyncMock(),
                    "cleanup_runtime_resources": AsyncMock(),
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
                    "graph_db": type(
                        "GraphVectorDatabase",
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
            component.bootstrap = bootstrap
            component.events = type(
                "MemoryEvents",
                (),
                {"shutdown": AsyncMock()},
            )()
            events = component.events

            await component.terminate()

            bootstrap.stop_background_tasks.assert_awaited_once()
            events.shutdown.assert_awaited_once()
            bootstrap.cleanup_runtime_resources.assert_awaited_once()
            conversation_store.close.assert_not_awaited()
            bootstrap.memory_engine.close.assert_not_awaited()
            bootstrap.graph_db.close.assert_not_awaited()
            bootstrap.db.close.assert_not_awaited()
            self.assertTrue(component._terminated)

    async def test_terminate_times_out_stuck_cleanup_and_continues(self):
        component_module = _load_component_module()
        with tempfile.TemporaryDirectory() as data_dir:
            component = component_module.MemoryService(_Context(), {}, data_dir)
            await asyncio.gather(*list(component._background_tasks))
            component.RESOURCE_STOP_TIMEOUT_SECONDS = 0.01

            async def ignore_cancellation():
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return None

            bootstrap = types.SimpleNamespace(
                stop_background_tasks=ignore_cancellation,
                cleanup_runtime_resources=AsyncMock(),
            )
            events = type("Events", (), {"shutdown": AsyncMock()})()
            component.bootstrap = bootstrap
            component.events = events

            await asyncio.wait_for(component.terminate(), timeout=0.2)

            events.shutdown.assert_awaited_once()
            bootstrap.cleanup_runtime_resources.assert_awaited_once()
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
            command_names.issubset(vars(component_module.MemoryService))
        )


if __name__ == "__main__":
    unittest.main()
