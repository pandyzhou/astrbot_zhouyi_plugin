"""
自定义异常定义
"""


class MemoryException(Exception):
    """Memory 插件基础异常"""

    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code or "UNKNOWN_ERROR"
        super().__init__(self.message)


class InitializationError(MemoryException):
    """初始化错误"""

    def __init__(self, message: str):
        super().__init__(message, "INIT_ERROR")


class ProviderNotReadyError(MemoryException):
    """Provider未就绪错误"""

    def __init__(self, message: str = "Provider未就绪"):
        super().__init__(message, "PROVIDER_NOT_READY")


class DatabaseError(MemoryException):
    """数据库错误"""

    def __init__(self, message: str):
        super().__init__(message, "DATABASE_ERROR")


class RetrievalError(MemoryException):
    """检索错误"""

    def __init__(self, message: str):
        super().__init__(message, "RETRIEVAL_ERROR")


class MemoryProcessingError(MemoryException):
    """记忆处理错误"""

    def __init__(self, message: str):
        super().__init__(message, "MEMORY_PROCESSING_ERROR")


class ConfigurationError(MemoryException):
    """配置错误"""

    def __init__(self, message: str):
        super().__init__(message, "CONFIG_ERROR")


class ValidationError(MemoryException):
    """验证错误"""

    def __init__(self, message: str):
        super().__init__(message, "VALIDATION_ERROR")


class EvolvingMemoryNotFoundError(MemoryException):
    """可演化记忆对象不存在。"""

    def __init__(self, message: str = "记忆对象不存在"):
        super().__init__(message, "EVOLVING_MEMORY_NOT_FOUND")


class EvolvingMemoryAccessError(MemoryException):
    """owner 或 scope 访问边界校验失败。"""

    def __init__(self, message: str = "无权访问该记忆对象"):
        super().__init__(message, "EVOLVING_MEMORY_ACCESS_DENIED")


class EvolvingMemoryVersionConflictError(MemoryException):
    """乐观锁版本冲突。"""

    def __init__(self, item_id: str, expected_version: int, current_version: int):
        self.item_id = item_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"记忆对象版本冲突: {item_id} expected={expected_version} current={current_version}",
            "EVOLVING_MEMORY_VERSION_CONFLICT",
        )


class EvolvingMemoryIdempotencyError(MemoryException):
    """operation_key 已被不兼容操作占用。"""

    def __init__(self, message: str = "operation_key 已被其他操作使用"):
        super().__init__(message, "EVOLVING_MEMORY_IDEMPOTENCY_CONFLICT")
