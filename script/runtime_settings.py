from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA_VERSION = 2
PREVIEW_TTL_SECONDS = 300

SETTING_DEFAULTS: dict[str, Any] = {
    "max_history_points": 10000,
    "trend_sampling_enabled": True,
    "auto_cleanup_enabled": True,
    "auto_cleanup_days": 10,
    "auto_refresh_on_page_open": True,
    "default_trend_hours": 24,
    "mc_lookup_timeout_seconds": 3.0,
    "mc_status_timeout_seconds": 7.0,
    "max_concurrent_queries": 5,
}
GROUP_OVERRIDE_KEYS = frozenset(SETTING_DEFAULTS) - {"max_concurrent_queries"}
GLOBAL_SETTING_KEYS = frozenset(SETTING_DEFAULTS)

_RANGES: dict[str, tuple[float, float]] = {
    "max_history_points": (168, 100000),
    "auto_cleanup_days": (1, 365),
    "default_trend_hours": (1, 168),
    "mc_lookup_timeout_seconds": (0.5, 30.0),
    "mc_status_timeout_seconds": (1.0, 60.0),
    "max_concurrent_queries": (1, 20),
}
_BOOL_KEYS = {
    "trend_sampling_enabled",
    "auto_cleanup_enabled",
    "auto_refresh_on_page_open",
}
_INT_KEYS = {
    "max_history_points",
    "auto_cleanup_days",
    "default_trend_hours",
    "max_concurrent_queries",
}
_FLOAT_KEYS = {"mc_lookup_timeout_seconds", "mc_status_timeout_seconds"}


class SettingsError(Exception):
    code = "settings_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code or self.code


class SettingsValidationError(SettingsError):
    code = "settings_validation_error"


class SettingsRevisionConflict(SettingsError):
    code = "settings_revision_conflict"


class SettingsPreviewExpired(SettingsError):
    code = "settings_preview_expired"


class SettingsPreviewRequired(SettingsError):
    code = "settings_preview_required"


class HistoryPruneConfirmationRequired(SettingsError):
    code = "history_prune_confirmation_required"


@dataclass(frozen=True)
class RuntimeSettings:
    max_history_points: int = 10000
    trend_sampling_enabled: bool = True
    auto_cleanup_enabled: bool = True
    auto_cleanup_days: int = 10
    auto_refresh_on_page_open: bool = True
    default_trend_hours: int = 24
    mc_lookup_timeout_seconds: float = 3.0
    mc_status_timeout_seconds: float = 7.0
    max_concurrent_queries: int = 5
    revision: int = 1
    updated_at: int = 0


@dataclass(frozen=True)
class GroupRuntimeSettings:
    group_id: str
    max_history_points: Optional[int] = None
    trend_sampling_enabled: Optional[bool] = None
    auto_cleanup_enabled: Optional[bool] = None
    auto_cleanup_days: Optional[int] = None
    auto_refresh_on_page_open: Optional[bool] = None
    default_trend_hours: Optional[int] = None
    mc_lookup_timeout_seconds: Optional[float] = None
    mc_status_timeout_seconds: Optional[float] = None
    revision: int = 0
    updated_at: int = 0


@dataclass(frozen=True)
class EffectiveRuntimeSettings(RuntimeSettings):
    group_id: str = ""
    global_revision: int = 1
    group_revision: int = 0


@dataclass(frozen=True)
class SettingsPreview:
    preview_id: str
    scope: str
    group_id: Optional[str]
    base_revision: int
    global_revision: int
    expires_at: int
    current: RuntimeSettings | GroupRuntimeSettings
    proposed: RuntimeSettings | GroupRuntimeSettings
    affected_groups: tuple[str, ...]
    affected_servers: int
    history_points_to_prune: int
    cleanup_candidates_before: int
    cleanup_candidates_after: int

    @property
    def cleanup_candidate_change(self) -> int:
        return self.cleanup_candidates_after - self.cleanup_candidates_before


@dataclass(frozen=True)
class SettingsApplyResult:
    settings: RuntimeSettings | GroupRuntimeSettings
    pruned_history_points: int


