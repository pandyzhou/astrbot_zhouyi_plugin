from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.script.json_operate import GroupStorage
from data.plugins.astrbot_zhouyi_plugin.script.runtime_settings import EffectiveRuntimeSettings


COMMAND_NOW = 1_735_819_200


class DummyEvent:
    def get_group_id(self):
        return "12345"

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


async def collect_results(generator):
    return [item async for item in generator]


def settings(**overrides):
    values = dict(
        group_id="12345",
        default_trend_hours=24,
        max_concurrent_queries=2,
        mc_lookup_timeout_seconds=1.5,
        mc_status_timeout_seconds=4.5,
    )
    values.update(overrides)
    return EffectiveRuntimeSettings(**values)


class McDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(MyPlugin)
        self.event = DummyEvent()
        self.storage = GroupStorage(PLUGIN_ROOT / "temp" / "mc_manager.sqlite3", "12345")
        self.background = Image.new("RGB", (800, 440), "navy")

    async def test_numeric_without_matching_id_is_hours_clamped_and_summary_keeps_order(self):
        servers = {
            "2": {"id": 2, "name": "Beta", "host": "beta.example:25565"},
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example:25565"},
        }
        histories = {"1": [], "2": [{"ts": 3600, "count": 8}]}

        async def status_for(host):
            return {"plays_online": 3} if host.startswith("alpha") else None

        captured = []

        def summary(inputs, **kwargs):
            captured.extend(inputs)
            self.assertEqual(kwargs["hours"], 168)
            self.assertIs(kwargs["background"], self.background)
            self.assertEqual(kwargs["now_ts"], COMMAND_NOW)
            return ["summary-base64"]

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=self.storage)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings", AsyncMock(return_value=settings())),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_servers", AsyncMock(return_value=servers)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_info", AsyncMock(return_value=None)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_trend_histories", AsyncMock(return_value=histories)) as get_histories,
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_status", AsyncMock(side_effect=status_for)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_card_background", AsyncMock(return_value=self.background)) as get_background,
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_summary_chart_images", side_effect=summary),
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_bar_chart_image") as detail,
            patch("data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64", return_value="image-component") as from_base64,
            patch("data.plugins.astrbot_zhouyi_plugin.main.time.time", return_value=COMMAND_NOW),
        ):
            results = await collect_results(self.plugin.mcdata(self.event, identifier="999"))

        get_histories.assert_awaited_once_with(self.storage, hours=168)
        get_background.assert_awaited_once()
        detail.assert_not_called()
        self.assertEqual([(item.id, item.name, item.history) for item in captured], [("1", "Alpha", [])])
        from_base64.assert_called_once_with("summary-base64")
        self.assertEqual(results, [("chain", ["image-component"])])

    async def test_numeric_matching_id_uses_detail_contract_and_one_background(self):
        server = {"id": 1, "name": "Alpha", "host": "alpha.example:25565"}
        history = [{"ts": 3600, "count": 5}]

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=self.storage)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings", AsyncMock(return_value=settings())),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_servers", AsyncMock(return_value={"1": server})),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_info", AsyncMock(return_value=server)) as get_info,
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_status", AsyncMock(return_value={"plays_online": 999})),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_trend_history", AsyncMock(return_value=history)) as get_history,
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_card_background", AsyncMock(return_value=self.background)) as get_background,
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_bar_chart_image", return_value="single-chart") as detail,
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_summary_chart_images") as summary,
            patch("data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64", return_value="single-image"),
            patch("data.plugins.astrbot_zhouyi_plugin.main.time.time", return_value=COMMAND_NOW),
        ):
            results = await collect_results(self.plugin.mcdata(self.event, identifier="1", hours=0))

        self.assertEqual(get_info.await_count, 2)
        get_history.assert_awaited_once_with(self.storage, "1", hours=1)
        get_background.assert_awaited_once()
        detail.assert_called_once_with(
            history,
            "Alpha",
            hours=1,
            background=self.background,
            now_ts=COMMAND_NOW,
        )
        summary.assert_not_called()
        self.assertEqual(results, [("chain", ["single-image"])])

    async def test_unreachable_single_does_not_fetch_background_or_history(self):
        server = {"id": 1, "name": "Alpha", "host": "alpha.example"}
        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=self.storage)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings", AsyncMock(return_value=settings())),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_servers", AsyncMock(return_value={"1": server})),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_info", AsyncMock(return_value=server)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_status", AsyncMock(return_value=None)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_trend_history", AsyncMock()) as get_history,
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_card_background", AsyncMock()) as get_background,
        ):
            results = await collect_results(self.plugin.mcdata(self.event, identifier="Alpha"))
        get_history.assert_not_awaited()
        get_background.assert_not_awaited()
        self.assertEqual(results, [("plain", "Alpha 当前不可达，已跳过")])

    async def test_all_servers_use_dynamic_defaults_timeouts_concurrency_order_and_empty_history(self):
        servers = {
            "3": {"id": 3, "name": "Gamma", "host": "gamma.example"},
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example"},
            "2": {"id": 2, "name": "Beta", "host": "beta.example"},
        }
        histories = {
            "1": [{"ts": 1, "count": 1}],
            "2": [{"ts": 2, "count": 2}],
            "3": [],
        }
        effective = settings(default_trend_hours=36)
        active = 0
        max_active = 0
        calls = []

        async def fake_status(host, *, lookup_timeout, status_timeout):
            nonlocal active, max_active
            calls.append((host, lookup_timeout, status_timeout))
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep({"alpha.example": 0.03, "beta.example": 0.01, "gamma.example": 0}[host])
                if host == "beta.example":
                    raise RuntimeError("probe failed")
                return {"plays_online": 777}
            finally:
                active -= 1

        captured = []

        def summary(inputs, **kwargs):
            captured.extend(inputs)
            self.assertEqual(kwargs["hours"], 36)
            self.assertEqual(kwargs["now_ts"], COMMAND_NOW)
            return ["page-1", "page-2"]

        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=self.storage)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings", AsyncMock(return_value=effective)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_servers", AsyncMock(return_value=servers)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_trend_histories", AsyncMock(return_value=histories)) as get_histories,
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_status", side_effect=fake_status),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_card_background", AsyncMock(return_value=self.background)) as get_background,
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_summary_chart_images", side_effect=summary),
            patch("data.plugins.astrbot_zhouyi_plugin.main.Comp.Image.fromBase64", side_effect=lambda value: value),
            patch("data.plugins.astrbot_zhouyi_plugin.main.time.time", return_value=COMMAND_NOW),
        ):
            results = await collect_results(self.plugin.mcdata(self.event))

        get_histories.assert_awaited_once_with(self.storage, hours=36)
        get_background.assert_awaited_once()
        self.assertEqual(max_active, 2)
        self.assertCountEqual(
            calls,
            [
                ("alpha.example", 1.5, 4.5),
                ("beta.example", 1.5, 4.5),
                ("gamma.example", 1.5, 4.5),
            ],
        )
        self.assertEqual([(item.id, item.name, item.history) for item in captured], [("1", "Alpha", histories["1"]), ("3", "Gamma", [])])
        self.assertEqual(results, [("chain", ["page-1", "page-2"])])

    async def test_all_unreachable_returns_text_without_background(self):
        servers = {
            "1": {"id": 1, "name": "Alpha", "host": "alpha.example"},
            "2": {"id": 2, "name": "Beta", "host": "beta.example"},
        }
        with (
            patch.object(MyPlugin, "get_group_storage", AsyncMock(return_value=self.storage)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_effective_settings", AsyncMock(return_value=settings())),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_servers", AsyncMock(return_value=servers)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_all_trend_histories", AsyncMock(return_value={})),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_server_status", AsyncMock(return_value=None)),
            patch("data.plugins.astrbot_zhouyi_plugin.main.get_card_background", AsyncMock()) as get_background,
            patch("data.plugins.astrbot_zhouyi_plugin.main.generate_summary_chart_images") as summary,
        ):
            results = await collect_results(self.plugin.mcdata(self.event))
        get_background.assert_not_awaited()
        summary.assert_not_called()
        self.assertEqual(results, [("plain", "所有服务器当前均不可达，已跳过")])


if __name__ == "__main__":
    unittest.main()
