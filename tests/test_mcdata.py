from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.script.json_operate import GroupStorage
from data.plugins.astrbot_zhouyi_plugin.script.runtime_settings import EffectiveRuntimeSettings


class DummyEvent:
    def get_group_id(self):
        return "12345"

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


async def collect_results(generator):
    return [item async for item in generator]


class McDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_without_matching_id_is_hours_clamped_and_unreachable_skipped(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(PLUGIN_ROOT / "temp" / "mc_manager.sqlite3", "12345")
        servers = {
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example:25565"},
            "2": {"id": 2, "name": "Beta", "host": "beta.example:25565"},
        }
        histories = {
            "1": [{"ts": 3600, "count": 3}],
            "2": [{"ts": 3600, "count": 8}],
        }

        async def status_for(host):
            return {"plays_online": 3} if host.startswith("alpha") else None

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_servers",
                AsyncMock(return_value=servers),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_info",
                AsyncMock(return_value=None),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_trend_histories",
                AsyncMock(return_value=histories),
            ) as get_histories,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                AsyncMock(side_effect=status_for),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_bar_chart_image",
                return_value="chart-base64",
            ) as generate_chart,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64",
                return_value="image-component",
            ) as from_base64,
        ):
            results = await collect_results(plugin.mcdata(event, identifier="999"))

        get_histories.assert_awaited_once_with(storage, hours=168)
        generate_chart.assert_called_once_with(histories["1"], "Alpha", hours=168)
        from_base64.assert_called_once_with("chart-base64")
        self.assertEqual(results, [("chain", ["image-component"])])

    async def test_numeric_matching_id_stays_identifier_and_hours_clamps_to_one(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(PLUGIN_ROOT / "temp" / "mc_manager.sqlite3", "12345")
        server = {"id": 1, "name": "Alpha", "host": "alpha.example:25565"}
        history = [{"ts": 3600, "count": 5}]

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_servers",
                AsyncMock(return_value={"1": server}),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_info",
                AsyncMock(return_value=server),
            ) as get_info,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                AsyncMock(return_value={"plays_online": 5}),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_trend_history",
                AsyncMock(return_value=history),
            ) as get_history,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_bar_chart_image",
                return_value="single-chart",
            ) as generate_chart,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64",
                return_value="single-image",
            ),
        ):
            results = await collect_results(plugin.mcdata(event, identifier="1", hours=0))

        self.assertEqual(get_info.await_count, 2)
        get_history.assert_awaited_once_with(storage, "1", hours=1)
        generate_chart.assert_called_once_with(history, "Alpha", hours=1)
        self.assertEqual(results, [("chain", ["single-image"])])

    async def test_all_servers_use_dynamic_defaults_timeouts_concurrency_and_order(self):
        plugin = object.__new__(MyPlugin)
        event = DummyEvent()
        storage = GroupStorage(PLUGIN_ROOT / "temp" / "mc_manager.sqlite3", "12345")
        servers = {
            "3": {"id": 3, "name": "Gamma", "host": "gamma.example"},
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example"},
            "2": {"id": 2, "name": "Beta", "host": "beta.example"},
        }
        histories = {
            "1": [{"ts": 1, "count": 1}],
            "2": [{"ts": 2, "count": 2}],
            "3": [{"ts": 3, "count": 3}],
        }
        effective = EffectiveRuntimeSettings(
            group_id="12345",
            default_trend_hours=36,
            max_concurrent_queries=2,
            mc_lookup_timeout_seconds=1.5,
            mc_status_timeout_seconds=4.5,
        )
        active = 0
        max_active = 0
        calls = []

        async def fake_status(host, *, lookup_timeout, status_timeout):
            nonlocal active, max_active
            calls.append((host, lookup_timeout, status_timeout))
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(
                    {"alpha.example": 0.03, "beta.example": 0.01, "gamma.example": 0}[host]
                )
                if host == "beta.example":
                    raise RuntimeError("probe failed")
                return {"plays_online": 1}
            finally:
                active -= 1

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=storage)),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings",
                AsyncMock(return_value=effective),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_servers",
                AsyncMock(return_value=servers),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_all_trend_histories",
                AsyncMock(return_value=histories),
            ) as get_histories,
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.get_server_status",
                side_effect=fake_status,
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.generate_bar_chart_image",
                side_effect=lambda history, name, *, hours: f"{name}-{hours}",
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64",
                side_effect=lambda value: value,
            ),
        ):
            results = await collect_results(plugin.mcdata(event))

        get_histories.assert_awaited_once_with(storage, hours=36)
        self.assertEqual(max_active, 2)
        self.assertCountEqual(
            calls,
            [
                ("alpha.example", 1.5, 4.5),
                ("beta.example", 1.5, 4.5),
                ("gamma.example", 1.5, 4.5),
            ],
        )
        self.assertEqual(
            results,
            [("chain", ["Alpha-36", "Gamma-36"])],
        )


if __name__ == "__main__":
    unittest.main()
