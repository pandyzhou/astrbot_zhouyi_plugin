"""
统计信息处理模块
"""

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.core.umo_alias import build_umo_alias_map, parse_umo, serialize_umo_alias

if TYPE_CHECKING:
    from .utils import PageApiUtils


class StatsHandler:
    """统计信息处理器"""

    _INVALID_DISPLAY_NAMES = {"n/a", "na", "unknown", "none", "null", "undefined", "未知"}

    def __init__(self, utils: "PageApiUtils", context: Any | None = None):
        """
        初始化统计处理器

        Args:
            utils: PageApiUtils 工具实例
            context: AstrBot Context，可选
        """
        self.utils = utils
        self.context = context

    @classmethod
    def _parse_group_session(cls, session_id: Any) -> tuple[str, str] | None:
        if not isinstance(session_id, str):
            return None
        raw_parts = session_id.split(":", 2)
        if len(raw_parts) != 3 or not raw_parts[0] or not raw_parts[1]:
            return None
        parsed = parse_umo(session_id)
        group_id = parsed.get("session_id", "")
        if parsed.get("message_type") != "GroupMessage" or not group_id.strip():
            return None
        return session_id, group_id

    @classmethod
    def _readable_display_name(
        cls, display_name: Any, *, session_id: str, group_id: str
    ) -> str | None:
        name = str(display_name or "").strip()
        if (
            not name
            or name.lower() in cls._INVALID_DISPLAY_NAMES
            or name == group_id
            or name == session_id
        ):
            return None
        return name

    async def _build_recall_sessions(self, session_data: Any) -> list[dict[str, Any]]:
        if not isinstance(session_data, dict):
            return []

        parsed_sessions: list[tuple[str, str, Any]] = []
        for session_id, message_count in session_data.items():
            parsed = self._parse_group_session(session_id)
            if parsed is not None:
                parsed_sessions.append((parsed[0], parsed[1], message_count))

        alias_map: dict[str, Any] = {}
        valid_umos = [session_id for session_id, _, _ in parsed_sessions]
        if valid_umos and self.context is not None:
            try:
                get_db = getattr(self.context, "get_db", None)
                if callable(get_db):
                    aliases = await get_db().get_umo_aliases(valid_umos)
                    alias_map = build_umo_alias_map(aliases)
            except Exception as exc:
                logger.warning(f"[PageAPI] 批量读取 UMO alias 失败，使用群号降级: {exc}")

        recall_sessions = []
        for session_id, group_id, message_count in parsed_sessions:
            alias = alias_map.get(session_id)
            serialized = serialize_umo_alias(alias, session_id)
            display_name = self._readable_display_name(
                serialized.get("display_name"),
                session_id=session_id,
                group_id=group_id,
            )
            recall_sessions.append(
                {
                    "session_id": session_id,
                    "group_id": group_id,
                    "display_name": display_name,
                    "message_count": message_count,
                }
            )

        return sorted(
            recall_sessions,
            key=lambda item: (-item["message_count"], item["session_id"]),
        )

    async def get_stats(self, memory_engine) -> dict[str, Any]:
        """
        获取插件统计信息

        包括：
        - 记忆总数、会话统计
        - 图谱节点、边、入口统计
        - 原子统计
        - 重要性分布
        - 最近会话列表

        Args:
            memory_engine: 记忆引擎实例

        Returns:
            包含统计信息的字典
        """
        try:
            stats = await memory_engine.get_statistics()

            # 使用专用的 COUNT(*) 统计，确保显示完整图谱总数
            graph_store = self.utils.get_graph_store(memory_engine)
            if graph_store is not None:
                try:
                    entry_stats = await graph_store.get_memory_entry_stats()
                    stats["graph_nodes"] = entry_stats.get("graph_nodes", 0)
                    stats["graph_edges"] = entry_stats.get("graph_edges", 0)
                    stats["graph_entries"] = entry_stats.get("graph_entries", 0)
                except Exception:
                    stats["graph_nodes"] = 0
                    stats["graph_edges"] = 0
                    stats["graph_entries"] = 0
            else:
                stats["graph_nodes"] = 0
                stats["graph_edges"] = 0
                stats["graph_entries"] = 0

            # 原子统计 (if available)
            atom_store = getattr(memory_engine, "atom_store", None)
            stats["atom_count"] = 0
            stats["atom_breakdown"] = {}
            if atom_store is not None:
                try:
                    stats["atom_count"] = await atom_store.count_atoms() or 0
                except Exception:
                    pass
                try:
                    stats["atom_breakdown"] = await atom_store.count_by_type()
                except Exception:
                    pass

            # 重要性分布 — 兜底默认值（get_statistics 已计算，此处仅容错）
            if "importance_distribution" not in stats:
                stats["importance_distribution"] = {
                    "0-1": 0,
                    "1-2": 0,
                    "2-3": 0,
                    "3-4": 0,
                    "4-5": 0,
                    "5-6": 0,
                    "6-7": 0,
                    "7-8": 0,
                    "8-9": 0,
                    "9-10": 0,
                }

            # 最近会话从 sessions 统计数据派生
            session_data = stats.get("sessions", {})
            stats["recent_sessions"] = (
                [
                    {"session_id": sid, "message_count": cnt}
                    for sid, cnt in sorted(session_data.items(), key=lambda x: -x[1])[
                        :10
                    ]
                ]
                if isinstance(session_data, dict)
                else []
            )
            stats["recall_sessions"] = await self._build_recall_sessions(session_data)

            return self.utils.ok(stats)
        except Exception as exc:
            logger.error(f"[PageAPI] 获取统计信息失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))
