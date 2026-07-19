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

DETAIL_URL = "https://www.mcmod.cn/item/137201.html"
REPLY_INSTRUCTION = mcmod_search.QQ_PLAIN_TEXT_REPLY_INSTRUCTION
DETAIL_HTML = """
<div class="itemname"><div class="name"><h5>命令方块</h5></div></div>
<div class="common-nav">
  <a href="/class/category/1.html">分类导航</a>
  <a href="https://mcmod.cn/class/1.html">Minecraft 原版</a>
</div>
<div class="item-give"><span>/give @p minecraft:command_block</span></div>
<table class="group-2">
  <tr><td>中文名：</td><td>命令方块</td></tr>
  <tr><td>英文名:</td><td>Command Block</td></tr>
  <tr><td>中文名：</td><td>重复值</td></tr>
</table>
<div class="item-info">
  <img src="//i.mcmod.cn/item/icon/128x128/0.png">
</div>
<div class="item-content common-text">
  <h2>物品介绍</h2>
  <p>命令方块是一种用于执行命令的方块。</p>
  <p>命令方块是一种用于执行命令的方块。</p>
  普通文本第一行<br>普通文本第二行
  <div class="uknowtoomuch">隐藏内容不得出现</div>
  <div class="content-tools">工具内容不得出现</div>
  <script>恶意脚本</script><style>.x{}</style><noscript>备用内容</noscript>
  <img src="/not-intro.png" alt="图片文本不得出现">
  <ul><li>可通过红石或始终活动触发。</li></ul>
</div>
"""

RECIPE_DETAIL_HTML = """
<div class="itemname"><div class="name"><h5>水车</h5></div></div>
<div class="item-content common-text"><p>水车介绍。</p></div>
<table class="table table-bordered item-table-block item-table-block-out">
  <tr><th>材料统计</th><th>输入 &gt;&gt; 输出</th><th>备注</th></tr>
  <tr>
    <td class="text item-table-count">
      <p>[使用: <a href="/item/52.html">工作台</a>]</p><br/>
      <p><a href="/oredict/minecraft:planks-1.html">标签: minecraft:planks</a> * 8</p>
      <p><a href="/item/196521.html">传动杆</a> * 1</p>
      <p>↓</p>
      <p><a href="/item/196531.html">水车</a> * 1</p>
    </td>
    <td class="text item-table-gui">
      <div class="TableBlock" style="background-image:url(//i.mcmod.cn/gui/bg/1.gif);">
        <div class="common-oredict-loop">
          <div class="item-table-hover" style="margin:34px 0 0 46px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
          <div class="item-table-hover" style="margin:34px 0 0 46px;"><a href="/oredict/minecraft:planks-1.html"><img alt="云杉木板" src="//i.mcmod.cn/item/icon/32x32/1/10967.png"/></a></div>
        </div>
        <div class="item-table-hover" style="margin:34px 0 0 82px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:34px 0 0 118px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:70px 0 0 46px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:70px 0 0 82px;"><a href="/item/196521.html"><img alt="传动杆" src="//i.mcmod.cn/item/icon/32x32/19/196521.png"/></a></div>
        <div class="item-table-hover" style="margin:70px 0 0 118px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:106px 0 0 46px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:106px 0 0 82px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:106px 0 0 118px;"><a href="/oredict/minecraft:planks-1.html"><img alt="橡木木板" src="//i.mcmod.cn/item/icon/32x32/0/40.png"/></a></div>
        <div class="item-table-hover" style="margin:70px 0 0 234px;"><a href="/item/196531.html"><img alt="水车" src="//i.mcmod.cn/item/icon/32x32/19/196531.png"/></a></div>
      </div>
    </td>
    <td class="text item-table-remarks"><span class="alert alert-table-startver">需要 v0.5.1 或更高版本</span></td>
  </tr>
</table>
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
        self.redirect_options = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        allow_redirects: bool = True,
    ):
        self.get_calls.append((url, params))
        self.redirect_options.append(allow_redirects)
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


class FakeEvent:
    def __init__(self, message_str: str = "") -> None:
        self.extra = {}
        self.message_str = message_str
        self.result = None

    def set_extra(self, key, value) -> None:
        self.extra[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extra
        return self.extra.get(key, default)

    def get_result(self):
        return self.result


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

    def test_reply_instruction_requires_question_scoped_minimal_answer(self):
        instruction = mcmod_search.QQ_PLAIN_TEXT_REPLY_INSTRUCTION

        self.assertIn("严格只回答用户明确询问的内容", instruction)
        self.assertIn("只问制作或合成方法", instruction)
        self.assertIn("禁止补充注册名、物品命令、最大堆叠、资料分类", instruction)
        self.assertIn("怎么用、用途、性能、外观", instruction)
        self.assertIn("默认正文最多 2 个短段", instruction)
        self.assertIn("第一句直接给答案", instruction)
        self.assertIn("禁止寒暄、称呼用户、确认问题或描述查询过程", instruction)
        self.assertIn("不要添加重复总结", instruction)
        self.assertNotIn("一句话总结", instruction)

    def test_crafting_only_filter_removes_unasked_sections_and_filler(self):
        source = """唔…