def _bool_db(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _runtime_from_row(row: sqlite3.Row) -> RuntimeSettings:
    return RuntimeSettings(
        max_history_points=int(row["max_history_points"]),
        trend_sampling_enabled=bool(row["trend_sampling_enabled"]),
        auto_cleanup_enabled=bool(row["auto_cleanup_enabled"]),
        auto_cleanup_days=int(row["auto_cleanup_days"]),
        auto_refresh_on_page_open=bool(row["auto_refresh_on_page_open"]),
        default_trend_hours=int(row["default_trend_hours"]),
        mc_lookup_timeout_seconds=float(row["mc_lookup_timeout_seconds"]),
        mc_status_timeout_seconds=float(row["mc_status_timeout_seconds"]),
        max_concurrent_queries=int(row["max_concurrent_queries"]),
        revision=int(row["revision"]),
        updated_at=int(row["updated_at"]),
    )


def _group_from_row(group_id: str, row: Optional[sqlite3.Row]) -> GroupRuntimeSettings:
    if row is None:
        return GroupRuntimeSettings(group_id=group_id)
    return GroupRuntimeSettings(
        group_id=group_id,
        max_history_points=row["max_history_points"],
        trend_sampling_enabled=_bool_db(row["trend_sampling_enabled"]),
        auto_cleanup_enabled=_bool_db(row["auto_cleanup_enabled"]),
        auto_cleanup_days=row["auto_cleanup_days"],
        auto_refresh_on_page_open=_bool_db(row["auto_refresh_on_page_open"]),
        default_trend_hours=row["default_trend_hours"],
        mc_lookup_timeout_seconds=row["mc_lookup_timeout_seconds"],
        mc_status_timeout_seconds=row["mc_status_timeout_seconds"],
        revision=int(row["revision"]),
        updated_at=int(row["updated_at"]),
    )


def migrate_schema_v1_to_v2(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    statements = (
        """CREATE TABLE IF NOT EXISTS runtime_global_settings (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            max_history_points INTEGER NOT NULL,
            trend_sampling_enabled INTEGER NOT NULL,
            auto_cleanup_enabled INTEGER NOT NULL,
            auto_cleanup_days INTEGER NOT NULL,
            auto_refresh_on_page_open INTEGER NOT NULL,
            default_trend_hours INTEGER NOT NULL,
            mc_lookup_timeout_seconds REAL NOT NULL,
            mc_status_timeout_seconds REAL NOT NULL,
            max_concurrent_queries INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS runtime_group_settings (
            group_id TEXT PRIMARY KEY,
            max_history_points INTEGER,
            trend_sampling_enabled INTEGER,
            auto_cleanup_enabled INTEGER,
            auto_cleanup_days INTEGER,
            auto_refresh_on_page_open INTEGER,
            default_trend_hours INTEGER,
            mc_lookup_timeout_seconds REAL,
            mc_status_timeout_seconds REAL,
            revision INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS runtime_settings_previews (
            preview_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            group_id TEXT,
            base_revision INTEGER NOT NULL,
            global_revision INTEGER NOT NULL DEFAULT 1,
            patch_json TEXT NOT NULL,
            reset_keys_json TEXT NOT NULL,
            history_points_to_prune INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL
        )""",
    )
    for statement in statements:
        conn.execute(statement)
    preview_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(runtime_settings_previews)")
    }
    if "history_points_to_prune" not in preview_columns:
        conn.execute(
            "ALTER TABLE runtime_settings_previews "
            "ADD COLUMN history_points_to_prune INTEGER NOT NULL DEFAULT 0"
        )
    if "global_revision" not in preview_columns:
        conn.execute(
            "ALTER TABLE runtime_settings_previews "
            "ADD COLUMN global_revision INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        "INSERT OR IGNORE INTO runtime_global_settings VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            SETTING_DEFAULTS["max_history_points"],
            int(SETTING_DEFAULTS["trend_sampling_enabled"]),
            int(SETTING_DEFAULTS["auto_cleanup_enabled"]),
            SETTING_DEFAULTS["auto_cleanup_days"],
            int(SETTING_DEFAULTS["auto_refresh_on_page_open"]),
            SETTING_DEFAULTS["default_trend_hours"],
            SETTING_DEFAULTS["mc_lookup_timeout_seconds"],
            SETTING_DEFAULTS["mc_status_timeout_seconds"],
            SETTING_DEFAULTS["max_concurrent_queries"],
            now,
        ),
    )


