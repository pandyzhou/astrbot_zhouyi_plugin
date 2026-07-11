from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.script.json_operate import (
    DATABASE_NAME,
    default_config,
    get_group_storage,
    get_trend_history,
    write_json,
)


class DummyContext:
    def __init__(self) -> None:
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, description) -> None:
        for index, current in enumerate(self.registered_web_apis):
            if current[0] == route and current[2] == methods:
                self.registered_web_apis[index] = (
                    route,
                    handler,
                    methods,
                    description,
                )
                return
        self.registered_web_apis.append((route, handler, methods, description))


class StopLoop(BaseException):
    pass


class MainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_registration_and_data_dir_use_current_plugin_name(self):
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        plugin_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        register_call = next(
            decorator
            for decorator in plugin_class.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "register"
        )
        self.assertEqual(register_call.args[0].value, "astrbot_zhouyi_plugin")

        data_dir_names = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_data_dir"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        self.assertEqual(
            data_dir_names,
            ["astrbot_zhouyi_plugin", "astrbot_zhouyi_plugin"],
        )
        self.assertNotIn("astrbot_mcgetter", data_dir_names)
        self.assertNotIn("astrbot_mcgetter_enhanced", data_dir_names)

    async def asyncSetUp(self) -> None:
        temp_root = PLUGIN_ROOT / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.data_dir = Path(self.temp_dir.name) / "groups"
        self.data_dir.mkdir()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_init_registers_fixed_routes_and_terminate_reclaims_both_tasks(self):
        context = DummyContext()
        trend_started = asyncio.Event()
        standalone_started = asyncio.Event()
        standalone_stopped = asyncio.Event()

        async def fake_loop(plugin):
            trend_started.set()
            await asyncio.Event().wait()

        class FakeStandaloneWebService:
            async def run(self):
                standalone_started.set()
                await standalone_stopped.wait()

            async def stop(self):
                standalone_stopped.set()

        with (
            patch.object(MyPlugin, "_bar_data_loop", fake_loop),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.StandaloneWebService",
                FakeStandaloneWebService,
            ),
        ):
            plugin = MyPlugin(context)
            await asyncio.gather(trend_started.wait(), standalone_started.wait())
            self.assertEqual(len(context.registered_web_apis), 9)
            trend_task = plugin._trend_task
            standalone_task = plugin._standalone_task
            self.assertIsNotNone(trend_task)
            self.assertIsNotNone(standalone_task)
            self.assertFalse(trend_task.done())
            self.assertFalse(standalone_task.done())
            await plugin.terminate()

        self.assertIsNone(plugin._trend_task)
        self.assertIsNone(plugin._standalone_task)
        self.assertTrue(trend_task.cancelled())
        self.assertTrue(standalone_task.done())
        self.assertTrue(standalone_stopped.is_set())

    async def test_get_group_storage_rejects_invalid_ids_and_stays_in_data_dir(self):
        plugin = object.__new__(MyPlugin)

        with patch(
            "data.plugins.astrbot_zhouyi_plugin.main.StarTools.get_data_dir",
            return_value=self.data_dir,
        ):
            safe = await plugin.get_group_storage("12345")
            self.assertEqual(safe.group_id, "12345")
            self.assertEqual(safe.db_path, (self.data_dir / DATABASE_NAME).resolve())
            self.assertEqual(safe.db_path.parent, self.data_dir.resolve())
            self.assertEqual(await plugin.get_json_path("12345"), safe)

            for invalid in ("../escape", "bad.name", "", "a" * 129):
                with self.subTest(group_id=invalid):
                    with self.assertRaises(ValueError):
                        await plugin.get_group_storage(invalid)

    async def test_hourly_sampler_queries_shared_host_once_and_writes_both_groups(self):
        shared_host = "alpha.example:25565"
        storages = [
            get_group_storage(self.data_dir, "12345"),
            get_group_storage(self.data_dir, "67890"),
        ]
        for index, storage in enumerate(storages, start=1):
            data = default_config()
            data["next_id"] = 2
            data["servers"] = {
                "1": {
                    "id": 1,
                    "name": f"Alpha-{index}",
                    "host": shared_host,
                }
            }
            await write_json(storage, data)

        (self.data_dir / "bad.name.json").write_text("{}", encoding="utf-8")
        (self.data_dir / "broken.json").write_text("{broken", encoding="utf-8")
        status_calls = []

        async def fake_status(host):
            status_calls.append(host)
            return {"plays_online": 7}

        async def stop_sleep(seconds):
            raise StopLoop

        plugin = object.__new__(MyPlugin)
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.StarTools.get_data_dir",
                return_value=self.data_dir,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.asyncio.sleep",
                side_effect=stop_sleep,
            ),
        ):
            with self.assertRaises(StopLoop):
                await plugin._bar_data_loop()

        self.assertEqual(status_calls, [shared_host])
        for storage in storages:
            history = await get_trend_history(storage, "1", hours=24)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["count"], 7)


if __name__ == "__main__":
    unittest.main()
