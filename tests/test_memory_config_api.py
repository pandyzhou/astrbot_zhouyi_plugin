from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.config.astrbot_config import AstrBotConfig

from data.plugins.astrbot_zhouyi_plugin.memory.core.base.config_validator import (
    MemoryConfig,
)
from data.plugins.astrbot_zhouyi_plugin.memory_config_api import (
    PLUGIN_NAME,
    MemoryConfigApi,
    PluginReloadAdapter,
)
from data.plugins.astrbot_zhouyi_plugin.runtime import PluginRuntime


class _Provider:
    def __init__(self, provider_id: str, model: str, provider_type: str, secret: str):
        self.provider_config = {
            "id": provider_id,
            "model": model,
            "key": [secret],
            "api_base": "https://secret.invalid",
        }
        self._meta = SimpleNamespace(id=provider_id, model=model, type=provider_type)

    def meta(self):
        return self._meta


class _Context:
    def __init__(self) -> None:
        self.llm = [_Provider("llm-ok", "chat-model", "openai", "llm-secret")]
        self.embedding = [
            _Provider("embed-ok", "embedding-model", "openai_embedding", "embed-secret")
        ]

    def get_all_providers(self):
        return self.llm

    def get_all_embedding_providers(self):
        return self.embedding


class _Reloader:
    def __init__(self, *, supported: bool = True, fail: bool = False) -> None:
        self.supported = supported
        self.fail = fail
        self.calls: list[str] = []

    async def reload_plugin(self) -> None:
        self.calls.append(PLUGIN_NAME)
        if self.fail:
            raise RuntimeError("reload failed")


class MemoryConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = PLUGIN_ROOT / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.config_path = Path(self.temp_dir.name) / "plugin_config.json"
        memory = MemoryConfig().model_dump(mode="json")
        memory["provider_settings"] = {
            "embedding_provider_id": "",
            "llm_provider_id": "",
        }
        self.config = AstrBotConfig(
            config_path=str(self.config_path),
            default_config={
                "memory": memory,
                "unrelated_root": {"preserved": True},
            },
        )
        self.context = _Context()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    async def _call(handler, payload: object | None = None):
        raw_body = b""
        headers: list[tuple[bytes, bytes]] = []
        method = "GET"
        if payload is not None:
            method = "POST"
            raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw_body)).encode("ascii")),
            ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/config/memory",
            "raw_path": b"/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/config/memory",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": raw_body, "more_body": False}

        plugin_request = PluginRequest(
            Request(scope, receive),
            plugin_name=PLUGIN_NAME,
            username="tester",
        )
        with bind_request_context(plugin_request):
            response = await handler()
        return response.status_code, json.loads(response.body.decode("utf-8"))

    async def _get(self, api: MemoryConfigApi):
        status, payload = await self._call(api.get_memory_config)
        self.assertEqual(status, 200)
        return payload["data"]

    async def test_get_is_available_when_memory_is_disabled_or_failed(self):
        self.config["memory"]["enabled"] = False
        api = MemoryConfigApi(self.config, self.context, "runtime-disabled")

        data = await self._get(api)

        self.assertFalse(data["config"]["enabled"])
        self.assertEqual(data["runtime_id"], "runtime-disabled")
        self.assertTrue(data["revision"].startswith("sha256:"))
        self.assertIn("items", data["schema"])
        self.assertIn("properties", data["schema"])
        self.assertEqual(
            data["constraints"]["session_manager.max_sessions"],
            {"min": 1, "max": 10000},
        )

    async def test_provider_options_expose_only_whitelisted_fields(self):
        api = MemoryConfigApi(self.config, self.context, "runtime-provider")

        data = await self._get(api)
        serialized = json.dumps(data["providers"], ensure_ascii=False)

        self.assertEqual(
            set(data["providers"]["llm"][0]), {"id", "model", "type"}
        )
        self.assertEqual(
            set(data["providers"]["embedding"][0]),
            {"id", "model", "type"},
        )
        self.assertNotIn("llm-secret", serialized)
        self.assertNotIn("embed-secret", serialized)
        self.assertNotIn("secret.invalid", serialized)

    async def test_valid_save_preserves_root_and_responds_before_reload(self):
        reloader = _Reloader()
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-old",
            reloader=reloader,
            reload_delay=0.02,
        )
        current = await self._get(api)
        updated = copy.deepcopy(current["config"])
        updated["enabled"] = False
        updated["provider_settings"] = {
            "embedding_provider_id": "embed-ok",
            "llm_provider_id": "llm-ok",
        }
        updated["graph_memory"]["document_route_weight"] = 0.6
        updated["graph_memory"]["graph_route_weight"] = 0.4

        status, payload = await self._call(
            api.post_memory_config,
            {"config": updated, "expected_revision": current["revision"]},
        )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["old_runtime_id"], "runtime-old")
        self.assertTrue(payload["data"]["reload_scheduled"])
        self.assertTrue(payload["data"]["reload_pending"])
        self.assertEqual(payload["data"]["reload_status"], "scheduled")
        self.assertFalse(payload["data"]["reload_failed"])
        self.assertFalse(payload["data"]["manual_reload_required"])
        self.assertEqual(payload["data"]["config"], updated)
        self.assertEqual(reloader.calls, [])
        persisted = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(persisted["memory"], updated)
        self.assertEqual(persisted["unrelated_root"], {"preserved": True})
        self.assertEqual(
            payload["data"]["revision"], api._revision(persisted["memory"])
        )

        await asyncio.sleep(0.04)
        self.assertEqual(reloader.calls, [PLUGIN_NAME])
        reloaded = await self._get(api)
        self.assertEqual(reloaded["reload_status"], "idle")
        self.assertFalse(reloaded["reload_failed"])

    async def test_same_runtime_only_schedules_one_reload(self):
        reloader = _Reloader()
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-old",
            reloader=reloader,
            reload_delay=0.02,
        )
        first = await self._get(api)
        first_config = copy.deepcopy(first["config"])
        first_config["bot_language"] = "en"
        first_status, first_payload = await self._call(
            api.post_memory_config,
            {"config": first_config, "expected_revision": first["revision"]},
        )
        second_config = copy.deepcopy(first_config)
        second_config["bot_language"] = "ru"
        second_status, second_payload = await self._call(
            api.post_memory_config,
            {
                "config": second_config,
                "expected_revision": first_payload["data"]["revision"],
            },
        )

        self.assertEqual([first_status, second_status], [202, 202])
        self.assertTrue(first_payload["data"]["reload_scheduled"])
        self.assertFalse(second_payload["data"]["reload_scheduled"])
        self.assertTrue(second_payload["data"]["reload_pending"])
        await asyncio.sleep(0.04)
        self.assertEqual(reloader.calls, [PLUGIN_NAME])

    async def test_stale_runtime_post_is_rejected_after_new_runtime_activation(self):
        self.config.save_config()
        old_api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-old",
            reloader=_Reloader(supported=False),
        )
        current = await self._get(old_api)
        submitted = copy.deepcopy(current["config"])
        submitted["enabled"] = False
        disk_before = self.config_path.read_bytes()
        request_ready = asyncio.Event()
        original_provider_options = old_api._provider_options

        def tracked_provider_options():
            options = original_provider_options()
            request_ready.set()
            return options

        old_api._provider_options = tracked_provider_options
        async with old_api._coordinator.save_lock:
            old_post = asyncio.create_task(
                self._call(
                    old_api.post_memory_config,
                    {
                        "config": submitted,
                        "expected_revision": current["revision"],
                    },
                )
            )
            await asyncio.wait_for(request_ready.wait(), timeout=1.0)
            new_config = AstrBotConfig(
                config_path=str(self.config_path),
                default_config={
                    "memory": MemoryConfig().model_dump(mode="json"),
                    "unrelated_root": {"preserved": True},
                },
            )
            new_memory_before = copy.deepcopy(new_config["memory"])
            new_api = MemoryConfigApi(
                new_config,
                self.context,
                "runtime-new",
                reloader=_Reloader(supported=False),
            )

        status, payload = await old_post

        self.assertEqual(status, 409)
        self.assertEqual(payload["data"]["code"], "STALE_MEMORY_CONFIG_RUNTIME")
        self.assertEqual(payload["data"]["active_runtime_id"], "runtime-new")
        self.assertEqual(self.config_path.read_bytes(), disk_before)
        self.assertEqual(new_config["memory"], new_memory_before)
        active = await self._get(new_api)
        stale_api_view = await self._get(old_api)
        self.assertEqual(active["config"], new_memory_before)
        self.assertEqual(stale_api_view["config"], new_memory_before)
        self.assertEqual(active["runtime_id"], "runtime-new")
        self.assertEqual(stale_api_view["runtime_id"], "runtime-new")

    async def test_route_weights_are_normalized_for_disk_response_revision_and_get(self):
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-normalized",
            reloader=_Reloader(supported=False),
        )
        current = await self._get(api)
        cases = ((0.0, 0.0, 0.65, 0.35), (0.6, 0.3, 2 / 3, 1 / 3))

        for document_weight, graph_weight, expected_document, expected_graph in cases:
            submitted = copy.deepcopy(current["config"])
            submitted["graph_memory"]["document_route_weight"] = document_weight
            submitted["graph_memory"]["graph_route_weight"] = graph_weight
            status, payload = await self._call(
                api.post_memory_config,
                {
                    "config": submitted,
                    "expected_revision": current["revision"],
                },
            )

            self.assertEqual(status, 202)
            normalized = payload["data"]["config"]
            self.assertAlmostEqual(
                normalized["graph_memory"]["document_route_weight"],
                expected_document,
            )
            self.assertAlmostEqual(
                normalized["graph_memory"]["graph_route_weight"],
                expected_graph,
            )
            persisted = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(persisted["memory"], normalized)
            self.assertEqual(
                payload["data"]["revision"], api._revision(normalized)
            )
            fetched = await self._get(api)
            self.assertEqual(fetched["config"], normalized)
            self.assertEqual(fetched["revision"], payload["data"]["revision"])
            current = fetched

    async def test_reload_failure_requires_manual_reload_until_new_runtime(self):
        failing_reloader = _Reloader(fail=True)
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-failing",
            reloader=failing_reloader,
            reload_delay=0.01,
        )
        current = await self._get(api)
        first_config = copy.deepcopy(current["config"])
        first_config["bot_language"] = "en"
        first_status, first_payload = await self._call(
            api.post_memory_config,
            {"config": first_config, "expected_revision": current["revision"]},
        )
        self.assertEqual(first_status, 202)
        self.assertTrue(first_payload["data"]["reload_pending"])

        await asyncio.sleep(0.03)
        failed = await self._get(api)
        self.assertEqual(failed["reload_status"], "failed")
        self.assertTrue(failed["reload_failed"])
        self.assertEqual(failing_reloader.calls, [PLUGIN_NAME])

        second_config = copy.deepcopy(failed["config"])
        second_config["bot_language"] = "ru"
        second_status, second_payload = await self._call(
            api.post_memory_config,
            {"config": second_config, "expected_revision": failed["revision"]},
        )
        self.assertEqual(second_status, 202)
        self.assertFalse(second_payload["data"]["reload_scheduled"])
        self.assertFalse(second_payload["data"]["reload_pending"])
        self.assertEqual(second_payload["data"]["reload_status"], "failed")
        self.assertTrue(second_payload["data"]["reload_failed"])
        self.assertTrue(second_payload["data"]["manual_reload_required"])
        self.assertEqual(failing_reloader.calls, [PLUGIN_NAME])

        new_reloader = _Reloader()
        new_api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-recovered",
            reloader=new_reloader,
            reload_delay=0.01,
        )
        activated = await self._get(new_api)
        self.assertEqual(activated["reload_status"], "idle")
        self.assertFalse(activated["reload_failed"])

    async def test_reload_task_is_not_cancelled_by_plugin_runtime_terminate(self):
        reloader = _Reloader()
        runtime = PluginRuntime(SimpleNamespace(), self.context, self.config)
        api = MemoryConfigApi(
            self.config,
            self.context,
            runtime.runtime_id,
            reloader=reloader,
            reload_delay=0.01,
        )
        current = await self._get(api)
        updated = copy.deepcopy(current["config"])
        updated["enabled"] = False
        status, _payload = await self._call(
            api.post_memory_config,
            {"config": updated, "expected_revision": current["revision"]},
        )
        self.assertEqual(status, 202)

        await runtime.terminate()
        await asyncio.sleep(0.03)

        self.assertEqual(reloader.calls, [PLUGIN_NAME])
        self.assertEqual(api._coordinator.reload_state, "idle")

    async def test_save_without_reload_capability_requires_manual_reload(self):
        reloader = _Reloader(supported=False)
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-manual",
            reloader=reloader,
        )
        current = await self._get(api)
        updated = copy.deepcopy(current["config"])
        updated["enabled"] = False

        status, payload = await self._call(
            api.post_memory_config,
            {"config": updated, "expected_revision": current["revision"]},
        )

        self.assertEqual(status, 202)
        self.assertFalse(payload["data"]["reload_scheduled"])
        self.assertFalse(payload["data"]["reload_pending"])
        self.assertTrue(payload["data"]["manual_reload_required"])
        self.assertIn("手动重载", payload["data"]["message"])
        self.assertEqual(reloader.calls, [])

    async def test_revision_conflict_returns_409_without_saving(self):
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-conflict",
            reloader=_Reloader(),
        )
        current = await self._get(api)
        updated = copy.deepcopy(current["config"])
        updated["enabled"] = False

        status, payload = await self._call(
            api.post_memory_config,
            {"config": updated, "expected_revision": "sha256:stale"},
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["data"]["code"], "REVISION_CONFLICT")
        self.assertEqual(self.config["memory"]["enabled"], True)

    async def test_unavailable_current_provider_does_not_block_unrelated_save(self):
        self.config["memory"]["provider_settings"] = {
            "embedding_provider_id": "missing-embedding",
            "llm_provider_id": "missing-llm",
        }
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-unavailable-provider",
            reloader=_Reloader(supported=False),
        )
        current = await self._get(api)
        updated = copy.deepcopy(current["config"])
        updated["recall_engine"]["top_k"] = 6

        status, payload = await self._call(
            api.post_memory_config,
            {"config": updated, "expected_revision": current["revision"]},
        )

        self.assertEqual(status, 202)
        self.assertTrue(payload["data"]["manual_reload_required"])
        self.assertEqual(
            self.config["memory"]["provider_settings"],
            {
                "embedding_provider_id": "missing-embedding",
                "llm_provider_id": "missing-llm",
            },
        )
        self.assertEqual(self.config["memory"]["recall_engine"]["top_k"], 6)

    async def test_rejects_unknown_request_and_config_fields(self):
        api = MemoryConfigApi(self.config, self.context, "runtime-invalid")
        current = await self._get(api)

        status, payload = await self._call(
            api.post_memory_config,
            {
                "config": current["config"],
                "expected_revision": current["revision"],
                "extra": True,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "UNSUPPORTED_FIELDS")

        unknown_config = copy.deepcopy(current["config"])
        unknown_config["recall_engine"]["unknown"] = 1
        status, payload = await self._call(
            api.post_memory_config,
            {"config": unknown_config, "expected_revision": current["revision"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "UNKNOWN_CONFIG_FIELDS")
        self.assertEqual(payload["data"]["fields"], ["recall_engine.unknown"])

        incomplete = copy.deepcopy(current["config"])
        del incomplete["backup_settings"]
        status, payload = await self._call(
            api.post_memory_config,
            {"config": incomplete, "expected_revision": current["revision"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "MISSING_CONFIG_FIELDS")
        self.assertEqual(payload["data"]["fields"], ["backup_settings"])

    async def test_rejects_invalid_types_enums_ranges_and_provider_options(self):
        api = MemoryConfigApi(self.config, self.context, "runtime-invalid-values")
        current = await self._get(api)
        cases = (
            ("type", ("enabled",), "true", "INVALID_CONFIG_TYPE"),
            ("language", ("bot_language",), "ja", "INVALID_CONFIG_OPTION"),
            (
                "injection",
                ("recall_engine", "injection_method"),
                "unknown",
                "INVALID_CONFIG_OPTION",
            ),
            (
                "integer_range",
                ("session_manager", "max_sessions"),
                0,
                "INVALID_MEMORY_CONFIG",
            ),
            (
                "float_range",
                ("recall_engine", "importance_weight"),
                10.1,
                "INVALID_MEMORY_CONFIG",
            ),
            (
                "provider",
                ("provider_settings", "llm_provider_id"),
                "not-configured",
                "INVALID_PROVIDER_OPTION",
            ),
        )

        for name, path, value, expected_code in cases:
            with self.subTest(name=name):
                invalid = copy.deepcopy(current["config"])
                target = invalid
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                status, payload = await self._call(
                    api.post_memory_config,
                    {"config": invalid, "expected_revision": current["revision"]},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["data"]["code"], expected_code)

    async def test_deprecated_injection_options_remain_valid(self):
        api = MemoryConfigApi(
            self.config,
            self.context,
            "runtime-deprecated",
            reloader=_Reloader(supported=False),
        )
        current = await self._get(api)
        for option in ("fake_tool_call_deepseek_v4", "system_prompt"):
            updated = copy.deepcopy(current["config"])
            updated["recall_engine"]["injection_method"] = option
            status, payload = await self._call(
                api.post_memory_config,
                {"config": updated, "expected_revision": current["revision"]},
            )
            self.assertEqual(status, 202)
            current = {"config": updated, "revision": payload["data"]["revision"]}

    async def test_conf_schema_leaf_fields_match_memory_config_model(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

        def schema_paths(node, prefix=()):
            items = node.get("items") if isinstance(node, dict) else None
            if not isinstance(items, dict):
                return {".".join(prefix)} if prefix else set()
            paths = set()
            for name, child in items.items():
                paths.update(schema_paths(child, (*prefix, name)))
            return paths

        model_schema = MemoryConfig.model_json_schema()
        definitions = model_schema.get("$defs", {})

        def model_paths(node, prefix=()):
            if "$ref" in node:
                node = definitions[node["$ref"].removeprefix("#/$defs/")]
            properties = node.get("properties")
            if not isinstance(properties, dict):
                return {".".join(prefix)} if prefix else set()
            paths = set()
            for name, child in properties.items():
                paths.update(model_paths(child, (*prefix, name)))
            return paths

        self.assertEqual(
            schema_paths(schema["memory"]),
            model_paths(model_schema),
        )

    async def test_reload_adapter_uses_fixed_plugin_name(self):
        calls = []

        class Manager:
            async def reload(self, plugin_name):
                calls.append(plugin_name)
                return True, None

        adapter = PluginReloadAdapter(SimpleNamespace(_star_manager=Manager()))
        self.assertTrue(adapter.supported)
        await adapter.reload_plugin()
        self.assertEqual(calls, [PLUGIN_NAME])


if __name__ == "__main__":
    unittest.main()