def validate_settings_patch(
    patch: Mapping[str, Any], *, scope: str, reset_keys: Sequence[str] = ()
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if scope not in {"global", "group"}:
        raise SettingsValidationError("scope 必须是 global 或 group")
    if not isinstance(patch, Mapping):
        raise SettingsValidationError("patch 必须是对象")
    allowed = GLOBAL_SETTING_KEYS if scope == "global" else GROUP_OVERRIDE_KEYS
    if any(not isinstance(key, str) for key in patch):
        raise SettingsValidationError("配置字段名必须是字符串")
    unknown = set(patch) - allowed
    if unknown:
        raise SettingsValidationError(f"未知或不允许的配置字段: {', '.join(sorted(unknown))}")
    if isinstance(reset_keys, (str, bytes)) or any(
        not isinstance(key, str) for key in reset_keys
    ):
        raise SettingsValidationError("reset_keys 必须是字段名列表")
    resets = tuple(dict.fromkeys(reset_keys))
    invalid_resets = set(resets) - allowed
    if invalid_resets:
        raise SettingsValidationError(f"不能重置的配置字段: {', '.join(sorted(invalid_resets))}")
    overlap = set(patch) & set(resets)
    if overlap:
        raise SettingsValidationError(f"字段不能同时更新和重置: {', '.join(sorted(overlap))}")
    if scope == "global" and resets:
        raise SettingsValidationError("全局配置不能 reset，必须显式设置默认值")

    normalized: dict[str, Any] = {}
    for key, value in patch.items():
        if key in _BOOL_KEYS:
            if type(value) is not bool:
                raise SettingsValidationError(f"{key} 必须是布尔值")
            normalized[key] = value
        elif key in _INT_KEYS:
            if type(value) is not int:
                raise SettingsValidationError(f"{key} 必须是整数")
            low, high = _RANGES[key]
            if not low <= value <= high:
                raise SettingsValidationError(f"{key} 必须在 {int(low)}-{int(high)} 之间")
            normalized[key] = value
        elif key in _FLOAT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingsValidationError(f"{key} 必须是数字")
            number = float(value)
            low, high = _RANGES[key]
            if not low <= number <= high:
                raise SettingsValidationError(f"{key} 必须在 {low:g}-{high:g} 之间")
            normalized[key] = number
    return normalized, resets


def get_global_settings_sync(conn: sqlite3.Connection) -> RuntimeSettings:
    row = conn.execute("SELECT * FROM runtime_global_settings WHERE singleton_id=1").fetchone()
    if row is None:
        migrate_schema_v1_to_v2(conn)
        row = conn.execute("SELECT * FROM runtime_global_settings WHERE singleton_id=1").fetchone()
    return _runtime_from_row(row)


def get_group_settings_sync(conn: sqlite3.Connection, group_id: str) -> GroupRuntimeSettings:
    row = conn.execute(
        "SELECT * FROM runtime_group_settings WHERE group_id=?", (group_id,)
    ).fetchone()
    return _group_from_row(group_id, row)


def get_effective_settings_sync(conn: sqlite3.Connection, group_id: str) -> EffectiveRuntimeSettings:
    global_settings = get_global_settings_sync(conn)
    group = get_group_settings_sync(conn, group_id)
    values = asdict(global_settings)
    for key in GROUP_OVERRIDE_KEYS:
        value = getattr(group, key)
        if value is not None:
            values[key] = value
    values["revision"] = max(global_settings.revision, group.revision)
    values["updated_at"] = max(global_settings.updated_at, group.updated_at)
    return EffectiveRuntimeSettings(
        **values,
        group_id=group_id,
        global_revision=global_settings.revision,
        group_revision=group.revision,
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    preview_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_settings_previews'"
    ).fetchone()
    if preview_table is not None:
        preview_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(runtime_settings_previews)")
        }
        if "global_revision" not in preview_columns:
            conn.execute(
                "ALTER TABLE runtime_settings_previews "
                "ADD COLUMN global_revision INTEGER NOT NULL DEFAULT 1"
            )
    return conn


