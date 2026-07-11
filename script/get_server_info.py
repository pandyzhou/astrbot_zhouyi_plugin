import asyncio
import base64
import re
import socket
from pathlib import Path

import aiohttp
from astrbot.api import logger
from mcstatus import JavaServer

csu_host = "csu-mc.org"
csu_get_players = "https://map.magicalsheep.cn/tiles/players.json"
MC_LOOKUP_TIMEOUT = 3.0
MC_STATUS_TIMEOUT = 7.0
CSU_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5.0, connect=3.0, sock_read=4.0)


async def get_server_status(host):
    try:
        server = await asyncio.wait_for(
            JavaServer.async_lookup(host), timeout=MC_LOOKUP_TIMEOUT
        )
        status = await asyncio.wait_for(
            server.async_status(), timeout=MC_STATUS_TIMEOUT
        )
        players_list = []
        latency = int(status.latency)
        plays_max = status.players.max
        plays_online = status.players.online
        server_version = status.version.name

        if status.icon:
            icon_data = status.icon.split(",", 1)[-1]
        else:
            image_path = (
                Path(__file__).resolve().parent.parent
                / "resource"
                / "default_icon.png"
            )
            with open(image_path, "rb") as image_file:
                icon_data = base64.b64encode(image_file.read()).decode("utf-8")

        if status.players.sample:
            players_list.extend(player.name for player in status.players.sample)

        # CSU 定制逻辑保持不变，仅为外部 HTTP 请求增加明确超时。
        if host == csu_host:
            players_list = await fetch_players_names(csu_get_players)

        players_list.sort()
        return {
            "players_list": players_list,
            "latency": latency,
            "plays_max": plays_max,
            "plays_online": plays_online,
            "server_version": server_version,
            "icon_base64": icon_data,
            "host": host,
        }
    except (socket.gaierror, ConnectionRefusedError) as exc:
        logger.error(f"连接服务器失败: {exc}")
        return None
    except asyncio.TimeoutError:
        logger.error("获取服务器状态超时")
        return None
    except Exception as exc:
        logger.error(f"获取服务器状态时发生未知错误: {exc}")
        return None


async def fetch_players_names(url: str) -> list[str]:
    """获取 CSU 玩家列表，并过滤 bot_ 前缀玩家。"""
    async with aiohttp.ClientSession(timeout=CSU_HTTP_TIMEOUT) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ValueError(f"请求失败，状态码: {response.status}")
            data = await response.json()
            names = [player["name"] for player in data.get("players", [])]
            pattern = re.compile(r"^bot_")
            return [name for name in names if not pattern.match(name)]


async def main():
    result = await get_server_status(csu_host)
    if result:
        print(result["players_list"])
    else:
        print("未获取到服务器状态信息")


if __name__ == "__main__":
    asyncio.run(main())
