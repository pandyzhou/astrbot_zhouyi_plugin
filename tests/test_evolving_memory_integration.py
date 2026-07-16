from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from astrbot.api.platform import MessageType

from data.plugins.astrbot_zhouyi_plugin.memory.core.base.config_manager import ConfigManager
from data.plugins.astrbot_zhouyi_plugin.memory.core.event_handler_modules.memory_recall import (
    MemoryRecall,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import (
    EvolvingMemoryManager,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.memory_engine import MemoryEngine
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_bootstrap import MemoryBootstrap
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_commands import MemoryCommands
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import (
    IndexStatus,
    MemoryActorType,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.processors.memory_processor import (
    MemoryProcessor,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.retrieval.graph_keyword_retriever import (
    GraphKeywordResult,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.retrieval.graph_retriever import (
    GraphRetriever,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.retrieval.rrf_fusion import RRFFusion
from data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_memorize_tool import (
    MemoryMemorizeTool,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_search_tool import (
    MemorySearchTool,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.utils import (
    RECALL_TRACE_EXTRA_KEY,
    build_access_context_from_event,
)
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import (
    EvolvingMemoryStore,
)


class _RuntimeEvent:
    def __init__(
        self,
        *,
        sender_id: str = "10001",
        self_id: str = "bot-a",
        umo: str = "qq:FriendMessage:10001",
        is_group: bool = False,
        message: str = "请回忆草莓蛋糕",
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = message
        self.message_obj = SimpleNamespace(
            self_id=self_id,
            message_id="123",
            sender=SimpleNamespace(user_id=sender_id),
        )
        self._sender_id = sender_id
        self._is_group = is_group
        self._extras = {}

    def get_platform_name(self):
        return "qq"

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return self._sender_id

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE if self._is_group else MessageType.FRIEND_MESSAGE

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return []

    def plain_result(self, text):
        return text

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)


class EvolvingMemoryRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.db_path = str(Path(self.temp_dir.name) / "runtime.db")
        self.store = EvolvingMemoryStore(self.db_path)
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.engine = MemoryEngine(
            db_path=self.db_path,
            faiss_db=None,
            graph_vector_db=None,
            llm_provider=None,
            config={
                "graph_memory_enabled": True,
                "atom_enabled": True,
                "atom_maintenance_interval_hours": 24,
                "search_cache_enabled": True,
                "search_cache_ttl_seconds": 300,
            },
            evolving_memory_store=self.store,
            evolving_memory_manager=self.manager,
        )
        await self.engine.initialize()
        await self.engine.text_processor.async_init()
        self.private = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        self.other = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10002",
            session_id="qq:FriendMessage:10002",
            persona_id="persona-a",
            is_group=False,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp_dir.cleanup()

    async def _create(self, context, content: str, operation_key: str):
        return await self.manager.create(
            context=context,
            content=content,
            operation_key=operation_key,
            actor_type=MemoryActorType.USER,
            actor_id="runtime-test",
        )

    async def test_event_access_context_keeps_same_qq_owner_and_rejects_missing_identity(self):
        private_event = _RuntimeEvent()
        group_event = _RuntimeEvent(
            umo="qq:GroupMessage:20001",
            is_group=True,
        )
        other_event = _RuntimeEvent(sender_id="10002", umo="qq:FriendMessage:10002")

        private_context = await build_access_context_from_event(
            private_event, self.manager, persona_id="persona-a"
        )
        group_context = await build_access_context_from_event(
            group_event, self.manager, persona_id="persona-a"
        )
        other_context = await build_access_context_from_event(
            other_event, self.manager, persona_id="persona-a"
        )
        missing_context = await build_access_context_from_event(
            _RuntimeEvent(sender_id=""), self.manager, persona_id="persona-a"
        )

        self.assertEqual(private_context.owner_user_id, group_context.owner_user_id)
        self.assertNotEqual(private_context.session_id, group_context.session_id)
        self.assertNotEqual(private_context.owner_user_id, other_context.owner_user_id)
        self.assertIsNone(missing_context)

    async def test_automatic_and_tool_recall_pass_access_context_and_append_trace(self):
        event = _RuntimeEvent(umo="qq:GroupMessage:20001", is_group=True)
        access_context = await build_access_context_from_event(
            event, self.manager, persona_id="persona-a"
        )
        recalled = SimpleNamespace(
            doc_id=-1,
            memory_item_id="mem-runtime-trace",
            version=3,
            source_type="memory_item",
            content="用户喜欢草莓蛋糕",
            final_score=0.91,
            metadata={
                "scope": "session",
                "importance": 0.8,
                "session_id": event.unified_msg_origin,
                "persona_id": "persona-a",
            },
        )
        engine = SimpleNamespace(
            evolving_memory_manager=self.manager,
            search_memories=AsyncMock(return_value=[recalled]),
        )
        config = ConfigManager({})
        recall = MemoryRecall(
            context=SimpleNamespace(),
            config_manager=config,
            memory_engine=engine,
            conversation_manager=SimpleNamespace(),
            message_utils=SimpleNamespace(
                get_event_message_str=AsyncMock(return_value=event.message_str),
            ),
            injection_adapter=SimpleNamespace(
                resolve=lambda _provider, method: (method, None)
            ),
        )
        request = SimpleNamespace(
            prompt=event.message_str,
            extra_user_content_parts=[],
            contexts=[],
        )
        with patch(
            "data.plugins.astrbot_zhouyi_plugin.memory.core.event_handler_modules.memory_recall.get_persona_id",
            AsyncMock(return_value="persona-a"),
        ):
            await recall.handle_memory_recall(event, request)

        self.assertTrue(request.extra_user_content_parts)
        self.assertIs(
            engine.search_memories.await_args.kwargs["access_context"].__class__,
            access_context.__class__,
        )
        trace = event.get_extra(RECALL_TRACE_EXTRA_KEY)
        self.assertEqual(trace[0]["memory_item_id"], "mem-runtime-trace")
        self.assertEqual(trace[0]["version"], 3)
        self.assertEqual(trace[0]["source_type"], "memory_item")
        self.assertEqual(
            trace[0]["access_context"]["owner_user_id"], access_context.owner_user_id
        )

        tool_event = _RuntimeEvent(umo="qq:GroupMessage:20001", is_group=True)
        search_tool = MemorySearchTool(
            context=SimpleNamespace(),
            config_manager=config,
            memory_engine=engine,
        )
        with patch(
            "data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_search_tool.get_persona_id",
            AsyncMock(return_value="persona-a"),
        ):
            payload = await search_tool.call(
                SimpleNamespace(context=SimpleNamespace(event=tool_event)),
                query="草莓蛋糕",
                k=5,
            )
        self.assertEqual(json.loads(payload)["results"][0]["memory_item_id"], "mem-runtime-trace")
        self.assertEqual(
            tool_event.get_extra(RECALL_TRACE_EXTRA_KEY)[0]["recall_source"],
            "agent_tool",
        )

        command_event = _RuntimeEvent(umo="qq:GroupMessage:20001", is_group=True)
        commands = MemoryCommands(
            context=SimpleNamespace(),
            config_manager=config,
            memory_engine=engine,
            conversation_manager=None,
            index_validator=None,
        )
        engine.search_memories.reset_mock()
        with patch(
            "data.plugins.astrbot_zhouyi_plugin.memory.core.memory_commands.get_persona_id",
            AsyncMock(return_value="persona-a"),
        ):
            command_results = [
                result async for result in commands.handle_search(command_event, "草莓蛋糕", 5)
            ]
        self.assertTrue(command_results)
        self.assertIsNotNone(
            engine.search_memories.await_args.kwargs["access_context"]
        )
        self.assertEqual(
            command_event.get_extra(RECALL_TRACE_EXTRA_KEY)[0]["recall_source"],
            "command",
        )

    async def test_memorize_tool_uses_manager_object_layer_and_projection(self):
        event = _RuntimeEvent(message="请记住我喜欢看星星")
        tool = MemoryMemorizeTool(
            context=SimpleNamespace(),
            memory_engine=self.engine,
            memory_processor=MemoryProcessor(llm_provider=None),
        )
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_memorize_tool.get_persona_id",
                AsyncMock(return_value="persona-a"),
            ),
            patch.object(self.manager, "create", wraps=self.manager.create) as create,
        ):
            payload = await tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                memory="用户喜欢看星星",
                key_facts=["用户喜欢看星星"],
            )

        result = json.loads(payload)
        self.assertTrue(result["memorized"])
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["needs_repair"])
        self.assertEqual(result["projection_status"], IndexStatus.CURRENT.value)
        self.assertTrue(result["memory_item_id"].startswith("mem_"))
        self.assertIsNotNone(result["id"])
        create.assert_awaited_once()
        item = await self.store.get_item(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=result["memory_item_id"],
        )
        self.assertEqual(item.current_document_id, result["id"])

    async def test_memorize_operation_key_isolated_by_persona_scope_and_session(self):
        event = _RuntimeEvent(message="请记住我喜欢看星星")
        tool = MemoryMemorizeTool(
            context=SimpleNamespace(),
            memory_engine=self.engine,
            memory_processor=MemoryProcessor(llm_provider=None),
        )
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_memorize_tool.get_persona_id",
                AsyncMock(side_effect=["persona-a", "persona-b"]),
            ),
            patch.object(self.manager, "create", wraps=self.manager.create) as create,
        ):
            first_payload = await tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                memory="用户喜欢看星星",
            )
            second_payload = await tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                memory="用户喜欢看星星",
            )

        first = json.loads(first_payload)
        second = json.loads(second_payload)
        self.assertTrue(first["memorized"])
        self.assertTrue(second["memorized"])
        self.assertNotEqual(first["memory_item_id"], second["memory_item_id"])
        operation_keys = [
            awaited.kwargs["operation_key"] for awaited in create.await_args_list
        ]
        self.assertEqual(len(operation_keys), 2)
        self.assertNotEqual(operation_keys[0], operation_keys[1])
        self.assertEqual(create.await_args_list[0].kwargs["scope"].value, "persona")
        self.assertEqual(create.await_args_list[1].kwargs["scope"].value, "persona")

    async def test_memorize_projection_failure_returns_partial_success(self):
        event = _RuntimeEvent(message="请记住投影失败也要保留对象")
        tool = MemoryMemorizeTool(
            context=SimpleNamespace(),
            memory_engine=self.engine,
            memory_processor=MemoryProcessor(llm_provider=None),
        )
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.tools.memory_memorize_tool.get_persona_id",
                AsyncMock(return_value="persona-a"),
            ),
            patch.object(
                self.engine,
                "add_memory",
                AsyncMock(side_effect=RuntimeError("projection boom")),
            ),
        ):
            payload = await tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                memory="投影失败仍保留的工具记忆",
            )

        result = json.loads(payload)
        self.assertTrue(result["memorized"])
        self.assertEqual(result["status"], "partial_success")
        self.assertTrue(result["needs_repair"])
        self.assertEqual(result["projection_status"], IndexStatus.NEEDS_REPAIR.value)
        self.assertEqual(result["projection_error"], "projection boom")
        self.assertIsNone(result["id"])
        item = await self.store.get_item(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=result["memory_item_id"],
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.index_status, IndexStatus.NEEDS_REPAIR)

    async def test_bm25_only_projection_dedup_stale_and_graph_atom_links(self):
        runtime = self.engine.get_runtime_status()
        self.assertEqual(runtime["retrieval_mode"], "bm25_only")
        self.assertEqual(runtime["llm_mode"], "no_llm")
        self.assertEqual(runtime["degraded_states"], ["bm25_only", "no_llm"])

        created = await self._create(
            self.private, "用户喜欢草莓蛋糕", "runtime:create:projection"
        )
        item_id = created.item.memory_item_id
        old_document_id = created.item.current_document_id
        self.assertIsNotNone(old_document_id)
        self.assertEqual(created.projection_status, IndexStatus.CURRENT)

        results = await self.engine.search_memories(
            "草莓蛋糕", k=10, access_context=self.private
        )
        matching = [result for result in results if result.memory_item_id == item_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].source_type, "memory_item")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            atom = await (
                await db.execute(
                    """
                    SELECT memory_item_id, memory_revision_no, metadata
                    FROM memory_atoms WHERE parent_memory_id = ?
                    """,
                    (old_document_id,),
                )
            ).fetchone()
            graph = await (
                await db.execute(
                    """
                    SELECT memory_item_id, memory_revision_no, projection_status, metadata
                    FROM graph_entries WHERE source_memory_id = ? LIMIT 1
                    """,
                    (old_document_id,),
                )
            ).fetchone()
        self.assertEqual(atom["memory_item_id"], item_id)
        self.assertEqual(int(atom["memory_revision_no"]), 1)
        self.assertEqual(json.loads(atom["metadata"])["owner_user_id"], self.private.owner_user_id)
        self.assertEqual(graph["memory_item_id"], item_id)
        self.assertEqual(int(graph["memory_revision_no"]), 1)
        self.assertEqual(graph["projection_status"], "current")

        updated = await self.manager.update(
            context=self.private,
            memory_item_id=item_id,
            expected_version=created.item.version,
            operation_key="runtime:update:projection",
            actor_type=MemoryActorType.USER,
            actor_id="runtime-test",
            content="用户现在最喜欢草莓蛋糕",
        )
        new_document_id = updated.item.current_document_id
        self.assertIsNotNone(new_document_id)
        self.assertNotEqual(old_document_id, new_document_id)

        old_document = await self.engine.get_memory(old_document_id)
        self.assertEqual(old_document["metadata"]["projection_status"], "stale")
        async with aiosqlite.connect(self.db_path) as db:
            old_fts = await (
                await db.execute(
                    "SELECT COUNT(*) FROM livingmemory_memories_fts WHERE doc_id = ?",
                    (old_document_id,),
                )
            ).fetchone()
            old_graph = await (
                await db.execute(
                    "SELECT COUNT(*) FROM graph_entries WHERE source_memory_id = ? AND projection_status = 'stale'",
                    (old_document_id,),
                )
            ).fetchone()
            old_atoms = await (
                await db.execute(
                    "SELECT COUNT(*) FROM memory_atoms WHERE parent_memory_id = ? AND status = 'superseded'",
                    (old_document_id,),
                )
            ).fetchone()
        self.assertEqual(int(old_fts[0]), 0)
        self.assertGreater(int(old_graph[0]), 0)
        self.assertGreater(int(old_atoms[0]), 0)

        current_document = await self.engine.get_memory(new_document_id)
        current_metadata = dict(current_document["metadata"])
        current_metadata.update({"create_time": 0.0, "importance": 0.0})
        await self.engine.hybrid_retriever.update_metadata(new_document_id, current_metadata)
        await self.engine.cleanup_old_memories(days_threshold=0, importance_threshold=1.0)
        self.assertIsNotNone(await self.engine.get_memory(new_document_id))

    async def test_owner_cache_isolation_and_generation_invalidation(self):
        first = await self._create(
            self.private, "owner-a 喜欢蓝色星星", "runtime:create:owner-a"
        )
        second = await self._create(
            self.other, "owner-b 喜欢蓝色星星", "runtime:create:owner-b"
        )

        first_results = await self.engine.search_memories(
            "蓝色星星", k=10, access_context=self.private
        )
        second_results = await self.engine.search_memories(
            "蓝色星星", k=10, access_context=self.other
        )
        self.assertIn(first.item.memory_item_id, {item.memory_item_id for item in first_results})
        self.assertNotIn(second.item.memory_item_id, {item.memory_item_id for item in first_results})
        self.assertIn(second.item.memory_item_id, {item.memory_item_id for item in second_results})
        self.assertNotIn(first.item.memory_item_id, {item.memory_item_id for item in second_results})

        await self.manager.update(
            context=self.private,
            memory_item_id=first.item.memory_item_id,
            expected_version=first.item.version,
            operation_key="runtime:update:owner-a",
            actor_type=MemoryActorType.USER,
            actor_id="runtime-test",
            content="owner-a 现在喜欢金色月亮",
        )
        refreshed = await self.engine.search_memories(
            "蓝色星星", k=10, access_context=self.private
        )
        self.assertNotIn(first.item.memory_item_id, {item.memory_item_id for item in refreshed})

    async def test_route_failures_are_isolated_and_cancelled_error_propagates(self):
        legacy_id = await self.engine.add_memory(
            "当前会话的 legacy 安全事实",
            session_id=self.private.session_id,
            persona_id=self.private.persona_id,
            metadata={"owner_user_id": self.private.owner_user_id},
        )
        with patch.object(
            self.engine.evolving_memory_retriever,
            "search",
            AsyncMock(side_effect=RuntimeError("item route boom")),
        ):
            results = await self.engine.search_memories(
                "legacy 安全事实", k=10, access_context=self.private
            )
        self.assertIn(legacy_id, {item.doc_id for item in results})

        item = await self._create(
            self.private, "仅对象路可见的极光偏好", "runtime:create:item-route"
        )
        with patch.object(
            self.engine.dual_route_retriever,
            "search",
            AsyncMock(side_effect=RuntimeError("legacy route boom")),
        ):
            results = await self.engine.search_memories(
                "极光偏好", k=10, access_context=self.private
            )
        self.assertIn(item.item.memory_item_id, {result.memory_item_id for result in results})

        with patch.object(
            self.engine.evolving_memory_retriever,
            "search",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine.search_memories(
                    "取消测试", k=5, access_context=self.private
                )

    async def test_bm25_only_legacy_crud_and_access_boundary(self):
        document_id = await self.engine.add_memory(
            "owner-a 的橙色风筝",
            session_id=self.private.session_id,
            persona_id=self.private.persona_id,
            metadata={"owner_user_id": self.private.owner_user_id},
        )
        own_results = await self.engine.search_memories(
            "橙色风筝", k=10, access_context=self.private
        )
        other_results = await self.engine.search_memories(
            "橙色风筝", k=10, access_context=self.other
        )
        self.assertIn(document_id, {item.doc_id for item in own_results})
        self.assertNotIn(document_id, {item.doc_id for item in other_results})

        self.assertTrue(
            await self.engine.update_memory(document_id, {"content": "owner-a 的金色风筝"})
        )
        self.assertIsNone(await self.engine.get_memory(document_id))
        revised = await self.engine.search_memories(
            "金色风筝", k=10, access_context=self.private
        )
        revised_ids = [item.doc_id for item in revised if item.source_type.startswith("legacy")]
        self.assertTrue(revised_ids)
        self.assertTrue(await self.engine.delete_memory(revised_ids[0]))
        self.assertIsNone(await self.engine.get_memory(revised_ids[0]))

        isolated_id = await self.engine.add_memory(
            "只有原会话可见的无 owner 文档",
            session_id=self.private.session_id,
            persona_id=self.private.persona_id,
        )
        isolated_other = await self.engine.search_memories(
            "无 owner 文档", k=10, access_context=self.other
        )
        self.assertNotIn(isolated_id, {item.doc_id for item in isolated_other})

    async def test_projection_failure_marks_needs_repair_but_canonical_item_is_searchable(self):
        with patch.object(
            self.engine,
            "add_memory",
            AsyncMock(side_effect=RuntimeError("projection boom")),
        ):
            created = await self._create(
                self.private, "投影失败仍保留的事实", "runtime:create:repair"
            )
        self.assertEqual(created.projection_status, IndexStatus.NEEDS_REPAIR)
        self.assertIsNone(created.item.current_document_id)

        results = await self.engine.search_memories(
            "投影失败仍保留", k=10, access_context=self.private
        )
        self.assertIn(created.item.memory_item_id, {item.memory_item_id for item in results})

    async def test_archived_item_and_projection_are_excluded(self):
        created = await self._create(
            self.private, "将被归档的旧偏好", "runtime:create:archive"
        )
        archived = await self.manager.archive(
            context=self.private,
            memory_item_id=created.item.memory_item_id,
            expected_version=created.item.version,
            operation_key="runtime:archive",
            actor_type=MemoryActorType.USER,
            actor_id="runtime-test",
        )
        self.assertEqual(archived.item.status.value, "archived")
        results = await self.engine.search_memories(
            "旧偏好", k=10, access_context=self.private
        )
        self.assertNotIn(created.item.memory_item_id, {item.memory_item_id for item in results})


class RetrievalFailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_vector_failure_keeps_keyword_results(self):
        keyword = SimpleNamespace(
            search=AsyncMock(
                return_value=[
                    GraphKeywordResult(
                        doc_id=7,
                        score=1.0,
                        content="keyword survives",
                        metadata={"importance": 0.8},
                    )
                ]
            )
        )
        vector = SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("vector boom")))
        retriever = GraphRetriever(keyword, vector, RRFFusion(k=60), {})
        results = await retriever.search("survives", k=5)
        self.assertEqual([item.doc_id for item in results], [7])
        self.assertIsNone(results[0].vector_score)


class BootstrapDegradedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_starts_without_providers_and_exposes_manager(self):
        class Context:
            def get_all_embedding_providers(self):
                return []

            def get_all_providers(self):
                return []

        class Config:
            session_manager = {}

            def get(self, key, default=None):
                values = {
                    "graph_memory.enabled": False,
                    "importance_decay.decay_rate": 0.0,
                    "forgetting_agent.auto_cleanup_enabled": False,
                    "migration_settings.auto_migrate": True,
                }
                return values.get(key, default)

        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            bootstrap = MemoryBootstrap(Context(), Config(), directory)
            self.assertTrue(await bootstrap.initialize())
            self.assertIsNotNone(bootstrap.evolving_memory_manager)
            self.assertIsNotNone(bootstrap.evolving_memory_retriever)
            self.assertIsNone(bootstrap.db)
            runtime = bootstrap.get_runtime_status()
            self.assertEqual(runtime["retrieval_mode"], "bm25_only")
            self.assertEqual(runtime["llm_mode"], "no_llm")
            await bootstrap.stop_background_tasks()
            await bootstrap.cleanup_runtime_resources()

    async def test_backfill_task_is_tracked_and_shutdown_cancels_it(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def backfill():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        bootstrap = MemoryBootstrap(
            SimpleNamespace(),
            SimpleNamespace(get=lambda _key, default=None: default),
            str(Path("temp") / "unused-backfill"),
        )
        bootstrap.evolving_memory_manager = SimpleNamespace(
            backfill_legacy_key_facts=backfill
        )
        bootstrap._start_evolving_backfill_task()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertEqual(bootstrap.get_runtime_status()["key_facts_backfill"]["status"], "running")
        await bootstrap.stop_background_tasks()
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        self.assertIsNone(bootstrap._backfill_task)
        self.assertEqual(bootstrap.get_runtime_status()["key_facts_backfill"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