async def _prepare(storage_arg: Any) -> tuple[Path, str]:
    from .json_operate import _prepared

    storage = await _prepared(storage_arg)
    return Path(storage.db_path), str(storage.group_id)


async def get_global_settings(storage_arg: Any) -> RuntimeSettings:
    db_path, _ = await _prepare(storage_arg)
    return await asyncio.to_thread(_read_settings_sync, db_path, "global", None)


async def get_group_settings(storage_arg: Any) -> GroupRuntimeSettings:
    db_path, group_id = await _prepare(storage_arg)
    return await asyncio.to_thread(_read_settings_sync, db_path, "group", group_id)


async def get_effective_settings(storage_arg: Any) -> EffectiveRuntimeSettings:
    db_path, group_id = await _prepare(storage_arg)
    return await asyncio.to_thread(_read_settings_sync, db_path, "effective", group_id)


def _read_settings_sync(db_path: Path, kind: str, group_id: Optional[str]):
    conn = _connect(db_path)
    try:
        if kind == "global":
            return get_global_settings_sync(conn)
        if kind == "group":
            return get_group_settings_sync(conn, str(group_id))
        return get_effective_settings_sync(conn, str(group_id))
    finally:
        conn.close()


def _apply_to_dataclass(current: Any, patch: Mapping[str, Any], reset_keys: Sequence[str]) -> Any:
    changes = dict(patch)
    for key in reset_keys:
        changes[key] = None
    return replace(current, **changes)


def _inheriting_groups(conn: sqlite3.Connection, key: str) -> tuple[str, ...]:
    rows = conn.execute(
        f"SELECT g.group_id FROM groups g LEFT JOIN runtime_group_settings r "
        f"ON r.group_id=g.group_id WHERE r.{key} IS NULL ORDER BY g.group_id"
    ).fetchall()
    return tuple(str(row["group_id"]) for row in rows)


def _prune_impact(conn: sqlite3.Connection, groups: Sequence[str], limit: int) -> tuple[int, int]:
    affected_servers = 0
    points = 0
    for group_id in groups:
        rows = conn.execute(
            "SELECT server_id, COUNT(*) AS total FROM trend_points WHERE group_id=? "
            "GROUP BY server_id HAVING total>?",
            (group_id, limit),
        ).fetchall()
        affected_servers += len(rows)
        points += sum(int(row["total"]) - limit for row in rows)
    return affected_servers, points


def _cleanup_count(conn: sqlite3.Connection, groups: Sequence[str], days: int) -> int:
    cutoff = int(time.time()) - days * 86400
    total = 0
    for group_id in groups:
        row = conn.execute(
            "SELECT COUNT(*) FROM (SELECT s.server_id, "
            "MAX(COALESCE(s.last_success_time, 0), COALESCE(MAX(t.ts), 0)) AS effective "
            "FROM servers s LEFT JOIN trend_points t ON t.group_id=s.group_id AND t.server_id=s.server_id "
            "WHERE s.group_id=? GROUP BY s.group_id, s.server_id HAVING effective < ?)",
            (group_id, cutoff),
        ).fetchone()
        total += int(row[0])
    return total


