from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.script.json_operate import (
    DATABASE_NAME,
    GroupStorage,
    default_config,
    get_group_storage,
    get_trend_history,
    write_json,
)
from data.plugins.astrbot_zhouyi_plugin.script.runtime_settings import (
    EffectiveRuntimeSettings,
    RuntimeSettings,
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


class DummyEvent:
    def get_group_id(self):
        return "12345"

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


async def collect_results(generator):
    return [item async for item in generator]


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
            self.assertEqual(len(context.registered_web_apis), 12)
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

    async def test_mc_batch_limits_concurrency_preserves_order_and_skips_disabled_cleanup(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        servers = {
            "3": {"id": 3, "name": "Gamma", "host": "gamma.example"},
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example"},
            "2": {"id": 2, "name": "Beta", "host": "beta.example"},
        }
        effective = EffectiveRuntimeSettings(
            group_id="12345",
            max_concurrent_queries=2,
            auto_cleanup_enabled=False,
            mc_lookup_timeout_seconds=1.5,
            mc_status_timeout_seconds=4.5,
        )
        active = 0
        max_active = 0
        received_settings = []

        async def fake_get_img(name, host, server_id, current_storage, *, settings=None):
            nonlocal active, max_active
            self.assertEqual(current_storage, storage)
            received_settings.append(settings)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep({"1": 0.03, "2": 0.01, "3": 0}[str(server_id)])
            active -= 1
            return f"image-{server_id}"

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.read_json",
                AsyncMock(return_value={"servers": servers}),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings",
                AsyncMock(return_value=effective),
            ),
            patch.object(MyPlugin, "get_img", side_effect=fake_get_img),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.auto_cleanup_servers",
                AsyncMock(),
            ) as cleanup,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64",
                side_effect=lambda value: value,
            ),
        ):
            results = await collect_results(plugin.mcgetter(event))

        self.assertEqual(max_active, 2)
        self.assertEqual(received_settings, [effective, effective, effective])
        self.assertEqual(results, [("chain", ["image-1", "image-2", "image-3"])])
        cleanup.assert_not_awaited()

    async def test_mcadd_and_get_img_use_effective_timeouts(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(
            group_id="12345",
            mc_lookup_timeout_seconds=1.25,
            mc_status_timeout_seconds=6.5,
            max_history_points=321,
        )
        calls = []

        async def fake_status(host, *, lookup_timeout, status_timeout):
            calls.append((host, lookup_timeout, status_timeout))
            return {
                "players_list": [],
                "latency": 1,
                "plays_max": 20,
                "plays_online": 4,
                "server_version": "test",
                "icon_base64": None,
                "host": host,
            }

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings",
                AsyncMock(return_value=effective),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.read_json",
                AsyncMock(return_value={"servers": {"1": {"name": "Alpha", "host": "alpha.example"}}}),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.add_data",
                AsyncMock(return_value=False),
            ),
        ):
            add_results = await collect_results(
                plugin.mcadd(event, "Beta", "beta.example")
            )

        self.assertEqual(add_results, [("plain", "无法添加 Beta，请检查是否已存在")])
        self.assertEqual(calls, [("beta.example", 1.25, 6.5)])

        calls.clear()
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.update_server_status",
                AsyncMock(return_value=True),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                AsyncMock(return_value=True),
            ) as append_point,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_server_info_image",
                AsyncMock(return_value="image-base64"),
            ),
        ):
            image = await plugin.get_img(
                "Alpha", "alpha.example", "1", storage, settings=effective
            )

        self.assertEqual(image, "image-base64")
        self.assertEqual(calls, [("alpha.example", 1.25, 6.5)])
        self.assertEqual(append_point.await_args.kwargs["max_history_points"], 321)

    async def test_mc_get_img_updates_success_without_trend_when_sampling_disabled(self):
        plugin = object.__new__(MyPlugin)
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(
            group_id="12345",
            trend_sampling_enabled=False,
        )
        status = {
            "players_list": [],
            "latency": 1,
            "plays_max": 20,
            "plays_online": 4,
            "server_version": "test",
            "icon_base64": None,
            "host": "alpha.example",
        }

        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                AsyncMock(return_value=status),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.update_server_status",
                AsyncMock(return_value=True),
            ) as update_status,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                AsyncMock(return_value=True),
            ) as append_point,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_server_info_image",
                AsyncMock(return_value="image-base64"),
            ),
        ):
            image = await plugin.get_img(
                "Alpha", "alpha.example", "1", storage, settings=effective
            )

        self.assertEqual(image, "image-base64")
        update_status.assert_awaited_once_with(storage, "1", True)
        append_point.assert_not_awaited()

    async def test_get_img_none_status_updates_once_and_returns_offline_card(self):
        plugin = object.__new__(MyPlugin)
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(group_id="12345")

        with (
            patch.object(
                MyPlugin,
                "_query_server_status",
                AsyncMock(return_value=None),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.update_server_status",
                AsyncMock(return_value=True),
            ) as update_status,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                AsyncMock(return_value=True),
            ) as append_point,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_server_info_image",
                AsyncMock(return_value="offline-image"),
            ) as generate_image,
        ):
            image = await plugin.get_img(
                "Alpha", "alpha.example:25565", "1", storage, settings=effective
            )

        self.assertEqual(image, "offline-image")
        update_status.assert_awaited_once_with(storage, "1", False)
        append_point.assert_not_awaited()
        kwargs = generate_image.await_args.kwargs
        self.assertFalse(kwargs["is_online"])
        self.assertEqual(kwargs["players_list"], [])
        self.assertIsNone(kwargs["latency"])
        self.assertEqual(kwargs["plays_online"], 0)
        self.assertEqual(kwargs["plays_max"], 0)
        self.assertEqual(kwargs["server_version"], "未知")
        self.assertEqual(kwargs["host_address"], "alpha.example:25565")

    async def test_get_img_query_exception_still_returns_offline_card_and_updates_once(self):
        plugin = object.__new__(MyPlugin)
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(group_id="12345")

        with (
            patch.object(
                MyPlugin,
                "_query_server_status",
                AsyncMock(side_effect=RuntimeError("lookup failed")),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.update_server_status",
                AsyncMock(return_value=True),
            ) as update_status,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_server_info_image",
                AsyncMock(return_value="offline-image"),
            ) as generate_image,
        ):
            image = await plugin.get_img(
                "Alpha", "alpha.example", "1", storage, settings=effective
            )

        self.assertEqual(image, "offline-image")
        update_status.assert_awaited_once_with(storage, "1", False)
        self.assertFalse(generate_image.await_args.kwargs["is_online"])

    async def test_get_img_render_failure_does_not_reverse_success_status(self):
        plugin = object.__new__(MyPlugin)
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(group_id="12345")
        status = {
            "players_list": ["Alice"],
            "latency": 12,
            "plays_max": 20,
            "plays_online": 1,
            "server_version": "1.21.4",
            "icon_base64": None,
            "host": "alpha.example",
        }

        with (
            patch.object(
                MyPlugin,
                "_query_server_status",
                AsyncMock(return_value=status),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.update_server_status",
                AsyncMock(return_value=True),
            ) as update_status,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                AsyncMock(return_value=True),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_server_info_image",
                AsyncMock(side_effect=RuntimeError("render failed")),
            ) as generate_image,
        ):
            image = await plugin.get_img(
                "Alpha", "alpha.example", "1", storage, settings=effective
            )

        self.assertIsNone(image)
        update_status.assert_awaited_once_with(storage, "1", True)
        self.assertTrue(generate_image.await_args.kwargs["is_online"])

    async def test_manual_cleanup_uses_dynamic_days_even_when_auto_cleanup_disabled(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(self.data_dir / DATABASE_NAME, "12345")
        effective = EffectiveRuntimeSettings(
            group_id="12345",
            auto_cleanup_enabled=False,
            auto_cleanup_days=37,
        )
        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings",
                AsyncMock(return_value=effective),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.auto_cleanup_servers",
                AsyncMock(return_value=[]),
            ) as cleanup,
        ):
            results = await collect_results(plugin.mccleanup(event))

        cleanup.assert_awaited_once_with(storage, cleanup_days=37)
        self.assertEqual(results, [("plain", "没有需要清理的服务器")])

    async def test_sampler_uses_effective_keys_global_limit_and_per_target_history_limit(self):
        plugin = object.__new__(MyPlugin)
        storages = [
            GroupStorage(self.data_dir / DATABASE_NAME, "12345"),
            GroupStorage(self.data_dir / DATABASE_NAME, "67890"),
            GroupStorage(self.data_dir / DATABASE_NAME, "disabled"),
        ]
        settings = {
            "12345": EffectiveRuntimeSettings(
                group_id="12345",
                mc_lookup_timeout_seconds=1.0,
                mc_status_timeout_seconds=5.0,
                max_history_points=200,
            ),
            "67890": EffectiveRuntimeSettings(
                group_id="67890",
                mc_lookup_timeout_seconds=2.0,
                mc_status_timeout_seconds=5.0,
                max_history_points=400,
            ),
            "disabled": EffectiveRuntimeSettings(
                group_id="disabled",
                trend_sampling_enabled=False,
            ),
        }
        servers = {
            "12345": {
                "1": {"host": "shared.example"},
                "2": {"host": "same-key.example"},
                "3": {"host": "shared.example"},
            },
            "67890": {
                "1": {"host": "shared.example"},
                "2": {"host": "same-key.example"},
            },
            "disabled": {"1": {"host": "disabled.example"}},
        }
        active = 0
        max_active = 0
        status_calls = []
        writes = []

        async def fake_effective(storage):
            return settings[storage.group_id]

        async def fake_servers(storage):
            return servers[storage.group_id]

        async def fake_status(host, *, lookup_timeout, status_timeout):
            nonlocal active, max_active
            status_calls.append((host, lookup_timeout, status_timeout))
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"plays_online": 7}

        async def fake_append(storage, sid, ts, count, *, max_history_points=None):
            writes.append((storage.group_id, sid, ts, count, max_history_points))
            return True

        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.list_group_storages",
                AsyncMock(return_value=storages),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_global_settings",
                AsyncMock(return_value=RuntimeSettings(max_concurrent_queries=1)),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings",
                side_effect=fake_effective,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_servers",
                side_effect=fake_servers,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.append_trend_point",
                side_effect=fake_append,
            ),
        ):
            await plugin._sample_trends_once(self.data_dir, 123456)

        self.assertEqual(max_active, 1)
        self.assertCountEqual(
            status_calls,
            [
                ("shared.example", 1.0, 5.0),
                ("same-key.example", 1.0, 5.0),
                ("shared.example", 2.0, 5.0),
                ("same-key.example", 2.0, 5.0),
            ],
        )
        self.assertNotIn("disabled.example", [call[0] for call in status_calls])
        self.assertCountEqual(
            writes,
            [
                ("12345", "1", 123456, 7, 200),
                ("12345", "2", 123456, 7, 200),
                ("12345", "3", 123456, 7, 200),
                ("67890", "1", 123456, 7, 400),
                ("67890", "2", 123456, 7, 400),
            ],
        )

    async def test_sampler_loop_retries_failed_bucket_and_notification_does_not_duplicate(self):
        plugin = object.__new__(MyPlugin)
        plugin._settings_changed_event = asyncio.Event()
        sample_calls = 0
        wait_calls = 0

        async def fake_sample(data_dir, now_ts):
            nonlocal sample_calls
            sample_calls += 1
            if sample_calls == 1:
                raise RuntimeError("temporary")

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            awaitable.close()
            if wait_calls == 1:
                return None
            if wait_calls == 2:
                plugin.notify_settings_changed()
                return None
            raise StopLoop

        with (
            patch.object(MyPlugin, "_sample_trends_once", side_effect=fake_sample),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.StarTools.get_data_dir",
                return_value=self.data_dir,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.asyncio.wait_for",
                side_effect=fake_wait_for,
            ),
        ):
            with self.assertRaises(StopLoop):
                await plugin._bar_data_loop()

        self.assertEqual(sample_calls, 2)


if __name__ == "__main__":
    unittest.main()
