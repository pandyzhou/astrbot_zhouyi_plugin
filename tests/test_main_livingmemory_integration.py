from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin import main as plugin_main
from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin


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


class MainLivingMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_module_migration_precedes_the_only_register_and_metadata_is_020(self):
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
        self.assertEqual(register_call.args[3].value, "0.2.0")

        migration_call = next(
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_migrate_living_memory_config"
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
            and node.name == "_migrate_living_memory_config"
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

    async def test_config_none_and_disabled_section_do_not_start_component(self):
        context = DummyContext()
        with (
            patch.object(MyPlugin, "_bar_data_loop", wait_forever),
            patch.object(MyPlugin, "_run_standalone_web", wait_forever),
            patch.object(plugin_main, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(plugin_main, "LivingMemoryComponent") as component_cls,
        ):
            plugin_none = MyPlugin(context, config=None)
            plugin_disabled = MyPlugin(
                context, config={"living_memory": {"enabled": False}}
            )
            self.assertIsNone(plugin_none._living_memory_component)
            self.assertIsNone(plugin_disabled._living_memory_component)
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
            patch.object(MyPlugin, "_run_standalone_web", wait_forever),
            patch.object(plugin_main, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(plugin_main, "LivingMemoryComponent", FakeComponent),
            patch.object(
                plugin_main.StarTools,
                "get_data_dir",
                return_value=Path("/legacy-livingmemory-data"),
            ) as get_data_dir,
        ):
            plugin = MyPlugin(
                context,
                config={"living_memory": {"enabled": True, "bot_language": "zh"}},
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

        get_data_dir.assert_called_once_with("astrbot_plugin_livingmemory")
        self.assertEqual(
            created,
            [
                (
                    context,
                    {"enabled": True, "bot_language": "zh"},
                    "/legacy-livingmemory-data",
                )
            ],
        )

    async def test_disabled_command_returns_clear_message_and_event_hooks_noop(self):
        plugin = object.__new__(MyPlugin)
        plugin._living_memory_enabled = False
        plugin._living_memory_component = None
        event = DummyEvent()

        self.assertEqual(
            await collect_results(plugin.help(event)),
            [
                (
                    "plain",
                    "LivingMemory 长期记忆功能未启用，请在插件配置中开启 living_memory.enabled。",
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
            patch.object(MyPlugin, "_run_standalone_web", wait_forever),
            patch.object(plugin_main, "StandaloneWebService", FakeStandaloneWebService),
            patch.object(
                plugin_main,
                "LivingMemoryComponent",
                side_effect=RuntimeError("component boom"),
            ),
            patch.object(
                plugin_main.StarTools,
                "get_data_dir",
                return_value=Path("/legacy-livingmemory-data"),
            ),
        ):
            plugin = MyPlugin(
                context, config={"living_memory": {"enabled": True}}
            )
            self.assertEqual(len(context.registered_web_apis), 12)
            self.assertIsNone(plugin._living_memory_component)
            self.assertIsNotNone(plugin._standalone_task)
            self.assertIsNotNone(plugin._trend_task)
            self.assertEqual(
                await collect_results(plugin.status(DummyEvent())),
                [("plain", "LivingMemory 长期记忆组件启动失败，请检查插件日志。")],
            )
            await plugin.terminate()

    async def test_terminate_is_idempotent_and_isolates_all_cleanup_failures(self):
        plugin = object.__new__(MyPlugin)
        plugin._terminate_lock = asyncio.Lock()
        plugin._terminated = False
        plugin._settings_changed_event = asyncio.Event()
        plugin._living_memory_component = type(
            "FailingComponent",
            (),
            {"terminate": AsyncMock(side_effect=RuntimeError("memory stop boom"))},
        )()
        plugin._standalone_service = type(
            "FailingService",
            (),
            {"stop": AsyncMock(side_effect=RuntimeError("web stop boom"))},
        )()
        plugin._standalone_task = asyncio.create_task(wait_forever())
        plugin._trend_task = asyncio.create_task(wait_forever())
        component = plugin._living_memory_component
        service = plugin._standalone_service
        standalone_task = plugin._standalone_task
        trend_task = plugin._trend_task

        await plugin.terminate()
        await plugin.terminate()

        component.terminate.assert_awaited_once()
        service.stop.assert_awaited_once()
        self.assertTrue(standalone_task.done())
        self.assertTrue(trend_task.done())
        self.assertTrue(plugin._settings_changed_event.is_set())
        self.assertIsNone(plugin._living_memory_component)
        self.assertIsNone(plugin._standalone_task)
        self.assertIsNone(plugin._trend_task)


if __name__ == "__main__":
    unittest.main()
