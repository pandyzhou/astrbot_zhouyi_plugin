"""Vendored LivingMemory 2.3.6 runtime by lxfight.

Source: https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory
License: GNU AGPL v3; see ``livingmemory/LICENSE``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .component import LivingMemoryComponent

__all__ = ["LivingMemoryComponent"]


def __getattr__(name: str) -> Any:
    if name == "LivingMemoryComponent":
        from .component import LivingMemoryComponent

        return LivingMemoryComponent
    raise AttributeError(name)
