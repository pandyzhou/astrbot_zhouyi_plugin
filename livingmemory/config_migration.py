"""LivingMemory 配置文件迁移工具。

本模块不依赖 AstrBot，也不会在导入时自动执行迁移。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

LEGACY_CONFIG_FILENAME = "astrbot_plugin_livingmemory_config.json"
TARGET_CONFIG_FILENAME = "astrbot_zhouyi_plugin_config.json"


def get_config_paths(config_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    """根据 AstrBot 配置目录返回旧、新配置文件路径。"""

    directory = Path(config_dir)
    return (
        directory / LEGACY_CONFIG_FILENAME,
        directory / TARGET_CONFIG_FILENAME,
    )


def wrap_legacy_config(legacy_config: Mapping[str, Any]) -> dict[str, Any]:
    """将旧版根配置包装为新版 ``living_memory`` 配置。"""

    wrapped_config = {"enabled": True}
    wrapped_config.update(copy.deepcopy(dict(legacy_config)))
    wrapped_config["enabled"] = True
    return {"living_memory": wrapped_config}


def _fsync_directory(directory: Path) -> None:
    """在当前平台支持时同步目录元数据。"""

    if not hasattr(os, "O_DIRECTORY"):
        return

    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return

    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def migrate_config_file(config_dir: str | os.PathLike[str]) -> bool:
    """在目标不存在时迁移旧配置，成功迁移返回 ``True``。

    旧文件始终保留；目标已存在或旧文件不存在时返回 ``False``。
    """

    legacy_path, target_path = get_config_paths(config_dir)
    if target_path.exists() or not legacy_path.is_file():
        return False

    with legacy_path.open("r", encoding="utf-8-sig") as legacy_file:
        legacy_config = json.load(legacy_file)
    if not isinstance(legacy_config, dict):
        raise ValueError("旧 LivingMemory 配置的根节点必须是 JSON 对象")

    migrated_config = wrap_legacy_config(legacy_config)
    temporary_fd, temporary_name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(migrated_config, output, ensure_ascii=False, indent=4)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

        try:
            os.link(temporary_path, target_path)
        except FileExistsError:
            return False
        _fsync_directory(target_path.parent)
        return True
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