def _preview_sync(
    db_path: Path,
    group_id: str,
    scope: str,
    patch: Mapping[str, Any],
    reset_keys: Sequence[str],
    ttl_seconds: int,
) -> SettingsPreview:
    normalized, resets = validate_settings_patch(patch, scope=scope, reset_keys=reset_keys)
    conn = _connect(db_path)
    try:
        current = (
            get_global_settings_sync(conn)
            if scope == "global"
            else get_group_settings_sync(conn, group_id)
        )
        proposed = _apply_to_dataclass(current, normalized, resets)
        base_revision = current.revision
        global_revision = get_global_settings_sync(conn).revision
        affected_groups: tuple[str, ...] = ()
        affected_servers = 0
        prune_points = 0

        old_effective = get_effective_settings_sync(conn, group_id)
        old_max = old_effective.max_history_points
        new_max = old_max
        if "max_history_points" in normalized or "max_history_points" in resets:
            if scope == "global":
                new_max = int(proposed.max_history_points)
                history_groups = _inheriting_groups(conn, "max_history_points")
                old_max = current.max_history_points
            else:
                new_max = (
                    int(proposed.max_history_points)
                    if proposed.max_history_points is not None
                    else get_global_settings_sync(conn).max_history_points
                )
                history_groups = (group_id,)
            if new_max < old_max:
                affected_groups = history_groups
                affected_servers, prune_points = _prune_impact(conn, affected_groups, new_max)

        cleanup_groups = (group_id,)
        old_days = old_effective.auto_cleanup_days
        new_days = old_days
        if "auto_cleanup_days" in normalized or "auto_cleanup_days" in resets:
            if scope == "global":
                cleanup_groups = _inheriting_groups(conn, "auto_cleanup_days")
                old_days = current.auto_cleanup_days
                new_days = int(proposed.auto_cleanup_days)
            else:
                new_days = (
                    int(proposed.auto_cleanup_days)
                    if proposed.auto_cleanup_days is not None
                    else get_global_settings_sync(conn).auto_cleanup_days
                )
        before = _cleanup_count(conn, cleanup_groups, old_days)
        after = _cleanup_count(conn, cleanup_groups, new_days)

        preview_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + max(1, int(ttl_seconds))
        conn.execute(
            "DELETE FROM runtime_settings_previews WHERE expires_at <= ?", (now,)
        )
        conn.execute(
            "INSERT INTO runtime_settings_previews(preview_id, scope, group_id, base_revision, "
            "global_revision, patch_json, reset_keys_json, history_points_to_prune, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                scope,
                group_id if scope == "group" else None,
                base_revision,
                global_revision,
                json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                json.dumps(list(resets), separators=(",", ":")),
                prune_points,
                expires_at,
            ),
        )
        return SettingsPreview(
            preview_id=preview_id,
            scope=scope,
            group_id=group_id if scope == "group" else None,
            base_revision=base_revision,
            global_revision=global_revision,
            expires_at=expires_at,
            current=current,
            proposed=proposed,
            affected_groups=affected_groups,
            affected_servers=affected_servers,
            history_points_to_prune=prune_points,
            cleanup_candidates_before=before,
            cleanup_candidates_after=after,
        )
    finally:
        conn.close()


async def preview_settings_update(
    storage_arg: Any,
    patch: Mapping[str, Any],
    *,
    scope: str = "group",
    reset_keys: Sequence[str] = (),
    preview_ttl_seconds: int = PREVIEW_TTL_SECONDS,
) -> SettingsPreview:
    db_path, group_id = await _prepare(storage_arg)
    return await asyncio.to_thread(
        _preview_sync, db_path, group_id, scope, patch, reset_keys, preview_ttl_seconds
    )


def _trim_groups(conn: sqlite3.Connection, groups: Sequence[str], limit: int) -> int:
    removed = 0
    for group_id in groups:
        before = conn.total_changes
        conn.execute(
            "DELETE FROM trend_points WHERE group_id=? AND (server_id, ts) IN ("
            "SELECT server_id, ts FROM (SELECT server_id, ts, ROW_NUMBER() OVER ("
            "PARTITION BY server_id ORDER BY ts DESC) AS rn FROM trend_points WHERE group_id=?) "
            "WHERE rn>?)",
            (group_id, group_id, limit),
        )
        removed += conn.total_changes - before
    return removed


