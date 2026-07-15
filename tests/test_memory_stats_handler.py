from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules.stats_handler import (
    StatsHandler,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules.utils import PageApiUtils


class _MemoryEngine:
    graph_store = None
    atom_store = None

    def __init__(self, stats):
        self.stats = stats

    async def get_statistics(self):
        return dict(self.stats)


class _AliasDb:
    def __init__(self, aliases=None, error: Exception | None = None):
        self.aliases = aliases or []
        self.error = error
        self.requests = []

    async def get_umo_aliases(self, umos):
        self.requests.append(list(umos))
        if self.error is not None:
            raise self.error
        return self.aliases


class _Context:
    def __init__(self, db):
        self.db = db

    def get_db(self):
        return self.db


class StatsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_sessions_strictly_filter_group_umos_and_keep_old_fields(self):
        sessions = {
            "qq:GroupMessage:200:extra": 8,
            "qq:GroupMessage:100": 8,
            "qq:GroupMessage:300": 3,
            "qq:GroupMessage:400": 2,
            "qq:GroupMessage:500": 1,
            "qq:FriendMessage:user": 20,
            "bare-id": 19,
            "qq:GroupMessage:": 18,
            ":GroupMessage:600": 17,
            "qq::700": 16,
        }
        aliases = [
            SimpleNamespace(
                umo="qq:GroupMessage:200:extra",
                auto_name="自动群名",
                user_alias="用户别名",
                creator_sender_id="creator",
            ),
            SimpleNamespace(
                umo="qq:GroupMessage:100",
                auto_name="群一",
                user_alias="",
                creator_sender_id="creator",
            ),
            SimpleNamespace(
                umo="qq:GroupMessage:300",
                auto_name="N/A",
                user_alias=None,
                creator_sender_id="creator",
            ),
            SimpleNamespace(
                umo="qq:GroupMessage:400",
                auto_name="400",
                user_alias=None,
                creator_sender_id="creator",
            ),
            SimpleNamespace(
                umo="qq:GroupMessage:500",
                auto_name="qq:GroupMessage:500",
                user_alias=None,
                creator_sender_id="creator",
            ),
        ]
        db = _AliasDb(aliases)
        handler = StatsHandler(PageApiUtils(), _Context(db))

        result = await handler.get_stats(
            _MemoryEngine({"sessions": sessions, "total_memories": 9})
        )

        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertEqual(data["sessions"], sessions)
        self.assertEqual(
            data["recent_sessions"],
            [
                {"session_id": "qq:FriendMessage:user", "message_count": 20},
                {"session_id": "bare-id", "message_count": 19},
                {"session_id": "qq:GroupMessage:", "message_count": 18},
                {"session_id": ":GroupMessage:600", "message_count": 17},
                {"session_id": "qq::700", "message_count": 16},
                {"session_id": "qq:GroupMessage:200:extra", "message_count": 8},
                {"session_id": "qq:GroupMessage:100", "message_count": 8},
                {"session_id": "qq:GroupMessage:300", "message_count": 3},
                {"session_id": "qq:GroupMessage:400", "message_count": 2},
                {"session_id": "qq:GroupMessage:500", "message_count": 1},
            ],
        )
        self.assertEqual(
            data["recall_sessions"],
            [
                {
                    "session_id": "qq:GroupMessage:100",
                    "group_id": "100",
                    "display_name": "群一",
                    "message_count": 8,
                },
                {
                    "session_id": "qq:GroupMessage:200:extra",
                    "group_id": "200:extra",
                    "display_name": "用户别名",
                    "message_count": 8,
                },
                {
                    "session_id": "qq:GroupMessage:300",
                    "group_id": "300",
                    "display_name": None,
                    "message_count": 3,
                },
                {
                    "session_id": "qq:GroupMessage:400",
                    "group_id": "400",
                    "display_name": None,
                    "message_count": 2,
                },
                {
                    "session_id": "qq:GroupMessage:500",
                    "group_id": "500",
                    "display_name": None,
                    "message_count": 1,
                },
            ],
        )
        self.assertEqual(
            db.requests,
            [[
                "qq:GroupMessage:200:extra",
                "qq:GroupMessage:100",
                "qq:GroupMessage:300",
                "qq:GroupMessage:400",
                "qq:GroupMessage:500",
            ]],
        )

    async def test_alias_db_failure_degrades_without_failing_stats(self):
        db = _AliasDb(error=RuntimeError("alias db failed"))
        handler = StatsHandler(PageApiUtils(), _Context(db))

        result = await handler.get_stats(
            _MemoryEngine({"sessions": {"qq:GroupMessage:100": 2}})
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["recall_sessions"],
            [
                {
                    "session_id": "qq:GroupMessage:100",
                    "group_id": "100",
                    "display_name": None,
                    "message_count": 2,
                }
            ],
        )

    async def test_optional_context_keeps_stats_available(self):
        result = await StatsHandler(PageApiUtils()).get_stats(
            _MemoryEngine({"sessions": {"qq:GroupMessage:100": 1}})
        )

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["data"]["recall_sessions"][0]["display_name"])


if __name__ == "__main__":
    unittest.main()
