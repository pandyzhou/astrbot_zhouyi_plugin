from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin import main
from data.plugins.astrbot_zhouyi_plugin.main import MyPlugin
from data.plugins.astrbot_zhouyi_plugin.script import mcmod_search
from astrbot.core.provider.func_tool_manager import FunctionToolManager


SUCCESS_HTML = """
<div class="search-result-list">
  <div class="result-item">
    <div class="head">
      <a href="https://evil.example/class/1.html">无效链接</a>
      <a href="/class/2021.html">(模组) 机械动力</a>
    </div>
    <div class="body">[ban:test] 机械 动力\n 自动化 </div>
  </div>
  <div class="result-item">
    <div class="head"><a href="https://mcmod.cn/post/9.html">教程条目</a></div>
    <div class="body">教程摘要</div>
  </div>
</div>
"""


class FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body

    async def text(self, *, errors: str) -> str:
        if errors != "replace":
            raise AssertionError("response.text 必须使用 errors='replace'")
        return self.body


class FakeRequestContext:
    def __init__(self, action) -> None:
        self.action = action

    async def __aenter__(self):
        if isinstance(self.action, BaseException):
            raise self.action
        return self.action

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeSession:
    def __init__(self, actions: list) -> None:
        self.actions = list(actions)
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def get(self, url: str, *, params: dict):
        self.get_calls.append((url, params))
        if not self.actions:
            raise AssertionError("没有可用的模拟响应")
        return FakeRequestContext(self.actions.pop(0))


class FakeSessionFactory:
    def __init__(self, actions: list) -> None:
        self.session = FakeSession(actions)
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.session


class ArgumentAndParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_argument_boundaries_and_normalization(self):
        normalized = mcmod_search._validate_arguments(" 机械动力 ", " MOD ", 1, 10)
        self.assertEqual(normalized, ("机械动力", "mod", None))
        self.assertEqual(
            mcmod_search._validate_arguments("甲" * 100, "all", 20, 1),
            ("甲" * 100, "all", None),
        )

        invalid_cases = [
            ("", "all", 1, 5),
            ("甲" * 101, "all", 1, 5),
            (123, "all", 1, 5),
            ("query", "other", 1, 5),
            ("query", 1, 1, 5),
            ("query", "all", 0, 5),
            ("query", "all", 21, 5),
            ("query", "all", 1, 0),
            ("query", "all", 1, 11),
        ]
        for args in invalid_cases:
            with self.subTest(args=args):
                self.assertIsNotNone(mcmod_search._validate_arguments(*args)[2])

    def test_bool_is_rejected_for_integer_arguments(self):
        self.assertIsNotNone(
            mcmod_search._validate_arguments("query", "all", True, 5)[2]
        )
        self.assertIsNotNone(
            mcmod_search._validate_arguments("query", "all", 1, False)[2]
        )

    async def test_invalid_argument_response_has_complete_terminal_fields(self):
        result = await mcmod_search.search_mcmod(" ", "all", 1, 5)
        self.assertEqual(result["status"], "invalid_argument")
        self.assertEqual(result["query"], " ")
        self.assertEqual(result["category"], "all")
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["limit"], 5)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        self.assertIn("error", result)

    def test_clean_text_removes_markup_whitespace_and_truncates(self):
        self.assertEqual(
            mcmod_search._clean_text(" [ban:x] 机械  动力\n[h1=标题] 测试 "),
            "机械动力测试",
        )
        self.assertEqual(mcmod_search._clean_text("a" * 501, 500), "a" * 500)

    def test_result_url_whitelist(self):
        valid = [
            "https://mcmod.cn/class/1.html",
            "http://www.mcmod.cn/modpack/22.html",
            "https://www.mcmod.cn/item/333.html",
            "https://mcmod.cn/post/4.html",
        ]
        invalid = [
            "https://search.mcmod.cn/class/1.html",
            "https://evil.example/class/1.html",
            "javascript:alert(1)",
            "https://www.mcmod.cn/class/not-number.html",
            "https://www.mcmod.cn/class/1.html/extra",
            "https://www.mcmod.cn/class/1.html?q=1",
            "https://www.mcmod.cn/class/1.html#part",
            "https://www.mcmod.cn:443/class/1.html",
        ]
        for url in valid:
            with self.subTest(url=url):
                self.assertTrue(mcmod_search._is_valid_result_url(url))
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(mcmod_search._is_valid_result_url(url))

    def test_result_type_prefers_title_prefix_then_path(self):
        self.assertEqual(
            mcmod_search._result_type("(教程) 入门", "https://mcmod.cn/class/1.html"),
            "tutorial",
        )
        self.assertEqual(
            mcmod_search._result_type("普通标题", "https://mcmod.cn/modpack/1.html"),
            "modpack",
        )
        self.assertEqual(
            mcmod_search._result_type("普通标题", "https://mcmod.cn/item/1.html"),
            "item",
        )
        self.assertEqual(
            mcmod_search._result_type("普通标题", "https://mcmod.cn/post/1.html"),
            "tutorial",
        )

    def test_parse_success_uses_first_valid_head_link(self):
        status, results = mcmod_search.parse_search_html(SUCCESS_HTML, 5)
        self.assertEqual(status, "success")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "(模组) 机械动力")
        self.assertEqual(results[0]["url"], "https://www.mcmod.cn/class/2021.html")
        self.assertEqual(results[0]["summary"], "机械动力自动化")
        self.assertEqual(results[0]["type"], "mod")
        self.assertEqual(results[1]["type"], "tutorial")

    def test_parse_empty_missing_container_bad_nodes_and_limit(self):
        self.assertEqual(
            mcmod_search.parse_search_html('<div class="search-result-list"></div>', 5),
            ("empty", []),
        )
        self.assertEqual(
            mcmod_search.parse_search_html("<html></html>", 5),
            ("parse_error", []),
        )
        bad_html = """
        <div class="search-result-list">
          <div class="result-item"><div class="body">缺少 head</div></div>
          <div class="result-item"><div class="head"><a href="/class/x.html">坏链接</a></div></div>
        </div>
        """
        self.assertEqual(
            mcmod_search.parse_search_html(bad_html, 5),
            ("parse_error", []),
        )
        status, results = mcmod_search.parse_search_html(SUCCESS_HTML, 1)
        self.assertEqual(status, "success")
        self.assertEqual(len(results), 1)

    async def test_search_builds_filter_params_and_complete_success_response(self):
        with patch.object(
            mcmod_search,
            "_fetch_search_html",
            AsyncMock(return_value=("success", SUCCESS_HTML)),
        ) as fetch:
            result = await mcmod_search.search_mcmod(" 机械动力 ", "MOD", 2, 1)

        fetch.assert_awaited_once_with({"key": "机械动力", "filter": 1, "page": 2})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["query"], "机械动力")
        self.assertEqual(result["category"], "mod")
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["results"]), 1)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, actions: list):
        factory = FakeSessionFactory(actions)
        with patch.object(mcmod_search.asyncio, "sleep", AsyncMock()) as sleep:
            result = await mcmod_search._fetch_search_html(
                {"key": "机械动力", "filter": 1, "page": 1},
                session_factory=factory,
            )
        return result, factory, sleep

    async def test_200_success_and_session_contract(self):
        result, factory, sleep = await self.fetch([FakeResponse(200, SUCCESS_HTML)])
        self.assertEqual(result, ("success", SUCCESS_HTML))
        self.assertEqual(len(factory.session.get_calls), 1)
        self.assertEqual(factory.session.get_calls[0][0], mcmod_search.SEARCH_URL)
        self.assertTrue(factory.kwargs["trust_env"])
        self.assertIn("Mozilla/5.0", factory.kwargs["headers"]["User-Agent"])
        self.assertEqual(factory.kwargs["headers"]["Accept-Language"], "zh-CN,zh;q=0.9")
        timeout = factory.kwargs["timeout"]
        self.assertEqual(timeout.total, 10)
        self.assertEqual(timeout.connect, 4)
        self.assertEqual(timeout.sock_read, 8)
        sleep.assert_not_awaited()

    async def test_429_does_not_retry(self):
        result, factory, sleep = await self.fetch([FakeResponse(429)])
        self.assertEqual(result, ("rate_limited", None))
        self.assertEqual(len(factory.session.get_calls), 1)
        sleep.assert_not_awaited()

    async def test_502_retries_once_then_succeeds(self):
        result, factory, sleep = await self.fetch(
            [FakeResponse(502), FakeResponse(200, SUCCESS_HTML)]
        )
        self.assertEqual(result, ("success", SUCCESS_HTML))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_503_twice_returns_upstream_error(self):
        result, factory, sleep = await self.fetch([FakeResponse(503), FakeResponse(503)])
        self.assertEqual(result, ("upstream_error", None))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_connection_failure_retries_then_succeeds(self):
        result, factory, sleep = await self.fetch(
            [aiohttp.ClientConnectionError("first"), FakeResponse(200, SUCCESS_HTML)]
        )
        self.assertEqual(result, ("success", SUCCESS_HTML))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_two_connection_failures_return_upstream_error(self):
        result, factory, sleep = await self.fetch(
            [aiohttp.ClientConnectionError("first"), aiohttp.ClientConnectionError("second")]
        )
        self.assertEqual(result, ("upstream_error", None))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_timeout_does_not_retry(self):
        result, factory, sleep = await self.fetch([asyncio.TimeoutError()])
        self.assertEqual(result, ("timeout", None))
        self.assertEqual(len(factory.session.get_calls), 1)
        sleep.assert_not_awaited()


