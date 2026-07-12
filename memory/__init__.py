"""周易插件的长期记忆后端服务。"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .service import MemoryService

__all__ = ["MemoryService"]


def __getattr__(name: str) -> Any:
    if name == "MemoryService":
        from .service import MemoryService

        return MemoryService
    raise AttributeError(name)