def _apply_sync(
    db_path: Path,
    group_id: str,
    scope: str,
    patch: Mapping[str, Any],
    expected_revision: int,
    reset_keys: Sequence[str],
    preview_id: Optional[str],
    confirm_history_prune: bool,
    expected_history_points_to_prune: Optional[int],
) -> SettingsApplyResult:
    normalized, resets = validate_settings_patch(patch, scope=scope, reset_keys=reset_keys)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = (
            get_global_settings_sync(conn)
            if scope == "global"
            else get_group_settings_sync(conn, group_id)
        )
        if current.revision != int(expected_revision):
            raise SettingsRevisionConflict(
                f"配置 revision 已变化，当前为 {current.revision}，请求为 {expected_revision}"
            )
        proposed = _apply_to_dataclass(current, normalized, resets)
        global_settings = get_global_settings_sync(conn)
        preview = None
        if preview_id is not None:
            preview = conn.execute(
                "SELECT * FROM runtime_settings_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            expected_patch = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
            expected_resets = json.dumps(list(resets), separators=(",", ":"))
            if (
                preview is None
                or int(preview["expires_at"]) <= int(time.time())
                or preview["scope"] != scope
                or preview["group_id"] != (group_id if scope == "group" else None)
                or int(preview["base_revision"]) != current.revision
                or int(preview["global_revision"]) != global_settings.revision
                or preview["patch_json"] != expected_patch
                or preview["reset_keys_json"] != expected_resets
            ):
                raise SettingsPreviewExpired("设置预览已过期或与当前请求不匹配")

        old_effective = get_effective_settings_sync(conn, group_id)
        old_max = old_effective.max_history_points
        new_max = old_max
        prune_groups: tuple[str, ...] = ()
        if "max_history_points" in normalized or "max_history_points" in resets:
            if scope == "global":
                old_max = current.max_history_points
                new_max = int(proposed.max_history_points)
                prune_groups = _inheriting_groups(conn, "max_history_points")
            else:
                new_max = (
                    int(proposed.max_history_points)
                    if proposed.max_history_points is not None
                    else global_settings.max_history_points
                )
                prune_groups = (group_id,)
        lowering = new_max < old_max
        if lowering:
            if preview is None:
                raise SettingsPreviewRequired("降低 max_history_points 前必须先预览")
            _, current_prune_points = _prune_impact(conn, prune_groups, new_max)
            preview_prune_points = int(preview["history_points_to_prune"])
            if (
                preview_prune_points != current_prune_points
                or (
                    expected_history_points_to_prune is not None
                    and int(expected_history_points_to_prune) != current_prune_points
                )
            ):
                raise SettingsPreviewExpired("历史裁剪数量已变化，请重新预览")
            if current_prune_points > 0 and not confirm_history_prune:
                raise HistoryPruneConfirmationRequired("降低 max_history_points 需要确认立即裁剪")

        now = int(time.time())
        new_revision = current.revision + 1
        if scope == "global":
            values = asdict(proposed)
            columns = sorted(GLOBAL_SETTING_KEYS)
            assignments = ", ".join(f"{key}=?" for key in columns)
            conn.execute(
                f"UPDATE runtime_global_settings SET {assignments}, revision=?, updated_at=? "
                "WHERE singleton_id=1",
                [int(values[key]) if key in _BOOL_KEYS else values[key] for key in columns]
                + [new_revision, now],
            )
            saved: RuntimeSettings | GroupRuntimeSettings = replace(
                proposed, revision=new_revision, updated_at=now
            )
        else:
            values = asdict(proposed)
            columns = sorted(GROUP_OVERRIDE_KEYS)
            conn.execute(
                f"INSERT INTO runtime_group_settings(group_id, {', '.join(columns)}, revision, updated_at) "
                f"VALUES(?, {', '.join('?' for _ in columns)}, ?, ?) "
                f"ON CONFLICT(group_id) DO UPDATE SET "
                + ", ".join(f"{key}=excluded.{key}" for key in columns)
                + ", revision=excluded.revision, updated_at=excluded.updated_at",
                [group_id]
                + [int(values[key]) if key in _BOOL_KEYS and values[key] is not None else values[key] for key in columns]
                + [new_revision, now],
            )
            saved = replace(proposed, revision=new_revision, updated_at=now)
        pruned = _trim_groups(conn, prune_groups, new_max) if lowering else 0
        if preview_id:
            conn.execute("DELETE FROM runtime_settings_previews WHERE preview_id=?", (preview_id,))
        conn.commit()
        return SettingsApplyResult(saved, pruned)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def apply_settings_update(
    storage_arg: Any,
    patch: Mapping[str, Any],
    *,
    expected_revision: int,
    scope: str = "group",
    reset_keys: Sequence[str] = (),
    preview_id: Optional[str] = None,
    confirm_history_prune: bool = False,
    expected_history_points_to_prune: Optional[int] = None,
) -> SettingsApplyResult:
    db_path, group_id = await _prepare(storage_arg)
    return await asyncio.to_thread(
        _apply_sync,
        db_path,
        group_id,
        scope,
        patch,
        expected_revision,
        reset_keys,
        preview_id,
        confirm_history_prune,
        expected_history_points_to_prune,
    )
