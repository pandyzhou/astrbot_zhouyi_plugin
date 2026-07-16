from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.memory.core.base.exceptions import (
    EvolvingMemoryAccessError,
    EvolvingMemoryIdempotencyError,
    EvolvingMemoryVersionConflictError,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import (
    EvolvingMemoryManager,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import (
    ConflictStatus,
    MemoryActorType,
    MemoryFeedback,
    MemoryItemStatus,
    MemoryScope,
    RevisionOperation,
)
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import (
    EvolvingMemoryStore,
)


class EvolvingMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.db_path = str(Path(self.temp_dir.name) / "evolving.db")
        self.store = EvolvingMemoryStore(self.db_path)
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.private = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        self.group = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:GroupMessage:20001",
            persona_id="persona-a",
            is_group=True,
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
        self.temp_dir.cleanup()

    async def _create(self, content: str, operation_key: str, **kwargs):
        return await self.manager.create(
            context=self.private,
            content=content,
            operation_key=operation_key,
            actor_type=MemoryActorType.USER,
            actor_id="tester",
            **kwargs,
        )

    async def test_stable_id_immutable_revisions_and_version_conflict(self):
        created = await self._create("用户喜欢草莓蛋糕", "create:stable")
        item_id = created.item.memory_item_id
        updated = await self.manager.update(
            context=self.private,
            memory_item_id=item_id,
            expected_version=created.item.version,
            operation_key="update:stable",
            actor_type=MemoryActorType.USER,
            actor_id="tester",
            content="用户现在最喜欢草莓蛋糕",
            reason="增加细节",
        )

        self.assertEqual(updated.item.memory_item_id, item_id)
        self.assertEqual(updated.item.version, 2)
        revisions = await self.store.list_revisions(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=item_id,
        )
        self.assertEqual([revision.revision_no for revision in revisions], [2, 1])
        self.assertEqual(revisions[0].content, "用户现在最喜欢草莓蛋糕")
        self.assertEqual(revisions[1].content, "用户喜欢草莓蛋糕")

        with self.assertRaises(EvolvingMemoryVersionConflictError):
            await self.manager.update(
                context=self.private,
                memory_item_id=item_id,
                expected_version=1,
                operation_key="update:stale",
                actor_type=MemoryActorType.USER,
                actor_id="tester",
                content="过期写入",
            )

        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            with self.assertRaises(aiosqlite.IntegrityError):
                await db.execute(
                    "UPDATE memory_item_revisions SET content = ? WHERE memory_item_id = ? AND revision_no = 1",
                    ("篡改", item_id),
                )

    async def test_five_actions_merge_supersede_archive_preserve_history(self):
        survivor = await self._create("用户喜欢南瓜汤", "create:survivor")
        source = await self._create("用户爱喝南瓜浓汤", "create:source")
        merged = await self.manager.merge(
            context=self.private,
            survivor_item_id=survivor.item.memory_item_id,
            source_item_ids=[source.item.memory_item_id],
            expected_versions={
                survivor.item.memory_item_id: survivor.item.version,
                source.item.memory_item_id: source.item.version,
            },
            content="用户喜欢香浓南瓜汤",
            operation_key="merge:one",
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        source_after = await self.store.get_item(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=source.item.memory_item_id,
        )
        self.assertEqual(merged.action.value, "merge")
        self.assertEqual(source_after.status, MemoryItemStatus.SUPERSEDED)
        relations = await self.store.list_relations(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=source.item.memory_item_id,
        )
        self.assertEqual(relations[0].relation_type.value, "merged_into")

        replacement = await self._create("用户改为喜欢玉米汤", "create:replacement")
        superseded = await self.manager.supersede(
            context=self.private,
            old_item_id=merged.item.memory_item_id,
            replacement_item_id=replacement.item.memory_item_id,
            expected_versions={
                merged.item.memory_item_id: merged.item.version,
                replacement.item.memory_item_id: replacement.item.version,
            },
            operation_key="supersede:one",
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        old_after = await self.store.get_item(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=merged.item.memory_item_id,
        )
        self.assertEqual(superseded.action.value, "supersede")
        self.assertEqual(old_after.status, MemoryItemStatus.SUPERSEDED)

        archived = await self.manager.archive(
            context=self.private,
            memory_item_id=replacement.item.memory_item_id,
            expected_version=superseded.item.version,
            operation_key="archive:one",
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        self.assertEqual(archived.item.status, MemoryItemStatus.ARCHIVED)
        history = await self.store.list_revisions(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=replacement.item.memory_item_id,
        )
        self.assertEqual(
            [revision.operation for revision in history],
            [RevisionOperation.ARCHIVE, RevisionOperation.SUPERSEDE, RevisionOperation.CREATE],
        )
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM livingmemory_memory_items_fts WHERE memory_item_id = ?",
                    (replacement.item.memory_item_id,),
                )
            ).fetchone()
            self.assertEqual(int(row[0]), 0)

    async def test_owner_is_hard_boundary_and_identity_is_exact(self):
        self.assertEqual(self.private.owner_user_id, self.group.owner_user_id)
        self.assertNotEqual(self.private.owner_user_id, self.other.owner_user_id)
        created = await self._create("owner 私有事实", "create:owner")

        self.assertIsNone(
            await self.store.get_item(
                owner_user_id=self.other.owner_user_id,
                memory_item_id=created.item.memory_item_id,
            )
        )
        self.assertEqual(await self.store.list_items(context=self.other), [])
        with self.assertRaises((EvolvingMemoryAccessError, Exception)) as caught:
            await self.manager.update(
                context=self.other,
                memory_item_id=created.item.memory_item_id,
                expected_version=created.item.version,
                operation_key="update:cross-owner",
                actor_type=MemoryActorType.USER,
                actor_id="tester",
                content="越权",
            )
        self.assertIsNotNone(caught.exception)

        links = await self.store.list_identity_links(self.private.owner_user_id)
        triples = {
            (link.platform_id, link.bot_id, link.external_user_id) for link in links
        }
        self.assertEqual(triples, {("qq", "bot-a", "10001")})

    async def test_scope_group_safe_public_and_legacy_rules(self):
        private_item = await self._create("私聊偏好", "create:private")
        group_visible = await self.store.list_items(context=self.group)
        self.assertNotIn(
            private_item.item.memory_item_id,
            {item.memory_item_id for item in group_visible},
        )

        safe_item = await self._create(
            "可在群聊使用的偏好",
            "create:group-safe",
            group_safe=True,
        )
        group_visible = await self.store.list_items(context=self.group)
        self.assertIn(
            safe_item.item.memory_item_id,
            {item.memory_item_id for item in group_visible},
        )

        group_created = await self.manager.create(
            context=self.group,
            content="群聊内的临时事实",
            operation_key="create:group-auto",
            actor_type=MemoryActorType.AUTOMATIC,
            actor_id="feedback",
        )
        self.assertEqual(group_created.item.scope, MemoryScope.SESSION)
        self.assertEqual(group_created.item.session_id, self.group.session_id)

        with self.assertRaises(EvolvingMemoryAccessError):
            await self.manager.create(
                context=self.private,
                content="自动公共事实",
                operation_key="create:public-auto",
                scope=MemoryScope.PUBLIC,
                actor_type=MemoryActorType.AUTOMATIC,
                actor_id="feedback",
            )
        with self.assertRaises(EvolvingMemoryAccessError):
            await self.manager.create(
                context=self.private,
                content="非迁移 legacy",
                operation_key="create:legacy-user",
                scope=MemoryScope.LEGACY_SESSION,
                actor_type=MemoryActorType.USER,
                actor_id="tester",
            )

    async def test_exact_canonical_and_fts_deduplication_checks(self):
        original = await self._create(
            "user enjoys hiking on weekends",
            "create:dedup-original",
            canonical_key="preference:weekend-hiking",
        )
        canonical_update = await self._create(
            "user especially enjoys mountain hiking on weekends",
            "create:dedup-canonical",
            canonical_key="preference:weekend-hiking",
        )
        self.assertTrue(canonical_update.deduplicated)
        self.assertEqual(
            canonical_update.item.memory_item_id,
            original.item.memory_item_id,
        )
        self.assertEqual(canonical_update.action.value, "update")

        candidates = await self.manager.find_duplicate_candidates(
            context=self.private,
            content="weekend hiking plans",
            canonical_key="different:key",
        )
        self.assertIn(
            original.item.memory_item_id,
            {candidate.item.memory_item_id for candidate in candidates},
        )
        self.assertIn("fts", {candidate.match_type for candidate in candidates})

    async def test_conflict_records_and_owner_scoped_queries(self):
        left = await self._create("用户住在北京", "create:conflict-left")
        right = await self._create("用户住在上海", "create:conflict-right")
        conflict = await self.store.create_conflict(
            context=self.private,
            left_item_id=left.item.memory_item_id,
            right_item_id=right.item.memory_item_id,
            expected_versions={
                left.item.memory_item_id: left.item.version,
                right.item.memory_item_id: right.item.version,
            },
            conflict_type="contradictory_location",
        )
        self.assertEqual(conflict.status, ConflictStatus.OPEN)
        conflicts = await self.store.list_conflicts(
            owner_user_id=self.private.owner_user_id,
            status=ConflictStatus.OPEN,
        )
        self.assertEqual([item.conflict_id for item in conflicts], [conflict.conflict_id])
        self.assertEqual(
            await self.store.list_conflicts(owner_user_id=self.other.owner_user_id),
            [],
        )

    async def test_idempotency_rejects_payload_mismatch_and_cross_persona_replay(self):
        operation_key = "create:payload-bound"
        created = await self._create("幂等请求原始内容", operation_key)

        with self.assertRaises(EvolvingMemoryIdempotencyError):
            await self._create("同一幂等键下的不同内容", operation_key)

        other_persona = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id=self.private.session_id,
            persona_id="persona-b",
            is_group=False,
        )
        with self.assertRaises(EvolvingMemoryIdempotencyError):
            await self.manager.create(
                context=other_persona,
                content="幂等请求原始内容",
                operation_key=operation_key,
                actor_type=MemoryActorType.USER,
                actor_id="tester",
            )

        import aiosqlite
        import json

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM memory_write_ops WHERE operation_key = ?",
                    (operation_key,),
                )
            ).fetchone()
        recorded = json.loads(row[0])
        self.assertEqual(recorded["owner_user_id"], self.private.owner_user_id)
        self.assertEqual(recorded["request"]["persona_id"], self.private.persona_id)
        self.assertTrue(recorded["request_digest"])
        self.assertEqual(recorded["entity_id"], created.item.memory_item_id)

    async def test_operation_key_source_and_useful_feedback_are_idempotent(self):
        source = {
            "source_key": "message:1-3",
            "source_type": "message_range",
            "session_id": self.private.session_id,
            "message_start_id": 1,
            "message_end_id": 3,
            "content_snapshot": "来源快照",
        }
        first = await self._create(
            "带来源的事实",
            "create:idempotent",
            source=source,
        )
        replay = await self._create(
            "带来源的事实",
            "create:idempotent",
            source=source,
        )
        self.assertEqual(first.item.memory_item_id, replay.item.memory_item_id)
        sources = await self.store.list_sources(
            owner_user_id=self.private.owner_user_id,
            memory_item_id=first.item.memory_item_id,
        )
        self.assertEqual(len(sources), 1)

        feedback = MemoryFeedback(
            memory_item_id=replay.item.memory_item_id,
            expected_version=replay.item.version,
            useful=True,
            score_delta=0.2,
            actor_type=MemoryActorType.AUTOMATIC,
            actor_id="feedback",
            operation_key="feedback:idempotent",
        )
        useful = await self.manager.useful_feedback(context=self.private, feedback=feedback)
        replayed = await self.manager.useful_feedback(context=self.private, feedback=feedback)
        self.assertEqual(useful.version, replayed.version)
        self.assertEqual(useful.useful_count, 2)
        self.assertAlmostEqual(useful.useful_score, 0.25)


if __name__ == "__main__":
    unittest.main()
