from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin import runtime as plugin_runtime
from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.runtime import PluginRuntime


class DummyContext:
    def __init__(self) -> None:
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, description) -> None:
        self.registered_web_apis.append((route, handler, methods, description))


class DummyEvent:
    def plain_result(self, text):
        return ("plain", text)


async def collect_results(generator):
    return [item async for item in generator]


async def wait_forever(*_args, **_kwargs):
    await asyncio.Event().wait()


class FakeStandaloneWebService:
    async def run(self):
        await asyncio.Event().wait()

    async def stop(self):
        return None


class MainMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_module_migration_precedes_the_only_register_and_metadata_is_030(self):
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        register_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register"
        ]
        self.assertEqual(len(register_calls), 1)
        register_call = register_calls[0]
        self.assertEqual(register_call.args[0].value, "astrbot_zhouyi_plugin")
        self.assertIn("长期记忆", register_call.args[2].value)
        self.assertEqual(register_call.args[3].value, "0.3.0")

        migration_call = next(
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_migrate_memory_config"
        )
        plugin_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        self.assertLess(migration_call.lineno, plugin_class.lineno)

        migration_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_migrate_memory_config"
        )
        called_names = {
            node.func.id
            for node in ast.walk(migration_function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("get_astrbot_config_path", called_names)
        self.assertIn("migrate_config_file", called_names)

    def test_root_declares_four_event_proxies_and_ten_admin_commands(self):
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        plugin_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        methods = {
            node.name: node
            for node in plugin_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {
                "handle_all_group_messages",
                "handle_memory_recall",
                "handle_memory_reflection",
                "handle_session_reset",
            }.issubset(methods)
        )

        command_names = {
            "status": "status",
            "search": "search",
            "forget": "forget",
            "rebuild_index": "rebuild-index",
            "rebuild_graph": "rebuild-graph",
            "webui": "webui",
            "summarize": "summarize",
            "reset": "reset",
            "cleanup": "cleanup",
            "help": "help",
        }
        for method_name, command_name in command_names.items():
            method = methods[method_name]
            permission_decorators = [
                decorator
                for decorator in method.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "permission_type"
            ]
            self.assertEqual(len(permission_decorators), 1, method_name)
            command_decorators = [
                decorator
                for decorator in method.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
            ]
            self.assertEqual(len(command_decorators), 1, method_name)
            self.assertEqual(command_decorators[0].args[0].value, command_name)

            nested = methods[f"zhouyi_memory_{method_name}"]
            self.assertTrue(
                any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "permission_type"
                    for decorator in nested.decorator_list
                ),
                method_name,
            )
            nested_command = next(
                decorator
                for decorator in nested.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
            )
            self.assertEqual(nested_command.args[0].value, command_name)

        legacy_mc = {
            "get_help": "mchelp",
            "mcgetter": "mc",
            "mcadd": "mcadd",
            "mcdel": "mcdel",
            "mcget": "mcget",
            "mcup": "mcup",
            "mclist": "mclist",
            "mccleanup": "mccleanup",
            "mcdata": "mcdata",
        }
        nested_mc = {
            "zhouyi_mc_help": "help",
            "zhouyi_mc_status": "status",
            "zhouyi_mc_add": "add",
            "zhouyi_mc_delete": "delete",
            "zhouyi_mc_get": "get",
            "zhouyi_mc_update": "update",
            "zhouyi_mc_list": "list",
            "zhouyi_mc_cleanup": "cleanup",
            "zhouyi_mc_data": "data",
        }
        for method_name, command_name in {**legacy_mc, **nested_mc}.items():
            method = methods[method_name]
            command = next(
                decorator
                for decorator in method.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
            )
            self.assertEqual(command.args[0].value, command_name)
            self.assertFalse(
                any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "permission_type"
                    for decorator in method.decorator_list
                ),
                method_name,
            )

        self.assertTrue(
            any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "group"
                and decorator.keywords[0].value.value == "mc"
                for decorator in methods["zhouyi_mc"].decorator_list
            )
        )
        self.assertTrue(
            any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "group"
                and decorator.keywords[0].value.value == "memory"
                for decorator in methods["zhouyi_memory"].decorator_list
            )
        )

    async def test_config_none_and_disabled_section_do_not_start_component(self):
        context = DummyContext()
        with (
            patch.object(MyPlugin, "_bar_data_loop", wait_forever),
            patch.object(plugin_runtime, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(plugin_runtime, "MemoryService") as component_cls,
        ):
            plugin_none = MyPlugin(context, config=None)
            plugin_disabled = MyPlugin(
                context, config={"memory": {"enabled": False}}
            )
            self.assertIsNone(plugin_none.runtime.memory)
            self.assertIsNone(plugin_disabled.runtime.memory)
            self.assertFalse(plugin_none.runtime.memory_enabled)
            self.assertFalse(plugin_disabled.runtime.memory_enabled)
            component_cls.assert_not_called()
            await plugin_none.terminate()
            await plugin_disabled.terminate()

    async def test_enabled_section_uses_legacy_data_dir_and_proxies_commands(self):
        context = DummyContext()
        created = []

        class FakeComponent:
            def __init__(self, component_context, config, data_dir):
                created.append((component_context, config, data_dir))

            async def status(self, event):
                yield event.plain_result("memory-status")

            async def search(self, event, query, k):
                yield event.plain_result(f"{query}:{k}")

            async def terminate(self):
                return None

        with (
            patch.object(MyPlugin, "_bar_data_loop", wait_forever),
            patch.object(plugin_runtime, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(plugin_runtime, "MemoryService", FakeComponent),
            patch.object(
                plugin_runtime,
                "ensure_memory_data",
                return_value=SimpleNamespace(
                    target_dir=Path("/current-plugin-data/memory"),
                    migrated=True,
                ),
            ) as ensure_data,
            patch.object(
                plugin_runtime.StarTools,
                "get_data_dir",
                side_effect=lambda name: Path(f"/{name}"),
            ) as get_data_dir,
        ):
            plugin = MyPlugin(
                context,
                config={"memory": {"enabled": True, "bot_language": "zh"}},
            )
            event = DummyEvent()
            self.assertEqual(
                await collect_results(plugin.status(event)),
                [("plain", "memory-status")],
            )
            self.assertEqual(
                await collect_results(plugin.search(event, "星星", 7)),
                [("plain", "星星:7")],
            )
            await plugin.terminate()

        self.assertEqual(
            get_data_dir.call_args_list,
            [
                call("astrbot_zhouyi_plugin"),
                call("astrbot_plugin_livingmemory"),
            ],
        )
        ensure_data.assert_called_once_with(
            Path("/astrbot_plugin_livingmemory"),
            Path("/astrbot_zhouyi_plugin"),
        )
        self.assertEqual(
            created,
            [
                (
                    context,
                    {"enabled": True, "bot_language": "zh"},
                    "/current-plugin-data/memory",
                )
            ],
        )

    async def test_disabled_command_returns_clear_message_and_event_hooks_noop(self):
        plugin = object.__new__(MyPlugin)
        plugin.runtime = SimpleNamespace(
            memory_enabled=False,
            memory=None,
            memory_error=None,
        )
        event = DummyEvent()

        self.assertEqual(
            await collect_results(plugin.help(event)),
            [
                (
                    "plain",
                    "长期记忆功能未启用，请在插件配置中开启 memory.enabled。",
                )
            ],
        )
        await plugin.handle_memory_recall(event, object())
        await plugin.handle_memory_reflection(event, object())
        await plugin.handle_session_reset(event)

    async def test_component_construction_failure_does_not_break_mc_initialization(self):
        context = DummyContext()
        with (
            patch.object(MyPlugin, "_bar_data_loop", wait_forever),
            patch.object(plugin_runtime, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(
                plugin_runtime,
                "MemoryService",
                side_effect=RuntimeError("component boom"),
            ),
            patch.object(
                plugin_runtime,
                "ensure_memory_data",
                return_value=SimpleNamespace(
                    target_dir=Path("/current-plugin-data/memory"),
                    migrated=False,
                ),
            ),
            patch.object(
                plugin_runtime.StarTools,
                "get_data_dir",
                side_effect=lambda name: Path(f"/{name}"),
            ),
        ):
            plugin = MyPlugin(
                context, config={"memory": {"enabled": True}}
            )
            self.assertEqual(len(context.registered_web_apis), 49)
            registered = {
                (route, tuple(methods))
                for route, _, methods, _ in context.registered_web_apis
            }
            self.assertIn(
                ("/astrbot_zhouyi_plugin/page/v1/config/memory", ("GET",)),
                registered,
            )
            self.assertIn(
                ("/astrbot_zhouyi_plugin/page/v1/config/memory", ("POST",)),
                registered,
            )
            self.assertIsNone(plugin.runtime.memory)
            self.assertIsNotNone(plugin.runtime.standalone_task)
            self.assertIsNotNone(plugin.runtime.trend_task)
            self.assertEqual(
                await collect_results(plugin.status(DummyEvent())),
                [("plain", "component boom")],
            )
            await plugin.terminate()

    async def test_memory_terminate_timeout_does_not_block_standalone_or_trend_cleanup(self):
        plugin = object.__new__(MyPlugin)
        runtime = PluginRuntime(plugin, DummyContext())
        plugin.runtime = runtime
        runtime._started = True
        runtime.MEMORY_TERMINATE_TIMEOUT_SECONDS = 0.01
        memory_started = asyncio.Event()

        async def stuck_memory_terminate():
            memory_started.set()
            await asyncio.Event().wait()

        runtime.memory = SimpleNamespace(terminate=stuck_memory_terminate)
        runtime.standalone_service = SimpleNamespace(stop=AsyncMock())
        runtime.standalone_task = asyncio.create_task(wait_forever())
        runtime.trend_task = asyncio.create_task(wait_forever())
        standalone_task = runtime.standalone_task
        trend_task = runtime.trend_task

        await asyncio.wait_for(plugin.terminate(), timeout=0.2)

        self.assertTrue(memory_started.is_set())
        runtime.standalone_service.stop.assert_awaited_once()
        self.assertTrue(standalone_task.done())
        self.assertTrue(trend_task.done())
        self.assertTrue(runtime._terminated)

    async def test_terminate_is_idempotent_and_isolates_all_cleanup_failures(self):
        plugin = object.__new__(MyPlugin)
        runtime = PluginRuntime(plugin, DummyContext())
        plugin.runtime = runtime
        runtime._started = True
        runtime.memory = type(
            "FailingComponent",
            (),
            {"terminate": AsyncMock(side_effect=RuntimeError("memory stop boom"))},
        )()
        runtime.standalone_service = type(
            "FailingService",
            (),
            {"stop": AsyncMock(side_effect=RuntimeError("web stop boom"))},
        )()
        runtime.standalone_task = asyncio.create_task(wait_forever())
        runtime.trend_task = asyncio.create_task(wait_forever())
        component = runtime.memory
        service = runtime.standalone_service
        standalone_task = runtime.standalone_task
        trend_task = runtime.trend_task

        await plugin.terminate()
        await plugin.terminate()

        component.terminate.assert_awaited_once()
        service.stop.assert_awaited_once()
        self.assertTrue(standalone_task.done())
        self.assertTrue(trend_task.done())
        self.assertTrue(runtime.settings_changed_event.is_set())
        self.assertIsNone(runtime.memory)
        self.assertIsNone(runtime.standalone_task)
        self.assertIsNone(runtime.trend_task)


if __name__ == "__main__":
    unittest.main()
