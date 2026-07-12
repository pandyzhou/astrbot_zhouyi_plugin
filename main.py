from typing import List, Optional, Dict, Any
from pathlib import Path
import json
import astrbot.core.message.components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from .script.get_server_info import get_server_status
from .script.get_img import generate_server_info_image, get_card_background
from .script.mcmod_search import search_mcmod
from .script.bar_chart import (
    ServerTrendInput,
    generate_bar_chart_image,
    generate_summary_chart_images,
)
from .script.query_runtime import call_status_fetcher, gather_limited
from .script.runtime_settings import (
    EffectiveRuntimeSettings,
    get_effective_settings,
    get_global_settings,
)
from .script.json_operate import (
    GroupStorage,
    read_json, add_data, del_data, update_data,
    get_all_servers, get_server_info, get_server_by_name,
    update_server_status, auto_cleanup_servers,
    append_trend_point, get_trend_history, get_all_trend_histories,
    get_group_storage as locate_group_storage,
    initialize_storage, list_group_storages,
)
from .web_api import McManagerWebApi
from .standalone_web import StandaloneWebService
import asyncio
import re
import time
from datetime import datetime, timedelta

# 常量定义
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

HELP_INFO = """
mchelp 
--查看帮助

/mc   
--查询保存的服务器

/mcadd 服务器名称 服务器地址 [force]
--添加要查询的服务器
--force: 可选参数，设为True时跳过预查询检查强制添加

/mcget 服务器名称/ID
--获取指定服务器的地址信息

/mcdel 服务器名称/ID 
--删除服务器

/mcup 服务器名称/ID [新名称] [新地址]
--更新服务器信息

/mclist
--列出所有服务器及其ID

/mccleanup
--按当前群配置手动清理长期未查询成功的服务器

/mcdata [服务器名称/ID] [小时数]
--输出当前群全部服务器的趋势汇总图，或指定服务器的趋势仪表卡；省略小时数时使用当前配置
"""