【水车制作】
在工作台里，把 1 个传动杆放中间，周围放 8 个木板，得到 1 个水车。
【怎么用】
1. 放进水流里驱动。
2. 产生 256 su 和 8 RPM。
【小提示】
可以更换木纹外观。
一句话总结：水车依靠水流工作。
如果需要准确配方，可以去游戏内 JEI 查看会更靠谱。
来源：https://www.mcmod.cn/item/196531.html"""

        filtered = mcmod_search.format_mcmod_crafting_only_reply(source)

        self.assertEqual(
            filtered,
            "【水车制作】\n"
            "在工作台里，把 1 个传动杆放中间，周围放 8 个木板，得到 1 个水车。\n"
            "来源：https://www.mcmod.cn/item/196531.html",
        )

    def test_crafting_only_filter_removes_address_and_false_uncertainty(self):
        filtered = mcmod_search.format_mcmod_crafting_only_reply(
            "主人，详情页只写了工作台合成。\n"
            "所以星瑶没法确定材料和产物数量。\n"
            "来源：https://www.mcmod.cn/item/196531.html"
        )

        self.assertEqual(
            filtered,
            "详情页只写了工作台合成。\n"
            "来源：https://www.mcmod.cn/item/196531.html",
        )

    def test_crafting_only_filter_keeps_shapeless_recipe_without_fixed_grid(self):
        source = (
            "这是无序合成，没有固定摆放方式；材料：1 个木板和 1 个铁锭，产物：1 个组件。\n"
            "来源：https://www.mcmod.cn/item/123.html"
        )

        self.assertEqual(mcmod_search.format_mcmod_crafting_only_reply(source), source)

    def test_format_mcmod_qq_plain_text_converts_common_markdown(self):
        source = """## **黄铜机壳**
- 注册名：`create:brass_casing`
+ 可用于机器外壳
1) 使用扳手获取
> __注意事项__



| 属性 | 内容 |
| --- | :---: |
| 来源 | [MC百科](https://www.mcmod.cn/item/123.html) |
---
```text
# 保留代码注释
补充说明
```
***"""

        formatted = mcmod_search.format_mcmod_qq_plain_text(source)

        self.assertEqual(
            formatted,
            "【黄铜机壳】\n"
            "· 注册名：create:brass_casing\n"
            "· 可用于机器外壳\n"
            "1. 使用扳手获取\n"
            "注意事项\n\n"
            "属性｜内容\n"
            "来源｜MC百科：https://www.mcmod.cn/item/123.html\n"
            "# 保留代码注释\n"
            "补充说明",
        )
        for marker in ("##", "**", "__", "`", "---", "***"):
            self.assertNotIn(marker, formatted)
        self.assertIn("【黄铜机壳】", formatted)
        self.assertIn("· 注册名：create:brass_casing", formatted)
        self.assertIn("1. 使用扳手获取", formatted)
        self.assertIn("https://www.mcmod.cn/item/123.html", formatted)
        self.assertEqual(
            mcmod_search.format_mcmod_qq_plain_text(
                "[](https://www.mcmod.cn/item/456.html)"
            ),
            "https://www.mcmod.cn/item/456.html",
        )
        self.assertEqual(mcmod_search.format_mcmod_qq_plain_text(""), "")

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

    def test_item_url_normalization_and_ssrf_rejection(self):
        valid = {
            "https://mcmod.cn/item/1.html": "https://www.mcmod.cn/item/1.html",
            "http://www.mcmod.cn/item/00123.html": "https://www.mcmod.cn/item/00123.html",
            "  https://www.mcmod.cn/item/9.html  ": "https://www.mcmod.cn/item/9.html",
        }
        invalid = [
            None,
            123,
            "",
            "ftp://www.mcmod.cn/item/1.html",
            "https://search.mcmod.cn/item/1.html",
            "https://evil.example/item/1.html",
            "https://www.mcmod.cn.evil.example/item/1.html",
            "https://www.mcmod.cn@evil.example/item/1.html",
            "https://evil.example@www.mcmod.cn/item/1.html",
            "https://www.mcmod.cn:443/item/1.html",
            "https://127.0.0.1/item/1.html",
            "https://[::1]/item/1.html",
            "https://www.mcmod.cn/item/not-number.html",
            "https://www.mcmod.cn/item/1.html/extra",
            "https://www.mcmod.cn/item/1.html;param",
            "https://www.mcmod.cn/item/1.html?q=1",
            "https://www.mcmod.cn/item/1.html#intro",
            "//www.mcmod.cn/item/1.html",
        ]
        for url, expected in valid.items():
            with self.subTest(url=url):
                self.assertEqual(mcmod_search.normalize_mcmod_item_url(url), expected)
        for url in invalid:
            with self.subTest(url=url):
                self.assertIsNone(mcmod_search.normalize_mcmod_item_url(url))

    def test_parse_item_detail_extracts_expected_fields_and_removes_hidden_content(self):
        status, detail = mcmod_search.parse_item_detail_html(DETAIL_HTML, DETAIL_URL)

        self.assertEqual(status, "success")
        self.assertEqual(detail["title"], "命令方块")
        self.assertEqual(
            detail["mod"],
            {
                "name": "Minecraft 原版",
                "url": "https://www.mcmod.cn/class/1.html",
            },
        )
        self.assertEqual(detail["item_command"], "/give @p minecraft:command_block")
        self.assertEqual(
            detail["attributes"],
            {"中文名": "命令方块", "英文名": "Command Block"},
        )
        self.assertIn("物品介绍", detail["introduction"])
        self.assertIn("普通文本第一行", detail["introduction"])
        self.assertIn("普通文本第二行", detail["introduction"])
        self.assertIn("可通过红石或始终活动触发。", detail["introduction"])
        self.assertEqual(detail["introduction"].count("命令方块是一种用于执行命令的方块。"), 1)
        self.assertNotIn("隐藏内容", detail["introduction"])
        self.assertNotIn("工具内容", detail["introduction"])
        self.assertNotIn("恶意脚本", detail["introduction"])
        self.assertEqual(
            detail["icon_url"],
            "https://i.mcmod.cn/item/icon/128x128/0.png",
        )
        self.assertEqual(detail["source_url"], DETAIL_URL)
        self.assertEqual(detail["recipes"], [])

    def test_parse_item_detail_extracts_crafting_materials_grid_output_and_condition(self):
        status, detail = mcmod_search.parse_item_detail_html(
            RECIPE_DETAIL_HTML,
            "https://www.mcmod.cn/item/196531.html",
        )

        self.assertEqual(status, "success")
        self.assertEqual(len(detail["recipes"]), 1)
        recipe = detail["recipes"][0]
        self.assertEqual(recipe["method"], "工作台")
        self.assertEqual(
            recipe["materials"],
            [
                {
                    "name": "任意木板",
                    "count": 8,
                    "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html",
                    "icon_url": "https://i.mcmod.cn/item/icon/32x32/0/40.png",
                },
                {
                    "name": "传动杆",
                    "count": 1,
                    "source": "https://www.mcmod.cn/item/196521.html",
                    "icon_url": "https://i.mcmod.cn/item/icon/32x32/19/196521.png",
                },
            ],
        )
        self.assertEqual(
            recipe["grid"],
            [
                ["任意木板", "任意木板", "任意木板"],
                ["任意木板", "传动杆", "任意木板"],
                ["任意木板", "任意木板", "任意木板"],
            ],
        )
        self.assertEqual(
            recipe["grid_slots"][1][1],
            {
                "name": "传动杆",
                "source": "https://www.mcmod.cn/item/196521.html",
                "icon_url": "https://i.mcmod.cn/item/icon/32x32/19/196521.png",
            },
        )
        self.assertEqual(recipe["grid_slots"][0][0]["name"], "任意木板")
        self.assertEqual(
            recipe["grid_slots"][0][0]["icon_url"],
            "https://i.mcmod.cn/item/icon/32x32/0/40.png",
        )
        self.assertEqual(
            recipe["output"],
            {
                "name": "水车",
                "count": 1,
                "source": "https://www.mcmod.cn/item/196531.html",
                "icon_url": "https://i.mcmod.cn/item/icon/32x32/19/196531.png",
            },
        )
        self.assertEqual(recipe["conditions"], ["需要 v0.5.1 或更高版本"])
        self.assertEqual(recipe["availability"], "active")
        self.assertEqual(recipe["required_mods"], [])

    def test_recipe_parser_marks_removed_and_required_mods(self):
        removed_html = RECIPE_DETAIL_HTML.replace(
            '<span class="alert alert-table-startver">需要 v0.5.1 或更高版本</span>',
            '<span class="alert alert-table-endver">在 v0.5.1 中被移除</span>',
        )
        status, detail = mcmod_search.parse_item_detail_html(
            removed_html,
            "https://www.mcmod.cn/item/196531.html",
        )
        self.assertEqual(status, "success")
        self.assertEqual(detail["recipes"][0]["availability"], "removed")

        addon_html = RECIPE_DETAIL_HTML.replace(
            '<span class="alert alert-table-startver">需要 v0.5.1 或更高版本</span>',
            '<span class="alert alert-table-forother">需要安装 '
            '<a href="//www.mcmod.cn/class/5941.html">CreatePlus</a>, '
            '<a href="//www.mcmod.cn/class/558.html">科技复兴</a> 模组</span>',
        )
        status, detail = mcmod_search.parse_item_detail_html(
            addon_html,
            "https://www.mcmod.cn/item/196531.html",
        )
        self.assertEqual(status, "success")
        self.assertEqual(detail["recipes"][0]["availability"], "active")
        self.assertEqual(detail["recipes"][0]["required_mods"], ["CreatePlus", "科技复兴"])

    def test_recipe_selection_excludes_removed_and_unasked_addons(self):
        base = {
            "method": "工作台",
            "grid_slots": [[{"name": "材料"} for _ in range(3)] for _ in range(3)],
            "availability": "active",
            "required_mods": [],
            "output": {"name": "水车"},
        }
        removed = base | {"availability": "removed", "marker": "removed"}
        addon = base | {"required_mods": ["CreatePlus", "科技复兴"], "marker": "addon"}
        current = base | {"marker": "current"}

        self.assertIs(
            mcmod_search.select_recipe_for_image(
                [removed, current, addon],
                "机械动力水车怎么制作",
            ),
            current,
        )
        self.assertIs(
            mcmod_search.select_recipe_for_image(
                [removed, current, addon],
                "CreatePlus 的水车怎么制作",
            ),
            addon,
        )
        self.assertIs(
            mcmod_search.select_recipe_for_image(
                [addon],
                "Create 水车怎么制作",
            ),
            None,
        )
        self.assertIs(
            mcmod_search.select_recipe_for_image([removed], "旧版水车怎么制作"),
            None,
        )

    def test_recipe_selection_rejects_unreliable_layouts(self):
        self.assertIsNone(
            mcmod_search.select_recipe_for_image(
                [
                    {
                        "method": "工作台",
                        "grid_slots": [[{}]],
                        "availability": "active",
                        "required_mods": [],
                        "output": {"name": "测试"},
                    }
                ],
                "测试怎么合成",
            )
        )
        self.assertIsNone(
            mcmod_search.select_recipe_for_image(
                [
                    {
                        "method": "熔炉",
                        "grid_slots": [[{"name": "材料"} for _ in range(3)] for _ in range(3)],
                        "availability": "active",
                        "required_mods": [],
                        "output": {"name": "测试"},
                    }
                ],
                "测试怎么合成",
            )
        )

    def test_parse_item_detail_accepts_recipe_even_when_introduction_is_missing(self):
        html = RECIPE_DETAIL_HTML.replace(
            '<div class="item-content common-text"><p>水车介绍。</p></div>',
            "",
        )

        status, detail = mcmod_search.parse_item_detail_html(
            html,
            "https://www.mcmod.cn/item/196531.html",
        )

        self.assertEqual(status, "success")
        self.assertEqual(detail["introduction"], "")
        self.assertEqual(detail["recipes"][0]["output"]["name"], "水车")

    def test_parse_item_detail_righttable_attribute_fallback_and_limits_attributes(self):
        rows = "".join(
            f"<tr><td>属性{i}：</td><td>值{i}</td></tr>" for i in range(25)
        )
        html = f"""
        <div class="itemname"><div class="name"><h5>测试物品</h5></div></div>
        <table class="righttable">{rows}</table>
        <div class="item-content common-text"><p>测试介绍。</p></div>
        """
        status, detail = mcmod_search.parse_item_detail_html(html, DETAIL_URL)
        self.assertEqual(status, "success")
        self.assertEqual(len(detail["attributes"]), 20)
        self.assertIsNone(detail["mod"])
        self.assertIsNone(detail["item_command"])
        self.assertIsNone(detail["icon_url"])

    def test_parse_item_detail_truncates_with_ellipsis_and_missing_nodes_fail(self):
        html = """
        <div class="itemname"><div class="name"><h5>测试物品</h5></div></div>
        <div class="item-content common-text"><p>一二三四五六七八九十</p></div>
        """
        status, detail = mcmod_search.parse_item_detail_html(
            html, DETAIL_URL, max_intro_length=6
        )
        self.assertEqual(status, "success")
        self.assertEqual(detail["introduction"], "一二三四五…")
        self.assertEqual(len(detail["introduction"]), 6)

        missing_title = '<div class="item-content common-text"><p>介绍</p></div>'
        missing_intro = '<div class="itemname"><div class="name"><h5>标题</h5></div></div>'
        self.assertEqual(
            mcmod_search.parse_item_detail_html(missing_title, DETAIL_URL),
            ("parse_error", None),
        )
        self.assertEqual(
            mcmod_search.parse_item_detail_html(missing_intro, DETAIL_URL),
            ("parse_error", None),
        )

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
        self.assertNotIn("required_next_tool", result)

    async def test_item_search_requires_detail_before_answering(self):
        item_result = {
            "title": "水车 (Water Wheel) - 机械动力 (Create)",
            "url": "https://www.mcmod.cn/item/196531.html",
            "summary": "候选摘要不能直接用于回答",
            "type": "item",
        }
        with (
            patch.object(
                mcmod_search,
                "_fetch_search_html",
                AsyncMock(return_value=("success", SUCCESS_HTML)),
            ),
            patch.object(
                mcmod_search,
                "parse_search_html",
                return_value=("success", [item_result]),
            ),
        ):
            result = await mcmod_search.search_mcmod("水车", "item", 1, 5)

        self.assertEqual(result["answer_state"], "incomplete_candidates_only")
        self.assertEqual(result["required_next_tool"], "mcmod_item_detail")
        self.assertIn("禁止依据 title 或 summary 回答", result["followup_instruction"])
        self.assertIn("必须直接调用 mcmod_item_detail", result["followup_instruction"])


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


class DetailFetchAndResponseTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, actions: list):
        factory = FakeSessionFactory(actions)
        with patch.object(mcmod_search.asyncio, "sleep", AsyncMock()) as sleep:
            result = await mcmod_search._fetch_item_detail_html(
                DETAIL_URL,
                session_factory=factory,
            )
        return result, factory, sleep

    async def test_detail_200_success_and_session_contract(self):
        result, factory, sleep = await self.fetch([FakeResponse(200, DETAIL_HTML)])
        self.assertEqual(result, ("success", DETAIL_HTML))
        self.assertEqual(factory.session.get_calls, [(DETAIL_URL, None)])
        self.assertEqual(factory.session.redirect_options, [False])
        self.assertTrue(factory.kwargs["trust_env"])
        self.assertIn("Mozilla/5.0", factory.kwargs["headers"]["User-Agent"])
        self.assertEqual(factory.kwargs["headers"]["Accept-Language"], "zh-CN,zh;q=0.9")
        timeout = factory.kwargs["timeout"]
        self.assertEqual(timeout.total, 10)
        self.assertEqual(timeout.connect, 4)
        self.assertEqual(timeout.sock_read, 8)
        sleep.assert_not_awaited()

    async def test_detail_429_does_not_retry(self):
        result, factory, sleep = await self.fetch([FakeResponse(429)])
        self.assertEqual(result, ("rate_limited", None))
        self.assertEqual(len(factory.session.get_calls), 1)
        sleep.assert_not_awaited()

    async def test_detail_5xx_retries_once(self):
        result, factory, sleep = await self.fetch(
            [FakeResponse(504), FakeResponse(200, DETAIL_HTML)]
        )
        self.assertEqual(result, ("success", DETAIL_HTML))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

        result, factory, sleep = await self.fetch([FakeResponse(502), FakeResponse(503)])
        self.assertEqual(result, ("upstream_error", None))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_detail_connection_failure_retries_once(self):
        result, factory, sleep = await self.fetch(
            [aiohttp.ClientConnectionError("sensitive"), FakeResponse(200, DETAIL_HTML)]
        )
        self.assertEqual(result, ("success", DETAIL_HTML))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

        result, factory, sleep = await self.fetch(
            [aiohttp.ClientConnectionError("first"), aiohttp.ClientConnectionError("second")]
        )
        self.assertEqual(result, ("upstream_error", None))
        self.assertEqual(len(factory.session.get_calls), 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_detail_timeout_does_not_retry(self):
        result, factory, sleep = await self.fetch([asyncio.TimeoutError()])
        self.assertEqual(result, ("timeout", None))
        self.assertEqual(len(factory.session.get_calls), 1)
        sleep.assert_not_awaited()

    async def test_get_item_detail_has_fixed_response_structure(self):
        invalid = await mcmod_search.get_mcmod_item_detail(
            "https://evil.example/item/1.html"
        )
        self.assertEqual(
            invalid,
            {
                "status": "invalid_argument",
                "source_url": "https://evil.example/item/1.html",
                "detail": None,
                "content_is_untrusted": True,
                "reply_instruction": REPLY_INSTRUCTION,
            },
        )

        with patch.object(
            mcmod_search,
            "_fetch_item_detail_html",
            AsyncMock(return_value=("timeout", None)),
        ) as fetch:
            timeout_result = await mcmod_search.get_mcmod_item_detail(
                "http://mcmod.cn/item/137201.html"
            )
        fetch.assert_awaited_once_with(DETAIL_URL)
        self.assertEqual(
            timeout_result,
            {
                "status": "timeout",
                "source_url": DETAIL_URL,
                "detail": None,
                "content_is_untrusted": True,
                "reply_instruction": REPLY_INSTRUCTION,
            },
        )

        with patch.object(
            mcmod_search,
            "_fetch_item_detail_html",
            AsyncMock(return_value=("success", DETAIL_HTML)),
        ):
            success = await mcmod_search.get_mcmod_item_detail(DETAIL_URL)
        self.assertEqual(
            set(success),
            {"status", "source_url", "detail", "content_is_untrusted", "reply_instruction"},
        )
        self.assertEqual(success["status"], "success")
        self.assertEqual(success["detail"]["title"], "命令方块")
        self.assertEqual(success["detail"]["source_url"], DETAIL_URL)
        self.assertTrue(success["content_is_untrusted"])

        with patch.object(
            mcmod_search,
            "_fetch_item_detail_html",
            AsyncMock(return_value=("success", "<html></html>")),
        ):
            parse_error = await mcmod_search.get_mcmod_item_detail(DETAIL_URL)
        self.assertEqual(parse_error["status"], "parse_error")
        self.assertIsNone(parse_error["detail"])


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

    def assert_own_tool(self, name: str, handler, *, active: bool = True) -> None:
        tools = [tool for tool in self.manager.func_list if tool.name == name]
        self.assertEqual(len(tools), 1, name)
        tool = tools[0]
        self.assertEqual(tool.handler_module_path, main.__name__)
        self.assertIs(tool.handler.__self__, self.plugin)
        self.assertIs(tool.handler.__func__, handler)
        self.assertIs(tool.active, active)

    def assert_both_tools(
        self,
        *,
        search_active: bool = True,
        detail_active: bool = True,
    ) -> None:
        self.assertEqual(
            sorted(tool.name for tool in self.manager.func_list),
            ["mcmod_item_detail", "mcmod_search"],
        )
        self.assert_own_tool(
            "mcmod_search", MyPlugin.mcmod_search, active=search_active
        )
        self.assert_own_tool(
            "mcmod_item_detail",
            MyPlugin.mcmod_item_detail,
            active=detail_active,
        )

    async def test_mcmod_then_own_load_registers_two_unique_tools(self):
        self.add_old_tool()

        await self.trigger("astrbot_zhouyi_plugin")

        self.assert_both_tools()

    async def test_own_then_mcmod_load_restores_after_old_plugin_overwrite(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.add_old_tool()
        overwritten = self.manager.get_func("mcmod_search")
        self.assertEqual(overwritten.handler_module_path, "data.plugins.mcmod_card.main")

        await self.trigger("mcmod_card")

        self.assert_both_tools()

    async def test_inactive_own_plugin_does_not_restore_or_add_tools(self):
        self.own_metadata.activated = False
        self.add_old_tool()

        await self.trigger("mcmod_card")

        tool = self.manager.get_func("mcmod_search")
        self.assertEqual(tool.handler_module_path, "data.plugins.mcmod_card.main")
        self.assertEqual(len(self.manager.func_list), 1)
        self.assertIsNone(self.manager.get_func("mcmod_item_detail"))

    async def test_missing_own_metadata_does_not_register(self):
        self.plugin.context.own_metadata = None

        await self.trigger("astrbot_zhouyi_plugin")

        self.assertEqual(self.manager.func_list, [])

    async def test_manual_search_inactive_state_is_preserved_independently(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.manager.get_func("mcmod_search").active = False

        await self.trigger("astrbot_zhouyi_plugin")

        self.assert_both_tools(search_active=False, detail_active=True)

    async def test_manual_detail_inactive_state_is_preserved_independently(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.manager.get_func("mcmod_item_detail").active = False

        await self.trigger("mcmod_card")

        self.assert_both_tools(search_active=True, detail_active=False)

    async def test_inactive_old_tool_is_replaced_as_active(self):
        self.add_old_tool(active=False)

        await self.trigger("mcmod_card")

        self.assert_both_tools()

    async def test_unrelated_plugin_load_does_not_change_existing_tools(self):
        await self.trigger("astrbot_zhouyi_plugin")
        self.manager.get_func("mcmod_item_detail").active = False
        before = list(self.manager.func_list)

        await self.trigger("other_plugin")

        self.assertEqual(self.manager.func_list, before)
        self.assert_both_tools(detail_active=False)

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
        self.detail_handler = getattr(
            MyPlugin.mcmod_item_detail,
            "__wrapped__",
            MyPlugin.mcmod_item_detail,
        )

    async def test_crafting_only_question_adds_explicit_answer_scope(self):
        event = FakeEvent("机械动力水车怎么制作")
        with patch.object(
            main,
            "get_mcmod_item_detail",
            AsyncMock(
                return_value={
                    "status": "success",
                    "source_url": DETAIL_URL,
                    "detail": {"title": "水车"},
                }
            ),
        ):
            payload = await self.detail_handler(self.plugin, event, DETAIL_URL)

        result = json.loads(payload)
        self.assertEqual(result["answer_scope"]["type"], "crafting_only")
        self.assertIn("最大堆叠", result["answer_scope"]["forbidden"])
        self.assertIn("制作材料", result["answer_scope"]["allowed"])

    async def test_mixed_crafting_and_usage_question_has_no_crafting_only_scope(self):
        event = FakeEvent("水车怎么制作和怎么用")
        with patch.object(
            main,
            "get_mcmod_item_detail",
            AsyncMock(
                return_value={
                    "status": "success",
                    "source_url": DETAIL_URL,
                    "detail": {"title": "水车"},
                }
            ),
        ):
            payload = await self.detail_handler(self.plugin, event, DETAIL_URL)

        self.assertNotIn("answer_scope", json.loads(payload))

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
            "reply_instruction": REPLY_INSTRUCTION,
        }
        event = FakeEvent()
        with patch.object(main, "search_mcmod", AsyncMock(return_value=expected)) as search:
            payload = await self.handler(self.plugin, event, "机械动力", "mod", 1, 2)

        search.assert_awaited_once_with("机械动力", "mod", 1, 2)
        self.assertTrue(event.get_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY))
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
                "reply_instruction": REPLY_INSTRUCTION,
            },
        )

    async def test_detail_tool_returns_unescaped_chinese_json(self):
        expected = {
            "status": "success",
            "source_url": DETAIL_URL,
            "detail": {
                "title": "命令方块",
                "mod": None,
                "item_command": None,
                "attributes": {},
                "introduction": "中文介绍",
                "icon_url": None,
                "source_url": DETAIL_URL,
            },
            "content_is_untrusted": True,
            "reply_instruction": REPLY_INSTRUCTION,
        }
        event = FakeEvent()
        with patch.object(
            main,
            "get_mcmod_item_detail",
            AsyncMock(return_value=expected),
        ) as detail:
            payload = await self.detail_handler(self.plugin, event, DETAIL_URL)

        detail.assert_awaited_once_with(DETAIL_URL)
        self.assertTrue(event.get_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY))
        self.assertNotIn("\\u", payload)
        self.assertEqual(json.loads(payload), expected)

    async def test_detail_tool_exception_returns_complete_upstream_error(self):
        with patch.object(
            main,
            "get_mcmod_item_detail",
            AsyncMock(side_effect=RuntimeError("sensitive upstream text")),
        ):
            payload = await self.detail_handler(self.plugin, None, DETAIL_URL)

        self.assertEqual(
            json.loads(payload),
            {
                "status": "upstream_error",
                "source_url": DETAIL_URL,
                "detail": None,
                "content_is_untrusted": True,
                "reply_instruction": REPLY_INSTRUCTION,
            },
        )

    async def test_tool_handlers_allow_none_event(self):
        with (
            patch.object(main, "search_mcmod", AsyncMock(return_value={})),
            patch.object(main, "get_mcmod_item_detail", AsyncMock(return_value={})),
        ):
            await self.handler(self.plugin, None, "机械动力")
            await self.detail_handler(self.plugin, None, DETAIL_URL)

    async def test_crafting_detail_atomically_replaces_final_chain_with_recipe_image(self):
        plugin = object.__new__(MyPlugin)
        event = FakeEvent("机械动力水车怎么制作")
        recipe = {
            "method": "工作台",
            "materials": [
                {"name": "任意木板", "count": 8, "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html"},
                {"name": "传动杆", "count": 1, "source": "https://www.mcmod.cn/item/196521.html"},
            ],
            "grid": [
                ["任意木板", "任意木板", "任意木板"],
                ["任意木板", "传动杆", "任意木板"],
                ["任意木板", "任意木板", "任意木板"],
            ],
            "grid_slots": [
                [
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                ],
                [
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                    {"name": "传动杆", "source": "https://www.mcmod.cn/item/196521.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/2.png"},
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                ],
                [
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                    {"name": "任意木板", "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html", "icon_url": "https://i.mcmod.cn/item/icon/16x16/1.png"},
                ],
            ],
            "output": {
                "name": "水车",
                "count": 1,
                "source": "https://www.mcmod.cn/item/196531.html",
                "icon_url": "https://i.mcmod.cn/item/icon/16x16/3.png",
            },
            "conditions": ["需要 v0.5.1 或更高版本"],
            "availability": "active",
            "required_mods": [],
        }
        detail_result = {
            "status": "success",
            "source_url": "https://www.mcmod.cn/item/196531.html",
            "detail": {"title": "水车", "recipes": [recipe]},
        }
        with patch.object(main, "get_mcmod_item_detail", AsyncMock(return_value=detail_result)):
            await self.detail_handler(
                plugin,
                event,
                "https://www.mcmod.cn/item/196531.html",
            )

        original = main.Comp.Plain("原始纯文本配方")
        event.result = SimpleNamespace(chain=[original])
        image_component = object()
        decorating_handler = getattr(
            MyPlugin.handle_mcmod_reply_decorating,
            "__wrapped__",
            MyPlugin.handle_mcmod_reply_decorating,
        )
        with (
            patch.object(
                main,
                "render_recipe_image_base64",
                AsyncMock(return_value="png-base64"),
                create=True,
            ),
            patch.object(
                main.Comp.Image,
                "fromBase64",
                return_value=image_component,
            ),
        ):
            await decorating_handler(plugin, event)

        self.assertEqual(len(event.result.chain), 2)
        self.assertIs(event.result.chain[0], image_component)
        self.assertIsInstance(event.result.chain[1], main.Comp.Plain)
        self.assertEqual(
            event.result.chain[1].text,
            "来源：https://www.mcmod.cn/item/196531.html",
        )
        self.assertNotIn(original, event.result.chain)

    async def test_recipe_image_failure_preserves_existing_plain_chain(self):
        plugin = object.__new__(MyPlugin)
        event = FakeEvent("水车怎么制作")
        event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        event.set_extra(
            main.MCMOD_RECIPE_IMAGE_EXTRA_KEY,
            {
                "title": "水车",
                "source_url": "https://www.mcmod.cn/item/196531.html",
                "recipe": {
                    "method": "工作台",
                    "grid_slots": [[{"name": "木板"} for _ in range(3)] for _ in range(3)],
                    "output": {"name": "水车", "count": 1},
                    "availability": "active",
                },
            },
        )
        original = main.Comp.Plain("材料：8 个木板、1 个传动杆。")
        event.result = SimpleNamespace(chain=[original])
        decorating_handler = getattr(
            MyPlugin.handle_mcmod_reply_decorating,
            "__wrapped__",
            MyPlugin.handle_mcmod_reply_decorating,
        )

        with patch.object(
            main,
            "render_recipe_image_base64",
            AsyncMock(side_effect=RuntimeError("render boom")),
        ):
            await decorating_handler(plugin, event)

        self.assertEqual(event.result.chain, [original])
        self.assertEqual(original.text, "材料：8 个木板、1 个传动杆。")

    async def test_image_component_failure_preserves_existing_plain_chain(self):
        plugin = object.__new__(MyPlugin)
        event = FakeEvent("水车怎么合成")
        event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        event.set_extra(
            main.MCMOD_RECIPE_IMAGE_EXTRA_KEY,
            {
                "title": "水车",
                "source_url": "https://www.mcmod.cn/item/196531.html",
                "recipe": {
                    "method": "工作台",
                    "grid_slots": [[{"name": "木板"} for _ in range(3)] for _ in range(3)],
                    "output": {"name": "水车", "count": 1},
                    "availability": "active",
                },
            },
        )
        original = main.Comp.Plain("原始配方")
        event.result = SimpleNamespace(chain=[original])
        decorating_handler = getattr(
            MyPlugin.handle_mcmod_reply_decorating,
            "__wrapped__",
            MyPlugin.handle_mcmod_reply_decorating,
        )

        with (
            patch.object(
                main,
                "render_recipe_image_base64",
                AsyncMock(return_value="png-base64"),
            ),
            patch.object(
                main.Comp.Image,
                "fromBase64",
                side_effect=ValueError("component boom"),
            ),
        ):
            await decorating_handler(plugin, event)

        self.assertEqual(event.result.chain, [original])

    async def test_non_crafting_mixed_and_search_only_events_have_no_recipe_payload(self):
        plugin = object.__new__(MyPlugin)
        detail_result = {
            "status": "success",
            "source_url": "https://www.mcmod.cn/item/196531.html",
            "detail": {
                "title": "水车",
                "recipes": [
                    {
                        "method": "工作台",
                        "grid_slots": [[{"name": "木板"} for _ in range(3)] for _ in range(3)],
                        "output": {"name": "水车", "count": 1},
                        "availability": "active",
                        "required_mods": [],
                    }
                ],
            },
        }
        with patch.object(main, "get_mcmod_item_detail", AsyncMock(return_value=detail_result)):
            for question in ("水车是什么", "水车怎么制作和怎么用"):
                event = FakeEvent(question)
                await self.detail_handler(plugin, event, "https://www.mcmod.cn/item/196531.html")
                self.assertIsNone(event.get_extra(main.MCMOD_RECIPE_IMAGE_EXTRA_KEY))

        search_event = FakeEvent("水车怎么制作")
        with patch.object(main, "search_mcmod", AsyncMock(return_value={})):
            await self.handler(plugin, search_event, "水车", "item")
        self.assertIsNone(search_event.get_extra(main.MCMOD_RECIPE_IMAGE_EXTRA_KEY))

    async def test_decorating_hook_filters_final_crafting_message_chain(self):
        plugin = object.__new__(MyPlugin)
        decorating_handler = getattr(
            MyPlugin.handle_mcmod_reply_decorating,
            "__wrapped__",
            MyPlugin.handle_mcmod_reply_decorating,
        )
        event = FakeEvent("机械动力水车怎么制作")
        event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        event.result = SimpleNamespace(
            chain=[
                main.Comp.Plain(
                    "唔…\n8 个木板围住 1 个传动杆，得到 1 个水车。\n"
                    "【怎么用】\n放在水流里。\n"
                    "如果需要准确配方，可以去游戏内 JEI 查看会更靠谱。\n"
                    "来源：https://www.mcmod.cn/item/196531.html"
                )
            ]
        )

        await decorating_handler(plugin, event)

        self.assertEqual(
            event.result.chain[0].text,
            "8 个木板围住 1 个传动杆，得到 1 个水车。\n"
            "来源：https://www.mcmod.cn/item/196531.html",
        )

    async def test_decorating_hook_preserves_recipe_and_removes_false_uncertainty(self):
        plugin = object.__new__(MyPlugin)
        decorating_handler = getattr(
            MyPlugin.handle_mcmod_reply_decorating,
            "__wrapped__",
            MyPlugin.handle_mcmod_reply_decorating,
        )
        event = FakeEvent("机械动力水车怎么合成")
        event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        event.result = SimpleNamespace(
            chain=[
                main.Comp.Plain(
                    "材料：8 个木板、1 个传动杆。\n"
                    "九宫格：\n"
                    "木板｜木板｜木板\n"
                    "木板｜传动杆｜木板\n"
                    "木板｜木板｜木板\n"
                    "产物：1 个水车。\n"
                    "详情解析器没有拿到配方，因此无法确定具体材料和产物数量。\n"
                    "【怎么用】\n放在水流里。\n"
                    "来源：https://www.mcmod.cn/item/196531.html"
                )
            ]
        )

        await decorating_handler(plugin, event)

        self.assertEqual(
            event.result.chain[0].text,
            "材料：8 个木板、1 个传动杆。\n"
            "九宫格：\n"
            "木板｜木板｜木板\n"
            "木板｜传动杆｜木板\n"
            "木板｜木板｜木板\n"
            "产物：1 个水车。\n"
            "来源：https://www.mcmod.cn/item/196531.html",
        )

    async def test_memory_reflection_filters_crafting_only_reply_scope(self):
        plugin = object.__new__(MyPlugin)
        plugin.runtime = SimpleNamespace(memory=None)
        reflection_handler = getattr(
            MyPlugin.handle_memory_reflection,
            "__wrapped__",
            MyPlugin.handle_memory_reflection,
        )
        event = FakeEvent("机械动力水车怎么制作")
        event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        resp = SimpleNamespace(
            completion_text=(
                "唔…\n【水车制作】\n8 个木板围住 1 个传动杆，得到 1 个水车。\n"
                "【怎么用】\n放在水流里。\n【小提示】\n可以换木纹。\n"
                "来源：https://www.mcmod.cn/item/196531.html"
            )
        )

        await reflection_handler(plugin, event, resp)

        self.assertEqual(
            resp.completion_text,
            "【水车制作】\n8 个木板围住 1 个传动杆，得到 1 个水车。\n"
            "来源：https://www.mcmod.cn/item/196531.html",
        )

    async def test_memory_reflection_cleans_only_marked_mcmod_response(self):
        memory_reflection = AsyncMock()
        plugin = object.__new__(MyPlugin)
        plugin.runtime = SimpleNamespace(
            memory=SimpleNamespace(handle_memory_reflection=memory_reflection)
        )
        reflection_handler = getattr(
            MyPlugin.handle_memory_reflection,
            "__wrapped__",
            MyPlugin.handle_memory_reflection,
        )

        marked_event = FakeEvent()
        marked_event.set_extra(mcmod_search.MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY, True)
        marked_resp = SimpleNamespace(
            completion_text="## **标题**\n- `create:brass_casing`\n[来源](https://www.mcmod.cn/item/123.html)"
        )
        await reflection_handler(plugin, marked_event, marked_resp)

        self.assertEqual(
            marked_resp.completion_text,
            "【标题】\n· create:brass_casing\n来源：https://www.mcmod.cn/item/123.html",
        )
        memory_reflection.assert_awaited_once_with(marked_event, marked_resp)
        self.assertEqual(
            memory_reflection.await_args.args[1].completion_text,
            marked_resp.completion_text,
        )

        memory_reflection.reset_mock()
        unmarked_event = FakeEvent()
        original = "## **普通回答**\n```python\nprint('keep markdown')\n```"
        unmarked_resp = SimpleNamespace(completion_text=original)
        await reflection_handler(plugin, unmarked_event, unmarked_resp)

        self.assertEqual(unmarked_resp.completion_text, original)
        memory_reflection.assert_awaited_once_with(unmarked_event, unmarked_resp)


if __name__ == "__main__":
    unittest.main()
