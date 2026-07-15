from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .memory.core.base.config_validator import MemoryConfig

PLUGIN_NAME = "astrbot_zhouyi_plugin"
RELOAD_DELAY_SECONDS = 0.75
_COORDINATOR_CONTEXT_ATTR = "_zhouyi_memory_config_coordinator"
_SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"

_RELOAD_IDLE = "idle"
_RELOAD_SCHEDULED = "scheduled"
_RELOAD_RUNNING = "running"
_RELOAD_FAILED = "failed"


class _MemoryConfigCoordinator:
    """保存在 AstrBot context 上、跨插件 runtime 共享的配置协调状态。"""

    def __init__(self) -> None:
        self.save_lock = asyncio.Lock()
        self.generation = 0
        self.active_generation = 0
        self.active_runtime_id: str | None = None
        self.active_config: Any = None
        self.reload_state = _RELOAD_IDLE
        self.reload_error: str | None = None
        self.reload_task: asyncio.Task[Any] | None = None


def _coordinator_for(context: Any) -> Any:
    coordinator = getattr(context, _COORDINATOR_CONTEXT_ATTR, None)
    if coordinator is None or not hasattr(coordinator, "save_lock"):
        coordinator = _MemoryConfigCoordinator()
        setattr(context, _COORDINATOR_CONTEXT_ATTR, coordinator)
    return coordinator


class ConfigValidationProblem(ValueError):
    def __init__(self, message: str, *, code: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


class PluginReloadAdapter:
    """隔离 AstrBot 插件管理器的私有重载接口。"""

    def __init__(self, context: Any) -> None:
        manager = getattr(context, "_star_manager", None)
        reload_method = getattr(manager, "reload", None)
        self._reload_method = reload_method if callable(reload_method) else None

    @property
    def supported(self) -> bool:
        return self._reload_method is not None

    async def reload_plugin(self) -> None:
        if self._reload_method is None:
            raise RuntimeError("插件重载接口不可用")
        result = self._reload_method(PLUGIN_NAME)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, tuple) and result and result[0] is False:
            detail = result[1] if len(result) > 1 else None
            raise RuntimeError(str(detail or "插件重载失败"))