@register("astrbot_zhouyi_plugin", "薄暝", "查询mc服务器信息和玩家列表,在线人数趋势仪表卡/汇总图,提供MC百科LLM搜索,渲染为图片(修改自QiChen的mcgetter)", "0.1.1")
class MyPlugin(Star):
    """Minecraft服务器信息查询插件"""
    
    def __init__(self, context: Context):
        """
        初始化插件

        Args:
            context: 插件上下文
        """
        super().__init__(context)
        self._settings_changed_event = asyncio.Event()
        self._page_api = McManagerWebApi(self)
        self._page_api.register_routes()
        logger.info("MyPlugin 初始化完成")
        self._standalone_service = StandaloneWebService()
        self._standalone_task: Optional[asyncio.Task] = asyncio.create_task(
            self._run_standalone_web()
        )
        # 启动每小时柱状图数据采样后台任务（单例，默认对所有已配置服务器启用）
        self._trend_task: Optional[asyncio.Task] = None
        if getattr(self, "_trend_task", None) is None:
            self._trend_task = asyncio.create_task(self._bar_data_loop())

    async def mcmod_search(
        self,
        event: AstrMessageEvent,
        query: str,
        category: str = "all",
        page: int = 1,
        limit: int = 5,
    ) -> str:
        """搜索 MC百科中的模组、整合包、物品/方块和教程。

        Args:
            query(str): 搜索关键词，长度为 1-100 个字符。
            category(str): 搜索分类，可选 all、mod、modpack、item、tutorial，默认为 all。
            page(int): 结果页码，范围为 1-20，默认为 1。
            limit(int): 返回结果数量，范围为 1-10，默认为 5。
        """
        try:
            result = await search_mcmod(query, category, page, limit)
        except Exception as exc:
            logger.warning("MC百科搜索工具执行异常：%s", type(exc).__name__)
            result = {
                "status": "upstream_error",
                "query": query,
                "category": category,
                "page": page,
                "limit": limit,
                "count": 0,
                "results": [],
            }
        return json.dumps(result, ensure_ascii=False)

    @filter.on_plugin_loaded()
    async def _register_mcmod_search_tool(self, metadata) -> None:
        if self is None or getattr(metadata, "root_dir_name", None) not in {
            "astrbot_zhouyi_plugin",
            "mcmod_card",
        }:
            return

        context = getattr(self, "context", None)
        if context is None:
            return
        own_metadata = context.get_registered_star("astrbot_zhouyi_plugin")
        if (
            own_metadata is None
            or getattr(own_metadata, "root_dir_name", None)
            != "astrbot_zhouyi_plugin"
            or not getattr(own_metadata, "activated", False)
        ):
            return

        manager = context.get_llm_tool_manager()
        existing_tools = [
            tool for tool in manager.func_list if tool.name == "mcmod_search"
        ]
        keep_inactive = any(
            getattr(tool, "handler_module_path", None) == __name__
            and getattr(tool, "active", True) is False
            for tool in existing_tools
        )
        while any(tool.name == "mcmod_search" for tool in manager.func_list):
            manager.remove_func("mcmod_search")

        manager.add_func(
            "mcmod_search",
            [
                {
                    "type": "string",
                    "name": "query",
                    "description": "搜索关键词，长度为 1-100 个字符。",
                },
                {
                    "type": "string",
                    "name": "category",
                    "description": "搜索分类，可选 all、mod、modpack、item、tutorial，默认为 all。",
                },
                {
                    "type": "integer",
                    "name": "page",
                    "description": "结果页码，范围为 1-20，默认为 1。",
                },
                {
                    "type": "integer",
                    "name": "limit",
                    "description": "返回结果数量，范围为 1-10，默认为 5。",
                },
            ],
            "搜索 MC百科中的模组、整合包、物品/方块和教程。",
            self.mcmod_search,
        )
        tool = manager.get_func("mcmod_search")
        if tool is not None:
            tool.handler_module_path = __name__
            tool.active = not keep_inactive

    def notify_settings_changed(self) -> None:
        """通知后台采样循环重新读取运行配置。"""
        settings_changed = getattr(self, "_settings_changed_event", None)
        if settings_changed is None:
            settings_changed = asyncio.Event()
            self._settings_changed_event = settings_changed
        settings_changed.set()

    @staticmethod
    async def _query_server_status(
        host: str,
        settings: EffectiveRuntimeSettings,
    ) -> dict[str, Any] | None:
        return await call_status_fetcher(
            get_server_status,
            host,
            lookup_timeout=settings.mc_lookup_timeout_seconds,
            status_timeout=settings.mc_status_timeout_seconds,
        )

    @filter.command("mchelp")
    async def get_help(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        显示帮助信息

        Args:
            event: 消息事件

        Returns:
            包含帮助信息的消息结果
        """
        yield event.plain_result(HELP_INFO)

    @filter.command("mc")
    async def mcgetter(self, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        """
        查询所有保存的服务器信息

        Args:
            event: 消息事件

        Returns:
            包含服务器信息图片的消息结果，如果出错则返回None
        """
        logger.info("开始执行 mc 命令")
        try:
            group_id = event.get_group_id()
            logger.info(f"获取到群组ID: {group_id}")
            
            storage = await self.get_group_storage(group_id)
            logger.info(f"群组存储定位: {storage}")

            json_data = await read_json(storage)
            logger.info(f"读取到的群组数据: {json_data}")

            if not json_data or not json_data.get("servers"):
                logger.warning("群组数据为空或没有服务器")
                yield event.plain_result("请先使用 /mcadd 添加服务器")
                return
            
            message_chain: List[Comp.Image] = []
            servers = json_data.get("servers", {})
            effective = await get_effective_settings(storage)
            selected = sorted(
                servers.items(),
                key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 1_000_000_000,
            )
            image_results = await gather_limited(
                (
                    lambda server_id=server_id, server_info=server_info: self.get_img(
                        server_info["name"],
                        server_info["host"],
                        server_id,
                        storage,
                        settings=effective,
                    )
                    for server_id, server_info in selected
                ),
                effective.max_concurrent_queries,
            )
            for (server_id, server_info), mcinfo_img in zip(selected, image_results):
                if isinstance(mcinfo_img, Exception):
                    logger.error(
                        f"处理服务器 {server_info['name']} (ID: {server_id}) 时出错: {mcinfo_img}"
                    )
                    continue
                if mcinfo_img:
                    message_chain.append(Comp.Image.fromBase64(mcinfo_img))
                else:
                    logger.warning(
                        f"获取服务器 {server_info['name']} (ID: {server_id}) 的图片失败"
                    )

            # 查询更新完成后再按当前群配置执行自动清理，避免误删刚成功的服务器。
            deleted_servers = (
                await auto_cleanup_servers(
                    storage,
                    cleanup_days=effective.auto_cleanup_days,
                )
                if effective.auto_cleanup_enabled
                else []
            )
            if deleted_servers:
                cleanup_message = (
                    f"自动清理完成，以下服务器因 {effective.auto_cleanup_days} 天未查询成功已被删除:\n"
                )
                for server in deleted_servers:
                    last_success = server.get("last_success_time")
                    last_success_date = (
                        datetime.fromtimestamp(last_success).strftime("%Y-%m-%d %H:%M:%S")
                        if last_success
                        else "从未成功"
                    )
                    cleanup_message += f"• {server['name']} (ID: {server['id']}) - 地址: {server['host']} - 最后成功: {last_success_date}\n"
                if message_chain:
                    yield event.chain_result(message_chain)
                yield event.plain_result(cleanup_message.strip())
                return

            if message_chain:
                logger.info(f"成功生成消息链，包含 {len(message_chain)} 张图片")
                yield event.chain_result(message_chain)
            else:
                logger.warning("没有可用的服务器信息")
                yield event.plain_result("没有可用的服务器信息，请检查服务器是否在线")
                
        except Exception as e:
            logger.error(f"执行 mc 命令时出错: {e}")
            yield event.plain_result("查询服务器信息时发生错误")

    @filter.command("mcadd")
    async def mcadd(self, event: AstrMessageEvent, name: str, host: str, force: bool = False) -> MessageEventResult:
        """
        添加新的服务器

        Args:
            event: 消息事件
            name: 服务器名称
            host: 服务器地址
            force: 是否强制添加（跳过预查询检查）

        Returns:
            操作结果消息
        """
        logger.info(f"开始执行 mcadd 命令: {name} -> {host}, force: {force}")
        
        try:
            # 检查host合法性
            if not re.match(r'^[a-zA-Z0-9.:-]+$', host):
                yield event.plain_result("服务器地址格式不正确，只能包含字母、数字和符号.:-")
                return

            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            effective = await get_effective_settings(storage)
            if not force and await self._query_server_status(host, effective) is None:
                yield event.plain_result("预查询失败，请检查服务器是否在线或地址是否正确，或在完整的/mcadd命令后加上True 强制添加")
                return
            
            # 检查当前地址是否已存在
            try:
                json_data = await read_json(storage)
                servers = json_data.get("servers", {})
                if servers:
                    for server_id, server_info in servers.items():
                        if server_info['host'] == host:
                            yield event.plain_result(f"已存在相同地址的服务器 {server_info['name']} (ID: {server_id})")
                            return
            except Exception as e:
                logger.error(f"检查服务器地址时出错: {e}")
                yield event.plain_result("检查服务器地址时发生错误")
                return
                
            if await add_data(storage, name, host):
                # 获取新添加的服务器ID
                json_data = await read_json(storage)
                servers = json_data.get("servers", {})
                for server_id, server_info in servers.items():
                    if server_info['name'] == name and server_info['host'] == host:
                        yield event.plain_result(f"成功添加服务器 {name} (ID: {server_id})")
                        return
                yield event.plain_result(f"成功添加服务器 {name}")
            else:
                yield event.plain_result(f"无法添加 {name}，请检查是否已存在")
                
        except Exception as e:
            logger.error(f"执行 mcadd 命令时出错: {e}")
            yield event.plain_result("添加服务器时发生错误")

    @filter.command("mcdel")
    async def mcdel(self, event: AstrMessageEvent, identifier: str) -> MessageEventResult:
        """
        删除指定的服务器（支持通过名称或ID删除）

        Args:
            event: 消息事件
            identifier: 要删除的服务器名称或ID

        Returns:
            操作结果消息
        """
        logger.info(f"开始执行 mcdel 命令: {identifier}")
        try:
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            
            if await del_data(storage, identifier):
                yield event.plain_result(f"成功删除服务器 {identifier}")
            else:
                yield event.plain_result(f"无法删除 {identifier}，请检查是否存在")
                
        except Exception as e:
            logger.error(f"执行 mcdel 命令时出错: {e}")
            yield event.plain_result("删除服务器时发生错误")

    @filter.command("mcget")
    async def mcget(self, event: AstrMessageEvent, identifier: str) -> MessageEventResult:
        """
        获取指定服务器的信息（支持通过名称或ID查找）
        """
        logger.info(f"开始执行 mcget 命令: {identifier}")
        try:
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            
            server_info = await get_server_info(storage, identifier)
            if not server_info:
                yield event.plain_result(f"没有找到服务器 {identifier}")
                return
                
            yield event.plain_result(f"{server_info['name']} (ID: {server_info['id']}) 的地址是:")
            yield event.plain_result(f"{server_info['host']}")
            
        except Exception as e:
            logger.error(f"执行 mcget 命令时出错: {e}")
            yield event.plain_result("获取服务器信息时发生错误")

    @filter.command("mcup")
    async def mcup(self, event: AstrMessageEvent, identifier: str, new_name: Optional[str] = None, new_host: Optional[str] = None) -> MessageEventResult:
        """
        更新服务器信息（支持通过名称或ID更新）

        Args:
            event: 消息事件
            identifier: 要更新的服务器名称或ID
            new_name: 新的服务器名称（可选）
            new_host: 新的服务器地址（可选）

        Returns:
            操作结果消息
        """
        logger.info(f"开始执行 mcup 命令: {identifier}, new_name: {new_name}, new_host: {new_host}")
        
        try:
            if not new_name and not new_host:
                yield event.plain_result("请提供要更新的信息（新名称或新地址）")
                return
                
            # 如果提供了新地址，检查格式
            if new_host and not re.match(r'^[a-zA-Z0-9.:-]+$', new_host):
                yield event.plain_result("服务器地址格式不正确，只能包含字母、数字和符号.:-")
                return
                
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            
            if await update_data(storage, identifier, new_name, new_host):
                # 获取更新后的服务器信息
                updated_info = await get_server_info(storage, identifier)
                if updated_info:
                    yield event.plain_result(f"成功更新服务器信息: {updated_info['name']} (ID: {updated_info['id']})")
                else:
                    yield event.plain_result(f"成功更新服务器 {identifier}")
            else:
                yield event.plain_result(f"无法更新 {identifier}，请检查是否存在或名称是否冲突")
                
        except Exception as e:
            logger.error(f"执行 mcup 命令时出错: {e}")
            yield event.plain_result("更新服务器信息时发生错误")

    @filter.command("mclist")
    async def mclist(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        列出所有服务器及其ID
        """
        logger.info("开始执行 mclist 命令")
        try:
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            
            servers = await get_all_servers(storage)
            if not servers:
                yield event.plain_result("没有保存的服务器")
                return
                
            server_list = "当前保存的服务器列表:\n"
            for server_id, server_info in sorted(servers.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 1_000_000_000):
                server_list += f"ID: {server_id}, 名称: {server_info['name']}, 地址: {server_info['host']}\n"
                
            yield event.plain_result(server_list.strip())
            
        except Exception as e:
            logger.error(f"执行 mclist 命令时出错: {e}")
            yield event.plain_result("获取服务器列表时发生错误")

    @filter.command("mccleanup")
    async def mccleanup(self, event: AstrMessageEvent) -> MessageEventResult:
        """按当前群配置手动触发清理。"""
        logger.info("开始执行 mccleanup 命令")
        try:
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            effective = await get_effective_settings(storage)

            # 手动命令始终有效，不受 auto_cleanup_enabled 开关影响。
            deleted_servers = await auto_cleanup_servers(
                storage,
                cleanup_days=effective.auto_cleanup_days,
            )
            if deleted_servers:
                cleanup_message = f"清理完成，以下服务器因 {effective.auto_cleanup_days} 天未查询成功已被删除:\n"
                for server in deleted_servers:
                    last_success = server.get("last_success_time")
                    last_success_date = (
                        datetime.fromtimestamp(last_success).strftime("%Y-%m-%d %H:%M:%S")
                        if last_success
                        else "从未成功"
                    )
                    cleanup_message += f"• {server['name']} (ID: {server['id']}) - 地址: {server['host']} - 最后成功: {last_success_date}\n"
                yield event.plain_result(cleanup_message.strip())
            else:
                yield event.plain_result("没有需要清理的服务器")
                
        except Exception as e:
            logger.error(f"执行 mccleanup 命令时出错: {e}")
            yield event.plain_result("自动清理时发生错误")

    @filter.command("mcdata")
    async def mcdata(
        self,
        event: AstrMessageEvent,
        identifier: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> Optional[MessageEventResult]:
        """输出指定服务器的趋势仪表卡，或当前群的全服趋势汇总图。"""
        try:
            group_id = event.get_group_id()
            storage = await self.get_group_storage(group_id)
            effective = await get_effective_settings(storage)
            servers = await get_all_servers(storage)
            if not servers:
                yield event.plain_result("当前群无已配置服务器，请先使用 /mcadd 添加。")
                return

            logger.info(f"mcdata 参数: identifier={identifier!r}, hours={hours!r}")
            if identifier is not None:
                ident_str = str(identifier)
                if ident_str.isdigit() and await get_server_info(storage, ident_str) is None:
                    hours = int(ident_str)
                    identifier = None
                else:
                    identifier = ident_str

            if hours is None:
                normalized_hours = effective.default_trend_hours
            else:
                try:
                    normalized_hours = int(hours)
                except (TypeError, ValueError):
                    normalized_hours = effective.default_trend_hours
            normalized_hours = max(1, min(168, normalized_hours))
            command_now = int(time.time())
            logger.info(
                f"mcdata 解析后: target={'ALL' if not identifier else identifier}, hours={normalized_hours}"
            )

            images: List[Comp.Image] = []
            if identifier:
                sinfo = await get_server_info(storage, identifier)
                if not sinfo:
                    yield event.plain_result(f"没有找到服务器 {identifier}")
                    return
                sid = str(sinfo.get("id"))
                name = sinfo.get("name", f"ID:{sid}")
                host = sinfo.get("host")
                status_now = (
                    await self._query_server_status(str(host), effective)
                    if host
                    else None
                )
                if not status_now:
                    yield event.plain_result(f"{name} 当前不可达，已跳过")
                    return
                hist = await get_trend_history(
                    storage,
                    sid,
                    hours=normalized_hours,
                )
                background = await get_card_background()
                img_b64 = generate_bar_chart_image(
                    hist or [],
                    name,
                    hours=normalized_hours,
                    background=background,
                    now_ts=command_now,
                )
                images.append(Comp.Image.fromBase64(img_b64))
            else:
                all_hist = await get_all_trend_histories(
                    storage,
                    hours=normalized_hours,
                )
                selected = sorted(
                    servers.items(),
                    key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 1_000_000_000,
                )

                async def probe(server_info: dict[str, Any]):
                    host = server_info.get("host")
                    return (
                        await self._query_server_status(str(host), effective)
                        if host
                        else None
                    )

                statuses = await gather_limited(
                    (lambda sinfo=sinfo: probe(sinfo) for _, sinfo in selected),
                    effective.max_concurrent_queries,
                )
                summary_inputs: list[ServerTrendInput] = []
                for (sid, sinfo), status_now in zip(selected, statuses):
                    if isinstance(status_now, Exception):
                        logger.debug(
                            f"mcdata 全服检测失败: {sinfo.get('name')} host={sinfo.get('host')} err={status_now}"
                        )
                        continue
                    if not status_now:
                        continue
                    summary_inputs.append(
                        ServerTrendInput(
                            id=str(sid),
                            name=sinfo.get("name", f"ID:{sid}"),
                            history=all_hist.get(str(sid), []) or [],
                        )
                    )

                if not summary_inputs:
                    yield event.plain_result("所有服务器当前均不可达，已跳过")
                    return
                background = await get_card_background()
                for img_b64 in generate_summary_chart_images(
                    summary_inputs,
                    hours=normalized_hours,
                    background=background,
                    now_ts=command_now,
                ):
                    images.append(Comp.Image.fromBase64(img_b64))

            if images:
                yield event.chain_result(images)
            else:
                yield event.plain_result("暂无趋势图数据，稍后再试。")
        except Exception as e:
            logger.error(f"生成趋势图失败: {e}")
            yield event.plain_result("生成趋势图失败，请稍后再试。")

    async def get_img(
        self,
        server_name: str,
        host: str,
        server_id: Optional[str] = None,
        storage: Optional[GroupStorage] = None,
        *,
        settings: Optional[EffectiveRuntimeSettings] = None,
    ) -> Optional[str]:
        """
        获取服务器信息图片

        Args:
            server_name: 服务器名称
            host: 服务器地址
            server_id: 服务器ID（可选）
            storage: 群组 SQLite 存储定位（用于更新状态）
            settings: 已读取的群组有效运行配置

        Returns:
            图片的base64编码字符串，如果获取失败则返回None
        """
        logger.info(f"开始获取服务器 {server_name} 的图片，主机地址: {host}")
        effective = settings
        if effective is None and storage is not None:
            effective = await get_effective_settings(storage)

        info: Optional[dict[str, Any]] = None
        try:
            info = (
                await self._query_server_status(host, effective)
                if effective is not None
                else await get_server_status(host)
            )
        except Exception as exc:
            logger.error(f"查询服务器 {server_name} 状态时出错: {exc}")

        is_online = bool(info)
        if not is_online:
            logger.warning(f"无法获取服务器 {server_name} 的状态信息，将生成离线卡")

        if storage and server_id:
            try:
                await update_server_status(storage, server_id, is_online)
            except Exception as exc:
                logger.warning(
                    f"更新服务器状态失败 group={storage}, sid={server_id}, online={is_online}: {exc}"
                )

        if is_online and info is not None:
            try:
                if (
                    storage
                    and server_id
                    and (effective is None or effective.trend_sampling_enabled)
                ):
                    await append_trend_point(
                        storage,
                        str(server_id),
                        int(datetime.now().timestamp()),
                        int(info["plays_online"]),
                        max_history_points=(
                            effective.max_history_points if effective is not None else None
                        ),
                    )
            except Exception as exc:
                logger.warning(f"追加柱状图数据失败 group={storage}, sid={server_id}: {exc}")

        display_name = f"[{server_id}]{server_name}" if server_id else server_name
        render_info = info or {
            "players_list": [],
            "latency": None,
            "plays_max": 0,
            "plays_online": 0,
            "server_version": "未知",
            "icon_base64": None,
            "host": host,
        }
        try:
            mcinfo_img = await generate_server_info_image(
                players_list=render_info.get("players_list") or [],
                latency=render_info.get("latency"),
                server_name=display_name,
                plays_max=render_info.get("plays_max", 0),
                plays_online=render_info.get("plays_online", 0),
                server_version=render_info.get("server_version") or "未知",
                icon_base64=render_info.get("icon_base64"),
                host_address=render_info.get("host", host),
                is_online=is_online,
            )
        except Exception as exc:
            logger.error(f"生成服务器 {server_name} 图片时出错: {exc}")
            return None

        logger.info(f"成功生成服务器 {server_name} 的图片")
        return mcinfo_img

    async def get_group_storage(self, group_id: str) -> GroupStorage:
        """校验群组 ID，并返回当前插件数据目录中的 SQLite 群存储定位。"""
        normalized_group_id = str(group_id)
        if not _GROUP_ID_RE.fullmatch(normalized_group_id):
            raise ValueError("群组 ID 格式不正确")
        data_path = Path(StarTools.get_data_dir("astrbot_zhouyi_plugin")).expanduser().resolve()
        await initialize_storage(data_path)
        storage = locate_group_storage(data_path, normalized_group_id)
        if storage.db_path.parent != data_path:
            raise ValueError("群组数据路径不安全")
        logger.info(f"群号 {normalized_group_id} 的 SQLite 存储: {storage.db_path}")
        return storage

    async def get_json_path(self, group_id: str) -> GroupStorage:
        """兼容旧调用名，返回群组存储定位。"""
        return await self.get_group_storage(group_id)

    async def _sample_trends_once(self, data_dir: Path, now_ts: int) -> None:
        """按有效配置完成一轮采样，并按查询参数组合去重。"""
        storages = await list_group_storages(data_dir)
        if not storages:
            return
        global_settings = await get_global_settings(storages[0])
        query_map: Dict[tuple[str, float, float], list[tuple[GroupStorage, str, int]]] = {}

        for storage in storages:
            try:
                effective = await get_effective_settings(storage)
                if not effective.trend_sampling_enabled:
                    continue
                servers = await get_all_servers(storage)
                for sid, sinfo in servers.items():
                    host = (sinfo or {}).get("host")
                    if not host:
                        continue
                    key = (
                        str(host),
                        float(effective.mc_lookup_timeout_seconds),
                        float(effective.mc_status_timeout_seconds),
                    )
                    query_map.setdefault(key, []).append(
                        (storage, str(sid), effective.max_history_points)
                    )
            except Exception as exc:
                logger.warning(
                    f"数据采样预处理失败: group={storage.group_id}: {exc}"
                )

        query_items = list(query_map.items())
        statuses = await gather_limited(
            (
                lambda key=key: call_status_fetcher(
                    get_server_status,
                    key[0],
                    lookup_timeout=key[1],
                    status_timeout=key[2],
                )
                for key, _ in query_items
            ),
            global_settings.max_concurrent_queries,
        )
        for ((host, _, _), targets), status in zip(query_items, statuses):
            if isinstance(status, Exception):
                logger.debug(f"host 采样失败 host={host}: {status}")
                continue
            online = status.get("plays_online") if isinstance(status, dict) else None
            if isinstance(online, bool) or not isinstance(online, int):
                continue
            for storage, sid, max_history_points in targets:
                try:
                    written = await append_trend_point(
                        storage,
                        sid,
                        now_ts,
                        int(online),
                        max_history_points=max_history_points,
                    )
                except Exception as exc:
                    logger.debug(
                        f"写入柱状图数据异常 host={host} group={storage.group_id} sid={sid}: {exc}"
                    )
                    continue
                if not written:
                    logger.debug(
                        f"写入柱状图数据失败 host={host} group={storage.group_id} sid={sid}"
                    )

    async def _bar_data_loop(self):
        """每小时采样一次；配置变更只唤醒重算，不重复当前整点。"""
        settings_changed = getattr(self, "_settings_changed_event", None)
        if settings_changed is None:
            settings_changed = asyncio.Event()
            self._settings_changed_event = settings_changed
        last_sampled_bucket: Optional[int] = None

        while True:
            try:
                now = datetime.now()
                now_ts = int(now.timestamp())
                current_bucket = now_ts // 3600 * 3600
                if current_bucket != last_sampled_bucket:
                    data_dir = Path(
                        StarTools.get_data_dir("astrbot_zhouyi_plugin")
                    ).expanduser()
                    await self._sample_trends_once(data_dir, now_ts)
                    last_sampled_bucket = current_bucket

                    last_sampled_bucket = current_bucket

                now = datetime.now()
                next_hour = (
                    now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=1)
                )
                wait_seconds = max(1.0, (next_hour - now).total_seconds())
                try:
                    await asyncio.wait_for(
                        settings_changed.wait(),
                        timeout=wait_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                else:
                    settings_changed.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"数据采样循环异常: {exc}")
                try:
                    await asyncio.wait_for(settings_changed.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
                else:
                    settings_changed.clear()

    async def _run_standalone_web(self):
        try:
            await self._standalone_service.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Minecraft Manager 独立页面启动失败", exc_info=True)

    async def terminate(self):
        """插件重载或停用时停止独立页面和每小时趋势采样任务。"""
        await self._standalone_service.stop()

        standalone_task = self._standalone_task
        self._standalone_task = None
        if standalone_task:
            await asyncio.gather(standalone_task, return_exceptions=True)

        trend_task = self._trend_task
        self._trend_task = None
        settings_changed = getattr(self, "_settings_changed_event", None)
        if settings_changed is not None:
            settings_changed.set()
        if trend_task:
            if not trend_task.done():
                trend_task.cancel()
            await asyncio.gather(trend_task, return_exceptions=True)
