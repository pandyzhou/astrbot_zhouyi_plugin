from __future__ import annotations

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

    async def test_get_json_path_rejects_traversal_and_symlink_escape(self):
        plugin = object.__new__(MyPlugin)
        outside = Path(self.temp_dir.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        symlink = self.data_dir / "linked.json"
        try:
            symlink.symlink_to(outside)
        except OSError:
            symlink = None

        with patch(
            "data.plugins.astrbot_zhouyi_plugin.main.StarTools.get_data_dir",
            return_value=self.data_dir,
        ):
            safe = await plugin.get_json_path("12345")
            self.assertEqual(safe, (self.data_dir / "12345.json").resolve())
            self.assertEqual(safe.parent, self.data_dir.resolve())

            for invalid in ("../escape", "bad.name", "", "a" * 129):
                with self.subTest(group_id=invalid):
                    with self.assertRaises(ValueError):
                        await plugin.get_json_path(invalid)

            if symlink is not None:
                with self.assertRaises(ValueError):
                    await plugin.get_json_path("linked")

    async def test_hourly_sampler_ignores_unsafe_files_and_writes_valid_trend(self):
        valid = self.data_dir / "12345.json"
        valid.write_text("{}", encoding="utf-8")
        (self.data_dir / "bad.name.json").write_text("{}", encoding="utf-8")
        outside = Path(self.temp_dir.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            (self.data_dir / "escaped.json").symlink_to(outside)
        except OSError:
            pass

        read_calls = []
        status_calls = []
        append_calls = []

        async def fake_read_json(path):
            read_calls.append(Path(path))
            return {"servers": {"1": {"host": "alpha.example:25565"}}}

        async def fake_status(host):
            status_calls.append(host)
            return {"plays_online": 7}

        async def fake_append(path, server_id, timestamp, count):
            append_calls.append((Path(path), server_id, timestamp, count))
            return True

        async def stop_sleep(seconds):
            raise StopLoop

        plugin = object.__new__(MyPlugin)
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.StarTools.get_data_dir",
                return_value=self.data_dir,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.read_json",
                side_effect=fake_read_json,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                side_effect=fake_append,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.asyncio.sleep",
                side_effect=stop_sleep,
            ),
        ):
            with self.assertRaises(StopLoop):
                await plugin._bar_data_loop()

        self.assertEqual(read_calls, [valid.resolve()])
        self.assertEqual(status_calls, ["alpha.example:25565"])
        self.assertEqual(len(append_calls), 1)
        self.assertEqual(append_calls[0][0], valid.resolve())
        self.assertEqual(append_calls[0][1], "1")
        self.assertEqual(append_calls[0][3], 7)


if __name__ == "__main__":
    unittest.main()