class FakePluginContext:
    def __init__(self, manager: FunctionToolManager, own_metadata) -> None:
        self.manager = manager
        self.own_metadata = own_metadata

    def get_registered_star(self, star_name: str):
        if star_name == "astrbot_zhouyi_plugin":
            return self.own_metadata
        return None

    def get_llm_tool_manager(self) -> FunctionToolManager:
        return self.manager


class PluginToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FunctionToolManager()
        self.own_metadata = SimpleNamespace(
            root_dir_name="astrbot_zhouyi_plugin",
            activated=True,
        )
        self.plugin = object.__new__(MyPlugin)
        self.plugin.context = FakePluginContext(self.manager, self.own_metadata)

    async def old_handler(self, event, query, category="all", page=1, limit=5):
        return "old"

    def add_old_tool(self, *, active: bool = True) -> None:
        self.manager.add_func("mcmod_search", [], "old", self.old_handler)
        tool = self.manager.get_func("mcmod_search")
        tool.handler_module_path = "data.plugins.mcmod_card.main"
        tool.active = active

    async def trigger(self, root_dir_name: str) -> None:
        await self.plugin._register_mcmod_search_tool(
            SimpleNamespace(root_dir_name=root_dir_name)
        )

    def assert_merged_tool(self, *, active: bool = True) -> None:
        tools = [
            tool for tool in self.manager.func_list if tool.name == "mcmod_search"
        ]
        self.assertEqual(len(tools), 1)
        tool = tools[0]
        self.assertEqual(tool.handler_module_path, main.__name__)
        self.assertIs(tool.handler.__self__, self.plugin)
        self.assertIs(tool.handler.__func__, MyPlugin.mcmod_search)
        self.assertIs(tool.active, active)

    async def test_mcmod_then_own_load_registers_single_merged_tool(self):
        self.add_old_tool()

        await self.trigger("astrbot_zhouyi_plugin")

        self.assert_merged_tool()

    async def test_own_then_mcmod_load_restores_after_old_plugin_overwrite(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.add_old_tool()
        overwritten = self.manager.get_func("mcmod_search")
        self.assertEqual(overwritten.handler_module_path, "data.plugins.mcmod_card.main")

        await self.trigger("mcmod_card")

        self.assert_merged_tool()

    async def test_inactive_own_plugin_does_not_restore_tool(self):
        self.own_metadata.activated = False
        self.add_old_tool()

        await self.trigger("mcmod_card")

        tool = self.manager.get_func("mcmod_search")
        self.assertEqual(tool.handler_module_path, "data.plugins.mcmod_card.main")
        self.assertEqual(len(self.manager.func_list), 1)

    async def test_missing_own_metadata_does_not_register(self):
        self.plugin.context.own_metadata = None

        await self.trigger("astrbot_zhouyi_plugin")

        self.assertEqual(self.manager.func_list, [])

    async def test_manual_inactive_state_is_preserved_for_own_tool(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.manager.get_func("mcmod_search").active = False

        await self.trigger("astrbot_zhouyi_plugin")

        self.assert_merged_tool(active=False)

    async def test_inactive_old_tool_is_replaced_as_active(self):
        self.add_old_tool(active=False)

        await self.trigger("mcmod_card")

        self.assert_merged_tool(active=True)

    async def test_unrelated_plugin_load_does_not_replace_old_tool(self):
        self.add_old_tool()

        await self.trigger("other_plugin")

        tool = self.manager.get_func("mcmod_search")
        self.assertEqual(tool.handler_module_path, "data.plugins.mcmod_card.main")
        self.assertEqual(len(self.manager.func_list), 1)


class PluginToolContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(MyPlugin)
        self.handler = getattr(MyPlugin.mcmod_search, "__wrapped__", MyPlugin.mcmod_search)

    async def test_tool_returns_unescaped_chinese_json(self):
        expected = {
            "status": "success",
            "query": "机械动力",
            "category": "mod",
            "page": 1,
            "limit": 2,
            "count": 1,
            "results": [
                {
                    "title": "机械动力",
                    "url": "https://www.mcmod.cn/class/2021.html",
                    "summary": "中文摘要",
                    "type": "mod",
                }
            ],
        }
        with patch.object(main, "search_mcmod", AsyncMock(return_value=expected)) as search:
            payload = await self.handler(self.plugin, None, "机械动力", "mod", 1, 2)

        search.assert_awaited_once_with("机械动力", "mod", 1, 2)
        self.assertNotIn("\\u", payload)
        self.assertEqual(json.loads(payload), expected)

    async def test_tool_exception_returns_complete_upstream_error(self):
        with patch.object(
            main,
            "search_mcmod",
            AsyncMock(side_effect=RuntimeError("sensitive upstream text")),
        ):
            payload = await self.handler(self.plugin, None, "机械动力", "mod", 3, 4)

        self.assertEqual(
            json.loads(payload),
            {
                "status": "upstream_error",
                "query": "机械动力",
                "category": "mod",
                "page": 3,
                "limit": 4,
                "count": 0,
                "results": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