class MemoryConfigApi:
    """长期记忆配置读取、校验、持久化与延迟重载。"""

    def __init__(
        self,
        config: Any,
        context: Any,
        runtime_id: str,
        *,
        reloader: PluginReloadAdapter | None = None,
        reload_delay: float = RELOAD_DELAY_SECONDS,
    ) -> None:
        self.context = context
        self.runtime_id = runtime_id
        self.reloader = reloader or PluginReloadAdapter(context)
        self.reload_delay = reload_delay
        self._coordinator = _coordinator_for(context)
        self._coordinator.generation += 1
        self.runtime_generation = self._coordinator.generation
        self._coordinator.active_generation = self.runtime_generation
        self._coordinator.active_runtime_id = runtime_id
        self._coordinator.active_config = config
        self._coordinator.reload_state = _RELOAD_IDLE
        self._coordinator.reload_error = None
        self._coordinator.reload_task = None
        self._memory_schema = self._load_memory_schema()
        self._numeric_constraints = self._extract_numeric_constraints()

    @staticmethod
    def _load_memory_schema() -> dict[str, Any]:
        with _SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        memory_schema = schema.get("memory") if isinstance(schema, dict) else None
        if not isinstance(memory_schema, dict):
            raise ValueError("_conf_schema.json 缺少 memory schema")
        return memory_schema

    def _schema_for_response(self) -> dict[str, Any]:
        schema = copy.deepcopy(self._memory_schema)

        def expose_properties(node: dict[str, Any]) -> None:
            items = node.get("items")
            if not isinstance(items, dict):
                return
            node["properties"] = copy.deepcopy(items)
            for child in node["properties"].values():
                if isinstance(child, dict):
                    expose_properties(child)

        expose_properties(schema)
        return schema

    @staticmethod
    def _revision(config: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def _active_config(self) -> Any:
        config = self._coordinator.active_config
        if config is None:
            raise RuntimeError("插件配置不可用")
        return config

    def _current_memory_config(self) -> dict[str, Any]:
        config = self._active_config()
        try:
            value = config.get("memory")
        except (AttributeError, TypeError) as exc:
            raise RuntimeError("插件配置不可用") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("memory 配置节点不可用")
        return copy.deepcopy(dict(value))

    def _is_active_runtime(self) -> bool:
        return (
            self._coordinator.active_generation == self.runtime_generation
            and self._coordinator.active_runtime_id == self.runtime_id
        )

    @staticmethod
    def _provider_option(provider: Any) -> dict[str, str | None] | None:
        try:
            meta = provider.meta()
            provider_id = getattr(meta, "id", None)
            if not isinstance(provider_id, str) or not provider_id:
                return None
            model = getattr(meta, "model", None)
            provider_type = getattr(meta, "type", None)
            return {
                "id": provider_id,
                "model": model if isinstance(model, str) else None,
                "type": provider_type if isinstance(provider_type, str) else None,
            }
        except Exception:
            logger.warning("读取 Provider 白名单信息失败", exc_info=True)
            return None

    def _provider_options(self) -> dict[str, list[dict[str, str | None]]]:
        options: dict[str, list[dict[str, str | None]]] = {
            "llm": [],
            "embedding": [],
        }
        for key, method_name in (
            ("llm", "get_all_providers"),
            ("embedding", "get_all_embedding_providers"),
        ):
            method = getattr(self.context, method_name, None)
            if not callable(method):
                continue
            try:
                providers = method()
            except Exception:
                logger.warning("读取 %s Provider 列表失败", key, exc_info=True)
                continue
            seen: set[str] = set()
            for provider in providers if isinstance(providers, list) else []:
                option = self._provider_option(provider)
                if option is None or option["id"] in seen:
                    continue
                seen.add(option["id"])
                options[key].append(option)
            options[key].sort(key=lambda item: item["id"] or "")
        return options

    @classmethod
    def _extract_numeric_constraints(cls) -> dict[str, dict[str, int | float]]:
        json_schema = MemoryConfig.model_json_schema()
        definitions = json_schema.get("$defs", {})
        constraints: dict[str, dict[str, int | float]] = {}

        def resolve(node: dict[str, Any]) -> dict[str, Any]:
            reference = node.get("$ref")
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                return node
            resolved = definitions.get(reference.removeprefix("#/$defs/"))
            return resolved if isinstance(resolved, dict) else node

        def walk(node: dict[str, Any], path: tuple[str, ...]) -> None:
            node = resolve(node)
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    if isinstance(child, dict):
                        walk(child, (*path, name))
                return
            bound_names = {
                "minimum": "min",
                "maximum": "max",
                "exclusiveMinimum": "exclusive_min",
                "exclusiveMaximum": "exclusive_max",
            }
            bounds = {
                output_name: value
                for schema_name, output_name in bound_names.items()
                if isinstance((value := node.get(schema_name)), (int, float))
                and not isinstance(value, bool)
            }
            if bounds and path:
                constraints[".".join(path)] = bounds

        walk(json_schema, ())
        return constraints

    @classmethod
    def _validate_schema_value(
        cls,
        value: Any,
        schema: dict[str, Any],
        path: tuple[str, ...],
    ) -> None:
        field_path = ".".join(path) or "config"
        schema_type = schema.get("type")
        if schema_type == "object":
            if not isinstance(value, dict):
                raise ConfigValidationProblem(
                    f"{field_path} 必须是对象",
                    code="INVALID_CONFIG_TYPE",
                    data={"field": field_path},
                )
            items = schema.get("items")
            if not isinstance(items, dict):
                raise ConfigValidationProblem(
                    f"{field_path} schema 无效", code="INVALID_CONFIG_SCHEMA"
                )
            unknown = sorted(set(value) - set(items))
            if unknown:
                raise ConfigValidationProblem(
                    "配置包含 schema 未知字段",
                    code="UNKNOWN_CONFIG_FIELDS",
                    data={
                        "fields": [".".join((*path, name)) for name in unknown],
                    },
                )
            missing = sorted(set(items) - set(value))
            if missing:
                raise ConfigValidationProblem(
                    "必须提交完整 memory config",
                    code="MISSING_CONFIG_FIELDS",
                    data={
                        "fields": [".".join((*path, name)) for name in missing],
                    },
                )
            for name, child_schema in items.items():
                if isinstance(child_schema, dict):
                    cls._validate_schema_value(value[name], child_schema, (*path, name))
            return

        valid = False
        if schema_type == "bool":
            valid = isinstance(value, bool)
        elif schema_type == "int":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif schema_type == "float":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        elif schema_type == "string":
            valid = isinstance(value, str)
        if not valid:
            raise ConfigValidationProblem(
                f"{field_path} 类型无效",
                code="INVALID_CONFIG_TYPE",
                data={"field": field_path, "expected": schema_type},
            )
        options = schema.get("options")
        if isinstance(options, list) and value not in options:
            raise ConfigValidationProblem(
                f"{field_path} 不在允许选项中",
                code="INVALID_CONFIG_OPTION",
                data={"field": field_path, "options": copy.deepcopy(options)},
            )

    def _validate_provider_ids(
        self,
        config: dict[str, Any],
        provider_options: dict[str, list[dict[str, str | None]]],
        current_config: dict[str, Any],
    ) -> None:
        settings = config["provider_settings"]
        current_settings = current_config.get("provider_settings", {})
        for field, option_key in (
            ("llm_provider_id", "llm"),
            ("embedding_provider_id", "embedding"),
        ):
            provider_id = settings[field]
            if provider_id == "" or provider_id == current_settings.get(field):
                continue
            allowed_ids = {item["id"] for item in provider_options[option_key]}
            if provider_id not in allowed_ids:
                raise ConfigValidationProblem(
                    f"provider_settings.{field} 不在可用 Provider 中",
                    code="INVALID_PROVIDER_OPTION",
                    data={"field": f"provider_settings.{field}"},
                )

    @staticmethod
    def _pydantic_error_data(exc: ValidationError) -> dict[str, Any]:
        errors = []
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            errors.append(
                {
                    "field": ".".join(str(part) for part in error.get("loc", ())),
                    "message": error.get("msg", "配置无效"),
                    "type": error.get("type", "value_error"),
                }
            )
        return {"errors": errors}

    async def get_memory_config(self):
        try:
            current = self._current_memory_config()
            payload = {
                "schema": self._schema_for_response(),
                "config": current,
                "revision": self._revision(current),
                "runtime_id": self._coordinator.active_runtime_id,
                "runtime_generation": self._coordinator.active_generation,
                "reload_status": self._coordinator.reload_state,
                "reload_failed": self._coordinator.reload_state == _RELOAD_FAILED,
                "providers": self._provider_options(),
                "constraints": copy.deepcopy(self._numeric_constraints),
            }
            return json_response({"status": "ok", "data": payload})
        except Exception:
            logger.error("读取长期记忆配置失败", exc_info=True)
            return error_response(
                "长期记忆配置当前不可用",
                status_code=503,
                data={"code": "MEMORY_CONFIG_UNAVAILABLE"},
            )

    async def post_memory_config(self):
        try:
            payload = await request.json(default=None)
            if not isinstance(payload, dict):
                raise ConfigValidationProblem(
                    "请求体必须是 JSON 对象", code="INVALID_JSON"
                )
            unknown = sorted(set(payload) - {"config", "expected_revision"})
            if unknown:
                raise ConfigValidationProblem(
                    "请求包含不允许的字段",
                    code="UNSUPPORTED_FIELDS",
                    data={"fields": unknown},
                )
            missing = sorted({"config", "expected_revision"} - set(payload))
            if missing:
                raise ConfigValidationProblem(
                    "请求缺少必填字段",
                    code="MISSING_FIELDS",
                    data={"fields": missing},
                )
            expected_revision = payload["expected_revision"]
            if not isinstance(expected_revision, str) or not expected_revision:
                raise ConfigValidationProblem(
                    "expected_revision 必须是非空字符串",
                    code="INVALID_EXPECTED_REVISION",
                )
            submitted = payload["config"]
            self._validate_schema_value(submitted, self._memory_schema, ())
            provider_options = self._provider_options()
            try:
                validated = MemoryConfig.model_validate(submitted, strict=True)
            except ValidationError as exc:
                raise ConfigValidationProblem(
                    "memory config 验证失败",
                    code="INVALID_MEMORY_CONFIG",
                    data=self._pydantic_error_data(exc),
                ) from exc
            saved_config = validated.model_dump(mode="json")

            async with self._coordinator.save_lock:
                if not self._is_active_runtime():
                    return error_response(
                        "memory config 请求来自已失效的插件 runtime",
                        status_code=409,
                        data={
                            "code": "STALE_MEMORY_CONFIG_RUNTIME",
                            "active_runtime_id": self._coordinator.active_runtime_id,
                            "active_runtime_generation": self._coordinator.active_generation,
                        },
                    )
                active_config = self._active_config()
                current = self._current_memory_config()
                current_revision = self._revision(current)
                if expected_revision != current_revision:
                    return error_response(
                        "memory config 已被其他请求修改",
                        status_code=409,
                        data={
                            "code": "REVISION_CONFLICT",
                            "revision": current_revision,
                        },
                    )
                self._validate_provider_ids(saved_config, provider_options, current)
                old_value = active_config.get("memory")
                active_config["memory"] = copy.deepcopy(saved_config)
                try:
                    active_config.save_config()
                except Exception:
                    active_config["memory"] = old_value
                    raise
                target_revision = self._revision(saved_config)

            reload_data = self._schedule_reload()
            response_data = {
                "config": copy.deepcopy(saved_config),
                "revision": target_revision,
                "old_runtime_id": self.runtime_id,
                **reload_data,
            }
            if response_data["manual_reload_required"]:
                if response_data["reload_failed"]:
                    response_data["message"] = "配置已保存，自动重载此前失败，请手动重载插件"
                else:
                    response_data["message"] = "配置已保存，当前环境不支持自动重载，请手动重载插件"
            return json_response(
                {"status": "ok", "data": response_data}, status_code=202
            )
        except ConfigValidationProblem as exc:
            return error_response(
                str(exc),
                status_code=400,
                data={"code": exc.code, **(exc.data or {})},
            )
        except Exception:
            logger.error("保存长期记忆配置失败", exc_info=True)
            return error_response(
                "长期记忆配置保存失败",
                status_code=500,
                data={"code": "MEMORY_CONFIG_SAVE_FAILED"},
            )

    def _schedule_reload(self) -> dict[str, Any]:
        state = self._coordinator.reload_state
        if not self._is_active_runtime():
            return {
                "reload_scheduled": False,
                "reload_pending": False,
                "reload_status": self._coordinator.reload_state,
                "reload_failed": self._coordinator.reload_state == _RELOAD_FAILED,
                "manual_reload_required": False,
            }
        if state == _RELOAD_FAILED:
            return {
                "reload_scheduled": False,
                "reload_pending": False,
                "reload_status": _RELOAD_FAILED,
                "reload_failed": True,
                "manual_reload_required": True,
            }
        if not self.reloader.supported:
            return {
                "reload_scheduled": False,
                "reload_pending": False,
                "reload_status": state,
                "reload_failed": False,
                "manual_reload_required": True,
            }
        if state in {_RELOAD_SCHEDULED, _RELOAD_RUNNING}:
            return {
                "reload_scheduled": False,
                "reload_pending": True,
                "reload_status": state,
                "reload_failed": False,
                "manual_reload_required": False,
            }

        self._coordinator.reload_state = _RELOAD_SCHEDULED
        task = asyncio.create_task(self._delayed_reload(self.runtime_generation))
        self._coordinator.reload_task = task
        return {
            "reload_scheduled": True,
            "reload_pending": True,
            "reload_status": _RELOAD_SCHEDULED,
            "reload_failed": False,
            "manual_reload_required": False,
        }

    async def _delayed_reload(self, generation: int) -> None:
        await asyncio.sleep(self.reload_delay)
        if (
            self._coordinator.active_generation != generation
            or self._coordinator.reload_state != _RELOAD_SCHEDULED
        ):
            return
        self._coordinator.reload_state = _RELOAD_RUNNING
        try:
            await self.reloader.reload_plugin()
        except Exception as exc:
            if self._coordinator.active_generation == generation:
                self._coordinator.reload_state = _RELOAD_FAILED
                self._coordinator.reload_error = str(exc)
            logger.error("长期记忆配置保存后的插件重载失败", exc_info=True)
        else:
            if self._coordinator.active_generation == generation:
                self._coordinator.reload_state = _RELOAD_IDLE
                self._coordinator.reload_error = None
        finally:
            current_task = asyncio.current_task()
            if (
                self._coordinator.active_generation == generation
                and self._coordinator.reload_task is current_task
            ):
                self._coordinator.reload_task = None
