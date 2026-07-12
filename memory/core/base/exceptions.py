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
